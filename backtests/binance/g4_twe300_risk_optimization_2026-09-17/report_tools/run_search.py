#!/usr/bin/env python3
"""Drive the risk-geometry parameter search with the repository's own optimizer.

The search is the study's only in-sample step, so it is fenced on both sides:

* **before** — the frozen `optimize` contract in `variant_input.json` fixes the backend, seed,
  budget, bounds, objectives, limits and the selection rule; this tool rebuilds the search
  config *from* that contract and refuses to run if the two disagree;
* **after** — every candidate is read back out of `optimize_results/<run>/pareto/`, filtered by
  the declared rule, and frozen into `artifacts/search_selection.json`, which is what the study
  then replays as arms on all three legs. The out-of-sample leg (`pre`) is never given to the
  optimizer.

Modes:

* `--smoke`      tiny budget (`SEARCH_SMOKE_ITERS`, one worker) to measure wall clock and peak
                 RSS before committing to the full run; writes `search_smoke.json` only, so no
                 search-derived arms can be frozen from a smoke run.
* `--select-only` reuse the newest (or `--results-dir`) `optimize_results` run and only redo the
  selection;
* `--fallback-grid` write the declared fallback cells instead of running the optimizer;
* default       full declared search, then selection.

Offline only: no network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_spec as spec  # noqa: E402

SEARCH_DIR = spec.ARTIFACTS / "search"
CONFIG_PATH = SEARCH_DIR / "config.json"
LOG_PATH = SEARCH_DIR / "optimize.log"
RESULTS_ROOT = spec.SEARCH_RESULTS_ROOT


def sanitized_command(command: list[str]) -> list[str]:
    """The recorded command, with host paths replaced by repository-relative labels.

    Tracked evidence must not carry the generating host's absolute paths, so the interpreter and
    every argument that resolves inside the repository is recorded relative to the repository
    root (the raw command stays in the local-only run log).
    """
    out: list[str] = []
    for item in command:
        path = Path(item)
        if path.is_absolute():
            try:
                out.append(str(path.resolve().relative_to(spec.REPO)))
                continue
            except ValueError:
                out.append(path.name)
                continue
        out.append(item)
    return out


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def search_contract() -> dict[str, Any]:
    """The frozen search contract the study input declares."""
    if not spec.VARIANT_INPUT_PATH.exists():
        fail(
            [f"missing {spec.relative(spec.VARIANT_INPUT_PATH)}; freeze the study input first"],
            "the search needs the frozen study input",
        )
    payload = spec.load_json(spec.VARIANT_INPUT_PATH)
    contract = payload.get("search_contract") or {}
    if not contract:
        fail(["variant_input.json carries no search_contract"], "the search contract is missing")
    return contract


def base_config() -> dict[str, Any]:
    """The search baseline: the previous round's validated guard on the search leg.

    The search starts from the arm the repository already trusts (`unified` / RED 0.20 / EMA 60 /
    12h, TWE 3.0, allowance 0.37) served by the search leg's frozen bundle, with every searched
    knob overwritten by the declared bounds.
    """
    source = spec.GUARD_STUDY / "artifacts/g4_g_user12h__3y.config.json"
    if not source.exists():
        fail([f"missing {spec.relative(source)}"], "the search baseline config is missing")
    config = spec.load_json(source)
    backtest = config.setdefault("backtest", {})
    backtest["base_dir"] = spec.relative(SEARCH_DIR / "backtest_results" / "binance_search")
    # The optimizer evaluates many candidates in parallel against one output location, and the
    # backtest creates its execution audit exclusively; a single shared path therefore fails from
    # the second candidate on. The optimizer writes its own per-candidate artefacts, so the audit
    # is disabled here (`None` is the schema default).
    backtest["execution_audit_path"] = None
    return config


def build_search_config() -> dict[str, Any]:
    """Baseline config + the declared `optimize` block (nothing else is changed)."""
    contract = search_contract()
    config = base_config()
    bounds = deepcopy(spec.SEARCH_BOUNDS)
    flat_bounds = {
        key: value for group in bounds.values() for fields in group.values() for key, value in fields.items()
    }
    optimize = {
        "backend": "pymoo",
        "seed": contract["seed"],
        "iters": contract["iters"],
        "population_size": contract["population_size"],
        "n_cpus": contract["n_cpus"],
        "pareto_max_size": 200,
        "write_all_results": True,
        "compress_results_file": True,
        "round_to_n_significant_digits": 3,
        "scoring": [dict(item) for item in contract["scoring"]],
        "limits": [dict(item) for item in contract["limits"]],
        "bounds": bounds,
        "fixed_params": list(contract["fixed_params"]),
    }
    config["optimize"] = optimize
    config["_search_contract"] = {
        "declared_in": spec.relative(spec.VARIANT_INPUT_PATH),
        "bounds": {key: list(value) for key, value in sorted(flat_bounds.items())},
        "fine_tune_params": list(contract.get("fine_tune_params") or ()),
        "search_leg": spec.SEARCH_LEG,
        "candidate_legs": list(spec.SEARCH_CANDIDATE_LEGS),
        "note": (
            "搜索域只有这六个风险几何维度：-ft/--fine-tune-params 会把其余全部 bounds 固定到"
            "当前配置值（hydration 会把引擎默认 bounds 展开进来，所以这一点必须靠 -ft 保证）；"
            "run_search.py 会在运行后从日志里核对 tunable/fixed 集合"
        ),
    }
    return config


def write_search_config() -> Path:
    config = build_search_config()
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    spec.write_json(CONFIG_PATH, config)
    print(f"wrote {spec.relative(CONFIG_PATH)}")
    return CONFIG_PATH


def run_optimizer(
    config_path: Path,
    *,
    iters: int | None = None,
    n_cpus: int | None = None,
    resume: str | None = None,
    fine_tune_params: tuple[str, ...] = (),
    log_path: Path = LOG_PATH,
) -> dict[str, Any]:
    """Run `src/optimize.py` offline and return its timing, memory and results directory."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    before = {path.name for path in RESULTS_ROOT.iterdir()} if RESULTS_ROOT.is_dir() else set()
    command = [sys.executable, str(spec.REPO / "src/optimize.py"), str(config_path)]
    if iters is not None:
        command += ["--optimize.iters", str(iters)]
    if n_cpus is not None:
        command += ["--optimize.n_cpus", str(n_cpus)]
    if fine_tune_params:
        command += ["--fine-tune-params", ",".join(fine_tune_params)]
    if resume:
        command += ["--resume", str(resume)]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(spec.REPO / "src")
    env.setdefault("PYTHONUNBUFFERED", "1")
    children_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    started = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        log.write("command: " + " ".join(command) + "\n")
        log.flush()
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=str(spec.REPO),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    elapsed = time.time() - started
    peak_rss_kb = max(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss, children_before)
    after = {path.name for path in RESULTS_ROOT.iterdir()} if RESULTS_ROOT.is_dir() else set()
    created = sorted(after - before)
    results_dir = RESULTS_ROOT / created[-1] if created else None
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    return {
        "command": sanitized_command(command),
        "returncode": completed.returncode,
        "elapsed_s": elapsed,
        "peak_rss_mb": peak_rss_kb / 1024.0,
        "results_dir": results_dir,
        "created_dirs": created,
        "log_path": log_path,
        "log_text": log_text,
    }


def offline_gate(log_text: str) -> list[str]:
    """The log must prove the frozen bundle was used and that nothing was fetched."""
    problems = [f"log contains fetch marker {marker!r}" for marker in spec.NETWORK_FETCH_MARKERS if marker in log_text]
    markers = ("[hlcvs] override", "[hlcvs] verified manifest", "Loaded hlcvs data from cache")
    if not any(marker in log_text for marker in markers):
        problems.append(
            "log never reports a frozen-bundle load (" + ", ".join(markers) + ")"
        )
    return problems


def _bound_block(log_text: str, header: str) -> list[str]:
    """The indented key lines of one `_log_bound_set` block."""
    lines = log_text.splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        if header in line:
            collecting = True
            continue
        if collecting:
            if line.startswith((" ", "\t")):
                out.append(line.strip())
                continue
            break
    return out


def scope_gate(log_text: str, contract: dict[str, Any]) -> list[str]:
    """The run must have tuned exactly the declared six dimensions.

    Hydration expands `optimize.bounds` with the engine's defaults, so the only thing that keeps
    the alpha surface out of the search is `-ft/--fine-tune-params`; the optimizer prints both
    bound sets, and this gate compares them with the frozen contract instead of trusting the
    configuration.
    """
    problems: list[str] = []
    tunable = _bound_block(log_text, "fine-tune tunable bounds")
    fixed = _bound_block(log_text, "fixed optimize bounds")
    selectors = [str(item) for item in (contract.get("fine_tune_params") or [])]
    if not tunable and not fixed:
        return [
            "the optimizer log carries no bound-set report; cannot prove the search stayed "
            "inside the declared risk-geometry dimensions"
        ]
    if len(tunable) != len(selectors):
        problems.append(
            f"the optimizer tuned {len(tunable)} bound(s), the contract declares "
            f"{len(selectors)}: {tunable}"
        )
    for selector in selectors:
        if not any(selector in line for line in tunable):
            problems.append(f"declared search dimension {selector!r} is not in the tunable set")
    if not fixed:
        problems.append("the optimizer fixed no bounds; the alpha surface would have been searched")
    return problems


def _scalar(value: Any, *, reducer: str = "min") -> float | None:
    """A metric may be a number or the optimizer's `{mean, min, max, ...}` aggregate."""
    if isinstance(value, dict):
        for key in (reducer, "mean", "min", "max"):
            item = value.get(key)
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                return float(item)
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def candidate_metrics(entry: dict[str, Any]) -> dict[str, Any]:
    """Pull the selection metrics out of a pareto entry, whatever shape the optimizer wrote."""
    metrics = entry.get("metrics") or {}
    pools = [metrics.get("stats") or {}, metrics.get("objectives") or {}, metrics, entry]
    wanted = ("adg_strategy_eq", "drawdown_worst_strategy_eq", "backtest_completion_ratio", "liquidated")
    out: dict[str, Any] = {}
    for key in wanted:
        for pool in pools:
            if isinstance(pool, dict) and key in pool:
                if key == "liquidated":
                    out[key] = bool(pool[key])
                else:
                    # `min` is the conservative reducer: a candidate is only eligible when its
                    # worst scenario keeps completion high and the drawdown inside the band.
                    out[key] = _scalar(pool[key], reducer="min")
                break
    return out


def candidate_parameters(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The six searched knobs, read back from the candidate config the optimizer persisted."""
    bot = ((entry.get("bot") or {}).get("long") or {})
    risk = bot.get("risk") or {}
    hsl = bot.get("hsl") or {}
    mapping = {
        "total_wallet_exposure_limit": risk.get("total_wallet_exposure_limit"),
        "n_positions": risk.get("n_positions"),
        "we_excess_allowance_pct": risk.get("we_excess_allowance_pct"),
        "hsl_red_threshold": hsl.get("red_threshold"),
        "hsl_ema_span_minutes": hsl.get("ema_span_minutes"),
        "hsl_cooldown_minutes_after_red": hsl.get("cooldown_minutes_after_red"),
    }
    if any(value is None for value in mapping.values()):
        return None
    params: dict[str, Any] = {}
    bounds = spec.search_bound_leaves()
    for key, value in mapping.items():
        low, high, step = bounds.get(key, (None, None, None))
        number = float(value)
        if key == "n_positions":
            params[key] = int(round(number))
            continue
        if step:
            number = round(round(number / step) * step, 6)
        params[key] = number
        if low is not None and not (low - 1e-9 <= number <= high + 1e-9):
            return None
    return params


def parameter_key(params: dict[str, Any]) -> tuple:
    return tuple(sorted((key, round(float(value), 6)) for key, value in params.items()))


def load_candidates(results_dir: Path) -> list[dict[str, Any]]:
    pareto_dir = results_dir / "pareto"
    if not pareto_dir.is_dir():
        fail([f"{spec.relative(pareto_dir)} is missing"], "the optimizer wrote no pareto directory")
    out: list[dict[str, Any]] = []
    for path in sorted(pareto_dir.glob("*.json")):
        entry = spec.load_json(path)
        params = candidate_parameters(entry)
        if params is None:
            continue
        out.append(
            {
                "path": path,
                "parameters": params,
                "metrics": candidate_metrics(entry),
            }
        )
    return out


def select_candidates(candidates: list[dict[str, Any]], contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply the declared filter and the three declared picks, then fill to the cap."""
    pick = contract.get("pick") or {}
    completion_min = float(pick.get("completion_min", 0.0))
    drawdown_max = float(pick.get("drawdown_max", 1.0))
    max_candidates = int(pick.get("max_candidates", 3))

    def adg(entry: dict[str, Any]) -> float:
        value = entry["metrics"].get("adg_strategy_eq")
        return float(value) if value is not None else float("-inf")

    def dd(entry: dict[str, Any]) -> float:
        value = entry["metrics"].get("drawdown_worst_strategy_eq")
        return float(value) if value is not None else float("inf")

    eligible = []
    for entry in candidates:
        metrics = entry["metrics"]
        if metrics.get("liquidated") is True:
            continue
        completion = metrics.get("backtest_completion_ratio")
        if completion is not None and float(completion) < completion_min:
            continue
        if dd(entry) > drawdown_max:
            continue
        eligible.append(entry)
    if not eligible:
        return []

    picks: list[tuple[str, dict[str, Any]]] = []
    if eligible:
        picks.append(("max_adg", max(eligible, key=adg)))
        picks.append(("min_dd", min(eligible, key=dd)))
        knee = max(eligible, key=lambda entry: (adg(entry) * (1.0 - min(1.0, dd(entry)))))
        picks.append(("knee", knee))
    ordered = sorted(
        eligible, key=lambda entry: adg(entry) * (1.0 - min(1.0, dd(entry))), reverse=True
    )
    for entry in ordered:
        if len(picks) >= max_candidates:
            break
        picks.append(("fill", entry))

    selected: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    steps = spec.search_bound_leaves()
    for kind, entry in picks:
        key = parameter_key(entry["parameters"])
        if key in seen:
            continue
        too_close = False
        for other in selected:
            diffs = [
                abs(float(entry["parameters"][name]) - float(other["parameters"][name]))
                for name in entry["parameters"]
            ]
            if all(diff <= 1e-9 for diff in diffs):
                too_close = True
        if too_close:
            continue
        seen.add(key)
        selected.append(
            {
                "lever": f"c{len(selected) + 1}",
                "selection_kind": kind,
                "parameters": entry["parameters"],
                "metrics": entry["metrics"],
                "pareto_path": spec.relative(entry["path"]),
                "pareto_sha256": spec.sha256_file(entry["path"]),
            }
        )
        if len(selected) >= max_candidates:
            break
    return selected


def summary_of(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    def adg(entry: dict[str, Any]) -> float:
        value = entry["metrics"].get("adg_strategy_eq")
        return float(value) if value is not None else float("-inf")

    def dd(entry: dict[str, Any]) -> float:
        value = entry["metrics"].get("drawdown_worst_strategy_eq")
        return float(value) if value is not None else float("inf")

    if not candidates:
        return {"pareto_candidates": 0}
    return {
        "pareto_candidates": len(candidates),
        "best_adg": max(adg(entry) for entry in candidates),
        "best_drawdown": min(dd(entry) for entry in candidates),
        "liquidated": sum(1 for entry in candidates if entry["metrics"].get("liquidated") is True),
    }


def write_selection(
    *,
    selected: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    contract: dict[str, Any],
    run: dict[str, Any] | None,
    mode: str,
) -> None:
    payload = {
        "mode": mode,
        "search": {
            "contract_declared_in": spec.relative(spec.VARIANT_INPUT_PATH),
            "search_leg": spec.SEARCH_LEG,
            "candidate_legs": list(spec.SEARCH_CANDIDATE_LEGS),
            "backend": contract.get("backend"),
            "seed": contract.get("seed"),
            "iters": contract.get("iters"),
            "population_size": contract.get("population_size"),
            "n_cpus": contract.get("n_cpus"),
            "scoring": contract.get("scoring"),
            "limits": contract.get("limits"),
            "bounds": contract.get("bound_leaves"),
            "pick": contract.get("pick"),
            "fixed_params": contract.get("fixed_params"),
            "fine_tune_params": contract.get("fine_tune_params"),
            "results_dir": spec.relative(run["results_dir"]) if run and run.get("results_dir") else None,
            "config_path": spec.relative(CONFIG_PATH) if CONFIG_PATH.exists() else None,
            "config_sha256": spec.sha256_file(CONFIG_PATH) if CONFIG_PATH.exists() else None,
            "log_path": spec.relative(run["log_path"]) if run else None,
            "elapsed_s": None if not run else run["elapsed_s"],
            "peak_rss_mb": None if not run else run["peak_rss_mb"],
            "pareto_summary": summary_of(candidates),
        },
        "selected": selected,
    }
    spec.write_json(spec.SEARCH_SELECTION_PATH, payload)
    print(
        f"wrote {spec.relative(spec.SEARCH_SELECTION_PATH)} "
        f"({len(selected)} candidate(s) from {len(candidates)} pareto entries)"
    )


def write_record(run: dict[str, Any], *, mode: str, extra: dict[str, Any] | None = None) -> None:
    payload = {
        "mode": mode,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "command": run["command"],
        "returncode": run["returncode"],
        "elapsed_s": run["elapsed_s"],
        "peak_rss_mb": run["peak_rss_mb"],
        "results_dir": spec.relative(run["results_dir"]) if run.get("results_dir") else None,
        "created_dirs": run.get("created_dirs"),
        "log_path": spec.relative(run["log_path"]),
        "log_sha256": spec.sha256_file(run["log_path"]) if Path(run["log_path"]).exists() else None,
        "config_path": spec.relative(CONFIG_PATH) if CONFIG_PATH.exists() else None,
        "config_sha256": spec.sha256_file(CONFIG_PATH) if CONFIG_PATH.exists() else None,
        "offline_gate_problems": offline_gate(run["log_text"]),
        "generated_at_unix_s": time.time(),
    }
    if extra:
        payload.update(extra)
    spec.write_json(spec.SEARCH_RECORD_PATH, payload)
    print(f"wrote {spec.relative(spec.SEARCH_RECORD_PATH)}")


def fallback_cells() -> list[dict[str, Any]]:
    """The declared fallback cells, frozen as search candidates without optimizer metrics."""
    selected = []
    for index, cell in enumerate(spec.SEARCH_FALLBACK_CELLS, start=1):
        selected.append(
            {
                "lever": f"c{index}",
                "selection_kind": "fallback_grid_cell",
                "parameters": {key: cell[key] for key in spec.SEARCH_PARAM_PATHS},
                "metrics": {},
                "pareto_path": None,
                "pareto_sha256": None,
            }
        )
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="tiny budget, records RSS/wall clock")
    parser.add_argument("--select-only", action="store_true", help="reuse the newest results dir")
    parser.add_argument("--fallback-grid", action="store_true", help="write the declared fallback cells")
    parser.add_argument("--results-dir", default=None, help="explicit optimize_results run directory")
    parser.add_argument("--resume", default=None, help="resume an interrupted optimizer run")
    args = parser.parse_args(argv)

    contract = search_contract()

    if args.fallback_grid:
        selected = fallback_cells()
        write_selection(selected=selected, candidates=[], contract=contract, run=None, mode="fallback_grid")
        print("fallback: the declared cells are frozen as arms; no optimizer metrics exist for them")
        return 0

    config_path = write_search_config()

    if args.select_only:
        if args.results_dir:
            results_dir = Path(args.results_dir)
        else:
            if not RESULTS_ROOT.is_dir():
                fail([f"{spec.relative(RESULTS_ROOT)} does not exist"], "no optimizer run to reuse")
            runs = sorted(
                (path for path in RESULTS_ROOT.iterdir() if path.is_dir() and (path / "pareto").is_dir()),
                key=lambda path: path.stat().st_mtime,
            )
            if not runs:
                fail(["no optimize_results run with a pareto/ directory"], "nothing to select from")
            results_dir = runs[-1]
        candidates = load_candidates(results_dir)
        selected = select_candidates(candidates, contract)
        write_selection(
            selected=selected,
            candidates=candidates,
            contract=contract,
            run={"results_dir": results_dir, "log_path": LOG_PATH, "elapsed_s": None, "peak_rss_mb": None},
            mode="select_only",
        )
        return 0

    iters = spec.SEARCH_SMOKE_ITERS if args.smoke else contract["iters"]
    n_cpus = spec.SEARCH_SMOKE_N_CPUS if args.smoke else contract["n_cpus"]
    log_path = SEARCH_DIR / ("smoke.log" if args.smoke else "optimize.log")
    print(
        f"running the optimizer: backend={contract['backend']} iters={iters} n_cpus={n_cpus} "
        f"seed={contract['seed']} leg={spec.SEARCH_LEG}"
    )
    run = run_optimizer(
        config_path,
        iters=iters,
        n_cpus=n_cpus,
        resume=args.resume,
        fine_tune_params=tuple(contract.get("fine_tune_params") or ()),
        log_path=log_path,
    )
    problems = offline_gate(run["log_text"])
    if run["returncode"] != 0:
        problems.append(f"src/optimize.py exited with code {run['returncode']}")
    if not args.smoke:
        problems.extend(scope_gate(run["log_text"], contract))
    if problems:
        write_record(run, mode="smoke" if args.smoke else "search")
        fail(problems, "the optimizer run did not stay inside the declared contract")

    if args.smoke:
        payload = {
            "mode": "smoke",
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "command": run["command"],
            "returncode": run["returncode"],
            "elapsed_s": run["elapsed_s"],
            "peak_rss_mb": run["peak_rss_mb"],
            "results_dir": spec.relative(run["results_dir"]) if run.get("results_dir") else None,
            "created_dirs": run.get("created_dirs"),
            "log_path": spec.relative(run["log_path"]),
            "log_sha256": (
                spec.sha256_file(run["log_path"]) if Path(run["log_path"]).exists() else None
            ),
            "config_path": spec.relative(CONFIG_PATH) if CONFIG_PATH.exists() else None,
            "config_sha256": spec.sha256_file(CONFIG_PATH) if CONFIG_PATH.exists() else None,
            "offline_gate_problems": [],
            "iters": iters,
            "n_cpus": n_cpus,
            "population_size": spec.SEARCH_SMOKE_POPULATION_SIZE,
            "note": (
                "冒烟测：只用于测量墙钟时间与峰值 RSS、并验证离线与搜索域门；按声明它不写成 "
                "search_selection.json（因此不会自动冻结 C 臂），但它的 pareto 目录可以用 "
                "--select-only 显式采用并在报告里如实标注预算"
            ),
        }
        spec.write_json(spec.SEARCH_SMOKE_PATH, payload)
        print(
            f"smoke ok: elapsed={run['elapsed_s']:.1f}s peak_rss={run['peak_rss_mb']:.0f}MB "
            f"results={payload['results_dir']}"
        )
        return 0

    if not run.get("results_dir"):
        fail(["the optimizer created no results directory"], "cannot find the search results")
    candidates = load_candidates(run["results_dir"])
    selected = select_candidates(candidates, contract)
    write_record(run, mode="search", extra={"pareto_summary": summary_of(candidates)})
    write_selection(
        selected=selected, candidates=candidates, contract=contract, run=run, mode="search"
    )
    if not selected:
        print(
            "WARNING: no candidate passed the declared filter; the study will run without C arms",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
