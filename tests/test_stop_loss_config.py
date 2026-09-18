"""Config-surface tests for the opt-in stop loss: ``bot.<pside>.stop_loss``.

The grouped block is the declared form; the four ``stop_loss_*`` spellings are its flat aliases and
are what the live producer reads through ``self.bp(...)``. These tests pin both directions and the
fail-closed rejections, and they prove a config frozen before the key existed still loads.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from config.load import prepare_config
from config.optimize_bounds import get_optimize_bounds_defaults
from config.overrides import parse_overrides
from config.schema import get_template_config
from config.shared_bot import get_grouped_bot_value
from config_utils import load_config

REPO = Path(__file__).resolve().parents[1]
FROZEN_ARM_CONFIG = (
    REPO
    / "backtests/binance/g4_twe300_risk_optimization_2026-09-17"
    / "artifacts/g4_a_allow000__3y.config.json"
)

FLAT_KEYS = (
    "stop_loss_enabled",
    "stop_loss_pct_from_avg_entry",
    "stop_loss_cooldown_minutes",
    "stop_loss_order_type",
)
GROUP_DEFAULTS = {
    "cooldown_minutes": 1440.0,
    "enabled": False,
    "order_type": "market",
    "pct_from_avg_entry": 0.15,
}
RESOLVED_DEFAULTS = {
    "stop_loss_enabled": False,
    "stop_loss_pct_from_avg_entry": 0.15,
    "stop_loss_cooldown_minutes": 1440.0,
    "stop_loss_order_type": "market",
}
DECLARED_GROUP = {
    "cooldown_minutes": 30.0,
    "enabled": True,
    "order_type": "limit",
    "pct_from_avg_entry": 0.2,
}
DECLARED_RESOLVED = {
    "stop_loss_enabled": True,
    "stop_loss_pct_from_avg_entry": 0.2,
    "stop_loss_cooldown_minutes": 30.0,
    "stop_loss_order_type": "limit",
}
PSIDES = ("long", "short")


def _resolved(config, pside):
    side = config["bot"][pside]
    return {key: get_grouped_bot_value(side, key) for key in FLAT_KEYS}


def _prepared(config):
    return prepare_config(deepcopy(config), verbose=False, log_config_transforms=False)


def _parse_overrides(overrides, *, mode="coin"):
    source = get_template_config()
    source["live"]["user"] = "tester"
    source["live"]["hsl_signal_mode"] = mode
    source["coin_overrides"] = deepcopy(overrides)
    prepared = prepare_config(source, verbose=False, log_config_transforms=False)
    return parse_overrides(
        prepared,
        verbose=False,
        override_loader=lambda config, coin: {},
        symbol_normalizer=lambda coin: coin,
    )


def _load_frozen_with(pside, block, tmp_path):
    raw = json.loads(FROZEN_ARM_CONFIG.read_text(encoding="utf-8"))
    raw["bot"][pside]["stop_loss"] = deepcopy(block)
    path = tmp_path / f"declared_{pside}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(path, verbose=False)


# --------------------------------------------------------------------------------------
# Defaults and backwards compatibility
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("pside", PSIDES)
def test_template_defaults_are_default_off(pside):
    template = get_template_config()

    assert template["bot"][pside]["stop_loss"] == GROUP_DEFAULTS
    assert _resolved(_prepared(template), pside) == RESOLVED_DEFAULTS


@pytest.mark.parametrize("pside", PSIDES)
def test_frozen_round_d_arm_still_loads_and_gains_default_off_stop_loss(pside):
    if not FROZEN_ARM_CONFIG.exists():
        pytest.skip(f"frozen arm config is not present: {FROZEN_ARM_CONFIG}")

    loaded = load_config(FROZEN_ARM_CONFIG, verbose=False)

    assert loaded["bot"][pside]["stop_loss"] == GROUP_DEFAULTS
    assert _resolved(loaded, pside) == RESOLVED_DEFAULTS


# --------------------------------------------------------------------------------------
# A declared group survives the loader, in both spellings
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("pside", PSIDES)
def test_declared_group_survives_load_config(pside, tmp_path):
    loaded = _load_frozen_with(pside, DECLARED_GROUP, tmp_path)

    assert loaded["bot"][pside]["stop_loss"] == DECLARED_GROUP
    assert _resolved(loaded, pside) == DECLARED_RESOLVED


@pytest.mark.parametrize("pside", PSIDES)
def test_flat_declaration_lands_in_the_grouped_block(pside):
    template = get_template_config()
    template["bot"][pside].update(DECLARED_RESOLVED)

    prepared = _prepared(template)

    assert prepared["bot"][pside]["stop_loss"] == DECLARED_GROUP
    assert _resolved(prepared, pside) == DECLARED_RESOLVED


@pytest.mark.parametrize("pside", PSIDES)
def test_disabled_stop_loss_accepts_a_zero_distance(pside):
    template = get_template_config()
    template["bot"][pside]["stop_loss"] = {
        "enabled": False,
        "pct_from_avg_entry": 0.0,
        "cooldown_minutes": 0.0,
        "order_type": "market",
    }

    prepared = _prepared(template)

    assert _resolved(prepared, pside)["stop_loss_enabled"] is False
    assert _resolved(prepared, pside)["stop_loss_pct_from_avg_entry"] == 0.0


# --------------------------------------------------------------------------------------
# Fail-closed rejections
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "message"),
    [
        (
            {
                "enabled": True,
                "pct_from_avg_entry": 0.0,
                "cooldown_minutes": 30.0,
                "order_type": "market",
            },
            "pct_from_avg_entry must be > 0",
        ),
        (
            {
                "enabled": True,
                "pct_from_avg_entry": -0.1,
                "cooldown_minutes": 30.0,
                "order_type": "market",
            },
            "pct_from_avg_entry must be finite and >= 0",
        ),
        (
            {
                "enabled": True,
                "pct_from_avg_entry": 0.15,
                "cooldown_minutes": 30.0,
                "order_type": "stop",
            },
            "order_type must be one of {limit, market}",
        ),
        (
            {
                "enabled": True,
                "pct_from_avg_entry": 0.15,
                "cooldown_minutes": -1.0,
                "order_type": "market",
            },
            "cooldown_minutes must be finite and >= 0",
        ),
    ],
)
def test_invalid_group_is_rejected(block, message):
    template = get_template_config()
    template["bot"]["long"]["stop_loss"] = dict(block)

    with pytest.raises(ValueError, match=message.replace("{", r"\{").replace("}", r"\}")):
        _prepared(template)


def test_declared_group_is_rejected_by_load_config_too(tmp_path):
    raw = json.loads(FROZEN_ARM_CONFIG.read_text(encoding="utf-8"))
    raw["bot"]["long"]["stop_loss"] = {
        "enabled": True,
        "pct_from_avg_entry": 0.0,
        "cooldown_minutes": 30.0,
        "order_type": "market",
    }
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="pct_from_avg_entry must be > 0"):
        load_config(path, verbose=False)


@pytest.mark.parametrize(
    "leaf",
    ["enabled", "pct_from_avg_entry", "cooldown_minutes", "order_type"],
)
@pytest.mark.parametrize("pside", PSIDES)
def test_coin_override_of_every_stop_loss_leaf_is_allowed(pside, leaf):
    value = {
        "enabled": True,
        "pct_from_avg_entry": 0.25,
        "cooldown_minutes": 60.0,
        "order_type": "limit",
    }[leaf]

    parsed = _parse_overrides(
        {"BTC": {"bot": {pside: {"stop_loss": {leaf: value}}}}}
    )

    assert (
        parsed["coin_overrides"]["BTC"]["bot"][pside]["stop_loss"][leaf] == value
    )


def test_flat_coin_override_spelling_is_accepted():
    parsed = _parse_overrides(
        {"BTC": {"bot": {"long": {"stop_loss_pct_from_avg_entry": 0.25}}}}
    )

    assert (
        parsed["coin_overrides"]["BTC"]["bot"]["long"]["stop_loss"][
            "pct_from_avg_entry"
        ]
        == 0.25
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"stop_loss_typo": 1.0},
        {"stop_loss": {"typo_leaf": 1.0}},
        {"stop_loss": {"pct": 0.2}},
    ],
)
def test_unknown_override_leaf_is_rejected(patch):
    with pytest.raises(ValueError, match="is not overridable"):
        _parse_overrides({"BTC": {"bot": {"long": patch}}})


@pytest.mark.parametrize("group", ["risk", "unstuck", "stop_loss"])
def test_unknown_group_leaf_in_a_config_file_is_dropped(group):
    """The loader drops unknown leaves under every bot group alike; none reach the engine.

    This pins the *existing*, group-agnostic behaviour rather than inventing a stop-loss-only rule:
    a misspelled leaf cannot silently change what the engine reads, but it is also not reported.
    """
    template = get_template_config()
    template["bot"]["long"].setdefault(group, {})["typo_leaf"] = 1.0

    prepared = _prepared(template)

    assert "typo_leaf" not in (prepared["bot"]["long"].get(group) or {})
    assert _resolved(prepared, "long") == RESOLVED_DEFAULTS


def test_stop_loss_is_not_an_optimize_bound():
    bounds = get_optimize_bounds_defaults()

    assert "stop_loss" not in json.dumps(bounds, sort_keys=True)
    for pside in PSIDES:
        assert "stop_loss" not in bounds[pside]
