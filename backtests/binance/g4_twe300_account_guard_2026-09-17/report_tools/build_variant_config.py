#!/usr/bin/env python3
"""Freeze every arm of the g4 @ TWE 3.0 account-guard study and gate the claims it makes.

Writes, next to each other:

* `artifacts/g4_<arm>.config.json` — the frozen parent g4 config plus that arm's declared
  changes (starting capital, total wallet exposure limit, and the account-guard block:
  scope, thresholds, EMA speed, halt length, terminal floor, peak window),
* `artifacts/variant_input.json` — the pre-registered arm table: declared deltas, pinned
  parent/anchor hashes, dataset identities, the capital and liquidation contract, the event
  rule, the wipe-out levels and the decision rules the report must apply.

The gates here are the study's scientific claim, so they are checked rather than assumed:
the parent config must hash to the pinned value, both reference analyses must hash to their
pinned values, and every arm must differ from the parent by its declared paths alone — with
the capital, the risk block, the HSL block, the modelled `live.hsl_signal_mode` and the
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
        "rule": "任一守护 arm 在 5.4 年腿 liquidated=false 且活到窗口末 ⇒ 判定“账户级守护在本地"
        "历史上确实能挡住同一次崩盘”，并记录是哪个配置与代价；若全部强平 ⇒ 判定在 TWE 3.0 这一档"
        "账户级止损来不及。",
    },
    {
        "id": "J2",
        "rule": "3 年腿上任一守护 arm 同时满足“最差回撤 ≤ 无守护基线 × 0.8”与“CAGR ≥ 无守护基线"
        " − 30pp” ⇒ 通过（收益代价可接受）。",
    },
    {
        "id": "J3",
        "rule": "同一阈值下对比停机时长（12H vs 24H）：更长的停机在强平/最差回撤/复牌后表现上不更差"
        " ⇒ 判定阶梯升级值得做；否则不值得。",
    },
    {
        "id": "J4",
        "rule": "g_soft_orange 或 g_orange_only 在回撤与终值上同时优于直译版 g_user12h ⇒ 推荐"
        "“先停加仓、后清仓”的分级方案；否则直译版已足够。",
    },
    {
        "id": "J5",
        "rule": "g_slow_ema 的 hard_stop_triggers ≈ 0 且结果与无守护基线一致 ⇒ 判定“触发速度”"
        "是必要条件，并把它写进结论第一条。",
    },
)
#: The boundaries the report must state alongside its numbers.
HONESTY_BOUNDARIES = (
    "任何账户级止损都挡不住“超过到强平距离的跳空”：TWE 3.0 满仓时到地板只有约 −31.8%，"
    "所以真正的黑天鹅防线仍是结构（TWE/we_excess_allowance_pct/n_positions）+ 快触发 + 分级反应。",
    "引擎的冷却时长是单一常量，无法表达“第二次升级为更长时间”的阶梯；本轮用 "
    "restart_after_red_policy=threshold + no_restart_drawdown_threshold 做终极端近似，"
    "并在 account_guard_design.md 给出引擎级设计方案。",
    "RED 的语义是“先 panic 平掉整个作用域、再停机”，因此“熔断”= 实现亏损 + 离场；"
    "“只停机不清仓”只能通过 ORANGE/TpOnly 表达（停加仓、保仓位、只止盈）。",
    "“浮亏 20%”在引擎口径下是“策略权益相对 7 天滚动峰值回撤 20%”，包含已实现亏损，"
    "比纯未实现亏损更保守；报告显式声明该解释。",
    "历史范围有限：币池是活到 2026 年的当前 top40（幸存者偏差），5.4 年腿 2021-04-20 起交易、"
    "当时仅 22 个币有数据；2021-05 事件落在闸门首次风险开启后 3 个交易日。",
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
    for key in ("3y", "ext"):
        dataset = spec.DATASETS[key]
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
        (spec.DATASETS[key].path / "manifest.json", spec.DATASETS[key].manifest_sha256, f"{key} manifest")
        for key in ("3y", "ext")
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
    problems = []
    for key in ("3y", "ext"):
        dataset = spec.DATASETS[key]
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
        # The bundle's last bar must be the declared end day.
        window_end = spec.day_of(_iso(effective.get("end_ts")))
        if window_end != dataset.window[1]:
            problems.append(
                f"{key} effective end {window_end!r} != declared window end {dataset.window[1]!r}"
            )
    if problems:
        fail(problems, "dataset manifests do not match the declared legs")
    print("gate: both frozen bundles carry the declared window and config hash")


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


def arm_payload(variant: spec.Variant) -> dict:
    lever = spec.LEVERS[variant.lever]
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
            "guard_contract": {
                "tier_semantics": dict(spec.GUARD_TIER_SEMANTICS),
                "mapping_table": [dict(row) for row in spec.GUARD_MAPPING_TABLE],
                "paths": list(spec.HSL_PATHS),
                "lookback_path": spec.PNLS_LOOKBACK_PATH,
                "lookback_days": spec.GUARD_LOOKBACK_DAYS,
                "note": (
                    "本轮把用户条件最大化还原为引擎可表达的配置；冷却阶梯本身需要引擎改动，"
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
