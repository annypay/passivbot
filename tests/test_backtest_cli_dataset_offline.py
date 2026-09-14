import asyncio
import json
import socket
import sys
from copy import deepcopy

import numpy as np
import pytest

import backtest
import hlcv_preparation
import hlcvs_override
import utils
from config_utils import get_template_config
from hlcvs_manifest import build_hlcvs_manifest, write_hlcvs_manifest
from rust_utils import verify_loaded_runtime_extension


@pytest.fixture
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("explicit dataset replay reached market discovery or the network")

    for module in (backtest, utils, hlcv_preparation):
        monkeypatch.setattr(module, "load_markets", forbidden)
    monkeypatch.setattr(backtest, "format_approved_ignored_coins", forbidden)
    monkeypatch.setattr(backtest, "coin_to_symbol", forbidden)
    monkeypatch.setattr(utils, "coin_to_symbol", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def _cli_config(tmp_path):
    config = get_template_config()
    config["backtest"].update(
        {
            "base_dir": str(tmp_path / "results"),
            "exchanges": ["binance"],
            "start_date": "2024-12-01",
            "end_date": "2025-02-01",
            "suite_enabled": False,
        }
    )
    config["live"].update(
        {
            "approved_coins": {"long": ["BTC"], "short": []},
            "ignored_coins": {"long": [], "short": []},
            "warmup_ratio": 0.0,
            "max_warmup_minutes": 0,
            "minimum_coin_age_days": 0,
        }
    )
    return config


def _write_cli_dataset(cache_dir, config):
    cache_dir.mkdir()
    combined = len(config["backtest"]["exchanges"]) > 1
    timestamps = 1735689600000 + np.arange(31, dtype=np.int64) * 60_000
    hlcvs = np.tile([101.0, 99.0, 100.0, 1000.0], (31, 2, 1))
    btc = np.full(len(timestamps), 100.0)
    coins = ["BTC", "ETH"]
    mss = {
        coin: {
            "symbol": f"{coin}/USDT:USDT",
            "id": f"{coin}USDT",
            "maker": 0.0002,
            "taker": 0.0005,
            "qty_step": 0.001,
            "price_step": 0.1,
            "min_qty": 0.001,
            "min_cost": 5.0,
            "c_mult": 1.0,
            "first_valid_index": 0,
            "last_valid_index": len(timestamps) - 1,
            "source_first_valid_index": 0,
            "source_last_valid_index": len(timestamps) - 1,
            "warmup_minutes": 0,
            "trade_start_index": 0,
        }
        for coin in coins
    }
    if combined:
        mss["BTC"]["exchange"] = "binance"
        mss["ETH"]["exchange"] = "bybit"
    mss["__meta__"] = {"btc_source_exchange": "binanceusdm"}
    np.save(cache_dir / "hlcvs.npy", hlcvs)
    np.save(cache_dir / "timestamps.npy", timestamps)
    np.save(cache_dir / "btc_usd_prices.npy", btc)
    (cache_dir / "coins.json").write_text(json.dumps(coins))
    (cache_dir / "market_specific_settings.json").write_text(json.dumps(mss))
    build_config = deepcopy(config)
    build_config["backtest"]["start_date"] = "2025-01-01T00:02:00"
    build_config["backtest"]["end_date"] = "2025-01-01T00:30:00"
    build_config["live"]["approved_coins"] = {"long": coins, "short": []}
    write_hlcvs_manifest(
        cache_dir,
        build_hlcvs_manifest(
            config=build_config,
            exchange="combined" if combined else "binance",
            cache_hash="offline-fixture",
            coins=coins,
            hlcvs=hlcvs,
            mss=mss,
            btc_usd_prices=btc,
            timestamps=timestamps,
            warmup_minutes=0,
            compressed=False,
        ),
    )
    return timestamps, mss


@pytest.mark.parametrize("mode", ["dataset", "intersection"])
@pytest.mark.parametrize("combined", [False, True])
def test_cli_replays_verified_dataset_offline(
    tmp_path, monkeypatch, no_network, mode, combined
):
    identity = verify_loaded_runtime_extension()
    assert identity.get("runtime_compiled_path") and not identity.get("skipped")
    monkeypatch.chdir(tmp_path)
    config = _cli_config(tmp_path)
    exchange = "combined" if combined else "binance"
    if combined:
        config["backtest"]["exchanges"] = ["binance", "bybit"]
    cache_dir = tmp_path / "dataset"
    timestamps, saved_mss = _write_cli_dataset(cache_dir, config)
    if combined:
        config["backtest"]["coin_sources"] = {"BTCUSDT": "bitget"}
        config["backtest"]["market_settings_sources"] = {"BTCUSDT": "kucoin"}
    config["backtest"].update(
        {"hlcvs_data_dir": str(cache_dir), "hlcvs_data_override_mode": mode}
    )
    config["live"]["approved_coins"] = {"long": ["BTCUSDT"], "short": []}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["backtest", str(path), "--suite", "n", "-dp", "all"])
    verified = []
    verify = hlcvs_override.verify_hlcvs_manifest
    run = backtest.run_backtest
    captured = {}

    def capture_verification(*args, **kwargs):
        result = verify(*args, **kwargs)
        verified.append(kwargs["out_arrays"])
        return result

    def capture_run(hlcvs, mss, run_config, exchange, btc, ts, **kwargs):
        captured.update(config=deepcopy(run_config), mss=deepcopy(mss), timestamps=ts.copy())
        if mode == "dataset":
            assert hlcvs is verified[0]["hlcvs"]
        return run(hlcvs, mss, run_config, exchange, btc, ts, **kwargs)

    monkeypatch.setattr(hlcvs_override, "verify_hlcvs_manifest", capture_verification)
    monkeypatch.setattr(backtest, "run_backtest", capture_run)

    asyncio.run(backtest.main())

    assert len(verified) == 1
    np.testing.assert_array_equal(captured["timestamps"], timestamps)
    assert captured["mss"]["BTC"] == {**saved_mss["BTC"], "exchange": "binance"}
    assert captured["mss"]["__meta__"]["btc_source_exchange"] == "binanceusdm"
    expected_coins = ["BTC", "ETH"] if mode == "dataset" else ["BTC"]
    effective = captured["config"]
    assert effective["backtest"]["coins"][exchange] == expected_coins
    if combined:
        assert effective["backtest"]["coin_sources"] == config["backtest"]["coin_sources"]
        assert effective["backtest"]["market_settings_sources"] == config["backtest"][
            "market_settings_sources"
        ]
    assert effective["live"]["approved_coins"] == {"long": expected_coins, "short": []}
    assert effective["backtest"]["end_date"] == "2025-01-01T00:30:00"
    expected_start = "2025-01-01T00:02:00" if mode == "dataset" else "2025-01-01T00:00:00"
    assert effective["backtest"]["start_date"] == expected_start
    result_dirs = list((tmp_path / "results" / exchange).iterdir())
    assert len(result_dirs) == 1
    original = json.loads((result_dirs[0] / "config.original.json").read_text())
    saved = json.loads((result_dirs[0] / "config.json").read_text())
    assert original["live"]["approved_coins"] == config["live"]["approved_coins"]
    assert original["backtest"]["start_date"] == config["backtest"]["start_date"]
    assert original["backtest"]["end_date"] == config["backtest"]["end_date"]
    assert saved["live"]["approved_coins"] == effective["live"]["approved_coins"]
    assert saved["bot"] == original["bot"]
    assert saved["coin_overrides"] == original["coin_overrides"]


def test_cli_invalid_override_fails_without_market_discovery(tmp_path, monkeypatch, no_network):
    config = _cli_config(tmp_path)
    config["backtest"]["hlcvs_data_dir"] = str(tmp_path / "missing-dataset")
    path = tmp_path / "input.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["backtest", str(path), "--suite", "n"])

    with pytest.raises(FileNotFoundError, match="override directory does not exist"):
        asyncio.run(backtest.main())


def test_cli_without_override_retains_normal_market_discovery(tmp_path, monkeypatch, no_network):
    config = _cli_config(tmp_path)
    path = tmp_path / "input.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(sys, "argv", ["backtest", str(path), "--suite", "n"])
    calls = []

    async def load_markets(exchange):
        calls.append(("markets", exchange))

    async def format_coins(config, exchanges, **kwargs):
        calls.append(("coins", exchanges, kwargs))

    class PreparationReached(Exception):
        pass

    async def prepare(config, exchange, **kwargs):
        assert "_original_backtest_config" not in config
        raise PreparationReached

    monkeypatch.setattr(backtest, "load_markets", load_markets)
    monkeypatch.setattr(backtest, "format_approved_ignored_coins", format_coins)
    monkeypatch.setattr(backtest, "prepare_hlcvs_mss", prepare)

    with pytest.raises(PreparationReached):
        asyncio.run(backtest.main())

    assert calls == [
        ("markets", "binance"),
        ("coins", ["binance"], {"prefer_backtest_coin_source_keys": True}),
    ]
