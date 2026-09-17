#!/usr/bin/env python3
"""Run one arm of the g4 @ TWE 3.0 account-guard study offline and write its provenance.

The runner drives the canonical backtest entrypoint (`src/backtest.py`) with the frozen
config `build_variant_config.py` produced, for exactly one arm of the comparison, and
refuses to accept anything that is not the frozen parent g4 config plus that arm's declared
changes served by that arm's frozen HLCV bundle:

* the arm's frozen dataset directory must contain every declared artifact,
* the run's own `dataset.json` must record that exact cache and the frozen coin basket,
* an override leg must report `dataset_override_mode = "dataset"` and the declared window,
* the liquidation evidence must match the arm's declared capital and floor,
* the account-guard telemetry (`hard_stop_*`) must be recorded for the study's analysis,
* the run must not load or rebuild a different HLCV dataset (no fetch markers),
* the gate must still be the declared, enabled 20/50 gate,
* the HSL block, the modelled `live.hsl_signal_mode`, the risk block and the execution/cost
  contract must equal the arm's declaration field by field.

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
from typing import Any

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
#: per-coin panels unless the arm disables that group; the three metric CSVs and the report
#: are written afterwards by the renderer, so they are not required here.
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
    if variant.key in spec.REUSED_ARM_KEYS:
        fail(
            [
                f"{variant.key} is a pinned reference arm from an earlier study; "
                "it is not re-run here"
            ],
            "refusing to run a reference arm",
        )
    if not variant.config_path.exists():
        problems.append(
            f"missing {spec.relative(variant.config_path)}; run build_variant_config.py first"
        )
    if not spec.VARIANT_INPUT_PATH.exists():
        problems.append(
            f"missing {spec.relative(spec.VARIANT_INPUT_PATH)}; run build_variant_config.py first"
        )
    dataset = variant.dataset
    for name in spec.FROZEN_CACHE_FILES:
        if not (dataset.path / name).exists():
            problems.append(f"missing dataset file {dataset.rel_path / name}")
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
        *spec.runtime_flags(variant),
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


def dataset_artifact_hashes(dataset: spec.DatasetSpec, *, full: bool = False) -> dict:
    """Hash the frozen dataset's logical arrays and gzip bytes.

    The bundle manifest records hashes of the *logical* NumPy payloads
    (`hlcvs_manifest.hash_logical_array`), not of the gzip byte stream, so the frozen
    comparison must use the same convention. The `hlcvs` array is 2-4 GB of float64 on the
    long leg, so its logical hash is only recomputed on request (`--full-array-hash`); the
    manifest hash chain remains pinned either way.
    """
    sys.path.insert(0, str(spec.REPO / "src"))
    from hlcvs_manifest import hash_logical_array  # noqa: E402
    from tools.verify_hlcvs_data import load_npy_gz  # noqa: E402

    out: dict = {"logical": {}, "file_bytes": {}, "shapes": {}, "dtypes": {}}
    artifacts = [
        ("timestamps.npy.gz", "timestamps"),
        ("btc_usd_prices.npy.gz", "btc_usd_prices"),
    ]
    if full:
        artifacts.append(("hlcvs.npy.gz", "hlcvs"))
    for name, manifest_key in artifacts:
        path = dataset.path / name
        array = load_npy_gz(path)
        out["logical"][manifest_key] = hash_logical_array(array)
        out["shapes"][manifest_key] = [int(dim) for dim in array.shape]
        out["dtypes"][manifest_key] = str(array.dtype)
        del array
    for name, manifest_key in (
        ("hlcvs.npy.gz", "hlcvs"),
        ("timestamps.npy.gz", "timestamps"),
        ("btc_usd_prices.npy.gz", "btc_usd_prices"),
    ):
        out["file_bytes"][manifest_key] = spec.sha256_file(dataset.path / name)
    return out


def disabled_plot_groups(config: dict) -> set[str]:
    """Figure groups a run's config disables, mirroring the report convention's parser."""
    return spec.disabled_plot_groups((config.get("backtest", {}) or {}).get("disable_plotting"))


def check_run_config(variant: spec.Variant, config: dict, run_config: dict) -> list[str]:
    """The dump must be this arm's frozen config, with only declared differences."""
    problems: list[str] = []
    # A run served by a dataset override dumps the config with `strip_config_metadata`, which
    # keeps `coins`/`cache_dir`/`hlcvs_data_dir` (as resolved host paths); every other arm
    # dumps it through `sanitize_prepared_config_for_dump`, which clears them. Compare
    # against the matching normalization so the check stays exact in both shapes.
    normalized = (
        config if variant.dataset.override_mode else spec.normalize_config_payload(config)
    )
    declared_in_roots: dict[str, tuple[str, ...]] = {}
    for dotted, _from, to in variant.deltas:
        root = dotted.split(".")[0]
        declared_in_roots[root] = (*declared_in_roots.get(root, ()), dotted)
    for root in ("bot", "live", "coin_overrides"):
        found = spec.diff_run_config_root(
            normalized[root], run_config[root], root, declared=declared_in_roots.get(root, ())
        )
        problems.extend(f"run config {problem}" for problem in found)
    run_approved = (run_config.get("live") or {}).get("approved_coins") or {}
    if sorted(run_approved.get("long") or []) != sorted(
        ((config.get("live") or {}).get("approved_coins") or {}).get("long") or []
    ):
        problems.append("run config live.approved_coins.long is not the frozen basket")
    if list(run_approved.get("short") or []):
        problems.append("run config live.approved_coins.short is not empty")
    for key in ("start_date", "end_date"):
        if not spec.same_day(
            normalized["backtest"].get(key), run_config["backtest"].get(key)
        ):
            problems.append(
                f"run config backtest.{key}: expected {normalized['backtest'].get(key)!r}, "
                f"got {run_config['backtest'].get(key)!r}"
            )
    if normalized["backtest"].get("exchanges") != run_config["backtest"].get("exchanges"):
        problems.append("run config backtest.exchanges does not match the frozen config")
    for key in spec.DUMP_CLEARED_BACKTEST_KEYS:
        if variant.dataset.override_mode is None:
            if run_config["backtest"].get(key) is not None:
                problems.append(
                    f"run config backtest.{key} = {run_config['backtest'].get(key)!r}; the dump is "
                    "expected to clear resolved dataset fields"
                )
    if variant.dataset.override_mode is not None:
        run_bt = run_config.get("backtest") or {}
        declared_coins = sorted(
            (config["backtest"].get("coins") or {}).get(spec.DATA_EXCHANGE) or []
        )
        run_coins = sorted((run_bt.get("coins") or {}).get(spec.DATA_EXCHANGE) or [])
        if run_coins != declared_coins:
            problems.append(
                f"run config backtest.coins differs from the frozen basket "
                f"({len(run_coins)} vs {len(declared_coins)} entries)"
            )
        for key in ("cache_dir", "hlcvs_data_dir"):
            actual = (
                (run_bt.get("cache_dir") or {}).get(spec.DATA_EXCHANGE)
                if key == "cache_dir"
                else run_bt.get("hlcvs_data_dir")
            )
            if actual is None or Path(str(actual)).name != variant.dataset.path.name:
                problems.append(
                    f"run config backtest.{key} = {actual!r}; expected the declared bundle "
                    f"{variant.dataset.path.name!r}"
                )
    run_bt = run_config.get("backtest") or {}
    for key, expected in (*spec.EXECUTION.items(), *spec.COSTS.items()):
        if not spec.numeric_equal(run_bt.get(key), expected):
            problems.append(
                f"run config backtest.{key}: expected {expected!r}, got {run_bt.get(key)!r}"
            )
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
    run_hsl = ((run_config.get("bot") or {}).get("long") or {}).get("hsl") or {}
    problems.extend(
        f"run config {problem}"
        for problem in spec.compare_subtrees(
            spec.expected_hsl_block(variant), run_hsl, "bot.long.hsl"
        )
    )
    run_live = run_config.get("live") or {}
    for key, expected in spec.expected_live_hsl(variant).items():
        if run_live.get(key) != expected:
            problems.append(f"run config live.{key}: expected {expected!r}, got {run_live.get(key)!r}")
    for key, expected in spec.expected_live_config(variant).items():
        if not spec.numeric_equal(run_live.get(key), expected):
            problems.append(f"run config live.{key}: expected {expected!r}, got {run_live.get(key)!r}")
    run_risk = ((run_config.get("bot") or {}).get("long") or {}).get("risk") or {}
    expected_risk = spec.expected_risk_block(variant)
    problems.extend(
        f"run config {problem}"
        for problem in spec.compare_subtrees(
            expected_risk, {key: run_risk.get(key) for key in expected_risk}, "bot.long.risk"
        )
    )
    return problems


def check_dataset_override(variant: spec.Variant, dataset_dump: dict, analysis: dict) -> list[str]:
    """An override leg must prove the run used the declared frozen bundle."""
    problems: list[str] = []
    if variant.dataset.override_mode is None:
        if dataset_dump.get("dataset_override"):
            problems.append("run dataset.json claims a dataset override the arm does not declare")
        return problems
    if not dataset_dump.get("dataset_override"):
        problems.append("run dataset.json does not report a dataset override")
    if dataset_dump.get("dataset_override_mode") != variant.dataset.override_mode:
        problems.append(
            f"run dataset.json dataset_override_mode = "
            f"{dataset_dump.get('dataset_override_mode')!r}, expected "
            f"{variant.dataset.override_mode!r}"
        )
    starts = str(dataset_dump.get("requested_start_date") or "")[:10]
    ends = str(dataset_dump.get("requested_end_date") or "")[:10]
    if starts and starts != variant.dataset.window[0]:
        problems.append(
            f"run dataset.json requested_start_date {starts!r} != declared window start "
            f"{variant.dataset.window[0]!r}"
        )
    if ends and ends != variant.dataset.window[1]:
        problems.append(
            f"run dataset.json requested_end_date {ends!r} != declared window end "
            f"{variant.dataset.window[1]!r}"
        )
    coins = dataset_dump.get("coins") or []
    if len(coins) != spec.COIN_COUNT:
        problems.append(f"run dataset.json has {len(coins)} coins, expected {spec.COIN_COUNT}")
    effective_start = str(analysis.get("effective_start_date") or "")[:10]
    effective_end = str(analysis.get("effective_end_date") or "")[:10]
    if effective_start and effective_start < variant.dataset.window[0]:
        problems.append(
            f"run analysis effective_start_date {effective_start!r} precedes the declared window "
            f"start {variant.dataset.window[0]!r}"
        )
    if effective_end and effective_end > variant.dataset.window[1]:
        problems.append(
            f"run analysis effective_end_date {effective_end!r} follows the declared window end "
            f"{variant.dataset.window[1]!r}"
        )
    return problems


def liquidation_evidence(
    variant: spec.Variant, analysis: dict, run_dir: Path
) -> dict[str, Any]:
    """Independent evidence about the engine's liquidation floor for this arm.

    The engine ends a run once equity reaches `starting_balance * liquidation_threshold`,
    snapping the final sample to that floor (`passivbot-rust/src/backtest.rs`). This reads the
    arm's own equity series and checks the two shapes: a liquidated run must stop before the
    declared window end and end at (or below) the floor; a surviving run must end above it.
    """
    floor = spec.liquidation_floor_usd(variant)
    starting_balance = variant.starting_balance
    out: dict[str, Any] = {
        "liquidated": bool(analysis.get("liquidated")),
        "floor_usd": floor,
        "starting_balance": starting_balance,
        "problems": [],
        "last_equity_usd": None,
        "last_sample_utc": None,
        "days_to_liquidation": None,
        "peak_equity_usd": None,
    }
    equity_path = run_dir / "balance_and_equity.csv.gz"
    if not equity_path.exists():
        out["problems"].append(f"missing {spec.relative(equity_path)}")
        return out
    import gzip

    with gzip.open(equity_path, "rt", encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
        if "usd_total_equity" not in header:
            out["problems"].append("equity series has no usd_total_equity column")
            return out
        equity_index = header.index("usd_total_equity")
        first_ts = None
        last_ts = None
        last_equity = None
        peak = None
        for line in handle:
            parts = line.rstrip("\n").split(",")
            if len(parts) <= equity_index:
                continue
            try:
                value = float(parts[equity_index])
            except ValueError:
                continue
            first_ts = first_ts or parts[0]
            last_ts = parts[0]
            last_equity = value
            peak = value if peak is None else max(peak, value)
    out["last_equity_usd"] = last_equity
    out["last_sample_utc"] = last_ts
    out["peak_equity_usd"] = peak
    if first_ts and last_ts:
        try:
            import datetime as dt

            start = dt.datetime.strptime(first_ts[:19], "%Y-%m-%d %H:%M:%S")
            end = dt.datetime.strptime(last_ts[:19], "%Y-%m-%d %H:%M:%S")
            out["days_to_liquidation"] = (end - start).total_seconds() / 86400.0
        except ValueError:
            out["problems"].append(f"unparsable equity timestamps {first_ts!r}/{last_ts!r}")
    if last_equity is None:
        out["problems"].append("equity series is empty")
        return out
    # The engine clamps its *internal* last equity sample to the floor and breaks the loop at
    # the liquidation bar; the persisted series is sampled every `balance_sample_divider`
    # minutes, so its last row can still be above the floor. The liquidation shape is
    # therefore checked against the engine's own numbers (liquidated flag, truncated window,
    # >=90% drawdown) plus the sampling lag between the last sample and the end date.
    if out["liquidated"]:
        if last_equity <= floor + max(1.0, floor * 0.01):
            out["problems"].append(
                f"liquidated but the last sampled equity {last_equity:.2f} is already at the "
                f"floor {floor:.2f}; the sampling lag should leave it above the floor"
            )
        worst_dd = analysis.get("drawdown_worst_strategy_eq")
        if worst_dd is None or float(worst_dd) < 0.9:
            out["problems"].append(
                f"liquidated but the engine's worst strategy-equity drawdown is {worst_dd!r}; "
                "a run that reached the 5% floor should show at least a 90% drawdown"
            )
        end_day = (out["last_sample_utc"] or "")[:10]
        window_end_day = variant.dataset.window[1]
        if end_day and end_day >= window_end_day:
            out["problems"].append(
                f"liquidated but the run reached the declared window end {end_day}"
            )
        out["last_sampled_equity_usd"] = last_equity
        out["drawdown_worst_strategy_eq"] = worst_dd
    else:
        if last_equity <= floor + max(1.0, floor * 0.01):
            out["problems"].append(
                f"not flagged liquidated but the last equity {last_equity:.2f} is at the floor "
                f"{floor:.2f}"
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        required=True,
        choices=spec.RUN_VARIANT_ORDER,
        help="which arm to run (reference arms are pinned, not run)",
    )
    parser.add_argument("--timeout-s", type=float, default=10800.0, help="backtest timeout")
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
    parser.add_argument(
        "--full-array-hash",
        action="store_true",
        help="also recompute the logical hash of the multi-GB hlcvs array",
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
    if missing:
        fail([f"missing artifact {name}" for name in missing], "replay artifacts are incomplete")

    run_config = spec.load_json(run_dir / "config.json")
    if "coin_fills" not in disabled_plot_groups(run_config) and not (run_dir / "fills_plots").is_dir():
        fail(
            ["missing artifact fills_plots/"],
            "replay artifacts are incomplete",
        )
    dataset_dump = spec.load_json(run_dir / "dataset.json")
    analysis = spec.load_json(run_dir / "analysis.json")

    problems: list[str] = []
    problems.extend(check_run_config(variant, config, run_config))
    problems.extend(check_dataset_override(variant, dataset_dump, analysis))
    run_disabled = disabled_plot_groups(run_config)
    if run_disabled != spec.declared_plot_groups(variant):
        problems.append(
            f"run config disables plot groups {sorted(run_disabled)}, but the arm declares "
            f"{sorted(spec.declared_plot_groups(variant))} (runtime flags "
            f"{list(spec.runtime_flags(variant))})"
        )
    liquidation = liquidation_evidence(variant, analysis, run_dir)
    problems.extend(f"liquidation: {problem}" for problem in liquidation["problems"])
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
    required_markers = (
        ("[hlcvs] override", "[hlcvs] verified manifest")
        if variant.dataset.override_mode
        else ("Loaded hlcvs data from cache",)
    )
    absent = [marker for marker in required_markers if marker not in log_text]
    if absent:
        fail(
            [f"the run log never reports {marker!r}" for marker in absent],
            "the replay did not read the frozen dataset; it may have fetched from the network",
        )
    fetch_hits = sorted({marker for marker in spec.NETWORK_FETCH_MARKERS if marker in log_text})
    if fetch_hits:
        fail(
            [f"run log contains fetch marker {marker!r}" for marker in fetch_hits],
            "the replay fetched market data; this study must run offline",
        )
    print("offline boundary: frozen dataset loaded from cache, no fetch markers in the log")

    manifest = spec.load_json(variant.dataset.path / "manifest.json")
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
    data_hashes = dataset_artifact_hashes(variant.dataset, full=args.full_array_hash)
    manifest_hashes = {
        key: manifest["files"][key]["sha256"]
        for key in ("hlcvs", "timestamps", "btc_usd_prices")
        if key in manifest.get("files", {})
    }
    hash_problems = [
        f"{manifest_key}: logical array hash {data_hashes['logical'].get(manifest_key)} != manifest "
        f"{manifest_hashes.get(manifest_key)}"
        for manifest_key in ("timestamps", "btc_usd_prices")
        if manifest_hashes.get(manifest_key) != data_hashes["logical"].get(manifest_key)
    ]
    if args.full_array_hash and manifest_hashes.get("hlcvs") != data_hashes["logical"].get("hlcvs"):
        hash_problems.append(
            f"hlcvs: logical array hash {data_hashes['logical'].get('hlcvs')} != manifest "
            f"{manifest_hashes.get('hlcvs')}"
        )
    if hash_problems:
        fail(hash_problems, "frozen dataset arrays do not match their manifest hashes")
    dataset_identity = {
        "key": variant.dataset.key,
        "leg": variant.dataset.leg,
        "synthetic": variant.dataset.synthetic,
        "cache_dir": spec.relative(variant.dataset.path),
        "frozen_bundle_label": variant.dataset.path.name,
        "override_mode": variant.dataset.override_mode,
        "declared_window": list(variant.dataset.window),
        "cache_dir_label": dataset_dump.get("cache_dir_label"),
        "cache_hash": dataset_dump.get("cache_hash"),
        "manifest_config_hash": manifest.get("config_hash"),
        "manifest_sha256": spec.sha256_file(variant.dataset.path / "manifest.json"),
        "manifest_pinned_sha256": variant.dataset.manifest_sha256,
        "coin_count": len(dataset_dump.get("coins") or []),
        "hash_convention": (
            "logical NumPy payload via hlcvs_manifest.hash_logical_array; file_bytes hashes the "
            "gzip byte stream for provenance only"
        ),
        "data_hashes": data_hashes["logical"],
        "file_bytes_hashes": data_hashes["file_bytes"],
        "array_shapes": data_hashes["shapes"],
        "array_dtypes": data_hashes["dtypes"],
        "manifest_hashes": manifest_hashes,
        "full_array_hash": bool(args.full_array_hash),
    }
    global_metrics = {
        "variant": variant.key,
        "variant_label": spec.LEVERS[variant.lever].label,
        "variant_description": variant.description,
        "disabled_plot_groups": sorted(disabled_plot_groups(run_config)),
        "runtime_flags": list(spec.runtime_flags(variant)),
        "lever": variant.lever,
        "leg": variant.leg,
        "synthetic": variant.synthetic,
        "run_dir": spec.relative(run_dir),
        "run_dir_name": run_dir.name,
        "run_base_dir": spec.relative(variant.base_dir),
        "config_sha256": spec.sha256_file(variant.config_path),
        "parent_config": spec.relative(spec.SOURCE_CONFIG),
        "parent_config_sha256": spec.SOURCE_CONFIG_SHA256,
        "declared_delta": [
            {"path": dotted, "from": _from, "to": to} for dotted, _from, to in variant.deltas
        ],
        "backtest_elapsed_s": elapsed_s,
        "execution_audit_rows": audit_lines,
        "analysis_sha256": spec.sha256_file(run_dir / "analysis.json"),
        "dataset": dataset_identity,
        "guard": variant.guard_params,
        "guard_label": variant.guard_label,
        "guard_telemetry": {
            key: value for key, value in analysis.items() if key.startswith("hard_stop_")
        },
        "capital": {
            "starting_balance": variant.starting_balance,
            "parent_starting_balance": spec.PARENT_STARTING_BALANCE,
            "scale_vs_parent": variant.starting_balance / spec.PARENT_STARTING_BALANCE,
            "starting_balance_path": spec.STARTING_BALANCE_PATH,
        },
        "liquidation": liquidation,
        "entry_regime_gate": run_config["backtest"].get("entry_regime_gate"),
        "hsl_long": run_config["bot"]["long"].get("hsl"),
        "risk_long": {
            key: (run_config["bot"]["long"].get("risk") or {}).get(key) for key in spec.PARENT_RISK
        },
        "exposure_caps": {
            "total_wallet_exposure_limit": variant.declared_twe,
            "n_positions": variant.declared_n_positions,
            "we_excess_allowance_pct": variant.declared_allowance_pct,
            "per_slot_cap": variant.per_slot_cap,
        },
        "live_hsl": {key: (run_config.get("live") or {}).get(key) for key in spec.LIVE_HSL},
        "live_max_realized_loss_pct": (run_config.get("live") or {}).get(
            "max_realized_loss_pct"
        ),
        "engine": provenance,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "executable": path_label(sys.executable),
        },
        "git": git_state(),
        "generated_at_unix_s": time.time(),
    }

    run_record = {
        "candidate_id": f"g4_account_guard_{variant.key}",
        "candidate_kind": "account_guard_arm_replay",
        "variant": {
            "key": variant.key,
            "lever": variant.lever,
            "lever_label": spec.LEVERS[variant.lever].label,
            "leg": variant.leg,
            "dataset": variant.dataset.key,
            "synthetic": variant.synthetic,
            "description": variant.description,
            "hsl_enabled": variant.hsl_enabled,
            "starting_balance": variant.starting_balance,
            "total_wallet_exposure_limit": variant.declared_twe,
            "per_slot_cap": variant.per_slot_cap,
            "guard": variant.guard_params,
            "guard_label": variant.guard_label,
        },
        "source_config": spec.relative(spec.SOURCE_CONFIG),
        "source_config_sha256": spec.SOURCE_CONFIG_SHA256,
        "candidate_config_path": spec.relative(variant.config_path),
        "candidate_config_sha256": global_metrics["config_sha256"],
        "runtime_flags": list(spec.runtime_flags(variant)),
        "disabled_plot_groups": global_metrics["disabled_plot_groups"],
        "declared_delta": global_metrics["declared_delta"],
        "capital": global_metrics["capital"],
        "liquidation": global_metrics["liquidation"],
        "guard": global_metrics["guard"],
        "guard_telemetry": global_metrics["guard_telemetry"],
        "exposure_caps": global_metrics["exposure_caps"],
        "hsl_block": global_metrics["hsl_long"],
        "risk_block": global_metrics["risk_long"],
        "live_hsl": global_metrics["live_hsl"],
        "entry_regime_gate": global_metrics["entry_regime_gate"],
        "window": {
            "start_date": config["backtest"]["start_date"],
            "end_date": config["backtest"]["end_date"],
            "effective_start_date": analysis.get("effective_start_date"),
            "effective_end_date": analysis.get("effective_end_date"),
        },
        "universe": {
            "exchange": spec.DATA_EXCHANGE,
            "coin_count": len(dataset_dump.get("coins") or []),
            "coins": sorted(dataset_dump.get("coins") or []),
        },
        "execution": spec.EXECUTION,
        "costs": spec.COSTS,
        "dataset": global_metrics["dataset"],
        "comparison": {
            "variant_input": spec.relative(spec.VARIANT_INPUT_PATH),
            "variant_input_sha256": spec.sha256_file(spec.VARIANT_INPUT_PATH),
            "reference_arms": {
                key: {
                    "run_dir": spec.relative(Path(item["run_dir"])),
                    "analysis_sha256": item["analysis_sha256"],
                }
                for key, item in spec.REFERENCE_RUNS.items()
            },
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
            "Every arm of this study runs on the same engine revision and the same execution and "
            "cost contract; the reference arms were produced by the same engine revision earlier, "
            "which is what makes the columns comparable."
        ),
    }

    spec.write_json(run_dir / "global_metrics.json", global_metrics)
    spec.write_json(run_dir / "run_record.json", run_record)
    print(f"wrote {spec.relative(run_dir / 'global_metrics.json')}")
    print(f"wrote {spec.relative(run_dir / 'run_record.json')}")
    print(
        f"variant={variant.key} leg={variant.leg} fills={actual_fills} "
        f"coins={dataset_identity['coin_count']} audit_rows={audit_lines} "
        f"elapsed={elapsed_s:.1f}s"
    )
    print(
        "engine fingerprint="
        f"{provenance.get('source_fingerprint')} "
        f"compiled_stamp={provenance.get('compiled_source_stamp')} "
        f"loaded_stamp={provenance.get('runtime_module_source_stamp')}"
    )
    print(f"hsl_long={json.dumps(global_metrics['hsl_long'], sort_keys=True)}")
    print(f"guard={variant.guard_label}")
    if not run_record["engine"]["source_fingerprint_matches_compiled_stamp"]:
        print(
            "WARNING: the compiled extension stamp does not match the current Rust sources; "
            "the report must not claim a verified engine identity",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
