"""Offline, reproducible local study of public low-drawdown strategy profiles.

The tool deliberately operates only on the already frozen Binance H/L/C/V cache.
It never starts a bot, accesses an exchange account, sends an exchange request, or
mutates the source example configurations.
"""

import argparse
import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import socket
import time

import numpy as np
import pandas as pd


REPO = Path(".")
STUDY = Path("backtests/binance/low_drawdown_strategy_study_2026-09-14")
DATASET = Path(
    "caches/hlcvs_data/"
    "binance__40_coins__2023-08-17_to_2026-09-12__2c11e36fd7fc3806"
)
SELECTION_CAP = 0.30
EPSILON = 1e-8
TIMELINE_MDD_RECONCILIATION_TOLERANCE = 1e-6
BALANCE_RECONCILIATION_TOLERANCE_USDT = 0.10
CLOSE_RECONCILIATION_TOLERANCE_USDT = 0.25
HIGH_INDEX = 0
LOW_INDEX = 1
CLOSE_INDEX = 2

CANDIDATES = {
    "default_trailing_martingale_long": {
        "path": Path("configs/examples/default_trailing_martingale_long.json"),
        "label": "Default 40-coin trailing martingale long",
    },
    "btc_eth_xrp_sol_ada_long": {
        "path": Path("configs/examples/BTC_ETH_XRP_SOL_ADA_long.json"),
        "label": "Five-coin trailing martingale long",
    },
    "btc_long": {
        "path": Path("configs/examples/btc_long.json"),
        "label": "BTC-only trailing martingale long",
    },
    "hsl_npos1": {
        "path": Path("configs/examples/hsl_npos1.json"),
        "label": "HSL-enabled trailing martingale",
    },
    "ema_anchor": {
        "path": Path("configs/examples/ema_anchor.json"),
        "label": "SOL EMA-anchor long/short",
    },
    "xmr_long_short": {
        "path": Path("configs/examples/xmr_long_short.json"),
        "label": "XMR trailing martingale hedge",
    },
}

WINDOWS = {
    "smoke": {"start": "2023-09-12", "end": "2023-09-14"},
    "selection": {"start": "2023-09-12", "end": "2025-09-12"},
    "holdout": {"start": "2025-09-12", "end": "2026-09-12"},
    "full": {"start": "2023-09-12", "end": "2026-09-12"},
}

SCENARIOS = {
    "C1": {
        "execution_delay_bars": 0,
        "intrabar_fill_order": "close_first",
        "description": "Causal nominal T+1, close-first",
    },
    "C2": {
        "execution_delay_bars": 0,
        "intrabar_fill_order": "entry_first",
        "description": "Causal nominal T+1, entry-first",
    },
    "C3": {
        "execution_delay_bars": 1,
        "intrabar_fill_order": "close_first",
        "description": "Causal T+2 stress, close-first",
    },
    "C4": {
        "execution_delay_bars": 1,
        "intrabar_fill_order": "entry_first",
        "description": "Causal T+2 stress, entry-first",
    },
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def repo_relative(path):
    return str(Path(path).relative_to(REPO))


def write_json_new(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def write_text_new(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        handle.write(value)


def write_csv_new(path, frame):
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite derived study file: {path}")
    frame.to_csv(path, index=False)


def json_scalar(value):
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"not a JSON scalar: {value!r}")


def read_json(path):
    return json.loads(Path(path).read_text())


def candidate_path(candidate):
    if candidate not in CANDIDATES:
        raise ValueError(f"unknown candidate {candidate!r}")
    return CANDIDATES[candidate]["path"]


def run_root(stage, candidate, scenario):
    return STUDY / stage / candidate / scenario


def identity_path(stage, candidate, scenario):
    return run_root(stage, candidate, scenario) / "run_identity.json"


def load_identity(stage, candidate, scenario):
    path = identity_path(stage, candidate, scenario)
    if not path.exists():
        raise FileNotFoundError(f"required replay identity is missing: {path}")
    return read_json(path)


def reject_network(*args, **kwargs):
    raise RuntimeError("network is forbidden during this frozen-dataset study")


def disable_network():
    socket.socket.connect = reject_network
    socket.socket.connect_ex = reject_network
    socket.create_connection = reject_network


def runtime_identity():
    from rust_utils import verify_loaded_runtime_extension

    import passivbot_rust

    runtime = verify_loaded_runtime_extension()
    return {
        "expected_source_fingerprint": runtime["expected_source_fingerprint"],
        "runtime_compiled_sha256": runtime["runtime_compiled_sha256"],
        "runtime_build_info": passivbot_rust.runtime_build_info(),
    }


def source_config(candidate):
    from config import load_prepared_config

    return load_prepared_config(str(candidate_path(candidate)), verbose=False)


def ticker(value):
    return value.split("/")[0]


def raw_requested_coins(config):
    requested = {}
    for side in ("long", "short"):
        values = config["live"]["approved_coins"][side]
        if not isinstance(values, list):
            raise ValueError(
                f"only explicit public coin lists are supported in this study: {side}={values!r}"
            )
        requested[side] = sorted({ticker(coin) for coin in values})
    return requested


def candidate_inventory(candidate, dataset_coins):
    config = source_config(candidate)
    requested = raw_requested_coins(config)
    requested_union = set(requested["long"]) | set(requested["short"])
    available = set(dataset_coins)
    strategy = config["live"]["strategy_kind"]
    record = {
        "candidate": candidate,
        "label": CANDIDATES[candidate]["label"],
        "config_source": str(candidate_path(candidate)),
        "config_source_sha256": sha256(candidate_path(candidate)),
        "strategy_kind": strategy,
        "hedge_mode": bool(config["live"]["hedge_mode"]),
        "hsl_signal_mode": config["live"]["hsl_signal_mode"],
        "requested_long_coins": ",".join(requested["long"]),
        "requested_short_coins": ",".join(requested["short"]),
        "requested_coin_count": len(requested_union),
        "effective_frozen_coin_count": len(requested_union & available),
        "missing_from_frozen_dataset": ",".join(sorted(requested_union - available)),
    }
    for side in ("long", "short"):
        risk = config["bot"][side]["risk"]
        hsl = config["bot"][side]["hsl"]
        enabled = risk["n_positions"] > 0 and risk["total_wallet_exposure_limit"] > 0
        record.update(
            {
                f"{side}_enabled": enabled,
                f"{side}_n_positions": risk["n_positions"],
                f"{side}_twel": risk["total_wallet_exposure_limit"],
                f"{side}_we_excess_allowance_pct": risk["we_excess_allowance_pct"],
                f"{side}_hsl_enabled": hsl["enabled"],
                f"{side}_hsl_red_threshold": hsl["red_threshold"],
                f"{side}_hsl_panic_close_order_type": hsl["panic_close_order_type"],
                f"{side}_unstuck_enabled": config["bot"][side]["unstuck"]["enabled"],
                f"{side}_position_exposure_enforcer_enabled": risk[
                    "position_exposure_enforcer_enabled"
                ],
                f"{side}_total_exposure_enforcer_enabled": risk[
                    "total_exposure_enforcer_enabled"
                ],
            }
        )
    return record


def load_dataset_metadata():
    manifest = read_json(DATASET / "manifest.json")
    coins = read_json(DATASET / "coins.json")
    settings = read_json(DATASET / "market_specific_settings.json")
    return manifest, coins, settings


def initialize():
    if STUDY.exists():
        raise FileExistsError(f"study directory already exists: {STUDY}")
    manifest, dataset_coins, _ = load_dataset_metadata()
    runtime = runtime_identity()
    STUDY.mkdir(parents=True)
    inventory = [candidate_inventory(candidate, dataset_coins) for candidate in CANDIDATES]
    write_csv_new(STUDY / "candidate_inventory.csv", pd.DataFrame(inventory))
    write_json_new(
        STUDY / "study_manifest.json",
        {
            "purpose": (
                "Offline selection of an existing public low-drawdown strategy profile; "
                "no economic parameter optimization or exchange interaction."
            ),
            "selection_cap_minute_close_equity_mdd": SELECTION_CAP,
            "selection_window": WINDOWS["selection"],
            "holdout_window": WINDOWS["holdout"],
            "full_window": WINDOWS["full"],
            "execution_contract": {
                "selection": "C1: causal T+1, close-first",
                "full_selected": SCENARIOS,
            },
            "dataset_relative_path": str(DATASET),
            "dataset_manifest_sha256": sha256(DATASET / "manifest.json"),
            "dataset_files": {
                name: {
                    "path": entry["path"],
                    "sha256": sha256(DATASET / entry["path"]),
                }
                for name, entry in manifest["files"].items()
            },
            "dataset_coins": dataset_coins,
            "runtime": runtime,
            "candidates": inventory,
            "excluded_public_example": {
                "path": "configs/examples/suite_example.json",
                "reason": (
                    "It is a six-month suite/CLI demonstration rather than a "
                    "production strategy profile."
                ),
            },
            "network_forbidden": True,
        },
    )
    print(f"initialized offline study at {STUDY}")


def study_config(candidate, stage, scenario, run_directory):
    if stage not in WINDOWS:
        raise ValueError(f"unknown study stage {stage!r}")
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown execution scenario {scenario!r}")
    config = source_config(candidate)
    original = deepcopy(config)
    window = WINDOWS[stage]
    config["_original_backtest_config"] = original
    config["backtest"]["start_date"] = window["start"]
    config["backtest"]["end_date"] = window["end"]
    config["backtest"]["exchanges"] = ["binance"]
    config["backtest"]["hlcvs_data_dir"] = str(DATASET)
    config["backtest"]["hlcvs_data_override_mode"] = "intersection"
    config["backtest"]["base_dir"] = str(run_directory)
    config["backtest"]["balance_sample_divider"] = 1
    config["backtest"]["execution_delay_bars"] = SCENARIOS[scenario][
        "execution_delay_bars"
    ]
    config["backtest"]["intrabar_fill_order"] = SCENARIOS[scenario][
        "intrabar_fill_order"
    ]
    config["backtest"]["execution_audit_path"] = str(
        run_directory / "execution_boundary_audit.csv"
    )
    config["backtest"]["suite_enabled"] = False
    config["disable_plotting"] = "all"
    return config


def validate_replay_request(stage, candidate, scenario):
    if stage == "smoke":
        if scenario != "C1":
            raise ValueError("bounded smoke replays are fixed to causal C1")
        return
    if stage == "selection":
        if scenario != "C1":
            raise ValueError("candidate selection is fixed to causal C1")
        return
    selection = read_json(STUDY / "selection_manifest.json")
    selected = selection["selected_candidate"]
    if candidate != selected:
        raise ValueError(
            f"{stage} may only replay locked candidate {selected!r}, not {candidate!r}"
        )
    if stage == "holdout" and scenario != "C1":
        raise ValueError("the locked holdout is fixed to causal C1")
    if stage == "full" and scenario not in SCENARIOS:
        raise ValueError(f"unsupported full-study scenario {scenario!r}")


async def replay(stage, candidate, scenario):
    if not (STUDY / "study_manifest.json").exists():
        raise FileNotFoundError("run init before replay")
    validate_replay_request(stage, candidate, scenario)
    output = run_root(stage, candidate, scenario)
    output.mkdir(parents=True, exist_ok=False)
    disable_network()

    import backtest
    from logging_setup import configure_logging

    configure_logging(debug=0)
    runtime = runtime_identity()
    study = read_json(STUDY / "study_manifest.json")
    if runtime != study["runtime"]:
        raise RuntimeError(
            "the loaded Rust extension identity differs from the frozen study runtime"
        )

    config = study_config(candidate, stage, scenario, output)
    requested = raw_requested_coins(config)
    write_json_new(output / "study_input_config.json", config)
    started = time.perf_counter()
    coins, hlcvs, settings, results_path, cache_dir, btc_prices, timestamps = (
        await backtest.prepare_hlcvs_mss(config, "binance")
    )
    config.setdefault("backtest", {}).setdefault("coins", {})["binance"] = coins
    config["backtest"].setdefault("cache_dir", {})["binance"] = str(cache_dir)

    expected = set(requested["long"]) | set(requested["short"])
    effective = set(coins)
    unavailable = sorted(expected - effective)
    if effective - set(study["dataset_coins"]):
        raise ValueError("override replay returned a coin not present in frozen dataset")
    if not effective:
        raise ValueError(f"{candidate}: no requested coins survive frozen-data intersection")
    fills, equity, analysis, payload = backtest.run_backtest(
        hlcvs,
        settings,
        config,
        "binance",
        btc_prices,
        timestamps,
        return_payload=True,
    )
    np.save(output / "minute_equity.npy", equity, allow_pickle=False)
    backtest.post_process(
        config,
        hlcvs,
        fills,
        equity,
        btc_prices,
        analysis,
        results_path,
        "binance",
        plot_context=backtest.BacktestPlotContext.from_payload(payload),
    )
    result_dirs = sorted(path.parent for path in output.glob("binance/*/analysis.json"))
    if len(result_dirs) != 1:
        raise RuntimeError(f"expected exactly one result directory, found {result_dirs}")
    result_dir = result_dirs[0]
    audit_path = output / "execution_boundary_audit.csv"
    if not audit_path.exists():
        raise RuntimeError(f"causal replay failed to write execution audit: {audit_path}")
    write_json_new(
        output / "run_identity.json",
        {
            "stage": stage,
            "candidate": candidate,
            "candidate_label": CANDIDATES[candidate]["label"],
            "candidate_source": str(candidate_path(candidate)),
            "candidate_source_sha256": sha256(candidate_path(candidate)),
            "window": WINDOWS[stage],
            "execution_contract": SCENARIOS[scenario],
            "scenario": scenario,
            "effective_coins": coins,
            "effective_coin_count": len(coins),
            "requested_coins": requested,
            "unavailable_requested_coins": unavailable,
            "effective_hlcv_shape": list(hlcvs.shape),
            "result_dir": repo_relative(result_dir),
            "execution_audit_path": repo_relative(audit_path),
            "runtime": runtime,
            "dataset_manifest_sha256": study["dataset_manifest_sha256"],
            "network_disabled": True,
            "elapsed_seconds": time.perf_counter() - started,
        },
    )
    print(
        f"completed {stage}/{candidate}/{scenario}: "
        f"{result_dir} with {len(coins)} effective coins"
    )


def load_run(identity):
    result_dir = Path(identity["result_dir"])
    config = read_json(result_dir / "config.json")
    analysis = read_json(result_dir / "analysis.json")
    with gzip.open(result_dir / "balance_and_equity.csv.gz", "rt") as handle:
        timeline = pd.read_csv(handle, index_col=0)
    timeline.index = pd.to_datetime(timeline.index, utc=True)
    timeline.index.name = "timestamp"
    timeline = timeline.rename(
        columns={
            "usd_total_balance": "balance",
            "usd_total_equity": "account_equity",
            "strategy_equity": "strategy_equity",
        }
    )
    timeline = timeline.loc[:, ["balance", "account_equity", "strategy_equity"]]
    timeline["equity"] = timeline["account_equity"]
    if timeline.empty:
        raise ValueError(f"empty balance/equity timeline in {result_dir}")
    if timeline.index.has_duplicates or not timeline.index.is_monotonic_increasing:
        raise ValueError(f"timeline lacks unique increasing timestamps in {result_dir}")
    if not (timeline.index.to_series().diff().dropna() == pd.Timedelta(minutes=1)).all():
        raise ValueError(f"timeline is not minute-contiguous in {result_dir}")
    if (
        not np.isfinite(timeline["account_equity"]).all()
        or not np.isfinite(timeline["strategy_equity"]).all()
        or (timeline["account_equity"] <= 0.0).any()
        or (timeline["strategy_equity"] <= 0.0).any()
    ):
        raise ValueError(f"account or strategy equity is invalid in {result_dir}")
    fills = pd.read_csv(result_dir / "fills.csv")
    fills = fills.loc[:, ~fills.columns.str.startswith("Unnamed:")]
    fills["timestamp"] = pd.to_datetime(fills["timestamp"], utc=True)
    fills = fills.sort_values(["timestamp", "index"], kind="stable").reset_index(drop=True)
    return config, analysis, timeline, fills


def drawdown_details(values, labels):
    values = np.asarray(values, dtype=np.float64)
    if len(values) != len(labels) or not len(values):
        raise ValueError("drawdown values and labels must be non-empty and aligned")
    if not np.isfinite(values).all() or (values <= 0.0).any():
        raise ValueError("drawdown requires positive, finite equity")
    high_water = np.maximum.accumulate(values)
    drawdowns = 1.0 - values / high_water
    trough = int(np.argmax(drawdowns))
    peak_value = high_water[trough]
    peak = int(np.flatnonzero(values[: trough + 1] == peak_value)[-1])
    recovered = np.flatnonzero(values[trough + 1 :] >= peak_value)
    recovery = int(trough + 1 + recovered[0]) if len(recovered) else None
    peak_time = labels[peak]
    recovery_time = labels[recovery] if recovery is not None else None
    return {
        "mdd_pct": float(drawdowns[trough]),
        "peak": float(peak_value),
        "peak_time": peak_time,
        "trough": float(values[trough]),
        "trough_time": labels[trough],
        "recovery_time": recovery_time,
        "peak_to_recovery_days": (
            (recovery_time - peak_time).total_seconds() / 86400
            if recovery_time is not None and peak_time is not None
            else None
        ),
    }


def is_finite_positive(series):
    values = np.asarray(series, dtype=np.float64)
    return bool(np.isfinite(values).all() and (values > 0.0).all())


def has_reconciliable_usd_cash_balance(config, timeline):
    """Whether persisted USD balance is a cash ledger rather than marked collateral."""

    return bool(
        float(config["backtest"]["btc_collateral_cap"]) <= 0.0
        and is_finite_positive(timeline["balance"])
    )


def daily_worst_drawdown_mean_1pct(values, initial):
    """Mean of the worst 1% calendar-day drawdowns from account-equity closes."""

    values = pd.Series(np.asarray(values, dtype=np.float64), index=values.index)
    high_water = np.maximum.accumulate(np.r_[initial, values.to_numpy()])[1:]
    drawdown = pd.Series(1.0 - values.to_numpy() / high_water, index=values.index)
    daily_worst = drawdown.groupby(drawdown.index.tz_localize(None).date).max()
    count = max(1, int(len(daily_worst) * 0.01))
    return float(daily_worst.nlargest(count).mean())


def monthly_account_metrics(
    timeline, initial, candidate, stage, scenario, balance_available
):
    previous_balance = initial if balance_available else np.nan
    previous_equity = initial
    rows = []
    months = timeline.index.tz_localize(None).to_period("M")
    for period, group in timeline.groupby(months, sort=True):
        opening_time = group.index[0] - pd.Timedelta(minutes=1)
        labels = pd.DatetimeIndex([opening_time]).append(group.index)
        balance_values = np.r_[previous_balance, group["balance"].to_numpy()]
        equity_values = np.r_[previous_equity, group["equity"].to_numpy()]
        balance_dd = (
            drawdown_details(balance_values, labels) if balance_available else None
        )
        equity_dd = drawdown_details(equity_values, labels)
        floating_loss = (
            group["balance"] - group["equity"] if balance_available else None
        )
        rows.append(
            {
                "candidate": candidate,
                "stage": stage,
                "scenario": scenario,
                "month": str(period),
                "opening_balance": previous_balance if balance_available else np.nan,
                "closing_balance": (
                    float(group["balance"].iloc[-1]) if balance_available else np.nan
                ),
                "opening_equity": previous_equity,
                "closing_equity": float(group["equity"].iloc[-1]),
                "monthly_equity_return": float(
                    group["equity"].iloc[-1] / previous_equity - 1.0
                ),
                "realized_balance_mdd_pct": (
                    balance_dd["mdd_pct"] if balance_dd is not None else np.nan
                ),
                "minute_close_equity_mdd_pct": equity_dd["mdd_pct"],
                "equity_mdd_peak_time": equity_dd["peak_time"].isoformat(),
                "equity_mdd_trough_time": equity_dd["trough_time"].isoformat(),
                "max_unrealized_loss_usdt": (
                    float(floating_loss.max()) if floating_loss is not None else np.nan
                ),
                "max_unrealized_loss_pct_of_balance": (
                    float((floating_loss / group["balance"]).max())
                    if floating_loss is not None
                    else np.nan
                ),
            }
        )
        if balance_available:
            previous_balance = float(group["balance"].iloc[-1])
        previous_equity = float(group["equity"].iloc[-1])
    return pd.DataFrame(rows)


def run_metrics(identity):
    config, analysis, timeline, fills = load_run(identity)
    initial = float(config["backtest"]["starting_balance"])
    btc_collateral_cap = float(config["backtest"]["btc_collateral_cap"])
    labels = pd.DatetimeIndex([timeline.index[0] - pd.Timedelta(minutes=1)]).append(
        timeline.index
    )
    equity_dd = drawdown_details(np.r_[initial, timeline["equity"].to_numpy()], labels)
    balance_available = has_reconciliable_usd_cash_balance(config, timeline)
    balance_dd = (
        drawdown_details(np.r_[initial, timeline["balance"].to_numpy()], labels)
        if balance_available
        else None
    )
    duration_days = (timeline.index[-1] - timeline.index[0]).total_seconds() / 86400
    ending_equity = float(timeline["equity"].iloc[-1])
    ending_balance = (
        float(timeline["balance"].iloc[-1]) if balance_available else np.nan
    )
    monthly = monthly_account_metrics(
        timeline,
        initial,
        identity["candidate"],
        identity["stage"],
        identity["scenario"],
        balance_available,
    )
    floating_loss = timeline["balance"] - timeline["equity"] if balance_available else None
    panic = fills["type"].astype(str).str.startswith("close_panic")
    net_realized = float((fills["pnl"] + fills["fee_paid"]).sum())
    record = {
        "candidate": identity["candidate"],
        "label": identity["candidate_label"],
        "stage": identity["stage"],
        "scenario": identity["scenario"],
        "strategy_kind": config["live"]["strategy_kind"],
        "effective_coin_count": identity["effective_coin_count"],
        "effective_coins": ",".join(identity["effective_coins"]),
        "unavailable_requested_coins": ",".join(
            identity["unavailable_requested_coins"]
        ),
        "starting_balance": initial,
        "btc_collateral_cap": btc_collateral_cap,
        "account_equity_metric": "persisted_usd_total_equity",
        "realized_balance_available": balance_available,
        "balance_accounting": (
            "usd_cash_ledger"
            if balance_available
            else "marked_btc_collateral_total_not_cash_ledger"
        ),
        "ending_balance": ending_balance,
        "ending_equity": ending_equity,
        "equity_return": ending_equity / initial - 1.0,
        "equity_cagr": (
            (ending_equity / initial) ** (365.25 / duration_days) - 1.0
            if duration_days > 0.0
            else np.nan
        ),
        "realized_balance_mdd_pct": (
            balance_dd["mdd_pct"] if balance_dd is not None else np.nan
        ),
        "minute_close_equity_mdd_pct": equity_dd["mdd_pct"],
        "equity_mdd_peak": equity_dd["peak"],
        "equity_mdd_peak_time": equity_dd["peak_time"].isoformat(),
        "equity_mdd_trough": equity_dd["trough"],
        "equity_mdd_trough_time": equity_dd["trough_time"].isoformat(),
        "equity_mdd_recovery_time": (
            equity_dd["recovery_time"].isoformat()
            if equity_dd["recovery_time"] is not None
            else None
        ),
        "equity_mdd_peak_to_recovery_days": equity_dd["peak_to_recovery_days"],
        "max_unrealized_loss_usdt": (
            float(floating_loss.max()) if floating_loss is not None else np.nan
        ),
        "max_unrealized_loss_pct_of_balance": (
            float((floating_loss / timeline["balance"]).max())
            if floating_loss is not None
            else np.nan
        ),
        "underwater_minutes": int(
            np.count_nonzero(
                timeline["equity"].to_numpy()
                < np.maximum.accumulate(timeline["equity"].to_numpy())
            )
        ),
        "underwater_fraction": float(
            np.mean(
                timeline["equity"].to_numpy()
                < np.maximum.accumulate(timeline["equity"].to_numpy())
            )
        ),
        "monthly_count": len(monthly),
        "positive_month_fraction": float((monthly["monthly_equity_return"] > 0.0).mean()),
        "negative_month_count": int((monthly["monthly_equity_return"] < 0.0).sum()),
        "median_monthly_equity_return": float(monthly["monthly_equity_return"].median()),
        "monthly_equity_return_std": float(monthly["monthly_equity_return"].std(ddof=0)),
        "worst_monthly_equity_mdd_pct": float(
            monthly["minute_close_equity_mdd_pct"].max()
        ),
        "p95_monthly_equity_mdd_pct": float(
            monthly["minute_close_equity_mdd_pct"].quantile(0.95)
        ),
        "fills": len(fills),
        "entries": int(fills["type"].astype(str).str.startswith("entry_").sum()),
        "closes": int(fills["type"].astype(str).str.startswith("close_").sum()),
        "hsl_panic_fills": int(panic.sum()),
        "gross_realized_pnl": float(fills["pnl"].sum()),
        "signed_fees": float(fills["fee_paid"].sum()),
        "net_realized_pnl": net_realized,
        "final_unrealized_pnl": (
            ending_equity - ending_balance if balance_available else np.nan
        ),
        "account_drawdown_worst_mean_1pct": daily_worst_drawdown_mean_1pct(
            timeline["equity"], initial
        ),
        "native_drawdown_worst_usd": float(analysis["drawdown_worst_usd"]),
        "native_drawdown_worst_strategy_eq": float(
            analysis["drawdown_worst_strategy_eq"]
        ),
        "native_drawdown_worst_mean_1pct_strategy_eq": float(
            analysis["drawdown_worst_mean_1pct_strategy_eq"]
        ),
        "native_strategy_eq_underwater_pct_mean": float(
            analysis["strategy_eq_underwater_pct_mean"]
        ),
        "native_peak_recovery_days_equity_usd": analysis.get(
            "peak_recovery_days_equity_usd"
        ),
        "completion_ratio": float(analysis["backtest_completion_ratio"]),
        "liquidated": bool(analysis["liquidated"]),
        "hsl_signal_mode": config["live"]["hsl_signal_mode"],
        "long_hsl_enabled": bool(config["bot"]["long"]["hsl"]["enabled"]),
        "short_hsl_enabled": bool(config["bot"]["short"]["hsl"]["enabled"]),
        "long_twel": float(config["bot"]["long"]["risk"]["total_wallet_exposure_limit"]),
        "short_twel": float(
            config["bot"]["short"]["risk"]["total_wallet_exposure_limit"]
        ),
        "long_n_positions": float(config["bot"]["long"]["risk"]["n_positions"]),
        "short_n_positions": float(config["bot"]["short"]["risk"]["n_positions"]),
        "result_dir": identity["result_dir"],
        "execution_audit_path": identity["execution_audit_path"],
    }
    return record, monthly


def selection_replays():
    return [load_identity("selection", candidate, "C1") for candidate in CANDIDATES]


def select_candidate():
    if (STUDY / "selection_manifest.json").exists():
        raise FileExistsError("selection is already locked; refusing to revise it")
    metrics_rows = []
    monthly_rows = []
    for identity in selection_replays():
        metrics, monthly = run_metrics(identity)
        metrics_rows.append(metrics)
        monthly_rows.append(monthly)
    metrics = pd.DataFrame(metrics_rows)
    monthly = pd.concat(monthly_rows, ignore_index=True)
    inventory = pd.read_csv(STUDY / "candidate_inventory.csv")
    metrics = metrics.merge(
        inventory[
            [
                "candidate",
                "config_source",
                "config_source_sha256",
                "requested_coin_count",
                "effective_frozen_coin_count",
                "missing_from_frozen_dataset",
            ]
        ],
        on="candidate",
        validate="one_to_one",
    )
    metrics["complete"] = metrics["completion_ratio"] >= 0.999999
    metrics["positive_return"] = metrics["equity_return"] > 0.0
    metrics["within_mdd_cap"] = metrics["minute_close_equity_mdd_pct"] <= SELECTION_CAP
    metrics["eligible"] = (
        metrics["complete"]
        & ~metrics["liquidated"]
        & metrics["positive_return"]
        & metrics["within_mdd_cap"]
    )
    reasons = []
    for row in metrics.itertuples(index=False):
        failures = []
        if not row.complete:
            failures.append("incomplete")
        if row.liquidated:
            failures.append("simulated_liquidation")
        if not row.positive_return:
            failures.append("non_positive_equity_return")
        if not row.within_mdd_cap:
            failures.append("MDD_above_30pct")
        reasons.append(",".join(failures) if failures else "eligible")
    metrics["eligibility"] = reasons
    rank_pool = metrics.loc[metrics["eligible"]].copy()
    selection_status = "eligible_pass"
    if rank_pool.empty:
        rank_pool = metrics.loc[
            metrics["complete"] & ~metrics["liquidated"] & metrics["positive_return"]
        ].copy()
        selection_status = "fallback_no_candidate_meets_30pct_mdd_cap"
    if rank_pool.empty:
        rank_pool = metrics.loc[metrics["complete"] & ~metrics["liquidated"]].copy()
        selection_status = "fallback_no_positive_return_candidate"
    if rank_pool.empty:
        raise RuntimeError("all candidate selection replays were incomplete or liquidated")
    rank_pool["recovery_days_for_rank"] = rank_pool[
        "equity_mdd_peak_to_recovery_days"
    ].fillna(np.inf)
    ranked = rank_pool.sort_values(
        [
            "minute_close_equity_mdd_pct",
            "account_drawdown_worst_mean_1pct",
            "recovery_days_for_rank",
            "positive_month_fraction",
            "median_monthly_equity_return",
            "equity_cagr",
            "candidate",
        ],
        ascending=[True, True, True, False, False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    ranked["selection_rank"] = np.arange(1, len(ranked) + 1)
    metrics = metrics.merge(
        ranked[["candidate", "selection_rank"]],
        on="candidate",
        how="left",
        validate="one_to_one",
    )
    metrics["selection_status"] = np.where(
        metrics["candidate"] == ranked.iloc[0]["candidate"],
        selection_status,
        "not_selected",
    )
    metrics = metrics.sort_values(
        ["selection_rank", "minute_close_equity_mdd_pct", "candidate"],
        na_position="last",
        kind="stable",
    )
    write_csv_new(STUDY / "selection_candidate_metrics.csv", metrics)
    write_csv_new(STUDY / "selection_monthly_account_metrics.csv", monthly)
    write_csv_new(STUDY / "selection_ranking.csv", ranked)
    selected = ranked.iloc[0]
    selected_identity = load_identity("selection", selected["candidate"], "C1")
    write_json_new(
        STUDY / "selection_manifest.json",
        {
            "selected_candidate": selected["candidate"],
            "selection_status": selection_status,
            "selection_cap_minute_close_equity_mdd": SELECTION_CAP,
            "selection_window": WINDOWS["selection"],
            "selection_contract": SCENARIOS["C1"],
            "selection_rules": [
                "complete run",
                "no simulated liquidation",
                "positive selection-window equity return",
                "minute-close equity MDD <= 30%",
                (
                    "tie-break: lower MDD, lower 1% drawdown severity, shorter recovery, "
                    "higher positive-month fraction, higher median monthly return, higher CAGR"
                ),
            ],
            "selected_selection_metrics": {
                key: json_scalar(value) for key, value in selected.to_dict().items()
            },
            "selected_run_identity": repo_relative(
                identity_path("selection", selected["candidate"], "C1")
            ),
            "selected_candidate_source_sha256": selected_identity[
                "candidate_source_sha256"
            ],
            "holdout_not_examined_at_selection": True,
        },
    )
    print(
        f"locked selection {selected['candidate']} ({selection_status}); "
        f"MDD={selected['minute_close_equity_mdd_pct']:.4%}"
    )


def load_market_data():
    with gzip.open(DATASET / "timestamps.npy.gz", "rb") as handle:
        timestamps = np.load(handle, allow_pickle=False)
    with gzip.open(DATASET / "hlcvs.npy.gz", "rb") as handle:
        hlcvs = np.load(handle, allow_pickle=False)
    _, coins, settings = load_dataset_metadata()
    if hlcvs.shape[:2] != (len(timestamps), len(coins)) or hlcvs.shape[2] != 4:
        raise ValueError("frozen H/L/C/V dataset has an unexpected shape")
    if not np.all(np.diff(timestamps) == 60_000):
        raise ValueError("frozen market-data timestamps are not minute contiguous")
    return timestamps, hlcvs, coins, settings


def calculate_adverse_hl_state_envelope(
    initial, timeline, fills, timestamps, hlcvs, coins, settings
):
    """Compute a labelled stress proxy, not a reconstructable intrabar equity path.

    Long positions are marked to each candle's Low and short positions to each
    candle's High.  Per-symbol adverse extrema are concurrent by construction, and
    each minute's known start/post-fill states are considered independently.
    """

    first_ms = int(timeline.index[0].value // 1_000_000)
    last_ms = int(timeline.index[-1].value // 1_000_000)
    first = int(np.searchsorted(timestamps, first_ms))
    last = int(np.searchsorted(timestamps, last_ms))
    observed = timeline.index.astype("int64").to_numpy() // 1_000_000
    if (
        last - first + 1 != len(timeline)
        or not np.array_equal(timestamps[first : last + 1], observed)
    ):
        raise ValueError("replay timeline does not map one-to-one to frozen candles")

    coin_index = {coin: index for index, coin in enumerate(coins)}
    multipliers = np.asarray([float(settings[coin]["c_mult"]) for coin in coins])
    sizes = np.zeros(len(coins), dtype=np.float64)
    prices = np.zeros(len(coins), dtype=np.float64)
    adverse_equity = np.empty(len(timeline), dtype=np.float64)
    close_reconstructed = np.empty(len(timeline), dtype=np.float64)
    fill_groups = {
        int(timestamp.value // 1_000_000): group
        for timestamp, group in fills.groupby("timestamp", sort=False)
    }
    balance = initial
    cursor = first

    def mark(mark_prices):
        active = sizes != 0.0
        if not active.any():
            return balance
        current = mark_prices[active]
        if not np.isfinite(current).all() or (current <= 0.0).any():
            raise ValueError("active position has invalid frozen mark price")
        signed_cost = sizes[active] * multipliers[active]
        return balance + float(np.dot(signed_cost, current - prices[active]))

    def adverse_prices(candle_index):
        active = sizes != 0.0
        values = hlcvs[candle_index, :, CLOSE_INDEX].copy()
        if active.any():
            values[active] = np.where(
                sizes[active] > 0.0,
                hlcvs[candle_index, active, LOW_INDEX],
                hlcvs[candle_index, active, HIGH_INDEX],
            )
        return values

    def fill_span(end):
        if end <= cursor:
            return
        active = sizes != 0.0
        local = slice(cursor - first, end - first)
        if not active.any():
            adverse_equity[local] = balance
            close_reconstructed[local] = balance
            return
        signed_cost = sizes[active] * multipliers[active]
        adverse_marks = np.where(
            sizes[active][None, :] > 0.0,
            hlcvs[cursor:end, active, LOW_INDEX],
            hlcvs[cursor:end, active, HIGH_INDEX],
        )
        close_marks = hlcvs[cursor:end, active, CLOSE_INDEX]
        if (
            not np.isfinite(adverse_marks).all()
            or not np.isfinite(close_marks).all()
            or (adverse_marks <= 0.0).any()
            or (close_marks <= 0.0).any()
        ):
            raise ValueError("active position has invalid frozen H/L/C mark")
        cost = float(np.dot(signed_cost, prices[active]))
        adverse_equity[local] = balance + adverse_marks @ signed_cost - cost
        close_reconstructed[local] = balance + close_marks @ signed_cost - cost

    for candle in range(first, last + 1):
        group = fill_groups.get(int(timestamps[candle]))
        if group is None:
            continue
        fill_span(candle)
        local = candle - first
        chosen = mark(adverse_prices(candle))
        for row in group.itertuples(index=False):
            index = coin_index[row.coin]
            sizes[index] = float(row.psize)
            prices[index] = float(row.pprice) if row.psize != 0.0 else 0.0
            balance = float(row.usd_total_balance)
            chosen = min(chosen, mark(adverse_prices(candle)))
        adverse_equity[local] = chosen
        close_reconstructed[local] = mark(hlcvs[candle, :, CLOSE_INDEX])
        cursor = candle + 1
    fill_span(last + 1)

    residual = float(
        np.max(np.abs(close_reconstructed - timeline["equity"].to_numpy()))
    )
    if residual > CLOSE_RECONCILIATION_TOLERANCE_USDT:
        raise ValueError(
            "fill-ledger position reconstruction does not reconcile with stored equity: "
            f"{residual}"
        )
    previous_close_hwm = np.maximum.accumulate(
        np.r_[initial, timeline["equity"].to_numpy(dtype=np.float64)[:-1]]
    )
    return (
        pd.DataFrame(
            {
                "adverse_hl_state_envelope_equity": adverse_equity,
                "adverse_hl_state_envelope_drawdown_pct": 1.0
                - adverse_equity / previous_close_hwm,
            },
            index=timeline.index,
        ),
        residual,
    )


def adverse_stress_monthly(timeline, stress, initial):
    previous_equity = initial
    rows = []
    months = timeline.index.tz_localize(None).to_period("M")
    for period, group in timeline.groupby(months, sort=True):
        values = stress.loc[group.index, "adverse_hl_state_envelope_equity"].to_numpy()
        hwm = np.maximum.accumulate(
            np.r_[previous_equity, group["equity"].to_numpy(dtype=np.float64)[:-1]]
        )
        local_dd = 1.0 - values / hwm
        worst = int(np.argmax(local_dd))
        global_worst = int(
            np.argmax(
                stress.loc[
                    group.index, "adverse_hl_state_envelope_drawdown_pct"
                ].to_numpy()
            )
        )
        rows.append(
            {
                "month": str(period),
                "lowest_adverse_hl_state_envelope_equity": float(values.min()),
                "worst_local_adverse_hl_state_envelope_drawdown_pct": float(
                    local_dd[worst]
                ),
                "worst_local_adverse_hl_state_envelope_time": group.index[
                    worst
                ].isoformat(),
                "worst_global_adverse_hl_state_envelope_drawdown_pct": float(
                    stress.loc[
                        group.index, "adverse_hl_state_envelope_drawdown_pct"
                    ].iloc[global_worst]
                ),
                "worst_global_adverse_hl_state_envelope_time": group.index[
                    global_worst
                ].isoformat(),
            }
        )
        previous_equity = float(group["equity"].iloc[-1])
    return pd.DataFrame(rows)


def position_snapshot(initial, fills, at_time, timestamps, hlcvs, coins, settings):
    positions = {}
    balance = initial
    for row in fills.loc[fills["timestamp"] <= at_time].itertuples(index=False):
        if row.psize == 0.0:
            positions.pop(row.coin, None)
        else:
            positions[row.coin] = (float(row.psize), float(row.pprice))
        balance = float(row.usd_total_balance)
    time_ms = int(at_time.value // 1_000_000)
    candle = int(np.searchsorted(timestamps, time_ms))
    if candle == len(timestamps) or int(timestamps[candle]) != time_ms:
        raise ValueError(f"cannot map snapshot time {at_time.isoformat()} to HLCV")
    coin_index = {coin: index for index, coin in enumerate(coins)}
    rows = []
    for coin, (psize, pprice) in sorted(positions.items()):
        index = coin_index[coin]
        close = float(hlcvs[candle, index, CLOSE_INDEX])
        adverse = float(
            hlcvs[candle, index, LOW_INDEX]
            if psize > 0.0
            else hlcvs[candle, index, HIGH_INDEX]
        )
        multiplier = float(settings[coin]["c_mult"])
        cost = abs(psize * pprice * multiplier)
        close_notional = abs(psize * close * multiplier)
        rows.append(
            {
                "coin": coin,
                "position_side": "long" if psize > 0.0 else "short",
                "psize": psize,
                "pprice": pprice,
                "close_mark_price": close,
                "adverse_hl_mark_price": adverse,
                "cost_basis_notional_usdt": cost,
                "close_mark_notional_usdt": close_notional,
                "close_unrealized_pnl_usdt": psize * multiplier * (close - pprice),
                "adverse_hl_unrealized_pnl_usdt": psize
                * multiplier
                * (adverse - pprice),
                "cost_basis_wallet_exposure": cost / balance,
            }
        )
    frame = pd.DataFrame(rows)
    return frame, {
        "timestamp": at_time.isoformat(),
        "balance": balance,
        "active_positions": len(frame),
        "cost_basis_notional_usdt": float(
            frame["cost_basis_notional_usdt"].sum() if len(frame) else 0.0
        ),
        "cost_basis_wallet_exposure": float(
            frame["cost_basis_wallet_exposure"].sum() if len(frame) else 0.0
        ),
        "close_unrealized_pnl_usdt": float(
            frame["close_unrealized_pnl_usdt"].sum() if len(frame) else 0.0
        ),
        "adverse_hl_unrealized_pnl_usdt": float(
            frame["adverse_hl_unrealized_pnl_usdt"].sum() if len(frame) else 0.0
        ),
    }


def fmt_usdt(value):
    if pd.isna(value):
        return "N/A"
    return f"{float(value):,.2f}"


def fmt_pct(value):
    if pd.isna(value):
        return "N/A"
    return f"{100.0 * float(value):.2f}%"


def markdown_table(frame, columns, headings):
    lines = [
        "| " + " | ".join(headings) + " |",
        "| " + " | ".join("---" for _ in headings) + " |",
    ]
    for row in frame.loc[:, columns].itertuples(index=False, name=None):
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(lines)


def strategy_mechanics(config):
    if config["live"]["strategy_kind"] == "ema_anchor":
        return (
            "EMA-anchor 在 EMA 带外以库存偏移的 bid/ask 报价；持仓越接近每侧有效 "
            "WEL 时，入场数量会按暴露比例提高。它不是止损趋势跟随策略，而是均值回归式 "
            "流动性报价，因此单边延续行情仍会累积库存风险。"
        )
    return (
        "Trailing martingale 在首次进场后按阈值/波动/现有 WEL 调整下一档限价补仓，"
        "并通过递归 close 阶梯或 trailing close 实现反弹利润。其收益依赖价格回归与"
        "订单完整成交；连续单边走势会把损失留在未实现 PnL 中。"
    )


def render_report(
    selection_manifest,
    ranking,
    selection_metrics,
    holdout_metrics,
    full_metrics,
    full_monthly,
    stress_summary,
    trough_snapshot,
    trough_aggregate,
):
    selected = selection_manifest["selected_candidate"]
    selection = selection_metrics.loc[
        selection_metrics["candidate"] == selected
    ].iloc[0]
    holdout = holdout_metrics.iloc[0]
    c1 = full_metrics.loc[full_metrics["scenario"] == "C1"].iloc[0]
    selected_config = read_json(Path(c1["result_dir"]) / "config.json")
    status = selection_manifest["selection_status"]
    if status == "eligible_pass":
        selection_text = (
            f"`{selected}` 在前两年选择窗口满足了 30% 分钟收盘权益 MDD 门槛，"
            "因此按预先锁定的风险优先级被选中。"
        )
    else:
        selection_text = (
            f"**没有现有公开配置满足 30% MDD 门槛。** `{selected}` 只是按预先锁定的"
            "回退规则选出的最低风险研究候选，不能称为达到目标的低回撤策略。"
        )

    selection_display = selection_metrics.copy()
    selection_display["mdd"] = selection_display["minute_close_equity_mdd_pct"].map(
        fmt_pct
    )
    selection_display["mdd_1pct"] = selection_display[
        "account_drawdown_worst_mean_1pct"
    ].map(fmt_pct)
    selection_display["positive_months"] = selection_display[
        "positive_month_fraction"
    ].map(fmt_pct)
    selection_display["median_monthly"] = selection_display[
        "median_monthly_equity_return"
    ].map(fmt_pct)
    selection_display["cagr"] = selection_display["equity_cagr"].map(fmt_pct)
    selection_display["return"] = selection_display["equity_return"].map(fmt_pct)
    selection_display["recovery"] = selection_display[
        "equity_mdd_peak_to_recovery_days"
    ].map(lambda value: "未恢复" if pd.isna(value) else f"{float(value):.1f} 天")
    selection_display["rank"] = selection_display["selection_rank"].map(
        lambda value: "—" if pd.isna(value) else str(int(value))
    )
    selection_display["btc_collateral"] = selection_display[
        "btc_collateral_cap"
    ].map(lambda value: "无" if float(value) == 0.0 else fmt_pct(value))
    selection_display["cash_ledger"] = selection_display[
        "realized_balance_available"
    ].map(
        lambda value: "可对账"
        if str(value).strip().lower() in ("true", "1")
        else "不适用"
    )

    full_display = full_metrics.copy()
    full_display["ending_equity_display"] = full_display["ending_equity"].map(fmt_usdt)
    full_display["mdd_display"] = full_display["minute_close_equity_mdd_pct"].map(
        fmt_pct
    )
    full_display["monthly_mdd_display"] = full_display[
        "worst_monthly_equity_mdd_pct"
    ].map(fmt_pct)
    full_display["recovery_display"] = full_display[
        "equity_mdd_peak_to_recovery_days"
    ].map(lambda value: "未恢复" if pd.isna(value) else f"{float(value):.1f} 天")
    full_display["fills_display"] = full_display["fills"].map(lambda value: f"{int(value):,}")

    monthly_display = full_monthly.loc[full_monthly["scenario"] == "C1"].copy()
    monthly_display["mdd"] = monthly_display["minute_close_equity_mdd_pct"].map(fmt_pct)
    monthly_display["balance_mdd"] = monthly_display["realized_balance_mdd_pct"].map(
        fmt_pct
    )
    monthly_display["return"] = monthly_display["monthly_equity_return"].map(fmt_pct)
    monthly_display["float_loss"] = monthly_display[
        "max_unrealized_loss_pct_of_balance"
    ].map(fmt_pct)
    monthly_display["end"] = monthly_display.apply(
        lambda row: f"{fmt_usdt(row['closing_balance'])} / {fmt_usdt(row['closing_equity'])}",
        axis=1,
    )

    position_display = trough_snapshot.copy()
    if position_display.empty:
        position_table = "_该分钟没有未平仓持仓。_"
    else:
        for column in (
            "psize",
            "pprice",
            "close_mark_price",
            "adverse_hl_mark_price",
            "cost_basis_notional_usdt",
            "close_mark_notional_usdt",
            "close_unrealized_pnl_usdt",
            "adverse_hl_unrealized_pnl_usdt",
            "cost_basis_wallet_exposure",
        ):
            position_display[column] = position_display[column].map(
                fmt_pct if column == "cost_basis_wallet_exposure" else fmt_usdt
            )
        position_table = markdown_table(
            position_display,
            [
                "coin",
                "position_side",
                "pprice",
                "close_mark_price",
                "cost_basis_notional_usdt",
                "close_unrealized_pnl_usdt",
                "cost_basis_wallet_exposure",
            ],
            [
                "币种",
                "方向",
                "均价",
                "收盘盯市价",
                "成本名义额",
                "收盘浮动 PnL",
                "成本敞口",
            ],
        )

    hsl = selected_config["bot"]["long"]["hsl"]
    hsl_summary = (
        f"long HSL={'开启' if hsl['enabled'] else '关闭'}，"
        f"RED 阈值={fmt_pct(hsl['red_threshold'])}，"
        f"panic close={hsl['panic_close_order_type']}，"
        f"信号范围={selected_config['live']['hsl_signal_mode']}。"
    )
    if stress_summary["available"]:
        stress_and_snapshot = f"""C1 的分钟收盘账户权益 MDD 为 {fmt_pct(c1['minute_close_equity_mdd_pct'])}：
从 {c1['equity_mdd_peak_time']} 的 {fmt_usdt(c1['equity_mdd_peak'])} USDT，
到 {c1['equity_mdd_trough_time']} 的 {fmt_usdt(c1['equity_mdd_trough'])} USDT。
谷底时按 fill 账本重建为 {trough_aggregate['active_positions']} 个持仓，建仓成本名义额
{fmt_usdt(trough_aggregate['cost_basis_notional_usdt'])} USDT（余额的
{fmt_pct(trough_aggregate['cost_basis_wallet_exposure'])}），收盘浮动 PnL
{fmt_usdt(trough_aggregate['close_unrealized_pnl_usdt'])} USDT。

{position_table}

将每个 long 同分钟标到 Low、每个 short 标到 High，并在每分钟起始与每笔 fill 后状态中取最差，
得到 **adverse H/L state envelope** 为 {fmt_pct(stress_summary['mdd_pct'])}，
发生在 {stress_summary['time']}，对应代理权益 {fmt_usdt(stress_summary['equity'])} USDT。
这故意是压力视图，不是完整行情路径：跨币种极值不一定同步，H/L 与成交的先后也无法从一分钟 candle
恢复；真实 order book/mark-price 仍可能更差或不同。"""
    else:
        stress_and_snapshot = f"""C1 的分钟收盘账户权益 MDD 为 {fmt_pct(c1['minute_close_equity_mdd_pct'])}：
从 {c1['equity_mdd_peak_time']} 的 {fmt_usdt(c1['equity_mdd_peak'])} USDT，
到 {c1['equity_mdd_trough_time']} 的 {fmt_usdt(c1['equity_mdd_trough'])} USDT。

未提供 H/L 状态压力代理，因为该配置使用 BTC 抵押（`btc_collateral_cap > 0`），而保存的分钟工件
没有足以重建每一分钟 BTC 现金转换状态的账本。为避免把缺失的抵押状态伪造成一个精确权益路径，本报告
只报告可直接复核的分钟收盘账户权益；这不是风险较低的证明。"""

    return f"""# 本地策略低回撤筛选与因果回测报告

## 决策

{selection_text}

本次没有调优、没有重写策略，也没有把不同币池的旧报告直接当成可比结果。所有候选均在同一份
冻结的 Binance 1 分钟 H/L/C/V 数据、同一 100,000 USDT 起始额、Binance 单交易所、因果 T+1
`close_first` 执行约定下重新筛选。选择只使用 2023-09-12 至 2025-09-11 的数据；选择锁定后才运行
2025-09-12 至 2026-09-11 的独立一年 holdout。

**这不是实盘推荐或收益保证。** “低回撤”主口径是逐分钟收盘的账户权益，不是已实现余额；OHLC
回放没有盘口队列、部分成交、真实 mark price、资金费率、保证金阶梯或强平细节。

### 抵押资产与账户权益口径

筛选使用每个 run 保存的逐分钟 `usd_total_equity` 重算 MDD、月收益与恢复期；它是回测模型的
USD 账户权益序列，并保留 BTC 抵押价值变化。部分公开示例启用了 `btc_collateral_cap`，其
`usd_total_balance` 即使显示为数值，也来自 fill 时点的余额快照并包含 BTC 抵押盯市，不能当成
逐分钟的已实现现金账本，也不能据此虚构“已实现余额 MDD”或浮亏/现金比例。

已在正 BTC 抵押 smoke 中复核到 `analysis.json` 的原生 USD drawdown 可显示 100%，而保存的
`usd_total_equity` 没有对应的 100% 下跌；因此原生 USD 字段**不参与**本研究的账户风险排名。
零 BTC 抵押配置仍要求保存时间线与原生 USD MDD 在序列化精度内一致；所有配置同时保留
`strategy_equity` 及其原生指标，以便将策略 PnL 与抵押资产价格风险分开审阅。

## 可用引擎与本地配置解读

当前仓库的可用策略引擎是 `trailing_martingale`、`ema_anchor` 和仅用于 V7 兼容的
`trailing_grid_v7`。公开示例只包含前两种；`suite_example.json` 是演示配置，不参与排名。

**选中引擎的交易机制：** {strategy_mechanics(selected_config)}

选中配置的风险档案：long `n_positions={selected_config['bot']['long']['risk']['n_positions']}`，
long TWE={selected_config['bot']['long']['risk']['total_wallet_exposure_limit']}，
short TWE={selected_config['bot']['short']['risk']['total_wallet_exposure_limit']}；
{hsl_summary} WEL/TWEL 与 slot 限制约束计划建仓规模，但它们不保证价格单边时的
mark-price 权益、强平距离或限价单实际成交。

## 两年选择窗口：完整候选排序

候选必须完成、未模拟强平、权益正收益并满足 MDD ≤ 30% 才算合格。若没有合格者，表中 rank=1
是预先声明的研究回退，而非风险目标达标认证。

{markdown_table(
    selection_display,
    [
        "rank",
        "candidate",
        "strategy_kind",
        "effective_coin_count",
        "btc_collateral",
        "cash_ledger",
        "mdd",
        "mdd_1pct",
        "positive_months",
        "median_monthly",
        "cagr",
        "return",
        "recovery",
        "eligibility",
    ],
    [
        "排名",
        "配置",
        "引擎",
        "有效币数",
        "BTC 抵押上限",
        "余额账本",
        "权益 MDD",
        "最差1%均值 DD",
        "正收益月占比",
        "月收益中位数",
        "年化权益增长",
        "累计权益收益",
        "峰值恢复",
        "门槛结果",
    ],
)}

缺失币种通过冻结数据的显式 intersection 处理并记录，不下载或虚构行情。`hsl_npos1.json`
名称虽然写 npos1，但实际 long `n_positions=10`；它不能被误读成单仓风险配置。
含 BTC 抵押的候选按其配置定义的 USD 账户组合（策略 PnL 加 BTC 抵押盯市）参与 MDD 排名，
而不是被错误地当作纯策略收益或可对账 USDT 现金余额。选中的 `ema_anchor` 没有 BTC 抵押，
故其后续余额、浮亏和 fill 账本可以逐项对账。

## 锁定后的独立一年 holdout

| 配置 | 期末权益 | 权益收益 | 分钟收盘权益 MDD | 正收益月占比 | 最差月内 MDD | 模拟强平 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| {selected} | {fmt_usdt(holdout['ending_equity'])} | {fmt_pct(holdout['equity_return'])} | {fmt_pct(holdout['minute_close_equity_mdd_pct'])} | {fmt_pct(holdout['positive_month_fraction'])} | {fmt_pct(holdout['worst_monthly_equity_mdd_pct'])} | {holdout['liquidated']} |

这一年没有参与候选选择，因此比全样本结果更适合检验“选择窗口内看起来较稳”的结论。不过它仍只是一次
历史样本，不能证明未来稳定性，也不能改写在选择阶段锁定的决定。**关键反证：该独立一年 MDD 为
{fmt_pct(holdout['minute_close_equity_mdd_pct'])}，已超过 30% 门槛。** 因此该选择只说明前两年
样本内风险最低，不能被表述为跨样本、可部署的“30% MDD 策略”。

## 选中配置的完整三年执行敏感性

| 场景 | 期末权益 | 分钟收盘权益 MDD | 最差月内 MDD | 峰值恢复 | fills | 模拟强平 |
| --- | ---: | ---: | ---: | --- | ---: | --- |
{markdown_table(
    full_display,
    [
        "scenario",
        "ending_equity_display",
        "mdd_display",
        "monthly_mdd_display",
        "recovery_display",
        "fills_display",
        "liquidated",
    ],
    ["场景", "期末权益", "分钟收盘权益 MDD", "最差月内 MDD", "峰值恢复", "fills", "模拟强平"],
).split(chr(10), 2)[2]}

完整 C1 的 MDD 为 {fmt_pct(c1['minute_close_equity_mdd_pct'])}，同样超过选择门槛；C3/C4 的
额外一根 bar 延迟进一步扩大到约 48%。这说明名义 T+1 下的较平滑结果对执行生命周期延迟敏感，
而不是一个稳健的低回撤承诺。

- C1 是名义因果 T+1 `close_first`；C2 只改变同 candle 的 entry/close 处理顺序。
- C3/C4 增加一个完整 bar 的创建、撤销、替换生效延迟（T+2 压力约定）。
- 四个数都不是真实交易所成交的上下界；差异只量化该 OHLC 执行模型对时序假设的敏感性。

## C1 三年逐月账户风险

下表的“余额 MDD”只反映已实现余额，不能代替期货账户风险；“权益 MDD”才包含每分钟收盘未实现
PnL。期末列为余额 / 分钟收盘权益。

{markdown_table(
    monthly_display,
    ["month", "return", "balance_mdd", "mdd", "float_loss", "end"],
    ["月份", "权益月收益", "余额 MDD", "月内权益 MDD", "最大浮亏/余额", "月末余额 / 权益"],
)}

## 最大回撤仓位与 OHLC 压力代理

{stress_and_snapshot}

## 实盘风险边界

1. 该筛选降低的是历史样本中的模型内权益波动，不是对未来 MDD 的上限承诺。
2. HSL 即使开启，仍取决于信号范围、订单受理、panic close 类型、可用流动性和价格路径；limit
   panic close 不保证立即退出，market panic close 则带来实际滑点与 taker 成本。
3. 多币种压力事件中相关性会抬升；slot/WEL/TWEL 不能阻止已持仓同时向不利方向运动。
4. 回测的 maker 穿价成交不代表订单真实排队、全额成交或费用/过滤器持续不变。实盘必须以账户
   mark-price、维持保证金、资金费、订单状态和风险阈值监控为准。

## 产物

- `candidate_inventory.csv`：公开候选和有效币池/风险开关。
- `selection_candidate_metrics.csv`、`selection_ranking.csv`、`selection_manifest.json`：
  选择证据与锁定规则。
- `holdout_metrics.csv`、`selected_full_execution_sensitivity.csv`：
  一年 holdout 与完整三年 C1–C4。
- `selected_full_monthly_account_metrics.csv`、`selected_c1_adverse_hl_stress.csv`：
  每月账户回撤与明确标记的 H/L 压力代理。
- 每个 run 目录中的原始 fills、analysis、分钟权益、有效配置和 execution audit：
  可用于逐笔复核。
"""


def write_report():
    required = [
        STUDY / "selection_manifest.json",
        STUDY / "selection_candidate_metrics.csv",
        STUDY / "selection_ranking.csv",
    ]
    for path in required:
        if not path.exists():
            raise FileNotFoundError(f"required selection artifact missing: {path}")
    selection_manifest = read_json(STUDY / "selection_manifest.json")
    selected = selection_manifest["selected_candidate"]
    holdout_identity = load_identity("holdout", selected, "C1")
    full_identities = [load_identity("full", selected, scenario) for scenario in SCENARIOS]

    holdout_record, holdout_monthly = run_metrics(holdout_identity)
    full_records = []
    full_monthly = []
    full_loaded = {}
    for identity in full_identities:
        record, monthly = run_metrics(identity)
        full_records.append(record)
        full_monthly.append(monthly)
        full_loaded[identity["scenario"]] = load_run(identity)
    holdout_metrics = pd.DataFrame([holdout_record])
    full_metrics = pd.DataFrame(full_records).sort_values("scenario")
    full_monthly = pd.concat(full_monthly, ignore_index=True)
    write_csv_new(STUDY / "holdout_metrics.csv", holdout_metrics)
    write_csv_new(STUDY / "holdout_monthly_account_metrics.csv", holdout_monthly)
    write_csv_new(STUDY / "selected_full_execution_sensitivity.csv", full_metrics)
    write_csv_new(STUDY / "selected_full_monthly_account_metrics.csv", full_monthly)

    c1_config, _, c1_timeline, c1_fills = full_loaded["C1"]
    c1_initial = float(c1_config["backtest"]["starting_balance"])
    btc_collateral_cap = float(c1_config["backtest"]["btc_collateral_cap"])
    if btc_collateral_cap == 0.0:
        timestamps, hlcvs, coins, settings = load_market_data()
        stress, residual = calculate_adverse_hl_state_envelope(
            c1_initial, c1_timeline, c1_fills, timestamps, hlcvs, coins, settings
        )
        stress_monthly = adverse_stress_monthly(c1_timeline, stress, c1_initial)
        stress_worst = stress["adverse_hl_state_envelope_drawdown_pct"].idxmax()
        stress_summary = {
            "available": True,
            "mdd_pct": float(
                stress.loc[stress_worst, "adverse_hl_state_envelope_drawdown_pct"]
            ),
            "time": stress_worst.isoformat(),
            "equity": float(
                stress.loc[stress_worst, "adverse_hl_state_envelope_equity"]
            ),
            "close_reconstruction_max_abs_residual_usdt": residual,
        }
        c1_row = full_metrics.loc[full_metrics["scenario"] == "C1"].iloc[0]
        trough_time = pd.Timestamp(c1_row["equity_mdd_trough_time"])
        trough_snapshot, trough_aggregate = position_snapshot(
            c1_initial,
            c1_fills,
            trough_time,
            timestamps,
            hlcvs,
            coins,
            settings,
        )
    else:
        stress_monthly = pd.DataFrame(
            [
                {
                    "status": "not_available",
                    "reason": (
                        "BTC collateral is enabled, but the persisted minute artifacts "
                        "do not contain sufficient BTC-cash conversion state to "
                        "reconstruct an adverse H/L account-equity envelope."
                    ),
                }
            ]
        )
        stress_summary = {
            "available": False,
            "reason": stress_monthly.loc[0, "reason"],
            "btc_collateral_cap": btc_collateral_cap,
        }
        trough_snapshot = pd.DataFrame()
        trough_aggregate = {
            "active_positions": 0,
            "cost_basis_notional_usdt": np.nan,
            "cost_basis_wallet_exposure": np.nan,
            "close_unrealized_pnl_usdt": np.nan,
        }
    write_csv_new(STUDY / "selected_c1_adverse_hl_stress.csv", stress_monthly)
    write_json_new(STUDY / "selected_c1_adverse_hl_stress_summary.json", stress_summary)
    write_csv_new(STUDY / "selected_c1_positions_at_equity_mdd_trough.csv", trough_snapshot)
    write_json_new(
        STUDY / "selected_c1_positions_at_equity_mdd_trough_summary.json",
        trough_aggregate,
    )

    ranking = pd.read_csv(STUDY / "selection_ranking.csv")
    selection_metrics = pd.read_csv(STUDY / "selection_candidate_metrics.csv")
    report = render_report(
        selection_manifest,
        ranking,
        selection_metrics,
        holdout_metrics,
        full_metrics,
        full_monthly,
        stress_summary,
        trough_snapshot,
        trough_aggregate,
    )
    write_text_new(STUDY / "low_drawdown_strategy_report.md", report)
    print(f"wrote study report at {STUDY / 'low_drawdown_strategy_report.md'}")


def validate_execution_audit(identity, fills):
    audit_path = Path(identity["execution_audit_path"])
    if not audit_path.exists():
        raise FileNotFoundError(f"missing execution audit: {audit_path}")
    audit = pd.read_csv(audit_path)
    if len(audit) != len(fills):
        raise ValueError(
            f"{identity['stage']}/{identity['candidate']}/{identity['scenario']}: "
            f"audit rows {len(audit)} != fills {len(fills)}"
        )
    fill_timestamps = (
        pd.to_datetime(fills["timestamp"], utc=True).astype("int64") // 1_000_000
    ).to_numpy()
    checks = {
        "fill_index": audit["fill_index"].to_numpy() == fills["index"].to_numpy(),
        "timestamp": audit["fill_candle_open_timestamp_ms"].to_numpy()
        == fill_timestamps,
        "symbol": audit["symbol"].to_numpy() == fills["coin"].to_numpy(),
        "quantity": np.isclose(
            audit["fill_qty"].to_numpy(),
            fills["qty"].to_numpy(),
            rtol=0.0,
            atol=1e-12,
        ),
        "price": np.isclose(
            audit["fill_price"].to_numpy(),
            fills["price"].to_numpy(),
            rtol=0.0,
            atol=1e-12,
        ),
    }
    failed = [key for key, values in checks.items() if not np.all(values)]
    if failed:
        raise ValueError(
            f"{identity['stage']}/{identity['candidate']}/{identity['scenario']}: "
            f"execution-audit mismatch {failed}"
        )
    return {
        "audit_rows": len(audit),
        "audit_unique_order_ids": int(audit["order_id"].nunique()),
        "audit_duplicate_order_ids": int(
            len(audit) - audit["order_id"].nunique()
        ),
    }


def validate():
    if (STUDY / "study_validation.json").exists():
        raise FileExistsError("validation output already exists; refusing to overwrite it")
    selection_manifest = read_json(STUDY / "selection_manifest.json")
    selected = selection_manifest["selected_candidate"]
    identities = selection_replays() + [
        load_identity("holdout", selected, "C1"),
        *(load_identity("full", selected, scenario) for scenario in SCENARIOS),
    ]
    manifest = read_json(STUDY / "study_manifest.json")
    output = {}
    for identity in identities:
        config, analysis, timeline, fills = load_run(identity)
        record, monthly = run_metrics(identity)
        if not identity["network_disabled"]:
            raise ValueError("a study run did not assert network isolation")
        if identity["dataset_manifest_sha256"] != manifest["dataset_manifest_sha256"]:
            raise ValueError("a study run used a mismatched frozen dataset manifest")
        if identity["runtime"] != manifest["runtime"]:
            raise ValueError("a study run used a mismatched Rust runtime identity")
        strategy_mdd = drawdown_details(
            np.r_[
                float(config["backtest"]["starting_balance"]),
                timeline["strategy_equity"].to_numpy(),
            ],
            pd.DatetimeIndex(
                [timeline.index[0] - pd.Timedelta(minutes=1)]
            ).append(timeline.index),
        )["mdd_pct"]
        if not np.isclose(
            strategy_mdd,
            float(analysis["drawdown_worst_strategy_eq"]),
            rtol=0.0,
            atol=TIMELINE_MDD_RECONCILIATION_TOLERANCE,
        ):
            raise ValueError(
                "recomputed strategy-equity MDD disagrees with native strategy analysis"
            )
        balance_available = has_reconciliable_usd_cash_balance(config, timeline)
        if balance_available:
            expected_balance = float(config["backtest"]["starting_balance"]) + float(
                (fills["pnl"] + fills["fee_paid"]).sum()
            )
            balance_residual = float(timeline["balance"].iloc[-1] - expected_balance)
            if abs(balance_residual) > BALANCE_RECONCILIATION_TOLERANCE_USDT:
                raise ValueError("saved final balance disagrees materially with fill ledger")
            if not np.allclose(
                monthly["opening_balance"].iloc[1:],
                monthly["closing_balance"].iloc[:-1],
                rtol=0.0,
                atol=EPSILON,
            ):
                raise ValueError("monthly realized-balance values are not continuous")
        else:
            balance_residual = np.nan
        if not np.allclose(
            monthly["opening_equity"].iloc[1:],
            monthly["closing_equity"].iloc[:-1],
            rtol=0.0,
            atol=EPSILON,
        ):
            raise ValueError("monthly account-equity values are not continuous")
        key = f"{identity['stage']}/{identity['candidate']}/{identity['scenario']}"
        btc_collateral_cap = float(config["backtest"]["btc_collateral_cap"])
        if btc_collateral_cap == 0.0 and not np.isclose(
            record["minute_close_equity_mdd_pct"],
            float(analysis["drawdown_worst_usd"]),
            rtol=0.0,
            atol=TIMELINE_MDD_RECONCILIATION_TOLERANCE,
        ):
            raise ValueError(
                "recomputed minute-close account MDD disagrees with native USD analysis"
            )
        output[key] = {
            "native_drawdown_worst_usd": float(analysis["drawdown_worst_usd"]),
            "recomputed_minute_close_equity_mdd": record[
                "minute_close_equity_mdd_pct"
            ],
            "native_drawdown_worst_strategy_eq": float(
                analysis["drawdown_worst_strategy_eq"]
            ),
            "recomputed_strategy_equity_mdd": strategy_mdd,
            "btc_collateral_cap": btc_collateral_cap,
            "native_usd_mdd_comparison": (
                "matched"
                if btc_collateral_cap == 0.0
                else "not_used_for_account_risk_selection"
            ),
            "ending_balance_ledger_residual_usdt": (
                balance_residual if balance_available else None
            ),
            "realized_balance_available": balance_available,
            "monthly_opening_closing_continuity": True,
            **validate_execution_audit(identity, fills),
        }
    write_json_new(STUDY / "study_validation.json", output)
    print(f"validated {len(output)} offline study replays")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=("init", "replay", "select", "report", "validate")
    )
    parser.add_argument("--stage", choices=tuple(WINDOWS))
    parser.add_argument("--candidate", choices=tuple(CANDIDATES))
    parser.add_argument("--scenario", choices=tuple(SCENARIOS))
    args = parser.parse_args()

    if args.action == "init":
        if args.stage or args.candidate or args.scenario:
            parser.error("init does not accept stage, candidate, or scenario")
        initialize()
        return
    if args.action == "select":
        if args.stage or args.candidate or args.scenario:
            parser.error("select does not accept stage, candidate, or scenario")
        select_candidate()
        return
    if args.action == "report":
        if args.stage or args.candidate or args.scenario:
            parser.error("report does not accept stage, candidate, or scenario")
        write_report()
        return
    if args.action == "validate":
        if args.stage or args.candidate or args.scenario:
            parser.error("validate does not accept stage, candidate, or scenario")
        validate()
        return
    if not (args.stage and args.candidate and args.scenario):
        parser.error("replay requires --stage, --candidate, and --scenario")
    asyncio.run(replay(args.stage, args.candidate, args.scenario))


if __name__ == "__main__":
    main()
