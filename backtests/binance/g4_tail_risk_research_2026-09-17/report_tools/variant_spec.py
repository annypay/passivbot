#!/usr/bin/env python3
"""Single source of truth for the g4 tail-risk and wipe-out research.

The study answers three questions with replayable evidence on frozen datasets:

1. does the published g4 config survive the worst regimes the local history contains?
2. which *existing* risk lever actually bounds the tail, and at what cost in return —
   the HSL scopes (`coin` / `pside` / `unified`), structural de-levering, the exposure
   enforcers, or the realized-loss gate?
3. what is the account's loss when one coin, three coins, or the whole loaded basket go
   to zero, and what does the protection cost?

Every arm is declared here *before* it is run. An arm is the frozen parent g4 profile
config plus an explicit list of `(dotted path, from, to)` changes; `build_variant_config`
asserts that nothing else differs and `run_variant.py` asserts the run dump carried the
same declaration.

Two real legs are served by two frozen HLCV bundles:

* `3y`  — the profile's native window/dataset (2023-09-12 -> 2026-09-12),
* `ext` — the long local history (2021-04-20 -> 2026-09-13) used as an out-of-sample
  stress leg; the earliest months are gate-warmup degenerate and the coin universe is
  narrower, so the leg is evidence about regimes, not about the published window.

The synthetic arms additionally patch one or three coins' price paths inside a derived
copy of the native bundle (see `make_synthetic_collapse_bundle.py`); they are labelled
synthetic everywhere and never mixed into a real-data conclusion.

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
STUDY = REPO / "backtests/binance/g4_tail_risk_research_2026-09-17"
ARTIFACTS = STUDY / "artifacts"

#: The parent study whose frozen config is every arm's source of truth.
PARENT_STUDY = REPO / "backtests/binance/g4_sma20_50_replay_2026-09-16"
SOURCE_CONFIG = PARENT_STUDY / "artifacts/g4_sma20_50.config.json"
SOURCE_CONFIG_SHA256 = "5c8ab5edad9b571cab32a91e82013268ad99ff746a49131cb37bf225b2908bd9"
SOURCE_PROFILE_INPUT = PARENT_STUDY / "artifacts/profile_input.json"
SOURCE_PROFILE_INPUT_SHA256 = "1e6e8c61811e303297e1e72365fd5dad48a05aaddf42c75b8f89265eaf8778ce"

#: The previous study replayed the same parent config on the current engine. Its control
#: reproduced the parent's tracked baseline byte for byte, so the two anchors below are
#: interchangeable evidence; both are pinned so a fresh checkout can compare against them.
PRIOR_STUDY = REPO / "backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17"
PARENT_BASELINE_RUN = PARENT_STUDY / "artifacts/backtest_results/binance/2026-09-16T11_45_49"
PARENT_BASELINE_ANALYSIS_SHA256 = (
    "295485faa423d6840decf7d7d1e9e9d17352f794a30d68e0ae704e1519d1c3db"
)
PRIOR_CONTROL_RUN = (
    PRIOR_STUDY
    / "artifacts/hsl_off_control/backtest_results/binance_control/binance/2026-09-17T04_46_11"
)
PRIOR_HSL_ON_RUN = (
    PRIOR_STUDY / "artifacts/hsl_on/backtest_results/binance_hsl_on/binance/2026-09-17T04_47_58"
)
PRIOR_HSL_ON_ANALYSIS_SHA256 = (
    "15f10f7056976e3635f4954565929aff29a703d20518f9ee99ebe830f1ecf5a7"
)

#: Reference arms that are *not* re-run by this study: their tracked analysis is pinned.
REFERENCE_RUNS: dict[str, dict[str, Any]] = {
    "ref_hsl_off_3y": {
        "label": "g4 profile, HSL off, 3y",
        "run_dir": PARENT_BASELINE_RUN,
        "analysis_sha256": PARENT_BASELINE_ANALYSIS_SHA256,
        "role": "the published profile's own tracked evidence (HSL off)",
    },
    "ref_hsl_coin_3y": {
        "label": "g4 profile, HSL coin, 3y",
        "run_dir": PRIOR_HSL_ON_RUN,
        "analysis_sha256": PRIOR_HSL_ON_ANALYSIS_SHA256,
        "role": "the previous study's HSL coin-mode replay on the current engine",
    },
}

VARIANT_INPUT_PATH = ARTIFACTS / "variant_input.json"
SYNTHETIC_BUNDLES_PATH = ARTIFACTS / "synthetic_bundles.json"

DATA_EXCHANGE = "binance"
COIN_COUNT = 40

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
    "synth_a": DatasetSpec(
        key="synth_a",
        leg="syn",
        rel_path=Path(
            "caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__synthetic_collapse_a"
        ),
        window=("2023-09-12", "2026-09-12"),
        manifest_sha256="",
        manifest_config_hash="",
        override_mode="dataset",
        synthetic=True,
        note="native bundle with the top-1 exposure coin collapsed (synthetic)",
    ),
    "synth_b": DatasetSpec(
        key="synth_b",
        leg="syn",
        rel_path=Path(
            "caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__synthetic_collapse_b"
        ),
        window=("2023-09-12", "2026-09-12"),
        manifest_sha256="",
        manifest_config_hash="",
        override_mode="dataset",
        synthetic=True,
        note="native bundle with the top-3 exposure coins collapsed (synthetic)",
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

#: Synthetic injection contract, executed by `make_synthetic_collapse_bundle.py`.
#: `targets` selects coins by the control run's `coin_metrics.csv`
#: (`max_abs_wallet_exposure_at_fill`, descending); the collapse starts at the minute the
#: coin reaches that maximum so the injected path always lands on a loaded position.
SYNTHETIC_INJECTIONS: dict[str, dict[str, Any]] = {
    "synth_a": {
        "label": "单币崩塌（合成 A，孤立事件）",
        "targets": 1,
        "collapse_days": 3,
        "end_multiple": 0.005,
        "hold_days": 14,
        "target_metrics_run": PARENT_BASELINE_RUN,
    },
    "synth_b": {
        "label": "三币先后崩塌（合成 B，每次以该币自身峰值敞口时点为起点）",
        "targets": 3,
        "collapse_days": 3,
        "end_multiple": 0.005,
        "hold_days": 14,
        "target_metrics_run": PARENT_BASELINE_RUN,
    },
}

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
    "off": Lever(
        key="off",
        label="HSL 关闭（控制）",
        question="基线收益与尾部损失是什么？",
        hypothesis="控制组；尾部上界由暴露峰值决定",
    ),
    "coin": Lever(
        key="coin",
        label="HSL coin（当前候选）",
        question="单币级熔断是否改变组合级尾部？",
        hypothesis="只砍单币，无法防组合级崩塌；代价 = 已实现 panic 亏损",
    ),
    "pside_r15": Lever(
        key="pside_r15",
        label="HSL pside red=0.15",
        question="组合级熔断在默认阈值下是否会介入？",
        hypothesis="阈值相对策略净值，原生窗最差 DD<0.15 ⇒ 休眠保险",
    ),
    "unified_r15": Lever(
        key="unified_r15",
        label="HSL unified red=0.15",
        question="账户级熔断与 pside 在单边配置下是否等价？",
        hypothesis="长仓单边下 pside 与 unified 行为接近",
    ),
    "unified_r10": Lever(
        key="unified_r10",
        label="HSL unified red=0.10",
        question="把阈值降到 0.10 能封住多少尾部、代价多大？",
        hypothesis="开始约束尾部；代价是更频繁的停牌与地板价平仓",
    ),
    "unified_r10_fast": Lever(
        key="unified_r10_fast",
        label="HSL unified red=0.10, EMA 120min",
        question="更快的熔断是否更有效，误触发是否更贵？",
        hypothesis="对持续性崩盘更快；对闪崩仍受 min(raw,EMA) 限制",
    ),
    "unified_r10_market": Lever(
        key="unified_r10_market",
        label="HSL unified red=0.10, panic=market",
        question="崩盘中限价砍不掉的现实成本是多少？",
        hypothesis="市价 panic 有滑点成本，但保证离场",
    ),
    "unified_r10_never": Lever(
        key="unified_r10_never",
        label="HSL unified red=0.10, restart=never",
        question="终止式开关（保命终点）代价几何？",
        hypothesis="一次触发即永久离线；上限清晰、复利代价最大",
    ),
    "twe_090": Lever(
        key="twe_090",
        label="结构性降杠杆 TWE 0.90",
        question="线性降杠杆换来多少尾部削减？",
        hypothesis="代价与收益近似线性，是任何叠层必须打败的基准",
    ),
    "allowance_000": Lever(
        key="allowance_000",
        label="取消超额允许 we_excess_allowance_pct=0",
        question="最小侵入地压低单币上界是否几乎免费？",
        hypothesis="单币上界 19.57%→14.29%，代价远小于 TWE 降杠杆",
    ),
    "twel_enforcer_070": Lever(
        key="twel_enforcer_070",
        label="组合暴露强制回收 threshold=0.70",
        question="崩盘中强制减仓是否优于不动？",
        hypothesis="硬刹车把下跌中的仓位强制实现亏损，预计劣于静态降杠杆",
    ),
    "max_realized_050": Lever(
        key="max_realized_050",
        label="实现亏损闸门 max_realized_loss_pct=0.50",
        question="阻止实现亏损是否等同于保命？",
        hypothesis="反而阻止 unstuck 减仓，把仓位钉在最大暴露上",
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
    def synthetic(self) -> bool:
        return self.dataset.synthetic

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
_PSIDE: tuple[tuple[str, Any, Any], ...] = (
    *_HSL_ON,
    ("live.hsl_signal_mode", "coin", "pside"),
)
_UNIFIED_R10: tuple[tuple[str, Any, Any], ...] = (
    *_UNIFIED,
    ("bot.long.hsl.red_threshold", 0.15, 0.10),
)

_ARM_SPECS: tuple[tuple[str, str, tuple[tuple[str, Any, Any], ...], str], ...] = (
    ("off", "3y", (), "HSL 关闭：原生窗口控制组（锚点复用，不重跑）"),
    ("coin", "3y", _HSL_ON, "HSL coin：上一轮研究的候选（锚点复用，不重跑）"),
    ("pside_r15", "3y", _PSIDE, "HSL 组合级（按边）熔断，red=0.15"),
    ("unified_r15", "3y", _UNIFIED, "HSL 账户级熔断，red=0.15"),
    ("unified_r10", "3y", _UNIFIED_R10, "HSL 账户级熔断，red=0.10"),
    (
        "unified_r10_fast",
        "3y",
        (*_UNIFIED_R10, ("bot.long.hsl.ema_span_minutes", 720.0, 120.0)),
        "HSL 账户级熔断 red=0.10，EMA 快速化到 120 分钟",
    ),
    (
        "unified_r10_market",
        "3y",
        (*_UNIFIED_R10, ("bot.long.hsl.panic_close_order_type", "limit", "market")),
        "HSL 账户级熔断 red=0.10，panic 走市价",
    ),
    (
        "unified_r10_never",
        "3y",
        (*_UNIFIED_R10, ("bot.long.hsl.restart_after_red_policy", "threshold", "never")),
        "HSL 账户级熔断 red=0.10，触发后永久停止",
    ),
    (
        "twe_090",
        "3y",
        (("bot.long.risk.total_wallet_exposure_limit", 1.0, 0.9),),
        "结构性降杠杆：总暴露上限 1.0→0.9",
    ),
    (
        "allowance_000",
        "3y",
        (("bot.long.risk.we_excess_allowance_pct", 0.37, 0.0),),
        "取消单槽超额允许：单币上界 19.57%→14.29%",
    ),
    (
        "twel_enforcer_070",
        "3y",
        (
            ("bot.long.risk.total_exposure_enforcer_enabled", False, True),
            ("bot.long.risk.total_exposure_enforcer_threshold", 1.0, 0.7),
        ),
        "组合暴露强制回收：超过 0.7 即减仓",
    ),
    ("off", "ext", (), "HSL 关闭：5.4 年压力腿控制组"),
    ("coin", "ext", _HSL_ON, "HSL coin：5.4 年压力腿"),
    ("pside_r15", "ext", _PSIDE, "HSL 组合级熔断 red=0.15：5.4 年压力腿"),
    ("unified_r15", "ext", _UNIFIED, "HSL 账户级熔断 red=0.15：5.4 年压力腿"),
    ("unified_r10", "ext", _UNIFIED_R10, "HSL 账户级熔断 red=0.10：5.4 年压力腿"),
    (
        "unified_r10_fast",
        "ext",
        (*_UNIFIED_R10, ("bot.long.hsl.ema_span_minutes", 720.0, 120.0)),
        "HSL 账户级熔断 red=0.10 快速化：5.4 年压力腿",
    ),
    (
        "unified_r10_market",
        "ext",
        (*_UNIFIED_R10, ("bot.long.hsl.panic_close_order_type", "limit", "market")),
        "HSL 账户级熔断 red=0.10 市价 panic：5.4 年压力腿",
    ),
    (
        "unified_r10_never",
        "ext",
        (*_UNIFIED_R10, ("bot.long.hsl.restart_after_red_policy", "threshold", "never")),
        "HSL 账户级熔断 red=0.10 永久停止：5.4 年压力腿",
    ),
    (
        "twe_090",
        "ext",
        (("bot.long.risk.total_wallet_exposure_limit", 1.0, 0.9),),
        "结构性降杠杆 TWE 0.9：5.4 年压力腿",
    ),
    (
        "allowance_000",
        "ext",
        (("bot.long.risk.we_excess_allowance_pct", 0.37, 0.0),),
        "取消单槽超额允许：5.4 年压力腿",
    ),
    (
        "twel_enforcer_070",
        "ext",
        (
            ("bot.long.risk.total_exposure_enforcer_enabled", False, True),
            ("bot.long.risk.total_exposure_enforcer_threshold", 1.0, 0.7),
        ),
        "组合暴露强制回收：5.4 年压力腿",
    ),
    (
        "max_realized_050",
        "ext",
        (("live.max_realized_loss_pct", PARENT_MAX_REALIZED_LOSS_PCT, 0.5),),
        "实现亏损闸门 0.5：证伪臂（预期阻止减仓、尾部更差）",
    ),
    ("off", "synth_a", (), "合成情景 A（单币崩塌）下的 HSL 关闭对照组"),
    ("unified_r10", "synth_a", _UNIFIED_R10, "合成情景 A 下的账户级熔断 red=0.10"),
    ("off", "synth_b", (), "合成情景 B（三币同时崩塌）下的 HSL 关闭对照组"),
    ("unified_r10", "synth_b", _UNIFIED_R10, "合成情景 B 下的账户级熔断 red=0.10"),
)

VARIANTS: tuple[Variant, ...] = tuple(
    _arm(lever, dataset_key, deltas, description)
    for lever, dataset_key, deltas, description in _ARM_SPECS
)
VARIANTS_BY_KEY: dict[str, Variant] = {variant.key: variant for variant in VARIANTS}
DEFAULT_VARIANT_ORDER = tuple(variant.key for variant in VARIANTS)
#: Arms whose 3y leg is pinned evidence from the parent/previous study instead of a new run.
REUSED_ARM_KEYS = tuple(
    variant.key
    for variant in VARIANTS
    if variant.leg == "3y" and variant.lever in ("off", "coin")
)
RUN_VARIANT_ORDER = tuple(
    key for key in DEFAULT_VARIANT_ORDER if key not in REUSED_ARM_KEYS
)


def variant_legs() -> dict[str, tuple[str, ...]]:
    """Arm keys grouped by dataset leg, in declaration order."""
    legs: dict[str, list[str]] = {}
    for variant in VARIANTS:
        legs.setdefault(variant.leg, []).append(variant.key)
    return {leg: tuple(keys) for leg, keys in legs.items()}


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
