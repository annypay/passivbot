#!/usr/bin/env python3
"""Verify the study-level synthesis against the arms it summarises.

The synthesis (`twe300_10k_analysis.md` + `artifacts/twe300_10k_summary.json`) is the study's
headline document, so it gets the same treatment as a per-run report: this script recomputes
its numbers from the arms' own tracked artifacts (`analysis.json`, `run_record.json`,
`tail_risk_events.json`, `tail_risk_wipeout.json`) with code that does not import
`build_synthesis.py`, and checks the document's structure, the capital/liquidation contract
and the honesty surface.

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

SYNTHESIS_PATH = study.STUDY / "twe300_10k_analysis.md"
SUMMARY_PATH = study.ARTIFACTS / "twe300_10k_summary.json"
REQUIRED_HEADINGS = (
    "# g4 门控 profile @ TWE 3.0 / 10,000 USDT",
    "## 一、问题与方法",
    "## 二、全期指标对比",
    "## 三、强平读数",
    "## 四、事件窗口压力表",
    "## 五、资金规模对照",
    "## 六、暴露上界与冲击损失矩阵",
    "## 七、HSL 熔断",
    "## 八、实盘准入",
    "## 九、判据裁决",
    "## 十、建议",
    "## 十一、边界",
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
            reference = next((item for item in study.REFERENCE_RUNS.items() if item[0] == key), None)
            if reference is None:
                problems.append(f"{key}: not a declared arm")
                continue
            run_dir = Path(reference[1]["run_dir"])
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
        wipeout_path = run_dir / "tail_risk_wipeout.json"
        if wipeout_path.exists():
            ruin = (study.load_json(wipeout_path).get("ruin")) or {}
            for field in ("peak_total_exposure", "worst_coin_exposure", "liquidation_shock_at_peak"):
                if ruin.get(field) is None:
                    continue
                if entry.get(field) is None and key in study.REFERENCE_RUNS:
                    # A pinned anchor from an earlier study carries no capital/liquidation
                    # geometry in this study's summary; the table states its settings instead.
                    continue
                if not _close(entry.get(field), ruin.get(field)):
                    problems.append(
                        f"{key}: summary {field} {entry.get(field)!r} != wipeout {ruin.get(field)!r}"
                    )
        if variant is not None:
            if not _close(entry.get("starting_balance"), variant.starting_balance):
                problems.append(
                    f"{key}: summary starting_balance {entry.get('starting_balance')!r} != declared "
                    f"{variant.starting_balance!r}"
                )
            if not _close(entry.get("declared_twe"), variant.declared_twe):
                problems.append(
                    f"{key}: summary declared_twe {entry.get('declared_twe')!r} != declared "
                    f"{variant.declared_twe!r}"
                )
            if analysis.get("liquidated") and not entry.get("days_to_liquidation"):
                problems.append(f"{key}: liquidated arm records no days_to_liquidation")

    verdicts = summary.get("verdicts") or {}
    rules = study.load_json(study.VARIANT_INPUT_PATH)["decision_rules"]
    for rule in rules:
        ids = [key for key in verdicts if key == rule["id"] or key.startswith(f"{rule['id']}_")]
        if not ids:
            problems.append(f"no verdict recorded for {rule['id']}")
            continue
        if rule["id"] not in document:
            problems.append(f"the synthesis does not state the verdict for {rule['id']}")
    for leg, data in (summary.get("j2") or {}).items():
        if data.get("baseline_drawdown") and data.get("floor_drawdown"):
            reduction = data["floor_drawdown"] / data["baseline_drawdown"]
            if (reduction <= 0.6 and not data.get("floor_liquidated")) and "60%" not in document:
                problems.append(f"J2_{leg}: a passing verdict is not reflected in the document")

    references = summary.get("reference_arms") or []
    if len(references) != len(study.REFERENCE_RUNS):
        problems.append(
            f"the summary records {len(references)} reference arms, expected "
            f"{len(study.REFERENCE_RUNS)}"
        )
    for reference in references:
        if not reference.get("matches_pin"):
            problems.append(
                f"reference {reference.get('key')}: analysis sha256 "
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
