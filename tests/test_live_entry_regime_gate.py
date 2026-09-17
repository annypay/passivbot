"""Live wiring for the daily entry-regime gate.

The gate has two halves that must agree:

* `src/entry_regime.py` decides the verdict for a UTC day from closed daily closes.
* `src/passivbot.py` fetches those closes and publishes a per-symbol/side gate table
  into the Rust orchestrator input.

These tests pin the live contract: the published profile's gate resolves to its
documented parameters, the daily series is read as closed UTC days only, a symbol
whose evidence cannot be assembled is blocked rather than silently ungated, and the
master (`symbol=None`) params never carry a symbol's regime.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import numpy as np
import pytest

from passivbot import Passivbot

DAY_MS = 86_400_000
# 2024-01-01T00:00:00Z: a UTC midnight, so `ts % DAY_MS == 0` holds for the series.
DAY0 = 1_704_067_200_000

PROFILE = Path(
    "configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json"
)
BASE_PROFILE = Path("configs/examples/trailing_martingale_twel100_ddf060.json")

# The gate the published profile ships, and the only thing it adds to the base profile.
PUBLISHED_GATE = {
    "enabled": True,
    "sma_fast_days": 20,
    "sma_slow_days": 50,
    "confirm_days": 0,
    "block_initial": True,
    "block_reentry": True,
}
PUBLISHED_GATE_DELTA = {f"backtest.entry_regime_gate.{key}" for key in PUBLISHED_GATE}


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _gate_profile_config() -> dict:
    raw = json.loads(PROFILE.read_text(encoding="utf-8"))
    raw["coin_overrides"] = {}
    raw.setdefault("live", {})
    raw["live"].setdefault("approved_coins", {"long": ["BTC"], "short": []})
    return raw


def make_gate_bot(
    gate: dict | None = None,
    *,
    approved_long: list[str] | None = None,
    approved_short: list[str] | None = None,
    config: dict | None = None,
) -> Passivbot:
    bot = Passivbot.__new__(Passivbot)
    bot.config = config if config is not None else _gate_profile_config()
    if gate is not None:
        bot.config["backtest"]["entry_regime_gate"] = gate
    bot.coin_overrides = {}
    bot.config["live"]["approved_coins"] = {
        "long": ["BTC"] if approved_long is None else approved_long,
        "short": [] if approved_short is None else approved_short,
    }
    return bot


class FakeCandleManager:
    """Minimal stand-in for `CandlestickManager`'s daily read path."""

    def __init__(self, closes_by_symbol: dict[str, list[float]], *, day0: int = DAY0):
        self.closes_by_symbol = closes_by_symbol
        self.day0 = day0
        self.exchange = object()
        self.calls: list[dict] = []

    def _array(self, symbol: str) -> np.ndarray:
        closes = self.closes_by_symbol.get(symbol) or []
        dt = np.dtype(
            [("ts", "int64"), ("o", "float32"), ("h", "float32"),
             ("l", "float32"), ("c", "float32"), ("bv", "float32")]
        )
        out = np.zeros(len(closes), dtype=dt)
        for i, close in enumerate(closes):
            out[i]["ts"] = self.day0 + i * DAY_MS
            out[i]["o"] = out[i]["h"] = out[i]["l"] = out[i]["c"] = close
        return out

    async def get_candles(self, symbol, **kwargs):
        self.calls.append({"symbol": symbol, **kwargs})
        full = self._array(symbol)
        # Mirror the real manager: serve only the requested inclusive window.
        start_ts, end_ts = kwargs.get("start_ts"), kwargs.get("end_ts")
        if start_ts is not None:
            full = full[full["ts"] >= int(start_ts)]
        if end_ts is not None:
            full = full[full["ts"] <= int(end_ts)]
        return full


def rising_then_flat(n: int, *, start: float = 100.0, step: float = 1.0) -> list[float]:
    """Monotone rising closes: the fast SMA is above the slow SMA once both are defined."""
    return [start + step * i for i in range(n)]


def falling_closes(n: int, *, start: float = 300.0, step: float = 1.0) -> list[float]:
    """Monotone falling closes: the fast SMA stays below the slow SMA."""
    return [start - step * i for i in range(n)]


# --------------------------------------------------------------------------- #
# the published profile
# --------------------------------------------------------------------------- #


def test_published_profile_ships_the_documented_gate():
    raw = json.loads(PROFILE.read_text(encoding="utf-8"))
    assert raw["backtest"]["entry_regime_gate"] == PUBLISHED_GATE


def test_published_profile_adds_only_the_gate_to_the_lower_tail_profile():
    """The gated profile is the published profile plus one additive block."""

    def flatten(node, prefix=""):
        flat = {}
        if isinstance(node, dict):
            for key, value in node.items():
                flat.update(flatten(value, f"{prefix}.{key}" if prefix else key))
        else:
            flat[prefix] = node
        return flat

    base = flatten(json.loads(BASE_PROFILE.read_text(encoding="utf-8")))
    gated = flatten(json.loads(PROFILE.read_text(encoding="utf-8")))
    changed = {
        key
        for key in set(base) | set(gated)
        if base.get(key) != gated.get(key)
    }
    assert changed == PUBLISHED_GATE_DELTA


def test_published_gate_resolves_to_its_documented_parameters(monkeypatch):
    """Pin the resolved config, so a changed default cannot silently retune the profile."""
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    bot = make_gate_bot()
    assert bot._entry_regime_gate_config() == {
        "fast": 20,
        "slow": 50,
        "confirm_days": 0,
        "block_initial": True,
        "block_reentry": True,
    }


# --------------------------------------------------------------------------- #
# config reading
# --------------------------------------------------------------------------- #


def test_missing_gate_key_reads_as_disabled():
    bot = Passivbot.__new__(Passivbot)
    bot.config = {"bot": {}, "live": {}, "backtest": {}}
    bot.coin_overrides = {}
    assert bot._entry_regime_gate_config() is None


def test_disabled_gate_reads_as_absent():
    bot = make_gate_bot({"enabled": False, "sma_fast_days": 20, "sma_slow_days": 50})
    assert bot._entry_regime_gate_config() is None


@pytest.mark.parametrize(
    "gate",
    [
        {"enabled": True, "sma_fast_days": 50, "sma_slow_days": 20},
        {"enabled": True, "sma_fast_days": 20, "sma_slow_days": 20},
        {"enabled": True, "sma_fast_days": 0, "sma_slow_days": 50},
        {"enabled": True, "sma_slow_days": 50},
    ],
)
def test_degenerate_windows_are_rejected(gate):
    bot = make_gate_bot(gate)
    with pytest.raises(ValueError, match="sma_fast_days"):
        bot._entry_regime_gate_config()


def test_long_short_flip_mode_is_not_a_live_configuration():
    """`invert_for_short` stays a research mode; live must not trade it silently."""
    bot = make_gate_bot(
        {
            "enabled": True,
            "sma_fast_days": 20,
            "sma_slow_days": 50,
            "gate_mode": "invert_for_short",
        }
    )
    with pytest.raises(ValueError, match="gate_mode"):
        bot._entry_regime_gate_config()


def test_non_dict_gate_is_rejected():
    bot = make_gate_bot()
    bot.config["backtest"]["entry_regime_gate"] = ["enabled"]
    with pytest.raises(TypeError, match="must be a dict"):
        bot._entry_regime_gate_config()


# --------------------------------------------------------------------------- #
# reading closed daily candles
# --------------------------------------------------------------------------- #


async def test_daily_read_requests_closed_utc_days_only():
    bot = make_gate_bot()
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000  # midday inside day 150

    series = await bot._orchestrator_daily_closes("BTC", lookback_days=60, now_ms=now)

    call = cm.calls[0]
    assert call["timeframe"] == "1d"
    assert call["max_lookback_candles"] == 61
    # The range ends at the last day that has closed, never at the forming day.
    assert call["end_ts"] == DAY0 + 149 * DAY_MS
    assert call["start_ts"] == DAY0 + 149 * DAY_MS - 60 * DAY_MS
    assert series.depth == 61
    # Every returned row is a UTC midnight, and the forming day is absent.
    assert all(ts % DAY_MS == 0 for ts in series.day_ts)
    assert series.day_ts[-1] == DAY0 + 149 * DAY_MS
    assert now // DAY_MS * DAY_MS not in series.day_ts
    assert len(series.closes) == series.depth


async def test_daily_read_is_ordered_oldest_first():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(120)})
    now = DAY0 + 119 * DAY_MS + 60_000
    series = await bot._orchestrator_daily_closes("BTC", lookback_days=60, now_ms=now)
    assert series.day_ts == sorted(series.day_ts)
    assert series.closes == sorted(series.closes)


async def test_daily_read_rejects_an_unaligned_bucket():
    bot = make_gate_bot()

    class Misaligned(FakeCandleManager):
        def _array(self, symbol):
            out = super()._array(symbol)
            out["ts"] = out["ts"] + 3_600_000  # one hour past the UTC boundary
            return out

    bot.cm = Misaligned({"BTC": rising_then_flat(60)})
    with pytest.raises(RuntimeError, match="not aligned to a UTC day"):
        await (
            bot._orchestrator_daily_closes(
                "BTC", lookback_days=60, now_ms=DAY0 + 60 * DAY_MS
            )
        )


async def test_daily_read_requires_an_exchange_backed_manager():
    bot = make_gate_bot()
    cm = FakeCandleManager({"BTC": rising_then_flat(60)})
    cm.exchange = None
    bot.cm = cm
    with pytest.raises(RuntimeError, match="exchange-backed"):
        await (
            bot._orchestrator_daily_closes(
                "BTC", lookback_days=60, now_ms=DAY0 + 60 * DAY_MS
            )
        )


async def test_daily_read_requires_a_candlestick_manager():
    bot = make_gate_bot()
    bot.cm = None
    with pytest.raises(RuntimeError, match="candlestick manager"):
        await (
            bot._orchestrator_daily_closes(
                "BTC", lookback_days=60, now_ms=DAY0 + 60 * DAY_MS
            )
        )


async def test_daily_read_rejects_an_empty_series():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": []})
    with pytest.raises(RuntimeError, match="no closed daily candles"):
        await (
            bot._orchestrator_daily_closes(
                "BTC", lookback_days=60, now_ms=DAY0 + 60 * DAY_MS
            )
        )


async def test_daily_read_skips_non_finite_closes():
    bot = make_gate_bot()
    closes = rising_then_flat(60)
    closes[10] = float("nan")
    bot.cm = FakeCandleManager({"BTC": closes})
    series = await bot._orchestrator_daily_closes(
        "BTC", lookback_days=60, now_ms=DAY0 + 60 * DAY_MS
    )
    # The tape holds 60 closed days and one of them has no usable close.
    assert series.depth == 59
    assert DAY0 + 10 * DAY_MS not in series.day_ts
    assert len(series.closes) == series.depth
    assert all(np.isfinite(value) for value in series.closes)


# --------------------------------------------------------------------------- #
# publishing the table
# --------------------------------------------------------------------------- #


async def run_publish(bot, symbols, now):
    await bot._load_orchestrator_entry_regime_gate(symbols, int(now))
    return bot._orchestrator_entry_regime_gate_tables


async def test_warm_rising_series_publishes_a_risk_on_table():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000

    tables = await run_publish(bot, ["BTC"], now)

    payload = tables["BTC"]["long"]
    assert payload["enabled"] is True
    assert payload["zero_is_on"] is False
    assert payload["block_initial"] is True
    assert payload["block_reentry"] is True
    # One boundary at the start of the current UTC day carries the whole verdict.
    assert payload["transition_ts"] == [now // DAY_MS * DAY_MS]
    assert payload["regime"] == [1]
    assert not bot._orchestrator_entry_regime_gate_unavailable_symbols


async def test_falling_series_publishes_a_risk_off_table():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": falling_closes(200)})
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000

    tables = await run_publish(bot, ["BTC"], now)

    assert tables["BTC"]["long"]["regime"] == [0]


async def test_table_boundary_is_the_start_of_the_current_utc_day():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    # One minute after a UTC midnight: the boundary must be that midnight, not "now".
    now = DAY0 + 150 * DAY_MS + 60_000
    tables = await run_publish(bot, ["BTC"], now)
    assert tables["BTC"]["long"]["transition_ts"] == [DAY0 + 150 * DAY_MS]


async def test_short_arm_absent_from_approved_coins_gets_no_table():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    tables = await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert set(tables["BTC"]) == {"long"}


async def test_unapproved_symbol_gets_no_table_and_no_read():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"ETH": rising_then_flat(200)})
    bot.cm = cm
    tables = await run_publish(bot, ["ETH"], DAY0 + 150 * DAY_MS)
    assert tables == {}
    assert cm.calls == []


async def test_unavailable_evidence_publishes_an_explicit_risk_off_table():
    """Fail closed: a missing daily series blocks new risk instead of trading ungated."""
    bot = make_gate_bot(approved_long=["BTC", "ETH"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200), "ETH": []})
    tables = await run_publish(bot, ["BTC", "ETH"], DAY0 + 150 * DAY_MS)

    assert set(tables) == {"BTC", "ETH"}
    assert bot._orchestrator_entry_regime_gate_unavailable_symbols == {"ETH"}
    assert tables["BTC"]["long"]["regime"] == [1]
    # The unavailable symbol carries a real risk-off row, through the same rule a
    # computed risk-off day uses, so the engine blocks it.
    assert tables["ETH"]["long"]["enabled"] is True
    assert tables["ETH"]["long"]["regime"] == [0]
    assert tables["ETH"]["long"]["block_initial"] is True
    assert tables["ETH"]["long"]["block_reentry"] is True
    # ... and the pass is not cached as settled, so the read is retried next cycle.
    assert getattr(bot, "_orchestrator_entry_regime_gate_cache", None) is None


async def test_a_too_short_series_cannot_define_the_filter():
    """Before the slow window fills there is no evidence, and the verdict is risk-off."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(10)})
    tables = await run_publish(bot, ["BTC"], DAY0 + 10 * DAY_MS)
    assert tables["BTC"]["long"]["regime"] == [0]


async def test_cold_start_series_is_risk_off_not_an_error():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(2)})
    tables = await run_publish(bot, ["BTC"], DAY0 + 2 * DAY_MS)
    assert tables["BTC"]["long"]["regime"] == [0]
    assert not bot._orchestrator_entry_regime_gate_unavailable_symbols


async def test_disabled_gate_publishes_nothing_and_reads_nothing():
    bot = make_gate_bot(
        {"enabled": False, "sma_fast_days": 20, "sma_slow_days": 50},
        approved_long=["BTC"],
        approved_short=[],
    )
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm
    tables = await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert tables == {}
    assert cm.calls == []


async def test_side_without_block_flags_needs_no_daily_evidence():
    """A side that never consults the regime must not be failed by a missing series."""
    bot = make_gate_bot(
        {
            "enabled": True,
            "sma_fast_days": 20,
            "sma_slow_days": 50,
            "block_initial": False,
            "block_reentry": False,
        },
        approved_long=["BTC"],
        approved_short=[],
    )
    cm = FakeCandleManager({"BTC": []})
    bot.cm = cm
    tables = await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert tables == {}
    assert cm.calls == []
    assert not bot._orchestrator_entry_regime_gate_unavailable_symbols


async def test_republish_replaces_the_previous_table():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert bot._orchestrator_entry_regime_gate_tables["BTC"]["long"]["regime"] == [1]

    bot.cm = FakeCandleManager({"BTC": []})
    await run_publish(bot, ["BTC"], DAY0 + 151 * DAY_MS)
    # The stale risk-on verdict is gone; the symbol now carries an explicit risk-off row.
    assert bot._orchestrator_entry_regime_gate_tables["BTC"]["long"]["regime"] == [0]
    assert bot._orchestrator_entry_regime_gate_unavailable_symbols == {"BTC"}


async def test_lookback_asks_for_more_than_the_filter_needs():
    bot = make_gate_bot(
        {
            "enabled": True,
            "sma_fast_days": 10,
            "sma_slow_days": 30,
            "confirm_days": 3,
        },
        approved_long=["BTC"],
        approved_short=[],
    )
    cm = FakeCandleManager({"BTC": rising_then_flat(400)})
    bot.cm = cm
    now = DAY0 + 300 * DAY_MS + 12 * 3_600_000
    await run_publish(bot, ["BTC"], now)

    # max(60, 30 slow + 3 confirmation + 10 margin) = 60 days of evidence, and an
    # inclusive [start, end] window of 60 days spans 61 candles. Both bounds end at
    # the last closed day.
    call = cm.calls[0]
    assert call["max_lookback_candles"] == 61
    assert call["end_ts"] == DAY0 + 299 * DAY_MS
    assert call["start_ts"] == DAY0 + 299 * DAY_MS - 60 * DAY_MS


async def test_lookback_grows_past_the_floor_for_a_longer_slow_window():
    bot = make_gate_bot(
        {
            "enabled": True,
            "sma_fast_days": 20,
            "sma_slow_days": 200,
            "confirm_days": 5,
        },
        approved_long=["BTC"],
        approved_short=[],
    )
    cm = FakeCandleManager({"BTC": rising_then_flat(400)})
    bot.cm = cm
    now = DAY0 + 300 * DAY_MS + 12 * 3_600_000
    await run_publish(bot, ["BTC"], now)

    # 200 + 5 + 10 = 215 days, above the 60-day floor, so the floor is a floor only.
    assert cm.calls[0]["max_lookback_candles"] == 216


# --------------------------------------------------------------------------- #
# what the Rust side receives
# --------------------------------------------------------------------------- #


async def test_master_params_never_carry_a_symbol_regime():
    """Global params are shared by every symbol, so they cannot encode one symbol's day."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert bot._orchestrator_entry_regime_gate_tables

    payload = bot._entry_regime_gate_for_rust("long", None)
    assert payload == {
        "enabled": False,
        "zero_is_on": False,
        "transition_ts": [],
        "regime": [],
    }


async def test_symbol_params_carry_the_published_table():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    now = DAY0 + 150 * DAY_MS
    await run_publish(bot, ["BTC"], now)

    payload = bot._entry_regime_gate_for_rust("long", "BTC")
    assert payload["enabled"] is True
    assert payload["transition_ts"] == [now]
    assert payload["regime"] == [1]


async def test_symbol_without_a_table_reads_as_disabled():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

    payload = bot._entry_regime_gate_for_rust("short", "BTC")
    assert payload["enabled"] is False
    assert payload["transition_ts"] == []


async def test_panic_plan_clears_a_cached_table():
    """Panic closes and never opens, so it must not carry a stale regime."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert bot._orchestrator_entry_regime_gate_tables

    # The panic path clears the cache before building its own input dict.
    bot._orchestrator_entry_regime_gate_tables = {}
    assert bot._entry_regime_gate_for_rust("long", "BTC")["enabled"] is False


class _ParamsStub:
    """`_bot_params_to_rust_dict` needs bot values plus the gate side channel."""

    def __init__(self, gate_tables: dict, *, config: dict | None = None):
        self.values = {
            "forager_score_weights": {"volume": 1.0, "ema_readiness": 0.0, "volatility": 0.0},
            "hsl_tier_ratios": {"yellow": 0.5, "orange": 0.75},
            "risk_twel_enforcer_policy": "reduce_overweight",
            "risk_we_excess_allowance_mode": "bounded",
            "hsl_restart_after_red_policy": "threshold",
        }
        self._orchestrator_entry_regime_gate_tables = gate_tables
        self.config = config if config is not None else {}
        self.coin_overrides = {}

    def bot_value(self, _pside, key):
        if key == "hsl_tier_ratios.yellow":
            return self.values["hsl_tier_ratios"]["yellow"]
        if key == "hsl_tier_ratios.orange":
            return self.values["hsl_tier_ratios"]["orange"]
        if key in self.values:
            return self.values[key]
        return 0.0

    def bp(self, _pside, key, _symbol=None):
        return self.values.get(key, 0.0)

    def config_get(self, path, symbol=None):
        raise KeyError(path)

    # Reuse the production accessor so this test covers the real read path.
    _entry_regime_gate_for_rust = Passivbot._entry_regime_gate_for_rust


def test_bot_params_to_rust_dict_carries_the_gate_per_symbol():
    table = {
        "enabled": True,
        "zero_is_on": False,
        "transition_ts": [DAY0],
        "regime": [1],
        "block_initial": True,
        "block_reentry": True,
    }
    stub = _ParamsStub({"BTC/USDT:USDT": {"long": dict(table)}})

    per_symbol = Passivbot._bot_params_to_rust_dict(stub, "long", "BTC/USDT:USDT")
    assert per_symbol["entry_regime_gate"] == table

    master = Passivbot._bot_params_to_rust_dict(stub, "long", None)
    assert master["entry_regime_gate"]["enabled"] is False
    assert master["entry_regime_gate"]["transition_ts"] == []


def test_bot_params_to_rust_dict_emits_a_disabled_gate_when_absent():
    stub = _ParamsStub({})
    for symbol in (None, "BTC/USDT:USDT"):
        payload = Passivbot._bot_params_to_rust_dict(stub, "short", symbol)[
            "entry_regime_gate"
        ]
        assert payload == {
            "enabled": False,
            "zero_is_on": False,
            "transition_ts": [],
            "regime": [],
        }


async def test_emitted_table_satisfies_the_rust_validation_rules():
    """The shape live emits must survive Rust's `EntryRegimeGateConfig::validate`."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    payload = bot._entry_regime_gate_for_rust("long", "BTC")

    assert len(payload["transition_ts"]) == len(payload["regime"])
    assert all(
        a < b for a, b in zip(payload["transition_ts"], payload["transition_ts"][1:])
    )
    assert all(value in (0, 1) for value in payload["regime"])
    assert payload["transition_ts"], "an enabled gate needs a non-empty table"
    assert payload["block_initial"] or payload["block_reentry"]


async def test_emitted_table_matches_the_shared_read_path():
    """The published table must equal the verdict `src/entry_regime.py` computes."""
    from entry_regime import regime_flag_for_day

    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    closes = falling_closes(200)
    bot.cm = FakeCandleManager({"BTC": closes})
    now = DAY0 + 150 * DAY_MS
    await run_publish(bot, ["BTC"], now)

    expected = regime_flag_for_day(
        [DAY0 + i * DAY_MS for i in range(150)],
        closes[:150],
        now,
        fast=20,
        slow=50,
        confirm_days=0,
    )
    assert bot._entry_regime_gate_for_rust("long", "BTC")["regime"] == [
        1 if expected else 0
    ]


async def test_table_is_rebuilt_once_per_utc_day():
    """A daily verdict cannot change inside a day, so the planning path must not refetch."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm
    now = DAY0 + 150 * DAY_MS + 3_600_000

    await run_publish(bot, ["BTC"], now)
    assert len(cm.calls) == 1

    await run_publish(bot, ["BTC"], now + 60_000)
    assert len(cm.calls) == 1, "same UTC day must reuse the published table"

    await run_publish(bot, ["BTC"], now + DAY_MS)
    assert len(cm.calls) == 2, "a new UTC day must rebuild the table"


async def test_a_failed_symbol_retries_on_the_next_cycle():
    """An unavailable symbol is not cached as a permanent verdict."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"BTC": []})
    bot.cm = cm
    await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert bot._orchestrator_entry_regime_gate_unavailable_symbols == {"BTC"}

    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS + 60_000)
    assert bot._orchestrator_entry_regime_gate_unavailable_symbols == set()
    assert bot._orchestrator_entry_regime_gate_tables["BTC"]["long"]["regime"] == [1]


async def test_gate_verdict_event_reports_the_published_table():
    bot = make_gate_bot(approved_long=["BTC", "ETH"], approved_short=[])
    bot.cm = FakeCandleManager(
        {"BTC": rising_then_flat(200), "ETH": falling_closes(200)}
    )
    captured: list[dict] = []
    bot._emit_entry_regime_gate_verdict = lambda **kwargs: captured.append(kwargs)
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000

    await run_publish(bot, ["BTC", "ETH"], now)

    assert len(captured) == 1, "one verdict event per rebuilt table"
    payload = captured[0]
    assert payload["fast"] == 20
    assert payload["slow"] == 50
    assert payload["confirm_days"] == 0
    assert payload["day_start_ms"] == now // DAY_MS * DAY_MS
    assert payload["planning_ts_ms"] == int(now)
    assert payload["symbol_count"] == 2
    assert payload["risk_on_sides"] == 1
    assert payload["risk_off_sides"] == 1
    assert payload["unavailable_count"] == 0


async def test_the_event_reports_unavailable_symbols_separately():
    bot = make_gate_bot(approved_long=["BTC", "ETH"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200), "ETH": []})
    captured: list[dict] = []
    bot._emit_entry_regime_gate_verdict = lambda **kwargs: captured.append(kwargs)

    await run_publish(bot, ["BTC", "ETH"], DAY0 + 150 * DAY_MS)

    payload = captured[0]
    assert payload["unavailable_count"] == 1
    assert payload["unavailable_symbols"] == ["ETH"]
    # An unavailable side is reported as unavailable, not as a computed risk-off day.
    assert payload["risk_on_sides"] == 1
    assert payload["risk_off_sides"] == 0


async def test_no_event_without_a_gate():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.config["backtest"].pop("entry_regime_gate", None)
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    captured: list[dict] = []
    bot._emit_entry_regime_gate_verdict = lambda **kwargs: captured.append(kwargs)

    await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

    assert captured == []


def test_the_live_key_wins_over_the_backtest_block():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.config["live"]["entry_regime_gate"] = {
        "enabled": True,
        "sma_fast_days": 5,
        "sma_slow_days": 9,
    }
    _, source = bot._entry_regime_gate_declaration()
    assert source == "live.entry_regime_gate"
    resolved = bot._entry_regime_gate_config()
    assert (resolved["fast"], resolved["slow"]) == (5, 9)


def test_the_declared_backtest_block_stays_reachable_when_live_strips_it():
    """A live load removes the whole `backtest` subtree, and the published profile
    states the gate there, so the declaration has to resolve from the raw document."""
    bot = Passivbot.__new__(Passivbot)
    bot.config = {
        "bot": {"long": {}, "short": {}},
        "live": {"approved_coins": {"long": ["BTC"], "short": []}},
        "_raw_effective": {
            "backtest": {
                "entry_regime_gate": {
                    "enabled": True,
                    "sma_fast_days": 20,
                    "sma_slow_days": 50,
                }
            }
        },
    }
    _, source = bot._entry_regime_gate_declaration()
    assert source == "_raw_effective:backtest.entry_regime_gate"
    assert bot._entry_regime_gate_config() == {
        "fast": 20,
        "slow": 50,
        "confirm_days": 0,
        "block_initial": True,
        "block_reentry": True,
    }
    assert bot._entry_regime_gate_eval_ts_ms(1_700_000_000_000) == 1_700_000_000_000


def test_eval_timestamp_is_present_only_for_a_configured_gate():
    gated = make_gate_bot(approved_long=["BTC"], approved_short=[])
    assert gated._entry_regime_gate_eval_ts_ms(1_700_000_000_000) == 1_700_000_000_000

    ungated = Passivbot.__new__(Passivbot)
    ungated.config = _gate_profile_config()
    ungated.config["backtest"].pop("entry_regime_gate", None)
    assert ungated._entry_regime_gate_eval_ts_ms(1_700_000_000_000) is None


async def test_traded_universe_wins_over_a_backtest_only_universe():
    """`backtest.approved_coins` may be wider; the gate must follow what live trades."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.config["backtest"]["approved_coins"] = {"long": ["BTC", "ETH"], "short": []}
    cm = FakeCandleManager(
        {"BTC": rising_then_flat(200), "ETH": rising_then_flat(200)}
    )
    bot.cm = cm

    tables = await run_publish(bot, ["BTC", "ETH"], DAY0 + 150 * DAY_MS)

    assert set(tables) == {"BTC"}
    assert [call["symbol"] for call in cm.calls] == ["BTC"]


async def test_backtest_universe_is_used_when_the_traded_universe_is_absent():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    del bot.config["live"]["approved_coins"]
    bot.config["backtest"]["approved_coins"] = {"long": ["BTC"], "short": []}
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    tables = await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

    assert set(tables) == {"BTC"}


async def test_resolved_runtime_universe_wins_over_config_strings():
    """Symbol resolution and `ignored_coins` are already applied to the runtime set."""
    bot = make_gate_bot(approved_long=["BTC", "ETH"], approved_short=[])
    bot.approved_coins_minus_ignored_coins = {"long": {"BTC"}, "short": set()}
    cm = FakeCandleManager(
        {"BTC": rising_then_flat(200), "ETH": rising_then_flat(200)}
    )
    bot.cm = cm

    tables = await run_publish(bot, ["BTC", "ETH"], DAY0 + 150 * DAY_MS)

    assert set(tables) == {"BTC"}
    assert [call["symbol"] for call in cm.calls] == ["BTC"]


async def test_a_settled_empty_side_reads_no_evidence():
    """A resolved empty set is an answer; only an absent key falls back to config."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.approved_coins_minus_ignored_coins = {"long": set(), "short": set()}
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    tables = await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

    assert tables == {}
    assert cm.calls == []


async def test_an_unresolved_universe_falls_back_to_the_config_list():
    """An absent key means the runtime has not resolved a universe yet."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    tables = await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

    assert set(tables) == {"BTC"}


def test_an_unaligned_day_stamp_is_rejected():
    from entry_regime import regime_table_from_daily_closes

    days = [DAY0, DAY0 + 2 * DAY_MS]
    with pytest.raises(ValueError, match="UTC midnights"):
        regime_table_from_daily_closes(
            [days[0] + 3_600_000, days[1]], [1.0, 2.0], fast=2, slow=3
        )


def test_out_of_order_day_stamps_are_rejected():
    from entry_regime import regime_table_from_daily_closes

    with pytest.raises(ValueError, match="strictly ascending"):
        regime_table_from_daily_closes(
            [DAY0 + DAY_MS, DAY0], [1.0, 2.0], fast=2, slow=3
        )


def test_a_gap_in_the_closed_days_reads_as_risk_off():
    """A missing day is not evidence, so a window containing it stays undefined."""
    from entry_regime import regime_table_from_daily_closes

    # Enough rising days before and after, but with one closed day missing entirely.
    days = [DAY0 + i * DAY_MS for i in range(80) if i != 40]
    closes = [100.0 + i for i in range(80) if i != 40]
    transitions, regimes = regime_table_from_daily_closes(
        days, closes, fast=20, slow=50, confirm_days=0, exclude_forming_last_day=False
    )
    # The verdict is defined for days whose slow window is gap-free, and risk-off
    # for the days whose window spans the gap.
    assert len(transitions) == len(regimes)
    assert set(regimes) <= {0, 1}
    assert any(value == 0 for value in regimes), "the gap must not read as risk-on"


# --------------------------------------------------------------------------- #
# warm-up depth and the boot pre-warm
# --------------------------------------------------------------------------- #


async def test_the_published_gate_warms_sixty_days_and_decides_immediately():
    """The published 20/50 gate needs 50 completed days; the pass asks for 60."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000

    tables = await run_publish(bot, ["BTC"], now)

    assert cm.calls[0]["max_lookback_candles"] == 61
    assert tables["BTC"]["long"]["regime"] == [1]
    stats = bot._orchestrator_entry_regime_gate_stats
    assert stats["lookback_days"] == 60
    assert stats["required_days"] == 50
    assert stats["min_history_days"] == 61
    assert stats["max_missing_days"] == 0
    assert stats["risk_on_sides"] == 1


async def test_a_series_below_the_slow_window_reports_its_depth():
    """The verdict needs slow + confirm_days completed days; fewer is risk-off."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(49)})

    tables = await run_publish(bot, ["BTC"], DAY0 + 49 * DAY_MS)

    assert tables["BTC"]["long"]["regime"] == [0]
    assert not bot._orchestrator_entry_regime_gate_unavailable_symbols
    stats = bot._orchestrator_entry_regime_gate_stats
    assert stats["min_history_days"] == 49
    assert stats["required_days"] == 50
    assert stats["min_history_days"] < stats["required_days"]


async def test_a_daily_gap_inside_the_slow_window_is_visible_and_risk_off():
    """A missing day is not evidence, so the window spanning it stays undefined."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    closes = rising_then_flat(200)
    closes[140] = float("nan")
    bot.cm = FakeCandleManager({"BTC": closes})

    tables = await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS + 12 * 3_600_000)

    assert tables["BTC"]["long"]["regime"] == [0]
    stats = bot._orchestrator_entry_regime_gate_stats
    # The depth looks sufficient, so the gap count is what explains the risk-off row.
    assert stats["max_missing_days"] == 1
    assert stats["min_history_days"] == 60


async def test_the_regime_gate_log_line_matches_its_arguments():
    """A formatting handler must render the record: seven args for six placeholders
    once turned every live-gate test red in a full run."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        await run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS + 12 * 3_600_000)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    gate_records = [r for r in records if "[regime_gate]" in str(r.getMessage())]
    assert gate_records, "the pass reports what it published"
    # getMessage() performs the %-formatting, so a placeholder/argument mismatch raises.
    text = gate_records[-1].getMessage()
    assert "lookback_days=60" in text
    assert "required_days=50" in text
    assert "history_days_min=61" in text
    assert "missing_days_max=0" in text


async def test_prewarm_resolves_the_gate_before_the_first_cycle():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000
    bot.get_exchange_time = lambda: int(now)
    bot.approved_coins_minus_ignored_coins = {"long": {"BTC"}, "short": set()}
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    summary = await bot.prewarm_entry_regime_gate()

    assert summary["enabled"] is True
    assert summary["symbol_count"] == 1
    assert summary["risk_on_sides"] == 1
    assert summary["lookback_days"] == 60
    assert summary["required_days"] == 50
    assert summary["min_history_days"] == 61
    assert summary["unavailable_count"] == 0
    assert bot._orchestrator_entry_regime_gate_tables["BTC"]["long"]["regime"] == [1]
    # The settled pass is cached for the rest of the UTC day, so the first planning
    # cycle reuses it instead of fetching the series again.
    assert getattr(bot, "_orchestrator_entry_regime_gate_cache", None) is not None
    assert len(cm.calls) == 1


async def test_prewarm_retries_a_transient_read_failure_once():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000
    bot.get_exchange_time = lambda: int(now)
    bot.approved_coins_minus_ignored_coins = {"long": {"BTC"}, "short": set()}

    class _FlakyOnce(FakeCandleManager):
        def __init__(self):
            super().__init__({"BTC": rising_then_flat(200)})
            self.failures = 0

        async def get_candles(self, symbol, **kwargs):
            if self.failures == 0:
                self.failures += 1
                raise RuntimeError("transient daily read failure")
            return await super().get_candles(symbol, **kwargs)

    async def _no_sleep(seconds, *, stage):
        return None

    bot._sleep_unless_shutdown = _no_sleep
    cm = _FlakyOnce()
    bot.cm = cm

    summary = await bot.prewarm_entry_regime_gate()

    assert cm.failures == 1
    assert summary["unavailable_count"] == 0
    assert summary["risk_on_sides"] == 1
    assert bot._orchestrator_entry_regime_gate_tables["BTC"]["long"]["regime"] == [1]
    assert getattr(bot, "_orchestrator_entry_regime_gate_cache", None) is not None


async def test_prewarm_is_a_no_op_without_a_gate():
    bot = make_gate_bot(
        {"enabled": False, "sma_fast_days": 20, "sma_slow_days": 50},
        approved_long=["BTC"],
        approved_short=[],
    )
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    assert await bot.prewarm_entry_regime_gate() == {"enabled": False}
    assert cm.calls == []


async def test_prewarm_without_a_resolved_universe_reads_nothing():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.approved_coins_minus_ignored_coins = {}
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    summary = await bot.prewarm_entry_regime_gate()

    assert summary["enabled"] is True
    assert summary["symbol_count"] == 0
    assert cm.calls == []
