#!/usr/bin/env python3
"""Freeze every arm of the g4 @ TWE 3.0 HSL halt-ladder study and gate the claims it makes.

Writes, next to each other:

* `artifacts/g4_<arm>.config.json` - the frozen parent g4 config plus that arm's declared changes
  (starting capital, the previous round's `a_allow000` geometry and guard, and the two engine keys
  this round turns on: `bot.long.hsl.halt_ladder_minutes` and
  `bot.long.hsl.realized_loss_budget_pct`),
* `artifacts/variant_input.json` - the pre-registered arm table: declared `(path, from, to)`
  triples, the parent derivation that adds the two engine defaults, pinned parent/anchor hashes,
  dataset identities for all three legs, the capital/liquidation contract, the ladder contract,
  the event rule, the wipe-out levels, and the decision rules the report must apply.

This round declares **no parameter search**: there is no search contract, no candidate registry and
no selection file, and `run.sh` has no search stage. The whole experimental design is the declared
delta table below plus the pre-registered criteria in `halt_ladder_design.md` 7.

The gates here are the study's scientific claim, so they are checked rather than assumed: the
parent config must hash to the pinned value, every reference analysis must hash to its pinned
value, the frozen bundles must carry the declared windows (the out-of-sample leg is clipped by the
`intersection` override, so its bundle must *cover* the window), the parent derivation must add
exactly the two declared engine defaults, and every arm must differ from the parent by its declared
paths alone - with the declared `from` value checked against the parent, the capital, the risk
block, the HSL block, the modelled `live.hsl_signal_mode`, the exposure geometry and the
execution/cost contract asserted field by field.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import math
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_spec as spec  # noqa: E402

#: Decision rules, pre-registered before any arm is run (the four claims of
#: `halt_ladder_design.md` 7, plus the default-off regression the other four depend on). The report
#: must apply these literally and state the verdict for each one.
DECISION_RULES = (
    {
        "id": "K0",
        "rule": "默认关闭回归：`l0_off` 在三条腿上必须与上一轮同臂（`a_allow000`）钉住的 evidence "
        "**逐位一致**——成交签名 sha256、终值、最差回撤、全部 `hard_stop_*` 读数；任何一项不等 ⇒ "
        "判定引擎在默认值下发生了漂移，本轮其余结论全部作废（先修引擎，再读阶梯/预算）。",
    },
    {
        "id": "K1",
        "rule": "阶梯生效：某条腿上 `l1_ladder` 的 `hard_stop_ladder_strikes_max >= 2` 时，"
        "`hard_stop_duration_minutes_max` 必须等于 1440（第 2 次触发取第 2 档），且不低于 "
        "`l0_off` 同腿的读数；若 `strikes_max <= 1` ⇒ 该腿未观察到阶梯，只能报“未观察到”，"
        "不得外推。",
    },
    {
        "id": "K2",
        "rule": "累计已实现亏损口径：`l2_budget` 若锁存永久停机，"
        "`hard_stop_realized_loss_halt_pct_max >= 0.30` 且该读数可由该腿的成交账本与权益序列复算；"
        "若从不锁存 ⇒ 报“本段历史未触及 30% 累计已实现预算”，并给出 "
        "`hard_stop_realized_loss_halt_pct_max` 的最大观测值作为余量。"
        "**测量局限**：停机原因字符串 `no_restart_reason` 只在实盘状态与 `hsl-startup-preview` 暴露，"
        "回测的 `analysis.json` 不导出它（`passivbot-rust/src/backtest.rs` 只用它设置上面的读数），"
        "因此报告不得声称“读到了原因是 realized_loss”，只能报该读数与是否发生锁存。",
    },
    {
        "id": "K3",
        "rule": "阶梯 vs 单纯加长冷却：`l1_ladder` 与 `l3_fixed24` 的差异必须**只**来自“第几次触发”"
        "——在 `strikes_max <= 1` 的腿上两臂必须逐位相同；若不同 ⇒ 该腿上的阶梯结论与冷却长度混淆，"
        "判定不可分辨。",
    },
    {
        "id": "K4",
        "rule": "定位裁决：阶梯是**风险塑形**、不是收益改进（`halt_ladder_design.md` 1）。报告必须"
        "并排给出三条腿的终值、最差回撤、停机次数与档位，并对“阶梯是否只塑形风险”给出裁决；"
        "任何“更优”表述都必须同时给出 `pre` 腿读数。",
    },
)
#: The boundaries the report must state alongside its numbers.
HONESTY_BOUNDARIES = (
    "本轮**没有参数搜索**：四个臂全部预注册，看到任何一条腿的结果之后不得新增臂、改档位或调阈值。",
    "阶梯是**风险塑形**，不是收益改进：上一轮已实测加长冷却在两条腿上都不更优"
    "（ext 腿 12H/24H/48H/72H 终值 2.3002× / 2.7662× / 2.0828× / 1.8666×，pre 腿单调变差"
    "0.6104× → 0.4832×），所以最高档取证据支持的 24H，且默认关闭。",
    "两个新键都是 trading-critical 的 Rust 行为变更：默认关闭，`l0_off` 的逐位一致是本轮能给出的"
    "“默认值无副作用”证据，它建立在同一条腿、同一份 bundle、同一份冻结父配置上。",
    "触发次数很少：5.4 年腿一共只有 4–5 次守护触发，`pre` 腿只有 2.4 年；涉及“第 N 次触发”的结论"
    "都建立在这几次之上，必须标注样本量。",
    "阶梯周期峰值受 `live.pnls_max_lookback_days`（本轮 7 天）的滚动窗口约束：窗口外的旧高点不参与"
    "周期判定——这是与既有 no-restart 峰值**一致**的已知边界，不是本轮新引入的近似。",
    "`no_restart_drawdown_threshold = 1` 是合法上界，钉住的父配置本来就是该值；`l2_budget` 显式"
    "声明它，用来把永久停机完全交给累计已实现亏损判据（设计契约 3.2）。",
    "回测不导出停机原因字符串 `no_restart_reason`（它只在实盘状态与 `hsl-startup-preview` 可见）；"
    "能读到的只有 `hard_stop_realized_loss_halt_pct_max` 与 `hard_stop_restarts`，"
    "因此“为什么锁存”的归因必须留作机制推断并显式标注。",
    "历史范围有限：币池是活到 2026 年的当前 top40（幸存者偏差），长腿 2021-04-20 起交易时仅 22 个"
    "币有数据。",
    "未建模：真实资金费率、滑点枯竭、API 断连、交易所/稳定币对手方风险；引擎不模拟保证金占用。",
)


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def require_inputs() -> None:
    problems = []
    for path in (spec.SOURCE_CONFIG, spec.SOURCE_PROFILE_INPUT):
        if not path.exists():
            problems.append(f"missing input {spec.relative(path)}")
    for key, item in spec.REFERENCE_RUNS.items():
        run_dir = Path(item["run_dir"])
        if not (run_dir / "analysis.json").exists():
            problems.append(f"missing reference analysis for {key}: {spec.relative(run_dir)}")
    for key, dataset in spec.DATASETS.items():
        for name in spec.FROZEN_CACHE_FILES:
            if not (dataset.path / name).exists():
                problems.append(f"missing dataset file {dataset.rel_path / name}")
    if problems:
        fail(problems, "required inputs are not present")


def validate_pins() -> None:
    problems = []
    checks = [
        (spec.SOURCE_CONFIG, spec.SOURCE_CONFIG_SHA256, "frozen parent config"),
        (spec.SOURCE_PROFILE_INPUT, spec.SOURCE_PROFILE_INPUT_SHA256, "parent profile input"),
    ]
    checks.extend(
        (
            Path(item["run_dir"]) / "analysis.json",
            item["analysis_sha256"],
            f"reference analysis {key}",
        )
        for key, item in spec.REFERENCE_RUNS.items()
    )
    checks.extend(
        (dataset.path / "manifest.json", dataset.manifest_sha256, f"{key} manifest")
        for key, dataset in spec.DATASETS.items()
    )
    for path, expected, label in checks:
        actual = spec.sha256_file(path)
        if actual != expected:
            problems.append(
                f"{label} drift: {spec.relative(path)} has sha256 {actual}, expected {expected}"
            )
    if problems:
        fail(problems, "a pinned input changed; this study compares against frozen artifacts")
    print("gate: pinned parent config, profile input, reference analyses and dataset manifests match")


def validate_dataset_manifests() -> None:
    """Every declared leg must be covered by its frozen bundle exactly as declared.

    A `dataset` override serves the bundle's own window, so the declared window must equal it.
    An `intersection` override clips to the requested window, so the bundle only has to cover
    it — that is what makes the out-of-sample leg possible without a second bundle.
    """
    problems = []
    for key, dataset in spec.DATASETS.items():
        manifest = spec.load_json(dataset.path / "manifest.json")
        if manifest.get("config_hash") != dataset.manifest_config_hash:
            problems.append(
                f"{key} manifest config_hash {manifest.get('config_hash')!r} != pinned "
                f"{dataset.manifest_config_hash!r}"
            )
        requested = manifest.get("requested") or {}
        window_start = spec.day_of(_iso(requested.get("start_ts")))
        if window_start != dataset.window[0]:
            problems.append(
                f"{key} requested start {window_start!r} != declared window start "
                f"{dataset.window[0]!r}"
            )
        effective = manifest.get("effective") or {}
        bundle_start = spec.day_of(_iso(effective.get("start_ts")))
        bundle_end = spec.day_of(_iso(effective.get("end_ts")))
        if dataset.override_mode == "intersection":
            if bundle_start > dataset.window[0] or bundle_end < dataset.window[1]:
                problems.append(
                    f"{key}: the {dataset.override_mode} override needs a bundle covering "
                    f"{dataset.window!r}, but it covers {(bundle_start, bundle_end)!r}"
                )
        else:
            if bundle_end != dataset.window[1]:
                problems.append(
                    f"{key} effective end {bundle_end!r} != declared window end "
                    f"{dataset.window[1]!r}"
                )
    if problems:
        fail(problems, "dataset manifests do not match the declared legs")
    print("gate: every frozen bundle carries (or covers) its declared window and config hash")


def _iso(ts_ms: object) -> str:
    """UTC day of a millisecond timestamp, without importing the engine's date helpers."""
    import datetime as dt

    if ts_ms is None:
        return ""
    return dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def derive_parent_config(raw: dict) -> tuple[dict, list[dict]]:
    """The frozen parent of this study: the pinned g4 config plus the two declared engine keys.

    The pinned profile was frozen before `halt_ladder_minutes` and `realized_loss_budget_pct`
    existed, so the parent of *this* round cannot be that file verbatim: an arm has to be able to
    declare `(path, from, to)` against a parent that really carries both keys, and the identity
    gate has to be able to see a *changed* key as a difference. The derivation is therefore
    explicit, adds **exactly** the declared defaults (gated against the raw file), and is recorded
    in `variant_input.json` under `parent_derivation`.
    """
    derived = deepcopy(raw)
    derivation: list[dict] = []
    problems: list[str] = []
    for dotted, default in spec.PARENT_ENGINE_DEFAULTS:
        try:
            present = spec.get_path(derived, dotted)
        except KeyError:
            present = None
        else:
            problems.append(
                f"{dotted}: already present in the pinned parent with value {present!r}; the "
                "derivation must not overwrite a pinned value"
            )
            continue
        spec.set_path(derived, dotted, deepcopy(default))
        derivation.append({"path": dotted, "from": None, "to": deepcopy(default)})
    if problems:
        fail(problems, "the pinned parent already carries a key this study claims to add")
    declared = tuple(item["path"] for item in derivation)
    problems = spec.diff_subtrees(raw.get("bot"), derived.get("bot"), "bot", allowed=declared)
    if problems:
        fail(problems, "the parent derivation changed more than the two declared engine defaults")
    print(
        "gate: the frozen parent is the pinned g4 config plus exactly the declared engine defaults "
        f"({', '.join(declared)})"
    )
    return derived, derivation


def gate_declared_from(parent: dict, variant: spec.Variant) -> None:
    """Every declared change must quote the frozen parent's own value in its ``from`` field."""
    problems = []
    for dotted, source, _target in variant.deltas:
        try:
            actual = spec.get_path(parent, dotted)
        except KeyError:
            problems.append(f"{dotted}: declared by the arm but absent from the frozen parent")
            continue
        if actual != source and not spec.numeric_equal(actual, source):
            problems.append(
                f"{dotted}: declared from {source!r}, but the frozen parent has {actual!r}"
            )
    if problems:
        fail(problems, f"{variant.key}: the declared (path, from, to) triple is not honest")


def validate_parent_config(parent: dict) -> list[str]:
    """The parent config must carry the contract this study claims it carries."""
    problems = []
    backtest = parent.get("backtest") or {}
    for key, expected in (*spec.EXECUTION.items(), *spec.COSTS.items()):
        if not spec.numeric_equal(backtest.get(key), expected):
            problems.append(f"backtest.{key}: expected {expected!r}, got {backtest.get(key)!r}")
    if backtest.get("exchanges") != [spec.DATA_EXCHANGE]:
        problems.append(
            f"backtest.exchanges = {backtest.get('exchanges')!r}, expected "
            f"[{spec.DATA_EXCHANGE!r}]"
        )
    window = (backtest.get("start_date"), backtest.get("end_date"))
    if window != spec.DATASETS["3y"].window:
        problems.append(f"backtest window {window!r} != {spec.DATASETS['3y'].window!r}")
    cache_dir = (backtest.get("cache_dir") or {}).get(spec.DATA_EXCHANGE)
    if cache_dir != spec.relative(spec.DATASETS["3y"].path):
        problems.append(
            f"backtest.cache_dir[{spec.DATA_EXCHANGE!r}] = {cache_dir!r}, expected the frozen "
            f"bundle {spec.relative(spec.DATASETS['3y'].path)!r}"
        )
    gate = backtest.get("entry_regime_gate")
    if not isinstance(gate, dict) or not gate.get("enabled"):
        problems.append(f"backtest.entry_regime_gate = {gate!r}; the gate must stay enabled")
    else:
        problems.extend(
            spec.compare_subtrees(spec.GATE, spec.gate_semantics(gate), "backtest.entry_regime_gate")
        )
    coins = (backtest.get("coins") or {}).get(spec.DATA_EXCHANGE) or []
    live_long = ((parent.get("live") or {}).get("approved_coins") or {}).get("long") or []
    if len(coins) != spec.COIN_COUNT or sorted(coins) != sorted(live_long):
        problems.append(
            f"backtest.coins has {len(coins)} entries and live.approved_coins.long has "
            f"{len(live_long)}; expected the same frozen {spec.COIN_COUNT}-coin basket"
        )
    if list(((parent.get("live") or {}).get("approved_coins") or {}).get("short") or []):
        problems.append("live.approved_coins.short is not empty; the profile is long-only")
    hsl = ((parent.get("bot") or {}).get("long") or {}).get("hsl") or {}
    problems.extend(spec.compare_subtrees(spec.HSL_BLOCK, hsl, "bot.long.hsl"))
    live = parent.get("live") or {}
    for key, expected in spec.LIVE_HSL.items():
        actual = live.get(key)
        if actual != expected:
            problems.append(f"live.{key}: expected {expected!r}, got {actual!r}")
    if not spec.numeric_equal(live.get("max_realized_loss_pct"), spec.PARENT_MAX_REALIZED_LOSS_PCT):
        problems.append(
            f"live.max_realized_loss_pct = {live.get('max_realized_loss_pct')!r}, expected "
            f"{spec.PARENT_MAX_REALIZED_LOSS_PCT!r} (the disabled default)"
        )
    risk = ((parent.get("bot") or {}).get("long") or {}).get("risk") or {}
    actual_risk = {key: risk.get(key) for key in spec.PARENT_RISK}
    problems.extend(spec.compare_subtrees(spec.PARENT_RISK, actual_risk, "bot.long.risk"))
    if not spec.numeric_equal(
        live.get("pnls_max_lookback_days"), spec.PARENT_PNLS_LOOKBACK_DAYS
    ):
        problems.append(
            f"live.pnls_max_lookback_days = {live.get('pnls_max_lookback_days')!r}, expected "
            f"{spec.PARENT_PNLS_LOOKBACK_DAYS!r}; the guard study declares its own peak window"
        )
    if not spec.numeric_equal(backtest.get("starting_balance"), spec.PARENT_STARTING_BALANCE):
        problems.append(
            f"backtest.starting_balance = {backtest.get('starting_balance')!r}, expected "
            f"{spec.PARENT_STARTING_BALANCE!r}; this study's scale control depends on it"
        )
    if not spec.numeric_equal(
        backtest.get("liquidation_threshold"), spec.PARENT_LIQUIDATION_THRESHOLD
    ):
        problems.append(
            f"backtest.liquidation_threshold = {backtest.get('liquidation_threshold')!r}, "
            f"expected {spec.PARENT_LIQUIDATION_THRESHOLD!r}; the liquidation floor is part of "
            "the reported contract"
        )
    if backtest.get("filter_by_min_effective_cost") not in (False, None):
        problems.append(
            "backtest.filter_by_min_effective_cost must stay disabled: the live admission "
            "rule is reported separately by the affordability appendix"
        )
    if problems:
        fail(problems, "the parent config does not carry the declared contract")
    print(
        "gate: parent config keeps the execution/cost contract, the 20/50 gate, the risk block, "
        "the 100k capital and the 5% liquidation threshold"
    )
    return problems


def dataset_retarget(frozen: dict, dataset: spec.DatasetSpec) -> None:
    """Point one arm at its frozen bundle, keeping the declaration explicit."""
    if dataset.override_mode is None:
        return
    backtest = frozen["backtest"]
    backtest["cache_dir"][spec.DATA_EXCHANGE] = spec.relative(dataset.path)
    backtest["hlcvs_data_dir"] = spec.relative(dataset.path)
    backtest["hlcvs_data_override_mode"] = dataset.override_mode
    backtest["start_date"] = dataset.window[0]
    backtest["end_date"] = dataset.window[1]


def build_arm(parent: dict, variant: spec.Variant) -> dict:
    frozen = deepcopy(parent)
    frozen["backtest"]["base_dir"] = spec.relative(variant.base_dir)
    dataset_retarget(frozen, variant.dataset)
    for dotted, _from, to in variant.deltas:
        spec.set_path(frozen, dotted, to)
    return frozen


def gate_arm_identity(parent: dict, frozen: dict, variant: spec.Variant) -> None:
    """Prove the frozen config is the parent config plus exactly the declared changes."""
    problems: list[str] = []
    # The arm's own `backtest` declarations (starting capital) are legitimate diffs; anything
    # else in `backtest` beyond the output location / dataset override is not.
    allowed_backtest = spec.allowed_backtest_paths(variant)
    problems.extend(
        spec.diff_subtrees(
            parent["backtest"], frozen["backtest"], "backtest", allowed=allowed_backtest
        )
    )
    declared_in_roots: dict[str, tuple[str, ...]] = {}
    for dotted, _from, to in variant.deltas:
        root = dotted.split(".")[0]
        declared_in_roots[root] = (*declared_in_roots.get(root, ()), dotted)
    for root in spec.IDENTITY_ROOTS:
        if root not in parent and root not in frozen:
            continue
        problems.extend(
            spec.diff_subtrees(
                parent.get(root),
                frozen.get(root),
                root,
                allowed=declared_in_roots.get(root, ()),
            )
        )
    expected_base_dir = spec.relative(variant.base_dir)
    if frozen["backtest"].get("base_dir") != expected_base_dir:
        problems.append(
            f"backtest.base_dir: expected {expected_base_dir!r}, "
            f"got {frozen['backtest'].get('base_dir')!r}"
        )
    # Every declared change must be present with its declared value.
    for dotted, _from, to in variant.deltas:
        actual = spec.get_path(frozen, dotted)
        if actual != to and not spec.numeric_equal(actual, to):
            problems.append(f"{dotted}: expected {to!r}, got {actual!r}")
    # ... and the surrounding blocks must be exactly the expected blocks.
    hsl = ((frozen.get("bot") or {}).get("long") or {}).get("hsl") or {}
    problems.extend(spec.compare_subtrees(spec.expected_hsl_block(variant), hsl, "bot.long.hsl"))
    live = frozen.get("live") or {}
    for key, expected in spec.expected_live_hsl(variant).items():
        if live.get(key) != expected:
            problems.append(f"live.{key}: expected {expected!r}, got {live.get(key)!r}")
    for key, expected in spec.expected_live_config(variant).items():
        if not spec.numeric_equal(live.get(key), expected):
            problems.append(f"live.{key}: expected {expected!r}, got {live.get(key)!r}")
    risk = ((frozen.get("bot") or {}).get("long") or {}).get("risk") or {}
    expected_risk = spec.expected_risk_block(variant)
    problems.extend(
        spec.compare_subtrees(
            expected_risk, {key: risk.get(key) for key in expected_risk}, "bot.long.risk"
        )
    )
    if variant.dataset.override_mode:
        backtest = frozen["backtest"]
        if backtest.get("hlcvs_data_dir") != spec.relative(variant.dataset.path):
            problems.append(
                f"backtest.hlcvs_data_dir: expected {spec.relative(variant.dataset.path)!r}, "
                f"got {backtest.get('hlcvs_data_dir')!r}"
            )
        if backtest.get("hlcvs_data_override_mode") != variant.dataset.override_mode:
            problems.append(
                f"backtest.hlcvs_data_override_mode: expected "
                f"{variant.dataset.override_mode!r}, got {backtest.get('hlcvs_data_override_mode')!r}"
            )
        if not spec.same_day(backtest.get("start_date"), variant.dataset.window[0]):
            problems.append(
                f"backtest.start_date: expected {variant.dataset.window[0]!r}, "
                f"got {backtest.get('start_date')!r}"
            )
        if not spec.same_day(backtest.get("end_date"), variant.dataset.window[1]):
            problems.append(
                f"backtest.end_date: expected {variant.dataset.window[1]!r}, "
                f"got {backtest.get('end_date')!r}"
            )
    guard = variant.guard_params
    problems.extend(
        f"guard: {problem}"
        for problem in (
            []
            if not guard["enabled"]
            else [
                problem
                for problem in (
                    None
                    if guard["red_threshold"] <= guard["no_restart_drawdown_threshold"] <= 1.0
                    else "red_threshold must be <= no_restart_drawdown_threshold <= 1.0",
                    None
                    if 0.0 < guard["orange_threshold"] < guard["red_threshold"]
                    else "orange tier must sit strictly below the red threshold",
                    None
                    if 0.0 < guard["yellow_ratio"] < guard["orange_ratio"] < 1.0
                    else "the engine requires 0 < yellow < orange < 1 in tier_ratios",
                    None
                    if guard["ema_span_minutes"] > 0.0
                    else "ema_span_minutes must be positive",
                    None
                    if guard["cooldown_minutes_after_red"] >= 0.0
                    else "cooldown_minutes_after_red must be >= 0",
                    None
                    if 0.0 < guard["lookback_days"] <= spec.PARENT_PNLS_LOOKBACK_DAYS
                    else "lookback_days must be within the parent window",
                    None
                    if all(
                        math.isfinite(float(rung)) and float(rung) >= 0.0
                        for rung in guard["halt_ladder_minutes"]
                    )
                    and len(guard["halt_ladder_minutes"]) <= spec.MAX_HALT_LADDER_RUNGS
                    else "halt_ladder_minutes must be at most "
                    f"{spec.MAX_HALT_LADDER_RUNGS} finite minutes >= 0",
                    None
                    if 0.0 <= guard["realized_loss_budget_pct"] <= 1.0
                    else "realized_loss_budget_pct must be within [0, 1]",
                    None
                    if guard["realized_loss_budget_pct"] == 0.0
                    or guard["restart_after_red_policy"] == "threshold"
                    else "a realized-loss budget only latches under "
                    "restart_after_red_policy == 'threshold'",
                )
                if problem is not None
            ]
        )
    )
    if frozen["backtest"].get("disable_plotting") not in (None, False, "", [], ()):
        problems.append(
            "backtest.disable_plotting must stay at the parent's value: this engine "
            "revision only honours the runtime flag, so a frozen config value would be "
            f"silently ignored (got {frozen['backtest'].get('disable_plotting')!r})"
        )
    if problems:
        fail(problems, f"{variant.key}: the frozen config is not the parent config plus its delta")


def gate_arm_geometry(variant: spec.Variant) -> None:
    """The exposure-geometry declaration must stay inside the engine's contract and the plan."""
    problems: list[str] = []
    twe = variant.declared_twe
    slots = variant.declared_n_positions
    allowance = variant.declared_allowance_pct
    if not (1.0 <= twe <= 3.0):
        problems.append(f"total_wallet_exposure_limit {twe} is outside the declared [1.0, 3.0]")
    if slots < 1 or abs(slots - round(slots)) > 1e-9:
        problems.append(f"n_positions {slots} must be a positive integer")
    if not (0.0 <= allowance <= spec.PARENT_ALLOWANCE_PCT):
        problems.append(
            f"we_excess_allowance_pct {allowance} is outside [0, {spec.PARENT_ALLOWANCE_PCT}]"
        )
    if twe / slots <= 0.0:
        problems.append("the per-slot budget TWE / n_positions must be positive")
    if spec.get_path(spec.load_json(variant.config_path), spec.ALLOWANCE_MODE_PATH) != "bounded":
        problems.append(
            f"{spec.ALLOWANCE_MODE_PATH} must stay 'bounded': the reported per-slot cap is the "
            "bounded-mode value"
        )
    if variant.lever not in spec.STAGE_BY_LEVER:
        problems.append(
            f"{variant.key} declares lever {variant.lever!r}, which is not one of the "
            f"pre-registered levers {sorted(spec.STAGE_BY_LEVER)}"
        )
    if allowance != spec.ARM_ALLOWANCE_GRID[0]:
        problems.append(
            f"{variant.key} declares allowance {allowance}; every arm of this round inherits the "
            f"previous round's a_allow000 geometry ({spec.ARM_ALLOWANCE_GRID[0]})"
        )
    if problems:
        fail(problems, f"{variant.key}: the exposure-geometry declaration is invalid")


def arm_payload(variant: spec.Variant) -> dict:
    lever = spec.lever_for(variant.lever)
    return {
        "key": variant.key,
        "lever": variant.lever,
        "lever_label": lever.label,
        "lever_question": lever.question,
        "lever_hypothesis": lever.hypothesis,
        "leg": variant.leg,
        "dataset": variant.dataset.key,
        "synthetic": variant.synthetic,
        "description": variant.description,
        "starting_balance": variant.starting_balance,
        "total_wallet_exposure_limit": variant.declared_twe,
        "we_excess_allowance_pct": variant.declared_allowance_pct,
        "per_slot_cap": variant.per_slot_cap,
        "liquidation_floor_usd": spec.liquidation_floor_usd(variant),
        "guard": variant.guard_params,
        "guard_label": variant.guard_label,
        "stage": spec.stage_of(variant),
        "declared_n_positions": variant.declared_n_positions,
        "declared_allowance_pct": variant.declared_allowance_pct,
        "declared_allowance_mode": spec.load_json(variant.config_path)
        .get("bot", {})
        .get("long", {})
        .get("risk", {})
        .get("we_excess_allowance_mode"),
        "deltas": [
            {"path": dotted, "from": _from, "to": to} for dotted, _from, to in variant.deltas
        ],
        "runtime_flags": list(spec.runtime_flags(variant)),
        "declared_plot_groups": sorted(spec.declared_plot_groups(variant)),
        "config_path": spec.relative(variant.config_path),
        "config_sha256": spec.sha256_file(variant.config_path),
        "bundle_dir": spec.relative(variant.bundle_dir),
        "base_dir": spec.relative(variant.base_dir),
        "execution_audit_path": spec.relative(variant.execution_audit_path),
        "reused": variant.key in spec.REUSED_ARM_KEYS,
    }


def dataset_payload(dataset: spec.DatasetSpec) -> dict:
    payload = {
        "key": dataset.key,
        "leg": dataset.leg,
        "path": spec.relative(dataset.path),
        "window": list(dataset.window),
        "override_mode": dataset.override_mode,
        "synthetic": dataset.synthetic,
        "note": dataset.note,
        "manifest_sha256": dataset.manifest_sha256,
        "manifest_config_hash": dataset.manifest_config_hash,
        "present": dataset.path.is_dir(),
    }
    if dataset.path.is_dir() and (dataset.path / "manifest.json").exists():
        manifest = spec.load_json(dataset.path / "manifest.json")
        payload["manifest_config_hash"] = manifest.get("config_hash")
        payload["manifest_sha256"] = spec.sha256_file(dataset.path / "manifest.json")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the frozen configs and artifacts/variant_input.json",
    )
    args = parser.parse_args(argv)

    require_inputs()
    validate_pins()
    validate_dataset_manifests()
    parent, parent_derivation = derive_parent_config(spec.load_json(spec.SOURCE_CONFIG))
    validate_parent_config(parent)

    for path in (*(v.config_path for v in spec.VARIANTS), spec.VARIANT_INPUT_PATH):
        if path.exists() and not args.force:
            fail(
                [f"{spec.relative(path)} already exists (use --force to overwrite)"],
                "refusing to overwrite frozen study inputs",
            )

    arms_payload: list[dict] = []
    for variant in spec.VARIANTS:
        frozen = build_arm(parent, variant)
        gate_declared_from(parent, variant)
        gate_arm_identity(parent, frozen, variant)
        spec.write_json(variant.config_path, frozen)
        gate_arm_geometry(variant)
        arms_payload.append(arm_payload(variant))
        print(
            f"wrote {spec.relative(variant.config_path)} "
            f"(leg={variant.leg} deltas={len(variant.deltas)} "
            f"sha256={arms_payload[-1]['config_sha256'][:12]}…)"
        )

    backtest = parent["backtest"]
    spec.write_json(
        spec.VARIANT_INPUT_PATH,
        {
            "study": spec.relative(spec.STUDY),
            "parent_study": spec.relative(spec.PARENT_STUDY),
            "parent_config": spec.relative(spec.SOURCE_CONFIG),
            "parent_config_sha256": spec.SOURCE_CONFIG_SHA256,
            "parent_profile_input": spec.relative(spec.SOURCE_PROFILE_INPUT),
            "parent_profile_input_sha256": spec.SOURCE_PROFILE_INPUT_SHA256,
            "prior_study": spec.relative(spec.PRIOR_STUDY),
            "reference_arms": [
                {
                    "key": key,
                    "label": item["label"],
                    "role": item["role"],
                    "leg": item["leg"],
                    "run_dir": spec.relative(Path(item["run_dir"])),
                    "analysis_sha256": item["analysis_sha256"],
                }
                for key, item in spec.REFERENCE_RUNS.items()
            ],
            "legs": [
                {
                    "key": key,
                    "title": spec.LEG_TITLES[key],
                    "window": list(spec.DATASETS[key].window),
                    "role": (
                        "上一轮的搜索窗（本轮样本内；本轮不搜索）"
                        if key == spec.PRIOR_SEARCH_LEG
                        else ("样本外验收窗" if key == "pre" else "全历史")
                    ),
                }
                for key in spec.LEG_ORDER
            ],
            "prior_search_leg": spec.PRIOR_SEARCH_LEG,
            "searched_leg_note": (
                "3y 是**上一轮**搜索所用的窗口；本轮不搜索，该腿只作为“早前被调过的窗口”标注"
            ),
            "reference_control_by_leg": dict(spec.REFERENCE_CONTROL_BY_LEG),
            "reference_kinds": {
                kind: sorted(
                    key
                    for key, item in spec.REFERENCE_RUNS.items()
                    if item.get("kind") == kind
                )
                for kind in sorted(
                    {str(item.get("kind")) for item in spec.REFERENCE_RUNS.values()}
                )
            },
            "geometry_contract": {
                "mapping_table": [dict(row) for row in spec.GEOMETRY_MAPPING_TABLE],
                "paths": [
                    spec.TOTAL_WALLET_EXPOSURE_LIMIT_PATH,
                    spec.N_POSITIONS_PATH,
                    spec.ALLOWANCE_PATH,
                    spec.ALLOWANCE_MODE_PATH,
                ],
                "allowance_grid": list(spec.ARM_ALLOWANCE_GRID),
                "parent_allowance_pct": spec.PARENT_ALLOWANCE_PCT,
                "parent_n_positions": spec.PARENT_N_POSITIONS,
                "slot_share": "total_wallet_exposure_limit / n_positions",
                "artifact": spec.GEOMETRY_ARTIFACT,
                "artifact_fields": list(spec.GEOMETRY_FIELDS),
                "note": (
                    "单币上限 = TWE / n_positions × (1 + effective allowance)；"
                    "bounded 模式把 effective allowance 夹到 min(raw, n_positions − 1)。"
                    "本轮所有臂都继承上一轮 a_allow000 的 allowance = 0"
                ),
            },
            "no_search": dict(spec.NO_SEARCH),
            "parent_derivation": parent_derivation,
            "ladder_contract": {
                "paths": [spec.HALT_LADDER_PATH, spec.REALIZED_LOSS_BUDGET_PATH],
                "rungs_path": spec.HALT_LADDER_PATH,
                "budget_path": spec.REALIZED_LOSS_BUDGET_PATH,
                "no_restart_path": spec.NO_RESTART_DRAWDOWN_THRESHOLD_PATH,
                "defaults": {
                    path: value for path, value in spec.PARENT_ENGINE_DEFAULTS
                },
                "rungs": list(spec.LADDER_RUNGS),
                "fixed_rung": list(spec.FIXED_LONG_HALT_MINUTES),
                "budget": spec.REALIZED_LOSS_BUDGET_PCT,
                "no_restart_off": spec.NO_RESTART_OFF,
                "rung_rule": (
                    "strike_index = min(strikes_this_cycle, len(ladder))（1-based）；"
                    "第 N 次触发取 ladder[N-1]，第 len+1 次及以后夹在最后一档"
                ),
                "cycle_rule": (
                    "周期 = 作用域权益未回到本周期峰值的那段时间；权益重新达到周期峰值时 "
                    "strikes_this_cycle 与周期峰值一起清零（不复用 drawdown_raw == 0）"
                ),
                "realized_basis": (
                    "realized_loss_pct = max(0, cycle_realized_pnl_peak - realized_pnl_now) / "
                    "max(ladder_cycle_peak_equity, EPS)，只在 episode 终结（平仓确认）时判定"
                ),
                "latch_rule": (
                    "policy == 'never' 恒锁存；policy == 'threshold' 时瞬时回撤判据与累计已实现"
                    "判据是 OR；no_restart_drawdown_threshold = 1 即关掉瞬时口径"
                ),
                "telemetry": [
                    "hard_stop_ladder_strikes_max",
                    "hard_stop_realized_loss_halt_pct_max",
                    "hard_stop_duration_minutes_max",
                ],
                "note": (
                    "两个键都默认关闭；默认值下引擎行为与改动前逐位相同，`l0_off` 就是这条回归证据"
                ),
            },


            "guard_contract": {
                "tier_semantics": dict(spec.GUARD_TIER_SEMANTICS),
                "mapping_table": [dict(row) for row in spec.GUARD_MAPPING_TABLE],
                "paths": list(spec.HSL_PATHS),
                "lookback_path": spec.PNLS_LOOKBACK_PATH,
                "lookback_days": spec.GUARD_LOOKBACK_DAYS,
                "note": (
                    "本轮沿用上一轮验证过的守护形状（unified / RED 0.20 / EMA 60 / 停 12H）"
                    "与它的 a_allow000 几何（allowance=0、TWE 3.0），把改动严格限制在两个新键上："
                    "冷却阶梯与累计已实现亏损口径（设计契约见 halt_ladder_design.md）"
                ),
            },
            "control_arms": [
                {
                    "key": key,
                    "label": item["label"],
                    "leg": item["leg"],
                    "run_dir": spec.relative(Path(item["run_dir"])),
                    "analysis_sha256": item["analysis_sha256"],
                }
                for key, item in spec.REFERENCE_RUNS.items()
            ],
            "capital_contract": {
                "parent_starting_balance": spec.PARENT_STARTING_BALANCE,
                "arm_starting_balance": spec.ARM_STARTING_BALANCE,
                "starting_balance_path": spec.STARTING_BALANCE_PATH,
                "note": (
                    "backtest.starting_balance 初始化模拟账户，并驱动所有按余额缩放的仓位大小"
                    "（passivbot-rust/src/backtest.rs: balance.usd_cash_wallet = starting_balance）"
                ),
            },
            "liquidation_contract": {
                "threshold_path": spec.LIQUIDATION_THRESHOLD_PATH,
                "threshold": spec.PARENT_LIQUIDATION_THRESHOLD,
                "floor_fraction_of_start": spec.PARENT_LIQUIDATION_THRESHOLD,
                "note": (
                    "引擎在 权益 ≤ 起始资金 × threshold 时置 liquidated=true、把末值钉在地板并"
                    "跳出回测循环（passivbot-rust/src/backtest.rs: check_and_apply_liquidation）"
                ),
            },
            "coin_count": len(((backtest.get("coins") or {}).get(spec.DATA_EXCHANGE) or [])),
            "coins": sorted((backtest.get("coins") or {}).get(spec.DATA_EXCHANGE) or []),
            "execution": spec.EXECUTION,
            "costs": spec.COSTS,
            "gate": dict(spec.GATE),
            "hsl_block": spec.HSL_BLOCK,
            "live_hsl": dict(spec.LIVE_HSL),
            "modelled_live_hsl_keys": list(spec.MODELLED_LIVE_HSL_KEYS),
            "context_live_hsl_keys": list(spec.CONTEXT_LIVE_HSL_KEYS),
            "parent_risk": dict(spec.PARENT_RISK),
            "parent_max_realized_loss_pct": spec.PARENT_MAX_REALIZED_LOSS_PCT,
            "datasets": [dataset_payload(spec.DATASETS[key]) for key in spec.DATASETS],
            "event_rule": {**spec.EVENT_RULE, "labels": [list(item) for item in spec.EVENT_LABELS]},
            "shock_levels": list(spec.SHOCK_LEVELS),
            "levers": [
                {
                    "key": lever.key,
                    "label": lever.label,
                    "question": lever.question,
                    "hypothesis": lever.hypothesis,
                }
                for lever in spec.LEVERS.values()
            ],
            "arms": arms_payload,
            "run_order": list(spec.RUN_VARIANT_ORDER),
            "reused_arm_keys": list(spec.REUSED_ARM_KEYS),
            "decision_rules": list(DECISION_RULES),
            "honesty_boundaries": list(HONESTY_BOUNDARIES),
            "comparison_metric_groups": {
                group: list(keys) for group, keys in spec.COMPARISON_METRIC_GROUPS.items()
            },
            "hsl_metrics": list(spec.HSL_METRICS),
            "panic_fill_marker": spec.PANIC_FILL_MARKER,
            "reference_report": spec.REFERENCE_REPORT,
            "report_convention": spec.REPORT_CONVENTION,
        },
    )
    print(f"wrote {spec.relative(spec.VARIANT_INPUT_PATH)}")
    print(
        f"arms={len(arms_payload)} to_run={len(spec.RUN_VARIANT_ORDER)} "
        f"reused={list(spec.REUSED_ARM_KEYS)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
