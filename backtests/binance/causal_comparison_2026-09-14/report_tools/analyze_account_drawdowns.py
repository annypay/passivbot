"""Generate a reproducible account-drawdown report from frozen local artifacts.

This is a reporting tool only.  It does not execute a bot, fetch data, alter the
backtest engine, or overwrite any replay artifact.
"""

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path("backtests/binance/causal_comparison_2026-09-14")
SCENARIOS = ("B0", "C1", "C2", "C3", "C4")
LOW_INDEX = 1
CLOSE_INDEX = 2
EPSILON = 1e-7
CLOSE_RECONCILIATION_TOLERANCE_USDT = 0.25
BALANCE_RECONCILIATION_TOLERANCE_USDT = 0.10


def scenario_result_dir(scenario):
    identity = json.loads((ROOT / scenario / "run_identity.json").read_text())
    return Path(identity["result_dir"])


def utc_index(values):
    return pd.to_datetime(values, utc=True)


def load_scenario(scenario):
    directory = scenario_result_dir(scenario)
    config = json.loads((directory / "config.json").read_text())
    initial = float(config["backtest"]["starting_balance"])
    with gzip.open(directory / "balance_and_equity.csv.gz", "rt") as handle:
        timeline = pd.read_csv(handle, index_col=0)
    timeline.index = utc_index(timeline.index)
    timeline.index.name = "timestamp"
    timeline = timeline.rename(
        columns={
            "usd_total_balance": "balance",
            "usd_total_equity": "equity",
            "strategy_equity": "strategy_equity",
        }
    )
    timeline = timeline.loc[:, ["balance", "equity", "strategy_equity"]]
    if timeline.empty:
        raise ValueError(f"{scenario}: balance/equity timeline is empty")
    if not timeline.index.is_monotonic_increasing or timeline.index.has_duplicates:
        raise ValueError(f"{scenario}: timeline timestamps are not unique and increasing")
    if not (timeline.index.to_series().diff().dropna() == pd.Timedelta(minutes=1)).all():
        raise ValueError(f"{scenario}: timeline is not contiguous at one-minute resolution")
    if not np.allclose(
        timeline["equity"], timeline["strategy_equity"], rtol=0.0, atol=EPSILON
    ):
        raise ValueError(f"{scenario}: strategy equity differs from USD total equity")

    fills = pd.read_csv(directory / "fills.csv")
    fills = fills.loc[:, ~fills.columns.str.startswith("Unnamed:")]
    fills["timestamp"] = utc_index(fills["timestamp"])
    fills = fills.sort_values(["timestamp", "index"], kind="stable").reset_index(drop=True)
    if fills["timestamp"].lt(timeline.index[0]).any():
        raise ValueError(f"{scenario}: ledger includes a fill before the saved timeline")
    return config, initial, timeline, fills


def drawdown_details(values, labels):
    """Return the maximum peak-to-trough drawdown for a non-empty series."""

    values = np.asarray(values, dtype=np.float64)
    if len(values) != len(labels) or not len(values):
        raise ValueError("drawdown input must have equally sized, non-empty values and labels")
    if not np.isfinite(values).all() or (values <= 0.0).any():
        raise ValueError("drawdown requires finite positive values")
    high_water = np.maximum.accumulate(values)
    drawdowns = 1.0 - values / high_water
    trough_offset = int(np.argmax(drawdowns))
    peak_value = high_water[trough_offset]
    peak_offset = int(
        np.flatnonzero(values[: trough_offset + 1] == peak_value)[-1]
    )
    return {
        "mdd_pct": float(drawdowns[trough_offset]),
        "peak": float(peak_value),
        "peak_time": labels[peak_offset],
        "trough": float(values[trough_offset]),
        "trough_time": labels[trough_offset],
    }


def global_high_water(values, initial, first_label):
    all_values = np.r_[initial, values.to_numpy(dtype=np.float64)]
    labels = pd.DatetimeIndex([first_label - pd.Timedelta(minutes=1)]).append(
        values.index
    )
    high_water = np.maximum.accumulate(all_values)
    new_high = all_values >= high_water
    high_labels = pd.Series(
        pd.NaT, index=np.arange(len(all_values)), dtype="datetime64[ns, UTC]"
    )
    high_labels.loc[new_high] = labels[new_high]
    high_labels = high_labels.ffill().to_numpy()
    return pd.DataFrame(
        {
            "global_hwm_equity": high_water[1:],
            "global_equity_drawdown_pct": 1.0 - all_values[1:] / high_water[1:],
            "global_hwm_time": high_labels[1:],
        },
        index=values.index,
    )


def local_monthly_rows(scenario, initial, timeline):
    """Calculate wallet and marked-equity risk with an opening state per month."""

    global_equity = global_high_water(timeline["equity"], initial, timeline.index[0])
    previous_balance = initial
    previous_equity = initial
    rows = []
    months = timeline.index.tz_localize(None).to_period("M")
    for period, group in timeline.groupby(months, sort=True):
        opening_time = group.index[0] - pd.Timedelta(minutes=1)
        labels = pd.DatetimeIndex([opening_time]).append(group.index)
        balance_values = np.r_[previous_balance, group["balance"].to_numpy()]
        equity_values = np.r_[previous_equity, group["equity"].to_numpy()]
        balance_dd = drawdown_details(balance_values, labels)
        equity_dd = drawdown_details(equity_values, labels)
        floating = group["equity"] - group["balance"]
        floating_loss = group["balance"] - group["equity"]
        floating_loss_pct = floating_loss / group["balance"]
        global_worst_time = global_equity["global_equity_drawdown_pct"].loc[
            group.index
        ].idxmax()
        rows.append(
            {
                "scenario": scenario,
                "month": str(period),
                "first_candle": group.index[0].isoformat(),
                "last_candle": group.index[-1].isoformat(),
                "opening_balance": previous_balance,
                "closing_balance": float(group["balance"].iloc[-1]),
                "realized_balance_peak": balance_dd["peak"],
                "realized_balance_trough": balance_dd["trough"],
                "realized_balance_mdd_pct": balance_dd["mdd_pct"],
                "realized_balance_mdd_peak_time": balance_dd["peak_time"].isoformat(),
                "realized_balance_mdd_trough_time": balance_dd["trough_time"].isoformat(),
                "opening_equity": previous_equity,
                "closing_equity": float(group["equity"].iloc[-1]),
                "minute_close_equity_peak": equity_dd["peak"],
                "minute_close_equity_trough": equity_dd["trough"],
                "minute_close_equity_mdd_pct": equity_dd["mdd_pct"],
                "minute_close_equity_mdd_peak_time": equity_dd["peak_time"].isoformat(),
                "minute_close_equity_mdd_trough_time": equity_dd["trough_time"].isoformat(),
                "worst_global_equity_drawdown_pct": float(
                    global_equity.loc[global_worst_time, "global_equity_drawdown_pct"]
                ),
                "global_hwm_equity_at_month_worst": float(
                    global_equity.loc[global_worst_time, "global_hwm_equity"]
                ),
                "global_hwm_time_at_month_worst": global_equity.loc[
                    global_worst_time, "global_hwm_time"
                ].isoformat(),
                "global_hwm_drawdown_trough_time": global_worst_time.isoformat(),
                "max_unrealized_loss_usdt": float(floating_loss.max()),
                "max_unrealized_loss_pct_of_balance": float(floating_loss_pct.max()),
                "max_unrealized_loss_time": floating_loss.idxmax().isoformat(),
                "max_unrealized_profit_usdt": float(floating.max()),
                "min_equity_to_balance_pct": float((group["equity"] / group["balance"]).min()),
            }
        )
        previous_balance = float(group["balance"].iloc[-1])
        previous_equity = float(group["equity"].iloc[-1])
    return pd.DataFrame(rows), global_equity


def fill_aggregates(fills, frequency):
    labels = fills["timestamp"].dt.tz_localize(None).dt.to_period(frequency).astype(str)
    kind = np.where(fills["type"].str.startswith("entry_"), "entry", "close")
    copy = fills.assign(
        period=labels,
        kind=kind,
        net_realized_pnl=fills["pnl"] + fills["fee_paid"],
    )
    grouped = copy.groupby("period", sort=True)
    totals = grouped.agg(
        fills=("coin", "size"),
        gross_realized_pnl=("pnl", "sum"),
        signed_fees=("fee_paid", "sum"),
        net_realized_pnl=("net_realized_pnl", "sum"),
    )
    totals["entries"] = copy.loc[copy["kind"] == "entry"].groupby("period").size()
    totals["closes"] = copy.loc[copy["kind"] == "close"].groupby("period").size()
    return totals.fillna(0)


def account_summary(scenario, initial, timeline, global_equity):
    labels = pd.DatetimeIndex([timeline.index[0] - pd.Timedelta(minutes=1)]).append(
        timeline.index
    )
    balance_values = np.r_[initial, timeline["balance"].to_numpy()]
    equity_values = np.r_[initial, timeline["equity"].to_numpy()]
    balance_dd = drawdown_details(balance_values, labels)
    equity_dd = drawdown_details(equity_values, labels)
    floating_loss = timeline["balance"] - timeline["equity"]
    floating_loss_time = floating_loss.idxmax()
    recovery_rows = timeline.loc[equity_dd["trough_time"] :]
    recovery = recovery_rows.loc[recovery_rows["equity"] >= equity_dd["peak"]]
    return {
        "scenario": scenario,
        "starting_balance": initial,
        "ending_balance": float(timeline["balance"].iloc[-1]),
        "ending_equity": float(timeline["equity"].iloc[-1]),
        "realized_balance_mdd_pct": balance_dd["mdd_pct"],
        "realized_balance_mdd_peak": balance_dd["peak"],
        "realized_balance_mdd_peak_time": balance_dd["peak_time"].isoformat(),
        "realized_balance_mdd_trough": balance_dd["trough"],
        "realized_balance_mdd_trough_time": balance_dd["trough_time"].isoformat(),
        "minute_close_equity_mdd_pct": equity_dd["mdd_pct"],
        "minute_close_equity_mdd_peak": equity_dd["peak"],
        "minute_close_equity_mdd_peak_time": equity_dd["peak_time"].isoformat(),
        "balance_at_equity_mdd_peak": float(
            timeline.loc[equity_dd["peak_time"], "balance"]
        ),
        "minute_close_equity_mdd_trough": equity_dd["trough"],
        "minute_close_equity_mdd_trough_time": equity_dd["trough_time"].isoformat(),
        "balance_at_equity_mdd_trough": float(
            timeline.loc[equity_dd["trough_time"], "balance"]
        ),
        "minute_close_equity_recovery_time": (
            recovery.index[0].isoformat() if not recovery.empty else None
        ),
        "minute_close_equity_peak_to_recovery_days": (
            (recovery.index[0] - equity_dd["peak_time"]).total_seconds() / 86400
            if not recovery.empty
            else None
        ),
        "max_unrealized_loss_usdt": float(floating_loss.max()),
        "max_unrealized_loss_pct_of_balance": float(
            (floating_loss / timeline["balance"]).max()
        ),
        "max_unrealized_loss_time": floating_loss_time.isoformat(),
        "global_equity_drawdown_last_pct": float(
            global_equity["global_equity_drawdown_pct"].iloc[-1]
        ),
    }


def load_market_data():
    frozen = json.loads((ROOT / "frozen_inputs.json").read_text())
    directory = Path(frozen["dataset_relative_path"])
    with gzip.open(directory / "timestamps.npy.gz", "rb") as handle:
        timestamps = np.load(handle, allow_pickle=False)
    with gzip.open(directory / "hlcvs.npy.gz", "rb") as handle:
        hlcvs = np.load(handle, allow_pickle=False)
    coins = json.loads((directory / "coins.json").read_text())
    settings = json.loads((directory / "market_specific_settings.json").read_text())
    if hlcvs.shape[:2] != (len(timestamps), len(coins)) or hlcvs.shape[2] != 4:
        raise ValueError("frozen market data has an unexpected H/L/C/V shape")
    if not np.all(np.diff(timestamps) == 60_000):
        raise ValueError("frozen market-data timestamps are not contiguous at one minute")
    return timestamps, hlcvs, coins, settings


def position_snapshot(fills, coins, settings, timestamps, hlcvs, at_time):
    """Reconstruct end-of-candle positions using the complete fill ledger through at_time."""

    snapshot = fills.loc[fills["timestamp"] <= at_time]
    positions = {}
    balance = None
    for row in snapshot.itertuples(index=False):
        if row.psize == 0.0:
            positions.pop(row.coin, None)
        else:
            positions[row.coin] = (float(row.psize), float(row.pprice))
        balance = float(row.usd_total_balance)
    if balance is None:
        raise ValueError(f"no fill ledger balance exists at {at_time.isoformat()}")
    time_ms = int(at_time.value // 1_000_000)
    candle_index = int(np.searchsorted(timestamps, time_ms))
    if candle_index == len(timestamps) or int(timestamps[candle_index]) != time_ms:
        raise ValueError(f"cannot map {at_time.isoformat()} to frozen candle data")
    coin_index = {coin: index for index, coin in enumerate(coins)}
    rows = []
    for coin, (psize, pprice) in sorted(positions.items()):
        index = coin_index[coin]
        price = float(hlcvs[candle_index, index, CLOSE_INDEX])
        multiplier = float(settings[coin]["c_mult"])
        notional = abs(psize * price * multiplier)
        cost_basis_notional = abs(psize * pprice * multiplier)
        unrealized = psize * multiplier * (price - pprice)
        rows.append(
            {
                "coin": coin,
                "psize": psize,
                "pprice": pprice,
                "close_mark_price": price,
                "close_mark_notional_usdt": notional,
                "cost_basis_notional_usdt": cost_basis_notional,
                "unrealized_pnl_usdt": unrealized,
                "wallet_exposure_at_close": notional / balance,
                "cost_basis_wallet_exposure": cost_basis_notional / balance,
            }
        )
    frame = pd.DataFrame(rows)
    aggregate = {
        "timestamp": at_time.isoformat(),
        "balance": balance,
        "active_positions": len(frame),
        "close_mark_gross_notional_usdt": float(frame["close_mark_notional_usdt"].sum()),
        "close_mark_total_wallet_exposure": float(frame["wallet_exposure_at_close"].sum()),
        "cost_basis_gross_notional_usdt": float(frame["cost_basis_notional_usdt"].sum()),
        "cost_basis_total_wallet_exposure": float(
            frame["cost_basis_wallet_exposure"].sum()
        ),
        "reconstructed_unrealized_pnl_usdt": float(frame["unrealized_pnl_usdt"].sum()),
    }
    return frame, aggregate


def calculate_candle_low_envelope(initial, timeline, fills, timestamps, hlcvs, coins, settings):
    """Mark each known start/post-fill state to the concurrent per-symbol candle low.

    It deliberately does not infer a sequence for the lows and fills inside a one-minute
    candle.  Taking the minimum over states therefore produces an adverse stress proxy,
    not a reconstructable exchange equity path or a strict real-world bound.
    """

    first_ms = int(timeline.index[0].value // 1_000_000)
    last_ms = int(timeline.index[-1].value // 1_000_000)
    first_index = int(np.searchsorted(timestamps, first_ms))
    last_index = int(np.searchsorted(timestamps, last_ms))
    expected = timestamps[first_index : last_index + 1]
    observed = timeline.index.astype("int64").to_numpy() // 1_000_000
    if len(expected) != len(timeline) or not np.array_equal(expected, observed):
        raise ValueError("saved B0 timeline does not map one-to-one onto frozen candle data")

    coin_index = {coin: index for index, coin in enumerate(coins)}
    multipliers = np.array([float(settings[coin]["c_mult"]) for coin in coins])
    sizes = np.zeros(len(coins), dtype=np.float64)
    average_prices = np.zeros(len(coins), dtype=np.float64)
    low_equity = np.empty(len(timeline), dtype=np.float64)
    close_reconstructed = np.empty(len(timeline), dtype=np.float64)
    fill_groups = {
        int(time.value // 1_000_000): group
        for time, group in fills.groupby("timestamp", sort=False)
    }
    balance = initial
    cursor = first_index
    best_state = None

    def mark(prices):
        active = sizes != 0.0
        if not active.any():
            return balance
        active_prices = prices[active]
        if not np.isfinite(active_prices).all() or (active_prices <= 0.0).any():
            raise ValueError("active position has an invalid frozen mark price")
        signed_multiplier_size = sizes[active] * multipliers[active]
        return balance + float(
            np.dot(signed_multiplier_size, active_prices - average_prices[active])
        )

    def fill_span(end_index):
        if end_index <= cursor:
            return
        active = sizes != 0.0
        relative = slice(cursor - first_index, end_index - first_index)
        if not active.any():
            low_equity[relative] = balance
            close_reconstructed[relative] = balance
            return
        signed_multiplier_size = sizes[active] * multipliers[active]
        low_prices = hlcvs[cursor:end_index, active, LOW_INDEX]
        close_prices = hlcvs[cursor:end_index, active, CLOSE_INDEX]
        if (
            not np.isfinite(low_prices).all()
            or not np.isfinite(close_prices).all()
            or (low_prices <= 0.0).any()
            or (close_prices <= 0.0).any()
        ):
            raise ValueError("active position has an invalid frozen H/L/C price")
        cost = float(np.dot(signed_multiplier_size, average_prices[active]))
        low_equity[relative] = balance + low_prices @ signed_multiplier_size - cost
        close_reconstructed[relative] = (
            balance + close_prices @ signed_multiplier_size - cost
        )

    for candle_index in range(first_index, last_index + 1):
        candle_ms = int(timestamps[candle_index])
        group = fill_groups.get(candle_ms)
        if group is None:
            continue
        fill_span(candle_index)
        local_index = candle_index - first_index
        candidate = mark(hlcvs[candle_index, :, LOW_INDEX])
        chosen = candidate
        chosen_state = "minute_start"
        for order, row in enumerate(group.itertuples(index=False), start=1):
            index = coin_index[row.coin]
            sizes[index] = float(row.psize)
            average_prices[index] = float(row.pprice) if row.psize != 0.0 else 0.0
            balance = float(row.usd_total_balance)
            candidate = mark(hlcvs[candle_index, :, LOW_INDEX])
            if candidate < chosen:
                chosen = candidate
                chosen_state = f"after_fill_{order}"
        low_equity[local_index] = chosen
        close_reconstructed[local_index] = mark(hlcvs[candle_index, :, CLOSE_INDEX])
        if best_state is None or chosen < best_state["low_equity"]:
            best_state = {
                "timestamp": timeline.index[local_index],
                "low_equity": chosen,
                "state": chosen_state,
                "balance": balance,
                "sizes": sizes.copy(),
                "average_prices": average_prices.copy(),
            }
        cursor = candle_index + 1
    fill_span(last_index + 1)

    max_close_residual = float(
        np.max(np.abs(close_reconstructed - timeline["equity"].to_numpy()))
    )
    # The serialized balance/equity and fill paths can accumulate small
    # per-component rounding/aggregation differences.  The threshold detects a
    # meaningful reconciliation failure without rejecting that display-level drift.
    if max_close_residual > CLOSE_RECONCILIATION_TOLERANCE_USDT:
        raise ValueError(
            "position-ledger close reconstruction does not reconcile with saved B0 equity: "
            f"{max_close_residual}"
        )
    prior_close_high_water = np.maximum.accumulate(
        np.r_[initial, timeline["equity"].to_numpy(dtype=np.float64)[:-1]]
    )
    proxy_drawdown = 1.0 - low_equity / prior_close_high_water
    return pd.DataFrame(
        {
            "candle_low_state_envelope_equity": low_equity,
            "candle_low_state_envelope_drawdown_pct": proxy_drawdown,
        },
        index=timeline.index,
    ), best_state, max_close_residual


def low_stress_monthly(timeline, low_stress, initial):
    rows = []
    prior_equity = initial
    months = timeline.index.tz_localize(None).to_period("M")
    for period, group in timeline.groupby(months, sort=True):
        stress = low_stress.loc[group.index]
        running_hwm = np.maximum.accumulate(
            np.r_[prior_equity, group["equity"].to_numpy(dtype=np.float64)[:-1]]
        )
        local_proxy = 1.0 - stress["candle_low_state_envelope_equity"].to_numpy() / running_hwm
        worst_offset = int(np.argmax(local_proxy))
        global_worst_offset = int(
            np.argmax(stress["candle_low_state_envelope_drawdown_pct"].to_numpy())
        )
        rows.append(
            {
                "month": str(period),
                "lowest_candle_low_state_envelope_equity": float(
                    stress["candle_low_state_envelope_equity"].min()
                ),
                "worst_local_candle_low_state_envelope_drawdown_pct": float(
                    local_proxy[worst_offset]
                ),
                "worst_local_candle_low_state_envelope_time": group.index[
                    worst_offset
                ].isoformat(),
                "worst_global_candle_low_state_envelope_drawdown_pct": float(
                    stress["candle_low_state_envelope_drawdown_pct"].iloc[
                        global_worst_offset
                    ]
                ),
                "worst_global_candle_low_state_envelope_time": group.index[
                    global_worst_offset
                ].isoformat(),
            }
        )
        prior_equity = float(group["equity"].iloc[-1])
    return pd.DataFrame(rows)


def hourly_event_rows(timeline, fills, low_stress):
    day_start = pd.Timestamp("2025-10-10", tz="UTC")
    day_end = day_start + pd.Timedelta(days=1)
    day = timeline.loc[(timeline.index >= day_start) & (timeline.index < day_end)]
    event_fills = fills.loc[
        (fills["timestamp"] >= day_start) & (fills["timestamp"] < day_end)
    ].copy()
    event_fills["hour"] = event_fills["timestamp"].dt.floor("h")
    event_fills["kind"] = np.where(
        event_fills["type"].str.startswith("entry_"), "entry", "close"
    )
    opening_balance = float(timeline.loc[day_start - pd.Timedelta(minutes=1), "balance"])
    opening_equity = float(timeline.loc[day_start - pd.Timedelta(minutes=1), "equity"])
    previous_balance = opening_balance
    previous_equity = opening_equity
    rows = []
    for hour, group in day.groupby(day.index.floor("h"), sort=True):
        selected = event_fills.loc[event_fills["hour"] == hour]
        labels = pd.DatetimeIndex([hour - pd.Timedelta(minutes=1)]).append(group.index)
        equity_dd = drawdown_details(
            np.r_[previous_equity, group["equity"].to_numpy()], labels
        )
        rows.append(
            {
                "hour_utc": hour.isoformat(),
                "opening_balance": previous_balance,
                "closing_balance": float(group["balance"].iloc[-1]),
                "opening_equity": previous_equity,
                "closing_equity": float(group["equity"].iloc[-1]),
                "minimum_minute_close_equity": float(group["equity"].min()),
                "maximum_minute_close_equity": float(group["equity"].max()),
                "hour_minute_close_equity_mdd_pct": equity_dd["mdd_pct"],
                "max_unrealized_loss_usdt": float(
                    (group["balance"] - group["equity"]).max()
                ),
                "min_candle_low_state_envelope_equity": float(
                    low_stress.loc[group.index, "candle_low_state_envelope_equity"].min()
                ),
                "fills": len(selected),
                "entries": int((selected["kind"] == "entry").sum()),
                "closes": int((selected["kind"] == "close").sum()),
                "gross_realized_pnl": float(selected["pnl"].sum()),
                "signed_fees": float(selected["fee_paid"].sum()),
                "net_realized_pnl": float(
                    (selected["pnl"] + selected["fee_paid"]).sum()
                ),
            }
        )
        previous_balance = float(group["balance"].iloc[-1])
        previous_equity = float(group["equity"].iloc[-1])
    return pd.DataFrame(rows), event_fills


def format_usdt(value):
    return f"{value:,.2f}"


def format_pct(value):
    return f"{100.0 * value:.2f}%"


def markdown_table(frame, columns, headings):
    header = "| " + " | ".join(headings) + " |"
    divider = "| " + " | ".join("---" for _ in headings) + " |"
    rows = [header, divider]
    for row in frame.loc[:, columns].itertuples(index=False, name=None):
        rows.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(rows)


def render_report(
    summary,
    scenario_summaries,
    monthly_b0,
    hourly_event,
    fills_event,
    low_stress_summary,
    peak_positions,
    trough_positions,
    oct_positions,
    close_residual,
):
    b0 = summary.loc[summary["scenario"] == "B0"].iloc[0]
    event = hourly_event.loc[hourly_event["hour_utc"] == "2025-10-10T21:00:00+00:00"].iloc[0]
    event_fills = fills_event
    event_net = float((event_fills["pnl"] + event_fills["fee_paid"]).sum())
    event_gross = float(event_fills["pnl"].sum())
    event_fees = float(event_fills["fee_paid"].sum())
    day_open_equity = float(hourly_event["opening_equity"].iloc[0])
    day_close_equity = float(hourly_event["closing_equity"].iloc[-1])
    day_low = float(hourly_event["minimum_minute_close_equity"].min())
    day_peak = float(hourly_event["maximum_minute_close_equity"].max())
    monthly_display = monthly_b0.copy()
    monthly_display["balance_mdd"] = monthly_display["realized_balance_mdd_pct"].map(format_pct)
    monthly_display["close_equity_mdd"] = monthly_display[
        "minute_close_equity_mdd_pct"
    ].map(format_pct)
    monthly_display["global_close_equity_dd"] = monthly_display[
        "worst_global_equity_drawdown_pct"
    ].map(format_pct)
    monthly_display["max_floating_loss"] = monthly_display[
        "max_unrealized_loss_pct_of_balance"
    ].map(format_pct)
    monthly_display["low_state_proxy"] = monthly_display[
        "worst_global_candle_low_state_envelope_drawdown_pct"
    ].map(format_pct)
    monthly_display["end_balance_equity"] = monthly_display.apply(
        lambda row: f"{format_usdt(row['closing_balance'])} / {format_usdt(row['closing_equity'])}",
        axis=1,
    )
    scenario_display = scenario_summaries.copy()
    scenario_display["ending_equity"] = scenario_display["ending_equity"].map(format_usdt)
    scenario_display["balance_mdd"] = scenario_display["realized_balance_mdd_pct"].map(
        format_pct
    )
    scenario_display["close_equity_mdd"] = scenario_display[
        "minute_close_equity_mdd_pct"
    ].map(format_pct)
    scenario_display["max_float_loss"] = scenario_display[
        "max_unrealized_loss_pct_of_balance"
    ].map(format_pct)
    hourly_display = hourly_event.copy()
    hourly_display["min_close_equity"] = hourly_display[
        "minimum_minute_close_equity"
    ].map(format_usdt)
    hourly_display["low_state_proxy_equity"] = hourly_display[
        "min_candle_low_state_envelope_equity"
    ].map(format_usdt)
    hourly_display["net_pnl"] = hourly_display["net_realized_pnl"].map(format_usdt)
    portfolio_tables = []
    for title, frame in [
        ("2025-02-01 01:37 UTC：全程权益峰值时的仓位", peak_positions),
        ("2025-04-07 06:54 UTC：全程分钟收盘权益谷底时的仓位", trough_positions),
        ("2025-10-10 21:20 UTC：急跌分钟收盘谷底时的仓位", oct_positions),
    ]:
        display = frame.copy()
        if display.empty:
            rendered = "_无持仓_"
        else:
            for column in (
                "psize",
                "pprice",
                "close_mark_price",
                "close_mark_notional_usdt",
                "cost_basis_notional_usdt",
                "unrealized_pnl_usdt",
                "wallet_exposure_at_close",
                "cost_basis_wallet_exposure",
            ):
                if column in display:
                    display[column] = display[column].map(
                        format_pct
                        if column in (
                            "wallet_exposure_at_close",
                            "cost_basis_wallet_exposure",
                        )
                        else format_usdt
                    )
            aggregate = frame.attrs["aggregate"]
            totals = (
                f"合计：{aggregate['active_positions']} 个持仓；收盘盯市名义额 "
                f"{format_usdt(aggregate['close_mark_gross_notional_usdt'])} USDT "
                f"（{format_pct(aggregate['close_mark_total_wallet_exposure'])} 余额）；"
                f"按建仓均价的名义额 "
                f"{format_usdt(aggregate['cost_basis_gross_notional_usdt'])} USDT "
                f"（{format_pct(aggregate['cost_basis_total_wallet_exposure'])} 余额）。"
            )
            rendered = markdown_table(
                display,
                [
                    "coin",
                    "psize",
                    "pprice",
                    "close_mark_price",
                    "close_mark_notional_usdt",
                    "cost_basis_notional_usdt",
                    "unrealized_pnl_usdt",
                    "wallet_exposure_at_close",
                    "cost_basis_wallet_exposure",
                ],
                [
                    "币种",
                    "仓位数量",
                    "均价",
                    "收盘盯市价",
                    "盯市名义额",
                    "成本名义额",
                    "浮动 PnL",
                    "相对余额敞口",
                    "成本敞口",
                ],
            )
            rendered = totals + "\n\n" + rendered
        portfolio_tables.append(f"### {title}\n\n{rendered}")
    portfolio_sections = "\n\n".join(portfolio_tables)

    return f"""# 账户回撤深度分析：余额、分钟收盘权益与蜡烛低点压力

## 结论先行

你看到的“余额曲线持续上升”和“最大回撤约 70%”**并不矛盾**，因为它们不是同一条资金序列：

1. **已实现余额（`usd_total_balance`）**只在平仓和手续费发生时变动；未平 long 仓的浮盈亏不写入这里。B0 的全程已实现余额最大回撤只有 **{format_pct(b0['realized_balance_mdd_pct'])}**。
2. **分钟收盘账户权益（`usd_total_equity` / `strategy_equity`）**是余额加上所有未平仓按该分钟 `Close` 盯市的浮盈亏。B0 的全程峰谷最大回撤为 **{format_pct(b0['minute_close_equity_mdd_pct'])}**，这正是原 `analysis.json` 的 `drawdown_worst_usd` 口径。
3. 因此，风险判断应以第二项为主，而不是以“余额没有怎么跌”为依据。在 2025-04 的**权益谷底当刻**，余额仍有 {format_usdt(b0['balance_at_equity_mdd_trough'])} USDT，但按分钟收盘盯市的权益只有 **{format_usdt(b0['minute_close_equity_mdd_trough'])} USDT**；两者相差 **{format_usdt(b0['max_unrealized_loss_usdt'])} USDT** 的未实现亏损。

该 73.69% 不表示“从初始 100,000 USDT 跌了 73.69%”。它表示账户曾先达到
{format_usdt(b0['minute_close_equity_mdd_peak'])} USDT 的权益峰值，随后跌到
{format_usdt(b0['minute_close_equity_mdd_trough'])} USDT；此时相对**历史最高权益**的
回撤为 73.69%。谷底仍略高于初始本金，但从历史峰值来看风险极深。

## 数据、范围与可信边界

- 主样本是 B0：原始回测引擎的可逐笔复现结果，窗口为 2023-09-12 至 2026-09-11（UTC），40 个有效 Binance USDT-M 永续合约 1 分钟 H/L/C/V 数据。
- 策略是 long-only trailing martingale；`n_positions=7`、`total_wallet_exposure_limit=1.5`，且本配置 **HSL 未启用**。回测报告中的 `liquidated=false` 只是该模拟的结果，绝不等同于 Binance 真实保证金体系下“不会爆仓”。
- 这份报告没有联网、没有读取账户、没有启动机器人；只重新聚合被冻结的本地 candle、fills 和逐分钟余额/权益工件。
- 当前配置中 `btc_collateral_cap=0`，且逐分钟验证 `strategy_equity == usd_total_equity`；下文的“账户权益”指这条 USD 策略权益曲线。
- 真实 Binance 风险还会受 mark price、资金费率、维持保证金阶梯、跨仓其他仓位、强平手续费、订单簿队列、部分成交、限价单受理与 API 延迟影响；这些没有逐笔/盘口/mark-price 数据就无法从 OHLC 精确复原。

## 三种口径，分别回答什么问题

| 口径 | 定义 | B0 全程结果 | 应如何使用 |
| --- | --- | ---: | --- |
| 已实现余额 MDD | `1 - balance / 历史 balance 高点` | {format_pct(b0['realized_balance_mdd_pct'])} | 解释现金账面为何几乎一直涨；**不能**作为期货账户风险指标 |
| 分钟收盘权益 MDD | `1 - (balance + 收盘未实现PnL) / 历史权益高点` | {format_pct(b0['minute_close_equity_mdd_pct'])} | 本研究中最重要、可逐分钟复核的账户级风险指标 |
| 蜡烛低点状态包络 | 对每分钟已知的开始/每笔 fill 后状态，分别用全部持仓币种该分钟 `Low` 同时盯市，取最差值 | {format_pct(low_stress_summary['global_proxy_mdd_pct'])} | 只作 OHLC 内部时序未知时的**压力代理**；不是实际路径、不是严格上下界 |

第三项故意偏向压力测试，但仍不能叫“真实最坏值”：同一分钟内不同币的 Low 未必同时发生；Low 与仓位变化的先后也未知；真实订单可能排队、拒绝、部分成交或根本未成交。反过来，真实 mark price 和强平也可能比收盘价更不利。因此不应把它解释为可兑现的实际 MDD 上界或下界。

本样本的该压力代理也在 2025-04-07 06:54 UTC 达到最大值：在此前分钟收盘高水位
{format_usdt(low_stress_summary['global_proxy_hwm_before_candle'])} USDT 下，组合的 Low 状态代理权益为
{format_usdt(low_stress_summary['global_proxy_equity'])} USDT，对应 {format_pct(low_stress_summary['global_proxy_mdd_pct'])}。

## 73.69% 是怎样产生的

| 节点 | UTC | 已实现余额 | 分钟收盘权益 | 未实现 PnL | 含义 |
| --- | --- | ---: | ---: | ---: | --- |
| 权益峰值 | {b0['minute_close_equity_mdd_peak_time']} | {format_usdt(b0['balance_at_equity_mdd_peak'])} | {format_usdt(b0['minute_close_equity_mdd_peak'])} | {format_usdt(peak_positions.attrs['aggregate']['reconstructed_unrealized_pnl_usdt'])} | 历史高水位 |
| 权益谷底 | {b0['minute_close_equity_mdd_trough_time']} | {format_usdt(b0['balance_at_equity_mdd_trough'])} | {format_usdt(b0['minute_close_equity_mdd_trough'])} | {format_usdt(trough_positions.attrs['aggregate']['reconstructed_unrealized_pnl_usdt'])} | 峰谷回撤 {format_pct(b0['minute_close_equity_mdd_pct'])} |
| 权益恢复 | {b0['minute_close_equity_recovery_time']} | — | 至少恢复到峰值 | — | 峰到恢复约 {b0['minute_close_equity_peak_to_recovery_days']:.2f} 天 |

余额自身的全程峰谷发生在 {b0['realized_balance_mdd_peak_time']} 至
{b0['realized_balance_mdd_trough_time']}，只下降了
{format_usdt(b0['realized_balance_mdd_peak'] - b0['realized_balance_mdd_trough'])} USDT；这与上表
权益 MDD 的峰谷不是同一对时间点。与此同时，权益谷底的持仓浮亏达到余额的
{format_pct(b0['max_unrealized_loss_pct_of_balance'])}。这正是马丁/网格模型的关键风险形态：
它可以持续把小额的反弹利润和已实现手续费后的利润写入余额，同时把单边不利行情的大部分损失留在未实现 PnL 中。只看余额会严重低估可用保证金和强平距离的压力。

## 每月账户回撤（B0，全部 UTC 自然月）

说明：

- **余额 MDD**：以当月开盘前一条余额作为起点、在当月内重置高水位。
- **收盘权益 MDD**：相同方式，以每分钟收盘权益计算的当月峰谷 MDD。
- **全程权益水下最大值**：不重置高水位，反映早前峰值造成的持续水下状态；2025-04 的 73.69% 在此列。
- **最大浮亏/余额**：当月最大 `(balance - equity) / balance`。
- **Low 状态代理**：上节定义的 OHLC 压力指标，不是实际账户路径。

{markdown_table(
    monthly_display,
    [
        "month",
        "balance_mdd",
        "close_equity_mdd",
        "global_close_equity_dd",
        "max_floating_loss",
        "low_state_proxy",
        "end_balance_equity",
    ],
    [
        "月份",
        "余额 MDD",
        "月内收盘权益 MDD",
        "全程权益水下最大值",
        "最大浮亏/余额",
        "Low 状态代理",
        "月末余额 / 权益",
    ],
)}

完整的可筛选数字、峰谷时间、每月 fills、入场、平仓、已实现 PnL 和手续费见：
`monthly_account_drawdowns.csv`。低点状态代理的逐月汇总见
`monthly_candle_low_state_stress_b0.csv`。

## 为什么 2025-10-10 急跌当日仍然录得利润

它并不是“下跌本身让 long 马丁赚钱”，而是一次在这份 OHLC 回测路径中出现的**急跌后快速反弹**：

- 当日已实现余额：从 {format_usdt(hourly_event['opening_balance'].iloc[0])} 上升到
  {format_usdt(hourly_event['closing_balance'].iloc[-1])} USDT。
- 157 笔模拟成交：104 笔 entry、53 笔 close；毛已实现 PnL
  {format_usdt(event_gross)} USDT，手续费 {format_usdt(event_fees)} USDT，净已实现
  **{format_usdt(event_net)} USDT**。
- 当日分钟收盘权益从 {format_usdt(day_open_equity)} 开始，日内峰值
  {format_usdt(day_peak)}，在 **2025-10-10 21:20 UTC** 跌至
  **{format_usdt(day_low)} USDT**；从本次急跌前当日高点计算，分钟收盘权益 MDD 为
  **14.29%**。
- 21:20 时余额还有 {format_usdt(oct_positions.attrs['aggregate']['balance'])} USDT，
  但组合浮亏为 {format_usdt(oct_positions.attrs['aggregate']['reconstructed_unrealized_pnl_usdt'])} USDT。
  随后的成交路径在 21:30 起出现多笔 close；21:34 的收盘权益已首次回到急跌前峰值之上。

所以“当日净已实现盈利”与“当日曾承受显著权益回撤”可以同时成立。回测路径里，急跌触发了 long 补仓限价单；随后短时间的反弹/波动使部分 close 网格成交并实现利润。若后续没有反弹、反弹不足、限价单没有真实完整成交、或真实保证金/mark-price 更严，这个相同的补仓过程就会变成更深的浮亏乃至强平风险。

### 2025-10-10 各小时路径（UTC）

{markdown_table(
    hourly_display,
    [
        "hour_utc",
        "min_close_equity",
        "low_state_proxy_equity",
        "fills",
        "entries",
        "closes",
        "net_pnl",
    ],
    [
        "小时",
        "最低收盘权益",
        "Low 状态代理权益",
        "成交",
        "入场",
        "平仓",
        "净已实现 PnL",
    ],
)}

21:00–21:59 这一小时有 {int(event['fills'])} 笔模拟成交（{int(event['entries'])} 入场、{int(event['closes'])} 平仓），净已实现
{format_usdt(event['net_realized_pnl'])} USDT；它把日内的账面风险转成了已实现利润，但不抵消此前 21:20 时真实承受的浮亏压力。

## 当时到底持有什么仓位

下表按完整 fills 账本重建仓位，并用相应分钟 `Close` 盯市；仓位未使用未来数据。所有未实现 PnL 加总与保存的账户权益在最多 {close_residual:.6f} USDT 的 CSV 舍入误差内相符。

{portfolio_sections}

尤其需要注意全程权益谷底：当时虽然只持有 6 个币种，但按建仓均价重建的总名义额为
{format_usdt(trough_positions.attrs['aggregate']['cost_basis_gross_notional_usdt'])} USDT，即余额的
**{format_pct(trough_positions.attrs['aggregate']['cost_basis_total_wallet_exposure'])}**；这恰好接近配置的
1.5 WEL 上限。价格下跌后，按当时收盘价计算的名义额仅为余额的
{format_pct(trough_positions.attrs['aggregate']['close_mark_total_wallet_exposure'])}，但此前已经形成的
成本基础和价格跌幅仍造成 313,322 USDT 浮亏。换言之，slot/WEL/TWEL 约束限制的是建仓意图/成本规模，
不是对浮亏、mark-price 权益或强平距离的保证。

## C1–C4 是否改变核心判断

因果修正后的 C1–C4 同样显示深度分钟收盘权益回撤。因此“余额很好看而权益承受巨大未实现亏损”不是 B0 的单一统计故障，而是该策略收益形态的结构性风险。C1/C2/C3/C4 仍不等于真实交易所路径，尤其不包括盘口队列、部分成交及 mark-price 强平。

{markdown_table(
    scenario_display,
    ["scenario", "ending_equity", "balance_mdd", "close_equity_mdd", "max_float_loss"],
    ["场景", "期末权益", "余额 MDD", "分钟收盘权益 MDD", "最大浮亏/余额"],
)}

## 风险边界：为什么不能只看 STOP SL / HSL

当前配置的 HSL 是关闭的，因此它没有构成这段回测的防线。即使开启某种 hard stop，也不能把风险边界简化为“价格到 STOP SL 就安全”，原因是：

1. 真正决定账户能否继续存活的是 **mark-price 下的权益、维持保证金和强平规则**，而非已实现余额。
2. 在快速行情中，stop/close 可能延迟、滑点、部分成交、排队，或在强平前无法完成；OHLC 不能证明这一点。
3. long martingale 在下跌中会提高成本基础和名义敞口；是否反弹、反弹何时发生，是收益与灾难之间的路径依赖分界。
4. 40 币组合的相关性在压力事件中会升高。7 个 nominal slots、WEL/TWEL 限制能约束意图规模，却不能保证所有标的不会同时向不利方向波动。

因此，若要把历史研究转化为实盘风险控制，应该把“分钟收盘权益 MDD 73.69%”视为已经很严重的模型内告警，而不是被 2.21% 的余额 MDD 安抚；并在真实 exchange 的 mark-price、保证金阶梯、资金费、订单簿/成交率和极端连续单边行情上另行做前向或逐笔级验证。
"""


def validate_account_metrics(summary, monthly_by_scenario, loaded):
    """Fail loudly if the report ceases to agree with its source artifacts."""

    result = {}
    for row in summary.itertuples(index=False):
        directory = scenario_result_dir(row.scenario)
        analysis = json.loads((directory / "analysis.json").read_text())
        _, initial, timeline, fills = loaded[row.scenario]
        if not np.isclose(
            row.minute_close_equity_mdd_pct,
            float(analysis["drawdown_worst_usd"]),
            rtol=0.0,
            atol=1e-8,
        ):
            raise ValueError(
                f"{row.scenario}: recomputed MDD disagrees with native analysis"
            )
        expected_balance = initial + float((fills["pnl"] + fills["fee_paid"]).sum())
        if not np.isclose(
            float(timeline["balance"].iloc[-1]),
            expected_balance,
            rtol=0.0,
            atol=BALANCE_RECONCILIATION_TOLERANCE_USDT,
        ):
            raise ValueError(
                f"{row.scenario}: ending saved balance disagrees with fill ledger"
            )
        monthly = monthly_by_scenario[row.scenario]
        if not np.allclose(
            monthly["opening_balance"].iloc[1:].to_numpy(),
            monthly["closing_balance"].iloc[:-1].to_numpy(),
            rtol=0.0,
            atol=EPSILON,
        ) or not np.allclose(
            monthly["opening_equity"].iloc[1:].to_numpy(),
            monthly["closing_equity"].iloc[:-1].to_numpy(),
            rtol=0.0,
            atol=EPSILON,
        ):
            raise ValueError(f"{row.scenario}: monthly opening/closing states are not continuous")
        result[row.scenario] = {
            "native_drawdown_worst_usd": float(analysis["drawdown_worst_usd"]),
            "recomputed_minute_close_equity_mdd": float(
                row.minute_close_equity_mdd_pct
            ),
            "ending_balance_ledger_residual_usdt": float(
                timeline["balance"].iloc[-1] - expected_balance
            ),
            "monthly_opening_closing_continuity": True,
        }
    return result


def main():
    summaries = []
    monthly_by_scenario = {}
    loaded = {}
    for scenario in SCENARIOS:
        if not (ROOT / scenario / "run_identity.json").exists():
            continue
        config, initial, timeline, fills = load_scenario(scenario)
        monthly, global_equity = local_monthly_rows(scenario, initial, timeline)
        aggregates = fill_aggregates(fills, "M")
        monthly = monthly.join(aggregates, on="month").fillna(
            {"fills": 0, "entries": 0, "closes": 0, "gross_realized_pnl": 0.0,
             "signed_fees": 0.0, "net_realized_pnl": 0.0}
        )
        summaries.append(account_summary(scenario, initial, timeline, global_equity))
        monthly_by_scenario[scenario] = monthly
        loaded[scenario] = (config, initial, timeline, fills)

    summary = pd.DataFrame(summaries)
    validation = validate_account_metrics(summary, monthly_by_scenario, loaded)
    monthly_all = pd.concat(monthly_by_scenario.values(), ignore_index=True)
    summary.to_csv(ROOT / "account_drawdown_summary.csv", index=False)
    monthly_all.to_csv(ROOT / "monthly_account_drawdowns.csv", index=False)

    _, b0_initial, b0_timeline, b0_fills = loaded["B0"]
    timestamps, hlcvs, coins, settings = load_market_data()
    low_stress, best_state, close_residual = calculate_candle_low_envelope(
        b0_initial, b0_timeline, b0_fills, timestamps, hlcvs, coins, settings
    )
    low_monthly = low_stress_monthly(b0_timeline, low_stress, b0_initial)
    low_monthly.to_csv(ROOT / "monthly_candle_low_state_stress_b0.csv", index=False)
    b0_monthly = monthly_by_scenario["B0"].merge(low_monthly, on="month", validate="one_to_one")
    b0_monthly.to_csv(ROOT / "monthly_account_drawdowns_b0.csv", index=False)

    b0_summary = summary.loc[summary["scenario"] == "B0"].iloc[0]
    low_proxy_dd = low_stress["candle_low_state_envelope_drawdown_pct"]
    low_proxy_worst = low_proxy_dd.idxmax()
    low_stress_summary = {
        "global_proxy_mdd_pct": float(low_proxy_dd.max()),
        "global_proxy_mdd_time": low_proxy_worst.isoformat(),
        "global_proxy_equity": float(
            low_stress.loc[low_proxy_worst, "candle_low_state_envelope_equity"]
        ),
        "global_proxy_hwm_before_candle": float(
            low_stress.loc[low_proxy_worst, "candle_low_state_envelope_equity"]
            / (1.0 - low_proxy_dd.loc[low_proxy_worst])
        ),
        "lowest_envelope_time": best_state["timestamp"].isoformat(),
        "lowest_envelope_equity": float(best_state["low_equity"]),
        "lowest_envelope_state": best_state["state"],
        "close_reconstruction_max_abs_residual_usdt": close_residual,
    }
    (ROOT / "candle_low_state_stress_b0_summary.json").write_text(
        json.dumps(low_stress_summary, indent=2) + "\n"
    )

    peak_time = pd.Timestamp(b0_summary["minute_close_equity_mdd_peak_time"])
    trough_time = pd.Timestamp(b0_summary["minute_close_equity_mdd_trough_time"])
    oct_time = pd.Timestamp("2025-10-10 21:20:00", tz="UTC")
    peak_positions, peak_aggregate = position_snapshot(
        b0_fills, coins, settings, timestamps, hlcvs, peak_time
    )
    trough_positions, trough_aggregate = position_snapshot(
        b0_fills, coins, settings, timestamps, hlcvs, trough_time
    )
    oct_positions, oct_aggregate = position_snapshot(
        b0_fills, coins, settings, timestamps, hlcvs, oct_time
    )
    for frame, aggregate, filename in [
        (peak_positions, peak_aggregate, "b0_positions_at_equity_peak.csv"),
        (trough_positions, trough_aggregate, "b0_positions_at_equity_trough.csv"),
        (oct_positions, oct_aggregate, "b0_positions_at_2025-10-10_2120.csv"),
    ]:
        frame.to_csv(ROOT / filename, index=False)
        frame.attrs["aggregate"] = aggregate

    hourly_event, event_fills = hourly_event_rows(b0_timeline, b0_fills, low_stress)
    daily_balance_change = float(
        hourly_event["closing_balance"].iloc[-1]
        - hourly_event["opening_balance"].iloc[0]
    )
    daily_net_realized = float((event_fills["pnl"] + event_fills["fee_paid"]).sum())
    if not np.isclose(
        daily_balance_change,
        daily_net_realized,
        rtol=0.0,
        atol=BALANCE_RECONCILIATION_TOLERANCE_USDT,
    ):
        raise ValueError("2025-10-10 balance change disagrees with its fill ledger")
    validation["B0"]["2025-10-10_balance_ledger_residual_usdt"] = (
        daily_balance_change - daily_net_realized
    )
    validation["B0"]["candle_low_close_reconstruction_max_abs_residual_usdt"] = (
        close_residual
    )
    (ROOT / "account_drawdown_validation.json").write_text(
        json.dumps(validation, indent=2) + "\n"
    )
    hourly_event.to_csv(ROOT / "account_drawdown_2025-10-10_hourly.csv", index=False)
    event_fills.to_csv(ROOT / "fills_2025-10-10.csv", index=False)

    report = render_report(
        summary,
        summary,
        b0_monthly,
        hourly_event,
        event_fills,
        low_stress_summary,
        peak_positions,
        trough_positions,
        oct_positions,
        close_residual,
    )
    (ROOT / "account_drawdown_deep_dive.md").write_text(report)
    print(
        json.dumps(
            {
                "report": str(ROOT / "account_drawdown_deep_dive.md"),
                "b0_balance_mdd_pct": float(b0_summary["realized_balance_mdd_pct"]),
                "b0_minute_close_equity_mdd_pct": float(
                    b0_summary["minute_close_equity_mdd_pct"]
                ),
                "b0_candle_low_state_proxy_mdd_pct": low_stress_summary[
                    "global_proxy_mdd_pct"
                ],
                "close_reconstruction_max_abs_residual_usdt": close_residual,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
