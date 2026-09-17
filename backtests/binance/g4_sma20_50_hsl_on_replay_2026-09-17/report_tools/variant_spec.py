#!/usr/bin/env python3
"""Single source of truth for the g4 "HSL on" variant replay.

The parent artifact is the tracked g4 published-profile replay
(`backtests/binance/g4_sma20_50_replay_2026-09-16`). This study replays that same frozen
run config with exactly **one** declared change — `bot.long.hsl.enabled: false -> true` —
and, because the parent bundle was produced by an older engine revision, it also replays
the unchanged config on the current engine as a paired control. Every comparison the
report makes therefore has a column that isolates the HSL change from engine drift.

Nothing else is retargeted: window, coin basket, frozen dataset, execution/cost contract
and the entry-regime gate are byte-identical to the parent config apart from the output
location this study owns.

Offline only: no network access, no credentials, no exchange account, no bot start.
Every path and every pinned constant the other tools need lives here.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

# <repo>/backtests/binance/<study>/report_tools/variant_spec.py
REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17"
ARTIFACTS = STUDY / "artifacts"

#: The parent study whose frozen config is the variant's source of truth.
PARENT_STUDY = REPO / "backtests/binance/g4_sma20_50_replay_2026-09-16"
SOURCE_CONFIG = PARENT_STUDY / "artifacts/g4_sma20_50.config.json"
SOURCE_CONFIG_SHA256 = "5c8ab5edad9b571cab32a91e82013268ad99ff746a49131cb37bf225b2908bd9"
SOURCE_PROFILE_INPUT = PARENT_STUDY / "artifacts/profile_input.json"
SOURCE_PROFILE_INPUT_SHA256 = "1e6e8c61811e303297e1e72365fd5dad48a05aaddf42c75b8f89265eaf8778ce"

#: The parent bundle: the recorded HSL-OFF evidence this study is compared against. Its
#: `analysis.json` is tracked, so the comparison survives a fresh checkout; the run directory
#: is named from the parent's UTC completion timestamp.
TRACKED_BASELINE_RUN = (
    PARENT_STUDY / "artifacts/backtest_results/binance/2026-09-16T11_45_49"
)
TRACKED_BASELINE_ANALYSIS_SHA256 = (
    "295485faa423d6840decf7d7d1e9e9d17352f794a30d68e0ae704e1519d1c3db"
)
TRACKED_BASELINE_LABEL = "tracked_baseline_hsl_off"

VARIANT_INPUT_PATH = ARTIFACTS / "variant_input.json"

#: The frozen HLCV bundle both variants must be served by. Paths are repository-relative in
#: the config and absolute here.
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

DATA_EXCHANGE = "binance"
EVIDENCE_WINDOW = ("2023-09-12", "2026-09-12")
EVIDENCE_COIN_COUNT = 40

#: Reported execution and cost contract, identical to the parent study.
EXECUTION = {"execution_delay_bars": 0, "intrabar_fill_order": "close_first"}
COSTS = {"maker_fee_override": 0.0002, "taker_fee_override": 0.0005}

#: The entry-regime gate the parent config declares; the variant must keep it exactly.
GATE = {
    "enabled": True,
    "sma_fast_days": 20,
    "sma_slow_days": 50,
    "confirm_days": 0,
    "block_initial": True,
    "block_reentry": True,
    "gate_mode": "both",
}

#: Top-level config subtrees that must match the parent config byte for byte.
IDENTITY_ROOTS = ("bot", "live", "coin_overrides", "monitor", "logging")

#: The *one* declared behavioural change of this study, as (dotted path, from, to). It is
#: applied to the parent config to build the `hsl_on` variant and asserted everywhere else.
DECLARED_DELTA = (("bot.long.hsl.enabled", False, True),)

#: The HSL block the variant must carry: the parent profile's own parameters, untouched. The
#: delta flips only `enabled`; a tuned threshold would be a different study.
HSL_BLOCK = {
    "enabled": True,
    "red_threshold": 0.15,
    "ema_span_minutes": 720.0,
    "cooldown_minutes_after_red": 2160.0,
    "no_restart_drawdown_threshold": 1,
    "restart_after_red_policy": "threshold",
    "orange_tier_mode": "tp_only_with_active_entry_cancellation",
    "panic_close_order_type": "limit",
    "tier_ratios": {"yellow": 0.5, "orange": 0.75},
}
#: HSL surface that is live-only; it is reported as context, never claimed as modelled.
LIVE_ONLY_HSL = {"hsl_signal_mode": "coin", "hsl_position_during_cooldown_policy": "panic"}

#: Config paths a variant is allowed to change relative to the parent frozen config.
ALLOWED_CONFIG_DIFF_PATHS = ("backtest.base_dir",)
#: Additional paths the *run dump* may differ by: the CLI supplies the audit path, and the
#: dumped config carries the resolved HSL flag the variant declares.
ALLOWED_RUN_DUMP_DIFF_PATHS = (
    *ALLOWED_CONFIG_DIFF_PATHS,
    "backtest.execution_audit_path",
    "bot.long.hsl.enabled",
)
#: Keys the backtest deliberately clears before dumping a run's config.
DUMP_CLEARED_BACKTEST_KEYS = ("coins", "cache_dir")

#: Markers that prove a run resolved the frozen bundle instead of building a new one.
NETWORK_FETCH_MARKERS = (
    "download ccxt",
    "Binance daily archive fetch",
    "v2 local fetching missing ranges",
    "starting v2-aware candle preparation",
)

#: Substring that identifies an HSL panic close in the fill ledger. The order type is
#: `ClosePanicLong`/`ClosePanicShort` in the engine; the ledger carries its snake-case form.
PANIC_FILL_MARKER = "panic"


class Variant:
    """One replay arm: its config, its bundle, and the flag it declares."""

    def __init__(
        self,
        key: str,
        *,
        label: str,
        hsl_enabled: bool,
        description: str,
    ) -> None:
        self.key = key
        self.label = label
        self.hsl_enabled = hsl_enabled
        self.description = description
        self.bundle_dir = ARTIFACTS / key
        self.base_dir = self.bundle_dir / f"backtest_results/{DATA_EXCHANGE}_{label}"
        self.config_path = ARTIFACTS / f"g4_sma20_50_{key}.config.json"
        self.execution_audit_path = self.bundle_dir / "execution_audit.csv"
        self.log_dir = self.bundle_dir / "logs"
        self.replay_log_path = self.log_dir / "run.log"

    @property
    def runs_base(self) -> Path:
        """`<base_dir>/<exchange>/`, where the backtest writes `<UTC timestamp>/`."""
        return self.base_dir / DATA_EXCHANGE

    def relative(self, path: Path | None = None) -> str:
        return relative(path if path is not None else self.bundle_dir)


VARIANTS: tuple[Variant, ...] = (
    Variant(
        "hsl_off_control",
        label="control",
        hsl_enabled=False,
        description=(
            "the parent g4 config re-run on the current engine: isolates engine drift from "
            "the HSL change"
        ),
    ),
    Variant(
        "hsl_on",
        label="hsl_on",
        hsl_enabled=True,
        description="the parent g4 config with bot.long.hsl.enabled flipped to true",
    ),
)
VARIANTS_BY_KEY = {variant.key: variant for variant in VARIANTS}
DEFAULT_VARIANT_ORDER = tuple(variant.key for variant in VARIANTS)

#: Analysis metrics the three-column comparison reports, grouped by the question they answer.
COMPARISON_METRIC_GROUPS: dict[str, tuple[str, ...]] = {
    "收益": (
        "gain_strategy_eq",
        "adg_strategy_eq",
        "mdg_strategy_eq",
    ),
    "回撤与恢复": (
        "drawdown_worst_strategy_eq",
        "drawdown_worst_mean_1pct_strategy_eq",
        "strategy_eq_recovery_days_max",
        "peak_recovery_days_strategy_eq_long",
    ),
    "风险调整": (
        "sortino_ratio_strategy_eq",
        "sharpe_ratio_strategy_eq",
        "loss_profit_ratio",
    ),
    "交易结构": (
        "fills_count",
        "fills_active_symbols_count",
        "fills_count_entry",
        "fills_count_close",
        "fills_gap_longest_days",
        "total_wallet_exposure_max",
        "total_wallet_exposure_mean",
        "position_held_days_max",
    ),
}
#: HSL runtime telemetry, reported for its own sake (all zero when HSL never triggers).
HSL_METRICS: tuple[str, ...] = (
    "hard_stop_triggers",
    "hard_stop_triggers_long",
    "hard_stop_triggers_per_year",
    "hard_stop_restarts",
    "hard_stop_restarts_long",
    "hard_stop_time_in_yellow_pct",
    "hard_stop_time_in_orange_pct",
    "hard_stop_time_in_red_pct",
    "hard_stop_duration_minutes_mean",
    "hard_stop_duration_minutes_max",
    "hard_stop_flatten_time_minutes_mean",
    "hard_stop_trigger_drawdown_mean",
    "hard_stop_panic_close_loss_sum",
    "hard_stop_panic_close_loss_max",
    "hard_stop_panic_close_loss_drawdown_pct_min",
    "hard_stop_panic_close_loss_drawdown_pct_mean",
    "hard_stop_panic_close_loss_drawdown_pct_max",
    "hard_stop_post_restart_retrigger_pct",
    "hard_stop_halt_to_restart_equity_loss_pct",
)
COMPARISON_METRIC_KEYS = tuple(
    key for group in COMPARISON_METRIC_GROUPS.values() for key in group
)

REFERENCE_REPORT = "backtests/binance/2026-09-14T03_25_14/annual_analysis.md"
REPORT_CONVENTION = "docs/ai/runbooks/strategy_report.md"


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
    node = config
    for part in parts[:-1]:
        node = node[part]
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


def diff_subtrees(
    left: Any, right: Any, root: str, *, allowed: tuple[str, ...] = ()
) -> list[str]:
    """Differences between two subtrees, ignoring the paths this study declares."""
    return [
        problem
        for problem in compare_subtrees(left, right, root)
        if not problem.startswith(allowed)
    ]


def normalize_config_payload(config: dict[str, Any]) -> dict[str, Any]:
    """Run a config through the same hydration/cleaning the backtest applies before dumping."""
    import sys

    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from config_utils import sanitize_prepared_config_for_dump

    return sanitize_prepared_config_for_dump(config)


def gate_semantics(gate: dict[str, Any] | None) -> dict[str, Any]:
    """The gate declaration without applier-internal wiring (`approved` is derived)."""
    if not isinstance(gate, dict):
        return {}
    defaults = {
        "enabled": False,
        "sma_fast_days": None,
        "sma_slow_days": None,
        "confirm_days": 0,
        "block_initial": True,
        "block_reentry": True,
        "gate_mode": "both",
    }
    return {key: gate.get(key, defaults[key]) for key in GATE}


def find_variant_run_dir(variant: "Variant", explicit: str | None = None) -> Path:
    """The single run directory inside one variant's bundle.

    The backtest writes `<base_dir>/<exchange>/<UTC timestamp>/`, and the study keeps one
    completed run per bundle, so anything other than exactly one run is an error rather
    than a guess.
    """
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_dir():
            candidate = variant.runs_base / str(explicit)
        if not candidate.is_dir():
            raise SystemExit(f"{explicit} is not a directory")
        return candidate
    runs = sorted(
        path
        for path in variant.runs_base.iterdir()
        if path.is_dir() and path.name[:2].isdigit()
    )
    if len(runs) != 1:
        raise SystemExit(
            f"{relative(variant.runs_base)}: expected exactly one run directory, "
            f"found {[path.name for path in runs]}"
        )
    return runs[0]