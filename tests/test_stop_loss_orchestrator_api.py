"""Stop-loss at the live boundary: the orchestrator's JSON API emits a stop-loss close.

The live planner hands the orchestrator a JSON symbol input and places whatever close orders come
back; that path goes through `passivbot_rust::bot_params_from_dict` and the same orchestrator the
backtest uses. These tests drive it directly with an armed `bot.long.stop_loss`, so the new key and
the new per-symbol `last_stop_loss_fill_timestamp_ms` field are proven to cross the bridge and to
change order output - not merely to exist in the config surface.

The exchange-facing half of a full fake-live run is deliberately out of scope here: the live
placement layer carries the order type as an opaque `pb_order_type` string, and this feature adds no
new placement shape beyond an ordinary reduce-only close. What a fake-live tape would have added on
top is the tape itself, not this contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for candidate in (REPO / "src", Path(__file__).resolve().parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import passivbot_rust as pbr  # noqa: E402
from test_orchestrator_json_api import (  # noqa: E402
    bot_params_pair,
    compute,
    make_input,
    make_symbol,
)

STOP_LEVEL_PCT = 0.15
COOLDOWN_MINUTES = 1440.0


def armed_symbol(*, bid: float, ask: float, position_price: float = 100.0, size: float = 1.0):
    """One long position with the stop loss armed at 15% below its average entry."""
    symbol = make_symbol(
        0,
        bid=bid,
        ask=ask,
        long_pos_size=size,
        long_pos_price=position_price,
        long_bp={
            "stop_loss_enabled": True,
            "stop_loss_pct_from_avg_entry": STOP_LEVEL_PCT,
            "stop_loss_cooldown_minutes": COOLDOWN_MINUTES,
            "stop_loss_order_type": "market",
        },
    )
    return symbol


def orders_of(result: dict, pside: str = "long") -> list[dict]:
    """The orchestrator returns one flat `orders` list; filter it by position side."""
    return [
        order
        for order in (result.get("orders") or [])
        if order and str(order.get("pside", "")).lower() == pside
    ]


def stop_loss_orders(result: dict, pside: str = "long") -> list[dict]:
    return [
        order
        for order in orders_of(result, pside)
        if str(order.get("order_type", "")).startswith("close_stop_loss")
    ]


def test_stop_loss_close_is_emitted_at_the_level() -> None:
    # 85 is exactly 15% below the 100 average entry: the level itself must trigger.
    result = compute(pbr, make_input(balance=1_000.0, symbols=[armed_symbol(bid=85.0, ask=85.0)]))
    stop_orders = stop_loss_orders(result)
    assert len(stop_orders) == 1, result
    order = stop_orders[0]
    assert order["order_type"] == "close_stop_loss_long"
    assert order["qty"] == -1.0
    # No entries on the triggering decision: adding and exiting in the same minute is churn.
    assert all(float(o["qty"]) < 0.0 for o in orders_of(result)), result


def test_stop_loss_is_not_emitted_above_the_level() -> None:
    # 86 is above the 85 level: the stop stays quiet and the position is not force-closed.
    result = compute(pbr, make_input(balance=1_000.0, symbols=[armed_symbol(bid=86.0, ask=86.0)]))
    assert stop_loss_orders(result) == [], result


def test_disabled_stop_loss_emits_nothing() -> None:
    symbol = make_symbol(
        0, bid=80.0, ask=80.0, long_pos_size=1.0, long_pos_price=100.0
    )
    symbol["long"]["bot_params"]["stop_loss_enabled"] = False
    result = compute(pbr, make_input(balance=1_000.0, symbols=[symbol]))
    assert stop_loss_orders(result) == [], result


def test_short_side_is_mirrored() -> None:
    # The short pside needs a real budget: a zero `total_wallet_exposure_limit` resolves the side to
    # Manual mode, and Manual deliberately gets no stop-loss order (the operator owns the position).
    symbol = make_symbol(
        0,
        bid=115.0,
        ask=115.0,
        short_pos_size=-1.0,
        short_pos_price=100.0,
        short_bp={
            "n_positions": 1,
            "total_wallet_exposure_limit": 1.0,
            "stop_loss_enabled": True,
            "stop_loss_pct_from_avg_entry": STOP_LEVEL_PCT,
            "stop_loss_cooldown_minutes": COOLDOWN_MINUTES,
            "stop_loss_order_type": "market",
        },
    )
    result = compute(
        pbr,
        make_input(
            balance=1_000.0,
            global_bp=bot_params_pair(
                short_overrides={"n_positions": 1, "total_wallet_exposure_limit": 1.0}
            ),
            symbols=[symbol],
        ),
    )
    stop_orders = stop_loss_orders(result, "short")
    assert len(stop_orders) == 1, result
    assert stop_orders[0]["order_type"] == "close_stop_loss_short"
    assert stop_orders[0]["qty"] == 1.0


def test_cooldown_anchor_blocks_adds_but_not_closes() -> None:
    """A stop-loss fill one minute ago suppresses adds for 1440 minutes; closes are unaffected."""
    symbol = armed_symbol(bid=110.0, ask=110.0)
    inp = make_input(balance=1_000.0, symbols=[symbol])
    inp["timestamp_ms"] = 120_000
    inp["symbols"][0]["long"]["last_stop_loss_fill_timestamp_ms"] = 60_000
    result = compute(pbr, inp)
    assert stop_loss_orders(result) == [], result
    assert all(float(o["qty"]) < 0.0 for o in orders_of(result)), result

    # Past the cooldown the same input may add again: the block is a window, not a latch.
    inp["timestamp_ms"] = 60_000 + int(COOLDOWN_MINUTES * 60_000) + 1
    reopened = compute(pbr, inp)
    assert any(float(o["qty"]) > 0.0 for o in orders_of(reopened)), reopened


def test_limit_tier_prices_at_the_level_market_tier_at_the_touch() -> None:
    level = 100.0 * (1.0 - STOP_LEVEL_PCT)
    limit_symbol = armed_symbol(bid=80.0, ask=80.0)
    limit_symbol["long"]["bot_params"]["stop_loss_order_type"] = "limit"
    limit_result = compute(pbr, make_input(balance=1_000.0, symbols=[limit_symbol]))
    limit_order = stop_loss_orders(limit_result)[0]
    assert abs(float(limit_order["price"]) - level) < 1e-6, limit_order

    market_result = compute(pbr, make_input(balance=1_000.0, symbols=[armed_symbol(bid=80.0, ask=80.0)]))
    market_order = stop_loss_orders(market_result)[0]
    # The market tier prices at the executable touch (below the level) and asks for market
    # execution, which is the tier that actually exits; the limit tier rests at the level.
    assert float(market_order["price"]) < level, market_order
    assert market_order["execution_type"] == "market", market_order
    assert market_order["execution_priority"] == "risk_critical", market_order
