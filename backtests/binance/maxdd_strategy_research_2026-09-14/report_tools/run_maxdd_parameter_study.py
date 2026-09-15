"""Offline exact-Rust MAXDD-first comparison of the canonical strategies.

This research tool uses only the already frozen local Binance H/L/C/V cache. It
does not start a bot, read credentials, access an exchange account, or send any
network request.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import logging
import math
import multiprocessing as mp
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Callable

import numpy as np
import pandas as pd
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.problem import Problem
from pymoo.optimize import minimize


REPO = Path(".").resolve()
STUDY = REPO / "backtests/binance/maxdd_strategy_research_2026-09-14"
DATASET = REPO / (
    "caches/hlcvs_data/"
    "binance__40_coins__2023-08-17_to_2026-09-12__2c11e36fd7fc3806"
)
SEEDS = {
    "ema_anchor": REPO / "configs/examples/ema_anchor.json",
    "trailing_martingale": REPO / "configs/examples/hsl_npos1.json",
}
BASKET = ["BTC", "ETH", "BNB", "XRP", "SOL"]
STRATEGIES = tuple(SEEDS)
SIDES = ("long", "short")
MAXDD_CAP = 0.25
COMPLETION_FLOOR = 0.999
ADG_FLOOR = 0.00005
ACTIVITY_FLOORS = {
    "E-L": {"fills_per_day_close": 0.10, "fills_active_days_ratio": 0.08},
    "E-S": {"fills_per_day_close": 0.10, "fills_active_days_ratio": 0.08},
    "B-L": {"fills_per_day_close": 0.25, "fills_active_days_ratio": 0.15},
}
BASELINE_WINDOW = ("2023-09-12", "2026-03-12")
FOLDS = {
    "F1": {
        "train": ("2023-09-12", "2024-09-12"),
        "validation": ("2024-09-12", "2025-03-12"),
    },
    "F2": {
        "train": ("2024-03-12", "2025-03-12"),
        "validation": ("2025-03-12", "2025-09-12"),
    },
    "F3": {
        "train": ("2024-09-12", "2025-09-12"),
        "validation": ("2025-09-12", "2026-03-12"),
    },
}
FINAL_TRAIN = ("2025-03-12", "2026-03-12")
FINAL_HOLDOUT = ("2026-03-12", "2026-09-12")
EXECUTION_SCENARIOS = {
    "C1": {"execution_delay_bars": 0, "intrabar_fill_order": "close_first"},
    "C2": {"execution_delay_bars": 0, "intrabar_fill_order": "entry_first"},
    "C3": {"execution_delay_bars": 1, "intrabar_fill_order": "close_first"},
    "C4": {"execution_delay_bars": 1, "intrabar_fill_order": "entry_first"},
}
SEARCH_BUDGETS = {"risk": 64, "strategy": 96, "fine": 64}
POPULATION_SIZE = 16
WORKERS = 4
SEARCH_SEEDS = {
    "F1": {"ema_anchor": 1101, "trailing_martingale": 1201},
    "F2": {"ema_anchor": 2101, "trailing_martingale": 2201},
    "F3": {"ema_anchor": 3101, "trailing_martingale": 3201},
    "FINAL": {"ema_anchor": 4101, "trailing_martingale": 4201},
}
BASELINE_RISK_LAYERS = {
    "gates_only": (False, False),
    "gates_unstuck": (False, True),
    "gates_hsl": (True, False),
    "gates_unstuck_hsl": (True, True),
}
METRICS = (
    "drawdown_worst_strategy_eq",
    "drawdown_worst_mean_1pct_strategy_eq",
    "strategy_eq_recovery_days_max",
    "strategy_eq_underwater_pct_mean",
    "adg_strategy_eq",
    "positive_gain_participation_strategy_eq",
    "gain_strategy_eq",
    "fills_count",
    "fills_count_close",
    "fills_per_day",
    "fills_per_day_close",
    "fills_active_days_ratio",
    "fills_analysis_duration_days",
    "total_wallet_exposure_max",
    "total_wallet_exposure_mean",
    "equity_balance_diff_neg_max_usd",
    "hard_stop_triggers",
    "hard_stop_restarts",
    "unstuck_close_fills",
    "position_held_days_max",
    "backtest_completion_ratio",
    "liquidated",
)


_STATE: dict[str, Any] = {}


def reject_network(*_args, **_kwargs):
    raise RuntimeError("network is forbidden during the frozen MAXDD study")


def disable_network() -> None:
    socket.socket.connect = reject_network
    socket.socket.connect_ex = reject_network
    socket.create_connection = reject_network


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, (np.integer, int)):
        return int(value)
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"unsupported JSON value {type(value).__name__}: {value!r}")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(json_value(value), ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def write_lock(path: Path, value: Any) -> None:
    normalized = json_value(value)
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != normalized:
            raise RuntimeError(f"refusing to replace an existing research lock: {path}")
        return
    write_json(path, normalized)


def run_git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "--no-pager", *args], cwd=REPO, text=True
    ).strip()


def runtime_identity() -> dict[str, Any]:
    from rust_utils import verify_loaded_runtime_extension

    import passivbot_rust

    verified = verify_loaded_runtime_extension()
    return {
        **verified,
        "runtime_build_info": passivbot_rust.runtime_build_info(),
    }


def source_config(strategy: str) -> dict[str, Any]:
    from config import load_prepared_config

    return load_prepared_config(str(SEEDS[strategy]), verbose=False)


def common_config(
    strategy: str,
    start: str,
    end: str,
    *,
    execution: str = "C1",
) -> dict[str, Any]:
    config = source_config(strategy)
    execution_settings = EXECUTION_SCENARIOS[execution]
    config["backtest"].update(
        {
            "start_date": start,
            "end_date": end,
            "exchanges": ["binance"],
            "hlcvs_data_dir": str(DATASET.relative_to(REPO)),
            "hlcvs_data_override_mode": "intersection",
            "btc_collateral_cap": 0.0,
            "btc_collateral_ltv_cap": None,
            "starting_balance": 100000.0,
            "candle_interval_minutes": 1,
            "maker_fee_override": 0.0004,
            "suite_enabled": False,
            "scenarios": [],
            "execution_delay_bars": execution_settings["execution_delay_bars"],
            "intrabar_fill_order": execution_settings["intrabar_fill_order"],
            "execution_audit_path": None,
            "balance_sample_divider": 1,
            "base_dir": str(STUDY / "_engine_runs"),
            "dynamic_wel_by_tradability": True,
            "filter_by_min_effective_cost": False,
        }
    )
    config["live"].update(
        {
            "approved_coins": {"long": list(BASKET), "short": []},
            "ignored_coins": {"long": [], "short": []},
            "strategy_kind": strategy,
            "hedge_mode": False,
            "hsl_signal_mode": "unified",
            "market_orders_allowed": False,
        }
    )
    config["bot"]["short"]["forager"] = deepcopy(config["bot"]["long"]["forager"])
    for side in SIDES:
        risk = config["bot"][side]["risk"]
        risk.update(
            {
                "entry_cooldown_minutes": 30.0,
                "n_positions": 2.0 if side == "long" else 0.0,
                "position_exposure_enforcer_enabled": True,
                "position_exposure_enforcer_threshold": 0.9,
                "total_exposure_enforcer_enabled": True,
                "total_exposure_enforcer_policy": "reduce_overweight",
                "total_exposure_enforcer_threshold": 0.9,
                "total_exposure_entry_gate_enabled": True,
                "total_wallet_exposure_limit": 0.75 if side == "long" else 0.0,
                "we_excess_allowance_mode": "bounded",
                "we_excess_allowance_pct": 0.0,
            }
        )
        hsl = config["bot"][side]["hsl"]
        hsl.update(
            {
                "enabled": False,
                "red_threshold": 0.15,
                "cooldown_minutes_after_red": 2160.0,
                "ema_span_minutes": 240.0,
                "no_restart_drawdown_threshold": 1.0,
                "orange_tier_mode": "tp_only_with_active_entry_cancellation",
                "panic_close_order_type": "limit",
            }
        )
        unstuck = config["bot"][side]["unstuck"]
        unstuck.update(
            {
                "enabled": False,
                "close_pct": 0.05,
                "loss_allowance_pct": 0.01,
                "threshold": 0.5,
                "ema_dist": -0.05,
                "ema_gating_enabled": True,
            }
        )
    apply_strategy_params(config, strategy, conservative_strategy_params(strategy))
    return config


def conservative_strategy_params(strategy: str) -> dict[str, Any]:
    if strategy == "ema_anchor":
        return {
            "base_qty_pct": 0.01,
            "ema_span_0": 415.0,
            "ema_span_1": 793.0,
            "entry_double_down_factor": 0.25,
            "offset": 0.01,
            "offset_psize_weight": 1.0,
            "offset_volatility_1h_weight": 13.0,
            "offset_volatility_1m_weight": 0.4,
            "offset_volatility_ema_span_1h": 448.0,
            "offset_volatility_ema_span_1m": 664.0,
        }
    return {
        "close_qty_pct": 0.10,
        "close_retracement_base_pct": 0.0,
        "close_threshold_base_pct": 0.006,
        "close_threshold_volatility_1h_weight": 1.0,
        "close_threshold_volatility_1m_weight": 0.0,
        "close_threshold_we_weight": -0.004,
        "entry_double_down_factor": 0.50,
        "entry_ema_span_0": 770.0,
        "entry_ema_span_1": 210.0,
        "entry_initial_ema_dist": 0.008,
        "entry_initial_qty_pct": 0.01,
        "entry_retracement_base_pct": 0.0,
        "entry_retracement_we_weight": 0.0,
        "entry_threshold_base_pct": 0.02,
        "entry_threshold_volatility_1h_weight": 2.4,
        "entry_threshold_volatility_1m_weight": 0.0,
        "entry_threshold_we_weight": 0.10,
        "volatility_ema_span_1h": 1690.0,
        "volatility_ema_span_1m": 60.0,
    }


def conservative_risk_params() -> dict[str, Any]:
    return {
        "twe": 0.75,
        "entry_cooldown_minutes": 30.0,
        "n_positions": 2,
        "enforcer_threshold": 0.9,
        "hsl_enabled": False,
        "unstuck_enabled": False,
        "hsl_red_threshold": 0.15,
        "hsl_cooldown_minutes": 2160.0,
        "hsl_ema_span_minutes": 240.0,
        "unstuck_close_pct": 0.05,
        "unstuck_loss_allowance_pct": 0.01,
        "unstuck_threshold": 0.5,
        "unstuck_ema_dist": -0.05,
    }


def all_default_params(strategy: str) -> dict[str, Any]:
    return {
        **conservative_risk_params(),
        **conservative_strategy_params(strategy),
    }


def apply_strategy_params(
    config: dict[str, Any], strategy: str, params: dict[str, Any]
) -> None:
    if strategy == "ema_anchor":
        for side in SIDES:
            target = config["bot"][side]["strategy"]["ema_anchor"]
            for key in (
                "base_qty_pct",
                "ema_span_0",
                "ema_span_1",
                "entry_double_down_factor",
                "offset",
                "offset_psize_weight",
                "offset_volatility_1h_weight",
                "offset_volatility_1m_weight",
                "offset_volatility_ema_span_1h",
                "offset_volatility_ema_span_1m",
            ):
                if key in params:
                    target[key] = params[key]
            config["bot"][side]["unstuck"]["ema_span_0"] = target["ema_span_0"]
            config["bot"][side]["unstuck"]["ema_span_1"] = target["ema_span_1"]
        return

    for side in SIDES:
        target = config["bot"][side]["strategy"]["trailing_martingale"]
        close = target["close"]
        entry = target["entry"]
        mappings = {
            "close_qty_pct": (close, "qty_pct"),
            "close_retracement_base_pct": (close, "retracement_base_pct"),
            "close_threshold_base_pct": (close, "threshold_base_pct"),
            "close_threshold_volatility_1h_weight": (
                close,
                "threshold_volatility_1h_weight",
            ),
            "close_threshold_volatility_1m_weight": (
                close,
                "threshold_volatility_1m_weight",
            ),
            "close_threshold_we_weight": (close, "threshold_we_weight"),
            "entry_double_down_factor": (entry, "double_down_factor"),
            "entry_ema_span_0": (entry, "ema_span_0"),
            "entry_ema_span_1": (entry, "ema_span_1"),
            "entry_initial_qty_pct": (entry, "initial_qty_pct"),
            "entry_retracement_base_pct": (entry, "retracement_base_pct"),
            "entry_retracement_we_weight": (entry, "retracement_we_weight"),
            "entry_threshold_base_pct": (entry, "threshold_base_pct"),
            "entry_threshold_volatility_1h_weight": (
                entry,
                "threshold_volatility_1h_weight",
            ),
            "entry_threshold_volatility_1m_weight": (
                entry,
                "threshold_volatility_1m_weight",
            ),
            "entry_threshold_we_weight": (entry, "threshold_we_weight"),
            "volatility_ema_span_1h": (target, "volatility_ema_span_1h"),
            "volatility_ema_span_1m": (target, "volatility_ema_span_1m"),
        }
        for key, (container, field) in mappings.items():
            if key in params:
                container[field] = params[key]
        if "entry_initial_ema_dist" in params:
            distance = abs(float(params["entry_initial_ema_dist"]))
            entry["initial_ema_dist"] = distance if side == "long" else -distance
        config["bot"][side]["unstuck"]["ema_span_0"] = entry["ema_span_0"]
        config["bot"][side]["unstuck"]["ema_span_1"] = entry["ema_span_1"]


def configure_track(
    base: dict[str, Any],
    strategy: str,
    params: dict[str, Any],
    track: str,
) -> dict[str, Any]:
    config = deepcopy(base)
    apply_strategy_params(config, strategy, params)
    for side in SIDES:
        risk = config["bot"][side]["risk"]
        enabled = (track in ("E-L", "B-L") and side == "long") or (
            track == "E-S" and side == "short"
        )
        n_positions = 1 if track in ("E-L", "E-S") else int(params["n_positions"])
        risk.update(
            {
                "entry_cooldown_minutes": params["entry_cooldown_minutes"],
                "n_positions": float(n_positions if enabled else 0),
                "position_exposure_enforcer_enabled": True,
                "position_exposure_enforcer_threshold": params["enforcer_threshold"],
                "total_exposure_enforcer_enabled": True,
                "total_exposure_enforcer_policy": "reduce_overweight",
                "total_exposure_enforcer_threshold": params["enforcer_threshold"],
                "total_exposure_entry_gate_enabled": True,
                "total_wallet_exposure_limit": params["twe"] if enabled else 0.0,
                "we_excess_allowance_mode": "bounded",
                "we_excess_allowance_pct": 0.0,
            }
        )
        hsl = config["bot"][side]["hsl"]
        hsl.update(
            {
                "enabled": bool(params["hsl_enabled"]) if enabled else False,
                "red_threshold": params["hsl_red_threshold"],
                "cooldown_minutes_after_red": params["hsl_cooldown_minutes"],
                "ema_span_minutes": params["hsl_ema_span_minutes"],
                "no_restart_drawdown_threshold": 1.0,
                "orange_tier_mode": "tp_only_with_active_entry_cancellation",
                "panic_close_order_type": "limit",
            }
        )
        unstuck = config["bot"][side]["unstuck"]
        unstuck.update(
            {
                "enabled": bool(params["unstuck_enabled"]) if enabled else False,
                "close_pct": params["unstuck_close_pct"],
                "loss_allowance_pct": params["unstuck_loss_allowance_pct"],
                "threshold": params["unstuck_threshold"],
                "ema_dist": params["unstuck_ema_dist"],
                "ema_gating_enabled": True,
            }
        )
    if track == "E-L":
        config["live"]["approved_coins"] = {"long": ["ETH"], "short": []}
        config["backtest"]["coins"]["binance"] = ["ETH"]
    elif track == "E-S":
        config["live"]["approved_coins"] = {"long": [], "short": ["ETH"]}
        config["backtest"]["coins"]["binance"] = ["ETH"]
    elif track == "B-L":
        config["live"]["approved_coins"] = {"long": list(BASKET), "short": []}
        config["backtest"]["coins"]["binance"] = list(_STATE["coins"])
    else:
        raise ValueError(f"unknown track {track!r}")
    return config


def maximum_warmup_config(strategy: str, start: str, end: str) -> dict[str, Any]:
    config = common_config(strategy, start, end)
    if strategy == "ema_anchor":
        maximum = conservative_strategy_params(strategy)
        maximum.update(
            {
                "ema_span_0": 1440.0,
                "ema_span_1": 1440.0,
                "offset_volatility_ema_span_1h": 672.0,
                "offset_volatility_ema_span_1m": 720.0,
            }
        )
    else:
        maximum = conservative_strategy_params(strategy)
        maximum.update(
            {
                "entry_ema_span_0": 1440.0,
                "entry_ema_span_1": 1440.0,
                "volatility_ema_span_1h": 2016.0,
                "volatility_ema_span_1m": 720.0,
            }
        )
    apply_strategy_params(config, strategy, maximum)
    for side in SIDES:
        config["bot"][side]["hsl"]["ema_span_minutes"] = 1440.0
    return config


async def prepare_window(
    strategy: str,
    start: str,
    end: str,
    *,
    execution: str = "C1",
) -> dict[str, Any]:
    import backtest

    config = maximum_warmup_config(strategy, start, end)
    execution_values = EXECUTION_SCENARIOS[execution]
    config["backtest"]["execution_delay_bars"] = execution_values["execution_delay_bars"]
    config["backtest"]["intrabar_fill_order"] = execution_values["intrabar_fill_order"]
    (
        coins,
        hlcvs,
        mss,
        _results_path,
        cache_dir,
        btc_usd_prices,
        timestamps,
    ) = await backtest.prepare_hlcvs_mss(config, "binance")
    missing = sorted(set(BASKET) - set(coins))
    if missing:
        raise RuntimeError(f"frozen dataset preparation omitted basket coins: {missing}")
    order = [coins.index(coin) for coin in BASKET]
    basket_hlcvs = np.ascontiguousarray(hlcvs[:, order, :])
    eth_hlcvs = np.ascontiguousarray(
        basket_hlcvs[:, [BASKET.index("ETH")], :]
    )
    prepared_coins = [coins[index] for index in order]
    config["backtest"].setdefault("coins", {})["binance"] = prepared_coins
    config["backtest"].setdefault("cache_dir", {})["binance"] = str(cache_dir)
    return {
        "strategy": strategy,
        "start": start,
        "end": end,
        "execution": execution,
        "base_config": config,
        "coins": prepared_coins,
        "hlcvs_basket": basket_hlcvs,
        "hlcvs_eth": eth_hlcvs,
        "mss": mss,
        "btc_usd_prices": np.ascontiguousarray(btc_usd_prices),
        "timestamps": np.ascontiguousarray(timestamps),
        "cache_dir": str(cache_dir),
    }


def install_state(state: dict[str, Any]) -> None:
    global _STATE
    _STATE = state


def extract_metrics(analysis: dict[str, Any]) -> dict[str, Any]:
    return {key: json_value(analysis.get(key)) for key in METRICS}


def run_track(
    strategy: str,
    params: dict[str, Any],
    track: str,
) -> dict[str, Any]:
    import backtest

    config = configure_track(_STATE["base_config"], strategy, params, track)
    config["backtest"]["cache_dir"]["binance"] = _STATE["cache_dir"]
    hlcvs = _STATE["hlcvs_eth"] if track in ("E-L", "E-S") else _STATE["hlcvs_basket"]
    payload = backtest.build_backtest_payload(
        hlcvs,
        _STATE["mss"],
        config,
        "binance",
        _STATE["btc_usd_prices"],
        _STATE["timestamps"],
        metrics_only=True,
        skip_btc_analysis=True,
    )
    _fills, _equities, analysis = backtest.execute_backtest(payload, config)
    return extract_metrics(analysis)


def evaluate_task(task: tuple[str, dict[str, Any], tuple[str, ...]]) -> dict[str, Any]:
    strategy, params, tracks = task
    return {
        "strategy": strategy,
        "params": params,
        "params_hash": stable_hash(params),
        "tracks": {track: run_track(strategy, params, track) for track in tracks},
    }


def pool_initializer() -> None:
    logging.getLogger().setLevel(logging.ERROR)


def numeric_metric(result: dict[str, Any], track: str, metric: str) -> float:
    value = result["tracks"][track].get(metric)
    return float(value) if value is not None and math.isfinite(float(value)) else math.nan


def summarize_result(result: dict[str, Any], tracks: tuple[str, ...]) -> dict[str, Any]:
    max_mdd = max(
        numeric_metric(result, track, "drawdown_worst_strategy_eq") for track in tracks
    )
    max_mdd_1pct = max(
        numeric_metric(result, track, "drawdown_worst_mean_1pct_strategy_eq")
        for track in tracks
    )
    max_recovery = max(
        numeric_metric(result, track, "strategy_eq_recovery_days_max")
        for track in tracks
    )
    min_adg = min(numeric_metric(result, track, "adg_strategy_eq") for track in tracks)
    min_gain = min(numeric_metric(result, track, "gain_strategy_eq") for track in tracks)
    min_completion = min(
        numeric_metric(result, track, "backtest_completion_ratio") for track in tracks
    )
    activity_violations = []
    for track in tracks:
        floors = ACTIVITY_FLOORS[track]
        for metric, floor in floors.items():
            value = numeric_metric(result, track, metric)
            activity_violations.append((floor - value) / floor)
    liquidated = any(bool(result["tracks"][track].get("liquidated")) for track in tracks)
    constraints = [
        max_mdd - MAXDD_CAP,
        COMPLETION_FLOOR - min_completion,
        ADG_FLOOR - min_adg,
        1.0 + 1e-12 - min_gain,
        max(activity_violations),
        1.0 if liquidated else -1.0,
    ]
    finite = all(
        math.isfinite(value)
        for value in (
            max_mdd,
            max_mdd_1pct,
            max_recovery,
            min_adg,
            min_gain,
            min_completion,
            *activity_violations,
        )
    )
    if not finite:
        constraints = [1e6] * len(constraints)
        max_mdd = max_mdd_1pct = max_recovery = 1e6
        min_adg = min_gain = -1e6
    violation = sum(max(0.0, value) for value in constraints)
    return {
        "objectives": [max_mdd, max_mdd_1pct, max_recovery / 365.25, -min_adg],
        "constraints": constraints,
        "constraint_violation": violation,
        "feasible": finite and violation <= 1e-12,
        "max_mdd": max_mdd,
        "max_mdd_1pct": max_mdd_1pct,
        "max_recovery_days": max_recovery,
        "min_adg": min_adg,
        "min_gain": min_gain,
        "min_completion": min_completion,
        "liquidated": liquidated,
    }


def selection_key(record: dict[str, Any]) -> tuple[Any, ...]:
    summary = record["summary"]
    return (
        not summary["feasible"],
        summary["constraint_violation"],
        summary["max_mdd"],
        summary["max_mdd_1pct"],
        summary["max_recovery_days"],
        -summary["min_adg"],
        record["params_hash"],
    )


def deduplicate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        current = unique.get(record["params_hash"])
        if current is None or selection_key(record) < selection_key(current):
            unique[record["params_hash"]] = record
    return sorted(unique.values(), key=selection_key)


def quantize(value: float, low: float, high: float, step: float) -> float:
    value = min(high, max(low, float(value)))
    if step <= 0:
        return value
    rounded = low + round((value - low) / step) * step
    rounded = min(high, max(low, rounded))
    digits = max(0, int(math.ceil(-math.log10(step))) + 2) if step < 1 else 6
    return round(rounded, digits)


RISK_SPECS = (
    ("twe", 0.25, 1.25, 0.01),
    ("entry_cooldown_minutes", 0.0, 120.0, 1.0),
    ("n_positions", 1.0, 3.0, 1.0),
    ("enforcer_threshold", 0.75, 1.0, 0.01),
    ("hsl_enabled", 0.0, 1.0, 1.0),
    ("unstuck_enabled", 0.0, 1.0, 1.0),
)
EMA_SPECS = (
    ("base_qty_pct", 0.002, 0.03, 0.0005),
    ("ema_span_0", 60.0, 1440.0, 10.0),
    ("ema_span_1", 60.0, 1440.0, 10.0),
    ("entry_double_down_factor", 0.0, 0.75, 0.01),
    ("offset", 0.002, 0.03, 0.0005),
    ("offset_psize_weight", 0.25, 4.0, 0.05),
    ("offset_volatility_1h_weight", 0.0, 20.0, 0.25),
    ("offset_volatility_1m_weight", 0.0, 5.0, 0.10),
    ("offset_volatility_ema_span_1h", 24.0, 672.0, 6.0),
    ("offset_volatility_ema_span_1m", 10.0, 720.0, 10.0),
)
TM_SPECS = (
    ("close_qty_pct", 0.05, 0.50, 0.01),
    ("close_threshold_base_pct", 0.002, 0.02, 0.0005),
    ("close_threshold_volatility_1h_weight", 0.0, 10.0, 0.25),
    ("close_threshold_we_weight", -0.03, 0.03, 0.001),
    ("entry_double_down_factor", 0.0, 0.90, 0.01),
    ("entry_ema_span_0", 60.0, 1440.0, 10.0),
    ("entry_ema_span_1", 60.0, 1440.0, 10.0),
    ("entry_initial_ema_dist", 0.002, 0.02, 0.0005),
    ("entry_initial_qty_pct", 0.002, 0.03, 0.0005),
    ("entry_retracement_base_pct", 0.0, 0.015, 0.0005),
    ("entry_retracement_we_weight", 0.0, 1.0, 0.025),
    ("entry_threshold_base_pct", 0.005, 0.05, 0.001),
    ("entry_threshold_volatility_1h_weight", 0.0, 10.0, 0.25),
    ("entry_threshold_we_weight", 0.0, 0.50, 0.01),
    ("volatility_ema_span_1h", 240.0, 2016.0, 24.0),
    ("volatility_ema_span_1m", 10.0, 720.0, 10.0),
)
HSL_FINE_SPECS = (
    ("hsl_red_threshold", 0.05, 0.30, 0.005),
    ("hsl_cooldown_minutes", 60.0, 4320.0, 60.0),
    ("hsl_ema_span_minutes", 30.0, 1440.0, 10.0),
)
UNSTUCK_FINE_SPECS = (
    ("unstuck_close_pct", 0.01, 0.15, 0.005),
    ("unstuck_loss_allowance_pct", 0.003, 0.08, 0.001),
    ("unstuck_threshold", 0.30, 0.95, 0.01),
    ("unstuck_ema_dist", -0.20, 0.02, 0.005),
)


def strategy_specs(strategy: str):
    return EMA_SPECS if strategy == "ema_anchor" else TM_SPECS


def decoder_for_stage(
    strategy: str,
    stage: str,
    base_params: dict[str, Any],
) -> tuple[
    list[tuple[str, float, float, float]],
    Callable[[np.ndarray], dict[str, Any]],
]:
    if stage == "risk":
        specs = list(RISK_SPECS)
    elif stage == "strategy":
        specs = list(strategy_specs(strategy))
    elif stage == "fine":
        global_specs = [
            *RISK_SPECS[:4],
            *strategy_specs(strategy),
            *(HSL_FINE_SPECS if base_params["hsl_enabled"] else ()),
            *(UNSTUCK_FINE_SPECS if base_params["unstuck_enabled"] else ()),
        ]
        specs = []
        for name, global_low, global_high, step in global_specs:
            center = float(base_params[name])
            half_width = max(abs(center) * 0.10, (global_high - global_low) * 0.05)
            low = max(global_low, center - half_width)
            high = min(global_high, center + half_width)
            if high - low < step:
                low, high = global_low, global_high
            specs.append((name, low, high, step))
    else:
        raise ValueError(f"unknown search stage {stage!r}")

    def decode(vector: np.ndarray) -> dict[str, Any]:
        params = deepcopy(base_params)
        for value, (name, low, high, step) in zip(vector, specs):
            quantized = quantize(float(value), low, high, step)
            if name in ("hsl_enabled", "unstuck_enabled"):
                params[name] = bool(round(quantized))
            elif name == "n_positions":
                params[name] = int(round(quantized))
            else:
                params[name] = quantized
        if strategy == "trailing_martingale":
            params["close_retracement_base_pct"] = 0.0
        return params

    return specs, decode


class ExactProblem(Problem):
    def __init__(
        self,
        strategy: str,
        specs: list[tuple[str, float, float, float]],
        decoder: Callable[[np.ndarray], dict[str, Any]],
        pool: mp.pool.Pool | None,
        tracks: tuple[str, ...] = ("E-L", "B-L"),
    ):
        self.strategy = strategy
        self.decoder = decoder
        self.pool = pool
        self.tracks = tracks
        self.records: list[dict[str, Any]] = []
        super().__init__(
            n_var=len(specs),
            n_obj=4,
            n_ieq_constr=6,
            xl=np.array([item[1] for item in specs], dtype=float),
            xu=np.array([item[2] for item in specs], dtype=float),
        )

    def _evaluate(self, x, out, *_args, **_kwargs):
        params = [self.decoder(vector) for vector in np.atleast_2d(x)]
        tasks = [(self.strategy, item, self.tracks) for item in params]
        if self.pool is None:
            raw = [evaluate_task(task) for task in tasks]
        else:
            raw = self.pool.map(evaluate_task, tasks)
        summaries = [summarize_result(item, self.tracks) for item in raw]
        for item, summary in zip(raw, summaries):
            item["summary"] = summary
            self.records.append(item)
        out["F"] = np.asarray([item["objectives"] for item in summaries], dtype=float)
        out["G"] = np.asarray([item["constraints"] for item in summaries], dtype=float)


def flatten_record(
    record: dict[str, Any],
    *,
    stage: str,
    fold: str,
    strategy: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fold": fold,
        "strategy": strategy,
        "stage": stage,
        "params_hash": record["params_hash"],
        **record["summary"],
    }
    row.pop("objectives", None)
    row.pop("constraints", None)
    for key, value in sorted(record["params"].items()):
        row[f"param.{key}"] = value
    for track, metrics in record["tracks"].items():
        for key, value in metrics.items():
            row[f"{track}.{key}"] = value
    return row


def run_exact_stage(
    strategy: str,
    fold: str,
    stage: str,
    base_params: dict[str, Any],
    *,
    seed: int,
    pool: mp.pool.Pool | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    specs, decoder = decoder_for_stage(strategy, stage, base_params)
    problem = ExactProblem(strategy, specs, decoder, pool)
    center = evaluate_task((strategy, base_params, ("E-L", "B-L")))
    center["summary"] = summarize_result(center, ("E-L", "B-L"))
    problem.records.append(center)
    algorithm = NSGA2(pop_size=POPULATION_SIZE, eliminate_duplicates=True)
    minimize(
        problem,
        algorithm,
        ("n_eval", SEARCH_BUDGETS[stage]),
        seed=seed,
        verbose=False,
        save_history=False,
    )
    records = deduplicate_records(problem.records)
    selected = records[0]
    stage_dir = STUDY / "search" / fold / strategy / stage
    write_json(
        stage_dir / "search_definition.json",
        {
            "fold": fold,
            "strategy": strategy,
            "stage": stage,
            "seed": seed,
            "budget": SEARCH_BUDGETS[stage],
            "population_size": POPULATION_SIZE,
            "parameter_specs": [
                {"name": name, "low": low, "high": high, "step": step}
                for name, low, high, step in specs
            ],
            "base_params": base_params,
            "selected_params_hash": selected["params_hash"],
        },
    )
    write_json(stage_dir / "selected_record.json", selected)
    pd.DataFrame(
        [
            flatten_record(
                record, stage=stage, fold=fold, strategy=strategy
            )
            for record in records
        ]
    ).to_csv(stage_dir / "candidate_metrics.csv", index=False)
    return selected, records


def candidate_config(
    strategy: str,
    params: dict[str, Any],
    start: str,
    end: str,
    track: str = "B-L",
    execution: str = "C1",
) -> dict[str, Any]:
    base = common_config(strategy, start, end, execution=execution)
    prepared_coins = list(_STATE.get("coins", BASKET))
    base["backtest"].setdefault("coins", {})["binance"] = prepared_coins
    base["backtest"].setdefault("cache_dir", {})["binance"] = _STATE.get(
        "cache_dir", ""
    )
    return configure_track(base, strategy, params, track)


def freeze_manifest() -> None:
    manifest_path = STUDY / "study_manifest.json"
    dataset_manifest = json.loads(
        (DATASET / "manifest.json").read_text(encoding="utf-8")
    )
    value = {
        "purpose": (
            "Offline exact-Rust MAXDD-first matched comparison and walk-forward "
            "selection of ema_anchor and trailing_martingale."
        ),
        "safety": {
            "network_forbidden": True,
            "account_access": False,
            "credentials": False,
            "bot_start": False,
            "exchange_orders": False,
        },
        "git": {
            "commit": run_git("rev-parse", "HEAD"),
            "branch": run_git("branch", "--show-current"),
            "status_porcelain": run_git("status", "--porcelain"),
        },
        "runtime": runtime_identity(),
        "dataset": {
            "path": str(DATASET.relative_to(REPO)),
            "manifest_sha256": sha256(DATASET / "manifest.json"),
            "manifest": dataset_manifest,
            "compressed_file_sha256": {
                name: sha256(DATASET / name)
                for name in (
                    "hlcvs.npy.gz",
                    "timestamps.npy.gz",
                    "btc_usd_prices.npy.gz",
                    "coins.json",
                    "market_specific_settings.json",
                )
            },
        },
        "seed_configs": {
            strategy: {
                "path": str(path.relative_to(REPO)),
                "sha256": sha256(path),
            }
            for strategy, path in SEEDS.items()
        },
        "account_contract": {
            "starting_balance_usdt": 100000.0,
            "btc_collateral_cap": 0.0,
            "authoritative_mdd": "minute-close usd_total_equity",
            "maker_fee_override": 0.0004,
            "we_excess_allowance_pct": 0.0,
        },
        "execution_contract": EXECUTION_SCENARIOS,
        "primary_optimization_execution": "C1",
        "strict_fill_crossing": {
            "buy": "low < limit",
            "sell": "high > limit",
            "equal_price_fills": False,
        },
        "tracks": {
            "E-L": {"coins": ["ETH"], "side": "long", "n_positions": 1},
            "E-S": {"coins": ["ETH"], "side": "short", "n_positions": 1},
            "B-L": {
                "coins": BASKET,
                "side": "long",
                "n_positions_range": [1, 3],
            },
            "B-LS": {
                "coins": BASKET,
                "side": "long+short",
                "status": "locked-candidate stress only",
            },
            "U-40": {
                "status": "final descriptive generalization stress only",
            },
        },
        "windows": {
            "baseline": BASELINE_WINDOW,
            "folds": FOLDS,
            "final_train": FINAL_TRAIN,
            "final_holdout": FINAL_HOLDOUT,
        },
        "hard_thresholds": {
            "maxdd": MAXDD_CAP,
            "completion_ratio": COMPLETION_FLOOR,
            "minimum_gain_strategy_eq": "strictly greater than 1.0",
            "minimum_adg_strategy_eq_per_track": ADG_FLOOR,
            "minimum_adg_annualized_equivalent": (1.0 + ADG_FLOOR) ** 365.25 - 1.0,
            "activity": ACTIVITY_FLOORS,
            "liquidation_allowed": False,
        },
        "search": {
            "engine": "pymoo NSGA-II calling exact CPU Rust metrics-only backtests",
            "budgets": SEARCH_BUDGETS,
            "population_size": POPULATION_SIZE,
            "workers": WORKERS,
            "seeds": SEARCH_SEEDS,
            "risk_specs": RISK_SPECS,
            "ema_specs": EMA_SPECS,
            "trailing_martingale_specs": TM_SPECS,
            "fine_tune_radius": (
                "max(10% of selected value, 5% of global range), clipped to "
                "pre-registered global bounds"
            ),
            "selection_order": [
                "feasibility",
                "constraint violation",
                "worst track MAXDD",
                "worst track mean of worst 1% drawdowns",
                "worst track recovery days",
                "worst track ADG",
                "parameter hash",
            ],
        },
    }
    write_lock(manifest_path, value)
    write_json(
        STUDY / "parameter_search_manifest.json",
        {
            "study_manifest_sha256": sha256(manifest_path),
            "strategies": list(STRATEGIES),
            "baseline_risk_layers": BASELINE_RISK_LAYERS,
            "conservative_strategy_params": {
                strategy: conservative_strategy_params(strategy)
                for strategy in STRATEGIES
            },
            "conservative_risk_params": conservative_risk_params(),
        },
    )


def baseline_rows_for_strategy(strategy: str) -> list[dict[str, Any]]:
    rows = []
    for layer, (hsl_enabled, unstuck_enabled) in BASELINE_RISK_LAYERS.items():
        params = all_default_params(strategy)
        params["hsl_enabled"] = hsl_enabled
        params["unstuck_enabled"] = unstuck_enabled
        for track in ("E-L", "E-S", "B-L"):
            metrics = run_track(strategy, params, track)
            rows.append(
                {
                    "strategy": strategy,
                    "risk_layer": layer,
                    "track": track,
                    "start": BASELINE_WINDOW[0],
                    "end": BASELINE_WINDOW[1],
                    "params_hash": stable_hash(params),
                    **params,
                    **metrics,
                }
            )
    return rows


async def run_baselines() -> None:
    rows = []
    for strategy in STRATEGIES:
        logging.info("Preparing baseline window for %s", strategy)
        state = await prepare_window(strategy, *BASELINE_WINDOW)
        install_state(state)
        rows.extend(baseline_rows_for_strategy(strategy))
        install_state({})
    frame = pd.DataFrame(rows)
    frame.to_csv(STUDY / "matched_baseline_metrics.csv", index=False)
    write_json(
        STUDY / "matched_baseline_summary.json",
        {
            "rows": len(rows),
            "strategies": list(STRATEGIES),
            "tracks": ["E-L", "E-S", "B-L"],
            "risk_layers": list(BASELINE_RISK_LAYERS),
            "window": BASELINE_WINDOW,
            "maxdd_cap": MAXDD_CAP,
        },
    )


async def search_one(strategy: str, fold: str) -> dict[str, Any]:
    train_start, train_end = (
        FINAL_TRAIN if fold == "FINAL" else FOLDS[fold]["train"]
    )
    logging.info(
        "Preparing exact search data strategy=%s fold=%s window=%s..%s",
        strategy,
        fold,
        train_start,
        train_end,
    )
    state = await prepare_window(strategy, train_start, train_end)
    install_state(state)
    context = mp.get_context("fork")
    pool = context.Pool(WORKERS, initializer=pool_initializer)
    try:
        base = all_default_params(strategy)
        risk_selected, _risk_records = run_exact_stage(
            strategy,
            fold,
            "risk",
            base,
            seed=SEARCH_SEEDS[fold][strategy],
            pool=pool,
        )
        strategy_selected, _strategy_records = run_exact_stage(
            strategy,
            fold,
            "strategy",
            risk_selected["params"],
            seed=SEARCH_SEEDS[fold][strategy] + 1,
            pool=pool,
        )
        fine_selected, fine_records = run_exact_stage(
            strategy,
            fold,
            "fine",
            strategy_selected["params"],
            seed=SEARCH_SEEDS[fold][strategy] + 2,
            pool=pool,
        )
    finally:
        pool.close()
        pool.join()
    selected_params = fine_selected["params"]
    selected_config = candidate_config(
        strategy,
        selected_params,
        train_start,
        train_end,
        track="B-L",
    )
    config_hash = stable_hash(selected_config)
    lock = {
        "fold": fold,
        "strategy": strategy,
        "train_window": [train_start, train_end],
        "selection_opened_validation": False,
        "selected_params": selected_params,
        "selected_params_hash": fine_selected["params_hash"],
        "selected_config_hash": config_hash,
        "selected_training_summary": fine_selected["summary"],
        "selected_training_tracks": fine_selected["tracks"],
        "shortlist": [
            {
                "rank": rank,
                "params_hash": record["params_hash"],
                "summary": record["summary"],
            }
            for rank, record in enumerate(fine_records[:10], start=1)
        ],
        "runtime": runtime_identity(),
        "dataset_manifest_sha256": sha256(DATASET / "manifest.json"),
    }
    lock_path = STUDY / "locks" / fold / f"{strategy}_candidate_lock.json"
    write_lock(lock_path, lock)
    write_json(
        STUDY / "locks" / fold / f"{strategy}_selected_config.json",
        selected_config,
    )
    install_state({})
    return lock


async def validate_one(strategy: str, fold: str) -> list[dict[str, Any]]:
    lock_path = STUDY / "locks" / fold / f"{strategy}_candidate_lock.json"
    if not lock_path.exists():
        raise FileNotFoundError(f"candidate must be locked before validation: {lock_path}")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    start, end = FOLDS[fold]["validation"]
    state = await prepare_window(strategy, start, end)
    install_state(state)
    rows = []
    for track in ("E-L", "E-S", "B-L"):
        metrics = run_track(strategy, lock["selected_params"], track)
        floors = ACTIVITY_FLOORS[track]
        pass_activity = all(
            float(metrics[metric]) >= floor for metric, floor in floors.items()
        )
        passed = (
            float(metrics["drawdown_worst_strategy_eq"]) <= MAXDD_CAP
            and float(metrics["backtest_completion_ratio"]) >= COMPLETION_FLOOR
            and float(metrics["adg_strategy_eq"]) >= ADG_FLOOR
            and float(metrics["gain_strategy_eq"]) > 1.0
            and not bool(metrics["liquidated"])
            and pass_activity
        )
        rows.append(
            {
                "fold": fold,
                "strategy": strategy,
                "track": track,
                "stage": "validation",
                "start": start,
                "end": end,
                "params_hash": lock["selected_params_hash"],
                "passed": passed,
                "activity_passed": pass_activity,
                **metrics,
            }
        )
    validation = {
        "fold": fold,
        "strategy": strategy,
        "candidate_lock_sha256": sha256(lock_path),
        "validation_window": [start, end],
        "rows": rows,
    }
    write_lock(
        STUDY / "validation" / fold / f"{strategy}_validation.json",
        validation,
    )
    install_state({})
    return rows


async def run_walk_forward(
    strategies: tuple[str, ...],
    folds: tuple[str, ...],
    *,
    search: bool,
    validate: bool,
) -> None:
    for fold in folds:
        for strategy in strategies:
            if search:
                await search_one(strategy, fold)
            if validate:
                await validate_one(strategy, fold)
    aggregate_validation_metrics()


def aggregate_validation_metrics() -> None:
    rows = []
    for fold in FOLDS:
        for strategy in STRATEGIES:
            path = STUDY / "validation" / fold / f"{strategy}_validation.json"
            if path.exists():
                rows.extend(json.loads(path.read_text(encoding="utf-8"))["rows"])
    if rows:
        pd.DataFrame(rows).to_csv(
            STUDY / "walk_forward_fold_metrics.csv", index=False
        )


def parse_multi(
    requested: list[str] | None,
    valid: tuple[str, ...],
) -> tuple[str, ...]:
    if not requested:
        return valid
    unknown = sorted(set(requested) - set(valid))
    if unknown:
        raise ValueError(f"unknown values {unknown}; expected one of {valid}")
    return tuple(requested)


async def async_main(args: argparse.Namespace) -> None:
    disable_network()
    freeze_manifest()
    if args.command == "freeze":
        return
    if args.command == "baseline":
        await run_baselines()
        return
    strategies = parse_multi(args.strategy, STRATEGIES)
    folds = parse_multi(args.fold, tuple(FOLDS))
    if args.command == "search":
        await run_walk_forward(strategies, folds, search=True, validate=False)
    elif args.command == "validate":
        await run_walk_forward(strategies, folds, search=False, validate=True)
    elif args.command == "walk-forward":
        await run_walk_forward(strategies, folds, search=True, validate=True)
    else:
        raise ValueError(f"unsupported command {args.command!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("freeze", "baseline", "search", "validate", "walk-forward"),
    )
    parser.add_argument(
        "--strategy",
        action="append",
        choices=STRATEGIES,
        help="Repeat to limit strategy paths.",
    )
    parser.add_argument(
        "--fold",
        action="append",
        choices=tuple(FOLDS),
        help="Repeat to limit walk-forward folds.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )
    started = time.perf_counter()
    asyncio.run(async_main(args))
    logging.info("Completed %s in %.1fs", args.command, time.perf_counter() - started)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
