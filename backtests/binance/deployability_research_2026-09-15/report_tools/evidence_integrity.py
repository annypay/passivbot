"""Versioned evidence checks without rewriting the completed primary screen."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from account_diagnostics import (
    FILL_COLUMNS, digest_json, drawdown, normalize_fills, performance, save_json, sha256,
)


ROOT = Path(__file__).resolve().parents[1]
NATIVE_FILES = (
    "config.json", "fills.csv", "minute_equity.npy", "native_analysis.json",
    "execution_audit.csv",
)
REQUIRED_RUN_FILES = set(NATIVE_FILES) | {
    "summary.json", "account_path.npz", "closed_inventory_ages.csv",
    "position_episodes.csv", "coin_concentration.csv",
}
SOURCE_FIELDS = {
    "runner_source_sha256": "run_deployability_study.py",
    "diagnostics_source_sha256": "account_diagnostics.py",
    "diagnostics_tool_sha256": "account_diagnostics.py",
    "sidecar_tool_sha256": "run_execution_and_portfolios.py",
    "execution_producer_sha256": "run_execution_and_portfolios.py",
    "evidence_integrity_source_sha256": "evidence_integrity.py",
}


def verify_artifact_hashes(folder: Path, hashes: dict, required=()) -> None:
    if not hashes or not set(required).issubset(hashes):
        raise AssertionError(f"required artifacts are not covered by manifest: {folder}")
    for name, checksum in hashes.items():
        if Path(name).name != name or name in (".", ".."):
            raise ValueError(f"artifact is not a direct child: {name}")
        if sha256(folder / name) != checksum:
            raise AssertionError(f"artifact checksum differs: {folder / name}")


def verify_completed_run(folder: Path, expected_identity: dict | None = None) -> dict:
    complete = json.loads((folder / "completion.json").read_text())
    identity = complete["identity"]
    if expected_identity is not None and identity != expected_identity:
        raise AssertionError(f"completed replay identity differs: {folder}")
    if ("artifact_hashes" in complete) == ("hashes" in complete):
        raise AssertionError("completion must contain exactly one artifact hash map")
    hashes = complete["artifact_hashes"] if "artifact_hashes" in complete else complete["hashes"]
    verify_artifact_hashes(folder, hashes, REQUIRED_RUN_FILES)
    config = json.loads((folder / "config.json").read_text())
    if digest_json(config) != identity["config_hash"]:
        raise AssertionError(f"completed config identity differs: {folder}")
    contract_path = ROOT / "research_contract.json"
    contract = json.loads(contract_path.read_text())
    contract_key = ("contract_sha256" if "contract_sha256" in identity
                    else "research_contract_sha256")
    if identity[contract_key] != sha256(contract_path):
        raise AssertionError("completed run uses a different research contract")
    if identity["dataset_manifest_sha256"] != contract["dataset_manifest_sha256"]:
        raise AssertionError("completed run uses a different dataset")
    if identity["runtime"] != contract["runtime"]:
        raise AssertionError("completed run uses a different runtime")
    coins = identity["prepared_coins"]
    if coins != sorted(set(coins)) or config["backtest"]["coins"]["binance"] != coins:
        raise AssertionError("completed coin labels are not in canonical prepared order")
    for key, name in SOURCE_FIELDS.items():
        if key in identity and identity[key] != sha256(Path(__file__).with_name(name)):
            raise AssertionError(f"recorded producer source differs: {name}")
    if identity.get("evidence_schema_version") == 2:
        verify_artifact_hashes(folder, hashes, ("execution_identity.json", "native_complete.json"))
        if json.loads((folder / "execution_identity.json").read_text()) != identity:
            raise AssertionError("pre-execution identity differs from completed identity")
        verify_native_complete(folder, identity)
    return complete


def verify_native_complete(folder: Path, identity: dict) -> None:
    native = json.loads((folder / "native_complete.json").read_text())
    if native["execution_identity_sha256"] != digest_json(identity):
        raise AssertionError("native artifacts belong to a different execution identity")
    verify_artifact_hashes(folder, native["hashes"], NATIVE_FILES)


def execute_or_recover(
    folder: Path, identity: dict, execute: Callable,
) -> tuple[pd.DataFrame, np.ndarray, dict]:
    """Never certify an incomplete old ledger under newly edited producer source."""
    lock = folder / "execution_identity.json"
    native_outputs = [folder / name for name in NATIVE_FILES if name != "config.json"]
    if any(path.exists() for path in native_outputs) and not lock.exists():
        raise RuntimeError(f"native outputs lack pre-execution identity; quarantine run: {folder}")
    save_json(lock, identity, immutable=True)
    if (folder / "native_complete.json").exists():
        verify_native_complete(folder, identity)
        fills = normalize_fills(pd.read_csv(folder / "fills.csv"))
        raw = np.load(folder / "minute_equity.npy", allow_pickle=False)
        native = json.loads((folder / "native_analysis.json").read_text())
        return fills, raw, native
    if any(path.exists() for path in native_outputs):
        raise RuntimeError(f"partially written native replay; quarantine before rerun: {folder}")
    source_fills, raw, native = execute()
    frame = pd.DataFrame(source_fills, columns=FILL_COLUMNS)
    frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="raise")
    fills = normalize_fills(frame)
    np.save(folder / "minute_equity.npy", raw, allow_pickle=False)
    fills[FILL_COLUMNS].to_csv(folder / "fills.csv", index=False)
    save_json(folder / "native_analysis.json", native, immutable=True)
    save_json(folder / "native_complete.json", {
        "execution_identity_sha256": digest_json(identity),
        "hashes": {name: sha256(folder / name) for name in NATIVE_FILES},
    }, immutable=True)
    return fills, raw, native


def finish_run(folder: Path, identity: dict) -> None:
    paths = sorted(path for path in folder.iterdir()
                   if path.is_file() and path.name != "completion.json")
    save_json(folder / "completion.json", {
        "identity": identity,
        "artifact_hashes": {path.name: sha256(path) for path in paths},
    }, immutable=True)
    verify_completed_run(folder, identity)


def halfyear_metrics(account: pd.DataFrame, initial: float, edges: list[str]) -> pd.DataFrame:
    previous_equity = previous_balance = initial
    rows = []
    for start, end in zip(edges[:-1], edges[1:]):
        group = account.loc[
            (account.index >= pd.Timestamp(start, tz="UTC"))
            & (account.index < pd.Timestamp(end, tz="UTC"))]
        if group.empty:
            raise ValueError(f"missing requested half-year account history: {start} to {end}")
        metrics = performance(group, previous_equity)
        times = group.index.astype("int64").to_numpy() // 1_000_000
        metrics["balance_mdd"] = drawdown(
            group["balance"].to_numpy(), times, previous_balance)["mdd"]
        rows.append({"start": start, "end": end, **metrics})
        previous_equity = float(group["equity"].iloc[-1])
        previous_balance = float(group["balance"].iloc[-1])
    return pd.DataFrame(rows)


def verify_all_runs() -> dict[str, str]:
    paths = list((ROOT / "runs").glob("*/*/completion.json"))
    paths += list((ROOT / "execution_and_portfolios/capital_50000").glob("*/*/completion.json"))
    if not paths:
        raise RuntimeError("no completed replay evidence")
    result = {}
    for path in sorted(paths):
        verify_completed_run(path.parent)
        result[str(path.relative_to(ROOT))] = sha256(path)
    return result
