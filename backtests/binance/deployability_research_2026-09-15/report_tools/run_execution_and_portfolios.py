"""Execution stress and genuinely capital-segregated portfolio research."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

import run_deployability_study as study
from account_diagnostics import (
    FILL_COLUMNS, apply_cost_overlay, audit_execution, digest_json,
    holding_diagnostics, normalize_fills, performance, period_metrics,
    reconstruct_account, save_json, sha256, weighted_quantile,
)
from evidence_integrity import (
    execute_or_recover, finish_run, halfyear_metrics, verify_all_runs, verify_completed_run,
)


ROOT = study.ROOT
SIDECAR = ROOT / "execution_and_portfolios"


def shortlist() -> dict:
    cases = study.specs()
    rows = []
    evidence = {}
    for spec in cases:
        directory = ROOT / "runs/C3_conservative" / spec["name"]
        completion = directory / "completion.json"
        if not completion.exists():
            raise RuntimeError(f"primary screen is not complete: {spec['name']}")
        verify_completed_run(directory)
        summary = json.loads((directory / "summary.json").read_text())
        evidence[spec["name"]] = sha256(completion)
        if spec["direction"] == "long":
            rows.append(summary)
    ranked = sorted(rows, key=lambda row: (
        len(row["gate_failures"]), row["funding_mdd"], -row["funding_cagr"], row["name"]))
    chosen = list(dict.fromkeys([row["name"] for row in ranked[:3]] + [
        "tm_reference", "tm_slow_combination", "ema_inventory"]))
    decision = {
        "primary_screen_contract_sha256": sha256(ROOT / "research_contract.json"),
        "source_completion_hashes": evidence, "selected_names": chosen,
        "selection_type": "prespecified_retrospective_stress_shortlist_not_unseen_selection",
        "scenarios": [
            ["C1", "conservative"], ["C2", "conservative"],
            ["C4", "conservative"], ["C3", "severe"],
        ],
    }
    save_json(SIDECAR / "stress_shortlist_lock.json", decision, immutable=True)
    return decision


def read_account(path: Path) -> pd.DataFrame:
    with np.load(path, allow_pickle=False) as source:
        data = {key: source[key] for key in source.files if key != "timestamps"}
        index = pd.to_datetime(source["timestamps"], unit="ms", utc=True)
    result = pd.DataFrame(data, index=index)
    result.index.name = "timestamp"
    return result


async def execution_stress(tool, execution: str | None, costs: str | None) -> None:
    import backtest

    decision = shortlist()
    selected = [row for row in study.specs() if row["name"] in decision["selected_names"]]
    scenarios = decision["scenarios"]
    if execution is not None:
        scenarios = [row for row in scenarios if row[0] == execution]
    if costs is not None:
        scenarios = [row for row in scenarios if row[1] == costs]
    if not scenarios:
        raise ValueError("requested scenario is not in the locked execution stress")
    for mode, fee_case in scenarios:
        state = study.canonicalize_state(
            await tool.prepare_window("trailing_martingale", *study.WINDOW, execution=mode))
        tool.install_state(state)
        for spec in selected:
            folder = ROOT / "runs" / f"{mode}_{fee_case}" / spec["name"]
            cfg = study.make_config(tool, spec, mode, fee_case)
            cfg["backtest"].update({
                "coins": {"binance": state["coins"]},
                "cache_dir": {"binance": state["cache_dir"]},
                "execution_audit_path": str(folder / "execution_audit.csv"),
            })
            identity = {
                "evidence_schema_version": 2,
                "spec": spec, "execution": mode, "cost_scenario": fee_case,
                "config_hash": digest_json(cfg),
                "contract_sha256": sha256(ROOT / "research_contract.json"),
                "runtime": tool.runtime_identity(),
                "dataset_manifest_sha256": sha256(tool.DATASET / "manifest.json"),
                "prepared_coins": state["coins"],
                "prepared_shape": list(state["hlcvs_basket"].shape),
                "runner_source_sha256": sha256(Path(study.__file__)),
                "diagnostics_source_sha256": sha256(
                    Path(__file__).with_name("account_diagnostics.py")),
                "execution_producer_sha256": sha256(Path(__file__)),
                "evidence_integrity_source_sha256": sha256(
                    Path(__file__).with_name("evidence_integrity.py")),
            }
            if (folder / "completion.json").exists():
                verify_completed_run(folder, identity)
                continue
            folder.mkdir(parents=True, exist_ok=True)
            save_json(folder / "config.json", cfg, immutable=True)
            started = time.monotonic()

            def execute():
                fills, raw, native, payload = backtest.run_backtest(
                    state["hlcvs_basket"], state["mss"], cfg, "binance",
                    state["btc_usd_prices"], state["timestamps"], return_payload=True)
                if payload.backtest_params["coins"] != state["coins"]:
                    raise AssertionError("runtime metadata differs from prepared HLCV axis")
                return fills, raw, native

            fills, raw, native = execute_or_recover(folder, identity, execute)
            summary = study.analyze_run(folder, cfg, spec, state, fills, raw, native)
            finish_run(folder, identity)
            print(json.dumps({
                "stress_candidate": spec["name"], "execution": mode, "costs": fee_case,
                "cagr": summary["funding_cagr"], "mdd": summary["funding_mdd"],
                "elapsed_s": time.monotonic() - started,
            }), flush=True)
    verify_all_runs()
    study.aggregate()


async def exact_sleeves(tool, execution: str, costs: str) -> None:
    import backtest

    contract = json.loads((ROOT / "research_contract.json").read_text())
    names = sorted({name for pair in contract["combo_pairs"] for name in pair})
    specs = [row for row in study.specs() if row["name"] in names]
    state = study.canonicalize_state(
        await tool.prepare_window("trailing_martingale", *study.WINDOW, execution=execution))
    coins = state["coins"]
    multipliers = {coin: float(state["mss"][coin]["c_mult"]) for coin in coins}
    price_steps = {coin: float(state["mss"][coin]["price_step"]) for coin in coins}
    for spec in specs:
        folder = SIDECAR / "capital_50000" / f"{execution}_{costs}" / spec["name"]
        cfg = study.make_config(tool, spec, execution, costs)
        cfg["backtest"].update({
            "starting_balance": 50000.0, "coins": {"binance": coins},
            "cache_dir": {"binance": state["cache_dir"]},
            "execution_audit_path": str(folder / "execution_audit.csv"),
        })
        identity = {
            "evidence_schema_version": 2,
            "research_contract_sha256": sha256(ROOT / "research_contract.json"),
            "candidate_name": spec["name"], "execution": execution, "costs": costs,
            "config_hash": digest_json(cfg), "runtime": tool.runtime_identity(),
            "dataset_manifest_sha256": sha256(tool.DATASET / "manifest.json"),
            "capital_usdt": 50000, "prepared_coins": coins,
            "sidecar_tool_sha256": sha256(Path(__file__)),
            "diagnostics_tool_sha256": sha256(Path(__file__).with_name("account_diagnostics.py")),
            "evidence_integrity_source_sha256": sha256(
                Path(__file__).with_name("evidence_integrity.py")),
        }
        complete = folder / "completion.json"
        if complete.exists():
            verify_completed_run(folder, identity)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        save_json(folder / "config.json", cfg, immutable=True)
        started = time.monotonic()
        def execute():
            source_fills, raw, native, payload = backtest.run_backtest(
                state["hlcvs_basket"], state["mss"], cfg, "binance",
                state["btc_usd_prices"], state["timestamps"], return_payload=True)
            if payload.backtest_params["coins"] != coins:
                raise AssertionError("runtime coin order differs from prepared HLCV axis")
            return source_fills, raw, native

        fills, raw, native = execute_or_recover(folder, identity, execute)
        account, concentration, risk = reconstruct_account(
            raw, fills, state["timestamps"], state["hlcvs_basket"], coins,
            multipliers, 50000.0)
        lots, episodes, holds = holding_diagnostics(fills, int(raw[-1, 0]), multipliers)
        audit = audit_execution(
            pd.read_csv(folder / "execution_audit.csv"), fills, state["timestamps"],
            state["hlcvs_basket"], coins, cfg["backtest"]["execution_delay_bars"],
            True, cfg["backtest"]["market_order_slippage_pct"], price_steps)
        metrics = performance(account, 50000)
        if abs(metrics["mdd"] - native["drawdown_worst_strategy_eq"]) > 1e-7:
            raise AssertionError("capital sleeve MDD differs from native")
        funded = apply_cost_overlay(account, extra_bps=0, funding_bps_per_8h=1)
        summary = {
            "name": spec["name"], "capital_usdt": 50000, "execution": execution,
            "costs": costs, **metrics, **risk, **holds, "audit": audit,
            "funding_metrics": performance(funded, 50000),
            "native_completion_ratio": native["backtest_completion_ratio"],
            "native_liquidated": native["liquidated"],
        }
        save_json(folder / "summary.json", summary)
        lots.to_csv(folder / "closed_inventory_ages.csv", index=False)
        episodes.to_csv(folder / "position_episodes.csv", index=False)
        concentration.to_csv(folder / "coin_concentration.csv", index=False)
        np.savez_compressed(
            folder / "account_path.npz", timestamps=raw[:, 0].astype(np.int64),
            **{column: account[column].to_numpy() for column in account})
        finish_run(folder, identity)
        print(json.dumps({"capital_sleeve": spec["name"], "execution": execution,
                          "costs": costs, "elapsed_s": time.monotonic() - started,
                          "cagr": summary["funding_metrics"]["cagr"]}), flush=True)


def combine_accounts(left: pd.DataFrame, right: pd.DataFrame) -> pd.DataFrame:
    if not left.index.equals(right.index):
        raise AssertionError("sleeve timelines differ; cannot backfill missing account history")
    columns = [
        "equity", "balance", "adverse_hl_equity", "gross_mark_notional",
        "cost_notional", "turnover_notional", "fees",
    ]
    # Each ledger really starts at 50k; this is a sum, not scaled 100k backtests.
    combined = left[columns].add(right[columns])
    combined.index.name = "timestamp"
    return combined


def portfolio_analysis(execution: str, costs: str) -> None:
    contract = json.loads((ROOT / "research_contract.json").read_text())
    rows = []
    for left, right in contract["combo_pairs"]:
        inputs = [SIDECAR / "capital_50000" / f"{execution}_{costs}" / name
                  for name in (left, right)]
        for path in inputs:
            if not (path / "completion.json").exists():
                raise RuntimeError(f"capital sleeve is not complete: {path}")
            verified = verify_completed_run(path)
            if verified["identity"]["capital_usdt"] != 50000:
                raise AssertionError("portfolio child is not a real 50k replay")
        accounts = [read_account(path / "account_path.npz") for path in inputs]
        account = combine_accounts(*accounts)
        funded = apply_cost_overlay(account, extra_bps=0, funding_bps_per_8h=1)
        extra = max(0.0, (study.COSTS["severe"]["maker_fee_override"]
                          - study.COSTS[costs]["maker_fee_override"]) * 10000)
        severe = apply_cost_overlay(account, extra_bps=extra, funding_bps_per_8h=1)
        metrics = performance(account, 100000)
        funded_metrics, severe_metrics = performance(funded, 100000), performance(severe, 100000)
        months = period_metrics(funded, 100000)
        halves = halfyear_metrics(funded, 100000, study.HALF_YEAR_EDGES)
        holdings = pd.concat([pd.read_csv(path / "closed_inventory_ages.csv")
                              for path in inputs], ignore_index=True)
        regular = holdings.loc[~holdings["protective"]]
        young = regular["holding_minimum_minutes"] < 5
        weights = regular["close_notional_usdt"]
        profits = regular["allocated_average_cost_pnl_usdt"].clip(lower=0)
        concentration = pd.concat([pd.read_csv(path / "coin_concentration.csv")
                                  for path in inputs]).groupby("coin", sort=True)[
                                      ["notional_minutes", "realized_pnl_usdt", "fees_usdt"]].sum()
        exposure_total = float(concentration["notional_minutes"].sum())
        if exposure_total <= 0:
            raise ValueError("portfolio has no observed exposure; cannot evaluate diversification")
        concentration["exposure_time_share"] = concentration["notional_minutes"] / exposure_total
        children = [json.loads((path / "summary.json").read_text()) for path in inputs]
        pair = f"{left}__{right}"
        output = SIDECAR / "portfolios" / f"{execution}_{costs}" / pair
        output.mkdir(parents=True, exist_ok=True)
        summary = {
            "name": pair, "execution": execution, "costs": costs, "capital_usdt": 100000,
            "left_sleeve": left, "right_sleeve": right, **metrics,
            **{f"funding_{key}": value for key, value in funded_metrics.items()},
            **{f"severe_fixed_ledger_{key}": value for key, value in severe_metrics.items()},
            "traded_coin_count": int((concentration["notional_minutes"] > 0).sum()),
            "top_coin_exposure_time_share": float(concentration["exposure_time_share"].max()),
            "effective_coin_count_exposure_time": float(
                1 / np.square(concentration["exposure_time_share"]).sum()),
            "regular_close_notional_under_5m_share": (
                float(weights[young].sum() / weights.sum()) if weights.sum() > 0 else None),
            "regular_gross_profit_under_5m_share": (
                float(profits[young].sum() / profits.sum()) if profits.sum() > 0 else None),
            "regular_closed_notional_weighted_median_hold_minutes": weighted_quantile(
                regular["holding_minimum_minutes"].to_numpy(), weights.to_numpy(), .5),
            "native_completion_ratio": min(row["native_completion_ratio"] for row in children),
            "native_liquidated": any(row["native_liquidated"] for row in children),
            "positive_month_share": float((months["return"] > 0).mean()),
            "positive_halfyears": int((halves["return"] > 0).sum()),
            "worst_halfyear_return": float(halves["return"].min()),
            "source_completion_sha256": [sha256(path / "completion.json") for path in inputs],
            "total_fees_usdt": float(account["fees"].sum()),
            "total_turnover_usdt": float(account["turnover_notional"].sum()),
        }
        summary["gate_failures"] = study.evaluate_gate(summary, months, halves)
        summary["research_screen_passed"] = not summary["gate_failures"]
        save_json(output / "summary.json", summary)
        concentration.reset_index().to_csv(output / "coin_concentration.csv", index=False)
        months.to_csv(output / "monthly_funding_stress.csv", index=False)
        halves.to_csv(output / "halfyear_funding_stress.csv", index=False)
        np.savez_compressed(output / "account_path.npz",
                            timestamps=account.index.astype("int64").to_numpy() // 1_000_000,
                            **{column: account[column].to_numpy() for column in account})
        daily = pd.concat([
            apply_cost_overlay(item, extra_bps=0, funding_bps_per_8h=1)["equity"]
            .resample("D").last().rename(name)
            for name, item in zip((left, right), accounts)], axis=1)
        returns = daily.pct_change(fill_method=None)
        returns.iloc[0] = daily.iloc[0] / 50000.0 - 1.0
        returns.to_csv(output / "daily_sleeve_returns.csv")
        summary["daily_return_correlation"] = float(returns[left].corr(returns[right]))
        summary["both_sleeves_negative_day_share"] = float((returns.lt(0).all(axis=1)).mean())
        tail_left = returns[left] <= returns[left].quantile(.05)
        summary["right_mean_return_on_left_worst_5pct_days"] = float(
            returns.loc[tail_left, right].mean())
        save_json(output / "summary.json", summary)
        files = sorted(path for path in output.iterdir()
                       if path.is_file() and path.name != "analysis_manifest.json")
        save_json(output / "analysis_manifest.json", {
            "source_completions": {str(path.relative_to(ROOT)): sha256(path / "completion.json")
                                   for path in inputs},
            "analysis_source_sha256": sha256(Path(__file__)),
            "artifact_hashes": {path.name: sha256(path) for path in files},
        })
        rows.append({**summary, "gate_failures": ",".join(summary["gate_failures"])})
    pd.DataFrame(rows).to_csv(SIDECAR / f"portfolio_summary_{execution}_{costs}.csv", index=False)


def pending_order_diagnostics() -> None:
    rows = []
    for completion in sorted((ROOT / "runs").glob("*/*/completion.json")):
        folder = completion.parent
        verify_completed_run(folder)
        config = json.loads((folder / "config.json").read_text())
        fills = normalize_fills(pd.read_csv(folder / "fills.csv"))
        audit = pd.read_csv(folder / "execution_audit.csv")
        if len(fills) != len(audit):
            raise AssertionError("pending-order diagnostic alignment mismatch")
        audit["entry"] = fills["type"].str.startswith("entry_")
        audit["timestamp"] = fills["timestamp"]
        entries = audit.loc[audit["entry"]].copy()
        previous = entries.groupby(["symbol", "pside"], sort=False)["fill_index"].shift(1)
        entries["gap_minutes"] = entries["fill_index"] - previous
        entries["decision_before_previous_entry_fill"] = entries["decision_index"] < previous
        gap = entries["gap_minutes"]
        cooldowns = {
            side: float(config["bot"][side]["risk"]["entry_cooldown_minutes"])
            for side in fills["position_side"].unique()
        }
        rows.append({
            "scenario": folder.parent.name, "name": folder.name,
            "entry_fills": len(entries),
            "entry_pairs_with_gap_under_2m": int((gap < 2).sum()),
            "entry_pairs_with_gap_under_5m": int((gap < 5).sum()),
            "under_2m_already_decided_before_previous_fill": int(
                ((gap < 2) & entries["decision_before_previous_entry_fill"]).sum()),
            "configured_entry_cooldown_minutes_by_pside": json.dumps(cooldowns, sort_keys=True),
            "under_5m_already_decided_before_previous_fill": int(
                ((gap < 5) & entries["decision_before_previous_entry_fill"]).sum()),
            "warning": "observed spacing, not proof of a cooldown violation or real exchange latency",
        })
    pd.DataFrame(rows).to_csv(SIDECAR / "pending_entry_spacing.csv", index=False)


async def main(args) -> None:
    study.offline()
    tool = study.prior_tool()
    tool.disable_network()
    study.freeze(tool)
    SIDECAR.mkdir(parents=True, exist_ok=True)
    if args.phase == "shortlist":
        print(json.dumps(shortlist(), indent=2))
    elif args.phase == "stress":
        await execution_stress(tool, args.execution, args.costs)
    elif args.phase == "sleeves":
        execution, costs = args.execution or "C3", args.costs or "conservative"
        await exact_sleeves(tool, execution, costs)
        portfolio_analysis(execution, costs)
    elif args.phase == "portfolios":
        portfolio_analysis(args.execution or "C3", args.costs or "conservative")
    elif args.phase == "pending":
        pending_order_diagnostics()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("shortlist", "stress", "sleeves", "portfolios", "pending"))
    parser.add_argument("--execution", choices=("C1", "C2", "C3", "C4"))
    parser.add_argument("--costs", choices=tuple(study.COSTS))
    asyncio.run(main(parser.parse_args()))
