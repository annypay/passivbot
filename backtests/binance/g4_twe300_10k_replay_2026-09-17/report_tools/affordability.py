#!/usr/bin/env python3
"""Offline live-admission ("can a 10k account afford an initial entry?") appendix.

A backtest admits every coin whose candles exist (`backtest.filter_by_min_effective_cost` is
disabled in this profile), but a live account also has to clear the exchange's minimum
notional value and quantity step. This tool answers that second question **entirely offline**
from the frozen bundle's own `market_specific_settings.json` (`min_cost`, `min_qty`,
`qty_step`, `contractSize`) plus the prices the arm actually traded at:

* per slot budget `WEL = total_wallet_exposure_limit / min(n_positions, coins)`;
* one initial entry costs `balance * WEL * (1 + allowance) * initial_qty_pct`;
* the entry is admissible when, after snapping the quantity to `qty_step`, the notional is at
  least `min_cost` and the quantity at least `min_qty`;
* `min_balance_required` is the balance at which that holds exactly.

The numbers are an estimate of the live admission rule, not a substitute for it: the live bot
computes `effective_min_cost` from the exchange's own contract data at the current price. The
live probe (`passivbot tool entry-regime-probe --balance N`) is the exact check and needs
public market data only; this appendix is the offline approximation that ships with the study.

Offline only: no network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_spec as study  # noqa: E402

#: How many decimals the reported per-coin numbers carry.
DIGITS = 4


def price_basis(fills_path: Path) -> dict[str, float]:
    """Per-coin median traded price from an arm's own ledger, used as the price basis."""
    if not fills_path.exists():
        return {}
    import statistics

    prices: dict[str, list[float]] = {}
    with fills_path.open(encoding="utf-8") as handle:
        header = handle.readline().strip().split(",")
        try:
            coin_index = header.index("coin")
            price_index = header.index("price")
        except ValueError:
            return {}
        for line in handle:
            parts = line.rstrip("\n").split(",")
            if len(parts) <= max(coin_index, price_index):
                continue
            try:
                price = float(parts[price_index])
            except ValueError:
                continue
            if price > 0:
                prices.setdefault(parts[coin_index], []).append(price)
    return {coin: statistics.median(values) for coin, values in prices.items() if values}


def affordability_row(
    coin: str,
    market: dict[str, Any],
    *,
    price: float | None,
    balance: float,
    per_slot_budget: float,
    entry_pct: float,
) -> dict[str, Any]:
    """One coin's admission numbers at the declared capital and exposure geometry."""
    min_cost = float(market.get("min_cost") or 0.0)
    min_qty = float(market.get("min_qty") or 0.0)
    qty_step = float(market.get("qty_step") or 0.0)
    contract_size = float(market.get("contractSize") or 1.0)
    entry_cost = balance * per_slot_budget * entry_pct
    row: dict[str, Any] = {
        "coin": coin,
        "min_cost": min_cost,
        "min_qty": min_qty,
        "qty_step": qty_step,
        "price_basis": price,
        "entry_cost_usd": entry_cost,
        "min_balance_required_usd": None,
        "planned_qty": None,
        "planned_notional_usd": None,
        "affordable": None,
        "reason": "",
    }
    if price is None or price <= 0:
        row["reason"] = "no traded price in this arm's ledger, no offline price basis"
        return row
    if per_slot_budget <= 0 or entry_pct <= 0:
        row["reason"] = "degenerate exposure geometry"
        return row
    # Balance needed for the *unrounded* entry to reach the exchange minimum notional, then
    # for the rounded quantity to clear min_qty.
    per_balance = per_slot_budget * entry_pct
    required_for_notional = min_cost / per_balance if per_balance > 0 else math.inf
    required_for_qty = (min_qty * price * contract_size) / per_balance if per_balance > 0 else math.inf
    row["min_balance_required_usd"] = max(required_for_notional, required_for_qty)
    raw_qty = entry_cost / (price * contract_size)
    if qty_step > 0:
        planned_qty = math.floor(raw_qty / qty_step) * qty_step
    else:
        planned_qty = raw_qty
    if planned_qty < min_qty and qty_step > 0:
        planned_qty = math.ceil(min_qty / qty_step) * qty_step
    row["planned_qty"] = planned_qty
    row["planned_notional_usd"] = planned_qty * price * contract_size
    row["affordable"] = bool(
        planned_qty >= min_qty and planned_qty * price * contract_size >= min_cost
    )
    if not row["affordable"]:
        if planned_qty < min_qty:
            row["reason"] = "rounded quantity below min_qty"
        else:
            row["reason"] = "rounded notional below min_cost"
    return row


def price_basis_for(variant: study.Variant) -> tuple[dict[str, float], Path | None, str]:
    """The arm's own traded prices, or the leg's control arm when coverage is thin.

    A liquidated arm trades for a few days, so its ledger covers only the coins it reached
    before the run ended. The admission question is about the basket as a whole, so when the
    arm's own coverage is thin the leg's scale control is used as the price basis and that
    substitution is recorded in the payload.
    """
    try:
        run_dir = study.find_variant_run_dir(variant)
    except SystemExit:
        return {}, None, "no run yet"
    own = price_basis(run_dir / "fills.csv")
    if len(own) >= 0.8 * study.COIN_COUNT:
        return own, run_dir / "fills.csv", "arm's own fills"
    control_key = f"twe100_10k__{variant.leg}"
    control = study.VARIANTS_BY_KEY.get(control_key)
    if control is not None and control.key != variant.key:
        try:
            control_dir = study.find_variant_run_dir(control)
            fallback = price_basis(control_dir / "fills.csv")
            if len(fallback) > len(own):
                return (
                    fallback,
                    control_dir / "fills.csv",
                    f"leg control arm {control_key} (this arm traded only {len(own)} coins)",
                )
        except SystemExit:
            pass
    return own, run_dir / "fills.csv", "arm's own fills"


def build(variant: study.Variant, dataset: study.DatasetSpec) -> dict[str, Any]:
    mss = study.load_json(dataset.path / "market_specific_settings.json")
    coins = [coin for coin in mss if coin != "__meta__"]
    prices, fills_path, basis_note = price_basis_for(variant)
    cfg = study.load_json(variant.config_path)
    risk = cfg["bot"]["long"]["risk"]
    entry_block = (
        ((cfg["bot"]["long"].get("strategy") or {}).get("trailing_martingale") or {}).get("entry")
        or {}
    )
    entry_pct = float(entry_block.get("initial_qty_pct") or 0.0)
    twe = float(risk["total_wallet_exposure_limit"])
    n_positions = float(risk["n_positions"])
    allowance_pct = float(risk["we_excess_allowance_pct"])
    balance = variant.starting_balance
    effective_n = min(n_positions, len(coins))
    per_slot_budget = (twe / effective_n) * (1.0 + allowance_pct)
    rows = [
        affordability_row(
            coin,
            mss.get(coin) or {},
            price=prices.get(coin),
            balance=balance,
            per_slot_budget=per_slot_budget,
            entry_pct=entry_pct,
        )
        for coin in sorted(coins)
    ]
    affordable = [row for row in rows if row["affordable"]]
    known = [row for row in rows if row["affordable"] is not None]
    return {
        "arm": variant.key,
        "leg": variant.leg,
        "dataset": dataset.rel_path.as_posix(),
        "starting_balance_usd": balance,
        "total_wallet_exposure_limit": twe,
        "n_positions": n_positions,
        "we_excess_allowance_pct": allowance_pct,
        "effective_n_positions": effective_n,
        "per_slot_budget_with_allowance": per_slot_budget,
        "entry_initial_qty_pct": entry_pct,
        "entry_cost_usd": balance * per_slot_budget * entry_pct,
        "price_basis": "per-coin median traded price (see price_basis_source)",
        "price_basis_source": basis_note,
        "fills_path": study.relative(fills_path) if fills_path else None,
        "coin_count": len(rows),
        "affordable_count": len(affordable),
        "coins_without_price_basis": sorted(
            row["coin"] for row in rows if row["affordable"] is None
        ),
        "min_balance_for_all_coins_usd": (
            max((row["min_balance_required_usd"] for row in known), default=None)
            if known
            else None
        ),
        "rows": rows,
        "note": (
            "离线近似：用冻结 bundle 的 min_cost/min_qty/qty_step/contractSize 与该 arm 的成交价"
            "中位数复算；实盘以 passivbot 的 effective_min_cost（按当前价格与交易所合约数据计算）为准。"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=study.RUN_VARIANT_ORDER)
    args = parser.parse_args(argv)
    variant = study.VARIANTS_BY_KEY[args.variant]
    payload = build(variant, variant.dataset)
    path = study.ARTIFACTS / f"affordability_{variant.key}.json"
    study.write_json(path, payload)
    print(f"wrote {study.relative(path)}")
    print(
        f"arm={variant.key} balance={payload['starting_balance_usd']:,.0f} "
        f"twe={payload['total_wallet_exposure_limit']:.2f} "
        f"entry={payload['entry_cost_usd']:,.2f} "
        f"affordable={payload['affordable_count']}/{payload['coin_count']} "
        f"min_balance_all={payload['min_balance_for_all_coins_usd']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
