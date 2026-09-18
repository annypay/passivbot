#!/usr/bin/env python3
"""Read the frozen HLCV bundle a run used and slice real candles for named coins.

Every Phase-A tool needs the same two things the backtest itself ran on: the run's frozen dataset
identity (`dataset.json`) and the raw candles of the coins that run actually held. This module is
the single reader for both, so no tool re-implements the bundle layout or the channel order.

Facts this module pins (verified against the repository):

* the bundle is `<repo>/caches/hlcvs_data/<cache_dir_label>/` with `hlcvs.npy.gz`,
  `timestamps.npy.gz`, `coins.json`, `market_specific_settings.json`;
* `hlcvs` has shape `(minutes, coins, 4)` and channel order **high, low, close, volume**
  (`src/ohlcv_utils.py`: "HLCV indices: 0=high, 1=low, 2=close, 3=volume");
* the bundle is read-only: this tool never writes into `caches/`.

Memory: the 3-year 40-coin bundle is ~2.1 GB uncompressed, so loading requires a guard. The loader
checks available memory first and refuses rather than risking an OOM kill that would corrupt a
concurrent backtest.

Offline only. No network, no credentials, no bot start.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[4]
HLCVS_ROOT = REPO / "caches/hlcvs_data"
#: `hlcvs` channel order; see the module docstring.
CHANNEL_HIGH, CHANNEL_LOW, CHANNEL_CLOSE, CHANNEL_VOLUME = 0, 1, 2, 3
#: Refuse to load a bundle when less than this much memory is available.
MIN_AVAILABLE_MB = 3000


class BundleTooLarge(RuntimeError):
    """Raised when loading the bundle would risk exhausting memory."""


def available_mb() -> int:
    out = subprocess.run(["free", "-m"], capture_output=True, text=True, check=False).stdout
    for line in out.splitlines():
        if line.startswith("Mem:"):
            return int(line.split()[6])
    return 0


def load_json(path: Path) -> Any:
    with Path(path).open() as handle:
        return json.load(handle)


def dataset_identity(run_dir: Path) -> dict[str, Any]:
    """The run's frozen dataset identity as recorded by the backtest."""
    run_dir = Path(run_dir)
    identity = load_json(run_dir / "dataset.json")
    label = identity.get("cache_dir_label")
    if not label:
        raise SystemExit(f"{run_dir}/dataset.json has no cache_dir_label")
    bundle = HLCVS_ROOT / str(label)
    if not bundle.is_dir():
        raise SystemExit(f"bundle for {label} is missing under {HLCVS_ROOT}")
    coin_index = identity.get("coin_index")
    if not isinstance(coin_index, dict) or not coin_index:
        coins_order = load_json(bundle / "coins.json")
        coin_index = {str(coin): index for index, coin in enumerate(coins_order)}
    return {
        "label": str(label),
        "bundle": bundle,
        "coin_index": {str(k): int(v) for k, v in coin_index.items()},
        "window": (identity.get("requested_start"), identity.get("requested_end")),
        "candle_interval_minutes": int(identity.get("candle_interval_minutes") or 1),
        "identity": identity,
    }


def _load_array(bundle: Path, name: str) -> np.ndarray:
    raw = bundle / f"{name}.npy"
    gz = bundle / f"{name}.npy.gz"
    if raw.exists():
        return np.load(raw)
    if gz.exists():
        return np.load(gzip.open(gz, "rb"))
    raise SystemExit(f"bundle {bundle} has neither {raw.name} nor {gz.name}")


def load_candles(
    run_dir: Path,
    coins: Sequence[str],
    *,
    start: Any = None,
    end: Any = None,
    min_available_mb: int = MIN_AVAILABLE_MB,
) -> dict[str, pd.DataFrame]:
    """Real candles for `coins` from the bundle the run used, as `{coin: DataFrame}`.

    The frame is indexed by UTC timestamp with `high` / `low` / `close` columns. `start`/`end` are
    inclusive bounds (anything `pd.Timestamp` accepts, or None) applied after loading, because a
    gzip stream cannot be seeked.
    """
    identity = dataset_identity(run_dir)
    if not coins:
        return {}
    missing = [coin for coin in coins if coin not in identity["coin_index"]]
    if missing:
        raise SystemExit(f"{identity['label']} has no candles for {missing}")
    avail = available_mb()
    if avail < min_available_mb:
        raise BundleTooLarge(
            f"only {avail} MB available; loading {identity['label']} needs about "
            f"{min_available_mb} MB. Stop the other work or lower --min-available-mb."
        )
    timestamps = _load_array(identity["bundle"], "timestamps")
    hlcvs = _load_array(identity["bundle"], "hlcvs")
    if hlcvs.shape[0] != timestamps.shape[0]:
        raise SystemExit(
            f"bundle {identity['label']}: hlcvs has {hlcvs.shape[0]} rows but timestamps has "
            f"{timestamps.shape[0]}"
        )
    index = pd.to_datetime(timestamps, unit="ms", utc=True)
    out: dict[str, pd.DataFrame] = {}
    for coin in coins:
        column = identity["coin_index"][coin]
        block = hlcvs[:, column, :3].astype("float64", copy=True)
        frame = pd.DataFrame(
            {
                "high": block[:, CHANNEL_HIGH],
                "low": block[:, CHANNEL_LOW],
                "close": block[:, CHANNEL_CLOSE],
            },
            index=index,
        )
        if start is not None:
            frame = frame.loc[frame.index >= pd.Timestamp(start, tz="UTC")]
        if end is not None:
            frame = frame.loc[frame.index <= pd.Timestamp(end, tz="UTC")]
        out[coin] = frame
        del block
    del hlcvs
    return out


def hold_run_lock(run_dir: Path) -> Path:  # pragma: no cover - documented no-op seam
    """Placeholder for callers that want to assert the run directory exists."""
    run_dir = Path(run_dir)
    if not (run_dir / "fills.csv").exists():
        raise SystemExit(f"{run_dir} has no fills.csv")
    return run_dir


def iter_candles(
    run_dir: Path,
    coins: Sequence[str],
    *,
    start: Any = None,
    end: Any = None,
    min_available_mb: int = MIN_AVAILABLE_MB,
) -> Iterable[tuple[str, pd.DataFrame]]:
    """Stream `(coin, frame)` pairs while holding the big array exactly once.

    `load_candles` builds a dict, which for a 40-coin bundle costs ~1.5 GB on top of the 2.1 GB
    array. Tooling that walks every coin should stream instead: the bundle is loaded once, each
    coin's slice is yielded and then dropped.
    """
    identity = dataset_identity(run_dir)
    wanted = [coin for coin in coins if coin in identity["coin_index"]]
    missing = [coin for coin in coins if coin not in identity["coin_index"]]
    if missing:
        raise SystemExit(f"{identity['label']} has no candles for {missing}")
    if not wanted:
        return
    avail = available_mb()
    if avail < min_available_mb:
        raise BundleTooLarge(
            f"only {avail} MB available; loading {identity['label']} needs about "
            f"{min_available_mb} MB. Stop the other work or lower --min-available-mb."
        )
    timestamps = _load_array(identity["bundle"], "timestamps")
    hlcvs = _load_array(identity["bundle"], "hlcvs")
    index = pd.to_datetime(timestamps, unit="ms", utc=True)
    try:
        for coin in wanted:
            column = identity["coin_index"][coin]
            block = hlcvs[:, column, :3].astype("float64", copy=True)
            frame = pd.DataFrame(
                {
                    "high": block[:, CHANNEL_HIGH],
                    "low": block[:, CHANNEL_LOW],
                    "close": block[:, CHANNEL_CLOSE],
                },
                index=index,
            )
            if start is not None:
                frame = frame.loc[frame.index >= pd.Timestamp(start, tz="UTC")]
            if end is not None:
                frame = frame.loc[frame.index <= pd.Timestamp(end, tz="UTC")]
            yield coin, frame
            del block, frame
    finally:
        del hlcvs


def bundle_summary(run_dir: Path) -> dict[str, Any]:
    identity = dataset_identity(run_dir)
    size_gb = None
    gz = identity["bundle"] / "hlcvs.npy.gz"
    if gz.exists():
        size_gb = round(os.path.getsize(gz) / 1e9, 3)
    return {
        "cache_dir_label": identity["label"],
        "coins": len(identity["coin_index"]),
        "hlcvs_gz_gb": size_gb,
        "channel_order": "high,low,close,volume",
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--coin", action="append", default=[])
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    args = parser.parse_args(argv)
    frames = load_candles(
        Path(args.run_dir), args.coin, start=args.start, end=args.end
    )
    for coin, frame in frames.items():
        print(f"{coin}: {len(frame)} candles {frame.index[0]} .. {frame.index[-1]}")
        print(
            f"  low={frame['low'].min():.6g} at {frame['low'].idxmin()} "
            f"high={frame['high'].max():.6g} at {frame['high'].idxmax()}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
