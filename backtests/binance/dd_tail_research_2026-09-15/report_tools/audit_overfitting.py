#!/usr/bin/env python3
"""Overfitting and selection-fragility audit for the tail-drawdown lever screen.

Offline only. Implements the combinatorially symmetric cross-validation (CSCV) estimate of
the probability of backtest overfitting (PBO) over the configurations that were actually
screened, plus a walk-forward comparison of the in-sample selection window against the
locked out-of-sample window.

The audit describes how fragile "pick the best configuration of this screen" is on this
history. It is not a claim about future performance.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/dd_tail_research_2026-09-15"
CONTRACT = STUDY / "research_contract_v4.json"
CELLS = STUDY / "cells"
ANALYSIS = STUDY / "analysis"
DEFAULT_BLOCKS = (8, 10, 12, 16)


def load_json(path: Path) -> Any:
    with path.open() as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    os.replace(tmp, path)


def load_panel(
    scenario: str, window: str
) -> tuple[list[str], list[str], np.ndarray, dict[str, Any]]:
    """Return (cell ids, month labels, returns matrix [n_cells, n_months], provenance).

    CSCV needs a rectangular panel, so only months covered by *every* curve are used. Some
    curves end early -- a cell that liquidates stops producing returns -- and those truncations
    are reported in the provenance rather than silently shrinking the panel.
    """
    contract = load_json(CONTRACT)
    cell_ids = ["baseline"] + [c["cell_id"] for c in contract["cells"]]
    per_cell: dict[str, dict[str, float]] = {}
    spans: dict[str, tuple[str, str]] = {}
    for cell_id in cell_ids:
        path = CELLS / window / scenario / cell_id / "result.json"
        if not path.exists():
            raise SystemExit(
                f"missing cell {cell_id!r} for {window}/{scenario}: run the study first ({path})"
            )
        metrics = load_json(path)["metrics"]
        monthly = metrics.get("monthly_returns")
        if not monthly:
            raise SystemExit(
                f"cell {cell_id!r} in {window}/{scenario} has no monthly_returns; "
                "this audit needs the monthly series, not half-year buckets"
            )
        per_cell[cell_id] = {bucket["start"]: float(bucket["return"]) for bucket in monthly}
        cell_months = sorted(per_cell[cell_id])
        spans[cell_id] = (cell_months[0], cell_months[-1])

    months = sorted({month for series in per_cell.values() for month in series})
    complete = [m for m in months if all(m in series for series in per_cell.values())]
    if len(complete) < 12:
        raise SystemExit(f"only {len(complete)} common months; not enough for CSCV")
    matrix = np.array(
        [[per_cell[cell_id][month] for month in complete] for cell_id in cell_ids], dtype=float
    )
    if not np.isfinite(matrix).all():
        bad = [cell_ids[i] for i in range(matrix.shape[0]) if not np.isfinite(matrix[i]).all()]
        raise SystemExit(f"non-finite monthly returns for cells: {bad}")

    widest = max(spans.values(), key=lambda span: span[1])[1]
    truncated = {
        cell_id: {"first_month": span[0], "last_month": span[1]}
        for cell_id, span in sorted(spans.items())
        if span[1] != widest
    }
    provenance = {
        "n_months_union": len(months),
        "n_months_common": len(complete),
        "union_first_month": months[0],
        "union_last_month": months[-1],
        "common_first_month": complete[0],
        "common_last_month": complete[-1],
        "truncated_cells": truncated,
        "panel_note": (
            "The panel is the intersection of the months every curve covers, because CSCV "
            "needs a rectangular matrix. `truncated_cells` lists curves that end before the "
            "widest one; a cell that liquidates stops producing returns, and the intersection "
            "is what survives."
        ),
    }
    return cell_ids, complete, matrix, provenance


def sharpe(returns: np.ndarray) -> float:
    arr = np.asarray(returns, dtype=float)
    if arr.size < 2:
        return float("nan")
    std = float(np.std(arr, ddof=1))
    if not math.isfinite(std) or std == 0.0:
        return float("nan")
    return float(np.mean(arr) / std * math.sqrt(12.0))


def cscv(matrix: np.ndarray, n_blocks: int, cell_ids: list[str]) -> dict[str, Any]:
    """Combinatorially symmetric cross-validation over `n_blocks` contiguous blocks."""
    n_cells, n_months = matrix.shape
    if n_blocks % 2 != 0:
        raise ValueError("n_blocks must be even")
    if n_blocks > n_months:
        raise ValueError(f"n_blocks={n_blocks} exceeds {n_months} months")
    edges = np.linspace(0, n_months, n_blocks + 1).astype(int)
    blocks = [matrix[:, edges[i] : edges[i + 1]] for i in range(n_blocks)]
    half = n_blocks // 2

    logits: list[float] = []
    best_in_sample: list[str] = []
    selected_perf: list[tuple[float, float]] = []
    for combo in combinations(range(n_blocks), half):
        complement = [b for b in range(n_blocks) if b not in combo]
        is_returns = np.concatenate([blocks[b] for b in combo], axis=1)
        oos_returns = np.concatenate([blocks[b] for b in complement], axis=1)
        is_scores = np.array([sharpe(is_returns[i]) for i in range(n_cells)])
        oos_scores = np.array([sharpe(oos_returns[i]) for i in range(n_cells)])
        if not np.isfinite(is_scores).any() or not np.isfinite(oos_scores).any():
            continue
        best = int(np.nanargmax(is_scores))
        order = np.argsort(np.argsort(oos_scores))
        rank = int(order[best])
        omega = (rank + 1) / (n_cells + 1)
        logits.append(math.log(omega / (1.0 - omega)))
        best_in_sample.append(cell_ids[best])
        selected_perf.append((float(is_scores[best]), float(oos_scores[best])))

    if not logits:
        raise SystemExit("CSCV produced no usable splits")

    logits_arr = np.array(logits, dtype=float)
    pbo = float(np.mean(logits_arr <= 0.0))
    perf = np.array(selected_perf, dtype=float)
    slope = intercept = float("nan")
    if perf.shape[0] >= 3 and float(np.std(perf[:, 0])) > 0.0:
        slope, intercept = np.polyfit(perf[:, 0], perf[:, 1], 1).tolist()
    counts = {cell_id: best_in_sample.count(cell_id) for cell_id in sorted(set(best_in_sample))}
    return {
        "n_blocks": n_blocks,
        "splits": len(logits),
        "pbo": pbo,
        "logit_mean": float(np.mean(logits_arr)),
        "logit_median": float(np.median(logits_arr)),
        "logit_q05": float(np.quantile(logits_arr, 0.05)),
        "logit_q95": float(np.quantile(logits_arr, 0.95)),
        "is_best_sharpe_mean": float(np.mean(perf[:, 0])),
        "oos_selected_sharpe_mean": float(np.mean(perf[:, 1])),
        "is_oos_slope": float(slope),
        "is_oos_intercept": float(intercept),
        "most_frequent_is_best": sorted(counts.items(), key=lambda kv: -kv[1])[:5],
    }


def walk_forward(scenario: str, candidate_id: str) -> dict[str, Any]:
    """Selection-window vs locked-holdout metrics for the baseline and the candidate."""
    out: dict[str, Any] = {}
    for label, cell_id in (("baseline", "baseline"), ("candidate", candidate_id)):
        row: dict[str, Any] = {}
        for tag, (window, scen) in {
            "selection": ("selection", scenario),
            "holdout": ("holdout", "C3_conservative"),
        }.items():
            path = CELLS / window / scen / cell_id / "result.json"
            if not path.exists():
                row[tag] = None
                continue
            m = load_json(path)["metrics"]
            row[tag] = {
                "scenario": scen,
                "mdd": m["minute_close_mdd"],
                "cagr": m["cagr"],
                "gain": m["gain_strategy_eq"],
                "underwater_days": m["total_underwater_days"],
            }
        out[label] = row
    return out


def screen_best(scenario: str, window: str) -> dict[str, Any]:
    """Best cell by CAGR and best by drawdown, as the screen itself would have selected."""
    contract = load_json(CONTRACT)
    rows = []
    for cell in contract["cells"]:
        path = CELLS / window / scenario / cell["cell_id"] / "result.json"
        if not path.exists():
            continue
        m = load_json(path)["metrics"]
        rows.append(
            {
                "cell_id": cell["cell_id"],
                "group": cell["group"],
                "mdd": m["minute_close_mdd"],
                "cagr": m["cagr"],
            }
        )
    if not rows:
        return {}
    by_cagr = max(rows, key=lambda r: r["cagr"])
    by_mdd = min(rows, key=lambda r: r["mdd"])
    return {"best_by_cagr": by_cagr, "best_by_drawdown": by_mdd, "n_configs": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=None, help="defaults to the v4 primary scenario")
    parser.add_argument("--window", default="full")
    parser.add_argument("--candidate-id", default="combo_twel100_ddf060_ddthr0030")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    contract = load_json(CONTRACT)
    scenario = args.scenario or contract["primary_scenario"]
    cell_ids, months, matrix, panel = load_panel(scenario, args.window)
    print(
        f"panel: {matrix.shape[0]} configurations x {matrix.shape[1]} months ({scenario}/{args.window}); "
        f"union {panel['n_months_union']} months, {panel['common_first_month']} -> "
        f"{panel['common_last_month']}"
    )
    for truncating_cell, span in panel["truncated_cells"].items():
        print(
            f"  panel truncated by {truncating_cell}: ends {span['last_month']} "
            f"(widest curve ends {panel['union_last_month']})"
        )

    results: dict[str, Any] = {
        "scenario": scenario,
        "window": args.window,
        "n_configurations": int(matrix.shape[0]),
        "n_months": int(matrix.shape[1]),
        "months": months,
        "panel": panel,
        "cscv": {},
        "walk_forward": walk_forward(scenario, args.candidate_id),
        "screen_selection": screen_best(scenario, args.window),
        "interpretation": (
            "PBO estimates how often the configuration that looks best on half of this history "
            "lands below the median of the same screen on the other half. It measures selection "
            "fragility of THIS screen on THIS history; it is not a probability of future loss and "
            "not evidence that any configuration is deployable."
        ),
    }
    for n_blocks in DEFAULT_BLOCKS:
        if n_blocks > matrix.shape[1]:
            continue
        block_result = cscv(matrix, n_blocks, cell_ids)
        results["cscv"][f"S{n_blocks}"] = block_result
        print(
            f"  S={n_blocks:2d} splits={block_result['splits']:4d} PBO={block_result['pbo']:.3f} "
            f"IS best Sharpe {block_result['is_best_sharpe_mean']:.2f} -> "
            f"OOS {block_result['oos_selected_sharpe_mean']:.2f} "
            f"(slope {block_result['is_oos_slope']:.2f})"
        )

    print("\nwalk-forward (baseline vs candidate):")
    for label, row in results["walk_forward"].items():
        sel, hold = row.get("selection"), row.get("holdout")
        sel_txt = f"mdd {sel['mdd']*100:.2f}% cagr {sel['cagr']*100:+.2f}%" if sel else "n/a"
        hold_txt = f"mdd {hold['mdd']*100:.2f}% cagr {hold['cagr']*100:+.2f}%" if hold else "n/a"
        print(f"  {label:10s} selection: {sel_txt}   holdout(v3 conservative): {hold_txt}")

    best = results["screen_selection"]
    if best:
        print(
            f"\nscreen extremes: best CAGR {best['best_by_cagr']['cell_id']} "
            f"({best['best_by_cagr']['cagr']*100:+.2f}%), "
            f"lowest MDD {best['best_by_drawdown']['cell_id']} "
            f"({best['best_by_drawdown']['mdd']*100:.2f}%) out of {best['n_configs']} configs"
        )

    out_path = Path(args.out) if args.out else ANALYSIS / "overfitting_audit.json"
    write_json(out_path, results)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()