import json
from copy import deepcopy

import numpy as np
import pytest
import utils
import hlcvs_override

from backtest import build_backtest_payload, ensure_valid_index_metadata
from config_utils import get_template_config
from hlcvs_manifest import (
    HlcvsManifestError,
    build_hlcvs_manifest,
    hash_json_value,
    load_hlcvs_manifest,
    write_hlcvs_manifest,
)
from hlcvs_override import _select_hlcvs, load_hlcvs_data_override


def _base_config(cache_dir, *, mode="dataset"):
    return {
        "backtest": {
            "base_dir": "backtests",
            "compress_cache": False,
            "end_date": "2025-01-03",
            "exchanges": ["binance"],
            "gap_tolerance_ohlcvs_minutes": 120,
            "hlcvs_data_dir": str(cache_dir),
            "hlcvs_data_override_mode": mode,
            "start_date": "2025-01-01",
        },
        "bot": {
            "long": {"n_positions": 1, "total_wallet_exposure_limit": 1.0},
            "short": {"n_positions": 1, "total_wallet_exposure_limit": 1.0},
        },
        "live": {
            "approved_coins": {"long": ["BTC"], "short": []},
            "minimum_coin_age_days": 0,
            "warmup_ratio": 0.0,
            "max_warmup_minutes": 0,
        },
    }


def _write_dataset(cache_dir, *, manifest_start_date="2025-01-01", mss_overrides=None, hlcvs=None):
    cache_dir.mkdir(parents=True)
    coins = ["BTC", "ETH"]
    timestamps = np.array([1735689600000, 1735776000000, 1735862400000], dtype=np.int64)
    if hlcvs is None:
        hlcvs = np.arange(24, dtype=np.float64).reshape(3, 2, 4)
    btc_usd_prices = np.array([100.0, 101.0, 102.0], dtype=np.float64)
    mss = {
        "BTC": {"exchange": "binance", "symbol": "BTC/USDT:USDT", "id": "BTCUSDT"},
        "ETH": {"exchange": "binance", "symbol": "ETH/USDT:USDT", "id": "ETHUSDT"},
        "__meta__": {"btc_source_exchange": "binanceusdm"},
    }
    for coin, overrides in (mss_overrides or {}).items():
        mss.setdefault(coin, {}).update(overrides)
    np.save(cache_dir / "hlcvs.npy", hlcvs)
    np.save(cache_dir / "timestamps.npy", timestamps)
    np.save(cache_dir / "btc_usd_prices.npy", btc_usd_prices)
    (cache_dir / "coins.json").write_text(json.dumps(coins))
    (cache_dir / "market_specific_settings.json").write_text(json.dumps(mss))
    manifest_config = _base_config(cache_dir)
    manifest_config["backtest"]["start_date"] = manifest_start_date
    manifest_config["live"]["approved_coins"] = {"long": ["BTC", "ETH"], "short": ["ETH"]}
    manifest = build_hlcvs_manifest(
        config=manifest_config,
        exchange="binance",
        cache_hash="abc123",
        coins=coins,
        hlcvs=hlcvs,
        mss=mss,
        btc_usd_prices=btc_usd_prices,
        timestamps=timestamps,
        warmup_minutes=0,
        compressed=False,
    )
    write_hlcvs_manifest(cache_dir, manifest)
    return timestamps, hlcvs


def test_hlcvs_dataset_override_dataset_mode_updates_side_membership_and_effective_config(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    timestamps, hlcvs = _write_dataset(cache_dir)
    config = _base_config(cache_dir, mode="dataset")

    cache_dir_out, coins, out_hlcvs, mss, _results_path, btc_usd_prices, out_timestamps = (
        load_hlcvs_data_override(config, "binance")
    )

    assert cache_dir_out == cache_dir.resolve()
    assert coins == ["BTC", "ETH"]
    assert config["live"]["approved_coins"] == {"long": ["BTC", "ETH"], "short": ["ETH"]}
    assert config["backtest"]["coins"]["binance"] == ["BTC", "ETH"]
    np.testing.assert_array_equal(out_timestamps, timestamps)
    np.testing.assert_allclose(out_hlcvs, hlcvs)
    np.testing.assert_allclose(btc_usd_prices, np.array([100.0, 101.0, 102.0]))
    assert mss["__meta__"]["dataset_override"] is True


def test_hlcvs_dataset_override_dataset_mode_preserves_manifest_requested_start(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    timestamps, _hlcvs = _write_dataset(cache_dir, manifest_start_date="2025-01-02")
    config = _base_config(cache_dir, mode="dataset")
    config["backtest"]["start_date"] = "2024-12-01"

    _cache_dir, _coins, _hlcvs, mss, _results_path, _btc, out_timestamps = (
        load_hlcvs_data_override(config, "binance")
    )

    np.testing.assert_array_equal(out_timestamps, timestamps)
    assert config["backtest"]["start_date"].startswith("2025-01-02")
    assert mss["__meta__"]["effective_data_start_ts"] == int(timestamps[0])
    assert mss["__meta__"]["effective_requested_start_ts"] == int(timestamps[1])


def test_hlcvs_dataset_override_intersection_mode_preserves_input_side_membership(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    _write_dataset(cache_dir)
    config = _base_config(cache_dir, mode="intersection")

    _cache_dir, coins, _hlcvs, _mss, _results_path, _btc, _timestamps = (
        load_hlcvs_data_override(config, "binance")
    )

    assert coins == ["BTC"]
    assert config["live"]["approved_coins"] == {"long": ["BTC"], "short": []}


@pytest.mark.parametrize("identifier", ["BTCUSDT", "BTC/USDT:USDT", "binanceusdm::BTCUSDT"])
def test_hlcvs_dataset_override_reconciles_exact_identifier_to_dataset_key(
    tmp_path, monkeypatch, identifier
):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    _write_dataset(cache_dir)

    def forbidden(*args, **kwargs):
        pytest.fail("dataset replay must not consult current market mappings")

    monkeypatch.setattr(utils, "coin_to_symbol", forbidden)
    monkeypatch.setattr(utils, "_load_coin_to_symbol_map", forbidden)
    config = _base_config(cache_dir, mode="intersection")
    config["live"]["approved_coins"] = {"long": [identifier], "short": []}

    _cache_dir, coins, _hlcvs, _mss, _results_path, _btc, _timestamps = (
        load_hlcvs_data_override(config, "binance")
    )

    assert coins == ["BTC"]
    assert config["live"]["approved_coins"] == {"long": ["BTC"], "short": []}


def test_hlcvs_dataset_override_intersection_applies_ignored_saved_identities(tmp_path):
    cache_dir = tmp_path / "dataset"
    _write_dataset(cache_dir)
    config = _base_config(cache_dir, mode="intersection")
    config["live"]["approved_coins"] = {"long": ["BTC", "ETH"], "short": ["ETHUSDT"]}
    config["live"]["ignored_coins"] = {"long": ["binance::ETHUSDT"], "short": []}

    result = load_hlcvs_data_override(config, "binance")

    assert result[1] == ["BTC", "ETH"]
    assert config["live"]["approved_coins"] == {"long": ["BTC"], "short": ["ETH"]}


def test_hlcvs_dataset_override_never_guesses_missing_exact_identity(tmp_path):
    cache_dir = tmp_path / "dataset"
    _write_dataset(cache_dir, mss_overrides={"BTC": {"id": None}})
    config = _base_config(cache_dir, mode="intersection")
    config["live"]["approved_coins"] = {"long": ["BTCUSDT"], "short": []}

    with pytest.raises(ValueError, match="empty coin set"):
        load_hlcvs_data_override(config, "binance")


def test_hlcvs_intersection_requires_explicit_coin_selection(tmp_path):
    cache_dir = tmp_path / "dataset"
    _write_dataset(cache_dir)
    config = _base_config(cache_dir, mode="intersection")
    config["live"]["approved_coins"] = {"long": ["all"], "short": []}

    with pytest.raises(ValueError, match="requires explicit approved coins"):
        load_hlcvs_data_override(config, "binance")


def test_hlcvs_dataset_mode_uses_manifest_without_expanding_all(tmp_path):
    cache_dir = tmp_path / "dataset"
    _write_dataset(cache_dir)
    config = _base_config(cache_dir)
    config["live"]["approved_coins"] = {"long": ["all"], "short": []}

    result = load_hlcvs_data_override(config, "binance")

    assert result[1] == ["BTC", "ETH"]
    assert config["live"]["approved_coins"] == {"long": ["BTC", "ETH"], "short": ["ETH"]}


def test_hlcvs_dataset_override_rejects_ambiguous_saved_identity(tmp_path):
    cache_dir = tmp_path / "dataset"
    _write_dataset(cache_dir, mss_overrides={"BTC": {"base": "TOKEN"}, "ETH": {"base": "TOKEN"}})
    config = _base_config(cache_dir, mode="intersection")
    config["live"]["approved_coins"] = {"long": ["TOKEN"], "short": []}

    with pytest.raises(ValueError, match="ambiguous HLCV dataset identifier"):
        load_hlcvs_data_override(config, "binance")


def test_hlcvs_dataset_override_prefers_exact_dataset_key_over_base_alias(tmp_path):
    cache_dir = tmp_path / "dataset"
    _write_dataset(cache_dir, mss_overrides={"ETH": {"base": "BTC"}})
    config = _base_config(cache_dir, mode="intersection")

    result = load_hlcvs_data_override(config, "binance")

    assert result[1] == ["BTC"]


def test_hlcvs_dataset_override_preserves_verified_arrays_and_metadata(tmp_path, monkeypatch):
    cache_dir = tmp_path / "dataset"
    metadata = {
        "BTC": {
            "maker": 0.0002,
            "taker": 0.0005,
            "qty_step": 0.001,
            "price_step": 0.1,
            "min_qty": 0.001,
            "min_cost": 5.0,
            "c_mult": 1.0,
            "first_valid_index": 1,
            "last_valid_index": 2,
            "source_first_valid_index": 1,
            "source_last_valid_index": 2,
            "trade_start_index": 2,
            "warmup_minutes": 1,
        },
        "__meta__": {
            "source_selection": {"BTC": {"selection_reason": "saved"}},
            "warmup_minutes_provided": 1440,
        },
    }
    _write_dataset(cache_dir, mss_overrides=metadata)
    original_mss = json.loads((cache_dir / "market_specific_settings.json").read_text())
    verified = {}
    verify = hlcvs_override.verify_hlcvs_manifest
    calls = []

    def capture_verification(*args, **kwargs):
        calls.append(args[0])
        result = verify(*args, **kwargs)
        verified.update(kwargs["out_arrays"])
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("verified arrays must not be loaded again")

    monkeypatch.setattr(hlcvs_override, "verify_hlcvs_manifest", capture_verification)
    monkeypatch.setattr(hlcvs_override, "load_numpy_artifact", forbidden)
    config = _base_config(cache_dir)
    original_config = deepcopy(config)
    result = load_hlcvs_data_override(config, "binance")

    assert len(calls) == 1
    for array, name in [(result[2], "hlcvs"), (result[5], "btc_usd_prices"), (result[6], "timestamps")]:
        assert array is verified[name]
        assert array.flags.c_contiguous
    assert result[3]["BTC"] == original_mss["BTC"]
    for key, value in original_mss["__meta__"].items():
        assert result[3]["__meta__"][key] == value
    assert config["bot"] == original_config["bot"]
    assert json.loads((cache_dir / "market_specific_settings.json").read_text()) == original_mss


@pytest.mark.parametrize(
    "row_start,row_end,positions,shares_memory",
    [
        (0, 6, [0, 1, 2], True),
        (1, 5, [0, 1, 2], True),
        (0, 6, [0, 1], False),
        (1, 5, [0, 2], False),
        (0, 6, [2, 0, 1], False),
    ],
)
def test_hlcvs_selection_copies_only_when_needed(row_start, row_end, positions, shares_memory):
    source = np.arange(72, dtype=np.float64).reshape(6, 3, 4)
    original = source.copy()
    source.flags.writeable = False

    result = _select_hlcvs(source, row_start, row_end, positions)

    np.testing.assert_array_equal(result, original[row_start:row_end][:, positions, :])
    np.testing.assert_array_equal(source, original)
    assert np.shares_memory(source, result) == shares_memory
    assert result.flags.c_contiguous
    if row_start == 0 and row_end == len(source) and positions == [0, 1, 2]:
        assert result is source


def test_hlcvs_selection_makes_noncontiguous_input_contiguous():
    source = np.arange(144, dtype=np.float64).reshape(12, 3, 4)[::2]

    result = _select_hlcvs(source, 0, len(source), [0, 1, 2])

    np.testing.assert_array_equal(result, source)
    assert result.flags.c_contiguous
    assert not np.shares_memory(result, source)


def test_hlcvs_intersection_rebases_window_metadata_without_mutating_dataset(tmp_path):
    cache_dir = tmp_path / "dataset"
    _write_dataset(
        cache_dir,
        mss_overrides={
            "BTC": {
                "first_valid_index": 1,
                "last_valid_index": 2,
                "source_first_valid_index": 1,
                "source_last_valid_index": 2,
            }
        },
    )
    saved = json.loads((cache_dir / "market_specific_settings.json").read_text())
    config = _base_config(cache_dir, mode="intersection")
    config["backtest"]["start_date"] = "2025-01-02"

    result = load_hlcvs_data_override(config, "binance")

    assert result[2].shape == (2, 1, 4)
    assert result[3]["BTC"]["first_valid_index"] == 0
    assert result[3]["BTC"]["last_valid_index"] == 1
    assert result[3]["BTC"]["source_first_valid_index"] == 0
    assert result[3]["BTC"]["source_last_valid_index"] == 1
    assert json.loads((cache_dir / "market_specific_settings.json").read_text()) == saved


@pytest.mark.parametrize("missing", ["market_settings", "symbol", "side_membership", "warmup"])
def test_hlcvs_dataset_override_rejects_incomplete_saved_metadata(tmp_path, missing):
    cache_dir = tmp_path / "dataset"
    _write_dataset(cache_dir)
    manifest = load_hlcvs_manifest(cache_dir)
    if missing in {"market_settings", "symbol"}:
        mss_path = cache_dir / "market_specific_settings.json"
        mss = json.loads(mss_path.read_text())
        if missing == "market_settings":
            del mss["BTC"]
        else:
            del mss["BTC"]["symbol"]
            del manifest["sources"]["BTC"]["symbol"]
        mss_path.write_text(json.dumps(mss))
        manifest["files"]["market_specific_settings"]["sha256"] = hash_json_value(mss)
    elif missing == "side_membership":
        del manifest["effective"]["build_side_membership"]
        del manifest["effective"]["side_membership"]
    else:
        del manifest["requested"]["warmup_minutes"]
    write_hlcvs_manifest(cache_dir, manifest)

    with pytest.raises(HlcvsManifestError, match="missing|requires"):
        load_hlcvs_data_override(_base_config(cache_dir), "binance")


@pytest.mark.parametrize("field", ["coins", "start_ts", "end_ts", "build_side_membership"])
def test_hlcvs_dataset_override_rejects_conflicting_manifest_metadata(tmp_path, field):
    cache_dir = tmp_path / "dataset"
    _write_dataset(cache_dir)
    manifest = load_hlcvs_manifest(cache_dir)
    manifest["effective"][field] = {
        "coins": ["ETH", "BTC"],
        "start_ts": 0,
        "end_ts": 0,
        "build_side_membership": {"long": ["BTC", "MISSING"], "short": []},
    }[field]
    write_hlcvs_manifest(cache_dir, manifest)

    with pytest.raises(HlcvsManifestError, match="disagree|invalid"):
        load_hlcvs_data_override(_base_config(cache_dir), "binance")


def test_hlcvs_dataset_override_intersection_mode_keeps_available_warmup_rows(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    timestamps, _hlcvs = _write_dataset(cache_dir)
    config = _base_config(cache_dir, mode="intersection")
    config["backtest"]["start_date"] = "2025-01-02"
    config["live"]["warmup_ratio"] = 1.0
    config["live"]["max_warmup_minutes"] = 1440
    config["bot"]["long"]["ema_span_0"] = 1440.0

    _cache_dir, _coins, _hlcvs, mss, _results_path, _btc, out_timestamps = (
        load_hlcvs_data_override(config, "binance")
    )

    np.testing.assert_array_equal(out_timestamps, timestamps)
    assert config["backtest"]["start_date"].startswith("2025-01-02")
    assert mss["__meta__"]["warmup_minutes"] == 1440
    assert mss["__meta__"]["requested_data_start_ts"] == int(timestamps[0])
    assert mss["__meta__"]["effective_data_start_ts"] == int(timestamps[0])
    assert mss["__meta__"]["original_requested_start_ts"] == int(timestamps[1])
    assert mss["__meta__"]["effective_requested_start_ts"] == int(timestamps[1])


def test_hlcvs_dataset_override_preserves_sliced_valid_window_metadata(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "edge_filled__abc123"
    hlcvs = np.ones((3, 2, 4), dtype=np.float64)
    hlcvs[0, 0, :] = np.array([100.0, 100.0, 100.0, 0.0])
    mss_overrides = {
        "BTC": {
            "first_valid_index": 1,
            "last_valid_index": 2,
            "source_first_valid_index": 1,
            "source_last_valid_index": 2,
            "trade_start_index": 1,
        }
    }
    _write_dataset(cache_dir, mss_overrides=mss_overrides, hlcvs=hlcvs)
    config = _base_config(cache_dir, mode="intersection")
    config["live"]["approved_coins"] = {"long": ["BTC"], "short": []}

    _cache_dir, coins, out_hlcvs, mss, _results_path, _btc, _timestamps = (
        load_hlcvs_data_override(config, "binance")
    )
    ensure_valid_index_metadata(mss, out_hlcvs, coins)

    assert coins == ["BTC"]
    assert mss["BTC"]["first_valid_index"] == 1
    assert mss["BTC"]["last_valid_index"] == 2
    assert mss["BTC"]["source_first_valid_index"] == 1
    assert mss["BTC"]["source_last_valid_index"] == 2
    assert mss["BTC"]["trade_start_index"] == 1


def test_backtest_payload_prefers_effective_override_requested_start():
    config = get_template_config()
    config["backtest"]["coins"] = {"binance": ["BTC"]}
    config["backtest"]["start_date"] = "2025-01-02"
    config["backtest"]["end_date"] = "2025-01-03"
    mss = {
        "BTC": {
            "maker": 0.0001,
            "taker": 0.0005,
            "qty_step": 0.001,
            "price_step": 0.1,
            "min_qty": 0.001,
            "min_cost": 10.0,
            "c_mult": 1.0,
        },
        "__meta__": {
            "requested_start_ts": 1735689600000,
            "effective_requested_start_ts": 1735776000000,
        },
    }

    payload = build_backtest_payload(
        np.zeros((2, 1, 4), dtype=np.float64),
        mss,
        config,
        "binance",
        np.ones(2, dtype=np.float64),
        timestamps=np.array([1735776000000, 1735776060000], dtype=np.int64),
    )

    assert payload.backtest_params["requested_start_timestamp_ms"] == 1735776000000


def test_hlcvs_dataset_override_rejects_manifest_hash_mismatch(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    _write_dataset(cache_dir)
    config = _base_config(cache_dir, mode="dataset")
    np.save(cache_dir / "btc_usd_prices.npy", np.array([999.0, 998.0, 997.0]))

    with pytest.raises(HlcvsManifestError, match="hash mismatch"):
        load_hlcvs_data_override(config, "binance")


def test_hlcvs_dataset_override_loads_manifest_verified_json_paths(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    _write_dataset(cache_dir)
    manifest = load_hlcvs_manifest(cache_dir)
    alt_coins = ["BTC", "ETH"]
    alt_mss = {
        "ETH": {"exchange": "binance", "marker": "verified_alt"},
        "BTC": {"exchange": "binance"},
        "__meta__": {"btc_source_exchange": "binanceusdm"},
    }
    (cache_dir / "coins.alt.json").write_text(json.dumps(alt_coins))
    (cache_dir / "market_specific_settings.alt.json").write_text(json.dumps(alt_mss))
    (cache_dir / "coins.json").write_text(json.dumps(["BTC"]))
    (cache_dir / "market_specific_settings.json").write_text(
        json.dumps({"BTC": {"exchange": "binance", "marker": "stale_fixed_name"}})
    )
    manifest["files"]["coins"] = {
        "path": "coins.alt.json",
        "sha256": hash_json_value(alt_coins),
    }
    manifest["files"]["market_specific_settings"] = {
        "path": "market_specific_settings.alt.json",
        "sha256": hash_json_value(alt_mss),
    }
    write_hlcvs_manifest(cache_dir, manifest)
    config = _base_config(cache_dir, mode="dataset")

    _cache_dir, coins, _hlcvs, mss, _results_path, _btc, _timestamps = (
        load_hlcvs_data_override(config, "binance")
    )

    assert coins == ["BTC", "ETH"]
    assert mss["ETH"]["marker"] == "verified_alt"


def test_hlcvs_dataset_override_rejects_manifest_paths_outside_dataset(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    _write_dataset(cache_dir)
    manifest = load_hlcvs_manifest(cache_dir)
    manifest["files"]["coins"]["path"] = "../coins.json"
    write_hlcvs_manifest(cache_dir, manifest)
    config = _base_config(cache_dir, mode="dataset")

    with pytest.raises(HlcvsManifestError, match="escapes dataset directory"):
        load_hlcvs_data_override(config, "binance")


def test_hlcvs_dataset_override_rejects_legacy_dataset_without_timestamps(tmp_path):
    cache_dir = tmp_path / "hlcvs_data" / "custom__abc123"
    _write_dataset(cache_dir)
    (cache_dir / "manifest.json").unlink()
    (cache_dir / "timestamps.npy").unlink()
    config = _base_config(cache_dir, mode="dataset")

    with pytest.raises(HlcvsManifestError, match="missing manifest"):
        load_hlcvs_data_override(config, "binance")
