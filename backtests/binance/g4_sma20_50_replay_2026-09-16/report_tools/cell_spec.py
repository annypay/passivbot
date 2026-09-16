#!/usr/bin/env python3
"""Single source of truth for the g4_sma20_50 published-profile replay.

The gate cell `g4_sma20_50` of the frozen drawdown study
(`backtests/binance/returns_guarded_dd_research_2026-09-16`) is now a published
profile: `configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json`. This
study replays **that file, as shipped**, as a full backtest artifact bundle, so its
report can be read next to `backtests/binance/2026-09-14T03_40_41/annual_analysis.md`
and next to the gate cell's own evidence in the source study.

Because the artifact is the published profile itself, the replay does not rebuild
the config from seed ops. The frozen copy is byte-equal to the tracked profile, and
the builder proves that rather than deriving it.

Offline only: no network access, no credentials, no exchange account, no bot start.
Every path and every constant the other tools need lives here.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

# <repo>/backtests/binance/<study>/report_tools/cell_spec.py
REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_sma20_50_replay_2026-09-16"
ARTIFACTS = STUDY / "artifacts"
#: The backtest writes `<backtest.base_dir>/<exchange>/<UTC timestamp>/`. The frozen config
#: retargets `base_dir` at this study's own artifact tree, so the run lands directly in the
#: nested `artifacts/backtest_results/<exchange>/<exchange>/<timestamp>/` shape the report
#: convention requires and no post-run move is needed.
RUNS_BASE = ARTIFACTS / "backtest_results/binance"
BACKTEST_BASE_DIR = ARTIFACTS / "backtest_results"
LOGS = ARTIFACTS / "logs"

CONFIG_PATH = ARTIFACTS / "g4_sma20_50.config.json"
PROFILE_INPUT_PATH = ARTIFACTS / "profile_input.json"
EXECUTION_AUDIT_PATH = ARTIFACTS / "execution_audit.csv"
REPLAY_LOG_PATH = LOGS / "replay_run.log"

CELL_ID = "g4_sma20_50"
CELL_GROUP = "G4_regime_gate"
CELL_WINDOW = "full"
SCENARIO = "C1_binance_actual"

#: The published profile this study replays. It is the artifact under test.
PUBLISHED_PROFILE = REPO / "configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json"
#: The profile it extends, used as the comparison column.
BASE_PROFILE = REPO / "configs/examples/trailing_martingale_twel100_ddf060.json"

SOURCE_STUDY = REPO / "backtests/binance/returns_guarded_dd_research_2026-09-16"
CELL_RESULT_PATH = SOURCE_STUDY / "cells" / CELL_WINDOW / SCENARIO / CELL_ID / "result.json"
CONTRACT_PATH = SOURCE_STUDY / "research_contract.json"
#: The gate cell's own bundle, produced by the source study. It is the second comparison
#: column: same cell, independent artifact, so the replay and the study must agree.
SOURCE_BUNDLE_RUN_GLOB = (
    "artifacts/best_dd_reducer/backtest_results/binance_best_dd_reducer/binance/*"
)
SOURCE_BUNDLE_LABEL = "returns_guarded_dd_research_2026-09-16/artifacts/best_dd_reducer"

#: Frozen baseline run the report compares against (same window and cost regime, different
#: strategy parameters: this is the un-gated default profile).
BASELINE_RUN = REPO / "backtests/binance/2026-09-14T03_40_41"
BASELINE_ANALYSIS = BASELINE_RUN / "analysis.json"
BASELINE_CONFIG = BASELINE_RUN / "config.json"
BASELINE_LABEL = "backtests/binance/2026-09-14T03_40_41"

FROZEN_CACHE_REL = Path(
    "caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__8300950b42789a26"
)
FROZEN_CACHE = REPO / FROZEN_CACHE_REL
FROZEN_CACHE_FILES = (
    "manifest.json",
    "hlcvs.npy.gz",
    "timestamps.npy.gz",
    "btc_usd_prices.npy.gz",
    "coins.json",
    "market_specific_settings.json",
)

REFERENCE_REPORT = "backtests/binance/2026-09-14T03_25_14/annual_analysis.md"
REPORT_CONVENTION = "docs/ai/runbooks/strategy_report.md"

#: Reported execution and cost contract (research contract `C1` + `binance_actual`).
EXECUTION = {"execution_delay_bars": 0, "intrabar_fill_order": "close_first"}
COSTS = {"maker_fee_override": 0.0002, "taker_fee_override": 0.0005}

#: The gate block the published profile declares. The replay must run with exactly this.
GATE = {
    "enabled": True,
    "sma_fast_days": 20,
    "sma_slow_days": 50,
    "confirm_days": 0,
    "block_initial": True,
    "block_reentry": True,
    "gate_mode": "both",
}

#: Top-level config subtrees that must match the published profile byte for byte.
IDENTITY_ROOTS = ("bot", "live", "coin_overrides", "monitor", "logging")

#: The window the recorded evidence covers. The published profile ships an *operational*
#: window (`start_date = 2021-04-20`, `end_date = "now"`) because that is what a live config
#: should say; the study's cells were all evaluated on this fixed three-year window.
EVIDENCE_WINDOW = ("2023-09-12", "2026-09-12")

#: The single exchange the frozen bundle and the research contract's primary scenario use. The
#: profile lists `["binance", "bybit"]`, which is a *data-source* preference for live operation:
#: leaving it in place makes the loader resolve a combined dataset under a different cache
#: identity, miss the frozen bundle, and rebuild from the network.
DATA_EXCHANGE = "binance"

#: The 40 coins the frozen bundle actually holds (the profile declares 41; `MNT` has no usable
#: history, so the dataset is 40).
EVIDENCE_COIN_COUNT = 40

#: Top-level config subtrees whose leaves the study retargets by name, keyed by the dotted path
#: inside the root. `live.approved_coins` is the *traded* universe, and the evidence pinned it to
#: the frozen basket with no short side. The published profile approves all 41 candidates on both
#: sides and disables short trading structurally instead (`bot.short.risk.
#: total_wallet_exposure_limit = 0.0`), so the two agree in behaviour; the study still matches the
#: evidence's own wiring so the comparison is exact rather than merely equivalent.
RETARGETED_LEAF_PATHS = ("live.approved_coins",)

#: Backtest keys the study retargets. The published profile is an **operational** config: its
#: window is open-ended and its data source is a live preference. The recorded evidence is a
#: **fixed** three-year, single-exchange, 40-coin dataset. Reproducing that evidence therefore
#: requires retargeting the data identity, and this study declares exactly which keys it
#: retargets rather than hiding the difference:
#:
#: * `base_dir`   — where the run directory is written (an output location).
#: * `exchanges`  — the evidence's data source.
#: * `start_date` / `end_date` — the evidence's fixed window.
#: * `coins`      — the evidence's frozen 40-coin universe.
#: * `cache_dir`  — the frozen HLCV bundle itself, so the run cannot rebuild or fetch.
#:
#: Every other key in `backtest`, and every one of `bot` / `live` / `coin_overrides` /
#: `monitor` / `logging`, must match the published profile. In particular no strategy, risk,
#: exit or gate parameter is retargeted.
RETARGETED_BACKTEST_KEYS = (
    "base_dir",
    "exchanges",
    "start_date",
    "end_date",
    "coins",
    "cache_dir",
)

#: The gate fields that are a property of the shipped strategy, as opposed to applier-internal
#: wiring (`gate_mode` is the declared mode; `approved` is derived from `live.approved_coins`).
GATE_DECLARATION_KEYS = (
    "enabled",
    "sma_fast_days",
    "sma_slow_days",
    "confirm_days",
    "block_initial",
    "block_reentry",
    "gate_mode",
)

#: Keys the backtest deliberately clears before dumping a run's config: they are resolved at
#: runtime, and `dataset.json` is the authoritative record of what was actually loaded.
DUMP_CLEARED_BACKTEST_KEYS = ("coins", "cache_dir")

#: Markers that prove a run resolved the frozen bundle instead of building a new one.
NETWORK_FETCH_MARKERS = (
    "download ccxt",
    "Binance daily archive fetch",
    "v2 local fetching missing range",
    "starting v2-aware candle preparation",
)

#: Cell-record metrics the replay's own `analysis.json` must reproduce, mapped to the
#: analysis key that carries the same quantity. The cell record uses the study's own names;
#: `analysis.json` uses the engine's.
CELL_METRIC_MAP = {
    "gain_strategy_eq": "gain_strategy_eq",
    "drawdown_worst_strategy_eq": "drawdown_worst_strategy_eq",
    "minute_close_mdd": "drawdown_worst_strategy_eq",
    "strategy_eq_recovery_days_max": "strategy_eq_recovery_days_max",
    "position_held_days_max": "position_held_days_max",
    "sortino_ratio_strategy_eq": "sortino_ratio_strategy_eq",
    "total_wallet_exposure_max": "total_wallet_exposure_max",
    "total_wallet_exposure_mean": "total_wallet_exposure_mean",
    "loss_profit_ratio": "loss_profit_ratio",
    "traded_coin_count": "fills_active_symbols_count",
    "active_days_ratio": "fills_active_days_ratio",
    "top_symbol_share": "fills_top_symbol_share",
}

#: Cell-record metrics that are *derived by the study* from the equity series rather than
#: reported by the engine, so the verifier recomputes them instead of reading a key.
CELL_DERIVED_METRICS = (
    "cagr",
    "worst_1pct_mean_drawdown",
    "total_underwater_days",
    "recovery_days",
)

#: Cell-record metrics the engine does not report at all. They describe the study's own
#: half-year bucketing; the replay reproduces the runs the study measured, and these appear in
#: the report as quoted context, never as checks.
CELL_CONTEXT_ONLY_METRICS = (
    "positive_halfyears",
    "halfyear_count",
    "worst_halfyear_return",
)

#: Every cell metric the tooling knows how to place, so an unplaced one is caught.
CELL_METRIC_KEYS = (
    *CELL_METRIC_MAP,
    *CELL_DERIVED_METRICS,
    *CELL_CONTEXT_ONLY_METRICS,
)
#: `result.json.metrics` key holding the fill-row count (`analysis.json.fills_count`).
CELL_FILLS_KEY = "fill_rows"


def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def relative(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def get_path(config: dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        node = node[part]
    return node


def numeric_equal(left: Any, right: Any, rel_tol: float = 1e-12) -> bool:
    """True when two config leaves are the same value, tolerating int/float spelling."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if not (math.isfinite(float(left)) and math.isfinite(float(right))):
            return float(left) == float(right)
        return math.isclose(float(left), float(right), rel_tol=rel_tol, abs_tol=1e-15)
    return left == right


def flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into `a.b.c` leaves; lists are compared as whole values."""
    out: dict[str, Any] = {}
    if isinstance(node, dict):
        for key in node:
            out.update(flatten(node[key], f"{prefix}.{key}" if prefix else str(key)))
    else:
        out[prefix] = node
    return out


def compare_subtrees(expected: Any, actual: Any, root: str) -> list[str]:
    """Return human-readable differences between two flattened subtrees."""
    left = flatten(expected, root)
    right = flatten(actual, root)
    problems: list[str] = []
    for key in sorted(set(left) | set(right)):
        if key not in right:
            problems.append(f"{key}: missing in candidate (expected {left[key]!r})")
        elif key not in left:
            problems.append(f"{key}: unexpected in candidate (value {right[key]!r})")
        elif not numeric_equal(left[key], right[key]):
            problems.append(f"{key}: expected {left[key]!r}, got {right[key]!r}")
    return problems


def equal_subtree(left: Any, right: Any, root: str) -> list[str]:
    """Differences between two subtrees, ignoring the study's declared retargets."""
    prefixes = tuple(f"backtest.{key}" for key in RETARGETED_BACKTEST_KEYS) + RETARGETED_LEAF_PATHS
    return [
        problem
        for problem in compare_subtrees(left, right, root)
        if not problem.startswith(prefixes)
    ]


#: Engine defaults for the declared gate fields, so a profile that omits one is read as the
#: engine would read it instead of as `None`.
GATE_DEFAULTS = {
    "enabled": False,
    "sma_fast_days": None,
    "sma_slow_days": None,
    "confirm_days": 0,
    "block_initial": True,
    "block_reentry": True,
    "gate_mode": "both",
}


def normalize_config_payload(config: dict[str, Any]) -> dict[str, Any]:
    """Run a config through the same hydration/cleaning the backtest applies before dumping.

    A raw profile file and a run's dumped `config.json` are not directly comparable: the dump is
    rebuilt from the current schema template, so template keys added after the profile was
    authored appear on the dumped side only. Comparing app-normalized payloads on both sides
    measures the configuration the app runs instead of the file's spelling.
    """
    import sys

    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from config_utils import sanitize_prepared_config_for_dump

    return sanitize_prepared_config_for_dump(config)


def gate_semantics(gate: dict[str, Any] | None) -> dict[str, Any]:
    """The shipped gate declaration, without applier-internal wiring.

    A key the profile omits resolves to the engine's default, so the comparison measures the
    configuration the engine actually runs rather than the file's spelling.
    """
    if not isinstance(gate, dict):
        return {}
    return {key: gate.get(key, GATE_DEFAULTS[key]) for key in GATE_DECLARATION_KEYS}


def resolve_source_bundle_run() -> Path:
    """The gate cell's own run directory inside the source study's bundle."""
    runs = sorted(
        path
        for path in SOURCE_STUDY.glob(SOURCE_BUNDLE_RUN_GLOB)
        if path.is_dir() and path.name[:2].isdigit()
    )
    if len(runs) != 1:
        raise SystemExit(
            f"expected exactly one run directory under {SOURCE_BUNDLE_LABEL}, found "
            f"{[path.name for path in runs]}"
        )
    return runs[0]
