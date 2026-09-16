#!/usr/bin/env python3
"""Materialise a full backtest artifact set for the locked best candidate.

Offline only: no network downloads, no credentials, no exchange account, no bot start.
The candidate is reconstructed from `holdout_candidate_lock.json` ops applied to a maintained
baseline config -- by default `configs/examples/default_trailing_martingale_long.json`, which the
lock was recorded against. Applying the ops is requested explicitly with `--apply-locked-ops`
rather than inferred from the path, because the archived config that produced the reference
`annual_analysis.md` and the published default profile carry identical strategy parameters.
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
CONTRACT = STUDY / "research_contract_v4.json"
DEFAULT_CANDIDATE_CONFIG = None  # resolved in resolve_paths()
sys.path.insert(0, str(STUDY / "report_tools"))
import run_tail_drawdown_study as study  # noqa: E402  (single source for the reported contract)
BASELINE_CONFIG = REPO / "backtests/binance/2026-09-14T03_40_41/config.json"
BASELINE_DATASET = REPO / "backtests/binance/2026-09-14T03_40_41/dataset.json"
ARTIFACTS = STUDY / "artifacts"
RESULTS_BASE = ARTIFACTS / "backtest_results"
DEFAULT_ARTIFACTS_SUBDIR = "binance_actual_candidate"
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
CANDIDATE_ID = "combo_twel100_ddf060_ddthr0030"
# Runtime paths; `resolve_paths()` fills these from the CLI so one tool can produce artifacts
# for the locked candidate and for any published profile without duplicating the contract.
ARTIFACTS = STUDY / "artifacts" / DEFAULT_ARTIFACTS_SUBDIR
RESULTS_BASE = ARTIFACTS / "backtest_results"
CANDIDATE_CONFIG = ARTIFACTS / "candidate.config.json"
RUN_RECORD = ARTIFACTS / "run_record.json"
AUDIT_PATH = ARTIFACTS / "execution_audit.csv"
RUN_LOG = ARTIFACTS / "backtest_run.log"
CONFIG_SOURCE = BASELINE_CONFIG
STUDY_CELL = STUDY / "cells" / "full" / study.PRIMARY_SCENARIO / CANDIDATE_ID


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


def build_candidate_config(
    apply_locked_ops: bool,
    study_window: str | None = None,
    label: str | None = None,
    disable_plotting: str | None = None,
) -> tuple[dict, list[dict]]:
    from config_utils import load_config

    cfg = load_config(str(CONFIG_SOURCE), verbose=False)
    applied: list[dict] = []
    if apply_locked_ops:
        lock = load_json(LOCK)
        candidates = {c["cell_id"]: c for c in lock["candidates"]}
        if CANDIDATE_ID not in candidates:
            raise SystemExit(f"candidate {CANDIDATE_ID!r} is not present in the lock")
        entry = candidates[CANDIDATE_ID]
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
    if study_window:
        # A published profile carries its own window (usually `end_date: now`). Pinning the study
        # window makes the artifact directly comparable with the study's own cells instead of
        # merely similar; the window is recorded in the run record either way.
        start, end = study.WINDOWS[study_window]
        cfg["backtest"]["start_date"] = start
        cfg["backtest"]["end_date"] = end
    # The reported contract comes from the study tool so config and evidence cannot drift.
    cfg["backtest"]["execution_delay_bars"] = study.PRIMARY_EXECUTION["execution_delay_bars"]
    cfg["backtest"]["intrabar_fill_order"] = study.PRIMARY_EXECUTION["intrabar_fill_order"]
    cfg["backtest"]["maker_fee_override"] = study.PRIMARY_COSTS["maker_fee_override"]
    cfg["backtest"]["taker_fee_override"] = study.PRIMARY_COSTS["taker_fee_override"]
    # Minute-resolution balance/equity series: drawdowns must be computed on the same
    # resolution as `analysis.json` (the reference report runs at minute resolution too).
    cfg["backtest"]["balance_sample_divider"] = 1
    if disable_plotting:
        cfg["backtest"]["disable_plotting"] = disable_plotting
    cfg["backtest"]["base_dir"] = str(RESULTS_BASE)
    # The backtest names the run directory from the completion timestamp and appends its own
    # `label` argument, so a labeled run lands in `<results>/binance_<label>/<timestamp>/`.
    # The `binance` level is what the result-directory lookup keys on, so the label has to
    # replace it rather than the parent `backtest_results` directory.
    cfg["backtest"]["base_dir"] = str(
        RESULTS_BASE / f"binance_{label}" if label else RESULTS_BASE
    )
    cfg["backtest"]["execution_audit_path"] = str(AUDIT_PATH)
    # The archived baseline config predates `backtest.coins`; pin the frozen dataset basket
    # explicitly so the run cannot depend on market-discovery ordering.
    basket = sorted(set(load_json(BASELINE_DATASET)["coins"]))
    configured = sorted(set(cfg["backtest"].get("coins", {}).get("binance") or basket))
    dropped = sorted(set(configured) - set(basket))
    if dropped:
        print(f"warning: configured coins without frozen data are dropped: {dropped}")
    missing = sorted(set(basket) - set(configured))
    if missing:
        print(f"warning: frozen dataset coins not configured in the profile: {missing}")
    cfg["backtest"]["coins"] = {"binance": basket}
    cfg.setdefault("live", {})["approved_coins"] = {"long": list(basket), "short": []}

    CANDIDATE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CANDIDATE_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=4, sort_keys=True) + "\n")
    os.replace(tmp, CANDIDATE_CONFIG)

    reloaded = load_config(str(CANDIDATE_CONFIG), verbose=False)
    if apply_locked_ops:
        for op in entry["ops"]:
            if get_path(reloaded, op["path"]) != op["value"]:
                raise SystemExit(f"round-trip validation failed for {op['path']}")
    for key, expected in (
        ("maker_fee_override", study.PRIMARY_COSTS["maker_fee_override"]),
        ("taker_fee_override", study.PRIMARY_COSTS["taker_fee_override"]),
        ("execution_delay_bars", study.PRIMARY_EXECUTION["execution_delay_bars"]),
    ):
        if reloaded["backtest"].get(key) != expected:
            raise SystemExit(f"reported contract drift on backtest.{key}: {reloaded['backtest'].get(key)!r}")
    return cfg, applied


def rust_identity() -> dict[str, Any]:
    import rust_utils

    fingerprint = rust_utils.source_fingerprint()
    return {
        "expected_source_fingerprint": fingerprint,
        "compiled_path": str(rust_utils.preferred_compiled_path() or ""),
    }


def run_backtest(cfg: dict) -> int:
    # The backtest opens the execution-audit CSV with create-new semantics, so a re-run into an
    # existing bundle fails with EEXIST instead of overwriting. Clear it here: this tool owns the
    # bundle, and a stale audit must never be mistaken for the current run's provenance.
    AUDIT_PATH.unlink(missing_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    cmd = [sys.executable, "-m", "backtest", str(CANDIDATE_CONFIG)]
    with RUN_LOG.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc.returncode


def run_dirs_under(results_base: Path) -> list[Path]:
    """Run directories under a bundle, at the `binance` or `binance_<label>` level.

    The backtest writes `<base_dir>/<exchange>[_label]/<UTC timestamp>/`, so the exchange level
    may carry a suffix. Globbing for timestamp-named directories avoids hardcoding either name.
    """
    root = Path(results_base)
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/binance*/*") if p.is_dir() and p.name[:2].isdigit())


def find_result_dir() -> Path:
    dirs = run_dirs_under(RESULTS_BASE)
    if not dirs:
        raise SystemExit(f"no result dir under {RESULTS_BASE}")
    if len(dirs) != 1:
        raise SystemExit(f"expected exactly one result dir under {RESULTS_BASE}, found {dirs}")
    return dirs[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-run", action="store_true")
    parser.add_argument(
        "--apply-locked-ops",
        action="store_true",
        help=(
            "apply the locked candidate ops to the given config. Off by default: without it the "
            "profile is run as-is, which is what a baseline or reference bundle wants."
        ),
    )
    parser.add_argument(
        "--candidate-config",
        default=None,
        help=(
            "config to run; defaults to the frozen baseline config with the locked candidate ops "
            "applied. Pass a published profile to produce its own artifact bundle."
        ),
    )
    parser.add_argument(
        "--artifacts-subdir",
        default=DEFAULT_ARTIFACTS_SUBDIR,
        help=f"artifact directory under <study>/artifacts (default {DEFAULT_ARTIFACTS_SUBDIR})",
    )
    parser.add_argument(
        "--disable-plotting",
        default=None,
        help=(
            "value for `backtest.disable_plotting`: `all`, `summary`, `summary+coin_fills`, a "
            "comma-separated group list, or a single group. Use it on hosts where the plotting "
            "tail is OOM-killed; the analytical artifacts are written before plotting starts."
        ),
    )
    parser.add_argument(
        "--label",
        default=None,
        help=(
            "suffix for the run directory the backtest creates. The directory is named "
            "<UTC timestamp> by the backtest itself; a label makes it identifiable when the "
            "run lives next to other dated runs, which is how the archived reference runs are "
            "laid out."
        ),
    )
    parser.add_argument(
        "--study-window",
        default=None,
        choices=sorted(study.WINDOWS),
        help=(
            "pin the artifact to a frozen study window; omit to run the config's own window. "
            "Pinning is what makes the artifact comparable with the study's own cells, whose "
            "metrics the verifier checks against."
        ),
    )
    args = parser.parse_args()

    global ARTIFACTS, RESULTS_BASE, CANDIDATE_CONFIG, RUN_RECORD, AUDIT_PATH, RUN_LOG, CONFIG_SOURCE
    ARTIFACTS = STUDY / "artifacts" / args.artifacts_subdir
    RESULTS_BASE = ARTIFACTS / "backtest_results"
    CANDIDATE_CONFIG = ARTIFACTS / "candidate.config.json"
    RUN_RECORD = ARTIFACTS / "run_record.json"
    AUDIT_PATH = ARTIFACTS / "execution_audit.csv"
    RUN_LOG = ARTIFACTS / "backtest_run.log"
    if args.candidate_config:
        CONFIG_SOURCE = (REPO / args.candidate_config).resolve()

    started = time.time()
    contract = load_json(CONTRACT)
    use_locked_ops = bool(args.apply_locked_ops)
    if use_locked_ops:
        # Every locked op carries its expected baseline value, and `build_candidate_config`
        # refuses on drift, so a wrong source config fails loudly instead of silently
        # producing a profile that is not the candidate.
        source_state = load_json(CONFIG_SOURCE)
        missing = [op["path"] for op in load_json(LOCK)["candidates"][0]["ops"] if get_path(source_state, op["path"]) != op["baseline"]]
        if missing:
            raise SystemExit(
                f"{CONFIG_SOURCE} does not carry the baseline values the lock was recorded "
                f"against; refusing to apply ops: {missing}"
            )
    cfg, applied = build_candidate_config(
        use_locked_ops, args.study_window, args.label, args.disable_plotting
    )
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
    matched_study_window = next(
        (
            name
            for name, (start, end) in sorted(study.WINDOWS.items())
            if str(cfg["backtest"]["start_date"]) == start and str(cfg["backtest"]["end_date"]) == end
        ),
        None,
    )
    audit_rows = None
    if AUDIT_PATH.exists():
        with AUDIT_PATH.open() as handle:
            audit_rows = sum(1 for _ in handle) - 1

    record = {
        "candidate_id": CANDIDATE_ID if use_locked_ops else CONFIG_SOURCE.stem,
        "locked_ops_applied": use_locked_ops,
        "candidate_ops": applied,
        "candidate_config": str(CANDIDATE_CONFIG.relative_to(REPO)),
        "candidate_config_sha256": config_sha,
        "source_config": str(CONFIG_SOURCE.relative_to(REPO)),
        "source_config_sha256": sha256_file(CONFIG_SOURCE),
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
        "run_label": args.label,
        "disable_plotting": args.disable_plotting,
        "window": {
            # Name the frozen window whose dates these are, so a bundle states its own
            # comparability instead of leaving the reader to match date strings.
            "study_window": args.study_window or matched_study_window,
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