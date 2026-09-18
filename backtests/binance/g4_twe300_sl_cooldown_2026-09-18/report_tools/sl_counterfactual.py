#!/usr/bin/env python3
"""Price the proposed stop on the run's own history: what it would have realized, what it would
have avoided, and what the interrupted episodes actually earned instead.

`candle_excursion.py` finds the breaches; this tool answers the only question that decides the
proposal. For every breach of `avg x (1 - level)` it computes

* the exit the stop would have got, in two tiers: a **limit** resting at the level (the level
  itself, maker fee) and a **market** close after a gap through (`breach_low`, taker fee plus the
  run's modelled slippage). Both are reported and neither is averaged away, because a limit stop
  that gaps through is not a stop at all, and a market stop in a cascade is not filled at the
  level either;
* the realized PnL of that exit against the average entry in force at the breach;
* what the baseline run actually realized on the same episode, and the difference -- the direct
  cost of stopping out;
* how far under water the position went after the exit, i.e. the damage the stop avoided at the
  worst point, which is what an insurance premium buys;
* what re-entry cost at cooldown expiry, so "exit and wait" can be judged against "hold".

Input is the excursion report, so this tool needs no candle bundle and runs in a second.

Offline only. No network, no credentials, no bot start.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import episode_ledger as ledger  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_twe300_sl_cooldown_2026-09-18"
#: Fee model of the frozen run: maker 2bp, taker 5bp, plus this slippage on a market order.
MAKER_FEE = 0.0002
TAKER_FEE = 0.0005
MARKET_SLIPPAGE = 0.0005


def load_report(path: Path) -> dict[str, Any]:
    with Path(path).open() as handle:
        return json.load(handle)


def price_breach(
    episode: dict[str, Any],
    detail: dict[str, Any],
    *,
    market_order: bool = False,
) -> dict[str, Any]:
    """One breached episode as a stop-out: exit fill, realized PnL, and the counterfactual ledger."""
    avg = float(detail["avg_price_at_breach"])
    level = float(detail["level"])
    qty = float(detail["qty_at_breach"])
    breach_low = float(detail["breach_low"])
    if market_order:
        # A market close after the level has already traded through fills at the worst printed
        # price of the trigger candle, less modelled slippage, and pays taker.
        fill = breach_low * (1.0 - MARKET_SLIPPAGE)
        fee_rate = TAKER_FEE
        tier = "market_gap_through"
    else:
        # A limit resting at the level fills at the level while the market trades there, and pays
        # maker. If the candle gapped straight through, this tier is optimistic by construction --
        # which is exactly why the market tier exists and why only the worse of the two may
        # support a verdict.
        fill = level
        fee_rate = MAKER_FEE
        tier = "limit_at_level"
    gross = qty * (fill - avg)
    fee = qty * fill * fee_rate
    realized = gross - fee
    baseline_net = float(episode["net_pnl_usd"])
    balance = float(episode.get("balance_at_exposure_max") or 0.0)
    # What the position was worth at its worst after the exit: the damage the stop avoided.
    worst_low = float(detail["min_low_to_episode_end"])
    avoided_at_worst = max(0.0, qty * (fill - worst_low))
    expiry = detail.get("close_at_cooldown_expiry")
    adds = detail.get("adds_within_cooldown") or {}
    return {
        "coin": episode["coin"],
        "start": episode["start"],
        "end": episode["end"],
        "tier": tier,
        "breach_ts": detail["breach_ts"],
        "level": round(level, 6),
        "avg_price_at_breach": round(avg, 6),
        "qty_at_breach": qty,
        "exit_fill": round(fill, 6),
        "exit_notional_usd": round(qty * fill, 2),
        "sl_realized_pnl_usd": round(realized, 2),
        "sl_realized_pct_of_balance": (
            None if balance <= 0.0 else round(100.0 * realized / balance, 4)
        ),
        "baseline_episode_net_usd": round(baseline_net, 2),
        "stop_cost_vs_baseline_usd": round(baseline_net - realized, 2),
        "worst_low_after_exit": round(worst_low, 6),
        "avoided_at_worst_usd": round(avoided_at_worst, 2),
        "close_at_cooldown_expiry": expiry,
        "reentry_premium_pct": (
            None if not expiry else round(100.0 * (float(expiry) / fill - 1.0), 4)
        ),
        "exposure_max": episode["exposure_max"],
        "cancelled_adds": adds.get("count"),
        "cancelled_add_notional_usd": adds.get("notional"),
        "cancelled_adds_below_level": adds.get("count_below_level"),
        "balance_at_exposure_max": balance or None,
        "close_type_baseline": episode.get("close_type"),
    }


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    cost = np.asarray([row["stop_cost_vs_baseline_usd"] for row in rows], dtype="float64")
    realized = np.asarray([row["sl_realized_pnl_usd"] for row in rows], dtype="float64")
    baseline = np.asarray([row["baseline_episode_net_usd"] for row in rows], dtype="float64")
    avoided = np.asarray([row["avoided_at_worst_usd"] for row in rows], dtype="float64")
    premiums = np.asarray(
        [row["reentry_premium_pct"] for row in rows if row["reentry_premium_pct"] is not None],
        dtype="float64",
    )
    return {
        "breaches": len(rows),
        "sum_sl_realized_pnl_usd": round(float(realized.sum()), 2),
        "sum_baseline_episode_net_usd": round(float(baseline.sum()), 2),
        "sum_stop_cost_vs_baseline_usd": round(float(cost.sum()), 2),
        "median_stop_cost_vs_baseline_usd": round(float(np.median(cost)), 2),
        "episodes_stop_cost_positive": int((cost > 0.0).sum()),
        "episodes_stop_cost_positive_pct": round(100.0 * float((cost > 0.0).mean()), 2),
        "sum_avoided_at_worst_usd": round(float(avoided.sum()), 2),
        "median_reentry_premium_pct": (
            None if premiums.size == 0 else round(float(np.median(premiums)), 4)
        ),
        "share_reentry_above_exit_pct": (
            None if premiums.size == 0 else round(100.0 * float((premiums > 0.0).mean()), 2)
        ),
        "sum_cancelled_add_notional_usd": round(
            sum(float(row["cancelled_add_notional_usd"] or 0.0) for row in rows), 2
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", default=ledger.BASELINE_ARM)
    parser.add_argument("--study", default=str(ledger.ROUND_D))
    parser.add_argument(
        "--report",
        default=None,
        help="excursion report to price; defaults to the study's own <arm>__excursion.json",
    )
    parser.add_argument("--level", type=float, default=0.15)
    parser.add_argument("--out", default=None)
    parser.add_argument("--top", type=int, default=8, help="rows to print per direction")
    args = parser.parse_args(argv)
    report_path = (
        Path(args.report)
        if args.report
        else STUDY / "artifacts/optimism" / f"{args.arm}__excursion.json"
    )
    report = load_report(report_path)
    level_key = str(args.level)
    episodes = [
        episode
        for episode in report["episodes_with_breaches"]
        if level_key in episode["breaches"]
    ]
    if not episodes:
        raise SystemExit(f"{report_path} has no episode breaching level {args.level}")
    limit_rows = [
        price_breach(episode, episode["breaches"][level_key], market_order=False)
        for episode in episodes
    ]
    market_rows = [
        price_breach(episode, episode["breaches"][level_key], market_order=True)
        for episode in episodes
    ]
    result = {
        "arm": args.arm,
        "report": str(report_path),
        "level": args.level,
        "cooldown_minutes": report.get("cooldown_minutes"),
        "fee_model": {
            "maker_fee": MAKER_FEE,
            "taker_fee": TAKER_FEE,
            "market_order_slippage": MARKET_SLIPPAGE,
        },
        "method": (
            "对 excursion 报告中每一条跌破记录，按两层成交假设计算止损平仓的已实现盈亏："
            "limit_at_level = 挂在该位的限价单按该位成交、付 maker；market_gap_through = 触发 K 线已经"
            "跌穿该位，市价按该 K 线最低价再减 0.05% 滑点成交、付 taker。stop_cost_vs_baseline = "
            "该 episode 基准实际净盈亏 − 止损已实现盈亏（>0 表示止损更差）；avoided_at_worst = 平仓后"
            "价格最低点相对成交价少亏的金额（即该止损在下一次反弹前避开的伤害）；reentry_premium = "
            "冷却到期时价格相对成交价的溢价（>0 表示再进场比出场更贵）。只报数，不合成结论。"
        ),
        "limit_tier": summarize(limit_rows),
        "market_tier": summarize(market_rows),
        "rows": limit_rows,
        "rows_market": market_rows,
    }
    out = Path(args.out) if args.out else STUDY / "artifacts/optimism" / f"{args.arm}__sl_counterfactual.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    for tier in ("limit_tier", "market_tier"):
        print(f"\n{tier}: {json.dumps(result[tier], ensure_ascii=False)}")
    for label, rows, key in (
        ("most costly stop-outs (baseline earned more)", limit_rows, False),
        ("stop-outs that avoided the most at the trough", limit_rows, True),
    ):
        ranked = sorted(
            rows,
            key=lambda row: row["avoided_at_worst_usd"] if key else row["stop_cost_vs_baseline_usd"],
            reverse=True,
        )[: args.top]
        print(f"\n{label}:")
        for row in ranked:
            print(
                f"  {row['coin']:6s} {row['breach_ts'][:16]} avg={row['avg_price_at_breach']:.4f} "
                f"L={row['level']:.4f} qty={row['qty_at_breach']:.4f} exit={row['exit_fill']:.4f} "
                f"sl={row['sl_realized_pnl_usd']:>10.2f} base={row['baseline_episode_net_usd']:>10.2f} "
                f"cost={row['stop_cost_vs_baseline_usd']:>10.2f} avoided={row['avoided_at_worst_usd']:>10.2f} "
                f"prem={row['reentry_premium_pct']} exp={row['exposure_max']:.3f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
