#!/usr/bin/env python3
"""Build the frozen run config for the `hsl_npos1` deep analysis.

The published profile is the subject of the report, so this tool only **retargets** it:

* `bot` and `coin_overrides` must stay equal to `configs/examples/hsl_npos1.json`, and
* only the `backtest` keys in `hsl_npos1_spec.ALLOWED_BACKTEST_KEYS` may differ.

The frozen local HLCV catalog is wired in as a dataset override, which makes the run fully
offline (`src/backtest.py` returns the override before any exchange manager is built) and makes
the effective window and coin basket a recorded property of the dataset instead of a guess.

Offline only: no network access, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hsl_npos1_spec as study  # noqa: E402


def _under_allowed_key(flat_key: str) -> bool:
    """True when a flattened `backtest.<...>` path sits under an allow-listed retarget key."""
    tail = flat_key.split(".")[1:]
    return any(
        tail[: len(part.split("."))] == part.split(".") for part in study.ALLOWED_BACKTEST_KEYS
    )


def build_config() -> tuple[dict, list[dict], dict]:
    """Return (run config, applied retarget ops, build notes)."""
    actual_sha = study.sha256_file(study.SOURCE_CONFIG)
    if actual_sha != study.SOURCE_CONFIG_SHA256:
        raise SystemExit(
            f"source profile drift: {study.relative(study.SOURCE_CONFIG)} has sha256 "
            f"{actual_sha}, expected {study.SOURCE_CONFIG_SHA256}. The published profile changed; "
            "re-review it before rebuilding this study rather than silently re-running."
        )

    source = study.load_json(study.SOURCE_CONFIG)
    config = study.load_json(study.SOURCE_CONFIG)
    source_flat = study.flatten(source)

    dataset_coins = set(study.frozen_dataset_coins())
    requested = [
        str(coin) for coin in source.get("live", {}).get("approved_coins", {}).get("long") or []
    ]
    if not requested:
        raise SystemExit("source profile declares no long approved coins")
    # A coin is eligible only when the frozen catalog can serve it *and* it is not excluded by
    # policy. Exclusion is decided before the run, so the basket never depends on results.
    effective = [
        coin
        for coin in requested
        if coin in dataset_coins and coin not in study.EXCLUDED_COINS
    ]
    dropped = [coin for coin in requested if coin not in effective]
    if not effective:
        raise SystemExit("frozen dataset has no overlap with the requested coins")

    ops: list[dict] = []

    def retarget(dotted: str, value: Any) -> None:
        previous = source_flat.get(dotted, "<absent>")
        if study.numeric_equal(previous, value):
            return
        study.set_path(config, dotted, value)
        ops.append({"path": dotted, "from": previous, "to": value})

    # `live.approved_coins` drives data materialization, so a coin the frozen catalog cannot
    # serve has to leave this list too: otherwise the run resolves its market and fetches it.
    retarget("live.approved_coins", {"long": effective, "short": []})

    retarget("backtest.start_date", study.REQUESTED_START)
    retarget("backtest.end_date", study.REQUESTED_END)
    retarget("backtest.exchanges", ["binance"])
    retarget("backtest.coins", {"binance": effective})
    retarget("backtest.candle_interval_minutes", study.CANDLE_INTERVAL_MINUTES)
    retarget("backtest.balance_sample_divider", study.BALANCE_SAMPLE_DIVIDER)
    retarget("backtest.maker_fee_override", study.COSTS["maker_fee_override"])
    retarget("backtest.taker_fee_override", study.COSTS["taker_fee_override"])
    retarget("backtest.execution_delay_bars", study.EXECUTION["execution_delay_bars"])
    retarget("backtest.intrabar_fill_order", study.EXECUTION["intrabar_fill_order"])
    retarget("backtest.execution_audit_path", study.relative(study.EXECUTION_AUDIT_PATH))
    retarget("backtest.disable_plotting", "coin_fills")
    retarget("backtest.suite_enabled", False)
    retarget("backtest.scenarios", [{"label": "base"}])
    # The run must land where the study looks for it. `base_dir` keeps the profile's own value
    # ("backtests") so the completed run directory is discovered and then moved by the runner.
    retarget("backtest.base_dir", source.get("backtest", {}).get("base_dir", "backtests"))

    # Guard the retarget itself: nothing outside the allow-list may differ from the profile.
    problems = study.compare_subtrees(source.get("bot"), config.get("bot"), "bot")
    problems += study.compare_subtrees(
        source.get("coin_overrides"), config.get("coin_overrides"), "coin_overrides"
    )
    unexpected = sorted(
        key
        for key in study.differing_paths(
            source.get("backtest"), config.get("backtest"), "backtest"
        )
        if not _under_allowed_key(key)
    )
    if unexpected:
        problems += [f"unexpected backtest change: {key}" for key in unexpected]
    if problems:
        raise SystemExit(
            "run config diverges from the published profile:\n  " + "\n  ".join(problems)
        )

    notes = {
        "requested_coins": requested,
        "requested_coin_count": len(requested),
        "effective_coins": effective,
        "effective_coin_count": len(effective),
        "dropped_coins": dropped,
        "dataset_coin_count": len(dataset_coins),
        "dataset_override": False,
        "source_config": study.relative(study.SOURCE_CONFIG),
        "source_config_sha256": actual_sha,
        "requested_window": [study.REQUESTED_START, study.REQUESTED_END],
        "execution": dict(study.EXECUTION),
        "costs": dict(study.COSTS),
        "ops": ops,
    }
    return config, ops, notes


def write_config(config: dict) -> None:
    study.write_json(study.CONFIG_PATH, config)
    print(f"wrote {study.relative(study.CONFIG_PATH)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the source profile and print the plan without writing the config",
    )
    args = parser.parse_args(argv)

    config, ops, notes = build_config()
    print(
        f"source profile : {notes['source_config']} "
        f"(sha256 {notes['source_config_sha256'][:16]}...)"
    )
    print(
        f"local catalog  : {study.FROZEN_DATASET_REL.as_posix()} "
        f"({notes['dataset_coin_count']} coins; materialized by the backtest itself)"
    )
    print(
        f"coins          : {notes['effective_coin_count']} of {notes['requested_coin_count']} "
        f"requested; dropped {notes['dropped_coins'] or 'none'}"
    )
    print(f"window request : {notes['requested_window'][0]} .. {notes['requested_window'][1]}")
    print(f"execution      : {notes['execution']}")
    print(f"costs          : {notes['costs']}")
    print(f"retarget ops   : {len(ops)}")
    for op in ops:
        print(f"  - {op['path']}: {op['from']!r} -> {op['to']!r}")
    if args.check_only:
        print("check-only: nothing written")
        return 0
    write_config(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
