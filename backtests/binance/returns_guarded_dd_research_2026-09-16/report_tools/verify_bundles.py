#!/usr/bin/env python3
"""Independently verify every bundle's deep-analysis report.

Each bundle records the cell it replays, so this resolves the comparison cell from
`run_record.json` and drives the repository's report verifier, which recomputes every reported
number from `fills.csv` and `balance_and_equity.csv.gz` with code that does not import the
report generator.

This study performs no candidate lock (it compares frontier cells rather than a locked
candidate), so the lock-derived checks are skipped explicitly.

Exit code is non-zero when any report fails verification.

Usage:
    venv/bin/python report_tools/verify_bundles.py [--bundle NAME ...]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
STUDY = Path(__file__).resolve().parents[1]
ARTIFACTS = STUDY / "artifacts"
VERIFIER = (
    REPO
    / "backtests/binance/dd_tail_research_2026-09-15/report_tools/verify_annual_report.py"
)
SCENARIO = "C1_binance_actual"


def bundle_names() -> list[str]:
    if not ARTIFACTS.is_dir():
        return []
    return sorted(
        path.name
        for path in ARTIFACTS.iterdir()
        if path.is_dir() and (path / "run_record.json").exists()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", action="append", default=None)
    args = parser.parse_args()

    names = args.bundle or bundle_names()
    if not names:
        raise SystemExit(f"no bundles under {ARTIFACTS}")

    failures = 0
    for subdir in names:
        record_path = ARTIFACTS / subdir / "run_record.json"
        if not record_path.exists():
            print(f"[SKIP] {subdir}: no run record")
            failures += 1
            continue
        record = json.loads(record_path.read_text())
        window = (record.get("window") or {}).get("name", "full")
        scenario = record.get("scenario", SCENARIO)
        cell = record.get("cell_id")
        if not cell:
            print(f"[SKIP] {subdir}: run record names no cell")
            failures += 1
            continue
        study_cell = f"cells/{window}/{scenario}/{cell}/result.json"
        if not (STUDY / study_cell).exists():
            print(f"[SKIP] {subdir}: cell record missing at {study_cell}")
            failures += 1
            continue

        proc = subprocess.run(
            [
                sys.executable,
                str(VERIFIER),
                "--study",
                str(STUDY.relative_to(REPO)),
                "--artifacts-subdir",
                subdir,
                "--study-cell",
                study_cell,
                "--no-expect-locked-ops",
                "--no-require-lock",
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        )
        tail = [line for line in proc.stdout.splitlines() if line.startswith("total checks")]
        fails = [
            line for line in proc.stdout.splitlines() if line.strip().startswith("[FAIL]")
        ]
        status = "PASS" if proc.returncode == 0 else "FAIL"
        print(f"[{status}] {subdir}: {cell}  {tail[0] if tail else ''}")
        for line in fails:
            print(f"        {line.strip()}")
        if proc.returncode != 0:
            failures += 1
            for line in (proc.stderr or "").strip().splitlines()[-4:]:
                print(f"        stderr: {line}")

    print()
    print(f"bundles verified: {len(names)}  failures: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
