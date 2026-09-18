#!/usr/bin/env python3
"""Independent recomputation of every headline number in this audit.

This module deliberately shares no code with :mod:`panel`, :mod:`cscv_pbo`,
:mod:`selection_bias` or :mod:`walkforward`. It re-reads the run-local equity ledgers with the
standard library (``gzip`` + ``csv``, no pandas), rebuilds the panels, re-derives the degeneracy
rule, re-runs CSCV, the deflated Sharpe ratio, the minimum backtest length and the fold statistics
from scratch, and only then compares its own numbers against ``artifacts/*.json``. A mismatch is a
hard failure with a non-zero exit code.

What it does **not** do is re-read the report's prose. The comparison targets are the tracked JSON
artifacts, which are exactly what the report cites.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import json
import math
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

import numpy as np

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_overfitting_audit_2026-09-18"
ARTIFACTS = STUDY / "artifacts"
PANEL_DIR = ARTIFACTS / "panels"

#: Re-declared, not imported: the verifier must not be able to inherit a threshold change.
TERMINAL_FLAT_MIN_DAYS = 30
ZERO_VARIANCE_TOL = 1e-10
MIN_NONZERO_MONTHS = 3
BLOCKS = (8, 10, 12, 16)
SELECTIONS = ("max_adg", "max_adg_x_1_minus_dd")
FOLD_COUNTS = (3, 6)
PRIMARY_FOLDS = 6
J4_PERCENTILE_BAR = 0.50
J4_SPEARMAN_BAR = 0.50
EULER_GAMMA = 0.5772156649015329
FLOOR = 1e-12

TOL = {
    "return_abs": 1e-12,
    "pbo_abs": 1e-12,
    #: The verifier's normal quantile is Acklam's rational approximation, whose relative error is
    #: ~1e-9; the DSR tolerance is therefore set just above that approximation floor, not at
    #: floating-point exactness. The skewness/kurtosis inputs are checked separately at 1e-9.
    "dsr_abs": 1e-7,
    "score_rel": 1e-9,
    "minbtl_rel": 1e-9,
    "fold_abs": 1e-9,
    "money_rel": 1e-9,
}

PROBLEMS: list[str] = []
CHECKS = {"passed": 0, "failed": 0}


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        CHECKS["passed"] += 1
    else:
        CHECKS["failed"] += 1
        PROBLEMS.append(f"{label}: {detail}")


def close(left: float | None, right: float | None, tol: float, *, rel: bool = False) -> bool:
    if left is None or right is None:
        return left is right
    if not (math.isfinite(left) and math.isfinite(right)):
        return left == right
    scale = max(abs(left), abs(right), 1.0) if rel else 1.0
    return abs(left - right) <= tol * scale


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


# ------------------------------------------------------------------ independent equity reader ---
def read_equity(path: Path) -> tuple[list[str], list[float]]:
    """Hourly (timestamp, strategy_equity) straight from the gzipped CSV, stdlib only."""
    timestamps: list[str] = []
    equity: list[float] = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        try:
            column = header.index("strategy_equity")
        except ValueError:
            raise SystemExit(f"{path}: no strategy_equity column")
        for row in reader:
            if not row or len(row) <= column:
                continue
            timestamps.append(row[0])
            equity.append(float(row[column]))
    return timestamps, equity


def month_key(timestamp: str) -> str:
    return timestamp[:7]


def last_of_month(timestamps: list[str], equity: list[float]) -> tuple[list[str], list[float]]:
    months: list[str] = []
    levels: list[float] = []
    for timestamp, value in zip(timestamps, equity):
        key = month_key(timestamp)
        if months and months[-1] == key:
            levels[-1] = value
        else:
            months.append(key)
            levels.append(value)
    return months, levels


def chained_returns(months: list[str], levels: list[float], first_equity: float) -> np.ndarray:
    out = np.empty(len(levels), dtype=float)
    for index, value in enumerate(levels):
        previous = first_equity if index == 0 else levels[index - 1]
        out[index] = value / previous - 1.0 if previous else 0.0
    return out


def terminal_flat_days(timestamps: list[str], equity: list[float]) -> float:
    last = equity[-1]
    tol = max(1e-9, 1e-12 * abs(last))
    index = len(equity) - 1
    while index > 0 and abs(equity[index - 1] - last) <= tol:
        index -= 1
    start = _parse_ts(timestamps[index])
    end = _parse_ts(timestamps[-1])
    return (end - start) / 86400.0


def _parse_ts(text: str) -> float:
    """Seconds since epoch for 'YYYY-MM-DD HH:MM:SS' (UTC), stdlib only."""
    from datetime import datetime, timezone

    cleaned = text.strip().replace("T", " ")[:19]
    return datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    ).timestamp()


# ------------------------------------------------------------------- independent score maths ---
def growth(rets: np.ndarray) -> np.ndarray:
    factors = np.clip(1.0 + np.asarray(rets, dtype=float), FLOOR, None)
    return np.exp(np.log(factors).sum(axis=1) / factors.shape[1] * 12.0) - 1.0


def worst_drawdown(rets: np.ndarray) -> np.ndarray:
    factors = np.clip(1.0 + np.asarray(rets, dtype=float), FLOOR, None)
    equity = np.concatenate([np.ones((factors.shape[0], 1)), np.cumprod(factors, axis=1)], axis=1)
    return (1.0 - equity / np.maximum.accumulate(equity, axis=1)).max(axis=1)


def score(rets: np.ndarray, selection: str) -> np.ndarray:
    if selection == "max_adg":
        return growth(rets)
    return growth(rets) * (1.0 - worst_drawdown(rets))


def average_ranks(scores: np.ndarray) -> np.ndarray:
    """Ranks 1..n with tied values sharing their average rank - the declared convention."""
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


def omega_of(scores: np.ndarray, index: int) -> float:
    """CSCV omega: average rank from worst, normalised. At or below 0.5 means the bottom half."""
    return float(average_ranks(scores)[index] / (scores.size + 1))


def pearson(left: np.ndarray, right: np.ndarray) -> float:
    a = left - left.mean()
    b = right - right.mean()
    denominator = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    return float((a * b).sum() / denominator) if denominator else float("nan")


def spearman(left: np.ndarray, right: np.ndarray) -> float:
    return pearson(average_ranks(left), average_ranks(right))


def block_edges(n_months: int, n_blocks: int) -> list[int]:
    """Declared block boundary rule: edges[i] = floor(i*M/S), remainder in the earliest blocks."""
    return [(index * n_months) // n_blocks for index in range(n_blocks + 1)]


def independent_cscv(matrix: np.ndarray, n_blocks: int, selection: str) -> dict[str, Any]:
    n_arms, n_months = matrix.shape
    edges = block_edges(n_months, n_blocks)
    blocks = [matrix[:, edges[i] : edges[i + 1]] for i in range(n_blocks)]
    half = n_blocks // 2
    logits, is_sel, oos_sel, spearmans = [], [], [], []
    for combo in combinations(range(n_blocks), half):
        complement = [block for block in range(n_blocks) if block not in combo]
        is_returns = np.concatenate([blocks[block] for block in combo], axis=1)
        oos_returns = np.concatenate([blocks[block] for block in complement], axis=1)
        is_scores = score(is_returns, selection)
        oos_scores = score(oos_returns, selection)
        best = int(np.argmax(is_scores))
        value = omega_of(oos_scores, best)
        logits.append(math.log(value / (1.0 - value)))
        is_sel.append(float(is_scores[best]))
        oos_sel.append(float(oos_scores[best]))
        spearmans.append(spearman(is_scores, oos_scores))
    logit_array = np.array(logits)
    is_array = np.array(is_sel)
    oos_array = np.array(oos_sel)
    slope = float(np.polyfit(is_array, oos_array, 1)[0])
    return {
        "splits": len(logits),
        "pbo": float(np.mean(logit_array <= 0.0)),
        "is_oos_slope": slope,
        "is_oos_spearman_mean": float(np.nanmean(spearmans)),
        "is_selected_score_mean": float(is_array.mean()),
        "oos_selected_score_mean": float(oos_array.mean()),
    }


def independent_rank_percentile_from_best(scores: np.ndarray, index: int) -> tuple[float, float]:
    """(rank from best, percentile from best) under the declared average-rank convention.

    ``omega_of`` ranks from the worst; the fold statistic ranks from the best, so this is
    ``n + 1 - omega*(n+1)`` - derived here the long way round to keep the two conventions apart.
    """
    from_worst = average_ranks(scores)[index]
    rank = scores.size + 1 - from_worst
    return rank, (rank - 1.0) / max(scores.size - 1, 1)


# ------------------------------------------------------------------------ independent DSR maths --
def independent_dsr(returns: np.ndarray, n_trials: int, sr_std: float, periods: float) -> dict:
    """DSR with the same *declared* estimators.

    The shape estimators are the bias-corrected sample skewness and the bias-corrected
    non-excess kurtosis (the ``bias=False`` convention) - re-implemented here from their closed
    forms rather than called from scipy, so the comparison still exercises real arithmetic.
    """
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    t_obs = values.size
    sr = float(values.mean() / values.std(ddof=1))
    centred = values - values.mean()
    m2 = float((centred**2).mean())
    m3 = float((centred**3).mean())
    m4 = float((centred**4).mean())
    g1 = m3 / m2**1.5
    skew = g1 * math.sqrt(t_obs * (t_obs - 1)) / (t_obs - 2)
    g2 = m4 / m2**2 - 3.0
    kurtosis = ((t_obs + 1) * g2 + 6.0) * (t_obs - 1) / ((t_obs - 2) * (t_obs - 3)) + 3.0
    left = _norm_ppf(1.0 - 1.0 / n_trials)
    right = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    sr0 = sr_std * ((1.0 - EULER_GAMMA) * left + EULER_GAMMA * right)
    variance_term = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    z = (sr - sr0) * math.sqrt(t_obs - 1) / math.sqrt(variance_term)
    return {
        "n_observations": int(t_obs),
        "sharpe_per_observation": sr,
        "sharpe_annualised": sr * math.sqrt(periods),
        "expected_max_sharpe_sr0": float(sr0),
        "skewness": float(skew),
        "kurtosis_non_excess": float(kurtosis),
        "variance_term": float(variance_term),
        "z": float(z),
        "dsr": float(_norm_cdf(z)),
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam's rational approximation - independent of scipy."""
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00)
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )


# ------------------------------------------------------------------------------------ driver ---
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    panel_index = load_json(PANEL_DIR / "index.json")
    cscv_artifact = load_json(ARTIFACTS / "cscv_pbo.json")
    selection_artifact = load_json(ARTIFACTS / "selection_bias.json")
    fold_artifact = load_json(ARTIFACTS / "fold_stability.json")
    stress_artifact = load_json(ARTIFACTS / "stress_arms.json")
    ledger = load_json(ARTIFACTS / "trial_ledger.json")

    cache: dict[str, tuple[list[str], list[float], list[str], np.ndarray, list[float]]] = {}

    def series_for(run_dir: str) -> tuple[list[str], np.ndarray, float]:
        if run_dir not in cache:
            path = REPO / run_dir / "balance_and_equity.csv.gz"
            timestamps, equity = read_equity(path)
            months, levels = last_of_month(timestamps, equity)
            returns = chained_returns(months, levels, equity[0])
            cache[run_dir] = (timestamps, equity, months, returns, levels)
        timestamps, equity, months, returns, _ = cache[run_dir]
        return months, returns, terminal_flat_days(timestamps, equity)

    if not args.quiet:
        print("== independent panel reconstruction ==")

    for name in sorted(panel_index["panels"]):
        meta = load_json(PANEL_DIR / f"{name}.json")
        months_expected = meta["months"]
        sources = meta["sources"]

        # 1. every arm's monthly series, recomputed from the gzip equity ledger
        per_arm: dict[str, tuple[list[str], np.ndarray, float]] = {}
        plain_name: dict[str, str] = {}
        for source in sources:
            arm_id = f"{source['round']}|{source['arm']}"
            plain_name[arm_id] = source["arm"]
            per_arm[arm_id] = series_for(source["run_dir"])

        # 2. the degeneracy rule, re-applied
        counts: dict[tuple[str, ...], int] = {}
        for months, _, _ in per_arm.values():
            counts[tuple(months)] = counts.get(tuple(months), 0) + 1
        modal = list(max(counts.items(), key=lambda item: (item[1], len(item[0])))[0])
        check(f"{name}/modal_months", modal == months_expected,
              f"verifier {modal[:2]}..{modal[-1:]} vs artifact {months_expected[:2]}..{months_expected[-1:]}")

        used, removed = [], {}
        for arm_id, (months, returns, flat_days) in per_arm.items():
            reasons = []
            if months != modal:
                reasons.append("D1_truncated")
            if flat_days >= TERMINAL_FLAT_MIN_DAYS:
                reasons.append("D2_terminal_halt")
            if returns.size >= 2:
                if float(np.std(returns, ddof=1)) < ZERO_VARIANCE_TOL or int(
                    np.sum(np.abs(returns) > 1e-9)
                ) < MIN_NONZERO_MONTHS:
                    reasons.append("D3_zero_variance")
            if reasons:
                for reason in reasons:
                    removed.setdefault(reason, []).append(plain_name[arm_id])
            else:
                used.append(arm_id)

        check(f"{name}/n_arms_used", len(used) == meta["n_arms_used"],
              f"verifier {len(used)} vs artifact {meta['n_arms_used']}")
        check(f"{name}/n_arms_removed", len(sources) - len(used) == meta["n_arms_removed"],
              f"verifier {len(sources) - len(used)} vs artifact {meta['n_arms_removed']}")
        check(f"{name}/arm_ids", sorted(used) == sorted(meta["arm_ids"]),
              "arm sets differ")
        for reason, arms in meta["removed_by_reason"].items():
            check(f"{name}/removed/{reason}", sorted(removed.get(reason, [])) == sorted(arms),
                  f"verifier {sorted(removed.get(reason, []))} vs artifact {sorted(arms)}")

        # 3. the tracked returns_matrix must equal the recomputed returns; the CSV, when present,
        #    must agree with both. The matrix - not the CSV - is the tracked artifact.
        matrix = np.zeros((len(meta["arm_ids"]), len(months_expected)), dtype=float)
        for row, arm_id in enumerate(meta["arm_ids"]):
            months, returns, _ = per_arm[arm_id]
            for column, month in enumerate(months_expected):
                matrix[row, column] = float(returns[months.index(month)])

        tracked = np.array(meta["returns_matrix"], dtype=float)
        check(f"{name}/returns_matrix_shape", tracked.shape == matrix.shape,
              f"artifact {tracked.shape} vs recomputed {matrix.shape}")
        worst = float(np.max(np.abs(tracked - matrix))) if tracked.shape == matrix.shape else math.inf
        check(f"{name}/returns_matrix_matches_equity", worst <= TOL["return_abs"],
              f"max |diff| = {worst:.3e}")

        csv_path = PANEL_DIR / f"{name}.csv"
        if csv_path.exists():
            from_csv = np.zeros_like(matrix)
            with csv_path.open(encoding="utf-8") as handle:
                next(handle)
                for line in handle:
                    arm_id, month, value = line.rstrip("\n").split(",")
                    from_csv[meta["arm_ids"].index(arm_id), months_expected.index(month)] = float(value)
            worst_csv = float(np.max(np.abs(from_csv - matrix)))
            #: The CSV is written with 12 significant digits, so it is a convenience copy, not
            #: the evidence; the JSON matrix round-trips exactly.
            check(f"{name}/panel_csv_matches_equity", worst_csv <= 1e-9,
                  f"max |diff| = {worst_csv:.3e}")
        else:
            check(f"{name}/panel_csv_optional", True, "")

        # 4. CSCV, recomputed
        artifact_panel = cscv_artifact["panels"][name]
        for selection in SELECTIONS:
            for blocks in BLOCKS:
                mine = independent_cscv(matrix, blocks, selection)
                theirs = artifact_panel["cscv"][f"{selection}|S{blocks}"]
                check(f"{name}/cscv/{selection}|S{blocks}/pbo",
                      close(mine["pbo"], theirs["pbo"], TOL["pbo_abs"]),
                      f"verifier {mine['pbo']:.12f} vs artifact {theirs['pbo']:.12f}")
                check(f"{name}/cscv/{selection}|S{blocks}/splits",
                      mine["splits"] == theirs["splits"],
                      f"verifier {mine['splits']} vs artifact {theirs['splits']}")
                check(f"{name}/cscv/{selection}|S{blocks}/slope",
                      close(mine["is_oos_slope"], theirs["is_oos_slope"], TOL["score_rel"], rel=True),
                      f"verifier {mine['is_oos_slope']:.9f} vs artifact {theirs['is_oos_slope']:.9f}")
                check(f"{name}/cscv/{selection}|S{blocks}/spearman",
                      close(mine["is_oos_spearman_mean"], theirs["is_oos_spearman_mean"],
                            TOL["score_rel"], rel=True),
                      f"verifier {mine['is_oos_spearman_mean']:.9f} "
                      f"vs artifact {theirs['is_oos_spearman_mean']:.9f}")

        # 5. deflated Sharpe and MinBTL, recomputed
        monthly_sharpes = [
            float(row.mean() / row.std(ddof=1))
            for row in matrix
            if row.size > 2 and float(row.std(ddof=1)) > 0.0
        ]
        sr_std_monthly = float(np.std(monthly_sharpes, ddof=1))
        artifact_sr_std = selection_artifact["panels"][name]["trial_sharpe_dispersion_proxy"][
            "monthly_sr_std"
        ]
        check(f"{name}/sr_std_monthly", close(sr_std_monthly, artifact_sr_std, 1e-9, rel=True),
              f"verifier {sr_std_monthly:.12f} vs artifact {artifact_sr_std:.12f}")

        for row, arm_id in enumerate(meta["arm_ids"]):
            artifact_dsr = selection_artifact["panels"][name]["deflated_sharpe"][arm_id]["monthly"]
            for n_trials, entry in artifact_dsr.items():
                mine = independent_dsr(matrix[row], int(n_trials), sr_std_monthly, 12.0)
                check(f"{name}/dsr/{arm_id}/N{n_trials}",
                      close(mine["dsr"], entry["dsr"], TOL["dsr_abs"]),
                      f"verifier {mine['dsr']:.12f} vs artifact {entry['dsr']:.12f}")
                check(f"{name}/dsr/{arm_id}/N{n_trials}/skew",
                      close(mine["skewness"], entry["skewness"], 1e-9, rel=True),
                      f"verifier {mine['skewness']:.12f} vs artifact {entry['skewness']:.12f}")
                check(f"{name}/dsr/{arm_id}/N{n_trials}/kurt",
                      close(mine["kurtosis_non_excess"], entry["kurtosis_non_excess"], 1e-9, rel=True),
                      f"verifier {mine['kurtosis_non_excess']:.12f} "
                      f"vs artifact {entry['kurtosis_non_excess']:.12f}")
            annualised = selection_artifact["panels"][name]["min_btl_years"][arm_id][
                "sharpe_annualised"
            ]
            for n_trials, value in selection_artifact["panels"][name]["min_btl_years"][arm_id][
                "years"
            ].items():
                if value is None:
                    check(f"{name}/minbtl/{arm_id}/N{n_trials}",
                          annualised <= 0.0 or int(n_trials) < 2,
                          f"artifact is None but annualised={annualised} N={n_trials}")
                    continue
                mine = 2.0 * math.log(int(n_trials)) / annualised**2
                check(f"{name}/minbtl/{arm_id}/N{n_trials}",
                      close(mine, value, TOL["minbtl_rel"], rel=True),
                      f"verifier {mine:.9f} vs artifact {value:.9f}")

        # 6. fold statistics, recomputed
        artifact_folds = fold_artifact["panels"][name]
        for folds in FOLD_COUNTS:
            entry = artifact_folds[f"K{folds}"]
            n_months = matrix.shape[1]
            edges = block_edges(n_months, folds)
            fold_scores = [growth(matrix[:, edges[i] : edges[i + 1]]) for i in range(folds)]
            pair_values = []
            for left, right in combinations(range(folds), 2):
                pair_values.append(spearman(fold_scores[left], fold_scores[right]))
            check(f"{name}/K{folds}/spearman",
                  close(float(np.mean(pair_values)), entry["mean_pairwise_spearman"],
                        TOL["fold_abs"]),
                  f"verifier {float(np.mean(pair_values)):.12f} "
                  f"vs artifact {entry['mean_pairwise_spearman']:.12f}")
            percentiles = []
            for index in range(folds):
                others = [
                    matrix[:, edges[j] : edges[j + 1]] for j in range(folds) if j != index
                ]
                oos_scores = growth(np.concatenate(others, axis=1))
                winner = int(np.nanargmax(fold_scores[index]))
                rank, percentile = independent_rank_percentile_from_best(oos_scores, winner)
                row = entry["per_fold"][index]
                check(f"{name}/K{folds}/fold{index}/winner",
                      meta["arm_ids"][winner] == row["in_fold_winner"],
                      f"verifier {meta['arm_ids'][winner]} vs artifact {row['in_fold_winner']}")
                check(f"{name}/K{folds}/fold{index}/rank",
                      close(rank, row["out_of_fold_rank_best1"], TOL["fold_abs"]),
                      f"verifier {rank} vs artifact {row['out_of_fold_rank_best1']}")
                check(f"{name}/K{folds}/fold{index}/percentile",
                      close(percentile, row["out_of_fold_percentile_best0"], TOL["fold_abs"]),
                      f"verifier {percentile:.12f} vs artifact {row['out_of_fold_percentile_best0']:.12f}")
                percentiles.append(percentile)
            check(f"{name}/K{folds}/j4_percentile",
                  close(float(max(percentiles)), entry["j4_percentile_measured"], TOL["fold_abs"]),
                  f"verifier {float(max(percentiles)):.12f} "
                  f"vs artifact {entry['j4_percentile_measured']:.12f}")

        if not args.quiet:
            print(f"   {name}: {len(used)} used / {len(sources) - len(used)} removed - verified")

    # 7. stress arms, recomputed from the fill ledgers
    if not args.quiet:
        print("== independent stress recomputation ==")
    for arm_id, entry in stress_artifact["arms"].items():
        if "unavailable" in entry:
            continue
        fills_path = REPO / entry["run_dir"] / "fills.csv"
        equity_path = REPO / entry["run_dir"] / "balance_and_equity.csv.gz"
        notional_by_time: dict[str, float] = {}
        notional_total = 0.0
        with fills_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                notional = abs(float(row["qty"]) * float(row["price"]))
                notional_total += notional
                key = row["timestamp"]
                notional_by_time[key] = notional_by_time.get(key, 0.0) + notional
        check(f"stress/{arm_id}/notional", close(notional_total, entry["provenance"]["traded_notional"],
                                                 TOL["money_rel"], rel=True),
              f"verifier {notional_total:.6f} vs artifact {entry['provenance']['traded_notional']:.6f}")
        fill_times = sorted(notional_by_time)
        running_sums = [0.0]
        for key in fill_times:
            running_sums.append(running_sums[-1] + notional_by_time[key])
        timestamps, equity = read_equity(equity_path)
        base_first = equity[0]
        for stress in stress_artifact["stresses"]:
            rate = stress["extra_bp"] / 1.0e4
            path = []
            for timestamp, value in zip(timestamps, equity):
                charged = running_sums[bisect.bisect_right(fill_times, timestamp)] * rate
                path.append(value - charged)
            stressed = np.maximum(np.array(path), 1e-9)
            final = stressed[-1] / base_first
            peak = np.maximum.accumulate(stressed)
            dd = float(np.max(1.0 - stressed / peak))
            theirs = entry["stress"][stress["key"]]
            check(f"stress/{arm_id}/{stress['key']}/final",
                  close(final, theirs["stressed_final_multiple"], TOL["money_rel"], rel=True),
                  f"verifier {final:.12f} vs artifact {theirs['stressed_final_multiple']:.12f}")
            check(f"stress/{arm_id}/{stress['key']}/dd",
                  close(dd, theirs["stressed_worst_drawdown"], 1e-9),
                  f"verifier {dd:.12f} vs artifact {theirs['stressed_worst_drawdown']:.12f}")
        if not args.quiet:
            print(f"   {arm_id}: verified")

    # 8. the ledger's own lower bound and its traceability samples
    if not args.quiet:
        print("== independent ledger check ==")
    recomputed_bound = sum(1 for row in ledger["trials"] if row.get("evaluated"))
    check("ledger/n_lower_bound", recomputed_bound == ledger["n_lower_bound"],
          f"verifier {recomputed_bound} vs artifact {ledger['n_lower_bound']}")
    tier_counts: dict[str, int] = {}
    evaluated_by_tier: dict[str, int] = {}
    for row in ledger["trials"]:
        tier_counts[row["tier"]] = tier_counts.get(row["tier"], 0) + 1
        if row.get("evaluated"):
            evaluated_by_tier[row["tier"]] = evaluated_by_tier.get(row["tier"], 0) + 1
    counts = ledger["counts"]
    check("ledger/counts/replay_arms_total",
          tier_counts.get("replay_arm", 0) == counts["replay_arms_total"],
          f"verifier {tier_counts.get('replay_arm', 0)} vs artifact {counts['replay_arms_total']}")
    check("ledger/counts/replay_arms_evaluated",
          evaluated_by_tier.get("replay_arm", 0) == counts["replay_arms_evaluated"],
          f"verifier {evaluated_by_tier.get('replay_arm', 0)} "
          f"vs artifact {counts['replay_arms_evaluated']}")
    check("ledger/counts/declared_cells_total",
          tier_counts.get("declared_cell", 0) == counts["declared_cells_total"],
          f"verifier {tier_counts.get('declared_cell', 0)} "
          f"vs artifact {counts['declared_cells_total']}")
    check("ledger/counts/declared_cells_evaluated",
          evaluated_by_tier.get("declared_cell", 0) == counts["declared_cells_evaluated"],
          f"verifier {evaluated_by_tier.get('declared_cell', 0)} "
          f"vs artifact {counts['declared_cells_evaluated']}")
    check("ledger/counts/optimizer_candidates",
          tier_counts.get("optimizer_candidate", 0) == counts["optimizer_candidates"],
          f"verifier {tier_counts.get('optimizer_candidate', 0)} "
          f"vs artifact {counts['optimizer_candidates']}")
    check("ledger/counts/screen_configs",
          tier_counts.get("screen_config", 0) == counts["screen_configs"],
          f"verifier {tier_counts.get('screen_config', 0)} vs artifact {counts['screen_configs']}")
    check("ledger/total_rows", sum(tier_counts.values()) == counts["total_rows"],
          f"verifier {sum(tier_counts.values())} vs artifact {counts['total_rows']}")
    check("ledger/counts/n_lower_bound",
          counts["n_lower_bound"] == recomputed_bound,
          f"verifier {recomputed_bound} vs artifact {counts['n_lower_bound']}")
    for sample in ledger["traceability_samples"]:
        path = REPO / sample["evidence_path"]
        check(f"ledger/trace/{sample['tier']}/exists", path.exists(),
              f"{sample['evidence_path']} missing")
        if sample["tier"] == "replay_arm":
            equity = path / "balance_and_equity.csv.gz"
            check(f"ledger/trace/{sample['tier']}/equity", equity.exists(), "equity missing")
            if equity.exists():
                timestamps, _ = read_equity(equity)
                check(f"ledger/trace/{sample['tier']}/first_ts",
                      timestamps[0] == sample["recheck"]["first_equity_ts"],
                      f"verifier {timestamps[0]} vs artifact {sample['recheck']['first_equity_ts']}")
        if not args.quiet:
            print(f"   {sample['tier']}: {sample['evidence_path']} - rechecked")

    print()
    summary = {
        "generated_by": "report_tools/verify_audit.py",
        "checks_passed": CHECKS["passed"],
        "checks_failed": CHECKS["failed"],
        "problems": PROBLEMS[:60],
        "tolerances": TOL,
        "note": (
            "本文件由独立复核在最后一步写出，记录它自己跑了多少项检查、以及每类比较用的容差。"
            "复核模块不 import panel/cscv_pbo/selection_bias/walkforward，"
            "用标准库 gzip+csv 重读权益账本并重算面板、退化规则、PBO、DSR、MinBTL、折叠统计与压力臂。"
        ),
    }
    write_json(ARTIFACTS / "verification.json", summary)
    print(f"checks passed: {CHECKS['passed']}, failed: {CHECKS['failed']}")
    if PROBLEMS:
        print("\nFAILURES:", file=sys.stderr)
        for problem in PROBLEMS[:60]:
            print(f"  - {problem}", file=sys.stderr)
        if len(PROBLEMS) > 60:
            print(f"  ... and {len(PROBLEMS) - 60} more", file=sys.stderr)
        raise SystemExit(1)
    print("independent verification passed: every recomputed headline number matches the artifacts")


if __name__ == "__main__":
    sys.exit(main())
