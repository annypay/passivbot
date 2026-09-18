#!/usr/bin/env python3
"""Combinatorially symmetric cross-validation (CSCV) and the probability of backtest overfitting.

Offline only: reads the tracked panels written by :mod:`panel` and writes
``artifacts/cscv_pbo.json``.

What PBO means here
-------------------
For every way of splitting the panel's months into two equal halves, the arms are ranked on one
half (in-sample) and the *same* arm is ranked on the other half (out-of-sample). PBO is the share
of splits in which the in-sample winner lands at or below the median out-of-sample. It therefore
measures **how fragile "pick the best arm of this pool" is on this history**. It is not the
probability of losing money, and it cannot detect parameter time travel or selection provenance -
no statistic computed from these curves can, because the curves themselves carry no record of when
each parameter became knowable.

Degeneracy
----------
The degeneracy rule lives in :mod:`panel` and is applied *before* this module sees a matrix: arms
whose series stopped producing returns (liquidation, dataset clip) or whose return variance is
effectively zero are removed from the pool, so they can neither be selected nor inflate the
denominator of the rank statistic. ``artifacts/panels/*.json`` records exactly how many were
removed and why.

Selection conventions
---------------------
Two pre-registered conventions are reported side by side:

* ``max_adg``            - pick the arm with the highest in-sample annualised geometric growth;
* ``max_adg_x_1_minus_dd`` - pick the arm with the highest in-sample
  ``ADG x (1 - worst drawdown)``, the risk-adjusted variant the rounds actually used to choose
  between "more return" and "less drawdown".
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_overfitting_audit_2026-09-18"
ARTIFACTS = STUDY / "artifacts"
PANEL_DIR = ARTIFACTS / "panels"

#: Block counts the audit reports. One block count alone can be an accident of the calendar, so the
#: spread is part of the answer rather than a robustness footnote.
DEFAULT_BLOCKS = (8, 10, 12, 16)
SELECTIONS: tuple[str, ...] = ("max_adg", "max_adg_x_1_minus_dd")
FLOOR = 1e-12


def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    import os

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        handle.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------------------- score maths ---
def _growth(rets: np.ndarray) -> np.ndarray:
    """Annualised geometric growth per row; a wiped-out month floors at ``FLOOR``."""
    factors = np.clip(1.0 + np.asarray(rets, dtype=float), FLOOR, None)
    months = factors.shape[1]
    if months == 0:
        return np.full(factors.shape[0], np.nan)
    return np.exp(np.log(factors).sum(axis=1) / months * 12.0) - 1.0


def _worst_drawdown(rets: np.ndarray) -> np.ndarray:
    """Worst peak-to-trough drawdown of the chained equity over the given months."""
    factors = np.clip(1.0 + np.asarray(rets, dtype=float), FLOOR, None)
    equity = np.cumprod(factors, axis=1)
    start = np.ones((equity.shape[0], 1))
    equity = np.concatenate([start, equity], axis=1)
    peak = np.maximum.accumulate(equity, axis=1)
    drawdown = 1.0 - equity / peak
    return drawdown.max(axis=1)


def score_adg(rets: np.ndarray) -> np.ndarray:
    return _growth(rets)


def score_adg_x_1_minus_dd(rets: np.ndarray) -> np.ndarray:
    return _growth(rets) * (1.0 - _worst_drawdown(rets))


SCORE_FUNCTIONS: dict[str, Callable[[np.ndarray], np.ndarray]] = {
    "max_adg": score_adg,
    "max_adg_x_1_minus_dd": score_adg_x_1_minus_dd,
}

SELECTION_LABELS = {
    "max_adg": "max ADG（样本内年化几何增长最大）",
    "max_adg_x_1_minus_dd": "max ADG×(1−最差回撤)（样本内风险调整后最大）",
}


# ------------------------------------------------------------------------------- statistics ---
def _average_ranks(scores: np.ndarray) -> np.ndarray:
    """Ranks 1..n with tied values sharing their average rank (the standard convention).

    Exact ties are real here: arms whose protection never triggers inside a window produce
    bit-identical equity curves, so a positional tie-break would decide the answer.
    """
    order = np.argsort(scores, kind="stable")
    ranks = np.empty(scores.size, dtype=float)
    ranks[order] = np.arange(1, scores.size + 1, dtype=float)
    sorted_scores = scores[order]
    index = 0
    while index < scores.size:
        end = index
        while end + 1 < scores.size and sorted_scores[end + 1] == sorted_scores[index]:
            end += 1
        if end > index:
            ranks[order[index : end + 1]] = (index + 1 + end + 1) / 2.0
        index = end + 1
    return ranks


def _rank_percentile(scores: np.ndarray, index: int) -> float:
    """CSCV omega = average rank from worst, normalised into (0, 1).

    Values at or below 0.5 mean the selected arm sits in the bottom half out-of-sample; that is
    what PBO counts.
    """
    return float(_average_ranks(scores)[index] / (scores.size + 1))


def _spearman(left: np.ndarray, right: np.ndarray) -> float:
    if left.size < 3:
        return float("nan")
    a = _average_ranks(left)
    b = _average_ranks(right)
    a = a - a.mean()
    b = b - b.mean()
    denominator = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    if denominator == 0.0:
        return float("nan")
    return float((a * b).sum() / denominator)


def block_edges(n_months: int, n_blocks: int) -> list[int]:
    """Declared block boundary rule: ``edges[i] = floor(i*M/S)``, remainder in the earliest blocks.

    Integer arithmetic on purpose - a float ``linspace`` would make the answer depend on rounding.
    """
    return [(index * n_months) // n_blocks for index in range(n_blocks + 1)]


def cscv(matrix: np.ndarray, n_blocks: int, arm_ids: list[str], selection: str) -> dict[str, Any]:
    """CSCV over ``n_blocks`` contiguous month blocks under one selection convention."""
    if n_blocks % 2 != 0:
        raise ValueError("n_blocks must be even")
    n_arms, n_months = matrix.shape
    if n_blocks > n_months:
        return {
            "n_blocks": n_blocks,
            "splits": 0,
            "skipped": f"n_blocks={n_blocks} exceeds the panel's {n_months} months",
        }
    score = SCORE_FUNCTIONS[selection]
    edges = block_edges(n_months, n_blocks)
    blocks = [matrix[:, edges[i] : edges[i + 1]] for i in range(n_blocks)]
    half = n_blocks // 2

    logits: list[float] = []
    is_selected: list[float] = []
    oos_selected: list[float] = []
    oos_percentiles: list[float] = []
    spearmans: list[float] = []
    winners: list[str] = []

    for combo in itertools.combinations(range(n_blocks), half):
        complement = [block for block in range(n_blocks) if block not in combo]
        is_returns = np.concatenate([blocks[block] for block in combo], axis=1)
        oos_returns = np.concatenate([blocks[block] for block in complement], axis=1)
        is_scores = score(is_returns)
        oos_scores = score(oos_returns)
        if not np.isfinite(is_scores).any() or not np.isfinite(oos_scores).any():
            continue
        best = int(np.nanargmax(np.where(np.isfinite(is_scores), is_scores, -np.inf)))
        percentile = _rank_percentile(oos_scores, best)
        logits.append(math.log(percentile / (1.0 - percentile)))
        is_selected.append(float(is_scores[best]))
        oos_selected.append(float(oos_scores[best]))
        oos_percentiles.append(percentile)
        spearmans.append(_spearman(is_scores, oos_scores))
        winners.append(arm_ids[best])

    if not logits:
        return {"n_blocks": n_blocks, "splits": 0, "skipped": "no usable split"}

    logit_array = np.array(logits, dtype=float)
    is_array = np.array(is_selected, dtype=float)
    oos_array = np.array(oos_selected, dtype=float)
    slope = intercept = float("nan")
    correlation = float("nan")
    if is_array.size >= 3 and float(np.std(is_array)) > 0.0 and float(np.std(oos_array)) > 0.0:
        slope, intercept = (float(value) for value in np.polyfit(is_array, oos_array, 1))
        correlation = float(np.corrcoef(is_array, oos_array)[0, 1])

    counts: dict[str, int] = {}
    for winner in winners:
        counts[winner] = counts.get(winner, 0) + 1

    return {
        "n_blocks": n_blocks,
        "selection": selection,
        "selection_label": SELECTION_LABELS[selection],
        "splits": len(logits),
        "pbo": float(np.mean(logit_array <= 0.0)),
        "logit_mean": float(np.mean(logit_array)),
        "logit_median": float(np.median(logit_array)),
        "logit_q05": float(np.quantile(logit_array, 0.05)),
        "logit_q95": float(np.quantile(logit_array, 0.95)),
        "is_selected_score_mean": float(np.mean(is_array)),
        "is_selected_score_median": float(np.median(is_array)),
        "oos_selected_score_mean": float(np.mean(oos_array)),
        "oos_selected_score_median": float(np.median(oos_array)),
        "is_oos_slope": slope,
        "is_oos_intercept": intercept,
        "is_oos_pearson": correlation,
        "is_oos_spearman_mean": float(np.nanmean(spearmans)),
        "oos_selected_percentile_mean": float(np.mean(oos_percentiles)),
        "oos_selected_percentile_median": float(np.median(oos_percentiles)),
        "most_frequent_is_best": sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5],
        "distinct_is_winners": len(counts),
        "score_definition": SCORE_DEFINITIONS[selection],
    }


SCORE_DEFINITIONS = {
    "max_adg": "ADG = prod(1+r)^(12/n_months) − 1，在样本内月份上计算（1+r 下限截断 1e-12）",
    "max_adg_x_1_minus_dd": "ADG × (1 − 样本内月份链式权益的最差回撤)",
}


def headline(matrix: np.ndarray, arm_ids: list[str], selection: str) -> dict[str, Any]:
    """Full-panel winner under one convention - the arm the round would have shipped."""
    scores = SCORE_FUNCTIONS[selection](matrix)
    adgs = score_adg(matrix)
    drawdowns = _worst_drawdown(matrix)
    order = sorted(range(len(arm_ids)), key=lambda i: (-scores[i], arm_ids[i]))
    best = order[0]
    return {
        "selection": selection,
        "winner": arm_ids[best],
        "winner_score": float(scores[best]),
        "winner_adg": float(adgs[best]),
        "winner_worst_drawdown": float(drawdowns[best]),
        "ranking": [
            {
                "arm_id": arm_ids[index],
                "score": float(scores[index]),
                "adg": float(adgs[index]),
                "worst_drawdown": float(drawdowns[index]),
            }
            for index in order
        ],
    }


# ------------------------------------------------------------------------------------ driver ---
def load_panel(pool: str, leg: str) -> tuple[list[str], list[str], np.ndarray, dict[str, Any]]:
    """Read the self-contained tracked panel JSON (matrix included), CSV only as a fallback."""
    meta = load_json(PANEL_DIR / f"{pool}__{leg}.json")
    months = meta["months"]
    arm_ids = meta["arm_ids"]
    if "returns_matrix" in meta:
        matrix = np.array(meta["returns_matrix"], dtype=float)
    else:  # pragma: no cover - only for artifacts written before the matrix was embedded
        matrix = np.zeros((len(arm_ids), len(months)), dtype=float)
        with (PANEL_DIR / f"{pool}__{leg}.csv").open(encoding="utf-8") as handle:
            next(handle)
            for line in handle:
                arm_id, month, value = line.rstrip("\n").split(",")
                matrix[arm_ids.index(arm_id), months.index(month)] = float(value)
    if matrix.shape != (len(arm_ids), len(months)):
        raise SystemExit(f"{pool}__{leg}: returns_matrix shape {matrix.shape} does not match the axes")
    if not np.isfinite(matrix).all():
        raise SystemExit(f"{pool}__{leg}: non-finite monthly returns")
    return arm_ids, months, matrix, meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    index = load_json(PANEL_DIR / "index.json")
    results: dict[str, Any] = {
        "generated_by": "report_tools/cscv_pbo.py",
        "selection_conventions": {
            key: {"label": SELECTION_LABELS[key], "score": SCORE_DEFINITIONS[key]}
            for key in SELECTIONS
        },
        "blocks": list(DEFAULT_BLOCKS),
        "interpretation": (
            "PBO 度量的是「在这批 arm 里挑最好的那一个」在这条历史上的脆弱程度：把月份切成 S 个"
            "连续块、枚举全部半样本划分，用一半排名、看样本内冠军在另一半的分位。它**不是**亏钱概率，"
            "也检测不到参数时间旅行与选择 provenance——曲线本身不记录每个参数何时变得可知。"
        ),
        "degeneracy_rule": index["degeneracy_rule"],
        "method_conventions": {
            "block_edges": "edges[i] = floor(i·M/S)：整数运算，余数落在最早的块",
            "rank_convention": (
                "并列值取平均名次（average rank）；CSCV omega = 平均名次/(n+1)，"
                "omega ≤ 0.5 = 样本内冠军落在样本外下半区"
            ),
            "tie_note": (
                "并列是真实存在的：某条保护在窗口内从未触发时，多个 arm 的权益曲线逐位相同，"
                "用位置式 tie-break 会让 arm 的字母序决定答案"
            ),
        },
        "panels": {},
    }

    for name in sorted(index["panels"]):
        pool, leg = name.split("__")
        arm_ids, months, matrix, meta = load_panel(pool, leg)
        entry: dict[str, Any] = {
            "pool": pool,
            "leg": leg,
            "n_arms_used": len(arm_ids),
            "n_months": len(months),
            "n_arms_in_pool": meta["n_arms_in_pool"],
            "n_arms_removed": meta["n_arms_removed"],
            "removed_by_reason": meta["removed_by_reason"],
            "arm_ids": arm_ids,
            "months": months,
            "degeneracy_rule": meta["degeneracy_rule"],
            "cscv": {},
            "headline": {},
        }
        for selection in SELECTIONS:
            entry["headline"][selection] = headline(matrix, arm_ids, selection)
            for blocks in DEFAULT_BLOCKS:
                result = cscv(matrix, blocks, arm_ids, selection)
                entry["cscv"][f"{selection}|S{blocks}"] = result
        results["panels"][name] = entry

        if not args.quiet:
            print(f"== {name}: {len(arm_ids)} arms x {len(months)} months "
                  f"(removed {meta['n_arms_removed']})")
            for selection in SELECTIONS:
                row = "  " + selection.ljust(22)
                for blocks in DEFAULT_BLOCKS:
                    result = entry["cscv"][f"{selection}|S{blocks}"]
                    row += f"S{blocks}={result.get('pbo', float('nan')):.3f} "
                row += (
                    f"| slope {entry['cscv'][f'{selection}|S{DEFAULT_BLOCKS[0]}']['is_oos_slope']:+.3f}"
                    f" | winner {entry['headline'][selection]['winner'].split('|')[-1]}"
                )
                print(row)

    write_json(ARTIFACTS / "cscv_pbo.json", results)
    if not args.quiet:
        print(f"\nwrote {ARTIFACTS / 'cscv_pbo.json'}")


if __name__ == "__main__":
    sys.exit(main())
