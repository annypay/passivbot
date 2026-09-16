#!/usr/bin/env python3
"""Check every bundle run directory against the report convention's layout contract.

`backtests/report_spec/annual_analysis.py` carries the persisted-bundle layout contract: which
files must live in the run directory for a report's numbers to stay re-derivable, that the run
directory is named and shaped by the convention, and that the figure set matches the run's own
`disable_plotting` rather than a guess.

The contract reads `backtest.disable_plotting` and `backtest.execution_audit_path` from the run's
own `config.json`, so a run has to describe itself for this to pass. That is the point: a bundle
that relied on a CLI-only flag to explain a missing panel is not self-describing.

Exit code is non-zero when any bundle is incomplete.

Usage:
    venv/bin/python report_tools/check_bundle_layout.py [--bundle NAME ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
REPORT_SPEC = REPO / "backtests" / "report_spec"
if str(REPORT_SPEC) not in sys.path:
    sys.path.insert(0, str(REPORT_SPEC))

import annual_analysis as spec  # noqa: E402

STUDY = Path(__file__).resolve().parents[1]
ARTIFACTS = STUDY / "artifacts"


def bundle_names() -> list[str]:
    if not ARTIFACTS.is_dir():
        return []
    return sorted(
        path.name
        for path in ARTIFACTS.iterdir()
        if path.is_dir() and (path / "run_record.json").exists()
    )


def run_dir(subdir: str) -> Path | None:
    base = ARTIFACTS / subdir / "backtest_results"
    if not base.is_dir():
        return None
    dirs = sorted(p for p in base.glob("*/binance*/*") if p.is_dir() and p.name[:2].isdigit())
    return dirs[-1] if dirs else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", action="append", default=None)
    args = parser.parse_args()

    names = args.bundle or bundle_names()
    if not names:
        raise SystemExit(f"no bundles under {ARTIFACTS}")

    problems_total = 0
    for subdir in names:
        directory = run_dir(subdir)
        if directory is None:
            print(f"[SKIP] {subdir}: no run directory")
            problems_total += 1
            continue
        config = json.loads((directory / "config.json").read_text())
        disabled = spec.disabled_plot_groups(config)
        # The contract derives the expected figure set from the run's own config, and the
        # per-coin panels are expected only when `coin_fills` is not among the disabled groups.
        expect_plots = "coin_fills" not in disabled
        problems = spec.assert_bundle_layout(
            directory, config=config, expect_plots=expect_plots
        )
        record_path = ARTIFACTS / subdir / "run_record.json"
        exit_code = None
        if record_path.exists():
            exit_code = json.loads(record_path.read_text()).get("backtest_exit_code")
        status = "PASS" if not problems else f"FAIL ({len(problems)})"
        print(
            f"[{status}] {subdir}: {directory.name}  disabled={sorted(disabled) or 'none'} "
            f"exit={exit_code}"
        )
        for problem in problems:
            print(f"        {problem}")
        problems_total += len(problems)

    print()
    print(f"bundles checked: {len(names)}  layout problems: {problems_total}")
    return 1 if problems_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
