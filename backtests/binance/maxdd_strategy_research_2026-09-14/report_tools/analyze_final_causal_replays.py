#!/usr/bin/env python3
"""Independently audit locked final causal replays and render their report.

The script is deliberately offline.  It reads the frozen local H/L/C/V cache,
the locked final candidate, and the completed replay artifacts.  It never
starts a bot, accesses credentials or an exchange account, or changes a
selection parameter after the final holdout was opened.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
from typing import Any

import numpy as np
import pandas as pd


REPO = Path(__file__).resolve().parents[4]
STUDY = Path(__file__).resolve().parents[1]
REPLAYS = STUDY / "final_replays"
HOLDOUT = REPLAYS / "holdout"
OUTPUT = REPLAYS / "analysis"
REPLAY_TOOL = Path(__file__).with_name("run_final_causal_replays.py")
RISK_TOOL = (
    REPO
    / "backtests/binance/causal_comparison_2026-09-14/report_tools"
    / "run_low_drawdown_strategy_study.py"
)

PRIMARY_TRACKS = ("E-L", "B-L")
CORE_TRACKS = ("E-L", "E-S", "B-L")
PAIRWISE_INTRABAR_COMPARISONS = (
    ("C1_e_l", "C2_e_l"),
    ("C1_e_s", "C2_e_s"),
    ("C1_b_l", "C2_b_l"),
    ("C3_e_l", "C4_e_l"),
    ("C3_e_s", "C4_e_s"),
    ("C3_b_l", "C4_b_l"),
)
LATENCY_COMPARISONS = (
    ("E-L", "C1_e_l", "C3_e_l"),
    ("E-S", "C1_e_s", "C3_e_s"),
    ("B-L", "C1_b_l", "C3_b_l"),
)
MDD_TOLERANCE = 1.0e-8
BALANCE_TOLERANCE_USDT = 1.0e-6


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_default(value: Any) -> Any:
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(
        frame.to_json(orient="records", date_format="iso", double_precision=15)
    )


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def write_file(path: Path, payload: str, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        if path.read_text(encoding="utf-8") == payload:
            return
        raise RuntimeError(
            f"refusing to replace derived artifact without --replace-derived: {path}"
        )
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, value: Any, *, replace: bool) -> None:
    write_file(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
            default=json_default,
        )
        + "\n",
        replace=replace,
    )


def write_csv(path: Path, frame: pd.DataFrame, *, replace: bool) -> None:
    payload = frame.to_csv(index=False)
    write_file(path, payload, replace=replace)


def write_lock(path: Path, value: Any) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=json_default,
    ) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise RuntimeError(f"refusing to replace final decision lock: {path}")
        return
    write_file(path, payload, replace=False)


def reject_network(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("network is forbidden during final replay analysis")


def disable_network() -> None:
    socket.socket.connect = reject_network
    socket.socket.connect_ex = reject_network
    socket.create_connection = reject_network


def scenario_name(execution: str, track: str) -> str:
    return f"{execution}_{track.lower().replace('-', '_')}"


def require_within(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(parent.resolve())
    except ValueError as exc:
        raise RuntimeError(f"artifact path escapes study output: {path}") from exc
    return resolved


def fill_ledger_balance(
    timeline_index: pd.DatetimeIndex, fills: pd.DataFrame, initial: float
) -> pd.Series:
    """Carry balances forward only from prior or same-minute fills."""

    changes = fills.groupby("timestamp", sort=True)["usd_total_balance"].last()
    combined_index = timeline_index.union(changes.index).sort_values()
    values = changes.reindex(combined_index).ffill().reindex(timeline_index)
    return values.fillna(initial).astype(float)


def mdd_record(
    risk: Any, values: np.ndarray, index: pd.DatetimeIndex, initial: float
) -> dict[str, Any]:
    labels = pd.DatetimeIndex([index[0] - pd.Timedelta(minutes=1)]).append(index)
    detail = risk.drawdown_details(np.r_[initial, values], labels)
    return {
        "mdd_pct": float(detail["mdd_pct"]),
        "peak_equity_usdt": float(detail["peak"]),
        "peak_time": detail["peak_time"].isoformat(),
        "trough_equity_usdt": float(detail["trough"]),
        "trough_time": detail["trough_time"].isoformat(),
        "recovery_time": (
            detail["recovery_time"].isoformat()
            if detail["recovery_time"] is not None
            else None
        ),
        "peak_to_recovery_days": detail["peak_to_recovery_days"],
    }


def track_eligibility(tool: Any, track: str, analysis: dict[str, Any]) -> dict[str, Any]:
    if track not in tool.ACTIVITY_FLOORS:
        return {
            "selection_scope": "diagnostic_only",
            "eligibility_passed": None,
            "activity_passed": None,
            "eligibility_failure_reasons": "not_a_selection_track",
        }
    failures: list[str] = []
    checks = {
        "mdd": float(analysis["drawdown_worst_strategy_eq"]) <= tool.MAXDD_CAP,
        "completion": float(analysis["backtest_completion_ratio"])
        >= tool.COMPLETION_FLOOR,
        "adg": float(analysis["adg_strategy_eq"]) >= tool.ADG_FLOOR,
        "gain": float(analysis["gain_strategy_eq"]) > 1.0,
        "not_liquidated": not bool(analysis["liquidated"]),
    }
    for name, passed in checks.items():
        if not passed:
            failures.append(name)
    activity_failures = []
    for metric, floor in tool.ACTIVITY_FLOORS[track].items():
        if float(analysis[metric]) < floor:
            activity_failures.append(metric)
    if activity_failures:
        failures.extend(f"activity:{metric}" for metric in activity_failures)
    return {
        "selection_scope": "primary" if track in PRIMARY_TRACKS else "directional_diagnostic",
        "eligibility_passed": not failures,
        "activity_passed": not activity_failures,
        "eligibility_failure_reasons": ",".join(failures) if failures else "",
    }


def load_postprocessed_balance_difference(
    result_dir: Path, timeline: pd.DataFrame
) -> dict[str, float]:
    path = result_dir / "balance_and_equity.csv.gz"
    with gzip.open(path, "rt") as file:
        persisted = pd.read_csv(file, index_col=0)
    persisted.index = pd.to_datetime(persisted.index, utc=True)
    if not persisted.index.equals(timeline.index):
        raise AssertionError(f"postprocessed timeline does not match raw equity: {path}")
    persisted_balance = pd.to_numeric(
        persisted["usd_total_balance"], errors="raise"
    ).to_numpy(dtype=float)
    ledger_balance = timeline["balance"].to_numpy(dtype=float)
    difference = persisted_balance - ledger_balance
    first_fill_time = timeline.attrs["first_fill_time"]
    prefill = timeline.index < first_fill_time if first_fill_time else np.zeros(
        len(timeline), dtype=bool
    )
    return {
        "postprocessed_balance_max_abs_difference_usdt": float(
            np.max(np.abs(difference))
        ),
        "postprocessed_prefill_balance_max_abs_difference_usdt": float(
            np.max(np.abs(difference[prefill])) if np.any(prefill) else 0.0
        ),
        "postprocessed_final_balance_difference_usdt": float(difference[-1]),
    }


def read_run(
    *,
    risk: Any,
    tool: Any,
    scenario: str,
    expected_provenance: dict[str, str],
    expected_runtime: dict[str, Any],
    expected_dataset_manifest_sha256: str,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, Any]]:
    scenario_root = HOLDOUT / scenario
    identity_path = scenario_root / "run_identity.json"
    identity = read_json(identity_path)
    if identity.get("scenario") != scenario:
        raise AssertionError(f"scenario identity mismatch: {identity_path}")
    if identity.get("provenance") != expected_provenance:
        raise AssertionError(f"stale lock provenance in {identity_path}")
    if identity.get("runtime") != expected_runtime:
        raise AssertionError(f"runtime identity mismatch in {identity_path}")
    if identity.get("dataset_manifest_sha256") != expected_dataset_manifest_sha256:
        raise AssertionError(f"dataset manifest mismatch in {identity_path}")
    result_dir = require_within(Path(identity["result_dir"]), HOLDOUT)
    config = read_json(result_dir / "config.json")
    analysis = read_json(result_dir / "analysis.json")
    if float(config["backtest"]["btc_collateral_cap"]) != 0.0:
        raise AssertionError(f"{scenario}: USD account comparison requires no BTC collateral")
    initial = float(config["backtest"]["starting_balance"])
    if not np.isfinite(initial) or initial <= 0.0:
        raise AssertionError(f"{scenario}: invalid starting balance")

    raw_equity = np.load(scenario_root / "minute_equity.npy", mmap_mode="r")
    if raw_equity.ndim != 2 or raw_equity.shape[1] < 4:
        raise AssertionError(f"{scenario}: invalid minute equity artifact")
    timestamps = raw_equity[:, 0].astype(np.int64)
    if not len(timestamps) or not np.all(np.diff(timestamps) == 60_000):
        raise AssertionError(f"{scenario}: minute equity timeline is not contiguous")
    equity = raw_equity[:, 1].astype(float)
    strategy_equity = raw_equity[:, 3].astype(float)
    if (
        not np.isfinite(equity).all()
        or not np.isfinite(strategy_equity).all()
        or (equity <= 0.0).any()
        or (strategy_equity <= 0.0).any()
    ):
        raise AssertionError(f"{scenario}: nonpositive or nonfinite equity")
    strategy_equity_difference = float(np.max(np.abs(equity - strategy_equity)))
    if strategy_equity_difference > BALANCE_TOLERANCE_USDT:
        raise AssertionError(
            f"{scenario}: account and strategy equity differ with BTC collateral disabled"
        )
    index = pd.to_datetime(timestamps, unit="ms", utc=True)
    prepared_shape = identity.get("prepared_shape")
    if (
        not isinstance(prepared_shape, list)
        or len(prepared_shape) != 3
        or prepared_shape[0] < len(raw_equity)
        or prepared_shape[1] != len(identity["prepared_coins"])
        or prepared_shape[2] != 4
    ):
        raise AssertionError(f"{scenario}: invalid prepared H/L/C/V shape")

    fills = pd.read_csv(result_dir / "fills.csv")
    fills = fills.loc[:, ~fills.columns.str.startswith("Unnamed:")].copy()
    required_fill_columns = {
        "index",
        "timestamp",
        "coin",
        "pnl",
        "fee_paid",
        "usd_total_balance",
        "qty",
        "price",
        "psize",
        "pprice",
        "type",
        "liquidity",
        "wallet_exposure",
        "twe_long",
        "twe_short",
    }
    missing = sorted(required_fill_columns - set(fills.columns))
    if missing:
        raise AssertionError(f"{scenario}: fill ledger misses {missing}")
    fills["timestamp"] = pd.to_datetime(fills["timestamp"], utc=True)
    fills = fills.sort_values(["timestamp", "index"], kind="stable").reset_index(
        drop=True
    )
    if fills.empty:
        raise AssertionError(f"{scenario}: expected nonempty final replay fills")
    if not fills["timestamp"].is_monotonic_increasing:
        raise AssertionError(f"{scenario}: fill ledger is not time-ordered")
    numeric_columns = [
        "pnl",
        "fee_paid",
        "usd_total_balance",
        "qty",
        "price",
        "psize",
        "pprice",
        "wallet_exposure",
        "twe_long",
        "twe_short",
    ]
    fills[numeric_columns] = fills[numeric_columns].apply(
        pd.to_numeric, errors="raise"
    )
    timeline = pd.DataFrame(
        {
            "equity": equity,
            "strategy_equity": strategy_equity,
        },
        index=index,
    )
    timeline.index.name = "timestamp"
    timeline["balance"] = fill_ledger_balance(index, fills, initial)
    timeline.attrs["first_fill_time"] = fills["timestamp"].iloc[0]

    expected_final_balance = initial + float((fills["pnl"] + fills["fee_paid"]).sum())
    final_balance = float(timeline["balance"].iloc[-1])
    if abs(final_balance - expected_final_balance) > BALANCE_TOLERANCE_USDT:
        raise AssertionError(
            f"{scenario}: fill ledger does not reconcile cash balance "
            f"{final_balance} vs {expected_final_balance}"
        )
    mdd = mdd_record(risk, equity, index, initial)
    native_mdd = float(analysis["drawdown_worst_strategy_eq"])
    if abs(mdd["mdd_pct"] - native_mdd) > MDD_TOLERANCE:
        raise AssertionError(
            f"{scenario}: minute-close MDD mismatch {mdd['mdd_pct']} vs {native_mdd}"
        )
    native_daily_1pct = float(analysis["drawdown_worst_mean_1pct_strategy_eq"])
    recomputed_daily_1pct = float(
        risk.daily_worst_drawdown_mean_1pct(timeline["equity"], initial)
    )
    if abs(native_daily_1pct - recomputed_daily_1pct) > MDD_TOLERANCE:
        raise AssertionError(
            f"{scenario}: daily 1% MDD mismatch "
            f"{recomputed_daily_1pct} vs {native_daily_1pct}"
        )
    monthly = risk.monthly_account_metrics(
        timeline,
        initial,
        "locked_final",
        "final_holdout",
        scenario,
        True,
    )
    for column, value in (
        ("scenario", scenario),
        ("execution", identity["execution"]),
        ("track", identity["track"]),
    ):
        if column in monthly:
            monthly[column] = value
        else:
            monthly.insert(0, column, value)
    equity_balance_gap = timeline["balance"] - timeline["equity"]
    eligibility = track_eligibility(tool, identity["track"], analysis)
    summary = {
        "scenario": scenario,
        "execution": identity["execution"],
        "track": identity["track"],
        "prepared_coin_count": len(identity["prepared_coins"]),
        "prepared_coins": ",".join(identity["prepared_coins"]),
        "traded_coin_count": int(fills["coin"].nunique()),
        "traded_coins": ",".join(sorted(fills["coin"].unique())),
        "first_equity_time": index[0].isoformat(),
        "last_equity_time": index[-1].isoformat(),
        "duration_days": (
            (index[-1] - index[0]).total_seconds() / 86_400.0
        ),
        "starting_balance_usdt": initial,
        "final_balance_usdt": final_balance,
        "final_equity_usdt": float(timeline["equity"].iloc[-1]),
        "terminal_account_equity_return": float(
            timeline["equity"].iloc[-1] / initial - 1.0
        ),
        "terminal_realized_balance_return": float(final_balance / initial - 1.0),
        "final_unrealized_pnl_usdt": float(
            timeline["equity"].iloc[-1] - final_balance
        ),
        "max_unrealized_loss_usdt": float(equity_balance_gap.max()),
        "max_unrealized_loss_pct_of_balance": float(
            (equity_balance_gap / timeline["balance"]).max()
        ),
        "native_gain_strategy_eq_smoothed": float(analysis["gain_strategy_eq"]),
        "native_adg_strategy_eq_smoothed": float(analysis["adg_strategy_eq"]),
        "native_gain_minus_terminal_account_return": float(
            analysis["gain_strategy_eq"] - timeline["equity"].iloc[-1] / initial
        ),
        "native_mdd_pct": native_mdd,
        "recomputed_minute_close_mdd_pct": mdd["mdd_pct"],
        "native_daily_1pct_mdd_pct": native_daily_1pct,
        "recomputed_daily_1pct_mdd_pct": recomputed_daily_1pct,
        "mdd_reconciliation_absolute_difference": abs(mdd["mdd_pct"] - native_mdd),
        "mdd_peak_equity_usdt": mdd["peak_equity_usdt"],
        "mdd_peak_time": mdd["peak_time"],
        "mdd_trough_equity_usdt": mdd["trough_equity_usdt"],
        "mdd_trough_time": mdd["trough_time"],
        "mdd_recovery_time": mdd["recovery_time"],
        "mdd_peak_to_recovery_days": mdd["peak_to_recovery_days"],
        "fills": int(len(fills)),
        "entries": int(fills["type"].str.startswith("entry_").sum()),
        "closes": int(fills["type"].str.startswith("close_").sum()),
        "maker_fills": int((fills["liquidity"] == "maker").sum()),
        "taker_fills": int((fills["liquidity"] == "taker").sum()),
        "close_grid_fills": int(fills["type"].str.startswith("close_grid").sum()),
        "close_trailing_fills": int(
            fills["type"].str.startswith("close_trailing").sum()
        ),
        "risk_reducer_close_fills": int(
            fills["type"]
            .str.startswith(("close_unstuck", "close_auto_reduce", "close_panic"))
            .sum()
        ),
        "native_fills_per_day": float(analysis["fills_per_day"]),
        "native_close_fills_per_day": float(analysis["fills_per_day_close"]),
        "native_active_days_ratio": float(analysis["fills_active_days_ratio"]),
        "native_total_wallet_exposure_max": float(
            analysis["total_wallet_exposure_max"]
        ),
        "native_total_wallet_exposure_mean": float(
            analysis["total_wallet_exposure_mean"]
        ),
        "max_recorded_twe_long": float(fills["twe_long"].max()),
        "max_recorded_twe_short": float(fills["twe_short"].max()),
        "native_position_held_days_max": float(analysis["position_held_days_max"]),
        "hard_stop_triggers": int(analysis["hard_stop_triggers"]),
        "hard_stop_restarts": int(analysis["hard_stop_restarts"]),
        "backtest_completion_ratio": float(analysis["backtest_completion_ratio"]),
        "liquidated": bool(analysis["liquidated"]),
        "account_strategy_equity_max_abs_difference_usdt": strategy_equity_difference,
        **eligibility,
        **load_postprocessed_balance_difference(result_dir, timeline),
    }
    return summary, monthly, timeline, fills, identity


def audit_execution(
    *,
    scenario: str,
    identity: dict[str, Any],
    fills: pd.DataFrame,
    timestamps: np.ndarray,
    hlcvs: np.ndarray,
    coins: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    audit = pd.read_csv(HOLDOUT / scenario / "execution_boundary_audit.csv")
    if len(audit) != len(fills):
        raise AssertionError(
            f"{scenario}: audit rows {len(audit)} != fill rows {len(fills)}"
        )
    checks = {
        "fill_index": audit["fill_index"].to_numpy(dtype=int)
        == fills["index"].to_numpy(dtype=int),
        "symbol": audit["symbol"].to_numpy() == fills["coin"].to_numpy(),
        "pside": audit["pside"].to_numpy()
        == fills["type"].str.rsplit("_", n=1).str[-1].to_numpy(),
        "order_type": audit["order_type"].to_numpy() == fills["type"].to_numpy(),
        "fill_qty": np.isclose(
            audit["fill_qty"].to_numpy(dtype=float),
            fills["qty"].to_numpy(dtype=float),
            rtol=0.0,
            atol=1.0e-12,
        ),
        "fill_price": np.isclose(
            audit["fill_price"].to_numpy(dtype=float),
            fills["price"].to_numpy(dtype=float),
            rtol=0.0,
            atol=1.0e-12,
        ),
    }
    failed = [name for name, passed in checks.items() if not passed.all()]
    if failed:
        raise AssertionError(f"{scenario}: execution audit mismatch: {failed}")
    expected_delay = int(identity["execution"] in {"C3", "C4"}) + 1
    decision = audit["decision_index"].to_numpy(dtype=np.int64)
    activation = audit["activation_index"].to_numpy(dtype=np.int64)
    fill_index = audit["fill_index"].to_numpy(dtype=np.int64)
    if (
        (decision < 0).any()
        or (activation >= len(timestamps)).any()
        or (fill_index >= len(timestamps)).any()
    ):
        raise AssertionError(f"{scenario}: audit index is outside prepared data")
    fill_timestamps = (
        pd.to_datetime(fills["timestamp"], utc=True).astype("int64") // 1_000_000
    ).to_numpy(dtype=np.int64)
    if not np.array_equal(timestamps[fill_index], fill_timestamps):
        raise AssertionError(f"{scenario}: fill timestamps do not match candle labels")
    if not np.array_equal(
        audit["decision_close_timestamp_ms"].to_numpy(dtype=np.int64),
        timestamps[decision] + 60_000,
    ):
        raise AssertionError(f"{scenario}: decision candle close mismatch")
    if not np.array_equal(
        audit["activation_timestamp_ms"].to_numpy(dtype=np.int64),
        timestamps[activation],
    ):
        raise AssertionError(f"{scenario}: activation timestamp mismatch")
    if not np.array_equal(
        audit["fill_candle_open_timestamp_ms"].to_numpy(dtype=np.int64),
        timestamps[fill_index],
    ):
        raise AssertionError(f"{scenario}: fill candle open mismatch")
    if not np.array_equal(
        audit["fill_candle_close_timestamp_ms"].to_numpy(dtype=np.int64),
        timestamps[fill_index] + 60_000,
    ):
        raise AssertionError(f"{scenario}: fill candle close mismatch")
    activation_delay = activation - decision
    resting_age = fill_index - activation
    if not (activation_delay == expected_delay).all():
        raise AssertionError(f"{scenario}: causal activation delay mismatch")
    if (resting_age < 0).any():
        raise AssertionError(f"{scenario}: fill before order activation")
    if audit["order_id"].duplicated().any():
        raise AssertionError(f"{scenario}: duplicate audit order IDs")

    coin_index = {coin: index for index, coin in enumerate(coins)}
    try:
        columns = np.asarray(
            [coin_index[coin] for coin in audit["symbol"]], dtype=np.int64
        )
    except KeyError as exc:
        raise AssertionError(f"{scenario}: unknown audit coin {exc.args[0]!r}") from exc
    order_type = audit["order_type"].astype(str)
    is_entry = order_type.str.startswith("entry_").to_numpy()
    is_close = order_type.str.startswith("close_").to_numpy()
    if not (is_entry | is_close).all():
        invalid = sorted(order_type.loc[~(is_entry | is_close)].unique())
        raise AssertionError(f"{scenario}: invalid audit order types {invalid}")
    is_long = (audit["pside"] == "long").to_numpy()
    is_buy = (is_entry & is_long) | (is_close & ~is_long)
    price = audit["fill_price"].to_numpy(dtype=float)
    lows = hlcvs[fill_index, columns, 1]
    highs = hlcvs[fill_index, columns, 0]
    strict_crossing = np.where(is_buy, lows < price, highs > price)
    if not strict_crossing.all():
        raise AssertionError(
            f"{scenario}: strict H/L crossing failed for "
            f"{int((~strict_crossing).sum())} fills"
        )

    audit["resting_age_bars"] = resting_age
    audit["decision_to_fill_bars"] = fill_index - decision
    waits: list[dict[str, Any]] = []
    for scope, mask in (
        ("all", np.ones(len(audit), dtype=bool)),
        ("entry", is_entry),
        ("close", is_close),
    ):
        values = audit.loc[mask, "resting_age_bars"].to_numpy(dtype=float)
        decision_values = audit.loc[mask, "decision_to_fill_bars"].to_numpy(
            dtype=float
        )
        waits.append(
            {
                "scenario": scenario,
                "execution": identity["execution"],
                "track": identity["track"],
                "scope": scope,
                "filled_orders": int(len(values)),
                "filled_on_activation_pct": float(np.mean(values == 0.0)),
                "resting_age_bars_median": float(np.quantile(values, 0.50)),
                "resting_age_bars_p95": float(np.quantile(values, 0.95)),
                "resting_age_bars_p99": float(np.quantile(values, 0.99)),
                "resting_age_bars_max": int(np.max(values)),
                "decision_to_fill_bars_median": float(
                    np.quantile(decision_values, 0.50)
                ),
                "decision_to_fill_bars_p95": float(
                    np.quantile(decision_values, 0.95)
                ),
                "decision_to_fill_bars_max": int(np.max(decision_values)),
            }
        )
    summary = {
        "scenario": scenario,
        "execution": identity["execution"],
        "track": identity["track"],
        "audit_rows": int(len(audit)),
        "unique_order_ids": int(audit["order_id"].nunique()),
        "activation_delay_bars": expected_delay,
        "preactivation_fill_rows": int((resting_age < 0).sum()),
        "fills_on_activation": int((resting_age == 0).sum()),
        "fills_after_activation": int((resting_age > 0).sum()),
        "max_resting_age_bars": int(resting_age.max()),
        "strict_crossing_verified_rows": int(strict_crossing.sum()),
        "strict_crossing_failed_rows": int((~strict_crossing).sum()),
    }
    return summary, waits


def calculate_stress(
    *,
    risk: Any,
    scenario: str,
    identity: dict[str, Any],
    timeline: pd.DataFrame,
    fills: pd.DataFrame,
    timestamps: np.ndarray,
    hlcvs: np.ndarray,
    coins: list[str],
    settings: dict[str, Any],
    initial: float,
) -> tuple[dict[str, Any], pd.DataFrame, list[pd.DataFrame], list[dict[str, Any]]]:
    stress, residual = risk.calculate_adverse_hl_state_envelope(
        initial,
        timeline,
        fills,
        timestamps,
        hlcvs,
        coins,
        settings,
    )
    envelope_mdd = stress["adverse_hl_state_envelope_drawdown_pct"]
    worst_index = int(np.argmax(envelope_mdd.to_numpy(dtype=float)))
    worst_time = stress.index[worst_index]
    monthly = risk.adverse_stress_monthly(timeline, stress, initial)
    monthly.insert(0, "track", identity["track"])
    monthly.insert(0, "execution", identity["execution"])
    monthly.insert(0, "scenario", scenario)
    summary = {
        "scenario": scenario,
        "execution": identity["execution"],
        "track": identity["track"],
        "worst_adverse_hl_state_envelope_drawdown_pct": float(envelope_mdd.max()),
        "worst_adverse_hl_state_envelope_time": worst_time.isoformat(),
        "lowest_adverse_hl_state_envelope_equity_usdt": float(
            stress["adverse_hl_state_envelope_equity"].min()
        ),
        "max_close_to_adverse_hl_equity_gap_usdt": float(
            (timeline["equity"] - stress["adverse_hl_state_envelope_equity"]).max()
        ),
        "close_equity_reconstruction_max_abs_residual_usdt": float(residual),
    }
    positions: list[pd.DataFrame] = []
    aggregates: list[dict[str, Any]] = []
    close_mdd_time = pd.Timestamp(timeline.attrs["mdd_trough_time"])
    for kind, time in (
        ("minute_close_mdd_trough_postfill_state", close_mdd_time),
        ("adverse_hl_envelope_worst_label_postfill_state", worst_time),
    ):
        frame, aggregate = risk.position_snapshot(
            initial, fills, time, timestamps, hlcvs, coins, settings
        )
        aggregate.update(
            {
                "scenario": scenario,
                "execution": identity["execution"],
                "track": identity["track"],
                "snapshot_kind": kind,
                "minute_close_equity_usdt": float(timeline.loc[time, "equity"]),
                "adverse_hl_envelope_equity_usdt": float(
                    stress.loc[time, "adverse_hl_state_envelope_equity"]
                ),
            }
        )
        aggregates.append(aggregate)
        if not frame.empty:
            frame.insert(0, "snapshot_kind", kind)
            frame.insert(0, "track", identity["track"])
            frame.insert(0, "execution", identity["execution"])
            frame.insert(0, "scenario", scenario)
            frame.insert(4, "timestamp", aggregate["timestamp"])
            positions.append(frame)
    return summary, monthly, positions, aggregates


def compare_paired_scenarios() -> tuple[pd.DataFrame, pd.DataFrame]:
    equality_rows = []
    for left, right in PAIRWISE_INTRABAR_COMPARISONS:
        left_root, right_root = HOLDOUT / left, HOLDOUT / right
        left_identity, right_identity = (
            read_json(left_root / "run_identity.json"),
            read_json(right_root / "run_identity.json"),
        )
        left_fills = pd.read_csv(Path(left_identity["result_dir"]) / "fills.csv")
        right_fills = pd.read_csv(Path(right_identity["result_dir"]) / "fills.csv")
        left_equity = np.load(left_root / "minute_equity.npy", mmap_mode="r")
        right_equity = np.load(right_root / "minute_equity.npy", mmap_mode="r")
        equality_rows.append(
            {
                "left_scenario": left,
                "right_scenario": right,
                "fill_ledgers_exactly_equal": bool(left_fills.equals(right_fills)),
                "minute_equity_exactly_equal": bool(
                    np.array_equal(left_equity, right_equity)
                ),
                "minute_equity_max_abs_difference_usdt": float(
                    np.max(np.abs(left_equity - right_equity))
                ),
            }
        )
    return pd.DataFrame(equality_rows), pd.DataFrame()


def latency_sensitivity(summary: pd.DataFrame) -> pd.DataFrame:
    by_scenario = summary.set_index("scenario", drop=False)
    rows = []
    for track, c1, c3 in LATENCY_COMPARISONS:
        base, delayed = by_scenario.loc[c1], by_scenario.loc[c3]
        rows.append(
            {
                "track": track,
                "nominal_t_plus_1_scenario": c1,
                "t_plus_2_scenario": c3,
                "terminal_equity_return_change": float(
                    delayed["terminal_account_equity_return"]
                    - base["terminal_account_equity_return"]
                ),
                "native_smoothed_adg_change": float(
                    delayed["native_adg_strategy_eq_smoothed"]
                    - base["native_adg_strategy_eq_smoothed"]
                ),
                "minute_close_mdd_change": float(
                    delayed["recomputed_minute_close_mdd_pct"]
                    - base["recomputed_minute_close_mdd_pct"]
                ),
                "fill_count_change": int(delayed["fills"] - base["fills"]),
                "t_plus_1_passed": bool(base["eligibility_passed"])
                if pd.notna(base["eligibility_passed"])
                else None,
                "t_plus_2_passed": bool(delayed["eligibility_passed"])
                if pd.notna(delayed["eligibility_passed"])
                else None,
                "t_plus_2_failure_reasons": delayed["eligibility_failure_reasons"],
            }
        )
    return pd.DataFrame(rows)


def pct(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{100.0 * float(value):.{digits}f}%"


def money(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):,.{digits}f}"


def number(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    return f"{float(value):.{digits}f}"


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def report_text(
    *,
    summary: pd.DataFrame,
    monthly: pd.DataFrame,
    stress: pd.DataFrame,
    stress_monthly: pd.DataFrame,
    audit: pd.DataFrame,
    waits: pd.DataFrame,
    snapshots: pd.DataFrame,
    equality: pd.DataFrame,
    latency: pd.DataFrame,
    decision: dict[str, Any],
    path_decision: dict[str, Any],
    mfe_decision: dict[str, Any],
    params: dict[str, Any],
) -> str:
    by_scenario = summary.set_index("scenario", drop=False)
    c1_el, c1_bl, c1_es = (
        by_scenario.loc["C1_e_l"],
        by_scenario.loc["C1_b_l"],
        by_scenario.loc["C1_e_s"],
    )
    c3_el, c3_bl, c3_es = (
        by_scenario.loc["C3_e_l"],
        by_scenario.loc["C3_b_l"],
        by_scenario.loc["C3_e_s"],
    )
    tm_path_summary = next(
        item
        for item in path_decision["summaries"]
        if item["strategy"] == "trailing_martingale"
    )
    c1_bls, c1_u40 = by_scenario.loc["C1_b_ls"], by_scenario.loc["C1_u_40"]
    c1_stress = stress.set_index("scenario").loc["C1_e_l"]
    c3_stress = stress.set_index("scenario").loc["C3_e_l"]
    monthly_c1 = monthly.loc[
        monthly["scenario"].isin(["C1_e_l", "C3_e_l"])
    ].copy()
    monthly_c1 = monthly_c1.pivot(
        index="month",
        columns="scenario",
        values=[
            "monthly_equity_return",
            "minute_close_equity_mdd_pct",
            "max_unrealized_loss_usdt",
        ],
    ).sort_index()
    latency_rows = [
        [
            row.track,
            pct(row.terminal_equity_return_change),
            pct(row.native_smoothed_adg_change, 4),
            pct(row.minute_close_mdd_change),
            str(int(row.fill_count_change)),
            "通过" if row.t_plus_1_passed else "未通过",
            "通过" if row.t_plus_2_passed else f"未通过（{row.t_plus_2_failure_reasons}）",
        ]
        for row in latency.itertuples(index=False)
    ]
    core_rows = []
    for scenario in (
        "C1_e_l",
        "C1_b_l",
        "C1_e_s",
        "C3_e_l",
        "C3_b_l",
        "C3_e_s",
        "C1_b_ls",
        "C1_u_40",
    ):
        row = by_scenario.loc[scenario]
        status = (
            "诊断"
            if row.selection_scope == "diagnostic_only"
            else ("通过" if row.eligibility_passed else f"未通过：{row.eligibility_failure_reasons}")
        )
        core_rows.append(
            [
                scenario,
                row.track,
                pct(row.terminal_account_equity_return),
                pct(row.native_gain_strategy_eq_smoothed - 1.0),
                pct(row.native_adg_strategy_eq_smoothed, 4),
                pct(row.recomputed_minute_close_mdd_pct),
                number(row.mdd_peak_to_recovery_days, 2),
                str(int(row.fills)),
                pct(row.native_total_wallet_exposure_max),
                status,
            ]
        )
    monthly_rows = []
    for month, row in monthly_c1.iterrows():
        monthly_rows.append(
            [
                month,
                pct(row[("monthly_equity_return", "C1_e_l")]),
                pct(row[("minute_close_equity_mdd_pct", "C1_e_l")]),
                money(row[("max_unrealized_loss_usdt", "C1_e_l")]),
                pct(row[("monthly_equity_return", "C3_e_l")]),
                pct(row[("minute_close_equity_mdd_pct", "C3_e_l")]),
                money(row[("max_unrealized_loss_usdt", "C3_e_l")]),
            ]
        )
    stress_rows = []
    for scenario in ("C1_e_l", "C1_b_l", "C1_e_s", "C3_e_l", "C3_b_l", "C3_e_s"):
        row = stress.set_index("scenario").loc[scenario]
        close = by_scenario.loc[scenario]
        stress_rows.append(
            [
                scenario,
                pct(close.recomputed_minute_close_mdd_pct),
                pct(row.worst_adverse_hl_state_envelope_drawdown_pct),
                money(row.lowest_adverse_hl_state_envelope_equity_usdt),
                row.worst_adverse_hl_state_envelope_time,
                money(row.max_close_to_adverse_hl_equity_gap_usdt),
            ]
        )
    audit_rows = []
    audit_by_scenario = audit.set_index("scenario")
    waits_close = waits.loc[waits["scope"] == "close"].set_index("scenario")
    for scenario in ("C1_e_l", "C1_b_l", "C1_e_s", "C3_e_l", "C3_b_l", "C3_e_s"):
        row, close_wait = audit_by_scenario.loc[scenario], waits_close.loc[scenario]
        audit_rows.append(
            [
                scenario,
                str(int(row.activation_delay_bars)),
                str(int(row.audit_rows)),
                str(int(row.strict_crossing_failed_rows)),
                pct(close_wait.filled_on_activation_pct),
                number(close_wait.resting_age_bars_p95, 1),
                str(int(close_wait.resting_age_bars_max)),
            ]
        )
    trough_rows = []
    if not snapshots.empty:
        selected = snapshots.loc[
            snapshots["snapshot_kind"] == "minute_close_mdd_trough_postfill_state"
        ]
        for row in selected.itertuples(index=False):
            trough_rows.append(
                [
                    row.scenario,
                    row.timestamp,
                    row.coin,
                    row.position_side,
                    number(row.psize, 8),
                    money(row.pprice, 4),
                    money(row.cost_basis_notional_usdt),
                    pct(row.cost_basis_wallet_exposure),
                    money(row.close_unrealized_pnl_usdt),
                    money(row.adverse_hl_unrealized_pnl_usdt),
                ]
            )
    c1_c2_exact = equality.loc[
        equality["left_scenario"].str.startswith("C1_")
    ]
    c3_c4_exact = equality.loc[
        equality["left_scenario"].str.startswith("C3_")
    ]
    c1_c2_all_equal = bool(
        c1_c2_exact["fill_ledgers_exactly_equal"].all()
        and c1_c2_exact["minute_equity_exactly_equal"].all()
    )
    c3_c4_all_equal = bool(
        c3_c4_exact["fill_ledgers_exactly_equal"].all()
        and c3_c4_exact["minute_equity_exactly_equal"].all()
    )

    return f"""# 最终未见样本 MAXDD 深度分析报告

**结论：`trailing_martingale` 仍只能作为研究路径，不能部署。** 最终六个月 holdout 的 C1 long 主轨道在分钟收盘权益 MDD 上很低，且满足该场景的原生资格门槛；但它的日化收益只略高于下限，额外一根完整 bar 的 C3/C4 延迟便跌破收益门槛。更根本的是，路径选择锁已经记录此前三折主 OOS 仅 **3/6** 行合格，因此 holdout 的局部正结果不能推翻既有的严格失败结论。

本报告只使用本地冻结的 Binance 1 分钟 **High/Low/Close/Volume** 数据、锁定参数和已完成的精确 Rust 回放。未使用逐笔成交、盘口、订单队列、部分成交路径、mark price、资金费率、真实保证金梯度或交易所强平规则；所有“成交”均是模型成交，不是 Binance 真实成交。

## 1. 锁定顺序与判定边界

| 项目 | 值 |
| --- | --- |
| 最终训练窗口 | 2025-03-12 至 2026-03-12 |
| 最终未见 holdout | 2026-03-12 至 2026-09-12 |
| 权威账户口径 | 每分钟收盘 `usd_total_equity`，初始 100,000 USDT，`btc_collateral_cap=0` |
| 最终锁定参数哈希 | `{decision["selected_params_hash"]}` |
| MFE / giveback | 已拒绝；最终 `close_retracement_base_pct=0`，使用 CloseGrid 而非 CloseTrailing |
| C1 | 因果 T+1：决策 candle `k` 的订单最早于 `k+1` 活化，close-first |
| C2 | 因果 T+1，entry-first |
| C3 | 因果 T+2：额外一根完整 bar 延迟，close-first |
| C4 | 因果 T+2，entry-first |

最终候选在打开 holdout 前已固定；`holdout_opened_lock.json` 记录了父路径锁、参数锁、MFE 决策锁和运行工具哈希。最终训练本身的最坏主轨道 MDD 为 **1.45%**，但训练内数字不构成部署证据。

关键已锁定风险配置：`n_positions={params["n_positions"]}`、`TWE={params["twe"]}`、初始下单比例 `{pct(params["entry_initial_qty_pct"])}`、递归数量系数 `{number(params["entry_double_down_factor"], 2)}`、HSL 启用 `{params["hsl_enabled"]}`、auto-unstuck 启用 `{params["unstuck_enabled"]}`、CloseGrid 分批比例 `{pct(params["close_qty_pct"])}`。这不是历史默认 40 币 / 高 TWE martingale 配置，不能把其 70%+ 历史账户 MDD 与这里的低暴露单槽位配置混为一谈。

## 2. 最终六个月 holdout：账户层结果

“终端账户收益”是最后一分钟的真实标记权益相对初始资金；“原生平滑收益/ADG”是选择合同所用的 Rust 指标：按日末权益、末尾最多 3 日均值平滑后计算。因此两者不必相同，资格判断以原生指标为准。

{markdown_table(
    ["场景", "轨道", "终端账户收益", "原生平滑收益", "原生 ADG", "分钟收盘 MDD", "峰谷恢复天数", "fills", "最大 TWE", "合同状态"],
    core_rows,
)}

**C1 主轨道：**

- ETH-long（E-L）终端账户收益为 **{pct(c1_el.terminal_account_equity_return)}**，原生 ADG 为 **{pct(c1_el.native_adg_strategy_eq_smoothed, 4)}**，只比 `{pct(0.00005, 4)}` 的下限高约 `{pct(c1_el.native_adg_strategy_eq_smoothed - 0.00005, 4)}`。
- 五币 long basket（B-L）实际仅交易了 **{c1_bl.traded_coins}**；其中 ETH 与单币轨道几乎相同，BNB 只贡献极少量 fills。因此它不是五个独立、同时分散持仓收益的证据。
- ETH-short（E-S）是方向诊断，不参与 long 路径选参；它终端为 **{pct(c1_es.terminal_account_equity_return)}**，原生 ADG 为 **{pct(c1_es.native_adg_strategy_eq_smoothed, 4)}**。这表明该 regime 下短侧不具可部署的稳健性。
- B-LS 是事后诊断，且双侧各压至 0.14 TWE 以保持合计名义预算 0.28；仍为 **{pct(c1_bls.terminal_account_equity_return)}**，MDD **{pct(c1_bls.recomputed_minute_close_mdd_pct)}**。不能通过叠加 short 宣称风险对冲。
- U-40 是冻结 40 币宇宙的描述性泛化压力，终端为 **{pct(c1_u40.terminal_account_equity_return)}**、MDD **{pct(c1_u40.recomputed_minute_close_mdd_pct)}**，但它改变了机会集合并在最终 holdout 后才查看，**不得**用于重选参数或反向宣称策略通过。

## 3. 延迟敏感性：C1 并不是现实成交保证

{markdown_table(
    ["轨道", "T+2 相比 T+1 终端收益变化", "原生 ADG 变化", "MDD 变化", "fills 变化", "T+1", "T+2"],
    latency_rows,
)}

C1/C2 的 E-L、E-S、B-L 三对 fill ledger 和每分钟权益均逐位一致；C3/C4 三对也逐位一致。这只说明在**这份锁定配置和该 holdout**中，没有遇到会使 entry-first 与 close-first 分叉的同分钟冲突，绝不意味着 OHLC 内顺序在真实市场中无关。真正可见的敏感性来自额外完整 bar 延迟：E-L 的 MDD 从 **{pct(c1_el.recomputed_minute_close_mdd_pct)}** 升到 **{pct(c3_el.recomputed_minute_close_mdd_pct)}**，原生 ADG 从 **{pct(c1_el.native_adg_strategy_eq_smoothed, 4)}** 降至 **{pct(c3_el.native_adg_strategy_eq_smoothed, 4)}**，从而不再达到既定收益下限。B-L 同样失败；short 侧则进一步恶化至 **{pct(c3_es.native_adg_strategy_eq_smoothed, 4)}** ADG 和 **{pct(c3_es.recomputed_minute_close_mdd_pct)}** MDD。

## 4. 每月账户最大回撤（以实际 fill ledger 重建余额）

下表的“月内 MDD”都以该月开始前一刻的账户权益为起点、逐分钟收盘权益为序列；不是已实现余额 MDD。余额由**模型 fill ledger**向前填充重建，绝不使用未来 fill 回填。首尾的 2026-03 与 2026-09 是不完整自然月，其他为完整自然月。

{markdown_table(
    ["月份", "C1 E-L 收益", "C1 E-L 月内 MDD", "C1 E-L 最大浮亏", "C3 E-L 收益", "C3 E-L 月内 MDD", "C3 E-L 最大浮亏"],
    monthly_rows,
)}

C1 E-L 的全期峰值为 **{money(c1_el.mdd_peak_equity_usdt)} USDT**（{c1_el.mdd_peak_time}），谷底为 **{money(c1_el.mdd_trough_equity_usdt)} USDT**（{c1_el.mdd_trough_time}），因此分钟收盘 MDD 为 **{pct(c1_el.recomputed_minute_close_mdd_pct)}**。这不是“从初始本金亏了这么多”，而是历史高水位到之后谷底的比例。C1 E-L 的峰谷恢复为 **{number(c1_el.mdd_peak_to_recovery_days, 2)} 天**；C3 E-L 为 **{number(c3_el.mdd_peak_to_recovery_days, 2)} 天**。

本轮还发现并修复了一个**展示层**问题：旧的 `balance_and_equity.csv.gz` 会把稀疏余额列 `.bfill()`，且在大于 1 的采样桶中可把桶后段 fill 标到桶起点，从而把未来费用显示到此前分钟。C1 E-L 的该差异最大为 **{money(c1_el.postprocessed_prefill_balance_max_abs_difference_usdt, 6)} USDT**。它不影响 Rust 的订单、模型 fills 或 `minute_equity.npy`，但会误导已实现余额曲线；本报告已完全绕开该导出值，使用可重算 ledger。

## 5. 没有逐笔数据时可得到的压力边界

H/L 状态包络把每一根已知 candle 内 long 按 Low、short 按 High 标记，并分别检查 candle 开始状态和每次 fill 后状态。多币极值被并发组合，因此它是有意保守的**压力代理**，不是能够从 OHLC 唯一复原的盘中权益路径，也不是实盘下界。

{markdown_table(
    ["场景", "分钟收盘 MDD", "H/L 状态包络 MDD", "包络最低权益", "最差标签时间", "收盘至包络最大缺口"],
    stress_rows,
)}

对于 C1 E-L，收盘 MDD 为 **{pct(c1_el.recomputed_minute_close_mdd_pct)}**，H/L 包络压力扩大到 **{pct(c1_stress.worst_adverse_hl_state_envelope_drawdown_pct)}**；C3 E-L 对应为 **{pct(c3_el.recomputed_minute_close_mdd_pct)}** 和 **{pct(c3_stress.worst_adverse_hl_state_envelope_drawdown_pct)}**。这些数值表明分钟收盘 MDD 不是所有 candle 内价格压力的上界，但也不能把 H/L 包络误称为真实盘中 MDD。

## 6. 回撤谷底仓位与执行审计

下表是分钟收盘 MDD 标签时、该 candle 所有模型 fills 后的仓位快照。若 H/L 包络在同标签的 pre-fill 状态更差，该表不是那个不可观测瞬时状态的精确重建。

{markdown_table(
    ["场景", "时间", "币种", "方向", "数量", "均价", "成本名义", "成本 WEL", "收盘浮盈亏", "H/L 不利浮盈亏"],
    trough_rows if trough_rows else [["无", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "N/A"]],
)}

{markdown_table(
    ["场景", "活化延迟 bars", "已审计 fills", "严格穿价失败", "close 当活化即成交", "close 静置 p95 bars", "close 最长静置 bars"],
    audit_rows,
)}

所有审计行满足 `activation_index = decision_index + 1 + execution_delay_bars`、`fill_index >= activation_index`，并且严格跨价：buy 要求 `Low < limit`、sell 要求 `High > limit`，不接受相等价格成交。上表的静置时间仅针对**最终填成的模型订单**；被撤销、被替换或始终未成交的订单不在 fill audit 中，所以它不能估计真实队列、部分成交和未成交概率。

最终 MFE/giveback 分支未启用，所有场景的 `close_trailing_fills` 均为 0。开发期 MFE 对照的表面 MDD 改善仅约 0.00106 个百分点，且仍未让全部主 OOS 行通过，因此禁用决定保持有效；本 holdout 的 close wait 不能被误解为 MFE trigger 后的真实成交等待。

## 7. 为什么低 MDD 不等于 martingale 已安全

1. **低 MDD 主要来自低且稀疏的风险使用，不是策略已消灭尾部风险。** C1 E-L 的平均 TWE 仅 **{pct(c1_el.native_total_wallet_exposure_mean)}**、最大 **{pct(c1_el.native_total_wallet_exposure_max)}**，且 `n_positions=1`。这是和历史 40 币、高暴露默认 martingale 不同的风险合同。
2. **TWE 是订单/成本暴露的控制，不是 mark-to-market 浮亏或强平距离的硬上界。** 例如 C3 E-S 的记录最大 TWE 达到 **{pct(c3_es.native_total_wallet_exposure_max)}**，超过设置的 28%，说明延迟、价格和递归执行状态可使实际暴露与设置目标不同。
3. **历史 OOS 与方向风险仍是阻断因素。** 路径锁中 TM 只有 3/6 主 OOS 行合格，最坏主 OOS MDD 为 {pct(tm_path_summary["worst_primary_oos_drawdown_worst_strategy_eq"])}，最低 ADG 为 {pct(tm_path_summary["minimum_primary_oos_adg_strategy_eq"], 4)}；这已足以禁止“稳定盈利”或“可部署”的结论。
4. **实盘风险尚未被 1 分钟 OHLC 覆盖。** mark price 与交易所保证金、盘口深度、排队优先级、maker/taker 身份、部分成交、跳空、资金费率、延迟抖动与真实强平规则都可能显著扩大风险。C1-C4 是模型敏感性情景，既不是实盘最乐观/最悲观边界。

## 8. 最终研究判定

| 判断 | 结果 |
| --- | --- |
| 参数路径选择 | `failed_research_path_only`；TM 仅因 3/6 主 OOS 优于 EMA 的 2/6 而作为研究优先路径 |
| MFE/giveback | 拒绝，最终保持 parameter-only CloseGrid |
| 最终 C1 主轨道 | E-L 与 B-L 分别通过该单次 holdout 的原生硬门槛 |
| 最终 C3/C4 延迟压力 | E-L 与 B-L 因 ADG 低于下限而未通过 |
| 全局部署资格 | **未通过 / 不可部署** |
| 后续结构改造 | 本轮不启用：现有证据显示收益对 regime 与延迟脆弱，且低 MDD 很大程度来自低暴露；没有证据支持直接叠加新的库存或 MFE 控制器 |

完整机器可读证据位于 `final_replays/analysis/`：`final_replay_summary.csv`、`final_monthly_account_metrics.csv`、`final_adverse_hl_envelope_summary.csv`、`final_execution_audit_summary.csv`、`final_execution_waits.csv`、`final_trough_positions.csv`、`final_holdout_analysis.json` 与 `final_holdout_decision_lock.json`。原始每分钟权益、fills 和逐 fill 生命周期审计保留在 `final_replays/holdout/<scenario>/`。
"""


async def async_main(*, replace: bool) -> None:
    replay = load_module("final_causal_replay_tool_for_analysis", REPLAY_TOOL)
    risk = load_module("low_drawdown_risk_analytics_for_final", RISK_TOOL)
    disable_network()
    replay.disable_network()
    tool = replay.load_parameter_tool()
    tool.disable_network()
    params, provenance = replay.locked_inputs(tool)
    opened = read_json(REPLAYS / "holdout_opened_lock.json")
    expected_scenarios = [
        scenario_name(execution, track) for execution, track in replay.SCENARIOS
    ]
    if opened.get("provenance") != provenance:
        raise AssertionError("holdout-open lock has stale candidate provenance")
    if opened.get("holdout_window") != list(tool.FINAL_HOLDOUT):
        raise AssertionError("holdout-open lock has the wrong date window")
    if sorted(
        scenario_name(item["execution"], item["track"])
        for item in opened.get("scenarios", [])
    ) != sorted(expected_scenarios):
        raise AssertionError("holdout-open lock does not enumerate the locked scenarios")
    actual_scenarios = sorted(
        path.parent.name for path in HOLDOUT.glob("*/run_identity.json")
    )
    if actual_scenarios != sorted(expected_scenarios):
        raise AssertionError(
            f"missing or unexpected completed replays: {actual_scenarios}"
        )

    summary_rows: list[dict[str, Any]] = []
    monthly_frames: list[pd.DataFrame] = []
    timelines: dict[str, pd.DataFrame] = {}
    fills_by_scenario: dict[str, pd.DataFrame] = {}
    identities: dict[str, dict[str, Any]] = {}
    source_hashes: dict[str, dict[str, str]] = {}
    for scenario in expected_scenarios:
        summary, monthly, timeline, fills, identity = read_run(
            risk=risk,
            tool=tool,
            scenario=scenario,
            expected_provenance=provenance,
            expected_runtime=tool.runtime_identity(),
            expected_dataset_manifest_sha256=sha256(tool.DATASET / "manifest.json"),
        )
        timeline.attrs["mdd_trough_time"] = summary["mdd_trough_time"]
        summary_rows.append(summary)
        monthly_frames.append(monthly)
        timelines[scenario] = timeline
        fills_by_scenario[scenario] = fills
        identities[scenario] = identity
        root = HOLDOUT / scenario
        result_dir = Path(identity["result_dir"])
        source_hashes[scenario] = {
            "run_identity_sha256": sha256(root / "run_identity.json"),
            "minute_equity_sha256": sha256(root / "minute_equity.npy"),
            "execution_audit_sha256": sha256(
                root / "execution_boundary_audit.csv"
            ),
            "fills_sha256": sha256(result_dir / "fills.csv"),
            "analysis_sha256": sha256(result_dir / "analysis.json"),
            "config_sha256": sha256(result_dir / "config.json"),
            "dataset_json_sha256": sha256(result_dir / "dataset.json"),
            "balance_and_equity_csv_gz_sha256": sha256(
                result_dir / "balance_and_equity.csv.gz"
            ),
            "checked_execution_audit_sha256": sha256(
                root / "execution_boundary_audit_checked.csv"
            ),
        }
    summary = pd.DataFrame(summary_rows).sort_values(
        ["execution", "track"], kind="stable"
    )
    monthly = pd.concat(monthly_frames, ignore_index=True).sort_values(
        ["execution", "track", "month"], kind="stable"
    )

    # Load the same frozen data interface used by the C1 U-40 replay once.
    # The source is then subset by the persisted per-scenario coin order.
    import backtest

    config = replay.configure_special_track(tool, params, "C1", "U-40")
    config["backtest"]["base_dir"] = str((OUTPUT / "_data_load").resolve())
    (
        all_coins,
        all_hlcvs,
        settings,
        _results_path,
        _cache_dir,
        _btc_prices,
        all_timestamps,
    ) = await backtest.prepare_hlcvs_mss(config, "binance")
    if not np.all(np.diff(all_timestamps) == 60_000):
        raise AssertionError("frozen source timestamps are not minute-contiguous")
    if all_coins != identities["C1_u_40"]["prepared_coins"]:
        raise AssertionError("U-40 source universe differs from persisted replay")
    if sha256(tool.DATASET / "manifest.json") != identities["C1_u_40"][
        "dataset_manifest_sha256"
    ]:
        raise AssertionError("frozen source manifest differs from replay identity")
    coin_positions = {coin: index for index, coin in enumerate(all_coins)}

    audit_rows: list[dict[str, Any]] = []
    wait_rows: list[dict[str, Any]] = []
    stress_rows: list[dict[str, Any]] = []
    stress_monthly_frames: list[pd.DataFrame] = []
    position_frames: list[pd.DataFrame] = []
    snapshot_aggregates: list[dict[str, Any]] = []
    for scenario in expected_scenarios:
        identity = identities[scenario]
        coins = identity["prepared_coins"]
        if identity["prepared_shape"][0] != len(all_timestamps):
            raise AssertionError(f"{scenario}: persisted prepared length differs from source")
        try:
            positions = [coin_positions[coin] for coin in coins]
        except KeyError as exc:
            raise AssertionError(
                f"{scenario}: replay coin absent from frozen source: {exc.args[0]!r}"
            ) from exc
        hlcvs = (
            all_hlcvs
            if positions == list(range(len(all_coins)))
            else np.take(all_hlcvs, positions, axis=1)
        )
        audit_summary, waits = audit_execution(
            scenario=scenario,
            identity=identity,
            fills=fills_by_scenario[scenario],
            timestamps=all_timestamps,
            hlcvs=hlcvs,
            coins=coins,
        )
        audit_rows.append(audit_summary)
        wait_rows.extend(waits)
        stress_summary, stress_monthly, positions_frames, aggregates = calculate_stress(
            risk=risk,
            scenario=scenario,
            identity=identity,
            timeline=timelines[scenario],
            fills=fills_by_scenario[scenario],
            timestamps=all_timestamps,
            hlcvs=hlcvs,
            coins=coins,
            settings=settings,
            initial=float(
                read_json(Path(identity["result_dir"]) / "config.json")["backtest"][
                    "starting_balance"
                ]
            ),
        )
        stress_rows.append(stress_summary)
        stress_monthly_frames.append(stress_monthly)
        position_frames.extend(positions_frames)
        snapshot_aggregates.extend(aggregates)
    audit = pd.DataFrame(audit_rows).sort_values(["execution", "track"])
    waits = pd.DataFrame(wait_rows).sort_values(["execution", "track", "scope"])
    stress = pd.DataFrame(stress_rows).sort_values(["execution", "track"])
    stress_monthly = pd.concat(stress_monthly_frames, ignore_index=True).sort_values(
        ["execution", "track", "month"]
    )
    snapshots = (
        pd.concat(position_frames, ignore_index=True)
        if position_frames
        else pd.DataFrame()
    )
    aggregates = pd.DataFrame(snapshot_aggregates).sort_values(
        ["execution", "track", "snapshot_kind"]
    )
    equality, _unused = compare_paired_scenarios()
    if not (
        equality["fill_ledgers_exactly_equal"].all()
        and equality["minute_equity_exactly_equal"].all()
    ):
        raise AssertionError("paired C1/C2 or C3/C4 replay outputs unexpectedly diverged")
    latency = latency_sensitivity(summary)

    by_scenario = summary.set_index("scenario")
    c1_primary_passed = bool(
        by_scenario.loc[["C1_e_l", "C1_b_l"], "eligibility_passed"].all()
    )
    execution_stress_passed = bool(
        by_scenario.loc[
            ["C1_e_l", "C1_b_l", "C2_e_l", "C2_b_l", "C3_e_l", "C3_b_l", "C4_e_l", "C4_b_l"],
            "eligibility_passed",
        ].all()
    )
    path_decision = read_json(STUDY / "strategy_path_decision_lock.json")
    mfe_decision = read_json(STUDY / "mfe_research" / "tm_mfe_path_decision_lock.json")
    if path_decision["parameter_only_status"] != "failed_research_path_only":
        raise AssertionError("unexpected path-selection status")
    if mfe_decision["mfe_enabled_for_final_training"] is not False:
        raise AssertionError("final candidate does not match disabled-MFE decision")
    decision = {
        "status": "not_deployable_research_path_only",
        "strategy": "trailing_martingale",
        "selected_params_hash": provenance["selected_params_hash"],
        "final_holdout_window": list(tool.FINAL_HOLDOUT),
        "final_c1_primary_tracks_passed": c1_primary_passed,
        "final_execution_c1_to_c4_primary_stress_passed": execution_stress_passed,
        "development_path_status": path_decision["parameter_only_status"],
        "development_primary_oos_rows_eligible": next(
            item["eligible_primary_rows"]
            for item in path_decision["summaries"]
            if item["strategy"] == "trailing_martingale"
        ),
        "development_primary_oos_rows_expected": next(
            item["expected_primary_rows"]
            for item in path_decision["summaries"]
            if item["strategy"] == "trailing_martingale"
        ),
        "mfe_enabled_for_final_training": False,
        "deployment_blockers": [
            "development walk-forward primary OOS eligibility was 3/6, not all rows",
            "C3/C4 T+2 primary holdout sensitivity fails the native ADG floor",
            "one-minute H/L/C/V cannot validate market microstructure, queue, partial fills, mark price, funding, or exchange liquidation behavior",
        ],
        "provenance": provenance,
        "holdout_opened_lock_sha256": sha256(REPLAYS / "holdout_opened_lock.json"),
    }
    report = report_text(
        summary=summary,
        monthly=monthly,
        stress=stress,
        stress_monthly=stress_monthly,
        audit=audit,
        waits=waits,
        snapshots=snapshots,
        equality=equality,
        latency=latency,
        decision=decision,
        path_decision=path_decision,
        mfe_decision=mfe_decision,
        params=params,
    )
    output_manifest = {
        "purpose": "Independent account-equity and causal-execution audit of locked final holdout replays",
        "network_forbidden": True,
        "strategy": "trailing_martingale",
        "holdout_window": list(tool.FINAL_HOLDOUT),
        "provenance": provenance,
        "holdout_opened_lock_sha256": sha256(REPLAYS / "holdout_opened_lock.json"),
        "analysis_tool_sha256": sha256(Path(__file__).resolve()),
        "replay_tool_sha256": sha256(REPLAY_TOOL),
        "backtest_python_sha256": sha256(REPO / "src/backtest.py"),
        "risk_helper_sha256": sha256(RISK_TOOL),
        "dataset_manifest_sha256": sha256(tool.DATASET / "manifest.json"),
        "runtime": tool.runtime_identity(),
        "source_artifacts_sha256": source_hashes,
        "checks": {
            "all_expected_scenarios_completed": True,
            "all_fill_ledgers_reconcile_to_cash": True,
            "all_minute_close_mdds_reconcile_to_native_metrics": True,
            "all_daily_1pct_mdds_reconcile_to_native_metrics": True,
            "all_execution_audits_match_fills": True,
            "all_execution_audits_are_causal": True,
            "all_model_fills_strictly_cross_hl": True,
            "all_c1_c2_and_c3_c4_pair_outputs_exactly_equal": True,
            "hl_state_envelope_reconstructs_close_equity_within_usdt": 0.25,
        },
    }

    write_csv(OUTPUT / "final_replay_summary.csv", summary, replace=replace)
    write_csv(
        OUTPUT / "final_monthly_account_metrics.csv", monthly, replace=replace
    )
    write_csv(
        OUTPUT / "final_adverse_hl_envelope_summary.csv", stress, replace=replace
    )
    write_csv(
        OUTPUT / "final_adverse_hl_monthly.csv", stress_monthly, replace=replace
    )
    write_csv(
        OUTPUT / "final_execution_audit_summary.csv", audit, replace=replace
    )
    write_csv(OUTPUT / "final_execution_waits.csv", waits, replace=replace)
    write_csv(
        OUTPUT / "final_intrabar_ordering_equivalence.csv", equality, replace=replace
    )
    write_csv(
        OUTPUT / "final_latency_sensitivity.csv", latency, replace=replace
    )
    write_csv(
        OUTPUT / "final_trough_positions.csv", snapshots, replace=replace
    )
    write_csv(
        OUTPUT / "final_trough_position_aggregates.csv", aggregates, replace=replace
    )
    write_json(
        OUTPUT / "final_holdout_analysis.json",
        {
            "decision": decision,
            "summary": json_records(summary),
            "execution_audit": json_records(audit),
            "execution_waits": json_records(waits),
            "adverse_hl_state_envelope": json_records(stress),
            "latency_sensitivity": json_records(latency),
        },
        replace=replace,
    )
    write_json(OUTPUT / "final_reproducibility_metadata.json", output_manifest, replace=replace)
    write_file(OUTPUT / "final_maxdd_deep_analysis_report.md", report, replace=replace)
    write_lock(OUTPUT / "final_holdout_decision_lock.json", decision)
    print(
        json.dumps(
            {
                "status": decision["status"],
                "c1_primary_passed": c1_primary_passed,
                "c1_to_c4_primary_stress_passed": execution_stress_passed,
                "output": str(OUTPUT.relative_to(REPO)),
            },
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--replace-derived",
        action="store_true",
        help="Replace non-lock derived analysis files if their content differs.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(async_main(replace=args.replace_derived))
