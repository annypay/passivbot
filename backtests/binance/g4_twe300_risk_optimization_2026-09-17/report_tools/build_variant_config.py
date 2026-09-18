#!/usr/bin/env python3
"""Freeze every arm of the g4 @ TWE 3.0 risk-geometry study and gate the claims it makes.

Writes, next to each other:

* `artifacts/g4_<arm>.config.json` — the frozen parent g4 config plus that arm's declared
  changes (starting capital, exposure geometry: total exposure limit, `n_positions`,
  `we_excess_allowance_pct`; guard geometry: scope, thresholds, EMA speed, halt length,
  terminal floor, peak window; and for the realized-loss arm `live.max_realized_loss_pct`),
* `artifacts/variant_input.json` — the pre-registered arm table: declared deltas, pinned
  parent/anchor hashes, dataset identities for all three legs, the capital/liquidation
  contract, the geometry and search contracts, the event rule, the wipe-out levels and the
  decision rules the report must apply.

The gates here are the study's scientific claim, so they are checked rather than assumed:
the parent config must hash to the pinned value, every reference analysis must hash to its
pinned value, the frozen bundles must carry the declared windows (the out-of-sample leg is
clipped by the `intersection` override, so its bundle must *cover* the window), and every arm
must differ from the parent by its declared paths alone — with the capital, the risk block,
the HSL block, the modelled `live.hsl_signal_mode`, the exposure geometry and the
execution/cost contract asserted field by field.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_spec as spec  # noqa: E402

#: Decision rules, pre-registered before any arm is run. The report must apply these
#: literally and state the verdict for each one.
DECISION_RULES = (
    {
        "id": "J1",
        "rule": "占用纪律有效性：`a_allow000__ext` 与守护基准 `g_user12h__ext` 相比，终值不低于其 "
        "80%、最差回撤不更高、单币归零上界与峰值单币敞口都更低 ⇒ 判定“占用纪律有效”；"
        "任一条不满足 ⇒ 判定无效并写明是哪一条。",
    },
    {
        "id": "J2",
        "rule": "占用×杠杆：在 `a_allow000*` 家族中，用**样本外 pre 腿**的“存活 + 终值/回撤”最优点作为"
        "推荐结构设定，并给出“每避免 1pp 回撤放弃多少终值”；若 pre 腿全部强平 ⇒ 判定结构档位需要"
        "继续下调，推荐值取 ext 腿最优点并标注它未经样本外确认。",
    },
    {
        "id": "J3",
        "rule": "冷却档位（为下一轮引擎阶梯定档）：若 24H/48H/72H 中任一档在 ext 腿的终值与最差回撤"
        "都不差于 12H、且 pre 腿不更差 ⇒ 判定“加长冷却值得做”并给出最优档；否则判定“冷却时长不是"
        "收益杠杆，阶梯只剩风险塑形理由”，下一轮阶梯的最高档仍取证据支持的上限。",
    },
    {
        "id": "J4",
        "rule": "累计二档：`b_term055` / `b_term070` 中任一在 ≥1 条腿上 `terminal_halt_count ≥ 1` 且"
        "终局窗口之前 `halt_count ≥ 2`（即没有首击锁存）、存活腿终值不低于 12H ⇒ 判定二档有效；"
        "若再次首击锁存 ⇒ 判定证伪，永久停机应改用已实现亏损口径。",
    },
    {
        "id": "J5",
        "rule": "搜索：至少一个冻结候选在 ext 腿终值 ≥ 7.8552×（守护基准）且 pre 腿未强平、最差回撤"
        " ≤ 50% ⇒ 判定“搜索找到优于手写守护的点”；否则判定“搜索未找到更优点”，并说明是搜索预算、"
        "参数域还是目标函数的问题。",
    },
    {
        "id": "J6",
        "rule": "结构 vs 熔断：用 2×2（无守护/守护 × allowance 0.37/0）分解“每放弃 1 单位终值买到多少"
        "回撤与生存改善”，给出首选杠杆，并判定两者是叠加还是冗余（冗余 = 同时使用不优于单独使用）。",
    },
)
#: The boundaries the report must state alongside its numbers.
HONESTY_BOUNDARIES = (
    "搜索是**样本内**的：参数只在原生 3 年窗（2023-09-12 → 2026-09-12）上搜索，"
    "任何“更优”结论都必须由样本外 pre 腿（2021-04-20 → 2023-09-11）与 ext 腿确认；"
    "看到样本外结果后不得再搜索（预注册）。",
    "任何账户级止损都挡不住“超过到强平距离”的跳空：满仓 TWE 3.0 到地板只有约 −31.8%、"
    "TWE 2.5 约 −40%、TWE 2.0 约 −50%（随暴露峰值变化），所以结构性上限仍是唯一确定性的尾部防线。",
    "引擎的冷却时长是单一常量，无法表达“第 N 次触发停更久”的阶梯；本轮用固定档位（12/24/48/72H）"
    "为下一轮引擎级阶梯定档，阶梯本身需要独立 PR（Rust + 实盘重建契约 + 测试）。",
    "`no_restart_drawdown_threshold` 的锁存判定发生在**平仓确认那一刻**、用 `max(raw, EMA)`；"
    "上一轮实测一次急跌的确认回撤约 0.47，因此 0.40 会在首击锁死。本轮只测 0.55/0.70，"
    "并在报告里核对是否出现首击锁存。",
    "`live.max_realized_loss_pct < 1` 会拦截**非 panic** 的亏损平仓（panic 豁免，"
    "`orchestrator.rs::close_passes_realized_loss_gate`），但 backtest 不导出拦截计数，"
    "因此该臂只能报终值/回撤/成交差异，机制归因属于未测量部分。",
    "占用几何（在场的币数、空槽时间占比）由成交账本重建，时间权重基于成交时间戳的分段积分，"
    "采样分辨率是两笔成交之间的间隔，不是逐分钟；报告给出推导口径。",
    "历史范围有限：币池是活到 2026 年的当前 top40（幸存者偏差），长腿 2021-04-20 起交易、"
    "当时仅 22 个币有数据；样本外腿只有 2.4 年、5.4 年腿一共只有 5 次守护触发，"
    "涉及触发次数的结论必须标注样本量。",
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
    if variant.lever.startswith(("b_", )) and allowance != 0.0:
        problems.append(f"{variant.key} is a stage-B arm and must keep allowance = 0")
    if variant.lever.startswith("a_") and variant.lever not in (
        "a_allow000",
        "a_allow000_twe250",
        "a_allow000_twe200",
    ):
        expected = {"a_allow010": 0.10, "a_allow020": 0.20, "a_allow037": 0.37}[variant.lever]
        if abs(allowance - expected) > 1e-9:
            problems.append(f"{variant.key} declares allowance {allowance}, expected {expected}")
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
    parent = spec.load_json(spec.SOURCE_CONFIG)
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
            "prior_study": spec.relative(spec.TAIL_RISK_STUDY),
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
                        "搜索窗（样本内）"
                        if key == spec.SEARCH_LEG
                        else ("样本外" if key == "pre" else "全历史")
                    ),
                }
                for key in spec.LEG_ORDER
            ],
            "search_leg": spec.SEARCH_LEG,
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
                    spec.MAX_REALIZED_LOSS_PATH,
                ],
                "allowance_grid": list(spec.ARM_ALLOWANCE_GRID),
                "parent_allowance_pct": spec.PARENT_ALLOWANCE_PCT,
                "parent_n_positions": spec.PARENT_N_POSITIONS,
                "slot_share": "total_wallet_exposure_limit / n_positions",
                "artifact": spec.GEOMETRY_ARTIFACT,
                "artifact_fields": list(spec.GEOMETRY_FIELDS),
                "note": (
                    "单币上限 = TWE / n_positions × (1 + effective allowance)；"
                    "bounded 模式把 effective allowance 夹到 min(raw, n_positions − 1)，"
                    "本研究的取值域内 raw 就是生效值"
                ),
            },
            "search_contract": {
                "selection_path": spec.relative(spec.SEARCH_SELECTION_PATH),
                "record_path": spec.relative(spec.SEARCH_RECORD_PATH),
                "smoke_path": spec.relative(spec.SEARCH_SMOKE_PATH),
                "config_path": spec.relative(spec.SEARCH_CONFIG_PATH),
                "results_root": spec.relative(spec.SEARCH_RESULTS_ROOT),
                "backend": "pymoo",
                "seed": spec.SEARCH_SEED,
                "iters": spec.SEARCH_ITERS,
                "population_size": spec.SEARCH_POPULATION_SIZE,
                "n_cpus": spec.SEARCH_N_CPUS,
                "smoke_iters": spec.SEARCH_SMOKE_ITERS,
                "smoke_n_cpus": spec.SEARCH_SMOKE_N_CPUS,
                "bounds": spec.SEARCH_BOUNDS,
                "bound_leaves": {
                    key: list(value) for key, value in sorted(spec.search_bound_leaves().items())
                },
                "param_paths": dict(spec.SEARCH_PARAM_PATHS),
                "fixed_params": list(spec.SEARCH_FIXED_PARAMS),
                "fine_tune_params": list(spec.SEARCH_FINE_TUNE_PARAMS),
                "scoring": [dict(item) for item in spec.SEARCH_SCORING],
                "limits": [dict(item) for item in spec.SEARCH_LIMITS],
                "pick": dict(spec.SEARCH_PICK),
                "candidate_legs": list(spec.SEARCH_CANDIDATE_LEGS),
                "fallback_cells": [dict(cell) for cell in spec.SEARCH_FALLBACK_CELLS],
                "wider_runtime_overrides": {
                    "note": (
                        "优化器默认把两侧 restart_after_red_policy 固定为 always"
                        "（optimize.fixed_runtime_overrides），本轮保留该默认并在记录里声明"
                    )
                },
            },
            "search_selection": (
                {
                    "path": spec.relative(spec.SEARCH_SELECTION_PATH),
                    "sha256": spec.sha256_file(spec.SEARCH_SELECTION_PATH),
                    "search": spec.load_search_selection().get("search"),
                    "selected": spec.load_search_selection().get("selected"),
                }
                if spec.SEARCH_SELECTION_PATH.exists()
                else None
            ),
            "guard_contract": {
                "tier_semantics": dict(spec.GUARD_TIER_SEMANTICS),
                "mapping_table": [dict(row) for row in spec.GUARD_MAPPING_TABLE],
                "paths": list(spec.HSL_PATHS),
                "lookback_path": spec.PNLS_LOOKBACK_PATH,
                "lookback_days": spec.GUARD_LOOKBACK_DAYS,
                "note": (
                    "本轮沿用上一轮验证过的守护形状（unified / 20% / EMA60 / 停 12H），"
                    "把改动集中在暴露几何与冷却档位；冷却阶梯本身需要引擎改动，"
                    "设计见 account_guard_design.md"
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
