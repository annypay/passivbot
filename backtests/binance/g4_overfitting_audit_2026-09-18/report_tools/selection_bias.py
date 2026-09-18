#!/usr/bin/env python3
"""Selection-bias corrections for the headline configurations.

Offline only: reads the tracked panels and the run-local equity ledgers they were derived from,
writes ``artifacts/selection_bias.json``.

Three corrections, each reported at several trial counts
-------------------------------------------------------
The trial count ``N`` this research line actually spent is only bounded from below (see
``artifacts/trial_ledger.json``). Every statistic below that takes ``N`` as an input is therefore
reported at ``N in {92, 200, 500, N_lower_bound}``. Because ``N_lower_bound`` is a lower bound,
every penalised number here is an **optimistic upper bound**: the true penalty is at least as
large.

* **Deflated Sharpe Ratio (DSR)** - Bailey & Lopez de Prado. Shrinks the observed Sharpe by the
  Sharpe expected from the best of ``N`` independent trials, then turns the gap into a probability.
  Reported on daily returns (the paper's convention, T ~ 700-2000) and on monthly panel returns
  (T ~ 30-66), with skewness and kurtosis shown so the arithmetic can be redone by hand.
* **Minimum Backtest Length (MinBTL)** - ``2*ln(N)/SR_ann^2`` years, the length of history needed
  before the observed annualised Sharpe stops being explainable by the best of ``N`` trials.
* **White/Hansen reality check** - stationary-bootstrap SPA and Romano-Wolf StepM p-values for
  "the best arm of this pool beats the benchmark". Unlike DSR this is a genuine multiple-testing
  test over the *observed* pool; it cannot see the trials that were never recorded, so a
  Bonferroni-style extrapolation to ``N`` is reported next to it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_overfitting_audit_2026-09-18"
ARTIFACTS = STUDY / "artifacts"
PANEL_DIR = ARTIFACTS / "panels"

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Pre-registered trial counts. The last entry is filled in from the ledger's own lower bound at
#: run time, so the sensitivity table can never silently disagree with ``trial_ledger.json``.
N_GRID_FIXED = (92, 200, 500)
#: Stationary-bootstrap settings, frozen before the numbers were seen.
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20260918
BLOCK_MONTHS_PRIMARY = 6
BLOCK_MONTHS_SENSITIVITY = (3, 6, 12)
EULER_GAMMA = 0.5772156649015329

#: Headline configurations, declared by key *before* any number was computed. These are the
#: settings the published rounds actually named, plus the in-sample search winner, which is
#: carried as the exhibit for how far an in-sample optimum travels.
HEADLINE_ARMS: tuple[tuple[str, str, str, str], ...] = (
    ("H1_guard_baseline_ext", "C_account_guard", "g_user12h__ext",
     "上一轮钉住的账户守护基准（unified 0.20 / EMA60 / 停 12H）"),
    ("H2_recommended_pre", "A_risk_geometry", "a_allow037__pre",
     "风险几何轮在样本外腿上推荐的设置（保持 allowance 0.37 + 守护）"),
    ("H3_best_geometry_ext", "A_risk_geometry", "b_red015__ext",
     "风险几何轮在 ext 腿上同时改善收益与回撤的变体（RED 0.15）"),
    ("H4_search_winner_3y", "A_risk_geometry", "c1__3y",
     "风险几何轮的搜索冠军（样本内 max ADG）——用于展示样本内最优的代价"),
    ("H5_10k_control_3y", "B_10k_replay", "twe300_10k__3y",
     "10k / TWE 3.0 无守护对照（各轮声明的 reference control）"),
)

#: Benchmark per panel. ``named`` must be a key that exists on disk; ``None`` means the declared
#: control is degenerate on that leg and only the equal-weight benchmark is available.
BENCHMARKS: dict[str, dict[str, str | None]] = {
    "poolA_risk_geometry__3y": {"named": "twe300_10k__3y"},
    "poolA_risk_geometry__ext": {"named": "twe300_10k_allowance0__ext"},
    "poolA_risk_geometry__pre": {"named": "a_allow037__pre"},
    "poolB_ext_union__ext": {"named": "g_user12h__ext"},
}
BENCHMARK_NOTES = {
    "twe300_10k__3y": "各轮 variant_input.json 声明的 3y 参考对照",
    "twe300_10k_allowance0__ext": (
        "ext 腿声明的对照 twe300_10k__ext 在 2021-05-19 强平、无完整序列，改用同一父配置族里"
        "序列完整的 twe300_10k_allowance0__ext"
    ),
    "a_allow037__pre": "风险几何轮声明的 pre 腿基准列",
    "g_user12h__ext": "账户守护轮发布并钉住的守护基准",
}
EQUAL_WEIGHT_NOTE = (
    "等权基准 = 该面板内所有入选 arm 月度收益的算术平均；它把「策略市场」当作对照，"
    "不依赖任何单一 arm 是否存活"
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


# ------------------------------------------------------------------------------- statistics ---
def sharpe_per_observation(returns: np.ndarray) -> float:
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 3:
        return float("nan")
    std = float(np.std(values, ddof=1))
    return float(np.mean(values) / std) if std > 0.0 else float("nan")


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """E[max Sharpe] of ``n_trials`` independent trials, in per-observation units."""
    if n_trials < 2 or not math.isfinite(sr_std) or sr_std <= 0.0:
        return 0.0
    left = stats.norm.ppf(1.0 - 1.0 / n_trials)
    right = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(sr_std * ((1.0 - EULER_GAMMA) * left + EULER_GAMMA * right))


def deflated_sharpe(
    returns: np.ndarray, n_trials: int, sr_std: float, periods_per_year: float
) -> dict[str, Any]:
    """DSR plus every input the arithmetic needs, so a reader can redo it."""
    values = np.asarray(returns, dtype=float)
    values = values[np.isfinite(values)]
    t_obs = int(values.size)
    out: dict[str, Any] = {
        "n_trials": n_trials,
        "n_observations": t_obs,
        "periods_per_year": periods_per_year,
    }
    if t_obs < 4:
        out.update({"dsr": None, "reason": "fewer than 4 observations"})
        return out
    sr = sharpe_per_observation(values)
    if not math.isfinite(sr):
        out.update({"dsr": None, "reason": "zero return variance"})
        return out
    skew = float(stats.skew(values, bias=False))
    kurtosis = float(stats.kurtosis(values, fisher=False, bias=False))
    sr0 = expected_max_sharpe(n_trials, sr_std)
    variance_term = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if variance_term <= 0.0:
        out.update({"dsr": None, "reason": "non-positive variance term"})
        return out
    z = (sr - sr0) * math.sqrt(t_obs - 1) / math.sqrt(variance_term)
    out.update(
        {
            "sharpe_per_observation": sr,
            "sharpe_annualised": sr * math.sqrt(periods_per_year),
            "expected_max_sharpe_sr0": sr0,
            "sr_std_across_trials": sr_std,
            "skewness": skew,
            "kurtosis_non_excess": kurtosis,
            "variance_term": variance_term,
            "z": float(z),
            "dsr": float(stats.norm.cdf(z)),
        }
    )
    return out


def min_btl_years(n_trials: int, sharpe_annualised: float) -> float | None:
    """Bailey/Lopez de Prado minimum backtest length, in years."""
    if n_trials < 2 or not math.isfinite(sharpe_annualised) or sharpe_annualised <= 0.0:
        return None
    return float(2.0 * math.log(n_trials) / (sharpe_annualised**2))


# ------------------------------------------------------------------- stationary bootstrap ---
def stationary_bootstrap_indices(
    n_observations: int, expected_block: float, rng: np.random.Generator
) -> np.ndarray:
    """Politis-Romano stationary bootstrap index path."""
    probability = 1.0 / max(expected_block, 1.0)
    indices = np.empty(n_observations, dtype=int)
    indices[0] = int(rng.integers(n_observations))
    for step in range(1, n_observations):
        if rng.random() < probability:
            indices[step] = int(rng.integers(n_observations))
        else:
            indices[step] = (indices[step - 1] + 1) % n_observations
    return indices


def reality_check(
    differences: np.ndarray, expected_block: float, replicates: int, rng: np.random.Generator
) -> dict[str, Any]:
    """Hansen SPA and Romano-Wolf StepM on ``differences`` of shape (n_arms, n_observations)."""
    n_arms, n_observations = differences.shape
    means = differences.mean(axis=1)
    stds = differences.std(axis=1, ddof=1)
    usable = stds > 0.0
    if not usable.any() or n_observations < 3:
        return {"spa_p_value": None, "stepm_p_value": None, "reason": "no usable arm"}
    t_stats = np.where(usable, math.sqrt(n_observations) * means / np.where(usable, stds, 1.0),
                       -np.inf)
    v_statistic = float(np.max(t_stats))

    z_boot = np.empty((replicates, n_arms), dtype=float)
    for replicate in range(replicates):
        indices = stationary_bootstrap_indices(n_observations, expected_block, rng)
        resampled = differences[:, indices]
        boot_means = resampled.mean(axis=1)
        z_boot[replicate] = np.where(
            usable, math.sqrt(n_observations) * (boot_means - means) / np.where(usable, stds, 1.0),
            -np.inf,
        )

    spa_p = float(np.mean(np.max(z_boot, axis=1) >= v_statistic))

    order = np.argsort(-t_stats)
    stepm = np.ones(n_arms, dtype=float)
    previous = 0.0
    for position, arm in enumerate(order):
        if not usable[arm]:
            stepm[arm] = 1.0
            continue
        remaining = order[position:]
        remaining = remaining[usable[remaining]]
        if remaining.size == 0:
            stepm[arm] = 1.0
            continue
        raw = float(np.mean(np.max(z_boot[:, remaining], axis=1) >= t_stats[arm]))
        previous = max(previous, raw)
        stepm[arm] = previous

    best = int(np.argmax(t_stats))
    resolution = 1.0 / replicates
    spa_raw = float(np.mean(np.max(z_boot, axis=1) >= v_statistic))
    stepm_raw = float(np.min(stepm))
    return {
        "spa_p_value": spa_raw,
        "stepm_p_value": stepm_raw,
        "stepm_p_value_best_arm": float(stepm[best]),
        #: A bootstrap p-value cannot resolve below 1/replicates; when the raw value is 0 the
        #: honest reading is "below the resolution", and every extrapolation uses that bound.
        "p_value_resolution": resolution,
        "spa_p_value_upper_bound": max(spa_raw, resolution),
        "stepm_p_value_upper_bound": max(stepm_raw, resolution),
        "best_arm_index": best,
        "best_arm_mean_difference": float(means[best]),
        "best_arm_t_statistic": float(t_stats[best]),
        "v_statistic": v_statistic,
        "n_arms_tested": int(usable.sum()),
        "expected_block": float(expected_block),
        "replicates": replicates,
        "seed": BOOTSTRAP_SEED,
        "multiple_testing_note": (
            "SPA/StepM 的零假设只覆盖**本面板里被观测到的** arm；没落盘的试验不在其中，"
            "因此下面另给一列按 N 外推的 Bonferroni 上界 p_adj = 1 − (1 − p)^(N/n_arms)，"
            "且 p 取自 bootstrap 分辨率上界（1/B）。"
        ),
    }


def bonferroni_extrapolation(p_value: float | None, n_trials: int, n_arms: int) -> float | None:
    if p_value is None or n_arms <= 0:
        return None
    ratio = max(1.0, n_trials / n_arms)
    return float(1.0 - (1.0 - p_value) ** ratio)


# ------------------------------------------------------------------------------------ driver ---
def monthly_lookup() -> dict[str, dict[str, float]]:
    """Every run's monthly returns by ``round|arm`` - benchmarks may live outside the pool."""
    import panel

    lookup: dict[str, dict[str, float]] = {}
    for run in panel.discover_runs():
        path = run.run_dir / "balance_and_equity.csv.gz"
        if not path.exists():
            continue
        frame = panel.load_equity(path)
        months, returns = panel.monthly_returns(frame)
        lookup[f"{run.round_key}|{run.arm}"] = dict(zip(months, returns.tolist()))
    return lookup


def daily_lookup() -> dict[str, np.ndarray]:
    import panel

    lookup: dict[str, np.ndarray] = {}
    for run in panel.discover_runs():
        path = run.run_dir / "balance_and_equity.csv.gz"
        if not path.exists():
            continue
        frame = panel.load_equity(path)
        lookup[f"{run.round_key}|{run.arm}"] = panel.daily_returns(frame)
    return lookup


def align(monthly: dict[str, float], months: list[str]) -> np.ndarray | None:
    values = np.array([monthly.get(month, np.nan) for month in months], dtype=float)
    if not np.isfinite(values).all():
        return None
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    ledger = load_json(ARTIFACTS / "trial_ledger.json")
    index = load_json(PANEL_DIR / "index.json")
    n_bound = int(ledger["n_lower_bound"])
    n_grid = list(N_GRID_FIXED) + [n_bound]

    monthly = monthly_lookup()
    daily = daily_lookup()

    results: dict[str, Any] = {
        "generated_by": "report_tools/selection_bias.py",
        "n_grid": n_grid,
        "n_lower_bound": n_bound,
        "n_direction_note": (
            f"N = {n_bound} 是台账给出的**下界**（见 artifacts/trial_ledger.json）：被合同声明但从未"
            "真跑的格子、优化器未落盘的非 Pareto 候选、以及任何没有留下记录的人工试错都不计入。"
            "因此下面所有以 N 为输入的惩罚（DSR 的 SR0、MinBTL、p_adj）都是**乐观端**：真实 N 更大，"
            "真实惩罚只会更重。判断「通过」时请把这一列读成「最好情况」。"
        ),
        "formulas": {
            "dsr": (
                "SR0 = sqrt(Var(SR_trials)) · [(1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e))]，γ=0.5772；"
                "DSR = Φ( (SR − SR0)·sqrt(T−1) / sqrt(1 − γ3·SR + (γ4−1)/4·SR²) )；"
                "SR 与 SR0 同为「每次观测」单位（日频或月频），γ3=偏度，γ4=非超额峰度"
            ),
            "min_btl": "MinBTL(年) = 2·ln(N) / SR_annualised²（Bailey & López de Prado 近似式）",
            "sr_std_proxy": (
                "Var(SR_trials) 用**本面板入选 arm 的横截面**每次观测 Sharpe 方差作代理；"
                "真实试验集的离散度不可知，这是本审计明确承认的近似"
            ),
            "reality_check": (
                "d_k,t = r_k,t − r_benchmark,t；t_k = sqrt(T)·mean(d_k)/std(d_k)；"
                "平稳自助（Politis–Romano，期望块长 q 个月，B=2000，seed=20260918）重抽时间索引，"
                "SPA p = P(max_k z*_k ≥ max_k t_k)；StepM 为 Romano–Wolf 逐步降维并强制单调"
            ),
        },
        "headline_arms": {},
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
            "months": months,
            "arm_ids": arm_ids,
            "n_arms_removed": meta["n_arms_removed"],
            "removed_by_reason": meta["removed_by_reason"],
            "trial_sharpe_dispersion_proxy": {},
            "reality_check": {},
            "deflated_sharpe": {},
            "min_btl_years": {},
        }

        monthly_sharpes = [sharpe_per_observation(row) for row in matrix]
        monthly_sharpes = [value for value in monthly_sharpes if math.isfinite(value)]
        sr_std_monthly = float(np.std(monthly_sharpes, ddof=1)) if len(monthly_sharpes) > 1 else 0.0
        daily_sharpes = []
        for arm_id in arm_ids:
            series = daily.get(arm_id)
            if series is None or series.size < 3:
                continue
            value = sharpe_per_observation(series)
            if math.isfinite(value):
                daily_sharpes.append(value)
        sr_std_daily = float(np.std(daily_sharpes, ddof=1)) if len(daily_sharpes) > 1 else 0.0
        entry["trial_sharpe_dispersion_proxy"] = {
            "monthly_sr_std": sr_std_monthly,
            "monthly_sr_n": len(monthly_sharpes),
            "daily_sr_std": sr_std_daily,
            "daily_sr_n": len(daily_sharpes),
        }

        # ---- deflated Sharpe per headline configuration within this panel -------------------
        for arm_id in arm_ids:
            monthly_series = matrix[arm_ids.index(arm_id)]
            daily_series = daily.get(arm_id)
            entry["deflated_sharpe"][arm_id] = {
                "monthly": {
                    str(n): deflated_sharpe(monthly_series, n, sr_std_monthly, 12.0)
                    for n in n_grid
                },
                "daily": (
                    {
                        str(n): deflated_sharpe(daily_series, n, sr_std_daily, 365.0)
                        for n in n_grid
                    }
                    if daily_series is not None and daily_series.size >= 4
                    else None
                ),
            }
            annualised = (
                sharpe_per_observation(daily_series) * math.sqrt(365.0)
                if daily_series is not None and daily_series.size >= 4
                else sharpe_per_observation(monthly_series) * math.sqrt(12.0)
            )
            entry["min_btl_years"][arm_id] = {
                "sharpe_annualised": float(annualised),
                "source": "daily" if daily_series is not None and daily_series.size >= 4 else "monthly",
                "years": {str(n): min_btl_years(n, float(annualised)) for n in n_grid},
                "observations_daily": int(daily_series.size) if daily_series is not None else 0,
                "observations_monthly": int(monthly_series.size),
            }

        # ---- reality check against two benchmarks ------------------------------------------
        equal_weight = matrix.mean(axis=0)
        benchmarks: dict[str, dict[str, Any]] = {
            "equal_weight": {
                "note": EQUAL_WEIGHT_NOTE,
                "returns": equal_weight.tolist(),
            }
        }
        named_key = BENCHMARKS.get(name, {}).get("named")
        if named_key:
            series = monthly.get(named_key)
            aligned = align(series, months) if series else None
            benchmarks["named_control"] = {
                "arm_id": named_key,
                "note": BENCHMARK_NOTES.get(named_key, ""),
                "available": aligned is not None,
                "returns": aligned.tolist() if aligned is not None else None,
            }
        for label, benchmark in benchmarks.items():
            returns = benchmark.get("returns")
            if not returns:
                continue
            differences = matrix - np.asarray(returns, dtype=float)[None, :]
            for block in BLOCK_MONTHS_SENSITIVITY:
                rng = np.random.default_rng(BOOTSTRAP_SEED + block)
                outcome = reality_check(differences, block, BOOTSTRAP_REPLICATES, rng)
                best = outcome.get("best_arm_index")
                outcome["best_arm"] = arm_ids[best] if isinstance(best, int) else None
                outcome["p_adjusted_by_n"] = {
                    str(n): bonferroni_extrapolation(
                        outcome.get("spa_p_value_upper_bound"), n, len(arm_ids)
                    )
                    for n in n_grid
                }
                entry["reality_check"][f"{label}|q{block}"] = outcome

        results["panels"][name] = entry
        if not args.quiet:
            primary = entry["reality_check"].get(f"equal_weight|q{BLOCK_MONTHS_PRIMARY}", {})
            print(
                f"== {name}: arms={len(arm_ids)} months={len(months)} "
                f"sr_std(daily)={sr_std_daily:.4f} sr_std(monthly)={sr_std_monthly:.4f}"
            )
            print(
                f"   SPA(q={BLOCK_MONTHS_PRIMARY}) p={primary.get('spa_p_value')} "
                f"StepM p={primary.get('stepm_p_value')} best={primary.get('best_arm')}"
            )

    # ---- headline configurations, each on its own leg --------------------------------------
    panel_members: dict[str, list[str]] = {
        name: load_json(PANEL_DIR / f"{name}.json")["arm_ids"] for name in index["panels"]
    }
    panel_legs: dict[str, str] = {
        name: load_json(PANEL_DIR / f"{name}.json")["leg"] for name in index["panels"]
    }

    def proxy_panel(arm_id: str) -> tuple[str | None, str]:
        """Panel whose Sharpe dispersion deflates this arm, and why that panel was chosen.

        Declared fallback order: (1) the panel the arm is inside; (2) among panels on the same leg,
        the one with the most arms, because dispersion estimated from more trials is the
        conservative choice; (3) the largest panel overall.
        """
        for name, members in sorted(panel_members.items()):
            if arm_id in members:
                return name, "arm 本身就在该面板内"
        leg = arm_id.split("|")[-1].rsplit("__", 1)[-1]
        same_leg = [name for name in sorted(panel_members) if panel_legs[name] == leg]
        if same_leg:
            best = max(same_leg, key=lambda name: (len(panel_members[name]), name))
            return best, f"该 arm 不在任何面板内，取同腿（{leg}）里 arm 数最多的面板作离散度代理"
        best = max(sorted(panel_members), key=lambda name: (len(panel_members[name]), name))
        return best, "该 arm 不在任何面板内且没有同腿面板，取全局 arm 数最多的面板"

    for label, round_key, arm, note in HEADLINE_ARMS:
        arm_id = f"{round_key}|{arm}"
        daily_series = daily.get(arm_id)
        monthly_series_map = monthly.get(arm_id)
        entry = {"arm": arm, "round": round_key, "note": note, "deflated_sharpe": {},
                 "min_btl_years": {}}
        if monthly_series_map:
            months = sorted(monthly_series_map)
            monthly_series = np.array([monthly_series_map[month] for month in months], dtype=float)
            panel_name, panel_reason = proxy_panel(arm_id)
            entry["panel"] = panel_name
            entry["panel_choice_reason"] = panel_reason
            sr_std_monthly = 0.0
            sr_std_daily = 0.0
            if panel_name:
                panel_entry = results["panels"][panel_name]
                sr_std_monthly = panel_entry["trial_sharpe_dispersion_proxy"]["monthly_sr_std"]
                sr_std_daily = panel_entry["trial_sharpe_dispersion_proxy"]["daily_sr_std"]
            entry["sr_std_used"] = {"daily": sr_std_daily, "monthly": sr_std_monthly}
            entry["deflated_sharpe"]["monthly"] = {
                str(n): deflated_sharpe(monthly_series, n, sr_std_monthly, 12.0) for n in n_grid
            }
            entry["deflated_sharpe"]["daily"] = (
                {str(n): deflated_sharpe(daily_series, n, sr_std_daily, 365.0) for n in n_grid}
                if daily_series is not None and daily_series.size >= 4
                else None
            )
            annualised = (
                sharpe_per_observation(daily_series) * math.sqrt(365.0)
                if daily_series is not None and daily_series.size >= 4
                else sharpe_per_observation(monthly_series) * math.sqrt(12.0)
            )
            entry["min_btl_years"] = {
                "sharpe_annualised": float(annualised),
                "source": "daily" if daily_series is not None and daily_series.size >= 4 else "monthly",
                "years": {str(n): min_btl_years(n, float(annualised)) for n in n_grid},
            }
        else:
            entry["unavailable"] = "no monthly series on disk"
        results["headline_arms"][label] = entry

    write_json(ARTIFACTS / "selection_bias.json", results)
    if not args.quiet:
        print(f"\nN grid: {n_grid}")
        print(f"wrote {ARTIFACTS / 'selection_bias.json'}")


if __name__ == "__main__":
    sys.exit(main())
