#!/usr/bin/env python3
"""Verify the account-guard synthesis against the arms it summarises.

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

import guard_analysis as guard  # noqa: E402
import variant_spec as study  # noqa: E402

SYNTHESIS_PATH = study.STUDY / "account_guard_analysis.md"
SUMMARY_PATH = study.ARTIFACTS / "account_guard_summary.json"
REQUIRED_HEADINGS = (
    "# g4 @ TWE 3.0 的账户级守护实测",
    "## 一、问题、条件映射与方法",
    "## 二、全期指标对比",
    "## 三、守护读数对照",
    "## 四、整装待发读数",
    "## 五、强平与存活",
    "## 六、事件窗口压力表",
    "## 七、暴露上界与冲击损失矩阵",
    "## 八、判据裁决",
    "## 九、结论与建议",
    "## 十、边界",
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
    for phrase in ("账户级守护", "整装待发", "阶梯"):
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
    for rule in rules:
        ids = [key for key in verdicts if key == rule["id"] or key.startswith(f"{rule['id']}_")]
        if not ids:
            problems.append(f"no verdict recorded for {rule['id']}")
            continue
        if rule["id"] not in document:
            problems.append(f"the synthesis does not state the verdict for {rule['id']}")
    j1 = verdicts.get("J1") or {}
    survivors = (summary.get("j1") or {}).get("ext_survivors")
    if survivors is not None:
        expected_phrase = "有守护臂活下来" if survivors else "仍被强平"
        if expected_phrase not in str(j1.get("verdict", "")):
            problems.append(
                f"J1 verdict {j1.get('verdict')!r} does not reflect the derived survivor list "
                f"{survivors}"
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
