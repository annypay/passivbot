#!/usr/bin/env python3
"""Occupancy and exposure geometry for the g4 @ TWE 3.0 risk-geometry study.

The user's question for this round is about *occupancy*: with `we_excess_allowance_pct > 0` a
single coin may spend the exposure budget of the slots that are still empty, so the book can
concentrate in a few names; with `= 0` every coin is capped at its equal share
`total_wallet_exposure_limit / n_positions`. This module turns that claim into numbers derived
from the arm's own artifacts:

* the engine's per-slot cap and the *effective* allowance, using the same bounded-mode formula
  the engine applies (`min(raw, total/base - 1)`, i.e. `min(raw, n_positions - 1)` here),
* the observed concentration: peak per-coin exposure, the worst coin's share of the peak total
  exposure, the top-3 sum, and the single-coin wipe-out bound,
* the occupancy itself: how many coins actually held a position over time (time-weighted, from
  the fill ledger's position sizes), how long slots stayed empty, and how much of the window had
  any position at all,
* the distance to the engine's liquidation floor at the observed peak exposure.

Everything is a pure function of the run directory (fills, equity series, `config.json`,
`analysis.json`), so the renderer and the independent verifier compute the same numbers from the
same inputs.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

import event_windows as events
import wipeout_matrix as wipeout

CONFIG_NAME = "config.json"
FLAT_TOLERANCE = 1e-12
#: The engine enforces the per-slot cap when it *plans* an order, from the balance at that
#: moment; `fills.csv` records `wallet_exposure` at the fill. Two measured effects keep the
#: realized reading above the cap: mark-to-market drift between fills, and the exchange's
#: minimum order size, which forces an add that the cap would have cropped to zero to trade the
#: smallest tradable quantity anyway (this study's `we_excess_allowance_pct = 0` arms show up to
#: ~3.5% overshoot, and the overshooting fills carry the instrument's minimum quantity).
#: The invariant is therefore checked with a 5% execution-slack tolerance, and the observed
#: overshoot plus the fill that caused it are reported instead of being hidden.
PER_SLOT_CAP_TOLERANCE_PCT = 0.05
#: Absolute partner of the relative band: an add the cap would have cropped to zero still trades
#: the instrument's minimum quantity, which on a 10k account is worth up to ~2% of the balance.
#: Measured across this study's arms the absolute overshoot never exceeds 0.020 exposure units
#: (the largest was 0.0191 on the tightest cap, TWE 2.5 / 7 slots), so the invariant is
#: `peak_coin <= cap * (1 + 5%) + 0.02`.
PER_SLOT_CAP_ABSOLUTE_SLACK = 0.02


def bounded_effective_allowance(
    *,
    raw_allowance: float,
    wallet_exposure_limit: float,
    total_wallet_exposure_limit: float,
    mode: str = "bounded",
) -> float:
    """The allowance the engine actually applies (`src/risk_limits.py`).

    In `legacy_raw` mode the raw value is used; in `bounded` mode it is clamped to
    `total/base - 1`, which for this study's geometry (`base = total / n_positions`) is
    `n_positions - 1` — never binding inside the declared ranges, so the raw value is what the
    report must quote. The formula is re-implemented here (instead of importing `src/`) so the
    verifier does not depend on the engine package; the unit tests cross-check it against
    `src.risk_limits.effective_we_excess_allowance_pct`.
    """
    raw = max(0.0, float(raw_allowance))
    if str(mode).strip().lower() == "legacy_raw":
        return raw
    base = float(wallet_exposure_limit)
    total = float(total_wallet_exposure_limit)
    if base > 0.0 and total > 0.0:
        return min(raw, max(0.0, total / base - 1.0))
    return raw


def cap_within_tolerance(per_slot_cap: float, peak_coin_exposure: float) -> bool:
    """Whether a realized per-coin peak is inside the cap plus its measured execution slack."""
    if per_slot_cap <= 0.0:
        return False
    limit = per_slot_cap * (1.0 + PER_SLOT_CAP_TOLERANCE_PCT) + PER_SLOT_CAP_ABSOLUTE_SLACK
    return float(peak_coin_exposure) <= limit + 1e-9


def geometry_declaration(config: dict[str, Any]) -> dict[str, Any]:
    """The exposure geometry the engine was actually served, read from the run's config."""
    risk = ((config.get("bot") or {}).get("long") or {}).get("risk") or {}
    backtest = config.get("backtest") or {}
    twe = float(risk.get("total_wallet_exposure_limit") or 0.0)
    slots = float(risk.get("n_positions") or 0.0)
    raw_allowance = float(risk.get("we_excess_allowance_pct") or 0.0)
    mode = str(risk.get("we_excess_allowance_mode") or "bounded")
    base = twe / slots if slots > 0 else 0.0
    effective = bounded_effective_allowance(
        raw_allowance=raw_allowance,
        wallet_exposure_limit=base,
        total_wallet_exposure_limit=twe,
        mode=mode,
    )
    return {
        "total_wallet_exposure_limit": twe,
        "n_positions": slots,
        "we_excess_allowance_pct": raw_allowance,
        "we_excess_allowance_mode": mode,
        "slot_share": base,
        "effective_allowance_pct": effective,
        "per_slot_cap": base * (1.0 + effective),
        "starting_balance": float(backtest.get("starting_balance") or 0.0),
        "liquidation_threshold": float(backtest.get("liquidation_threshold") or 0.0),
        "source": CONFIG_NAME,
    }


def occupancy_timeline(
    fills: pd.DataFrame,
    *,
    close_cash_minutes: float = 5.0,
) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Segments of constant active-coin count, derived from the fill ledger.

    `fills.csv` records the position size *after* each fill, so a coin re-opens when a fill
    leaves `psize != 0` and closes when a fill leaves `psize == 0`. Fills sharing a timestamp
    are applied together (they are one instant), and a segment runs to the next timestamp.
    """
    if fills.empty or not {"timestamp", "coin", "psize"} <= set(fills.columns):
        return []
    frame = fills.dropna(subset=["timestamp", "coin", "psize"]).sort_values("timestamp")
    if frame.empty:
        return []
    open_coins: set[str] = set()
    segments: list[tuple[pd.Timestamp, pd.Timestamp, int]] = []
    index = 0
    stamps = list(frame["timestamp"])
    coins = list(frame["coin"])
    sizes = list(frame["psize"])
    while index < len(stamps):
        stamp = stamps[index]
        while index < len(stamps) and stamps[index] == stamp:
            coin = str(coins[index])
            if abs(float(sizes[index])) > FLAT_TOLERANCE:
                open_coins.add(coin)
            else:
                open_coins.discard(coin)
            index += 1
        if index < len(stamps):
            segments.append((stamp, stamps[index], len(open_coins)))
    return segments


def occupancy_summary(
    fills: pd.DataFrame,
    *,
    window_start: pd.Timestamp | None,
    window_end: pd.Timestamp | None,
    n_positions: float,
) -> dict[str, Any]:
    """Time-weighted occupancy readings over the run window.

    The window is the equity series' span, so time before the first fill (gate warmup, a halt)
    and after the last fill counts as "no position" rather than being hidden by the fill span.
    """
    segments = occupancy_timeline(fills)
    if window_start is None or window_end is None or window_end <= window_start:
        return {
            "window_minutes": 0.0,
            "active_coins_mean": None,
            "active_coins_peak": None,
            "empty_slot_time_share": None,
            "time_with_any_position_share": None,
            "position_minutes": 0.0,
            "segments": len(segments),
        }
    total_minutes = (window_end - window_start).total_seconds() / 60.0
    weighted = 0.0
    position_minutes = 0.0
    peak = 0
    for start, end, active in segments:
        start = max(start, window_start)
        end = min(end, window_end)
        minutes = (end - start).total_seconds() / 60.0
        if minutes <= 0:
            continue
        weighted += minutes * active
        if active > 0:
            position_minutes += minutes
        peak = max(peak, active)
    mean_active = weighted / total_minutes if total_minutes > 0 else None
    empty_share = None
    if mean_active is not None and n_positions > 0:
        empty_share = max(0.0, min(1.0, (n_positions - mean_active) / n_positions))
    return {
        "window_minutes": total_minutes,
        "active_coins_mean": mean_active,
        "active_coins_peak": float(peak),
        "empty_slot_time_share": empty_share,
        "time_with_any_position_share": (
            position_minutes / total_minutes if total_minutes > 0 else None
        ),
        "position_minutes": position_minutes,
        "segments": len(segments),
    }


def cap_overshoot_fill(fills: pd.DataFrame, coin: str) -> dict[str, Any] | None:
    """The fill that took one coin to its largest absolute wallet exposure."""
    if fills.empty or not {"coin", "wallet_exposure"} <= set(fills.columns):
        return None
    subset = fills[fills["coin"].astype(str) == str(coin)]
    if subset.empty:
        return None
    exposure = subset["wallet_exposure"].abs()
    row = subset.loc[exposure.idxmax()]
    return {
        "coin": str(coin),
        "timestamp": str(row.get("timestamp")),
        "type": str(row.get("type")),
        "wallet_exposure": float(exposure.loc[exposure.idxmax()]),
        "qty": None if pd.isna(row.get("qty")) else float(row.get("qty")),
        "price": None if pd.isna(row.get("price")) else float(row.get("price")),
        "balance_usd": (
            None if pd.isna(row.get("usd_total_balance")) else float(row.get("usd_total_balance"))
        ),
    }


def build_geometry_artifact(run_dir: Path, analysis: dict[str, Any]) -> dict[str, Any]:
    """Derive one arm's occupancy/exposure geometry from its own artifacts."""
    run_dir = Path(run_dir)
    config = json.loads((run_dir / CONFIG_NAME).read_text(encoding="utf-8"))
    declared = geometry_declaration(config)
    fills = events.load_fills(run_dir)
    equity = events.load_equity(run_dir)
    series = equity["usd_total_equity"].dropna() if not equity.empty else pd.Series(dtype=float)
    window_start = series.index[0] if len(series) else None
    window_end = series.index[-1] if len(series) else None

    per_coin = wipeout.per_coin_max_exposure(fills)
    peak_coin = float(per_coin[0][1]) if per_coin else 0.0
    top3 = float(sum(value for _coin, value in per_coin[:3]))
    peak_total = float(analysis.get("total_wallet_exposure_max") or 0.0)
    mean_total = analysis.get("total_wallet_exposure_mean")
    peak_balance = None
    if not fills.empty and {"twe_long", "usd_total_balance"} <= set(fills.columns):
        clean = fills.dropna(subset=["twe_long", "usd_total_balance"])
        if not clean.empty:
            peak_balance = float(clean.loc[clean["twe_long"].idxmax(), "usd_total_balance"])
    shock = wipeout.liquidation_shock(
        peak_exposure=peak_total,
        balance_usd=peak_balance if peak_balance is not None else 0.0,
        starting_balance=declared["starting_balance"],
        threshold=declared["liquidation_threshold"],
    )
    occupancy = occupancy_summary(
        fills,
        window_start=window_start,
        window_end=window_end,
        n_positions=declared["n_positions"],
    )
    observed = {
        "peak_total_exposure": peak_total,
        "mean_total_exposure": None if mean_total is None else float(mean_total),
        "peak_coin_exposure": peak_coin,
        "top3_exposure": top3,
        "peak_coin_share_of_peak_total": (
            peak_coin / peak_total if peak_total > 0 else None
        ),
        "active_coins_mean": occupancy["active_coins_mean"],
        "active_coins_peak": occupancy["active_coins_peak"],
        "empty_slot_time_share": occupancy["empty_slot_time_share"],
        "time_with_any_position_share": occupancy["time_with_any_position_share"],
        "liquidation_shock_at_peak": shock,
        "single_coin_wipeout_bound": peak_coin,
        "peak_exposure_balance_usd": peak_balance,
        "fills_count": int(fills.shape[0]) if not fills.empty else 0,
        "window_minutes": occupancy["window_minutes"],
        "position_minutes": occupancy["position_minutes"],
        "occupancy_segments": occupancy["segments"],
    }
    problems = geometry_checks(declared, observed)
    cap = declared["per_slot_cap"]
    observed["per_slot_cap"] = cap
    observed["per_slot_cap_excess_pct"] = (
        max(0.0, (peak_coin - cap) / cap) if cap > 0 else None
    )
    overshoot = None
    if per_coin and peak_coin > cap:
        overshoot = cap_overshoot_fill(fills, per_coin[0][0])
        if overshoot is not None:
            overshoot["cap"] = cap
            overshoot["excess_pct"] = peak_coin / cap - 1.0 if cap > 0 else None
    return {
        "arm_dir": run_dir.name,
        "declared": declared,
        "observed": observed,
        "per_coin": [[coin, value] for coin, value in per_coin],
        "cap_overshoot_fill": overshoot,
        "cross_check_problems": problems,
        "method": (
            "几何读数全部来自本 arm 的成交账本、权益序列与冻结配置：单槽上限用引擎的 "
            "bounded 模式公式（min(raw, TWE/(TWE/n) − 1)）；在场币数按成交时间戳分段做时间加权，"
            "窗口取权益序列首末采样（未持仓的时间计入分母）；单币归零上界 = 单币峰值敞口。"
            "单槽上限是“下单规划时”的约束，成交时的 wallet_exposure 会因为盯市漂移与"
            "交易所最小下单量（额度被裁到 0 时仍会成交最小可交易数量）而略高于上限，"
            f"因此该不变量用 {PER_SLOT_CAP_TOLERANCE_PCT:.0%} 的执行余量校验，"
            "实际超出比例与造成它的那笔成交记在 observed.per_slot_cap_excess_pct 与 cap_overshoot_fill。"
        ),
    }


def geometry_checks(declared: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Invariants the geometry must satisfy; a violation means the derivation is wrong."""
    problems: list[str] = []
    cap = declared["per_slot_cap"]
    peak_coin = observed["peak_coin_exposure"]
    if cap <= 0.0:
        problems.append("per-slot cap is not positive")
    elif not cap_within_tolerance(cap, peak_coin):
        problems.append(
            f"peak per-coin exposure {peak_coin:.6f} exceeds the per-slot cap {cap:.6f} beyond "
            f"the measured execution slack (cap x {1 + PER_SLOT_CAP_TOLERANCE_PCT:.2f} "
            f"+ {PER_SLOT_CAP_ABSOLUTE_SLACK:.2f})"
        )
    twe = declared["total_wallet_exposure_limit"]
    peak_total = observed["peak_total_exposure"]
    if twe > 0.0 and peak_total > twe + 1e-3:
        problems.append(
            f"peak total exposure {peak_total:.6f} exceeds the declared limit {twe:.6f}"
        )
    slots = declared["n_positions"]
    peak_active = observed["active_coins_peak"]
    if peak_active is not None and slots > 0 and float(peak_active) > slots + 1e-9:
        problems.append(
            f"{peak_active:.0f} coins held a position at once, more than n_positions={slots:.0f}"
        )
    mean_total = observed["mean_total_exposure"]
    if mean_total is not None and peak_total > 0.0 and float(mean_total) > peak_total + 1e-9:
        problems.append(
            f"mean total exposure {mean_total:.6f} exceeds the peak {peak_total:.6f}"
        )
    for key in ("empty_slot_time_share", "time_with_any_position_share"):
        value = observed.get(key)
        if value is not None and not (0.0 - 1e-9 <= float(value) <= 1.0 + 1e-9):
            problems.append(f"{key} = {value!r} is outside [0, 1]")
    if not math.isfinite(peak_coin):
        problems.append(f"peak per-coin exposure is not finite: {peak_coin!r}")
    return problems
