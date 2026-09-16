#!/usr/bin/env python3
"""Audit the entry-regime gate's time boundary on the real data.

The claim under audit: **every bar evaluated at minute `t` is gated by a moving average
that uses only daily closes completed strictly before `t`.** This checks the claim against
a brute-force reference computed independently, rather than by re-reading the code that
produced the table.

What is checked per coin:

1. the regime in force on the first bar of UTC day D equals a simple moving-average cross
   computed from daily closes through day D-1 only, and never from day D's own close;
2. every regime boundary lands on the first bar of a UTC day, except the first boundary,
   which anchors at the series' first bar because the series starts mid-day;
3. the first day carries no regime (risk-off), which blocks entries rather than favouring
   them, so it cannot bias a result toward a favourable path;
4. the inverted table used by the short side is the exact complement at the same instants.

Exit code is non-zero when any check fails, so this can gate a release or a study.

Usage:
    venv/bin/python report_tools/audit_gate_causality.py [--cell g4_sma20_50] [--coins N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[4]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import backtest  # noqa: E402

STUDY = Path(__file__).resolve().parents[1]
CELLS = STUDY / "cells"
MINUTES_PER_DAY = 1440
MS_PER_MINUTE = 60_000
MS_PER_DAY = MINUTES_PER_DAY * MS_PER_MINUTE


def cell_config(cell: str, window: str, scenario: str) -> dict:
    path = CELLS / window / scenario / cell / "result.json"
    if not path.exists():
        raise SystemExit(f"no screening result for cell {cell!r} at {path}")
    return json.loads(path.read_text())["config"]


def daily_close_series(ts: np.ndarray, close: np.ndarray) -> tuple[list[int], np.ndarray]:
    """UTC day index -> last finite close, ascending by day."""
    days = (ts // MS_PER_MINUTE // MINUTES_PER_DAY).astype("int64")
    table: dict[int, float] = {}
    for day, value in zip(days, close):
        if np.isfinite(value):
            table[int(day)] = float(value)
    ordered = sorted(table)
    return ordered, np.array([table[day] for day in ordered], dtype="float64")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", default="g4_sma20_50")
    parser.add_argument("--window", default="full")
    parser.add_argument("--scenario", default="C1_binance_actual")
    parser.add_argument("--coins", type=int, default=8, help="how many coins to audit")
    args = parser.parse_args()

    cfg = cell_config(args.cell, args.window, args.scenario)
    gate_block = cfg.get("backtest", {}).get("entry_regime_gate")
    if not gate_block:
        raise SystemExit(f"cell {args.cell!r} carries no entry_regime_gate; nothing to audit")
    fast = int(gate_block["sma_fast_days"])
    slow = int(gate_block["sma_slow_days"])
    confirm_days = int(gate_block.get("confirm_days") or 0)
    print(
        f"cell={args.cell} window={args.window} scenario={args.scenario} "
        f"sma={fast}/{slow} confirm_days={confirm_days} mode={gate_block.get('gate_mode')}"
    )

    cfg = json.loads(json.dumps(cfg))
    cfg.setdefault("backtest", {})["base_dir"] = "backtests"
    coins, hlcvs, _mss, _rd, _cd, _btc, timestamps = asyncio.run(
        backtest.prepare_hlcvs_mss(cfg, "binance")
    )
    order = sorted(range(len(coins)), key=lambda idx: coins[idx])
    hlcvs = np.ascontiguousarray(hlcvs[:, order, :])
    coins = [coins[idx] for idx in order]
    ts = np.asarray(timestamps, dtype="int64")

    failures: list[str] = []
    compared = 0
    first_boundaries = 0

    for idx in range(min(args.coins, len(coins))):
        coin = coins[idx]
        close = hlcvs[:, idx, 2]
        transitions, regimes = backtest._entry_regime_transitions(
            ts, close, fast=fast, slow=slow, confirm_days=confirm_days
        )
        if not transitions:
            continue

        # (2) boundaries. The first may anchor at the series' first bar because the series
        # can start mid-day; every later one must be a UTC midnight.
        for position, stamp in enumerate(transitions):
            is_midnight = stamp % MS_PER_DAY == 0
            if position == 0:
                first_boundaries += 1
                if not is_midnight and stamp != int(ts[0]):
                    failures.append(
                        f"{coin}: first boundary {stamp} is neither a UTC midnight nor "
                        f"the series' first bar {int(ts[0])}"
                    )
                continue
            if not is_midnight:
                failures.append(f"{coin}: boundary {stamp} is not a UTC midnight")

        # (1) brute force, per completed day.
        days, closes = daily_close_series(ts, close)
        day_first_ts: dict[int, int] = {}
        day_index = (ts // MS_PER_MINUTE // MINUTES_PER_DAY).astype("int64")
        for day_value, stamp in zip(day_index, ts):
            day_first_ts.setdefault(int(day_value), int(stamp))

        for position, day in enumerate(days):
            stamp = day_first_ts[day]
            state: int | None = None
            for transition, regime in zip(transitions, regimes):
                if transition <= stamp:
                    state = int(regime)
                else:
                    break
            if state is None:
                # Before the first boundary: risk-off by contract.
                if position == 0:
                    continue
                failures.append(f"{coin}: day {day} precedes the first boundary")
                continue

            # Reference: SMA cross of closes through day D-1, with the same availability
            # rule (a window containing any unusable day yields no signal).
            expected = 0
            if position >= slow:
                fast_window = closes[position - fast : position]
                slow_window = closes[position - slow : position]
                if fast_window.size == fast and slow_window.size == slow:
                    if confirm_days > 0:
                        ok = True
                        for offset in range(confirm_days + 1):
                            end = position - offset
                            if end < slow:
                                ok = False
                                break
                            f = closes[end - fast : end]
                            s = closes[end - slow : end]
                            if f.size != fast or s.size != slow or not (f.mean() > s.mean()):
                                ok = False
                                break
                        expected = 1 if ok else 0
                    else:
                        expected = 1 if fast_window.mean() > slow_window.mean() else 0
            if state != expected:
                failures.append(
                    f"{coin}: day {day} gate={state} but prior-days-only SMA cross={expected}"
                )
            compared += 1

    # (4) the inverted table is the exact complement at the same instants.
    transitions, regimes = backtest._entry_regime_transitions(
        ts, hlcvs[:, 0, 2], fast=fast, slow=slow, confirm_days=confirm_days
    )
    inv_t, inv_r = backtest._invert_regime_table(transitions, regimes)
    if inv_t != list(transitions) or inv_r != [1 - value for value in regimes]:
        failures.append("inverted table is not the exact complement at the same instants")

    print(f"coins audited={min(args.coins, len(coins))} day comparisons={compared}")
    print(f"first boundaries checked={first_boundaries}")
    if failures:
        print(f"FAIL ({len(failures)}):")
        for line in failures[:20]:
            print("  " + line)
        return 1
    print("PASS: every day's gate matches a prior-days-only SMA cross; boundaries are UTC")
    print("      midnights after the first; the short-side table is the exact complement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
