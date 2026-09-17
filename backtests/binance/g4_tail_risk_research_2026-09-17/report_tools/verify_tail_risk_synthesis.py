#!/usr/bin/env python3
"""Verify the study-level synthesis against the arms it summarises.

The synthesis (`tail_risk_analysis.md` + `artifacts/tail_risk_summary.json`) is the study's
headline document, so it gets the same treatment as a per-run report: this script recomputes
its numbers from the arms' own tracked artifacts (`analysis.json`, `tail_risk_events.json`,
`tail_risk_wipeout.json`, `run_record.json`) with code that does not import
`build_tail_risk_synthesis.py`, and checks the document's structure and honesty surface.

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

import variant_spec as study  # noqa: E402

SYNTHESIS_PATH = study.STUDY / "tail_risk_analysis.md"
SUMMARY_PATH = study.ARTIFACTS / "tail_risk_summary.json"
REQUIRED_HEADINGS = (
    "# g4 尾部风险研究",
    "## 一、问题与方法",
    "## 二、全期指标对比",
    "## 三、事件窗口压力表",
    "## 四、暴露上界",
    "## 五、HSL 熔断",
    "## 六、判据裁决",
    "## 七、对实盘的处置建议",
    "## 八、未覆盖风险与非目标",
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


def worst_event_drawdown(events_payload: dict[str, Any] | None) -> float | None:
    if not events_payload:
        return None
    values = [
        float(item["max_drawdown"])
        for item in events_payload.get("events", [])
        if item.get("max_drawdown") is not None
    ]
    return max(values) if values else None


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

    arms = summary.get("arms") or {}
    missing = summary.get("missing_arms") or []
    declared = set(study.RUN_VARIANT_ORDER)
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
            problems.append(f"{key}: not a declared arm")
            continue
        try:
            run_dir = study.find_variant_run_dir(variant)
        except SystemExit as exc:
            problems.append(f"{key}: {exc}")
            continue
        analysis = study.load_json(run_dir / "analysis.json")
        events_payload = (
            study.load_json(run_dir / "tail_risk_events.json")
            if (run_dir / "tail_risk_events.json").exists()
            else None
        )
        wipeout_payload = (
            study.load_json(run_dir / "tail_risk_wipeout.json")
            if (run_dir / "tail_risk_wipeout.json").exists()
            else None
        )
        if entry.get("run_dir") != study.relative(run_dir):
            problems.append(
                f"{key}: summary run_dir {entry.get('run_dir')!r} != {study.relative(run_dir)!r}"
            )
        for field, expected in (
            ("gain_strategy_eq", analysis.get("gain_strategy_eq")),
            ("drawdown_worst_strategy_eq", analysis.get("drawdown_worst_strategy_eq")),
            ("liquidated", analysis.get("liquidated")),
        ):
            recorded = entry.get(field)
            if expected is None and recorded is None:
                continue
            if recorded is None or not _close(recorded, expected):
                problems.append(f"{key}: summary {field} {recorded!r} != analysis {expected!r}")
        recorded_cagr = entry.get("cagr")
        expected_cagr = cagr(analysis)
        if expected_cagr is not None and not _close(recorded_cagr, expected_cagr, tol=1e-9):
            problems.append(f"{key}: summary cagr {recorded_cagr!r} != recomputed {expected_cagr!r}")
        recorded_event = entry.get("worst_event_drawdown")
        expected_event = worst_event_drawdown(events_payload)
        if expected_event is not None and not _close(recorded_event, expected_event, tol=1e-9):
            problems.append(
                f"{key}: summary worst_event_drawdown {recorded_event!r} != recomputed "
                f"{expected_event!r}"
            )
        if wipeout_payload:
            ruin = wipeout_payload.get("ruin") or {}
            for field in ("peak_total_exposure", "ruin_distance", "worst_coin_exposure"):
                if not _close(entry.get(field), ruin.get(field), tol=1e-9):
                    problems.append(
                        f"{key}: summary {field} {entry.get(field)!r} != wipeout {ruin.get(field)!r}"
                    )

    verdicts = summary.get("verdicts") or {}
    for rule in study.load_json(study.VARIANT_INPUT_PATH)["decision_rules"]:
        ids = [key for key in verdicts if key == rule["id"] or key.startswith(f"{rule['id']}_")]
        if not ids:
            problems.append(f"no verdict recorded for {rule['id']}")
            continue
        if f"### {rule['id']}" not in document and f"### {rule['id']}｜" not in document:
            if rule["id"] not in document:
                problems.append(f"the synthesis does not state the verdict for {rule['id']}")
    for leg, candidates in (summary.get("j1_candidates") or {}).items():
        control_key = f"off__{leg}"
        control = arms.get(control_key) or {}
        for candidate in candidates:
            arm = arms.get(candidate["arm"])
            if arm is None:
                problems.append(f"J1 candidate {candidate['arm']} is not in the summary")
                continue
            if control.get("cagr") is not None and arm.get("cagr") is not None:
                expected = (arm["cagr"] - control["cagr"]) * 100.0
                if not _close(candidate.get("delta_cagr_pp"), expected, tol=1e-6):
                    problems.append(
                        f"J1 {candidate['arm']}: delta {candidate.get('delta_cagr_pp')!r} != "
                        f"recomputed {expected!r}"
                    )
            if control.get("worst_event_drawdown") and arm.get("worst_event_drawdown") is not None:
                expected = 1.0 - arm["worst_event_drawdown"] / control["worst_event_drawdown"]
                if not _close(candidate.get("worst_event_reduction"), expected, tol=1e-6):
                    problems.append(
                        f"J1 {candidate['arm']}: reduction {candidate.get('worst_event_reduction')!r}"
                        f" != recomputed {expected!r}"
                    )

    references = summary.get("reference_arms") or []
    if not references:
        problems.append("the summary records no reference arms")
    for reference in references:
        if not reference.get("matches_pin"):
            problems.append(
                f"reference {reference.get('key')}: analysis sha256 {reference.get('analysis_sha256')}"
                f" != pinned {reference.get('pinned_sha256')}"
            )
    return problems


def _close(left: Any, right: Any, tol: float = 1e-12) -> bool:
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
