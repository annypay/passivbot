#!/usr/bin/env python3
"""Conventional entry point for the independent recomputation of this study's verdicts.

The recomputation lives in `sl_replay.py analyse`: it reads only the run directories' own
`analysis.json` / `fills.csv` / `balance_and_equity.csv.gz` and the Phase A census, never the
build step's intermediate state. This file exists so the previous round's tool name still resolves.
See `report_tools/README.md` for the layout deviation.

    python report_tools/verify_variant_report.py --strict
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sl_replay  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    return sl_replay.main(["analyse", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    sys.exit(main())
