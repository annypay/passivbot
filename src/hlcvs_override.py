from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path

import numpy as np

from backtest_universe import (
    POSITION_SIDES,
    effective_backtest_approved_coins_by_side,
    effective_backtest_data_coins,
)
from config.access import get_optional_config_value, require_config_value
from hlcvs_manifest import (
    HlcvsManifestError,
    load_hlcvs_manifest,
    load_numpy_artifact,
    manifest_has_required_schema,
    verify_hlcvs_manifest,
)
from utils import (
    date_to_ts,
    format_end_date,
    split_exchange_qualified_market_identifier,
    to_standard_exchange_name,
    ts_to_date,
)
from warmup_utils import compute_backtest_warmup_minutes


def _hlcvs_cache_artifact_path(
    cache_dir: Path, manifest, name: str, candidates: tuple[str, ...]
) -> Path | None:
    cache_root = cache_dir.resolve()
    if manifest_has_required_schema(manifest):
        files = manifest.get("files", {})
        entry = files.get(name) if isinstance(files, dict) else None
        if not isinstance(entry, dict):
            raise HlcvsManifestError(f"HLCV manifest missing required file entry {name!r}")
        rel_path = entry.get("path") if isinstance(entry, dict) else None
        if not rel_path:
            raise HlcvsManifestError(f"HLCV manifest file entry {name!r} is missing path")
        path = (cache_root / str(rel_path)).resolve()
        try:
            path.relative_to(cache_root)
        except ValueError as exc:
            raise HlcvsManifestError(
                f"HLCV manifest file entry {name!r} escapes dataset directory: {rel_path}"
            ) from exc
        if not path.exists():
            raise HlcvsManifestError(f"HLCV manifest file is missing: {path}")
        return path
    for filename in candidates:
        path = cache_dir / filename
        if path.exists():
            return path
    return None


def _load_hlcvs_cache_arrays(cache_dir: Path, manifest, preloaded_arrays=None):
    coins_path = _hlcvs_cache_artifact_path(cache_dir, manifest, "coins", ("coins.json",))
    mss_path = _hlcvs_cache_artifact_path(
        cache_dir, manifest, "market_specific_settings", ("market_specific_settings.json",)
    )
    if coins_path is None or mss_path is None:
        raise FileNotFoundError(f"HLCV dataset missing coins or market settings in {cache_dir}")
    coins = json.load(open(coins_path))
    mss = json.load(open(mss_path))
    preloaded_arrays = preloaded_arrays or {}
    if {"hlcvs", "btc_usd_prices", "timestamps"} <= preloaded_arrays.keys():
        # Reuse arrays already decompressed by manifest verification.
        return (
            coins,
            preloaded_arrays["hlcvs"],
            mss,
            preloaded_arrays["btc_usd_prices"],
            preloaded_arrays["timestamps"],
        )
    hlcvs_path = _hlcvs_cache_artifact_path(
        cache_dir, manifest, "hlcvs", ("hlcvs.npy.gz", "hlcvs.npy")
    )
    timestamps_path = _hlcvs_cache_artifact_path(
        cache_dir, manifest, "timestamps", ("timestamps.npy.gz", "timestamps.npy")
    )
    btc_path = _hlcvs_cache_artifact_path(
        cache_dir,
        manifest,
        "btc_usd_prices",
        ("btc_usd_prices.npy.gz", "btc_usd_prices.npy"),
    )
    if hlcvs_path is None or btc_path is None:
        raise FileNotFoundError(f"HLCV dataset missing required arrays in {cache_dir}")
    if timestamps_path is None:
        raise FileNotFoundError(
            f"HLCV dataset missing timestamps.npy/timestamps.npy.gz in {cache_dir}"
        )
    hlcvs = load_numpy_artifact(hlcvs_path)
    btc_usd_prices = load_numpy_artifact(btc_path)
    timestamps = load_numpy_artifact(timestamps_path)
    return coins, hlcvs, mss, btc_usd_prices, timestamps


def _reconcile_override_identifiers(
    identifiers, dataset_coins: list[str], mss: dict
) -> list[str]:
    # Only saved identities may connect an alias to a dataset column. Current market
    # caches can have removed or reassigned markets since this dataset was built.
    dataset_by_identifier = {}
    for dataset_coin in dataset_coins:
        meta = mss[dataset_coin]
        venue = to_standard_exchange_name(meta["exchange"])
        aliases = [dataset_coin, meta.get("symbol"), meta.get("id"), meta.get("base")]
        for alias in aliases:
            if not alias:
                continue
            qualified_venue, raw = split_exchange_qualified_market_identifier(alias)
            if qualified_venue is not None and qualified_venue != venue:
                raise ValueError(
                    f"HLCV dataset identity {alias!r} conflicts with saved exchange {venue!r}"
                )
            keys = [(venue, raw)]
            if qualified_venue is None:
                keys.append((None, raw))
            for key in keys:
                dataset_by_identifier.setdefault(key, set()).add(dataset_coin)

    reconciled = []
    dataset_coin_set = set(dataset_coins)
    for identifier in identifiers:
        venue, raw = split_exchange_qualified_market_identifier(identifier)
        matches = (
            {identifier}
            if identifier in dataset_coin_set
            else dataset_by_identifier.get((venue, raw), set())
        )
        if len(matches) > 1:
            raise ValueError(
                f"ambiguous HLCV dataset identifier {identifier!r}: {sorted(matches)}; "
                "use an exact saved symbol or exchange-qualified market ID"
            )
        resolved = next(iter(matches)) if matches else identifier
        if resolved not in reconciled:
            reconciled.append(resolved)
    return reconciled


def _side_membership_for_override(
    config: dict, dataset_coins: list[str], manifest, mode: str, mss: dict
) -> dict:
    dataset_coin_set = set(dataset_coins)
    if mode == "intersection":
        input_sides = {
            pside: _reconcile_override_identifiers(coins, dataset_coins, mss)
            for pside, coins in effective_backtest_approved_coins_by_side(config).items()
        }
        if any("all" in coins for coins in input_sides.values()):
            raise ValueError(
                "HLCV intersection override requires explicit approved coins; "
                "use dataset mode to replay the saved universe without market discovery"
            )
        ignored = config.get("live", {}).get("ignored_coins", {})
        return {
            pside: sorted(
                set(side_coins).intersection(dataset_coin_set)
                - set(_reconcile_override_identifiers(ignored.get(pside, []), dataset_coins, mss))
            )
            for pside, side_coins in input_sides.items()
        }

    manifest_sides = None
    if manifest_has_required_schema(manifest):
        effective = manifest.get("effective", {})
        if isinstance(effective, dict):
            manifest_sides = effective.get("build_side_membership")
            if not isinstance(manifest_sides, dict):
                manifest_sides = effective.get("side_membership")
    if isinstance(manifest_sides, dict):
        for pside in POSITION_SIDES:
            coins = manifest_sides.get(pside)
            if not isinstance(coins, list) or any(
                coin not in dataset_coin_set for coin in coins
            ):
                raise HlcvsManifestError(f"HLCV manifest has invalid {pside} side membership")
        return {pside: sorted(set(manifest_sides[pside])) for pside in POSITION_SIDES}
    raise HlcvsManifestError("dataset override mode 'dataset' requires manifest side_membership")


def _select_hlcvs(hlcvs, row_start: int, row_end: int, coin_positions: list[int]):
    rows = hlcvs if row_start == 0 and row_end == len(hlcvs) else hlcvs[row_start:row_end]
    if coin_positions == list(range(hlcvs.shape[1])):
        return np.ascontiguousarray(rows)
    first = coin_positions[0]
    if coin_positions == list(range(first, first + len(coin_positions))):
        return np.ascontiguousarray(rows[:, first : first + len(coin_positions), :])
    # take() writes the selected columns directly in C order, unlike fancy indexing
    # followed by ascontiguousarray(), which can require two full subset copies.
    return np.take(rows, coin_positions, axis=1)


def _validate_dataset_metadata(dataset_coins, hlcvs, mss, btc_usd_prices, timestamps, manifest):
    if (
        not isinstance(dataset_coins, list)
        or not dataset_coins
        or not all(isinstance(coin, str) and coin for coin in dataset_coins)
        or len(set(dataset_coins)) != len(dataset_coins)
    ):
        raise HlcvsManifestError("HLCV dataset coins must be a nonempty list of unique identities")
    if (
        hlcvs.ndim != 3
        or hlcvs.shape[1] != len(dataset_coins)
        or timestamps.ndim != 1
        or not len(timestamps)
        or len(hlcvs) != len(timestamps)
        or btc_usd_prices.shape != timestamps.shape
        or np.any(np.diff(timestamps) <= 0)
    ):
        raise HlcvsManifestError("HLCV dataset arrays have inconsistent dimensions or timestamps")
    effective = manifest.get("effective", {})
    if (
        effective.get("coins") != dataset_coins
        or effective.get("start_ts") != int(timestamps[0])
        or effective.get("end_ts") != int(timestamps[-1])
    ):
        raise HlcvsManifestError("HLCV manifest effective coins/window disagree with saved arrays")
    requested_exchange = manifest.get("requested", {}).get("exchange")
    single_exchange = (
        requested_exchange if requested_exchange and requested_exchange != "combined" else None
    )
    for coin in dataset_coins:
        meta = mss.get(coin)
        source = manifest.get("sources", {}).get(coin, {})
        if not isinstance(meta, dict) or not meta:
            raise HlcvsManifestError(f"HLCV dataset missing saved market settings for {coin!r}")
        # Single-exchange caches can omit the per-coin venue; the build request
        # records it. Never substitute the current config's exchange.
        saved_exchange = source.get("market_settings_exchange") or single_exchange
        if not meta.get("exchange"):
            if not saved_exchange:
                raise HlcvsManifestError(f"HLCV dataset missing saved exchange for {coin!r}")
            meta["exchange"] = saved_exchange
        if saved_exchange and to_standard_exchange_name(saved_exchange) != to_standard_exchange_name(
            meta["exchange"]
        ):
            raise HlcvsManifestError(f"HLCV manifest market settings exchange conflicts for {coin!r}")
        saved_symbol = source.get("symbol")
        if saved_symbol:
            if meta.get("symbol") and meta["symbol"] != saved_symbol:
                raise HlcvsManifestError(f"HLCV manifest symbol conflicts for {coin!r}")
            meta["symbol"] = saved_symbol
        if not meta.get("symbol"):
            raise HlcvsManifestError(f"HLCV dataset missing saved symbol for {coin!r}")


def _slice_index_span(
    meta: dict,
    *,
    first_key: str,
    last_key: str,
    row_start: int,
    row_end: int,
    n_rows: int,
) -> None:
    if first_key not in meta or last_key not in meta:
        return
    try:
        first_idx = int(meta[first_key])
        last_idx = int(meta[last_key])
    except (TypeError, ValueError):
        meta.pop(first_key, None)
        meta.pop(last_key, None)
        return
    selected_last = int(row_end) - 1
    if first_idx > last_idx or last_idx < row_start or first_idx > selected_last:
        meta[first_key] = int(n_rows)
        meta[last_key] = int(n_rows)
        return
    meta[first_key] = int(max(first_idx, row_start) - row_start)
    meta[last_key] = int(min(last_idx, selected_last) - row_start)


def _slice_valid_window_metadata(meta: dict, *, row_start: int, row_end: int) -> None:
    n_rows = int(row_end) - int(row_start)
    _slice_index_span(
        meta,
        first_key="first_valid_index",
        last_key="last_valid_index",
        row_start=int(row_start),
        row_end=int(row_end),
        n_rows=n_rows,
    )
    _slice_index_span(
        meta,
        first_key="source_first_valid_index",
        last_key="source_last_valid_index",
        row_start=int(row_start),
        row_end=int(row_end),
        n_rows=n_rows,
    )
    meta.pop("trade_start_index", None)


def load_hlcvs_data_override(config, exchange):
    override_dir = get_optional_config_value(config, "backtest.hlcvs_data_dir")
    if not override_dir:
        return None
    mode = str(
        get_optional_config_value(config, "backtest.hlcvs_data_override_mode", "intersection")
        or "intersection"
    )
    if mode not in {"intersection", "dataset"}:
        raise ValueError("backtest.hlcvs_data_override_mode must be 'intersection' or 'dataset'")
    cache_dir = Path(override_dir).expanduser().resolve()
    if not cache_dir.is_dir():
        raise FileNotFoundError(f"HLCV dataset override directory does not exist: {cache_dir}")
    manifest = load_hlcvs_manifest(cache_dir)
    verified_arrays: dict = {}
    if manifest is None:
        raise HlcvsManifestError(f"HLCV dataset override {cache_dir} is missing manifest.json")
    elif manifest_has_required_schema(manifest):
        verify_hlcvs_manifest(cache_dir, manifest, out_arrays=verified_arrays)
    else:
        raise HlcvsManifestError(f"HLCV dataset override {cache_dir} has unsupported manifest schema")

    dataset_coins, hlcvs, mss, btc_usd_prices, timestamps = _load_hlcvs_cache_arrays(
        cache_dir, manifest, preloaded_arrays=verified_arrays
    )
    _validate_dataset_metadata(dataset_coins, hlcvs, mss, btc_usd_prices, timestamps, manifest)
    requested_coins = effective_backtest_data_coins(config)
    side_membership = _side_membership_for_override(
        config, dataset_coins, manifest, mode, mss
    )
    if mode == "intersection":
        requested_coins = _reconcile_override_identifiers(requested_coins, dataset_coins, mss)
        eligible_coins = set().union(*(set(coins) for coins in side_membership.values()))
        selected_coins = [coin for coin in dataset_coins if coin in eligible_coins]
    else:
        selected_coins = list(dataset_coins)
    if not selected_coins:
        raise ValueError("HLCV dataset override produced an empty coin set")

    dataset_start_ts = int(timestamps[0])
    dataset_end_ts = int(timestamps[-1])
    requested_start_ts = int(date_to_ts(require_config_value(config, "backtest.start_date")))
    requested_end_ts = int(date_to_ts(format_end_date(require_config_value(config, "backtest.end_date"))))
    warmup_minutes = compute_backtest_warmup_minutes(config)
    requested_data_start_ts = max(0, requested_start_ts - int(warmup_minutes) * 60_000)
    if mode == "intersection":
        effective_start_ts = max(requested_data_start_ts, dataset_start_ts)
        effective_end_ts = min(requested_end_ts, dataset_end_ts)
        if effective_end_ts < effective_start_ts:
            raise ValueError(
                "HLCV dataset override has no date overlap with requested backtest range"
            )
        effective_config_start_ts = max(requested_start_ts, dataset_start_ts)
    else:
        effective_start_ts = dataset_start_ts
        effective_end_ts = dataset_end_ts
        requested = manifest.get("requested", {})
        if requested.get("start_ts") is None or requested.get("warmup_minutes") is None:
            raise HlcvsManifestError("HLCV manifest missing requested start or warmup")
        manifest_requested_start_ts = int(requested["start_ts"])
        warmup_minutes = int(requested["warmup_minutes"])
        requested_data_start_ts = max(0, manifest_requested_start_ts - warmup_minutes * 60_000)
        effective_config_start_ts = min(
            max(int(manifest_requested_start_ts), dataset_start_ts),
            dataset_end_ts,
        )

    row_start = int(np.searchsorted(timestamps, effective_start_ts, side="left"))
    row_end = int(np.searchsorted(timestamps, effective_end_ts, side="right"))
    if row_start == row_end:
        raise ValueError("HLCV dataset override selected no timestamp rows")
    coin_positions = [dataset_coins.index(coin) for coin in selected_coins]
    hlcvs = _select_hlcvs(hlcvs, row_start, row_end, coin_positions)
    full_rows = row_start == 0 and row_end == len(timestamps)
    if not full_rows:
        btc_usd_prices = btc_usd_prices[row_start:row_end]
        timestamps = timestamps[row_start:row_end]
    btc_usd_prices = np.ascontiguousarray(btc_usd_prices)
    timestamps = np.ascontiguousarray(timestamps)

    selected_mss = {}
    for coin in selected_coins:
        meta = deepcopy(mss[coin])
        if not full_rows:
            _slice_valid_window_metadata(meta, row_start=row_start, row_end=row_end)
        selected_mss[coin] = meta
    if not set().union(*(set(side_membership[pside]) for pside in POSITION_SIDES)):
        raise ValueError("HLCV dataset override produced no side-eligible coins")

    original_approved = deepcopy(config.get("live", {}).get("approved_coins", {}))
    config.setdefault("live", {})["approved_coins"] = side_membership
    config.setdefault("backtest", {})["start_date"] = ts_to_date(int(effective_config_start_ts))
    config["backtest"]["end_date"] = ts_to_date(int(timestamps[-1]))
    config["backtest"].setdefault("cache_dir", {})[exchange] = str(cache_dir)
    config["backtest"].setdefault("coins", {})[exchange] = selected_coins
    override_meta = {
        "dataset_override": True,
        "dataset_override_mode": mode,
        "requested_coins": requested_coins,
        "dataset_coins": dataset_coins,
        "effective_backtested_coins": selected_coins,
        "original_requested_start_ts": requested_start_ts,
        "requested_start_ts": requested_start_ts,
        "effective_requested_start_ts": int(effective_config_start_ts),
        "requested_end_ts": requested_end_ts,
        "requested_data_start_ts": requested_data_start_ts,
        "warmup_minutes": int(warmup_minutes),
        "dataset_start_ts": dataset_start_ts,
        "dataset_end_ts": dataset_end_ts,
        "effective_start_ts": int(effective_config_start_ts),
        "effective_data_start_ts": int(timestamps[0]),
        "effective_end_ts": int(timestamps[-1]),
        "dropped_requested_coins": sorted(set(requested_coins) - set(selected_coins)),
        "added_dataset_only_coins": sorted(set(selected_coins) - set(requested_coins)),
        "input_side_membership": original_approved,
        "effective_side_membership": side_membership,
    }
    selected_mss["__meta__"] = deepcopy(mss.get("__meta__", {}))
    selected_mss["__meta__"].update(override_meta)
    config["_hlcvs_dataset_override_meta"] = deepcopy(override_meta)
    logging.info(
        "[hlcvs] override %s mode=%s coins=%s range=%s -> %s",
        cache_dir,
        mode,
        ",".join(selected_coins),
        ts_to_date(int(timestamps[0])),
        ts_to_date(int(timestamps[-1])),
    )
    results_path = str(Path(require_config_value(config, "backtest.base_dir")) / str(exchange)) + "/"
    return cache_dir, selected_coins, hlcvs, selected_mss, results_path, btc_usd_prices, timestamps
