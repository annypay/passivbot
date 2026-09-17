#!/usr/bin/env python3
"""Event-window analytics for the g4 tail-risk study.

The event table is derived from the data, not hand-picked: the benchmark's 7-day log return
(`variant_spec.EVENT_RULE`) identifies crash episodes, nearby breaches merge into one
episode, and each episode is then measured inside every arm's own equity path. Episodes are
labelled afterwards, only for readability.

Everything here is a pure function over local, regenerable run artifacts plus the frozen
dataset's benchmark series, so the report renderer and the independent verifier can compute
the same table from the same inputs.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

MS_PER_DAY = 86_400_000


def load_npy_gz(path: Path) -> np.ndarray:
    """Read a gzipped `.npy` artifact without importing the engine's tooling."""
    with gzip.open(Path(path), "rb") as handle:
        return np.load(handle)


@dataclass(frozen=True)
class Episode:
    """One benchmark crash episode, before any arm is measured."""

    breach_start_ms: int
    breach_end_ms: int
    peak_ms: int
    peak_price: float
    trough_ms: int
    trough_price: float
    drop_pct: float
    label: str = ""

    @property
    def key(self) -> str:
        return pd.Timestamp(self.breach_start_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d")


def load_equity(run_dir: Path) -> pd.DataFrame:
    """The arm's minute equity series, indexed by UTC timestamp.

    The CSV's first column is an unnamed timestamp, so the frame is loaded with
    `index_col=0` and re-parsed rather than trusted.
    """
    path = Path(run_dir) / "balance_and_equity.csv.gz"
    frame = pd.read_csv(path, index_col=0)
    frame.index = pd.to_datetime(frame.index, utc=True, errors="coerce")
    frame = frame[frame.index.notna()]
    for column in frame.columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.sort_index()
    frame.index.name = "timestamp"
    return frame


def load_fills(run_dir: Path) -> pd.DataFrame:
    path = Path(run_dir) / "fills.csv"
    frame = pd.read_csv(path, index_col=0)
    if "timestamp" not in frame.columns:
        raise ValueError(f"{path}: fills ledger has no timestamp column")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame[frame["timestamp"].notna()].sort_values("timestamp")
    for column in ("wallet_exposure", "twe_long", "pnl", "fee_paid", "usd_total_balance"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_coin_metrics(run_dir: Path) -> pd.DataFrame:
    path = Path(run_dir) / "coin_metrics.csv"
    frame = pd.read_csv(path)
    for column in (
        "max_abs_wallet_exposure_at_fill",
        "net_realized_pnl_usd",
        "realized_pnl_raw_usd",
        "fees_signed_usd",
        "fills_count",
        "entry_fills_count",
        "reduction_or_close_fills_count",
    ):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values(
        "max_abs_wallet_exposure_at_fill", ascending=False
    ).reset_index(drop=True)


def load_benchmark(dataset_path: Path) -> pd.DataFrame:
    """The frozen bundle's BTC benchmark series, read straight from its own artifacts."""
    dataset_path = Path(dataset_path)
    timestamps = load_npy_gz(dataset_path / "timestamps.npy.gz")
    prices = load_npy_gz(dataset_path / "btc_usd_prices.npy.gz")
    if timestamps.shape != prices.shape:
        raise ValueError(
            f"{dataset_path}: benchmark series shape {prices.shape} != timestamps {timestamps.shape}"
        )
    return pd.DataFrame(
        {"btc_usd_price": prices.astype("float64")},
        index=pd.to_datetime(timestamps.astype("int64"), unit="ms", utc=True),
    ).sort_index()


def daily_closes(series: pd.Series) -> pd.Series:
    """Last observation per UTC day."""
    if series.empty:
        return series
    bucketed = series.groupby(series.index.floor("D")).last()
    return bucketed.dropna()


def detect_episodes(
    benchmark: pd.DataFrame,
    *,
    lookback_days: int,
    threshold: float,
    min_gap_days: int,
    labels: Sequence[tuple[str, str, str]] = (),
) -> list[Episode]:
    """Ordered crash episodes derived from the benchmark's rolling log return."""
    closes = daily_closes(benchmark["btc_usd_price"])
    if closes.size < lookback_days + 2:
        return []
    log_ret = np.log(closes / closes.shift(lookback_days))
    breach_days = list(log_ret.index[log_ret <= threshold])
    if not breach_days:
        return []
    clusters: list[list[pd.Timestamp]] = [[breach_days[0]]]
    for day in breach_days[1:]:
        if (day - clusters[-1][-1]).days >= min_gap_days:
            clusters.append([])
        clusters[-1].append(day)

    episodes: list[Episode] = []
    for cluster in clusters:
        first, last = cluster[0], cluster[-1]
        search_start = first - pd.Timedelta(days=lookback_days)
        pre = closes.loc[search_start:first]
        window = closes.loc[first : last + pd.Timedelta(days=min_gap_days)]
        if pre.empty or window.empty:
            continue
        peak_day = pre.idxmax()
        trough_day = window.idxmin()
        peak_price = float(pre.loc[peak_day])
        trough_price = float(window.loc[trough_day])
        if not np.isfinite(peak_price) or not np.isfinite(trough_price) or peak_price <= 0:
            continue
        episodes.append(
            Episode(
                breach_start_ms=int(first.value // 1_000_000),
                breach_end_ms=int(last.value // 1_000_000),
                peak_ms=int(peak_day.value // 1_000_000),
                peak_price=peak_price,
                trough_ms=int(trough_day.value // 1_000_000),
                trough_price=trough_price,
                drop_pct=float(1.0 - trough_price / peak_price),
            )
        )
    return label_episodes(episodes, labels)


def _day_ms(value: str) -> int:
    return int(pd.Timestamp(value, tz="UTC").value // 1_000_000)


def label_episodes(
    episodes: Iterable[Episode], labels: Sequence[tuple[str, str, str]]
) -> list[Episode]:
    """Attach the first label whose window overlaps the episode's breach cluster."""
    out: list[Episode] = []
    for episode in episodes:
        name = ""
        for label, start, end in labels:
            if episode.breach_end_ms >= _day_ms(start) and episode.breach_start_ms <= _day_ms(end):
                name = label
                break
        out.append(
            Episode(
                breach_start_ms=episode.breach_start_ms,
                breach_end_ms=episode.breach_end_ms,
                peak_ms=episode.peak_ms,
                peak_price=episode.peak_price,
                trough_ms=episode.trough_ms,
                trough_price=episode.trough_price,
                drop_pct=episode.drop_pct,
                label=name or "未标注事件",
            )
        )
    return out


def window_metrics(
    equity: pd.DataFrame, start_ms: int, end_ms: int, *, column: str = "strategy_equity"
) -> dict[str, Any]:
    """Peak-to-trough drawdown, underwater time and recovery inside one window."""
    if equity.empty:
        return {}
    series = equity[column].dropna()
    if series.empty:
        return {}
    start = pd.Timestamp(start_ms, unit="ms", tz="UTC")
    end = pd.Timestamp(end_ms, unit="ms", tz="UTC")
    window = series.loc[start:end]
    if window.empty:
        return {}
    prior = series.loc[:start]
    base = float(prior.iloc[-1]) if not prior.empty else float(window.iloc[0])
    running_peak = window.cummax().clip(lower=base)
    drawdown = 1.0 - window / running_peak
    worst = float(drawdown.max())
    peak_value = float(running_peak.max())
    trough_ts = drawdown.idxmax()
    recovered = window.loc[trough_ts:][lambda frame: frame >= peak_value]
    recovery_ts = recovered.index[0] if not recovered.empty else None
    breached = drawdown[drawdown > 0.0]
    return {
        "start": window.index[0],
        "end": window.index[-1],
        "start_value": base,
        "end_value": float(window.iloc[-1]),
        "window_return": float(window.iloc[-1] / base - 1.0) if base else None,
        "max_drawdown": worst,
        "trough_ts": trough_ts,
        "days_to_trough": float((trough_ts - window.index[0]).total_seconds() / 86400.0),
        "recovery_ts": recovery_ts,
        "days_to_recovery": (
            float((recovery_ts - trough_ts).total_seconds() / 86400.0) if recovery_ts else None
        ),
        "underwater_days": (
            float((window.index[-1] - breached.index[0]).total_seconds() / 86400.0)
            if not breached.empty
            else 0.0
        ),
        "samples": int(window.size),
    }


def episode_window(
    episode: Episode, equity: pd.DataFrame, *, max_extension_days: int = 180
) -> tuple[int, int]:
    """The measurement window: pre-crash peak through the arm's own recovery (capped)."""
    hard_end = episode.breach_end_ms + max_extension_days * MS_PER_DAY
    if equity.empty or "strategy_equity" not in equity:
        return episode.peak_ms, hard_end
    series = equity["strategy_equity"].dropna()
    start = pd.Timestamp(episode.peak_ms, unit="ms", tz="UTC")
    pre = series.loc[:start]
    if pre.empty:
        return episode.peak_ms, hard_end
    peak_value = float(pre.max())
    after = series.loc[start:]
    recovered = after[after >= peak_value]
    if recovered.empty:
        return episode.peak_ms, hard_end
    return episode.peak_ms, min(int(recovered.index[0].value // 1_000_000), hard_end)


def arm_event_row(
    episode: Episode, equity: pd.DataFrame, fills: pd.DataFrame, *, synthetic: bool = False
) -> dict[str, Any]:
    """One arm's measurement of one episode."""
    if not fills.empty and not pd.api.types.is_datetime64_any_dtype(fills["timestamp"]):
        fills = fills.assign(
            timestamp=pd.to_datetime(fills["timestamp"], utc=True, errors="coerce")
        )
    start_ms, end_ms = episode_window(episode, equity)
    metrics = window_metrics(equity, start_ms, end_ms)
    fmt = lambda ms: pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%Y-%m-%d")
    row: dict[str, Any] = {
        "event": episode.key,
        "label": episode.label,
        "breach_start": fmt(episode.breach_start_ms),
        "breach_end": fmt(episode.breach_end_ms),
        "benchmark_drop_pct": episode.drop_pct,
        "benchmark_trough": fmt(episode.trough_ms),
        "window_start": fmt(start_ms),
        "window_end": fmt(end_ms),
        "synthetic": synthetic,
    }
    if metrics:
        row.update(
            {
                "max_drawdown": metrics["max_drawdown"],
                "days_to_trough": metrics["days_to_trough"],
                "days_to_recovery": metrics["days_to_recovery"],
                "underwater_days": metrics["underwater_days"],
                "window_return": metrics["window_return"],
                "samples": metrics["samples"],
            }
        )
    if not fills.empty:
        window = fills.loc[
            (fills["timestamp"] >= pd.Timestamp(start_ms, unit="ms", tz="UTC"))
            & (fills["timestamp"] <= pd.Timestamp(end_ms, unit="ms", tz="UTC"))
        ]
        row["fills"] = int(window.shape[0])
        if window.empty:
            # No fill in the window: the arm was flat (gate closed, or the universe had not
            # listed the coin yet). Report absence as absence instead of a NaN.
            row["twe_peak"] = None
            row["realized_pnl_usd"] = 0.0
            row["fees_usd"] = 0.0
            row["panic_fills"] = 0
            row["panic_loss_usd"] = 0.0
            return row
        twe_peak = window["twe_long"].max() if "twe_long" in window else None
        row["twe_peak"] = None if twe_peak is None or not np.isfinite(twe_peak) else float(twe_peak)
        row["realized_pnl_usd"] = float(window["pnl"].fillna(0.0).sum()) if "pnl" in window else None
        row["fees_usd"] = (
            float(window["fee_paid"].fillna(0.0).sum()) if "fee_paid" in window else None
        )
        if "type" in window:
            panic = window[window["type"].astype(str).str.contains("panic", case=False)]
            row["panic_fills"] = int(panic.shape[0])
            row["panic_loss_usd"] = (
                float(panic["pnl"].fillna(0.0).sum()) if "pnl" in panic else 0.0
            )
    return row


def arm_event_table(
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    episodes: Sequence[Episode],
    *,
    synthetic: bool = False,
) -> list[dict[str, Any]]:
    return [arm_event_row(episode, equity, fills, synthetic=synthetic) for episode in episodes]


def full_window_summary(
    equity: pd.DataFrame, fills: pd.DataFrame, *, column: str = "strategy_equity"
) -> dict[str, Any]:
    series = equity[column].dropna()
    if series.empty:
        return {}
    running_peak = series.cummax()
    drawdown = 1.0 - series / running_peak
    trough_ts = drawdown.idxmax()
    peak_ts = series.loc[:trough_ts].idxmax()
    return {
        "samples": int(series.size),
        "start": series.index[0],
        "end": series.index[-1],
        "start_value": float(series.iloc[0]),
        "end_value": float(series.iloc[-1]),
        "gain": float(series.iloc[-1] / series.iloc[0] - 1.0),
        "max_drawdown": float(drawdown.max()),
        "trough_ts": trough_ts,
        "peak_before_trough_ts": peak_ts,
        "days_to_trough": float((trough_ts - peak_ts).total_seconds() / 86400.0),
        "twe_peak_at_fills": float(fills["twe_long"].max()) if "twe_long" in fills else None,
    }


def injection_episodes(record: dict[str, Any]) -> list[Episode]:
    """The injected collapses of a synthetic bundle, as extra measurable episodes.

    The benchmark-driven rule cannot see an injected path (the benchmark is untouched), so a
    synthetic arm measures its own injection window explicitly: the episode starts at the
    minute the target coin reached its maximum exposure in the control run, the trough is the
    end of the collapse, and the flat hold period is included in the window.
    """
    transform = (record or {}).get("transform") or {}
    out: list[Episode] = []
    for target in transform.get("targets", []):
        start_ms = int(target["injection_start_ms"])
        collapse_rows = int(target.get("collapse_rows") or 0)
        hold_rows = int(target.get("hold_rows") or 0)
        if collapse_rows <= 0:
            continue
        trough_ms = start_ms + collapse_rows * 60_000
        end_ms = start_ms + (collapse_rows + hold_rows) * 60_000
        first_price = float(target.get("first_price") or 0.0)
        trough_price = float(target.get("trough_price") or 0.0)
        drop = (1.0 - trough_price / first_price) if first_price > 0 else 0.0
        out.append(
            Episode(
                breach_start_ms=start_ms,
                breach_end_ms=max(trough_ms, end_ms - 60_000),
                peak_ms=start_ms,
                peak_price=first_price,
                trough_ms=trough_ms,
                trough_price=trough_price,
                drop_pct=drop,
                label=(
                    f"合成注入：{target['coin']} 价格 →"
                    f"{float(target.get('end_multiple') or 0.0):.3f}×（{target['injection_start_utc']} 起）"
                ),
            )
        )
    return out


def write_csv(rows: Sequence[dict[str, Any]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(list(rows)).to_csv(path, index=False)
    return path
