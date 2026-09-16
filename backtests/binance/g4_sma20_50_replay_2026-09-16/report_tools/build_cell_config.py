#!/usr/bin/env python3
"""Freeze the published gated profile as this study's run config, and gate it.

Writes `artifacts/g4_sma20_50.config.json` (the config the replay runs, equal to the
tracked published profile apart from the run-directory location) and
`artifacts/profile_input.json` (the study-cell metrics the replay must reproduce).

The gate here is the study's scientific claim, so it is checked rather than assumed:
the frozen config is the published profile, and the published profile really does carry
the declared 20/50 gate.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cell_spec as spec  # noqa: E402


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def require_inputs() -> None:
    problems = []
    for path in (
        spec.PUBLISHED_PROFILE,
        spec.BASE_PROFILE,
        spec.CELL_RESULT_PATH,
        spec.CONTRACT_PATH,
        spec.BASELINE_ANALYSIS,
        spec.BASELINE_CONFIG,
    ):
        if not path.exists():
            problems.append(f"missing input {spec.relative(path)}")
    for name in spec.FROZEN_CACHE_FILES:
        if not (spec.FROZEN_CACHE / name).exists():
            problems.append(f"missing frozen dataset file {spec.FROZEN_CACHE_REL / name}")
    if problems:
        fail(problems, "required inputs are not present")


def validate_cell_record(record: dict) -> None:
    problems = []
    for key, expected in (
        ("cell_id", spec.CELL_ID),
        ("group", spec.CELL_GROUP),
        ("window", spec.CELL_WINDOW),
        ("scenario", spec.SCENARIO),
    ):
        if record.get(key) != expected:
            problems.append(f"result.json {key!r}: expected {expected!r}, got {record.get(key)!r}")
    metrics = record.get("metrics") or {}
    for key in (spec.CELL_FILLS_KEY, *spec.CELL_METRIC_KEYS):
        if key not in metrics:
            problems.append(f"result.json metrics missing {key!r}")
    unplaced = set(metrics) - set(spec.CELL_METRIC_KEYS) - {spec.CELL_FILLS_KEY}
    if unplaced:
        # Not a failure: the record carries many metrics. But one that the tooling neither
        # checks nor quotes is easy to lose silently, so the leftovers are named.
        print(f"note: {len(unplaced)} cell metric(s) unused by this study (e.g. {sorted(unplaced)[:4]})")
    if not isinstance(record.get("coins"), list) or not record["coins"]:
        problems.append("result.json coins is not a non-empty list")
    if problems:
        fail(problems, "study cell record does not match the declared cell")


def validate_contract(contract: dict) -> str:
    problems = []
    cell = next(
        (entry for entry in contract.get("cells", []) if entry.get("cell_id") == spec.CELL_ID),
        None,
    )
    if cell is None:
        problems.append(f"research contract has no cell {spec.CELL_ID!r}")
    else:
        if cell.get("group") != spec.CELL_GROUP:
            problems.append(f"contract group: expected {spec.CELL_GROUP!r}, got {cell.get('group')!r}")
    if problems:
        fail(problems, "research contract disagrees with the declared cell")
    return str(contract.get("cell_matrix_sha256", ""))


def validate_gate(config: dict) -> None:
    """The published profile must carry exactly the gate this study reports on."""
    gate = (config.get("backtest") or {}).get("entry_regime_gate")
    if not isinstance(gate, dict):
        fail(
            [f"backtest.entry_regime_gate is {gate!r}, expected a dict"],
            "the published profile does not declare an entry-regime gate",
        )
    problems = spec.compare_subtrees(spec.GATE, spec.gate_semantics(gate), "backtest.entry_regime_gate")
    if problems:
        fail(problems, "published profile gate differs from the declared gate")
    if not gate.get("enabled"):
        fail(["backtest.entry_regime_gate.enabled is false"], "the gate is not enabled")
    print(f"gate: backtest.entry_regime_gate (declaration) == {spec.GATE}")


def freeze_config(profile: dict, coins: list[str]) -> dict:
    """Copy the published profile and retarget only the declared data-identity keys.

    The profile is an operational config; the evidence is a fixed dataset. This function makes
    the run reproduce the evidence and nothing else: no strategy, risk, exit or gate parameter is
    touched.
    """
    frozen = deepcopy(profile)
    backtest = frozen["backtest"]
    backtest["base_dir"] = spec.relative(spec.BACKTEST_BASE_DIR)
    backtest["exchanges"] = [spec.DATA_EXCHANGE]
    backtest["start_date"], backtest["end_date"] = spec.EVIDENCE_WINDOW
    backtest["coins"] = {spec.DATA_EXCHANGE: list(coins)}
    backtest["cache_dir"] = {spec.DATA_EXCHANGE: spec.relative(spec.FROZEN_CACHE)}
    # The traded universe, matched to the evidence: the frozen basket on the long side, nothing
    # on the short side. Short trading is inert either way (`bot.short.risk.
    # total_wallet_exposure_limit = 0.0`), so this changes no behaviour; it removes a difference
    # the report would otherwise have to explain.
    frozen["live"] = deepcopy(frozen["live"])
    frozen["live"]["approved_coins"] = {"long": list(coins), "short": []}
    return frozen


def gate_profile_identity(profile: dict, frozen: dict, coins: list[str]) -> None:
    """Prove the frozen config is the published profile, retargeting aside."""
    problems = spec.equal_subtree(profile["backtest"], frozen["backtest"], "backtest")
    for root in spec.IDENTITY_ROOTS:
        if root not in profile and root not in frozen:
            continue
        problems.extend(spec.equal_subtree(profile.get(root), frozen.get(root), root))
    live_approved = frozen.get("live", {}).get("approved_coins") or {}
    if sorted(live_approved.get("long") or []) != sorted(coins):
        problems.append(
            f"live.approved_coins.long: expected the frozen {len(coins)}-coin basket, got "
            f"{len(live_approved.get('long') or [])} entries"
        )
    if list(live_approved.get("short") or []):
        problems.append("live.approved_coins.short must be empty for this long-only evidence")

    expected_retargets = {
        "base_dir": spec.relative(spec.BACKTEST_BASE_DIR),
        "exchanges": [spec.DATA_EXCHANGE],
        "start_date": spec.EVIDENCE_WINDOW[0],
        "end_date": spec.EVIDENCE_WINDOW[1],
        "coins": {spec.DATA_EXCHANGE: list(coins)},
        "cache_dir": {spec.DATA_EXCHANGE: spec.relative(spec.FROZEN_CACHE)},
    }
    for key in spec.RETARGETED_BACKTEST_KEYS:
        expected = expected_retargets[key]
        if frozen["backtest"].get(key) != expected:
            problems.append(
                f"backtest.{key}: expected {expected!r}, got {frozen['backtest'].get(key)!r}"
            )
    if problems:
        fail(problems, "the frozen config is not the published profile")

    print("gate: frozen config == published profile except for these declared retargets:")
    for key in spec.RETARGETED_BACKTEST_KEYS:
        value = frozen["backtest"][key]
        if isinstance(value, dict) and len(str(value)) > 90:
            value = {k: (f"…{str(v)[-42:]}" if isinstance(v, str) and len(str(v)) > 42 else v)
                     for k, v in value.items()}
        print(f"  backtest.{key} = {value!r}")
    for root in spec.IDENTITY_ROOTS:
        print(f"  identical root: {root}")
    for path in spec.RETARGETED_LEAF_PATHS:
        node = frozen
        for part in path.split("."):
            node = node[part]
        print(
            f"  retargeted leaf {path} = long:{len(node.get('long') or [])} coins, "
            f"short:{len(node.get('short') or [])} coins"
        )


def gate_execution_contract(config: dict) -> None:
    problems = []
    backtest = config["backtest"]
    for key, expected in (*spec.EXECUTION.items(), *spec.COSTS.items()):
        if not spec.numeric_equal(backtest.get(key), expected):
            problems.append(f"backtest.{key}: expected {expected!r}, got {backtest.get(key)!r}")
    if backtest.get("cache_dir"):
        problems.append(
            "backtest.cache_dir is set in the published profile; the study pins it itself so the "
            "run cannot resolve a different bundle"
        )
    exchanges = backtest.get("exchanges")
    if not isinstance(exchanges, list) or spec.DATA_EXCHANGE not in exchanges:
        problems.append(
            f"backtest.exchanges = {exchanges!r} does not include the contract's "
            f"{spec.DATA_EXCHANGE!r}; the frozen bundle could not have been produced from it"
        )
    elif len(exchanges) > 1:
        print(
            f"note: the published profile lists {exchanges!r}; the study narrows this to "
            f"[{spec.DATA_EXCHANGE!r}] because the recorded evidence is single-exchange"
        )
    if problems:
        fail(problems, "the published profile does not carry the reported execution/cost contract")
    print("gate: execution/cost regime matches the research contract C1 + binance_actual")
    print(f"gate: frozen dataset {spec.FROZEN_CACHE_REL}")


def build_profile_input(record: dict, contract: dict, cell_matrix_sha256: str, profile: dict) -> dict:
    metrics = record["metrics"]
    keep = {key: metrics[key] for key in (spec.CELL_FILLS_KEY, *spec.CELL_METRIC_KEYS) if key in metrics}
    backtest = profile["backtest"]
    return {
        "cell_id": spec.CELL_ID,
        "group": spec.CELL_GROUP,
        "window": spec.CELL_WINDOW,
        "scenario": spec.SCENARIO,
        "published_profile": spec.relative(spec.PUBLISHED_PROFILE),
        "published_profile_sha256": spec.sha256_file(spec.PUBLISHED_PROFILE),
        "base_profile": spec.relative(spec.BASE_PROFILE),
        "base_profile_sha256": spec.sha256_file(spec.BASE_PROFILE),
        "gate": dict(spec.GATE),
        "coin_count": int(record.get("coin_count") or len(record["coins"])),
        "coins": sorted(record["coins"]),
        "execution": spec.EXECUTION,
        "costs": spec.COSTS,
        "window": {
            "start_date": backtest.get("start_date"),
            "end_date": backtest.get("end_date"),
            "candle_interval_minutes": backtest.get("candle_interval_minutes"),
            "balance_sample_divider": backtest.get("balance_sample_divider"),
        },
        "contract": {
            "path": spec.relative(spec.CONTRACT_PATH),
            "version": contract.get("version"),
            "cell_matrix_sha256": cell_matrix_sha256,
            "primary_scenario": contract.get("primary_scenario"),
            "window_full": (contract.get("windows") or {}).get("full"),
            "seed_config": contract.get("seed", {}).get("config_path"),
            "seed_config_sha256": contract.get("seed", {}).get("config_sha256"),
        },
        "metrics": keep,
        "source_hashes": {
            "cell_result_json": spec.sha256_file(spec.CELL_RESULT_PATH),
            "research_contract_json": spec.sha256_file(spec.CONTRACT_PATH),
            "published_profile_json": spec.sha256_file(spec.PUBLISHED_PROFILE),
            "run_config_json": spec.sha256_file(spec.CONFIG_PATH),
        },
        "elapsed_s": record.get("elapsed_s"),
        "peak_rss_gb": record.get("peak_rss_gb"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite artifacts/g4_sma20_50.config.json and artifacts/profile_input.json",
    )
    args = parser.parse_args(argv)

    require_inputs()
    record = spec.load_json(spec.CELL_RESULT_PATH)
    validate_cell_record(record)
    contract = spec.load_json(spec.CONTRACT_PATH)
    cell_matrix_sha256 = validate_contract(contract)

    profile = spec.load_json(spec.PUBLISHED_PROFILE)
    validate_gate(profile)
    gate_execution_contract(profile)
    coins = sorted(record["coins"])
    if len(coins) != spec.EVIDENCE_COIN_COUNT:
        fail(
            [f"cell record lists {len(coins)} coins, expected {spec.EVIDENCE_COIN_COUNT}"],
            "the cell record's universe is not the frozen 40-coin basket",
        )
    frozen = freeze_config(profile, coins)
    gate_profile_identity(profile, frozen, coins)

    for path in (spec.CONFIG_PATH, spec.PROFILE_INPUT_PATH):
        if path.exists() and not args.force:
            fail(
                [f"{spec.relative(path)} already exists (use --force to overwrite)"],
                "refusing to overwrite frozen study inputs",
            )

    spec.write_json(spec.CONFIG_PATH, frozen)
    spec.write_json(
        spec.PROFILE_INPUT_PATH,
        build_profile_input(record, contract, cell_matrix_sha256, profile),
    )

    print(f"wrote {spec.relative(spec.CONFIG_PATH)}")
    print(f"wrote {spec.relative(spec.PROFILE_INPUT_PATH)}")
    print(
        "cell cell_matrix_sha256="
        f"{cell_matrix_sha256} fills={record['metrics'][spec.CELL_FILLS_KEY]} "
        f"coins={record.get('coin_count')} window={spec.CELL_WINDOW}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
