#!/usr/bin/env python3
"""Freeze both replay variants of this study and gate them.

Writes, next to each other:

* `artifacts/g4_sma20_50_hsl_off_control.config.json` — the parent config with only the
  output location retargeted (the paired control on the current engine),
* `artifacts/g4_sma20_50_hsl_on.config.json` — the same config with the study's single
  declared change, `bot.long.hsl.enabled: false -> true`,
* `artifacts/variant_input.json` — the declared delta, the pinned parent hashes, the
  comparison columns and the metric groups the report must fill.

The gates here are the study's scientific claim, so they are checked rather than assumed:
the parent config must hash to the pinned value, the control must differ from it by the
output location alone, the variant must differ by that plus the declared delta and nothing
else, and the HSL block must be the parent profile's own parameters with `enabled` flipped.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_spec as spec  # noqa: E402


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def require_inputs() -> None:
    problems = []
    for path in (spec.SOURCE_CONFIG, spec.SOURCE_PROFILE_INPUT, spec.TRACKED_BASELINE_RUN):
        if not path.exists():
            problems.append(f"missing input {spec.relative(path)}")
    for name in spec.FROZEN_CACHE_FILES:
        if not (spec.FROZEN_CACHE / name).exists():
            problems.append(f"missing frozen dataset file {spec.FROZEN_CACHE_REL / name}")
    if problems:
        fail(problems, "required inputs are not present")


def validate_parent_hashes() -> None:
    problems = []
    for path, expected, label in (
        (spec.SOURCE_CONFIG, spec.SOURCE_CONFIG_SHA256, "frozen parent config"),
        (spec.SOURCE_PROFILE_INPUT, spec.SOURCE_PROFILE_INPUT_SHA256, "parent profile input"),
        (
            spec.TRACKED_BASELINE_RUN / "analysis.json",
            spec.TRACKED_BASELINE_ANALYSIS_SHA256,
            "tracked baseline analysis",
        ),
    ):
        actual = spec.sha256_file(path)
        if actual != expected:
            problems.append(
                f"{label} drift: {spec.relative(path)} has sha256 {actual}, expected {expected}"
            )
    if problems:
        fail(
            problems,
            "a pinned input changed; this study compares against a frozen artifact",
        )
    print("gate: pinned parent config, profile input and baseline analysis hashes match")


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
    if window != spec.EVIDENCE_WINDOW:
        problems.append(f"backtest window {window!r} != {spec.EVIDENCE_WINDOW!r}")
    cache_dir = (backtest.get("cache_dir") or {}).get(spec.DATA_EXCHANGE)
    if cache_dir != spec.relative(spec.FROZEN_CACHE):
        problems.append(
            f"backtest.cache_dir[{spec.DATA_EXCHANGE!r}] = {cache_dir!r}, expected the frozen "
            f"bundle {spec.relative(spec.FROZEN_CACHE)!r}"
        )
    gate = backtest.get("entry_regime_gate")
    if not isinstance(gate, dict) or not gate.get("enabled"):
        problems.append(f"backtest.entry_regime_gate = {gate!r}; the gate must stay enabled")
    else:
        problems.extend(
            spec.compare_subtrees(spec.GATE, spec.gate_semantics(gate), "backtest.entry_regime_gate")
        )
    hsl = ((parent.get("bot") or {}).get("long") or {}).get("hsl") or {}
    if hsl.get("enabled") is not False:
        problems.append(
            f"bot.long.hsl.enabled = {hsl.get('enabled')!r}; the parent artifact is the HSL-OFF "
            "evidence this study departs from"
        )
    for key, expected in spec.LIVE_ONLY_HSL.items():
        actual = (parent.get("live") or {}).get(key)
        if actual != expected:
            problems.append(f"live.{key}: expected {expected!r}, got {actual!r}")
    if problems:
        fail(problems, "the parent config does not carry the declared contract")
    print("gate: parent config keeps the reported execution/cost contract and the 20/50 gate")
    return problems


def frozen_coins(parent: dict) -> list[str]:
    backtest_coins = ((parent.get("backtest") or {}).get("coins") or {}).get(spec.DATA_EXCHANGE) or []
    live_long = (((parent.get("live") or {}).get("approved_coins") or {}).get("long")) or []
    if len(backtest_coins) != spec.EVIDENCE_COIN_COUNT or sorted(backtest_coins) != sorted(live_long):
        fail(
            [
                f"backtest.coins[{spec.DATA_EXCHANGE!r}] has {len(backtest_coins)} entries and "
                f"live.approved_coins.long has {len(live_long)}; expected the same frozen "
                f"{spec.EVIDENCE_COIN_COUNT}-coin basket"
            ],
            "the parent config's universe is not the frozen basket",
        )
    if list(((parent.get("live") or {}).get("approved_coins") or {}).get("short") or []):
        fail(["live.approved_coins.short is not empty"], "the parent config is long-only")
    return sorted(backtest_coins)


def build_control(parent: dict, variant: spec.Variant) -> dict:
    frozen = deepcopy(parent)
    frozen["backtest"]["base_dir"] = spec.relative(variant.base_dir)
    return frozen


def build_hsl_on(parent: dict, variant: spec.Variant) -> dict:
    frozen = build_control(parent, variant)
    for dotted, _from, to in spec.DECLARED_DELTA:
        spec.set_path(frozen, dotted, to)
    return frozen


def gate_variant_identity(
    parent: dict, frozen: dict, variant: spec.Variant, *, expect_delta: bool
) -> None:
    """Prove the frozen config is the parent config plus exactly the declared change."""
    problems = spec.diff_subtrees(
        parent["backtest"], frozen["backtest"], "backtest",
        allowed=spec.ALLOWED_CONFIG_DIFF_PATHS,
    )
    for root in spec.IDENTITY_ROOTS:
        if root not in parent and root not in frozen:
            continue
        allowed = tuple(dotted for dotted, _f, _t in spec.DECLARED_DELTA) if expect_delta else ()
        problems.extend(
            spec.diff_subtrees(parent.get(root), frozen.get(root), root, allowed=allowed)
        )
    expected_base_dir = spec.relative(variant.base_dir)
    if frozen["backtest"].get("base_dir") != expected_base_dir:
        problems.append(
            f"backtest.base_dir: expected {expected_base_dir!r}, "
            f"got {frozen['backtest'].get('base_dir')!r}"
        )
    # The declared delta must be present exactly when this variant declares it, and the
    # resulting HSL block must be the parent profile's parameters with `enabled` flipped.
    for dotted, _from, to in spec.DECLARED_DELTA:
        actual = spec.get_path(frozen, dotted)
        expected = to if expect_delta else _from
        if actual is not expected:
            problems.append(f"{dotted}: expected {expected!r}, got {actual!r}")
    hsl = ((frozen.get("bot") or {}).get("long") or {}).get("hsl") or {}
    expected_hsl = dict(spec.HSL_BLOCK) if expect_delta else dict(spec.HSL_BLOCK, enabled=False)
    problems.extend(spec.compare_subtrees(expected_hsl, hsl, "bot.long.hsl"))
    if problems:
        fail(problems, f"{variant.key}: the frozen config is not the parent config plus its delta")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite the frozen configs and artifacts/variant_input.json",
    )
    args = parser.parse_args(argv)

    require_inputs()
    validate_parent_hashes()
    parent = spec.load_json(spec.SOURCE_CONFIG)
    validate_parent_config(parent)
    coins = frozen_coins(parent)

    for path in (*(v.config_path for v in spec.VARIANTS), spec.VARIANT_INPUT_PATH):
        if path.exists() and not args.force:
            fail(
                [f"{spec.relative(path)} already exists (use --force to overwrite)"],
                "refusing to overwrite frozen study inputs",
            )

    variants_payload: list[dict] = []
    for variant in spec.VARIANTS:
        if variant.hsl_enabled:
            frozen = build_hsl_on(parent, variant)
        else:
            frozen = build_control(parent, variant)
        gate_variant_identity(parent, frozen, variant, expect_delta=variant.hsl_enabled)
        spec.write_json(variant.config_path, frozen)
        variants_payload.append(
            {
                "key": variant.key,
                "label": variant.label,
                "description": variant.description,
                "hsl_enabled": variant.hsl_enabled,
                "config_path": spec.relative(variant.config_path),
                "config_sha256": spec.sha256_file(variant.config_path),
                "bundle_dir": spec.relative(variant.bundle_dir),
                "base_dir": spec.relative(variant.base_dir),
                "execution_audit_path": spec.relative(variant.execution_audit_path),
            }
        )
        print(f"wrote {spec.relative(variant.config_path)}")

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
            "declared_delta": [
                {"path": dotted, "from": _from, "to": to}
                for dotted, _from, to in spec.DECLARED_DELTA
            ],
            "hsl_block": spec.HSL_BLOCK,
            "live_only_hsl": spec.LIVE_ONLY_HSL,
            "coin_count": len(coins),
            "coins": coins,
            "window": {
                "start_date": backtest.get("start_date"),
                "end_date": backtest.get("end_date"),
                "candle_interval_minutes": backtest.get("candle_interval_minutes"),
                "balance_sample_divider": backtest.get("balance_sample_divider"),
                "dynamic_wel_by_tradability": backtest.get("dynamic_wel_by_tradability"),
            },
            "execution": spec.EXECUTION,
            "costs": spec.COSTS,
            "gate": dict(spec.GATE),
            "dataset": {
                "cache_dir": spec.relative(spec.FROZEN_CACHE),
                "bundle": spec.FROZEN_CACHE.name,
            },
            "variants": variants_payload,
            "comparison_columns": [
                {
                    "key": spec.TRACKED_BASELINE_LABEL,
                    "role": "tracked HSL-OFF evidence (older engine revision)",
                    "run_dir": spec.relative(spec.TRACKED_BASELINE_RUN),
                    "analysis_sha256": spec.TRACKED_BASELINE_ANALYSIS_SHA256,
                },
                *[
                    {
                        "key": item["key"],
                        "role": item["description"],
                        "bundle_dir": item["bundle_dir"],
                    }
                    for item in variants_payload
                ],
            ],
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
    for item in variants_payload:
        print(f"  {item['key']}: hsl_enabled={item['hsl_enabled']} sha256={item['config_sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())