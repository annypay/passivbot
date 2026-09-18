#!/usr/bin/env python3
"""Walk-forward fold stability and declared cost/execution stress arms.

Offline only. Reads the tracked panels written by :mod:`panel` plus the run-local fill ledgers the
panels were derived from. Writes ``artifacts/fold_stability.json`` and
``artifacts/stress_arms.json``.

Fold stability (``--stage folds``)
---------------------------------
The search window (the ``3y`` leg) is cut into ``K`` contiguous folds. For each fold the arms are
ranked on that fold alone; the fold's winner is then ranked on the pooled out-of-fold months. A
pool that carries signal should keep its in-fold winner in the better half out-of-fold, and the
fold rankings should agree with each other. The pre-registered J4 bar is: the in-fold winner lands
at or below the 50th percentile out-of-fold **and** the mean pairwise Spearman between fold
rankings is at least 0.5.

Stress arms (``--stage stress``)
--------------------------------
Two or three *declared* stresses - never a selection input - are applied analytically to the fill
ledger: an extra per-fill cost of ``bp`` basis points of traded notional is subtracted from the
equity path, and the final multiple and worst drawdown are recomputed. These are first-order
bounds: a real fee change would also move optimal order placement, which this arithmetic cannot
model. The runs themselves are not modified and nothing is re-simulated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from itertools import combinations
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_overfitting_audit_2026-09-18"
ARTIFACTS = STUDY / "artifacts"
PANEL_DIR = ARTIFACTS / "panels"

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Pre-registered fold counts. K=6 (six-month folds on a 37-month window) is the primary split the
#: J4 bar is judged on; K=3 is reported as a coarser second look.
FOLD_COUNTS = (3, 6)
PRIMARY_FOLDS = 6
J4_PERCENTILE_BAR = 0.50
J4_SPEARMAN_BAR = 0.50
FLOOR = 1e-12

#: Declared stress arms. ``extra_bp`` is added cost per fill, in basis points of |qty x price|.
#: Rounded from the earlier rounds' declared cost scenarios (maker 0.0002 -> conservative 0.0006
#: -> severe 0.0010) plus an adverse-slippage arm. Declared before the numbers were computed.
STRESS_ARMS: tuple[dict[str, Any], ...] = (
    {
        "key": "FEE_CONSERVATIVE",
        "kind": "fee_inflation",
        "extra_bp": 4.0,
        "note": "maker 0.0002 → 0.0006（早期研究声明的 conservative 成本档）",
    },
    {
        "key": "FEE_SEVERE",
        "kind": "fee_inflation",
        "extra_bp": 8.0,
        "note": "maker 0.0002 → 0.0010（早期研究声明的 severe 成本档）",
    },
    {
        "key": "SLIP_5BP",
        "kind": "adverse_slippage",
        "extra_bp": 5.0,
        "note": "每笔成交按名义额额外 5bp 逆价（挂单被穿价的保守代理）",
    },
)

#: Arms the stress table reports. Declared list: the published baselines plus the in-sample search
#: winner. Pool winners are appended automatically at run time.
STRESS_BASE_ARMS: tuple[str, ...] = (
    "C_account_guard|g_user12h__ext",
    "A_risk_geometry|b_red015__ext",
    "A_risk_geometry|a_allow000__ext",
    "A_risk_geometry|a_allow037__pre",
    "A_risk_geometry|c1__3y",
    "B_10k_replay|twe300_10k__3y",
)


def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        handle.write("\n")
    os.replace(tmp, path)


def sha256_file(path: Path) -> str | None:
    path = Path(path)
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    return arm_ids, months, matrix, meta


# ------------------------------------------------------------------------------- fold maths ---
def growth(rets: np.ndarray) -> np.ndarray:
    factors = np.clip(1.0 + np.asarray(rets, dtype=float), FLOOR, None)
    if factors.shape[1] == 0:
        return np.full(factors.shape[0], np.nan)
    return np.exp(np.log(factors).sum(axis=1) / factors.shape[1] * 12.0) - 1.0


def worst_drawdown(rets: np.ndarray) -> np.ndarray:
    factors = np.clip(1.0 + np.asarray(rets, dtype=float), FLOOR, None)
    equity = np.concatenate([np.ones((factors.shape[0], 1)), np.cumprod(factors, axis=1)], axis=1)
    peak = np.maximum.accumulate(equity, axis=1)
    return (1.0 - equity / peak).max(axis=1)


def average_ranks_from_worst(scores: np.ndarray) -> np.ndarray:
    """Ranks 1..n from the **worst** score; tied values share their average rank."""
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


def rank_positions_for(scores: np.ndarray, index: int) -> tuple[float, float, float]:
    """(rank from best, percentile from best, CSCV omega) for a specific arm.

    ``rank`` is 1 for the best score (fractional under ties). ``percentile`` is 0 at the best arm
    and 1 at the worst, so "the in-fold winner lands at or below the 50th percentile out-of-fold"
    reads directly as ``percentile <= 0.50``. ``omega`` is the CSCV-compatible
    ``rank-from-worst/(n+1)``: at or below 0.5 means the arm sits in the bottom half, which is what
    PBO counts.
    """
    from_worst = average_ranks_from_worst(scores)
    rank = float(scores.size + 1 - from_worst[index])
    span = max(scores.size - 1, 1)
    percentile = (rank - 1.0) / span
    omega = float(from_worst[index] / (scores.size + 1))
    return rank, float(percentile), omega


def block_edges(n_months: int, n_blocks: int) -> list[int]:
    """Declared block boundary rule: ``edges[i] = floor(i*M/S)``, remainder in the earliest blocks."""
    return [(index * n_months) // n_blocks for index in range(n_blocks + 1)]


def spearman(left: np.ndarray, right: np.ndarray) -> float:
    """Pearson correlation of average ranks - the standard tie-corrected Spearman."""
    if left.size < 3:
        return float("nan")
    a = average_ranks_from_worst(left)
    b = average_ranks_from_worst(right)
    a = a - a.mean()
    b = b - b.mean()
    denominator = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    return float((a * b).sum() / denominator) if denominator else float("nan")


def fold_stability(
    arm_ids: list[str], months: list[str], matrix: np.ndarray, n_folds: int
) -> dict[str, Any]:
    n_months = matrix.shape[1]
    if n_folds > n_months:
        return {"n_folds": n_folds, "skipped": f"n_folds={n_folds} exceeds {n_months} months"}
    edges = block_edges(n_months, n_folds)
    folds = [matrix[:, edges[i] : edges[i + 1]] for i in range(n_folds)]
    fold_labels = [
        {"fold": index, "months": [months[edges[index]], months[edges[index + 1] - 1]],
         "n_months": int(edges[index + 1] - edges[index])}
        for index in range(n_folds)
    ]

    fold_scores = [growth(fold) for fold in folds]
    per_fold: list[dict[str, Any]] = []
    for index in range(n_folds):
        others = [matrix[:, edges[j] : edges[j + 1]] for j in range(n_folds) if j != index]
        oos = np.concatenate(others, axis=1)
        oos_scores = growth(oos)
        in_scores = fold_scores[index]
        winner = int(np.nanargmax(np.where(np.isfinite(in_scores), in_scores, -np.inf)))
        rank, percentile, omega = rank_positions_for(oos_scores, winner)
        per_fold.append(
            {
                "fold": index,
                "months": fold_labels[index]["months"],
                "n_months": fold_labels[index]["n_months"],
                "in_fold_winner": arm_ids[winner],
                "in_fold_winner_score": float(in_scores[winner]),
                "out_of_fold_rank_best1": rank,
                "out_of_fold_percentile_best0": percentile,
                "out_of_fold_omega": omega,
                "out_of_fold_score": float(oos_scores[winner]),
                "n_arms_ranked": len(arm_ids),
            }
        )

    pair_spearman = []
    for left, right in combinations(range(n_folds), 2):
        value = spearman(fold_scores[left], fold_scores[right])
        if math.isfinite(value):
            pair_spearman.append({"folds": [left, right], "spearman": value})
    values = [entry["spearman"] for entry in pair_spearman]
    percentiles = [entry["out_of_fold_percentile_best0"] for entry in per_fold]
    omegas = [entry["out_of_fold_omega"] for entry in per_fold]

    return {
        "n_folds": n_folds,
        "fold_months": fold_labels,
        "per_fold": per_fold,
        "mean_pairwise_spearman": float(np.mean(values)) if values else None,
        "min_pairwise_spearman": float(np.min(values)) if values else None,
        "max_pairwise_spearman": float(np.max(values)) if values else None,
        "pairwise_spearman": pair_spearman,
        "worst_out_of_fold_percentile_best0": float(np.max(percentiles)),
        "mean_out_of_fold_percentile_best0": float(np.mean(percentiles)),
        "worst_out_of_fold_omega": float(np.min(omegas)),
        "share_of_folds_winner_in_top_half": float(np.mean([value <= 0.50 for value in percentiles])),
        "j4_percentile_bar": J4_PERCENTILE_BAR,
        "j4_spearman_bar": J4_SPEARMAN_BAR,
        "j4_percentile_measured": float(np.max(percentiles)),
        "j4_spearman_measured": float(np.mean(values)) if values else None,
    }


# ----------------------------------------------------------------------------- stress maths ---
def fill_costs(fills_path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = pd.read_csv(
        fills_path,
        usecols=lambda name: name in ("timestamp", "qty", "price", "liquidity", "type"),
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame["notional"] = (frame["qty"].astype(float) * frame["price"].astype(float)).abs()
    liquidity = (
        frame["liquidity"].value_counts().to_dict() if "liquidity" in frame.columns else {}
    )
    provenance = {
        "fills": int(frame.shape[0]),
        "traded_notional": float(frame["notional"].sum()),
        "liquidity_counts": {str(key): int(value) for key, value in liquidity.items()},
        "first_fill": str(frame["timestamp"].min()),
        "last_fill": str(frame["timestamp"].max()),
        "fills_sha256": sha256_file(fills_path),
    }
    return frame, provenance


def stress_arm(frame: pd.DataFrame, equity: pd.Series, extra_bp: float) -> dict[str, Any]:
    """Subtract ``extra_bp`` of traded notional from the equity path and re-measure.

    Accounting convention: the equity sample at time ``T`` already reflects every fill up to
    ``T``, so the extra cost charged to that sample is the running sum of
    ``|qty x price| x bp/1e4`` over all fills with timestamp at or before ``T``.
    """
    extra = frame["notional"].to_numpy(dtype=float) * extra_bp / 1.0e4
    by_time = pd.Series(extra, index=frame["timestamp"]).groupby(level=0).sum().sort_index()
    cumulative = by_time.cumsum()
    aligned = cumulative.reindex(equity.index, method="ffill").fillna(0.0)
    stressed = np.maximum(equity.to_numpy(dtype=float) - aligned.to_numpy(dtype=float), 1e-9)

    def longest_drawdown(values: np.ndarray) -> float:
        peak = np.maximum.accumulate(values)
        with np.errstate(divide="ignore", invalid="ignore"):
            drawdown = np.where(peak > 0, 1.0 - values / peak, 0.0)
        return float(np.nanmax(drawdown))

    base_first = float(equity.iloc[0])
    return {
        "extra_bp": extra_bp,
        "total_extra_cost": float(extra.sum()),
        "total_extra_cost_share_of_initial": float(extra.sum() / base_first),
        "baseline_final_multiple": float(equity.iloc[-1] / base_first),
        "stressed_final_multiple": float(stressed[-1] / base_first),
        "baseline_worst_drawdown": longest_drawdown(equity.to_numpy(dtype=float)),
        "stressed_worst_drawdown": longest_drawdown(stressed),
    }


def build_stress(bound_arms: dict[str, list[str]]) -> dict[str, Any]:
    import panel

    wanted = list(STRESS_BASE_ARMS)
    for arm_ids in bound_arms.values():
        for arm_id in arm_ids:
            if arm_id not in wanted:
                wanted.append(arm_id)
    lookup = {}
    for run in panel.discover_runs():
        lookup[f"{run.round_key}|{run.arm}"] = run

    out: dict[str, Any] = {
        "generated_by": "report_tools/walkforward.py --stage stress",
        "declared_before_measurement": True,
        "selection_input": False,
        "selection_input_note": (
            "这些压力臂**明确不是选择输入**：它们在看任何压力数字之前就被声明，"
            "并且不参与 CSCV/PBO、DSR、折叠稳定性或任何选臂规则。它们只回答"
            "「已发布的头条结论在更差的成本下还剩多少」。"
        ),
        "method": (
            "对每一笔成交按其名义额 |qty×price| 追加 extra_bp 的额外成本，"
            "在成交时间戳上累积后前向填充到逐小时权益序列并直接相减，再重算终值倍数与最差回撤。"
            "这是一阶界：真实费率变化还会改变下单位置与订单结构，本算式无法建模；"
            "成交账本 fills.csv 是 run-local 文件，其 sha256 记录在此以便复核。"
        ),
        "stresses": [dict(stress) for stress in STRESS_ARMS],
        "arms": {},
    }
    for arm_id in wanted:
        run = lookup.get(arm_id)
        if run is None:
            out["arms"][arm_id] = {"unavailable": "arm not found on disk"}
            continue
        fills_path = run.run_dir / "fills.csv"
        equity_path = run.run_dir / "balance_and_equity.csv.gz"
        if not fills_path.exists() or not equity_path.exists():
            out["arms"][arm_id] = {
                "unavailable": "fills.csv or balance_and_equity.csv.gz missing",
                "run_dir": panel.relative(run.run_dir),
            }
            continue
        frame, provenance = fill_costs(fills_path)
        equity_frame = panel.load_equity(equity_path)
        equity = equity_frame[panel.EQUITY_COLUMN].astype(float)
        entry = {
            "round": run.round_key,
            "arm": run.arm,
            "leg": run.leg,
            "run_dir": panel.relative(run.run_dir),
            "declared_headline": arm_id in STRESS_BASE_ARMS,
            "provenance": provenance,
            "baseline": {
                "final_multiple": float(equity.iloc[-1] / equity.iloc[0]),
                "worst_drawdown": float(
                    np.nanmax(
                        1.0
                        - equity.to_numpy(dtype=float)
                        / np.maximum.accumulate(equity.to_numpy(dtype=float))
                    )
                ),
                "in_panel": any(arm_id in ids for ids in bound_arms.values()),
            },
            "stress": {},
        }
        for stress in STRESS_ARMS:
            result = stress_arm(frame, equity, stress["extra_bp"])
            result["retained_final_multiple_share"] = (
                result["stressed_final_multiple"] / result["baseline_final_multiple"]
                if result["baseline_final_multiple"]
                else None
            )
            result["drawdown_added"] = (
                result["stressed_worst_drawdown"] - result["baseline_worst_drawdown"]
            )
            entry["stress"][stress["key"]] = result
        out["arms"][arm_id] = entry
    return out


# ------------------------------------------------------------------------------------ driver ---
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("folds", "stress", "all"), default="all")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    index = load_json(PANEL_DIR / "index.json")

    if args.stage in ("folds", "all"):
        results: dict[str, Any] = {
            "generated_by": "report_tools/walkforward.py --stage folds",
            "fold_counts": list(FOLD_COUNTS),
            "primary_folds": PRIMARY_FOLDS,
            "j4_rule": (
                f"预注册 J4：样本内冠军在样本外的分位 ≤ {J4_PERCENTILE_BAR:.2f}，"
                f"且折叠间排名的平均 Spearman ≥ {J4_SPEARMAN_BAR:.2f}"
            ),
            "percentile_convention": (
                "out_of_fold_percentile_best0 = (rank_best1 − 1)/(n_arms − 1)：**0 = 最好、1 = 最差**，"
                "所以「不超过第 50 百分位」= 落在样本外前半区 = percentile ≤ 0.50。"
                "out_of_fold_omega = rank_best1/(n_arms+1) 是 CSCV 口径（≤ 0.5 = 下半区），"
                "两列都给出以避免读法歧义。"
            ),
            "rank_metric": "ADG = prod(1+r)^(12/n_months) − 1，仅在当期月份上计算",
            "conventions": {
                "block_edges": "edges[i] = floor(i·M/K)：整数运算，余数落在最早的块",
                "ranks": "并列值取平均名次（真实存在：多条保护未触发时权益曲线逐位相同）",
                "spearman": "对平均名次求 Pearson（标准并列修正 Spearman）",
            },
            "panels": {},
        }
        for name in sorted(index["panels"]):
            pool, leg = name.split("__")
            arm_ids, months, matrix, meta = load_panel(pool, leg)
            entry = {
                "pool": pool,
                "leg": leg,
                "n_arms_used": len(arm_ids),
                "n_months": len(months),
                "n_arms_removed": meta["n_arms_removed"],
                "removed_by_reason": meta["removed_by_reason"],
                "primary": None,
            }
            for folds in FOLD_COUNTS:
                key = f"K{folds}"
                entry[key] = fold_stability(arm_ids, months, matrix, folds)
                if folds == PRIMARY_FOLDS:
                    entry["primary"] = key
            results["panels"][name] = entry
            primary = entry[entry["primary"]]
            if not args.quiet:
                print(
                    f"== {name}: K={primary['n_folds']} "
                    f"worst OOF percentile={primary['j4_percentile_measured']:.3f} "
                    f"mean Spearman={primary['j4_spearman_measured']:+.3f}"
                )
                for row in primary["per_fold"]:
                    print(
                        f"     fold {row['fold']} {row['months'][0]}..{row['months'][1]} "
                        f"winner {row['in_fold_winner'].split('|')[-1]:22s} "
                        f"OOF rank {row['out_of_fold_rank_best1']}/{row['n_arms_ranked']} "
                        f"pct(best0) {row['out_of_fold_percentile_best0']:.3f}"
                    )
        write_json(ARTIFACTS / "fold_stability.json", results)

    if args.stage in ("stress", "all"):
        bound = {
            load_json(PANEL_DIR / f"{name}.json")["pool"] + "__"
            + load_json(PANEL_DIR / f"{name}.json")["leg"]:
            load_json(PANEL_DIR / f"{name}.json")["arm_ids"]
            for name in index["panels"]
        }
        stress = build_stress(bound)
        write_json(ARTIFACTS / "stress_arms.json", stress)
        if not args.quiet:
            print()
            for arm_id, entry in stress["arms"].items():
                if "unavailable" in entry:
                    print(f"== {arm_id}: {entry['unavailable']}")
                    continue
                print(
                    f"== {arm_id}: final {entry['baseline']['final_multiple']:.3f}x "
                    f"→ " + " ".join(
                        f"{key}={value['stressed_final_multiple']:.3f}x"
                        for key, value in entry["stress"].items()
                    )
                )


if __name__ == "__main__":
    sys.exit(main())
