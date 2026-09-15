#!/usr/bin/env python3
"""Run the locked final holdout with full causal replay artifacts.

This tool is intentionally offline.  It only reads the frozen local Binance
H/L/C/V cache and the locked research artifacts; it does not access an account,
credentials, a network, or an exchange.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
from typing import Any

import numpy as np
import pandas as pd


REPO = Path(__file__).resolve().parents[4]
STUDY = Path(__file__).resolve().parents[1]
OUTPUT = STUDY / "final_replays"
PARAMETER_TOOL = Path(__file__).with_name("run_maxdd_parameter_study.py")
PARAMETER_LOCK = (
    STUDY / "locks" / "FINAL" / "trailing_martingale_candidate_lock.json"
)
PARAMETER_CONTEXT = STUDY / "locks" / "FINAL" / "parameter_selection_context.json"
PATH_DECISION = STUDY / "strategy_path_decision_lock.json"
MFE_DECISION = STUDY / "mfe_research" / "tm_mfe_path_decision_lock.json"
MFE_LOCK = STUDY / "mfe_research" / "locks" / "FINAL" / "tm_final_candidate_lock.json"

# C1-C4 are full diagnostic replays for the primary tracks.  B-LS and U-40 are
# descriptive C1-only portfolio/generalization checks and do not affect selection.
SCENARIOS = (
    ("C1", "E-L"),
    ("C1", "E-S"),
    ("C1", "B-L"),
    ("C1", "B-LS"),
    ("C1", "U-40"),
    ("C2", "E-L"),
    ("C2", "E-S"),
    ("C2", "B-L"),
    ("C3", "E-L"),
    ("C3", "E-S"),
    ("C3", "B-L"),
    ("C4", "E-L"),
    ("C4", "E-S"),
    ("C4", "B-L"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, value: Any, *, locked: bool = False) -> None:
    payload = canonical_json(value) if locked else json.dumps(
        value, ensure_ascii=True, indent=2, sort_keys=True
    )
    payload += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if locked and path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise RuntimeError(f"refusing to replace existing lock: {path}")
        return
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def reject_network(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("network is forbidden during the final causal replay")


def disable_network() -> None:
    socket.socket.connect = reject_network
    socket.socket.connect_ex = reject_network
    socket.create_connection = reject_network


def load_parameter_tool():
    spec = importlib.util.spec_from_file_location(
        "maxdd_parameter_study_final_replay", PARAMETER_TOOL
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load parameter study tool: {PARAMETER_TOOL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def scenario_name(execution: str, track: str) -> str:
    return f"{execution}_{track.lower().replace('-', '_')}"


def locked_inputs(tool: Any) -> tuple[dict[str, Any], dict[str, str]]:
    path_decision = read_json(PATH_DECISION)
    parameter_lock = read_json(PARAMETER_LOCK)
    parameter_context = read_json(PARAMETER_CONTEXT)
    mfe_decision = read_json(MFE_DECISION)
    mfe_lock = read_json(MFE_LOCK)

    if path_decision.get("selected_strategy") != "trailing_martingale":
        raise RuntimeError("final replay requires the locked trailing_martingale path")
    if parameter_lock.get("selection_opened_validation") is not False:
        raise RuntimeError("final parameter lock unexpectedly opened the holdout")
    if parameter_lock.get("train_window") != list(tool.FINAL_TRAIN):
        raise RuntimeError("final parameter lock has the wrong training window")
    if parameter_context.get("final_holdout_unopened") is not True:
        raise RuntimeError("final parameter selection context does not preserve holdout")
    if parameter_context.get("strategy_path_decision_lock_sha256") != sha256(
        PATH_DECISION
    ):
        raise RuntimeError("final parameter selection context has stale path provenance")
    if parameter_context.get("parameter_tool_sha256") != sha256(PARAMETER_TOOL):
        raise RuntimeError("final parameter selection context has stale tool provenance")
    if mfe_decision.get("mfe_enabled_for_final_training") is not False:
        raise RuntimeError("final replay only supports the locked disabled-MFE decision")
    if mfe_lock.get("parameter_candidate_lock_sha256") != sha256(PARAMETER_LOCK):
        raise RuntimeError("final MFE lock does not reference the final parameter lock")
    if mfe_lock.get("mfe_path_decision_lock_sha256") != sha256(MFE_DECISION):
        raise RuntimeError("final MFE lock has stale MFE-decision provenance")
    if mfe_lock.get("selected_params") != parameter_lock.get("selected_params"):
        raise RuntimeError("disabled MFE lock changed the locked parameter-only configuration")
    if mfe_lock.get("selected_params_hash") != parameter_lock.get("selected_params_hash"):
        raise RuntimeError("final parameter and MFE hashes differ")
    params = parameter_lock["selected_params"]
    if float(params["close_retracement_base_pct"]) != 0.0:
        raise RuntimeError("disabled MFE decision must retain the parameter-only close mode")

    provenance = {
        "strategy_path_decision_lock_sha256": sha256(PATH_DECISION),
        "parameter_candidate_lock_sha256": sha256(PARAMETER_LOCK),
        "parameter_selection_context_sha256": sha256(PARAMETER_CONTEXT),
        "mfe_path_decision_lock_sha256": sha256(MFE_DECISION),
        "mfe_final_candidate_lock_sha256": sha256(MFE_LOCK),
        "parameter_tool_sha256": sha256(PARAMETER_TOOL),
        "selected_params_hash": parameter_lock["selected_params_hash"],
    }
    return params, provenance


def configure_special_track(
    tool: Any,
    params: dict[str, Any],
    execution: str,
    track: str,
) -> dict[str, Any]:
    """Build a deterministic locked-config replay for a named diagnostic track."""

    tool.install_state({"coins": list(tool.BASKET), "cache_dir": ""})
    if track in {"E-L", "E-S", "B-L"}:
        return tool.candidate_config(
            "trailing_martingale",
            params,
            tool.FINAL_HOLDOUT[0],
            tool.FINAL_HOLDOUT[1],
            track=track,
            execution=execution,
        )

    config = tool.candidate_config(
        "trailing_martingale",
        params,
        tool.FINAL_HOLDOUT[0],
        tool.FINAL_HOLDOUT[1],
        track="B-L",
        execution=execution,
    )
    if track == "B-LS":
        # Keep combined requested gross exposure at the selected single-side TWE.
        # This diagnostic does not alter or validate the selected one-sided path.
        for side in ("long", "short"):
            risk = config["bot"][side]["risk"]
            risk["n_positions"] = float(params["n_positions"])
            risk["total_wallet_exposure_limit"] = float(params["twe"]) / 2.0
            risk["entry_cooldown_minutes"] = float(params["entry_cooldown_minutes"])
            risk["position_exposure_enforcer_enabled"] = True
            risk["position_exposure_enforcer_threshold"] = float(
                params["enforcer_threshold"]
            )
            risk["total_exposure_enforcer_enabled"] = True
            risk["total_exposure_enforcer_policy"] = "reduce_overweight"
            risk["total_exposure_enforcer_threshold"] = float(
                params["enforcer_threshold"]
            )
            risk["total_exposure_entry_gate_enabled"] = True
            risk["we_excess_allowance_mode"] = "bounded"
            risk["we_excess_allowance_pct"] = 0.0
            config["bot"][side]["hsl"]["enabled"] = bool(params["hsl_enabled"])
            config["bot"][side]["unstuck"]["enabled"] = bool(params["unstuck_enabled"])
        config["live"]["approved_coins"] = {
            "long": list(tool.BASKET),
            "short": list(tool.BASKET),
        }
        config["backtest"]["coins"]["binance"] = list(tool.BASKET)
        return config

    if track == "U-40":
        coins = read_json(tool.DATASET / "coins.json")
        if not isinstance(coins, list) or not coins:
            raise RuntimeError("frozen universe has no valid coin list")
        config["live"]["approved_coins"] = {"long": coins, "short": []}
        config["backtest"]["coins"]["binance"] = coins
        return config
    raise ValueError(f"unsupported final replay track: {track}")


def execution_audit(
    *,
    scenario_root: Path,
    execution: str,
    fills: pd.DataFrame,
    coins: list[str],
    hlcvs: np.ndarray,
) -> dict[str, Any]:
    audit_path = scenario_root / "execution_boundary_audit.csv"
    if not audit_path.exists():
        raise FileNotFoundError(f"missing execution audit: {audit_path}")
    audit = pd.read_csv(audit_path)
    if len(audit) != len(fills):
        raise AssertionError(
            f"audit/fill row mismatch for {scenario_root.name}: {len(audit)} != {len(fills)}"
        )
    fills = fills.loc[:, ~fills.columns.str.startswith("Unnamed:")].reset_index(drop=True)
    fill_timestamp_ms = (
        pd.to_datetime(fills["timestamp"], utc=True).astype("int64") // 1_000_000
    ).to_numpy()
    checks = {
        "fill_index": audit["fill_index"].to_numpy(dtype=int)
        == fills["index"].to_numpy(dtype=int),
        "fill_timestamp": audit["fill_candle_open_timestamp_ms"].to_numpy(dtype=np.int64)
        == fill_timestamp_ms,
        "symbol": audit["symbol"].to_numpy() == fills["coin"].to_numpy(),
        "pside": audit["pside"].to_numpy()
        == fills["type"].str.rsplit("_", n=1).str[-1].to_numpy(),
        "order_type": audit["order_type"].to_numpy() == fills["type"].to_numpy(),
        "fill_qty": np.isclose(
            audit["fill_qty"].to_numpy(dtype=float),
            fills["qty"].to_numpy(dtype=float),
            rtol=0.0,
            atol=1.0e-12,
        ),
        "fill_price": np.isclose(
            audit["fill_price"].to_numpy(dtype=float),
            fills["price"].to_numpy(dtype=float),
            rtol=0.0,
            atol=1.0e-12,
        ),
    }
    failed = [name for name, result in checks.items() if not result.all()]
    if failed:
        raise AssertionError(f"{scenario_root.name}: audit mismatch: {failed}")
    if audit["order_id"].duplicated().any():
        raise AssertionError(f"{scenario_root.name}: duplicate order IDs in audit")

    delay = int({"C1": 0, "C2": 0, "C3": 1, "C4": 1}[execution])
    audit["activation_delay_bars"] = (
        audit["activation_index"] - audit["decision_index"]
    )
    audit["resting_age_bars"] = audit["fill_index"] - audit["activation_index"]
    audit["activation_after_decision_close_ms"] = (
        audit["activation_timestamp_ms"] - audit["decision_close_timestamp_ms"]
    )
    audit["fill_interval_ms"] = (
        audit["fill_candle_close_timestamp_ms"]
        - audit["fill_candle_open_timestamp_ms"]
    )
    if not (audit["activation_delay_bars"] == delay + 1).all():
        raise AssertionError(f"{scenario_root.name}: non-causal order activation")
    if not (
        audit["activation_after_decision_close_ms"] == delay * 60_000
    ).all():
        raise AssertionError(f"{scenario_root.name}: unexpected activation timestamp")
    if not (audit["resting_age_bars"] >= 0).all():
        raise AssertionError(f"{scenario_root.name}: pre-activation fill")
    if not (audit["fill_interval_ms"] == 60_000).all():
        raise AssertionError(f"{scenario_root.name}: non-minute fill interval")

    coin_index = {coin: index for index, coin in enumerate(coins)}
    try:
        fill_coins = np.asarray([coin_index[symbol] for symbol in audit["symbol"]])
    except KeyError as exc:
        raise AssertionError(
            f"{scenario_root.name}: audit references absent coin {exc.args[0]!r}"
        ) from exc
    fill_index = audit["fill_index"].to_numpy(dtype=int)
    if (fill_index < 0).any() or (fill_index >= len(hlcvs)).any():
        raise AssertionError(f"{scenario_root.name}: audit fill index is out of range")
    order_type = audit["order_type"].astype(str)
    is_entry = order_type.str.startswith("entry_").to_numpy()
    is_close = order_type.str.startswith("close_").to_numpy()
    if not (is_entry | is_close).all():
        invalid = sorted(order_type.loc[~(is_entry | is_close)].unique())
        raise AssertionError(f"{scenario_root.name}: unknown order types {invalid}")
    is_long = (audit["pside"] == "long").to_numpy()
    is_buy = (is_entry & is_long) | (is_close & ~is_long)
    low = hlcvs[fill_index, fill_coins, 1]
    high = hlcvs[fill_index, fill_coins, 0]
    price = audit["fill_price"].to_numpy(dtype=float)
    strict_crossing = np.where(is_buy, low < price, high > price)
    if not strict_crossing.all():
        raise AssertionError(
            f"{scenario_root.name}: fill violates strict H/L crossing "
            f"({int((~strict_crossing).sum())} rows)"
        )
    audit["inferred_order_side"] = np.where(is_buy, "buy", "sell")
    audit["strict_crossing_verified"] = strict_crossing
    audit.to_csv(scenario_root / "execution_boundary_audit_checked.csv", index=False)
    return {
        "audit_rows": int(len(audit)),
        "unique_order_ids": int(audit["order_id"].nunique()),
        "preactivation_fill_rows": int((audit["resting_age_bars"] < 0).sum()),
        "fills_on_activation": int((audit["resting_age_bars"] == 0).sum()),
        "fills_after_activation": int((audit["resting_age_bars"] > 0).sum()),
        "max_resting_age_bars": int(audit["resting_age_bars"].max())
        if len(audit)
        else 0,
        "activation_delay_bars": int(delay + 1),
        "strict_crossing_verified_rows": int(strict_crossing.sum()),
        "strict_crossing_failed_rows": int((~strict_crossing).sum()),
    }


async def run_one(
    tool: Any,
    params: dict[str, Any],
    provenance: dict[str, str],
    execution: str,
    track: str,
) -> None:
    import backtest

    name = scenario_name(execution, track)
    scenario_root = OUTPUT / "holdout" / name
    identity_path = scenario_root / "run_identity.json"
    if identity_path.exists():
        identity = read_json(identity_path)
        if (
            identity.get("execution") != execution
            or identity.get("track") != track
            or identity.get("provenance") != provenance
        ):
            raise RuntimeError(f"existing replay identity differs: {identity_path}")
        if not Path(identity["result_dir"]).exists():
            raise FileNotFoundError(f"existing replay result directory is absent: {name}")
        print(f"already complete: {name}", flush=True)
        return
    if scenario_root.exists():
        raise RuntimeError(
            f"refusing to reuse incomplete replay directory; inspect manually: {scenario_root}"
        )
    scenario_root.mkdir(parents=True)

    config = configure_special_track(tool, params, execution, track)
    config["backtest"].update(
        {
            "base_dir": str(scenario_root.resolve()),
            "balance_sample_divider": 1,
            "execution_audit_path": str(
                (scenario_root / "execution_boundary_audit.csv").resolve()
            ),
        }
    )
    config["disable_plotting"] = "all"
    config_hash = hashlib.sha256(canonical_json(config).encode()).hexdigest()
    (
        coins,
        hlcvs,
        mss,
        results_path,
        cache_dir,
        btc_usd_prices,
        timestamps,
    ) = await backtest.prepare_hlcvs_mss(config, "binance")
    config["backtest"]["coins"]["binance"] = coins
    config["backtest"]["cache_dir"]["binance"] = str(cache_dir)
    if not np.all(np.diff(timestamps) == 60_000):
        raise RuntimeError(f"{name}: prepared data is not minute-contiguous")
    expected_start = np.datetime64(tool.FINAL_HOLDOUT[0], "ms").astype(np.int64)
    if int(timestamps[0]) > expected_start:
        raise RuntimeError(
            f"{name}: prepared data begins after holdout start "
            f"({int(timestamps[0])} > {expected_start})"
        )

    fills, equity, analysis, payload = backtest.run_backtest(
        hlcvs, mss, config, "binance", btc_usd_prices, timestamps, return_payload=True
    )
    np.save(scenario_root / "minute_equity.npy", equity, allow_pickle=False)
    backtest.post_process(
        config,
        hlcvs,
        fills,
        equity,
        btc_usd_prices,
        analysis,
        results_path,
        "binance",
        plot_context=backtest.BacktestPlotContext.from_payload(payload),
    )
    result_dirs = sorted(
        path.parent for path in scenario_root.glob("binance/*/analysis.json")
    )
    if len(result_dirs) != 1:
        raise RuntimeError(f"{name}: expected one result directory, got {result_dirs}")
    fills_frame = pd.read_csv(result_dirs[0] / "fills.csv")
    audit = execution_audit(
        scenario_root=scenario_root,
        execution=execution,
        fills=fills_frame,
        coins=coins,
        hlcvs=hlcvs,
    )
    identity = {
        "scenario": name,
        "execution": execution,
        "track": track,
        "strategy": "trailing_martingale",
        "holdout_window": list(tool.FINAL_HOLDOUT),
        "result_dir": str(result_dirs[0].resolve()),
        "prepared_coins": coins,
        "prepared_shape": list(hlcvs.shape),
        "config_sha256_before_data_prepare": config_hash,
        "runtime": tool.runtime_identity(),
        "dataset_manifest_sha256": tool.sha256(tool.DATASET / "manifest.json"),
        "provenance": provenance,
        "execution_audit_summary": audit,
        "network_disabled": True,
    }
    write_json(identity_path, identity, locked=True)
    print(f"completed: {name}", flush=True)


async def async_main(selected: set[str] | None) -> None:
    tool = load_parameter_tool()
    disable_network()
    tool.disable_network()
    params, provenance = locked_inputs(tool)
    open_lock = {
        "purpose": "Final locked six-month holdout causal replay",
        "holdout_window": list(tool.FINAL_HOLDOUT),
        "scenarios": [
            {"execution": execution, "track": track} for execution, track in SCENARIOS
        ],
        "provenance": provenance,
        "network_forbidden": True,
        "selection_after_opening_forbidden": True,
    }
    write_json(OUTPUT / "holdout_opened_lock.json", open_lock, locked=True)
    for execution, track in SCENARIOS:
        name = scenario_name(execution, track)
        if selected is not None and name not in selected:
            continue
        await run_one(tool, params, provenance, execution, track)
        tool.install_state({})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario",
        action="append",
        choices=[scenario_name(execution, track) for execution, track in SCENARIOS],
        help="Run only named locked scenarios; omitted runs all incomplete scenarios.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(async_main(set(args.scenario) if args.scenario else None))
