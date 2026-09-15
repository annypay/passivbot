"""Independently check recorded fills against immutable HLCV and runtime evidence."""

import gzip
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path("backtests/binance/causal_comparison_2026-09-14")
SCENARIOS = ["B0", "C1", "C2", "C3", "C4"]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify():
    frozen = json.loads((ROOT / "frozen_inputs.json").read_text())
    cache = Path(frozen["dataset_relative_path"])
    assert sha256(cache / "manifest.json") == frozen["manifest_sha256"]
    for name, expected in frozen["dataset_file_sha256"].items():
        assert sha256(cache / name) == expected, name
    coins = json.loads((cache / "coins.json").read_text())
    settings = json.loads((cache / "market_specific_settings.json").read_text())
    with gzip.open(cache / "timestamps.npy.gz", "rb") as handle:
        timestamps = np.load(handle, allow_pickle=False)
    with gzip.open(cache / "hlcvs.npy.gz", "rb") as handle:
        hlcvs = np.load(handle, allow_pickle=False)
    assert hlcvs.shape == (len(timestamps), len(coins), 4)
    assert (np.diff(timestamps) == 60_000).all()
    coin_indices = {coin: index for index, coin in enumerate(coins)}
    baseline_config = None
    summaries = []
    temporal = []
    for scenario in SCENARIOS:
        identity = json.loads((ROOT / scenario / "run_identity.json").read_text())
        directory = Path(identity["result_dir"])
        config = json.loads((directory / "config.json").read_text())
        if baseline_config is None:
            baseline_config = config
        for section in ("bot", "live", "coin_overrides"):
            assert config.get(section) == baseline_config.get(section), (scenario, section)
        for name in (
            "starting_balance", "start_date", "end_date", "exchanges",
            "btc_collateral_cap", "maker_fee_override", "taker_fee_override",
        ):
            assert config["backtest"].get(name) == baseline_config["backtest"].get(name)
        assert identity["source_manifest_sha256"] == frozen["manifest_sha256"]
        assert identity["dataset_logical_hashes"] == frozen["dataset_logical_hashes"]
        assert identity["effective_shape"] == list(hlcvs.shape)
        assert identity["network_disabled"]
        fills = pd.read_csv(directory / "fills.csv")
        indices = fills["index"].to_numpy(dtype=np.int64)
        columns = fills["coin"].map(coin_indices).to_numpy(dtype=np.int64)
        first = fills["coin"].map({c: settings[c]["first_valid_index"] for c in coins})
        last = fills["coin"].map({c: settings[c]["last_valid_index"] for c in coins})
        assert (indices >= first).all() and (indices <= last).all()
        assert (indices < len(timestamps) - 1).all()
        recorded = pd.to_datetime(fills["timestamp"], utc=True).astype("int64") // 1_000_000
        np.testing.assert_array_equal(recorded, timestamps[indices])
        low = hlcvs[indices, columns, 1]
        high = hlcvs[indices, columns, 0]
        price = fills["price"].to_numpy()
        buy = fills["qty"].to_numpy() > 0
        maker = fills["liquidity"].to_numpy() == "maker"
        assert (fills["qty"] != 0).all()
        crossed = np.where(buy, low < price, high > price)
        equal = np.where(buy, low == price, high == price)
        assert crossed[maker].all(), (scenario, np.flatnonzero(maker & ~crossed)[:10])
        assert not equal[maker].any()
        summaries.append({
            "scenario": scenario,
            "fill_rows": len(fills),
            "maker_rows": int(maker.sum()),
            "taker_rows": int((~maker).sum()),
            "buy_rows": int(buy.sum()),
            "sell_rows": int((~buy).sum()),
            "maker_strict_hl_cross_rows": int((maker & crossed).sum()),
            "maker_equality_only_rows": int((maker & equal).sum()),
            "maker_non_cross_rows": int((maker & ~crossed).sum()),
            "invalid_window_fill_rows": 0,
            "end_sentinel_fill_rows": 0,
            "first_fill_candle": fills["timestamp"].iloc[0],
            "last_fill_candle": fills["timestamp"].iloc[-1],
        })
        if scenario == "B0":
            continue
        audit = pd.read_csv(ROOT / scenario / "execution_boundary_audit.csv")
        decision = audit["decision_index"].to_numpy(dtype=np.int64)
        activation = audit["activation_index"].to_numpy(dtype=np.int64)
        np.testing.assert_array_equal(audit["decision_close_timestamp_ms"], timestamps[decision] + 60_000)
        np.testing.assert_array_equal(audit["activation_timestamp_ms"], timestamps[activation])
        np.testing.assert_array_equal(audit["fill_candle_open_timestamp_ms"], timestamps[indices])
        np.testing.assert_array_equal(audit["fill_candle_close_timestamp_ms"], timestamps[indices] + 60_000)
        np.testing.assert_array_equal(audit["fill_index"], indices)
        assert (activation == decision + identity["contract"]["delay"] + 1).all()
        assert (indices >= activation).all()
        assert len(audit) == len(fills)
        for label, index in (
            ("first_fill", 0),
            ("last_fill", len(audit) - 1),
            ("longest_resting", int(np.argmax(indices - activation))),
        ):
            example = audit.iloc[index].to_dict()
            example.update(scenario=scenario, example=label)
            for key in (
                "decision_close_timestamp_ms", "activation_timestamp_ms",
                "fill_candle_open_timestamp_ms", "fill_candle_close_timestamp_ms",
            ):
                example[key.replace("_ms", "_utc")] = pd.Timestamp(
                    example[key], unit="ms", tz="UTC"
                ).isoformat()
            temporal.append(example)
    pd.DataFrame(summaries).to_csv(ROOT / "fill_price_boundary_summary.csv", index=False)
    pd.DataFrame(temporal).to_csv(ROOT / "execution_boundary_examples.csv", index=False)
    print(pd.DataFrame(summaries).to_string(index=False))


if __name__ == "__main__":
    verify()
