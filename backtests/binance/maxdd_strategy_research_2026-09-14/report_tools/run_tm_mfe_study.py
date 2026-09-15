#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import importlib.util
import json
import math
import multiprocessing as mp
from pathlib import Path
import sys
from typing import Any

import pandas as pd


STUDY_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = STUDY_DIR / "mfe_research"
PARAMETER_TOOL = Path(__file__).with_name("run_maxdd_parameter_study.py")
FOLDS = ("F1", "F2", "F3")
PRIMARY_TRACKS = ("E-L", "B-L")
VALIDATION_TRACKS = ("E-L", "E-S", "B-L")
STRICT_GIVEBACK_FRACTIONS = (0.0, 0.10, 0.20, 0.35, 0.50, 0.70)
FINE_THRESHOLD_MULTIPLIERS = (0.90, 1.00, 1.10)
FINE_GIVEBACK_MULTIPLIERS = (0.75, 1.00, 1.25)
FINE_QTY_MULTIPLIERS = (0.75, 1.00, 1.25)
WORKERS = 4
THRESHOLD_BOUNDS = (0.002, 0.02, 0.00005)
GIVEBACK_BOUNDS = (0.00005, 0.01, 0.00005)
QTY_BOUNDS = (0.05, 0.50, 0.001)


def load_parameter_tool():
    spec = importlib.util.spec_from_file_location("maxdd_parameter_study", PARAMETER_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load parameter study tool: {PARAMETER_TOOL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict[str, Any]:
    with path.open() as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def tm_mfe_params(
    tool,
    base: dict[str, Any],
    *,
    threshold_multiplier: float = 1.0,
    giveback_fraction: float,
    qty_multiplier: float = 1.0,
) -> dict[str, Any]:
    params = deepcopy(base)
    base_threshold = float(base["close_threshold_base_pct"])
    threshold = (
        base_threshold
        if math.isclose(threshold_multiplier, 1.0, rel_tol=0.0, abs_tol=1.0e-15)
        else tool.quantize(
            base_threshold * threshold_multiplier, *THRESHOLD_BOUNDS
        )
    )
    base_qty = float(base["close_qty_pct"])
    qty = (
        base_qty
        if math.isclose(qty_multiplier, 1.0, rel_tol=0.0, abs_tol=1.0e-15)
        else tool.quantize(
            base_qty * qty_multiplier,
            *QTY_BOUNDS,
        )
    )
    giveback = (
        0.0
        if giveback_fraction <= 0.0
        else tool.quantize(
            threshold * min(max(giveback_fraction, 0.01), 0.85),
            *GIVEBACK_BOUNDS,
        )
    )
    if giveback >= threshold:
        giveback = tool.quantize(
            threshold - GIVEBACK_BOUNDS[2], *GIVEBACK_BOUNDS
        )
    if giveback_fraction > 0.0 and giveback <= 0.0:
        giveback = GIVEBACK_BOUNDS[0]
    params["close_threshold_base_pct"] = threshold
    params["close_retracement_base_pct"] = giveback
    params["close_qty_pct"] = qty
    return params


def evaluate_candidates(tool, strategy: str, candidates: list[dict[str, Any]]):
    unique: dict[str, dict[str, Any]] = {}
    for params in candidates:
        unique.setdefault(tool.stable_hash(params), params)
    tasks = [
        (strategy, params, PRIMARY_TRACKS)
        for _params_hash, params in sorted(unique.items())
    ]
    context = mp.get_context("fork")
    with context.Pool(WORKERS, initializer=tool.pool_initializer) as pool:
        results = pool.map(tool.evaluate_task, tasks)
    for record in results:
        record["summary"] = tool.summarize_result(record, PRIMARY_TRACKS)
    return tool.deduplicate_records(results)


def write_stage(tool, fold: str, stage: str, records: list[dict[str, Any]]) -> None:
    stage_dir = OUTPUT_DIR / "search" / fold / stage
    selected = records[0]
    tool.write_json(
        stage_dir / "search_definition.json",
        {
            "fold": fold,
            "strategy": "trailing_martingale",
            "stage": stage,
            "candidate_count": len(records),
            "primary_tracks": list(PRIMARY_TRACKS),
            "strict_giveback_fractions": list(STRICT_GIVEBACK_FRACTIONS),
            "fine_threshold_multipliers": list(FINE_THRESHOLD_MULTIPLIERS),
            "fine_giveback_multipliers": list(FINE_GIVEBACK_MULTIPLIERS),
            "fine_qty_multipliers": list(FINE_QTY_MULTIPLIERS),
            "selected_params_hash": selected["params_hash"],
        },
    )
    tool.write_json(stage_dir / "selected_record.json", selected)
    pd.DataFrame(
        [
            tool.flatten_record(
                record,
                stage=stage,
                fold=fold,
                strategy="trailing_martingale",
            )
            for record in records
        ]
    ).to_csv(stage_dir / "candidate_metrics.csv", index=False)


def strict_candidates(tool, base: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        tm_mfe_params(tool, base, giveback_fraction=fraction)
        for fraction in STRICT_GIVEBACK_FRACTIONS
    ]


def fine_candidates(
    tool, base: dict[str, Any], strict_selected: dict[str, Any]
) -> list[dict[str, Any]]:
    selected_threshold = float(strict_selected["close_threshold_base_pct"])
    selected_giveback = float(strict_selected["close_retracement_base_pct"])
    if selected_giveback <= 0.0:
        return [deepcopy(base)]
    selected_fraction = selected_giveback / selected_threshold
    candidates = [deepcopy(base), deepcopy(strict_selected)]
    for threshold_multiplier in FINE_THRESHOLD_MULTIPLIERS:
        for giveback_multiplier in FINE_GIVEBACK_MULTIPLIERS:
            for qty_multiplier in FINE_QTY_MULTIPLIERS:
                candidates.append(
                    tm_mfe_params(
                        tool,
                        base,
                        threshold_multiplier=threshold_multiplier,
                        giveback_fraction=selected_fraction * giveback_multiplier,
                        qty_multiplier=qty_multiplier,
                    )
                )
    return candidates


def validation_row(
    tool,
    fold: str,
    variant: str,
    params_hash: str,
    track: str,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    activity = all(
        float(metrics[name]) >= floor
        for name, floor in tool.ACTIVITY_FLOORS[track].items()
    )
    passed = (
        float(metrics["drawdown_worst_strategy_eq"]) <= tool.MAXDD_CAP
        and float(metrics["backtest_completion_ratio"]) >= tool.COMPLETION_FLOOR
        and float(metrics["adg_strategy_eq"]) >= tool.ADG_FLOOR
        and float(metrics["gain_strategy_eq"]) > 1.0
        and not bool(metrics.get("liquidated"))
        and activity
    )
    return {
        "fold": fold,
        "strategy": "trailing_martingale",
        "variant": variant,
        "track": track,
        "params_hash": params_hash,
        "passed": passed,
        "activity_passed": activity,
        **metrics,
    }


async def train_and_lock_fold(tool, fold: str) -> dict[str, Any]:
    parameter_lock_path = (
        STUDY_DIR
        / "locks"
        / fold
        / "trailing_martingale_candidate_lock.json"
    )
    parameter_lock = read_json(parameter_lock_path)
    base = parameter_lock["selected_params"]
    if float(base["close_threshold_we_weight"]) == 0.0:
        raise RuntimeError(
            f"{fold}: retracement=0 forces a full grid close when "
            "close_threshold_we_weight=0, so close quantity is not matched"
        )
    train_start, train_end = tool.FOLDS[fold]["train"]
    state = await tool.prepare_window(
        "trailing_martingale", train_start, train_end, execution="C1"
    )
    tool.install_state(state)

    strict_records = evaluate_candidates(
        tool, "trailing_martingale", strict_candidates(tool, base)
    )
    write_stage(tool, fold, "strict_ablation", strict_records)
    strict_selected = strict_records[0]["params"]

    fine_records = evaluate_candidates(
        tool,
        "trailing_martingale",
        fine_candidates(tool, base, strict_selected),
    )
    write_stage(tool, fold, "narrow_joint_search", fine_records)
    selected = fine_records[0]
    comparator_result = tool.evaluate_task(
        ("trailing_martingale", deepcopy(base), PRIMARY_TRACKS)
    )
    comparator_result["summary"] = tool.summarize_result(
        comparator_result, PRIMARY_TRACKS
    )

    lock = {
        "fold": fold,
        "strategy": "trailing_martingale",
        "selection_opened_validation": False,
        "parameter_candidate_lock_sha256": tool.sha256(parameter_lock_path),
        "mfe_contract_sha256": tool.sha256(
            STUDY_DIR / "mfe_selection_contract.json"
        ),
        "mfe_tool_sha256": tool.sha256(Path(__file__).resolve()),
        "parameter_tool_sha256": tool.sha256(PARAMETER_TOOL),
        "train_window": [train_start, train_end],
        "comparator_params": base,
        "comparator_params_hash": comparator_result["params_hash"],
        "comparator_training_summary": comparator_result["summary"],
        "selected_params": selected["params"],
        "selected_params_hash": selected["params_hash"],
        "selected_training_summary": selected["summary"],
        "selected_training_tracks": selected["tracks"],
        "runtime": tool.runtime_identity(),
        "dataset_manifest_sha256": tool.sha256(tool.DATASET / "manifest.json"),
    }
    lock_path = OUTPUT_DIR / "locks" / fold / "tm_mfe_candidate_lock.json"
    tool.write_lock(lock_path, lock)
    return lock


async def validate_fold(tool, fold: str) -> list[dict[str, Any]]:
    lock_path = OUTPUT_DIR / "locks" / fold / "tm_mfe_candidate_lock.json"
    lock = read_json(lock_path)
    start, end = tool.FOLDS[fold]["validation"]
    state = await tool.prepare_window(
        "trailing_martingale", start, end, execution="C1"
    )
    tool.install_state(state)
    rows: list[dict[str, Any]] = []
    for variant, params in (
        ("parameter_only", lock["comparator_params"]),
        ("mfe_selected", lock["selected_params"]),
    ):
        result = tool.evaluate_task(
            ("trailing_martingale", params, VALIDATION_TRACKS)
        )
        for track, metrics in result["tracks"].items():
            rows.append(
                validation_row(
                    tool,
                    fold,
                    variant,
                    result["params_hash"],
                    track,
                    metrics,
                )
            )
    original_validation = read_json(
        STUDY_DIR
        / "validation"
        / fold
        / "trailing_martingale_validation.json"
    )
    original_by_track = {
        row["track"]: row
        for row in original_validation["rows"]
        if row["track"] in VALIDATION_TRACKS
    }
    comparator_by_track = {
        row["track"]: row for row in rows if row["variant"] == "parameter_only"
    }
    for track in VALIDATION_TRACKS:
        if comparator_by_track[track]["params_hash"] != original_by_track[track]["params_hash"]:
            raise RuntimeError(f"{fold}/{track}: comparator params drifted")
        for metric in (
            "drawdown_worst_strategy_eq",
            "gain_strategy_eq",
            "adg_strategy_eq",
            "backtest_completion_ratio",
        ):
            if not math.isclose(
                float(comparator_by_track[track][metric]),
                float(original_by_track[track][metric]),
                rel_tol=0.0,
                abs_tol=1.0e-12,
            ):
                raise RuntimeError(
                    f"{fold}/{track}: comparator {metric} does not reproduce "
                    "the locked parameter-only validation"
                )
    tool.write_lock(
        OUTPUT_DIR / "validation" / fold / "tm_mfe_validation.json",
        {
            "fold": fold,
            "strategy": "trailing_martingale",
            "candidate_lock_sha256": tool.sha256(lock_path),
            "validation_window": [start, end],
            "rows": rows,
        },
    )
    return rows


def aggregate_metrics(rows: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    primary = [
        row
        for row in rows
        if row["variant"] == variant and row["track"] in PRIMARY_TRACKS
    ]
    if len(primary) != len(FOLDS) * len(PRIMARY_TRACKS):
        raise RuntimeError(f"incomplete primary OOS rows for {variant}")
    return {
        "all_primary_rows_eligible": all(bool(row["passed"]) for row in primary),
        "worst_primary_oos_drawdown_worst_strategy_eq": max(
            float(row["drawdown_worst_strategy_eq"]) for row in primary
        ),
        "worst_primary_oos_drawdown_worst_mean_1pct_strategy_eq": max(
            float(row["drawdown_worst_mean_1pct_strategy_eq"]) for row in primary
        ),
        "worst_primary_oos_strategy_eq_recovery_days_max": max(
            float(row["strategy_eq_recovery_days_max"]) for row in primary
        ),
        "minimum_primary_oos_adg_strategy_eq": min(
            float(row["adg_strategy_eq"]) for row in primary
        ),
        "geometric_compounded_primary_oos_gain_strategy_eq": math.prod(
            float(row["gain_strategy_eq"]) for row in primary
        ),
    }


def aggregate_ranking_tuple(summary: dict[str, Any]) -> tuple[float, ...]:
    return (
        float(summary["worst_primary_oos_drawdown_worst_strategy_eq"]),
        float(summary["worst_primary_oos_drawdown_worst_mean_1pct_strategy_eq"]),
        float(summary["worst_primary_oos_strategy_eq_recovery_days_max"]),
        -float(summary["minimum_primary_oos_adg_strategy_eq"]),
        -float(summary["geometric_compounded_primary_oos_gain_strategy_eq"]),
    )


def write_development_decision(tool) -> dict[str, Any]:
    all_rows: list[dict[str, Any]] = []
    locks = {}
    for fold in FOLDS:
        lock_path = OUTPUT_DIR / "locks" / fold / "tm_mfe_candidate_lock.json"
        validation_path = (
            OUTPUT_DIR / "validation" / fold / "tm_mfe_validation.json"
        )
        validation = read_json(validation_path)
        if validation["candidate_lock_sha256"] != tool.sha256(lock_path):
            raise RuntimeError(f"{fold} MFE validation lock identity mismatch")
        current_contract_sha = tool.sha256(
            STUDY_DIR / "mfe_selection_contract.json"
        )
        if locks.get(fold) is not None:
            raise RuntimeError(f"duplicate fold lock while aggregating {fold}")
        lock = read_json(lock_path)
        parameter_lock_path = (
            STUDY_DIR
            / "locks"
            / fold
            / "trailing_martingale_candidate_lock.json"
        )
        if lock["mfe_contract_sha256"] != current_contract_sha:
            raise RuntimeError(f"{fold} MFE contract changed after selection")
        if lock["mfe_tool_sha256"] != tool.sha256(Path(__file__).resolve()):
            raise RuntimeError(f"{fold} MFE tool changed after selection")
        if lock["parameter_tool_sha256"] != tool.sha256(PARAMETER_TOOL):
            raise RuntimeError(f"{fold} parameter tool changed after MFE selection")
        if lock["parameter_candidate_lock_sha256"] != tool.sha256(
            parameter_lock_path
        ):
            raise RuntimeError(f"{fold} parameter lock changed after MFE selection")
        all_rows.extend(validation["rows"])
        locks[fold] = lock

    comparator = aggregate_metrics(all_rows, "parameter_only")
    selected = aggregate_metrics(all_rows, "mfe_selected")
    positive_training_winners = sum(
        float(lock["selected_params"]["close_retracement_base_pct"]) > 0.0
        for lock in locks.values()
    )
    non_worse_folds = 0
    for fold in FOLDS:
        fold_rows = [row for row in all_rows if row["fold"] == fold]
        baseline_mdd = max(
            float(row["drawdown_worst_strategy_eq"])
            for row in fold_rows
            if row["variant"] == "parameter_only"
            and row["track"] in PRIMARY_TRACKS
        )
        selected_mdd = max(
            float(row["drawdown_worst_strategy_eq"])
            for row in fold_rows
            if row["variant"] == "mfe_selected"
            and row["track"] in PRIMARY_TRACKS
        )
        non_worse_folds += selected_mdd <= baseline_mdd + 1.0e-12

    enabled = (
        positive_training_winners >= 2
        and bool(selected["all_primary_rows_eligible"])
        and non_worse_folds >= 2
        and aggregate_ranking_tuple(selected) < aggregate_ranking_tuple(comparator)
    )
    decision = {
        "strategy": "trailing_martingale",
        "mfe_enabled_for_final_training": enabled,
        "positive_training_winners": positive_training_winners,
        "non_worse_validation_folds": non_worse_folds,
        "comparator_oos": comparator,
        "selected_mfe_oos": selected,
        "comparator_ranking_tuple": list(aggregate_ranking_tuple(comparator)),
        "selected_mfe_ranking_tuple": list(aggregate_ranking_tuple(selected)),
        "contract_sha256": tool.sha256(
            STUDY_DIR / "mfe_selection_contract.json"
        ),
        "fold_lock_sha256": {
            fold: tool.sha256(
                OUTPUT_DIR / "locks" / fold / "tm_mfe_candidate_lock.json"
            )
            for fold in FOLDS
        },
        "fold_validation_sha256": {
            fold: tool.sha256(
                OUTPUT_DIR / "validation" / fold / "tm_mfe_validation.json"
            )
            for fold in FOLDS
        },
    }
    tool.write_lock(OUTPUT_DIR / "tm_mfe_path_decision_lock.json", decision)
    pd.DataFrame(all_rows).to_csv(
        OUTPUT_DIR / "tm_mfe_walk_forward_metrics.csv", index=False
    )
    return decision


async def run_development(tool, selected_fold: str | None) -> None:
    path_decision = read_json(STUDY_DIR / "strategy_path_decision_lock.json")
    if path_decision.get("selected_strategy") != "trailing_martingale":
        raise RuntimeError("TM MFE research requires the locked TM strategy path")
    if path_decision.get("parameter_only_status") not in {
        "passed",
        "failed_research_path_only",
    }:
        raise RuntimeError("TM MFE research has an invalid path-decision status")
    if (OUTPUT_DIR / "final_holdout_metrics.json").exists():
        raise RuntimeError("development research cannot run after final holdout was opened")
    folds = (selected_fold,) if selected_fold else FOLDS
    for fold in folds:
        lock_path = OUTPUT_DIR / "locks" / fold / "tm_mfe_candidate_lock.json"
        validation_path = (
            OUTPUT_DIR / "validation" / fold / "tm_mfe_validation.json"
        )
        if not lock_path.exists():
            await train_and_lock_fold(tool, fold)
        if not validation_path.exists():
            await validate_fold(tool, fold)
        tool.install_state({})
    if all(
        (OUTPUT_DIR / "validation" / fold / "tm_mfe_validation.json").exists()
        for fold in FOLDS
    ):
        print(json.dumps(write_development_decision(tool), indent=2))


async def run_final_search(tool) -> None:
    if (OUTPUT_DIR / "final_holdout_metrics.json").exists():
        raise RuntimeError("final search cannot run after final holdout was opened")
    decision = read_json(OUTPUT_DIR / "tm_mfe_path_decision_lock.json")
    parameter_lock_path = (
        STUDY_DIR
        / "locks"
        / "FINAL"
        / "trailing_martingale_candidate_lock.json"
    )
    parameter_lock = read_json(parameter_lock_path)
    base = parameter_lock["selected_params"]
    if float(base["close_threshold_we_weight"]) == 0.0:
        raise RuntimeError(
            "FINAL: retracement=0 forces a full grid close when "
            "close_threshold_we_weight=0, so close quantity is not matched"
        )
    state = await tool.prepare_window(
        "trailing_martingale",
        tool.FINAL_TRAIN[0],
        tool.FINAL_TRAIN[1],
        execution="C1",
    )
    tool.install_state(state)
    if decision["mfe_enabled_for_final_training"]:
        strict_records = evaluate_candidates(
            tool, "trailing_martingale", strict_candidates(tool, base)
        )
        write_stage(tool, "FINAL", "strict_ablation", strict_records)
        fine_records = evaluate_candidates(
            tool,
            "trailing_martingale",
            fine_candidates(tool, base, strict_records[0]["params"]),
        )
        write_stage(tool, "FINAL", "narrow_joint_search", fine_records)
        selected = fine_records[0]
    else:
        selected = tool.evaluate_task(
            ("trailing_martingale", deepcopy(base), PRIMARY_TRACKS)
        )
        selected["summary"] = tool.summarize_result(selected, PRIMARY_TRACKS)
    lock = {
        "strategy": "trailing_martingale",
        "train_window": list(tool.FINAL_TRAIN),
        "holdout_unopened": True,
        "parameter_candidate_lock_sha256": tool.sha256(parameter_lock_path),
        "mfe_path_decision_lock_sha256": tool.sha256(
            OUTPUT_DIR / "tm_mfe_path_decision_lock.json"
        ),
        "selected_params": selected["params"],
        "selected_params_hash": selected["params_hash"],
        "selected_training_summary": selected["summary"],
        "selected_training_tracks": selected["tracks"],
        "runtime": tool.runtime_identity(),
        "dataset_manifest_sha256": tool.sha256(tool.DATASET / "manifest.json"),
    }
    tool.write_lock(
        OUTPUT_DIR / "locks" / "FINAL" / "tm_final_candidate_lock.json",
        lock,
    )
    tool.install_state({})
    print(json.dumps(lock, indent=2))


async def run_final_holdout(tool) -> None:
    lock_path = OUTPUT_DIR / "locks" / "FINAL" / "tm_final_candidate_lock.json"
    lock = read_json(lock_path)
    state = await tool.prepare_window(
        "trailing_martingale",
        tool.FINAL_HOLDOUT[0],
        tool.FINAL_HOLDOUT[1],
        execution="C1",
    )
    tool.install_state(state)
    result = tool.evaluate_task(
        ("trailing_martingale", lock["selected_params"], VALIDATION_TRACKS)
    )
    rows = [
        validation_row(
            tool,
            "FINAL",
            "locked_final",
            result["params_hash"],
            track,
            metrics,
        )
        for track, metrics in result["tracks"].items()
    ]
    payload = {
        "strategy": "trailing_martingale",
        "candidate_lock_sha256": tool.sha256(lock_path),
        "holdout_window": list(tool.FINAL_HOLDOUT),
        "rows": rows,
    }
    tool.write_lock(OUTPUT_DIR / "final_holdout_metrics.json", payload)
    pd.DataFrame(rows).to_csv(OUTPUT_DIR / "final_holdout_metrics.csv", index=False)
    tool.install_state({})
    print(json.dumps(payload, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=("development", "final-search", "final-holdout"),
    )
    parser.add_argument("--fold", choices=FOLDS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tool = load_parameter_tool()
    tool.disable_network()
    if args.phase == "development":
        asyncio.run(run_development(tool, args.fold))
    elif args.phase == "final-search":
        if args.fold is not None:
            raise ValueError("--fold is only valid for the development phase")
        asyncio.run(run_final_search(tool))
    else:
        if args.fold is not None:
            raise ValueError("--fold is only valid for the development phase")
        asyncio.run(run_final_holdout(tool))


if __name__ == "__main__":
    main()
