#!/usr/bin/env python3
"""Materialise a full backtest artifact set for the locked best candidate.

Offline only: no network downloads, no credentials, no exchange account, no bot start.
The candidate is reconstructed from `holdout_candidate_lock.json` ops applied to the
frozen baseline config that produced the reference `annual_analysis.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Repository root, resolved from this file so the study runs from any checkout location
# and any working directory: <repo>/backtests/binance/<study>/report_tools/<script>.py
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

STUDY = REPO / "backtests/binance/dd_tail_research_2026-09-15"
LOCK = STUDY / "holdout_candidate_lock.json"
CONTRACT = STUDY / "research_contract.json"
BASELINE_CONFIG = REPO / "backtests/binance/2026-09-14T03_40_41/config.json"
BASELINE_DATASET = REPO / "backtests/binance/2026-09-14T03_40_41/dataset.json"
ARTIFACTS = STUDY / "artifacts"
RESULTS_BASE = ARTIFACTS / "backtest_results"
# Files that must exist before a run counts as usable. The plotting tail of the CLI can be
# OOM-killed on a small host; the analytical artifacts are already complete at that point.
REQUIRED_ARTIFACTS = (
    "analysis.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
    "config.json",
    "dataset.json",
    "balance_and_equity.png",
    "total_wallet_exposure.png",
    "pnl_cumsum.png",
)
CANDIDATE_CONFIG = ARTIFACTS / "candidate.config.json"
RUN_RECORD = ARTIFACTS / "run_record.json"
AUDIT_PATH = ARTIFACTS / "execution_audit.csv"
RUN_LOG = ARTIFACTS / "backtest_run.log"
CANDIDATE_ID = "combo_twel100_ddf060_ddthr0030"
STUDY_CELL = STUDY / "cells/full/C3_conservative" / CANDIDATE_ID


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    with path.open() as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    os.replace(tmp, path)


def get_path(cfg: dict, dotted: str) -> Any:
    node = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def set_path(cfg: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def build_candidate_config() -> tuple[dict, list[dict]]:
    from config_utils import load_config

    lock = load_json(LOCK)
    candidates = {c["cell_id"]: c for c in lock["candidates"]}
    if CANDIDATE_ID not in candidates:
        raise SystemExit(f"candidate {CANDIDATE_ID!r} is not present in the lock")
    entry = candidates[CANDIDATE_ID]

    cfg = load_config(str(BASELINE_CONFIG), verbose=False)
    applied = []
    for op in entry["ops"]:
        current = get_path(cfg, op["path"])
        if current != op["baseline"]:
            raise SystemExit(
                f"baseline drift on {op['path']}: config has {current!r}, lock recorded {op['baseline']!r}"
            )
        set_path(cfg, op["path"], op["value"])
        applied.append({"path": op["path"], "from": current, "to": op["value"]})

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    RESULTS_BASE.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    cfg["backtest"]["exchanges"] = ["binance"]
    cfg["backtest"]["execution_delay_bars"] = 1
    cfg["backtest"]["intrabar_fill_order"] = "close_first"
    cfg["backtest"]["maker_fee_override"] = 0.0006
    cfg["backtest"]["taker_fee_override"] = 0.0008
    # Minute-resolution balance/equity series: drawdowns must be computed on the same
    # resolution as `analysis.json` (the reference report runs at minute resolution too).
    cfg["backtest"]["balance_sample_divider"] = 1
    cfg["backtest"]["base_dir"] = str(RESULTS_BASE)
    cfg["backtest"]["execution_audit_path"] = str(AUDIT_PATH)
    # The archived baseline config predates `backtest.coins`; pin the frozen basket explicitly so
    # the run cannot depend on market-discovery ordering.
    basket = sorted(set(load_json(BASELINE_DATASET)["coins"]))
    study_coins = sorted(set(load_json(STUDY_CELL / "result.json")["coins"]))
    if basket != study_coins:
        raise SystemExit(
            "frozen basket mismatch between baseline dataset and study cell: "
            f"dataset-only={sorted(set(basket) - set(study_coins))} "
            f"cell-only={sorted(set(study_coins) - set(basket))}"
        )
    cfg["backtest"]["coins"] = {"binance": basket}
    cfg.setdefault("live", {})["approved_coins"] = {"long": list(basket), "short": []}

    CANDIDATE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CANDIDATE_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=4, sort_keys=True) + "\n")
    os.replace(tmp, CANDIDATE_CONFIG)

    reloaded = load_config(str(CANDIDATE_CONFIG), verbose=False)
    for op in entry["ops"]:
        if get_path(reloaded, op["path"]) != op["value"]:
            raise SystemExit(f"round-trip validation failed for {op['path']}")
    return cfg, applied


def rust_identity() -> dict[str, Any]:
    import rust_utils

    fingerprint = rust_utils.source_fingerprint()
    return {
        "expected_source_fingerprint": fingerprint,
        "compiled_path": str(rust_utils.preferred_compiled_path() or ""),
    }


def run_backtest(cfg: dict) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    cmd = [sys.executable, "-m", "backtest", str(CANDIDATE_CONFIG)]
    with RUN_LOG.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc.returncode


def find_result_dir() -> Path:
    root = RESULTS_BASE / "binance"
    if not root.is_dir():
        raise SystemExit(f"no results directory at {root}")
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if len(dirs) != 1:
        raise SystemExit(f"expected exactly one result dir under {root}, found {[p.name for p in dirs]}")
    return dirs[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-run", action="store_true")
    args = parser.parse_args()

    started = time.time()
    contract = load_json(CONTRACT)
    cfg, applied = build_candidate_config()
    config_sha = sha256_file(CANDIDATE_CONFIG)
    print(f"candidate config written: {CANDIDATE_CONFIG}")
    print(f"  sha256={config_sha}")
    for op in applied:
        print(f"  {op['path']}: {op['from']!r} -> {op['to']!r}")

    rc = 0
    artifact_status = "not_run"
    if not args.skip_run:
        print("running backtest ...", flush=True)
        rc = run_backtest(cfg)
        print(f"backtest exit code: {rc} (log: {RUN_LOG})")
        if rc != 0:
            result_dir = find_result_dir()
            missing = [name for name in REQUIRED_ARTIFACTS if not (result_dir / name).exists()]
            if missing:
                tail = RUN_LOG.read_text().splitlines()[-40:]
                print("\n".join(tail))
                raise SystemExit(f"backtest failed with exit code {rc}; missing artifacts: {missing}")
            artifact_status = (
                f"partial_exit_code_{rc}: all required analytical artifacts were written before the "
                "process was killed (matplotlib plotting tail only)"
            )
            print(f"warning: {artifact_status}")

    result_dir = find_result_dir()
    audit_rows = None
    if AUDIT_PATH.exists():
        with AUDIT_PATH.open() as handle:
            audit_rows = sum(1 for _ in handle) - 1

    record = {
        "candidate_id": CANDIDATE_ID,
        "candidate_ops": applied,
        "candidate_config": str(CANDIDATE_CONFIG.relative_to(REPO)),
        "candidate_config_sha256": config_sha,
        "source_config": str(BASELINE_CONFIG.relative_to(REPO)),
        "source_config_sha256": sha256_file(BASELINE_CONFIG),
        "lock_sha256": sha256_file(LOCK),
        "contract_sha256": sha256_file(CONTRACT),
        "contract_cell_matrix_sha256": contract["cell_matrix_sha256"],
        "execution": {
            "execution_delay_bars": cfg["backtest"]["execution_delay_bars"],
            "intrabar_fill_order": cfg["backtest"]["intrabar_fill_order"],
        },
        "costs": {
            "maker_fee_override": cfg["backtest"]["maker_fee_override"],
            "taker_fee_override": cfg["backtest"]["taker_fee_override"],
        },
        "window": {
            "start_date": cfg["backtest"]["start_date"],
            "end_date": cfg["backtest"]["end_date"],
        },
        "universe": {
            "exchange": "binance",
            "coins": cfg["backtest"]["coins"]["binance"],
            "coin_count": len(cfg["backtest"]["coins"]["binance"]),
        },
        "results_dir": str(result_dir.relative_to(REPO)),
        "execution_audit_rows": audit_rows,
        "backtest_exit_code": rc,
        "artifact_status": artifact_status,
        "rust_identity": rust_identity(),
        "elapsed_s": time.time() - started,
        "safety": {
            "network_downloads": False,
            "credentials": False,
            "exchange_account_or_orders": False,
            "bot_start": False,
        },
    }
    write_json(RUN_RECORD, record)
    print(f"run record written: {RUN_RECORD}")
    print(f"results dir: {result_dir}")
    for name in sorted(p.name for p in result_dir.iterdir()):
        print(f"  {name}")


if __name__ == "__main__":
    main()