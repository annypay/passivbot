#!/usr/bin/env python3
"""Freeze the g3_cth0000 run configuration and gate it against its declared cell.

Writes `artifacts/g3_cth0000.config.json` (the byte-exact config the study cell ran) and
`artifacts/cell_input.json` (the study-cell metrics the replay must reproduce), after
proving that config really is the published seed profile plus the one declared op.

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
        spec.CELL_RESULT_PATH,
        spec.CONTRACT_PATH,
        spec.SEED_CONFIG_PATH,
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
    ops = record.get("ops")
    expected_ops = [dict(op) for op in spec.EXPECTED_OPS]
    if ops != expected_ops:
        problems.append(f"result.json ops: expected {expected_ops!r}, got {ops!r}")
    config = record.get("config")
    if not isinstance(config, dict):
        problems.append("result.json config is not an object")
    elif spec.get_path(config, spec.PATCH_PATH) != 0.0:
        problems.append(
            f"result.json config {spec.PATCH_PATH}: expected 0.0, "
            f"got {spec.get_path(config, spec.PATCH_PATH)!r}"
        )
    metrics = record.get("metrics") or {}
    for key in (spec.CELL_FILLS_KEY, *spec.CELL_METRIC_KEYS):
        if key not in metrics:
            problems.append(f"result.json metrics missing {key!r}")
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
        expected_ops = [dict(op) for op in spec.EXPECTED_OPS if op["op"] == "set"]
        if cell.get("ops") != expected_ops:
            problems.append(f"contract ops: expected {expected_ops!r}, got {cell.get('ops')!r}")
        if cell.get("group") != spec.CELL_GROUP:
            problems.append(f"contract group: expected {spec.CELL_GROUP!r}, got {cell.get('group')!r}")
    if problems:
        fail(problems, "research contract disagrees with the declared cell")
    return str(contract.get("cell_matrix_sha256", ""))


def gate_seed_equivalence(cell_config: dict) -> None:
    """Prove the cell config is the published profile plus exactly the declared ops."""
    seed = spec.load_json(spec.SEED_CONFIG_PATH)
    derived = deepcopy(seed)
    for op in spec.EXPECTED_OPS:
        if op["op"] != "set":
            fail([f"unsupported op {op['op']!r}"], "gate only supports set ops")
        node = derived
        parts = op["path"].split(".")
        for part in parts[:-1]:
            node = node[part]
        if node.get(parts[-1]) != op["seed"]:
            fail(
                [
                    f"{op['path']}: seed profile value {node.get(parts[-1])!r} "
                    f"differs from the contract seed {op['seed']!r}"
                ],
                "seed profile drifted from the research contract",
            )
        node[parts[-1]] = op["value"]

    problems = spec.compare_subtrees(derived[spec.BOT_ROOT], cell_config[spec.BOT_ROOT], spec.BOT_ROOT)
    if problems:
        fail(problems, "cell config is not the seed profile plus the declared ops")

    seed_backtest = seed["backtest"]
    cell_backtest = cell_config["backtest"]
    missing_added = [key for key in spec.STUDY_ADDED_BACKTEST_KEYS if key not in cell_backtest]
    if missing_added:
        fail(
            [f"cell config backtest is missing study-added key {key!r}" for key in missing_added],
            "cell config is missing study-added backtest keys",
        )
    allowed_prefixes = tuple(
        f"backtest.{key}"
        for key in (*spec.STUDY_ADDED_BACKTEST_KEYS, *spec.STUDY_RETARGETED_BACKTEST)
    )
    backtest_problems = [
        problem
        for problem in spec.compare_subtrees(seed_backtest, cell_backtest, "backtest")
        if not problem.startswith(allowed_prefixes)
    ]
    if backtest_problems:
        fail(backtest_problems, "cell config backtest differs from the seed profile")

    retarget_problems = []
    for key, expected in spec.STUDY_RETARGETED_BACKTEST.items():
        if not spec.numeric_equal(cell_backtest.get(key), expected):
            retarget_problems.append(
                f"backtest.{key}: expected {expected!r}, got {cell_backtest.get(key)!r}"
            )
    cache_dir = (cell_backtest.get("cache_dir") or {}).get("binance")
    if not isinstance(cache_dir, str) or Path(cache_dir).name != spec.FROZEN_CACHE.name:
        retarget_problems.append(
            f"backtest.cache_dir.binance: expected the frozen bundle {spec.FROZEN_CACHE_REL}, "
            f"got {cache_dir!r}"
        )
    execution_problems = []
    for key, expected in spec.EXECUTION.items():
        if not spec.numeric_equal(cell_backtest.get(key), expected):
            execution_problems.append(f"backtest.{key}: expected {expected!r}, got {cell_backtest.get(key)!r}")
    for key, expected in spec.COSTS.items():
        if not spec.numeric_equal(cell_backtest.get(key), expected):
            execution_problems.append(f"backtest.{key}: expected {expected!r}, got {cell_backtest.get(key)!r}")
    if retarget_problems or execution_problems:
        fail(
            retarget_problems + execution_problems,
            "cell config does not carry the reported window, data source and cost regime",
        )

    print("gate: cell config == seed profile + declared ops (bot subtree exact)")
    print("gate: execution/cost regime matches the research contract C1 + binance_actual")
    print(f"gate: frozen dataset {spec.FROZEN_CACHE_REL}")
    for key in spec.STUDY_ADDED_BACKTEST_KEYS:
        print(f"  study-added backtest.{key} = {cell_backtest[key]!r}")


def build_cell_input(record: dict, contract: dict, cell_matrix_sha256: str) -> dict:
    metrics = record["metrics"]
    keep = {}
    for key in (spec.CELL_FILLS_KEY, *spec.CELL_METRIC_KEYS):
        if key in metrics:
            keep[key] = metrics[key]
    return {
        "cell_id": spec.CELL_ID,
        "group": spec.CELL_GROUP,
        "window": spec.CELL_WINDOW,
        "scenario": spec.SCENARIO,
        "ops": record["ops"],
        "coin_count": int(record.get("coin_count") or len(record["coins"])),
        "coins": sorted(record["coins"]),
        "execution": spec.EXECUTION,
        "costs": spec.COSTS,
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
            "seed_config_json": spec.sha256_file(spec.SEED_CONFIG_PATH),
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
        help="overwrite artifacts/g3_cth0000.config.json and artifacts/cell_input.json",
    )
    args = parser.parse_args(argv)

    require_inputs()
    record = spec.load_json(spec.CELL_RESULT_PATH)
    validate_cell_record(record)
    contract = spec.load_json(spec.CONTRACT_PATH)
    cell_matrix_sha256 = validate_contract(contract)

    cell_config = record["config"]
    gate_seed_equivalence(cell_config)

    for path in (spec.CONFIG_PATH, spec.CELL_INPUT_PATH):
        if path.exists() and not args.force:
            fail(
                [f"{spec.relative(path)} already exists (use --force to overwrite)"],
                "refusing to overwrite frozen study inputs",
            )

    spec.write_json(spec.CONFIG_PATH, cell_config)
    spec.write_json(spec.CELL_INPUT_PATH, build_cell_input(record, contract, cell_matrix_sha256))

    print(f"wrote {spec.relative(spec.CONFIG_PATH)}")
    print(f"wrote {spec.relative(spec.CELL_INPUT_PATH)}")
    print(
        "cell cell_matrix_sha256="
        f"{cell_matrix_sha256} fills={record['metrics'][spec.CELL_FILLS_KEY]} "
        f"coins={record.get('coin_count')} window={spec.CELL_WINDOW}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
