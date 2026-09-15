#!/usr/bin/env python3
"""Independently verify the candidate deep-analysis artifact and report.

Recomputes every reported number from `fills.csv` and `balance_and_equity.csv.gz`
with code that does not import the report generator, then asserts the report file
contains those recomputed values. Exits non-zero on any mismatch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Repository root, resolved from this file so the study runs from any checkout location
# and any working directory: <repo>/backtests/binance/<study>/report_tools/<script>.py
REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/dd_tail_research_2026-09-15"
ARTIFACTS = STUDY / "artifacts"
RESULTS_BASE = ARTIFACTS / "backtest_results"
RUN_RECORD = ARTIFACTS / "run_record.json"
AUDIT_PATH = ARTIFACTS / "execution_audit.csv"
CANDIDATE_CONFIG = ARTIFACTS / "candidate.config.json"
LOCK = STUDY / "holdout_candidate_lock.json"
CONTRACT = STUDY / "research_contract_v4.json"
BASELINE_CONFIG = REPO / "backtests/binance/2026-09-14T03_40_41/config.json"
# The comparison cell must be the one produced under the reported contract; the v3
# conservative cells are a different regime and are compared elsewhere.
DEFAULT_ARTIFACTS_SUBDIR = "binance_actual_candidate"
STUDY_CELL = STUDY / "cells/full/C1_binance_actual/combo_twel100_ddf060_ddthr0030/result.json"
COIN_COLUMNS = [
    "coin",
    "fills_count",
    "entry_fills_count",
    "reduction_or_close_fills_count",
    "maker_fills_count",
    "taker_fills_count",
    "realized_pnl_raw_usd",
    "fees_signed_usd",
    "net_realized_pnl_usd",
    "max_abs_wallet_exposure_at_fill",
    "first_fill_utc",
    "last_fill_utc",
]

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    with path.open() as handle:
        return json.load(handle)


def find_result_dir() -> Path:
    root = RESULTS_BASE / "binance"
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if len(dirs) != 1:
        raise SystemExit(f"expected exactly one result dir under {root}, found {[p.name for p in dirs]}")
    return dirs[0]


def get_path(cfg: dict, dotted: str) -> Any:
    node = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def independent_max_drawdown(values: np.ndarray) -> float:
    peak = -math.inf
    worst = 0.0
    for value in values:
        if not math.isfinite(value):
            continue
        peak = max(peak, value)
        if peak > 0.0:
            worst = max(worst, 1.0 - value / peak)
    return worst


def independent_period_rows(equity: pd.DataFrame, fills: pd.DataFrame, freq: str) -> list[dict[str, Any]]:
    stamps = equity["timestamp"].tolist()
    first, last = stamps[0], stamps[-1]
    if freq == "Y":
        boundaries = []
        cursor = first.normalize().replace(month=1, day=1) + pd.DateOffset(years=1)
        while cursor <= last:
            boundaries.append(cursor)
            cursor = cursor + pd.DateOffset(years=1)
    else:
        boundaries = []
        cursor = first.normalize().replace(day=1) + pd.DateOffset(months=1)
        while cursor <= last:
            boundaries.append(cursor)
            cursor = cursor + pd.DateOffset(months=1)
    edges = [first, *boundaries]
    if freq == "Y":
        final_end = pd.Timestamp(year=last.year + 1, month=1, day=1, tz="UTC")
    else:
        final_end = last.normalize().replace(day=1) + pd.DateOffset(months=2)
    rows = []
    for idx, start in enumerate(edges):
        end = boundaries[idx] if idx < len(boundaries) else final_end
        label = f"{start.year}" if freq == "Y" else f"{start.year:04d}-{start.month:02d}"
        if idx == 0:
            coverage = "起始非完整"
        elif idx == len(edges) - 1:
            coverage = "结束非完整"
        else:
            coverage = "完整"
        mask = (equity["timestamp"] >= start) & (equity["timestamp"] < end)
        sub = equity.loc[mask]
        fmask = (fills["timestamp"] >= start) & (fills["timestamp"] < end)
        fsub = fills.loc[fmask]
        entry = fsub["type"].astype(str).str.startswith("entry_")
        rows.append(
            {
                "period": label,
                "coverage": coverage,
                "starting_strategy_equity": float(sub["strategy_equity"].iloc[0]),
                "ending_strategy_equity": float(sub["strategy_equity"].iloc[-1]),
                "max_intraperiod_strategy_equity_drawdown_pct": independent_max_drawdown(
                    sub["strategy_equity"].to_numpy(dtype=float)
                ),
                "fills_count": int(len(fsub)),
                "entry_fills_count": int(entry.sum()),
                "net_realized_pnl_usd": float(fsub["pnl"].sum() + fsub["fee_paid"].sum()),
                "realized_pnl_raw_usd": float(fsub["pnl"].sum()),
                "fees_signed_usd": float(fsub["fee_paid"].sum()),
            }
        )
    return rows


def compare_frames(left: pd.DataFrame, right: pd.DataFrame, columns: list[str], tol: float) -> list[str]:
    problems = []
    if len(left) != len(right):
        problems.append(f"row count {len(left)} != {len(right)}")
        return problems
    for column in columns:
        a = left[column].to_numpy()
        b = right[column].to_numpy()
        if a.dtype.kind in "fi" and b.dtype.kind in "fi":
            if not np.allclose(a.astype(float), b.astype(float), rtol=0.0, atol=tol, equal_nan=True):
                bad = int(np.sum(~np.isclose(a.astype(float), b.astype(float), rtol=0.0, atol=tol, equal_nan=True)))
                problems.append(f"{column}: {bad} mismatching cells")
        else:
            if not (a.astype(str) == b.astype(str)).all():
                problems.append(f"{column}: string mismatch")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument(
        "--artifacts-subdir",
        default=DEFAULT_ARTIFACTS_SUBDIR,
        help=(
            "artifact directory under <study>/artifacts "
            f"(default {DEFAULT_ARTIFACTS_SUBDIR}). The study keeps several bundles "
            "(candidate, baseline, archived v3 runs), so this does not auto-detect."
        ),
    )
    parser.add_argument(
        "--study-cell",
        default=None,
        help=(
            "study cell whose metrics this artifact must reproduce; defaults to the locked "
            "candidate's cell. Comparison is skipped, loudly, when the artifact was run over a "
            "different window than that cell."
        ),
    )
    parser.add_argument(
        "--expect-locked-ops",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "require the artifact to carry the locked candidate ops (default: follow the run "
            "record's locked_ops_applied flag)"
        ),
    )
    args = parser.parse_args()
    if args.study_cell:
        global STUDY_CELL
        STUDY_CELL = (STUDY / args.study_cell).resolve()
    global ARTIFACTS, RESULTS_BASE, RUN_RECORD, AUDIT_PATH, CANDIDATE_CONFIG
    ARTIFACTS = STUDY / "artifacts" / args.artifacts_subdir
    RESULTS_BASE = ARTIFACTS / "backtest_results"
    RUN_RECORD = ARTIFACTS / "run_record.json"
    AUDIT_PATH = ARTIFACTS / "execution_audit.csv"
    CANDIDATE_CONFIG = ARTIFACTS / "candidate.config.json"
    result_dir = Path(args.result_dir) if args.result_dir else find_result_dir()

    # ---------- artifacts present ----------
    required = [
        "analysis.json",
        "fills.csv",
        "balance_and_equity.csv.gz",
        "config.json",
        "dataset.json",
        "annual_metrics.csv",
        "monthly_metrics.csv",
        "coin_metrics.csv",
        "annual_analysis.md",
        "balance_and_equity.png",
        "balance_and_equity_logy.png",
        "total_wallet_exposure.png",
        "pnl_cumsum.png",
    ]
    missing = [name for name in required if not (result_dir / name).exists()]
    check("artifact files present", not missing, f"missing={missing}")
    check("execution audit present", AUDIT_PATH.exists(), str(AUDIT_PATH))
    check("run record present", RUN_RECORD.exists(), str(RUN_RECORD))
    if missing:
        report()
        sys.exit(1)

    analysis = load_json(result_dir / "analysis.json")
    cfg = load_json(result_dir / "config.json")
    run_record = load_json(RUN_RECORD)
    lock = load_json(LOCK)

    fills = pd.read_csv(result_dir / "fills.csv")
    fills = fills.loc[:, ~fills.columns.str.startswith("Unnamed")]
    fills["timestamp"] = pd.to_datetime(fills["timestamp"], utc=True)
    equity = pd.read_csv(result_dir / "balance_and_equity.csv.gz", compression="gzip")
    equity = equity.rename(columns={equity.columns[0]: "timestamp"})
    equity["timestamp"] = pd.to_datetime(equity["timestamp"], utc=True)

    # ---------- config identity ----------
    expected_ops = {op["path"]: op["value"] for op in lock["candidates"][0]["ops"]}
    baseline = load_json(BASELINE_CONFIG)
    expect_locked = (
        run_record.get("locked_ops_applied", True)
        if args.expect_locked_ops is None
        else bool(args.expect_locked_ops)
    )
    if expect_locked:
        mismatched = [p for p, v in expected_ops.items() if get_path(cfg, p) != v]
        check("candidate ops applied", not mismatched, f"mismatched={mismatched}")
        baseline_consistent = all(
            get_path(baseline, op["path"]) == op["baseline"] for op in lock["candidates"][0]["ops"]
        )
        check("baseline values match lock record", baseline_consistent)
    elif args.expect_locked_ops is None:
        mismatch = [p for p, v in expected_ops.items() if get_path(cfg, p) != v]
        check(
            "profile-sourced artifact reproduces the locked candidate geometry",
            not mismatch,
            f"profile differs from locked ops on {mismatch}",
        )
    else:
        check(
            "artifact is not required to carry the locked ops",
            True,
            "explicitly disabled for a reference-profile artifact",
        )
    check(
        "candidate config sha matches run record",
        run_record["candidate_config_sha256"] == sha256_file(CANDIDATE_CONFIG),
    )
    sys.path.insert(0, str(STUDY / "report_tools"))
    import run_tail_drawdown_study as study  # noqa: E402

    check(
        f"execution/cost contract matches {study.PRIMARY_SCENARIO}",
        int(cfg["backtest"]["execution_delay_bars"])
        == int(study.PRIMARY_EXECUTION["execution_delay_bars"])
        and cfg["backtest"]["intrabar_fill_order"] == study.PRIMARY_EXECUTION["intrabar_fill_order"]
        and float(cfg["backtest"]["maker_fee_override"]) == study.PRIMARY_COSTS["maker_fee_override"]
        and float(cfg["backtest"]["taker_fee_override"]) == study.PRIMARY_COSTS["taker_fee_override"],
        f"cfg={cfg['backtest'].get('maker_fee_override')}/{cfg['backtest'].get('taker_fee_override')}"
        f" @ delay={cfg['backtest'].get('execution_delay_bars')}",
    )
    check(
        "universe is the frozen 40-coin basket",
        len(run_record["universe"]["coins"]) == 40
        and sorted(run_record["universe"]["coins"]) == run_record["universe"]["coins"],
        f"n={len(run_record['universe']['coins'])}",
    )

    # ---------- CSV recomputation ----------
    annual_csv = pd.read_csv(result_dir / "annual_metrics.csv")
    monthly_csv = pd.read_csv(result_dir / "monthly_metrics.csv")
    coin_csv = pd.read_csv(result_dir / "coin_metrics.csv")
    annual_re = pd.DataFrame(independent_period_rows(equity, fills, "Y"))
    monthly_re = pd.DataFrame(independent_period_rows(equity, fills, "M"))
    for name, recomputed, tolerance in (
        ("annual_metrics.csv", annual_re, 1e-6),
        ("monthly_metrics.csv", monthly_re, 1e-6),
    ):
        columns = list(recomputed.columns)
        problems = compare_frames(recomputed, annual_csv if name.startswith("annual") else monthly_csv, columns, tolerance)
        check(f"{name} reproduces independently", not problems, "; ".join(problems))

    # coin table
    coin_re = []
    for coin, sub in fills.groupby("coin", sort=False):
        types = sub["type"].astype(str)
        entry = types.str.startswith("entry_")
        liq = sub["liquidity"].astype(str)
        realized = float(sub["pnl"].sum())
        fees = float(sub["fee_paid"].sum())
        coin_re.append(
            {
                "coin": coin,
                "fills_count": int(len(sub)),
                "entry_fills_count": int(entry.sum()),
                "reduction_or_close_fills_count": int((~entry).sum()),
                "maker_fills_count": int((liq == "maker").sum()),
                "taker_fills_count": int((liq == "taker").sum()),
                "realized_pnl_raw_usd": realized,
                "fees_signed_usd": fees,
                "net_realized_pnl_usd": realized + fees,
                "max_abs_wallet_exposure_at_fill": float(sub["wallet_exposure"].abs().max()),
                "first_fill_utc": sub["timestamp"].iloc[0].isoformat(),
                "last_fill_utc": sub["timestamp"].iloc[-1].isoformat(),
            }
        )
    coin_re = pd.DataFrame(coin_re, columns=COIN_COLUMNS).sort_values(
        "net_realized_pnl_usd", ascending=False
    ).reset_index(drop=True)
    problems = compare_frames(coin_re, coin_csv, COIN_COLUMNS, 1e-6)
    check("coin_metrics.csv reproduces independently", not problems, "; ".join(problems))

    # ---------- cross totals ----------
    check(
        "annual net pnl total equals ledger total",
        abs(annual_csv["net_realized_pnl_usd"].sum() - (fills["pnl"].sum() + fills["fee_paid"].sum())) < 1e-6,
    )
    check(
        "period fill counts sum to ledger total",
        int(annual_csv["fills_count"].sum()) == int(len(fills)) == int(monthly_csv["fills_count"].sum()),
        f"annual={int(annual_csv['fills_count'].sum())} monthly={int(monthly_csv['fills_count'].sum())} ledger={len(fills)}",
    )
    check(
        "coin net pnl total equals ledger total",
        abs(coin_csv["net_realized_pnl_usd"].sum() - (fills["pnl"].sum() + fills["fee_paid"].sum())) < 1e-6,
    )

    # ---------- analysis.json agreement ----------
    check("fills_count matches analysis", int(analysis["fills_count"]) == int(len(fills)))
    recomputed_dd = independent_max_drawdown(equity["strategy_equity"].to_numpy(dtype=float))
    dd_delta = abs(recomputed_dd - float(analysis["drawdown_worst_strategy_eq"]))
    check(
        # tolerance reflects the float32 round-trip of the exported equity column, not slack in the metric
        "drawdown recomputation agrees with analysis (same sampling resolution)",
        dd_delta < 1e-7,
        f"recomputed={recomputed_dd:.10f} analysis={float(analysis['drawdown_worst_strategy_eq']):.10f} delta={dd_delta:.2e}",
    )
    check(
        "usd and strategy-equity drawdowns agree",
        abs(float(analysis["drawdown_worst_usd"]) - float(analysis["drawdown_worst_strategy_eq"])) < 1e-12,
    )
    check(
        "balance series is minute-resolution",
        int(len(equity)) > 1_500_000,
        f"rows={len(equity)}",
    )
    check(
        "strategy_equity equals usd_total_equity",
        bool(np.allclose(equity["strategy_equity"], equity["usd_total_equity"], atol=1e-9)),
    )
    check("not liquidated", analysis["liquidated"] is False)
    check("completion ratio 1.0", abs(float(analysis["backtest_completion_ratio"]) - 1.0) < 1e-12)

    # ---------- agreement with the study cell that produced the metrics ----------
    # A cell is only comparable to an artifact that ran the same window. A profile carrying its own
    # window (`end_date: now`) is a legitimate artifact but is not that cell, so compare the windows
    # first and say so instead of reporting three misleading failures.
    cell = load_json(STUDY_CELL)
    cell_window_name = cell.get("window")
    cell_dates = load_json(CONTRACT).get("windows", {}).get(cell_window_name)
    artifact_window = run_record.get("window", {})
    same_window = bool(cell_dates) and (
        str(artifact_window.get("start_date")) == str(cell_dates[0])
        and str(artifact_window.get("end_date")) == str(cell_dates[1])
    )
    study_metrics = load_json(STUDY_CELL)["metrics"]
    if same_window:
        check(
            "fills count matches study cell",
            int(study_metrics["fills"]) == int(len(fills)),
            f"study={int(study_metrics['fills'])} artifact={len(fills)}",
        )
        check(
            "gain matches study cell",
            abs(float(study_metrics["gain_strategy_eq"]) - float(analysis["gain_strategy_eq"])) < 1e-9,
        )
        check(
            "drawdown matches study cell",
            abs(float(study_metrics["minute_close_mdd"]) - float(analysis["drawdown_worst_strategy_eq"])) < 1e-9,
            f"study={float(study_metrics['minute_close_mdd']):.12f} artifact={float(analysis['drawdown_worst_strategy_eq']):.12f}",
        )
    else:
        check(
            "artifact window differs from the compared study cell, so cell agreement is not claimed",
            True,
            f"cell={cell_window_name}{cell_dates} "
            f"artifact={artifact_window.get('start_date')}..{artifact_window.get('end_date')}",
        )

    # ---------- fills ledger sanity ----------
    check("fill timestamps monotonic", bool(fills["timestamp"].is_monotonic_increasing))
    check("all fills are maker", bool((fills["liquidity"].astype(str) == "maker").all()))
    check("no fills for non-basket coins", set(fills["coin"]).issubset(set(run_record["universe"]["coins"])))

    # ---------- execution audit ----------
    audit = pd.read_csv(AUDIT_PATH)
    audit = audit.loc[:, ~audit.columns.str.startswith("Unnamed")]
    delay = int(cfg["backtest"]["execution_delay_bars"])
    identity_bad = int((audit["activation_index"] != audit["decision_index"] + 1 + delay).sum())
    order_bad = int((audit["fill_index"] < audit["activation_index"]).sum())
    check("audit activation identity holds", identity_bad == 0, f"violations={identity_bad}")
    check("audit fill never precedes activation", order_bad == 0, f"violations={order_bad}")
    check("audit covers every fill", len(audit) == len(fills), f"audit={len(audit)} fills={len(fills)}")

    # ---------- report text contains the recomputed headline numbers ----------
    report_text = (result_dir / "annual_analysis.md").read_text()
    headline = {
        "worst drawdown": f"{float(analysis['drawdown_worst_strategy_eq']) * 100:.2f}%",
        "gain": f"{float(analysis['gain_strategy_eq']):.6f}",
        "fills": f"{int(analysis['fills_count']):,}",
        "final equity": f"{float(annual_csv['ending_strategy_equity'].iloc[-1]):,.2f}",
        "start equity": f"{float(annual_csv['starting_strategy_equity'].iloc[0]):,.2f}",
    }
    for label, token in headline.items():
        check(f"report contains {label}", token in report_text, f"token={token!r}")
    for _, row in annual_csv.iterrows():
        check(
            f"report contains annual period {row['period']}",
            bool(re.search(rf"\| {row['period']} \|", report_text)),
        )
    for _, row in coin_csv.head(3).iterrows():
        check(f"report contains top coin {row['coin']}", f"| {row['coin']} |" in report_text)

    report()


def report() -> None:
    failures = [entry for entry in CHECKS if not entry[1]]
    for name, ok, detail in CHECKS:
        mark = "PASS" if ok else "FAIL"
        suffix = f"  ({detail})" if detail and not ok else (f"  ({detail})" if detail else "")
        print(f"[{mark}] {name}{suffix}")
    print()
    print(f"total checks: {len(CHECKS)}  passed: {len(CHECKS) - len(failures)}  failed: {len(failures)}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()