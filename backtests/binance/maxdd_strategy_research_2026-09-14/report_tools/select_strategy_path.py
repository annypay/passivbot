#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any


STUDY_DIR = Path(__file__).resolve().parents[1]
FOLDS = ("F1", "F2", "F3")
STRATEGIES = ("ema_anchor", "trailing_martingale")
PRIMARY_TRACKS = ("E-L", "B-L")
EXPECTED_PRIMARY_ROWS = len(FOLDS) * len(PRIMARY_TRACKS)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open() as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def finite_float(row: dict[str, Any], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(f"non-finite {key} in validation row: {row}")
    return value


def row_is_eligible(row: dict[str, Any]) -> bool:
    return (
        finite_float(row, "backtest_completion_ratio") >= 0.999
        and finite_float(row, "drawdown_worst_strategy_eq") <= 0.25
        and finite_float(row, "adg_strategy_eq") >= 0.00005
        and finite_float(row, "gain_strategy_eq") > 1.0
        and bool(row.get("activity_passed"))
        and not bool(row.get("liquidated"))
    )


def load_strategy_rows(strategy: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows: list[dict[str, Any]] = []
    evidence: dict[str, str] = {}
    for fold in FOLDS:
        validation_path = (
            STUDY_DIR / "validation" / fold / f"{strategy}_validation.json"
        )
        lock_path = STUDY_DIR / "locks" / fold / f"{strategy}_candidate_lock.json"
        validation = read_json(validation_path)
        lock_sha = sha256(lock_path)
        if validation.get("candidate_lock_sha256") != lock_sha:
            raise RuntimeError(
                f"{validation_path} does not reference the current immutable candidate lock"
            )
        fold_rows = validation.get("rows")
        if not isinstance(fold_rows, list):
            raise TypeError(f"{validation_path} rows must be a list")
        primary = [
            row
            for row in fold_rows
            if isinstance(row, dict) and row.get("track") in PRIMARY_TRACKS
        ]
        if {row["track"] for row in primary} != set(PRIMARY_TRACKS):
            raise RuntimeError(
                f"{validation_path} must contain exactly the primary tracks {PRIMARY_TRACKS}"
            )
        rows.extend(primary)
        evidence[f"{fold}_candidate_lock_sha256"] = lock_sha
        evidence[f"{fold}_validation_sha256"] = sha256(validation_path)
    if len(rows) != EXPECTED_PRIMARY_ROWS:
        raise RuntimeError(
            f"{strategy} has {len(rows)} primary rows, expected {EXPECTED_PRIMARY_ROWS}"
        )
    return rows, evidence


def summarize(strategy: str) -> dict[str, Any]:
    rows, evidence = load_strategy_rows(strategy)
    gain_product = math.prod(finite_float(row, "gain_strategy_eq") for row in rows)
    summary = {
        "strategy": strategy,
        "eligible_primary_rows": sum(row_is_eligible(row) for row in rows),
        "expected_primary_rows": EXPECTED_PRIMARY_ROWS,
        "all_primary_rows_eligible": all(row_is_eligible(row) for row in rows),
        "worst_primary_oos_drawdown_worst_strategy_eq": max(
            finite_float(row, "drawdown_worst_strategy_eq") for row in rows
        ),
        "worst_primary_oos_drawdown_worst_mean_1pct_strategy_eq": max(
            finite_float(row, "drawdown_worst_mean_1pct_strategy_eq") for row in rows
        ),
        "worst_primary_oos_strategy_eq_recovery_days_max": max(
            finite_float(row, "strategy_eq_recovery_days_max") for row in rows
        ),
        "minimum_primary_oos_adg_strategy_eq": min(
            finite_float(row, "adg_strategy_eq") for row in rows
        ),
        "geometric_compounded_primary_oos_gain_strategy_eq": gain_product,
        "evidence": evidence,
    }
    summary["ranking_tuple"] = [
        -summary["eligible_primary_rows"],
        summary["worst_primary_oos_drawdown_worst_strategy_eq"],
        summary["worst_primary_oos_drawdown_worst_mean_1pct_strategy_eq"],
        summary["worst_primary_oos_strategy_eq_recovery_days_max"],
        -summary["minimum_primary_oos_adg_strategy_eq"],
        -summary["geometric_compounded_primary_oos_gain_strategy_eq"],
        strategy,
    ]
    return summary


def write_lock(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=False) + "\n"
    if path.exists():
        if path.read_text() != encoded:
            raise FileExistsError(f"refusing to overwrite non-matching path lock: {path}")
        return
    path.write_text(encoded)


def write_summary_csv(path: Path, summaries: list[dict[str, Any]]) -> None:
    fields = [
        "strategy",
        "eligible_primary_rows",
        "expected_primary_rows",
        "all_primary_rows_eligible",
        "worst_primary_oos_drawdown_worst_strategy_eq",
        "worst_primary_oos_drawdown_worst_mean_1pct_strategy_eq",
        "worst_primary_oos_strategy_eq_recovery_days_max",
        "minimum_primary_oos_adg_strategy_eq",
        "geometric_compounded_primary_oos_gain_strategy_eq",
    ]
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for summary in summaries:
            writer.writerow({field: summary[field] for field in fields})


def main() -> None:
    contract_path = STUDY_DIR / "path_selection_contract.json"
    summaries = [summarize(strategy) for strategy in STRATEGIES]
    selected = min(summaries, key=lambda item: tuple(item["ranking_tuple"]))
    decision = {
        "contract_sha256": sha256(contract_path),
        "selected_strategy": selected["strategy"],
        "parameter_only_status": (
            "passed"
            if bool(selected["all_primary_rows_eligible"])
            else "failed_research_path_only"
        ),
        "summaries": summaries,
    }
    lock_path = STUDY_DIR / "strategy_path_decision_lock.json"
    write_lock(lock_path, decision)
    write_summary_csv(STUDY_DIR / "strategy_path_oos_summary.csv", summaries)
    print(json.dumps(decision, indent=2))


if __name__ == "__main__":
    main()
