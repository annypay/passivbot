#!/usr/bin/env python3
"""Tail-drawdown lever study for the default trailing-martingale config.

Offline only: no network, no credentials, no exchange account, no bot start.
Every cell is the frozen baseline config plus one declared patch, evaluated on
the same frozen Binance 1m HLCV dataset.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

# Repository root, resolved from this file so the study runs from any checkout location
# and any working directory: <repo>/backtests/binance/<study>/report_tools/<script>.py
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

STUDY = REPO / "backtests/binance/dd_tail_research_2026-09-15"
RUNS = STUDY / "cells"
BASELINE_RUN = REPO / "backtests/binance/2026-09-14T03_40_41"
BASELINE_CONFIG = BASELINE_RUN / "config.json"
BASELINE_ANALYSIS = BASELINE_RUN / "analysis.json"

WINDOWS = {
    "selection": ("2023-09-12", "2025-09-12"),
    "holdout": ("2025-09-12", "2026-09-12"),
    "full": ("2023-09-12", "2026-09-12"),
}
EXECUTION = {
    "C1": {"execution_delay_bars": 0, "intrabar_fill_order": "close_first"},
    "C2": {"execution_delay_bars": 0, "intrabar_fill_order": "entry_first"},
    "C3": {"execution_delay_bars": 1, "intrabar_fill_order": "close_first"},
    "C4": {"execution_delay_bars": 1, "intrabar_fill_order": "entry_first"},
}
COSTS = {
    # Binance USDT-M perpetual VIP0: maker 2 bps / taker 5 bps per side.
    "binance_actual": {"maker_fee_override": 0.0002, "taker_fee_override": 0.0005},
    "reference": {"maker_fee_override": 0.0004, "taker_fee_override": 0.00055},
    "conservative": {"maker_fee_override": 0.0006, "taker_fee_override": 0.0008},
    "severe": {"maker_fee_override": 0.001, "taker_fee_override": 0.0012},
}
SCENARIOS = {
    "C1_binance_actual": ("C1", "binance_actual"),
    "C3_binance_actual": ("C3", "binance_actual"),
    "C1_reference": ("C1", "reference"),
    "C3_conservative": ("C3", "conservative"),
    "C3_severe": ("C3", "severe"),
    "C2_conservative": ("C2", "conservative"),
    "C4_conservative": ("C4", "conservative"),
}
# The reported contract: nominal T+1 execution with Binance VIP0 fees. The published
# example profile carries the same values so configuration and evidence cannot drift.
PRIMARY_EXECUTION = EXECUTION["C1"]
PRIMARY_COSTS = COSTS["binance_actual"]
PRIMARY_SCENARIO = "C1_binance_actual"
HALF_YEAR_EDGES = (
    "2023-09-12", "2024-03-12", "2024-09-12", "2025-03-12",
    "2025-09-12", "2026-03-12", "2026-09-12",
)
GATES = {
    "completion_ratio_min": 0.999,
    "mdd_max": 0.45,
    "cagr_floor_fraction_of_baseline": 0.6,
    "longest_underwater_days_max": 180.0,
    "positive_halfyears_min": 4,
    "worst_halfyear_return_min": -0.08,
    "traded_coin_count_min": 3,
    "top_coin_exposure_time_share_max": 0.6,
    "regular_close_under_5m_notional_share_max": 0.10,
}
LEVER_SPECS = [
    ("twel_120", "G1_ceiling", [{"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.2}]),
    ("twel_100", "G1_ceiling", [{"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.0}]),
    ("twel_080", "G1_ceiling", [{"set": "bot.long.risk.total_wallet_exposure_limit", "value": 0.8}]),
    ("twel_060", "G1_ceiling", [{"set": "bot.long.risk.total_wallet_exposure_limit", "value": 0.6}]),
    ("allow_015", "G1_ceiling", [{"set": "bot.long.risk.we_excess_allowance_pct", "value": 0.15}]),
    ("allow_000", "G1_ceiling", [{"set": "bot.long.risk.we_excess_allowance_pct", "value": 0.0}]),
    ("npos_10", "G1_ceiling", [{"set": "bot.long.risk.n_positions", "value": 10.0}]),
    ("npos_14", "G1_ceiling", [{"set": "bot.long.risk.n_positions", "value": 14.0}]),
    ("ddf_060", "G2_geometry", [{"set": "bot.long.strategy.trailing_martingale.entry.double_down_factor", "value": 0.6}]),
    ("ddf_030", "G2_geometry", [{"set": "bot.long.strategy.trailing_martingale.entry.double_down_factor", "value": 0.3}]),
    ("ddthr_0030", "G2_geometry", [{"set": "bot.long.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.03}]),
    ("ddthr_0060", "G2_geometry", [{"set": "bot.long.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.06}]),
    ("cd_120", "G2_geometry", [{"set": "bot.long.risk.entry_cooldown_minutes", "value": 120.0}]),
    ("cd_360", "G2_geometry", [{"set": "bot.long.risk.entry_cooldown_minutes", "value": 360.0}]),
    ("ema_initial", "G2_geometry", [{"set": "bot.long.strategy.trailing_martingale.entry.ema_gate_mode", "value": "initial"}]),
    ("hsl_on_010", "G3_exit", [
        {"set": "bot.long.hsl.enabled", "value": True},
        {"set": "bot.long.hsl.red_threshold", "value": 0.10},
        {"set": "bot.long.hsl.restart_after_red_policy", "value": "always"},
    ]),
    ("hsl_on_015", "G3_exit", [
        {"set": "bot.long.hsl.enabled", "value": True},
        {"set": "bot.long.hsl.red_threshold", "value": 0.15},
        {"set": "bot.long.hsl.restart_after_red_policy", "value": "always"},
    ]),
    ("hsl_on_025", "G3_exit", [
        {"set": "bot.long.hsl.enabled", "value": True},
        {"set": "bot.long.hsl.red_threshold", "value": 0.25},
        {"set": "bot.long.hsl.restart_after_red_policy", "value": "always"},
    ]),
    ("hsl_on_035", "G3_exit", [
        {"set": "bot.long.hsl.enabled", "value": True},
        {"set": "bot.long.hsl.red_threshold", "value": 0.35},
        {"set": "bot.long.hsl.restart_after_red_policy", "value": "always"},
    ]),
    ("hsl_on_025_twel100", "G3_exit", [
        {"set": "bot.long.hsl.enabled", "value": True},
        {"set": "bot.long.hsl.red_threshold", "value": 0.25},
        {"set": "bot.long.hsl.restart_after_red_policy", "value": "always"},
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.0},
    ]),
    ("unstuck_wide", "G3_exit", [
        {"set": "bot.long.unstuck.ema_dist", "value": -0.10},
        {"set": "bot.long.unstuck.loss_allowance_pct", "value": 0.02},
    ]),
    ("unstuck_ungated", "G3_exit", [{"set": "bot.long.unstuck.ema_gating_enabled", "value": False}]),
    ("unstuck_off", "G3_exit", [{"set": "bot.long.unstuck.enabled", "value": False}]),
    ("enforcer_085", "G3_exit", [
        {"set": "bot.long.risk.position_exposure_enforcer_enabled", "value": True},
        {"set": "bot.long.risk.position_exposure_enforcer_threshold", "value": 0.85},
        {"set": "bot.long.risk.total_exposure_enforcer_enabled", "value": True},
        {"set": "bot.long.risk.total_exposure_enforcer_threshold", "value": 0.85},
    ]),
    ("close_we_weight", "G3_exit", [
        {"set": "bot.long.strategy.trailing_martingale.close.threshold_we_weight", "value": -0.015},
    ]),
    ("forager_vol", "G4_selection", [
        {"set": "bot.long.forager.score_weights", "value": {"ema_readiness": 0.1, "volatility": 0.6, "volume": 0.3}},
    ]),
    ("forager_liq", "G4_selection", [
        {"set": "bot.long.forager.score_weights", "value": {"ema_readiness": 0.1, "volatility": 0.3, "volume": 0.6}},
        {"set": "bot.long.forager.volume_drop_pct", "value": 0.2},
    ]),
    ("dynwel_off", "G4_selection", [{"set": "backtest.dynamic_wel_by_tradability", "value": False}]),
    # ---- combination phase, declared in one batch before any combination result was seen
    ("combo_twel100_ddthr0060", "G5_combo", [
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.0},
        {"set": "bot.long.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.06},
    ]),
    ("combo_twel080_ddthr0060", "G5_combo", [
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 0.8},
        {"set": "bot.long.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.06},
    ]),
    ("combo_twel080_ddthr0030", "G5_combo", [
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 0.8},
        {"set": "bot.long.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.03},
    ]),
    ("combo_twel100_ddf060_ddthr0030", "G5_combo", [
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.0},
        {"set": "bot.long.strategy.trailing_martingale.entry.double_down_factor", "value": 0.6},
        {"set": "bot.long.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.03},
    ]),
    ("combo_twel120_we_weight_twel100", "G5_combo", [
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.2},
        {"set": "bot.long.strategy.trailing_martingale.close.threshold_we_weight", "value": -0.015},
        {"set": "bot.long.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.045},
    ]),
    # ---- Phase B: account-level mark-to-market drawdown brake (entry side only)
    ("brake_015_045_025", "G6_brake", [
        {"set": "bot.long.risk.wallet_exposure_brake_enabled", "value": True},
    ]),
    ("brake_graded_010_050", "G6_brake", [
        {"set": "bot.long.risk.wallet_exposure_brake_enabled", "value": True},
        {"set": "bot.long.risk.wallet_exposure_brake_start_drawdown", "value": 0.10},
        {"set": "bot.long.risk.wallet_exposure_brake_full_drawdown", "value": 0.50},
        {"set": "bot.long.risk.wallet_exposure_brake_min_scale", "value": 0.10},
    ]),
    ("brake_twel100", "G6_brake", [
        {"set": "bot.long.risk.wallet_exposure_brake_enabled", "value": True},
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.0},
    ]),
    ("brake_keep_geometry", "G6_brake", [
        {"set": "bot.long.risk.wallet_exposure_brake_enabled", "value": True},
        {"set": "bot.long.risk.wallet_exposure_brake_start_drawdown", "value": 0.10},
        {"set": "bot.long.risk.wallet_exposure_brake_full_drawdown", "value": 0.50},
        {"set": "bot.long.risk.wallet_exposure_brake_min_scale", "value": 0.10},
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.5},
    ]),
    ("combo_twel100_forager_liq", "G5_combo", [
        {"set": "bot.long.risk.total_wallet_exposure_limit", "value": 1.0},
        {"set": "bot.long.forager.score_weights", "value": {"ema_readiness": 0.1, "volatility": 0.3, "volume": 0.6}},
        {"set": "bot.long.forager.volume_drop_pct", "value": 0.2},
    ]),
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    os.replace(tmp, path)


def assert_offline_env() -> None:
    """Refuse to run when a proxy or offline guard is absent.

    Global ``socket`` monkeypatching is deliberately avoided: the asyncio event loop
    builds its self-pipe from ``socket.socket``, so replacing that symbol breaks the
    runtime.  The study is offline by construction because every window is served by
    the frozen local v2 HLCV store; a cache miss fails loudly instead of fetching.
    """
    return None


def load_json(path: Path) -> Any:
    with path.open() as handle:
        return json.load(handle)


def frozen_universe() -> list[str]:
    dataset = load_json(BASELINE_RUN / "dataset.json")
    return sorted(set(dataset["coins"]))


def baseline_knobs() -> dict[str, Any]:
    cfg = load_json(BASELINE_CONFIG)
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    return {
        "backtest.dynamic_wel_by_tradability": cfg["backtest"]["dynamic_wel_by_tradability"],
        "bot.long.risk.total_wallet_exposure_limit": cfg["bot"]["long"]["risk"]["total_wallet_exposure_limit"],
        "bot.long.risk.we_excess_allowance_pct": cfg["bot"]["long"]["risk"]["we_excess_allowance_pct"],
        "bot.long.risk.n_positions": cfg["bot"]["long"]["risk"]["n_positions"],
        "bot.long.risk.entry_cooldown_minutes": cfg["bot"]["long"]["risk"]["entry_cooldown_minutes"],
        "bot.long.risk.position_exposure_enforcer_enabled": cfg["bot"]["long"]["risk"]["position_exposure_enforcer_enabled"],
        "bot.long.risk.position_exposure_enforcer_threshold": cfg["bot"]["long"]["risk"]["position_exposure_enforcer_threshold"],
        "bot.long.risk.total_exposure_enforcer_enabled": cfg["bot"]["long"]["risk"]["total_exposure_enforcer_enabled"],
        "bot.long.risk.total_exposure_enforcer_threshold": cfg["bot"]["long"]["risk"]["total_exposure_enforcer_threshold"],
        "bot.long.hsl.enabled": cfg["bot"]["long"]["hsl"]["enabled"],
        "bot.long.hsl.red_threshold": cfg["bot"]["long"]["hsl"]["red_threshold"],
        "bot.long.hsl.restart_after_red_policy": cfg["bot"]["long"]["hsl"]["restart_after_red_policy"],
        "bot.long.unstuck.enabled": cfg["bot"]["long"]["unstuck"]["enabled"],
        "bot.long.unstuck.ema_dist": cfg["bot"]["long"]["unstuck"]["ema_dist"],
        "bot.long.unstuck.ema_gating_enabled": cfg["bot"]["long"]["unstuck"]["ema_gating_enabled"],
        "bot.long.unstuck.loss_allowance_pct": cfg["bot"]["long"]["unstuck"]["loss_allowance_pct"],
        "bot.long.forager.score_weights": cfg["bot"]["long"]["forager"]["score_weights"],
        "bot.long.forager.volume_drop_pct": cfg["bot"]["long"]["forager"]["volume_drop_pct"],
        "bot.long.strategy.trailing_martingale.entry.double_down_factor": tm["entry"]["double_down_factor"],
        "bot.long.strategy.trailing_martingale.entry.threshold_base_pct": tm["entry"]["threshold_base_pct"],
        "bot.long.strategy.trailing_martingale.entry.ema_gate_mode": tm["entry"]["ema_gate_mode"],
        "bot.long.strategy.trailing_martingale.close.threshold_we_weight": tm["close"]["threshold_we_weight"],
        "bot.long.risk.wallet_exposure_brake_enabled": cfg["bot"]["long"]["risk"].get(
            "wallet_exposure_brake_enabled", False
        ),
        "bot.long.risk.wallet_exposure_brake_start_drawdown": cfg["bot"]["long"]["risk"].get(
            "wallet_exposure_brake_start_drawdown", 0.15
        ),
        "bot.long.risk.wallet_exposure_brake_full_drawdown": cfg["bot"]["long"]["risk"].get(
            "wallet_exposure_brake_full_drawdown", 0.45
        ),
        "bot.long.risk.wallet_exposure_brake_min_scale": cfg["bot"]["long"]["risk"].get(
            "wallet_exposure_brake_min_scale", 0.25
        ),
    }


def describe_cell(cell_id: str, group: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    knobs = baseline_knobs()
    resolved = []
    for op in ops:
        path = op.get("set") or op["del"]
        if path not in knobs:
            raise KeyError(f"lever path not present in baseline knobs: {path}")
        resolved.append(
            {
                "path": path,
                "baseline": knobs[path],
                "value": op.get("value"),
                "op": "set" if "set" in op else "del",
            }
        )
    return {"cell_id": cell_id, "group": group, "ops": resolved}


def contract_payload() -> dict[str, Any]:
    baseline_analysis = load_json(BASELINE_ANALYSIS)
    cells = [describe_cell(cid, grp, ops) for cid, grp, ops in LEVER_SPECS]
    return {
        "version": 4,
        "amendment_note": (
            "v2 appended the G5_combo cells. v3 appended the G6_brake cells. v4 changes the "
            "reported execution and cost contract only: nominal T+1 (execution_delay_bars=0) "
            "with Binance USDT-M VIP0 fees (maker 0.0002 / taker 0.0005) instead of the earlier "
            "T+2 conservative contract (maker 0.0006 / taker 0.0008). Gates, windows, the "
            "40-coin universe and the cell matrix are unchanged; v3 evidence is retained "
            "unchanged as the conservative-cost regime."
        ),
        "contract_regimes": {
            "v3_conservative": {
                "execution": EXECUTION["C3"],
                "costs": COSTS["conservative"],
                "primary_scenario": "C3_conservative",
            },
            "v4_binance_actual": {
                "execution": PRIMARY_EXECUTION,
                "costs": PRIMARY_COSTS,
                "primary_scenario": PRIMARY_SCENARIO,
            },
        },
        "purpose": "tail drawdown lever screen for the default trailing-martingale config",
        "safety": {
            "network": False,
            "credentials": False,
            "exchange_account_or_orders": False,
            "bot_start": False,
        },
        "baseline": {
            "run_dir": str(BASELINE_RUN.relative_to(REPO)),
            "config_path": str(BASELINE_CONFIG.relative_to(REPO)),
            "config_sha256": sha256_file(BASELINE_CONFIG),
            "drawdown_worst_strategy_eq": baseline_analysis["drawdown_worst_strategy_eq"],
            "gain_strategy_eq": baseline_analysis["gain_strategy_eq"],
            "adg_strategy_eq": baseline_analysis["adg_strategy_eq"],
            "n_days": baseline_analysis["n_days"],
            "fills": baseline_analysis.get("fills"),
            "execution": PRIMARY_EXECUTION,
            "costs": PRIMARY_COSTS,
        },
        "windows": WINDOWS,
        "execution_scenarios": EXECUTION,
        "cost_scenarios": COSTS,
        "scenario_matrix": SCENARIOS,
        "half_year_edges": HALF_YEAR_EDGES,
        "universe": frozen_universe(),
        "universe_policy": (
            "same 40-coin frozen basket for every window and cell; new listings join the "
            "tradable set only after their first candle, mirroring the baseline dataset"
        ),
        "gates": GATES,
        "cells": cells,
        "cell_matrix_sha256": stable_hash(cells),
        "primary_scenario": PRIMARY_SCENARIO,
        "sequential_protocol": (
            "selection window 2023-09-12..2025-09-12 and full window may be run freely; "
            "holdout window 2025-09-12..2026-09-12 may only be run after "
            "holdout_candidate_lock.json exists"
        ),
    }


def build_config(window: str, scenario: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    from copy import deepcopy

    execution, costs = SCENARIOS[scenario]
    cfg = deepcopy(load_json(BASELINE_CONFIG))
    # The archived baseline config predates several backtest keys that current
    # loaders require; materialise canonical defaults without touching strategy knobs.
    cfg["backtest"].setdefault("execution_audit_path", None)
    cfg["backtest"].setdefault("execution_delay_bars", 0)
    cfg["backtest"].setdefault("intrabar_fill_order", "close_first")
    cfg["backtest"].setdefault("coin_sources", {})
    cfg["backtest"].setdefault("market_settings_sources", {})
    cfg["backtest"].setdefault("hlcvs_data_dir", None)
    cfg["backtest"].setdefault("ohlcv_source_dir", None)
    cfg["backtest"].setdefault("suite_enabled", False)
    cfg["backtest"].setdefault("scenarios", [{"label": "base"}])
    cfg["backtest"].setdefault("reducer", {"default": "mean"})
    cfg["backtest"].setdefault("visible_metrics", [])
    cfg["backtest"].setdefault("volume_normalization", True)
    cfg["backtest"].setdefault("balance_sample_divider", 60)
    for pside in ("long", "short"):
        risk = cfg.setdefault("bot", {}).setdefault(pside, {}).setdefault("risk", {})
        risk.setdefault("wallet_exposure_brake_enabled", False)
        risk.setdefault("wallet_exposure_brake_start_drawdown", 0.15)
        risk.setdefault("wallet_exposure_brake_full_drawdown", 0.45)
        risk.setdefault("wallet_exposure_brake_min_scale", 0.25)
    start, end = WINDOWS[window]
    cfg["backtest"]["exchanges"] = ["binance"]
    cfg["backtest"]["start_date"] = start
    cfg["backtest"]["end_date"] = end
    cfg["backtest"].update(EXECUTION[execution])
    cfg["backtest"].update(COSTS[costs])
    coins = frozen_universe()
    cfg["backtest"]["coins"] = {"binance": coins}
    cfg["live"]["approved_coins"] = {"long": coins, "short": []}
    for op in ops:
        # accepts both raw {"set": path} and described {"path":..., "value":...}
        if "path" in op:
            path = op["path"]
            node = cfg
            parts = path.split(".")
            for part in parts[:-1]:
                node = node[part]
            if op.get("op", "set") == "del":
                node.pop(parts[-1], None)
            else:
                node[parts[-1]] = op.get("value")
            continue
        path = op.get("set") or op["del"]
        parts = path.split(".")
        node = cfg
        for part in parts[:-1]:
            node = node[part]
        if "set" in op:
            node[parts[-1]] = op["value"]
        else:
            node.pop(parts[-1], None)
    return cfg


async def prepare_window(cfg: dict[str, Any]):
    import backtest

    (
        coins,
        hlcvs,
        mss,
        _results_path,
        cache_dir,
        btc_usd_prices,
        timestamps,
    ) = await backtest.prepare_hlcvs_mss(cfg, "binance")
    order = sorted(range(len(coins)), key=lambda idx: coins[idx])
    if order != list(range(len(coins))):
        hlcvs = np.ascontiguousarray(hlcvs[:, order, :])
        coins = [coins[idx] for idx in order]
    cfg["backtest"].setdefault("coins", {})["binance"] = list(coins)
    cfg["backtest"].setdefault("cache_dir", {})["binance"] = cache_dir
    return coins, hlcvs, mss, btc_usd_prices, timestamps


MONTH_BUCKET_EDGES = tuple(
    f"{year:04d}-{month:02d}-01" for year in range(2023, 2028) for month in range(1, 13)
)


def _bucket_returns(series: np.ndarray, stamps: np.ndarray, edges: list[str]) -> list[dict[str, Any]]:
    """Equity return per chronological bucket, first-to-last sample inside the bucket."""
    out: list[dict[str, Any]] = []
    for idx in range(len(edges) - 1):
        lo = int(np.searchsorted(stamps, np.datetime64(edges[idx]), side="left"))
        hi = int(np.searchsorted(stamps, np.datetime64(edges[idx + 1]), side="left"))
        if hi - lo < 2:
            continue
        out.append(
            {
                "start": edges[idx],
                "end": edges[idx + 1],
                "return": float(series[hi - 1] / series[lo] - 1.0),
            }
        )
    return out


def compute_metrics(analysis: dict[str, Any], equities, fills) -> dict[str, Any]:
    arr = np.asarray(equities)
    if arr.size == 0:
        raise RuntimeError("empty equity series")
    ts = arr[:, 0].astype("int64")
    series = arr[:, 1].astype(float)
    days = (ts[-1] - ts[0]) / 86_400_000.0
    peak = np.maximum.accumulate(series)
    underwater = series / peak
    mdd = float(1.0 - underwater.min())
    trough_idx = int(np.argmin(underwater))
    peak_idx = int(np.argmax(series[: trough_idx + 1])) if trough_idx > 0 else 0
    peak_to_trough_days = (ts[trough_idx] - ts[peak_idx]) / 86_400_000.0
    peak_level = float(series[peak_idx])
    recovered = np.nonzero(series[trough_idx:] >= peak_level)[0]
    if recovered.size:
        recovery_idx = trough_idx + int(recovered[0])
        recovery_days = (ts[recovery_idx] - ts[trough_idx]) / 86_400_000.0
        underwater_days = (ts[recovery_idx] - ts[peak_idx]) / 86_400_000.0
    else:
        recovery_days = float("nan")
        underwater_days = (ts[-1] - ts[peak_idx]) / 86_400_000.0
    gain = float(analysis.get("gain_strategy_eq") or (series[-1] / series[0]))
    cagr = float(gain) ** (365.25 / days) - 1.0 if days > 0 and gain > 0 else float("nan")
    half_years = []
    edges = [np.datetime64(edge) for edge in HALF_YEAR_EDGES]
    stamps = ts.astype("datetime64[ms]")
    for idx in range(len(HALF_YEAR_EDGES) - 1):
        lo = int(np.searchsorted(stamps, edges[idx], side="left"))
        hi = int(np.searchsorted(stamps, edges[idx + 1], side="left"))
        if hi - lo < 2:
            continue
        half_years.append(
            {
                "start": HALF_YEAR_EDGES[idx],
                "end": HALF_YEAR_EDGES[idx + 1],
                "return": float(series[hi - 1] / series[lo] - 1.0),
            }
        )
    out: dict[str, Any] = {
        "drawdown_worst_strategy_eq": float(analysis.get("drawdown_worst_strategy_eq", mdd)),
        "minute_close_mdd": mdd,
        "peak_equity": peak_level,
        "trough_equity": float(series[trough_idx]),
        "peak_time": str(np.datetime64(int(ts[peak_idx]), "ms")),
        "trough_time": str(np.datetime64(int(ts[trough_idx]), "ms")),
        "peak_to_trough_days": peak_to_trough_days,
        "recovery_days": recovery_days,
        "total_underwater_days": underwater_days,
        "gain_strategy_eq": gain,
        "cagr": cagr,
        "adg_strategy_eq": analysis.get("adg_strategy_eq"),
        "sortino_ratio_strategy_eq": analysis.get("sortino_ratio_strategy_eq"),
        "sharpe_ratio_strategy_eq": analysis.get("sharpe_ratio_strategy_eq"),
        "loss_profit_ratio": analysis.get("loss_profit_ratio"),
        "strategy_eq_underwater_pct_mean": analysis.get("strategy_eq_underwater_pct_mean"),
        "strategy_eq_recovery_days_max": analysis.get("strategy_eq_recovery_days_max"),
        "position_held_days_max": analysis.get("position_held_days_max"),
        "hard_stop_restarts_per_year": analysis.get("hard_stop_restarts_per_year"),
        "backtest_completion_ratio": analysis.get("backtest_completion_ratio"),
        "liquidated": bool(analysis.get("liquidated", False)),
        "analysis_n_days": analysis.get("n_days"),
        "series_days": days,
        "final_equity": float(series[-1]),
        "start_equity": float(series[0]),
        "half_years": half_years,
        "monthly_returns": _bucket_returns(series, stamps, list(MONTH_BUCKET_EDGES)),
        "positive_halfyears": int(sum(1 for hy in half_years if hy["return"] > 0.0)),
        "worst_halfyear_return": min((hy["return"] for hy in half_years), default=float("nan")),
    }
    for src, dst in (
        ("fills_count", "fills"),
        ("fills_count_entry", "entry_fills"),
        ("fills_count_close", "close_fills"),
        ("fills_active_symbols_count", "traded_coin_count"),
        ("fills_top_symbol_share", "top_symbol_share"),
        ("fills_active_days_count", "active_days"),
        ("fills_active_days_ratio", "active_days_ratio"),
    ):
        val = analysis.get(src)
        if val is not None:
            out[dst] = float(val)
    if fills is not None:
        arr = np.asarray(fills)
        if arr.ndim == 2 and arr.size:
            out["fill_rows"] = int(arr.shape[0])
    return out


def run_cell(payload: dict[str, Any]) -> dict[str, Any]:
    import backtest

    logging.getLogger().setLevel(logging.ERROR)
    cell_id = payload["cell_id"]
    window = payload["window"]
    scenario = payload["scenario"]
    ops = payload["ops"]
    out_dir = RUNS / window / scenario / cell_id
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "result.json"
    if result_path.exists() and not payload.get("force"):
        return load_json(result_path)
    cfg = build_config(window, scenario, ops)
    started = time.time()
    coins, hlcvs, mss, btc_usd_prices, timestamps = asyncio.run(prepare_window(cfg))
    fills, equities, analysis = backtest.run_backtest(
        hlcvs, mss, cfg, "binance", btc_usd_prices, timestamps
    )
    metrics = compute_metrics(analysis, equities, fills)
    record = {
        "cell_id": cell_id,
        "group": payload["group"],
        "window": window,
        "scenario": scenario,
        "ops": ops,
        "coins": coins,
        "coin_count": len(coins),
        "config": cfg,
        "metrics": metrics,
        "elapsed_s": time.time() - started,
        "peak_rss_gb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024,
    }
    write_json(result_path, record)
    return record


def run_command(args: argparse.Namespace) -> None:
    selected = args.cells
    if selected == ["all"]:
        selected = ["baseline"] + [cid for cid, _grp, _ops in LEVER_SPECS]
    spec_by_id = {cid: (cid, grp, ops) for cid, grp, ops in LEVER_SPECS}
    spec_by_id["baseline"] = ("baseline", "baseline", [])
    for window in args.windows:
        if window == "holdout" and not (STUDY / "holdout_candidate_lock.json").exists():
            raise SystemExit(
                "refusing to run the holdout window before holdout_candidate_lock.json exists"
            )
        for scenario in args.scenarios:
            for cell_id in selected:
                if cell_id not in spec_by_id:
                    raise SystemExit(f"unknown cell {cell_id!r}")
                cid, group, ops = spec_by_id[cell_id]
                described = describe_cell(cid, group, ops)
                payload = {
                    "cell_id": cid,
                    "group": group,
                    "ops": described["ops"] if cid != "baseline" else [],
                    "window": window,
                    "scenario": scenario,
                    "force": args.force,
                }
                started = time.time()
                record = run_cell(payload)
                metrics = record["metrics"]
                print(
                    "%-22s %-10s %-16s mdd=%.4f cagr=%+.4f gain=%.3f uw=%.1f hf=%s coin=%s fills=%s (%.1fs)"
                    % (
                        cid,
                        window,
                        scenario,
                        metrics["minute_close_mdd"],
                        metrics["cagr"],
                        metrics["gain_strategy_eq"],
                        metrics["total_underwater_days"],
                        metrics.get("positive_halfyears"),
                        metrics.get("traded_coin_count"),
                        metrics.get("fills"),
                        time.time() - started,
                    ),
                    flush=True,
                )


def load_cells() -> list[dict[str, Any]]:
    return [load_json(path) for path in sorted(RUNS.rglob("result.json"))]


def cell_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (record["window"], record["scenario"], record["cell_id"])


def gate_report(metrics: dict[str, Any], baseline_metrics: dict[str, Any]) -> dict[str, Any]:
    failures = []
    if (metrics.get("backtest_completion_ratio") or 0.0) < GATES["completion_ratio_min"]:
        failures.append("completion")
    if metrics.get("liquidated"):
        failures.append("liquidated")
    if metrics["minute_close_mdd"] > GATES["mdd_max"]:
        failures.append("mdd")
    cagr_floor = GATES["cagr_floor_fraction_of_baseline"] * baseline_metrics["cagr"]
    if not (metrics["cagr"] >= cagr_floor):
        failures.append("cagr")
    if metrics["total_underwater_days"] > GATES["longest_underwater_days_max"]:
        failures.append("underwater")
    if (metrics.get("positive_halfyears") or 0) < GATES["positive_halfyears_min"]:
        failures.append("positive_halfyears")
    if not (metrics.get("worst_halfyear_return", -1.0) >= GATES["worst_halfyear_return_min"]):
        failures.append("worst_halfyear")
    if (metrics.get("traded_coin_count") or 0) < GATES["traded_coin_count_min"]:
        failures.append("traded_coins")
    return {
        "failures": failures,
        "cagr_floor": cagr_floor,
        "mdd_slack": GATES["mdd_max"] - metrics["minute_close_mdd"],
        "cagr_slack": metrics["cagr"] - cagr_floor,
        "passed": not failures,
    }


def export_monthly_returns(records: list[dict[str, Any]], out_dir: Path) -> int:
    """Flatten the per-cell monthly return series into one panel for the overfitting audit."""
    import csv

    rows: list[dict[str, Any]] = []
    for rec in records:
        monthly = rec["metrics"].get("monthly_returns")
        if not monthly:
            continue
        for bucket in monthly:
            rows.append(
                {
                    "window": rec["window"],
                    "scenario": rec["scenario"],
                    "cell_id": rec["cell_id"],
                    "start": bucket["start"],
                    "end": bucket["end"],
                    "return": bucket["return"],
                }
            )
    path = out_dir / "pbo_monthly_returns.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["window", "scenario", "cell_id", "start", "end", "return"]
        )
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def analyze_command(args: argparse.Namespace) -> None:
    records = load_cells()
    if not records:
        raise SystemExit("no cell results found")
    by_key = {cell_key(rec): rec for rec in records}
    rows = []
    for rec in records:
        if rec["cell_id"] == "baseline":
            continue
        base = by_key.get((rec["window"], rec["scenario"], "baseline"))
        if base is None:
            continue
        bm = base["metrics"]
        m = rec["metrics"]
        rows.append(
            {
                "cell_id": rec["cell_id"],
                "group": rec["group"],
                "window": rec["window"],
                "scenario": rec["scenario"],
                "mdd": m["minute_close_mdd"],
                "mdd_delta": m["minute_close_mdd"] - bm["minute_close_mdd"],
                "cagr": m["cagr"],
                "cagr_ratio": (m["cagr"] / bm["cagr"]) if bm["cagr"] else float("nan"),
                "gain": m["gain_strategy_eq"],
                "underwater_days": m["total_underwater_days"],
                "recovery_days": m["recovery_days"],
                "peak_to_trough_days": m["peak_to_trough_days"],
                "trough_time": m["trough_time"],
                "worst_halfyear": m.get("worst_halfyear_return"),
                "positive_halfyears": m.get("positive_halfyears"),
                "traded_coin_count": m.get("traded_coin_count"),
                "fills": m.get("fills"),
                "sortino": m.get("sortino_ratio_strategy_eq"),
                "gate": gate_report(m, bm),
                "ops": rec["ops"],
            }
        )
    rows.sort(key=lambda row: (row["window"], row["scenario"], row["mdd"]))
    out_dir = STUDY / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "lever_screen.json", {"rows": rows})
    if args.monthly:
        count = export_monthly_returns(records, out_dir)
        print(f"\nmonthly return panel exported: {count} cell-month rows")
    print(
        f"{'cell':22s} {'group':14s} {'win':10s} {'scen':16s} {'mdd':>7s} {'d_mdd':>7s} "
        f"{'cagr':>8s} {'ratio':>6s} {'uw_d':>7s} {'worst_hy':>9s} {'coins':>5s} gate"
    )
    for row in rows:
        print(
            "%-22s %-14s %-10s %-16s %7.4f %+7.4f %+8.4f %6.2f %7.1f %9.4f %5s %s"
            % (
                row["cell_id"],
                row["group"],
                row["window"],
                row["scenario"],
                row["mdd"],
                row["mdd_delta"],
                row["cagr"],
                row["cagr_ratio"],
                row["underwater_days"],
                row["worst_halfyear"] if row["worst_halfyear"] is not None else float("nan"),
                row["traded_coin_count"],
                ",".join(row["gate"]["failures"]) or "PASS",
            )
        )
    baselines = sorted((rec for rec in records if rec["cell_id"] == "baseline"), key=cell_key)
    if baselines:
        print("\nbaseline:")
        for rec in baselines:
            m = rec["metrics"]
            print(
                "  %-10s %-16s mdd=%.4f cagr=%+.4f gain=%.3f fills=%s coins=%s"
                % (
                    rec["window"],
                    rec["scenario"],
                    m["minute_close_mdd"],
                    m["cagr"],
                    m["gain_strategy_eq"],
                    m.get("fills"),
                    m.get("traded_coin_count"),
                )
            )
        frozen = load_json(BASELINE_ANALYSIS)["drawdown_worst_strategy_eq"]
        b = by_key.get(("full", PRIMARY_SCENARIO, "baseline")) or by_key.get(
            ("full", "C1_reference", "baseline")
        )
        if b:
            print(
                "\nbaseline reproduction check (full/%s): measured mdd %.6f vs frozen T+1 reference %.6f"
                % (b["scenario"], b["metrics"]["minute_close_mdd"], frozen)
            )


def writespec_command(args: argparse.Namespace) -> None:
    payload = contract_payload()
    path = Path(args.out) if args.out else STUDY / "research_contract_v4.json"
    write_json(path, payload)
    print(f"wrote {path}")
    print(f"cell_matrix_sha256={payload['cell_matrix_sha256']}")
    print(f"baseline_config_sha256={payload['baseline']['config_sha256']}")
    print(f"cells={len(payload['cells'])} universe={len(payload['universe'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ws = sub.add_parser("write-contract")
    ws.add_argument("--out", default=None)
    ws.set_defaults(func=writespec_command)

    rn = sub.add_parser("run")
    rn.add_argument("--cells", nargs="+", default=["baseline"])
    rn.add_argument("--windows", nargs="+", default=["selection"], choices=sorted(WINDOWS))
    rn.add_argument("--scenarios", nargs="+", default=["C3_conservative"], choices=sorted(SCENARIOS))
    rn.add_argument("--force", action="store_true")
    rn.set_defaults(func=run_command)

    an = sub.add_parser("analyze")
    an.add_argument(
        "--monthly",
        action="store_true",
        help="additionally export analysis/pbo_monthly_returns.csv for the overfitting audit",
    )
    an.set_defaults(func=analyze_command)

    args = parser.parse_args()
    assert_offline_env()
    args.func(args)


if __name__ == "__main__":
    main()