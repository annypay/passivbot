#!/usr/bin/env python3
"""Single source of truth for the `hsl_npos1` standalone deep analysis.

Named `hsl_npos1_spec` rather than `cell_spec` because pytest imports study tool modules by
basename: another study already ships a `cell_spec.py`, and a shared basename would make one
study silently import the other's constants.

The study runs the published example profile `configs/examples/hsl_npos1.json` as one
offline backtest artifact bundle and renders the repository's canonical strategy-research
deep analysis for it, so the result can be read next to
`backtests/binance/2026-09-14T03_40_41/annual_analysis.md`.

The profile has been backtested before, but only as a study cell:
`backtests/binance/low_drawdown_strategy_study_2026-09-14/selection/hsl_npos1/C1/` covers
2023-09-12..2025-09-12 and carries no `annual_analysis.md`. This study fills that gap.

Offline only: the run reads the frozen local HLCV dataset and never contacts a network,
uses credentials, touches an exchange account, or starts a bot. Every path and every
constant the other tools need lives here.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

# <repo>/backtests/binance/<study>/report_tools/cell_spec.py
REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/hsl_npos1_analysis_2026-09-16"
ARTIFACTS = STUDY / "artifacts"
#: The backtest writes `<backtest.base_dir>/<exchange>/<UTC timestamp>/`. The profile keeps the
#: repository's own `base_dir = "backtests"` (changing it would edit a field the published
#: profile owns), so discovery looks in `backtests/binance/` and the runner then moves the run
#: into this study's artifact tree.
RUN_SOURCE_BASE = REPO / "backtests/binance"
RUNS_BASE = ARTIFACTS / "backtest_results/binance"
LOGS = ARTIFACTS / "logs"

SOURCE_CONFIG = REPO / "configs/examples/hsl_npos1.json"
SOURCE_CONFIG_SHA256 = "9b3075f258fcb4f38680d5e0120f6383d03a2062f623884963820e6429893901"

CONFIG_PATH = ARTIFACTS / "hsl_npos1.config.json"
EXECUTION_AUDIT_PATH = ARTIFACTS / "execution_audit.csv"
RUN_LOG_PATH = LOGS / "run.log"

STUDY_ID = "hsl_npos1"
CANDIDATE_LABEL = "HSL-enabled trailing martingale (n_positions=10 long)"

#: Frozen local dataset used as the HLCV source. `caches/ohlcvs` is the repository's own
#: offline candle catalog (chunk store + sqlite catalog), so `intersection` mode selects the
#: requested coins that actually have local data and clamps the window to it.
FROZEN_DATASET_REL = Path("caches/ohlcvs")
FROZEN_DATASET = REPO / FROZEN_DATASET_REL

#: The published profile requests 2021-01-01..now. The local catalog starts 2021-02-01, so the
#: request is aligned to a date the frozen data actually covers and the effective window is then
#: decided by the dataset intersection (recorded in `_hlcvs_dataset_override_meta`).
REQUESTED_START = "2021-04-20"
REQUESTED_END = "2026-09-13T00:00:00"

#: Reported execution and cost contract. The published profile does not declare either execution
#: key, so these are the schema defaults (`src/config/schema.py`) written explicitly into the run
#: config: nominal causal T+1, close-first.
EXECUTION = {"execution_delay_bars": 0, "intrabar_fill_order": "close_first"}
COSTS = {"maker_fee_override": 0.0004, "taker_fee_override": 0.00055}
#: Minute-resolution balance/equity series, the same resolution as `analysis.json`.
BALANCE_SAMPLE_DIVIDER = 1
CANDLE_INTERVAL_MINUTES = 1

#: `backtest` keys this study is allowed to retarget. Everything else in `backtest`, and the
#: whole `bot` / `coin_overrides` subtree, must stay equal to the published profile.
ALLOWED_BACKTEST_KEYS = (
    "balance_sample_divider",
    "base_dir",
    "candle_interval_minutes",
    "coins",
    "disable_plotting",
    "end_date",
    "execution_audit_path",
    "execution_delay_bars",
    "exchanges",
    "hlcvs_data_dir",
    "hlcvs_data_override_mode",
    "intrabar_fill_order",
    "maker_fee_override",
    "scenarios",
    "start_date",
    "suite_enabled",
    "taker_fee_override",
)

#: Reference material the report convention is pinned to.
REFERENCE_REPORT = "backtests/binance/2026-09-14T03_40_41/annual_analysis.md"
REPORT_CONVENTION = "docs/ai/runbooks/strategy_report.md"

#: Previously existing `hsl_npos1` run this study compares against qualitatively. It uses a
#: different window, a different coin basket and a different sampling divider, so its numbers
#: are never placed in the same table as this report's numbers.
PRIOR_RUN_REL = Path(
    "backtests/binance/low_drawdown_strategy_study_2026-09-14/selection/hsl_npos1/C1/"
    "binance/2026-09-14T08_18_04"
)
PRIOR_RUN = REPO / PRIOR_RUN_REL
PRIOR_RUN_LABEL = (
    "backtests/binance/low_drawdown_strategy_study_2026-09-14/selection/hsl_npos1/C1/"
    "binance/2026-09-14T08_18_04"
)

#: Order types the ledger expects. Anything else is reported as unrecognised so a silent change
#: in the Rust order templates cannot pass unnoticed.
ENTRY_TEMPLATES = (
    "entry_initial_normal_long",
    "entry_initial_partial_long",
    "entry_grid_normal_long",
    "entry_grid_cropped_long",
    "entry_trailing_normal_long",
    "entry_trailing_cropped_long",
)
CLOSE_TEMPLATES = (
    "close_grid_long",
    "close_trailing_long",
    "close_unstuck_long",
    "close_panic_long",
    "close_auto_reduce_wel_long",
    "close_auto_reduce_twel_long",
)
#: Fill types the risk layer or HSL can emit; the measured evidence that the exposure enforcers
#: actually bounded the book, and that HSL or unstuck fired at all.
RISK_FILL_TYPES = (
    "close_auto_reduce_wel_long",
    "close_auto_reduce_twel_long",
    "close_unstuck_long",
    "close_panic_long",
)

#: Explicitly excluded profile coins, with the reason the report must state.
#:
#: `XAUT` is a tokenized-gold perpetual that Binance USDT-M listed long after this window; the
#: local catalog has no history for it. It is excluded by policy rather than by catalog lookup,
#: because a run that resolves it will fetch its market metadata from the network and register
#: a synthetic, fully gap-filled series that starts in 2026-03-26 -- both of which would
#: falsify this study's offline claim and its "everything in the window was tradable" reading.
EXCLUDED_COINS = {"XAUT": "本地 K 线目录无历史数据；只有 2026-03-26 起的合成序列"}

#: Artifacts a completed run directory must carry before a report may be rendered from it.
REQUIRED_ARTIFACTS = (
    "analysis.json",
    "config.json",
    "dataset.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
)


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


def set_path(config: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node: Any = config
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


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


def differing_paths(expected: Any, actual: Any, root: str) -> list[str]:
    """Flat dotted paths where two subtrees disagree (missing keys count as differing)."""
    left = flatten(expected, root)
    right = flatten(actual, root)
    return sorted(
        key
        for key in set(left) | set(right)
        if key not in left or key not in right or not numeric_equal(left[key], right[key])
    )


def frozen_dataset_coins() -> list[str]:
    """Coin list of the frozen local HLCV catalog, read from its sqlite sidecar."""
    catalog = FROZEN_DATASET / "catalog.sqlite"
    if catalog.is_file():
        connection = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM symbols WHERE exchange = 'binance'"
            ).fetchall()
        finally:
            connection.close()
        coins = sorted(
            str(symbol).split("/")[0] for (symbol,) in rows if symbol and "/" in str(symbol)
        )
        if coins:
            return coins
    raise SystemExit(f"no local coin catalog found under {FROZEN_DATASET}")


def dated_run_dirs(base: Path) -> list[Path]:
    """Run directories under a results base, newest last.

    Two layouts exist in this repository and both must be found:

    * **flat** -- the published profiles keep `backtest.base_dir = "backtests"`, so their runs
      land directly in `backtests/binance/<UTC timestamp>/`;
    * **nested** -- study tooling points `base_dir` at a study's own results tree, producing
      `<base>/binance[_label]/<UTC timestamp>/`.

    The walk stays shallow on purpose: the backtest creates the timestamped directory before it
    writes anything into it, so a deeper pattern would miss a run that is still being written.
    """
    root = Path(base)
    if not root.is_dir():
        return []
    exchange_levels = [root, *(child for child in sorted(root.iterdir()) if child.is_dir())]
    found: list[Path] = []
    for level in exchange_levels:
        if level != root and not level.name.startswith("binance"):
            continue
        found.extend(
            path
            for path in sorted(level.iterdir())
            if path.is_dir() and path.name[:2].isdigit()
        )
    return sorted(set(found))
