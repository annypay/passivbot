#!/usr/bin/env python3
"""Phase C driver for the single-coin stop-loss study: build, run, identity, analyse.

Four subcommands over the 30 pre-registered arms declared in `variant_spec.py`:

* `build`    - derive the parent, gate it, and write the 30 arm configs + `variant_input.json`;
* `run`      - replay one arm (or a whole stage/leg) with a memory guard and no network;
* `identity` - prove `s0_off__<leg>` is bit-identical to the pinned previous-round anchor;
* `analyse`  - read the arm bundles and emit the S0-S7 verdicts (`artifacts/sl_analysis.json`).

Deliberately one tool instead of the previous round's six-file split: this round has a single
mechanism, one author and no search stage, so the gates live next to the thing they gate. The
deviations from the previous round's harness layout are listed in `report_tools/README.md`.

Offline only: no network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_spec as spec  # noqa: E402

PY = spec.REPO / "venv/bin/python"
#: Telemetry keys that postdate the pinned anchors; allowed to be extra but must be zero.
NEW_METRIC_ALLOWLIST = ("hard_stop_ladder_strikes_max", "hard_stop_realized_loss_halt_pct_max")
#: The paths that decide which frozen bundle and window a leg is served by. A leg that does not
#: retarget these silently replays the parent's native window -- every leg then produces the same
#: numbers, which is exactly the failure this study's own run gate must catch.
DATASET_PATHS = (
    "backtest.cache_dir.binance",
    "backtest.hlcvs_data_dir",
    "backtest.hlcvs_data_override_mode",
    "backtest.start_date",
    "backtest.end_date",
)


def apply_dataset(config: dict[str, Any], dataset: spec.DatasetSpec) -> None:
    """Point one arm at its leg's frozen bundle and window."""
    rel = dataset.rel_path.as_posix()
    if dataset.override_mode is None:
        # Native leg: keep the parent's own resolution, but pin it explicitly.
        config["backtest"]["cache_dir"]["binance"] = rel
        config["backtest"]["hlcvs_data_dir"] = None
        config["backtest"]["hlcvs_data_override_mode"] = "intersection"
    else:
        config["backtest"]["cache_dir"]["binance"] = rel
        config["backtest"]["hlcvs_data_dir"] = rel
        config["backtest"]["hlcvs_data_override_mode"] = dataset.override_mode
    config["backtest"]["start_date"], config["backtest"]["end_date"] = dataset.window
#: Verdict thresholds, declared here (not in the spec) so the experiment stays separate from the
#: judgement. Every one of them is fixed before the arms are run.
THRESHOLDS = {
    "S1_fill_vs_level_tolerance": 0.005,
    "S2_tail_improvement_pp": 5.0,
    "S3_terminal_degradation_pct": 25.0,
    "S6_spike_exit_share_pct": 25.0,
}


def available_mb() -> int:
    out = subprocess.run(["free", "-m"], capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        if line.startswith("Mem:"):
            return int(line.split()[6])
    return 0


def newest_run(base: Path, *, required: bool = True) -> Path | None:
    """The backtest writes `<base_dir>/<exchange>/<UTC stamp>/`; `base` is that `<exchange>` level."""
    candidates = sorted(
        path for path in base.glob("*") if (path / "analysis.json").exists()
    )
    if not candidates:
        if required:
            raise SystemExit(f"no completed run under {base}")
        return None
    return candidates[-1]


# --------------------------------------------------------------------------- build


def gate_parent() -> tuple[dict[str, Any], dict[str, Any]]:
    raw, derived = spec.derive_parent_config()
    sha = spec.sha256_file(spec.PARENT_CONFIG)
    if sha != spec.PARENT_CONFIG_SHA256:
        raise SystemExit(f"parent config sha256 {sha} != pinned {spec.PARENT_CONFIG_SHA256}")
    problems = spec.compare_subtrees(raw, derived, "root")
    # Every difference must be one of the eight declared derivations, and nothing else.
    allowlisted = {
        f"root.{path}: unexpected in candidate "
        f"(value {spec.STOP_LOSS_DEFAULTS[path.rsplit('.', 1)[1]]!r})"
        for path in spec.PARENT_DERIVATION_PATHS
    }
    not_allowlisted = [line for line in problems if line not in allowlisted]
    if not_allowlisted:
        raise SystemExit(
            "the derived parent differs from the frozen config by more than the eight stop-loss "
            "paths:\n  " + "\n  ".join(not_allowlisted[:10])
        )
    for path in spec.PARENT_DERIVATION_PATHS:
        if spec.has_path(raw, path):
            raise SystemExit(f"frozen parent already carries {path}; the derivation would be a no-op")
    bounds = json.dumps(spec.load_json(spec.PARENT_CONFIG).get("optimize", {}), ensure_ascii=False)
    if "stop_loss" in bounds:
        raise SystemExit("a stop_loss leaf is present in optimize.bounds; it must not be searchable")
    return raw, derived


def gate_datasets() -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for key, dataset in spec.DATASETS.items():
        path = dataset.path
        if not path.is_dir():
            raise SystemExit(f"dataset {key} is missing: {spec.relative(path)}")
        manifest = spec.load_json(path / "manifest.json")
        config_hash = (
            manifest.get("config_hash")
            or manifest.get("config", {}).get("hash")
            or (manifest.get("synthetic_transform") or {}).get("source_manifest_config_hash")
        )
        entry = {"path": spec.relative(path), "observed_config_hash": config_hash}
        if dataset.synthetic:
            transform = manifest.get("synthetic_transform") or {}
            if transform.get("kind") != "synthetic_price_path_collapse":
                raise SystemExit(f"synthetic bundle {key} has an unexpected transform: {transform}")
            entry["synthetic_transform"] = {
                name: transform.get(name)
                for name in ("kind", "collapse_days", "end_multiple", "hold_days")
            }
            if transform.get("collapse_days") != spec.SYNTHETIC_INJECTIONS[key]["collapse_days"]:
                raise SystemExit(f"{key}: collapse_days disagrees with the declared contract")
            if transform.get("end_multiple") != spec.SYNTHETIC_INJECTIONS[key]["end_multiple"]:
                raise SystemExit(f"{key}: end_multiple disagrees with the declared contract")
        else:
            sha = spec.sha256_file(path / "manifest.json")
            if sha != dataset.manifest_sha256:
                raise SystemExit(f"{key}: manifest sha256 {sha} != pinned {dataset.manifest_sha256}")
            if config_hash != dataset.manifest_config_hash:
                raise SystemExit(f"{key}: config hash {config_hash} != pinned {dataset.manifest_config_hash}")
        observed[key] = entry
    return observed


def gate_reference_runs() -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for key, reference in spec.REFERENCE_RUNS.items():
        if not reference.run_dir.is_dir():
            raise SystemExit(f"reference run {key} is missing: {spec.relative(reference.run_dir)}")
        sha = spec.sha256_file(reference.run_dir / "analysis.json")
        if sha != reference.analysis_sha256:
            raise SystemExit(f"reference run {key}: analysis sha256 {sha} != pinned")
        observed[key] = {"run_dir": spec.relative(reference.run_dir), "analysis_sha256": sha}
    return observed


def build_configs(*, check: bool, force: bool) -> int:
    raw, derived = gate_parent()
    datasets = gate_datasets()
    references = gate_reference_runs()

    for variant in spec.VARIANTS:
        config = json.loads(json.dumps(derived))
        for path, expected_from, to in variant.deltas:
            current = spec.get_path(config, path)
            if not spec.numeric_equal(current, expected_from):
                raise SystemExit(
                    f"{variant.key}: declared from {expected_from!r} but the parent carries {current!r} at {path}"
                )
            spec.set_path(config, path, to)
        for path in spec.PARENT_DERIVATION_PATHS:
            if path.startswith("bot.short.") and not spec.numeric_equal(
                spec.get_path(config, path), spec.STOP_LOSS_DEFAULTS[path.rsplit(".", 1)[1]]
            ):
                raise SystemExit(f"{variant.key}: an arm must not change the short side ({path})")
        config["backtest"]["base_dir"] = str(variant.base_dir)
        apply_dataset(config, variant.dataset)
        if check:
            print(
                f"  would write {spec.relative(variant.config_path)} "
                f"(leg={variant.leg} bundle={variant.dataset.rel_path.name})"
            )
            continue
        spec.write_json(variant.config_path, config)

    if not check:
        payload = {
            "study": "g4_twe300_sl_cooldown_2026-09-18",
            "parent_config": spec.relative(spec.PARENT_CONFIG),
            "parent_config_sha256": spec.PARENT_CONFIG_SHA256,
            "parent_derivation_paths": list(spec.PARENT_DERIVATION_PATHS),
            "stop_loss_defaults": spec.STOP_LOSS_DEFAULTS,
            "no_search": spec.NO_SEARCH,
            "datasets": datasets,
            "reference_runs": references,
            "synthetic_injections": spec.SYNTHETIC_INJECTIONS,
            "honesty_boundaries": list(spec.HONESTY_BOUNDARIES),
            "thresholds": THRESHOLDS,
            "arms": [
                {
                    "key": variant.key,
                    "lever": variant.lever,
                    "leg": variant.leg,
                    "description": variant.description,
                    "stop_loss": variant.declared_stop_loss,
                    "deltas": [[path, from_value, to] for path, from_value, to in variant.deltas],
                    "config": spec.relative(variant.config_path),
                    "bundle_dir": spec.relative(variant.bundle_dir),
                    "synthetic": variant.synthetic,
                }
                for variant in spec.VARIANTS
            ],
        }
        if spec.VARIANT_INPUT_PATH.exists() and not force:
            previous = spec.load_json(spec.VARIANT_INPUT_PATH)
            if previous != payload:
                print("note: variant_input.json content changed (rerun with --force to overwrite)")
        spec.write_json(spec.VARIANT_INPUT_PATH, payload)
        print(f"wrote {len(spec.VARIANTS)} arm configs + {spec.relative(spec.VARIANT_INPUT_PATH)}")
    return 0


# ----------------------------------------------------------------------------- run


def assert_dataset(variant: spec.Variant, run_dir: Path) -> None:
    """A leg is only that leg if the run really resolved the intended frozen bundle.

    Without this gate a mis-retargeted config replays the parent's native window and every leg
    silently produces the same numbers -- which is what a reused stale run would otherwise hide.
    """
    dataset = spec.load_json(run_dir / "dataset.json")
    label = str(dataset.get("cache_dir_label") or "")
    if label != variant.dataset.rel_path.name:
        raise SystemExit(
            f"{variant.key}: run {spec.relative(run_dir)} resolved {label!r} but the leg declares "
            f"{variant.dataset.rel_path.name!r}; delete the stale run or fix the dataset override"
        )


def run_one(variant: spec.Variant, *, force: bool, timeout_s: float) -> Path:
    existing = newest_run(variant.runs_base, required=False)
    if existing is not None and not force:
        assert_dataset(variant, existing)
        print(f"  {variant.key}: reusing {spec.relative(existing)}")
        return existing
    need = spec.MIN_AVAILABLE_MB[variant.leg]
    avail = available_mb()
    if avail < need:
        raise SystemExit(
            f"{variant.key}: only {avail} MB available, this leg needs about {need} MB; "
            "stop other work and retry"
        )
    variant.log_dir.mkdir(parents=True, exist_ok=True)
    # The engine refuses to append to an existing audit file, so a resumed run must clear it.
    if variant.execution_audit_path.exists():
        variant.execution_audit_path.unlink()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(spec.REPO / "src")
    env.setdefault("PYTHONUNBUFFERED", "1")
    command = [
        str(PY),
        str(spec.REPO / "src/backtest.py"),
        str(variant.config_path),
        "--execution-audit-path",
        str(variant.execution_audit_path),
        *spec.RUNTIME_FLAGS,
    ]
    started = time.time()
    with variant.replay_log_path.open("w", encoding="utf-8") as log:
        log.write("command: " + " ".join(command) + "\n")
        log.flush()
        completed = subprocess.run(
            command, cwd=str(spec.REPO), env=env, stdout=log, stderr=subprocess.STDOUT,
            timeout=timeout_s, check=False,
        )
    elapsed = time.time() - started
    if completed.returncode != 0:
        tail = variant.replay_log_path.read_text(encoding="utf-8")[-2000:]
        raise SystemExit(f"{variant.key}: backtest exited {completed.returncode}\n{tail}")
    run_dir = newest_run(variant.runs_base)
    assert_dataset(variant, run_dir)
    spec.write_json(
        variant.bundle_dir / "replay_provenance.json",
        {
            "arm": variant.key,
            "lever": variant.lever,
            "leg": variant.leg,
            "synthetic": variant.synthetic,
            "stop_loss": variant.declared_stop_loss,
            "description": variant.description,
            "run_dir": spec.relative(run_dir),
            "elapsed_s": round(elapsed, 2),
            "flags": list(spec.RUNTIME_FLAGS),
            "disabled_plot_groups": list(spec.DISABLED_PLOT_GROUPS),
        },
    )
    print(f"  {variant.key}: {spec.relative(run_dir)} in {elapsed:.0f}s")
    return run_dir


def run_arms(*, only_variant: str | None, only_leg: str | None, only_stage: str | None,
             force: bool, check: bool, timeout_s: float) -> int:
    order = [
        spec.VARIANTS_BY_KEY[key]
        for key in spec.RUN_VARIANT_ORDER
        if (only_variant is None or key == only_variant)
        and (only_leg is None or key.endswith(f"__{only_leg}"))
        and (only_stage is None or spec.stage_of(spec.VARIANTS_BY_KEY[key]) == only_stage)
    ]
    if not order:
        raise SystemExit("no arm matches the given filters")
    print(f"{len(order)} arm(s) selected; available memory {available_mb()} MB")
    if check:
        for variant in order:
            need = spec.MIN_AVAILABLE_MB[variant.leg]
            print(f"  {variant.key}: leg={variant.leg} needs>={need}MB config={variant.config_path.exists()}")
        return 0
    for variant in order:
        run_one(variant, force=force, timeout_s=timeout_s)
    return 0


# ------------------------------------------------------------------------ identity


def identity(*, legs: tuple[str, ...], check: bool) -> int:
    """`s0_off__<leg>` must reproduce the pinned previous-round run exactly."""
    failures: list[str] = []
    report: dict[str, Any] = {}
    for leg in legs:
        reference = spec.reference_for_leg(leg)
        if reference is None:
            continue
        variant = spec.VARIANTS_BY_KEY[f"s0_off__{leg}"]
        run_dir = newest_run(variant.runs_base, required=False)
        if run_dir is None:
            message = f"{variant.key}: no completed run yet"
            if check:
                print(f"  PENDING {message}")
                continue
            failures.append(message)
            continue
        pinned_fills = reference.run_dir / "fills.csv"
        fresh_fills = run_dir / "fills.csv"
        pinned_sha, fresh_sha = spec.sha256_file(pinned_fills), spec.sha256_file(fresh_fills)
        left = spec.load_json(reference.run_dir / "analysis.json")
        right = spec.load_json(run_dir / "analysis.json")
        problems: list[str] = []
        for key in sorted(set(left) | set(right)):
            if key not in right:
                problems.append(f"{key}: missing")
            elif key not in left:
                if key == "hard_stop_ladder_strikes_max":
                    # New telemetry that postdates the anchor. The ladder is off in every arm of
                    # this study, so it must merely restate the anchor's own halt count; a different
                    # number would mean the guard behaved differently, which is a real failure.
                    strikes = right[key]
                    triggers = left.get("hard_stop_triggers")
                    if triggers is not None and strikes != triggers:
                        problems.append(
                            f"{key}: {strikes!r} but the anchor's hard_stop_triggers is {triggers!r}"
                        )
                    continue
                if key == "hard_stop_realized_loss_halt_pct_max":
                    if right[key] not in (0, 0.0):
                        problems.append(f"{key}: allowlisted new metric is non-zero ({right[key]!r})")
                    continue
                problems.append(f"{key}: extra ({right[key]!r})")
            elif left[key] != right[key]:
                problems.append(f"{key}: pinned {left[key]!r} != fresh {right[key]!r}")
        sl_rows = sum(1 for row in read_fills(fresh_fills) if is_stop_loss(row))
        report[leg] = {
            "arm": variant.key,
            "run_dir": spec.relative(run_dir),
            "reference_run_dir": spec.relative(reference.run_dir),
            "fills_sha256_match": pinned_sha == fresh_sha,
            "fills_sha256": fresh_sha,
            "metric_differences": problems,
            "stop_loss_fills": sl_rows,
        }
        print(
            f"  {leg}: fills {'identical' if pinned_sha == fresh_sha else 'DIFFER'}, "
            f"{len(problems)} metric difference(s), {sl_rows} stop-loss fill(s)"
        )
        if pinned_sha != fresh_sha:
            failures.append(f"{leg}: fills.csv differs from the pinned anchor")
        if problems:
            failures.extend(f"{leg}: {line}" for line in problems[:10])
        if sl_rows:
            failures.append(f"{leg}: the default-off arm produced {sl_rows} stop-loss fill(s)")
    if not check:
        spec.write_json(spec.ARTIFACTS / "default_off_identity.json", report)
    if failures:
        for line in failures:
            print("FAIL", line)
        return 1
    print("identity: PASS")
    return 0


# ------------------------------------------------------------------------- analyse


def read_fills(path: Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def is_stop_loss(row: dict[str, str]) -> bool:
    return str(row.get("type", "")).startswith("close_stop_loss")


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def stop_loss_stats(run_dir: Path, cooldown_minutes: float) -> dict[str, Any]:
    rows = read_fills(run_dir / "fills.csv")
    tail_types = {str(row["type"]) for row in rows if is_stop_loss(row)}
    unknown = tail_types - set(spec.SL_FILL_TYPES)
    if unknown:
        raise SystemExit(f"{spec.relative(run_dir)}: unknown stop-loss fill types {sorted(unknown)}")
    sl_rows = [row for row in rows if is_stop_loss(row)]
    per_coin: dict[str, float] = {}
    last: dict[str, datetime] = {}
    inside = 0
    for row in rows:
        coin = row["coin"]
        kind = str(row["type"])
        if is_stop_loss(row):
            last[coin] = parse_stamp(row["timestamp"])
            per_coin[coin] = per_coin.get(coin, 0.0) + float(row["pnl"]) + float(row["fee_paid"])
        elif kind.startswith("entry_") and coin in last:
            if parse_stamp(row["timestamp"]) - last[coin] < timedelta(minutes=cooldown_minutes):
                inside += 1
    maker = sum(1 for row in sl_rows if str(row.get("liquidity", "")) == "maker")
    return {
        "marker_found": bool(sl_rows),
        "stop_loss_fills_count": len(sl_rows),
        "stop_loss_realized_pnl_usd": round(sum(per_coin.values()), 2),
        "stop_loss_coins_count": len(per_coin),
        "entries_inside_cooldown": inside,
        "stop_loss_maker_fills": maker,
        "stop_loss_taker_fills": len(sl_rows) - maker,
        "worst_coins": sorted(per_coin.items(), key=lambda item: item[1])[:10],
        "max_single_coin_loss_usd": round(min(per_coin.values()), 2) if per_coin else None,
    }


def june_anchor(run_dir: Path) -> dict[str, Any]:
    """The 2026-06 anchor: month-end return and the worst intra-month equity drawdown.

    `monthly_metrics.csv` is written by the report step, not by the backtest, so the fallback
    recomputes both from the equity series the run itself wrote.
    """
    path = run_dir / "monthly_metrics.csv"
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("period") == "2026-06":
                    return {
                        "max_intraperiod_equity_drawdown_pct": round(
                            float(row["max_intraperiod_equity_drawdown_pct"]) * 100, 4
                        ),
                        "total_equity_return_pct": round(float(row["total_equity_return_pct"]) * 100, 4),
                        "fills_count": int(float(row["fills_count"])),
                        "source": "monthly_metrics.csv",
                    }
    equity = run_dir / "balance_and_equity.csv.gz"
    if not equity.exists():
        return {}
    import gzip

    first = lastest = None
    peak = None
    worst = 0.0
    with gzip.open(equity, "rt", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        stamp_key = columns[0]
        for row in reader:
            stamp = str(row.get(stamp_key, ""))
            if not stamp.startswith("2026-06"):
                continue
            value = float(row["usd_total_equity"])
            if first is None:
                first = value
            lastest = value
            peak = value if peak is None else max(peak, value)
            if peak:
                worst = min(worst, value / peak - 1.0)
    if first is None or lastest is None:
        return {}
    return {
        "max_intraperiod_equity_drawdown_pct": round(worst * 100, 4),
        "total_equity_return_pct": round((lastest / first - 1.0) * 100, 4),
        "fills_count": None,
        "source": "balance_and_equity.csv.gz",
    }


def metrics_of(run_dir: Path) -> dict[str, Any]:
    analysis = spec.load_json(run_dir / "analysis.json")
    out = {key: analysis.get(key) for key in spec.COMPARISON_METRIC_KEYS}
    equity = run_dir / "balance_and_equity.csv.gz"
    if equity.exists():
        import gzip

        first = last = None
        with gzip.open(equity, "rt", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                value = row.get("usd_total_equity")
                if value:
                    first = float(value) if first is None else first
                    last = float(value)
        if first and last:
            out["terminal_multiple"] = round(last / first, 4)
    return out


def analyse(*, out: Path, strict: bool, top_n: int) -> int:
    rows: list[dict[str, Any]] = []
    for variant in spec.VARIANTS:
        run_dir = newest_run(variant.runs_base, required=False)
        if run_dir is None:
            rows.append({"arm": variant.key, "leg": variant.leg, "status": "pending"})
            continue
        stats = stop_loss_stats(run_dir, float(variant.declared_stop_loss["cooldown_minutes"]))
        rows.append(
            {
                "arm": variant.key,
                "lever": variant.lever,
                "leg": variant.leg,
                "status": "complete",
                "synthetic": variant.synthetic,
                "stop_loss": variant.declared_stop_loss,
                "run_dir": spec.relative(run_dir),
                "metrics": metrics_of(run_dir),
                "stop_loss_stats": stats,
                "june_2026": june_anchor(run_dir),
            }
        )
    by_key = {row["arm"]: row for row in rows}

    def value(arm: str, *path: str) -> Any:
        node: Any = by_key.get(arm)
        for part in path:
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node

    def delta_pp(arm: str, base: str, key: str) -> float | None:
        left = value(arm, "metrics", key)
        right = value(base, "metrics", key)
        if left is None or right is None:
            return None
        return round((float(left) - float(right)) * 100, 4)

    verdicts: list[dict[str, Any]] = []

    def verdict(identifier: str, statement: str, threshold: Any, observed: Any, passed: bool | None) -> None:
        verdicts.append(
            {
                "id": identifier,
                "statement": statement,
                "threshold": threshold,
                "observed": observed,
                "pass": passed,
            }
        )

    # S0 -- default-off identity
    identity_path = spec.ARTIFACTS / "default_off_identity.json"
    if identity_path.exists():
        identity_report = spec.load_json(identity_path)
        legs_ok = [
            leg for leg, payload in identity_report.items()
            if payload.get("fills_sha256_match") and not payload.get("metric_differences")
            and not payload.get("stop_loss_fills")
        ]
        s0_pass = {"3y", "ext"}.issubset(set(legs_ok))
        verdict(
            "S0", "默认关闭时 s0_off 与上一轮同几何臂逐位一致（3y 与 ext）",
            "fills.csv sha256 相同 + 指标零差异 + 零止损成交",
            {"legs_identical": sorted(legs_ok)}, s0_pass,
        )
    else:
        verdict("S0", "默认关闭逐位一致", "identity 报告存在", None, None)

    # S1 -- mechanism
    s1 = "s1_sl15_cd1440__3y"
    fills = value(s1, "stop_loss_stats", "stop_loss_fills_count")
    inside = value(s1, "stop_loss_stats", "entries_inside_cooldown")
    if fills is None:
        verdict("S1", "机制：触发即整仓离场且冷却内不再加仓", "≥1 次止损成交且冷却内 0 笔入场", None, None)
    else:
        verdict(
            "S1", "机制：触发即整仓离场且冷却内不再加仓",
            "≥1 次止损成交且冷却内 0 笔入场",
            {"stop_loss_fills": fills, "entries_inside_cooldown": inside},
            bool(fills) and inside == 0,
        )

    # S2/S5 -- tail improvement on the synthetic single-coin wipeout (market tier only)
    base_syn = "s0_off__synth_a"
    armed_syn = "s1_sl15_cd1440__synth_a"
    dd_delta = delta_pp(armed_syn, base_syn, "drawdown_worst_strategy_eq")
    multiple_delta = None
    left_mult = value(armed_syn, "metrics", "terminal_multiple")
    right_mult = value(base_syn, "metrics", "terminal_multiple")
    if left_mult and right_mult:
        multiple_delta = round(float(left_mult) - float(right_mult), 4)
    observed = {"worst_drawdown_delta_pp": dd_delta, "terminal_multiple_delta": multiple_delta}
    if dd_delta is None:
        verdict("S2", "合成单币崩塌腿上尾部改善（市价档）", f"最差回撤改善 ≥ {THRESHOLDS['S2_tail_improvement_pp']}pp",
                observed, None)
        verdict("S5", "尾部收益必须在悲观（市价）档成立", "同 S2，且用的是 market 档臂", observed, None)
    else:
        improvement = -dd_delta  # drawdown is negative; a smaller magnitude is an improvement
        passed = improvement >= THRESHOLDS["S2_tail_improvement_pp"]
        verdict("S2", "合成单币崩塌腿上尾部改善（市价档）",
                f"最差回撤改善 ≥ {THRESHOLDS['S2_tail_improvement_pp']}pp", observed, passed)
        verdict("S5", "尾部收益必须在悲观（市价）档成立",
                "market 档臂满足 S2（limit 档不算数）", observed, passed)

    # S3 -- real-leg cost
    base_3y, armed_3y = "s0_off__3y", "s1_sl15_cd1440__3y"
    base_mult = value(base_3y, "metrics", "terminal_multiple")
    armed_mult = value(armed_3y, "metrics", "terminal_multiple")
    dd_3y = delta_pp(armed_3y, base_3y, "drawdown_worst_strategy_eq")
    if base_mult and armed_mult:
        degradation = round((1.0 - float(armed_mult) / float(base_mult)) * 100, 4)
        passed = degradation <= THRESHOLDS["S3_terminal_degradation_pct"] and (dd_3y or 0.0) <= 0.0
        verdict("S3", "真实腿代价不超过阈值，且回撤不恶化",
                f"终值退化 ≤ {THRESHOLDS['S3_terminal_degradation_pct']}% 且最差回撤不恶化",
                {"terminal_degradation_pct": degradation, "worst_drawdown_delta_pp": dd_3y}, passed)
    else:
        verdict("S3", "真实腿代价", f"终值退化 ≤ {THRESHOLDS['S3_terminal_degradation_pct']}% 且回撤不恶化", None, None)

    # S4 -- the limit tier must not beat the market tier on the synthetic legs
    market_dd = value("s1_sl15_cd1440__synth_a", "metrics", "drawdown_worst_strategy_eq")
    limit_dd = value("s4_sl15_cd1440_limit__synth_a", "metrics", "drawdown_worst_strategy_eq")
    limit_fills = value("s4_sl15_cd1440_limit__synth_a", "stop_loss_stats", "stop_loss_fills_count")
    if market_dd is None or limit_dd is None:
        verdict("S4", "限价档不得优于市价档（穿价不成交）", "limit 档最差回撤 ≥ market 档",
                {"limit_fills": limit_fills}, None)
    else:
        verdict("S4", "限价档不得优于市价档（穿价不成交）", "limit 档最差回撤 ≥ market 档",
                {"limit_worst_drawdown": limit_dd, "market_worst_drawdown": market_dd,
                 "limit_stop_loss_fills": limit_fills},
                float(limit_dd) >= float(market_dd))

    # S6 -- wick cost, taken from the Phase A census (declared threshold)
    census_path = spec.ARTIFACTS / "optimism" / "a_allow000__3y__excursion.json"
    if census_path.exists():
        census = spec.load_json(census_path)
        row = next((item for item in census["census"] if abs(item["level_pct"] - 0.15) < 1e-9), None)
        share = row.get("spike_exit_share_pct") if row else None
        verdict("S6", "插针代价：触发里「卖在低点」的比例",
                f"< {THRESHOLDS['S6_spike_exit_share_pct']}%",
                {"spike_exit_share_pct": share, "breaches": row.get("breaches") if row else None},
                None if share is None else share < THRESHOLDS["S6_spike_exit_share_pct"])
    else:
        verdict("S6", "插针代价", "census 存在", None, None)

    # S7 -- the 2026-06 anchor
    june_base = value("s0_off__3y", "june_2026")
    june_armed = value("s1_sl15_cd1440__3y", "june_2026")
    verdict("S7", "2026-06 锚点：止损臂相对基准的 6 月表现",
            "报数，不设通过线（基准 20.10% 回撤 / +6.81%）",
            {"baseline": june_base, "armed": june_armed}, None)

    payload = {
        "study": "g4_twe300_sl_cooldown_2026-09-18",
        "thresholds": THRESHOLDS,
        "honesty_boundaries": list(spec.HONESTY_BOUNDARIES),
        "verdicts": verdicts,
        "arms": rows,
    }
    spec.write_json(out, payload)

    print(f"{'arm':32s} {'leg':8s} {'mult':>7s} {'dd%':>8s} {'SL fills':>8s} {'SL pnl':>10s} {'inside':>7s}")
    for row in rows:
        if row.get("status") != "complete":
            print(f"{row['arm']:32s} PENDING")
            continue
        metrics = row["metrics"]
        stats = row["stop_loss_stats"]
        print(
            f"{row['arm']:32s} {row['leg']:8s} "
            f"{(metrics.get('terminal_multiple') or float('nan')):>7.3f} "
            f"{(metrics.get('drawdown_worst_strategy_eq') or float('nan')) * 100:>8.2f} "
            f"{stats['stop_loss_fills_count']:>8d} {stats['stop_loss_realized_pnl_usd']:>10.0f} "
            f"{stats['entries_inside_cooldown']:>7d}"
        )
    print("\nverdicts:")
    for item in verdicts:
        state = {True: "PASS", False: "FAIL", None: "N/A "}[item["pass"]]
        print(f"  {item['id']} {state} threshold={item['threshold']}")
        print(f"      observed={json.dumps(item['observed'], ensure_ascii=False)}")
    print(f"\nwrote {spec.relative(out)}")
    incomplete = [row for row in rows if row.get("status") != "complete"]
    if strict and incomplete:
        print(f"{len(incomplete)} arm(s) pending")
        return 1
    if any(item["pass"] is False for item in verdicts):
        return 1
    return 0


# ---------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="gate the parent and write the arm configs")
    build.add_argument("--check", action="store_true")
    build.add_argument("--force", action="store_true")

    run = sub.add_parser("run", help="replay arms")
    run.add_argument("--variant")
    run.add_argument("--leg", choices=list(spec.LEG_ORDER))
    run.add_argument("--stage", choices=list(spec.STAGES))
    run.add_argument("--force", action="store_true")
    run.add_argument("--check", action="store_true")
    run.add_argument("--timeout-s", type=float, default=3600.0)

    identity_parser = sub.add_parser("identity", help="default-off bit identity against the anchors")
    identity_parser.add_argument("--legs", nargs="+", default=["3y", "ext"])
    identity_parser.add_argument("--check", action="store_true")

    analyse_parser = sub.add_parser("analyse", help="emit the S0-S7 verdicts")
    analyse_parser.add_argument("--out", default=str(spec.ARTIFACTS / "sl_analysis.json"))
    analyse_parser.add_argument("--strict", action="store_true")
    analyse_parser.add_argument("--top-n", type=int, default=10)

    args = parser.parse_args(argv)
    if args.command == "build":
        return build_configs(check=args.check, force=args.force)
    if args.command == "run":
        return run_arms(
            only_variant=args.variant, only_leg=args.leg, only_stage=args.stage,
            force=args.force, check=args.check, timeout_s=args.timeout_s,
        )
    if args.command == "identity":
        return identity(legs=tuple(args.legs), check=args.check)
    if args.command == "analyse":
        return analyse(out=Path(args.out), strict=args.strict, top_n=args.top_n)
    raise SystemExit(f"unknown command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
