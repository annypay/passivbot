#!/usr/bin/env python3
"""Trace one martingale episode and re-derive the engine's sizing arithmetic from the ledger.

The study's reports say "the position grows by `double_down_factor` per add and is cropped at the
per-slot budget"; this tool proves that claim for a named episode of a named arm, from the arm's
own `fills.csv` and frozen `config.json`, and records the trace as tracked evidence.

For every entry fill of the episode it re-derives:

* the per-slot budget `base = TWE / n_positions x (1 + effective allowance)`,
* the initial-entry size `balance x base x initial_qty_pct`,
* the add size `max(double_down_factor x position_before, balance x base x initial_qty_pct)`
  rounded to the instrument's quantity step and floored at the exchange minimum,
* the crop that happens when the add would exceed the remaining slot budget,

and compares each derivation with what the ledger recorded. It also re-derives the account-level
panic close that ended the episode (whole-position size, the cohort of coins flattened in the same
minute, the realized loss and the drawdown telemetry the engine reported).

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import event_windows as events  # noqa: E402
import variant_spec as study  # noqa: E402

TRACE_NAME = "episode_trace.json"
#: Quantity rounding, the balance/price snapshot the engine used at planning time, and the
#: quantity step make an exact byte match impossible; 2% plus half a step is the measured band.
REL_TOLERANCE = 0.02


def market_params(variant: study.Variant, coin: str) -> dict[str, float]:
    """The instrument contract the engine was served with (`qty_step`, minimums, `c_mult`)."""
    path = variant.dataset.path / "market_specific_settings.json"
    if not path.exists():
        return {}
    settings = study.load_json(path).get(coin) or {}
    precision = settings.get("precision") or {}
    limits = settings.get("limits") or {}
    return {
        "qty_step": float(precision.get("amount") or 0.0),
        "price_step": float(precision.get("price") or 0.0),
        "min_qty": float((limits.get("amount") or {}).get("min") or 0.0),
        "min_cost": float((limits.get("cost") or {}).get("min") or 0.0),
        "c_mult": float(settings.get("contractSize") or 1.0),
    }


def effective_allowance(config: dict[str, Any]) -> dict[str, float]:
    """The engine's bounded-mode allowance and the per-slot budget (`entries.rs:40-66`)."""
    risk = ((config.get("bot") or {}).get("long") or {}).get("risk") or {}
    twe = float(risk.get("total_wallet_exposure_limit") or 0.0)
    slots = float(risk.get("n_positions") or 0.0)
    raw = max(0.0, float(risk.get("we_excess_allowance_pct") or 0.0))
    mode = str(risk.get("we_excess_allowance_mode") or "bounded")
    base = twe / slots if slots else 0.0
    if mode == "legacy_raw" or base <= 0.0 or twe <= 0.0:
        effective = raw
    else:
        effective = min(raw, max(0.0, twe / base - 1.0))
    return {
        "total_wallet_exposure_limit": twe,
        "n_positions": slots,
        "raw_allowance_pct": raw,
        "effective_allowance_pct": effective,
        "slot_cap": base * (1.0 + effective),
    }


def min_entry_qty(price: float, params: dict[str, float]) -> float:
    """`entries.rs:91-114`: the larger of the minimum quantity and the minimum notional, on grid."""
    step = params.get("qty_step") or 0.0
    if step <= 0.0:
        return 0.0
    raw = max(params.get("min_qty") or 0.0, (params.get("min_cost") or 0.0) / price if price > 0 else 0.0)
    steps = raw / step
    nearest = round(steps)
    nearest_step = nearest * step
    if nearest > 0 and abs(steps - nearest) <= 1e-8 and nearest_step >= raw - 1e-9:
        return max(nearest_step, raw)
    return math.ceil(steps) * step


def round_to_step(value: float, step: float) -> float:
    if step <= 0.0:
        return value
    return round(round(value / step) * step, 10)


def round_up_to_step(value: float, step: float) -> float:
    if step <= 0.0:
        return value
    return round(math.ceil(value / step) * step, 10)


def entry_params(config: dict[str, Any]) -> dict[str, Any]:
    strategy = (((config.get("bot") or {}).get("long") or {}).get("strategy") or {})
    block = strategy.get("trailing_martingale") or {}
    return {
        "entry": block.get("entry") or {},
        "close": block.get("close") or {},
        "kind": next(iter(strategy), None),
    }


def derive_episode(
    run_dir: Path, variant: study.Variant, coin: str, start: str | None, end: str | None
) -> dict[str, Any]:
    config = study.load_json(run_dir / "config.json")
    analysis = study.load_json(run_dir / "analysis.json")
    fills = events.load_fills(run_dir)
    params = market_params(variant, coin)
    allowance = effective_allowance(config)
    block = entry_params(config)
    entry = block["entry"]
    close = block["close"]
    initial_qty_pct = float(entry.get("initial_qty_pct") or 0.0)
    ddf = float(entry.get("double_down_factor") or 0.0)

    coin_fills = fills[fills["coin"].astype(str) == coin].sort_values("timestamp")
    panic_rows = coin_fills[coin_fills["type"].astype(str).str.contains(study.PANIC_FILL_MARKER)]
    entries = coin_fills[coin_fills["type"].astype(str).str.startswith("entry_")]
    if entries.empty:
        raise SystemExit(f"{variant.key}: no entry fills for {coin}")

    panic = None if panic_rows.empty else panic_rows.iloc[-1]
    if end is not None:
        window_end = pd.Timestamp(end, tz="UTC")
    elif panic is not None:
        window_end = panic["timestamp"]
    else:
        window_end = entries["timestamp"].iloc[-1]
    if start is not None:
        window_start = pd.Timestamp(start, tz="UTC")
    else:
        initials = entries[entries["type"].astype(str).str.startswith("entry_initial")]
        candidates = initials[initials["timestamp"] <= window_end]
        if candidates.empty:
            raise SystemExit(f"{variant.key}: no initial entry before {window_end} for {coin}")
        window_start = candidates["timestamp"].iloc[-1]

    ladder: list[dict[str, Any]] = []
    problems: list[str] = []
    position = 0.0
    for _index, row in entries[
        (entries["timestamp"] >= window_start) & (entries["timestamp"] <= window_end)
    ].iterrows():
        qty = float(row["qty"])
        price = float(row["price"])
        balance = float(row["usd_total_balance"])
        position_before = position
        position_after = float(row["psize"])
        exposure_after = float(row["wallet_exposure"])
        # `fills.csv` records the exposure *after* the fill; the engine cropped against the
        # exposure it saw *before* it, which this recovers exactly (exposure is linear in qty).
        exposure_before = (
            exposure_after - (qty * price / balance) if balance > 0.0 else 0.0
        )
        floor_qty = (balance * allowance["slot_cap"] * initial_qty_pct) / price if price > 0 else 0.0
        ddf_qty = position_before * ddf
        candidate = max(ddf_qty, floor_qty)
        minimum = min_entry_qty(price, params)
        step = params.get("qty_step") or 0.0
        candidate = max(minimum, round_to_step(candidate, step))
        # `entries.rs:292-338`: the crop only engages when the filled exposure would exceed the
        # slot budget by more than 1%, and it interpolates the quantity that lands exactly on it.
        we_if_filled = exposure_before + (candidate * price / balance if balance > 0.0 else 0.0)
        crop_applied = position_before > 0.0 and we_if_filled > allowance["slot_cap"] * 1.01
        if crop_applied:
            crop_qty = (
                (allowance["slot_cap"] - exposure_before) * balance / price if price > 0 else 0.0
            )
            expected = max(minimum, round_to_step(crop_qty, step))
        else:
            expected = candidate
        rel_error = abs(qty - expected) / max(abs(expected), 1e-12)
        matched = abs(qty - expected) <= max(step / 2 + 1e-9, REL_TOLERANCE * abs(expected))
        if not matched:
            problems.append(
                f"{coin} {row['timestamp']}: ledger qty {qty} vs derived {expected:.6f} "
                f"(ddf {ddf_qty:.6f}, floor {floor_qty:.6f}, "
                f"crop {'yes' if crop_applied else 'no'})"
            )
        ladder.append(
            {
                "timestamp": row["timestamp"].isoformat(),
                "type": str(row["type"]),
                "price": price,
                "balance_usd": balance,
                "position_before": position_before,
                "ledger_qty": qty,
                "position_after": position_after,
                "wallet_exposure_before": exposure_before,
                "wallet_exposure_after": exposure_after,
                "formula_double_down_qty": ddf_qty,
                "formula_initial_floor_qty": floor_qty,
                "uncropped_candidate_qty": candidate,
                "wallet_exposure_if_filled_uncropped": we_if_filled,
                "crop_applied": crop_applied,
                "min_entry_qty": minimum,
                "expected_qty": expected,
                "rel_error": rel_error,
                "matched": matched,
            }
        )
        position = position_after

    panic_block: dict[str, Any] | None = None
    if panic is not None:
        cohort = fills[fills["timestamp"] == panic["timestamp"]]
        cohort_panic = cohort[cohort["type"].astype(str).str.contains(study.PANIC_FILL_MARKER)]
        equity = events.load_equity(run_dir)
        series = equity["usd_total_equity"].dropna() if not equity.empty else pd.Series(dtype=float)
        balances = (
            equity["usd_total_balance"].dropna() if "usd_total_balance" in equity else pd.Series(dtype=float)
        )
        before = balances.loc[: panic["timestamp"]].iloc[:-1] if not balances.empty else pd.Series(dtype=float)
        prior_balance = float(before.iloc[-1]) if not before.empty else None
        realized = float(cohort_panic["pnl"].fillna(0.0).sum())
        position_before_panic = float(position)
        panic_block = {
            "timestamp": panic["timestamp"].isoformat(),
            "type": str(panic["type"]),
            "position_before": position_before_panic,
            "ledger_qty": float(panic["qty"]),
            "price": float(panic["price"]),
            "position_price": float(panic["pprice"]),
            "coin_move_from_average_pct": (
                float(panic["price"]) / float(panic["pprice"]) - 1.0
                if float(panic["pprice"]) > 0
                else None
            ),
            "realized_pnl_usd": float(panic["pnl"]),
            "cohort_coins": sorted(cohort_panic["coin"].astype(str).unique().tolist()),
            "cohort_realized_pnl_usd": realized,
            "prior_balance_usd": prior_balance,
            "account_loss_pct_of_prior_balance": (
                realized / prior_balance if prior_balance else None
            ),
            "telemetry_trigger_drawdown_mean": analysis.get("hard_stop_trigger_drawdown_mean"),
            "telemetry_confirm_drawdown_pct_mean": analysis.get(
                "hard_stop_panic_close_loss_drawdown_pct_mean"
            ),
            "telemetry_panic_loss_sum": analysis.get("hard_stop_panic_close_loss_sum"),
            "telemetry_triggers": analysis.get("hard_stop_triggers"),
            "closes_full_position": math.isclose(
                abs(float(panic["qty"])),
                abs(position_before_panic),
                rel_tol=1e-9,
                abs_tol=1e-9,
            ),
        }
        if not panic_block["closes_full_position"]:
            problems.append(
                f"{coin} {panic['timestamp']}: the panic fill closed {abs(float(panic['qty']))} "
                f"but the position was {abs(position_before_panic)}"
            )
        if abs(abs(realized) - float(analysis.get("hard_stop_panic_close_loss_sum") or 0.0)) > max(
            1.0, abs(realized) * 0.001
        ):
            problems.append(
                f"{coin} {panic['timestamp']}: the same-minute panic cohort realizes {realized:.2f} "
                f"USDT but the engine telemetry reports "
                f"{analysis.get('hard_stop_panic_close_loss_sum')}"
            )

    return {
        "arm": variant.key,
        "leg": variant.leg,
        "run_dir": study.relative(run_dir),
        "coin": coin,
        "window": [window_start.isoformat(), window_end.isoformat()],
        "declared": {
            **allowance,
            "initial_qty_pct": initial_qty_pct,
            "double_down_factor": ddf,
            "entry_threshold_base_pct": entry.get("threshold_base_pct"),
            "entry_retracement_base_pct": entry.get("retracement_base_pct"),
            "close_qty_pct": close.get("qty_pct"),
            "close_threshold_base_pct": close.get("threshold_base_pct"),
            "close_retracement_base_pct": close.get("retracement_base_pct"),
            "strategy_kind": block["kind"],
            **params,
        },
        "ladder": ladder,
        "panic": panic_block,
        "gate_note": (
            "入场闸门（entry_regime_gate）只拦入场：orchestrator.rs:2533-2537 明确 "
            "“Deliberately entry-only. Closes, panic, and auto-unstuck keep their own independent "
            "paths”，其判决只被 side_regime_verdict（:2556）在入场生成处（:3806、:3909）消费。"
            "因此本 episode 的离场不是门控造成的。"
        ),
        "cross_check_problems": problems,
        "method": (
            "逐笔复算引擎的下单算术：单槽额度 = TWE/n_positions × (1 + effective allowance)" 
            "（entries.rs:40-66）；初始入场 = balance × 额度 × initial_qty_pct（:68-89）；"
            "加仓 = max(double_down_factor × 持仓, 初始入场规模)，再按 qty_step 取整并以交易所最小"
            "下单量托底（:340-367）；额度不足时按剩余额度裁剪并把类型标为 cropped（:116-160）。"
            f"数量比对容差 = max(半个 qty_step, {REL_TOLERANCE:.0%})（下单时的余额/价格快照与"
            "成交时的账本值存在小幅差异）。"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=study.RUN_VARIANT_ORDER)
    parser.add_argument("--coin", default="ZEC")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    variant = study.VARIANTS_BY_KEY[args.variant]
    run_dir = study.find_variant_run_dir(variant)
    payload = derive_episode(run_dir, variant, args.coin, args.start, args.end)
    out_path = Path(args.out) if args.out else run_dir / TRACE_NAME
    study.write_json(out_path, payload)
    print(
        f"wrote {study.relative(out_path)}: {len(payload['ladder'])} ladder fill(s), "
        f"{len(payload['cross_check_problems'])} problem(s)"
    )
    for problem in payload["cross_check_problems"]:
        print(f"  - {problem}", file=sys.stderr)
    return 1 if payload["cross_check_problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
