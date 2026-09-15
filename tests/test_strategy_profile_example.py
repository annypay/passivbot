"""Pin the published strategy profile to its documented delta.

`configs/examples/trailing_martingale_twel100_ddf060.json` is the lower-tail variant of the
default long trailing-martingale profile. Its evidence is stored under
`backtests/binance/dd_tail_research_2026-09-15/` and summarised in `docs/strategy_profiles.md`.

These tests fail when the profile drifts from its documented three-parameter delta, when it
stops being loadable, or when its optimizer bounds no longer cover the values it ships.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config_utils import load_config

PROFILE = Path("configs/examples/trailing_martingale_twel100_ddf060.json")
TEMPLATE = Path("configs/examples/default_trailing_martingale_long.json")

# The only keys this profile is allowed to change relative to the default profile, with the
# template value and the profile value. Three are the behavioural delta; the fourth widens the
# optimizer bound so the deliberately lowered exposure cap stays inside its own search space.
EXPECTED_DELTA = {
    "bot.long.risk.total_wallet_exposure_limit": (1.5, 1.0),
    "bot.long.strategy.trailing_martingale.entry.double_down_factor": (0.94, 0.6),
    "bot.long.strategy.trailing_martingale.entry.threshold_base_pct": (0.019, 0.03),
    "optimize.bounds.long.risk.total_wallet_exposure_limit": ([1.5, 1.5, 0.01], [0.6, 1.5, 0.01]),
}

# Keys that carry behaviour; the optimizer bound above is not one of them.
BEHAVIOURAL_DELTA = {
    key: value for key, value in EXPECTED_DELTA.items() if not key.startswith("optimize.")
}


def flatten(node, prefix=""):
    flat = {}
    if isinstance(node, dict):
        for key, value in node.items():
            flat.update(flatten(value, f"{prefix}.{key}" if prefix else key))
    else:
        flat[prefix] = node
    return flat


def test_profile_is_loadable_and_keeps_the_grouped_shape():
    loaded = load_config(str(PROFILE), verbose=False)
    assert loaded["live"]["strategy_kind"] == "trailing_martingale"
    for pside in ("long", "short"):
        assert set(loaded["bot"][pside]) == {
            "forager",
            "hsl",
            "risk",
            "strategy",
            "unstuck",
        }


def test_profile_matches_the_template_except_for_the_documented_delta():
    profile = flatten(json.loads(PROFILE.read_text()))
    template = flatten(json.loads(TEMPLATE.read_text()))

    # Compare through the loader so int/float normalization cannot create false drift.
    loaded_profile = flatten(load_config(str(PROFILE), verbose=False))
    loaded_template = flatten(load_config(str(TEMPLATE), verbose=False))
    changed = {
        key: (loaded_template.get(key), loaded_profile.get(key))
        for key in sorted(set(loaded_profile) | set(loaded_template))
        if loaded_template.get(key) != loaded_profile.get(key)
    }

    # Loading injects provenance metadata; it is not part of the profile's own delta.
    changed = {
        key: value
        for key, value in changed.items()
        if not key.startswith("_") and key != "live.base_config_path"
    }

    assert set(changed) == set(EXPECTED_DELTA), (
        f"profile delta drifted: unexpected={sorted(set(changed) - set(EXPECTED_DELTA))} "
        f"missing={sorted(set(EXPECTED_DELTA) - set(changed))}"
    )
    for key, (expected_before, expected_after) in EXPECTED_DELTA.items():
        assert changed[key] == (expected_before, expected_after), key
    assert len(BEHAVIOURAL_DELTA) == 3, "behavioural delta must stay at three parameters"


@pytest.mark.parametrize("path", sorted(BEHAVIOURAL_DELTA))
def test_profile_optimizer_bounds_cover_the_shipped_value(path):
    loaded = load_config(str(PROFILE), verbose=False)
    node = loaded
    for part in path.split("."):
        node = node[part]
    value = float(node)

    # The profile keeps the template's bounds section; a bound that excludes the shipped
    # value would silently make the profile unusable for optimization.
    bounds = loaded["optimize"]["bounds"]
    key = path.split(".")[-1]
    side = path.split(".")[1]
    section = bounds[side]
    found = _find_bound(section, key)
    assert found is not None, f"no optimizer bound found for {path}"
    low, high = float(found[0]), float(found[1])
    assert low <= value <= high, f"{path}={value} outside bounds [{low}, {high}]"
    assert key in EXPECTED_BOUNDS, (
        f"{path} bounds were adjusted; record the new bounds in EXPECTED_BOUNDS. got {found}"
    )
    low, high = float(EXPECTED_BOUNDS[key][0]), float(EXPECTED_BOUNDS[key][1])
    assert low <= value <= high


# Bounds the profile is expected to ship, so a silent bounds edit is caught by review.
EXPECTED_BOUNDS = {
    "total_wallet_exposure_limit": [0.6, 1.5, 0.01],
    "double_down_factor": [0.2, 1.2, 0.01],
    "threshold_base_pct": [0.001, 0.035, 0.0001],
}


def _find_bound(node, key):
    if isinstance(node, dict):
        if key in node and isinstance(node[key], list) and len(node[key]) >= 2:
            return node[key]
        for value in node.values():
            found = _find_bound(value, key)
            if found is not None:
                return found
    return None