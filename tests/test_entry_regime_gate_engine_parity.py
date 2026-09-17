"""The engine and the shared Python table must answer the same question.

`SymbolInput.regime_eval_ts_ms` is the single evaluation point for the entry-regime
gate: the orchestrator reads the table it already carries in `bot_params` at that
instant. This module walks a real daily series through the shared Python table
(`src/entry_regime.py`) and then asks the engine for its verdict on both sides of every
boundary it produced. Anything but exact agreement is live/backtest drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from entry_regime import regime_table_from_daily_closes  # noqa: E402

from test_orchestrator_json_api import (  # noqa: E402
    compute,
    make_input,
    make_symbol,
)


@pytest.fixture(scope="module")
def pbr():
    """The real extension: a stub would make this parity check meaningless."""
    import passivbot_rust

    if getattr(passivbot_rust, "__is_stub__", False):
        pytest.fail("requires the real passivbot_rust extension; stub detected")
    return passivbot_rust


DAY_MS = 86_400_000
DAY0 = 1_700_000_000_000 // DAY_MS * DAY_MS
FAST_DAYS = 20
SLOW_DAYS = 50
N_DAYS = 400


def _daily_closes() -> list[float]:
    """A series that crosses both ways, so the table holds several boundaries."""
    closes: list[float] = []
    price = 100.0
    for day in range(N_DAYS):
        price *= 1.004 if (day // 40) % 2 == 0 else 0.996
        closes.append(price)
    return closes


def _table() -> tuple[list[int], list[int]]:
    closes = _daily_closes()
    days = [DAY0 + i * DAY_MS for i in range(len(closes))]
    return regime_table_from_daily_closes(days, closes, fast=FAST_DAYS, slow=SLOW_DAYS)


def _table_verdict(table: tuple[list[int], list[int]], ts: int) -> bool:
    transitions, regimes = table
    value = 0
    for boundary, regime in zip(transitions, regimes):
        if int(boundary) <= int(ts):
            value = int(regime)
        else:
            break
    return bool(value)


def _run(pbr, ts: int | None, table: tuple[list[int], list[int]], **flags) -> dict:
    transitions, regimes = table
    gate = {
        "enabled": not flags.get("disabled", False),
        "zero_is_on": False,
        "transition_ts": [int(value) for value in transitions],
        "regime": [int(value) for value in regimes],
        "block_initial": bool(flags.get("block_initial", True)),
        "block_reentry": bool(flags.get("block_reentry", True)),
    }
    symbol = make_symbol(0, bid=100.0, ask=101.0, long_bp={"entry_regime_gate": gate})
    if ts is not None:
        symbol["regime_eval_ts_ms"] = int(ts)
    return compute(pbr, make_input(balance=1_000.0, symbols=[symbol]))


def _entry_orders(out: dict) -> list[dict]:
    return [
        order
        for order in out.get("orders") or []
        if str(order.get("order_type") or "").startswith("entry_")
    ]


def _risk_off_instant(table: tuple[list[int], list[int]]) -> int:
    transitions, regimes = table
    for boundary, regime in zip(transitions, regimes):
        if int(regime) == 0:
            return int(boundary) + 60_000
    raise AssertionError("the synthetic series must contain a risk-off stretch")


def test_engine_entry_verdict_matches_the_shared_python_table_at_every_boundary(pbr):
    table = _table()
    transitions, _ = table
    assert len(transitions) >= 4, f"expected several boundaries, got {transitions}"

    for boundary in transitions:
        for ts in (int(boundary) - 1, int(boundary), int(boundary) + 60_000):
            out = _run(pbr, ts, table)
            expected = _table_verdict(table, ts)
            assert bool(_entry_orders(out)) is expected, (
                f"engine and table disagree at ts={ts} "
                f"(boundary={boundary}, table_verdict={expected})"
            )


def test_a_side_that_does_not_declare_the_flag_ignores_a_risk_off_table(pbr):
    table = _table()
    ts = _risk_off_instant(table)

    assert not _entry_orders(_run(pbr, ts, table))
    assert _entry_orders(_run(pbr, ts, table, block_initial=False))


def test_a_caller_without_a_timestamp_keeps_the_transmitted_flags(pbr):
    """Legacy producers (no `regime_eval_ts_ms`) must not be gated by a table."""
    table = _table()
    out = _run(pbr, None, table)
    assert _entry_orders(out), "an absent timestamp must leave the boolean contract in force"


def test_the_gate_is_what_blocks_the_entry_at_a_risk_off_instant(pbr):
    """Counterfactual: same table, same instant, only `enabled` differs.

    This is the entry-level version of the dry run's falling-leg comparison: the fake
    exchange harness cannot drive it (its staged planner refuses a fake clock days away
    from the wall clock), so the isolation property is asserted here against the engine.
    """
    table = _table()
    ts = _risk_off_instant(table)

    assert not _entry_orders(_run(pbr, ts, table)), "risk-off must block the entry"
    assert _entry_orders(_run(pbr, ts, table, disabled=True)), (
        "the same instant with the gate disabled must trade, which isolates the gate"
    )
