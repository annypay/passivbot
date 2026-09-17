"""Unit tests for the read-only entry-regime probe.

The probe never talks to an exchange in these tests: a fake client supplies markets and
daily rows, so the assertions pin the report's arithmetic, its depth accounting and its
failure handling rather than the network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from tools import probe_entry_regime_gate as probe  # noqa: E402

MS_PER_DAY = 86_400_000
QUERY_DAY = 20_000
NOW_MS = QUERY_DAY * MS_PER_DAY + 12 * 3_600_000
LAST_CLOSED_DAY = (QUERY_DAY - 1) * MS_PER_DAY

CONFIG = {
    "live": {
        "approved_coins": {"long": ["BTC", "ETH"], "short": []},
        "ignored_coins": {"long": [], "short": []},
    },
    "backtest": {
        "entry_regime_gate": {
            "enabled": True,
            "sma_fast_days": 20,
            "sma_slow_days": 50,
            "confirm_days": 0,
            "block_initial": True,
            "block_reentry": True,
        }
    },
}


def single_coin_config(coin: str) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config["live"]["approved_coins"] = {"long": [coin], "short": []}
    return config


def market(coin: str, *, quote: str = "USDT", min_cost: float = 5.0) -> dict:
    symbol = f"{coin}/{quote}:{quote}"
    return {
        symbol: {
            "symbol": symbol,
            "base": coin,
            "quote": quote,
            "swap": True,
            "linear": True,
            "active": True,
            "limits": {"cost": {"min": min_cost}, "amount": {"min": 0.001}},
        }
    }


def rows(closes: list[float], *, drop: tuple[int, ...] = (), forming: bool = False) -> list[list]:
    """Daily rows ending at the last closed UTC day, optionally with a gap or a forming day."""
    out: list[list] = []
    total = len(closes)
    for index, close in enumerate(closes):
        if index in drop:
            continue
        ts = LAST_CLOSED_DAY - (total - 1 - index) * MS_PER_DAY
        out.append([ts, close, close, close, close, 1.0])
    if forming:
        out.append([QUERY_DAY * MS_PER_DAY, closes[-1], closes[-1], closes[-1], closes[-1], 1.0])
    return out


def rising(n: int, *, start: float = 100.0) -> list[float]:
    return [start + index for index in range(n)]


def falling(n: int, *, start: float = 400.0) -> list[float]:
    return [start - index for index in range(n)]


class FakeClient:
    def __init__(self, markets: dict, series: dict[str, list[list]], *, prices: dict | None = None):
        self.markets = markets
        self.series = series
        self.prices = prices or {}
        self.calls: list[tuple] = []

    async def fetch_ohlcv(self, symbol, timeframe="1d", since=None, limit=None):
        self.calls.append((symbol, timeframe, since, limit))
        data = list(self.series.get(symbol) or [])
        if since is not None:
            data = [row for row in data if int(row[0]) >= int(since)]
        if limit:
            data = data[-int(limit):]
        return data

    async def fetch_ticker(self, symbol):
        return {"last": self.prices.get(symbol, 100.0)}


class RaisingClient(FakeClient):
    def __init__(self, markets: dict, series: dict, *, failing: set[str]):
        super().__init__(markets, series)
        self.failing = failing

    async def fetch_ohlcv(self, symbol, timeframe="1d", since=None, limit=None):
        if symbol in self.failing:
            raise RuntimeError("exchange unavailable")
        return await super().fetch_ohlcv(symbol, timeframe, since, limit)


async def run(client, *, config=None, sides=("long",), coins=None, balance=None) -> dict:
    return await probe.build_report(
        config or CONFIG,
        markets=client.markets,
        fetch_ohlcv=client.fetch_ohlcv,
        fetch_ticker=client.fetch_ticker,
        now_ms=NOW_MS,
        exchange="binance",
        quote="USDT",
        sides=list(sides),
        coins_override=list(coins) if coins else None,
        balance=balance,
    )


async def test_gate_depth_comes_from_the_live_lookback_rule():
    report = await run(FakeClient({**market("BTC"), **market("ETH")},
                            {"BTC/USDT:USDT": rows(rising(70)),
                             "ETH/USDT:USDT": rows(rising(70))}))
    assert report["gate"]["required_days"] == 50
    assert report["gate"]["lookback_days"] == 60
    assert report["gate_block_source"] == "backtest.entry_regime_gate"
    assert report["exchange"] == "binance"
    assert report["window"]["end_day_start"] == LAST_CLOSED_DAY
    assert report["notes"][0] == "public_unauthenticated_market_data_probe"
    assert any("load_ccxt_instance" in note for note in report["notes"])


async def test_the_live_key_wins_over_the_backtest_block():
    config = json.loads(json.dumps(CONFIG))
    config["live"]["entry_regime_gate"] = {
        "enabled": True,
        "sma_fast_days": 10,
        "sma_slow_days": 30,
        "confirm_days": 5,
    }
    report = await run(FakeClient(market("BTC"), {"BTC/USDT:USDT": rows(rising(70))}), config=config)
    assert report["gate_block_source"] == "live.entry_regime_gate"
    assert report["gate"]["required_days"] == 35
    assert report["gate"]["lookback_days"] == 60


async def test_a_coin_without_an_enabled_gate_is_rejected():
    with pytest.raises(ValueError, match="no enabled entry-regime gate"):
        await run(FakeClient(market("BTC"), {}), config={"live": {}, "backtest": {}})


async def test_rising_series_reads_risk_on_and_reports_depth():
    client = FakeClient(market("BTC"), {"BTC/USDT:USDT": rows(rising(90))})
    report = await run(client)
    row = report["coins"][0]
    assert row["status"] == probe.STATUS_RISK_ON
    assert row["depth"] == 61
    assert row["missing_days"] == 0
    assert row["last_closed_day"] == LAST_CLOSED_DAY
    assert row["min_cost"] == 5.0
    assert report["summary"]["risk_on"] == 1
    # One daily request per coin, for the derived warm-up window: the live read's
    # inclusive [start, end] range plus one row for the forming candle.
    assert client.calls[0][1] == "1d"
    assert client.calls[0][2] == LAST_CLOSED_DAY - 60 * MS_PER_DAY
    assert client.calls[0][3] == 62


async def test_falling_series_reads_risk_off():
    report = await run(FakeClient(market("BTC"), {"BTC/USDT:USDT": rows(falling(90))}))
    assert report["coins"][0]["status"] == probe.STATUS_RISK_OFF
    assert report["summary"]["risk_off"] == 1


async def test_the_forming_day_is_not_evidence():
    report = await run(
        FakeClient(market("BTC"), {"BTC/USDT:USDT": rows(rising(90), forming=True)})
    )
    row = report["coins"][0]
    assert row["depth"] == 61
    assert row["last_closed_day"] == LAST_CLOSED_DAY
    assert row["status"] == probe.STATUS_RISK_ON


async def test_a_short_series_reads_risk_off_and_flags_the_age_floor():
    report = await run(FakeClient(market("ETH"), {"ETH/USDT:USDT": rows(rising(30))}))
    row = next(r for r in report["coins"] if r["coin"] == "ETH")
    assert row["status"] == probe.STATUS_RISK_OFF
    assert row["depth"] == 30
    assert row["listed_under_min_age"] is True
    assert report["summary"]["listed_under_min_age"] == ["ETH"]


async def test_a_daily_gap_is_reported_and_reads_risk_off():
    report = await run(
        FakeClient(market("BTC"), {"BTC/USDT:USDT": rows(rising(90), drop=(50,))})
    )
    row = report["coins"][0]
    assert row["status"] == probe.STATUS_RISK_OFF
    assert row["depth"] == 60
    assert row["missing_days"] == 1
    assert report["summary"]["missing_days_max"] == 1


async def test_a_coin_without_a_linear_swap_market_is_unavailable():
    report = await run(FakeClient({}, {}), coins=["BTC"])
    assert report["coins"][0]["status"] == probe.STATUS_UNAVAILABLE
    assert report["coins"][0]["error"] == "no_linear_swap_market"
    assert report["summary"]["unavailable_coins"] == ["BTC"]


async def test_an_inactive_contract_is_reported_as_such():
    """A delisted contract is not the same as one the exchange never listed."""
    markets = market("TON")
    markets["TON/USDT:USDT"]["active"] = False
    report = await run(FakeClient(markets, {}), config=single_coin_config("TON"))
    assert report["coins"][0]["status"] == probe.STATUS_UNAVAILABLE
    assert report["coins"][0]["error"] == "inactive_linear_swap_market"


async def test_a_failed_read_is_reported_without_aborting_the_report():
    client = RaisingClient(
        {**market("BTC"), **market("ETH")},
        {"ETH/USDT:USDT": rows(rising(90))},
        failing={"BTC/USDT:USDT"},
    )
    report = await run(client)
    statuses = {row["coin"]: row["status"] for row in report["coins"]}
    assert statuses["BTC"] == probe.STATUS_UNAVAILABLE
    assert statuses["ETH"] == probe.STATUS_RISK_ON
    assert [row["coin"] for row in report["coins"] if row.get("error")] == ["BTC"]


async def test_ignored_coins_are_skipped():
    config = json.loads(json.dumps(CONFIG))
    config["live"]["ignored_coins"]["long"] = ["ETH"]
    report = await run(FakeClient({**market("BTC"), **market("ETH")},
                            {"BTC/USDT:USDT": rows(rising(90))}), config=config)
    assert [row["coin"] for row in report["coins"]] == ["BTC"]


async def test_a_coin_subset_must_be_approved():
    with pytest.raises(ValueError, match="not approved"):
        await run(FakeClient(market("BTC"), {}), coins=["SOL"])


async def test_default_sides_follow_the_approved_lists():
    config = {"live": {"approved_coins": {"long": ["BTC"], "short": []}}, "backtest": {}}
    assert probe.requested_sides(config, None) == ["long"]
    assert probe.requested_sides(config, "long,short") == ["long", "short"]
    with pytest.raises(ValueError, match="unknown sides"):
        probe.requested_sides(config, "both")


# --------------------------------------------------------------------------- #
# funding math: what balance the live admission filter needs
# --------------------------------------------------------------------------- #

# The published g4 gate profile's long side, in the shape the loader resolves it.
G4_LONG_RISK = {
    "risk": {
        "n_positions": 7,
        "total_wallet_exposure_limit": 1.0,
        "we_excess_allowance_pct": 0.37,
        "we_excess_allowance_mode": "bounded",
    },
    "strategy": {"trailing_martingale": {"entry": {"initial_qty_pct": 0.0081}}},
}


def funding_config(*, min_cost: float = 5.0, filter_enabled: bool = True) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config["live"]["strategy_kind"] = "trailing_martingale"
    config["live"]["filter_by_min_effective_cost"] = filter_enabled
    config["live"]["approved_coins"] = {"long": ["BTC"], "short": []}
    config["bot"] = {"long": G4_LONG_RISK, "short": {"risk": {"n_positions": 1, "total_wallet_exposure_limit": 0.0}}}
    return config


def funding_markets(min_cost: float = 5.0, *, qty_step: float = 0.001, min_qty: float = 0.001) -> dict:
    markets = market("BTC")
    entry = markets["BTC/USDT:USDT"]
    entry["limits"] = {"cost": {"min": min_cost}, "amount": {"min": min_qty}}
    entry["precision"] = {"amount": qty_step, "price": 0.1}
    entry["contractSize"] = 1.0
    return markets


def test_the_sizing_factor_mirrors_the_live_bounded_allowance():
    factor = probe.initial_entry_sizing_factor(funding_config(), "long")
    # (1.0 / 7) * (1 + 0.37) * 0.0081
    assert factor == pytest.approx((1.0 / 7.0) * 1.37 * 0.0081, rel=1e-12)
    assert factor == pytest.approx(0.001585285, rel=1e-6)


def test_a_disabled_side_has_no_sizing_factor():
    config = funding_config()
    assert probe.initial_entry_sizing_factor(config, "short") is None


def test_bounded_allowance_cannot_exceed_the_side_total_limit():
    # base 0.5, total 1.0 -> the allowance is clamped to 1.0 (100%), not the raw 5.0
    assert probe.effective_we_excess_allowance_pct(
        base_limit=0.5, raw_pct=5.0, total_limit=1.0, mode="bounded"
    ) == 1.0
    assert probe.effective_we_excess_allowance_pct(
        base_limit=0.5, raw_pct=5.0, total_limit=1.0, mode="legacy_raw"
    ) == 5.0


async def test_min_balance_required_matches_the_admission_test():
    client = FakeClient(
        funding_markets(min_cost=5.0),
        {"BTC/USDT:USDT": rows(rising(90))},
        prices={"BTC/USDT:USDT": 100.0},
    )
    report = await run(client, config=funding_config())
    row = report["coins"][0]
    factor = probe.initial_entry_sizing_factor(funding_config(), "long")
    assert row["min_balance_required"] == pytest.approx(row["effective_min_cost"] / factor)
    # min_cost 5 at price 100 with qty_step 0.001 -> effective cost 5.0
    assert row["effective_min_cost"] == pytest.approx(5.0, rel=1e-9)
    assert row["min_balance_required"] == pytest.approx(3154.0, rel=1e-3)
    assert report["summary"]["min_balance_for_all_coins"] == pytest.approx(
        row["min_balance_required"]
    )


async def test_balance_argument_reports_affordability():
    client = FakeClient(
        funding_markets(min_cost=5.0),
        {"BTC/USDT:USDT": rows(rising(90))},
        prices={"BTC/USDT:USDT": 100.0},
    )
    poor = await run(client, config=funding_config(), balance=1000.0)
    rich = await run(client, config=funding_config(), balance=5000.0)
    assert poor["coins"][0]["affordable_at_balance"] is False
    assert poor["summary"]["affordable_coins"] == 0
    assert rich["coins"][0]["affordable_at_balance"] is True
    assert rich["summary"]["affordable_coins"] == 1


async def test_filter_disabled_is_reported_and_thresholds_stay_informational():
    client = FakeClient(
        funding_markets(min_cost=5.0),
        {"BTC/USDT:USDT": rows(rising(90))},
        prices={"BTC/USDT:USDT": 100.0},
    )
    report = await run(client, config=funding_config(filter_enabled=False), balance=1000.0)
    assert report["summary"]["filter_by_min_effective_cost"] is False
    # The threshold is still reported; it just does not govern admission.
    assert report["coins"][0]["min_balance_required"] > 1000.0
