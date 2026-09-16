#!/usr/bin/env python3
"""Run the frozen `hsl_npos1` backtest and file the run under the study's artifact tree.

Offline only. The run config pins the frozen local HLCV catalog as its dataset override, so
`python -m backtest` reads local candles and never contacts an exchange.

Usage:
    PYTHONPATH=src python report_tools/run_backtest.py [--force]

The backtest names its run directory from the UTC completion timestamp and writes it under
`<backtest.base_dir>/<exchange>/`, and this study keeps the profile's own `base_dir` so the run
lands in `backtests/binance/<timestamp>/`. The runner snapshots that directory first, runs, then
moves the single new run directory into `artifacts/backtest_results/binance/<timestamp>/`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hsl_npos1_spec as study  # noqa: E402


def snapshot_runs() -> set[str]:
    return {str(path) for path in study.dated_run_dirs(study.RUN_SOURCE_BASE)}


def new_run_dir(before: set[str]) -> Path:
    after = {str(path) for path in study.dated_run_dirs(study.RUN_SOURCE_BASE)}
    fresh = sorted(after - before)
    if not fresh:
        raise SystemExit(
            f"no new run directory appeared under {study.relative(study.RUN_SOURCE_BASE)}"
        )
    if len(fresh) > 1:
        raise SystemExit(f"expected one new run directory, found {fresh}")
    return Path(fresh[0])


def run_backtest() -> None:
    if not study.CONFIG_PATH.is_file():
        raise SystemExit(
            f"{study.relative(study.CONFIG_PATH)} is missing; run build_run_config.py first"
        )
    study.EXECUTION_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The backtest opens the execution-audit CSV with create-new semantics, so a re-run into an
    # existing bundle fails instead of overwriting. This tool owns the file: a stale audit must
    # never be mistaken for the current run's provenance.
    study.EXECUTION_AUDIT_PATH.unlink(missing_ok=True)
    study.RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(study.REPO / "src")
    cmd = [sys.executable, "-m", "backtest", str(study.CONFIG_PATH)]
    print("$ " + " ".join(cmd), flush=True)
    started = time.monotonic()
    with study.RUN_LOG_PATH.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=str(study.REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    elapsed = time.monotonic() - started
    print(f"backtest exit={proc.returncode} elapsed={elapsed:,.1f}s log={study.relative(study.RUN_LOG_PATH)}")
    if proc.returncode != 0:
        tail = study.RUN_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        raise SystemExit("backtest failed:\n" + "\n".join(tail))


def file_run(source: Path) -> Path:
    study.RUNS_BASE.mkdir(parents=True, exist_ok=True)
    target = study.RUNS_BASE / source.name
    if target.exists():
        shutil.rmtree(target)
    print(f"filing run: {study.relative(source)} -> {study.relative(target)}")
    shutil.move(str(source), str(target))
    # Only an emptied, timestamp-named source directory is removed; anything else is left alone.
    exchange_level = source.parent
    if exchange_level.is_dir() and not any(exchange_level.iterdir()):
        exchange_level.rmdir()
    return target


def report_run(result_dir: Path) -> int:
    missing = [name for name in study.REQUIRED_ARTIFACTS if not (result_dir / name).exists()]
    if missing:
        raise SystemExit(f"run directory {study.relative(result_dir)} is missing {missing}")
    analysis = study.load_json(result_dir / "analysis.json")
    # `dataset.json` is the authority for the run basket: `backtest.coins` is consumed during
    # preparation and is not present in the written run config.
    dataset = study.load_json(result_dir / "dataset.json")
    coins = list(dataset.get("coins") or [])
    completion = float(analysis.get("backtest_completion_ratio") or 0.0)
    print(f"result dir        : {study.relative(result_dir)}")
    print(
        f"effective window  : {analysis.get('effective_start_date')} .. "
        f"{analysis.get('effective_end_date')} ({float(analysis.get('n_days') or 0.0):,.1f} days)"
    )
    print(f"coins             : {len(coins)}")
    print(f"excluded          : {sorted(study.EXCLUDED_COINS)}")
    print(f"cache             : {dataset.get('cache_dir_label')}")
    print(f"completion ratio  : {completion:.6f}")
    print(f"liquidated        : {analysis.get('liquidated')}")
    print(f"fills             : {int(analysis.get('fills_count') or 0):,}")
    print(
        f"drawdown worst    : strategy_eq {float(analysis.get('drawdown_worst_strategy_eq') or 0.0):.4f} "
        f"/ usd {float(analysis.get('drawdown_worst_usd') or 0.0):.4f}"
    )
    print(f"execution audit   : {study.relative(study.EXECUTION_AUDIT_PATH)}")
    if completion < 1.0:
        raise SystemExit(
            f"backtest did not complete: backtest_completion_ratio={completion}. "
            "Report rendering requires a fully completed run."
        )
    if analysis.get("liquidated"):
        raise SystemExit("backtest simulated a liquidation; the run is not reportable as-is")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run and replace the bundle's existing run instead of reporting it",
    )
    args = parser.parse_args(argv)

    existing = [
        path
        for path in study.dated_run_dirs(study.RUNS_BASE)
        if (path / "analysis.json").exists()
    ]
    if existing and not args.force:
        print(f"artifact already present: {study.relative(existing[-1])} (use --force to re-run)")
        return report_run(existing[-1])
    # A bundle holds one run directory and the backtest names it from the UTC completion
    # timestamp, so --force replaces the bundle's run instead of leaving a second one that the
    # report tooling would refuse to pick between.
    for path in existing:
        print(f"removing previous run: {study.relative(path)}")
        shutil.rmtree(path)

    before = snapshot_runs()
    run_backtest()
    return report_run(file_run(new_run_dir(before)))


if __name__ == "__main__":
    raise SystemExit(main())
