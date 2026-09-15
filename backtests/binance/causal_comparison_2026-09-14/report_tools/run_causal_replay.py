"""Local study driver; uses repository APIs and immutable prepared Binance inputs."""

import argparse
import asyncio
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import time


ORIGINAL = Path("backtests/binance/2026-09-14T03_40_41")
STUDY = Path("backtests/binance/causal_comparison_2026-09-14")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True, indent=2, allow_nan=False)
        handle.write("\n")


def freeze():
    from rust_utils import check_and_maybe_compile

    check_and_maybe_compile(fail_on_stale=True)
    import passivbot_rust
    from rust_utils import verify_loaded_runtime_extension

    runtime = verify_loaded_runtime_extension()
    dataset = json.loads((ORIGINAL / "dataset.json").read_text())
    config = json.loads((ORIGINAL / "config.json").read_text())
    cache = Path(dataset["hlcv_cache_dir"])
    manifest = json.loads((cache / "manifest.json").read_text())
    settings = json.loads((cache / "market_specific_settings.json").read_text())
    study = {
        "original_result": str(ORIGINAL),
        "dataset_relative_path": str(cache.relative_to(Path.cwd())),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "rust_source_fingerprint": runtime["expected_source_fingerprint"],
        "rust_artifact_sha256": runtime["runtime_compiled_sha256"],
        "rust_build_info": passivbot_rust.runtime_build_info(),
        "original_artifact_sha256": {
            name: file_hash(ORIGINAL / name)
            for name in (
                "config.json", "dataset.json", "fills.csv", "analysis.json",
                "annual_analysis.md", "balance_and_equity.csv.gz",
                "backtest_lookahead_and_candle_boundary_audit.md",
            )
        },
        "dataset_file_sha256": {
            entry["path"]: file_hash(cache / entry["path"])
            for entry in manifest["files"].values()
        },
        "dataset_logical_hashes": dataset["content_hashes"],
        "manifest_sha256": file_hash(cache / "manifest.json"),
        "window": {
            "start": config["backtest"]["start_date"],
            "end_exclusive": config["backtest"]["end_date"],
            "warmup_minutes": json.loads((cache / "cache_meta.json").read_text())[
                "warmup_minutes"
            ],
        },
        "coverage": {
            coin: {
                key: value
                for key, value in settings[coin].items()
                if key.startswith(("coverage_", "source_"))
                or key in (
                    "first_valid_index", "last_valid_index", "trade_start_index",
                    "warmup_minutes", "synthetic_gap_fill_count", "synthetic_gap_fill_source",
                )
            }
            for coin in dataset["coins"]
        },
        "scenario_contract": {
            "B0": {"engine": "original", "delay": 0, "ordering": "close_first"},
            "C1": {"engine": "causal", "delay": 0, "ordering": "close_first"},
            "C2": {"engine": "causal", "delay": 0, "ordering": "entry_first"},
            "C3": {"engine": "causal", "delay": 1, "ordering": "close_first"},
            "C4": {"engine": "causal", "delay": 1, "ordering": "entry_first"},
        },
    }
    STUDY.mkdir(exist_ok=False)
    write_json(STUDY / "frozen_inputs.json", study)
    print("Frozen immutable study inputs:", STUDY, flush=True)


def forbid_network(*args, **kwargs):
    raise RuntimeError("Network is forbidden during frozen-dataset replay")


def audit_raw_cache(coins, hlcvs, mss, timestamps):
    import numpy as np

    connection = sqlite3.connect("file:caches/ohlcvs/catalog.sqlite?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    summaries = []
    for idx, coin in enumerate(coins):
        first = int(mss[coin]["first_valid_index"])
        last = int(mss[coin]["last_valid_index"])
        matched = np.zeros(last - first + 1, dtype=bool)
        mismatch = 0
        checked_chunks = 0
        records = connection.execute(
            "SELECT * FROM chunks WHERE exchange=? AND timeframe=? AND symbol=? "
            "AND end_ts>=? AND start_ts<=? ORDER BY start_ts",
            ("binance", "1m", mss[coin]["symbol"], int(timestamps[first]), int(timestamps[last])),
        )
        for record in records:
            body = np.load(record["body_path"], mmap_mode="r", allow_pickle=False)
            valid = np.load(record["valid_path"], mmap_mode="r", allow_pickle=False)
            digest = hashlib.sha256()
            for array in (body, valid):
                digest.update(str(array.dtype).encode())
                digest.update(str(array.shape).encode())
                digest.update(array.data)
            if digest.hexdigest() != record["checksum"]:
                raise ValueError(f"Raw cache checksum mismatch: {coin} {record['year']}-{record['month']}")
            start_ms = max(int(record["start_ts"]), int(timestamps[first]))
            end_ms = min(int(record["end_ts"]), int(timestamps[last]))
            src_start = (start_ms - int(record["start_ts"])) // 60000
            count = (end_ms - start_ms) // 60000 + 1
            dst_start = (start_ms - int(timestamps[0])) // 60000
            valid_rows = valid[src_start:src_start + count]
            expected = body[src_start:src_start + count]
            actual = hlcvs[dst_start:dst_start + count, idx]
            equal = np.all(actual == expected, axis=1)
            mismatch += int(np.count_nonzero(valid_rows & ~equal))
            matched[dst_start-first:dst_start-first+count] |= valid_rows & equal
            checked_chunks += 1
            del body, valid
        summaries.append({
            "coin": coin,
            "declared_valid_rows": last-first+1,
            "matched_valid_v2_rows": int(matched.sum()),
            "unmatched_rows": int(np.count_nonzero(~matched)),
            "mismatching_valid_rows": mismatch,
            "checksummed_v2_chunks": checked_chunks,
            "excluded_leading_storage_rows": first,
            "excluded_trailing_storage_rows": len(timestamps)-last-1,
            "source_evidence": "checksummed_binance_v2_cache_not_per_row_archive_provenance",
        })
    connection.close()
    with (STUDY / "data_provenance_audit.csv").open("x", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    mismatches = sum(row["mismatching_valid_rows"] for row in summaries)
    absent = sum(row["unmatched_rows"] for row in summaries)
    print("Raw-cache comparison: mismatched=", mismatches, "unmatched=", absent, flush=True)
    if mismatches or absent:
        raise ValueError("Frozen active inputs differ from verified v2 cache; inspect provenance before replay")


async def replay(scenario, smoke_bars):
    socket.socket.connect = forbid_network
    socket.socket.connect_ex = forbid_network
    socket.create_connection = forbid_network

    import numpy as np
    import backtest
    from config import load_prepared_config
    from logging_setup import configure_logging
    from rust_utils import verify_loaded_runtime_extension

    configure_logging(debug=0)
    frozen = json.loads((STUDY / "frozen_inputs.json").read_text())
    runtime = verify_loaded_runtime_extension()
    if scenario == "B0":
        assert runtime["expected_source_fingerprint"] == frozen["rust_source_fingerprint"]
        assert runtime["runtime_compiled_sha256"] == frozen["rust_artifact_sha256"]
    scenario_root = STUDY / (f"{scenario}_smoke" if smoke_bars else scenario)
    scenario_root.mkdir(exist_ok=False)
    contract = frozen["scenario_contract"][scenario]
    config = load_prepared_config(str(ORIGINAL / "config.json"), verbose=False)
    config["_original_backtest_config"] = deepcopy(config)
    config["backtest"]["hlcvs_data_dir"] = frozen["dataset_relative_path"]
    config["backtest"]["hlcvs_data_override_mode"] = "dataset"
    config["backtest"]["base_dir"] = str(scenario_root)
    config["backtest"]["balance_sample_divider"] = 1
    config["disable_plotting"] = "all"
    if scenario != "B0":
        config["backtest"]["execution_delay_bars"] = contract["delay"]
        config["backtest"]["intrabar_fill_order"] = contract["ordering"]
        config["backtest"]["execution_audit_path"] = str(
            scenario_root / "execution_boundary_audit.csv"
        )
    config.setdefault("backtest", {}).setdefault("coins", {})
    config["backtest"].setdefault("cache_dir", {})
    start = time.perf_counter()
    print("Loading verified frozen dataset for", scenario, flush=True)
    coins, hlcvs, mss, results_path, cache_dir, btc, timestamps = (
        await backtest.prepare_hlcvs_mss(config, "binance")
    )
    config["backtest"]["coins"]["binance"] = coins
    config["backtest"]["cache_dir"]["binance"] = str(cache_dir)
    if scenario == "B0" and not smoke_bars:
        audit_raw_cache(coins, hlcvs, mss, timestamps)
    if smoke_bars:
        end = min(len(timestamps), frozen["window"]["warmup_minutes"] + smoke_bars + 2)
        hlcvs = hlcvs[:end]
        btc = btc[:end]
        timestamps = timestamps[:end]
        config["backtest"]["end_date"] = backtest.ts_to_date(int(timestamps[-1]))
        for coin in coins:
            mss[coin]["last_valid_index"] = min(mss[coin]["last_valid_index"], end - 1)
            mss[coin]["source_last_valid_index"] = min(
                mss[coin]["source_last_valid_index"], end - 1
            )
    print("Executing", scenario, "shape", hlcvs.shape, flush=True)
    fills, equity, analysis, payload = backtest.run_backtest(
        hlcvs, mss, config, "binance", btc, timestamps, return_payload=True
    )
    np.save(scenario_root / "minute_equity.npy", equity, allow_pickle=False)
    backtest.post_process(
        config, hlcvs, fills, equity, btc, analysis, results_path, "binance",
        plot_context=backtest.BacktestPlotContext.from_payload(payload),
    )
    result_dirs = sorted(p.parent for p in scenario_root.glob("binance/*/analysis.json"))
    assert len(result_dirs) == 1, result_dirs
    identity = {
        "scenario": scenario,
        "contract": contract,
        "result_dir": str(result_dirs[0]),
        "smoke_bars": smoke_bars,
        "runtime_source_fingerprint": runtime["expected_source_fingerprint"],
        "runtime_artifact_sha256": runtime["runtime_compiled_sha256"],
        "source_manifest_sha256": frozen["manifest_sha256"],
        "dataset_logical_hashes": frozen["dataset_logical_hashes"],
        "effective_shape": list(hlcvs.shape),
        "elapsed_seconds": time.perf_counter() - start,
        "network_disabled": True,
    }
    write_json(scenario_root / "run_identity.json", identity)
    print("Completed", scenario, result_dirs[0], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["freeze", "run"])
    parser.add_argument("--scenario", choices=["B0", "C1", "C2", "C3", "C4"], default="B0")
    parser.add_argument("--smoke-bars", type=int, default=0)
    args = parser.parse_args()
    if args.action == "freeze":
        freeze()
    else:
        asyncio.run(replay(args.scenario, args.smoke_bars))
