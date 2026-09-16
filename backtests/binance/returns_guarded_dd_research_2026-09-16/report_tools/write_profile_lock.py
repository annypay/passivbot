#!/usr/bin/env python3
"""Write the profile decision lock for the frozen holdout comparison.

The lock is written **before** any locked window runs. It fixes which cells compete, the
gates they must clear, and the ranking order, so the holdout cannot be used to pick the
gates. The study runner already refuses `holdout` / `fresh` / `stress` while
`holdout_candidate_lock.json` is absent, so writing this file is the single event that
opens them.

Usage:
    venv/bin/python report_tools/write_profile_lock.py [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
STUDY = Path(__file__).resolve().parents[1]
if str(STUDY / "report_tools") not in sys.path:
    sys.path.insert(0, str(STUDY / "report_tools"))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import run_study as study  # noqa: E402

LOCK_PATH = STUDY / "holdout_candidate_lock.json"
DECISION_PATH = STUDY / "artifacts" / "profile_decision_lock.json"

# The competing cells and why each is here. `g4_sma20_50` is the drawdown-efficiency leader
# and the only gate that holds in both cost regimes; `g3_cth0000` is the return leader's
# family and the cell under discussion; `g5_tw090_ddf070` is the best return inside a 30%
# drawdown cap; `published` is the already-published baseline these must beat.
CANDIDATES: dict[str, str] = {
    "g4_sma20_50": "drawdown-efficiency leader; gate holds under the harsher cost regime",
    "g3_cth0000": "return family under discussion (single-parameter variant of the seed)",
    "g5_tw090_ddf070": "best return inside a 30% drawdown cap at the reported contract",
    "published": "the already-published lower-tail profile, as the baseline to beat",
}

GATES = {
    "completion_ratio_min": 0.999,
    "liquidated_must_be_false": True,
    "mdd_max": 0.30,
    "applies_to_windows": ["holdout", "fresh"],
    "ranking": [
        "minimise worst(holdout MDD, fresh MDD)",
        "then maximise CAGR / MDD on the full window",
        "then maximise CAGR on the full window",
    ],
    "no_candidate_passes_conclusion": (
        "if no candidate clears mdd_max on BOTH holdout and fresh, the study has no "
        "publish-grade configuration and the comparison must say so"
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if DECISION_PATH.exists() and not args.force:
        raise SystemExit(
            f"refusing to overwrite the frozen decision lock {DECISION_PATH} (use --force)"
        )

    fingerprint = study.stamp_rust_extension()
    contract = study.load_json(STUDY / "research_contract.json")

    candidates = []
    for cell_id, reason in CANDIDATES.items():
        candidates.append(
            {
                "cell_id": cell_id,
                "reason": reason,
                "config_sha256": study.cell_config_sha256(
                    cell_id, "full", study.PRIMARY_SCENARIO
                ),
            }
        )

    lock = {
        "purpose": (
            "holdout comparison of the study's frontier candidates, to decide whether any "
            "configuration is publish-grade"
        ),
        "written_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "written_before_any_locked_window_run": True,
        "engine_fingerprint": fingerprint,
        "contract": {
            "path": str((STUDY / "research_contract.json").relative_to(REPO)),
            "cell_matrix_sha256": contract["cell_matrix_sha256"],
        },
        "windows": {
            name: list(study.WINDOWS[name])
            for name in ("holdout", "fresh", "stress", "full")
        },
        "execution_and_costs": {
            "scenario": study.PRIMARY_SCENARIO,
            "execution": study.PRIMARY_EXECUTION,
            "costs": study.PRIMARY_COSTS,
        },
        "candidates": candidates,
        "gates": GATES,
        "safety": {
            "network": False,
            "credentials": False,
            "exchange_account_or_orders": False,
            "bot_start": False,
        },
    }

    study.write_json(DECISION_PATH, lock)
    print(f"wrote {DECISION_PATH.relative_to(REPO)}")
    print(f"engine fingerprint: {fingerprint[:16]}")

    # The runner's locked-window guard reads this file, so writing it is what opens the
    # holdout. It carries the same candidate set so the two cannot disagree.
    study.write_json(LOCK_PATH, lock)
    print(f"wrote {LOCK_PATH.relative_to(REPO)}  (this opens holdout/fresh/stress)")
    for candidate in candidates:
        print(f"  {candidate['cell_id']:<20} {candidate['config_sha256'][:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
