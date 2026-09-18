#!/usr/bin/env python3
"""Single source of truth for the g4 @ TWE 3.0 HSL halt-ladder / realized-loss-budget study.

The previous round fixed the two facts this round builds on: the engine's halt length was a single
constant (so "the Nth RED halt waits longer" could not be expressed at all), and the permanent
no-restart floor was decided at panic-close confirmation from `max(drawdown_raw, drawdown_ema)`
sampled at that instant - an instantaneous unrealized-drawdown spike that latched the account shut
on its first touch for every threshold this history allows (0.55 latched on the first strike, 0.70
on the second, while the surviving arms bottomed at 76.56%).

This round is a **declared, no-search replay** of the two engine features that answer those two
facts, both **default-off**:

1. `bot.<pside>.hsl.halt_ladder_minutes` - the Nth RED halt of a ladder cycle uses rung
   `min(N, len) - 1`, saturating at the last rung; the cycle is cleared only when the scope's
   strategy equity regains the peak the cycle started from;
2. `bot.<pside>.hsl.realized_loss_budget_pct` - with `restart_after_red_policy == "threshold"`,
   the permanent halt also latches when the ladder cycle's cumulative realized giveback
   `max(0, cycle_realized_pnl_peak - realized_pnl_now) / ladder_cycle_peak_equity` reaches the
   budget; `no_restart_drawdown_threshold = 1` switches fully to that realized basis.

Four arms, all on the previous round's `a_allow000` geometry (allowance 0, TWE 3.0, unified guard
RED 0.20 / EMA 60 min / 12h halt), are declared here *before* they are run:

* `l0_off`     - both new keys at their engine defaults (`[]`, `0.0`): the default-off regression
  arm, whose parent is the previous round's tracked `a_allow000` evidence;
* `l1_ladder`  - `halt_ladder_minutes = [720, 1440]`;
* `l2_budget`  - `no_restart_drawdown_threshold = 1` + `realized_loss_budget_pct = 0.30`;
* `l3_fixed24` - `halt_ladder_minutes = [1440]`: the "just a longer flat cooldown" control.

Every arm is the frozen parent config plus an explicit list of `(dotted path, from, to)` changes;
`build_variant_config` asserts that nothing else differs and that each declared `from` is the
frozen parent's own value, and `run_variant.py` asserts the run dump carried the same declaration.

Three real legs are served by two frozen HLCV bundles:

* `3y`  - the profile's native window/dataset (2023-09-12 -> 2026-09-12): the window the
  **previous** round searched on, so a reading here is in-sample for the geometry;
* `ext` - the long local history (2021-04-20 -> 2026-09-13), the leg the previous rounds used;
* `pre` - 2021-04-20 -> 2023-09-11 served from the *same* long bundle through the `intersection`
  override: the **out-of-sample** window (2021-05 crash, LUNA, FTX).

This round adds **no parameter search**: nothing is tuned, every arm is declared by hand, and the
frozen `(path, from, to)` table is the whole experimental design.

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
STUDY = REPO / "backtests/binance/g4_hsl_halt_ladder_2026-09-18"
ARTIFACTS = STUDY / "artifacts"

#: The parent study whose frozen config is every arm's source of truth.
PARENT_STUDY = REPO / "backtests/binance/g4_sma20_50_replay_2026-09-16"
SOURCE_CONFIG = PARENT_STUDY / "artifacts/g4_sma20_50.config.json"
SOURCE_CONFIG_SHA256 = "5c8ab5edad9b571cab32a91e82013268ad99ff746a49131cb37bf225b2908bd9"
SOURCE_PROFILE_INPUT = PARENT_STUDY / "artifacts/profile_input.json"
SOURCE_PROFILE_INPUT_SHA256 = "1e6e8c61811e303297e1e72365fd5dad48a05aaddf42c75b8f89265eaf8778ce"

#: The two engine keys this round turns on. The pinned g4 profile predates them, so the frozen
#: parent of *this* study is that pinned config plus these two keys at their engine defaults:
#: `build_variant_config.derive_parent_config` applies them, records the addition under
#: `parent_derivation` in `variant_input.json`, and gates that the raw pinned config differs from
#: the derived parent by exactly these two paths. Every arm's `(path, from, to)` triple is
#: therefore read against a parent that really carries both keys.
HALT_LADDER_PATH = "bot.long.hsl.halt_ladder_minutes"
REALIZED_LOSS_BUDGET_PATH = "bot.long.hsl.realized_loss_budget_pct"
NO_RESTART_DRAWDOWN_THRESHOLD_PATH = "bot.long.hsl.no_restart_drawdown_threshold"
PARENT_HALT_LADDER_MINUTES: list[float] = []
PARENT_REALIZED_LOSS_BUDGET_PCT = 0.0
PARENT_NO_RESTART_DRAWDOWN_THRESHOLD = 1
PARENT_ENGINE_DEFAULTS: tuple[tuple[str, Any], ...] = (
    ("bot.long.hsl.halt_ladder_minutes", []),
    ("bot.long.hsl.realized_loss_budget_pct", PARENT_REALIZED_LOSS_BUDGET_PCT),
    # Both pside blocks: the engine's config surface is per side, and a frozen parent that carries
    # the keys on one side only would make every run dump look like it changed the other one.
    ("bot.short.hsl.halt_ladder_minutes", []),
    ("bot.short.hsl.realized_loss_budget_pct", PARENT_REALIZED_LOSS_BUDGET_PCT),
)
#: What the non-default arms declare: the 12h/24h ladder of the design contract (2.2), the same
#: ladder truncated to a single 24h rung (the "just a longer flat cooldown" control), and the
#: realized basis with the budget set at the measured confirmation loss of one ZEC RED event
#: (`hard_stop_panic_close_loss_drawdown_pct_mean` = 30.03%, design contract 3.1).
LADDER_RUNGS: tuple[float, ...] = (720.0, 1440.0)
FIXED_LONG_HALT_MINUTES: tuple[float, ...] = (1440.0,)
REALIZED_LOSS_BUDGET_PCT = 0.30
#: The legal upper bound of `no_restart_drawdown_threshold`; at 1.0 the instantaneous rule cannot
#: fire, which is how the design contract switches to the realized basis (3.2).
NO_RESTART_OFF = 1
#: The engine's own bound on the ladder (`MAX_HSL_HALT_LADDER_MINUTES`, src/config/bot.py): a
#: longer list is rejected rather than truncated.
MAX_HALT_LADDER_RUNGS = 32

#: The previous round's `a_allow000` arms (allowance 0, TWE 3.0, unified guard RED 0.20 /
#: EMA 60 / 12h halt) are this round's control columns: pinned by the sha256 of their tracked
#: `analysis.json` and never re-run. `l0_off` must reproduce them bit for bit.
PRIOR_STUDY = REPO / "backtests/binance/g4_twe300_risk_optimization_2026-09-17"
TAIL_RISK_STUDY = REPO / "backtests/binance/g4_tail_risk_research_2026-09-17"

#: Reference arms that are *not* re-run by this study: their tracked analysis is pinned. This
#: round needs exactly one kind of anchor - the previous round's `a_allow000` arms, which are the
#: same frozen geometry with the two new keys at their defaults. `l0_off` is the declared
#: regression arm against them: with both keys off the engine must be unchanged, so the fill
#: signature, the terminal multiple, the worst drawdown and every `hard_stop_*` reading must agree.
REFERENCE_RUNS: dict[str, dict[str, Any]] = {
    "a_allow000__3y": {
        "label": "上一轮 a_allow000（allowance=0 + 守护 0.20/EMA60/停 12H）, 3y",
        "run_dir": PRIOR_STUDY
        / "artifacts/a_allow000__3y/backtest_results/binance_a_allow000__3y"
        "/binance/2026-09-17T10_49_03",
        "analysis_sha256": "8726a5b8457abddec6dd2fa174f6ddaf49c056118d1a56a2bdfa2f79b125b78f",
        "leg": "3y",
        "kind": "prior_geometry",
        "role": "默认关闭回归基准：l0_off 必须与它逐位一致（成交签名、终值、最差回撤、hard_stop_*）",
    },
    "a_allow000__ext": {
        "label": "上一轮 a_allow000（allowance=0 + 守护 0.20/EMA60/停 12H）, 5.4y",
        "run_dir": PRIOR_STUDY
        / "artifacts/a_allow000__ext/backtest_results/binance_a_allow000__ext"
        "/binance/2026-09-17T10_50_30",
        "analysis_sha256": "615b449134b8b7a9077d43a23b54f08267154aaf303ececf09fc75c547983c97",
        "leg": "ext",
        "kind": "prior_geometry",
        "role": "崩盘腿上的默认关闭基准：阶梯与累计口径的每一点差异都在这一列上读",
    },
    "a_allow000__pre": {
        "label": "上一轮 a_allow000（allowance=0 + 守护 0.20/EMA60/停 12H）, 样本外 2.4y",
        "run_dir": PRIOR_STUDY
        / "artifacts/a_allow000__pre/backtest_results/binance_a_allow000__pre"
        "/binance/2026-09-17T10_47_03",
        "analysis_sha256": "c6f000472e15610118c1eded61f9d35e1a01f70329528a3fd1062cb260636c32",
        "leg": "pre",
        "kind": "prior_geometry",
        "role": "样本外腿的默认关闭基准（上一轮 0.6104× / 最差回撤 76.56%）",
    },
}

#: The arm each leg compares its declared variants against. All three legs have a pinned anchor
#: this round; nothing is re-run to build a control column.
REFERENCE_CONTROL_BY_LEG = {
    "3y": "a_allow000__3y",
    "ext": "a_allow000__ext",
    "pre": "a_allow000__pre",
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

#: The account-guard surface this study manipulates, plus the engine's contract for it.
PNLS_LOOKBACK_PATH = "live.pnls_max_lookback_days"
PARENT_PNLS_LOOKBACK_DAYS = 30.0
GUARD_LOOKBACK_DAYS = 7.0
HSL_PATHS = (
    "bot.long.hsl.enabled",
    "bot.long.hsl.red_threshold",
    "bot.long.hsl.ema_span_minutes",
    "bot.long.hsl.cooldown_minutes_after_red",
    "bot.long.hsl.no_restart_drawdown_threshold",
    "bot.long.hsl.restart_after_red_policy",
    "bot.long.hsl.tier_ratios.yellow",
    "bot.long.hsl.tier_ratios.orange",
    "bot.long.hsl.orange_tier_mode",
    "bot.long.hsl.panic_close_order_type",
    "bot.long.hsl.halt_ladder_minutes",
    "bot.long.hsl.realized_loss_budget_pct",
)
#: What each HSL tier does in this engine revision (verified in `passivbot-rust/src`).
GUARD_TIER_SEMANTICS = {
    "yellow": "仅遥测（不改变交易模式）",
    "orange": "整个作用域进入 TpOnly：不产生任何新入场（含加仓），只走止盈路径",
    "red": "先 Panic 平掉整个作用域，再停机 cooldown_minutes_after_red",
}
GUARD_MAPPING_TABLE = (
    {
        "request": "账户级",
        "engine": "unified 作用域",
        "config": "live.hsl_signal_mode=unified + bot.long.hsl.enabled=true",
        "note": "backtest 直接读取 live.hsl_signal_mode（src/backtest.py）",
    },
    {
        "request": "一周内",
        "engine": "滚动峰值窗口 7 天",
        "config": f"{PNLS_LOOKBACK_PATH}=7.0",
        "note": "分母 = 窗口内最高策略权益；含已实现亏损，比“纯浮亏”更保守",
    },
    {
        "request": "浮亏 20%",
        "engine": "RED 阈值 20%",
        "config": "bot.long.hsl.red_threshold=0.20",
        "note": "触发指标 min(raw, EMA)；引擎硬约束 red <= no_restart <= 1.0",
    },
    {
        "request": "立即反应",
        "engine": "快速 EMA",
        "config": "bot.long.hsl.ema_span_minutes=60",
        "note": "默认 720 分钟正是上一轮“0 次触发就被强平”的原因",
    },
    {
        "request": "熔断 12H / 升级 24H",
        "engine": "冷却阶梯：第 N 次 RED 停机取 min(N, len)-1 档，夹在最后一档",
        "config": "bot.long.hsl.halt_ladder_minutes=[720, 1440]（默认 [] = 关闭）",
        "note": "周期 = 作用域权益未回到本周期峰值的那段时间；阶梯启用时 cooldown_minutes_after_red 被忽略",
    },
    {
        "request": "永久停机按累计已实现亏损判定",
        "engine": "累计已实现回吐 / 周期起点权益 ≥ budget 时锁存",
        "config": "bot.long.hsl.realized_loss_budget_pct=0.30 + no_restart_drawdown_threshold=1",
        "note": "只在 restart_after_red_policy=threshold 下比较；1.0 是关掉瞬时口径的合法上界",
    },
    {
        "request": "第二次按剩余余额再算 20%",
        "engine": "episode 结束后滚动峰值清空",
        "config": "无需配置",
        "note": "第二次触发天然以停机后的剩余权益为峰值基准（rolling_peak_strategy_pnl.clear()）",
    },
)

#: The exposure-geometry surface this study manipulates. `we_excess_allowance_pct` is the
#: "occupancy discipline" knob: the engine caps each coin at
#: `TWE / n_positions * (1 + effective_allowance)`, and the bounded mode
#: (`src/risk_limits.py`) further clamps the allowance to `TWE/(TWE/n) - 1 = n - 1`, so the raw
#: value is what binds inside this study's ranges.
TOTAL_WALLET_EXPOSURE_LIMIT_PATH = "bot.long.risk.total_wallet_exposure_limit"
N_POSITIONS_PATH = "bot.long.risk.n_positions"
ALLOWANCE_PATH = "bot.long.risk.we_excess_allowance_pct"
ALLOWANCE_MODE_PATH = "bot.long.risk.we_excess_allowance_mode"
MAX_REALIZED_LOSS_PATH = "live.max_realized_loss_pct"
PARENT_ALLOWANCE_PCT = 0.37
PARENT_N_POSITIONS = 7
#: Every arm of this round inherits the previous round's `a_allow000` geometry: allowance 0.
ARM_ALLOWANCE_GRID = (0.0,)
#: The user's question this round answers, in one line per knob.
GEOMETRY_MAPPING_TABLE = (
    {
        "request": "不去占用空槽的额度",
        "engine": "单币上限 = TWE / n_positions × (1 + allowance)",
        "config": "bot.long.risk.we_excess_allowance_pct = 0",
        "note": "allowance>0 时单币可以吃掉空槽的额度；=0 时每个币只有自己的均分额度",
    },
    {
        "request": "同等杠杆下降低尾部",
        "engine": "总暴露上限 / 槽位预算",
        "config": "total_wallet_exposure_limit ∈ {3.0, 2.5, 2.0}",
        "note": "结构性降杠杆：到强平地板的距离随暴露上限线性放大",
    },
    {
        "request": "冷却阶梯接在冷却时长上",
        "engine": "第 N 次 RED 停机取阶梯第 min(N, len)-1 档（本轮已实现）",
        "config": "bot.long.hsl.halt_ladder_minutes ∈ {[], [720, 1440], [1440]}",
        "note": "阶梯启用时 cooldown_minutes_after_red 被忽略；周期在权益回到本周期峰值时清零",
    },
    {
        "request": "永久地板不要被瞬时尖峰扣动",
        "engine": "累计已实现亏损口径（分母 = 周期起点权益，分子只随成交变化）",
        "config": "restart_after_red_policy=threshold + no_restart_drawdown_threshold=1"
        " + realized_loss_budget_pct=0.30",
        "note": "threshold 策略下两个判据是 OR；1.0 用来关掉瞬时口径（设计契约 3.2）",
    },
    {
        "request": "阶梯与累计口径可复核",
        "engine": "analysis.json 新增 hard_stop_ladder_strikes_max / "
        "hard_stop_realized_loss_halt_pct_max",
        "config": "无需配置（引擎遥测）",
        "note": "hard_stop_duration_minutes_max 报实际使用的那一档；strikes_max=0 表示阶梯未启用",
    },
)

#: This round declares **no parameter search**: there is no search contract, no bounds, no
#: candidate registry, no selection file and no search stage in `run.sh`. The four arms are
#: pre-registered by hand from the engine contract (`halt_ladder_design.md` 7), and that
#: pre-registration is what the report is judged against.
NO_SEARCH: dict[str, Any] = {
    "search": None,
    "reason": (
        "本轮只验证两个默认关闭的引擎改动（冷却阶梯 halt_ladder_minutes、永久停机改累计已实现"
        "亏损口径 realized_loss_budget_pct）；四个臂全部预注册，没有任何参数被搜索或调优"
    ),
    "precedent": (
        "上一轮的搜索给出过反例：搜索窗里 ADG 最高的候选 c1 在样本外 pre 腿被强平，"
        "因此这一轮把预算放在机制验证上，而不是再搜一遍风险几何"
    ),
}


#: Occupancy / exposure geometry recorded in every arm's `risk_geometry.json`.
GEOMETRY_ARTIFACT = "risk_geometry.json"
GEOMETRY_FIELDS = (
    "per_slot_cap",
    "effective_allowance_pct",
    "peak_total_exposure",
    "mean_total_exposure",
    "peak_coin_exposure",
    "top3_exposure",
    "peak_coin_share_of_peak_total",
    "active_coins_mean",
    "active_coins_peak",
    "empty_slot_time_share",
    "time_with_any_position_share",
    "liquidation_shock_at_peak",
    "single_coin_wipeout_bound",
)

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
#:
#: The last two keys are the engine defaults this study's parent derivation adds: the pinned g4
#: profile was frozen before they existed, and `build_variant_config.derive_parent_config` makes
#: the frozen parent carry them so that every arm's `(path, from, to)` triple is read against a
#: parent that really has the key. They stay part of the identity gate on purpose - an arm that
#: dropped or re-spelled either one is a difference, not a default.
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
    "halt_ladder_minutes": [],
    "realized_loss_budget_pct": 0.0,
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
    # The out-of-sample leg: the same long bundle sliced to the window the search never sees.
    # `intersection` (not `dataset`) is what makes the engine honour a truncated window
    # (`src/hlcvs_override.py`: dataset mode serves the whole bundle, intersection mode
    # clips to `min(requested_end, dataset_end)`).
    "pre": DatasetSpec(
        key="pre",
        leg="pre",
        rel_path=Path(
            "caches/hlcvs_data/binance__40_coins__2021-03-25_to_2026-09-13__a02b6ae1c140f2b7"
        ),
        window=("2021-04-20", "2023-09-11"),
        manifest_sha256="d7e3b9756be4a0a2460b148de3753130f07f1840ac29d0403409d845fad32f8c",
        manifest_config_hash="a02b6ae1c140f2b77c92d88e4292426dd3e8d4c0c287f2671cd3c9668d639483",
        override_mode="intersection",
        note="out-of-sample leg: the long bundle clipped before the search window starts",
    ),
}

#: Leg order used by every table and by `run.sh`.
LEG_ORDER = ("3y", "ext", "pre")
LEG_TITLES = {
    "3y": "原生搜索窗腿（2023-09-12 → 2026-09-12，样本内）",
    "ext": "全历史腿（2021-04-20 → 2026-09-13）",
    "pre": "样本外腿（2021-04-20 → 2023-09-11，与搜索窗不重叠）",
}
#: The leg whose window the **previous** round's parameter search was allowed to see. This round
#: searches nothing; the leg keeps its role as "the window an earlier round tuned on".
PRIOR_SEARCH_LEG = "3y"

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
    "l0_off": Lever(
        key="l0_off",
        label="默认关闭回归臂：halt_ladder_minutes=[] + realized_loss_budget_pct=0.0",
        question="两个新键都关掉时，引擎是否与改动前逐位相同？",
        hypothesis="逐位一致（成交签名、终值、最差回撤、hard_stop_* 全等）——这是默认关闭的回归证据",
    ),
    "l1_ladder": Lever(
        key="l1_ladder",
        label="冷却阶梯：halt_ladder_minutes=[720, 1440]（第 1/2 次及以后 12H / 24H）",
        question="连续触发时按档加长冷却，能否只塑形风险而不改收益？",
        hypothesis="第二次触发看到 1440 分钟；档位可分辨，且第 3 次及以后夹在 24H",
    ),
    "l2_budget": Lever(
        key="l2_budget",
        label="累计已实现亏损口径：no_restart=1 + realized_loss_budget_pct=0.30",
        question="把永久停机从瞬时回撤尖峰换成累计已实现回吐，是否还锁存、锁存在哪里？",
        hypothesis="不再因确认那一刻的瞬时尖峰锁存；若锁存则 no_restart_reason=realized_loss 且可复算",
    ),
    "l3_fixed24": Lever(
        key="l3_fixed24",
        label="对照臂：halt_ladder_minutes=[1440]（每一档都是 24H）",
        question="阶梯的效果是否只是“冷却更长”，而不是“按第几次触发加长”？",
        hypothesis="与 l1_ladder 的差异只来自档位选择；单档 24H 等价于把冷却常量改成 1440",
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

    @property
    def declared_halt_ladder_minutes(self) -> list[float]:
        """The halt ladder this arm declares (`[]` is the engine's default-off value)."""
        return [
            float(value) for value in self.declared(HALT_LADDER_PATH, PARENT_HALT_LADDER_MINUTES)
        ]

    @property
    def declared_realized_loss_budget_pct(self) -> float:
        """The realized-loss budget this arm declares (`0.0` is the engine's default-off value)."""
        return float(
            self.declared(REALIZED_LOSS_BUDGET_PATH, PARENT_REALIZED_LOSS_BUDGET_PCT)
        )

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
    def declared_lookback_days(self) -> float:
        """The HSL peak window this arm declares (the "within a week" knob)."""
        return float(self.declared(PNLS_LOOKBACK_PATH, PARENT_PNLS_LOOKBACK_DAYS))

    @property
    def guard_params(self) -> dict[str, Any]:
        """The account-guard parameters this arm declares, for reporting and verification."""
        block = json.loads(json.dumps(HSL_BLOCK))
        for dotted, _from, to in self.deltas:
            if dotted.startswith("bot.long.hsl."):
                set_path(block, dotted[len("bot.long.hsl.") :], to)
        live_mode = LIVE_HSL["hsl_signal_mode"]
        for dotted, _from, to in self.deltas:
            if dotted == "live.hsl_signal_mode":
                live_mode = to
        tier = block.get("tier_ratios") or {}
        red = float(block.get("red_threshold") or 0.0)
        return {
            "scope": live_mode,
            "enabled": bool(block.get("enabled")),
            "red_threshold": red,
            "orange_ratio": float(tier.get("orange") or 0.0),
            "yellow_ratio": float(tier.get("yellow") or 0.0),
            "orange_threshold": float(tier.get("orange") or 0.0) * red,
            "yellow_threshold": float(tier.get("yellow") or 0.0) * red,
            "ema_span_minutes": float(block.get("ema_span_minutes") or 0.0),
            "cooldown_minutes_after_red": float(block.get("cooldown_minutes_after_red") or 0.0),
            "no_restart_drawdown_threshold": float(
                block.get("no_restart_drawdown_threshold") or 0.0
            ),
            "restart_after_red_policy": block.get("restart_after_red_policy"),
            "panic_close_order_type": block.get("panic_close_order_type"),
            "halt_ladder_minutes": [
                float(value) for value in (block.get("halt_ladder_minutes") or [])
            ],
            "realized_loss_budget_pct": float(block.get("realized_loss_budget_pct") or 0.0),
            "lookback_days": self.declared_lookback_days,
        }

    @property
    def guard_label(self) -> str:
        params = self.guard_params
        if not params["enabled"]:
            return "无守护（对照）"
        halt = (
            f"阶梯 {[int(value) for value in params['halt_ladder_minutes']]} 分钟"
            if params["halt_ladder_minutes"]
            else f"停 {params['cooldown_minutes_after_red'] / 60:.0f}H"
        )
        return (
            f"{params['scope']}：{params['red_threshold']:.2f} 清仓+{halt}"
            f"（橙 {params['orange_threshold']:.2f} 停加仓，{params['lookback_days']:.0f} 天窗口，"
            f"EMA {params['ema_span_minutes']:.0f} 分钟"
            + (
                f"，累计 {params['no_restart_drawdown_threshold']:.2f} 永久停机"
                if params["no_restart_drawdown_threshold"] < 1.0
                else ""
            )
            + (
                f"，累计已实现亏损预算 {params['realized_loss_budget_pct']:.2f}"
                if params["realized_loss_budget_pct"] > 0.0
                else ""
            )
            + "）"
        )


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


#: The two production changes the previous rounds already made; every arm keeps them.
def _capital(twe: float) -> tuple[tuple[str, Any, Any], ...]:
    """10,000 USDT starting capital plus this arm's declared total exposure limit."""
    return (
        (STARTING_BALANCE_PATH, PARENT_STARTING_BALANCE, ARM_STARTING_BALANCE),
        (TOTAL_WALLET_EXPOSURE_LIMIT_PATH, 1.0, twe),
    )


#: Account-level scope every arm of this study uses (the validated guard shape).
_UNIFIED: tuple[tuple[str, Any, Any], ...] = (
    ("bot.long.hsl.enabled", False, True),
    ("live.hsl_signal_mode", "coin", "unified"),
)


def _guard(
    *,
    red: float | None = 0.20,
    ema: float = 60.0,
    cooldown: float = 720.0,
    no_restart: float | None = None,
    allow: float | None = 0.0,
) -> tuple[tuple[str, Any, Any], ...]:
    """The guard plus exposure deltas, with each config path declared exactly once.

    `None` means "keep the parent value" (that is how `b_red015` keeps RED at 0.15), and
    `allow=None` keeps the parent allowance of 0.37.
    """
    declared: list[tuple[str, Any, Any]] = [*_UNIFIED]
    if red is not None:
        declared.append(("bot.long.hsl.red_threshold", 0.15, red))
    if ema != 720.0:
        declared.append(("bot.long.hsl.ema_span_minutes", 720.0, ema))
    if cooldown != 2160.0:
        declared.append(("bot.long.hsl.cooldown_minutes_after_red", 2160.0, cooldown))
    if no_restart is not None:
        declared.append(("bot.long.hsl.no_restart_drawdown_threshold", 1, no_restart))
    declared.append((PNLS_LOOKBACK_PATH, PARENT_PNLS_LOOKBACK_DAYS, GUARD_LOOKBACK_DAYS))
    if allow is not None:
        declared.append((ALLOWANCE_PATH, PARENT_ALLOWANCE_PCT, allow))
    return tuple(declared)


#: The frozen geometry every arm of this round inherits, exactly as the previous round's
#: `a_allow000` declared it: 10,000 USDT starting capital, TWE 3.0, `we_excess_allowance_pct = 0`,
#: and the validated account guard (unified scope, RED 0.20, EMA 60 min, 12h halt, 7-day peak).
def _base(*extra: tuple[str, Any, Any]) -> tuple[tuple[str, Any, Any], ...]:
    return (*_capital(3.0), *_guard(), *extra)


#: (lever, declared deltas, human description). Every arm runs on all three legs.
_L_ARMS: tuple[tuple[str, tuple[tuple[str, Any, Any], ...], str], ...] = (
    (
        "l0_off",
        _base(
            (
                HALT_LADDER_PATH,
                list(PARENT_HALT_LADDER_MINUTES),
                list(PARENT_HALT_LADDER_MINUTES),
            ),
            (
                REALIZED_LOSS_BUDGET_PATH,
                PARENT_REALIZED_LOSS_BUDGET_PCT,
                PARENT_REALIZED_LOSS_BUDGET_PCT,
            ),
        ),
        "默认关闭回归臂：两个新键都钉在引擎默认值（[] / 0.0）",
    ),
    (
        "l1_ladder",
        _base(
            (HALT_LADDER_PATH, list(PARENT_HALT_LADDER_MINUTES), list(LADDER_RUNGS)),
        ),
        "冷却阶梯：第 1/2 次及以后 RED 停机 12H / 24H（夹在最后一档）",
    ),
    (
        "l2_budget",
        _base(
            (
                NO_RESTART_DRAWDOWN_THRESHOLD_PATH,
                PARENT_NO_RESTART_DRAWDOWN_THRESHOLD,
                NO_RESTART_OFF,
            ),
            (
                REALIZED_LOSS_BUDGET_PATH,
                PARENT_REALIZED_LOSS_BUDGET_PCT,
                REALIZED_LOSS_BUDGET_PCT,
            ),
        ),
        "永久停机改累计已实现亏损口径：预算 0.30、瞬时口径关到合法上界 1.0",
    ),
    (
        "l3_fixed24",
        _base(
            (HALT_LADDER_PATH, list(PARENT_HALT_LADDER_MINUTES), list(FIXED_LONG_HALT_MINUTES)),
        ),
        "对照臂：把阶梯换成单一 24H 档（区分“阶梯”与“单纯加长冷却”）",
    ),
)


def lever_for(lever_key: str) -> "Lever":
    """The declared lever.

    Every arm of this round is pre-registered in `LEVERS`; the study runs no search, so there is no
    search-derived arm to synthesize a label for and an unknown key is a hard error.
    """
    try:
        return LEVERS[lever_key]
    except KeyError:
        raise KeyError(f"unknown lever {lever_key!r}; this study declares no search-derived arms")


#: Every declared arm runs on all three legs, in declaration order.
ARM_LEGS: tuple[str, ...] = LEG_ORDER
_ARM_SPECS: tuple[tuple[str, str, tuple[tuple[str, Any, Any], ...], str], ...] = tuple(
    (
        lever,
        leg,
        deltas,
        f"{description}｜{LEG_TITLES[leg]}",
    )
    for lever, deltas, description in _L_ARMS
    for leg in ARM_LEGS
)


VARIANTS: tuple[Variant, ...] = tuple(
    _arm(lever, dataset_key, deltas, description)
    for lever, dataset_key, deltas, description in _ARM_SPECS
)
VARIANTS_BY_KEY: dict[str, Variant] = {variant.key: variant for variant in VARIANTS}
DEFAULT_VARIANT_ORDER = tuple(variant.key for variant in VARIANTS)
#: Every declared arm is run by this study; the four reference anchors are pinned evidence.
REUSED_ARM_KEYS: tuple[str, ...] = ()
RUN_VARIANT_ORDER = tuple(DEFAULT_VARIANT_ORDER)
#: One stage per declared lever, so `run.sh --stage L1` replays exactly one arm of the round.
STAGE_BY_LEVER: dict[str, str] = {
    "l0_off": "L0",
    "l1_ladder": "L1",
    "l2_budget": "L2",
    "l3_fixed24": "L3",
}
STAGES: tuple[str, ...] = tuple(STAGE_BY_LEVER[lever] for lever, *_rest in _L_ARMS)


def stage_of(variant: "Variant") -> str:
    """Which stage of the study an arm belongs to (one stage per declared lever)."""
    return STAGE_BY_LEVER.get(variant.lever, "L")


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
    # The two telemetry keys this round exists to read: the highest ladder strike of any halt cycle
    # (0 when the ladder is off) and the cumulative realized giveback that latched a permanent halt
    # (0 otherwise).
    "hard_stop_ladder_strikes_max",
    "hard_stop_realized_loss_halt_pct_max",
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


def references_of_kind(kind: str) -> dict[str, dict[str, Any]]:
    """Pinned reference arms filtered by role (`no_guard` or `guard`)."""
    return {key: item for key, item in REFERENCE_RUNS.items() if item.get("kind") == kind}
