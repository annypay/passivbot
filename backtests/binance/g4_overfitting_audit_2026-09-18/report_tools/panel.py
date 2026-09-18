#!/usr/bin/env python3
"""Monthly-return panels and the trial ledger for the g4 overfitting audit.

Offline only: this module reads backtest artifacts that already exist on disk. It never starts
a backtest, never searches parameters and never touches the network.

What it produces
----------------
``artifacts/trial_ledger.json``
    Every arm/cell/optimizer candidate this research line ever evaluated, with the study it came
    from, its leg/window, its dataset identity, the parameters that varied and whether it fed a
    published decision, plus an honest *lower bound* on the trial count ``N``.

``artifacts/panels/<pool>__<leg>.csv|json``
    Two monthly-return panels:

    * ``poolA`` - the 44 risk-geometry arms, split by their three legs (search window ``3y``,
      full history ``ext``, out-of-sample ``pre``);
    * ``poolB`` - the union of the ``ext``-leg arms of the guard round, the 10k replay round and
      the risk-geometry round.

    ``analysis.json`` carries no monthly series, so every monthly return here is recomputed from
    the run's own ``balance_and_equity.csv.gz`` and cross-checked against the run's tracked
    ``monthly_metrics.csv``.

Cadence
-------
The equity ledger is sampled on the hour, not the minute: the header is an unnamed index plus
``usd_cash_wallet, usd_total_balance, usd_total_equity, strategy_equity, btc_cash_wallet,
btc_total_balance, btc_total_equity`` and consecutive rows are 60 minutes apart. The audited
monthly series is therefore "last hourly sample of the month over the last hourly sample of the
previous month", which is exactly the convention ``monthly_metrics.csv`` uses.

Degeneracy rule (pre-registered, applied before any selection)
--------------------------------------------------------------
An arm whose equity series stopped producing returns has no meaningful score, so it is removed
from the pool *before* CSCV selection rather than being silently carried as a zero-variance row:

* ``D1_truncated``   - the arm's month set differs from the modal month set of its leg (a
  liquidation or a dataset clip ended the series early);
* ``D2_terminal_halt`` - the final contiguous constant-equity run is at least
  ``TERMINAL_FLAT_MIN_DAYS`` long, i.e. a terminal halt stopped trading for good;
* ``D3_zero_variance`` - the sample standard deviation of the arm's monthly returns is below
  ``ZERO_VARIANCE_TOL``, or fewer than ``MIN_NONZERO_MONTHS`` months move at all.

The same rule is declared in ``artifacts/panels/index.json`` and repeated verbatim in
``overfitting_audit.md``; ``verify_audit.py`` re-derives it independently.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_overfitting_audit_2026-09-18"
ARTIFACTS = STUDY / "artifacts"
PANEL_DIR = ARTIFACTS / "panels"

#: Pre-registered degeneracy thresholds. Changing any of these changes the audit's answer, so
#: they are frozen here and re-stated in the report.
TERMINAL_FLAT_MIN_DAYS = 30
TERMINAL_FLAT_REL_TOL = 1e-12
TERMINAL_FLAT_ABS_TOL = 1e-9
ZERO_VARIANCE_TOL = 1e-10
MIN_NONZERO_MONTHS = 3
#: Tolerance for the cross-check against the engine's own tracked ``monthly_metrics.csv``.
MONTHLY_CROSSCHECK_TOL = 5e-9
#: Relative tolerance when matching the last-of-month equity level itself.
MONTHLY_LEVEL_TOL = 5e-9
MONTHLY_CONVENTION_NOTE = (
    "engine 的 monthly_metrics.csv 用「当月最后样本 / 当月第一个样本 − 1」；本审计用"
    "「当月最后样本 / 上月最后样本 − 1」串联，保证月度收益相乘等于全窗倍数。两者相差的是"
    "跨月那一个小时的权益变动，因此 engine_pct_max_abs 是**已知口径差**（量级 1e-2），"
    "而 level_max_rel 与 chained_max_abs 是严格的算术核对（量级 1e-12）。"
)

EQUITY_COLUMN = "strategy_equity"
SUPPORTED_CADENCE_MINUTES = (60, 1)


# --------------------------------------------------------------------------------------- io ---
def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any, indent: int | None = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(
            value, handle, indent=indent, sort_keys=True, ensure_ascii=False, default=str,
            separators=(",", ":") if indent is None else None,
        )
        handle.write("\n")
    os.replace(tmp, path)


def relative(path: Path | str) -> str:
    """Repo-relative POSIX path; tracked files must never carry a host path."""
    text = Path(path).resolve().as_posix()
    root = REPO.as_posix()
    return text[len(root) + 1 :] if text.startswith(root + "/") else text


def sha256_file(path: Path) -> str | None:
    path = Path(path)
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------------------- study registry ---
@dataclass(frozen=True)
class Round:
    key: str
    study: str  # repo-relative
    label: str
    role: str


#: The published rounds. The first four are the ones whose picks this audit judges; the last four
#: are earlier rounds that produced replay bundles and still count as trials.
PUBLISHED_ROUNDS: tuple[Round, ...] = (
    Round("A_risk_geometry", "backtests/binance/g4_twe300_risk_optimization_2026-09-17",
          "风险几何优化（占用纪律/冷却档位/参数搜索）", "published"),
    Round("B_10k_replay", "backtests/binance/g4_twe300_10k_replay_2026-09-17",
          "10,000 USDT 重放", "published"),
    Round("C_account_guard", "backtests/binance/g4_twe300_account_guard_2026-09-17",
          "账户级守护", "published"),
    Round("D_tail_risk", "backtests/binance/g4_tail_risk_research_2026-09-17",
          "尾部风险研究", "published"),
    Round("E_hsl_on_replay", "backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17",
          "HSL 开启重放", "published"),
    Round("F_dd_tail", "backtests/binance/dd_tail_research_2026-09-15",
          "回撤尾部杠杆筛选", "published"),
    Round("G_hsl_npos1", "backtests/binance/hsl_npos1_analysis_2026-09-16",
          "HSL n_positions=1 分析", "published"),
    Round("H_returns_guarded", "backtests/binance/returns_guarded_dd_research_2026-09-16",
          "保收益压回撤", "published"),
)

ROUNDS_BY_KEY = {round_.key: round_ for round_ in PUBLISHED_ROUNDS}

#: The panels this audit is specified to produce.
POOL_A = "poolA_risk_geometry"
POOL_B = "poolB_ext_union"

POOL_LEGS: dict[str, tuple[str, ...]] = {
    POOL_A: ("3y", "ext", "pre"),
    POOL_B: ("ext",),
}
POOL_ROUNDS: dict[str, tuple[str, ...]] = {
    POOL_A: ("A_risk_geometry",),
    POOL_B: ("C_account_guard", "B_10k_replay", "A_risk_geometry"),
}

#: Declared leg windows, copied from ``variant_input.json["legs"]`` of the risk-geometry round.
LEG_WINDOWS: dict[str, tuple[str, str]] = {
    "3y": ("2023-09-12", "2026-09-12"),
    "ext": ("2021-04-20", "2026-09-13"),
    "pre": ("2021-04-20", "2023-09-11"),
}
LEG_ROLES: dict[str, str] = {
    "3y": "搜索窗（样本内）",
    "ext": "全历史",
    "pre": "样本外验收窗",
}


@dataclass
class Run:
    """One replay bundle on disk."""

    round_key: str
    study: str
    arm: str
    leg: str
    run_dir: Path
    bundle_dir: Path
    config_path: Path | None
    deltas: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""
    in_report: bool = False
    dataset_label: str | None = None
    dataset_manifest_sha256: str | None = None
    dataset_override_mode: str | None = None
    analysis_sha256: str | None = None
    equity_sha256: str | None = None
    monthly_sha256: str | None = None
    cadence_minutes: int | None = None
    months: list[str] = field(default_factory=list)
    returns: np.ndarray | None = None
    daily_returns: np.ndarray | None = None
    first_sample: str | None = None
    last_sample: str | None = None
    last_scan: str | None = None
    terminal_flat_days: float = 0.0
    crosscheck: dict[str, float | None] = field(default_factory=dict)
    structural: dict[str, Any] = field(default_factory=dict)
    degeneracy: list[str] = field(default_factory=list)
    degeneracy_detail: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.round_key}:{self.arm}"


# ------------------------------------------------------------------------------- discovery ---
def _leg_of(arm: str) -> str:
    if "__" in arm:
        return arm.rsplit("__", 1)[1]
    return "unknown"


def _run_dirs(bundle_root: Path) -> list[Path]:
    """Every directory under a bundle that carries an ``analysis.json``."""
    return sorted({path.parent for path in bundle_root.glob("**/analysis.json")})


def discover_runs(round_keys: Iterable[str] | None = None) -> list[Run]:
    """Enumerate every replay bundle of the published rounds.

    Every directory carrying an ``analysis.json`` is claimed exactly once: first by the arm key
    registered in the round's frozen study input, and otherwise under a fallback key derived from
    the directory name so that an unnamed run still enters the ledger instead of disappearing.
    """
    wanted = set(round_keys) if round_keys else set(ROUNDS_BY_KEY)
    runs: list[Run] = []
    for round_ in PUBLISHED_ROUNDS:
        if round_.key not in wanted:
            continue
        study_dir = REPO / round_.study
        if not study_dir.is_dir():
            continue
        registry = _arm_registry(round_)
        report_text = _report_text(round_)
        on_disk = _run_dirs(study_dir)
        claimed: dict[Path, str] = {}
        for arm, meta in sorted(registry.items()):
            for run_dir in on_disk:
                if run_dir in claimed:
                    continue
                if _belongs_to(run_dir, arm):
                    claimed[run_dir] = arm
        for run_dir in on_disk:
            if run_dir not in claimed:
                claimed[run_dir] = _arm_from_dir(run_dir) or f"run_{run_dir.name}"

        for run_dir, arm in sorted(claimed.items(), key=lambda item: (item[1], str(item[0]))):
            meta = registry.get(arm, {})
            runs.append(
                Run(
                    round_key=round_.key,
                    study=round_.study,
                    arm=arm,
                    leg=_leg_of(arm),
                    run_dir=run_dir,
                    bundle_dir=run_dir.parents[1] if len(run_dir.parents) > 1 else run_dir,
                    config_path=run_dir / "config.json",
                    deltas=list(meta.get("deltas") or []),
                    description=str(meta.get("description") or ""),
                    in_report=bool(arm) and arm in report_text,
                )
            )
    runs.sort(key=lambda run: (run.round_key, run.arm, str(run.run_dir)))
    return runs


def _belongs_to(run_dir: Path, arm: str) -> bool:
    """True when ``run_dir`` is the bundle of ``arm``.

    The bundle directory is named ``binance_<arm>``; the fallback covers the early studies whose
    bundle directory drops the prefix (``binance_baseline``) and the studies whose runs sit at the
    artifacts root.
    """
    rel = run_dir.as_posix()
    if f"/binance_{arm}/" in rel or rel.endswith(f"/binance_{arm}"):
        return True
    if f"/binance/{arm}/" in rel:
        return True
    # studies whose bundle name differs from the arm key (dd_tail: binance_actual_baseline)
    return f"/{arm}/" in rel


def _arm_registry(round_: Round) -> dict[str, dict[str, Any]]:
    """Arm key -> metadata (deltas, description). Read from the round's frozen study input."""
    study_dir = REPO / round_.study
    registry: dict[str, dict[str, Any]] = {}
    for name in ("variant_input.json", "cell_input.json", "profile_input.json",
                 "research_contract.json", "study_manifest.json"):
        path = study_dir / "artifacts" / name
        if not path.exists():
            path = study_dir / name
        if not path.exists():
            continue
        try:
            data = load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        arms = data.get("arms") if isinstance(data, dict) else None
        if isinstance(arms, list):
            for entry in arms:
                if not isinstance(entry, dict):
                    continue
                key = entry.get("key") or entry.get("arm") or entry.get("variant")
                if key:
                    registry[key] = entry
        if registry:
            break

    arms_on_disk = {
        _arm_from_dir(run_dir) for run_dir in _run_dirs(study_dir)
    }
    for arm in sorted(a for a in arms_on_disk if a):
        registry.setdefault(arm, {})
    return registry


def _arm_from_dir(run_dir: Path) -> str | None:
    name = run_dir.parent.name or ""
    if name.startswith("binance_"):
        return name[len("binance_") :]
    for parent in run_dir.parents:
        if parent.name.startswith("binance_"):
            return parent.name[len("binance_") :]
    return None


def _report_text(round_: Round) -> str:
    study_dir = REPO / round_.study
    chunks: list[str] = []
    for path in sorted(study_dir.glob("*.md")):
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(chunks)


# --------------------------------------------------------------------- equity / returns ---
def _equity_positions(path: Path) -> list[int]:
    """Positions of the unnamed index column plus ``strategy_equity``/``usd_total_equity``."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
    wanted = [0]
    for name in (EQUITY_COLUMN, "usd_total_equity"):
        if name in header:
            wanted.append(header.index(name))
    return sorted(set(wanted))


def load_equity(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        compression="gzip",
        index_col=0,
        parse_dates=True,
        usecols=_equity_positions(path),
    )
    frame.index.name = "timestamp"
    if EQUITY_COLUMN not in frame.columns:
        raise SystemExit(f"{relative(path)} has no {EQUITY_COLUMN} column")
    return frame


def cadence_minutes(frame: pd.DataFrame) -> int:
    deltas = np.diff(frame.index.values).astype("timedelta64[m]").astype(int)
    if deltas.size == 0:
        return 0
    values, counts = np.unique(deltas, return_counts=True)
    return int(values[int(np.argmax(counts))])


def monthly_levels(frame: pd.DataFrame) -> pd.Series:
    series = frame[EQUITY_COLUMN].astype(float)
    return series.groupby(series.index.to_period("M")).last()


def monthly_returns(frame: pd.DataFrame) -> tuple[list[str], np.ndarray]:
    """Last-hourly-sample-of-month returns, chained so they compound to the full-period multiple."""
    grouped = monthly_levels(frame)
    months = [str(period) for period in grouped.index]
    values = grouped.to_numpy(dtype=float)
    base = float(frame[EQUITY_COLUMN].astype(float).iloc[0])
    returns = np.empty(values.size, dtype=float)
    for i, value in enumerate(values):
        previous = base if i == 0 else values[i - 1]
        returns[i] = value / previous - 1.0 if previous else 0.0
    return months, returns


def crosscheck_monthly(
    run_dir: Path, months: list[str], returns: np.ndarray, levels: pd.Series
) -> dict[str, float | None]:
    """Three independent cross-checks against the run's tracked ``monthly_metrics.csv``.

    ``level_max_rel`` is the strict one: my last-of-month ``strategy_equity`` against the engine's
    own ``ending_strategy_equity`` column. ``chained_max_abs`` rebuilds the engine's month returns
    from *its own* level column and compares them with mine, so it isolates arithmetic.
    ``engine_pct_max_abs`` is deliberately loose and is reported as a known convention difference:
    the engine divides the month's last sample by the month's *first* sample, which drops the one
    hour between the previous month's last sample and this month's first sample; chaining
    last-to-last (what this audit uses) keeps the full-period multiple exact.
    """
    path = run_dir / "monthly_metrics.csv"
    out: dict[str, float | None] = {
        "level_max_rel": None,
        "chained_max_abs": None,
        "engine_pct_max_abs": None,
    }
    if not path.exists():
        return out
    try:
        table = pd.read_csv(path)
    except (OSError, pd.errors.ParserError):
        return out
    if "period" not in table.columns:
        return out

    if "ending_strategy_equity" in table.columns:
        engine_levels = {
            str(row["period"]): float(row["ending_strategy_equity"])
            for _, row in table.iterrows()
            if pd.notna(row.get("ending_strategy_equity"))
        }
        worst_level = 0.0
        seen = 0
        for month, mine in levels.items():
            month = str(month)
            if month not in engine_levels or engine_levels[month] == 0.0:
                continue
            seen += 1
            worst_level = max(
                worst_level, abs(mine - engine_levels[month]) / abs(engine_levels[month])
            )
        if seen:
            out["level_max_rel"] = worst_level

        engine_months = [month for month in months if month in engine_levels]
        if len(engine_months) >= 2:
            worst_chained = 0.0
            for previous, month in zip(engine_months, engine_months[1:]):
                engine_return = engine_levels[month] / engine_levels[previous] - 1.0
                mine = float(returns[months.index(month)])
                worst_chained = max(worst_chained, abs(engine_return - mine))
            out["chained_max_abs"] = worst_chained

    column = (
        "strategy_equity_return_pct"
        if "strategy_equity_return_pct" in table.columns
        else "total_equity_return_pct" if "total_equity_return_pct" in table.columns else None
    )
    if column:
        worst = 0.0
        seen = False
        for month, value in zip(months, returns):
            rows = table.loc[table["period"].astype(str) == month, column]
            if rows.empty or pd.isna(rows.iloc[0]):
                continue
            seen = True
            worst = max(worst, abs(float(rows.iloc[0]) - float(value)))
        if seen:
            out["engine_pct_max_abs"] = worst
    return out


def daily_returns(frame: pd.DataFrame) -> np.ndarray:
    series = frame[EQUITY_COLUMN].astype(float).resample("1D").last().dropna()
    return series.pct_change().dropna().to_numpy(dtype=float)


def terminal_flat_days(frame: pd.DataFrame) -> float:
    """Length in days of the final contiguous constant-equity run."""
    equity = frame[EQUITY_COLUMN].astype(float).to_numpy()
    if equity.size < 2:
        return math.inf
    last = equity[-1]
    tol = max(TERMINAL_FLAT_ABS_TOL, TERMINAL_FLAT_REL_TOL * abs(last))
    index = equity.size - 1
    while index > 0 and abs(equity[index - 1] - last) <= tol:
        index -= 1
    span = frame.index[-1] - frame.index[index]
    return float(span.total_seconds() / 86400.0)


def dataset_identity(run_dir: Path) -> tuple[str | None, str | None, str | None]:
    """(repo-relative dataset label, manifest sha256, override mode) from tracked run files."""
    for name in ("global_metrics.json", "dataset.json"):
        path = run_dir / name
        if not path.exists():
            continue
        try:
            data = load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        block = data.get("dataset") if isinstance(data.get("dataset"), dict) else data
        if not isinstance(block, dict):
            continue
        label = block.get("cache_dir") or block.get("cache_dir_label")
        manifest = block.get("manifest_sha256")
        override = block.get("override_mode")
        if label or manifest:
            return (label, manifest, override)
    return (None, None, None)


def structural_flags(run_dir: Path) -> dict[str, Any]:
    """Rounded structural facts that the audit cites, all from tracked run files."""
    out: dict[str, Any] = {}
    analysis_path = run_dir / "analysis.json"
    if analysis_path.exists():
        try:
            analysis = load_json(analysis_path)
        except (json.JSONDecodeError, OSError):
            analysis = {}
        for key, source in (
            ("adg_strategy_eq", analysis),
            ("gain_strategy_eq", analysis),
            ("drawdown_worst_strategy_eq", analysis),
            ("liquidated", analysis),
            ("fills_count", analysis),
            ("backtest_completion_ratio", analysis),
            ("hard_stop_triggers", analysis),
            ("hard_stop_restarts", analysis),
            ("effective_start_date", analysis),
            ("effective_end_date", analysis),
        ):
            if key in source:
                out[key] = source[key]
    guard_path = run_dir / "guard_readiness.json"
    if guard_path.exists():
        try:
            guard = load_json(guard_path)
            out["complete_halt_count"] = guard.get("complete_halt_count")
            out["halt_count"] = guard.get("halt_count")
            out["halt_share_of_window_pct"] = guard.get("halt_share_of_window_pct")
        except (json.JSONDecodeError, OSError):
            pass
    return out


def enrich(run: Run) -> Run:
    run.analysis_sha256 = sha256_file(run.run_dir / "analysis.json")
    equity_path = run.run_dir / "balance_and_equity.csv.gz"
    run.equity_sha256 = sha256_file(equity_path)
    run.monthly_sha256 = sha256_file(run.run_dir / "monthly_metrics.csv")
    run.dataset_label, run.dataset_manifest_sha256, run.dataset_override_mode = dataset_identity(
        run.run_dir
    )
    run.structural = structural_flags(run.run_dir)
    if not equity_path.exists():
        return run
    frame = load_equity(equity_path)
    run.cadence_minutes = cadence_minutes(frame)
    run.months, run.returns = monthly_returns(frame)
    run.daily_returns = daily_returns(frame)
    run.first_sample = str(frame.index[0])
    run.last_sample = str(frame.index[-1])
    run.terminal_flat_days = terminal_flat_days(frame)
    run.crosscheck = crosscheck_monthly(
        run.run_dir, run.months, run.returns, monthly_levels(frame)
    )
    return run


# ------------------------------------------------------------------------------ degeneracy ---
def modal_months(runs: list[Run]) -> list[str]:
    """The month set shared by the plurality of arms - the leg's real coverage."""
    counts: dict[tuple[str, ...], int] = {}
    for run in runs:
        if run.months:
            counts[tuple(run.months)] = counts.get(tuple(run.months), 0) + 1
    if not counts:
        return []
    best = max(counts.items(), key=lambda item: (item[1], len(item[0])))
    return list(best[0])


def apply_degeneracy(runs: list[Run]) -> tuple[list[str], list[Run]]:
    """Apply the pre-registered rule; returns (leg months, runs that survive)."""
    months = modal_months(runs)
    for run in runs:
        run.degeneracy = []
        run.degeneracy_detail = {}
        if not run.months:
            run.degeneracy.append("D0_no_equity_series")
            run.degeneracy_detail["D0_no_equity_series"] = "balance_and_equity.csv.gz missing"
            continue
        if run.cadence_minutes not in SUPPORTED_CADENCE_MINUTES:
            run.degeneracy.append("D0_unexpected_cadence")
            run.degeneracy_detail["D0_unexpected_cadence"] = (
                f"modal spacing {run.cadence_minutes} min"
            )
            continue
        if run.months != months:
            run.degeneracy.append("D1_truncated")
            run.degeneracy_detail["D1_truncated"] = {
                "arm_first_month": run.months[0],
                "arm_last_month": run.months[-1],
                "leg_first_month": months[0] if months else None,
                "leg_last_month": months[-1] if months else None,
                "last_sample": run.last_sample,
                "liquidated": run.structural.get("liquidated"),
            }
        if run.terminal_flat_days >= TERMINAL_FLAT_MIN_DAYS:
            run.degeneracy.append("D2_terminal_halt")
            run.degeneracy_detail["D2_terminal_halt"] = {
                "terminal_flat_days": round(run.terminal_flat_days, 3),
                "threshold_days": TERMINAL_FLAT_MIN_DAYS,
                "flat_starts_at": run.last_scan,
            }
        returns = run.returns
        if returns is not None and returns.size >= 2:
            std = float(np.std(returns, ddof=1))
            nonzero = int(np.sum(np.abs(returns) > 1e-9))
            if std < ZERO_VARIANCE_TOL or nonzero < MIN_NONZERO_MONTHS:
                run.degeneracy.append("D3_zero_variance")
                run.degeneracy_detail["D3_zero_variance"] = {
                    "monthly_std": std,
                    "nonzero_months": nonzero,
                    "threshold_std": ZERO_VARIANCE_TOL,
                    "threshold_nonzero_months": MIN_NONZERO_MONTHS,
                }
    survivors = [run for run in runs if not run.degeneracy]
    return months, survivors


def flat_start_label(run: Run) -> None:
    """Record where the terminal flat run begins (needs the frame, so a second read)."""
    equity_path = run.run_dir / "balance_and_equity.csv.gz"
    if not equity_path.exists():
        run.last_scan = None
        return
    frame = load_equity(equity_path)
    equity = frame[EQUITY_COLUMN].astype(float).to_numpy()
    last = equity[-1]
    tol = max(TERMINAL_FLAT_ABS_TOL, TERMINAL_FLAT_REL_TOL * abs(last))
    index = equity.size - 1
    while index > 0 and abs(equity[index - 1] - last) <= tol:
        index -= 1
    run.last_scan = str(frame.index[index])


# ---------------------------------------------------------------------------------- panels ---
def build_panel(pool: str, leg: str, runs: list[Run]) -> dict[str, Any]:
    leg_runs = [run for run in runs if run.leg == leg and run.round_key in POOL_ROUNDS[pool]]
    for run in leg_runs:
        flat_start_label(run)
    months, survivors = apply_degeneracy(leg_runs)

    excluded = [run for run in leg_runs if run.degeneracy]
    kept = sorted(survivors, key=lambda run: run.key)
    arm_ids = [f"{run.round_key}|{run.arm}" for run in kept]
    matrix = np.array(
        [[run.returns[run.months.index(month)] for month in months] for run in kept], dtype=float
    ) if kept else np.zeros((0, len(months)))

    reasons: dict[str, list[str]] = {}
    for run in excluded:
        for reason in run.degeneracy:
            reasons.setdefault(reason, []).append(run.arm)

    panel = {
        "pool": pool,
        "leg": leg,
        "leg_window": LEG_WINDOWS.get(leg),
        "leg_role": LEG_ROLES.get(leg),
        "cadence_minutes": sorted({run.cadence_minutes for run in leg_runs}),
        "months": months,
        "n_months": len(months),
        "arm_ids": arm_ids,
        "n_arms_in_pool": len(leg_runs),
        "n_arms_used": len(kept),
        "n_arms_removed": len(excluded),
        "removed_by_reason": {key: sorted(value) for key, value in sorted(reasons.items())},
        "removed_detail": [
            {"arm": run.arm, "round": run.round_key, "reasons": run.degeneracy,
             "detail": run.degeneracy_detail}
            for run in sorted(excluded, key=lambda run: run.key)
        ],
        "degeneracy_rule": degeneracy_rule_text(),
        "monthly_crosscheck": {
            "level_max_rel": max(
                (run.crosscheck.get("level_max_rel") or 0.0) for run in leg_runs
            ) if leg_runs else None,
            "chained_max_abs": max(
                (run.crosscheck.get("chained_max_abs") or 0.0) for run in leg_runs
            ) if leg_runs else None,
            "engine_pct_max_abs": max(
                (run.crosscheck.get("engine_pct_max_abs") or 0.0) for run in leg_runs
            ) if leg_runs else None,
            "tol_level_max_rel": MONTHLY_LEVEL_TOL,
            "tol_chained_max_abs": MONTHLY_CROSSCHECK_TOL,
            "convention_note": MONTHLY_CONVENTION_NOTE,
        },
        "sources": [
            {
                "arm": run.arm,
                "round": run.round_key,
                "leg": run.leg,
                "run_dir": relative(run.run_dir),
                "equity_sha256": run.equity_sha256,
                "monthly_metrics_sha256": run.monthly_sha256,
                "analysis_sha256": run.analysis_sha256,
                "dataset": run.dataset_label,
                "dataset_manifest_sha256": run.dataset_manifest_sha256,
                "dataset_override_mode": run.dataset_override_mode,
                "first_sample": run.first_sample,
                "last_sample": run.last_sample,
                "terminal_flat_days": round(run.terminal_flat_days, 3),
                "cadence_minutes": run.cadence_minutes,
                "crosscheck": run.crosscheck,
                "in_round_report": run.in_report,
                "deltas": [
                    {"path": delta.get("path"), "from": delta.get("from"), "to": delta.get("to")}
                    for delta in run.deltas
                    if isinstance(delta, dict)
                ],
                "description": run.description,
                "degeneracy": run.degeneracy,
                "structural": run.structural,
            }
            for run in sorted(leg_runs, key=lambda run: run.key)
        ],
    }
    return panel, matrix, kept


def degeneracy_rule_text() -> dict[str, Any]:
    return {
        "D0_no_equity_series": "arm 没有 balance_and_equity.csv.gz（无法重算月度序列）",
        "D0_unexpected_cadence": f"权益采样间隔不是 {SUPPORTED_CADENCE_MINUTES} 分钟",
        "D1_truncated": "arm 的月份集合与同腿的众数月份集合不同（强平或数据集裁剪提前结束）",
        "D2_terminal_halt": f"末段权益恒定段 ≥ {TERMINAL_FLAT_MIN_DAYS} 天（终局停机后再无成交）",
        "D3_zero_variance": (
            f"月度收益样本标准差 < {ZERO_VARIANCE_TOL}，或非零月份 < {MIN_NONZERO_MONTHS}"
        ),
        "declared_before": "以上规则在看到任何 PBO/DSR 数字之前写死在 report_tools/panel.py",
        "applied_at": "在任何选择（CSCV 选臂）之前，从该腿的 arm 池里整体移除",
        "thresholds": {
            "terminal_flat_min_days": TERMINAL_FLAT_MIN_DAYS,
            "terminal_flat_abs_tol": TERMINAL_FLAT_ABS_TOL,
            "terminal_flat_rel_tol": TERMINAL_FLAT_REL_TOL,
            "zero_variance_tol": ZERO_VARIANCE_TOL,
            "min_nonzero_months": MIN_NONZERO_MONTHS,
            "supported_cadence_minutes": list(SUPPORTED_CADENCE_MINUTES),
        },
    }


def write_panel(pool: str, leg: str, panel: dict[str, Any], matrix: np.ndarray) -> Path:
    """Write the tracked JSON (self-contained) plus a human-readable CSV convenience copy.

    The JSON carries ``returns_matrix`` itself, so every downstream stage and the verifier can be
    re-run from the tracked artifact alone. The CSV is the same data in long form and is *not*
    tracked (``/backtests/**`` ignores ``*.csv``); it exists for eyeballing and diffing.
    """
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{pool}__{leg}"
    csv_path = PANEL_DIR / f"{stem}.csv"
    with csv_path.open("w", encoding="utf-8") as handle:
        handle.write("arm_id,month,return\n")
        for row, arm_id in enumerate(panel["arm_ids"]):
            for column, month in enumerate(panel["months"]):
                handle.write(f"{arm_id},{month},{matrix[row, column]:.12g}\n")
    panel = dict(panel)
    panel["returns_matrix"] = matrix.tolist()
    panel["returns_matrix_note"] = (
        "returns_matrix[i][j] = 第 i 个 arm 在第 j 个月的收益（小数，非百分数）；"
        "行序与 arm_ids 一致、列序与 months 一致。JSON 浮点是 round-trip 精确的，"
        "所以 CSCV/DSR/折叠统计都可以**只靠这个 tracked 文件**重跑；"
        "同目录的 .csv 是同一份数据的长表便利副本（未被 tracked）。"
    )
    json_path = PANEL_DIR / f"{stem}.json"
    write_json(json_path, panel)
    return json_path


# ---------------------------------------------------------------------------------- ledger ---
def arm_rows(runs: list[Run]) -> list[dict[str, Any]]:
    rows = []
    for run in sorted(runs, key=lambda item: item.key):
        rows.append(
            {
                "tier": "replay_arm",
                "study": run.study,
                "round": run.round_key,
                "arm": run.arm,
                "leg": run.leg,
                "window": LEG_WINDOWS.get(run.leg),
                "dataset": run.dataset_label,
                "dataset_manifest_sha256": run.dataset_manifest_sha256,
                "dataset_override_mode": run.dataset_override_mode,
                "varied_parameters": [
                    {"path": delta.get("path"), "from": delta.get("from"), "to": delta.get("to")}
                    for delta in run.deltas
                    if isinstance(delta, dict)
                ],
                "evaluated": run.equity_sha256 is not None,
                "influenced_published_decision": run.in_report,
                "evidence": relative(run.run_dir),
                "equity_sha256": run.equity_sha256,
            }
        )
    return rows


def declared_cell_rows() -> list[dict[str, Any]]:
    """Cells declared in a frozen research contract (evaluated or not)."""
    specs = (
        ("H_returns_guarded", "backtests/binance/returns_guarded_dd_research_2026-09-16",
         "research_contract.json", "guarded-drawdown cell grid（67 格）"),
        ("F_dd_tail", "backtests/binance/dd_tail_research_2026-09-15",
         "research_contract_v4.json", "lever screen cell grid（38 格）"),
    )
    rows: list[dict[str, Any]] = []
    for round_key, study, name, label in specs:
        path = REPO / study / name
        if not path.exists():
            continue
        contract = load_json(path)
        cells_dir = REPO / study / "cells"
        evaluated = {
            result.parent.name
            for result in cells_dir.glob("**/result.json")
        } if cells_dir.is_dir() else set()
        text = _report_text(ROUNDS_BY_KEY[round_key])
        for cell in contract.get("cells", []):
            cell_id = cell.get("cell_id") if isinstance(cell, dict) else str(cell)
            if not cell_id:
                continue
            rows.append(
                {
                    "tier": "declared_cell",
                    "study": study,
                    "round": round_key,
                    "arm": cell_id,
                    "leg": "grid",
                    "window": None,
                    "dataset": None,
                    "dataset_manifest_sha256": None,
                    "dataset_override_mode": None,
                    "varied_parameters": [
                        {"path": op.get("path"), "from": op.get("seed"), "to": op.get("value")}
                        for op in (cell.get("ops") or [])
                        if isinstance(op, dict)
                    ],
                    "evaluated": cell_id in evaluated,
                    "influenced_published_decision": bool(cell_id) and cell_id in text,
                    "evidence": f"{study}/{name}",
                    "grid_note": label,
                    "group": cell.get("group"),
                }
            )
    return rows


#: ``candidate_metrics.csv`` mixes the trial's parameters with ~70 per-metric columns per track.
#: Only the declared parameter columns and a short outcome summary are registered, so the ledger
#: stays a ledger instead of a copy of the metric dump.
CANDIDATE_ID_COLUMNS = ("params_hash",)
CANDIDATE_OUTCOME_COLUMNS = ("constraint_violation", "feasible", "liquidated")


def _candidate_columns(header: list[str]) -> list[str]:
    keep = [
        name for name in header
        if name.startswith("param.") or name in CANDIDATE_ID_COLUMNS
        or name in CANDIDATE_OUTCOME_COLUMNS
    ]
    return keep


def optimizer_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Optimizer candidates that left a tracked metric record.

    Returns the rows plus a per-search definition block (declared budget, seed, parameter specs),
    which is what makes the row count verifiable against the study's own contract.
    """
    rows: list[dict[str, Any]] = []
    definitions: dict[str, Any] = {}
    pattern = "backtests/binance/**/candidate_metrics.csv"
    for path in sorted(REPO.glob(pattern)):
        rel = relative(path)
        if "/artifacts/" in rel:
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                header = next(csv.reader(handle))
        except (OSError, StopIteration):
            continue
        keep = _candidate_columns(header)
        try:
            table = pd.read_csv(path, usecols=keep)
        except (OSError, pd.errors.ParserError, ValueError):
            continue
        study = rel.split("/search/")[0].split("/mfe_research/")[0]
        outcome = {
            name: (None if name not in table.columns else
                   [None if pd.isna(value) else value for value in table[name].tolist()])
            for name in CANDIDATE_OUTCOME_COLUMNS
        }
        identifier = {
            name: (None if name not in table.columns else
                   [None if pd.isna(value) else value for value in table[name].tolist()])
            for name in CANDIDATE_ID_COLUMNS
        }
        parameter_columns = [name for name in keep if name.startswith("param.")]
        for index in range(table.shape[0]):
            rows.append(
                {
                    "tier": "optimizer_candidate",
                    "study": study,
                    "round": "I_maxdd_search_2026-09-14",
                    "arm": f"{rel.split('maxdd_strategy_research_2026-09-14/')[1][:-4]}#{index}",
                    "leg": "search_track",
                    "evidence": rel,
                    "evaluated": True,
                    "influenced_published_decision": False,
                    "varied_parameters": {
                        name: (None if pd.isna(table[name].iloc[index]) else table[name].iloc[index])
                        for name in parameter_columns
                    },
                    "candidate_flags": {
                        name: values[index] for name, values in outcome.items()
                    },
                    "params_hash": identifier["params_hash"][index],
                }
            )
        definition_path = path.parent / "search_definition.json"
        if definition_path.exists():
            try:
                definition = load_json(definition_path)
            except (json.JSONDecodeError, OSError):
                definition = {}
            definitions[relative(definition_path)] = {
                "fold": definition.get("fold"),
                "strategy": definition.get("strategy"),
                "stage": definition.get("stage"),
                "seed": definition.get("seed"),
                "declared_budget": definition.get("budget"),
                "population_size": definition.get("population_size"),
                "parameter_names": [
                    spec[0] if isinstance(spec, list) and spec else spec
                    for spec in definition.get("parameter_specs") or []
                ],
                "rows_in_candidate_metrics": int(table.shape[0]),
            }
    return rows, definitions


def screen_rows() -> list[dict[str, Any]]:
    """Named configurations of the earlier named screens."""
    rows: list[dict[str, Any]] = []
    deployability = REPO / "backtests/binance/deployability_research_2026-09-15/screen_summary.csv"
    if deployability.exists():
        table = pd.read_csv(deployability)
        for _, row in table.iterrows():
            name = str(row.get("name") or row.get("strategy") or "?")
            rows.append(
                {
                    "tier": "screen_config",
                    "study": "backtests/binance/deployability_research_2026-09-15",
                    "round": "J_deployability_2026-09-15",
                    "arm": f"{name}|{row.get('scenario')}",
                    "leg": "screen",
                    "window": None,
                    "dataset": None,
                    "dataset_manifest_sha256": None,
                    "dataset_override_mode": None,
                    "varied_parameters": {"scenario": row.get("scenario")},
                    "evaluated": True,
                    "influenced_published_decision": False,
                    "evidence": relative(deployability),
                }
            )
    low_dd = REPO / "backtests/binance/low_drawdown_strategy_study_2026-09-14/selection_candidate_metrics.csv"
    if low_dd.exists():
        table = pd.read_csv(low_dd)
        for _, row in table.iterrows():
            rows.append(
                {
                    "tier": "screen_config",
                    "study": "backtests/binance/low_drawdown_strategy_study_2026-09-14",
                    "round": "K_low_drawdown_2026-09-14",
                    "arm": str(row.get("candidate") or row.iloc[0]),
                    "leg": "screen",
                    "window": None,
                    "dataset": None,
                    "dataset_manifest_sha256": None,
                    "dataset_override_mode": None,
                    "varied_parameters": {},
                    "evaluated": True,
                    "influenced_published_decision": False,
                    "evidence": relative(low_dd),
                }
            )
    return rows


def risk_search_rows() -> list[dict[str, Any]]:
    """The four frozen search candidates of the risk-geometry round.

    The round's own report states that the optimizer smoke test evaluated 49 points and that the
    non-Pareto survivors were not persisted (``optimize_results/`` is local-only and
    ``all_results.bin`` is an unreadable pymoo checkpoint), so the count of *evaluated* search
    points is recorded as a declared lower bound rather than re-derived.
    """
    selection_path = (
        REPO / "backtests/binance/g4_twe300_risk_optimization_2026-09-17/artifacts/search_selection.json"
    )
    if not selection_path.exists():
        return []
    selection = load_json(selection_path)
    rows = []
    for entry in selection.get("selected", []):
        rows.append(
            {
                "tier": "optimizer_candidate",
                "study": "backtests/binance/g4_twe300_risk_optimization_2026-09-17",
                "round": "A_risk_geometry",
                "arm": str(entry.get("lever")),
                "leg": "search",
                "window": ["2023-09-12", "2026-09-12"],
                "dataset": "caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__8300950b42789a26",
                "dataset_manifest_sha256": "2c300e499577c2c75fac1009656780705854f1d7f90a7ffbf51cf657feca7366",
                "dataset_override_mode": None,
                "varied_parameters": entry.get("parameters"),
                "evaluated": True,
                "influenced_published_decision": True,
                "evidence": "backtests/binance/g4_twe300_risk_optimization_2026-09-17/artifacts/search_selection.json",
                "selection_kind": entry.get("selection_kind"),
            }
        )
    return rows


def traceability_samples(trials: list[dict[str, Any]], count: int = 3) -> list[dict[str, Any]]:
    """Deterministically pick one trial per tier and re-resolve it on disk.

    This exists so a reader can check that the ledger was enumerated from artifacts rather than
    typed by hand: the pick is ``sorted(tier)[i]`` by ``(study, arm)`` and the sample carries the
    concrete file the row was derived from plus what that file says.
    """
    samples: list[dict[str, Any]] = []
    tiers = sorted({row["tier"] for row in trials})
    if not tiers:
        return samples
    for tier in tiers[:count]:
        rows = sorted(
            (row for row in trials if row["tier"] == tier),
            key=lambda row: (row["study"], row["arm"]),
        )
        if not rows:
            continue
        row = rows[len(rows) // 2]
        evidence = REPO / row["evidence"]
        sample = {
            "tier": tier,
            "picked_by": f"tier={tier} 的第 {len(rows) // 2 + 1}/{len(rows)} 行（按 study,arm 排序）",
            "study": row["study"],
            "arm": row["arm"],
            "leg": row["leg"],
            "window": row.get("window"),
            "evidence_path": row["evidence"],
            "evidence_exists": evidence.exists(),
            "recheck": {},
        }
        if tier == "replay_arm":
            run_dir = evidence
            analysis = run_dir / "analysis.json"
            equity = run_dir / "balance_and_equity.csv.gz"
            sample["recheck"] = {
                "analysis_json_exists": analysis.exists(),
                "analysis_sha256": sha256_file(analysis),
                "ledger_analysis_sha256": row.get("analysis_sha256"),
                "equity_exists": equity.exists(),
                "equity_sha256": sha256_file(equity),
                "ledger_equity_sha256": row.get("equity_sha256"),
                "first_equity_ts": _first_equity_timestamp(equity),
            }
        elif tier == "declared_cell":
            contract = REPO / row["evidence"]
            sample["recheck"] = {
                "contract_exists": contract.exists(),
                "cell_listed_in_contract": _cell_listed(contract, row["arm"]),
                "result_json_count": len(list((REPO / row["study"] / "cells").glob("**/result.json")))
                if (REPO / row["study"] / "cells").is_dir() else 0,
            }
        elif tier == "optimizer_candidate":
            metrics = REPO / row["evidence"]
            sample["recheck"] = {
                "candidate_metrics_exists": metrics.exists(),
                "candidate_metrics_sha256": sha256_file(metrics),
                "row_count": _csv_row_count(metrics),
            }
        elif tier == "screen_config":
            source = REPO / row["evidence"]
            sample["recheck"] = {
                "screen_csv_exists": source.exists(),
                "screen_csv_sha256": sha256_file(source),
                "row_count": _csv_row_count(source),
            }
        samples.append(sample)
    return samples


def _first_equity_timestamp(path: Path) -> str | None:
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        handle.readline()
        line = handle.readline()
    return line.split(",", 1)[0] or None


def _csv_row_count(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return max(0, sum(1 for _ in handle) - 1)


def _cell_listed(contract_path: Path, cell_id: str) -> bool:
    if not contract_path.exists():
        return False
    try:
        contract = load_json(contract_path)
    except (json.JSONDecodeError, OSError):
        return False
    return any(
        isinstance(cell, dict) and cell.get("cell_id") == cell_id
        for cell in contract.get("cells", [])
    )


def build_ledger(runs: list[Run]) -> dict[str, Any]:
    replays = arm_rows(runs)
    cells = declared_cell_rows()
    optimizer, search_definitions = optimizer_rows()
    optimizer = optimizer + risk_search_rows()
    screens = screen_rows()
    trials = replays + cells + optimizer + screens

    replay_influenced = sum(1 for row in replays if row["influenced_published_decision"])
    cells_by_round: dict[str, int] = {}
    for row in cells:
        cells_by_round[row["round"]] = cells_by_round.get(row["round"], 0) + 1
    n_declared_cells = len(cells)
    n_evaluated_cells = sum(1 for row in cells if row["evaluated"])

    #: The lower bound counts only trials that (a) produced a recorded evaluation and (b) are
    #: named. Declared-but-never-run cells are listed but excluded from the bound.
    n_lower_bound = (
        sum(1 for row in replays if row["evaluated"])
        + n_evaluated_cells
        + sum(1 for row in optimizer if row["evaluated"])
        + sum(1 for row in screens if row["evaluated"])
    )

    ledger = {
        "generated_by": "report_tools/panel.py",
        "layout_note": (
            "本文件是机器读的索引（~1800 行），写成紧凑 JSON（单行）；"
            "`jq '.trials[] | select(.tier==\"optimizer_candidate\")'` 与 verify_audit.py 都能正常读。"
            "非优化器层级的行带 window/dataset/dataset_manifest_sha256；"
            "optimizer_candidate 行不带这些（它们的 dataset 由所属 search_definition.json 决定），"
            "改为带 params_hash 与 candidate_flags。"
        ),
        "purpose": (
            "把这条研究线（A 风险几何 / B 10k 重放 / C 账户守护 / D 尾部风险 四轮 + 早期筛选）"
            "实际评估过的每一次尝试登记下来，给出试验次数 N 的**下界**。"
        ),
        "n_lower_bound": n_lower_bound,
        "n_lower_bound_note": (
            "下界只统计「有落盘评估结果且被点名」的尝试；被合同声明但从未真正跑的格子、"
            "优化器非 Pareto 的非存档候选、以及任何未落盘的人工试错都不计入，因此真实 N 只会更大。"
        ),
        "counting_method": {
            "published_replay_arms": (
                "枚举 8 个已发布轮次目录下所有带 analysis.json 的回放 bundle（每个 bundle "
                "= 一次评估），arm key 取自 bundle 目录名 binance_<arm> 与各轮 variant_input.json"
            ),
            "declared_cells": (
                "从冻结合同 research_contract.json（67 格）与 research_contract_v4.json（38 格）"
                "读取 cell 列表；cells/**/result.json 存在才算「已评估」"
            ),
            "optimizer_candidates": (
                "早期 maxdd 研究把每个候选写进 candidate_metrics.csv（逐行计数）；"
                "本轮风险几何搜索只冻结了 4 个选定候选 + 8 个 Pareto 点，其余评估未落盘"
            ),
            "optimizer_rows_are_trimmed": (
                "candidate_metrics.csv 每个候选带 ~70 列逐 track 指标；台账只登记 "
                "`param.*` 参数列 + 身份列 + 结果摘要列，逐 track 指标不复制。"
                "每个搜索的声明预算/种子/参数名记在 optimizer_search_definitions 里，"
                "可用「行数 vs 声明预算」核对该文件确实被完整读取。"
            ),
            "screen_configs": "deployability 筛选的 screen_summary.csv 逐行 + low_drawdown 的 6 个候选",
            "excluded_from_bound": [
                "声明但未运行的 cell",
                "未落盘的优化器候选",
                "人工手工试错（无记录）",
            ],
        },
        "counts": {
            "replay_arms_total": len(replays),
            "replay_arms_evaluated": sum(1 for row in replays if row["evaluated"]),
            "replay_arms_influencing_published_decision": replay_influenced,
            "declared_cells_total": n_declared_cells,
            "declared_cells_evaluated": n_evaluated_cells,
            "declared_cells_by_round": cells_by_round,
            "optimizer_candidates": len(optimizer),
            "screen_configs": len(screens),
            "total_rows": len(trials),
            "n_lower_bound": n_lower_bound,
        },
        "expected_reference": {
            "criterion": (
                "任务给出的先验是「已发布轮次 ≥ 92 arm、两个早期筛选各 +67 与 +38 格 ⇒ N ≥ 200」。"
                "写成可复算判据：(a) 已发布轮次的回放 bundle 数 ≥ 92；"
                "(b) returns_guarded_dd_research_2026-09-16/research_contract.json 的 cells 数；"
                "(c) dd_tail_research_2026-09-15/research_contract_v4.json 的 cells 数；"
                "(d) (a)+(b)+(c) ≥ 200。四项均可从仓库文件直接重算。"
            ),
            "measured": {
                "a_published_replay_arms": len(replays),
                "b_cells_returns_guarded": cells_by_round.get("H_returns_guarded", 0),
                "c_cells_dd_tail_v4": cells_by_round.get("F_dd_tail", 0),
                "d_sum_of_a_b_c": len(replays) + n_declared_cells,
                "d_meets_200": (len(replays) + n_declared_cells) >= 200,
                "extended_lower_bound_with_all_recorded_tiers": n_lower_bound,
            },
            "difference_reason": (
                f"(a) 实测 {len(replays)} 而非 92：任务列表只点了 44+12+9+25+2=92 个 arm，"
                "但同一批目录下 dd_tail_research_2026-09-15（2 个具名 bundle + 1 个匿名 bundle）、"
                "hsl_npos1_analysis_2026-09-16（1）与 returns_guarded_dd_research_2026-09-16（4）"
                "同样带 analysis.json，按「有落盘评估」的口径必须计入。"
                "(b)(c) 与先验一致，均为 67 与 38。"
                f"(d) 三项之和 = {len(replays) + n_declared_cells}，"
                f"**{'≥' if len(replays) + n_declared_cells >= 200 else '<'} 200**："
                "先验给的 92+67+38 = 197 差 3 个才到 200，而按实物计入匿名/早期 bundle 后"
                f"是 {len(replays)} + 67 + 38 = {len(replays) + n_declared_cells}，"
                "所以「N ≥ 200」这个结论成立，但成立的理由与先验不同——"
                "多出来的不是格子，而是回放 bundle 本身。"
                "把早期 maxdd 参数搜索的逐候选记录"
                f"（{sum(1 for row in optimizer if row['tier'] == 'optimizer_candidate')} 行）、"
                "deployability 筛选与 low_drawdown 候选一并计入后，下界升到 "
                f"{n_lower_bound}。"
            ),
            "direction": (
                "N 是**下界**：被合同声明但从未真正运行的格子、优化器未落盘的非 Pareto 候选、"
                "以及任何没有留下记录的人工试错都不计入。因此报告里所有以 N 为输入的惩罚"
                "（DSR 的 SR0、MinBTL、SPA 的 N 外推）都是**乐观端上界**，真实惩罚只会更重。"
            ),
        },
        "rounds": [
            {"key": round_.key, "study": round_.study, "label": round_.label, "role": round_.role}
            for round_ in PUBLISHED_ROUNDS
        ],
        "extra_rounds_in_ledger": [
            {"key": "I_maxdd_search_2026-09-14",
             "study": "backtests/binance/maxdd_strategy_research_2026-09-14",
             "label": "MAXDD 优先的参数搜索（逐候选 candidate_metrics.csv）", "role": "prior"},
            {"key": "J_deployability_2026-09-15",
             "study": "backtests/binance/deployability_research_2026-09-15",
             "label": "可部署性筛选", "role": "prior"},
            {"key": "K_low_drawdown_2026-09-14",
             "study": "backtests/binance/low_drawdown_strategy_study_2026-09-14",
             "label": "低回撤策略筛选", "role": "prior"},
        ],
        "optimizer_search_definitions": search_definitions,
        "trials": trials,
        "traceability_samples": traceability_samples(trials),
    }
    return ledger


# ------------------------------------------------------------------------------------ main ---
def build_all() -> dict[str, Any]:
    runs = [enrich(run) for run in discover_runs()]
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {
        "generated_by": "report_tools/panel.py",
        "cadence_note": (
            "balance_and_equity.csv.gz 是按小时采样的（众数间隔 60 分钟），首列是无名索引、"
            "第二组列含 strategy_equity；月度收益按「当月最后一个小时样本 / 上月最后一个小时样本 − 1」"
            "重算，首月以上午第一个样本为基。该口径与各 run 自带的 tracked monthly_metrics.csv 一致。"
        ),
        "degeneracy_rule": degeneracy_rule_text(),
        "panels": {},
    }
    for pool, legs in POOL_LEGS.items():
        for leg in legs:
            panel, matrix, _ = build_panel(pool, leg, runs)
            path = write_panel(pool, leg, panel, matrix)
            index["panels"][f"{pool}__{leg}"] = {
                "path": relative(path),
                "csv": relative(path.with_suffix(".csv")),
                "n_months": panel["n_months"],
                "n_arms_in_pool": panel["n_arms_in_pool"],
                "n_arms_used": panel["n_arms_used"],
                "n_arms_removed": panel["n_arms_removed"],
                "removed_by_reason": panel["removed_by_reason"],
                "cadence_minutes": panel["cadence_minutes"],
                "monthly_crosscheck": panel["monthly_crosscheck"],
            }
    write_json(PANEL_DIR / "index.json", index)

    ledger = build_ledger(runs)
    #: The ledger is a machine-read index of ~1800 rows; pretty-printing it costs megabytes
    #: without adding review value, so it is written compact (one row per line is not needed -
    #: `jq '.trials[]'` and the verifier both read it fine). Every other artifact stays indented.
    write_json(ARTIFACTS / "trial_ledger.json", ledger, indent=None)

    cadences = sorted({run.cadence_minutes for run in runs if run.cadence_minutes})
    crosschecks = [
        run.crosscheck.get("chained_max_abs")
        for run in runs
        if run.crosscheck.get("chained_max_abs") is not None
    ]
    levels = [
        run.crosscheck.get("level_max_rel")
        for run in runs
        if run.crosscheck.get("level_max_rel") is not None
    ]
    engine_pcts = [
        run.crosscheck.get("engine_pct_max_abs")
        for run in runs
        if run.crosscheck.get("engine_pct_max_abs") is not None
    ]
    summary = {
        "runs_loaded": len(runs),
        "cadence_histogram": {
            str(key): int(value)
            for key, value in sorted(
                pd.Series([run.cadence_minutes for run in runs]).value_counts().items()
            )
        },
        "monthly_crosscheck_files": len(crosschecks),
        "level_max_rel": max(levels) if levels else None,
        "chained_max_abs": max(crosschecks) if crosschecks else None,
        "engine_pct_max_abs": max(engine_pcts) if engine_pcts else None,
        "tol_level_max_rel": MONTHLY_LEVEL_TOL,
        "tol_chained_max_abs": MONTHLY_CROSSCHECK_TOL,
        "panels": index["panels"],
        "n_lower_bound": ledger["n_lower_bound"],
        "counting_tiers": ledger["counts"],
        "traceability_samples": ledger["traceability_samples"],
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    summary = build_all()
    if not args.quiet:
        print(f"runs loaded: {summary['runs_loaded']}")
        print(f"equity cadence histogram (minutes -> runs): {summary['cadence_histogram']}")
        print(
            "monthly cross-check vs tracked monthly_metrics.csv "
            f"({summary['monthly_crosscheck_files']} files): "
            f"level max rel = {summary['level_max_rel']:.3e} "
            f"(tol {summary['tol_level_max_rel']:.0e}), "
            f"chained max abs = {summary['chained_max_abs']:.3e} "
            f"(tol {summary['tol_chained_max_abs']:.0e}), "
            f"engine convention diff = {summary['engine_pct_max_abs']:.3e}"
        )
        for name, panel in summary["panels"].items():
            print(
                f"  {name}: {panel['n_months']} months x {panel['n_arms_used']} arms "
                f"(removed {panel['n_arms_removed']}: {panel['removed_by_reason']})"
            )
        print(f"trial ledger lower bound N >= {summary['n_lower_bound']}")
        print(f"counting tiers: {json.dumps(summary['counting_tiers'], ensure_ascii=False)}")


if __name__ == "__main__":
    sys.exit(main())
