"""Read-only daily-candle probe for the live entry-regime gate.

**Public unauthenticated market-data probe.** It fetches closed daily candles from the
exchange's public REST API, never loads API keys, never touches a private endpoint and
never places, cancels or modifies an order.

It builds its client through the runtime's own loader (`utils.load_ccxt_instance`), so a
`HTTP_PROXY` / `HTTPS_PROXY` in the environment and any custom-endpoint override apply
here exactly as they do for the bot: a probe that reached the exchange by a different
route than the bot would not prove the bot can reach it.

What it answers before a live start:

* the verdict the live gate would publish for the current UTC day, per approved coin,
  computed with the same arithmetic the runtime uses (`src/entry_regime.py`), so a probe
  and the bot cannot disagree;
* how deep the assembled daily history actually is against the live warm-up request
  (`live_lookback_days(sma_slow_days, confirm_days) + 1` rows). A coin whose depth is
  below `sma_slow_days + confirm_days` reads risk-off in live no matter what the market
  does, and a coin whose depth is short because it is newly listed is also what
  `live.minimum_coin_age_days` filters;
* each coin's `min_cost` / `min_qty`, the executable `effective_min_cost` at its current
  price, and the account balance that coin needs before the live
  `filter_by_min_effective_cost` admission test passes — `balance * (TWE / n_positions)
  * (1 + excess allowance) * initial_qty_pct >= effective_min_cost`. A balance below that
  threshold does not shrink the position: with the filter on, the coin is dropped from the
  traded universe entirely.

The requested window is the live one: `[end_day - lookback_days, end_day]` where
`end_day` is the last closed UTC day. The forming day is never evidence, and a missing
day is reported rather than smoothed — in the shared rule a window spanning a gap stays
undefined and reads risk-off.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from entry_regime import (  # noqa: E402
    MS_PER_UTC_DAY,
    live_lookback_days,
    regime_flag_for_day,
)
from config.shared_bot import get_grouped_bot_value  # noqa: E402
from utils import load_ccxt_instance  # noqa: E402

STATUS_RISK_ON = "risk_on"
STATUS_RISK_OFF = "risk_off"
STATUS_UNAVAILABLE = "unavailable"

#: A coin needs this many closed days before `live.minimum_coin_age_days` keeps it; the
#: probe only sees the requested window, so this is a floor on the estimate.
LISTED_DAYS_FLOOR = 60


def _log(message: str) -> None:
    print(message, flush=True)


def load_config(path: str | Path) -> dict:
    import hjson

    text = Path(path).read_text(encoding="utf-8")
    try:
        config = hjson.loads(text)
    except Exception:
        config = json.loads(text)
    if not isinstance(config, dict):
        raise ValueError(f"config must be an object: {path}")
    return config


def resolve_gate_block(config: dict) -> tuple[dict, str]:
    """The declared gate block and where it came from.

    Mirrors the live runtime: `live.entry_regime_gate` wins, and a config that only
    states `backtest.entry_regime_gate` (the published profile and every recorded
    evidence bundle) is read from there, because the live loader strips the rest of the
    `backtest` subtree.
    """
    live_block = (config.get("live") or {}).get("entry_regime_gate")
    if isinstance(live_block, dict) and live_block.get("enabled"):
        return live_block, "live.entry_regime_gate"
    backtest_block = (config.get("backtest") or {}).get("entry_regime_gate")
    if isinstance(backtest_block, dict) and backtest_block.get("enabled"):
        return backtest_block, "backtest.entry_regime_gate"
    return {}, ""


def resolve_gate(gate_block: dict) -> dict:
    fast = int(gate_block.get("sma_fast_days", 0) or 0)
    slow = int(gate_block.get("sma_slow_days", 0) or 0)
    if fast <= 0 or slow <= 0 or fast >= slow:
        raise ValueError(
            f"gate needs 0 < sma_fast_days < sma_slow_days, got fast={fast} slow={slow}"
        )
    confirm_days = max(0, int(gate_block.get("confirm_days", 0) or 0))
    return {
        "sma_fast_days": fast,
        "sma_slow_days": slow,
        "confirm_days": confirm_days,
        "block_initial": bool(gate_block.get("block_initial", True)),
        "block_reentry": bool(gate_block.get("block_reentry", True)),
        "required_days": slow + confirm_days,
        "lookback_days": live_lookback_days(slow, confirm_days),
    }


def effective_we_excess_allowance_pct(
    *, base_limit: float, raw_pct: float, total_limit: float, mode: str
) -> float:
    """Mirror the engine's bounded allowance clamp.

    `bounded` mode never lets the allowance push the per-position budget past the side's
    own total limit; `legacy_raw` uses the configured value as-is.
    """
    raw = max(0.0, float(raw_pct))
    if str(mode or "").strip().lower() == "legacy_raw":
        return raw
    if not (base_limit > 0.0 and total_limit > 0.0):
        return 0.0
    return min(raw, max(0.0, total_limit / base_limit - 1.0))


def initial_entry_sizing_factor(config: dict, pside: str) -> float | None:
    """Balance multiplier for one side's initial entry cost, or None when disabled.

    Mirrors the live rule: `wallet_exposure_limit` (= total limit / n_positions) times the
    allowance multiplier times the strategy's initial sizing fraction. This is the number
    the `filter_by_min_effective_cost` admission test compares against a symbol's
    executable minimum cost, so it is the honest answer to "how much balance do I need".
    """
    bot = (config.get("bot") or {}).get(pside)
    if not isinstance(bot, dict):
        return None
    # The canonical resolver reads both the grouped config spelling
    # (`risk.we_excess_allowance_pct`) and the flat runtime one.
    total_limit = float(
        get_grouped_bot_value(bot, "total_wallet_exposure_limit", 0.0) or 0.0
    )
    n_positions = int(round(float(get_grouped_bot_value(bot, "n_positions", 0.0) or 0.0)))
    if total_limit <= 0.0 or n_positions <= 0:
        return None
    base_limit = total_limit / n_positions
    allowance = effective_we_excess_allowance_pct(
        base_limit=base_limit,
        raw_pct=float(
            get_grouped_bot_value(bot, "risk_we_excess_allowance_pct", 0.0) or 0.0
        ),
        total_limit=total_limit,
        mode=str(
            get_grouped_bot_value(bot, "risk_we_excess_allowance_mode", "bounded")
            or "bounded"
        ),
    )
    strategy_kind = str(
        ((config.get("live") or {}).get("strategy_kind")) or "trailing_martingale"
    ).strip().lower()
    strategy = bot.get("strategy") if isinstance(bot.get("strategy"), dict) else {}
    params = strategy.get(strategy_kind) if isinstance(strategy.get(strategy_kind), dict) else {}
    entry = params.get("entry") if isinstance(params.get("entry"), dict) else {}
    fraction = entry.get("initial_qty_pct")
    if fraction is None and strategy_kind == "ema_anchor":
        fraction = params.get("base_qty_pct")
    if fraction is None:
        return None
    fraction = float(fraction)
    if fraction <= 0.0:
        return None
    return base_limit * (1.0 + allowance) * fraction


def _effective_min_cost(
    price: float, c_mult: float, qty_step: float, min_qty: float, min_cost: float
) -> float:
    """Executable minimum order cost, using the engine's own helper when importable."""
    if not (price > 0.0):
        return float(min_cost)
    try:
        import passivbot_rust as pbr

        qty = float(
            pbr.calc_min_entry_qty_py(price, c_mult, qty_step, min_qty, min_cost)
        )
        return float(pbr.qty_to_cost(qty, price, c_mult))
    except Exception:
        return float(min_cost)


def resolve_approved_coins(config: dict, sides: Iterable[str]) -> dict[str, list[str]]:
    approved = ((config.get("live") or {}).get("approved_coins")) or {}
    ignored = ((config.get("live") or {}).get("ignored_coins")) or {}
    out: dict[str, list[str]] = {}
    for pside in sides:
        listed = approved.get(pside)
        if not isinstance(listed, list):
            out[pside] = []
            continue
        skip = set(ignored.get(pside) or []) if isinstance(ignored, dict) else set()
        out[pside] = [str(coin) for coin in listed if str(coin) not in skip]
    return out


def requested_sides(config: dict, requested: str | None) -> list[str]:
    if requested:
        sides = [part.strip().lower() for part in requested.split(",") if part.strip()]
        unknown = [s for s in sides if s not in ("long", "short")]
        if unknown:
            raise ValueError(f"unknown sides: {','.join(unknown)}")
        return sides
    approved = ((config.get("live") or {}).get("approved_coins")) or {}
    return [s for s in ("long", "short") if approved.get(s)]


def _swap_markets(markets: dict, coin: str, quote: str) -> list[dict]:
    wanted_quote = quote.upper()
    out: list[dict] = []
    for symbol, market in markets.items():
        if not isinstance(market, dict):
            continue
        if str(market.get("base", "")).upper() != coin.upper():
            continue
        if str(market.get("quote", "")).upper() != wanted_quote:
            continue
        if not market.get("swap") or not market.get("linear"):
            continue
        out.append(dict(market, symbol=str(market.get("symbol") or symbol)))
    return out


def resolve_swap_symbol(markets: dict, coin: str, quote: str) -> tuple[str | None, str | None]:
    """(symbol, reason) for one coin's linear swap.

    A market that exists but is inactive is a different operator signal from one that
    does not exist at all: the live runtime treats an inactive contract as ineligible and
    drops the coin from the traded universe, while a missing market means the config
    names something this exchange never listed.
    """
    candidates = _swap_markets(markets, coin, quote)
    if not candidates:
        return None, "no_linear_swap_market"
    active = [m for m in candidates if m.get("active") is not False]
    if not active:
        return None, "inactive_linear_swap_market"
    return str(active[0]["symbol"]), None


def daily_window(now_ms: int, lookback_days: int) -> tuple[int, int]:
    """Inclusive [start_day_start, end_day_start] of closed UTC days, live semantics."""
    day_ms = MS_PER_UTC_DAY
    end_day_start = (int(now_ms) // day_ms) * day_ms - day_ms
    return end_day_start - day_ms * int(lookback_days), end_day_start


def closed_daily_rows(
    rows: list[list[float]], *, start_day_start: int, end_day_start: int
) -> tuple[list[int], list[float]]:
    """(day_ts, closes) for the closed UTC days inside the requested window.

    The window ends at the last closed UTC day, so the exchange's forming day is never
    evidence — the same cap the live read gets from the candlestick manager's finalized
    bucket. Rows outside the window are dropped rather than counted, because a row
    before it would inflate the depth the report compares against the warm-up request.
    """
    by_day: dict[int, float] = {}
    for row in rows or []:
        if not row:
            continue
        ts = int(row[0])
        if ts % MS_PER_UTC_DAY != 0:
            raise ValueError(f"daily candle not aligned to a UTC day: {ts}")
        if ts < int(start_day_start) or ts > int(end_day_start):
            continue
        close = float(row[4])
        if close != close or close <= 0.0:  # NaN or non-positive is not usable evidence
            continue
        by_day[ts] = close
    ordered = sorted(by_day)
    return ordered, [by_day[ts] for ts in ordered]


async def probe_coin(
    coin: str,
    symbol: str,
    markets: dict,
    fetch_ohlcv: Callable[..., Awaitable[Any]],
    fetch_ticker: Callable[..., Awaitable[Any]] | None,
    *,
    gate: dict,
    now_ms: int,
    sides: list[str],
    sizing_factors: dict[str, float | None],
    filter_enabled: bool,
    balance: float | None,
) -> dict:
    market = markets.get(symbol) or {}
    limits = market.get("limits") if isinstance(market.get("limits"), dict) else {}
    precision = market.get("precision") if isinstance(market.get("precision"), dict) else {}
    min_cost = (limits.get("cost") or {}).get("min")
    min_qty = (limits.get("amount") or {}).get("min")
    c_mult = float(market.get("contractSize") or 1.0)
    qty_step = float(precision.get("amount") or 0.0) or float(min_qty or 0.0)
    base = {
        "coin": coin,
        "symbol": symbol,
        "sides": sides,
        "min_cost": min_cost,
        "min_qty": min_qty,
    }
    price = None
    if fetch_ticker is not None:
        try:
            ticker = await fetch_ticker(symbol)
            price = float((ticker or {}).get("last") or 0.0) or None
        except Exception:
            price = None
    if price is not None and min_cost:
        base["effective_min_cost"] = _effective_min_cost(
            price, c_mult, qty_step, float(min_qty or 0.0), float(min_cost)
        )
        usable = [sizing_factors.get(side) for side in sides if sizing_factors.get(side)]
        if usable:
            # The strictest side decides: a coin must clear the filter on every side it
            # is approved for, and the factor is side-specific.
            base["min_balance_required"] = max(
                float(base["effective_min_cost"]) / float(factor) for factor in usable
            )
            if balance is not None:
                base["affordable_at_balance"] = bool(
                    float(balance) >= float(base["min_balance_required"])
                )
    start_day_start, end_day_start = daily_window(now_ms, gate["lookback_days"])
    try:
        # The live read asks for exactly this inclusive range; the extra row absorbs the
        # forming candle the exchange appends to a `limit`-bounded answer.
        rows = await fetch_ohlcv(
            symbol, "1d", start_day_start, int(gate["lookback_days"]) + 2
        )
        day_ts, closes = closed_daily_rows(
            rows, start_day_start=start_day_start, end_day_start=end_day_start
        )
        if not day_ts:
            raise RuntimeError("no closed daily candles returned")
        verdict = regime_flag_for_day(
            day_ts,
            closes,
            now_ms,
            fast=gate["sma_fast_days"],
            slow=gate["sma_slow_days"],
            confirm_days=gate["confirm_days"],
        )
    except Exception as exc:
        return dict(base, status=STATUS_UNAVAILABLE, error=type(exc).__name__)
    depth = len(day_ts)
    return dict(
        base,
        status=STATUS_RISK_ON if verdict else STATUS_RISK_OFF,
        depth=depth,
        window_days=int(gate["lookback_days"]) + 1,
        missing_days=max(0, int(gate["lookback_days"]) + 1 - depth),
        last_closed_day=day_ts[-1],
        listed_under_min_age=depth < LISTED_DAYS_FLOOR,
    )


async def build_report(
    config: dict,
    *,
    markets: dict,
    fetch_ohlcv: Callable[..., Awaitable[Any]],
    fetch_ticker: Callable[..., Awaitable[Any]] | None = None,
    now_ms: int,
    exchange: str,
    quote: str,
    sides: list[str],
    coins_override: list[str] | None = None,
    balance: float | None = None,
) -> dict:
    gate_block, gate_source = resolve_gate_block(config)
    if not gate_block:
        raise ValueError("config declares no enabled entry-regime gate")
    gate = resolve_gate(gate_block)
    sizing_factors = {side: initial_entry_sizing_factor(config, side) for side in sides}
    filter_enabled = bool(
        (config.get("live") or {}).get("filter_by_min_effective_cost", True)
    )
    approved = resolve_approved_coins(config, sides)
    coins: dict[str, list[str]] = {}
    for pside, listed in approved.items():
        for coin in listed:
            coins.setdefault(coin, []).append(pside)
    if coins_override:
        wanted = {coin.upper() for coin in coins_override}
        coins = {coin: psides for coin, psides in coins.items() if coin.upper() in wanted}
        missing = sorted(wanted - {coin.upper() for coin in coins})
        if missing:
            raise ValueError(f"requested coins are not approved: {','.join(missing)}")
    rows: list[dict] = []
    for coin in sorted(coins):
        symbol, reason = resolve_swap_symbol(markets, coin, quote)
        if symbol is None:
            rows.append(
                {
                    "coin": coin,
                    "symbol": None,
                    "sides": coins[coin],
                    "status": STATUS_UNAVAILABLE,
                    "error": reason,
                }
            )
            continue
        rows.append(
            await probe_coin(
                coin,
                symbol,
                markets,
                fetch_ohlcv,
                fetch_ticker,
                gate=gate,
                now_ms=now_ms,
                sides=coins[coin],
                sizing_factors=sizing_factors,
                filter_enabled=filter_enabled,
                balance=balance,
            )
        )
    depths = [row["depth"] for row in rows if isinstance(row.get("depth"), int)]
    required_balances = [
        float(row["min_balance_required"])
        for row in rows
        if row.get("min_balance_required")
    ]
    summary = {
        "filter_by_min_effective_cost": bool(filter_enabled),
        "sizing_factors": {side: sizing_factors.get(side) for side in sides},
        "min_balance_for_all_coins": max(required_balances) if required_balances else None,
        "balance_checked": balance,
        "affordable_coins": (
            sum(1 for row in rows if row.get("affordable_at_balance"))
            if balance is not None
            else None
        ),
        "coins": len(rows),
        "risk_on": sum(1 for row in rows if row["status"] == STATUS_RISK_ON),
        "risk_off": sum(1 for row in rows if row["status"] == STATUS_RISK_OFF),
        "unavailable": sum(1 for row in rows if row["status"] == STATUS_UNAVAILABLE),
        "depth_min": min(depths) if depths else 0,
        "missing_days_max": max([row.get("missing_days", 0) for row in rows] or [0]),
        "listed_under_min_age": sorted(
            row["coin"] for row in rows if row.get("listed_under_min_age")
        ),
        "unavailable_coins": sorted(
            row["coin"] for row in rows if row["status"] == STATUS_UNAVAILABLE
        ),
    }
    start_day_start, end_day_start = daily_window(now_ms, gate["lookback_days"])
    return {
        "balance": balance,
        "generated_ms": int(now_ms),
        "exchange": exchange,
        "quote": quote,
        "gate_block_source": gate_source,
        "gate": gate,
        "sides": sides,
        "window": {"start_day_start": start_day_start, "end_day_start": end_day_start},
        "summary": summary,
        "coins": rows,
        "notes": [
            "public_unauthenticated_market_data_probe",
            "does_not_load_credentials_or_contact_private_endpoints",
            "does_not_place_cancel_or_modify_orders",
            "verdict_uses_the_same_rule_as_the_live_gate",
            "client_built_by_utils.load_ccxt_instance_so_proxy_and_endpoint_overrides_apply",
        ],
    }


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def print_table(report: dict) -> None:
    gate = report["gate"]
    _log(
        f"[gate] sma={gate['sma_fast_days']}/{gate['sma_slow_days']} "
        f"confirm_days={gate['confirm_days']} required_days={gate['required_days']} "
        f"lookback_days={gate['lookback_days']} source={report['gate_block_source']}"
    )
    _log(f"[exchange] {report['exchange']} quote={report['quote']} sides={','.join(report['sides'])}")
    for side, factor in (report["summary"].get("sizing_factors") or {}).items():
        _log(f"[sizing] {side}: initial-entry balance factor={factor}")
    _log(
        f"{'coin':<10}{'status':<13}{'depth':>7}{'missing':>9}"
        f"{'min_cost':>10}{'eff_cost':>10}{'min_balance':>13}  affordable"
    )
    for row in report["coins"]:
        affordable = row.get("affordable_at_balance")
        _log(
            f"{row['coin']:<10}{row['status']:<13}"
            f"{str(row.get('depth', '-')):>7}{str(row.get('missing_days', '-')):>9}"
            f"{str(row.get('min_cost', '-')):>10}"
            f"{_fmt(row.get('effective_min_cost')):>10}"
            f"{_fmt(row.get('min_balance_required')):>13}  "
            f"{'-' if affordable is None else ('yes' if affordable else 'no')}"
            + (f"  error={row['error']}" if row.get("error") else "")
        )
    summary = report["summary"]
    _log(
        f"[summary] coins={summary['coins']} risk_on={summary['risk_on']} "
        f"risk_off={summary['risk_off']} unavailable={summary['unavailable']} "
        f"depth_min={summary['depth_min']} missing_days_max={summary['missing_days_max']}"
    )
    if summary["unavailable_coins"]:
        _log(f"[summary] unavailable: {','.join(summary['unavailable_coins'])}")
    if summary["listed_under_min_age"]:
        _log(
            "[summary] listed under the 60-day age floor (live filters these): "
            + ",".join(summary["listed_under_min_age"])
        )
    if not summary.get("filter_by_min_effective_cost", True):
        _log(
            "[summary] live.filter_by_min_effective_cost is false: the thresholds below are "
            "informational only, and entries would instead be floored at the exchange minimum"
        )
    required = summary.get("min_balance_for_all_coins")
    if required is not None:
        _log(
            f"[summary] min_balance_for_all_coins={required:.2f} "
            f"(balance at which every probed coin clears the admission test)"
        )
    if summary.get("balance_checked") is not None:
        _log(
            f"[summary] at balance={summary['balance_checked']:.2f}: "
            f"affordable_coins={summary['affordable_coins']}/{summary['coins']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only public daily-candle probe for the live entry-regime gate: "
            "no API keys, no private endpoints, no orders."
        )
    )
    parser.add_argument("config_path", help="Live config JSON/HJSON file to inspect.")
    parser.add_argument("--coins", help="comma-separated subset of the approved coins")
    parser.add_argument("--exchange", default="binance", help="standard exchange name")
    parser.add_argument("--quote", default="USDT", help="quote currency")
    parser.add_argument(
        "--sides",
        default=None,
        help="comma-separated sides to probe; default: every side with approved coins",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=None,
        help=(
            "account balance in the quote currency; reports how many probed coins clear "
            "the live filter_by_min_effective_cost admission test at that balance"
        ),
    )
    parser.add_argument("--out", help="output JSON path")
    parser.add_argument("--json", action="store_true", help="also print the JSON report")
    return parser


async def _run(args: argparse.Namespace) -> dict:
    config = load_config(args.config_path)
    sides = requested_sides(config, args.sides)
    if not sides:
        raise ValueError("config approves no coins on either side")
    coins_override = (
        [part.strip() for part in args.coins.split(",") if part.strip()]
        if args.coins
        else None
    )
    client = load_ccxt_instance(args.exchange, enable_rate_limit=True)
    try:
        markets = await client.load_markets()
        now_ms = int(time.time() * 1000)
        return await build_report(
            config,
            markets=markets,
            fetch_ohlcv=client.fetch_ohlcv,
            fetch_ticker=getattr(client, "fetch_ticker", None),
            now_ms=now_ms,
            exchange=args.exchange,
            quote=args.quote,
            sides=sides,
            coins_override=coins_override,
            balance=args.balance,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = asyncio.run(_run(args))
    print_table(report)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _log(f"[out] {out_path}")
    if args.json:
        _log(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())