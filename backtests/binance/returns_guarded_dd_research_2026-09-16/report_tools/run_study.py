#!/usr/bin/env python3
"""Return-preserving drawdown study for the default trailing-martingale config.

Offline only: no network, no credentials, no exchange account, no bot start.
Every cell is the published baseline config plus one declared patch, evaluated on
the same frozen Binance 1m HLCV dataset.

Windows (predeclared in research_contract.json before any cell ran):

- ``fit``      2023-09-12 .. 2025-09-12  parameter selection
- ``full``     2023-09-12 .. 2026-09-12  reported window
- ``holdout``  2025-09-12 .. 2026-09-12  locked before it is opened
- ``fresh``    2026-06-13 .. 2026-09-11  never-before-used tail, finalists only
- ``stress``   2021-06-01 .. 2026-09-11  extended cache, finalists only
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

# Repository root, resolved from this file so the study runs from any checkout
# location and any working directory:
# <repo>/backtests/binance/<study>/report_tools/<script>.py
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "src"))

STUDY = REPO / "backtests/binance/returns_guarded_dd_research_2026-09-16"
RUNS = STUDY / "cells"
ANALYSIS = STUDY / "analysis"

# Frozen baseline: the run directory that produced annual_analysis.md and that the
# published tail-drawdown study compares against.
BASELINE_RUN = REPO / "backtests/binance/2026-09-14T03_40_41"
BASELINE_CONFIG = BASELINE_RUN / "config.json"
BASELINE_ANALYSIS = BASELINE_RUN / "analysis.json"

# The lower-tail profile already published. Used as the primary seed for this
# study's grids because it is the closest known point to the <=30% drawdown goal.
PUBLISHED_PROFILE = REPO / "configs/examples/trailing_martingale_twel100_ddf060.json"

WINDOWS = {
    "fit": ("2023-09-12", "2025-09-12"),
    "full": ("2023-09-12", "2026-09-12"),
    "holdout": ("2025-09-12", "2026-09-12"),
    "fresh": ("2026-06-13", "2026-09-11"),
    "stress": ("2021-06-01", "2026-09-11"),
}
# Windows that may only run after holdout_candidate_lock.json exists.
LOCKED_WINDOWS = ("holdout", "fresh", "stress")

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
}
# Reported contract: nominal T+1 execution with Binance VIP0 fees. The published
# example profile carries the same values, so configuration and evidence cannot drift.
PRIMARY_EXECUTION = EXECUTION["C1"]
PRIMARY_COSTS = COSTS["binance_actual"]
PRIMARY_SCENARIO = "C1_binance_actual"
EXTREME_SCENARIO = "C3_severe"

HALF_YEAR_EDGES = (
    "2023-09-12", "2024-03-12", "2024-09-12", "2025-03-12",
    "2025-09-12", "2026-03-12", "2026-09-12",
)
HALF_YEAR_EDGES_STRESS = tuple(
    f"{year}-{month:02d}-01" for year in range(2021, 2027) for month in (1, 7)
) + ("2026-09-11",)

# Acceptance gates. Deliberately expressed as absolute floors rather than as a
# fraction of the baseline: the baseline compounds fast enough that a relative
# floor would be meaningless (see docs/strategy_profiles.md, gates section).
GATES = {
    "completion_ratio_min": 0.999,
    "mdd_max_full": 0.30,
    "mdd_max_valid": 0.30,
    "cagr_floor_full": 0.25,
    "cagr_floor_valid": 0.15,
    "longest_underwater_days_max": 180.0,
    "positive_halfyears_min": 10,
    "worst_halfyear_return_min": -0.08,
    "traded_coin_count_min": 3,
}

P_LONG = "bot.long"
P_SHORT = "bot.short"
TM = f"{P_LONG}.strategy.trailing_martingale"
TS = f"{P_SHORT}.strategy.trailing_martingale"

_RAW_CELLS: list[tuple[str, str, list[dict[str, Any]]]] = [
    # ---- G0: reference points -------------------------------------------------
    ("published", "G0_reference", []),
    # ---- G1: re-entry ladder geometry (entry side only) ----------------------
    ("g1_ddf070", "G1_geometry", [{"set": f"{TM}.entry.double_down_factor", "value": 0.7}]),
    ("g1_ddf050", "G1_geometry", [{"set": f"{TM}.entry.double_down_factor", "value": 0.5}]),
    ("g1_thr02500", "G1_geometry", [{"set": f"{TM}.entry.threshold_base_pct", "value": 0.025}]),
    ("g1_thr04000", "G1_geometry", [{"set": f"{TM}.entry.threshold_base_pct", "value": 0.04}]),
    ("g1_thr05000", "G1_geometry", [{"set": f"{TM}.entry.threshold_base_pct", "value": 0.05}]),
    ("g1_rw000", "G1_geometry", [{"set": f"{TM}.entry.retracement_we_weight", "value": 0.0}]),
    ("g1_rw070", "G1_geometry", [{"set": f"{TM}.entry.retracement_we_weight", "value": 0.7}]),
    ("g1_tw105", "G1_geometry", [{"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 1.05}]),
    ("g1_tw090", "G1_geometry", [{"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.9}]),
    ("g1_tw080", "G1_geometry", [{"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.8}]),
    ("g1_allow020", "G1_geometry", [{"set": f"{P_LONG}.risk.we_excess_allowance_pct", "value": 0.2}]),
    # ---- G2: path-dependent stop / cut (unstuck) -----------------------------
    ("g2_la005", "G2_stop", [{"set": f"{P_LONG}.unstuck.loss_allowance_pct", "value": 0.05}]),
    ("g2_la010", "G2_stop", [{"set": f"{P_LONG}.unstuck.loss_allowance_pct", "value": 0.10}]),
    ("g2_dist020", "G2_stop", [{"set": f"{P_LONG}.unstuck.ema_dist", "value": -0.20}]),
    ("g2_thr025", "G2_stop", [{"set": f"{P_LONG}.unstuck.threshold", "value": 0.25}]),
    ("g2_cp010", "G2_stop", [{"set": f"{P_LONG}.unstuck.close_pct", "value": 0.10}]),
    ("g2_wide_la010_d020", "G2_stop", [
        {"set": f"{P_LONG}.unstuck.loss_allowance_pct", "value": 0.10},
        {"set": f"{P_LONG}.unstuck.ema_dist", "value": -0.20},
    ]),
    ("g2_wide_la010_d020_t025", "G2_stop", [
        {"set": f"{P_LONG}.unstuck.loss_allowance_pct", "value": 0.10},
        {"set": f"{P_LONG}.unstuck.ema_dist", "value": -0.20},
        {"set": f"{P_LONG}.unstuck.threshold", "value": 0.25},
    ]),
    ("g2_wide_la005_d010", "G2_stop", [
        {"set": f"{P_LONG}.unstuck.loss_allowance_pct", "value": 0.05},
        {"set": f"{P_LONG}.unstuck.ema_dist", "value": -0.10},
    ]),
    ("g2_wide_span050", "G2_stop", [
        {"set": f"{P_LONG}.unstuck.loss_allowance_pct", "value": 0.10},
        {"set": f"{P_LONG}.unstuck.ema_dist", "value": -0.20},
        {"set": f"{P_LONG}.unstuck.ema_span_0", "value": 360.0},
        {"set": f"{P_LONG}.unstuck.ema_span_1", "value": 720.0},
    ]),
    ("g2_wide_span200", "G2_stop", [
        {"set": f"{P_LONG}.unstuck.loss_allowance_pct", "value": 0.10},
        {"set": f"{P_LONG}.unstuck.ema_dist", "value": -0.20},
        {"set": f"{P_LONG}.unstuck.ema_span_0", "value": 2000.0},
        {"set": f"{P_LONG}.unstuck.ema_span_1", "value": 2880.0},
    ]),
    ("g2_force_la050_d050", "G2_stop", [
        {"set": f"{P_LONG}.unstuck.loss_allowance_pct", "value": 0.50},
        {"set": f"{P_LONG}.unstuck.ema_dist", "value": -0.50},
    ]),
    # ---- G3: scale-out / partial close ---------------------------------------
    ("g3_qty050", "G3_scaleout", [{"set": f"{TM}.close.qty_pct", "value": 0.50}]),
    ("g3_qty070", "G3_scaleout", [{"set": f"{TM}.close.qty_pct", "value": 0.70}]),
    ("g3_qty100", "G3_scaleout", [{"set": f"{TM}.close.qty_pct", "value": 1.0}]),
    ("g3_crw0005", "G3_scaleout", [{"set": f"{TM}.close.retracement_we_weight", "value": 0.0005}]),
    ("g3_crw001", "G3_scaleout", [{"set": f"{TM}.close.retracement_we_weight", "value": 0.001}]),
    ("g3_cth0020", "G3_scaleout", [{"set": f"{TM}.close.threshold_base_pct", "value": 0.002}]),
    ("g3_cth0000", "G3_scaleout", [{"set": f"{TM}.close.threshold_base_pct", "value": 0.0}]),
]

# ---- G4: daily-SMA entry-regime gate ---------------------------------------
# Each entry maps a cell id to a `backtest.entry_regime_gate` block. The gate is
# entry-only: it can block new positions and/or re-entries but never touches
# closes or auto-unstuck.
_GATE_CELLS: dict[str, dict[str, Any]] = {
    "g4_sma30_60": {
        "enabled": True,
        "sma_fast_days": 30,
        "sma_slow_days": 60,
    },
    "g4_sma30_60_initial_only": {
        "enabled": True,
        "sma_fast_days": 30,
        "sma_slow_days": 60,
        "block_initial": True,
        "block_reentry": False,
    },
    "g4_sma30_60_reentry_only": {
        "enabled": True,
        "sma_fast_days": 30,
        "sma_slow_days": 60,
        "block_initial": False,
        "block_reentry": True,
    },
    "g4_sma20_50": {
        "enabled": True,
        "sma_fast_days": 20,
        "sma_slow_days": 50,
    },
    "g4_sma10_30_confirm3": {
        "enabled": True,
        "sma_fast_days": 10,
        "sma_slow_days": 30,
        "confirm_days": 3,
    },
    "g4_sma5_20": {
        "enabled": True,
        "sma_fast_days": 5,
        "sma_slow_days": 20,
    },
    "g4_sma60_120": {
        "enabled": True,
        "sma_fast_days": 60,
        "sma_slow_days": 120,
    },
    # Sanity control: a 1/2-day cross is risk-on ~50% of days too, but with far
    # more transitions, which separates "gate is too restrictive" from
    # "gate plumbing is broken".
    "g4_sma1_2": {
        "enabled": True,
        "sma_fast_days": 1,
        "sma_slow_days": 2,
    },
}
for _gate_id in _GATE_CELLS:
    _RAW_CELLS.append((_gate_id, "G4_regime_gate", []))



# --------------------------------------------------------------------------- #
# G6: long/short flip (SMA fast below slow -> the short side may enter)
# --------------------------------------------------------------------------- #

# Per-cell long/short configuration. Each entry declares the sides that may trade, the
# hedge mode, the regime gate, and whether the short side consumes the *inverted* gate.
_LS_SPECS: dict[str, dict[str, Any]] = {}


def _ls_spec(
    cell_id: str,
    group: str,
    ops: list[dict[str, Any]],
    *,
    sides: tuple[str, ...],
    hedge_mode: bool,
    gate: dict[str, Any] | None,
    gate_mode: str,
    note: str = "",
) -> None:
    # The mode belongs to the gate block, because that is what
    # `_entry_regime_gate_config` reads and what a report cites.
    gate_block = dict(gate) if gate else None
    if gate_block is not None:
        gate_block["gate_mode"] = gate_mode
    _LS_SPECS[cell_id] = {
        "group": group,
        "ops": ops,
        "sides": sides,
        "hedge_mode": hedge_mode,
        "gate": gate_block,
        "gate_mode": gate_mode,
        "note": note,
    }


# The long side's geometry that these cells build on. `g5_tw090_ddf070` is the
# study's best 30%-capped cell and shares TWEL 0.9 with `best_dd_reducer`, so the
# only added variable is the short arm.
_LS_LONG_OPS = [
    {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.9},
    {"set": f"{TM}.entry.double_down_factor", "value": 0.7},
]
_GATE_20_50 = {"enabled": True, "sma_fast_days": 20, "sma_slow_days": 50}
_GATE_30_60 = {"enabled": True, "sma_fast_days": 30, "sma_slow_days": 60}

# Mirror of the long ladder in `g5_tw090_ddf070`: 60% re-entry scale, 3% spacing,
# 0.81% initial size. The short side's own seed values are placeholders and must not
# be used, or the test would report "shorting does not work" for an uncalibrated arm.
_MIRROR_SHORT = [
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.double_down_factor", "value": 0.7},
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.03},
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.initial_qty_pct", "value": 0.0081},
]
_AGGRESSIVE_SHORT = [
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.double_down_factor", "value": 0.94},
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.019},
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.initial_qty_pct", "value": 0.0081},
]
_CONSERVATIVE_SHORT = [
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.double_down_factor", "value": 0.6},
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.threshold_base_pct", "value": 0.04},
    {"set": f"{P_SHORT}.strategy.trailing_martingale.entry.initial_qty_pct", "value": 0.0081},
]


def _short_ops(twel: float, allow: float, geometry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"set": f"{P_SHORT}.risk.total_wallet_exposure_limit", "value": twel},
        {"set": f"{P_SHORT}.risk.we_excess_allowance_pct", "value": allow},
        *geometry,
    ]


def _declare_ls_cells() -> None:
    # ---- G6.0 probes: does the short side fire at all? ----------------------
    _ls_spec(
        "g6_probe_hedge",
        "G6.0_probe",
        _LS_LONG_OPS + _short_ops(0.9, 0.37, _MIRROR_SHORT),
        sides=("long", "short"),
        hedge_mode=True,
        gate=_GATE_20_50,
        gate_mode="invert_for_short",
        note="probe: both sides enabled, hedge mode, long gated on / short gated off",
    )
    _ls_spec(
        "g6_probe_oneway",
        "G6.0_probe",
        _LS_LONG_OPS + _short_ops(0.9, 0.37, _MIRROR_SHORT),
        sides=("long", "short"),
        hedge_mode=False,
        gate=_GATE_20_50,
        gate_mode="invert_for_short",
        note="probe: same as g6_probe_hedge but one-way mode",
    )

    # ---- confound controls: what is doing the work, the gate or the size? ---
    _ls_spec(
        "g6_ctrl_both_always",
        "G6.1_confound",
        _LS_LONG_OPS + _short_ops(0.9, 0.37, _MIRROR_SHORT),
        sides=("long", "short"),
        hedge_mode=True,
        gate=None,
        gate_mode="",
        note="control: both sides always eligible, no gate. Measures the gate's own effect",
    )
    _ls_spec(
        "g6_ctrl_short_only",
        "G6.1_confound",
        [{"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.0}]
        + _short_ops(0.9, 0.37, _MIRROR_SHORT),
        sides=("long", "short"),
        hedge_mode=True,
        gate=None,
        gate_mode="",
        note="control: short side alone, no gate. Upper bound on the short arm's own PnL",
    )

    # ---- gate geometry -----------------------------------------------------
    for tag, gate in (("20_50", _GATE_20_50), ("30_60", _GATE_30_60)):
        _ls_spec(
            f"g6_flip_{tag}",
            "G6.2_gate",
            _LS_LONG_OPS + _short_ops(0.9, 0.37, _MIRROR_SHORT),
            sides=("long", "short"),
            hedge_mode=True,
            gate=gate,
            gate_mode="invert_for_short",
            note="the proposal: long only when fast>slow, short only when fast<slow",
        )

    # ---- short-side exposure budget ----------------------------------------
    for tag, twel in (("tw060", 0.6), ("tw030", 0.3)):
        _ls_spec(
            f"g6_flip_20_50_{tag}",
            "G6.3_short_budget",
            _LS_LONG_OPS + _short_ops(twel, 0.37, _MIRROR_SHORT),
            sides=("long", "short"),
            hedge_mode=True,
            gate=_GATE_20_50,
            gate_mode="invert_for_short",
        )

    # ---- short-side ladder geometry ----------------------------------------
    _ls_spec(
        "g6_flip_20_50_aggr",
        "G6.4_short_geometry",
        _LS_LONG_OPS + _short_ops(0.9, 0.37, _AGGRESSIVE_SHORT),
        sides=("long", "short"),
        hedge_mode=True,
        gate=_GATE_20_50,
        gate_mode="invert_for_short",
    )
    _ls_spec(
        "g6_flip_20_50_cons",
        "G6.4_short_geometry",
        _LS_LONG_OPS + _short_ops(0.9, 0.37, _CONSERVATIVE_SHORT),
        sides=("long", "short"),
        hedge_mode=True,
        gate=_GATE_20_50,
        gate_mode="invert_for_short",
    )


_declare_ls_cells()



# G5 is appended only by ``write-contract --with-combos`` after G1-G4 results are
# read, so the combination phase is declared in one batch before any combo runs.
_COMBO_CELLS: list[tuple[str, str, list[dict[str, Any]]]] = [
    # ---- geometry x geometry: two independent drawdown reducers --------------
    ("g5_tw090_thr040", "G5_combo", [
        {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.9},
        {"set": f"{TM}.entry.threshold_base_pct", "value": 0.04},
    ]),
    ("g5_tw095_thr035", "G5_combo", [
        {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.95},
        {"set": f"{TM}.entry.threshold_base_pct", "value": 0.035},
    ]),
    ("g5_tw090_ddf070", "G5_combo", [
        {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.9},
        {"set": f"{TM}.entry.double_down_factor", "value": 0.7},
    ]),
    # ---- geometry x the zero close threshold return enhancer ----------------
    ("g5_tw095_cth000", "G5_combo", [
        {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.95},
        {"set": f"{TM}.close.threshold_base_pct", "value": 0.0},
    ]),
    ("g5_thr035_cth000", "G5_combo", [
        {"set": f"{TM}.entry.threshold_base_pct", "value": 0.035},
        {"set": f"{TM}.close.threshold_base_pct", "value": 0.0},
    ]),
    ("g5_ddf070_cth000", "G5_combo", [
        {"set": f"{TM}.entry.double_down_factor", "value": 0.7},
        {"set": f"{TM}.close.threshold_base_pct", "value": 0.0},
    ]),
    # ---- three-way draws on the Pareto frontier -----------------------------
    ("g5_tw095_ddf070_cth000", "G5_combo", [
        {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.95},
        {"set": f"{TM}.entry.double_down_factor", "value": 0.7},
        {"set": f"{TM}.close.threshold_base_pct", "value": 0.0},
    ]),
    ("g5_tw090_thr035_cth000", "G5_combo", [
        {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.9},
        {"set": f"{TM}.entry.threshold_base_pct", "value": 0.035},
        {"set": f"{TM}.close.threshold_base_pct", "value": 0.0},
    ]),
    # ---- gate combined with the best geometry -------------------------------
    # The gate is declared as a backtest block; `_COMBO_CELLS` entries carry it via
    # `_COMBO_GATES` below, keyed by cell id.
]

# Gate blocks for combination cells that also enable the regime gate.
_COMBO_GATES: dict[str, dict[str, Any]] = {
    "g5_gate_tw095_thr035": {"enabled": True, "sma_fast_days": 30, "sma_slow_days": 60},
    "g5_gate_tw090": {"enabled": True, "sma_fast_days": 30, "sma_slow_days": 60},
}
_COMBO_CELLS.append((
    "g5_gate_tw095_thr035",
    "G5_combo_gated",
    [
        {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.95},
        {"set": f"{TM}.entry.threshold_base_pct", "value": 0.035},
    ],
))
_COMBO_CELLS.append((
    "g5_gate_tw090",
    "G5_combo_gated",
    [{"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": 0.9}],
))
_GATE_CELLS.update(_COMBO_GATES)


def _cells() -> list[tuple[str, str, list[dict[str, Any]]]]:
    return _RAW_CELLS + _COMBO_CELLS


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


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


def load_json(path: Path) -> Any:
    with path.open() as handle:
        return json.load(handle)


def assert_offline_env() -> None:
    """The study is offline by construction.

    Every window is served by the frozen local v2 HLCV store; a cache miss fails
    loudly instead of fetching. Global ``socket`` monkeypatching is deliberately
    avoided because the asyncio event loop builds its self-pipe from
    ``socket.socket``.
    """
    return None


def frozen_universe() -> list[str]:
    dataset = load_json(BASELINE_RUN / "dataset.json")
    return sorted(set(dataset["coins"]))


def seed_config() -> tuple[Path, dict[str, Any]]:
    """The published lower-tail profile is the grid's seed.

    Its three behavioural parameters are already the closest known point to the
    <=30% drawdown goal, so grids are declared as patches against it. The frozen
    default baseline is kept as an explicit reference cell.
    """
    return PUBLISHED_PROFILE, load_json(PUBLISHED_PROFILE)


def seed_knobs() -> dict[str, Any]:
    cfg = load_json(PUBLISHED_PROFILE)
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    risk = cfg["bot"]["long"]["risk"]
    unstuck = cfg["bot"]["long"]["unstuck"]
    short_risk = cfg["bot"]["short"]["risk"]
    short_tm = cfg["bot"]["short"]["strategy"]["trailing_martingale"]
    return {
        f"{P_LONG}.risk.total_wallet_exposure_limit": risk["total_wallet_exposure_limit"],
        f"{P_LONG}.risk.we_excess_allowance_pct": risk["we_excess_allowance_pct"],
        f"{P_LONG}.risk.n_positions": risk["n_positions"],
        f"{P_LONG}.risk.entry_cooldown_minutes": risk["entry_cooldown_minutes"],
        f"{TM}.entry.double_down_factor": tm["entry"]["double_down_factor"],
        f"{TM}.entry.threshold_base_pct": tm["entry"]["threshold_base_pct"],
        f"{TM}.entry.retracement_we_weight": tm["entry"]["retracement_we_weight"],
        f"{TM}.entry.retracement_base_pct": tm["entry"]["retracement_base_pct"],
        f"{TM}.entry.ema_gate_mode": tm["entry"]["ema_gate_mode"],
        f"{TM}.close.qty_pct": tm["close"]["qty_pct"],
        f"{TM}.close.threshold_base_pct": tm["close"]["threshold_base_pct"],
        f"{TM}.close.retracement_base_pct": tm["close"]["retracement_base_pct"],
        f"{TM}.close.retracement_we_weight": tm["close"].get("retracement_we_weight", 0.0),
        f"{TM}.close.threshold_volatility_1m_weight": tm["close"]["threshold_volatility_1m_weight"],
        f"{TM}.close.threshold_volatility_1h_weight": tm["close"]["threshold_volatility_1h_weight"],
        f"{P_LONG}.unstuck.ema_dist": unstuck["ema_dist"],
        f"{P_LONG}.unstuck.ema_span_0": unstuck["ema_span_0"],
        f"{P_LONG}.unstuck.ema_span_1": unstuck["ema_span_1"],
        f"{P_LONG}.unstuck.threshold": unstuck["threshold"],
        f"{P_LONG}.unstuck.close_pct": unstuck["close_pct"],
        f"{P_LONG}.unstuck.loss_allowance_pct": unstuck["loss_allowance_pct"],
        # Short side. In the seed profile it is a disabled placeholder: the exposure
        # limit is 0, which is the only thing stopping it from trading.
        "live.hedge_mode": cfg["live"]["hedge_mode"],
        f"{P_SHORT}.risk.total_wallet_exposure_limit": short_risk[
            "total_wallet_exposure_limit"
        ],
        f"{P_SHORT}.risk.n_positions": short_risk["n_positions"],
        f"{P_SHORT}.risk.we_excess_allowance_pct": short_risk["we_excess_allowance_pct"],
        f"{TS}.entry.double_down_factor": short_tm["entry"]["double_down_factor"],
        f"{TS}.entry.threshold_base_pct": short_tm["entry"]["threshold_base_pct"],
        f"{TS}.entry.initial_qty_pct": short_tm["entry"]["initial_qty_pct"],
        f"{TS}.entry.initial_ema_dist": short_tm["entry"]["initial_ema_dist"],
        f"{TS}.entry.ema_gate_mode": short_tm["entry"]["ema_gate_mode"],
        f"{TS}.entry.retracement_we_weight": short_tm["entry"]["retracement_we_weight"],
        f"{TS}.entry.retracement_base_pct": short_tm["entry"]["retracement_base_pct"],
        f"{TS}.close.qty_pct": short_tm["close"]["qty_pct"],
        f"{TS}.close.threshold_base_pct": short_tm["close"]["threshold_base_pct"],
        f"{TS}.close.retracement_base_pct": short_tm["close"]["retracement_base_pct"],
        f"{TS}.close.threshold_we_weight": short_tm["close"]["threshold_we_weight"],
        f"{TS}.entry.threshold_we_weight": short_tm["entry"]["threshold_we_weight"],
        f"{TS}.entry.threshold_volatility_1h_weight": short_tm["entry"][
            "threshold_volatility_1h_weight"
        ],
        f"{TS}.entry.threshold_volatility_1m_weight": short_tm["entry"][
            "threshold_volatility_1m_weight"
        ],
        f"{TS}.entry.retracement_volatility_1h_weight": short_tm["entry"][
            "retracement_volatility_1h_weight"
        ],
        f"{TS}.entry.retracement_volatility_1m_weight": short_tm["entry"][
            "retracement_volatility_1m_weight"
        ],
        f"{TS}.entry.ema_span_0": short_tm["entry"]["ema_span_0"],
        f"{TS}.entry.ema_span_1": short_tm["entry"]["ema_span_1"],
        f"{TS}.close.threshold_volatility_1h_weight": short_tm["close"][
            "threshold_volatility_1h_weight"
        ],
        f"{TS}.close.threshold_volatility_1m_weight": short_tm["close"][
            "threshold_volatility_1m_weight"
        ],
        f"{TS}.close.retracement_volatility_1h_weight": short_tm["close"][
            "retracement_volatility_1h_weight"
        ],
        f"{TS}.close.retracement_volatility_1m_weight": short_tm["close"][
            "retracement_volatility_1m_weight"
        ],
    }


def describe_cell(cell_id: str, group: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    knobs = seed_knobs()
    resolved = []
    for op in ops:
        path = op.get("set") or op["del"]
        if path not in knobs:
            raise KeyError(f"lever path not present in seed knobs: {path}")
        resolved.append(
            {
                "path": path,
                "seed": knobs[path],
                "value": op.get("value"),
                "op": "set" if "set" in op else "del",
            }
        )
    resolved.sort(key=lambda item: item["path"])
    described: dict[str, Any] = {"cell_id": cell_id, "group": group, "ops": resolved}
    ls = _LS_SPECS.get(cell_id)
    if ls is not None:
        described["sides"] = list(ls["sides"])
        described["hedge_mode"] = ls["hedge_mode"]
        described["gate_mode"] = ls["gate_mode"]
        # The cell's ops must be exactly what `_LS_SPECS` declares, or the declared
        # sides/gate would describe a different run than the one that executes.
        if ops != ls["ops"]:
            raise ValueError(
                f"long/short cell {cell_id!r} ops do not match its _LS_SPECS declaration"
            )
    gate = _GATE_CELLS.get(cell_id)
    if ls is not None:
        gate = ls["gate"]
    if gate is not None:
        described["entry_regime_gate"] = gate
    return described


# --------------------------------------------------------------------------- #
# artifact bundles: the input `annual_analysis.md` is rendered from
# --------------------------------------------------------------------------- #

ARTIFACTS = STUDY / "artifacts"
HASH_AMENDMENT = (
    "per-cell `config_sha256` added. It is a derived field describing the config "
    "`run` executed and `bundle` emits; no cell, gate, window, universe or "
    "execution/cost setting was edited."
)
# Files the report renderer reads. Kept to the analytical set: `--disable_plotting all`
# skips the figure tail, whose memory peak is what gets OOM-killed on a small host.
ANALYTICAL_BUNDLE_ARTIFACTS = (
    "analysis.json",
    "config.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
    "dataset.json",
)
# The verifier additionally requires the streamed execution-audit CSV and the figure
# set. They are written after the analytical artifacts, so a killed plotting tail can
# leave them missing while the analysis is complete; the record says which happened.
REQUIRED_BUNDLE_ARTIFACTS = ANALYTICAL_BUNDLE_ARTIFACTS + (
    "execution_audit.csv",
    "balance_and_equity.png",
    "balance_and_equity_logy.png",
    "total_wallet_exposure.png",
    "pnl_cumsum.png",
)
# Cells that get a bundle. Each answers a different question: `best_dd_reducer` is the
# only long-only artifact that exercises `backtest.entry_regime_gate`, and
# `long_short_flip` is the long/short counterpart of the `best_dd_reducer` cell's long
# arm, so the two reports are directly comparable.
BUNDLE_CELLS: dict[str, str] = {
    "seed": "published",
    "best_capped": "g5_tw090_ddf070",
    "best_dd_reducer": "g4_sma20_50",
    "long_short_flip": "g6_bud_g20_50_tw025",
}


def cell_spec(cell_id: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Return (cell_id, group, ops) for a declared cell id."""
    spec = _spec_index()
    if cell_id not in spec:
        raise SystemExit(f"unknown cell {cell_id!r}")
    return spec[cell_id]


def cell_config(
    cell_id: str,
    window: str,
    scenario: str,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the exact config a cell ran with, optionally redirected to a bundle."""
    cid, group, ops = cell_spec(cell_id)
    described = describe_cell(cid, group, ops)
    ls = _LS_SPECS.get(cell_id)
    cfg = build_config(
        window,
        scenario,
        described["ops"],
        described.get("entry_regime_gate"),
        tuple(ls["sides"]) if ls else ("long",),
    )
    if ls is not None:
        cfg["live"]["hedge_mode"] = bool(ls["hedge_mode"])
    if base_dir is not None:
        cfg["backtest"]["base_dir"] = str(base_dir)
    return cfg


def cell_config_sha256(cell_id: str, window: str, scenario: str) -> str:
    """Stable hash of a cell's config, registered in the research contract."""
    return stable_hash(cell_config(cell_id, window, scenario))


def bundle_dir(subdir: str) -> Path:
    return ARTIFACTS / subdir


def bundle_results_base(subdir: str) -> Path:
    return bundle_dir(subdir) / "backtest_results"


def run_dirs_under(results_base: Path) -> list[Path]:
    """Run directories under a bundle, at the `binance` or `binance_<label>` level.

    The backtest writes `<base_dir>/<exchange>[_label]/<UTC timestamp>/`, so the
    exchange level may carry a suffix. Globbing for timestamp-named directories
    avoids hardcoding either name.
    """
    root = Path(results_base)
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/binance*/*") if p.is_dir() and p.name[:2].isdigit())


def find_run_dir(subdir: str, explicit: str | None = None, label: str | None = None) -> Path:
    """Locate the single run directory under a bundle, refusing to guess.

    `label` is accepted for callers that keep several labels in one bundle; it is not
    applied by `run_bundle`, because that already encodes the label in `base_dir` and
    scoping twice would look under `binance_<label>/binance_<label>`.
    """
    if explicit:
        return Path(explicit).resolve()
    base = bundle_results_base(subdir)
    dirs = run_dirs_under(base / f"binance_{label}" if label else base)
    if not dirs:
        raise SystemExit(f"no run directory under {bundle_results_base(subdir)}")
    if len(dirs) != 1:
        raise SystemExit(
            f"expected exactly one run directory under {bundle_results_base(subdir)}, "
            f"found {len(dirs)}: {[str(path) for path in dirs]}"
        )
    return dirs[0]


def missing_bundle_artifacts(run_dir: Path) -> list[str]:
    return [name for name in REQUIRED_BUNDLE_ARTIFACTS if not (run_dir / name).exists()]


def rust_identity() -> dict[str, Any]:
    import rust_utils

    return {
        "expected_source_fingerprint": rust_utils.source_fingerprint(),
        "compiled_path": str(rust_utils.preferred_compiled_path() or ""),
    }


def stamp_rust_extension() -> str:
    """Align the compiled extension with the current sources before a bundle run."""
    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))
    from rust_utils import (  # noqa: PLC0415
        source_fingerprint,
        stamp_compiled_extensions,
        sync_installed_extension_into_src,
    )

    sync_installed_extension_into_src()
    fingerprint = source_fingerprint()
    stamp_compiled_extensions(fingerprint)
    return fingerprint


def cell_metrics_for(cell_id: str, window: str, scenario: str) -> dict[str, Any] | None:
    """Metrics of the screening cell, for the alignment check. None when absent."""
    path = RUNS / window / scenario / cell_id / "result.json"
    if not path.exists():
        return None
    return load_json(path).get("metrics")


def alignment_report(
    run_dir: Path, cell_id: str, window: str, scenario: str
) -> dict[str, Any]:
    """Compare a bundle's analysis against the cell's screening metrics.

    The bundle is the same window, cost regime and universe as the cell, so the
    minute-close metrics must agree. A disagreement is recorded rather than hidden,
    because the report cites the bundle's own numbers.
    """
    analysis = load_json(run_dir / "analysis.json")
    cell = cell_metrics_for(cell_id, window, scenario)
    if cell is None:
        return {"cell_result_found": False, "checked": False}
    # (analysis.json key, screening metrics key, tolerance). The two dicts name the
    # same quantities differently, so map them explicitly instead of guessing.
    fields = [
        ("drawdown_worst_strategy_eq", "minute_close_mdd", 1e-6),
        ("gain_strategy_eq", "gain_strategy_eq", 1e-6),
        ("fills_count", "fills", 0.0),
        ("n_days", "analysis_n_days", 1e-6),
    ]
    if analysis.get("fills_count_short"):
        # A long/short cell must also agree on the short arm, which is the variable
        # under test; comparing only the total could hide a side mix-up.
        fields.append(("fills_count_short", "fills_short", 0.0))
    compared: dict[str, Any] = {}
    mismatches: list[str] = []
    for key, cell_key, tolerance in fields:
        bundle_value = analysis.get(key)
        cell_value = cell.get(cell_key)
        if bundle_value is None or cell_value is None:
            compared[key] = {"bundle": bundle_value, "cell": cell_value, "abs_delta": None}
            continue
        delta = abs(float(bundle_value) - float(cell_value))
        compared[key] = {
            "analysis_key": key,
            "cell_key": cell_key,
            "bundle": bundle_value,
            "cell": cell_value,
            "abs_delta": delta,
        }
        if delta > tolerance:
            mismatches.append(key)
    return {
        "cell_result_found": True,
        "checked": True,
        "cell_id": cell_id,
        "window": window,
        "scenario": scenario,
        "compared": compared,
        "mismatches": mismatches,
        "aligned": not mismatches,
        "note": (
            "bundle minute-close metrics must equal the screening cell's because the "
            "bundle runs the same window, cost regime and universe"
        ),
    }


def emit_config_command(args: argparse.Namespace) -> None:
    cfg = cell_config(
        args.cell,
        args.window,
        args.scenario,
        Path(args.base_dir) if args.base_dir else None,
    )
    out = Path(args.out)
    write_json(out, cfg)
    print(f"wrote {out}")
    print(f"cell={args.cell} window={args.window} scenario={args.scenario}")
    print(f"cell_config_sha256={stable_hash(cell_config(args.cell, args.window, args.scenario))}")
    gate = cfg.get("backtest", {}).get("entry_regime_gate")
    print(f"entry_regime_gate={gate if gate else 'disabled'}")


def run_bundle(
    subdir: str,
    cell_id: str,
    window: str,
    scenario: str,
    label: str,
    force: bool,
) -> dict[str, Any]:
    """Produce one artifact bundle and render its report."""
    art = bundle_dir(subdir)
    results_base = bundle_results_base(subdir)
    art.mkdir(parents=True, exist_ok=True)
    results_base.mkdir(parents=True, exist_ok=True)
    emitted_config = art / "emitted.config.json"
    run_record_path = art / "run_record.json"
    audit_path = art / "execution_audit.csv"
    run_log = art / "backtest_run.log"

    if run_record_path.exists() and not force:
        record = load_json(run_record_path)
        if record.get("artifact_status") == "complete" and record.get("render_exit_code") == 0:
            print(f"{subdir}: already complete (use --force to re-run)")
            return record

    fingerprint = stamp_rust_extension()
    # The backtest opens the execution-audit CSV with create-new semantics, so a
    # re-run into an existing bundle fails with EEXIST instead of overwriting.
    audit_path.unlink(missing_ok=True)
    # A bundle holds exactly one run directory *per label*: the backtest names it from
    # the completion timestamp, so a forced re-run must clear the previous one rather
    # than leave two behind for `find_run_dir` to refuse. The clean-up is scoped to this
    # label's tree, because clearing the whole bundle would delete a sibling cell's run.
    label_base = results_base / f"binance_{label}"
    if force and label_base.exists():
        for stale in sorted(label_base.rglob("*"), reverse=True):
            if stale.is_file() or stale.is_symlink():
                stale.unlink()
            elif stale.is_dir():
                stale.rmdir()

    emitted = cell_config(cell_id, window, scenario, results_base / f"binance_{label}")
    # The verifier reads the streamed audit to cross-check fill provenance.
    emitted["backtest"]["execution_audit_path"] = str(audit_path)
    # The exported balance/equity series must be minute-resolution: the report
    # convention and the verifier both recompute every per-period figure from it, and
    # the seed profile's own divider is 60. This only sets the resolution of the
    # exported series, not anything the engine computed.
    emitted["backtest"]["balance_sample_divider"] = 1
    write_json(emitted_config, emitted)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "src"), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(REPO / "src")]
    )
    # The verifier requires the summary figures, which the analytical artifacts precede.
    # The per-coin fill panels are the memory peak and get killed on a small host, so
    # they are skipped; `DISABLE_PLOTTING` overrides this, and an empty value attempts
    # the full figure set.
    disable_plotting = os.environ.get("DISABLE_PLOTTING", "coin_fills")
    cmd = [sys.executable, "-m", "backtest", str(emitted_config)]
    if disable_plotting:
        cmd += ["--disable_plotting", disable_plotting]
    started = time.time()
    with run_log.open("w") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    elapsed = time.time() - started

    # `base_dir` already ends in `binance_<label>`, so discovery must not add the label
    # again; the one-run-per-bundle invariant holds because each bundle owns one label.
    run_dir = find_run_dir(subdir)
    # Mirror the streamed audit into the run directory so the run describes itself and
    # the verifier, which resolves the audit under the bundle, reads the same rows.
    if audit_path.exists():
        shutil.copyfile(audit_path, run_dir / "execution_audit.csv")
    else:
        print(f"warning: {subdir}: no execution audit at {audit_path}")
    missing_analysis = [
        name for name in ANALYTICAL_BUNDLE_ARTIFACTS if not (run_dir / name).exists()
    ]
    if missing_analysis:
        raise SystemExit(
            f"{subdir}: backtest exit {proc.returncode} and the analytical artifacts are "
            f"incomplete: missing {missing_analysis}. See {run_log}"
        )
    missing = missing_bundle_artifacts(run_dir)
    if proc.returncode == 0 and missing:
        artifact_status = "complete_missing_declared_artifacts"
    elif proc.returncode != 0:
        artifact_status = "partial_plotting_tail"
    else:
        artifact_status = "complete"
    record: dict[str, Any] = {
        "bundle": subdir,
        "cell_id": cell_id,
        "label": label,
        "window": {
            "name": window,
            "start_date": WINDOWS[window][0],
            "end_date": WINDOWS[window][1],
        },
        "scenario": scenario,
        "execution": EXECUTION[SCENARIOS[scenario][0]],
        "costs": COSTS[SCENARIOS[scenario][1]],
        "emitted_config": str(emitted_config.relative_to(REPO)),
        "emitted_config_sha256": sha256_file(emitted_config),
        "cell_config_sha256": cell_config_sha256(cell_id, window, scenario),
        "run_dir": str(run_dir.relative_to(REPO)),
        "backtest_exit_code": proc.returncode,
        "artifact_status": artifact_status,
        "required_artifacts": list(REQUIRED_BUNDLE_ARTIFACTS),
        "missing_artifacts": missing,
        "elapsed_s": elapsed,
        "rust_identity": {**rust_identity(), "stamped_source_fingerprint": fingerprint},
        "universe": {"exchange": "binance", "coins": sorted(frozen_universe())},
        "alignment": alignment_report(run_dir, cell_id, window, scenario),
        "safety": {
            "network_downloads": False,
            "credentials": False,
            "exchange_account_or_orders": False,
            "bot_start": False,
        },
        "candidate_lock": None,
        "candidate_lock_note": (
            "this study performs no candidate lock and opens no holdout window; each bundle "
            "reproduces a screening cell under the frozen research contract"
        ),
        "plotting": (
            f"summary figures enabled, per-coin panels disabled (--disable_plotting "
            f"{os.environ.get('DISABLE_PLOTTING', 'coin_fills') or '<none>'}); analytical "
            f"artifacts are written before the figure tail"
        ),
    }
    write_json(run_record_path, record)

    renderer = Path(__file__).resolve().parent / "render_annual_report.py"
    render = subprocess.run(
        [sys.executable, str(renderer), "--bundle", subdir],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
    )
    record["render_exit_code"] = render.returncode
    record["render_stdout"] = render.stdout.strip().splitlines()[-6:]
    record["render_stderr"] = render.stderr.strip().splitlines()[-6:]
    write_json(run_record_path, record)

    alignment = record["alignment"]
    print(
        "%-16s %-22s exit=%-4s status=%-22s aligned=%-5s %5.1fs  %s"
        % (
            subdir,
            cell_id,
            record["backtest_exit_code"],
            artifact_status,
            alignment.get("aligned"),
            elapsed,
            run_dir.name,
        )
    )
    if render.returncode != 0:
        print(f"  render FAILED (exit {render.returncode}):")
        for line in record["render_stderr"]:
            print(f"    {line}")
    else:
        print(f"  report: {run_dir.relative_to(REPO)}/annual_analysis.md")
    return record


def bundle_command(args: argparse.Namespace) -> None:
    cells = args.cells
    if cells == ["all"]:
        cells = list(BUNDLE_CELLS)
    for subdir in cells:
        if subdir not in BUNDLE_CELLS:
            raise SystemExit(
                f"unknown bundle {subdir!r}; declared bundles: {sorted(BUNDLE_CELLS)}"
            )
        run_bundle(
            subdir,
            BUNDLE_CELLS[subdir],
            args.window,
            args.scenario,
            subdir,
            args.force,
        )


def contract_payload(with_combos: bool = False) -> dict[str, Any]:
    seed_path, _seed_cfg = seed_config()
    cells = [describe_cell(cid, grp, ops) for cid, grp, ops in _cells()]
    for cell in cells:
        # A report cites this hash instead of restating parameters by hand. It is the
        # config `run` executes for the cell and the config `bundle` emits.
        cell["config_sha256"] = cell_config_sha256(
            cell["cell_id"], "full", PRIMARY_SCENARIO
        )
    return {
        "version": 1,
        "purpose": (
            "return-preserving drawdown reduction for the default trailing-martingale "
            "profile: can the three-year return multiple be kept while the worst "
            "mark-to-market drawdown is capped near 30%?"
        ),
        "amendments": (
            [
                HASH_AMENDMENT,
            ]
            if not with_combos
            else [
                "combos: G5 cells appended in one batch after G1-G3 results were read, "
                "before any combination cell ran. Gates, windows, the 40-coin universe "
                "and the execution/cost regime were never edited.",
                HASH_AMENDMENT,
            ]
        ),
        "safety": {
            "network": False,
            "credentials": False,
            "exchange_account_or_orders": False,
            "bot_start": False,
        },
        "seed": {
            "config_path": str(seed_path.relative_to(REPO)),
            "config_sha256": sha256_file(seed_path),
            "role": (
                "published lower-tail profile; grids are patches against it because it is "
                "the closest known geometry to the <=30% drawdown goal"
            ),
        },
        "reference": {
            "run_dir": str(BASELINE_RUN.relative_to(REPO)),
            "config_path": str(BASELINE_CONFIG.relative_to(REPO)),
            "config_sha256": sha256_file(BASELINE_CONFIG),
            "drawdown_worst_strategy_eq": load_json(BASELINE_ANALYSIS)[
                "drawdown_worst_strategy_eq"
            ],
            "gain_strategy_eq": load_json(BASELINE_ANALYSIS)["gain_strategy_eq"],
        },
        "windows": WINDOWS,
        "locked_windows": list(LOCKED_WINDOWS),
        "execution_scenarios": EXECUTION,
        "cost_scenarios": COSTS,
        "scenario_matrix": SCENARIOS,
        "half_year_edges": HALF_YEAR_EDGES,
        "half_year_edges_stress": HALF_YEAR_EDGES_STRESS,
        "universe": frozen_universe(),
        "universe_policy": (
            "same 40-coin frozen basket for every window and cell; new listings join the "
            "tradable set only after their first candle, mirroring the baseline dataset"
        ),
        "primary_scenario": PRIMARY_SCENARIO,
        "extreme_scenario": EXTREME_SCENARIO,
        "gates": GATES,
        "cells": cells,
        "cell_matrix_sha256": stable_hash(cells),
        "sequential_protocol": (
            "fit and full windows may be run freely; holdout, fresh and stress may only be "
            "run after holdout_candidate_lock.json exists. The lock records the selected "
            "cells and the artifact hashes it was derived from, and the lock mtime must "
            "precede every locked-window cell mtime."
        ),
        "excluded_by_design": [
            "MFE giveback re-test: implemented natively as close.retracement_base_pct and "
            "already rejected by maxdd_strategy_research_2026-09-14 "
            "(mfe_enabled_for_final_training=false)",
            "account-level mark-to-market brake: implemented and measured worse than a static "
            "exposure cap in dd_tail_research_2026-09-15 (Phase B)",
            "Deribit put tail hedge: no local option-chain history",
            "short-side hedging: prior studies measured -8.4%..-9.1% CAGR",
        ],
    }


def build_config(
    window: str,
    scenario: str,
    ops: list[dict[str, Any]],
    entry_regime_gate: dict[str, Any] | None = None,
    sides: tuple[str, ...] = ("long",),
) -> dict[str, Any]:
    from copy import deepcopy

    execution, costs = SCENARIOS[scenario]
    cfg = deepcopy(load_json(PUBLISHED_PROFILE))
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
    # `sides` is how a long/short cell lets the short side trade at all: the seed
    # profile approves no short coins, so the side stays inert regardless of its
    # exposure limit. Default stays long-only so existing cells are unchanged.
    cfg["live"]["approved_coins"] = {
        "long": coins if "long" in sides else [],
        "short": coins if "short" in sides else [],
    }
    if entry_regime_gate:
        gate_block = dict(entry_regime_gate)
        # `gate_mode` and the approved sides are inputs to the gate, not caller knobs,
        # so they are filled in here where the resolved config is known.
        gate_block.setdefault("gate_mode", "both")
        gate_block["approved"] = {
            "long": list(cfg["live"]["approved_coins"].get("long") or []),
            "short": list(cfg["live"]["approved_coins"].get("short") or []),
        }
        cfg["backtest"]["entry_regime_gate"] = gate_block
    for op in ops:
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
    f"{year:04d}-{month:02d}-01" for year in range(2021, 2028) for month in (1, 7)
) + ("2026-09-11",)


def _bucket_returns(
    series: np.ndarray, stamps: np.ndarray, edges: list[str]
) -> list[dict[str, Any]]:
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


def compute_metrics(
    analysis: dict[str, Any], equities, fills, half_year_edges: tuple[str, ...]
) -> dict[str, Any]:
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
    # Worst 1% mean drawdown: mean of the deepest 1% of minute-close underwater samples.
    depth = np.sort(1.0 - underwater)[::-1]
    worst_1pct_mean = float(np.mean(depth[: max(1, len(depth) // 100)]))
    half_years = []
    edges = [np.datetime64(edge) for edge in half_year_edges]
    stamps = ts.astype("datetime64[ms]")
    for idx in range(len(half_year_edges) - 1):
        lo = int(np.searchsorted(stamps, edges[idx], side="left"))
        hi = int(np.searchsorted(stamps, edges[idx + 1], side="left"))
        if hi - lo < 2:
            continue
        half_years.append(
            {
                "start": half_year_edges[idx],
                "end": half_year_edges[idx + 1],
                "return": float(series[hi - 1] / series[lo] - 1.0),
            }
        )
    out: dict[str, Any] = {
        "drawdown_worst_strategy_eq": float(analysis.get("drawdown_worst_strategy_eq", mdd)),
        "minute_close_mdd": mdd,
        "worst_1pct_mean_drawdown": worst_1pct_mean,
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
        "high_exposure_days_max_long": analysis.get("high_exposure_days_max_long"),
        "total_wallet_exposure_max": analysis.get("total_wallet_exposure_max"),
        "total_wallet_exposure_mean": analysis.get("total_wallet_exposure_mean"),
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
        "halfyear_count": len(half_years),
    }
    for src, dst in (
        ("fills_count", "fills"),
        ("fills_count_entry", "entry_fills"),
        ("fills_count_close", "close_fills"),
        ("fills_count_long", "fills_long"),
        ("fills_count_short", "fills_short"),
        ("fills_count_long_close", "close_fills_long"),
        ("fills_count_short_close", "close_fills_short"),
        # Per-side economics. Without these a long/short cell shows only a total, and the
        # short arm's contribution cannot be separated from the long arm's.
        ("pnl_long", "pnl_long"),
        ("pnl_short", "pnl_short"),
        ("exposure_ratio_long_usd", "exposure_ratio_long"),
        ("exposure_ratio_short_usd", "exposure_ratio_short"),
        ("exposure_mean_ratio_long_usd", "exposure_mean_ratio_long"),
        ("exposure_mean_ratio_short_usd", "exposure_mean_ratio_short"),
        ("total_wallet_exposure_max", "total_wallet_exposure_max_analysis"),
        ("fills_active_symbols_count", "traded_coin_count"),
        ("fills_top_symbol_share", "top_symbol_share"),
        ("fills_active_days_count", "active_days"),
        ("fills_active_days_ratio", "active_days_ratio"),
    ):
        val = analysis.get(src)
        if val is not None:
            out[dst] = float(val)
    if fills is not None:
        fills_arr = np.asarray(fills)
        if fills_arr.ndim == 2 and fills_arr.size:
            out["fill_rows"] = int(fills_arr.shape[0])
    return out


def _resolve_long_strategy_config(ops: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve one cell's long `trailing_martingale` subtree from the seed plus its ops.

    Kept local to the mirror so that `build_config` (which also resolves the universe
    and the gate) does not depend on the long/short declarations, and they do not depend
    on it.
    """
    from copy import deepcopy

    with PUBLISHED_PROFILE.open() as handle:
        cfg = json.load(handle)
    for op in ops:
        path = op.get("set") or op.get("del")
        parts = path.split(".")
        node = cfg
        for part in parts[:-1]:
            node = node[part]
        if "set" in op:
            node[parts[-1]] = op["value"]
        else:
            node.pop(parts[-1], None)
    return cfg["bot"]["long"]["strategy"]["trailing_martingale"]


def _declare_budgeted_ls_cells() -> None:
    """Long/short cells that fit both arms inside one exposure budget.

    `total_wallet_exposure_limit` is per side, so the naive flip (0.9 long + 0.9 short)
    is 1.8x gross on one balance and liquidates after ~439 days. These cells keep the long
    arm at the study's best 30%-capped setting and give the short arm a share of the same
    budget, so total gross exposure stays inside the single-side contract the strategy
    was calibrated for.
    """
    long_twel = 0.9
    budgets = (("tw035", 0.35), ("tw025", 0.25), ("tw015", 0.15), ("tw010", 0.10))
    for gate_tag, gate in (("g20_50", _GATE_20_50), ("g30_60", _GATE_30_60)):
        for tag, short_twel in budgets:
            _ls_spec(
                f"g6_bud_{gate_tag}_{tag}",
                "G6.5_budgeted_flip",
                [
                    {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": long_twel},
                    {"set": f"{TM}.entry.double_down_factor", "value": 0.7},
                    {
                        "set": f"{P_SHORT}.risk.total_wallet_exposure_limit",
                        "value": short_twel,
                    },
                ],
                sides=("long", "short"),
                hedge_mode=True,
                gate=gate,
                gate_mode="invert_for_short",
                note=(
                    "long arm at the study's best 30%-capped setting; the short arm gets a "
                    "share of the same budget so gross exposure stays inside it"
                ),
            )
    # One-way mode forbids holding both sides of the same coin, which is the strictest
    # form of the flip, so it gets one comparison cell at a moderate short budget.
    _ls_spec(
        "g6_bud_oneway_g20_50_tw025",
        "G6.5_budgeted_flip",
        [
            {"set": f"{P_LONG}.risk.total_wallet_exposure_limit", "value": long_twel},
            {"set": f"{TM}.entry.double_down_factor", "value": 0.7},
            {"set": f"{P_SHORT}.risk.total_wallet_exposure_limit", "value": 0.25},
        ],
        sides=("long", "short"),
        hedge_mode=False,
        gate=_GATE_20_50,
        gate_mode="invert_for_short",
        note="one-way mode: the strictest flip, no simultaneous long and short",
    )


_declare_budgeted_ls_cells()


def _mirror_short_from_long() -> None:
    """Give every long/short cell a short ladder derived from its own long ladder.

    The seed profile's short side is an uncalibrated placeholder, so copying it would
    measure the placeholder rather than the idea. Every behavioral path the arms can
    share is copied from the long cell under test; only two cannot be copied verbatim:

    - `initial_ema_dist`: a short's initial order sits above the EMA band rather than
      below it, so the magnitude mirrors the long's and only the sign differs.
    - `initial_qty_pct`: it scales by the *nominal* exposure limit, and the short arm
      runs a different limit, so it is rescaled to make the first entry the same share
      of balance on both arms.
    """
    with PUBLISHED_PROFILE.open() as handle:
        seed_cfg = json.load(handle)
    long_risk = seed_cfg["bot"]["long"]["risk"]
    long_allow = long_risk["we_excess_allowance_pct"]

    for spec in _LS_SPECS.values():
        # The declarations are raw `{"set": path, "value": ...}` ops; the described form
        # used for execution carries `"path"` instead.
        raw_ops = spec["ops"]
        ops = {op.get("set") or op["del"]: op.get("value") for op in raw_ops}
        long_twel = ops.get(f"{P_LONG}.risk.total_wallet_exposure_limit")
        short_twel = ops.get(f"{P_SHORT}.risk.total_wallet_exposure_limit")
        if long_twel is None or not short_twel:
            continue  # no long geometry, or no short arm to mirror onto

        long_tm = _resolve_long_strategy_config(spec["ops"])
        long_eff = float(long_twel) * (1.0 + long_allow)
        short_eff = float(short_twel) * (1.0 + long_allow)

        mirror: dict[str, Any] = {
            f"{P_SHORT}.risk.we_excess_allowance_pct": long_allow,
            f"{TS}.entry.double_down_factor": long_tm["entry"]["double_down_factor"],
            f"{TS}.entry.threshold_base_pct": long_tm["entry"]["threshold_base_pct"],
            f"{TS}.entry.threshold_we_weight": long_tm["entry"]["threshold_we_weight"],
            f"{TS}.entry.threshold_volatility_1h_weight": long_tm["entry"][
                "threshold_volatility_1h_weight"
            ],
            f"{TS}.entry.threshold_volatility_1m_weight": long_tm["entry"][
                "threshold_volatility_1m_weight"
            ],
            f"{TS}.entry.retracement_base_pct": long_tm["entry"]["retracement_base_pct"],
            f"{TS}.entry.retracement_we_weight": long_tm["entry"]["retracement_we_weight"],
            f"{TS}.entry.retracement_volatility_1h_weight": long_tm["entry"][
                "retracement_volatility_1h_weight"
            ],
            f"{TS}.entry.retracement_volatility_1m_weight": long_tm["entry"][
                "retracement_volatility_1m_weight"
            ],
            f"{TS}.entry.ema_gate_mode": long_tm["entry"]["ema_gate_mode"],
            # The band spans are part of the entry geometry, not a side-specific
            # convention: with `ema_gate_mode = all` they cap/floor every entry price.
            f"{TS}.entry.ema_span_0": long_tm["entry"]["ema_span_0"],
            f"{TS}.entry.ema_span_1": long_tm["entry"]["ema_span_1"],
            f"{TS}.entry.initial_ema_dist": abs(long_tm["entry"]["initial_ema_dist"]),
            f"{TS}.entry.initial_qty_pct": (
                long_eff * long_tm["entry"]["initial_qty_pct"] / short_eff
            ),
            f"{TS}.close.qty_pct": long_tm["close"]["qty_pct"],
            f"{TS}.close.threshold_base_pct": long_tm["close"]["threshold_base_pct"],
            f"{TS}.close.threshold_we_weight": long_tm["close"]["threshold_we_weight"],
            f"{TS}.close.threshold_volatility_1h_weight": long_tm["close"][
                "threshold_volatility_1h_weight"
            ],
            f"{TS}.close.threshold_volatility_1m_weight": long_tm["close"][
                "threshold_volatility_1m_weight"
            ],
            f"{TS}.close.retracement_base_pct": long_tm["close"]["retracement_base_pct"],
            f"{TS}.close.retracement_volatility_1h_weight": long_tm["close"][
                "retracement_volatility_1h_weight"
            ],
            f"{TS}.close.retracement_volatility_1m_weight": long_tm["close"][
                "retracement_volatility_1m_weight"
            ],
        }
        # A cell may deliberately pin a mirrored path; such an override wins and is
        # recorded so the deviation stays visible instead of silently disappearing.
        overrides = {
            path: value
            for path, value in spec.get("short_overrides", {}).items()
            if path in mirror
        }
        preserved = [op for op in raw_ops if (op.get("set") or op["del"]) not in mirror]
        merged = {**mirror, **overrides}
        spec["ops"] = preserved + [
            {"set": path, "value": value} for path, value in sorted(merged.items())
        ]
        spec["mirrored_short_ladder"] = {
            "source": f"this cell's own {P_LONG} arm",
            "long_effective_exposure_limit": long_eff,
            "short_effective_exposure_limit": short_eff,
            "short_initial_qty_pct": (
                long_eff * long_tm["entry"]["initial_qty_pct"] / short_eff
            ),
            "deliberate_short_overrides": sorted(overrides),
            "note": (
                "the short initial_qty_pct is rescaled so the first entry is the same "
                "share of balance on both arms; the initial EMA distance is sign-flipped"
            ),
        }


_mirror_short_from_long()

# The cells were declared with placeholder short ops, so their registrations are rebuilt
# from the mirrored specs.
_RAW_CELLS = [cell for cell in _RAW_CELLS if cell[0] not in _LS_SPECS]
for _ls_id in _LS_SPECS:
    _RAW_CELLS.append((_ls_id, _LS_SPECS[_ls_id]["group"], _LS_SPECS[_ls_id]["ops"]))

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
    cfg = build_config(
        window,
        scenario,
        ops,
        payload.get("entry_regime_gate"),
        tuple(payload.get("sides") or ("long",)),
    )
    if payload.get("hedge_mode") is not None:
        cfg["live"]["hedge_mode"] = bool(payload["hedge_mode"])
    started = time.time()
    coins, hlcvs, mss, btc_usd_prices, timestamps = asyncio.run(prepare_window(cfg))
    fills, equities, analysis = backtest.run_backtest(
        hlcvs, mss, cfg, "binance", btc_usd_prices, timestamps
    )
    edges = HALF_YEAR_EDGES_STRESS if window == "stress" else HALF_YEAR_EDGES
    metrics = compute_metrics(analysis, equities, fills, edges)
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


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def _spec_index() -> dict[str, tuple[str, str, list[dict[str, Any]]]]:
    spec = {cid: (cid, grp, ops) for cid, grp, ops in _cells()}
    spec["reference"] = ("reference", "G0_reference", [])
    return spec


def run_command(args: argparse.Namespace) -> None:
    selected = args.cells
    spec = _spec_index()
    if selected == ["all"]:
        selected = ["reference"] + [cid for cid, _g, _o in _cells()]
    for window in args.windows:
        if window in LOCKED_WINDOWS and not (STUDY / "holdout_candidate_lock.json").exists():
            raise SystemExit(
                f"refusing to run window {window!r} before holdout_candidate_lock.json exists"
            )
        for scenario in args.scenarios:
            for cell_id in selected:
                if cell_id not in spec:
                    raise SystemExit(f"unknown cell {cell_id!r}")
                cid, group, ops = spec[cell_id]
                described = describe_cell(cid, group, ops)
                payload = {
                    "cell_id": cid,
                    "group": group,
                    "ops": described["ops"] if cid != "reference" else [],
                    "window": window,
                    "scenario": scenario,
                    "force": args.force,
                }
                if "entry_regime_gate" in described:
                    payload["entry_regime_gate"] = described["entry_regime_gate"]
                for key in ("sides", "hedge_mode"):
                    if key in described:
                        payload[key] = described[key]
                started = time.time()
                try:
                    record = run_cell(payload)
                except Exception as exc:  # noqa: BLE001 - a window may be unavailable
                    print(
                        "%-26s %-8s %-16s FAILED: %s: %s"
                        % (cid, window, scenario, type(exc).__name__, exc),
                        flush=True,
                    )
                    if args.fail_fast:
                        raise
                    continue
                m = record["metrics"]
                print(
                    "%-26s %-8s %-16s mdd=%.4f w1=%.4f cagr=%+.4f gain=%.3f uw=%.1f "
                    "hy=%s/%s coin=%s fills=%s (%.1fs)"
                    % (
                        cid,
                        window,
                        scenario,
                        m["minute_close_mdd"],
                        m["worst_1pct_mean_drawdown"],
                        m["cagr"],
                        m["gain_strategy_eq"],
                        m["total_underwater_days"],
                        m.get("positive_halfyears"),
                        m.get("halfyear_count"),
                        m.get("traded_coin_count"),
                        m.get("fills"),
                        time.time() - started,
                    ),
                    flush=True,
                )


def load_cells() -> list[dict[str, Any]]:
    return [load_json(path) for path in sorted(RUNS.rglob("result.json"))]


def cell_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (record["window"], record["scenario"], record["cell_id"])


def gate_report(window: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """Absolute gates. The reported and validation windows use different floors."""
    failures = []
    if (metrics.get("backtest_completion_ratio") or 0.0) < GATES["completion_ratio_min"]:
        failures.append("completion")
    if metrics.get("liquidated"):
        failures.append("liquidated")
    if window == "full":
        mdd_max = GATES["mdd_max_full"]
        cagr_floor = GATES["cagr_floor_full"]
    else:
        mdd_max = GATES["mdd_max_valid"]
        cagr_floor = GATES["cagr_floor_valid"]
    if metrics["minute_close_mdd"] > mdd_max:
        failures.append("mdd")
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
        "mdd_max": mdd_max,
        "cagr_floor": cagr_floor,
        "mdd_slack": mdd_max - metrics["minute_close_mdd"],
        "cagr_slack": metrics["cagr"] - cagr_floor,
        "passed": not failures,
    }


def analyze_command(args: argparse.Namespace) -> None:
    records = load_cells()
    if not records:
        raise SystemExit("no cell results found")
    by_key = {cell_key(rec): rec for rec in records}
    rows = []
    for rec in records:
        if rec["cell_id"] == "reference":
            continue
        base = by_key.get((rec["window"], rec["scenario"], "reference"))
        bm = base["metrics"] if base else None
        m = rec["metrics"]
        rows.append(
            {
                "cell_id": rec["cell_id"],
                "group": rec["group"],
                "window": rec["window"],
                "scenario": rec["scenario"],
                "mdd": m["minute_close_mdd"],
                "worst_1pct_mean": m["worst_1pct_mean_drawdown"],
                "mdd_delta_vs_reference": (m["minute_close_mdd"] - bm["minute_close_mdd"])
                if bm
                else None,
                "cagr": m["cagr"],
                "gain": m["gain_strategy_eq"],
                "underwater_days": m["total_underwater_days"],
                "recovery_days": m["recovery_days"],
                "peak_to_trough_days": m["peak_to_trough_days"],
                "trough_time": m["trough_time"],
                "worst_halfyear": m.get("worst_halfyear_return"),
                "positive_halfyears": m.get("positive_halfyears"),
                "halfyear_count": m.get("halfyear_count"),
                "traded_coin_count": m.get("traded_coin_count"),
                "fills": m.get("fills"),
                "twe_max": m.get("total_wallet_exposure_max"),
                "sortino": m.get("sortino_ratio_strategy_eq"),
                "gate": gate_report(rec["window"], m),
                "ops": rec["ops"],
            }
        )
    rows.sort(key=lambda row: (row["window"], row["scenario"], row["mdd"]))
    ANALYSIS.mkdir(parents=True, exist_ok=True)
    write_json(ANALYSIS / "screen.json", {"rows": rows})
    print(
        f"{'cell':26s} {'group':14s} {'win':8s} {'mdd':>7s} {'w1%':>7s} {'cagr':>8s} "
        f"{'gain':>7s} {'uw_d':>7s} {'worst_hy':>9s} {'coins':>5s} gate"
    )
    for row in rows:
        print(
            "%-26s %-14s %-8s %7.4f %7.4f %+8.4f %7.3f %7.1f %9.4f %5s %s"
            % (
                row["cell_id"],
                row["group"],
                row["window"],
                row["mdd"],
                row["worst_1pct_mean"],
                row["cagr"],
                row["gain"],
                row["underwater_days"],
                row["worst_halfyear"] if row["worst_halfyear"] is not None else float("nan"),
                row["traded_coin_count"],
                ",".join(row["gate"]["failures"]) or "PASS",
            )
        )


def writespec_command(args: argparse.Namespace) -> None:
    payload = contract_payload(with_combos=bool(getattr(args, "with_combos", False)))
    path = Path(args.out) if args.out else STUDY / "research_contract.json"
    write_json(path, payload)
    print(f"wrote {path}")
    print(f"cell_matrix_sha256={payload['cell_matrix_sha256']}")
    print(f"seed_config_sha256={payload['seed']['config_sha256']}")
    print(f"cells={len(payload['cells'])} universe={len(payload['universe'])}")


def list_command(args: argparse.Namespace) -> None:
    for cid, grp, ops in _cells():
        print(f"{cid:26s} {grp:14s} {len(ops)} ops")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ws = sub.add_parser("write-contract")
    ws.add_argument("--out", default=None)
    ws.add_argument("--with-combos", action="store_true")
    ws.set_defaults(func=writespec_command)

    lsr = sub.add_parser("list")
    lsr.set_defaults(func=list_command)

    rn = sub.add_parser("run")
    rn.add_argument("--cells", nargs="+", default=["reference"])
    rn.add_argument("--windows", nargs="+", default=["full"], choices=sorted(WINDOWS))
    rn.add_argument("--scenarios", nargs="+", default=[PRIMARY_SCENARIO], choices=sorted(SCENARIOS))
    rn.add_argument("--force", action="store_true")
    rn.add_argument("--fail-fast", action="store_true")
    rn.set_defaults(func=run_command)

    an = sub.add_parser("analyze")
    an.set_defaults(func=analyze_command)

    ec = sub.add_parser("emit-config", help="write a cell's exact CLI config")
    ec.add_argument("--cell", required=True)
    ec.add_argument("--window", default="full", choices=sorted(WINDOWS))
    ec.add_argument("--scenario", default=PRIMARY_SCENARIO, choices=sorted(SCENARIOS))
    ec.add_argument("--base-dir", default=None)
    ec.add_argument("--out", required=True)
    ec.set_defaults(func=emit_config_command)

    bd = sub.add_parser("bundle", help="produce artifact bundles and render their reports")
    bd.add_argument("--cells", nargs="+", default=["all"])
    bd.add_argument("--window", default="full", choices=sorted(WINDOWS))
    bd.add_argument("--scenario", default=PRIMARY_SCENARIO, choices=sorted(SCENARIOS))
    bd.add_argument("--force", action="store_true")
    bd.set_defaults(func=bundle_command)

    args = parser.parse_args()
    assert_offline_env()
    args.func(args)


if __name__ == "__main__":
    main()
