#!/usr/bin/env python3
"""Measure every holding episode against real candles: maximum adverse excursion, and the
breach / cooldown census a stop loss lives or dies by.

`episode_ledger.py` reconstructs what a run *did*; this tool asks what the *market* did while the
position was open, which is the only way to answer the questions a stop-loss proposal needs:

1. **How deep did it really go?** The ledger only knows fill prices, and for a martingale that adds
   on the way down the fills are a poor proxy for the excursion: the June-2026 ZEC episode's last
   fill printed 391.72 while the market low that day was 250.00.
2. **If a stop sat at `avg x (1 - pct)`, when would it have fired, and what would it have done?**
   For every breach the census reports how much further the market fell inside the cooldown window,
   the close when the cooldown expires, whether the close came back above the level, and how many
   entry fills the stop would have cancelled. That is the measured form of the repository's own
   design precedent: `equity_hard_stop_loss.rs` picks `min(raw, ema)` precisely so a wick does not
   end the run, whereas a coin-level stop is a hard level and fires on wicks by construction.

Two distinct outcomes are counted, because conflating them was a defect of the first draft:

* `spike_exit` -- the market dipped barely below the level and was back above it inside the cooldown:
  the stop sold the low, and 24h of no re-entry is pure cost;
* `good_exit` -- the market fell `GOOD_EXIT_FURTHER_DROP` or more below the level: the stop avoided
  real damage.

Both thresholds are declared constants below, so a census percentage means exactly one thing.

Writes `artifacts/optimism/<arm>__excursion.json` (tracked: census + every breached episode +
the deepest excursions) and optionally every episode to an untracked file.

Offline only. No network, no credentials, no bot start.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import candle_source as candles  # noqa: E402
import episode_ledger as ledger  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_twe300_sl_cooldown_2026-09-18"
#: Levels the census reports. 0.15 is the proposal under study; the others bracket it.
DEFAULT_LEVELS = (0.10, 0.15, 0.20, 0.25)
#: A breach the market barely went below, then left behind: the stop sold the low. Declared.
SPOT_EXIT_FURTHER_DROP = 0.02
#: A breach the market then fell this far below: the stop avoided real damage. Declared.
GOOD_EXIT_FURTHER_DROP = 0.05
#: How many of the deepest excursions stay in the tracked report.
DEEPEST_KEPT = 25


def to_ns(value: Any) -> int:
    """UTC nanoseconds since epoch, the integer unit every window bound is expressed in."""
    ts = pd.Timestamp(value)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return int(ts.value)


def display(path: Path) -> str:
    """A repo-relative path when the target is inside the repo, so logging stays portable."""
    path = Path(path)
    try:
        return str(path.resolve().relative_to(REPO))
    except ValueError:
        return str(path)


class CoinCandles:
    """One coin's candles as plain arrays, so a window is a `searchsorted` pair, not a mask.

    The per-coin slice of the 3-year bundle is ~1.6 M minutes; a boolean mask per entry fill per
    episode would be billions of comparisons across a 40-coin census, whereas array bounds make the
    whole census a few seconds.
    """

    __slots__ = ("ts", "high", "low", "close")

    def __init__(self, frame: pd.DataFrame) -> None:
        index = frame.index
        self.ts = np.asarray(index.asi8 if hasattr(index, "asi8") else index.view("int64"))
        self.high = frame["high"].to_numpy(dtype="float64", copy=False)
        self.low = frame["low"].to_numpy(dtype="float64", copy=False)
        self.close = frame["close"].to_numpy(dtype="float64", copy=False)

    def __len__(self) -> int:
        return int(self.ts.size)

    def left(self, ts_ns: int) -> int:
        """First candle index at or after `ts_ns`."""
        return int(np.searchsorted(self.ts, ts_ns, side="left"))

    def right(self, ts_ns: int) -> int:
        """First candle index after `ts_ns`."""
        return int(np.searchsorted(self.ts, ts_ns, side="right"))

    def clamp(self, index: int) -> int:
        return max(0, min(int(index), self.ts.size))

    def price_at(self, ts_ns: int) -> float | None:
        """The close in force at `ts_ns`, or None when that is past the candle series."""
        if self.ts.size == 0 or ts_ns > int(self.ts[-1]):
            return None
        position = self.right(ts_ns) - 1
        if position < 0:
            return None
        return float(self.close[position])


def adverse_excursion(
    coin_candles: CoinCandles,
    avg_path: Sequence[dict[str, Any]],
    *,
    window: tuple[int, int],
) -> dict[str, Any]:
    """Deepest `1 - low / avg` over the episode, with the average held piecewise constant.

    `avg_path` is the ledger's `[{ts, avg_price, qty}, ...]`, one entry per entry fill, so the
    average in force at any minute is the last one at or before it.
    """
    start_index, end_index = window
    if end_index <= start_index or not avg_path:
        return {}
    marks = [
        (to_ns(item["ts"]), float(item["avg_price"]), float(item["qty"]))
        for item in avg_path
        if float(item["avg_price"]) > 0.0
    ]
    if not marks:
        return {}
    worst: dict[str, Any] = {"ratio": 0.0, "ts": None, "low": None, "avg": None, "qty": None}
    for index, (ts_ns, avg_price, qty) in enumerate(marks):
        lower = coin_candles.clamp(coin_candles.left(ts_ns))
        upper = (
            coin_candles.clamp(coin_candles.left(marks[index + 1][0]))
            if index + 1 < len(marks)
            else end_index
        )
        lower = max(lower, start_index)
        upper = min(upper, end_index)
        if upper <= lower:
            continue
        segment = coin_candles.low[lower:upper]
        offset = int(np.argmin(segment))
        low = float(segment[offset])
        ratio = max(0.0, 1.0 - low / avg_price)
        if ratio > worst["ratio"]:
            worst = {
                "ratio": round(ratio, 6),
                "ts": pd.Timestamp(int(coin_candles.ts[lower + offset]), unit="ns", tz="UTC").isoformat(),
                "low": round(low, 6),
                "avg": round(avg_price, 6),
                "qty": qty,
            }
    episode_low = coin_candles.low[start_index:end_index]
    episode_high = coin_candles.high[start_index:end_index]
    worst["episode_min_low"] = round(float(episode_low.min()), 6)
    worst["episode_min_low_ts"] = pd.Timestamp(
        int(coin_candles.ts[start_index + int(np.argmin(episode_low))]), unit="ns", tz="UTC"
    ).isoformat()
    worst["episode_max_high"] = round(float(episode_high.max()), 6)
    return worst


def breach_detail(
    coin_candles: CoinCandles,
    avg_path: Sequence[dict[str, Any]],
    adds: Sequence[tuple[int, float, float]],
    *,
    window: tuple[int, int],
    level_pct: float,
    cooldown_minutes: float,
) -> dict[str, Any] | None:
    """The first breach of `avg x (1 - level_pct)`, and what the stop plus its cooldown would see.

    `adds` are the coin's entry fills as `(ts_ns, price, qty)`; they are what the stop would cancel
    inside the cooldown, so they are counted rather than assumed away.
    """
    start_index, end_index = window
    if end_index <= start_index or not avg_path:
        return None
    marks = [
        (to_ns(item["ts"]), float(item["avg_price"]), float(item["qty"]))
        for item in avg_path
        if float(item["avg_price"]) > 0.0
    ]
    for index, (ts_ns, avg_price, qty) in enumerate(marks):
        lower = max(coin_candles.clamp(coin_candles.left(ts_ns)), start_index)
        upper = (
            min(coin_candles.clamp(coin_candles.left(marks[index + 1][0])), end_index)
            if index + 1 < len(marks)
            else end_index
        )
        if upper <= lower:
            continue
        level = avg_price * (1.0 - level_pct)
        segment = coin_candles.low[lower:upper]
        below = np.nonzero(segment <= level)[0]
        if below.size == 0:
            continue
        breach_index = lower + int(below[0])
        breach_ts_ns = int(coin_candles.ts[breach_index])
        expiry_ns = breach_ts_ns + int(round(cooldown_minutes * 60e9))

        # Primary horizon: the cooldown window. That is exactly the interval in which the stop's
        # decision has consequences, because after the expiry the arm is free to trade again.
        cooldown_end = coin_candles.clamp(coin_candles.right(expiry_ns))
        cooldown_end = max(cooldown_end, breach_index + 1)
        damage = coin_candles.low[breach_index:cooldown_end]
        damage_offset = int(np.argmin(damage))
        min_low = float(damage[damage_offset])
        min_low_ts_ns = int(coin_candles.ts[breach_index + damage_offset])
        further_drop = max(0.0, 1.0 - min_low / level)

        closes = coin_candles.close[breach_index:cooldown_end]
        recovered = bool(np.any(closes >= level)) if closes.size else None
        # Secondary horizon: the whole episode, i.e. how deep it went while the position was on.
        episode_damage = coin_candles.low[breach_index:end_index]
        episode_offset = int(np.argmin(episode_damage))
        episode_min_low = float(episode_damage[episode_offset])
        close_at_expiry = coin_candles.price_at(expiry_ns)

        inside = [
            (price, size) for (add_ts, price, size) in adds if breach_ts_ns < add_ts <= expiry_ns
        ]
        below_level = [(price, size) for (price, size) in inside if price < level]
        return {
            "level_pct": level_pct,
            "level": round(level, 6),
            "avg_price_at_breach": round(avg_price, 6),
            "qty_at_breach": qty,
            "breach_ts": pd.Timestamp(breach_ts_ns, unit="ns", tz="UTC").isoformat(),
            "breach_low": round(float(coin_candles.low[breach_index]), 6),
            "min_low_to_cooldown_expiry": round(min_low, 6),
            "min_low_to_cooldown_expiry_ts": pd.Timestamp(
                min_low_ts_ns, unit="ns", tz="UTC"
            ).isoformat(),
            "further_drop_pct": round(further_drop, 6),
            "min_low_to_episode_end": round(episode_min_low, 6),
            "min_low_to_episode_end_ts": pd.Timestamp(
                int(coin_candles.ts[breach_index + episode_offset]), unit="ns", tz="UTC"
            ).isoformat(),
            "episode_further_drop_pct": round(max(0.0, 1.0 - episode_min_low / level), 6),
            "close_at_cooldown_expiry": close_at_expiry,
            "close_at_expiry_vs_level_pct": (
                None if close_at_expiry is None else round(close_at_expiry / level - 1.0, 6)
            ),
            "recovered_above_level_within_cooldown": recovered,
            "spike_exit": bool(further_drop <= SPOT_EXIT_FURTHER_DROP and recovered),
            "good_exit": bool(further_drop >= GOOD_EXIT_FURTHER_DROP),
            "adds_within_cooldown": {
                "count": len(inside),
                "count_below_level": len(below_level),
                "qty": round(sum(size for _, size in inside), 6),
                "qty_below_level": round(sum(size for _, size in below_level), 6),
                "notional": round(sum(price * size for price, size in inside), 4),
                "notional_below_level": round(sum(price * size for price, size in below_level), 4),
            },
        }
    return None


def excursion_summary(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """How deep the measured episodes actually went, and which coins own the deep tail.

    The census above answers "would a stop have fired"; this answers "how bad does it get", which is
    the question the stop is a proposed answer to. Both are needed: a stop that fires often on
    shallow dips and never on the deep ones is worthless, and that is not visible from either
    number alone.
    """
    scored = [
        (item["coin"], float((item["excursion"] or {}).get("ratio") or 0.0))
        for item in results
    ]
    if not scored:
        return {}
    series = np.asarray([ratio for _, ratio in scored], dtype="float64")
    per_coin: dict[str, float] = {}
    for coin, ratio in scored:
        per_coin[coin] = max(per_coin.get(coin, 0.0), ratio)
    deepest_coins = sorted(per_coin.items(), key=lambda item: -item[1])[:10]
    return {
        "episodes": int(series.size),
        "median_mae_pct": round(float(np.median(series)) * 100, 4),
        "p75_mae_pct": round(float(np.percentile(series, 75)) * 100, 4),
        "p90_mae_pct": round(float(np.percentile(series, 90)) * 100, 4),
        "p95_mae_pct": round(float(np.percentile(series, 95)) * 100, 4),
        "p99_mae_pct": round(float(np.percentile(series, 99)) * 100, 4),
        "max_mae_pct": round(float(series.max()) * 100, 4),
        "episodes_over_pct": {
            f"{level:.2f}": int((series >= level).sum()) for level in (0.10, 0.15, 0.20, 0.25, 0.30)
        },
        "deepest_coins_max_mae_pct": {
            coin: round(ratio * 100, 4) for coin, ratio in deepest_coins
        },
    }


def census(entries: Sequence[dict[str, Any]], level_pct: float) -> dict[str, Any]:
    """What a stop at `level_pct` would have met, and what the stopped episodes actually earned.

    The episode PnL columns are the point of the whole exercise: a stop is only worth its cost if
    the episodes it interrupts were losing ones. A census that reports fire frequency without the
    realized outcome of the episodes it fires in invites exactly the wrong conclusion.
    """
    pairs = [
        (entry, entry["breaches"][str(level_pct)])
        for entry in entries
        if entry.get("breaches", {}).get(str(level_pct))
    ]
    rows = [detail for _, detail in pairs]
    if not rows:
        return {"level_pct": level_pct, "breaches": 0, "episodes": len(entries)}
    further = [row["further_drop_pct"] for row in rows]
    episode_further = [row["episode_further_drop_pct"] for row in rows]
    recovered = [row for row in rows if row["recovered_above_level_within_cooldown"]]
    spike = [row for row in rows if row["spike_exit"]]
    good = [row for row in rows if row["good_exit"]]
    with_adds = [row for row in rows if row["adds_within_cooldown"]["count"]]
    below = [row for row in rows if row["adds_within_cooldown"]["count_below_level"]]
    nets = np.asarray([float(entry["net_pnl_usd"]) for entry, _ in pairs], dtype="float64")
    exposures = np.asarray([float(entry["exposure_max"]) for entry, _ in pairs], dtype="float64")
    expiries = np.asarray(
        [
            float(row["close_at_expiry_vs_level_pct"])
            for row in rows
            if row["close_at_expiry_vs_level_pct"] is not None
        ],
        dtype="float64",
    )
    return {
        "level_pct": level_pct,
        "breaches": len(rows),
        "episodes": len(entries),
        "share_of_episodes_pct": round(100.0 * len(rows) / max(len(entries), 1), 2),
        "spike_exit": len(spike),
        "spike_exit_share_pct": round(100.0 * len(spike) / len(rows), 2),
        "good_exit": len(good),
        "good_exit_share_pct": round(100.0 * len(good) / len(rows), 2),
        "recovered_above_level_within_cooldown": len(recovered),
        "recovered_share_pct": round(100.0 * len(recovered) / len(rows), 2),
        "median_further_drop_pct": round(float(np.median(further)), 6),
        "mean_further_drop_pct": round(float(np.mean(further)), 6),
        "max_further_drop_pct": round(float(np.max(further)), 6),
        "median_episode_further_drop_pct": round(float(np.median(episode_further)), 6),
        "max_episode_further_drop_pct": round(float(np.max(episode_further)), 6),
        "breaches_with_cancelled_adds": len(with_adds),
        "breaches_with_cancelled_adds_pct": round(100.0 * len(with_adds) / len(rows), 2),
        "breaches_with_cancelled_adds_below_level": len(below),
        "cancelled_add_notional_usd": round(
            sum(row["adds_within_cooldown"]["notional"] for row in rows), 2
        ),
        "cancelled_add_notional_below_level_usd": round(
            sum(row["adds_within_cooldown"]["notional_below_level"] for row in rows), 2
        ),
        # What the interrupted episodes actually earned, and how the market stood when the
        # cooldown expired. Together these say whether the stop was insurance or a tax.
        "breached_episodes_net_positive": int((nets > 0.0).sum()),
        "breached_episodes_net_positive_pct": round(100.0 * float((nets > 0.0).sum()) / len(nets), 2),
        "breached_episodes_net_sum_usd": round(float(nets.sum()), 2),
        "breached_episodes_net_median_usd": round(float(np.median(nets)), 2),
        "breached_episodes_net_win_sum_usd": round(float(nets[nets > 0.0].sum()), 2),
        "breached_episodes_net_loss_sum_usd": round(float(nets[nets < 0.0].sum()), 2),
        "median_exposure_max": round(float(np.median(exposures)), 6),
        "median_close_at_expiry_vs_level_pct": (
            None if expiries.size == 0 else round(float(np.median(expiries)), 6)
        ),
        "share_close_at_expiry_below_level_pct": (
            None if expiries.size == 0 else round(100.0 * float((expiries < 0.0).mean()), 2)
        ),
    }


def breach_clusters(
    entries: Sequence[dict[str, Any]], level_pct: float, *, window_hours: float = 72.0
) -> list[dict[str, Any]]:
    """Breaches grouped in time: a stop defends against correlation, not against single coins.

    One coin dipping 15% is noise; five coins dipping 15% inside three days is the event a
    per-coin stop is meant for. The grouping window is declared here so the count means one thing.
    """
    rows = [
        (entry, entry["breaches"][str(level_pct)])
        for entry in entries
        if entry.get("breaches", {}).get(str(level_pct))
    ]
    rows.sort(key=lambda item: item[1]["breach_ts"])
    span_ns = int(window_hours * 3600e9)
    clusters: list[dict[str, Any]] = []
    current: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry, detail in rows:
        if current and to_ns(detail["breach_ts"]) - to_ns(current[0][1]["breach_ts"]) > span_ns:
            clusters.append(current)
            current = []
        current.append((entry, detail))
    if current:
        clusters.append(current)
    out: list[dict[str, Any]] = []
    for group in clusters:
        coins = sorted({entry["coin"] for entry, _ in group})
        if len(coins) < 2:
            continue
        out.append(
            {
                "level_pct": level_pct,
                "first_breach": group[0][1]["breach_ts"],
                "last_breach": group[-1][1]["breach_ts"],
                "coins": coins,
                "breach_count": len(group),
                "exposure_sum": round(sum(float(entry["exposure_max"]) for entry, _ in group), 4),
                "net_pnl_usd_sum": round(sum(float(entry["net_pnl_usd"]) for entry, _ in group), 2),
                "max_further_drop_pct": round(
                    max(row["further_drop_pct"] for _, row in group), 6
                ),
                "details": [
                    {
                        "coin": entry["coin"],
                        "exposure_max": entry["exposure_max"],
                        "net_pnl_usd": entry["net_pnl_usd"],
                        "breach_ts": row["breach_ts"],
                        "level": row["level"],
                        "further_drop_pct": row["further_drop_pct"],
                        "close_at_cooldown_expiry": row["close_at_cooldown_expiry"],
                        "cancelled_add_notional_usd": row["adds_within_cooldown"]["notional"],
                    }
                    for entry, row in group
                ],
            }
        )
    return out


def coin_adds(run_dir: Path) -> dict[str, list[tuple[int, float, float]]]:
    """Every entry fill of the run, per coin, as `(ts_ns, price, qty)`."""
    fills = ledger.load_fills(run_dir)
    by_coin: dict[str, list[tuple[int, float, float]]] = {}
    for row in fills.itertuples(index=False):
        if ledger.classify(str(row.type)) == "reduction":
            continue
        by_coin.setdefault(str(row.coin), []).append(
            (int(pd.Timestamp(row.timestamp).value), float(row.price), float(row.qty))
        )
    for entries in by_coin.values():
        entries.sort()
    return by_coin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", default=ledger.BASELINE_ARM)
    parser.add_argument("--study", default=str(ledger.ROUND_D))
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--coin", action="append", default=[])
    parser.add_argument("--level", action="append", type=float, default=[])
    parser.add_argument("--cooldown-minutes", type=float, default=1440.0)
    parser.add_argument("--min-qty", type=float, default=0.0, help="skip episodes below this peak size")
    parser.add_argument(
        "--min-exposure",
        type=float,
        default=0.05,
        help=(
            "skip episodes whose peak wallet exposure is below this share of the balance; the "
            "default keeps positions big enough for a stop to matter, and unlike --min-qty it is "
            "comparable across coins"
        ),
    )
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--cluster-level",
        type=float,
        default=0.15,
        help="level whose breaches are grouped into correlated clusters (the level under study)",
    )
    parser.add_argument(
        "--cluster-window-hours",
        type=float,
        default=72.0,
        help="breaches of at least two coins inside this window count as one correlated event",
    )
    parser.add_argument(
        "--all-episodes-out",
        default=None,
        help="also dump every measured episode here; keep the target untracked (too large to track)",
    )
    parser.add_argument("--census-only", action="store_true")
    args = parser.parse_args(argv)
    levels = tuple(args.level) if args.level else DEFAULT_LEVELS
    run_dir = Path(args.run_dir) if args.run_dir else ledger.find_run_dir(args.arm, study=Path(args.study))
    arm = run_dir.parents[3].name if len(run_dir.parents) > 3 else run_dir.parent.name
    episodes = ledger.build_ledger(run_dir, args.coin or None)["episodes"]
    episodes = [
        item
        for item in episodes
        if float(item.get("qty_max") or 0.0) >= args.min_qty
        and float(item.get("exposure_max") or 0.0) >= args.min_exposure
    ]
    adds_by_coin = coin_adds(run_dir)
    by_coin: dict[str, list[dict[str, Any]]] = {}
    for episode in episodes:
        by_coin.setdefault(episode["coin"], []).append(episode)
    results: list[dict[str, Any]] = []
    frames_seen = 0
    # One generator for the whole census: `iter_candles` holds the ~2 GB array and yields one coin
    # at a time, so calling it per coin would re-decompress the bundle 40 times over.
    for coin, frame in candles.iter_candles(run_dir, sorted(by_coin)):
        frames_seen += 1
        coin_candles = CoinCandles(frame)
        for episode in by_coin[coin]:
            start_ns = to_ns(episode["start"])
            end = episode["end"]
            end_ns = to_ns(end) if end else int(coin_candles.ts[-1])
            window = (
                coin_candles.clamp(coin_candles.left(start_ns)),
                coin_candles.clamp(coin_candles.right(end_ns)),
            )
            if window[1] <= window[0]:
                continue
            breaches = {}
            for level_pct in levels:
                detail = breach_detail(
                    coin_candles,
                    episode["avg_path"],
                    adds_by_coin.get(coin, ()),
                    window=window,
                    level_pct=level_pct,
                    cooldown_minutes=args.cooldown_minutes,
                )
                if detail is not None:
                    breaches[str(level_pct)] = detail
            results.append(
                {
                    "coin": coin,
                    "start": episode["start"],
                    "end": episode["end"],
                    "minutes": episode["minutes"],
                    "qty_max": episode["qty_max"],
                    "exposure_max": episode["exposure_max"],
                    "balance_at_exposure_max": episode.get("balance_at_exposure_max"),
                    "net_pnl_usd": episode["net_pnl_usd"],
                    "close_type": episode.get("close_type"),
                    "excursion": adverse_excursion(coin_candles, episode["avg_path"], window=window),
                    "breaches": breaches,
                }
            )
        del coin_candles, frame
    breached = [item for item in results if item["breaches"]]
    deepest = sorted(
        results, key=lambda item: (item["excursion"] or {}).get("ratio", 0.0), reverse=True
    )[:DEEPEST_KEPT]
    report = {
        "arm": arm,
        "run_dir": str(run_dir.relative_to(REPO)),
        "levels": list(levels),
        "cooldown_minutes": args.cooldown_minutes,
        "episodes_measured": len(results),
        "episodes_with_breach": len(breached),
        "coins": sorted(by_coin),
        "min_exposure": args.min_exposure,
        "min_qty": args.min_qty,
        "candles_source": candles.bundle_summary(run_dir),
        "thresholds": {
            "spike_exit_further_drop": SPOT_EXIT_FURTHER_DROP,
            "good_exit_further_drop": GOOD_EXIT_FURTHER_DROP,
        },
        "method": (
            "均价在两次入场成交之间保持不变（取自 fills.csv 的 pprice）；跌破 = 真实 1m candle low ≤ "
            "均价 × (1 − level)；主口径的后续跌幅取「跌破 → 冷却到期」窗口（即止损决定真正起作用的区间），"
            "episode_further_drop 取「跌破 → 该 episode 结束」；spike_exit = 后续跌幅 ≤ 2% 且冷却窗口内"
            "收盘价回到该位之上（止损卖在低点）；good_exit = 后续跌幅 ≥ 5%（止损避开真实伤害）；"
            "adds_within_cooldown = 冷却窗口内本会被该止损撤掉的入场成交（数量/名义额，以及其中低于该位的部分）。"
            "所有价格来自 run 自己的冻结 HLCV bundle（通道序 high,low,close,volume），无任何网络访问。"
        ),
        "census": [census(results, level_pct) for level_pct in levels],
        "breach_clusters": breach_clusters(
            results, args.cluster_level, window_hours=args.cluster_window_hours
        ),
        "cluster_level": args.cluster_level,
        "cluster_window_hours": args.cluster_window_hours,
        "excursion_summary": excursion_summary(results),
        "episodes_with_breaches": breached,
        "deepest_episodes": deepest,
    }
    out = Path(args.out) if args.out else STUDY / "artifacts/optimism" / f"{arm}__excursion.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {display(out)}")
    print(
        f"episodes measured={len(results)} breached>=1 level={len(breached)} "
        f"coins={len(by_coin)} frames={frames_seen}"
    )
    if args.all_episodes_out:
        full = dict(report, episodes=results)
        full.pop("episodes_with_breaches")
        full.pop("deepest_episodes")
        Path(args.all_episodes_out).write_text(
            json.dumps(full, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.all_episodes_out} (untracked, every episode)")
    print(
        f"{'level':>7} {'breach':>7} {'of eps':>8} {'spike':>7} {'good':>7} {'recov<cd':>9} "
        f"{'med drop':>9} {'med ep':>8} {'+adds':>6} {'adds<$L':>8} {'cancelled $':>12}"
    )
    for row in report["census"]:
        if not row.get("breaches"):
            print(f"{row['level_pct']:>7.2f} {0:>7}")
            continue
        print(
            f"{row['level_pct']:>7.2f} {row['breaches']:>7} {row['share_of_episodes_pct']:>7.2f}% "
            f"{row['spike_exit_share_pct']:>6.2f}% {row['good_exit_share_pct']:>6.2f}% "
            f"{row['recovered_share_pct']:>8.2f}% "
            f"{row['median_further_drop_pct'] * 100:>8.2f}% {row['median_episode_further_drop_pct'] * 100:>7.2f}% "
            f"{row['breaches_with_cancelled_adds']:>6} {row['breaches_with_cancelled_adds_below_level']:>8} "
            f"{row['cancelled_add_notional_usd']:>12.0f}"
        )
    print(
        f"\n{'level':>7} {'breached ep net>0':>18} {'net sum $':>11} {'net median $':>12} "
        f"{'win sum $':>10} {'loss sum $':>11} {'med close@expiry vs L':>21} {'close<L':>8}"
    )
    for row in report["census"]:
        if not row.get("breaches"):
            continue
        print(
            f"{row['level_pct']:>7.2f} {row['breached_episodes_net_positive']:>4}/{row['breaches']:<4}"
            f"{row['breached_episodes_net_positive_pct']:>9.2f}% {row['breached_episodes_net_sum_usd']:>11.0f} "
            f"{row['breached_episodes_net_median_usd']:>12.2f} {row['breached_episodes_net_win_sum_usd']:>10.0f} "
            f"{row['breached_episodes_net_loss_sum_usd']:>11.0f} "
            f"{(row['median_close_at_expiry_vs_level_pct'] or 0.0) * 100:>20.2f}% "
            f"{row['share_close_at_expiry_below_level_pct']:>7.2f}%"
        )
    clusters = report["breach_clusters"]
    print(
        f"\ncorrelated breach clusters at {report['cluster_level']:.2f} "
        f"(>=2 coins within {report['cluster_window_hours']:.0f}h): {len(clusters)}"
    )
    for cluster in clusters:
        print(
            f"  {cluster['first_breach'][:16]} .. {cluster['last_breach'][:16]} "
            f"coins={len(cluster['coins'])} exposure_sum={cluster['exposure_sum']:.3f} "
            f"net={cluster['net_pnl_usd_sum']:.0f} max_drop={cluster['max_further_drop_pct'] * 100:.1f}% "
            f"{','.join(cluster['coins'])}"
        )
    if not args.census_only:
        summary = report["excursion_summary"]
        if summary:
            print(
                f"\nexcursion over {summary['episodes']} episodes: median={summary['median_mae_pct']}% "
                f"p90={summary['p90_mae_pct']}% p99={summary['p99_mae_pct']}% max={summary['max_mae_pct']}%"
            )
            over = ", ".join(f"≥{k}%: {v}" for k, v in summary["episodes_over_pct"].items())
            print(f"  episodes {over}")
            print(f"  deepest coins: {summary['deepest_coins_max_mae_pct']}")
        print("\ndeepest episodes (max adverse excursion vs the average in force):")
        for item in deepest[:12]:
            excursion = item["excursion"] or {}
            print(
                f"  {item['coin']:6s} {item['start'][:16]} mae={excursion.get('ratio', 0) * 100:6.2f}% "
                f"low={excursion.get('episode_min_low')} at {str(excursion.get('episode_min_low_ts'))[:16]} "
                f"exp={item['exposure_max']:.3f} qty={item['qty_max']} net={item['net_pnl_usd']} "
                f"breach={'yes' if item['breaches'] else 'no'}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
