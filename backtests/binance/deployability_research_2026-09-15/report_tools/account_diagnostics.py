"""Offline account, inventory-age and cost diagnostics for exact Rust ledgers.

FIFO age attribution is an analytical convention, not exchange lot identity.
Rust's average-cost realized PnL is allocated by closed quantity for age reports;
it is never replaced by hypothetical FIFO tax-lot PnL.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


MINUTE_MS = 60_000
DAY_MS = 86_400_000
FILL_COLUMNS = [
    "index", "timestamp", "coin", "pnl", "fee_paid", "usd_total_balance",
    "btc_cash_wallet", "usd_cash_wallet", "btc_price", "qty", "price",
    "psize", "pprice", "type", "liquidity", "wallet_exposure",
    "twe_long", "twe_short", "twe_net",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (Path, pd.Timestamp)):
        return str(value)
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"unsupported JSON type: {type(value).__name__}")


def digest_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"),
                   allow_nan=False).encode()
    ).hexdigest()


def save_json(path: Path, value: Any, *, immutable: bool = False) -> None:
    payload = json.dumps(jsonable(value), indent=2, sort_keys=True,
                         ensure_ascii=False, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise ValueError(f"immutable artifact differs: {path}")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def normalize_fills(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(FILL_COLUMNS) - set(frame)
    if missing:
        raise ValueError(f"fill columns missing: {sorted(missing)}")
    result = frame[FILL_COLUMNS].copy().reset_index(drop=True)
    for name in set(FILL_COLUMNS) - {"timestamp", "coin", "type", "liquidity"}:
        result[name] = pd.to_numeric(result[name], errors="raise")
        if not np.isfinite(result[name]).all():
            raise ValueError(f"nonfinite fill field: {name}")
    if pd.api.types.is_numeric_dtype(result["timestamp"]):
        result["timestamp"] = result["timestamp"].astype(np.int64)
    else:
        result["timestamp"] = (
            pd.to_datetime(result["timestamp"], utc=True).astype("int64") // 1_000_000
        )
    if not result["timestamp"].is_monotonic_increasing:
        raise ValueError("fill order is not chronological; cannot invent intraminute ordering")
    result["position_side"] = result["type"].str.rsplit("_", n=1).str[-1]
    if not result["position_side"].isin(["long", "short"]).all():
        raise ValueError("unrecognized fill position side")
    if not result["type"].str.startswith(("entry_", "close_")).all():
        raise ValueError("unrecognized fill intent")
    result["fill_row"] = np.arange(len(result), dtype=np.int64)
    return result


def weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float | None:
    if not len(values):
        return None
    if (
        len(values) != len(weights) or not np.isfinite(values).all()
        or not np.isfinite(weights).all() or (weights < 0).any()
        or weights.sum() <= 0.0 or not 0.0 <= q <= 1.0
    ):
        raise ValueError("invalid weighted quantile input")
    order = np.argsort(values, kind="stable")
    cumulative = np.cumsum(weights[order])
    chosen = min(int(np.searchsorted(cumulative, q * cumulative[-1], side="left")),
                 len(order) - 1)
    return float(values[order[chosen]])


@dataclass
class Lot:
    timestamp: int
    quantity: float
    entry_fee: float


def holding_diagnostics(
    fills: pd.DataFrame, end_ms: int, multipliers: dict[str, float]
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    lots: dict[tuple[str, str], deque[Lot]] = defaultdict(deque)
    sizes: dict[tuple[str, str], float] = defaultdict(float)
    episodes: dict[tuple[str, str], dict[str, Any]] = {}
    episode_rows, lot_rows = [], []
    tolerance = 1e-8
    for row in fills.itertuples(index=False):
        key = (row.coin, row.position_side)
        signed = 1.0 if row.position_side == "long" else -1.0
        old = sizes[key]
        if abs(old + row.qty - row.psize) > max(tolerance, abs(row.psize) * 1e-9):
            raise AssertionError(f"broken position chain at fill {row.fill_row}: {key}")
        if row.psize * signed < -tolerance:
            raise AssertionError(f"position side/sign mismatch at fill {row.fill_row}")
        if row.type.startswith("entry_"):
            if row.qty * signed <= 0.0:
                raise AssertionError("entry quantity has the wrong sign")
            if abs(old) <= tolerance:
                episodes[key] = {
                    "coin": row.coin, "position_side": row.position_side,
                    "open_ms": int(row.timestamp), "entry_fills": 0, "close_fills": 0,
                    "pnl_usdt": 0.0, "fees_usdt": 0.0,
                }
            lots[key].append(Lot(int(row.timestamp), abs(row.qty), float(row.fee_paid)))
            episodes[key]["entry_fills"] += 1
        else:
            if row.qty * signed >= 0.0 or abs(row.qty) > abs(old) + tolerance:
                raise AssertionError(f"invalid reduce-only close: {row.fill_row}")
            remaining = abs(row.qty)
            while remaining > tolerance:
                if not lots[key]:
                    raise AssertionError(f"close has no entry inventory: {row.fill_row}")
                lot = lots[key][0]
                matched = min(lot.quantity, remaining)
                fee_share = lot.entry_fee * matched / lot.quantity
                share = matched / abs(row.qty)
                age = (row.timestamp - lot.timestamp) / MINUTE_MS
                lot_rows.append({
                    "coin": row.coin, "position_side": row.position_side,
                    "entry_ms": lot.timestamp, "close_ms": int(row.timestamp),
                    "close_fill_row": int(row.fill_row), "close_type": row.type,
                    "qty": matched,
                    "close_notional_usdt": matched * row.price * multipliers[row.coin],
                    "holding_label_minutes": age,
                    "holding_minimum_minutes": max(0.0, age - 1.0),
                    "holding_maximum_minutes": age + 1.0,
                    "allocated_average_cost_pnl_usdt": row.pnl * share,
                    "allocated_entry_fee_usdt": fee_share,
                    "allocated_close_fee_usdt": row.fee_paid * share,
                    "allocated_net_pnl_usdt": row.pnl * share + row.fee_paid * share
                    + fee_share,
                    "protective": row.type.startswith(
                        ("close_unstuck", "close_auto_reduce", "close_panic")
                    ),
                })
                lot.quantity -= matched
                lot.entry_fee -= fee_share
                remaining -= matched
                if lot.quantity <= tolerance:
                    lots[key].popleft()
            episodes[key]["close_fills"] += 1
        episodes[key]["pnl_usdt"] += row.pnl
        episodes[key]["fees_usdt"] += row.fee_paid
        sizes[key] = float(row.psize)
        if abs(row.psize) <= tolerance:
            episode = episodes.pop(key)
            episode.update({
                "end_ms": int(row.timestamp), "closed": True,
                "duration_minutes": (row.timestamp - episode["open_ms"]) / MINUTE_MS,
            })
            episode_rows.append(episode)
    for key, episode in episodes.items():
        episode.update({
            "end_ms": int(end_ms), "closed": False,
            "duration_minutes": (end_ms - episode["open_ms"]) / MINUTE_MS,
        })
        episode_rows.append(episode)
    lot_frame = pd.DataFrame(lot_rows)
    episode_frame = pd.DataFrame(episode_rows)
    pending_fees = sum(lot.entry_fee for queue in lots.values() for lot in queue)
    allocated = float(lot_frame["allocated_net_pnl_usdt"].sum()) if len(lot_frame) else 0.0
    realized = float((fills["pnl"] + fills["fee_paid"]).sum())
    if not np.isclose(allocated + pending_fees, realized, atol=1e-6, rtol=1e-10):
        raise AssertionError("age-attributed PnL and pending entry fees do not reconcile")
    result: dict[str, Any] = {
        "position_episodes": len(episode_frame),
        "closed_episodes": int(episode_frame["closed"].sum()) if len(episode_frame) else 0,
        "open_censored_episodes": len(episodes),
        "open_censored_notional_qty_not_comparable_across_coins": sum(
            sum(lot.quantity for lot in queue) for queue in lots.values()
        ),
        "unallocated_open_entry_fees_usdt": pending_fees,
    }
    if len(episode_frame):
        closed = episode_frame.loc[episode_frame["closed"], "duration_minutes"]
        result.update({
            "closed_episode_median_minutes": float(closed.median()) if len(closed) else None,
            "episode_max_observed_days": float(episode_frame["duration_minutes"].max() / 1440),
        })
    if len(lot_frame):
        regular = lot_frame.loc[~lot_frame["protective"]]
        all_notional = lot_frame["close_notional_usdt"].to_numpy()
        result.update({
            "closed_notional_weighted_median_hold_minutes": weighted_quantile(
                lot_frame["holding_minimum_minutes"].to_numpy(), all_notional, 0.5),
            "closed_notional_weighted_p95_hold_minutes": weighted_quantile(
                lot_frame["holding_minimum_minutes"].to_numpy(), all_notional, 0.95),
            "regular_closed_notional_weighted_median_hold_minutes": weighted_quantile(
                regular["holding_minimum_minutes"].to_numpy(),
                regular["close_notional_usdt"].to_numpy(), 0.5),
        })
        for horizon in (1, 5, 15, 60):
            young = lot_frame["holding_minimum_minutes"] < horizon
            regular_young = regular["holding_minimum_minutes"] < horizon
            normal_notional = regular["close_notional_usdt"].sum()
            gross_profit = regular["allocated_average_cost_pnl_usdt"].clip(lower=0)
            result[f"all_close_notional_under_{horizon}m_share"] = float(
                lot_frame.loc[young, "close_notional_usdt"].sum() / all_notional.sum())
            result[f"regular_close_notional_under_{horizon}m_share"] = (
                float(regular.loc[regular_young, "close_notional_usdt"].sum()
                      / normal_notional) if normal_notional > 0 else None)
            result[f"regular_gross_profit_under_{horizon}m_share"] = (
                float(gross_profit.loc[regular_young].sum() / gross_profit.sum())
                if gross_profit.sum() > 0 else None)
    return lot_frame, episode_frame, result


def drawdown(values: np.ndarray, timestamps: np.ndarray, initial: float) -> dict[str, Any]:
    if len(values) != len(timestamps) or not len(values):
        raise ValueError("invalid equity timeline")
    if not np.isfinite(values).all() or initial <= 0.0:
        raise ValueError("invalid equity values")
    series = np.r_[initial, values]
    times = np.r_[timestamps[0] - MINUTE_MS, timestamps]
    high = np.maximum.accumulate(series)
    dd = 1.0 - series / high
    trough = int(np.argmax(dd))
    peak = int(np.flatnonzero(series[:trough + 1] == high[trough])[-1])
    recovered = np.flatnonzero(series[trough + 1:] >= high[trough])
    recovery = trough + 1 + int(recovered[0]) if len(recovered) else None
    at_hwm = series >= high
    anchors = np.where(at_hwm, np.arange(len(series)), 0)
    last_peak = np.maximum.accumulate(anchors)
    underwater_ms = times - times[last_peak]
    return {
        "mdd": float(dd[trough]), "peak_ms": int(times[peak]),
        "trough_ms": int(times[trough]), "peak_equity": float(high[trough]),
        "trough_equity": float(series[trough]),
        "recovery_ms": int(times[recovery]) if recovery is not None else None,
        "mdd_peak_to_recovery_days": (
            float((times[recovery] - times[peak]) / DAY_MS)
            if recovery is not None else None),
        "longest_underwater_observed_days": float(underwater_ms.max() / DAY_MS),
        "terminal_underwater_days": float(underwater_ms[-1] / DAY_MS),
        "terminal_drawdown": float(dd[-1]),
    }


def reconstruct_account(
    raw_equity: np.ndarray,
    fills: pd.DataFrame,
    timestamps: np.ndarray,
    hlcvs: np.ndarray,
    coins: list[str],
    multipliers: dict[str, float],
    initial: float,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Rebuild signed coin+side inventory, cash, gross exposure and H/L stress."""
    times = raw_equity[:, 0].astype(np.int64)
    if not len(times) or not (np.diff(times) == MINUTE_MS).all():
        raise AssertionError("raw minute timeline has gaps")
    first = int(np.searchsorted(timestamps, times[0]))
    last = first + len(times)
    if not np.array_equal(timestamps[first:last], times):
        raise AssertionError("raw equity labels do not match prepared market data")
    if len(fills) and (
        int(fills["timestamp"].iloc[0]) < times[0]
        or int(fills["timestamp"].iloc[-1]) > times[-1]
    ):
        raise AssertionError("fill ledger extends beyond account timeline")
    if raw_equity.shape[1] < 4 or not np.allclose(
        raw_equity[:, 1], raw_equity[:, 3], rtol=0.0, atol=1e-6
    ):
        raise AssertionError("USD strategy/account equity must match for cash-only study")
    coin_idx = {coin: index for index, coin in enumerate(coins)}
    cm = np.array([multipliers[coin] for coin in coins])
    sizes = np.zeros((2, len(coins)))
    entries = np.zeros_like(sizes)
    n = len(times)
    balance_series, equity, adverse = (np.empty(n) for _ in range(3))
    gross_mark, cost_notional = np.empty(n), np.empty(n)
    active_count = np.empty(n, dtype=np.int16)
    turnover, fees = np.zeros(n), np.zeros(n)
    exposures_by_coin = np.zeros((n, len(coins)))
    groups = {int(key): group for key, group in fills.groupby("timestamp", sort=False)}
    cash, cursor = initial, 0

    def marks(candle: int) -> tuple[float, float]:
        current = hlcvs[first + candle]
        active = sizes != 0
        if not active.any():
            return cash, cash
        mark = np.broadcast_to(current[:, 2], sizes.shape)
        worse = np.stack((current[:, 1], current[:, 0]))
        if (
            not np.isfinite(mark[active]).all() or (mark[active] <= 0).any()
            or not np.isfinite(worse[active]).all() or (worse[active] <= 0).any()
        ):
            raise AssertionError("missing mark on active inventory")
        return (
            cash + float(np.sum((sizes * cm * (mark - entries))[active])),
            cash + float(np.sum((sizes * cm * (worse - entries))[active])),
        )

    def span(end: int) -> None:
        if end <= cursor:
            return
        balance_series[cursor:end] = cash
        active = np.any(sizes != 0, axis=0)
        active_count[cursor:end] = int(active.sum())
        total_cost = float((np.abs(sizes) * entries * cm).sum())
        cost_notional[cursor:end] = total_cost
        if not active.any():
            equity[cursor:end] = adverse[cursor:end] = cash
            gross_mark[cursor:end] = 0.0
            return
        mark = hlcvs[first + cursor:first + end, active, 2]
        low = hlcvs[first + cursor:first + end, active, 1]
        high = hlcvs[first + cursor:first + end, active, 0]
        if (
            not np.isfinite(mark).all() or not np.isfinite(low).all()
            or not np.isfinite(high).all()
            or (mark <= 0).any() or (low <= 0).any() or (high <= 0).any()
        ):
            raise AssertionError("invalid required position valuation")
        long = sizes[0, active] * cm[active]
        short = sizes[1, active] * cm[active]
        basis = float((sizes[:, active] * entries[:, active] * cm[active]).sum())
        equity[cursor:end] = cash + mark @ (long + short) - basis
        adverse[cursor:end] = cash + low @ long + high @ short - basis
        exposures_by_coin[cursor:end, active] = mark * (np.abs(long) + np.abs(short))
        gross_mark[cursor:end] = exposures_by_coin[cursor:end].sum(axis=1)

    max_balance_residual = 0.0
    for timestamp, group in groups.items():
        candle = int(np.searchsorted(times, timestamp))
        if candle >= n or times[candle] != timestamp:
            raise AssertionError("fill timestamp absent from equity grid")
        span(candle)
        cursor = candle
        _, worst = marks(candle)
        for row in group.itertuples(index=False):
            side = int(row.position_side == "short")
            col = coin_idx[row.coin]
            if not np.isclose(sizes[side, col] + row.qty, row.psize,
                              atol=1e-8, rtol=1e-9):
                raise AssertionError(f"inventory ledger mismatch: fill {row.fill_row}")
            cash += row.pnl + row.fee_paid
            residual = abs(cash - row.usd_total_balance)
            max_balance_residual = max(max_balance_residual, residual)
            if residual > max(1e-6, abs(cash) * 1e-10):
                raise AssertionError("cash ledger does not reconcile at every fill")
            sizes[side, col] = row.psize
            entries[side, col] = row.pprice if row.psize else 0.0
            _, current_adverse = marks(candle)
            worst = min(worst, current_adverse)
            turnover[candle] += abs(row.qty) * row.price * cm[col]
            fees[candle] -= row.fee_paid
        span(candle + 1)
        adverse[candle] = worst
        cursor = candle + 1
    span(n)
    residual = float(np.max(np.abs(equity - raw_equity[:, 1])))
    if residual > max(1e-5, initial * 1e-9):
        raise AssertionError(f"raw account equity does not reconcile: {residual}")
    previous_hwm = np.maximum.accumulate(np.r_[initial, raw_equity[:-1, 1]])
    index = pd.to_datetime(times, unit="ms", utc=True)
    account = pd.DataFrame({
        "equity": raw_equity[:, 1], "balance": balance_series,
        "adverse_hl_equity": adverse, "previous_close_hwm": previous_hwm,
        "gross_mark_notional": gross_mark, "cost_notional": cost_notional,
        "active_coin_count": active_count, "turnover_notional": turnover,
        "fees": fees,
    }, index=index)
    account.index.name = "timestamp"
    coin_time = exposures_by_coin.sum(axis=0)
    total_coin_time = coin_time.sum()
    concentration = pd.DataFrame({
        "coin": coins, "notional_minutes": coin_time,
        "exposure_time_share": coin_time / total_coin_time
        if total_coin_time > 0 else np.zeros(len(coins)),
        "invested_minutes": (exposures_by_coin > 0).sum(axis=0),
    })
    concentration["realized_pnl_usdt"] = concentration["coin"].map(
        fills.groupby("coin")["pnl"].sum()
    ).fillna(0.0)
    concentration["fees_usdt"] = -concentration["coin"].map(
        fills.groupby("coin")["fee_paid"].sum()
    ).fillna(0.0)
    invested = active_count > 0
    summary = {
        "cash_reconstruction_max_residual_usdt": max_balance_residual,
        "equity_reconstruction_max_residual_usdt": residual,
        "adverse_hl_drawdown_from_previous_close_hwm": float(
            np.maximum(0.0, 1.0 - adverse / previous_hwm).max()),
        "max_gross_cost_exposure": float(np.max(cost_notional / balance_series)),
        "max_gross_mark_exposure": float(np.max(gross_mark / raw_equity[:, 1])),
        "average_gross_mark_exposure": float(np.mean(gross_mark / raw_equity[:, 1])),
        "max_active_coins": int(active_count.max()),
        "invested_time_share": float(invested.mean()),
        "mean_active_coins_when_invested": float(active_count[invested].mean())
        if invested.any() else 0.0,
        "median_active_coins_when_invested": float(np.median(active_count[invested]))
        if invested.any() else 0.0,
        "top_coin_exposure_time_share": float(concentration["exposure_time_share"].max()),
        "effective_coin_count_exposure_time": float(
            1.0 / np.square(concentration["exposure_time_share"]).sum())
        if total_coin_time > 0 else 0.0,
        "traded_coin_count": int(fills["coin"].nunique()),
        "total_turnover_usdt": float(turnover.sum()),
        "total_fees_usdt": float(fees.sum()),
        "gross_realized_pnl_usdt": float(fills["pnl"].sum()),
        "net_realized_pnl_usdt": float((fills["pnl"] + fills["fee_paid"]).sum()),
        "max_unrealized_loss_usdt": float(
            np.maximum(0.0, balance_series - raw_equity[:, 1]).max()),
        "final_unrealized_pnl_usdt": float(raw_equity[-1, 1] - cash),
        "terminal_remaining_mark_notional_usdt": float(gross_mark[-1]),
    }
    return account, concentration, summary


def performance(account: pd.DataFrame, initial: float) -> dict[str, Any]:
    times = account.index.astype("int64").to_numpy() // 1_000_000
    equity = account["equity"].to_numpy()
    days = (times[-1] - times[0] + MINUTE_MS) / DAY_MS
    gain = float(equity[-1] / initial)
    result = {
        **drawdown(equity, times, initial),
        "duration_days": days, "return": gain - 1.0,
        "cagr": gain ** (365.25 / days) - 1.0 if gain > 0 else -1.0,
        "final_equity": float(equity[-1]),
        "balance_mdd": drawdown(account["balance"].to_numpy(), times, initial)["mdd"],
    }
    return result


def period_metrics(account: pd.DataFrame, initial: float) -> pd.DataFrame:
    months = account.index.tz_localize(None).to_period("M")
    previous_balance, previous_equity = initial, initial
    rows = []
    for month, group in account.groupby(months, sort=True):
        times = group.index.astype("int64").to_numpy() // 1_000_000
        dd = drawdown(group["equity"].to_numpy(), times, previous_equity)
        bd = drawdown(group["balance"].to_numpy(), times, previous_balance)
        rows.append({
            "month": str(month), "start_ms": int(times[0]), "end_ms": int(times[-1]),
            "observed_days": len(group) / 1440,
            "return": float(group["equity"].iloc[-1] / previous_equity - 1.0),
            "mdd": dd["mdd"], "balance_mdd": bd["mdd"],
            "final_equity": float(group["equity"].iloc[-1]),
            "max_unrealized_loss_usdt": float(
                (group["balance"] - group["equity"]).clip(lower=0).max()),
            "turnover_usdt": float(group["turnover_notional"].sum()),
        })
        previous_equity, previous_balance = (
            float(group["equity"].iloc[-1]), float(group["balance"].iloc[-1]))
    return pd.DataFrame(rows)


def apply_cost_overlay(
    account: pd.DataFrame, *, extra_bps: float, funding_bps_per_8h: float,
    terminal_exit_bps: float = 8.0,
) -> pd.DataFrame:
    """Fixed-ledger stress, not an endogenous re-run or historical funding data."""
    if min(extra_bps, funding_bps_per_8h, terminal_exit_bps) < 0:
        raise ValueError("stress charges must be nonnegative")
    costs = account["turnover_notional"] * (extra_bps / 10_000)
    # Prior-minute gross notional avoids charging new fills before they exist.
    costs += account["gross_mark_notional"].shift(1, fill_value=0.0) * (
        funding_bps_per_8h / 10_000 / 480
    )
    costs.iloc[-1] += float(account["gross_mark_notional"].iloc[-1]) * (
        terminal_exit_bps / 10_000
    )
    result = account.copy()
    cumulative = costs.cumsum()
    for column in ("equity", "balance", "adverse_hl_equity"):
        result[column] -= cumulative
    result.attrs["extra_cost_usdt"] = float(costs.sum())
    return result


def audit_execution(
    audit: pd.DataFrame, fills: pd.DataFrame, timestamps: np.ndarray,
    hlcvs: np.ndarray, coins: list[str], delay_bars: int,
    panic_market: bool, market_slippage_pct: float, price_steps: dict[str, float],
) -> dict[str, Any]:
    if len(audit) != len(fills):
        raise AssertionError("execution audit/fill row mismatch")
    if not len(audit):
        return {"fill_rows": 0, "preactivation_fills": 0, "crossing_failures": 0}
    decision = audit["decision_index"].to_numpy(dtype=np.int64)
    activation = audit["activation_index"].to_numpy(dtype=np.int64)
    filled = audit["fill_index"].to_numpy(dtype=np.int64)
    if (
        (decision < 0).any() or (filled >= len(timestamps)).any()
        or not (activation - decision == delay_bars + 1).all()
        or not (filled >= activation).all()
        or audit["order_id"].duplicated().any()
    ):
        raise AssertionError("invalid lifecycle boundary")
    for field, expected in {
        "decision_close_timestamp_ms": timestamps[decision] + MINUTE_MS,
        "activation_timestamp_ms": timestamps[activation],
        "fill_candle_open_timestamp_ms": timestamps[filled],
        "fill_candle_close_timestamp_ms": timestamps[filled] + MINUTE_MS,
    }.items():
        if not np.array_equal(audit[field].to_numpy(dtype=np.int64), expected):
            raise AssertionError(f"audit timestamp mismatch: {field}")
    for field, fill_field in (("symbol", "coin"), ("pside", "position_side"),
                              ("order_type", "type")):
        if not np.array_equal(audit[field], fills[fill_field]):
            raise AssertionError(f"audit fill mismatch: {field}")
    for field, fill_field in (("fill_qty", "qty"), ("fill_price", "price")):
        if not np.allclose(audit[field], fills[fill_field], atol=1e-10, rtol=0):
            raise AssertionError(f"audit fill mismatch: {field}")
    if not np.array_equal(timestamps[filled], fills["timestamp"].to_numpy()):
        raise AssertionError("audit/fill time mismatch")
    mapping = {coin: idx for idx, coin in enumerate(coins)}
    column = np.array([mapping[coin] for coin in fills["coin"]])
    buy = fills["qty"].to_numpy() > 0
    price = fills["price"].to_numpy()
    crosses = np.where(buy, hlcvs[filled, column, 1] < price,
                       hlcvs[filled, column, 0] > price)
    market = fills["liquidity"].eq("taker").to_numpy()
    if market.any():
        if not panic_market or not fills.loc[market, "type"].str.startswith("close_panic").all():
            raise AssertionError("unexpected market fills")
        expected = hlcvs[filled, column, 2] * np.where(
            buy, 1.0 + market_slippage_pct, 1.0 - market_slippage_pct)
        step = np.array([price_steps[coin] for coin in fills["coin"]])
        signed_rounding = (price - expected) * np.where(buy, 1.0, -1.0)
        if (
            (signed_rounding[market] < -1e-8).any()
            or (signed_rounding[market] > step[market] + 1e-8).any()
            or not np.allclose(price[market] / step[market],
                               np.round(price[market] / step[market]),
                               rtol=0.0, atol=1e-7)
        ):
            raise AssertionError("market fill differs from slipped close rounded adversely to tick")
    if not crosses[~market].all():
        raise AssertionError("non-market fill violates strict H/L crossing")
    age = filled - activation
    closes = fills["type"].str.startswith("close_").to_numpy()
    return {
        "fill_rows": len(fills), "preactivation_fills": 0,
        "crossing_failures": 0, "strict_crossing_limit_rows": int((~market).sum()),
        "market_rows": int(market.sum()), "activation_delay_bars": delay_bars + 1,
        "filled_on_activation_share": float(np.mean(age == 0)),
        "close_decision_to_fill_median_bars": float(np.median((filled - decision)[closes]))
        if closes.any() else None,
        "close_resting_age_p95_bars": float(np.quantile(age[closes], 0.95))
        if closes.any() else None,
        "close_resting_age_max_bars": int(age[closes].max()) if closes.any() else None,
    }
