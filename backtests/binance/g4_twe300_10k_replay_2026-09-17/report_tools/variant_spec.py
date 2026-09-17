#!/usr/bin/env python3
"""Single source of truth for the g4 @ TWE 3.0 / 10,000 USDT replay.

The study re-runs the published gated g4 profile with two declared production changes —
`total_wallet_exposure_limit: 1.0 -> 3.0` and `backtest.starting_balance: 100000 -> 10000` —
on two frozen legs, and measures what that does to the account's tail:

1. how deep does the drawdown go, and does the account survive the local history at all
   (the engine ends the run at its liquidation floor: `starting_balance * liquidation_threshold`)?
2. what does a hard floor (`unified` HSL at `red_threshold=0.10`, plus one terminal arm) buy,
   and what does it cost?
3. what changes from the 10x smaller starting balance alone (scale control), and what is the
   structural alternative (`we_excess_allowance_pct=0`)?

Every arm is declared here *before* it is run. An arm is the frozen parent g4 profile config
plus an explicit list of `(dotted path, from, to)` changes; `build_variant_config` asserts
that nothing else differs and `run_variant.py` asserts the run dump carried the same
declaration.

Two real legs are served by two frozen HLCV bundles:

* `3y`  — the profile's native window/dataset (2023-09-12 -> 2026-09-12),
* `ext` — the long local history (2021-04-20 -> 2026-09-13) used as an out-of-sample
  stress leg; the earliest months are gate-warmup degenerate and the coin universe is
  narrower, so the leg is evidence about regimes, not about the published window.

Offline only: no network access, no credentials, no exchange account, no bot start.
Every path and every pinned constant the other tools need lives here.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# <repo>/backtests/binance/<study>/report_tools/variant_spec.py
REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_twe300_10k_replay_2026-09-17"
ARTIFACTS = STUDY / "artifacts"

#: The parent study whose frozen config is every arm's source of truth.
PARENT_STUDY = REPO / "backtests/binance/g4_sma20_50_replay_2026-09-16"
SOURCE_CONFIG = PARENT_STUDY / "artifacts/g4_sma20_50.config.json"
SOURCE_CONFIG_SHA256 = "5c8ab5edad9b571cab32a91e82013268ad99ff746a49131cb37bf225b2908bd9"
SOURCE_PROFILE_INPUT = PARENT_STUDY / "artifacts/profile_input.json"
SOURCE_PROFILE_INPUT_SHA256 = "1e6e8c61811e303297e1e72365fd5dad48a05aaddf42c75b8f89265eaf8778ce"

#: The tail-risk study measured the same profile at the published 100k / TWE 1.0 settings on
#: the long leg; its run is pinned here as this study's long-leg reference column.
TAIL_RISK_STUDY = REPO / "backtests/binance/g4_tail_risk_research_2026-09-17"
PARENT_BASELINE_RUN = PARENT_STUDY / "artifacts/backtest_results/binance/2026-09-16T11_45_49"
PARENT_BASELINE_ANALYSIS_SHA256 = (
    "295485faa423d6840decf7d7d1e9e9d17352f794a30d68e0ae704e1519d1c3db"
)
TAIL_RISK_OFF_EXT_RUN = (
    TAIL_RISK_STUDY
    / "artifacts/off__ext/backtest_results/binance_off__ext/binance/2026-09-17T06_40_45"
)
TAIL_RISK_OFF_EXT_ANALYSIS_SHA256 = (
    "4c15d68890600ebd96f3fafa32ad066a91fcb92866b2dcdb549f070bfc939ac6"
)

#: Reference arms that are *not* re-run by this study: their tracked analysis is pinned.
REFERENCE_RUNS: dict[str, dict[str, Any]] = {
    "ref_published_3y": {
        "label": "published g4 profile, 100k / TWE 1.0, 3y",
        "run_dir": PARENT_BASELINE_RUN,
        "analysis_sha256": PARENT_BASELINE_ANALYSIS_SHA256,
        "leg": "3y",
        "role": "the published profile's own tracked evidence (100,000 USDT, TWE 1.0)",
    },
    "ref_tailrisk_off_ext": {
        "label": "published g4 profile, 100k / TWE 1.0, 5.4y",
        "run_dir": TAIL_RISK_OFF_EXT_RUN,
        "analysis_sha256": TAIL_RISK_OFF_EXT_ANALYSIS_SHA256,
        "leg": "ext",
        "role": "the tail-risk study's 5.4-year control at the published settings",
    },
}

VARIANT_INPUT_PATH = ARTIFACTS / "variant_input.json"

DATA_EXCHANGE = "binance"
COIN_COUNT = 40

#: The capital and liquidation contract of this study. `backtest.starting_balance` initialises
#: the simulated account (`passivbot-rust/src/backtest.rs`: `balance.usd_cash_wallet =
#: starting_balance`), and the engine ends a run once equity reaches
#: `starting_balance * liquidation_threshold` (`check_and_apply_liquidation`, same file).
PARENT_STARTING_BALANCE = 100000
ARM_STARTING_BALANCE = 10000
STARTING_BALANCE_PATH = "backtest.starting_balance"
LIQUIDATION_THRESHOLD_PATH = "backtest.liquidation_threshold"
PARENT_LIQUIDATION_THRESHOLD = 0.05

#: Reported execution and cost contract, identical to the parent study.
EXECUTION = {"execution_delay_bars": 0, "intrabar_fill_order": "close_first"}
COSTS = {"maker_fee_override": 0.0002, "taker_fee_override": 0.0005}

#: The entry-regime gate the parent config declares; every arm must keep it exactly.
GATE = {
    "enabled": True,
    "sma_fast_days": 20,
    "sma_slow_days": 50,
    "confirm_days": 0,
    "block_initial": True,
    "block_reentry": True,
    "gate_mode": "both",
}

#: Top-level config subtrees that must match the parent config leaf for leaf except for the
#: paths an arm declares.
IDENTITY_ROOTS = ("bot", "live", "coin_overrides", "monitor", "logging")

#: Config paths the freeze step is allowed to retarget for every arm (output location only).
ALLOWED_CONFIG_DIFF_PATHS = ("backtest.base_dir",)
#: `backtest` paths an arm may change *by declaration* (the study's experimental variables).
#: The identity gate allows these on top of the output-location retarget.
DECLARED_BACKTEST_PATHS = (STARTING_BALANCE_PATH,)
#: Additional paths an *override* leg (dataset from another frozen bundle) retargets.
ALLOWED_DATASET_DIFF_PATHS = (
    *ALLOWED_CONFIG_DIFF_PATHS,
    "backtest.cache_dir.binance",
    "backtest.hlcvs_data_dir",
    "backtest.hlcvs_data_override_mode",
    "backtest.start_date",
    "backtest.end_date",
)
#: Plot groups every arm drops through the *runtime* flag `--disable_plotting`: with the local
#: host at ~7.8 GB of memory the per-coin panels are a run's memory peak (a 3-year arm peaked
#: near 6.8 GB with them and 4.2 GB without), and the report convention allows dropping a
#: figure group as long as the report states which groups it dropped. The value is a CLI flag
#: in this engine revision (the config key is overwritten with False at load time), so the
#: frozen config keeps the parent's value and the flag is recorded as a runtime flag in
#: `variant_input.json`, `run_record.json` and `global_metrics.json`.
DISABLED_PLOT_GROUPS = ("coin_fills",)
RUNTIME_FLAGS: tuple[str, ...] = ("--disable_plotting", "coin_fills")
#: Additional paths the *run dump* may differ by: the CLI supplies the audit path, the runtime
#: plot-group flag, and the dumped config clears the resolved dataset fields, so the dataset
#: window is compared by day rather than by string.
ALLOWED_RUN_DUMP_DIFF_PATHS = (
    *ALLOWED_DATASET_DIFF_PATHS,
    "backtest.disable_plotting",
    "backtest.execution_audit_path",
    "live.base_config_path",
)
#: Keys the backtest deliberately clears before dumping a run's config.
DUMP_CLEARED_BACKTEST_KEYS = ("coins", "cache_dir", "hlcvs_data_dir")

#: Markers that prove a run resolved the frozen bundle instead of building a new one.
NETWORK_FETCH_MARKERS = (
    "download ccxt",
    "Binance daily archive fetch",
    "v2 local fetching missing ranges",
    "starting v2-aware candle preparation",
)

#: Substring that identifies an HSL panic close in the fill ledger.
PANIC_FILL_MARKER = "panic"

#: The parent profile's HSL block. An arm may change declared fields of it; the rest must
#: stay the parent profile's own values.
HSL_BLOCK = {
    "enabled": False,
    "red_threshold": 0.15,
    "ema_span_minutes": 720.0,
    "cooldown_minutes_after_red": 2160.0,
    "no_restart_drawdown_threshold": 1,
    "restart_after_red_policy": "threshold",
    "orange_tier_mode": "tp_only_with_active_entry_cancellation",
    "panic_close_order_type": "limit",
    "tier_ratios": {"yellow": 0.5, "orange": 0.75},
}
#: Live HSL surface the backtest also consumes. `live.hsl_signal_mode` is *modelled*: the
#: backtest resolves it (`src/backtest.py::_resolve_backtest_hsl_signal_mode`) and passes it
#: to the engine as the HSL scope, so an arm that changes it is a real variant.
LIVE_HSL = {"hsl_signal_mode": "coin", "hsl_position_during_cooldown_policy": "panic"}
MODELLED_LIVE_HSL_KEYS = ("hsl_signal_mode",)
CONTEXT_LIVE_HSL_KEYS = ("hsl_position_during_cooldown_policy",)

#: Parent risk block values an arm may move. Asserted so a drift in the parent is loud.
PARENT_RISK = {
    "n_positions": 7,
    "total_wallet_exposure_limit": 1.0,
    "we_excess_allowance_pct": 0.37,
    "we_excess_allowance_mode": "bounded",
    "position_exposure_enforcer_enabled": False,
    "position_exposure_enforcer_threshold": 1.0,
    "total_exposure_enforcer_enabled": False,
    "total_exposure_enforcer_policy": "reduce_overweight",
    "total_exposure_enforcer_threshold": 1.0,
    "total_exposure_entry_gate_enabled": True,
    "wallet_exposure_brake_enabled": False,
    "entry_cooldown_minutes": 24.1,
}
PARENT_MAX_REALIZED_LOSS_PCT = 1


@dataclass(frozen=True)
class DatasetSpec:
    """One frozen HLCV bundle an arm may be served by."""

    key: str
    leg: str
    rel_path: Path
    window: tuple[str, str]
    manifest_sha256: str
    manifest_config_hash: str
    override_mode: str | None
    synthetic: bool = False
    note: str = ""

    @property
    def path(self) -> Path:
        return REPO / self.rel_path


DATASETS: dict[str, DatasetSpec] = {
    "3y": DatasetSpec(
        key="3y",
        leg="3y",
        rel_path=Path(
            "caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__8300950b42789a26"
        ),
        window=("2023-09-12", "2026-09-12"),
        manifest_sha256="2c300e499577c2c75fac1009656780705854f1d7f90a7ffbf51cf657feca7366",
        manifest_config_hash="8300950b42789a26485595e34e0b106e7d1a084ad887730c53693a2453121792",
        override_mode=None,
        note="the profile's native bundle",
    ),
    "ext": DatasetSpec(
        key="ext",
        leg="ext",
        rel_path=Path(
            "caches/hlcvs_data/binance__40_coins__2021-03-25_to_2026-09-13__a02b6ae1c140f2b7"
        ),
        window=("2021-04-20", "2026-09-13"),
        manifest_sha256="d7e3b9756be4a0a2460b148de3753130f07f1840ac29d0403409d845fad32f8c",
        manifest_config_hash="a02b6ae1c140f2b77c92d88e4292426dd3e8d4c0c287f2671cd3c9668d639483",
        override_mode="dataset",
        note="dataset override serves the bundle's own requested window",
    ),
}

FROZEN_CACHE_FILES = (
    "manifest.json",
    "hlcvs.npy.gz",
    "timestamps.npy.gz",
    "btc_usd_prices.npy.gz",
    "coins.json",
    "market_specific_settings.json",
)

#: Impact levels (fraction of the position value lost) used by the wipe-out matrix.
SHOCK_LEVELS = (0.2, 0.4, 0.6, 0.8, 1.0)

#: Episode detection rule for the event table: a benchmark crash is a 7-day log return at or
#: below `threshold`, episodes closer than `min_gap_days` merge into one, and an episode ends
#: when the arm's own strategy equity regains its pre-episode peak (capped at window end).
EVENT_RULE = {
    "benchmark": "BTC",
    "lookback_days": 7,
    "threshold": -0.15,
    "min_gap_days": 30,
}
#: Human-readable labels for the episodes the rule is expected to find. Matching is by
#: overlap with the labelled window; an unlabelled episode is still reported.
EVENT_LABELS = (
    ("2021-05 中国挖矿禁令/五月崩盘（闸门预热期，仅参考）", "2021-05-01", "2021-06-15"),
    ("2022-05 LUNA/UST 崩盘与三箭传染", "2022-05-01", "2022-07-05"),
    ("2022-11 FTX 崩盘", "2022-11-01", "2023-01-10"),
    ("2023-08 基线起点回撤", "2023-08-15", "2023-10-31"),
    ("2024-08 日元套息平仓", "2024-07-25", "2024-09-15"),
    ("2025-02 关税冲击与山寨下跌", "2025-01-25", "2025-05-05"),
    ("2025-10 闪崩（历史最大清算）", "2025-10-01", "2025-11-20"),
    ("2026 近端窗口", "2026-05-15", "2026-09-13"),
)


@dataclass(frozen=True)
class Lever:
    """One risk lever under test, with the question it answers."""

    key: str
    label: str
    question: str
    hypothesis: str


LEVERS: dict[str, Lever] = {
    "twe100_10k": Lever(
        key="twe100_10k",
        label="10,000 USDT / TWE 1.0（规模对照）",
        question="把起始资金从 100k 降到 10k、其余不动，成绩会变多少？",
        hypothesis="仓位按余额等比缩放，差异只应来自最小下单量/步长取整与费用占比",
    ),
    "twe300_10k": Lever(
        key="twe300_10k",
        label="10,000 USDT / TWE 3.0（本次请求）",
        question="三倍总暴露在本地历史上会不会被强平？",
        hypothesis="单槽上界升到 0.5871；原生窗可能只表现为更深的回撤，历史压力腿很可能触发强平",
    ),
    "twe300_10k_floor": Lever(
        key="twe300_10k_floor",
        label="TWE 3.0 + unified red=0.10（硬底线）",
        question="账户级熔断能否在 TWE 3.0 下封住尾部、代价多少？",
        hypothesis="满仓时约 −3.3% 行情即触发，能显著压低最差回撤，但频繁停牌会切掉复利",
    ),
    "twe300_10k_allowance0": Lever(
        key="twe300_10k_allowance0",
        label="TWE 3.0 + we_excess_allowance_pct=0",
        question="不加熔断、只削单槽超额，能否更便宜地压低尾部？",
        hypothesis="单币上界 0.5871→0.4286，尾部降低但代价小于熔断",
    ),
    "twe300_10k_never": Lever(
        key="twe300_10k_never",
        label="TWE 3.0 + unified red=0.10 + restart=never（终止式）",
        question="把硬底线做成永久停机，是否值得？",
        hypothesis="一次触发即永久离线，上限最清晰、复利代价最大",
    ),
}


@dataclass(frozen=True)
class Variant:
    """One replay arm: its config, its dataset, and the changes it declares."""

    key: str
    lever: str
    dataset_key: str
    deltas: tuple[tuple[str, Any, Any], ...]
    description: str
    stage: str = "a"

    @property
    def dataset(self) -> DatasetSpec:
        return DATASETS[self.dataset_key]

    @property
    def leg(self) -> str:
        return self.dataset.leg

    @property
    def starting_balance(self) -> float:
        """The arm's declared starting capital (the study's scale variable)."""
        return float(self.declared(STARTING_BALANCE_PATH, PARENT_STARTING_BALANCE))

    @property
    def synthetic(self) -> bool:
        """No arm of this study uses a synthetic dataset; kept so the shared tooling is uniform."""
        return bool(self.dataset.synthetic)

    @property
    def declared_twe(self) -> float:
        """The arm's declared total wallet exposure limit (the study's leverage variable)."""
        return float(self.declared("bot.long.risk.total_wallet_exposure_limit", 1.0))

    @property
    def declared_allowance_pct(self) -> float:
        return float(self.declared("bot.long.risk.we_excess_allowance_pct", 0.37))

    @property
    def declared_n_positions(self) -> float:
        return float(self.declared("bot.long.risk.n_positions", 7))

    def declared(self, dotted: str, default: Any) -> Any:
        for path, _from, to in self.deltas:
            if path == dotted:
                return to
        return default

    @property
    def per_slot_cap(self) -> float:
        """The slot budget with the excess allowance, as the engine computes it."""
        return self.declared_twe / self.declared_n_positions * (1.0 + self.declared_allowance_pct)

    @property
    def hsl_enabled(self) -> bool:
        for dotted, _from, to in self.deltas:
            if dotted == "bot.long.hsl.enabled":
                return bool(to)
        return bool(HSL_BLOCK["enabled"])

    @property
    def bundle_dir(self) -> Path:
        return ARTIFACTS / self.key

    @property
    def base_dir(self) -> Path:
        return self.bundle_dir / f"backtest_results/{DATA_EXCHANGE}_{self.key}"

    @property
    def config_path(self) -> Path:
        return ARTIFACTS / f"g4_{self.key}.config.json"

    @property
    def execution_audit_path(self) -> Path:
        return self.bundle_dir / "execution_audit.csv"

    @property
    def log_dir(self) -> Path:
        return self.bundle_dir / "logs"

    @property
    def replay_log_path(self) -> Path:
        return self.log_dir / "run.log"

    @property
    def runs_base(self) -> Path:
        """`<base_dir>/<exchange>/`, where the backtest writes `<UTC timestamp>/`."""
        return self.base_dir / DATA_EXCHANGE

    def relative(self, path: Path | None = None) -> str:
        return relative(path if path is not None else self.bundle_dir)


def _arm(
    lever: str,
    dataset_key: str,
    deltas: tuple[tuple[str, Any, Any], ...],
    description: str,
) -> Variant:
    return Variant(
        key=f"{lever}__{dataset_key}",
        lever=lever,
        dataset_key=dataset_key,
        deltas=deltas,
        description=description,
    )


_HSL_ON: tuple[tuple[str, Any, Any], ...] = (("bot.long.hsl.enabled", False, True),)
_UNIFIED: tuple[tuple[str, Any, Any], ...] = (
    *_HSL_ON,
    ("live.hsl_signal_mode", "coin", "unified"),
)
_UNIFIED_R10: tuple[tuple[str, Any, Any], ...] = (
    *_UNIFIED,
    ("bot.long.hsl.red_threshold", 0.15, 0.10),
)
#: The two declared production changes this study is about.
_10K: tuple[tuple[str, Any, Any], ...] = (
    (STARTING_BALANCE_PATH, PARENT_STARTING_BALANCE, ARM_STARTING_BALANCE),
)
_TWE300: tuple[tuple[str, Any, Any], ...] = (
    *_10K,
    ("bot.long.risk.total_wallet_exposure_limit", 1.0, 3.0),
)
_ALLOWANCE0: tuple[tuple[str, Any, Any], ...] = (
    *_TWE300,
    ("bot.long.risk.we_excess_allowance_pct", 0.37, 0.0),
)

_ARM_SPECS: tuple[tuple[str, str, tuple[tuple[str, Any, Any], ...], str], ...] = (
    ("twe100_10k", "3y", _10K, "规模对照：10,000 USDT + 发布参数（TWE 1.0，HSL 关）"),
    ("twe300_10k", "3y", _TWE300, "本次请求：10,000 USDT + TWE 3.0（HSL 关）"),
    (
        "twe300_10k_floor",
        "3y",
        (*_TWE300, *_UNIFIED_R10),
        "TWE 3.0 + 硬底线：HSL unified、red_threshold=0.10",
    ),
    (
        "twe300_10k_allowance0",
        "3y",
        _ALLOWANCE0,
        "TWE 3.0 + 取消单槽超额（单币上界 0.5871→0.4286）",
    ),
    ("twe100_10k", "ext", _10K, "规模对照：5.4 年历史压力腿"),
    ("twe300_10k", "ext", _TWE300, "TWE 3.0：5.4 年历史压力腿"),
    (
        "twe300_10k_floor",
        "ext",
        (*_TWE300, *_UNIFIED_R10),
        "TWE 3.0 + unified red=0.10：5.4 年历史压力腿",
    ),
    (
        "twe300_10k_allowance0",
        "ext",
        _ALLOWANCE0,
        "TWE 3.0 + we_excess_allowance_pct=0：5.4 年历史压力腿",
    ),
    (
        "twe300_10k_never",
        "ext",
        (*_TWE300, *_UNIFIED_R10, ("bot.long.hsl.restart_after_red_policy", "threshold", "never")),
        "TWE 3.0 + 硬底线且触发后永久停止：5.4 年历史压力腿",
    ),
)

VARIANTS: tuple[Variant, ...] = tuple(
    _arm(lever, dataset_key, deltas, description)
    for lever, dataset_key, deltas, description in _ARM_SPECS
)
VARIANTS_BY_KEY: dict[str, Variant] = {variant.key: variant for variant in VARIANTS}
DEFAULT_VARIANT_ORDER = tuple(variant.key for variant in VARIANTS)
#: Every declared arm is run by this study; the two reference anchors are pinned evidence.
REUSED_ARM_KEYS: tuple[str, ...] = ()
RUN_VARIANT_ORDER = tuple(DEFAULT_VARIANT_ORDER)


def variant_legs() -> dict[str, tuple[str, ...]]:
    """Arm keys grouped by dataset leg, in declaration order."""
    legs: dict[str, list[str]] = {}
    for variant in VARIANTS:
        legs.setdefault(variant.leg, []).append(variant.key)
    return {leg: tuple(keys) for leg, keys in legs.items()}


def liquidation_floor_usd(variant: "Variant", *, threshold: float | None = None) -> float:
    """The equity level at which the engine ends the run (`starting_balance * threshold`)."""
    if threshold is None:
        threshold = PARENT_LIQUIDATION_THRESHOLD
    return float(variant.starting_balance) * float(threshold)


def liquidation_shock(
    *, peak_exposure: float, balance_usd: float, starting_balance: float, threshold: float
) -> float | None:
    """The adverse price move that takes equity to the engine's liquidation floor.

    At the observed peak exposure `e` (position value / balance) an adverse move `r` costs
    `e * |r| * balance`; the run ends when equity reaches `starting_balance * threshold`.
    Returns None when the account is already at or below the floor, or when there is no
    exposure to lose.
    """
    if peak_exposure <= 0.0 or not math.isfinite(balance_usd) or balance_usd <= 0.0:
        return None
    floor = float(starting_balance) * float(threshold)
    room = balance_usd - floor
    if room <= 0.0:
        return None
    return room / (peak_exposure * balance_usd)


#: Metric groups the multi-arm comparison reports, grouped by the question they answer.
COMPARISON_METRIC_GROUPS: dict[str, tuple[str, ...]] = {
    "收益": ("gain_strategy_eq", "adg_strategy_eq", "mdg_strategy_eq"),
    "回撤与恢复": (
        "drawdown_worst_strategy_eq",
        "drawdown_worst_mean_1pct_strategy_eq",
        "strategy_eq_recovery_days_max",
        "peak_recovery_days_strategy_eq",
        "strategy_eq_underwater_pct_mean",
    ),
    "尾部形状": (
        "expected_shortfall_1pct_strategy_eq",
        "omega_ratio_strategy_eq",
        "sterling_ratio_strategy_eq",
        "loss_profit_ratio",
    ),
    "暴露与结构": (
        "total_wallet_exposure_max",
        "total_wallet_exposure_mean",
        "exposure_mean_ratio_usd",
        "high_exposure_days_max_long",
        "fills_count",
        "fills_count_close",
        "fills_active_symbols_count",
    ),
    "生存": ("liquidated", "n_days", "backtest_completion_ratio"),
}
COMPARISON_METRIC_KEYS = tuple(
    key for group in COMPARISON_METRIC_GROUPS.values() for key in group
)

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

#: Per-coin artifacts the tail analysis reads (local, regenerable).
COIN_METRICS_FILENAME = "coin_metrics.csv"
EQUITY_FILENAME = "balance_and_equity.csv.gz"
FILLS_FILENAME = "fills.csv"

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
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


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


def day_of(value: Any) -> str:
    """The UTC day a config date carries, tolerant of ISO-with-time spellings."""
    return str(value)[:10]


def disabled_plot_groups(value: Any) -> set[str]:
    """Normalize a `disable_plotting` value (config-style list or CLI-style string) to tokens."""
    if value in (None, False, "", [], ()):
        return set()
    if value is True:
        return {"all", "summary", "balance", "twe", "pnl", "hard_stop", "coin_fills"}
    if isinstance(value, str):
        tokens = [token.strip().lower() for token in value.split(",") if token.strip()]
    elif isinstance(value, (list, tuple, set)):
        tokens = [str(token).strip().lower() for token in value if str(token).strip()]
    else:
        raise ValueError(f"invalid disable_plotting value type: {type(value).__name__}")
    out: set[str] = set()
    for token in tokens:
        if token in {"true", "all", "y", "yes", "1"}:
            out.update({"balance", "twe", "pnl", "hard_stop", "coin_fills", "all"})
        elif token == "summary":
            out.update({"balance", "twe", "pnl", "hard_stop"})
        else:
            out.add(token)
    return out


def runtime_flags(variant: "Variant") -> tuple[str, ...]:
    """Extra CLI flags an arm needs beyond the frozen config (currently plot groups only)."""
    return RUNTIME_FLAGS


def declared_plot_groups(variant: "Variant") -> set[str]:
    return set(DISABLED_PLOT_GROUPS)


def same_day(left: Any, right: Any) -> bool:
    return day_of(left) == day_of(right)


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


def diff_run_config_root(
    expected: Any, actual: Any, root: str, *, declared: tuple[str, ...] = ()
) -> list[str]:
    """Diff one root of a frozen config against the run's dumped config.

    Hydration adds engine defaults the frozen profile does not carry (websocket candles,
    risk-input retries, the CLI's own config path). Every ``live`` key this study declares is
    asserted explicitly by the callers, so an *added* live key is not a difference in the arm;
    a changed one still is.
    """
    allowed = (*ALLOWED_RUN_DUMP_DIFF_PATHS, *declared)
    found = diff_subtrees(expected, actual, root, allowed=allowed)
    if root == "live":
        found = [problem for problem in found if "unexpected in candidate" not in problem]
    return found


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


def expected_hsl_block(variant: "Variant") -> dict[str, Any]:
    """The HSL block this arm must carry: the parent profile's values plus its deltas."""
    block = json.loads(json.dumps(HSL_BLOCK))
    for dotted, _from, to in variant.deltas:
        if dotted.startswith("bot.long.hsl."):
            set_path(block, dotted[len("bot.long.hsl.") :], to)
    return block


def expected_live_hsl(variant: "Variant") -> dict[str, Any]:
    live = dict(LIVE_HSL)
    for dotted, _from, to in variant.deltas:
        short = dotted[len("live.") :]
        if dotted.startswith("live.") and short in MODELLED_LIVE_HSL_KEYS:
            live[short] = to
    return live


def expected_live_config(variant: "Variant") -> dict[str, Any]:
    """`live.*` values an arm declares outside the modelled HSL surface."""
    live: dict[str, Any] = {}
    for dotted, _from, to in variant.deltas:
        short = dotted[len("live.") :]
        if dotted.startswith("live.") and short not in MODELLED_LIVE_HSL_KEYS:
            live[short] = to
    return live


def expected_risk_block(variant: "Variant") -> dict[str, Any]:
    risk = dict(PARENT_RISK)
    for dotted, _from, to in variant.deltas:
        if dotted.startswith("bot.long.risk."):
            risk[dotted[len("bot.long.risk.") :]] = to
    return risk


def declared_backtest_paths(variant: "Variant") -> tuple[str, ...]:
    """`backtest.*` paths this arm changes *by declaration* (its experimental variables)."""
    return tuple(dotted for dotted, _from, _to in variant.deltas if dotted.startswith("backtest."))


def expected_backtest_values(variant: "Variant") -> dict[str, Any]:
    """The declared `backtest.*` values an arm must carry (path -> declared value)."""
    return {dotted: to for dotted, _from, to in variant.deltas if dotted.startswith("backtest.")}


def allowed_backtest_paths(variant: "Variant") -> tuple[str, ...]:
    """Output-location retargets plus this arm's own declared `backtest` changes."""
    base = ALLOWED_DATASET_DIFF_PATHS if variant.dataset.override_mode else ALLOWED_CONFIG_DIFF_PATHS
    return (*base, *declared_backtest_paths(variant))


def find_variant_run_dir(variant: "Variant", explicit: str | None = None) -> Path:
    """The single run directory inside one arm's bundle.

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
    if not variant.runs_base.is_dir():
        raise SystemExit(
            f"{relative(variant.runs_base)}: no run directory yet; run run_variant.py first"
        )
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


def reference_run_dirs() -> dict[str, Path]:
    return {key: Path(item["run_dir"]) for key, item in REFERENCE_RUNS.items()}
