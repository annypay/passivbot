#!/usr/bin/env python3
"""Independently verify one arm's deep analysis and the study's evidence chain.

This script deliberately does **not** import the renderer (`generate_annual_report.py`): it
re-derives the numbers from `analysis.json`, `fills.csv`, `balance_and_equity.csv.gz` and the
frozen dataset's benchmark series, then compares them with what the report and its side
artifacts claim. It also re-checks the things the study's claim rests on:

* the frozen config really is the parent config plus the arm's declared changes,
* the run's dumped config carried the same declaration (including the runtime plot flag),
* the reference anchors still hash to their pins,
* the bundle layout and the report skeleton match the convention,
* no tracked evidence carries a host-specific path,
* a synthetic arm says so in its own scope section.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPORT_SPEC_DIR = Path(__file__).resolve().parents[3] / "report_spec"
TOOLS_DIR = Path(__file__).resolve().parent
for path in (str(REPORT_SPEC_DIR), str(TOOLS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import annual_analysis as spec  # noqa: E402  (the convention's own validator)
import event_windows as events  # noqa: E402
import variant_spec as study  # noqa: E402
import wipeout_matrix as wipeout  # noqa: E402

REQUIRED_LOCAL = (
    "analysis.json",
    "config.json",
    "dataset.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
    "coin_metrics.csv",
    "annual_metrics.csv",
    "monthly_metrics.csv",
    "run_record.json",
    "global_metrics.json",
    "tail_risk_events.json",
    "tail_risk_wipeout.json",
    "annual_analysis.md",
)
HOST_PATH_PATTERNS = (
    re.compile(r"/home/[A-Za-z0-9._-]+/"),
    re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    re.compile(r"[A-Za-z]:\\\\Users\\\\"),
    re.compile(r"\\\\\\\\wsl"),
)
TRACKED_EVIDENCE = (
    "analysis.json",
    "run_record.json",
    "global_metrics.json",
    "tail_risk_events.json",
    "tail_risk_wipeout.json",
    "annual_analysis.md",
)
PERIOD_COLUMNS = (
    "period",
    "sample_start_utc",
    "sample_end_utc",
    "coverage",
    "starting_total_balance_usd",
    "ending_total_balance_usd",
    "total_balance_return_pct",
    "starting_total_equity_usd",
    "ending_total_equity_usd",
    "total_equity_return_pct",
    "starting_strategy_equity",
    "ending_strategy_equity",
    "strategy_equity_return_pct",
    "max_intraperiod_equity_drawdown_pct",
    "max_intraperiod_strategy_equity_drawdown_pct",
    "fills_count",
    "entry_fills_count",
    "reduction_or_close_fills_count",
    "long_fills_count",
    "short_fills_count",
    "maker_fills_count",
    "taker_fills_count",
    "realized_pnl_raw_usd",
    "fees_signed_usd",
    "net_realized_pnl_usd",
    "max_abs_wallet_exposure_at_fill",
    "active_coins",
)
COIN_COLUMNS = (
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
)


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def read_csv(path: Path) -> pd.DataFrame:
    """The equity series: first column is the timestamp, so it becomes the index."""
    frame = pd.read_csv(path, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")
    return frame.sort_index()


def read_plain_csv(path: Path) -> pd.DataFrame:
    """A ledger/table file whose first column is a row index, not a timestamp."""
    return pd.read_csv(path)


def check_layout(run_dir: Path, config: dict[str, Any]) -> list[str]:
    return spec.assert_bundle_layout(run_dir, config=config, expect_plots=True)


def check_skeleton(run_dir: Path) -> list[str]:
    report = (run_dir / "annual_analysis.md").read_text(encoding="utf-8")
    return spec.assert_report_structure(report)


def check_arm_identity(
    variant: study.Variant, config: dict[str, Any], run_config: dict[str, Any]
) -> list[str]:
    """Re-derive the arm's frozen config from the parent and compare against what ran."""
    problems: list[str] = []
    parent = study.load_json(study.SOURCE_CONFIG)
    expected = study.load_json(variant.config_path)
    expected_sha = study.sha256_file(variant.config_path)
    arms_input = study.load_json(study.VARIANT_INPUT_PATH).get("arms", [])
    pinned = next(
        (item.get("config_sha256") for item in arms_input if item.get("key") == variant.key), None
    )
    if pinned and pinned != expected_sha:
        problems.append(f"frozen config sha256 {expected_sha} != variant_input pin {pinned}")
    allowed = (
        study.ALLOWED_DATASET_DIFF_PATHS
        if variant.dataset.override_mode
        else study.ALLOWED_CONFIG_DIFF_PATHS
    )
    problems.extend(
        f"frozen config {problem}"
        for problem in study.diff_subtrees(parent["backtest"], expected["backtest"], "backtest", allowed=allowed)
    )
    declared_paths: dict[str, tuple[str, ...]] = {}
    for dotted, _from, _to in variant.deltas:
        root = dotted.split(".")[0]
        declared_paths[root] = (*declared_paths.get(root, ()), dotted)
    for root in study.IDENTITY_ROOTS:
        problems.extend(
            f"frozen config {problem}"
            for problem in study.diff_subtrees(
                parent.get(root), expected.get(root), root, allowed=declared_paths.get(root, ())
            )
        )
    normalized = study.normalize_config_payload(expected)
    for root in ("bot", "live", "coin_overrides"):
        problems.extend(
            f"run config {problem}"
            for problem in study.diff_run_config_root(
                normalized[root], run_config[root], root, declared=declared_paths.get(root, ())
            )
        )
    for key in ("start_date", "end_date"):
        if not study.same_day(normalized["backtest"].get(key), run_config["backtest"].get(key)):
            problems.append(
                f"run config backtest.{key} {run_config['backtest'].get(key)!r} != frozen "
                f"{normalized['backtest'].get(key)!r}"
            )
    run_disabled = study.disabled_plot_groups(run_config["backtest"].get("disable_plotting"))
    if run_disabled != study.declared_plot_groups(variant):
        problems.append(
            f"run config disabled plot groups {sorted(run_disabled)} != declared "
            f"{sorted(study.declared_plot_groups(variant))}"
        )
    return problems


def check_reference_pins() -> list[str]:
    problems = []
    for key, item in study.REFERENCE_RUNS.items():
        path = Path(item["run_dir"]) / "analysis.json"
        if not path.exists():
            problems.append(f"reference {key}: missing {study.relative(path)}")
            continue
        actual = study.sha256_file(path)
        if actual != item["analysis_sha256"]:
            problems.append(
                f"reference {key}: analysis sha256 {actual} != pinned {item['analysis_sha256']}"
            )
    return problems


def check_period_tables(run_dir: Path) -> list[str]:
    problems: list[str] = []
    equity = spec.load_balance_equity(run_dir)
    fills = spec.load_fills(run_dir)
    for name, freq in (("annual_metrics.csv", "Y"), ("monthly_metrics.csv", "M")):
        path = run_dir / name
        frame = pd.read_csv(path)
        if list(frame.columns) != list(PERIOD_COLUMNS):
            problems.append(f"{name}: columns {list(frame.columns)} != convention schema")
            continue
        expected = spec.build_period_table(equity, fills, freq)
        frame["period"] = frame["period"].astype(str)
        expected["period"] = expected["period"].astype(str)
        if len(frame) != len(expected):
            problems.append(f"{name}: {len(frame)} rows, recomputed {len(expected)}")
            continue
        joins = frame.merge(expected, on="period", suffixes=("_report", "_recheck"))
        for column in (
            "ending_total_balance_usd",
            "ending_strategy_equity",
            "strategy_equity_return_pct",
            "max_intraperiod_strategy_equity_drawdown_pct",
            "fills_count",
            "net_realized_pnl_usd",
        ):
            left = joins[f"{column}_report"].to_numpy(dtype="float64")
            right = joins[f"{column}_recheck"].to_numpy(dtype="float64")
            if not np.allclose(left, right, rtol=1e-9, atol=1e-6, equal_nan=True):
                delta = float(np.nanmax(np.abs(left - right)))
                problems.append(f"{name}: {column} differs from the recomputation by {delta:g}")
    coin_path = run_dir / "coin_metrics.csv"
    coin_frame = pd.read_csv(coin_path)
    if list(coin_frame.columns) != list(COIN_COLUMNS):
        problems.append(f"coin_metrics.csv: columns {list(coin_frame.columns)} != convention schema")
    else:
        expected_coins = spec.build_coin_table(fills)
        if sorted(coin_frame["coin"]) != sorted(expected_coins["coin"]):
            problems.append("coin_metrics.csv: coin set differs from the ledger")
        else:
            joins = coin_frame.merge(expected_coins, on="coin", suffixes=("_report", "_recheck"))
            for column in ("fills_count", "net_realized_pnl_usd", "max_abs_wallet_exposure_at_fill"):
                left = joins[f"{column}_report"].to_numpy(dtype="float64")
                right = joins[f"{column}_recheck"].to_numpy(dtype="float64")
                if not np.allclose(left, right, rtol=1e-9, atol=1e-9, equal_nan=True):
                    delta = float(np.nanmax(np.abs(left - right)))
                    problems.append(f"coin_metrics.csv: {column} differs by {delta:g}")
    return problems


def check_events_artifact(
    run_dir: Path, variant: study.Variant, equity: pd.DataFrame, fills: pd.DataFrame
) -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    payload = study.load_json(run_dir / "tail_risk_events.json")
    benchmark = events.load_benchmark(variant.dataset.path)
    rule = study.EVENT_RULE
    episodes = events.detect_episodes(
        benchmark,
        lookback_days=int(rule["lookback_days"]),
        threshold=float(rule["threshold"]),
        min_gap_days=int(rule["min_gap_days"]),
        labels=study.EVENT_LABELS,
    )
    injections: list[events.Episode] = []
    if variant.synthetic and study.SYNTHETIC_BUNDLES_PATH.exists():
        record = (study.load_json(study.SYNTHETIC_BUNDLES_PATH).get("bundles") or {}).get(
            variant.dataset.key
        )
        injections = events.injection_episodes(record) if record else []
    recomputed = events.arm_event_table(equity, fills, episodes, synthetic=False) + (
        events.arm_event_table(equity, fills, injections, synthetic=True) if injections else []
    )
    recorded = payload.get("events", [])
    if len(recorded) != len(recomputed):
        problems.append(f"tail_risk_events.json has {len(recorded)} events, recomputed {len(recomputed)}")
        return problems, payload
    for left, right in zip(recorded, recomputed):
        if left["event"] != right["event"]:
            problems.append(f"event key {left['event']} != {right['event']}")
            continue
        for key in ("max_drawdown", "days_to_trough", "underwater_days"):
            a, b = left.get(key), right.get(key)
            if a is None and b is None:
                continue
            if a is None or b is None or not np.isclose(float(a), float(b), rtol=1e-9, atol=1e-9):
                problems.append(f"event {left['event']}: {key} {a} != recomputed {b}")
        if int(left.get("panic_fills", 0)) != int(right.get("panic_fills", 0)):
            problems.append(
                f"event {left['event']}: panic_fills {left.get('panic_fills')} != "
                f"recomputed {right.get('panic_fills')}"
            )
    ledger = payload.get("ledger") or {}
    if int(ledger.get("fills_count", -1)) != int(fills.shape[0]):
        problems.append(f"events ledger fills_count {ledger.get('fills_count')} != {len(fills)}")
    return problems, payload


def check_wipeout_artifact(
    run_dir: Path, variant: study.Variant, analysis: dict[str, Any], fills: pd.DataFrame
) -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    payload = study.load_json(run_dir / "tail_risk_wipeout.json")
    equity = read_csv(run_dir / "balance_and_equity.csv.gz")
    start_balance = float(equity["usd_total_balance"].iloc[0]) if not equity.empty else None
    matrix = wipeout.build_matrix(
        variant.key,
        analysis=analysis,
        per_coin=wipeout.per_coin_max_exposure(fills),
        fills=fills,
        shock_levels=study.SHOCK_LEVELS,
        start_balance_usd=start_balance,
    )
    problems.extend(wipeout.assert_identities(matrix))
    recorded_ruin = payload.get("ruin") or {}
    for key, expected in (
        ("peak_total_exposure", matrix.peak_total_exposure),
        ("ruin_distance", matrix.ruin_distance),
        ("worst_coin_exposure", matrix.scope_exposure("worst_coin")),
        ("top3_exposure", matrix.scope_exposure("top3_worst")),
        ("top7_exposure", matrix.scope_exposure("top7_slots")),
    ):
        actual = recorded_ruin.get(key)
        if actual is None or not np.isclose(float(actual), float(expected), rtol=1e-9, atol=1e-9):
            problems.append(f"wipeout ruin.{key} {actual} != recomputed {expected}")
    recorded_per_coin = {
        item["coin"]: float(item["max_abs_wallet_exposure"])
        for item in payload.get("per_coin_max_exposure", [])
    }
    for coin, value in matrix.per_coin:
        if coin not in recorded_per_coin:
            problems.append(f"wipeout per-coin list is missing {coin}")
        elif not np.isclose(recorded_per_coin[coin], value, rtol=1e-9, atol=1e-9):
            problems.append(
                f"wipeout per-coin {coin}: {recorded_per_coin[coin]} != recomputed {value}"
            )
    if analysis.get("total_wallet_exposure_max") is not None and not np.isclose(
        matrix.peak_total_exposure,
        float(analysis["total_wallet_exposure_max"]),
        rtol=1e-12,
        atol=1e-12,
    ):
        problems.append(
            "wipeout peak exposure does not equal analysis.json total_wallet_exposure_max"
        )
    # The engine's own per-coin column must agree with the ledger-derived one.
    coin_frame = read_plain_csv(run_dir / "coin_metrics.csv")
    engine_per_coin = dict(wipeout.per_coin_max_exposure_from_coin_metrics(coin_frame))
    for coin, value in matrix.per_coin[:10]:
        engine_value = engine_per_coin.get(coin)
        if engine_value is None:
            continue
        if not np.isclose(engine_value, value, rtol=1e-6, atol=1e-9):
            problems.append(
                f"per-coin exposure for {coin}: ledger {value} != engine column {engine_value}"
            )
    return problems, payload


def check_report_numbers(
    run_dir: Path, variant: study.Variant, events_payload: dict[str, Any], wipeout_payload: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    report = (run_dir / "annual_analysis.md").read_text(encoding="utf-8")
    ruin = wipeout_payload["ruin"]
    checks = (
        (f"{ruin['peak_total_exposure']:.6f}", "总暴露峰值（6 位小数）"),
        (f"{ruin['ruin_distance'] * 100:.2f}%", "爆仓距离（百分比）"),
        (f"{ruin['worst_coin_exposure']:.6f}", "最大单币敞口（6 位小数）"),
    )
    for text, label in checks:
        if text not in report:
            problems.append(f"the report does not state {label} as {text!r}")
    for item in events_payload["events"]:
        if item["label"] not in report:
            problems.append(f"the report does not list the event {item['label']!r}")
    record = study.load_json(run_dir / "run_record.json")
    for delta in record["declared_delta"]:
        if f"`{delta['path']}`" not in report:
            problems.append(f"the report does not declare the change {delta['path']!r}")
    if variant.synthetic and "合成" not in report:
        problems.append("a synthetic arm must say so in its own report")
    if variant.leg == "ext" and "disable_plotting" not in report and "coin_fills" not in report:
        problems.append("the long leg must state which figure group it disabled")
    return problems


def check_no_host_paths(run_dir: Path) -> list[str]:
    problems: list[str] = []
    for name in TRACKED_EVIDENCE:
        path = run_dir / name
        if not path.exists():
            problems.append(f"missing tracked evidence {name}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in HOST_PATH_PATTERNS:
            match = pattern.search(text)
            if match:
                problems.append(f"{name}: host-specific path {match.group(0)!r} in tracked evidence")
    return problems


def check_synthetic_record(variant: study.Variant, run_dir: Path) -> list[str]:
    if not variant.synthetic:
        return []
    problems = []
    if not study.SYNTHETIC_BUNDLES_PATH.exists():
        return [f"missing {study.relative(study.SYNTHETIC_BUNDLES_PATH)}"]
    record = study.load_json(study.SYNTHETIC_BUNDLES_PATH)
    bundle = (record.get("bundles") or {}).get(variant.dataset.key)
    if not bundle:
        problems.append(f"no synthetic record for {variant.dataset.key}")
        return problems
    manifest = variant.dataset.path / "manifest.json"
    if manifest.exists():
        actual = study.sha256_file(manifest)
        if bundle.get("manifest_sha256") not in (None, actual):
            problems.append(
                f"synthetic bundle manifest sha256 {actual} != recorded {bundle.get('manifest_sha256')}"
            )
    if not bundle.get("targets"):
        problems.append("synthetic record lists no target coin")
    return problems


def verify_arm(variant: study.Variant, result_dir: Path | None = None) -> list[str]:
    problems: list[str] = []
    try:
        run_dir = study.find_variant_run_dir(variant, result_dir)
    except SystemExit as exc:
        return [str(exc)]
    missing = [name for name in REQUIRED_LOCAL if not (run_dir / name).exists()]
    if missing:
        return [f"missing artifact {name}" for name in missing]

    config = study.load_json(run_dir / "config.json")
    run_config = config
    analysis = study.load_json(run_dir / "analysis.json")
    equity = events.load_equity(run_dir)
    fills = events.load_fills(run_dir)
    if "type" not in fills.columns:
        return ["fills.csv has no type column"]

    problems.extend(check_layout(run_dir, config))
    problems.extend(check_skeleton(run_dir))
    problems.extend(check_arm_identity(variant, study.load_json(variant.config_path), run_config))
    problems.extend(check_period_tables(run_dir))
    event_problems, events_payload = check_events_artifact(run_dir, variant, equity, fills)
    problems.extend(event_problems)
    wipeout_problems, wipeout_payload = check_wipeout_artifact(run_dir, variant, analysis, fills)
    problems.extend(wipeout_problems)
    problems.extend(check_report_numbers(run_dir, variant, events_payload, wipeout_payload))
    problems.extend(check_no_host_paths(run_dir))
    problems.extend(check_synthetic_record(variant, run_dir))

    audit = variant.execution_audit_path
    if audit.exists():
        rows = sum(1 for _ in audit.open(encoding="utf-8")) - 1
        if int(analysis.get("fills_count") or 0) != rows:
            problems.append(
                f"execution audit rows {rows} != analysis fills_count {analysis.get('fills_count')}"
            )
    else:
        problems.append(f"missing execution audit {study.relative(audit)}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default=None, choices=study.RUN_VARIANT_ORDER)
    parser.add_argument("--all", action="store_true", help="verify every arm with a bundle")
    parser.add_argument("--result-dir", default=None)
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="report arms without a bundle as skipped instead of failing",
    )
    args = parser.parse_args(argv)

    if not args.variant and not args.all:
        parser.error("pass --variant <key> or --all")

    keys = list(study.RUN_VARIANT_ORDER) if args.all else [args.variant]
    failures: dict[str, list[str]] = {}
    skipped: list[str] = []
    for key in keys:
        variant = study.VARIANTS_BY_KEY[key]
        try:
            run_dir = study.find_variant_run_dir(variant, args.result_dir if args.variant else None)
        except SystemExit:
            if args.allow_missing:
                skipped.append(key)
                continue
            failures[key] = ["no run directory; run run_variant.py first"]
            continue
        problems = verify_arm(variant, str(run_dir))
        if problems:
            failures[key] = problems
        else:
            print(f"verified {key}: {len(REQUIRED_LOCAL)} artifacts, event and wipe-out tables recomputed")

    pin_problems = check_reference_pins()
    if pin_problems:
        failures["reference_pins"] = pin_problems

    if skipped:
        print(f"skipped arms without a bundle: {', '.join(skipped)}")
    if failures:
        for key, problems in failures.items():
            print(f"FAIL {key}:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"all {len(keys) - len(skipped)} verified arm(s) pass independent verification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
