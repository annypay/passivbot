"""The entry-regime gate must survive config hydration and sanitizing.

This is a regression test for a real defect: `clean_config` rebuilds a config from
the schema template, and `_clean_with_template` only visits keys the template
declares. `backtest.entry_regime_gate` was therefore dropped on the CLI path — the
path that writes the artifact bundle a deep-analysis report is rendered from — while
the in-process screening path, which hands the raw config straight to
`build_backtest_payload`, kept working. The result was a gated bundle that silently
ran ungated.

Two declarations keep it: the template key, so the subtree is visited, and
`PARTIALLY_OPEN_CONFIG_PATHS`, so the subtree's own keys are preserved rather than
rebuilt from the template.

Both spellings are covered: `backtest.entry_regime_gate` is what the published profile
and every tracked evidence bundle state, and `live.entry_regime_gate` is the live-native
key. A live load removes the whole `backtest` subtree, so the live path resolves the
former from the raw document (see `tests/test_live_entry_regime_gate.py`).
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from config.hydrate import PARTIALLY_OPEN_CONFIG_PATHS  # noqa: E402
from config.load import prepare_config  # noqa: E402
from config_utils import clean_config, get_template_config  # noqa: E402

GATE = {"enabled": True, "sma_fast_days": 20, "sma_slow_days": 50}
BACKTEST_GATE_PATH = ("backtest", "entry_regime_gate")
LIVE_GATE_PATH = ("live", "entry_regime_gate")
GATE_PATHS = (BACKTEST_GATE_PATH, LIVE_GATE_PATH)


def minimal_config(gate: dict | None, *, gate_path: tuple[str, str] = BACKTEST_GATE_PATH) -> dict:
    """A config carrying only what the gate path needs."""
    config = {
        "backtest": {
            "exchanges": ["binance"],
            "coins": {"binance": ["BTC"]},
            "start_date": "2024-01-01",
            "end_date": "2024-02-01",
        },
        "bot": {"long": {}, "short": {}},
        "live": {"approved_coins": {"long": ["BTC"], "short": []}},
    }
    if gate is not None:
        section, key = gate_path
        config.setdefault(section, {})[key] = deepcopy(gate)
    return config


def gate_at(config: dict, gate_path: tuple[str, str]):
    section, key = gate_path
    return (config.get(section) or {}).get(key)


@pytest.mark.parametrize("gate_path", GATE_PATHS)
def test_gate_path_is_declared_partially_open(gate_path: tuple[str, str]) -> None:
    assert gate_path in PARTIALLY_OPEN_CONFIG_PATHS


@pytest.mark.parametrize("gate_path", GATE_PATHS)
def test_template_declares_the_key(gate_path: tuple[str, str]) -> None:
    section, key = gate_path
    assert key in get_template_config()[section]


@pytest.mark.parametrize("gate_path", GATE_PATHS)
def test_clean_config_preserves_the_gate(gate_path: tuple[str, str]) -> None:
    cleaned = clean_config(minimal_config(GATE, gate_path=gate_path))
    assert gate_at(cleaned, gate_path) == GATE


@pytest.mark.parametrize("gate_path", GATE_PATHS)
def test_clean_config_preserves_an_empty_table_without_inventing_one(
    gate_path: tuple[str, str],
) -> None:
    """A disabled gate must stay disabled rather than gaining a default."""
    disabled = {"enabled": False}
    cleaned = clean_config(minimal_config(disabled, gate_path=gate_path))
    assert gate_at(cleaned, gate_path) == disabled


@pytest.mark.parametrize("gate_path", GATE_PATHS)
def test_hydration_keeps_the_gate(gate_path: tuple[str, str]) -> None:
    prepared = prepare_config(
        minimal_config(GATE, gate_path=gate_path),
        base_config_path=None,
        verbose=False,
        log_config_transforms=False,
        raw_snapshot=None,
    )
    assert gate_at(prepared, gate_path) == GATE


@pytest.mark.parametrize("gate_path", GATE_PATHS)
def test_hydration_without_a_gate_does_not_run_one(gate_path: tuple[str, str]) -> None:
    prepared = prepare_config(
        minimal_config(None, gate_path=gate_path),
        base_config_path=None,
        verbose=False,
        log_config_transforms=False,
        raw_snapshot=None,
    )
    gate = gate_at(prepared, gate_path)
    assert not gate or not gate.get("enabled")


@pytest.mark.parametrize("gate_path", GATE_PATHS)
def test_sanitized_dump_keeps_the_gate(gate_path: tuple[str, str]) -> None:
    """The dumped artifact config is what a report cites, so it must show the gate."""
    from config_utils import sanitize_prepared_config_for_dump

    prepared = prepare_config(
        minimal_config(GATE, gate_path=gate_path),
        base_config_path=None,
        verbose=False,
        log_config_transforms=False,
        raw_snapshot=None,
    )
    dumped = sanitize_prepared_config_for_dump(prepared)
    assert gate_at(dumped, gate_path) == GATE


@pytest.mark.parametrize("gate_path", GATE_PATHS)
@pytest.mark.parametrize("gate", [{"enabled": True, "sma_fast_days": 1, "sma_slow_days": 2}])
def test_arbitrary_gate_geometry_round_trips(
    gate: dict, gate_path: tuple[str, str]
) -> None:
    cleaned = clean_config(minimal_config(gate, gate_path=gate_path))
    assert gate_at(cleaned, gate_path) == gate
