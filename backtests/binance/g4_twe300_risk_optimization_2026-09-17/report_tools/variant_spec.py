#!/usr/bin/env python3
"""Single source of truth for the g4 @ TWE 3.0 risk-geometry study.

The two previous rounds established the facts this round builds on: TWE 3.0 / 10,000 USDT is
liquidated in the local history without a guard, and the account-level guard that survives it
(`unified`, RED 0.20 against a 7-day peak, EMA 60 min, 12h halt) costs a large part of the
terminal wealth on the benign window. This study asks how to buy the same survival more
cheaply, using only knobs the engine already has:

1. **occupancy discipline** — `we_excess_allowance_pct = 0`, i.e. no coin may exceed its equal
   share `TWE / n_positions` of the exposure budget ("do not spend the empty slots'
   allowance"), together with the guard and with lower exposure limits;
2. **guard geometry** — cooldown rungs (12/24/48/72h), a cumulative terminal latch placed
   *above* a single crash's confirmation drawdown, the realized-loss brake, and faster/earlier
   triggers at the new concentration level;
3. **parameter search** — the repository's own pymoo optimizer over
   `TWE in [2.0,3.0] x allowance in [0,0.37] x n_positions in [5,10] x guard red/EMA/cooldown`
   with every alpha parameter pinned, searched on the native window and validated out of
   sample.

Every arm is declared here *before* it is run. An arm is the frozen parent g4 profile config
plus an explicit list of `(dotted path, from, to)` changes; `build_variant_config` asserts
that nothing else differs and `run_variant.py` asserts the run dump carried the same
declaration.

Three real legs are served by two frozen HLCV bundles:

* `3y`  — the profile's native window/dataset (2023-09-12 -> 2026-09-12): the **search**
  window, so any result on it is in-sample;
* `ext` — the long local history (2021-04-20 -> 2026-09-13), the leg the previous rounds used;
* `pre` — 2021-04-20 -> 2023-09-11 served from the *same* long bundle through the
  `intersection` override: the **out-of-sample** window (2021-05 crash, LUNA, FTX) that the
  search never sees.

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
STUDY = REPO / "backtests/binance/g4_twe300_risk_optimization_2026-09-17"
#: The study whose validated guard shape and frozen arm configs this round builds on.
GUARD_STUDY = REPO / "backtests/binance/g4_twe300_account_guard_2026-09-17"
ARTIFACTS = STUDY / "artifacts"

#: The parent study whose frozen config is every arm's source of truth.
PARENT_STUDY = REPO / "backtests/binance/g4_sma20_50_replay_2026-09-16"
SOURCE_CONFIG = PARENT_STUDY / "artifacts/g4_sma20_50.config.json"
SOURCE_CONFIG_SHA256 = "5c8ab5edad9b571cab32a91e82013268ad99ff746a49131cb37bf225b2908bd9"
SOURCE_PROFILE_INPUT = PARENT_STUDY / "artifacts/profile_input.json"
SOURCE_PROFILE_INPUT_SHA256 = "1e6e8c61811e303297e1e72365fd5dad48a05aaddf42c75b8f89265eaf8778ce"

#: The previous study's unguarded TWE 3.0 arms are this study's control columns: pinned by
#: the sha256 of their tracked `analysis.json`, never re-run.
PRIOR_STUDY = REPO / "backtests/binance/g4_twe300_10k_replay_2026-09-17"
TAIL_RISK_STUDY = REPO / "backtests/binance/g4_tail_risk_research_2026-09-17"

#: Reference arms that are *not* re-run by this study: their tracked analysis is pinned.
#: Two kinds are needed: the unguarded arms (what the guard buys) and the previous round's
#: guarded arms (what this round must beat).
REFERENCE_RUNS: dict[str, dict[str, Any]] = {
    "twe300_10k__3y": {
        "label": "10k / TWE 3.0, 无守护, 3y",
        "run_dir": PRIOR_STUDY
        / "artifacts/twe300_10k__3y/backtest_results/binance_twe300_10k__3y/binance/2026-09-17T08_18_53",
        "analysis_sha256": "58d65d52312db205e544f97b9bbabd944455d141f8001b5df80f3503a208aec4",
        "leg": "3y",
        "kind": "no_guard",
        "role": "无守护的 TWE 3.0 臂：守护买到的生存与放弃的收益都以它为基准",
    },
    "twe300_10k__ext": {
        "label": "10k / TWE 3.0, 无守护, 5.4y（2021-05-19 强平）",
        "run_dir": PRIOR_STUDY
        / "artifacts/twe300_10k__ext/backtest_results/binance_twe300_10k__ext/binance/2026-09-17T08_25_04",
        "analysis_sha256": "72be330e8d5cb14ba261447eb51c7509c40c81868ba82c06623e172313495331",
        "leg": "ext",
        "kind": "no_guard",
        "role": "无守护臂在 5.4 年腿上被强平，是本轮所有生存结论的对照",
    },
    "twe300_10k_allowance0__3y": {
        "label": "10k / TWE 3.0 + allowance=0, 无守护, 3y",
        "run_dir": PRIOR_STUDY
        / "artifacts/twe300_10k_allowance0__3y/backtest_results/binance_twe300_10k_allowance0__3y"
        "/binance/2026-09-17T08_23_12",
        "analysis_sha256": "c7a55dfc92989ef0f4527e710103b406face13bf968780587230649e153eb482",
        "leg": "3y",
        "kind": "no_guard_allow0",
        "role": "结构性去风险单独使用的 3 年腿读数：2×2 分解里“只改占用纪律”的那一格",
    },
    "twe300_10k_allowance0__ext": {
        "label": "10k / TWE 3.0 + allowance=0, 无守护, 5.4y",
        "run_dir": PRIOR_STUDY
        / "artifacts/twe300_10k_allowance0__ext/backtest_results/binance_twe300_10k_allowance0__ext"
        "/binance/2026-09-17T08_32_07",
        "analysis_sha256": "c07aa89ceefa8d68e0932e425a3a8e823053a1cfe820da263904325d9be4c3aa",
        "leg": "ext",
        "kind": "no_guard_allow0",
        "role": "结构性去风险单独使用就活下来了（14.29×、最差回撤 92.22%）："
        "2×2 分解里“只改占用纪律”的那一格，也是“结构与熔断谁更值”的对照",
    },
    "g_user12h__3y": {
        "label": "10k / TWE 3.0 + 账户守护（unified 0.20 / EMA60 / 停 12H）, 3y",
        "run_dir": GUARD_STUDY
        / "artifacts/g_user12h__3y/backtest_results/binance_g_user12h__3y/binance/2026-09-17T09_22_55",
        "analysis_sha256": "f6b9fdb9ac1b0a83c36cffb9e4dbf6216ee0162b47f8a2ee57d65ec73ed9e223",
        "leg": "3y",
        "kind": "guard",
        "role": "上一轮验证过的守护直译版：本轮的结构性改动必须与它比较（收益代价 vs 生存）",
    },
    "g_user12h__ext": {
        "label": "10k / TWE 3.0 + 账户守护（unified 0.20 / EMA60 / 停 12H）, 5.4y",
        "run_dir": GUARD_STUDY
        / "artifacts/g_user12h__ext/backtest_results/binance_g_user12h__ext/binance/2026-09-17T09_36_13",
        "analysis_sha256": "25f6cb32768f274273851bdb34e88e2f917d4093b6a9be155a1981c424d0d00d",
        "leg": "ext",
        "kind": "guard",
        "role": "守护基准：ext 腿 7.8552× / 最差回撤 62.40%，本轮要回答能否用结构换到更好的点",
    },
}

#: The arm each leg compares its structural variants against (pinned for 3y/ext, a study arm
#: for the out-of-sample leg, which has no pinned reference).
REFERENCE_CONTROL_BY_LEG = {
    "3y": "twe300_10k__3y",
    "ext": "twe300_10k__ext",
    "pre": "a_allow037__pre",
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
        "engine": "停机时长（单一常量）",
        "config": "bot.long.hsl.cooldown_minutes_after_red=720 或 1440",
        "note": "阶梯本身不可表达；近似见 restart_after_red_policy=threshold + no_restart_drawdown_threshold",
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
ARM_ALLOWANCE_GRID = (0.0, 0.10, 0.20, 0.37)
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
        "request": "冷却阶梯要接在冷却时长上",
        "engine": "停机时长是单一常量（阶梯需引擎改动）",
        "config": "cooldown_minutes_after_red ∈ {720, 1440, 2880, 4320}",
        "note": "本轮用固定档位对比为下一轮引擎级阶梯定档；累计阈值二档见 no_restart_drawdown_threshold",
    },
    {
        "request": "累计口径的第二级",
        "engine": "跨重启持久峰值回撤锁存（平仓确认时用 max(raw, EMA) 判定）",
        "config": "restart_after_red_policy=threshold + no_restart_drawdown_threshold ∈ {0.55, 0.70}",
        "note": "阈值必须高于一次急跌的确认回撤（上一轮实测 ≈0.47），否则第一次触发就永久关停",
    },
    {
        "request": "用已实现亏损而不是浮亏尖峰",
        "engine": "全局实现亏损闸门（拦截非 panic 的亏损平仓；panic 豁免）",
        "config": "live.max_realized_loss_pct = 0.50",
        "note": "引擎不导出拦截计数，因此该臂只报终值/回撤/成交差异，并声明测量局限",
    },
)

#: The parameter search contract. Every value here is frozen into `variant_input.json` and
#: re-checked by the selection tool, so the search cannot silently change shape.
SEARCH_SELECTION_PATH = ARTIFACTS / "search_selection.json"
SEARCH_RECORD_PATH = ARTIFACTS / "search_record.json"
SEARCH_SMOKE_PATH = ARTIFACTS / "search_smoke.json"
SEARCH_CONFIG_PATH = ARTIFACTS / "search.config.json"
SEARCH_RESULTS_ROOT = REPO / "optimize_results"
SEARCH_SEED = 20260917
SEARCH_ITERS = 1024
SEARCH_POPULATION_SIZE = 64
SEARCH_N_CPUS = 2
SEARCH_SMOKE_ITERS = 16
SEARCH_SMOKE_N_CPUS = 2
#: pymoo evaluates a whole population per generation, so a cheap smoke run also shrinks the
#: population: one generation of eight candidates measures the per-evaluation cost and the
#: per-worker memory footprint without paying for a full generation of 64.
SEARCH_SMOKE_POPULATION_SIZE = 8
SEARCH_CANDIDATE_LEGS = ("3y", "ext", "pre")
SEARCH_PICK = {
    "completion_min": 0.99,
    "drawdown_max": 0.50,
    "max_candidates": 6,
}
SEARCH_SCORING = (
    {"metric": "adg_strategy_eq", "goal": "max"},
    {"metric": "drawdown_worst_strategy_eq", "goal": "min"},
)
SEARCH_LIMITS = (
    {
        "enabled": True,
        "metric": "drawdown_worst_strategy_eq",
        "penalize_if": "greater_than",
        "value": 0.60,
    },
    {
        "enabled": True,
        "metric": "backtest_completion_ratio",
        "penalize_if": "less_than",
        "value": 0.99,
    },
)
SEARCH_BOUNDS = {
    "long": {
        "risk": {
            "total_wallet_exposure_limit": [2.0, 3.0, 0.05],
            "n_positions": [5, 10, 1],
            "we_excess_allowance_pct": [0.0, 0.37, 0.01],
        },
        "hsl": {
            "red_threshold": [0.15, 0.35, 0.01],
            "ema_span_minutes": [15, 120, 15],
            "cooldown_minutes_after_red": [720, 4320, 720],
        },
    }
}
#: Flat optimizer bound key -> dotted config path, for the frozen candidates.
SEARCH_PARAM_PATHS = {
    "total_wallet_exposure_limit": TOTAL_WALLET_EXPOSURE_LIMIT_PATH,
    "n_positions": N_POSITIONS_PATH,
    "we_excess_allowance_pct": ALLOWANCE_PATH,
    "hsl_red_threshold": "bot.long.hsl.red_threshold",
    "hsl_ema_span_minutes": "bot.long.hsl.ema_span_minutes",
    "hsl_cooldown_minutes_after_red": "bot.long.hsl.cooldown_minutes_after_red",
}
#: The six dotted selectors the optimizer is allowed to tune. They are passed as
#: `-ft/--fine-tune-params`, which makes the optimizer fix *every other* bound to its current
#: config value (`optimize.py::_resolve_fine_tune_key_sets`), so the search cannot silently
#: wander into the alpha surface even though hydration expands `optimize.bounds` with the
#: engine's defaults. The run log then prints the tunable and fixed bound sets, and the search
#: driver refuses a run whose tunable set is not exactly these six.
SEARCH_FINE_TUNE_PARAMS = (
    TOTAL_WALLET_EXPOSURE_LIMIT_PATH,
    N_POSITIONS_PATH,
    ALLOWANCE_PATH,
    "bot.long.hsl.red_threshold",
    "bot.long.hsl.ema_span_minutes",
    "bot.long.hsl.cooldown_minutes_after_red",
)
#: Alpha / strategy surface the search must not touch. Listed explicitly so the frozen search
#: config can assert them pinned (`optimize.fixed_params`) and the report can state what stayed
#: constant; `-ft` is what enforces it.
SEARCH_FIXED_PARAMS = (
    "long_forager_score_weights_ema_readiness",
    "long_forager_score_weights_volatility",
    "long_forager_score_weights_volume",
    "long_forager_volatility_ema_span_1m",
    "long_forager_volume_drop_pct",
    "long_forager_volume_ema_span_1m",
    "long_unstuck_close_pct",
    "long_unstuck_ema_dist",
    "long_unstuck_ema_span_0",
    "long_unstuck_ema_span_1",
    "long_unstuck_loss_allowance_pct",
    "long_unstuck_threshold",
    "long_risk_entry_cooldown_minutes",
    "long_risk_position_exposure_enforcer_threshold",
    "long_risk_total_exposure_enforcer_threshold",
)
#: The declared fallback if the optimizer cannot run inside this host's memory budget: six
#: explicit joint cells that the Stage A/B grids do not already cover, evaluated through the
#: study pipeline itself (declared, bounded, and reported as the fallback path).
SEARCH_FALLBACK_CELLS = (
    {
        "total_wallet_exposure_limit": 2.5,
        "n_positions": 7,
        "we_excess_allowance_pct": 0.15,
        "hsl_red_threshold": 0.20,
        "hsl_ema_span_minutes": 60.0,
        "hsl_cooldown_minutes_after_red": 720.0,
    },
    {
        "total_wallet_exposure_limit": 2.0,
        "n_positions": 7,
        "we_excess_allowance_pct": 0.15,
        "hsl_red_threshold": 0.20,
        "hsl_ema_span_minutes": 60.0,
        "hsl_cooldown_minutes_after_red": 720.0,
    },
    {
        "total_wallet_exposure_limit": 2.5,
        "n_positions": 7,
        "we_excess_allowance_pct": 0.37,
        "hsl_red_threshold": 0.20,
        "hsl_ema_span_minutes": 60.0,
        "hsl_cooldown_minutes_after_red": 720.0,
    },
    {
        "total_wallet_exposure_limit": 2.0,
        "n_positions": 7,
        "we_excess_allowance_pct": 0.37,
        "hsl_red_threshold": 0.20,
        "hsl_ema_span_minutes": 60.0,
        "hsl_cooldown_minutes_after_red": 720.0,
    },
    {
        "total_wallet_exposure_limit": 3.0,
        "n_positions": 10,
        "we_excess_allowance_pct": 0.0,
        "hsl_red_threshold": 0.20,
        "hsl_ema_span_minutes": 60.0,
        "hsl_cooldown_minutes_after_red": 1440.0,
    },
    {
        "total_wallet_exposure_limit": 2.5,
        "n_positions": 10,
        "we_excess_allowance_pct": 0.0,
        "hsl_red_threshold": 0.20,
        "hsl_ema_span_minutes": 60.0,
        "hsl_cooldown_minutes_after_red": 1440.0,
    },
)

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
#: The leg whose window the parameter search is allowed to see.
SEARCH_LEG = "3y"

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
    "a_allow000": Lever(
        key="a_allow000",
        label="占用纪律：allowance=0（单币不得超过均分额度）+ 守护",
        question="不让任何币占用空槽的额度，能否在不牺牲生存的前提下把尾部压下来？",
        hypothesis="单币敞口与单币归零上界下降、峰值总暴露更均匀；代价可能是收益与在场币数",
    ),
    "a_allow010": Lever(
        key="a_allow010",
        label="占用纪律中间档：allowance=0.10 + 守护",
        question="allowance 从 0.37 收到 0.10 能拿到多少尾部改善？",
        hypothesis="介于 0 与 0.37 之间，用来画出“占用纪律”的边际曲线",
    ),
    "a_allow020": Lever(
        key="a_allow020",
        label="占用纪律中间档：allowance=0.20 + 守护",
        question="allowance 收到 0.20 是否已经足够？",
        hypothesis="若 0.20 与 0 的关键读数接近，则不必收到 0",
    ),
    "a_allow037": Lever(
        key="a_allow037",
        label="样本外腿基准：allowance=0.37 + 守护（与上一轮同形）",
        question="样本外窗口上，上一轮的守护形状本身表现如何？",
        hypothesis="这是 pre 腿唯一的基准列；3y/ext 用上一轮钉住的控制臂",
    ),
    "a_allow000_twe250": Lever(
        key="a_allow000_twe250",
        label="结构降杠杆：TWE 2.5 + allowance=0 + 守护",
        question="占用纪律之外再降 0.5 倍杠杆，能否用更小代价买到同样的生存？",
        hypothesis="到强平地板距离从约 −33% 放大到 −40%，但收益按比例下降",
    ),
    "a_allow000_twe200": Lever(
        key="a_allow000_twe200",
        label="结构降杠杆：TWE 2.0 + allowance=0 + 守护",
        question="TWE 2.0 是否已经进入“回撤可接受”的区域？",
        hypothesis="回撤显著收敛；测出“每 1pp 回撤放弃多少终值”的另一端",
    ),
    "b_cool24": Lever(
        key="b_cool24",
        label="冷却档位 24H（allowance=0 + 守护）",
        question="把停机从 12H 拉到 24H，在低集中度几何下是否更安全？",
        hypothesis="上一轮在 allowance=0.37 下 24H 更差；本轮检验是否被集中度掩盖",
    ),
    "b_cool48": Lever(
        key="b_cool48",
        label="冷却档位 48H（allowance=0 + 守护）",
        question="48H 档是否开始出现“错过反弹”的代价？",
        hypothesis="档位越高，离场时间越长；用于为下一轮引擎级阶梯定中间档",
    ),
    "b_cool72": Lever(
        key="b_cool72",
        label="冷却档位 72H（allowance=0 + 守护）",
        question="72H 档能否挡住 2021-05 的第二波？",
        hypothesis="若 72H 明显更好，则阶梯的最高档应设在 72H 附近",
    ),
    "b_term055": Lever(
        key="b_term055",
        label="累计二档：no_restart 0.55（allowance=0 + 守护）",
        question="把永久停机阈值设在一次急跌确认回撤（≈0.47）之上，能否只关停真正恶化的账户？",
        hypothesis="阈值高于单次确认回撤 ⇒ 不再首击锁存；但累计回撤仍可能很快触及",
    ),
    "b_term070": Lever(
        key="b_term070",
        label="累计二档：no_restart 0.70（allowance=0 + 守护）",
        question="阈值再抬高到 0.70，是否等价于“几乎不锁存”？",
        hypothesis="若 0.70 从不触发，则累计二档在本样本上没有可测效果，应改用已实现亏损口径",
    ),
    "b_lossgate050": Lever(
        key="b_lossgate050",
        label="实现亏损刹车：live.max_realized_loss_pct=0.50（allowance=0 + 守护）",
        question="在守护之上再加一层“不再兑现更深亏损”的闸门，是帮忙还是变成扛单？",
        hypothesis="它拦截非 panic 的亏损平仓（panic 豁免），可能减少底部割肉，也可能把仓位留到更差",
    ),
    "b_ema15": Lever(
        key="b_ema15",
        label="触发速度：EMA 15 分钟（allowance=0 + 守护）",
        question="低集中度几何下，“触发要快”还是必要条件吗？",
        hypothesis="更快触发更早离场；代价是更多假信号与更高离场频率",
    ),
    "b_red015": Lever(
        key="b_red015",
        label="更早熔断：RED 0.15（allowance=0 + 守护）",
        question="把熔断线从 20% 提前到 15%，是否比占用纪律更划算？",
        hypothesis="更早清仓减少最大回撤，但把更多浮亏变成实亏",
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
            "lookback_days": self.declared_lookback_days,
        }

    @property
    def guard_label(self) -> str:
        params = self.guard_params
        if not params["enabled"]:
            return "无守护（对照）"
        return (
            f"{params['scope']}：{params['red_threshold']:.2f} 清仓+停 "
            f"{params['cooldown_minutes_after_red'] / 60:.0f}H"
            f"（橙 {params['orange_threshold']:.2f} 停加仓，{params['lookback_days']:.0f} 天窗口，"
            f"EMA {params['ema_span_minutes']:.0f} 分钟"
            + (
                f"，累计 {params['no_restart_drawdown_threshold']:.2f} 永久停机"
                if params["no_restart_drawdown_threshold"] < 1.0
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


#: (lever, TWE, guard/exposure deltas, legs, human description)
_A_ARMS: tuple[tuple[str, float, tuple[tuple[str, Any, Any], ...], tuple[str, ...], str], ...] = (
    ("a_allow000", 3.0, _guard(), ("3y", "ext", "pre"), "TWE 3.0 + allowance=0 + 守护"),
    ("a_allow010", 3.0, _guard(allow=0.10), ("3y", "ext"), "TWE 3.0 + allowance=0.10 + 守护"),
    ("a_allow020", 3.0, _guard(allow=0.20), ("3y", "ext"), "TWE 3.0 + allowance=0.20 + 守护"),
    ("a_allow037", 3.0, _guard(allow=None), ("pre",), "TWE 3.0 + allowance=0.37（上一轮形状）"),
    (
        "a_allow000_twe250",
        2.5,
        _guard(),
        ("3y", "ext", "pre"),
        "TWE 2.5 + allowance=0 + 守护",
    ),
    ("a_allow000_twe200", 2.0, _guard(), ("3y", "ext"), "TWE 2.0 + allowance=0 + 守护"),
)
_B_ARMS: tuple[tuple[str, float, tuple[tuple[str, Any, Any], ...], tuple[str, ...], str], ...] = (
    ("b_cool24", 3.0, _guard(cooldown=1440.0), ("3y", "ext", "pre"), "allowance=0 + 停 24H"),
    ("b_cool48", 3.0, _guard(cooldown=2880.0), ("3y", "ext", "pre"), "allowance=0 + 停 48H"),
    ("b_cool72", 3.0, _guard(cooldown=4320.0), ("3y", "ext", "pre"), "allowance=0 + 停 72H"),
    ("b_term055", 3.0, _guard(no_restart=0.55), ("3y", "ext"), "allowance=0 + 累计 0.55 永久停机"),
    ("b_term070", 3.0, _guard(no_restart=0.70), ("3y", "ext"), "allowance=0 + 累计 0.70 永久停机"),
    (
        "b_lossgate050",
        3.0,
        (*_guard(), (MAX_REALIZED_LOSS_PATH, PARENT_MAX_REALIZED_LOSS_PCT, 0.5)),
        ("3y", "ext"),
        "allowance=0 + 实现亏损刹车 0.50",
    ),
    ("b_ema15", 3.0, _guard(ema=15.0), ("3y", "ext"), "allowance=0 + EMA 15 分钟"),
    ("b_red015", 3.0, _guard(red=None), ("3y", "ext"), "allowance=0 + RED 0.15（更早熔断）"),
)


def _noop(delta: tuple[str, Any, Any]) -> bool:
    _path, source, target = delta
    if isinstance(source, bool) or isinstance(target, bool):
        return source is target
    try:
        return abs(float(source) - float(target)) < 1e-12
    except (TypeError, ValueError):
        return source == target


def search_candidate_deltas(params: dict[str, Any]) -> tuple[tuple[str, Any, Any], ...]:
    """The declared deltas of one frozen search candidate (parent -> candidate values)."""
    native = {
        "total_wallet_exposure_limit": 1.0,
        "n_positions": PARENT_N_POSITIONS,
        "we_excess_allowance_pct": PARENT_ALLOWANCE_PCT,
        "hsl_red_threshold": 0.15,
        "hsl_ema_span_minutes": 720.0,
        "hsl_cooldown_minutes_after_red": 2160.0,
    }
    declared: list[tuple[str, Any, Any]] = [
        (STARTING_BALANCE_PATH, PARENT_STARTING_BALANCE, ARM_STARTING_BALANCE),
        *_UNIFIED,
        (PNLS_LOOKBACK_PATH, PARENT_PNLS_LOOKBACK_DAYS, GUARD_LOOKBACK_DAYS),
    ]
    for key, dotted in SEARCH_PARAM_PATHS.items():
        if key not in params:
            raise SystemExit(f"search candidate is missing the parameter {key!r}")
        value = params[key]
        if key == "n_positions":
            value = int(round(float(value)))
        else:
            value = float(value)
        declared.append((dotted, native[key], value))
    return tuple(delta for delta in declared if not _noop(delta))


def lever_for(lever_key: str) -> "Lever":
    """The declared lever, or a synthesized one for a search-derived arm.

    Arms selected by the parameter search appear only after the search has run, so they cannot be
    listed in `LEVERS`; this keeps their payload honest (a label plus the question they answer)
    without pretending they were pre-registered by hand.
    """
    lever = LEVERS.get(lever_key)
    if lever is not None:
        return lever
    selection = load_search_selection() or {}
    for candidate in selection.get("selected") or []:
        if candidate.get("lever") != lever_key:
            continue
        params = candidate.get("parameters") or {}
        kind = candidate.get("selection_kind", "candidate")
        return Lever(
            key=lever_key,
            label=(
                f"搜索候选（{kind}）：TWE {float(params.get('total_wallet_exposure_limit', 0)):.2f}"
                f" / 槽位 {int(params.get('n_positions', 0))}"
                f" / 占用余量 {float(params.get('we_excess_allowance_pct', 0)):.2f}"
            ),
            question="优化器在搜索窗里选出的风险几何点，在样本外腿上是否仍然成立？",
            hypothesis="搜索窗内的最优点不一定在崩盘腿上更优——这正是样本外验收窗要回答的问题",
        )
    raise KeyError(f"unknown lever {lever_key!r} (not declared and not in the search selection)")


def load_search_selection(path: Path | None = None) -> dict[str, Any] | None:
    """The frozen selection produced by the search step, or None before the search ran.

    This runs while the module is being imported (the registry is built from the selection), so it
    uses `json` directly instead of the `load_json` helper defined further down the file.
    """
    path = Path(path or SEARCH_SELECTION_PATH)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _search_candidate_specs() -> tuple[tuple[str, str, tuple[tuple[str, Any, Any], ...], str], ...]:
    selection = load_search_selection()
    if not selection:
        return ()
    specs: list[tuple[str, str, tuple[tuple[str, Any, Any], ...], str]] = []
    for candidate in selection.get("selected") or []:
        lever = str(candidate["lever"])
        deltas = search_candidate_deltas(candidate["parameters"])
        kind = candidate.get("selection_kind", "candidate")
        for leg in SEARCH_CANDIDATE_LEGS:
            specs.append(
                (
                    lever,
                    leg,
                    deltas,
                    f"搜索候选（{kind}）：TWE {candidate['parameters']['total_wallet_exposure_limit']:.2f}"
                    f" / allowance {candidate['parameters']['we_excess_allowance_pct']:.2f}"
                    f" / n_pos {int(candidate['parameters']['n_positions'])}"
                    f" / RED {candidate['parameters']['hsl_red_threshold']:.2f}"
                    f" / EMA {candidate['parameters']['hsl_ema_span_minutes']:.0f}"
                    f" / 停 {candidate['parameters']['hsl_cooldown_minutes_after_red'] / 60:.0f}H",
                )
            )
    return tuple(specs)


_ARM_SPECS: tuple[tuple[str, str, tuple[tuple[str, Any, Any], ...], str], ...] = tuple(
    (
        lever,
        leg,
        (*_capital(twe), *deltas),
        f"{description}｜{LEG_TITLES[leg]}",
    )
    for lever, twe, deltas, legs, description in (*_A_ARMS, *_B_ARMS)
    for leg in legs
) + _search_candidate_specs()

VARIANTS: tuple[Variant, ...] = tuple(
    _arm(lever, dataset_key, deltas, description)
    for lever, dataset_key, deltas, description in _ARM_SPECS
)
VARIANTS_BY_KEY: dict[str, Variant] = {variant.key: variant for variant in VARIANTS}
DEFAULT_VARIANT_ORDER = tuple(variant.key for variant in VARIANTS)
#: Every declared arm is run by this study; the four reference anchors are pinned evidence.
REUSED_ARM_KEYS: tuple[str, ...] = ()
RUN_VARIANT_ORDER = tuple(DEFAULT_VARIANT_ORDER)
STAGE_A_LEVERS = tuple(lever for lever, *_rest in _A_ARMS)
STAGE_B_LEVERS = tuple(lever for lever, *_rest in _B_ARMS)


def stage_of(variant: "Variant") -> str:
    """Which stage of the study an arm belongs to."""
    if variant.lever in STAGE_A_LEVERS:
        return "A"
    if variant.lever in STAGE_B_LEVERS:
        return "B"
    return "C"


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


def references_of_kind(kind: str) -> dict[str, dict[str, Any]]:
    """Pinned reference arms filtered by role (`no_guard` or `guard`)."""
    return {key: item for key, item in REFERENCE_RUNS.items() if item.get("kind") == kind}


#: Group prefix of the *logical* key each `optimize.bounds` leaf is reported under. The
#: optimizer's own flat namespace prefixes the hsl group (and spells the risk allowance
#: `risk_we_excess_allowance_pct`); the study reports the six dimensions under the logical names
#: `SEARCH_PARAM_PATHS` uses, and `SEARCH_PARAM_FLAT_KEYS` carries the optimizer spelling for
#: provenance.
SEARCH_PARAM_FLAT_KEYS = {
    "total_wallet_exposure_limit": "long_total_wallet_exposure_limit",
    "n_positions": "long_n_positions",
    "we_excess_allowance_pct": "long_risk_we_excess_allowance_pct",
    "hsl_red_threshold": "long_hsl_red_threshold",
    "hsl_ema_span_minutes": "long_hsl_ema_span_minutes",
    "hsl_cooldown_minutes_after_red": "long_hsl_cooldown_minutes_after_red",
}


def search_bound_leaves() -> dict[str, tuple[float, float, float]]:
    """`logical key -> (low, high, step)` view of the declared search bounds."""
    leaves: dict[str, tuple[float, float, float]] = {}
    for group_name, group in SEARCH_BOUNDS.items():
        for fields_name, fields in group.items():
            for key, bound in fields.items():
                logical = f"{fields_name}_{key}" if fields_name == "hsl" else key
                leaves[logical] = (float(bound[0]), float(bound[1]), float(bound[2]))
    return leaves
