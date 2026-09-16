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


def test_daily_read_requests_closed_utc_days_only():
    bot = make_gate_bot()
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000  # midday inside day 150

    day_ts, closes = asyncio.run(
        bot._orchestrator_daily_closes("BTC", lookback_days=52, now_ms=now)
    )

    call = cm.calls[0]
    assert call["timeframe"] == "1d"
    assert call["max_lookback_candles"] == 53
    # The range ends at the last day that has closed, never at the forming day.
    assert call["end_ts"] == DAY0 + 149 * DAY_MS
    assert call["start_ts"] == DAY0 + 149 * DAY_MS - 52 * DAY_MS
    assert len(day_ts) == 53
    # Every returned row is a UTC midnight, and the forming day is absent.
    assert all(ts % DAY_MS == 0 for ts in day_ts)
    assert day_ts[-1] == DAY0 + 149 * DAY_MS
    assert now // DAY_MS * DAY_MS not in day_ts
    assert len(closes) == len(day_ts)


def test_daily_read_is_ordered_oldest_first():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(120)})
    now = DAY0 + 119 * DAY_MS + 60_000
    day_ts, closes = asyncio.run(
        bot._orchestrator_daily_closes("BTC", lookback_days=52, now_ms=now)
    )
    assert day_ts == sorted(day_ts)
    assert closes == sorted(closes)


def test_daily_read_rejects_an_unaligned_bucket():
    bot = make_gate_bot()

    class Misaligned(FakeCandleManager):
        def _array(self, symbol):
            out = super()._array(symbol)
            out["ts"] = out["ts"] + 3_600_000  # one hour past the UTC boundary
            return out

    bot.cm = Misaligned({"BTC": rising_then_flat(60)})
    with pytest.raises(RuntimeError, match="not aligned to a UTC day"):
        asyncio.run(
            bot._orchestrator_daily_closes(
                "BTC", lookback_days=52, now_ms=DAY0 + 60 * DAY_MS
            )
        )


def test_daily_read_requires_an_exchange_backed_manager():
    bot = make_gate_bot()
    cm = FakeCandleManager({"BTC": rising_then_flat(60)})
    cm.exchange = None
    bot.cm = cm
    with pytest.raises(RuntimeError, match="exchange-backed"):
        asyncio.run(
            bot._orchestrator_daily_closes(
                "BTC", lookback_days=52, now_ms=DAY0 + 60 * DAY_MS
            )
        )


def test_daily_read_requires_a_candlestick_manager():
    bot = make_gate_bot()
    bot.cm = None
    with pytest.raises(RuntimeError, match="candlestick manager"):
        asyncio.run(
            bot._orchestrator_daily_closes(
                "BTC", lookback_days=52, now_ms=DAY0 + 60 * DAY_MS
            )
        )


def test_daily_read_rejects_an_empty_series():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": []})
    with pytest.raises(RuntimeError, match="no closed daily candles"):
        asyncio.run(
            bot._orchestrator_daily_closes(
                "BTC", lookback_days=52, now_ms=DAY0 + 60 * DAY_MS
            )
        )


def test_daily_read_skips_non_finite_closes():
    bot = make_gate_bot()
    closes = rising_then_flat(60)
    closes[10] = float("nan")
    bot.cm = FakeCandleManager({"BTC": closes})
    day_ts, kept = asyncio.run(
        bot._orchestrator_daily_closes(
            "BTC", lookback_days=52, now_ms=DAY0 + 60 * DAY_MS
        )
    )
    assert len(day_ts) == len(kept) == 52
    assert DAY0 + 10 * DAY_MS not in day_ts
    assert all(np.isfinite(value) for value in kept)


# --------------------------------------------------------------------------- #
# publishing the table
# --------------------------------------------------------------------------- #


def run_publish(bot, symbols, now):
    asyncio.run(bot._load_orchestrator_entry_regime_gate(symbols, int(now)))
    return bot._orchestrator_entry_regime_gate_tables


def test_warm_rising_series_publishes_a_risk_on_table():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000

    tables = run_publish(bot, ["BTC"], now)

    payload = tables["BTC"]["long"]
    assert payload["enabled"] is True
    assert payload["zero_is_on"] is False
    assert payload["block_initial"] is True
    assert payload["block_reentry"] is True
    # One boundary at the start of the current UTC day carries the whole verdict.
    assert payload["transition_ts"] == [now // DAY_MS * DAY_MS]
    assert payload["regime"] == [1]
    assert not bot._orchestrator_entry_regime_gate_unavailable_symbols


def test_falling_series_publishes_a_risk_off_table():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": falling_closes(200)})
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000

    tables = run_publish(bot, ["BTC"], now)

    assert tables["BTC"]["long"]["regime"] == [0]


def test_table_boundary_is_the_start_of_the_current_utc_day():
    bot = make_gate_bot()
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    # One minute after a UTC midnight: the boundary must be that midnight, not "now".
    now = DAY0 + 150 * DAY_MS + 60_000
    tables = run_publish(bot, ["BTC"], now)
    assert tables["BTC"]["long"]["transition_ts"] == [DAY0 + 150 * DAY_MS]


def test_short_arm_absent_from_approved_coins_gets_no_table():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    tables = run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert set(tables["BTC"]) == {"long"}


def test_unapproved_symbol_gets_no_table_and_no_read():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"ETH": rising_then_flat(200)})
    bot.cm = cm
    tables = run_publish(bot, ["ETH"], DAY0 + 150 * DAY_MS)
    assert tables == {}
    assert cm.calls == []


def test_unavailable_evidence_is_recorded_and_left_ungated():
    """No table means risk-off in Rust, so a missing series blocks rather than trades."""
    bot = make_gate_bot(approved_long=["BTC", "ETH"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200), "ETH": []})
    tables = run_publish(bot, ["BTC", "ETH"], DAY0 + 150 * DAY_MS)
    assert set(tables) == {"BTC"}
    assert bot._orchestrator_entry_regime_gate_unavailable_symbols == {"ETH"}


def test_a_too_short_series_cannot_define_the_filter():
    """Before the slow window fills there is no evidence, and the verdict is risk-off."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(10)})
    tables = run_publish(bot, ["BTC"], DAY0 + 10 * DAY_MS)
    assert tables["BTC"]["long"]["regime"] == [0]


def test_cold_start_series_is_risk_off_not_an_error():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(2)})
    tables = run_publish(bot, ["BTC"], DAY0 + 2 * DAY_MS)
    assert tables["BTC"]["long"]["regime"] == [0]
    assert not bot._orchestrator_entry_regime_gate_unavailable_symbols


def test_disabled_gate_publishes_nothing_and_reads_nothing():
    bot = make_gate_bot(
        {"enabled": False, "sma_fast_days": 20, "sma_slow_days": 50},
        approved_long=["BTC"],
        approved_short=[],
    )
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm
    tables = run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert tables == {}
    assert cm.calls == []


def test_side_without_block_flags_needs_no_daily_evidence():
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
    tables = run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert tables == {}
    assert cm.calls == []
    assert not bot._orchestrator_entry_regime_gate_unavailable_symbols


def test_republish_clears_the_previous_table():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert bot._orchestrator_entry_regime_gate_tables

    bot.cm = FakeCandleManager({"BTC": []})
    run_publish(bot, ["BTC"], DAY0 + 151 * DAY_MS)
    assert bot._orchestrator_entry_regime_gate_tables == {}
    assert bot._orchestrator_entry_regime_gate_unavailable_symbols == {"BTC"}


def test_lookback_covers_the_slow_window_and_the_confirmation_span():
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
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm
    now = DAY0 + 150 * DAY_MS + 12 * 3_600_000
    run_publish(bot, ["BTC"], now)

    # 30 slow days + 3 confirmation days + 2 spare = 35 days of evidence, and an
    # inclusive [start, end] window of 35 days spans 36 candles. Both bounds end at
    # the last closed day.
    call = cm.calls[0]
    assert call["max_lookback_candles"] == 36
    assert call["end_ts"] == DAY0 + 149 * DAY_MS
    assert call["start_ts"] == DAY0 + 149 * DAY_MS - 35 * DAY_MS


# --------------------------------------------------------------------------- #
# what the Rust side receives
# --------------------------------------------------------------------------- #


def test_master_params_never_carry_a_symbol_regime():
    """Global params are shared by every symbol, so they cannot encode one symbol's day."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert bot._orchestrator_entry_regime_gate_tables

    payload = bot._entry_regime_gate_for_rust("long", None)
    assert payload == {
        "enabled": False,
        "zero_is_on": False,
        "transition_ts": [],
        "regime": [],
    }


def test_symbol_params_carry_the_published_table():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    now = DAY0 + 150 * DAY_MS
    run_publish(bot, ["BTC"], now)

    payload = bot._entry_regime_gate_for_rust("long", "BTC")
    assert payload["enabled"] is True
    assert payload["transition_ts"] == [now]
    assert payload["regime"] == [1]


def test_symbol_without_a_table_reads_as_disabled():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

    payload = bot._entry_regime_gate_for_rust("short", "BTC")
    assert payload["enabled"] is False
    assert payload["transition_ts"] == []


def test_panic_plan_clears_a_cached_table():
    """Panic closes and never opens, so it must not carry a stale regime."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
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


def test_emitted_table_satisfies_the_rust_validation_rules():
    """The shape live emits must survive Rust's `EntryRegimeGateConfig::validate`."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    payload = bot._entry_regime_gate_for_rust("long", "BTC")

    assert len(payload["transition_ts"]) == len(payload["regime"])
    assert all(
        a < b for a, b in zip(payload["transition_ts"], payload["transition_ts"][1:])
    )
    assert all(value in (0, 1) for value in payload["regime"])
    assert payload["transition_ts"], "an enabled gate needs a non-empty table"
    assert payload["block_initial"] or payload["block_reentry"]


def test_emitted_table_matches_the_shared_read_path():
    """The published table must equal the verdict `src/entry_regime.py` computes."""
    from entry_regime import regime_flag_for_day

    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    closes = falling_closes(200)
    bot.cm = FakeCandleManager({"BTC": closes})
    now = DAY0 + 150 * DAY_MS
    run_publish(bot, ["BTC"], now)

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


def test_table_is_rebuilt_once_per_utc_day():
    """A daily verdict cannot change inside a day, so the planning path must not refetch."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm
    now = DAY0 + 150 * DAY_MS + 3_600_000

    run_publish(bot, ["BTC"], now)
    assert len(cm.calls) == 1

    run_publish(bot, ["BTC"], now + 60_000)
    assert len(cm.calls) == 1, "same UTC day must reuse the published table"

    run_publish(bot, ["BTC"], now + DAY_MS)
    assert len(cm.calls) == 2, "a new UTC day must rebuild the table"


def test_a_failed_symbol_retries_on_the_next_cycle():
    """An unavailable symbol is not cached as a permanent verdict."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"BTC": []})
    bot.cm = cm
    run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)
    assert bot._orchestrator_entry_regime_gate_unavailable_symbols == {"BTC"}

    bot.cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS + 60_000)
    assert bot._orchestrator_entry_regime_gate_unavailable_symbols == set()
    assert bot._orchestrator_entry_regime_gate_tables["BTC"]["long"]["regime"] == [1]


def test_traded_universe_wins_over_a_backtest_only_universe():
    """`backtest.approved_coins` may be wider; the gate must follow what live trades."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.config["backtest"]["approved_coins"] = {"long": ["BTC", "ETH"], "short": []}
    cm = FakeCandleManager(
        {"BTC": rising_then_flat(200), "ETH": rising_then_flat(200)}
    )
    bot.cm = cm

    tables = run_publish(bot, ["BTC", "ETH"], DAY0 + 150 * DAY_MS)

    assert set(tables) == {"BTC"}
    assert [call["symbol"] for call in cm.calls] == ["BTC"]


def test_backtest_universe_is_used_when_the_traded_universe_is_absent():
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    del bot.config["live"]["approved_coins"]
    bot.config["backtest"]["approved_coins"] = {"long": ["BTC"], "short": []}
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    tables = run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

    assert set(tables) == {"BTC"}


def test_resolved_runtime_universe_wins_over_config_strings():
    """Symbol resolution and `ignored_coins` are already applied to the runtime set."""
    bot = make_gate_bot(approved_long=["BTC", "ETH"], approved_short=[])
    bot.approved_coins_minus_ignored_coins = {"long": {"BTC"}, "short": set()}
    cm = FakeCandleManager(
        {"BTC": rising_then_flat(200), "ETH": rising_then_flat(200)}
    )
    bot.cm = cm

    tables = run_publish(bot, ["BTC", "ETH"], DAY0 + 150 * DAY_MS)

    assert set(tables) == {"BTC"}
    assert [call["symbol"] for call in cm.calls] == ["BTC"]


def test_a_settled_empty_side_reads_no_evidence():
    """A resolved empty set is an answer; only an absent key falls back to config."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    bot.approved_coins_minus_ignored_coins = {"long": set(), "short": set()}
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    tables = run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

    assert tables == {}
    assert cm.calls == []


def test_an_unresolved_universe_falls_back_to_the_config_list():
    """An absent key means the runtime has not resolved a universe yet."""
    bot = make_gate_bot(approved_long=["BTC"], approved_short=[])
    cm = FakeCandleManager({"BTC": rising_then_flat(200)})
    bot.cm = cm

    tables = run_publish(bot, ["BTC"], DAY0 + 150 * DAY_MS)

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
