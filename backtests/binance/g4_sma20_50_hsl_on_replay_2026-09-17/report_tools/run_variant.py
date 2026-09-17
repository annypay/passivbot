#!/usr/bin/env python3
"""Run one replay variant offline and write this study's provenance.

The runner drives the canonical backtest entrypoint (`src/backtest.py`) with the frozen
config `build_variant_config.py` produced, for exactly one arm of the comparison, and
refuses to accept anything that is not the parent g4 config plus that arm's declared
change served by the frozen HLCV bundle:

* the frozen dataset directory must contain every declared artifact,
* the run's own `dataset.json` must record that exact cache and the frozen coin basket,
* the run must not load or rebuild a different HLCV dataset (no fetch markers),
* the gate must still be the declared, enabled 20/50 gate,
* `bot.long.hsl.enabled` must equal the variant's declared value, and every other HSL
  field must be the parent profile's own value.

It then writes `run_record.json` (cited by the report) and `global_metrics.json` (engine,
dataset hashes, environment, timing) into the run directory the backtest created.

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

import variant_spec as spec  # noqa: E402

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

#: Artifacts the run must leave behind. The full figure set is produced, including the
#: per-coin panels, so the bundle is complete on the convention's own terms.
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


def require_preconditions(variant: spec.Variant, *, force: bool, reuse_run: bool) -> None:
    problems = []
    if not variant.config_path.exists():
        problems.append(
            f"missing {spec.relative(variant.config_path)}; run build_variant_config.py first"
        )
    if not spec.VARIANT_INPUT_PATH.exists():
        problems.append(
            f"missing {spec.relative(spec.VARIANT_INPUT_PATH)}; run build_variant_config.py first"
        )
    for name in spec.FROZEN_CACHE_FILES:
        if not (spec.FROZEN_CACHE / name).exists():
            problems.append(f"missing frozen dataset file {spec.FROZEN_CACHE_REL / name}")
    if reuse_run:
        if not variant.execution_audit_path.exists():
            problems.append(
                f"{spec.relative(variant.execution_audit_path)} is missing; the reused run must "
                "have streamed an execution audit"
            )
    elif variant.execution_audit_path.exists() and not force:
        problems.append(
            f"{spec.relative(variant.execution_audit_path)} already exists; the backtest creates "
            "audit files exclusively (use --force to remove it first, or --reuse-run to collect "
            "the run that wrote it)"
        )
    if problems:
        fail(problems, f"{variant.key}: replay preconditions are not met")


def source_runs(variant: spec.Variant) -> set[str]:
    """Run directories the backtest writes, i.e. `<base_dir>/<exchange>/<timestamp>/`."""
    if not variant.runs_base.exists():
        return set()
    return {path.name for path in variant.runs_base.iterdir() if path.is_dir()}


def run_backtest(
    variant: spec.Variant, config_path: Path, *, timeout_s: float
) -> tuple[int, float]:
    variant.log_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(spec.REPO / "src")
    env.setdefault("PYTHONUNBUFFERED", "1")
    command = [
        sys.executable,
        str(spec.REPO / "src/backtest.py"),
        str(config_path),
        "--execution-audit-path",
        str(variant.execution_audit_path),
    ]
    started = time.time()
    with variant.replay_log_path.open("w", encoding="utf-8") as log:
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


def log_tail(variant: spec.Variant, lines: int = 40) -> str:
    if not variant.replay_log_path.exists():
        return "(no log)"
    text = variant.replay_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
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


def path_label(value: Any) -> Any:
    """A repository-relative label for a host path, or its basename when it is outside.

    Tracked evidence must not carry the generating host's absolute paths, so the engine
    provenance records `venv/bin/python` rather than `/home/<operator>/.../venv/bin/python`.
    """
    if not isinstance(value, str) or not value:
        return value
    path = Path(value)
    try:
        return str(path.resolve().relative_to(spec.REPO))
    except ValueError:
        return path.name


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


def check_run_config(
    variant: spec.Variant, config: dict, run_config: dict, profile_input: dict
) -> list[str]:
    """The dump must be this variant's frozen config, with only declared differences."""
    problems: list[str] = []
    normalized = spec.normalize_config_payload(config)
    for root in ("bot", "live", "coin_overrides"):
        problems.extend(
            f"run config {problem}"
            for problem in spec.diff_subtrees(
                normalized[root], run_config[root], root,
                allowed=spec.ALLOWED_RUN_DUMP_DIFF_PATHS,
            )
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
            f"run config backtest.entry_regime_gate = {run_gate!r}; the replay must run with the "
            "gate enabled, otherwise it is not the same strategy"
        )
    else:
        problems.extend(
            f"run config {problem}"
            for problem in spec.compare_subtrees(
                spec.GATE, spec.gate_semantics(run_gate), "backtest.entry_regime_gate"
            )
        )
    # The arm's own variable, and the invariant that surrounds it: the flag is what the
    # variant declares, and every other HSL field is the parent profile's own value.
    run_hsl = ((run_config.get("bot") or {}).get("long") or {}).get("hsl") or {}
    if run_hsl.get("enabled") is not variant.hsl_enabled:
        problems.append(
            f"run config bot.long.hsl.enabled = {run_hsl.get('enabled')!r}; variant "
            f"{variant.key!r} declares {variant.hsl_enabled!r}"
        )
    expected_hsl = dict(spec.HSL_BLOCK, enabled=variant.hsl_enabled)
    problems.extend(
        f"run config {problem}"
        for problem in spec.compare_subtrees(expected_hsl, run_hsl, "bot.long.hsl")
    )
    for key, expected in spec.LIVE_ONLY_HSL.items():
        actual = (run_config.get("live") or {}).get(key)
        if actual != expected:
            problems.append(f"run config live.{key}: expected {expected!r}, got {actual!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        required=True,
        choices=spec.DEFAULT_VARIANT_ORDER,
        help="which replay arm to run",
    )
    parser.add_argument("--timeout-s", type=float, default=7200.0, help="backtest timeout")
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove an existing execution audit and the bundle's previous run before running",
    )
    parser.add_argument(
        "--reuse-run",
        metavar="TS_OR_PATH",
        default=None,
        help=(
            "skip the backtest and collect an already written run directory instead: a bare "
            "timestamp directory name (e.g. 2026-09-17T04_29_03) or a path to it"
        ),
    )
    args = parser.parse_args(argv)
    variant = spec.VARIANTS_BY_KEY[args.variant]

    require_preconditions(variant, force=args.force, reuse_run=bool(args.reuse_run))
    if args.force and variant.execution_audit_path.exists():
        variant.execution_audit_path.unlink()
    if args.force and not args.reuse_run:
        # A bundle holds one run directory and the backtest names it from the UTC completion
        # timestamp, so --force replaces the bundle's run instead of leaving a second one that
        # the report tooling would refuse to pick between.
        stale = sorted(source_runs(variant))
        for name in stale:
            shutil.rmtree(variant.runs_base / name)
        if stale:
            print(f"removed {len(stale)} previous run(s): {', '.join(stale)}")

    profile_input = spec.load_json(spec.VARIANT_INPUT_PATH)
    config = spec.load_json(variant.config_path)

    if args.reuse_run:
        run_dir = spec.find_variant_run_dir(variant, args.reuse_run)
        elapsed_s = 0.0
        print(f"reusing run directory: {spec.relative(run_dir)}")
    else:
        before = source_runs(variant)
        print(f"running offline replay [{variant.key}]: {spec.relative(variant.config_path)}")
        returncode, elapsed_s = run_backtest(variant, variant.config_path, timeout_s=args.timeout_s)
        if returncode != 0:
            print("backtest log tail:\n" + log_tail(variant), file=sys.stderr)
            fail([f"src/backtest.py exited with code {returncode}"], "replay backtest failed")
        if not variant.execution_audit_path.exists():
            fail(
                [f"no execution audit written at {spec.relative(variant.execution_audit_path)}"],
                "replay did not stream the execution audit",
            )
        new_runs = sorted(source_runs(variant) - before)
        if len(new_runs) != 1:
            fail(
                [f"expected exactly one new run directory, found {new_runs!r}"],
                "cannot identify the replay run directory",
            )
        run_dir = variant.runs_base / new_runs[0]
        print(f"run directory: {spec.relative(run_dir)}")

    missing = [name for name in REQUIRED_ARTIFACTS if not (run_dir / name).exists()]
    if not (run_dir / "fills_plots").is_dir():
        missing.append("fills_plots/")
    if missing:
        fail([f"missing artifact {name}" for name in missing], "replay artifacts are incomplete")

    run_config = spec.load_json(run_dir / "config.json")
    dataset = spec.load_json(run_dir / "dataset.json")
    analysis = spec.load_json(run_dir / "analysis.json")

    problems: list[str] = []
    dataset_coins = sorted(dataset.get("coins") or [])
    if dataset_coins != sorted(profile_input["coins"]):
        problems.append(
            f"dataset coins ({len(dataset_coins)}) != frozen basket ({len(profile_input['coins'])})"
        )
    problems.extend(check_run_config(variant, config, run_config, profile_input))
    if problems:
        fail(problems, f"{variant.key}: replay did not reproduce the declared arm")

    # Offline boundary: the run must have been served by the frozen bundle. A cache miss makes
    # the loader rebuild the dataset from the network, which both breaks the offline claim and
    # silently swaps the data out from under the report.
    log_text = (
        variant.replay_log_path.read_text(encoding="utf-8", errors="replace")
        if variant.replay_log_path.exists()
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
    audit_lines = sum(1 for _ in variant.execution_audit_path.open(encoding="utf-8")) - 1
    actual_fills = int(analysis.get("fills_count") or 0)
    if audit_lines != actual_fills:
        fail(
            [
                f"execution audit rows {audit_lines} != fills_count {actual_fills}; "
                "the audit did not stream one row per fill"
            ],
            "execution audit does not describe this run",
        )
    provenance = collect_provenance()
    for key in ("compiled_path", "preferred_compiled_path", "runtime_module_path"):
        if key in provenance:
            provenance[key] = path_label(provenance[key])
    data_hashes = dataset_artifact_hashes()
    global_metrics = {
        "variant": variant.key,
        "variant_label": variant.label,
        "variant_description": variant.description,
        "run_dir": spec.relative(run_dir),
        "run_dir_name": run_dir.name,
        "run_base_dir": spec.relative(variant.base_dir),
        "config_sha256": spec.sha256_file(variant.config_path),
        "parent_config": spec.relative(spec.SOURCE_CONFIG),
        "parent_config_sha256": spec.SOURCE_CONFIG_SHA256,
        "declared_delta": [
            {"path": dotted, "from": _from, "to": to}
            for dotted, _from, to in spec.DECLARED_DELTA
        ],
        "backtest_elapsed_s": elapsed_s,
        "execution_audit_rows": audit_lines,
        "analysis_sha256": spec.sha256_file(run_dir / "analysis.json"),
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
        "entry_regime_gate": run_config["backtest"].get("entry_regime_gate"),
        "hsl_long": run_config["bot"]["long"].get("hsl"),
        "live_hsl": {key: (run_config.get("live") or {}).get(key) for key in spec.LIVE_ONLY_HSL},
        "engine": provenance,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "executable": path_label(sys.executable),
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

    run_record = {
        "candidate_id": f"g4_sma20_50_{variant.key}",
        "candidate_kind": "hsl_variant_replay",
        "variant": {
            "key": variant.key,
            "label": variant.label,
            "description": variant.description,
            "hsl_enabled": variant.hsl_enabled,
        },
        "source_config": spec.relative(spec.SOURCE_CONFIG),
        "source_config_sha256": spec.SOURCE_CONFIG_SHA256,
        "candidate_config_path": spec.relative(variant.config_path),
        "candidate_config_sha256": global_metrics["config_sha256"],
        "declared_delta": global_metrics["declared_delta"],
        "hsl_block": global_metrics["hsl_long"],
        "live_only_hsl": global_metrics["live_hsl"],
        "entry_regime_gate": global_metrics["entry_regime_gate"],
        "window": {
            "start_date": config["backtest"]["start_date"],
            "end_date": config["backtest"]["end_date"],
        },
        "universe": {
            "exchange": spec.DATA_EXCHANGE,
            "coin_count": len(profile_input["coins"]),
            "coins": sorted(profile_input["coins"]),
        },
        "execution": spec.EXECUTION,
        "costs": spec.COSTS,
        "dataset": global_metrics["dataset"],
        "comparison": {
            "tracked_baseline_run": spec.relative(spec.TRACKED_BASELINE_RUN),
            "tracked_baseline_analysis_sha256": spec.TRACKED_BASELINE_ANALYSIS_SHA256,
            "variant_input": spec.relative(spec.VARIANT_INPUT_PATH),
            "variant_input_sha256": spec.sha256_file(spec.VARIANT_INPUT_PATH),
        },
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
            "The parent bundle was produced by an older engine revision, so this study replays "
            "the unchanged config on the current engine as a paired control; the HSL comparison "
            "is made between the two runs of this study, and the tracked parent bundle is "
            "reported as a third column."
        ),
    }

    spec.write_json(run_dir / "global_metrics.json", global_metrics)
    spec.write_json(run_dir / "run_record.json", run_record)
    print(f"wrote {spec.relative(run_dir / 'global_metrics.json')}")
    print(f"wrote {spec.relative(run_dir / 'run_record.json')}")
    print(
        f"variant={variant.key} fills={actual_fills} coins={len(dataset_coins)} "
        f"audit_rows={audit_lines} elapsed={elapsed_s:.1f}s"
    )
    print(
        "engine fingerprint="
        f"{provenance.get('source_fingerprint')} "
        f"compiled_stamp={provenance.get('compiled_source_stamp')} "
        f"loaded_stamp={provenance.get('runtime_module_source_stamp')}"
    )
    print(f"hsl_long={json.dumps(global_metrics['hsl_long'], sort_keys=True)}")
    if not run_record["engine"]["source_fingerprint_matches_compiled_stamp"]:
        print(
            "WARNING: the compiled extension stamp does not match the current Rust sources; "
            "the report must not claim a verified engine identity",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())