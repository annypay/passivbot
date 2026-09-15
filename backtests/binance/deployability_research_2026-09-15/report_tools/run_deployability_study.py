"""Locked offline parameter-only economics and execution research.

No new unseen holdout is claimed: this frozen dataset has already been inspected.
Candidate configurations and numerical gates are fixed before the new screen.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import importlib.util
import json
import logging
from pathlib import Path
import socket
import sys
import time
from typing import Any

import numpy as np
import pandas as pd

from account_diagnostics import (
    FILL_COLUMNS, MINUTE_MS, DAY_MS, apply_cost_overlay, audit_execution,
    digest_json, holding_diagnostics, jsonable, normalize_fills, performance,
    period_metrics, reconstruct_account, save_json, sha256,
)


REPO = Path(__file__).resolve().parents[4]
ROOT = Path(__file__).resolve().parents[1]
PRIOR = REPO / "backtests/binance/maxdd_strategy_research_2026-09-14"
sys.path.insert(0, str(REPO / "src"))
WINDOW = ("2023-09-12", "2026-09-12")
BASKET = ["BTC", "ETH", "BNB", "XRP", "SOL"]
RISK = {
    "total_wallet_exposure_limit": 0.75, "n_positions": 2.0,
    "entry_cooldown_minutes": 30.0,
    "position_exposure_enforcer_enabled": True,
    "position_exposure_enforcer_threshold": 0.9,
    "total_exposure_enforcer_enabled": True, "total_exposure_enforcer_threshold": 0.9,
    "total_exposure_enforcer_policy": "reduce_overweight",
    "total_exposure_entry_gate_enabled": True,
    "we_excess_allowance_mode": "bounded", "we_excess_allowance_pct": 0.0,
}
COSTS = {
    "reference": {"maker_fee_override": .0004, "taker_fee_override": .0006},
    "conservative": {"maker_fee_override": .0006, "taker_fee_override": .0008},
    "severe": {"maker_fee_override": .0010, "taker_fee_override": .0012},
}
GATES = {
    "conservative_funding_net_cagr_min": .08,
    "conservative_funding_net_account_mdd_max": .15,
    "severe_funding_net_account_mdd_max": .20,
    "severe_funding_net_return_min": 0.0,
    "conservative_positive_month_share_min": .60,
    "worst_halfyear_return_min": -.05,
    "positive_halfyears_min": 4,
    "longest_underwater_days_max": 120,
    "regular_close_notional_under_5m_share_max": .10,
    "regular_gross_profit_under_5m_share_max": .10,
    "regular_closed_notional_weighted_median_hold_minutes_min": 30.0,
    "traded_coin_count_min": 3,
    "top_coin_exposure_time_share_max": .60,
    "effective_coin_count_exposure_time_min": 2.5,
    "completion_ratio_min": .999,
}
HALF_YEAR_EDGES = [
    "2023-09-12", "2024-03-12", "2024-09-12", "2025-03-12",
    "2025-09-12", "2026-03-12", "2026-09-12",
]


def no_network(*_args: Any, **_kwargs: Any) -> None:
    raise RuntimeError("network is forbidden in the deployability study")


def offline() -> None:
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network


def prior_tool():
    path = PRIOR / "report_tools/run_maxdd_parameter_study.py"
    spec = importlib.util.spec_from_file_location("previous_locked_parameter_tool", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def specs() -> list[dict[str, Any]]:
    rows = []

    def add(name: str, changes: dict[str, Any], hypothesis: str,
            *, template: str | None = None, strategy: str = "trailing_martingale",
            direction: str = "long") -> None:
        rows.append({"name": name, "changes": changes, "hypothesis": hypothesis,
                     "template": template, "strategy": strategy, "direction": direction})

    add("tm_reference", {}, "Matched conservative TM; HSL panic executes as market under stress.")
    add("tm_slots3", {"risk.n_positions": 3.0}, "More simultaneous slots, same total budget.")
    add("tm_slots5", {"risk.n_positions": 5.0}, "Five slots, same total budget; less per-coin inventory.")
    add("tm_cooldown90", {"risk.entry_cooldown_minutes": 90.0},
        "Slow additions without forbidding protective exits.")
    add("tm_wider_profit", {"strategy.trailing_martingale.close.threshold_base_pct": .012},
        "Wider normal profit threshold versus fee-sensitive exits.")
    add("tm_early_unstuck", {"unstuck.threshold": .30, "unstuck.close_pct": .025},
        "Start reducing earlier, at half the normal reduction size.")
    add("tm_ungated_unstuck", {"unstuck.ema_gating_enabled": False},
        "Do not wait for EMA recovery; same finite account-wide loss allowance.")
    add("tm_exposure_exit", {"strategy.trailing_martingale.close.threshold_we_weight": -.015},
        "High exposure permits negative markup; slices become less aggressive as inventory shrinks.")
    add("tm_smaller_reentry", {"strategy.trailing_martingale.entry.double_down_factor": .25},
        "Restrain recursive quantity growth, not a per-position absolute loss cap.")
    add("tm_ema_all", {"strategy.trailing_martingale.entry.ema_gate_mode": "all"},
        "Require EMA entry band for reentries as well as initials.")
    liquidity = {
        "forager.score_weights": {"volume": .6, "volatility": .3, "ema_readiness": .1},
        "forager.volume_drop_pct": .2,
    }
    add("tm_liquidity", liquidity, "Completed-candle liquidity ranking, not a real depth filter.")
    add("tm_without_unstuck", {"unstuck.enabled": False},
        "Ablate gradual loss taking while retaining exposure caps and HSL.")
    add("tm_enforcer85", {"risk.position_exposure_enforcer_threshold": .85,
                         "risk.total_exposure_enforcer_threshold": .85},
        "Earlier exposure recycling; measure reduction/refill churn and realized costs.")
    slow = {
        "risk.n_positions": 3.0, "risk.entry_cooldown_minutes": 90.0,
        "strategy.trailing_martingale.entry.double_down_factor": .35,
        "strategy.trailing_martingale.entry.threshold_base_pct": .03,
        "strategy.trailing_martingale.entry.ema_gate_mode": "all",
        "strategy.trailing_martingale.close.threshold_base_pct": .012,
        "strategy.trailing_martingale.close.threshold_we_weight": -.01,
        "unstuck.threshold": .30, "unstuck.close_pct": .025,
        **liquidity,
    }
    add("tm_slow_combination", slow, "Predeclared combined conservative inventory and turnover design.")
    add("tm_public_core", {"risk.n_positions": 3.0, "risk.entry_cooldown_minutes": 90.0},
        "Public five-coin signal template with equalized total budget and shared safety.",
        template="BTC_ETH_XRP_SOL_ADA_long.json")
    add("tm_public_adaptive", {"risk.n_positions": 3.0, "risk.entry_cooldown_minutes": 90.0},
        "Public default volatility/trailing signals with equalized budget; not old locked MFE selection.",
        template="default_trailing_martingale_long.json")
    add("ema_public", {"risk.n_positions": 3.0, "risk.entry_cooldown_minutes": 90.0},
        "EMA-anchor public signal template with common capped account risk.",
        template="ema_anchor.json", strategy="ema_anchor")
    add("ema_inventory", {
        "risk.n_positions": 3.0, "risk.entry_cooldown_minutes": 90.0,
        "strategy.ema_anchor.base_qty_pct": .02,
        "strategy.ema_anchor.entry_double_down_factor": .5,
        "strategy.ema_anchor.offset": .012,
        "strategy.ema_anchor.offset_psize_weight": 1.0,
        **liquidity,
    }, "Small, slower EMA-anchor inventory as a distinct fixed-budget sleeve.",
        template="ema_anchor.json", strategy="ema_anchor")
    add("tm_short_diagnostic", slow, "Symmetric slow short sleeve; never assume hedge profitability.",
        direction="short")
    return rows


def set_path(config: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = config
    for part in parts[:-1]:
        current = current[part]
    # ema_gate_mode is canonical but may not be materialized in an older template.
    if parts[-1] not in current and parts[-1] != "ema_gate_mode":
        raise KeyError(f"unknown parameter path {path}")
    current[parts[-1]] = deepcopy(value)


def make_config(tool: Any, spec: dict[str, Any], execution: str, costs: str,
                start: str = WINDOW[0], end: str = WINDOW[1]) -> dict[str, Any]:
    from config import load_prepared_config

    cfg = tool.common_config(spec["strategy"], start, end, execution=execution)
    if spec["template"]:
        source = load_prepared_config(
            str(REPO / "configs/examples" / spec["template"]), verbose=False)
        cfg["bot"] = deepcopy(source["bot"])
    cfg["coin_overrides"] = {}
    for side in ("long", "short"):
        active = side == spec["direction"]
        bot = cfg["bot"][side]
        bot["risk"].update(deepcopy(RISK))
        bot["hsl"].update({
            "enabled": active, "red_threshold": .15, "ema_span_minutes": 240.0,
            "cooldown_minutes_after_red": 2160.0,
            "orange_tier_mode": "tp_only_with_active_entry_cancellation",
            "panic_close_order_type": "market", "restart_after_red_policy": "threshold",
            "no_restart_drawdown_threshold": 1.0,
        })
        bot["unstuck"].update({
            "enabled": active, "threshold": .5, "close_pct": .05,
            "loss_allowance_pct": .01, "ema_gating_enabled": True, "ema_dist": -.05,
        })
        if active:
            for path, value in spec["changes"].items():
                set_path(bot, path, value)
        else:
            bot["risk"].update({"n_positions": 0.0, "total_wallet_exposure_limit": 0.0})
        if spec["strategy"] == "trailing_martingale":
            initial = bot["strategy"]["trailing_martingale"]["entry"]
            initial["initial_ema_dist"] = (
                abs(initial["initial_ema_dist"]) * (1 if side == "long" else -1))
    cfg["live"].update({
        "approved_coins": {
            "long": list(BASKET) if spec["direction"] == "long" else [],
            "short": list(BASKET) if spec["direction"] == "short" else [],
        },
        "hedge_mode": False, "hsl_signal_mode": "unified", "market_orders_allowed": False,
    })
    cfg["backtest"].update({
        **COSTS[costs], "market_order_slippage_pct": .001,
        "starting_balance": 100000.0, "balance_sample_divider": 1,
        "base_dir": str(ROOT / "_engine"), "btc_collateral_cap": 0.0,
    })
    cfg["disable_plotting"] = "all"
    frozen_path = ROOT / "candidate_configs" / f"{spec['name']}.json"
    if frozen_path.exists():
        frozen = json.loads(frozen_path.read_text())
        expected = deepcopy(frozen)
        expected["backtest"].update({
            **COSTS[costs], **tool.EXECUTION_SCENARIOS[execution],
            "start_date": start, "end_date": end,
        })
        actual_semantics, expected_semantics = deepcopy(cfg), deepcopy(expected)
        actual_semantics.pop("_transform_log", None)
        expected_semantics.pop("_transform_log", None)
        if digest_json(actual_semantics) != digest_json(expected_semantics):
            raise AssertionError(f"frozen candidate semantics changed: {spec['name']}")
        # Normalization timestamps are diagnostics, not candidate parameters.
        cfg["_transform_log"] = deepcopy(frozen["_transform_log"])
    return cfg


def freeze(tool: Any) -> dict[str, Any]:
    rows = specs()
    candidates = {row["name"]: make_config(tool, row, "C3", "conservative") for row in rows}
    contract = {
        "version": 1, "purpose": "retrospective multicoin economics and deployability research",
        "sample_status": "previously_observed_reused_not_a_new_unseen_holdout",
        "runtime": tool.runtime_identity(),
        "dataset_manifest_sha256": sha256(tool.DATASET / "manifest.json"),
        "safety": {"network": False, "credentials": False, "bot_start": False,
                   "exchange_account_or_orders": False},
        "window": list(WINDOW), "universe": BASKET, "capital_usdt": 100000,
        "gates": GATES, "cost_scenarios": COSTS,
        "cost_interpretation": "assumed effective transaction costs, not verified account fee tiers",
        "primary_execution": "C3", "execution_scenarios": tool.EXECUTION_SCENARIOS,
        "funding_stress_bps_per_8h": 1.0,
        "funding_method": "continuous accrual on prior-minute gross mark notional; not real funding",
        "terminal_exit_bps": 8.0,
        "halfyear_edges": HALF_YEAR_EDGES,
        "short_hold_gate": "FIFO minimum age over candle uncertainty; ordinary closes only; never block risk exits",
        "portfolio_combination": "predeclared equal-budget segregated sleeves, no dynamic rebalancing",
        "combo_pairs": [["tm_slow_combination", "ema_inventory"],
                        ["tm_public_core", "ema_inventory"],
                        ["tm_reference", "ema_public"],
                        ["tm_slow_combination", "tm_short_diagnostic"]],
        "candidate_specs": rows,
        "candidate_config_hashes": {name: digest_json(cfg) for name, cfg in candidates.items()},
        "candidate_budget": len(rows), "selection_after_new_screen": "no new parameter tuning",
        "promotion_scope": "research screen only; all deployments remain blocked pending fresh data and execution evidence",
        "stress_shortlist_rule": (
            "rank long sleeves by count of primary gate failures, then MDD, then negative CAGR; "
            "take first 3 plus tm_reference, tm_slow_combination and ema_inventory, deduplicated"
        ),
        "helpers": {"parameter_tool": sha256(PRIOR / "report_tools/run_maxdd_parameter_study.py")},
    }
    save_json(ROOT / "research_contract.json", contract, immutable=True)
    for name, cfg in candidates.items():
        save_json(ROOT / "candidate_configs" / f"{name}.json", cfg, immutable=True)
    return contract


def previous_holding_reports() -> None:
    rows = []
    for identity_path in sorted((PRIOR / "final_replays/holdout").glob("*/run_identity.json")):
        identity = json.loads(identity_path.read_text())
        result_dir = Path(identity["result_dir"])
        fills = normalize_fills(pd.read_csv(result_dir / "fills.csv"))
        raw = np.load(identity_path.parent / "minute_equity.npy", mmap_mode="r")
        # USDT-M symbols in this study use linear unit multipliers. Check saved metadata.
        dataset = json.loads((result_dir / "dataset.json").read_text())
        mss_path = prior_tool().DATASET / "market_specific_settings.json"
        if not mss_path.exists():
            mss_path = prior_tool().DATASET / "mss.json"
        settings = json.loads(mss_path.read_text())
        multipliers = {coin: float(settings[coin]["c_mult"]) for coin in identity["prepared_coins"]}
        lots, episodes, metrics = holding_diagnostics(fills, int(raw[-1, 0]), multipliers)
        scenario = identity["scenario"]
        output = ROOT / "previous_ledger_diagnostics" / scenario
        output.mkdir(parents=True, exist_ok=True)
        lots.to_csv(output / "closed_inventory_ages.csv", index=False)
        episodes.to_csv(output / "position_episodes.csv", index=False)
        turnover = float(sum(abs(row.qty) * row.price * multipliers[row.coin]
                             for row in fills.itertuples(index=False)))
        profit = float(raw[-1, 1] - 100000)
        rows.append({
            "scenario": scenario, **metrics, "turnover_usdt": turnover,
            "fees_usdt": float(-fills["fee_paid"].sum()),
            "raw_terminal_profit_usdt": profit,
            "additional_fee_bps_to_zero_terminal_profit_fixed_ledger": (
                profit / turnover * 10000 if turnover else None),
            "source_fills_sha256": sha256(result_dir / "fills.csv"),
        })
    save_json(ROOT / "previous_ledger_diagnostics/summary.json", rows)
    pd.DataFrame(rows).to_csv(ROOT / "previous_ledger_diagnostics/summary.csv", index=False)
    print(f"previous ledger diagnostics: {len(rows)} scenarios", flush=True)


def evaluate_gate(summary: dict[str, Any], months: pd.DataFrame,
                  halves: pd.DataFrame) -> list[str]:
    failures = []
    checks = {
        "cagr": summary["funding_cagr"] >= GATES["conservative_funding_net_cagr_min"],
        "mdd": summary["funding_mdd"] <= GATES["conservative_funding_net_account_mdd_max"],
        "severe_mdd": summary["severe_fixed_ledger_mdd"] <= GATES["severe_funding_net_account_mdd_max"],
        "severe_gain": summary["severe_fixed_ledger_return"] > GATES["severe_funding_net_return_min"],
        "positive_months": (months["return"] > 0).mean() >= GATES["conservative_positive_month_share_min"],
        "worst_halfyear": halves["return"].min() >= GATES["worst_halfyear_return_min"],
        "positive_halfyears": (halves["return"] > 0).sum() >= GATES["positive_halfyears_min"],
        "underwater": summary["funding_longest_underwater_observed_days"] <= GATES["longest_underwater_days_max"],
        "active_coins": summary["traded_coin_count"] >= GATES["traded_coin_count_min"],
        "coin_concentration": summary["top_coin_exposure_time_share"] <= GATES["top_coin_exposure_time_share_max"],
        "effective_coins": summary["effective_coin_count_exposure_time"] >= GATES["effective_coin_count_exposure_time_min"],
        "complete": summary["native_completion_ratio"] >= GATES["completion_ratio_min"],
        "not_liquidated": not summary["native_liquidated"],
    }
    for metric, direction, threshold in (
        ("regular_close_notional_under_5m_share", "max", GATES["regular_close_notional_under_5m_share_max"]),
        ("regular_gross_profit_under_5m_share", "max", GATES["regular_gross_profit_under_5m_share_max"]),
        ("regular_closed_notional_weighted_median_hold_minutes", "min",
         GATES["regular_closed_notional_weighted_median_hold_minutes_min"]),
    ):
        value = summary.get(metric)
        checks[metric] = value is not None and (
            value <= threshold if direction == "max" else value >= threshold)
    for name, ok in checks.items():
        if not ok:
            failures.append(name)
    return failures


def halves_for_account(account: pd.DataFrame, initial: float) -> pd.DataFrame:
    previous = initial
    rows = []
    for start, end in zip(HALF_YEAR_EDGES[:-1], HALF_YEAR_EDGES[1:]):
        mask = (account.index >= pd.Timestamp(start, tz="UTC")) & (
            account.index < pd.Timestamp(end, tz="UTC"))
        group = account.loc[mask]
        if group.empty:
            continue
        metrics = performance(group, previous)
        rows.append({"start": start, "end": end, **metrics})
        previous = float(group["equity"].iloc[-1])
    return pd.DataFrame(rows)


def canonicalize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Align HLCV columns with prep_backtest_args' sorted coin/market order."""
    current = state["coins"]
    if len(set(current)) != len(current):
        raise ValueError("duplicate prepared coin identity")
    canonical = sorted(current)
    permutation = [current.index(coin) for coin in canonical]
    result = dict(state)
    result["coins"] = canonical
    result["hlcvs_basket"] = np.ascontiguousarray(
        state["hlcvs_basket"][:, permutation, :])
    if result["hlcvs_basket"].shape[1] != len(canonical):
        raise AssertionError("coin axis mismatch")
    return result


def analyze_run(
    folder: Path, cfg: dict[str, Any], spec: dict[str, Any], state: dict[str, Any],
    fills: pd.DataFrame, raw: np.ndarray, analysis: dict[str, Any],
) -> dict[str, Any]:
    coins = state["coins"]
    multipliers = {coin: float(state["mss"][coin]["c_mult"]) for coin in coins}
    account, concentration, risk = reconstruct_account(
        raw, fills, state["timestamps"], state["hlcvs_basket"], coins, multipliers, 100000.0)
    lots, episodes, holding = holding_diagnostics(fills, int(raw[-1, 0]), multipliers)
    actual = performance(account, 100000.0)
    if abs(actual["mdd"] - float(analysis["drawdown_worst_strategy_eq"])) > 1e-7:
        raise AssertionError("native and reconstructed minute MDD differ")
    funding = apply_cost_overlay(account, extra_bps=0, funding_bps_per_8h=1.0)
    funding_metrics = performance(funding, 100000.0)
    # 10 bps severe assumed cost minus the cost already charged in the Rust run.
    extra = max(0.0, (COSTS["severe"]["maker_fee_override"]
                      - cfg["backtest"]["maker_fee_override"]) * 10000)
    severe = apply_cost_overlay(account, extra_bps=extra, funding_bps_per_8h=1.0)
    severe_metrics = performance(severe, 100000.0)
    months = period_metrics(funding, 100000.0)
    halves = halves_for_account(funding, 100000.0)
    audit = audit_execution(
        pd.read_csv(folder / "execution_audit.csv"), fills, state["timestamps"],
        state["hlcvs_basket"], coins, cfg["backtest"]["execution_delay_bars"], True,
        cfg["backtest"]["market_order_slippage_pct"],
        {coin: float(state["mss"][coin]["price_step"]) for coin in coins},
    )
    summary = {
        "name": spec["name"], "strategy": spec["strategy"], "direction": spec["direction"],
        **actual, **risk, **holding,
        **{f"funding_{key}": value for key, value in funding_metrics.items()},
        **{f"severe_fixed_ledger_{key}": value for key, value in severe_metrics.items()},
        "funding_and_terminal_exit_cost_usdt": funding.attrs["extra_cost_usdt"],
        "positive_month_share": float((months["return"] > 0).mean()),
        "positive_halfyears": int((halves["return"] > 0).sum()),
        "worst_halfyear_return": float(halves["return"].min()),
        "native_completion_ratio": float(analysis["backtest_completion_ratio"]),
        "native_liquidated": bool(analysis["liquidated"]),
        "hard_stop_triggers": int(analysis["hard_stop_triggers"]),
        "protective_close_fills": int(fills["type"].str.startswith(
            ("close_unstuck", "close_auto_reduce", "close_panic")).sum()),
        "normal_close_fills": int(fills["type"].str.startswith("close_").sum()
                                  - fills["type"].str.startswith(
                                      ("close_unstuck", "close_auto_reduce", "close_panic")).sum()),
        "fills": len(fills), "audit": audit,
    }
    summary["gate_failures"] = evaluate_gate(summary, months, halves)
    summary["research_screen_passed"] = not summary["gate_failures"]
    # Compressed persistent raw paths retain exact minute values for combinations.
    np.savez_compressed(
        folder / "account_path.npz", timestamps=raw[:, 0].astype(np.int64),
        **{column: account[column].to_numpy() for column in account})
    lots.to_csv(folder / "closed_inventory_ages.csv", index=False)
    episodes.to_csv(folder / "position_episodes.csv", index=False)
    concentration.to_csv(folder / "coin_concentration.csv", index=False)
    months.to_csv(folder / "monthly_funding_stress.csv", index=False)
    halves.to_csv(folder / "halfyear_funding_stress.csv", index=False)
    fills.groupby(["coin", "type"], sort=True).agg(
        fills=("qty", "size"), pnl=("pnl", "sum"), fees=("fee_paid", "sum")
    ).reset_index().to_csv(folder / "fill_type_economics.csv", index=False)
    save_json(folder / "summary.json", summary)
    return summary


async def run_specs(tool: Any, chosen: list[dict[str, Any]], execution: str,
                    costs: str) -> None:
    import backtest

    state = canonicalize_state(
        await tool.prepare_window("trailing_martingale", *WINDOW, execution=execution))
    tool.install_state(state)
    for spec in chosen:
        folder = ROOT / "runs" / f"{execution}_{costs}" / spec["name"]
        completion = folder / "completion.json"
        cfg = make_config(tool, spec, execution, costs)
        cfg["backtest"]["coins"] = {"binance": state["coins"]}
        cfg["backtest"]["cache_dir"] = {"binance": state["cache_dir"]}
        cfg["backtest"]["execution_audit_path"] = str(folder / "execution_audit.csv")
        identity = {
            "spec": spec, "execution": execution, "cost_scenario": costs,
            "config_hash": digest_json(cfg), "contract_sha256": sha256(ROOT / "research_contract.json"),
            "runtime": tool.runtime_identity(),
            "dataset_manifest_sha256": sha256(tool.DATASET / "manifest.json"),
            "prepared_coins": state["coins"], "prepared_shape": list(state["hlcvs_basket"].shape),
            "diagnostics_source_sha256": sha256(Path(__file__).with_name("account_diagnostics.py")),
            "runner_source_sha256": sha256(Path(__file__)),
        }
        if completion.exists():
            saved = json.loads(completion.read_text())
            if saved["identity"] != identity:
                raise AssertionError(f"completed replay identity changed: {folder}")
            for name, checksum in saved["artifact_hashes"].items():
                if sha256(folder / name) != checksum:
                    raise AssertionError(f"completed replay artifact changed: {folder / name}")
            print(f"already complete {execution}/{costs}/{spec['name']}", flush=True)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        save_json(folder / "config.json", cfg, immutable=True)
        started = time.monotonic()
        # Recover only persisted exact artifacts whose prior immutable config still matches.
        if (folder / "native_analysis.json").exists():
            raw = np.load(folder / "minute_equity.npy", allow_pickle=False)
            fills = normalize_fills(pd.read_csv(folder / "fills.csv"))
            analysis = json.loads((folder / "native_analysis.json").read_text())
        else:
            raw_fills, raw, analysis = backtest.run_backtest(
                state["hlcvs_basket"], state["mss"], cfg, "binance",
                state["btc_usd_prices"], state["timestamps"])
            frame = pd.DataFrame(raw_fills, columns=FILL_COLUMNS)
            frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="raise")
            fills = normalize_fills(frame)
            np.save(folder / "minute_equity.npy", raw, allow_pickle=False)
            fills[FILL_COLUMNS].to_csv(folder / "fills.csv", index=False)
            save_json(folder / "native_analysis.json", analysis, immutable=True)
        summary = analyze_run(folder, cfg, spec, state, fills, raw, analysis)
        paths = sorted(path for path in folder.iterdir() if path.is_file()
                       and path.name != "completion.json")
        save_json(completion, {"identity": identity,
                              "artifact_hashes": {p.name: sha256(p) for p in paths}},
                  immutable=True)
        print(json.dumps({
            "name": spec["name"], "execution": execution, "costs": costs,
            "cagr": summary["funding_cagr"], "mdd": summary["funding_mdd"],
            "holding_median_min": summary.get("regular_closed_notional_weighted_median_hold_minutes"),
            "failures": summary["gate_failures"], "elapsed_s": time.monotonic() - started,
        }), flush=True)


def aggregate() -> None:
    rows = []
    for path in sorted((ROOT / "runs").glob("*/*/completion.json")):
        summary = json.loads((path.parent / "summary.json").read_text())
        summary["scenario"] = path.parent.parent.name
        summary["gate_failures"] = ",".join(summary["gate_failures"])
        summary.pop("audit")
        rows.append(summary)
    if rows:
        pd.DataFrame(rows).to_csv(ROOT / "screen_summary.csv", index=False)


async def main(args: argparse.Namespace) -> None:
    offline()
    tool = prior_tool()
    tool.disable_network()
    logging.getLogger().setLevel(logging.ERROR)
    freeze(tool)
    if args.phase == "freeze":
        print(f"frozen {len(specs())} candidates before new results", flush=True)
    elif args.phase == "previous":
        previous_holding_reports()
    elif args.phase == "run":
        chosen = [item for item in specs() if not args.candidate or item["name"] in args.candidate]
        if args.candidate and len(chosen) != len(set(args.candidate)):
            raise ValueError("unknown candidate")
        await run_specs(tool, chosen, args.execution, args.costs)
        aggregate()
    elif args.phase == "aggregate":
        aggregate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("freeze", "previous", "run", "aggregate"))
    parser.add_argument("--execution", default="C3", choices=("C1", "C2", "C3", "C4"))
    parser.add_argument("--costs", default="conservative", choices=tuple(COSTS))
    parser.add_argument("--candidate", action="append")
    asyncio.run(main(parser.parse_args()))
