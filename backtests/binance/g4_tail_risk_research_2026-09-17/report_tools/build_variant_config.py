#!/usr/bin/env python3
"""Freeze every arm of the g4 tail-risk study and gate the claims it makes.

Writes, next to each other:

* `artifacts/g4_<arm>.config.json` — the frozen parent g4 config plus that arm's declared
  changes (and, for the long-history leg, the frozen dataset override),
* `artifacts/variant_input.json` — the pre-registered arm table: declared deltas, pinned
  parent/anchor hashes, dataset identities, the event rule, the wipe-out levels and the
  decision rules the report must apply.

The gates here are the study's scientific claim, so they are checked rather than assumed:
the parent config must hash to the pinned value, the two reference analyses must hash to
their pinned values, and every arm must differ from the parent by its declared paths alone
— with the HSL block, the modelled `live.hsl_signal_mode`, the risk block and the
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
        "rule": "某账户级 arm 在两腿 ΔCAGR ≥ −0.5pp/年 且事件内最差回撤下降 ≥30% ⇒ 推荐启用，"
        "取满足条件的最大 red_threshold。",
    },
    {
        "id": "J2",
        "rule": "扩展腿（5.4 年）最差 DD < 20% 且合成单币归零损失 ≤ 20% ⇒ 判定单币归零不构成"
        "一波带走；给出同时归零的临界币数 k* = 判据阈值 / 单币 WE 上界。",
    },
    {
        "id": "J3",
        "rule": "任一 arm 出现 liquidated=true 或最差 DD > 50% ⇒ 上实盘前必须结构性降杠杆"
        "（TWE ≤ 0.8 或 n_positions ≥ 10）。",
    },
    {
        "id": "J4",
        "rule": "合成注入显示引擎在崩塌中仍持续加仓、且损失超过 WE 上限 ⇒ 升级为结构性修正工单"
        "（本 PR 不改引擎）。",
    },
)
#: The boundaries the report must state alongside its numbers.
HONESTY_BOUNDARIES = (
    "历史未崩不等于未来安全；结论只在两条冻结腿与合成情景的范围内成立。",
    "扩展腿 2021-04-20 起交易、2022 年之前只有 22 个币有数据，且币池是活到 2026 年的当前"
    "top40，存在幸存者偏差；早期月份受闸门预热影响，2021-05 事件只能作旁证。",
    "HSL 触发指标为 min(raw, EMA)，对一天内完成并反弹的闪崩天然迟钝，不能当作插针保险。",
    "合成情景是价格路径注入，不是历史事实，也不等价于真实退市流程（无撮合/无交易所公告）。",
    "回测不含交易所/稳定币对手方风险、真实期权与资金费率成本、真实流动性枯竭下的滑点与"
    "API 断连；这些必须在报告里作为未建模风险列出。",
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
    if problems:
        fail(problems, "the parent config does not carry the declared contract")
    print("gate: parent config keeps the execution/cost contract, the 20/50 gate and the risk block")
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
    allowed_backtest = (
        spec.ALLOWED_DATASET_DIFF_PATHS
        if variant.dataset.override_mode
        else spec.ALLOWED_CONFIG_DIFF_PATHS
    )
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
        if backtest.get("disable_plotting") not in (None, False, "", [], ()):
            problems.append(
                "backtest.disable_plotting must stay at the parent's value: this engine "
                "revision only honours the runtime flag, so a frozen config value would be "
                f"silently ignored (got {backtest.get('disable_plotting')!r})"
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


def synthetic_payload() -> dict:
    if not spec.SYNTHETIC_BUNDLES_PATH.exists():
        return {"built": False, "bundles": {}, "note": "run make_synthetic_collapse_bundle.py"}
    payload = spec.load_json(spec.SYNTHETIC_BUNDLES_PATH)
    payload["built"] = True
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
            "prior_study": spec.relative(spec.PRIOR_STUDY),
            "reference_arms": [
                {
                    "key": key,
                    "label": item["label"],
                    "role": item["role"],
                    "run_dir": spec.relative(Path(item["run_dir"])),
                    "analysis_sha256": item["analysis_sha256"],
                }
                for key, item in spec.REFERENCE_RUNS.items()
            ],
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
            "synthetic": synthetic_payload(),
            "synthetic_injections": {
                key: {k: v for k, v in value.items() if k != "target_metrics_run"}
                | {"target_metrics_run": spec.relative(value["target_metrics_run"])}
                for key, value in spec.SYNTHETIC_INJECTIONS.items()
            },
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
