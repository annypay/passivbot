#!/usr/bin/env python3
"""Single source of truth for the g4 @ TWE 3.0 single-coin stop-loss study.

The study replays one brand-new, **default-off** engine key on the previous round's validated
`a_allow000` geometry (allowance 0, TWE 3.0, unified guard RED 0.20 / EMA 60 min / 12h halt):
`bot.<pside>.stop_loss`, a whole-position reduce-only stop at `average entry x (1 - pct)` with an
entry cooldown after it fills.

**No parameter search.** `pct_from_avg_entry = 0.15` and `cooldown_minutes = 1440` are the values the
operator asked for, not a tuned optimum; every arm is declared by hand below, before it is run, and
that declaration is what the report is judged against.

Six levers x five legs (three real, two synthetic collapse) = 30 arms. The synthetic legs are the
only place a path that never bounces exists, so they are the only evidence for "does the stop buy
tail protection" -- the real legs can only say what it cost on paths that recovered.

Offline only: no network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_twe300_sl_cooldown_2026-09-18"
ARTIFACTS = STUDY / "artifacts"

DATA_EXCHANGE = "binance"
COIN_COUNT = 40

#: The frozen parent: the previous round's published `a_allow000` arm config, which already carries
#: the validated guard and exposure geometry. It predates `bot.<pside>.stop_loss`, so the derived
#: parent is that config plus the eight stop-loss paths at their engine defaults.
PARENT_CONFIG = (
    REPO / "backtests/binance/g4_twe300_risk_optimization_2026-09-17/artifacts/g4_a_allow000__3y.config.json"
)
PARENT_CONFIG_SHA256 = "e241b1191e07a9583c73c38a01d390ac28940c2a8d7e470cf83f6012d632174f"

#: Engine defaults, spelled exactly as `src/config/schema.py` and `passivbot-rust/src/types.rs` do.
STOP_LOSS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "pct_from_avg_entry": 0.15,
    "cooldown_minutes": 1440.0,
    "order_type": "market",
}
STOP_LOSS_LEAVES: tuple[str, ...] = tuple(STOP_LOSS_DEFAULTS)
#: The eight paths the derived parent adds (both position sides).
PARENT_DERIVATION_PATHS: tuple[str, ...] = tuple(
    f"bot.{pside}.stop_loss.{leaf}" for pside in ("long", "short") for leaf in STOP_LOSS_LEAVES
)
#: Flat spellings the Rust bridge consumes (`src/config/shared_bot.py`).
SL_FLAT_KEYS: tuple[str, ...] = (
    "stop_loss_enabled",
    "stop_loss_pct_from_avg_entry",
    "stop_loss_cooldown_minutes",
    "stop_loss_order_type",
)
#: Fill types the engine emits for a stop-out (`OrderType::CloseStopLoss{Long,Short}`).
SL_FILL_TYPES: tuple[str, ...] = ("close_stop_loss_long", "close_stop_loss_short")
#: The engine key this study exists to replay, in the spelling an operator types.
SL_GROUP_PREFIX = "bot.long.stop_loss"

#: What the analysis has to read per arm and leg. Thresholds live in `sl_replay.py`, not here: this
#: file declares the experiment, not the verdict.
SL_TAIL_KEYS: tuple[str, ...] = (
    "terminal_multiple",
    "terminal_multiple_ratio_vs_s0",
    "worst_drawdown_pct",
    "worst_drawdown_delta_pp_vs_s0",
    "stop_loss_fills_count",
    "stop_loss_realized_pnl_usd",
    "stop_loss_coins_count",
    "entries_inside_cooldown",
    "max_single_coin_loss_usd",
)

COMPARISON_METRIC_KEYS: tuple[str, ...] = (
    "gain_strategy_eq",
    "adg_strategy_eq",
    "mdg_strategy_eq",
    "drawdown_worst_strategy_eq",
    "drawdown_worst_mean_1pct_strategy_eq",
    "expected_shortfall_1pct_strategy_eq",
    "omega_ratio_strategy_eq",
    "sterling_ratio_strategy_eq",
    "loss_profit_ratio",
    "sharpe_ratio_usd",
    "sortino_ratio_usd",
    "calmar_ratio_usd",
    "total_wallet_exposure_max",
    "total_wallet_exposure_mean",
    "fills_count",
    "fills_count_entry",
    "fills_count_close",
    "fills_active_symbols_count",
    "position_held_hours_max",
    "position_held_hours_median",
    "strategy_eq_recovery_days_max",
    "strategy_eq_underwater_pct_mean",
    "liquidated",
    "n_days",
    "backtest_completion_ratio",
    "hard_stop_triggers",
    "hard_stop_restarts",
    "hard_stop_time_in_red_pct",
    "hard_stop_panic_close_loss_sum",
    "hard_stop_trigger_drawdown_mean",
    "hard_stop_ladder_strikes_max",
)

#: Plot groups every arm drops: the per-coin fill panels are a run's memory peak on this host.
DISABLED_PLOT_GROUPS = ("coin_fills",)
RUNTIME_FLAGS: tuple[str, ...] = ("--disable_plotting", "coin_fills")
EXECUTION = {"execution_delay_bars": 0, "intrabar_fill_order": "close_first"}
COSTS = {"maker_fee_override": 0.0002, "taker_fee_override": 0.0005, "market_order_slippage_pct": 0.0005}

NO_SEARCH: dict[str, Any] = {
    "search": None,
    "reason": (
        "本轮只验证一个默认关闭的引擎键（单币止损）；15% 与 1440 分钟是用户给定的机制参数，"
        "不做任何样本内择优，六个臂全部预注册"
    ),
    "precedent": (
        "上一轮把预算放在机制验证而非再搜一遍风险几何：搜索窗里 ADG 最高的候选在样本外腿被强平"
    ),
}


@dataclass(frozen=True)
class DatasetSpec:
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
        rel_path=Path("caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__8300950b42789a26"),
        window=("2023-09-12", "2026-09-12"),
        manifest_sha256="2c300e499577c2c75fac1009656780705854f1d7f90a7ffbf51cf657feca7366",
        manifest_config_hash="8300950b42789a26485595e34e0b106e7d1a084ad887730c53693a2453121792",
        override_mode=None,
        note="the profile's native bundle",
    ),
    "ext": DatasetSpec(
        key="ext",
        leg="ext",
        rel_path=Path("caches/hlcvs_data/binance__40_coins__2021-03-25_to_2026-09-13__a02b6ae1c140f2b7"),
        window=("2021-04-20", "2026-09-13"),
        manifest_sha256="d7e3b9756be4a0a2460b148de3753130f07f1840ac29d0403409d845fad32f8c",
        manifest_config_hash="a02b6ae1c140f2b77c92d88e4292426dd3e8d4c0c287f2671cd3c9668d639483",
        override_mode="dataset",
        note="full local history, contains the 2021-2022 tail",
    ),
    "pre": DatasetSpec(
        key="pre",
        leg="pre",
        rel_path=Path("caches/hlcvs_data/binance__40_coins__2021-03-25_to_2026-09-13__a02b6ae1c140f2b7"),
        window=("2021-04-20", "2023-09-11"),
        manifest_sha256="d7e3b9756be4a0a2460b148de3753130f07f1840ac29d0403409d845fad32f8c",
        manifest_config_hash="a02b6ae1c140f2b77c92d88e4292426dd3e8d4c0c287f2671cd3c9668d639483",
        override_mode="intersection",
        note="out-of-sample window: the search never saw it",
    ),
    # The two synthetic collapse legs, built by the previous round's tool over this study's own 3y
    # bundle. Their manifest hashes are intentionally unpinned: they are regenerable, and the pinned
    # evidence is the injection contract below plus the observed config hash recorded at build time.
    "synth_a": DatasetSpec(
        key="synth_a",
        leg="synth_a",
        rel_path=Path(
            "caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__synthetic_collapse_a"
        ),
        window=("2023-09-12", "2026-09-12"),
        manifest_sha256="",
        manifest_config_hash="",
        override_mode="dataset",
        synthetic=True,
        note="native bundle with the top-1 exposure coin collapsed and never recovered",
    ),
    "synth_b": DatasetSpec(
        key="synth_b",
        leg="synth_b",
        rel_path=Path(
            "caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__synthetic_collapse_b"
        ),
        window=("2023-09-12", "2026-09-12"),
        manifest_sha256="",
        manifest_config_hash="",
        override_mode="dataset",
        synthetic=True,
        note="native bundle with the top-3 exposure coins collapsed",
    ),
}

#: Injection contract, as executed by
#: `g4_tail_risk_research_2026-09-17/report_tools/make_synthetic_collapse_bundle.py`.
SYNTHETIC_INJECTIONS: dict[str, dict[str, Any]] = {
    "synth_a": {
        "label": "单币崩塌（合成 A，孤立事件）",
        "targets": 1,
        "collapse_days": 3,
        "end_multiple": 0.005,
        "hold_days": 14,
    },
    "synth_b": {
        "label": "三币先后崩塌（合成 B）",
        "targets": 3,
        "collapse_days": 3,
        "end_multiple": 0.005,
        "hold_days": 14,
    },
}
SYNTHETIC_BUILDER = "backtests/binance/g4_tail_risk_research_2026-09-17/report_tools/make_synthetic_collapse_bundle.py"

#: Honest boundaries of this round, stated up front so the report cannot quietly omit them.
HONESTY_BOUNDARIES: tuple[str, ...] = (
    "合成腿的注入目标币取自上一轮的 baseline run（g4_sma20_50_replay_2026-09-16），"
    "而不是本研究的 control 臂；目标选择与本研究无关，但它决定了哪几个币被砸。",
    "冻结配置声明 backtest.market_orders_allowed = false；止损的 market 档刻意不受该开关约束"
    "（它是保护性平仓，不是策略单），因此 market 档的 taker 费与滑点是被建模的、而不是被跳过的。",
    "引擎侧止损按采样价格触发，不按 K 线插针触发；Phase A 普查按 low（触及）统计，"
    "两者口径不同，Phase C 一律以引擎自身语义为准。",
    "全样本零 taker（28,550 笔全部 maker），真实行情下「看到价格就能平」不成立。",
)

LEG_ORDER: tuple[str, ...] = ("3y", "ext", "pre", "synth_a", "synth_b")
LEG_TITLES: dict[str, str] = {
    "3y": "原生窗腿（2023-09-12 → 2026-09-12，样本内）",
    "ext": "全历史腿（2021-04-20 → 2026-09-13）",
    "pre": "样本外腿（2021-04-20 → 2023-09-11）",
    "synth_a": "单币崩塌合成腿（原生窗 + 单币注入崩塌）",
    "synth_b": "三币崩塌合成腿（原生窗 + 三币注入崩塌）",
}
#: Memory guard per leg: the long bundles peak near 5 GB resident, the native window near 2.6 GB.
MIN_AVAILABLE_MB = {"3y": 2600, "ext": 5200, "pre": 5200, "synth_a": 2600, "synth_b": 2600}


@dataclass(frozen=True)
class Lever:
    key: str
    label: str
    question: str
    hypothesis: str
    stop_loss: dict[str, Any]


LEVERS: dict[str, Lever] = {
    "s0_off": Lever(
        key="s0_off",
        label="默认关闭回归臂：stop_loss.enabled = false",
        question="关掉时引擎是否与改动前逐位相同？",
        hypothesis="与上一轮 a_allow000 在成交签名与全部指标上逐位一致，且不产生任何止损成交",
        stop_loss={"enabled": False, "pct_from_avg_entry": 0.15, "cooldown_minutes": 1440.0, "order_type": "market"},
    ),
    "s1_sl15_cd1440": Lever(
        key="s1_sl15_cd1440",
        label="用户给定机制：均价 −15% 止损 + 24h 冷却（市价档）",
        question="用户要求的机制在真实腿上值多少、在合成崩塌腿上救多少？",
        hypothesis="真实腿变差（打断会反弹的深坑），合成腿尾部改善",
        stop_loss={"enabled": True, "pct_from_avg_entry": 0.15, "cooldown_minutes": 1440.0, "order_type": "market"},
    ),
    "s2_sl15_cd240": Lever(
        key="s2_sl15_cd240",
        label="冷却长度敏感性：−15% + 4h 冷却",
        question="24h 冷却本身贡献了多少（相对 4h）？",
        hypothesis="冷却越短越接近「没有冷却」，成本应更低而尾部保护更弱",
        stop_loss={"enabled": True, "pct_from_avg_entry": 0.15, "cooldown_minutes": 240.0, "order_type": "market"},
    ),
    "s3_sl25_cd1440": Lever(
        key="s3_sl25_cd1440",
        label="止损位敏感性：−25% + 24h 冷却",
        question="把止损位放到样本内触发频率更低的档位，代价是否变小？",
        hypothesis="触发更少、单次更深；成本下降但尾部保护也随之变薄",
        stop_loss={"enabled": True, "pct_from_avg_entry": 0.25, "cooldown_minutes": 1440.0, "order_type": "market"},
    ),
    "s4_sl15_cd1440_limit": Lever(
        key="s4_sl15_cd1440_limit",
        label="成交层敏感性：−15% + 24h 冷却（限价档）",
        question="挂在该位的限价单在瀑布里到底成不成交？",
        hypothesis="跳空穿价时不成交，仓位继续下探；因此限价档不应优于市价档",
        stop_loss={"enabled": True, "pct_from_avg_entry": 0.15, "cooldown_minutes": 1440.0, "order_type": "limit"},
    ),
    "s5_sl15_cd0": Lever(
        key="s5_sl15_cd0",
        label="隔离冷却：−15% 止损 + 无冷却",
        question="成本与收益里，哪一部分来自冷却而不是止损本身？",
        hypothesis="去掉冷却后成本下降；若止损的全部代价都消失，说明起作用的是冷却",
        stop_loss={"enabled": True, "pct_from_avg_entry": 0.15, "cooldown_minutes": 0.0, "order_type": "market"},
    ),
}

STAGE_BY_LEVER = {key: f"S{index}" for index, key in enumerate(LEVERS)}


@dataclass(frozen=True)
class ReferenceRun:
    key: str
    leg: str
    run_dir: Path
    analysis_sha256: str
    role: str


PRIOR_STUDY = REPO / "backtests/binance/g4_twe300_risk_optimization_2026-09-17"
REFERENCE_RUNS: dict[str, ReferenceRun] = {
    "a_allow000__3y": ReferenceRun(
        key="a_allow000__3y",
        leg="3y",
        run_dir=PRIOR_STUDY
        / "artifacts/a_allow000__3y/backtest_results/binance_a_allow000__3y/binance/2026-09-17T10_49_03",
        analysis_sha256="8726a5b8457abddec6dd2fa174f6ddaf49c056118d1a56a2bdfa2f79b125b78f",
        role="默认关闭回归基准（3y）",
    ),
    "a_allow000__ext": ReferenceRun(
        key="a_allow000__ext",
        leg="ext",
        run_dir=PRIOR_STUDY
        / "artifacts/a_allow000__ext/backtest_results/binance_a_allow000__ext/binance/2026-09-17T10_50_30",
        analysis_sha256="615b449134b8b7a9077d43a23b54f08267154aaf303ececf09fc75c547983c97",
        role="默认关闭回归基准（ext，含 2021-2022 尾部）",
    ),
    "a_allow000__pre": ReferenceRun(
        key="a_allow000__pre",
        leg="pre",
        run_dir=PRIOR_STUDY
        / "artifacts/a_allow000__pre/backtest_results/binance_a_allow000__pre/binance/2026-09-17T10_47_03",
        analysis_sha256="c6f000472e15610118c1eded61f9d35e1a01f70329528a3fd1062cb260636c32",
        role="默认关闭回归基准（pre，样本外；额外证据）",
    ),
}
REFERENCE_CONTROL_BY_LEG = {"3y": "a_allow000__3y", "ext": "a_allow000__ext", "pre": "a_allow000__pre"}


@dataclass(frozen=True)
class Variant:
    lever: str
    dataset_key: str
    deltas: tuple[tuple[str, Any, Any], ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return f"{self.lever}__{self.dataset_key}"

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
    def declared_stop_loss(self) -> dict[str, Any]:
        return dict(LEVERS[self.lever].stop_loss)

    @property
    def bundle_dir(self) -> Path:
        return ARTIFACTS / self.key

    @property
    def base_dir(self) -> Path:
        return self.bundle_dir / f"backtest_results/{DATA_EXCHANGE}_{self.key}"

    @property
    def runs_base(self) -> Path:
        return self.base_dir / DATA_EXCHANGE

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
    def description(self) -> str:
        return f"{LEVERS[self.lever].label}｜{LEG_TITLES[self.leg]}"

    def relative(self, path: Path | None = None) -> str:
        return relative(path if path is not None else self.bundle_dir)


def _deltas(stop_loss: dict[str, Any]) -> tuple[tuple[str, Any, Any], ...]:
    """The declared change for every stop-loss leaf on the long side.

    Declaring a leaf even when the arm keeps the default is deliberate: it makes "this arm pins
    `order_type = market`" an assertion rather than an omission, and the identity gate then proves the
    short side was left alone.
    """
    return tuple(
        (f"bot.long.stop_loss.{leaf}", STOP_LOSS_DEFAULTS[leaf], stop_loss[leaf])
        for leaf in STOP_LOSS_LEAVES
    )


VARIANTS: tuple[Variant, ...] = tuple(
    Variant(lever=lever, dataset_key=leg, deltas=_deltas(LEVERS[lever].stop_loss))
    for leg in LEG_ORDER
    for lever in LEVERS
)
VARIANTS_BY_KEY: dict[str, Variant] = {variant.key: variant for variant in VARIANTS}
#: Every leg runs `s0_off` first, so a leg's control exists before anything is compared to it.
RUN_VARIANT_ORDER: tuple[str, ...] = tuple(
    variant.key for leg in LEG_ORDER for variant in VARIANTS if variant.leg == leg
)
STAGES: tuple[str, ...] = tuple(STAGE_BY_LEVER[lever] for lever in LEVERS)

VARIANT_INPUT_PATH = ARTIFACTS / "variant_input.json"


def stage_of(variant: Variant) -> str:
    return STAGE_BY_LEVER[variant.lever]


def reference_for_leg(leg: str) -> ReferenceRun | None:
    key = REFERENCE_CONTROL_BY_LEG.get(leg)
    return REFERENCE_RUNS[key] if key else None


def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def get_path(config: dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        node = node[part]
    return node


def has_path(config: dict[str, Any], dotted: str) -> bool:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def set_path(config: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(node, dict):
        for key in node:
            out.update(flatten(node[key], f"{prefix}.{key}" if prefix else str(key)))
    else:
        out[prefix] = node
    return out


def numeric_equal(left: Any, right: Any, rel_tol: float = 1e-12) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if not (math.isfinite(float(left)) and math.isfinite(float(right))):
            return float(left) == float(right)
        return math.isclose(float(left), float(right), rel_tol=rel_tol, abs_tol=1e-15)
    return left == right


def compare_subtrees(expected: Any, actual: Any, root: str) -> list[str]:
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


def relative(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)


def derive_parent_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """The frozen parent config plus the eight stop-loss paths at their engine defaults."""
    raw = load_json(PARENT_CONFIG)
    derived = json.loads(json.dumps(raw))
    for pside in ("long", "short"):
        derived["bot"][pside].setdefault("stop_loss", {})
    for path in PARENT_DERIVATION_PATHS:
        set_path(derived, path, STOP_LOSS_DEFAULTS[path.rsplit(".", 1)[1]])
    return raw, derived
