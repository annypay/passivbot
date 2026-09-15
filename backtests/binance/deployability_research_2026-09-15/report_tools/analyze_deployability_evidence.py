"""Derived economic/risk evidence, separate from immutable native replay artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import run_deployability_study as study
from account_diagnostics import (
    MINUTE_MS, apply_cost_overlay, digest_json, normalize_fills,
    period_metrics, save_json, sha256,
)
from evidence_integrity import (
    halfyear_metrics, verify_artifact_hashes, verify_completed_run,
)
from run_execution_and_portfolios import read_account


ROOT = study.ROOT
AGE_EDGES = [0, 5, 30, 120, 360, 1440, 10080, 43200, np.inf]
AGE_LABELS = ["0-5m", "5-30m", "30-120m", "2-6h", "6-24h", "1-7d", "7-30d", "30d+"]


def monthly_risk(account: pd.DataFrame, initial: float) -> pd.DataFrame:
    if not np.all(np.diff(account.index.astype("int64")) == MINUTE_MS * 1_000_000):
        raise AssertionError("monthly risk requires a complete minute timeline")
    result = period_metrics(account, initial)
    equity = account["equity"].to_numpy()
    hwm = np.maximum.accumulate(np.r_[initial, equity])[1:]
    previous_hwm = np.maximum.accumulate(np.r_[initial, equity[:-1]])
    extra = pd.DataFrame({
        "global_account_drawdown": np.maximum(0., 1. - equity / hwm),
        "adverse_hl_stress_from_previous_close_hwm": np.maximum(
            0., 1. - account["adverse_hl_equity"].to_numpy() / previous_hwm),
        "gross_mark_exposure": account["gross_mark_notional"].to_numpy() / equity,
    }, index=account.index)
    months = account.index.tz_localize(None).to_period("M")
    for index, (month, group) in enumerate(extra.groupby(months, sort=True)):
        if result.loc[index, "month"] != str(month):
            raise AssertionError("monthly grouping mismatch")
        result.loc[index, "worst_since_inception_drawdown_in_month"] = (
            group["global_account_drawdown"].max())
        result.loc[index, "month_end_since_inception_drawdown"] = (
            group["global_account_drawdown"].iloc[-1])
        result.loc[index, "adverse_hl_stress_from_previous_close_hwm"] = (
            group["adverse_hl_stress_from_previous_close_hwm"].max())
        result.loc[index, "max_gross_mark_exposure"] = group["gross_mark_exposure"].max()
        result.loc[index, "full_calendar_month"] = len(group) == month.days_in_month * 1440
    return result


def age_economics(lots: pd.DataFrame) -> pd.DataFrame:
    if lots.empty:
        raise ValueError("no closed lots available for age economics")
    frame = lots.copy()
    frame["age_bucket"] = pd.cut(
        frame["holding_minimum_minutes"], AGE_EDGES, labels=AGE_LABELS, right=False)
    frame["gross_profit"] = frame["allocated_average_cost_pnl_usdt"].clip(lower=0.)
    frame["gross_loss"] = -frame["allocated_average_cost_pnl_usdt"].clip(upper=0.)
    return frame.groupby(["protective", "age_bucket"], observed=True, sort=True).agg(
        lot_matches=("qty", "size"), closed_notional_usdt=("close_notional_usdt", "sum"),
        gross_profit_usdt=("gross_profit", "sum"), gross_loss_usdt=("gross_loss", "sum"),
        average_cost_pnl_usdt=("allocated_average_cost_pnl_usdt", "sum"),
        entry_fees_signed_usdt=("allocated_entry_fee_usdt", "sum"),
        exit_fees_signed_usdt=("allocated_close_fee_usdt", "sum"),
        net_attributed_pnl_usdt=("allocated_net_pnl_usdt", "sum"),
    ).reset_index()


def reduction_economics(fills: pd.DataFrame, audit: pd.DataFrame, multipliers: dict) -> tuple:
    if len(fills) != len(audit):
        raise AssertionError("reduction diagnostic audit length differs")
    if not np.array_equal(fills["coin"].to_numpy(), audit["symbol"].to_numpy()):
        raise AssertionError("reduction diagnostic audit coin order differs")
    frame = fills.copy()
    frame["notional"] = (
        frame["qty"].abs() * frame["price"] * frame["coin"].map(multipliers))
    if not np.isfinite(frame["notional"]).all():
        raise ValueError("missing contract multiplier for traded coin")
    frame["decision_index"] = audit["decision_index"]
    frame["fill_index"] = audit["fill_index"]
    frame["protective"] = frame["type"].str.startswith(
        ("close_unstuck", "close_auto_reduce", "close_panic"))
    frame["gross_profit"] = frame["pnl"].clip(lower=0.)
    frame["gross_loss"] = -frame["pnl"].clip(upper=0.)
    frame["net_cash_pnl"] = frame["pnl"] + frame["fee_paid"]
    economics = frame.groupby("type", sort=True).agg(
        fills=("qty", "size"), turnover_usdt=("notional", "sum"),
        gross_profit_usdt=("gross_profit", "sum"), gross_loss_usdt=("gross_loss", "sum"),
        pnl_usdt=("pnl", "sum"), signed_fees_usdt=("fee_paid", "sum"),
        net_cash_pnl_usdt=("net_cash_pnl", "sum"),
    ).reset_index()
    previous = frame.groupby(["coin", "position_side"], sort=False)[
        ["timestamp", "psize", "protective", "fill_index", "type"]].shift(1)
    entries = frame["type"].str.startswith("entry_")
    follows_partial_reducer = (
        entries & previous["protective"].eq(True) & previous["psize"].abs().gt(0))
    followups = frame.loc[follows_partial_reducer].copy()
    followups["prior_reducer_type"] = previous.loc[follows_partial_reducer, "type"]
    followups["gap_minutes"] = (
        followups["timestamp"] - previous.loc[follows_partial_reducer, "timestamp"]) / MINUTE_MS
    followups["decided_before_reduction_fill"] = (
        followups["decision_index"] < previous.loc[follows_partial_reducer, "fill_index"])
    regular = frame["type"].str.startswith("close_") & ~frame["protective"]
    entry_notional = float(frame.loc[entries, "notional"].sum())
    if entry_notional <= 0:
        raise ValueError("no entry notional available for reducer churn diagnostic")
    summary = {
        "strategy_close_gross_loss_usdt": float(frame.loc[regular, "gross_loss"].sum()),
        "strategy_close_gross_profit_usdt": float(frame.loc[regular, "gross_profit"].sum()),
        "strategy_close_loss_fill_share": float(frame.loc[regular, "pnl"].lt(0).mean()),
        "protective_close_gross_loss_usdt": float(frame.loc[frame["protective"], "gross_loss"].sum()),
        "protective_close_net_cash_pnl_usdt": float(
            frame.loc[frame["protective"], "net_cash_pnl"].sum()),
        "entry_immediately_after_partial_reducer_count": len(followups),
    }
    for minutes in (5, 60, 180):
        recent = followups["gap_minutes"] < minutes
        summary[f"entry_within_{minutes}m_of_partial_reducer_count"] = int(recent.sum())
        summary[f"entry_within_{minutes}m_of_partial_reducer_notional_share"] = float(
            followups.loc[recent, "notional"].sum() / entry_notional)
        summary[f"entry_within_{minutes}m_decided_before_reduction_count"] = int(
            (recent & followups["decided_before_reduction_fill"]).sum())
    return economics, followups, summary


def derive_run(folder: Path, output: Path, multipliers: dict) -> tuple:
    complete = verify_completed_run(folder)
    summary = json.loads((folder / "summary.json").read_text())
    config = json.loads((folder / "config.json").read_text())
    initial = float(config["backtest"]["starting_balance"])
    account = read_account(folder / "account_path.npz")
    funded = apply_cost_overlay(account, extra_bps=0., funding_bps_per_8h=1.)
    months, halves = monthly_risk(funded, initial), halfyear_metrics(
        funded, initial, study.HALF_YEAR_EDGES)
    fills = normalize_fills(pd.read_csv(folder / "fills.csv"))
    lots = pd.read_csv(folder / "closed_inventory_ages.csv")
    audit = pd.read_csv(folder / "execution_audit.csv")
    type_economics, followups, reduction = reduction_economics(fills, audit, multipliers)
    expected_rates = fills["liquidity"].map({
        "maker": config["backtest"]["maker_fee_override"],
        "taker": config["backtest"]["taker_fee_override"],
    })
    expected_fees = fills["qty"].abs() * fills["price"] * fills["coin"].map(
        multipliers) * expected_rates
    if not np.isfinite(expected_fees).all():
        raise AssertionError("unrecognized liquidity or multiplier in fee reconciliation")
    fee_residual = float((fills["fee_paid"] + expected_fees).abs().max())
    if fee_residual > 1e-7:
        raise AssertionError(f"native charged fees differ from configured rates: {fee_residual}")
    source_months = pd.read_csv(folder / "monthly_funding_stress.csv")
    np.testing.assert_allclose(months["mdd"], source_months["mdd"], atol=1e-12, rtol=1e-10)
    np.testing.assert_allclose(months["return"], source_months["return"], atol=1e-12, rtol=1e-10)
    original_halves = pd.read_csv(folder / "halfyear_funding_stress.csv")
    np.testing.assert_allclose(halves["mdd"], original_halves["mdd"], atol=1e-12, rtol=1e-10)
    np.testing.assert_allclose(halves["return"], original_halves["return"], atol=1e-12, rtol=1e-10)
    recomputed_failures = study.evaluate_gate(summary, months, halves)
    if recomputed_failures != summary["gate_failures"]:
        raise AssertionError("versioned balance-MDD correction changed an account-equity gate")
    metrics = {
        **summary, **reduction, "scenario": folder.parent.name,
        "extra_one_way_bps_to_exhaust_funded_terminal_gain_fixed_ledger": (
            (float(funded["equity"].iloc[-1]) - initial)
            / float(account["turnover_notional"].sum()) * 10000.),
        "fee_reconciliation_max_residual_usdt": fee_residual,
        "worst_month_local_mdd": float(months["mdd"].max()),
        "worst_month_return": float(months["return"].min()),
        "full_month_positive_share": float(
            months.loc[months["full_calendar_month"], "return"].gt(0).mean()),
        "original_halfyear_balance_mdd_max_absolute_correction": float(
            (halves["balance_mdd"] - original_halves["balance_mdd"]).abs().max()),
    }
    for key in ("peak_ms", "trough_ms", "recovery_ms"):
        value = summary[f"funding_{key}"]
        metrics[f"funding_{key.removesuffix('_ms')}_utc"] = (
            pd.Timestamp(value, unit="ms", tz="UTC").isoformat() if value is not None else None)
    target = output / "cases" / folder.parent.name / folder.name
    target.mkdir(parents=True, exist_ok=True)
    months.to_csv(target / "monthly_account_risk.csv", index=False)
    halves.to_csv(target / "halfyear_account_risk.csv", index=False)
    age_economics(lots).to_csv(target / "age_bucket_economics.csv", index=False)
    type_economics.to_csv(target / "fill_type_economics.csv", index=False)
    followups.to_csv(target / "partial_reducer_followup_entries.csv", index=False)
    save_json(target / "summary.json", metrics)
    for table in (months, halves):
        table.insert(0, "name", folder.name)
        table.insert(0, "scenario", folder.parent.name)
    metrics.pop("audit")
    metrics["gate_failures"] = ",".join(metrics["gate_failures"])
    return metrics, months, halves, sha256(folder / "completion.json"), complete["identity"]


def compare_execution_paths(names: list[str]) -> pd.DataFrame:
    rows = []
    for name in names:
        for left, right in (("C1", "C2"), ("C3", "C4")):
            folders = [ROOT / "runs" / f"{mode}_conservative" / name for mode in (left, right)]
            for folder in folders:
                verify_completed_run(folder)
            paths = [np.load(folder / "minute_equity.npy", allow_pickle=False)
                     for folder in folders]
            fills = [pd.read_csv(folder / "fills.csv") for folder in folders]
            rows.append({
                "name": name, "left": left, "right": right,
                "minute_equity_bitwise_equal": bool(np.array_equal(*paths)),
                "fills_equal": fills[0].equals(fills[1]),
                "max_absolute_equity_difference_usdt": float(
                    np.max(np.abs(paths[0][:, 1] - paths[1][:, 1])))
                if paths[0].shape == paths[1].shape else None,
            })
    return pd.DataFrame(rows)


def parameter_diagnostics(output: Path) -> None:
    active = {}
    for spec in study.specs():
        config = json.loads((ROOT / "candidate_configs" / f"{spec['name']}.json").read_text())
        active[spec["name"]] = config["bot"][spec["direction"]]
    save_json(output / "active_side_parameters.json", active)
    hashes = {name: digest_json(value) for name, value in active.items()}
    duplicate_groups = {}
    for name, checksum in hashes.items():
        duplicate_groups.setdefault(checksum, []).append(name)
    save_json(output / "active_side_parameter_duplicates.json", {
        "scope": "entire active bot side; inactive side and metadata excluded",
        "groups": [names for names in duplicate_groups.values() if len(names) > 1],
        "active_side_parameter_hashes": hashes,
    })


def build(phase: str) -> None:
    study.offline()
    tool = study.prior_tool()
    tool.disable_network()
    settings = json.loads((tool.DATASET / "market_specific_settings.json").read_text())
    multipliers = {coin: float(values["c_mult"]) for coin, values in settings.items()}
    names = [item["name"] for item in study.specs()]
    folders = [ROOT / "runs/C3_conservative" / name for name in names]
    sidecar = ROOT / "execution_and_portfolios"
    decision = json.loads((sidecar / "stress_shortlist_lock.json").read_text())
    if phase == "final":
        folders += [
            ROOT / "runs" / f"{mode}_{cost}" / name
            for mode, cost in decision["scenarios"] for name in decision["selected_names"]]
    output = ROOT / "analysis_v2" / phase
    output.mkdir(parents=True, exist_ok=True)
    rows, months, halves, sources, identities = [], [], [], {}, {}
    for folder in folders:
        metrics, month, half, checksum, identity = derive_run(folder, output, multipliers)
        rows.append(metrics)
        months.append(month)
        halves.append(half)
        sources[str(folder.relative_to(ROOT))] = checksum
        identities[str(folder.relative_to(ROOT))] = identity
    frame = pd.DataFrame(rows)
    frame.to_csv(output / "account_economics.csv", index=False)
    pd.concat(months, ignore_index=True).to_csv(output / "monthly_account_risk.csv", index=False)
    pd.concat(halves, ignore_index=True).to_csv(output / "halfyear_account_risk.csv", index=False)
    primary = frame.loc[frame["scenario"] == "C3_conservative"].set_index("name")
    columns = [
        "funding_cagr", "funding_mdd", "total_fees_usdt", "max_unrealized_loss_usdt",
        "average_gross_mark_exposure", "effective_coin_count_exposure_time",
        "regular_closed_notional_weighted_median_hold_minutes",
        "protective_close_gross_loss_usdt", "strategy_close_gross_loss_usdt",
        "entry_within_60m_of_partial_reducer_notional_share",
    ]
    (primary[columns] - primary.loc["tm_reference", columns]).reset_index().to_csv(
        output / "controlled_deltas_vs_tm_reference.csv", index=False)
    parameter_diagnostics(output)
    if phase == "final":
        compare_execution_paths(decision["selected_names"]).to_csv(
            output / "c1_c2_c3_c4_exact_equivalence.csv", index=False)
        pairs = json.loads((ROOT / "research_contract.json").read_text())["combo_pairs"]
        portfolio_rows = []
        for left, right in pairs:
            folder = sidecar / "portfolios/C3_conservative" / f"{left}__{right}"
            manifest = json.loads((folder / "analysis_manifest.json").read_text())
            for child, checksum in manifest["source_completions"].items():
                verify_completed_run(ROOT / child)
                if sha256(ROOT / child / "completion.json") != checksum:
                    raise AssertionError("portfolio input changed since analysis")
            verify_artifact_hashes(folder, manifest["artifact_hashes"],
                                   ("summary.json", "account_path.npz"))
            row = json.loads((folder / "summary.json").read_text())
            row["gate_failures"] = ",".join(row["gate_failures"])
            portfolio_rows.append(row)
            sources[str(folder.relative_to(ROOT))] = sha256(folder / "analysis_manifest.json")
        pd.DataFrame(portfolio_rows).to_csv(output / "portfolio_economics.csv", index=False)
    save_json(output / "producer_identities.json", identities)
    files = sorted(path for path in output.rglob("*")
                   if path.is_file() and path.name != "analysis_manifest.json")
    save_json(output / "analysis_manifest.json", {
        "phase": phase, "schema_version": 2,
        "source_completion_hashes": sources,
        "research_contract_sha256": sha256(ROOT / "research_contract.json"),
        "analysis_source_sha256": sha256(Path(__file__)),
        "account_equity_gates_unchanged": True,
        "correction": "halfyear balance MDD seeded with actual prior balance, not prior equity",
        "artifact_hashes": {str(path.relative_to(output)): sha256(path) for path in files},
    })
    print(frame[[
        "name", "scenario", "funding_cagr", "funding_mdd",
        "regular_close_notional_under_5m_share", "strategy_close_gross_loss_usdt",
        "protective_close_gross_loss_usdt", "entry_within_60m_of_partial_reducer_notional_share",
    ]].to_string(index=False))
    print(f"Versioned analysis persisted: {output}; scenarios={len(frame)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("primary", "final"))
    build(parser.parse_args().phase)
