#!/usr/bin/env python3
"""Verify the risk-geometry synthesis against the arms it summarises.

The synthesis (`account_guard_analysis.md` + `artifacts/account_guard_summary.json`) is the
study's headline document, so it gets the same treatment as a per-run report: this script
recomputes its numbers from the arms' own tracked artifacts (`analysis.json`,
`run_record.json`, `guard_readiness.json`, `tail_risk_wipeout.json`) with code that does not
import `build_synthesis.py`, and checks the document's structure, the guard configuration
surface and the honesty boundary.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import geometry_analysis as geometry  # noqa: E402
import guard_analysis as guard  # noqa: E402
import variant_spec as study  # noqa: E402

SYNTHESIS_PATH = study.STUDY / "risk_geometry_analysis.md"
SUMMARY_PATH = study.ARTIFACTS / "risk_geometry_summary.json"
REQUIRED_HEADINGS = (
    "# g4 @ TWE 3.0 的风险几何优化",
    "## 一、问题、条件映射与方法",
    "## 二、全期指标对比",
    "## 三、暴露几何与占用纪律对照",
    "## 四、守护读数对照",
    "## 五、整装待发读数",
    "## 六、参数搜索轨迹与选择",
    "## 七、样本外与跨腿对照",
    "## 八、强平与存活",
    "## 九、事件窗口压力表",
    "## 十、暴露上界与冲击损失矩阵",
    "## 十一、判据裁决",
    "## 十二、结论与建议",
    "## 十三、边界",
)
GEOMETRY_FIELDS = (
    "per_slot_cap",
    "peak_coin_exposure",
    "peak_coin_share_of_peak_total",
    "top3_exposure",
    "active_coins_mean",
    "empty_slot_time_share",
    "single_coin_wipeout_bound",
    "liquidation_shock_at_peak",
)
HOST_PATH_PATTERNS = (
    re.compile(r"/home/[A-Za-z0-9._-]+/"),
    re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    re.compile(r"[A-Za-z]:\\\\Users\\\\"),
    re.compile(r"\\\\\\\\wsl"),
)


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def cagr(analysis: dict[str, Any]) -> float | None:
    """Annualised growth; a liquidated run's truncated window has no meaningful CAGR."""
    if analysis.get("liquidated"):
        return None
    gain = analysis.get("gain_strategy_eq")
    days = analysis.get("n_days")
    try:
        gain = float(gain)
        days = float(days)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(gain) and math.isfinite(days)) or gain <= 0 or days <= 0:
        return None
    return gain ** (365.25 / days) - 1.0


def _close(left: Any, right: Any, tol: float = 1e-9) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    try:
        a, b = float(left), float(right)
    except (TypeError, ValueError):
        return left == right
    if not (math.isfinite(a) and math.isfinite(b)):
        return a == b
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


def declared_halt_hours(readiness: dict[str, Any]) -> float | None:
    """Total time the guard itself declared it would keep the account out, in hours."""
    declared = [
        float(halt["declared_minutes"])
        for halt in (readiness.get("halts") or [])
        if halt.get("declared_minutes") is not None
    ]
    if not declared:
        return None
    return sum(declared) / 60.0


def verify(document: str, summary: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for heading in REQUIRED_HEADINGS:
        if heading not in document:
            problems.append(f"the synthesis is missing the section {heading!r}")
    if len(document) < 4000:
        problems.append(f"the synthesis looks too thin ({len(document)} characters)")
    for pattern in HOST_PATH_PATTERNS:
        match = pattern.search(document)
        if match:
            problems.append(f"the synthesis carries a host path {match.group(0)!r}")
            break
    for phrase in ("占用纪律", "整装待发", "样本外", "we_excess_allowance_pct"):
        if phrase not in document:
            problems.append(f"the synthesis never mentions {phrase!r}")

    arms = summary.get("arms") or {}
    missing = summary.get("missing_arms") or []
    declared = set(study.RUN_VARIANT_ORDER) | set(study.REFERENCE_RUNS)
    covered = set(arms) | set(missing)
    if covered != declared:
        problems.append(
            f"the summary covers {sorted(covered)} but the study declares {sorted(declared)}"
        )
    if missing:
        problems.append(f"the synthesis was written with missing arms: {sorted(missing)}")

    for key, entry in arms.items():
        variant = study.VARIANTS_BY_KEY.get(key)
        if variant is None:
            reference = study.REFERENCE_RUNS.get(key)
            if reference is None:
                problems.append(f"{key}: not a declared arm")
                continue
            run_dir = Path(reference["run_dir"])
        else:
            try:
                run_dir = study.find_variant_run_dir(variant)
            except SystemExit as exc:
                problems.append(f"{key}: {exc}")
                continue
        analysis = study.load_json(run_dir / "analysis.json")
        for field, expected in (
            ("gain_strategy_eq", analysis.get("gain_strategy_eq")),
            ("drawdown_worst_strategy_eq", analysis.get("drawdown_worst_strategy_eq")),
            ("liquidated", bool(analysis.get("liquidated"))),
        ):
            if not _close(entry.get(field), expected):
                problems.append(f"{key}: summary {field} {entry.get(field)!r} != analysis {expected!r}")
        expected_cagr = cagr(analysis)
        if expected_cagr is not None and not _close(entry.get("cagr"), expected_cagr):
            problems.append(f"{key}: summary cagr {entry.get('cagr')!r} != recomputed {expected_cagr!r}")
        if variant is not None:
            declared_guard = variant.guard_params
            recorded_guard = entry.get("guard") or {}
            for guard_key, expected in declared_guard.items():
                if guard_key not in recorded_guard:
                    problems.append(f"{key}: summary guard is missing {guard_key}")
                elif isinstance(expected, bool):
                    if recorded_guard[guard_key] is not expected:
                        problems.append(
                            f"{key}: summary guard {guard_key} {recorded_guard[guard_key]!r} "
                            f"!= declared {expected!r}"
                        )
                elif not _close(recorded_guard[guard_key], expected):
                    problems.append(
                        f"{key}: summary guard {guard_key} {recorded_guard[guard_key]!r} "
                        f"!= declared {expected!r}"
                    )
            readiness_path = run_dir / "guard_readiness.json"
            if readiness_path.exists():
                recomputed = guard.build_guard_artifact(run_dir, analysis)
                for field in (
                    "halt_count",
                    "total_halt_hours",
                    "terminal_halt_count",
                    "complete_halt_count",
                    "immediate_retriggers_within_7d",
                ):
                    recorded = entry.get(field)
                    expected_value = recomputed.get(field)
                    if isinstance(expected_value, (int, float)) and not isinstance(
                        expected_value, bool
                    ):
                        if recorded is None or not _close(recorded, expected_value):
                            problems.append(
                                f"{key}: summary {field} {recorded!r} != recomputed "
                                f"{expected_value!r}"
                            )
                for field in ("out_of_market_vs_declared_ratio", "idle_after_halt_minutes_max"):
                    recorded = entry.get(field)
                    expected_value = recomputed.get(field)
                    if not _close(recorded, expected_value):
                        problems.append(
                            f"{key}: summary {field} {recorded!r} != recomputed "
                            f"{expected_value!r}"
                        )
                expected_hours = declared_halt_hours(recomputed)
                if not _close(entry.get("declared_halt_hours"), expected_hours):
                    problems.append(
                        f"{key}: summary declared_halt_hours {entry.get('declared_halt_hours')!r} "
                        f"!= recomputed {expected_hours!r}"
                    )
                if (recomputed.get("cross_check_problems") or []):
                    problems.append(
                        f"{key}: the arm's own guard cross-check still reports "
                        f"{recomputed['cross_check_problems']!r}"
                    )
            geometry_path = run_dir / "risk_geometry.json"
            if geometry_path.exists():
                recorded_geometry = study.load_json(geometry_path)
                recomputed_geometry = geometry.build_geometry_artifact(run_dir, analysis)
                observed = recomputed_geometry.get("observed") or {}
                for field in GEOMETRY_FIELDS:
                    if not _close(entry.get(field), observed.get(field)):
                        problems.append(
                            f"{key}: summary {field} {entry.get(field)!r} != recomputed "
                            f"{observed.get(field)!r}"
                        )
                if recorded_geometry.get("cross_check_problems"):
                    problems.append(
                        f"{key}: risk_geometry still reports "
                        f"{recorded_geometry['cross_check_problems']!r}"
                    )
                if not _close(
                    entry.get("per_slot_cap"), recorded_geometry.get("per_slot_cap")
                ):
                    problems.append(
                        f"{key}: summary per_slot_cap {entry.get('per_slot_cap')!r} != "
                        f"risk_geometry {recorded_geometry.get('per_slot_cap')!r}"
                    )
                for halt in recomputed.get("halts") or []:
                    if str(halt.get("start", ""))[:16] not in document:
                        problems.append(
                            f"{key}: the synthesis does not list the halt starting "
                            f"{str(halt.get('start'))[:16]}"
                        )
            if analysis.get("hard_stop_triggers") and not entry.get("halt_count"):
                problems.append(
                    f"{key}: the engine reports triggers but the summary has no halt count"
                )

    verdicts = summary.get("verdicts") or {}
    rules = study.load_json(study.VARIANT_INPUT_PATH)["decision_rules"]
    _ = rules
    for rule in rules:
        ids = [key for key in verdicts if key == rule["id"] or key.startswith(f"{rule['id']}_")]
        if not ids:
            problems.append(f"no verdict recorded for {rule['id']}")
            continue
        if rule["id"] not in document:
            problems.append(f"the synthesis does not state the verdict for {rule['id']}")
    j1 = verdicts.get("J1") or {}
    recorded_checks = (summary.get("j1_occupancy") or {}).get("checks") or {}
    guard_key = (summary.get("j1_occupancy") or {}).get("guard")
    allow0_entry = summary.get("arms", {}).get("a_allow000__ext")
    guard_entry = summary.get("arms", {}).get(guard_key) if guard_key else None
    if recorded_checks and allow0_entry and guard_entry:
        expected = {
            "终值不低于基准 80%": (allow0_entry.get("gain_strategy_eq") or 0.0)
            >= 0.8 * (guard_entry.get("gain_strategy_eq") or 0.0),
            "最差回撤不更高": (allow0_entry.get("drawdown_worst_strategy_eq") or 0.0)
            <= (guard_entry.get("drawdown_worst_strategy_eq") or 0.0) + 1e-9,
            "峰值单币敞口更低": (allow0_entry.get("peak_coin_exposure") or 0.0)
            < (guard_entry.get("peak_coin_exposure") or 0.0),
            "单币归零上界更低": (allow0_entry.get("single_coin_wipeout_bound") or 0.0)
            < (guard_entry.get("single_coin_wipeout_bound") or 0.0),
        }
        for name, value in expected.items():
            if name not in recorded_checks or bool(recorded_checks[name]) is not bool(value):
                problems.append(
                    f"J1 check {name!r}: recorded {recorded_checks.get(name)!r} != recomputed "
                    f"{value!r}"
                )
        failed = [name for name, ok in expected.items() if not ok]
        expected_phrase = "占用纪律有效" if not failed else "占用纪律无效"
        if expected_phrase not in str(j1.get("verdict", "")):
            problems.append(
                f"J1 verdict {j1.get('verdict')!r} does not reflect the recomputed checks "
                f"{expected!r}"
            )
    for rule in rules:
        verdict = None
        for key, value in verdicts.items():
            if key == rule["id"] or key.startswith(f"{rule['id']}_"):
                verdict = value
                break
        if verdict and str(verdict.get("verdict", "")) not in document:
            problems.append(
                f"the synthesis does not state the {rule['id']} verdict "
                f"{verdict.get('verdict')!r}"
            )

    control_arms = summary.get("control_arms") or []
    if len(control_arms) != len(study.REFERENCE_RUNS):
        problems.append(
            f"the summary records {len(control_arms)} control arms, expected "
            f"{len(study.REFERENCE_RUNS)}"
        )
    for reference in control_arms:
        if not reference.get("matches_pin"):
            problems.append(
                f"control {reference.get('key')}: analysis sha256 "
                f"{reference.get('analysis_sha256')} != pinned {reference.get('pinned_sha256')}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-missing", action="store_true", help="tolerate missing arms")
    args = parser.parse_args(argv)

    if not SYNTHESIS_PATH.exists() or not SUMMARY_PATH.exists():
        fail(
            [f"missing {study.relative(SYNTHESIS_PATH)} or {study.relative(SUMMARY_PATH)}"],
            "the synthesis has not been built yet",
        )
    document = SYNTHESIS_PATH.read_text(encoding="utf-8")
    summary = study.load_json(SUMMARY_PATH)
    problems = verify(document, summary)
    if args.allow_missing:
        problems = [problem for problem in problems if "missing arms" not in problem]
    if problems:
        fail(problems, "the synthesis does not match the arms it summarises")
    print(
        f"synthesis verified: {len(summary.get('arms') or {})} arms, "
        f"{len(summary.get('verdicts') or {})} verdicts, {len(document):,} characters"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
