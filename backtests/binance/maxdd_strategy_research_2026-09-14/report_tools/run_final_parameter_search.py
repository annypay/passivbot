#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


STUDY_DIR = Path(__file__).resolve().parents[1]
PARAMETER_TOOL = Path(__file__).with_name("run_maxdd_parameter_study.py")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_parameter_tool():
    spec = importlib.util.spec_from_file_location("maxdd_parameter_study", PARAMETER_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load parameter study tool: {PARAMETER_TOOL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def async_main() -> None:
    decision_path = STUDY_DIR / "strategy_path_decision_lock.json"
    with decision_path.open() as file:
        decision = json.load(file)
    strategy = decision["selected_strategy"]
    if strategy not in {"ema_anchor", "trailing_martingale"}:
        raise ValueError(f"unsupported locked strategy: {strategy!r}")
    if decision.get("parameter_only_status") not in {
        "passed",
        "failed_research_path_only",
    }:
        raise RuntimeError("final parameter search has an invalid path-decision status")
    holdout_paths = (
        STUDY_DIR / "final_holdout_metrics.json",
        STUDY_DIR / "mfe_research" / "final_holdout_metrics.json",
        STUDY_DIR / "final_replays" / "holdout",
    )
    opened_holdout = [path for path in holdout_paths if path.exists()]
    if opened_holdout:
        raise RuntimeError(
            f"final parameter search cannot run after holdout artifacts exist: {opened_holdout}"
        )

    tool = load_parameter_tool()
    tool.disable_network()
    final_lock_path = (
        STUDY_DIR / "locks" / "FINAL" / f"{strategy}_candidate_lock.json"
    )
    context_path = STUDY_DIR / "locks" / "FINAL" / "parameter_selection_context.json"
    expected_context = {
        "strategy_path_decision_lock_sha256": sha256(decision_path),
        "selected_strategy": strategy,
        "parameter_only_status": decision["parameter_only_status"],
        "parameter_tool_sha256": sha256(PARAMETER_TOOL),
        "final_holdout_unopened": True,
    }
    if final_lock_path.exists():
        if not context_path.exists():
            raise RuntimeError(
                f"existing final lock lacks selection context: {final_lock_path}"
            )
        if json.loads(context_path.read_text()) != expected_context:
            raise RuntimeError("existing final parameter lock has a different context")
        print(final_lock_path.read_text())
        return
    tool.FOLDS["FINAL"] = {
        "train": tool.FINAL_TRAIN,
        "validation": tool.FINAL_HOLDOUT,
    }
    result = await tool.search_one(strategy, "FINAL")
    tool.write_lock(context_path, expected_context)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(async_main())
