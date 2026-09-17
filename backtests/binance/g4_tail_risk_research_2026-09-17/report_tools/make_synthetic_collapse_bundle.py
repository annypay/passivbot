#!/usr/bin/env python3
"""Build the synthetic collapse bundles for the g4 tail-risk study.

The question this answers is the one no local dataset contains: what happens to the account
when a coin that the strategy is already holding goes to (almost) zero — the "next LUNA".
The native bundle's coin list is the current top-40, so no delisted coin exists in it; the
only honest way to test the mechanism is to inject a collapse path into a *derived copy* of
the frozen bundle and label it synthetic everywhere.

Contract (recorded in `artifacts/synthetic_bundles.json`):

* the collapse targets are chosen by the control run's own `coin_metrics.csv`
  (`max_abs_wallet_exposure_at_fill`, descending), so the injection always lands on the coin
  the strategy actually loaded most;
* the collapse starts at the minute that coin reached its maximum exposure in the control
  run, so the position is loaded when the price starts falling;
* the price path falls linearly (log-free, and OHLC stays internally consistent) from 1.0 to
  `end_multiple` over `collapse_days`, then holds the collapsed price for `hold_days`;
* volume, timestamps, the benchmark series, coin metadata and the valid windows are copied
  unchanged, so the only difference from the native bundle is the injected price path.

Offline only: the derived bundle is built from the frozen local bundle, no network and no
credentials are involved.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_spec as study  # noqa: E402

SOURCE_DATASET_KEY = "3y"
MINUTES_PER_DAY = 1440
#: HLCV column order in the materialized array: high, low, close, volume.
PRICE_COLUMNS = slice(0, 3)


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def load_npy_gz(path: Path) -> np.ndarray:
    import gzip

    with gzip.open(Path(path), "rb") as handle:
        return np.load(handle)


def select_targets(injection: dict[str, Any]) -> list[dict[str, Any]]:
    """Pick the injection targets and their start minute from the control run's artifacts."""
    run_dir = Path(injection["target_metrics_run"])
    metrics_path = run_dir / "coin_metrics.csv"
    fills_path = run_dir / "fills.csv"
    if not metrics_path.exists():
        fail(
            [f"missing {study.relative(metrics_path)}"],
            "the control run's per-coin metrics are required to choose the injection targets",
        )
    if not fills_path.exists():
        fail(
            [f"missing {study.relative(fills_path)}"],
            "the control run's fill ledger is required to place the injection on a loaded minute",
        )
    import pandas as pd

    metrics = pd.read_csv(metrics_path)
    fills = pd.read_csv(fills_path)
    fills["timestamp"] = pd.to_datetime(fills["timestamp"], utc=True, errors="coerce")
    count = int(injection["targets"])
    ranked = metrics.sort_values(
        "max_abs_wallet_exposure_at_fill", ascending=False
    ).head(count)
    targets: list[dict[str, Any]] = []
    for _, row in ranked.iterrows():
        coin = str(row["coin"])
        coin_fills = fills.loc[fills["coin"].astype(str) == coin].dropna(subset=["wallet_exposure"])
        if coin_fills.empty:
            fail([f"no fills for target coin {coin}"], "cannot place the injection")
        peak = coin_fills.loc[coin_fills["wallet_exposure"].abs().idxmax()]
        targets.append(
            {
                "coin": coin,
                "max_abs_wallet_exposure_at_fill": float(
                    row["max_abs_wallet_exposure_at_fill"]
                ),
                "injection_start_utc": peak["timestamp"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "injection_start_ms": int(peak["timestamp"].value // 1_000_000),
                "exposure_at_injection_usd": float(abs(peak["wallet_exposure"]))
                * float(peak["usd_total_balance"]),
            }
        )
    return targets


def apply_collapse(
    hlcvs: np.ndarray,
    coin_index: int,
    start_row: int,
    *,
    collapse_rows: int,
    hold_rows: int,
    end_multiple: float,
) -> dict[str, Any]:
    """Scale one coin's H/L/C path down over the collapse window, then hold it."""
    total = hlcvs.shape[0]
    if start_row < 0 or start_row >= total:
        raise ValueError(f"start row {start_row} outside the array ({total} rows)")
    collapse_end = min(total, start_row + collapse_rows)
    hold_end = min(total, collapse_end + hold_rows)
    path = hlcvs[start_row:collapse_end, coin_index, :]
    before = path.copy()
    steps = max(1, collapse_end - start_row)
    factors = np.linspace(1.0, end_multiple, steps, endpoint=True, dtype="float64")
    for factor_index in range(1, steps):
        path[factor_index, PRICE_COLUMNS] *= factors[factor_index]
    hold_slice = hlcvs[collapse_end:hold_end, coin_index, :]
    hold_slice[:, PRICE_COLUMNS] *= end_multiple
    return {
        "start_row": int(start_row),
        "collapse_rows": int(collapse_end - start_row),
        "hold_rows": int(hold_end - collapse_end),
        "first_price": float(before[0, 2]),
        "trough_price": float(path[-1, 2]) if path.shape[0] else None,
        "end_multiple": float(end_multiple),
    }


def build_bundle(key: str, *, force: bool) -> dict[str, Any]:
    injection = study.SYNTHETIC_INJECTIONS[key]
    dataset = study.DATASETS[key]
    source = study.DATASETS[SOURCE_DATASET_KEY]
    target_dir = dataset.path
    if target_dir.exists() and not force:
        existing = {}
        if study.SYNTHETIC_BUNDLES_PATH.exists():
            existing = study.load_json(study.SYNTHETIC_BUNDLES_PATH).get("bundles", {})
        record = existing.get(key)
        if record is None:
            fail(
                [f"{study.relative(target_dir)} exists but is not recorded"],
                "the derived bundle is unknown; rebuild it with --force",
            )
        manifest_path = target_dir / "manifest.json"
        if manifest_path.exists() and record.get("manifest_sha256") != study.sha256_file(
            manifest_path
        ):
            fail(
                [f"{study.relative(manifest_path)} does not match its recorded hash"],
                "the derived bundle changed; rebuild it with --force",
            )
        print(f"reusing derived bundle {study.relative(target_dir)}")
        record["label"] = injection["label"]
        return record
    if target_dir.exists():
        shutil.rmtree(target_dir)
    targets = select_targets(injection)

    timestamps = load_npy_gz(source.path / "timestamps.npy.gz")
    hlcvs = load_npy_gz(source.path / "hlcvs.npy.gz")
    btc_usd_prices = load_npy_gz(source.path / "btc_usd_prices.npy.gz")
    coins = study.load_json(source.path / "coins.json")
    mss = study.load_json(source.path / "market_specific_settings.json")

    collapse_rows = int(injection["collapse_days"]) * MINUTES_PER_DAY
    hold_rows = int(injection["hold_days"]) * MINUTES_PER_DAY
    transforms: list[dict[str, Any]] = []
    for target in targets:
        coin = target["coin"]
        if coin not in coins:
            fail([f"{coin} is not in the bundle"], "injection target is outside the bundle")
        start_row = int(
            np.searchsorted(timestamps, np.int64(target["injection_start_ms"]), side="left")
        )
        if start_row >= len(timestamps) - collapse_rows:
            fail(
                [f"{coin}: injection start {target['injection_start_utc']} leaves no room"],
                "injection window does not fit inside the bundle",
            )
        applied = apply_collapse(
            hlcvs,
            coins.index(coin),
            start_row,
            collapse_rows=collapse_rows,
            hold_rows=hold_rows,
            end_multiple=float(injection["end_multiple"]),
        )
        transforms.append({**target, **applied})

    target_dir.mkdir(parents=True, exist_ok=True)
    from hlcvs_manifest import save_numpy_artifact_with_hash, write_hlcvs_manifest

    def write_array(path: Path, array: np.ndarray) -> str:
        """Write a gzipped `.npy` artifact the way the engine's own cache writer does."""
        import gzip

        with gzip.open(path, "wb", compresslevel=6) as handle:
            return save_numpy_artifact_with_hash(handle, array)

    hashes = {
        "hlcvs": write_array(target_dir / "hlcvs.npy.gz", hlcvs),
        "timestamps": write_array(target_dir / "timestamps.npy.gz", timestamps),
        "btc_usd_prices": write_array(target_dir / "btc_usd_prices.npy.gz", btc_usd_prices),
    }
    for name in ("coins.json", "market_specific_settings.json", "cache_meta.json"):
        shutil.copyfile(source.path / name, target_dir / name)

    manifest = study.load_json(source.path / "manifest.json")
    for name, digest in hashes.items():
        entry = manifest["files"][name]
        entry["path"] = f"{name}.npy.gz"
        entry["sha256"] = digest
        entry["shape"] = [int(dim) for dim in (
            hlcvs.shape if name == "hlcvs" else timestamps.shape if name == "timestamps" else btc_usd_prices.shape
        )]
        entry["dtype"] = str(
            hlcvs.dtype if name == "hlcvs" else timestamps.dtype if name == "timestamps" else btc_usd_prices.dtype
        )
    transform_record = {
        "kind": "synthetic_price_path_collapse",
        "source_bundle": source.rel_path.as_posix(),
        "source_manifest_sha256": source.manifest_sha256,
        "source_manifest_config_hash": source.manifest_config_hash,
        "targets": transforms,
        "collapse_days": int(injection["collapse_days"]),
        "hold_days": int(injection["hold_days"]),
        "end_multiple": float(injection["end_multiple"]),
        "price_columns_scaled": "high, low, close (volume, timestamps, benchmark unchanged)",
        "declared_window": list(dataset.window),
    }
    manifest["config_hash"] = study.sha256_text(
        f"{source.manifest_config_hash}|{key}|{str(transform_record)}"
    )
    manifest["built_at"] = int(__import__("time").time() * 1000)
    manifest["synthetic_transform"] = transform_record
    write_hlcvs_manifest(target_dir, manifest)
    record = {
        "key": key,
        "label": injection["label"],
        "path": dataset.rel_path.as_posix(),
        "manifest_sha256": study.sha256_file(target_dir / "manifest.json"),
        "manifest_config_hash": manifest["config_hash"],
        "file_hashes": hashes,
        "synthetic": True,
        "targets": [target["coin"] for target in transforms],
        "transform": transform_record,
    }
    del hlcvs, timestamps, btc_usd_prices
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        action="append",
        choices=sorted(study.SYNTHETIC_INJECTIONS),
        help="which synthetic bundle to build (default: all)",
    )
    parser.add_argument("--force", action="store_true", help="rebuild an existing bundle")
    args = parser.parse_args(argv)

    keys = args.bundle or sorted(study.SYNTHETIC_INJECTIONS)
    source = study.DATASETS[SOURCE_DATASET_KEY]
    for name in study.FROZEN_CACHE_FILES:
        if not (source.path / name).exists():
            fail([f"missing {source.rel_path / name}"], "the source bundle is incomplete")

    records: dict[str, Any] = {}
    if study.SYNTHETIC_BUNDLES_PATH.exists():
        records = study.load_json(study.SYNTHETIC_BUNDLES_PATH).get("bundles", {})
    for key in keys:
        record = build_bundle(key, force=args.force)
        records[key] = record
        print(
            f"built {record['path']} targets={record['targets']} "
            f"manifest={record['manifest_sha256'][:12]}…"
        )
        for target in record["transform"]["targets"]:
            print(
                f"  {target['coin']}: start {target['injection_start_utc']} "
                f"exposure {target['max_abs_wallet_exposure_at_fill']:.6f} "
                f"→ trough price {target['trough_price']:.8g}"
            )
    study.write_json(
        study.SYNTHETIC_BUNDLES_PATH,
        {
            "study": study.relative(study.STUDY),
            "source_bundle": source.rel_path.as_posix(),
            "source_manifest_sha256": source.manifest_sha256,
            "note": (
                "派生 bundle 是本地、可重建的合成数据；它们不出现在任何真实数据结论里，"
                "报告必须标注“合成”。"
            ),
            "bundles": records,
        },
    )
    print(f"wrote {study.relative(study.SYNTHETIC_BUNDLES_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
