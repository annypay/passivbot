"""Reproducible local report calculations; never executes or alters a strategy."""

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path("backtests/binance/causal_comparison_2026-09-14")
ORIGINAL = Path("backtests/binance/2026-09-14T03_40_41")


def result_dir(scenario):
    return Path(json.loads((ROOT / scenario / "run_identity.json").read_text())["result_dir"])


def fills_for(directory):
    frame = pd.read_csv(directory / "fills.csv")
    frame = frame.loc[:, ~frame.columns.str.startswith("Unnamed:")]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def baseline_check():
    original = fills_for(ORIGINAL)
    baseline = fills_for(result_dir("B0"))
    assert len(original) == len(baseline), (len(original), len(baseline))
    pd.testing.assert_frame_equal(original, baseline, check_exact=True)
    original_analysis = json.loads((ORIGINAL / "analysis.json").read_text())
    baseline_analysis = json.loads((result_dir("B0") / "analysis.json").read_text())
    deltas = {}
    for key in sorted(set(original_analysis) & set(baseline_analysis)):
        before, after = original_analysis[key], baseline_analysis[key]
        if isinstance(before, (float, int)) and not isinstance(before, bool):
            if not np.isclose(before, after, rtol=1e-12, atol=1e-12, equal_nan=True):
                deltas[key] = {"original": before, "baseline": after}
    assert not deltas, deltas
    output = {
        "fill_rows_exact_match": len(original),
        "common_numeric_analysis_match": True,
        "sampling_only_change": "minute equity saved separately; historical hour labels preserved",
    }
    (ROOT / "baseline_reproduction.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output))


def drawdown(values, initial):
    values = np.r_[initial, np.asarray(values, dtype=np.float64)]
    running_max = np.maximum.accumulate(values)
    return float(np.max(1.0 - values / running_max))


def drawdown_episode(timeline, initial, start):
    values = np.r_[initial, timeline["equity"].to_numpy(dtype=np.float64)]
    labels = pd.DatetimeIndex([start]).append(timeline.index)
    trough = int(np.argmax(1.0 - values / np.maximum.accumulate(values)))
    peak_value = float(values[:trough + 1].max())
    peak = int(np.flatnonzero(values[:trough + 1] == peak_value)[-1])
    recoveries = np.flatnonzero(values[trough + 1:] >= peak_value)
    recovery = int(trough + 1 + recoveries[0]) if len(recoveries) else None
    return {
        "max_drawdown_peak_candle": labels[peak].isoformat(),
        "max_drawdown_trough_candle": labels[trough].isoformat(),
        "max_drawdown_peak_equity": peak_value,
        "max_drawdown_trough_equity": float(values[trough]),
        "max_drawdown_recovery_candle": (
            labels[recovery].isoformat() if recovery is not None else None
        ),
        "max_drawdown_peak_to_recovery_days": (
            (labels[recovery] - labels[peak]).total_seconds() / 86400
            if recovery is not None else None
        ),
    }


def periods(scenario, timeline, fills, frequency, initial):
    previous_balance = initial
    previous_equity = initial
    output = []
    for period, group in timeline.groupby(timeline.index.strftime(frequency), sort=True):
        selected = fills.loc[fills["timestamp"].dt.strftime(frequency) == period]
        last = group.iloc[-1]
        output.append({
            "scenario": scenario,
            "period": period,
            "first_candle_label": group.index[0].isoformat(),
            "last_candle_label": group.index[-1].isoformat(),
            "opening_balance": previous_balance,
            "closing_balance": float(last["balance"]),
            "opening_equity": previous_equity,
            "closing_equity": float(last["equity"]),
            "balance_return": float(last["balance"] / previous_balance - 1),
            "equity_return": float(last["equity"] / previous_equity - 1),
            "minute_close_drawdown": drawdown(group["equity"], previous_equity),
            "fills": len(selected),
            "realized_pnl": float(selected["pnl"].sum()),
            "signed_fees": float(selected["fee_paid"].sum()),
            "net_realized_pnl": float((selected["pnl"] + selected["fee_paid"]).sum()),
        })
        previous_balance = float(last["balance"])
        previous_equity = float(last["equity"])
    return output


def analyze(scenario):
    directory = result_dir(scenario)
    cfg = json.loads((directory / "config.json").read_text())
    analysis = json.loads((directory / "analysis.json").read_text())
    fills = fills_for(directory)
    initial = float(cfg["backtest"]["starting_balance"])
    raw_equity = np.load(ROOT / scenario / "minute_equity.npy", mmap_mode="r", allow_pickle=False)
    timeline = pd.DataFrame(
        {"equity": raw_equity[:, 1]},
        index=pd.to_datetime(raw_equity[:, 0].astype(np.int64), unit="ms", utc=True),
    )
    assert float(cfg["backtest"]["btc_collateral_cap"]) == 0
    cash_changes = fills.groupby("timestamp")["usd_total_balance"].last()
    timeline["balance"] = cash_changes.reindex(timeline.index).ffill().fillna(initial)
    final_balance = float(timeline["balance"].iloc[-1])
    final_equity = float(timeline["equity"].iloc[-1])
    net_pnl = float((fills["pnl"] + fills["fee_paid"]).sum())
    assert np.isclose(final_balance, initial + net_pnl, rtol=1e-11, atol=1e-7)
    positions = {}
    slots = defaultdict(float)
    max_slots = 0
    start = pd.Timestamp(cfg["backtest"]["start_date"], tz="UTC")
    end = timeline.index[-1] + pd.Timedelta(minutes=1)
    previous_timestamp = start
    position_records = []
    fill_slots = []
    for timestamp, group in fills.groupby("timestamp", sort=False):
        slots[len(positions)] += (timestamp - previous_timestamp).total_seconds() / 60
        for row in group.itertuples(index=False):
            pside = "long" if row.type.endswith("_long") else "short"
            key = row.coin, pside
            if row.psize == 0:
                positions.pop(key, None)
            else:
                positions[key] = (row.psize, row.pprice)
            max_slots = max(max_slots, len(positions))
            fill_slots.append(len(positions))
        position_records.append({"timestamp": timestamp, "held_slots": len(positions)})
        previous_timestamp = timestamp
    slots[len(positions)] += (end - previous_timestamp).total_seconds() / 60
    base_wel = float(cfg["bot"]["long"]["risk"]["total_wallet_exposure_limit"]) / float(
        cfg["bot"]["long"]["risk"]["n_positions"]
    )
    effective_wel = base_wel * (
        1 + float(cfg["bot"]["long"]["risk"]["we_excess_allowance_pct"])
    )
    configured_slots = int(cfg["bot"]["long"]["risk"]["n_positions"])
    frozen = json.loads((ROOT / "frozen_inputs.json").read_text())
    settings = json.loads(
        (Path(frozen["dataset_relative_path"]) / "market_specific_settings.json").read_text()
    )
    multipliers = fills["coin"].map({coin: settings[coin]["c_mult"] for coin in fills["coin"].unique()})
    assert np.isfinite(multipliers).all() and (multipliers > 0).all()
    gross_turnover = float((fills["qty"].abs() * fills["price"] * multipliers).sum())
    excess = fills.assign(held_slots_after_fill=fill_slots)
    excess = excess.loc[
        (excess["held_slots_after_fill"] > configured_slots)
        | (excess["wallet_exposure"] > effective_wel + 1e-12)
        | (excess["twe_long"] > 1.5 + 1e-12)
    ].copy()
    excess.insert(0, "ledger_row", excess.index)
    excess.to_csv(ROOT / scenario / "constraint_events.csv", index=False)
    per_coin_kind = fills.assign(kind=fills["type"].str.split("_").str[0]).groupby(
        ["timestamp", "coin"]
    )["kind"].nunique()
    mixed = per_coin_kind[per_coin_kind > 1]
    closes = fills.loc[fills["type"].str.startswith("close_")]
    years = (end - start).total_seconds() / (365.25 * 86400)
    row = {
        "scenario": scenario,
        "starting_balance": initial,
        "final_balance": final_balance,
        "final_equity": final_equity,
        "balance_return": final_balance / initial - 1,
        "equity_return": final_equity / initial - 1,
        "equity_cagr": (final_equity / initial) ** (1 / years) - 1,
        "minute_close_max_drawdown": drawdown(timeline["equity"], initial),
        "native_drawdown_worst_usd": analysis["drawdown_worst_usd"],
        "native_gain_usd": analysis["gain_usd"],
        "net_realized_pnl": net_pnl,
        "final_unrealized_pnl": final_equity - final_balance,
        "signed_fees": float(fills["fee_paid"].sum()),
        "fills": len(fills),
        "entries": int(fills["type"].str.startswith("entry_").sum()),
        "closes": len(closes),
        "close_fill_win_rate_net": float(((closes["pnl"] + closes["fee_paid"]) > 0).mean()),
        "traded_coins": int(fills["coin"].nunique()),
        "multi_fill_minutes": int((fills.groupby("timestamp").size() > 1).sum()),
        "mixed_coin_minutes": len(mixed),
        "mixed_minutes": int(mixed.index.get_level_values(0).nunique()),
        "max_slots": max_slots,
        "ending_positions": len(positions),
        "slot_limit_excess_fill_rows": int((np.asarray(fill_slots) > configured_slots).sum()),
        "slot_limit_excess_label_minutes": sum(v for k, v in slots.items() if k > configured_slots),
        "full_slots_label_fraction": slots[configured_slots] / sum(slots.values()),
        "nominal_wel_long": base_wel,
        "effective_wel_long": effective_wel,
        "max_wel_long": float(fills["wallet_exposure"].max()),
        "wel_above_effective_rows": int((fills["wallet_exposure"] > effective_wel + 1e-12).sum()),
        "max_twel_long": float(fills["twe_long"].max()),
        "twel_above_limit_rows": int((fills["twe_long"] > 1.5 + 1e-12).sum()),
        "liquidated": bool(analysis["liquidated"]),
        "completion_ratio": analysis["backtest_completion_ratio"],
        "native_pnl_sharpe": analysis.get("sharpe_ratio_pnl"),
        "native_pnl_sortino": analysis.get("sortino_ratio_pnl"),
        "pnl_peak_recovery_days": analysis.get("peak_recovery_days_pnl"),
        "equity_peak_recovery_days": analysis.get("peak_recovery_days_equity_usd"),
        "position_held_days_max": analysis.get("position_held_days_max"),
        "high_exposure_days_max_long": analysis.get("high_exposure_days_max_long"),
        "data_end_boundary": end.isoformat(),
        "gross_realized_pnl": float(fills["pnl"].sum()),
        "gross_traded_notional_usdt": gross_turnover,
        "maker_fills": int((fills["liquidity"] == "maker").sum()),
        "taker_fills": int((fills["liquidity"] == "taker").sum()),
        **drawdown_episode(timeline, initial, start),
    }
    coin_rows = []
    for coin, group in fills.groupby("coin", sort=True):
        coin_rows.append({
            "scenario": scenario, "coin": coin, "fills": len(group),
            "realized_pnl": float(group["pnl"].sum()),
            "signed_fees": float(group["fee_paid"].sum()),
            "net_realized_pnl": float((group["pnl"] + group["fee_paid"]).sum()),
            "max_recorded_wel": float(group["wallet_exposure"].max()),
        })
    pd.DataFrame(position_records).to_csv(ROOT / scenario / "position_occupancy.csv", index=False)
    pd.DataFrame([
        {"scenario": scenario, "slots": key, "minutes": value,
         "fraction": value / sum(slots.values())}
        for key, value in sorted(slots.items())
    ]).to_csv(ROOT / scenario / "slot_occupancy.csv", index=False)
    return row, periods(scenario, timeline, fills, "%Y", initial), periods(
        scenario, timeline, fills, "%Y-%m", initial
    ), coin_rows, pd.DataFrame({
        "equity": timeline["equity"].resample("D").last(),
        "drawdown": (
            timeline["equity"] / timeline["equity"].cummax().clip(lower=initial) - 1
        ).resample("D").min(),
    })


def execution_audit_metrics(scenario):
    audit_path = ROOT / scenario / "execution_boundary_audit.csv"
    if not audit_path.exists():
        return None, None
    audit = pd.read_csv(audit_path)
    fills = pd.read_csv(result_dir(scenario) / "fills.csv")
    if len(audit) != len(fills):
        raise AssertionError(
            f"{scenario}: audit rows {len(audit)} != fill rows {len(fills)}"
        )
    fill_timestamp_ms = (
        pd.to_datetime(fills["timestamp"], utc=True).astype("int64") // 1_000_000
    ).to_numpy()
    checks = {
        "fill_index": audit["fill_index"].to_numpy() == fills["index"].to_numpy(),
        "fill_timestamp": audit["fill_candle_open_timestamp_ms"].to_numpy()
        == fill_timestamp_ms,
        "symbol": audit["symbol"].to_numpy() == fills["coin"].to_numpy(),
        "pside": audit["pside"].to_numpy()
        == fills["type"].str.rsplit("_", n=1).str[-1].to_numpy(),
        "order_type": audit["order_type"].to_numpy() == fills["type"].to_numpy(),
        "fill_qty": np.isclose(
            audit["fill_qty"].to_numpy(), fills["qty"].to_numpy(), rtol=0.0, atol=1e-12
        ),
        "fill_price": np.isclose(
            audit["fill_price"].to_numpy(), fills["price"].to_numpy(), rtol=0.0, atol=1e-12
        ),
    }
    failed = [name for name, values in checks.items() if not np.all(values)]
    if failed:
        raise AssertionError(f"{scenario}: execution audit mismatch: {failed}")
    audit.insert(0, "scenario", scenario)
    audit["activation_delay_bars"] = audit["activation_index"] - audit["decision_index"]
    audit["resting_age_bars"] = audit["fill_index"] - audit["activation_index"]
    audit["activation_after_decision_close_ms"] = (
        audit["activation_timestamp_ms"] - audit["decision_close_timestamp_ms"]
    )
    audit["fill_interval_ms"] = (
        audit["fill_candle_close_timestamp_ms"] - audit["fill_candle_open_timestamp_ms"]
    )
    identity = json.loads((ROOT / scenario / "run_identity.json").read_text())
    assert (audit["activation_delay_bars"] == identity["contract"]["delay"] + 1).all()
    assert (audit["activation_after_decision_close_ms"] == identity["contract"]["delay"] * 60_000).all()
    assert (audit["resting_age_bars"] >= 0).all()
    assert (audit["fill_interval_ms"] == 60_000).all()
    assert not audit["order_id"].duplicated().any()
    summary = {
        "scenario": scenario,
        "audit_rows": len(audit),
        "unique_order_ids": audit["order_id"].nunique(),
        "duplicate_order_id_rows": len(audit) - audit["order_id"].nunique(),
        "min_activation_delay_bars": audit["activation_delay_bars"].min(),
        "max_activation_delay_bars": audit["activation_delay_bars"].max(),
        "min_activation_after_decision_close_ms": audit[
            "activation_after_decision_close_ms"
        ].min(),
        "max_activation_after_decision_close_ms": audit[
            "activation_after_decision_close_ms"
        ].max(),
        "preactivation_fill_rows": int((audit["resting_age_bars"] < 0).sum()),
        "fills_on_activation": int((audit["resting_age_bars"] == 0).sum()),
        "fills_after_activation": int((audit["resting_age_bars"] > 0).sum()),
        "max_resting_age_bars": audit["resting_age_bars"].max(),
        "invalid_fill_interval_rows": int((audit["fill_interval_ms"] != 60_000).sum()),
    }
    return audit, summary


def report_data():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    summaries, annual, monthly, coins, audit_rows, audit_summaries = [], [], [], [], [], []
    fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True)
    scenarios = [
        scenario
        for scenario in ["B0", "C1", "C2", "C3", "C4"]
        if (ROOT / scenario / "run_identity.json").exists()
    ]
    if not scenarios or scenarios[0] != "B0":
        raise FileNotFoundError("A completed B0 run is required before generating report data.")
    for scenario in scenarios:
        summary, a, m, c, daily = analyze(scenario)
        summaries.append(summary)
        annual.extend(a)
        monthly.extend(m)
        coins.extend(c)
        audit, audit_summary = execution_audit_metrics(scenario)
        if audit is not None:
            audit_rows.append(audit)
            audit_summaries.append(audit_summary)
        axes[0].plot(daily.index, daily["equity"] / summary["starting_balance"], label=scenario)
        axes[1].plot(daily.index, 100 * daily["drawdown"], label=scenario)
    for ax in axes:
        ax.legend()
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("Equity / starting capital (daily display)")
    axes[1].set_ylabel("Worst minute-close drawdown each day (%)")
    fig.tight_layout()
    fig.savefig(ROOT / "scenario_equity_comparison.png", dpi=160)
    plt.close(fig)
    summary_frame = pd.DataFrame(summaries)
    baseline = summary_frame.loc[summary_frame["scenario"] == "B0"].iloc[0]
    delta_columns = (
        "final_balance", "final_equity", "net_realized_pnl", "signed_fees",
        "fills", "entries", "closes", "minute_close_max_drawdown",
        "max_slots", "max_wel_long", "max_twel_long",
    )
    for column in delta_columns:
        summary_frame[f"{column}_vs_B0"] = summary_frame[column] - baseline[column]
        if float(baseline[column]) != 0.0:
            summary_frame[f"{column}_relative_vs_B0"] = (
                summary_frame[column] / baseline[column] - 1.0
            )
    for name, rows in [
        ("scenario_comparison", summary_frame), ("annual_metrics", annual),
        ("monthly_metrics", monthly), ("coin_metrics", coins),
    ]:
        frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
        frame.to_csv(ROOT / f"{name}.csv", index=False)
    if audit_rows:
        pd.concat(audit_rows, ignore_index=True).to_csv(
            ROOT / "execution_boundary_audit.csv", index=False
        )
        pd.DataFrame(audit_summaries).to_csv(
            ROOT / "execution_boundary_summary.csv", index=False
        )
    print(summary_frame.to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["baseline-check", "report-data"])
    args = parser.parse_args()
    baseline_check() if args.action == "baseline-check" else report_data()
