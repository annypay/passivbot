#!/usr/bin/env python3
"""Run the published gated profile offline and write this study's provenance.

The replay runs the canonical backtest entrypoint (`src/backtest.py`) against the frozen
config produced by `build_cell_config.py`, with the execution audit enabled, and refuses
to accept anything that is not served by the frozen HLCV bundle:

* the frozen dataset directory must contain every declared artifact,
* the run's own `dataset.json` must record that exact cache,
* the run must not load or rebuild a different HLCV dataset,
* the run must actually have run **with the gate enabled** and with exactly the
  declared 20/50 parameters, because an ungated run is a different strategy.

It then writes `run_record.json` (cited by the report) and `global_metrics.json` (engine,
data hashes, environment) into the run directory the backtest created.

Offline only: no network access, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cell_spec as spec  # noqa: E402

PROVENANCE_SNIPPET = """
import json, sys
sys.path.insert(0, "src")
from rust_utils import (
    collect_runtime_provenance,
    preferred_compiled_path,
    read_source_stamp,
    source_fingerprint,
)
payload = collect_runtime_provenance()
payload["source_fingerprint"] = source_fingerprint()
compiled = preferred_compiled_path()
payload["compiled_path"] = str(compiled) if compiled is not None else None
payload["compiled_source_stamp"] = (
    read_source_stamp(compiled) if compiled is not None else None
)
print("PROVENANCE_JSON=" + json.dumps(payload, sort_keys=True))
"""

#: Artifacts the run must leave behind. `fills_plots/` is the per-coin panel set, which is the
#: memory peak of a 40-coin three-year run; the study runs with the full figure set so the
#: bundle is complete on the convention's own terms.
REQUIRED_ARTIFACTS = (
    "analysis.json",
    "config.json",
    "dataset.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
    "balance_and_equity.png",
    "balance_and_equity_logy.png",
    "drawdown.png",
    "total_wallet_exposure.png",
    "pnl_cumsum.png",
)


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def require_preconditions(*, force: bool, reuse_run: bool) -> None:
    problems = []
    if not spec.CONFIG_PATH.exists():
        problems.append(
            f"missing {spec.relative(spec.CONFIG_PATH)}; run build_cell_config.py first"
        )
    if not spec.PROFILE_INPUT_PATH.exists():
        problems.append(
            f"missing {spec.relative(spec.PROFILE_INPUT_PATH)}; run build_cell_config.py first"
        )
    for name in spec.FROZEN_CACHE_FILES:
        if not (spec.FROZEN_CACHE / name).exists():
            problems.append(f"missing frozen dataset file {spec.FROZEN_CACHE_REL / name}")
    if reuse_run:
        if not spec.EXECUTION_AUDIT_PATH.exists():
            problems.append(
                f"{spec.relative(spec.EXECUTION_AUDIT_PATH)} is missing; the reused run must "
                "have streamed an execution audit"
            )
    elif spec.EXECUTION_AUDIT_PATH.exists() and not force:
        problems.append(
            f"{spec.relative(spec.EXECUTION_AUDIT_PATH)} already exists; the backtest "
            "creates audit files exclusively (use --force to remove it first, or "
            "--reuse-run to collect the run that wrote it)"
        )
    if problems:
        fail(problems, "replay preconditions are not met")


def source_runs() -> set[str]:
    """Run directories the backtest writes, i.e. `<base_dir>/binance/<timestamp>/`."""
    exchange_dir = spec.BACKTEST_BASE_DIR / "binance"
    if not exchange_dir.exists():
        return set()
    return {path.name for path in exchange_dir.iterdir() if path.is_dir()}


def run_backtest(config_path: Path, *, timeout_s: float) -> tuple[int, float]:
    spec.LOGS.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(spec.REPO / "src")
    env.setdefault("PYTHONUNBUFFERED", "1")
    command = [
        sys.executable,
        str(spec.REPO / "src/backtest.py"),
        str(config_path),
        "--execution-audit-path",
        str(spec.EXECUTION_AUDIT_PATH),
    ]
    started = time.time()
    with spec.REPLAY_LOG_PATH.open("w", encoding="utf-8") as log:
        log.write("command: " + " ".join(command) + "\n")
        log.flush()
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=str(spec.REPO),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
        )
    return completed.returncode, time.time() - started


def log_tail(lines: int = 40) -> str:
    if not spec.REPLAY_LOG_PATH.exists():
        return "(no log)"
    text = spec.REPLAY_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(text[-lines:])


def collect_provenance() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(spec.REPO / "src")
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROVENANCE_SNIPPET],
        cwd=str(spec.REPO),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    payload: dict = {"error": None}
    if completed.returncode != 0:
        payload["error"] = (completed.stderr or "").strip()[-2000:]
        return payload
    for line in completed.stdout.splitlines():
        if line.startswith("PROVENANCE_JSON="):
            payload = json.loads(line.split("=", 1)[1])
            payload["error"] = None
            return payload
    payload["error"] = "provenance snippet produced no payload"
    return payload


def git_state() -> dict:
    def _git(*args: str) -> str:
        completed = subprocess.run(  # noqa: S603
            ["git", *args],
            cwd=str(spec.REPO),
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip()

    status = _git("status", "--porcelain")
    return {
        "head": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status),
        "status_porcelain": status.splitlines(),
    }


def dataset_artifact_hashes() -> dict:
    """Hash the frozen dataset's logical arrays and gzip bytes.

    The bundle manifest records hashes of the *logical* NumPy payloads
    (`hlcvs_manifest.hash_logical_array`), not of the gzip byte stream, so the frozen
    comparison must use the same convention.
    """
    sys.path.insert(0, str(spec.REPO / "src"))
    from hlcvs_manifest import hash_logical_array  # noqa: E402
    from tools.verify_hlcvs_data import load_npy_gz  # noqa: E402

    out: dict = {"logical": {}, "file_bytes": {}, "shapes": {}, "dtypes": {}}
    for name, manifest_key in (
        ("hlcvs.npy.gz", "hlcvs"),
        ("timestamps.npy.gz", "timestamps"),
        ("btc_usd_prices.npy.gz", "btc_usd_prices"),
    ):
        path = spec.FROZEN_CACHE / name
        array = load_npy_gz(path)
        out["logical"][manifest_key] = hash_logical_array(array)
        out["shapes"][manifest_key] = [int(dim) for dim in array.shape]
        out["dtypes"][manifest_key] = str(array.dtype)
        out["file_bytes"][manifest_key] = spec.sha256_file(path)
        del array
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-s", type=float, default=7200.0, help="backtest timeout")
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove an existing execution_audit.csv and the bundle's previous run before running",
    )
    parser.add_argument(
        "--reuse-run",
        metavar="TS_OR_PATH",
        default=None,
        help=(
            "skip the backtest and collect an already written run directory instead: a bare "
            "timestamp directory name (e.g. 2026-09-16T20_29_03) or a path to it"
        ),
    )
    args = parser.parse_args(argv)

    require_preconditions(force=args.force, reuse_run=bool(args.reuse_run))
    if args.force and spec.EXECUTION_AUDIT_PATH.exists():
        spec.EXECUTION_AUDIT_PATH.unlink()
    if args.force and not args.reuse_run:
        # A bundle holds one run directory and the backtest names it from the UTC completion
        # timestamp, so --force replaces the bundle's run instead of leaving a second one that
        # the report tooling would refuse to pick between.
        exchange_dir = spec.BACKTEST_BASE_DIR / "binance"
        stale = sorted(source_runs())
        for name in stale:
            shutil.rmtree(exchange_dir / name)
        if stale:
            print(f"removed {len(stale)} previous run(s): {', '.join(stale)}")

    profile_input = spec.load_json(spec.PROFILE_INPUT_PATH)
    config = spec.load_json(spec.CONFIG_PATH)

    if args.reuse_run:
        candidate = Path(args.reuse_run)
        run_dir = (
            candidate
            if candidate.is_dir()
            else spec.BACKTEST_BASE_DIR / "binance" / str(args.reuse_run)
        )
        if not run_dir.is_dir():
            fail([f"{run_dir} is not a directory"], "cannot reuse that run directory")
        elapsed_s = 0.0
        print(f"reusing run directory: {spec.relative(run_dir)}")
    else:
        before = source_runs()
        print(f"running offline replay: {spec.relative(spec.CONFIG_PATH)}")
        returncode, elapsed_s = run_backtest(spec.CONFIG_PATH, timeout_s=args.timeout_s)
        if returncode != 0:
            print("backtest log tail:\n" + log_tail(), file=sys.stderr)
            fail([f"src/backtest.py exited with code {returncode}"], "replay backtest failed")
        if not spec.EXECUTION_AUDIT_PATH.exists():
            fail(
                [f"no execution audit written at {spec.relative(spec.EXECUTION_AUDIT_PATH)}"],
                "replay did not stream the execution audit",
            )
        new_runs = sorted(source_runs() - before)
        if len(new_runs) != 1:
            fail(
                [f"expected exactly one new run directory, found {new_runs!r}"],
                "cannot identify the replay run directory",
            )
        run_dir = spec.BACKTEST_BASE_DIR / "binance" / new_runs[0]
        print(f"run directory: {spec.relative(run_dir)}")

    missing = [name for name in REQUIRED_ARTIFACTS if not (run_dir / name).exists()]
    if not (run_dir / "fills_plots").is_dir():
        missing.append("fills_plots/")
    if missing:
        fail([f"missing artifact {name}" for name in missing], "replay artifacts are incomplete")

    run_config = spec.load_json(run_dir / "config.json")
    dataset = spec.load_json(run_dir / "dataset.json")
    analysis = spec.load_json(run_dir / "analysis.json")

    # The dumped config carries no resolved cache/coin fields by design
    # (`sanitize_prepared_config_for_dump` clears them); the run's own dataset metadata is
    # the authoritative record of which HLCV data this run consumed.
    problems = []
    dataset_coins = sorted(dataset.get("coins") or [])
    if dataset_coins != sorted(profile_input["coins"]):
        problems.append(
            f"dataset coins ({len(dataset_coins)}) != cell coins ({len(profile_input['coins'])})"
        )
    expected_fills = int(profile_input["metrics"][spec.CELL_FILLS_KEY])
    actual_fills = int(analysis.get("fills_count") or 0)
    if actual_fills != expected_fills:
        problems.append(f"analysis fills_count {actual_fills} != cell fill_rows {expected_fills}")

    # The artifact under test is the *gated* profile. Every identity root must match the
    # published profile, and the gate must be the declared one and enabled: an ungated run
    # is a different strategy (~29% drawdown instead of ~10.5%).
    # Compare app-normalized payloads on both sides: the dump is rebuilt from the current
    # schema template, so a raw profile file and a dumped run config are not directly
    # comparable. The retargeted data-identity keys are checked separately below.
    normalized = spec.normalize_config_payload(config)
    for root in ("bot", "live", "coin_overrides"):
        problems.extend(
            f"run config {problem}"
            for problem in spec.equal_subtree(normalized[root], run_config[root], root)
        )
    run_approved = (run_config.get("live") or {}).get("approved_coins") or {}
    if sorted(run_approved.get("long") or []) != sorted(profile_input["coins"]):
        problems.append("run config live.approved_coins.long is not the frozen basket")
    if list(run_approved.get("short") or []):
        problems.append("run config live.approved_coins.short is not empty")
    for key in ("start_date", "end_date", "exchanges"):
        if normalized["backtest"].get(key) != run_config["backtest"].get(key):
            problems.append(
                f"run config backtest.{key}: expected {normalized['backtest'].get(key)!r}, "
                f"got {run_config['backtest'].get(key)!r}"
            )
    # The dump deliberately clears the resolved dataset fields; `dataset.json` is the
    # authoritative record, and it is checked below.
    for key in spec.DUMP_CLEARED_BACKTEST_KEYS:
        if run_config["backtest"].get(key) is not None:
            problems.append(
                f"run config backtest.{key} = {run_config['backtest'].get(key)!r}; the dump is "
                "expected to clear resolved dataset fields"
            )
    run_bt = run_config.get("backtest") or {}
    for key, expected in (*spec.EXECUTION.items(), *spec.COSTS.items()):
        if not spec.numeric_equal(run_bt.get(key), expected):
            problems.append(f"run config backtest.{key}: expected {expected!r}, got {run_bt.get(key)!r}")
    run_gate = run_bt.get("entry_regime_gate")
    if not isinstance(run_gate, dict) or not run_gate.get("enabled"):
        problems.append(
            f"run config backtest.entry_regime_gate = {run_gate!r}; the replay must run with "
            "the gate enabled, otherwise it is not the published gated profile"
        )
    else:
        problems.extend(
            f"run config {problem}"
            for problem in spec.compare_subtrees(
                spec.GATE, spec.gate_semantics(run_gate), "backtest.entry_regime_gate"
            )
        )
    # The profile's candidate list (41) is wider than the frozen basket (40): `MNT` has no
    # usable history in this window. The study retargets the traded universe, so the gap is
    # expected and reported rather than treated as drift.
    profile_candidates = sorted(
        ((spec.load_json(spec.PUBLISHED_PROFILE).get("live") or {}).get("approved_coins") or {})
        .get("long")
        or []
    )
    dropped = sorted(set(profile_candidates) - set(profile_input["coins"]))
    if dropped:
        print(
            f"note: profile declares {len(profile_candidates)} candidates; frozen dataset holds "
            f"{len(profile_input['coins'])}. Dropped for lack of usable history: {dropped}"
        )
    if problems:
        fail(problems, "replay did not reproduce the frozen profile inputs")

    # Offline boundary: the run must have been served by the frozen bundle. A cache miss makes
    # the loader rebuild the dataset from the network, which both breaks the offline claim and
    # silently swaps the data out from under the report.
    log_text = (
        spec.REPLAY_LOG_PATH.read_text(encoding="utf-8", errors="replace")
        if spec.REPLAY_LOG_PATH.exists()
        else ""
    )
    if "Loaded hlcvs data from cache" not in log_text:
        fail(
            ["the run log never reports 'Loaded hlcvs data from cache'"],
            "the replay did not read the frozen dataset; it may have fetched from the network",
        )
    fetch_hits = sorted({marker for marker in spec.NETWORK_FETCH_MARKERS if marker in log_text})
    if fetch_hits:
        fail(
            [f"run log contains fetch marker {marker!r}" for marker in fetch_hits],
            "the replay fetched market data; this study must run offline",
        )
    print("offline boundary: frozen dataset loaded from cache, no fetch markers in the log")

    bundle_manifest = spec.load_json(spec.FROZEN_CACHE / "manifest.json")
    audit_lines = sum(1 for _ in spec.EXECUTION_AUDIT_PATH.open(encoding="utf-8")) - 1
    if audit_lines != actual_fills:
        fail(
            [
                f"execution audit rows {audit_lines} != fills_count {actual_fills}; "
                "the audit did not stream one row per fill"
            ],
            "execution audit does not describe this run",
        )
    provenance = collect_provenance()
    data_hashes = dataset_artifact_hashes()
    global_metrics = {
        "cell_id": spec.CELL_ID,
        "run_dir": spec.relative(run_dir),
        "run_dir_name": run_dir.name,
        "run_base_dir": spec.relative(spec.BACKTEST_BASE_DIR),
        "config_sha256": spec.sha256_file(spec.CONFIG_PATH),
        "published_profile_sha256": spec.sha256_file(spec.PUBLISHED_PROFILE),
        "backtest_elapsed_s": elapsed_s,
        "execution_audit_rows": audit_lines,
        "dataset": {
            "cache_dir": spec.relative(spec.FROZEN_CACHE),
            "frozen_bundle_label": spec.FROZEN_CACHE.name,
            "cache_dir_label": dataset.get("cache_dir_label"),
            "cache_hash": dataset.get("cache_hash"),
            "manifest_config_hash": bundle_manifest.get("config_hash"),
            "manifest_cache_bundle": spec.FROZEN_CACHE.name,
            "coin_count": len(dataset_coins),
            "hash_convention": (
                "logical NumPy payload via hlcvs_manifest.hash_logical_array; "
                "file_bytes hashes the gzip byte stream for provenance only"
            ),
            "data_hashes": data_hashes["logical"],
            "file_bytes_hashes": data_hashes["file_bytes"],
            "array_shapes": data_hashes["shapes"],
            "array_dtypes": data_hashes["dtypes"],
            "manifest_hashes": {
                key: bundle_manifest["files"][key]["sha256"]
                for key in ("hlcvs", "timestamps", "btc_usd_prices")
                if key in bundle_manifest.get("files", {})
            },
        },
        "entry_regime_gate": run_gate,
        "engine": provenance,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "executable": sys.executable,
        },
        "git": git_state(),
        "generated_at_unix_s": time.time(),
    }
    hash_problems = [
        f"{manifest_key}: logical array hash "
        f"{global_metrics['dataset']['data_hashes'].get(manifest_key)} != manifest "
        f"{global_metrics['dataset']['manifest_hashes'].get(manifest_key)}"
        for manifest_key in ("hlcvs", "timestamps", "btc_usd_prices")
        if global_metrics["dataset"]["manifest_hashes"].get(manifest_key)
        != global_metrics["dataset"]["data_hashes"].get(manifest_key)
    ]
    if hash_problems:
        fail(hash_problems, "frozen dataset arrays do not match their manifest hashes")

    contract = profile_input["contract"]
    run_record = {
        "candidate_id": spec.CELL_ID,
        "candidate_group": spec.CELL_GROUP,
        "candidate_kind": "published_profile_replay",
        "source_config": profile_input["published_profile"],
        "source_config_sha256": profile_input["published_profile_sha256"],
        "candidate_config_path": spec.relative(spec.CONFIG_PATH),
        "candidate_config_sha256": global_metrics["config_sha256"],
        "candidate_ops": [],
        "frozen_from_published_profile": True,
        "retargeted_backtest_keys": list(spec.RETARGETED_BACKTEST_KEYS),
        "entry_regime_gate": run_gate,
        "window": spec.CELL_WINDOW,
        "scenario": spec.SCENARIO,
        "universe": {
            "exchange": "binance",
            "coin_count": profile_input["coin_count"],
            "coins": profile_input["coins"],
            "start_date": config["backtest"]["start_date"],
            "end_date": config["backtest"]["end_date"],
        },
        "execution": spec.EXECUTION,
        "costs": spec.COSTS,
        "contract": contract,
        "dataset": global_metrics["dataset"],
        "engine": {
            "expected_source_fingerprint": provenance.get("source_fingerprint"),
            "source_fingerprint_matches_loaded_stamp": (
                provenance.get("source_fingerprint") is not None
                and provenance.get("source_fingerprint")
                == provenance.get("runtime_module_source_stamp")
            ),
            "source_fingerprint_matches_compiled_stamp": (
                provenance.get("source_fingerprint") is not None
                and provenance.get("source_fingerprint")
                == provenance.get("compiled_source_stamp")
            ),
            "compiled_path": provenance.get("compiled_path"),
            "compiled_source_stamp": provenance.get("compiled_source_stamp"),
            "preferred_compiled_path": provenance.get("preferred_compiled_path"),
            "runtime_module_path": provenance.get("runtime_module_path"),
            "git": global_metrics["git"],
        },
        "engine_note": (
            "Replayed with the worktree engine current at run time; the Rust source "
            "fingerprint and the uncommitted-worktree record are in global_metrics.json. "
            "The frozen baseline run used a different engine revision, and the source study's "
            "gate-cell bundle was produced by an earlier run of the same worktree."
        ),
        "cell_reference": {
            "result_json": spec.relative(spec.CELL_RESULT_PATH),
            "result_json_sha256": spec.sha256_file(spec.CELL_RESULT_PATH),
            "metrics": profile_input["metrics"],
        },
    }

    spec.write_json(run_dir / "global_metrics.json", global_metrics)
    spec.write_json(run_dir / "run_record.json", run_record)
    print(f"wrote {spec.relative(run_dir / 'global_metrics.json')}")
    print(f"wrote {spec.relative(run_dir / 'run_record.json')}")
    print(
        f"fills={actual_fills} coins={len(dataset_coins)} "
        f"audit_rows={audit_lines} elapsed={elapsed_s:.1f}s"
    )
    print(
        "engine fingerprint="
        f"{provenance.get('source_fingerprint')} "
        f"compiled_stamp={provenance.get('compiled_source_stamp')} "
        f"loaded_stamp={provenance.get('runtime_module_source_stamp')}"
    )
    print(f"gate={run_gate}")
    if not run_record["engine"]["source_fingerprint_matches_compiled_stamp"]:
        print(
            "WARNING: the compiled extension stamp does not match the current Rust sources; "
            "the report must not claim a verified engine identity",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
