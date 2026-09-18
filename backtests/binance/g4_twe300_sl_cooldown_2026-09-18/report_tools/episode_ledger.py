#!/usr/bin/env python3
"""Reconstruct every per-coin holding episode of a run from its own `fills.csv`.

This is the Phase-A workhorse: the ZEC question ("it kept adding, no risk control fired, did it
survive?") is answered from the ledger, not from memory. For each maximal stretch in which a coin's
position is non-zero it records

* the ladder: initial entry, every add (`entry_trailing_*`), every crop, and the average-entry path
  the adds imply (the average is constant between fills, which is what a moving stop level rides on);
* how the episode ended: which close types fired, in what order, and what they realized;
* the episode's net realized PnL and fees, plus the peak position and peak wallet exposure.

Everything is derived from `fills.csv` alone, so a re-run of this tool on the same run directory
reproduces the same numbers. Candle-based quantities (true low, maximum adverse excursion) live in
`candle_excursion.py`; nothing here guesses a price that is not in the ledger.

Offline only. No network, no credentials, no bot start.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_twe300_sl_cooldown_2026-09-18"
#: Round D study, whose arms this study compares against and whose evidence it cites.
ROUND_D = REPO / "backtests/binance/g4_twe300_risk_optimization_2026-09-17"
#: The arm the plan treats as the pinned baseline (allowance 0, TWE 3.0, unified RED 0.20 / 12H).
BASELINE_ARM = "a_allow000"

TIER_KEYS = ("initial", "add", "crop", "reduction")


def load_json(path: Path) -> Any:
    with Path(path).open() as handle:
        return json.load(handle)


def find_run_dir(arm: str, *, study: Path = ROUND_D) -> Path:
    """The single completed run directory of `arm`, refusing to guess between several."""
    root = Path(study) / "artifacts" / arm / "backtest_results"
    candidates = sorted(
        path for path in root.glob(f"binance_{arm}/binance/*") if (path / "analysis.json").exists()
    )
    if not candidates:
        raise SystemExit(f"no completed run for {arm} under {root}")
    if len(candidates) > 1:
        raise SystemExit(
            f"{arm} has {len(candidates)} run directories; pass --run-dir explicitly: "
            + ", ".join(path.name for path in candidates)
        )
    return candidates[0]


def load_fills(run_dir: Path) -> pd.DataFrame:
    run_dir = Path(run_dir)
    path = run_dir / "fills.csv"
    if not path.exists():
        raise SystemExit(f"{run_dir} has no fills.csv")
    frame = pd.read_csv(path)
    frame.columns = [str(name).strip() for name in frame.columns]
    if "timestamp" not in frame or "coin" not in frame:
        raise SystemExit(f"{path} lacks the timestamp/coin columns")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["type"] = frame["type"].astype(str)
    frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    return frame


def classify(fill_type: str) -> str:
    # A cropped *entry* is still an add; the crop flag is what matters for the ladder arithmetic,
    # so it is tested before the plain `entry_` prefix.
    if "cropped" in fill_type and fill_type.startswith("entry_"):
        return "crop"
    if fill_type.startswith("entry_initial"):
        return "initial"
    if fill_type.startswith("entry_"):
        return "add"
    return "reduction"


def episodes_for_coin(fills: pd.DataFrame, coin: str) -> list[dict[str, Any]]:
    rows = fills[fills["coin"].astype(str) == coin]
    if rows.empty:
        return []
    episodes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for _, row in rows.iterrows():
        position = float(row.get("psize") or 0.0)
        qty = float(row.get("qty") or 0.0)
        price = float(row.get("price") or 0.0)
        avg_price = float(row.get("pprice") or 0.0)
        exposure = float(row.get("wallet_exposure") or 0.0)
        balance = float(row.get("usd_total_balance") or 0.0)
        pnl = float(row.get("pnl") or 0.0)
        fee = float(row.get("fee_paid") or 0.0)
        kind = classify(str(row["type"]))
        if current is None:
            if position == 0.0 or qty <= 0.0:
                continue
            current = {
                "coin": coin,
                "start": row["timestamp"],
                "end": None,
                "balance_at_start": balance,
                "fills": 0,
                "initial_fills": 0,
                "add_fills": 0,
                "crop_fills": 0,
                "reduction_fills": 0,
                "reduction_types": {},
                "qty_max": 0.0,
                "exposure_max": 0.0,
                "balance_at_exposure_max": balance,
                "avg_price_first": avg_price,
                "avg_price_last": avg_price,
                "avg_price_min": avg_price,
                "avg_price_max": avg_price,
                "entry_price_first": price,
                "avg_path": [],
                "net_pnl_usd": 0.0,
                "fees_usd": 0.0,
                "gross_pnl_usd": 0.0,
                "realized_reduction_usd": 0.0,
            }
        current["fills"] += 1
        if kind == "initial":
            current["initial_fills"] += 1
        elif kind == "add":
            current["add_fills"] += 1
        elif kind == "crop":
            current["crop_fills"] += 1
            current["add_fills"] += 1
        else:
            current["reduction_fills"] += 1
            types = current["reduction_types"]
            types[str(row["type"])] = int(types.get(str(row["type"]), 0)) + 1
            current["realized_reduction_usd"] += pnl + fee
        current["net_pnl_usd"] += pnl + fee
        current["gross_pnl_usd"] += pnl
        current["fees_usd"] += fee
        if qty > 0.0 and avg_price > 0.0:
            current["avg_path"].append(
                {"ts": row["timestamp"].isoformat(), "avg_price": avg_price, "qty": position}
            )
        if position != 0.0:
            current["avg_price_last"] = avg_price
            if avg_price > 0.0:
                current["avg_price_min"] = min(current["avg_price_min"], avg_price)
                current["avg_price_max"] = max(current["avg_price_max"], avg_price)
        if abs(position) > current["qty_max"]:
            current["qty_max"] = abs(position)
        if abs(exposure) > current["exposure_max"]:
            current["exposure_max"] = abs(exposure)
            current["balance_at_exposure_max"] = balance
        if position == 0.0:
            current["end"] = row["timestamp"]
            current["close_type"] = str(row["type"])
            episodes.append(current)
            current = None
    if current is not None:
        current["end"] = None
        current["close_type"] = None
        current["open_at_window_end"] = True
        episodes.append(current)
    for episode in episodes:
        start = episode["start"]
        end = episode["end"] or rows["timestamp"].iloc[-1]
        episode["start"] = start.isoformat()
        episode["end"] = None if episode["end"] is None else end.isoformat()
        episode["minutes"] = round((end - start).total_seconds() / 60.0, 1)
        for key in ("avg_price_first", "avg_price_last", "avg_price_min", "avg_price_max"):
            episode[key] = round(float(episode[key]), 6)
        episode["qty_max"] = round(float(episode["qty_max"]), 6)
        episode["exposure_max"] = round(float(episode["exposure_max"]), 6)
        for key in ("net_pnl_usd", "fees_usd", "gross_pnl_usd", "realized_reduction_usd"):
            episode[key] = round(float(episode[key]), 4)
    return episodes


def coin_summary(fills: pd.DataFrame, coin: str) -> dict[str, Any]:
    rows = fills[fills["coin"].astype(str) == coin]
    if rows.empty:
        return {"coin": coin, "fills": 0}
    maker = int((rows["liquidity"].astype(str) == "maker").sum()) if "liquidity" in rows else None
    return {
        "coin": coin,
        "fills": int(len(rows)),
        "entries": int(rows["type"].astype(str).str.startswith("entry_").sum()),
        "reductions": int((~rows["type"].astype(str).str.startswith("entry_")).sum()),
        "maker_fills": maker,
        "taker_fills": None if maker is None else int(len(rows)) - maker,
        "net_realized_pnl_usd": round(float((rows["pnl"] + rows["fee_paid"]).sum()), 4),
        "first_fill": rows["timestamp"].iloc[0].isoformat(),
        "last_fill": rows["timestamp"].iloc[-1].isoformat(),
    }


def build_ledger(run_dir: Path, coins: Sequence[str] | None = None) -> dict[str, Any]:
    fills = load_fills(run_dir)
    wanted = list(coins) if coins else sorted(fills["coin"].astype(str).unique())
    episodes: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for coin in wanted:
        episodes.extend(episodes_for_coin(fills, coin))
        if len(fills[fills["coin"].astype(str) == coin]):
            summaries.append(coin_summary(fills, coin))
    episodes.sort(key=lambda item: (item["coin"], item["start"]))
    summaries.sort(key=lambda item: -item.get("net_realized_pnl_usd", 0.0))
    return {
        "run_dir": str(Path(run_dir).relative_to(REPO)),
        "coins": wanted,
        "episode_count": len(episodes),
        "method": (
            "episode = 该币 psize 从 0 变非 0 到回到 0 的最长连续成交段（窗口末仍持仓则标记 "
            "open_at_window_end）；均价路径取自每笔入场成交后的 pprice（两次成交之间均价不变）；"
            "净已实现 = sum(pnl + fee_paid)。全部来自 fills.csv，不含任何蜡烛推测。"
        ),
        "episodes": episodes,
        "coin_summaries": summaries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", default=BASELINE_ARM)
    parser.add_argument("--study", default=str(ROUND_D))
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--coin", action="append", default=[])
    parser.add_argument("--out", default=None)
    parser.add_argument("--top", type=int, default=5, help="how many rows to print")
    parser.add_argument(
        "--top-by",
        choices=("qty", "exposure", "net", "fills"),
        default="qty",
        help="which episode dimension the printed table sorts by",
    )
    parser.add_argument("--around", default=None, help="only episodes overlapping this UTC window, e.g. 2026-06")
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir) if args.run_dir else find_run_dir(args.arm, study=Path(args.study))
    arm = run_dir.parents[3].name if len(run_dir.parents) > 3 else run_dir.parent.name
    ledger = build_ledger(run_dir, args.coin or None)
    ledger["arm"] = arm
    out = (
        Path(args.out)
        if args.out
        else STUDY / "artifacts/optimism" / f"{arm}__episodes.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ledger, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(REPO)} ({ledger['episode_count']} episodes, arm={arm})")
    for summary in ledger["coin_summaries"][: args.top]:
        print(
            f"  {summary['coin']:6s} fills={summary['fills']:5d} net={summary['net_realized_pnl_usd']:>10.2f}"
        )
    episodes = ledger["episodes"]
    if args.around:
        start = pd.Timestamp(args.around + "-01", tz="UTC") if len(args.around) == 7 else pd.Timestamp(args.around, tz="UTC")
        end = start + pd.DateOffset(months=1) if len(args.around) == 7 else start + pd.Timedelta(days=1)
        episodes = [
            episode
            for episode in episodes
            if pd.Timestamp(episode["start"]) < end
            and (episode["end"] is None or pd.Timestamp(episode["end"]) >= start)
        ]
    key = {"qty": "qty_max", "exposure": "exposure_max", "net": "net_pnl_usd", "fills": "fills"}[args.top_by]
    episodes = sorted(episodes, key=lambda item: abs(float(item.get(key) or 0.0)), reverse=True)
    for episode in episodes[: args.top]:
        print(
            f"  {episode['coin']:6s} {episode['start']} .. {episode['end']} "
            f"fills={episode['fills']:4d} add={episode['add_fills']:3d} crops={episode['crop_fills']:2d} "
            f"red={episode['reduction_fills']:3d} qty_max={episode['qty_max']:>10.4f} "
            f"exp_max={episode['exposure_max']:.4f} "
            f"avg {episode['avg_price_first']:.2f}->{episode['avg_price_last']:.2f} "
            f"net={episode['net_pnl_usd']:>10.2f} close={episode.get('close_type')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
