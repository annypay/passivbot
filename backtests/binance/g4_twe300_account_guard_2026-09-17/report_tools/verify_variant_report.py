#!/usr/bin/env python3
"""Independently verify one arm's deep analysis (account-guard study) and its evidence chain.

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
import guard_analysis as guard  # noqa: E402
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
    "guard_readiness.json",
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
    "guard_readiness.json",
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
    # The arm's own `backtest` declarations (starting capital) are legitimate differences;
    # `allowed_backtest_paths` = output location / dataset override + those declarations.
    allowed = study.allowed_backtest_paths(variant)
    problems.extend(
        f"frozen config {problem}"
        for problem in study.diff_subtrees(
            parent["backtest"], expected["backtest"], "backtest", allowed=allowed
        )
    )
    for dotted, _from, to in variant.deltas:
        if not dotted.startswith("backtest."):
            continue
        actual = study.get_path(expected, dotted)
        if not study.numeric_equal(actual, to):
            problems.append(f"frozen config {dotted}: expected {to!r}, got {actual!r}")
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
    recomputed = events.arm_event_table(equity, fills, episodes, synthetic=False)
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
    config = study.load_json(run_dir / "config.json")
    declared_twe = float(config["bot"]["long"]["risk"]["total_wallet_exposure_limit"])
    starting_balance = float(config["backtest"].get("starting_balance") or 0.0)
    liquidation_threshold = float(config["backtest"].get("liquidation_threshold") or 0.0)
    if not np.isclose(declared_twe, variant.declared_twe, rtol=1e-12, atol=1e-12):
        problems.append(
            f"run config total_wallet_exposure_limit {declared_twe} != declared "
            f"{variant.declared_twe}"
        )
    if not np.isclose(starting_balance, variant.starting_balance, rtol=1e-12, atol=1e-12):
        problems.append(
            f"run config starting_balance {starting_balance} != declared {variant.starting_balance}"
        )
    matrix = wipeout.build_matrix(
        variant.key,
        analysis=analysis,
        per_coin=wipeout.per_coin_max_exposure(fills),
        fills=fills,
        shock_levels=study.SHOCK_LEVELS,
        declared_twe=declared_twe,
        start_balance_usd=start_balance,
        starting_balance_usd=starting_balance,
        liquidation_threshold=liquidation_threshold,
    )
    problems.extend(wipeout.assert_identities(matrix))
    if matrix.peak_total_exposure > declared_twe + 1e-3:
        problems.append(
            f"observed peak exposure {matrix.peak_total_exposure} exceeds the declared limit "
            f"{declared_twe}"
        )
    floor = starting_balance * liquidation_threshold
    last_equity = float(equity["usd_total_equity"].iloc[-1]) if not equity.empty else None
    # The persisted series is sampled every `balance_sample_divider` minutes, so a liquidated
    # arm's last row is *above* the floor; the liquidation shape is checked against the
    # engine's own numbers instead: the flag, the at-least-90% drawdown and the truncated
    # window (verified separately below).
    if analysis.get("liquidated"):
        worst_dd = analysis.get("drawdown_worst_strategy_eq")
        if worst_dd is None or float(worst_dd) < 0.9:
            problems.append(
                f"liquidated arm reports a worst drawdown of {worst_dd!r}; reaching the 5% floor "
                "implies at least a 90% drawdown"
            )
        if last_equity is not None and last_equity <= floor + max(1.0, floor * 0.01):
            problems.append(
                f"liquidated arm's last sampled equity {last_equity} is already at the floor "
                f"{floor}; the sampling lag should leave it above"
            )
        if str(analysis.get("effective_end_date") or "") >= variant.dataset.window[1]:
            problems.append(
                f"liquidated arm reaches the declared window end "
                f"{analysis.get('effective_end_date')!r}"
            )
    elif last_equity is not None and last_equity <= floor + max(1.0, floor * 0.01):
        problems.append(
            f"arm is not flagged liquidated but ends at equity {last_equity} at the floor {floor}"
        )
    recorded_ruin = payload.get("ruin") or {}
    for key, expected in (
        ("peak_total_exposure", matrix.peak_total_exposure),
        ("ruin_distance", matrix.ruin_distance),
        ("worst_coin_exposure", matrix.scope_exposure("worst_coin")),
        ("top3_exposure", matrix.scope_exposure("top3_worst")),
        ("top7_exposure", matrix.scope_exposure("top7_slots")),
        ("declared_twe", declared_twe),
        ("liquidation_shock_at_peak", matrix.liquidation_shock),
    ):
        actual = recorded_ruin.get(key)
        if expected is None:
            continue
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


def _same_value(left: Any, right: Any, tol: float = 1e-9) -> bool:
    """Compare two config values: strings/bools by identity, numbers with a tolerance."""
    if isinstance(left, str) or isinstance(right, str):
        return str(left) == str(right)
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if left is None or right is None:
        return left is right
    try:
        a, b = float(left), float(right)
    except (TypeError, ValueError):
        return left == right
    if not (np.isfinite(a) and np.isfinite(b)):
        return a == b
    return bool(np.isclose(a, b, rtol=tol, atol=tol))


def check_guard_artifact(
    run_dir: Path, variant: study.Variant, analysis: dict[str, Any]
) -> list[str]:
    """Recompute the halt table and compare it with what the arm recorded."""
    problems: list[str] = []
    payload = study.load_json(run_dir / "guard_readiness.json")
    recorded_guard = payload.get("guard") or {}
    declared = variant.guard_params
    for key, expected in declared.items():
        if key not in recorded_guard:
            problems.append(f"guard_readiness is missing {key}")
            continue
        actual = recorded_guard[key]
        same = actual is expected if isinstance(expected, bool) else _same_value(actual, expected)
        if not same:
            problems.append(f"guard_readiness {key} {actual!r} != declared {expected!r}")
    recomputed = guard.build_guard_artifact(run_dir, analysis)
    for key in (
        "halt_count",
        "terminal_halt_count",
        "complete_halt_count",
        "total_halt_minutes",
        "max_halt_minutes",
        "mean_halt_minutes",
        "immediate_retriggers_within_7d",
    ):
        recorded_value = payload.get(key)
        expected_value = recomputed.get(key)
        if isinstance(expected_value, (int, float)) and not isinstance(expected_value, bool):
            if recorded_value is None or not np.isclose(
                float(recorded_value), float(expected_value), rtol=1e-9, atol=1e-9
            ):
                problems.append(
                    f"guard_readiness {key} {recorded_value!r} != recomputed {expected_value!r}"
                )
    for key in ("idle_after_halt_minutes_max", "out_of_market_vs_declared_ratio"):
        recorded_value = payload.get(key)
        expected_value = recomputed.get(key)
        if (recorded_value is None) != (expected_value is None):
            problems.append(
                f"guard_readiness {key} {recorded_value!r} != recomputed {expected_value!r}"
            )
        elif expected_value is not None and not _same_value(recorded_value, expected_value):
            problems.append(
                f"guard_readiness {key} {recorded_value!r} != recomputed {expected_value!r}"
            )
    problems.extend(_halt_table_problems(payload, recomputed))
    problems.extend(
        f"guard cross-check: {problem}" for problem in (payload.get("cross_check_problems") or [])
    )
    problems.extend(
        f"guard cross-check: {problem}" for problem in (recomputed.get("cross_check_problems") or [])
    )
    triggers = int(analysis.get("hard_stop_triggers") or 0)
    if triggers > 0 and payload.get("halt_count", 0) == 0:
        problems.append(
            f"the engine reports {triggers} trigger(s) but no halt window was derived"
        )
    declared_halt = float(declared.get("cooldown_minutes_after_red") or 0.0)
    if declared_halt > 0 and payload.get("complete_halt_count", payload.get("halt_count", 0)):
        shortest = min(
            (
                float(halt["minutes"])
                for halt in (payload.get("halts") or [])
                if not halt.get("terminal")
            ),
            default=None,
        )
        if shortest is not None and shortest + 120.0 < declared_halt:
            problems.append(
                f"shortest derived halt {shortest:.1f} min is shorter than the declared halt "
                f"{declared_halt:.1f} min beyond the sampling tolerance"
            )
    return problems


def _halt_table_problems(
    payload: dict[str, Any], recomputed: dict[str, Any], tol: float = 1e-9
) -> list[str]:
    """Compare the recorded halt table with the recomputed one, row by row."""
    problems: list[str] = []
    recorded_rows = payload.get("halts") or []
    expected_rows = recomputed.get("halts") or []
    if len(recorded_rows) != len(expected_rows):
        return [
            f"guard_readiness records {len(recorded_rows)} halt row(s) but {len(expected_rows)} "
            "were recomputed"
        ]
    fields = (
        "start",
        "end",
        "minutes",
        "equity_usd",
        "drawdown_at_start",
        "resume_equity_usd",
        "terminal",
        "declared_minutes",
        "shortfall_minutes",
        "excess_minutes",
        "post_halt_return_7d",
        "post_halt_return_30d",
    )
    for index, (recorded, expected) in enumerate(zip(recorded_rows, expected_rows)):
        for field in fields:
            if field not in recorded:
                problems.append(f"halt row {index} is missing {field}")
                continue
            if not _same_value(recorded.get(field), expected.get(field), tol=tol):
                problems.append(
                    f"halt row {index} {field}: recorded {recorded.get(field)!r} != recomputed "
                    f"{expected.get(field)!r}"
                )
    return problems


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
    floor_text = f"{ruin['starting_balance_usd'] * (ruin['liquidation_threshold'] or 0):,.0f}"
    if floor_text not in report:
        problems.append(f"the report does not state the liquidation floor as {floor_text!r}")
    if variant.leg == "ext" and "强平" not in report:
        problems.append("a long-leg report must state the liquidation reading")
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
    problems.extend(check_guard_artifact(run_dir, variant, analysis))
    problems.extend(check_no_host_paths(run_dir))

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
