#!/usr/bin/env python3
"""Conventional entry point for the default-off identity gate of this study.

The gate itself lives in `sl_replay.py identity`; this file exists so an operator (or a reviewer)
looking for the previous round's tool name finds it. See `report_tools/README.md` for why this
round folds the previous round's six-file harness into four subcommands.

    python report_tools/default_off_identity.py --legs 3y ext pre
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sl_replay  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    return sl_replay.main(["identity", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    sys.exit(main())
