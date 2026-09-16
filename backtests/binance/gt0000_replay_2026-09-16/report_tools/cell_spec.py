#!/usr/bin/env python3
"""Single source of truth for the g3_cth0000 offline deterministic replay.

The replay reproduces one declared cell of the frozen drawdown study
(`backtests/binance/returns_guarded_dd_research_2026-09-16`) as a full backtest
artifact bundle, so the resulting report can be read next to
`backtests/binance/2026-09-14T03_40_41/annual_analysis.md`.

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
STUDY = REPO / "backtests/binance/gt0000_replay_2026-09-16"
ARTIFACTS = STUDY / "artifacts"
#: The backtest writes `<backtest.base_dir>/<exchange>/<UTC timestamp>/`. The frozen config
#: keeps the repository's own `base_dir = "backtests"` (changing it would edit the config the
#: study cell ran), so discovery looks in `backtests/binance/` and the run is then moved into
#: the study's own artifact tree.
RUN_SOURCE_BASE = REPO / "backtests/binance"
RUNS_BASE = ARTIFACTS / "backtest_results/binance"
LOGS = ARTIFACTS / "logs"

CONFIG_PATH = ARTIFACTS / "g3_cth0000.config.json"
CELL_INPUT_PATH = ARTIFACTS / "cell_input.json"
EXECUTION_AUDIT_PATH = ARTIFACTS / "execution_audit.csv"
REPLAY_LOG_PATH = LOGS / "replay_run.log"

CELL_ID = "g3_cth0000"
CELL_GROUP = "G3_scaleout"
CELL_WINDOW = "full"
SCENARIO = "C1_binance_actual"

SOURCE_STUDY = REPO / "backtests/binance/returns_guarded_dd_research_2026-09-16"
CELL_RESULT_PATH = SOURCE_STUDY / "cells" / CELL_WINDOW / SCENARIO / CELL_ID / "result.json"
CONTRACT_PATH = SOURCE_STUDY / "research_contract.json"

SEED_CONFIG_PATH = REPO / "configs/examples/trailing_martingale_twel100_ddf060.json"
PATCH_PATH = "bot.long.strategy.trailing_martingale.close.threshold_base_pct"
#: The declared cell is exactly one op against the published lower-tail profile.
EXPECTED_OPS = (
    {"op": "set", "path": PATCH_PATH, "seed": -0.0027, "value": 0.0},
)

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

#: Frozen baseline run the report compares against (same window, same cost regime).
BASELINE_RUN = REPO / "backtests/binance/2026-09-14T03_40_41"
BASELINE_ANALYSIS = BASELINE_RUN / "analysis.json"
BASELINE_CONFIG = BASELINE_RUN / "config.json"
BASELINE_LABEL = "backtests/binance/2026-09-14T03_40_41"

REFERENCE_REPORT = "backtests/binance/2026-09-14T03_25_14/annual_analysis.md"
REPORT_CONVENTION = "docs/ai/runbooks/strategy_report.md"

#: Reported execution and cost contract (research contract `C1` + `binance_actual`).
EXECUTION = {"execution_delay_bars": 0, "intrabar_fill_order": "close_first"}
COSTS = {"maker_fee_override": 0.0002, "taker_fee_override": 0.0005}

#: `bot` subtree the cell config must reproduce from the seed profile plus `EXPECTED_OPS`.
BOT_ROOT = "bot"

#: Backtest keys the study adds on top of the seed profile. The gate requires these keys
#: to exist in the cell config and checks their values; it never permits value drift.
STUDY_ADDED_BACKTEST_KEYS = (
    "cache_dir",
    "coins",
    "execution_audit_path",
    "scenarios",
    "suite_enabled",
    "visible_metrics",
    "volume_normalization",
)

#: Backtest keys the study is allowed to retarget, with the value the gate requires. These
#: carry the study's declared window and its single-exchange data source; nothing else in
#: `backtest` may differ from the seed profile.
STUDY_RETARGETED_BACKTEST = {
    "start_date": "2023-09-12",
    "end_date": "2026-09-12",
    "exchanges": ["binance"],
}

#: `analysis.json` keys the replay must reproduce from the study cell record.
CELL_METRIC_KEYS = (
    "gain_strategy_eq",
    "drawdown_worst_strategy_eq",
    "strategy_eq_recovery_days_max",
    "position_held_days_max",
    "minute_close_mdd",
    "worst_1pct_mean_drawdown",
    "cagr",
    "total_underwater_days",
    "recovery_days",
    "positive_halfyears",
    "halfyear_count",
    "worst_halfyear_return",
    "traded_coin_count",
    "total_wallet_exposure_max",
    "sortino_ratio_strategy_eq",
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
