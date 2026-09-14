"""Real-extension causal simulation checks; run only after rebuilding Rust."""

from copy import deepcopy
import csv

import numpy as np
import pytest

from backtest import build_backtest_payload, execute_backtest
from config import get_template_config, prepare_config


START_MS = 1_704_067_200_000
BAR_MS = 60_000
COIN = "BTC"
EXCHANGE = "binance"
FILL_INDEX = 0
FILL_TIMESTAMP = 1
FILL_QTY = 9
FILL_PRICE = 10
POSITION_SIZE = 11
ORDER_TYPE = 13


@pytest.fixture(scope="module", autouse=True)
def require_real_passivbot_rust_module():
    import passivbot_rust as pbr
    from rust_utils import verify_loaded_runtime_extension

    if getattr(pbr, "__is_stub__", False):
        pytest.fail("causal backtest integration requires the real Rust extension")
    identity = verify_loaded_runtime_extension()
    assert identity["runtime_compiled_path"] is not None
    return pbr


def _config(*, side="long", delay=0, fill_order="close_first", audit_path=None):
    config = get_template_config()
    config["live"].update(
        strategy_kind="ema_anchor",
        approved_coins={
            "long": [COIN] if side == "long" else [],
            "short": [COIN] if side == "short" else [],
        },
        ignored_coins={"long": [], "short": []},
        hedge_mode=True,
        warmup_ratio=1.0,
        max_warmup_minutes=3,
        market_orders_allowed=False,
        max_realized_loss_pct=1.0,
        forager_score_hysteresis_pct=0.0,
    )
    config["backtest"].update(
        exchanges=[EXCHANGE],
        start_date="2024-01-01",
        end_date="2024-01-02",
        starting_balance=1_000.0,
        btc_collateral_cap=0.0,
        btc_collateral_ltv_cap=None,
        candle_interval_minutes=1,
        filter_by_min_effective_cost=False,
        maker_fee_override=0.0,
        taker_fee_override=0.0,
        execution_delay_bars=delay,
        intrabar_fill_order=fill_order,
        execution_audit_path=None if audit_path is None else str(audit_path),
    )
    for pside in ("long", "short"):
        bot = config["bot"][pside]
        bot["risk"].update(
            n_positions=1,
            total_wallet_exposure_limit=1.0 if pside == side else 0.0,
            entry_cooldown_minutes=0.0,
            we_excess_allowance_pct=0.0,
            position_exposure_enforcer_enabled=False,
            total_exposure_enforcer_enabled=False,
            total_exposure_entry_gate_enabled=True,
        )
        bot["hsl"]["enabled"] = False
        bot["unstuck"]["enabled"] = False
        bot["forager"].update(
            volume_ema_span_1m=2.0,
            volatility_ema_span_1m=2.0,
            volume_drop_pct=0.0,
        )
        bot["strategy"]["ema_anchor"].update(
            ema_span_0=2.0,
            ema_span_1=3.0,
            base_qty_pct=0.2,
            offset=0.01,
            offset_psize_weight=0.0,
            entry_double_down_factor=0.0,
            offset_volatility_ema_span_1m=2.0,
            offset_volatility_ema_span_1h=2.0,
            offset_volatility_1m_weight=0.0,
            offset_volatility_1h_weight=0.0,
        )
    prepared = prepare_config(config, target="backtest", verbose=False)
    prepared["backtest"]["coins"] = {EXCHANGE: [COIN]}
    return prepared


def _inputs(n_bars=32):
    timestamps = START_MS + np.arange(n_bars, dtype=np.int64) * BAR_MS
    hlcvs = np.empty((n_bars, 1, 4), dtype=np.float64)
    hlcvs[:, 0, :] = [110.0, 90.0, 100.0, 1_000.0]
    market_settings = {
        COIN: {
            "exchange": EXCHANGE,
            "qty_step": 0.001,
            "price_step": 0.01,
            "min_qty": 0.001,
            "min_cost": 1.0,
            "c_mult": 1.0,
            "maker": 0.0,
            "taker": 0.0,
            "first_valid_index": 0,
            "last_valid_index": n_bars - 1,
            "warmup_minutes": 3,
        },
        "__meta__": {
            "requested_start_ts": START_MS,
            "requested_start_date": "2024-01-01",
            "warmup_minutes_requested": 3,
            "warmup_minutes_provided": 3,
        },
    }
    return hlcvs, market_settings, np.full(n_bars, 50_000.0), timestamps


def _payload(config, inputs):
    hlcvs, market_settings, btc_prices, timestamps = inputs
    return build_backtest_payload(
        hlcvs,
        market_settings,
        config,
        EXCHANGE,
        btc_prices,
        timestamps,
        skip_btc_analysis=True,
    )


def _run(config, inputs):
    payload = _payload(config, inputs)
    fills, _, _ = execute_backtest(payload, config)
    return np.asarray(fills, dtype=object), payload


def _assert_fill_clock(fills, timestamps):
    assert len(fills) > 0, "the deterministic fixture must actually trade"
    for fill in fills:
        index = int(fill[FILL_INDEX])
        assert 0 < index < len(timestamps) - 1
        assert int(fill[FILL_TIMESTAMP]) == int(timestamps[index])


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("fill_order", ["close_first", "entry_first"])
def test_extra_delay_shifts_first_fill_one_bar_without_signal_bar_fill(side, fill_order):
    inputs = _inputs()
    results = []
    for delay in (0, 1):
        config = _config(side=side, delay=delay, fill_order=fill_order)
        fills, payload = _run(config, inputs)
        _assert_fill_clock(fills, inputs[3])
        assert payload.backtest_params["execution_delay_bars"] == delay
        assert payload.backtest_params["intrabar_fill_order"] == fill_order
        assert str(fills[0, ORDER_TYPE]) == f"entry_ema_anchor_{side}"
        # Orders are first planned after the warmup guard, then activate on a
        # later candle; even that first entry cannot create a same-candle close.
        first_decision = max(1, payload.backtest_params["global_warmup_bars"]) + 1
        assert int(fills[0, FILL_INDEX]) == first_decision + 1 + delay
        first_bar = fills[fills[:, FILL_INDEX] == fills[0, FILL_INDEX]]
        assert all(str(row[ORDER_TYPE]).startswith("entry_") for row in first_bar)
        results.append(fills)

    assert int(results[1][0, FILL_TIMESTAMP]) - int(results[0][0, FILL_TIMESTAMP]) == BAR_MS


@pytest.mark.parametrize("side", ["long", "short"])
def test_resting_entry_close_collision_obeys_selected_order_without_overclosing(side):
    inputs = _inputs()
    collisions = {}
    for fill_order in ("close_first", "entry_first"):
        fills, _ = _run(_config(side=side, fill_order=fill_order), inputs)
        _assert_fill_clock(fills, inputs[3])
        collision = None
        for index in sorted(set(int(value) for value in fills[:, FILL_INDEX])):
            rows = fills[fills[:, FILL_INDEX] == index]
            kinds = [str(row[ORDER_TYPE]).split("_", 1)[0] for row in rows]
            if "entry" in kinds and "close" in kinds:
                collision = rows
                expected = (
                    ["close", "entry"] if fill_order == "close_first" else ["entry", "close"]
                )
                assert kinds == expected
                break
        assert collision is not None, "fixture must collide resting entry and close orders"
        assert int(collision[0, FILL_INDEX]) > int(fills[0, FILL_INDEX])
        collisions[fill_order] = collision

        position = 0.0
        for row in fills:
            qty = float(row[FILL_QTY])
            if str(row[ORDER_TYPE]).startswith("close_"):
                assert position != 0.0
                assert qty * position < 0.0
                assert abs(qty) <= abs(position) + 1e-9
            position += qty
            assert float(row[POSITION_SIZE]) == pytest.approx(position, abs=1e-9)
            assert position >= -1e-9 if side == "long" else position <= 1e-9

    close_first = collisions["close_first"]
    entry_first = collisions["entry_first"]
    assert int(close_first[0, FILL_INDEX]) == int(entry_first[0, FILL_INDEX])
    assert close_first[:, ORDER_TYPE].tolist() != entry_first[:, ORDER_TYPE].tolist()
    assert float(close_first[0, POSITION_SIZE]) != pytest.approx(
        float(entry_first[0, POSITION_SIZE])
    )


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("delay", [0, 1])
@pytest.mark.parametrize("fill_order", ["close_first", "entry_first"])
def test_future_suffix_cannot_change_prefix_fills(side, delay, fill_order):
    original = _inputs()
    changed = deepcopy(original)
    cutoff = 14
    changed[0][cutoff:, 0, :] = [125.0, 105.0, 115.0, 10.0]
    config = _config(side=side, delay=delay, fill_order=fill_order)
    original_fills, _ = _run(config, original)
    changed_fills, _ = _run(config, changed)
    original_prefix = original_fills[original_fills[:, FILL_INDEX] < cutoff]
    changed_prefix = changed_fills[changed_fills[:, FILL_INDEX] < cutoff]

    assert len(original_prefix) > 0
    np.testing.assert_array_equal(original_prefix, changed_prefix)


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("delay", [0, 1])
def test_missing_held_valuation_fails_instead_of_anticipatory_tail_exit(side, delay):
    inputs = _inputs()
    hlcvs, market_settings, btc_prices, timestamps = inputs
    if side == "long":
        hlcvs[:, 0, 0] = 100.0
    else:
        hlcvs[:, 0, 1] = 100.0
    config = _config(side=side, delay=delay)
    complete_fills, _ = _run(config, inputs)
    assert len(complete_fills) > 0
    assert all(str(row[ORDER_TYPE]).startswith("entry_") for row in complete_fills)
    assert abs(float(complete_fills[-1, POSITION_SIZE])) > 0.0

    missing_index = 14
    # Exceed the historical anticipatory-delist tail threshold without doing
    # more work: the corrected simulation must stop at the first missing bar.
    hlcvs, market_settings, btc_prices, timestamps = _inputs(missing_index + 1_402)
    if side == "long":
        hlcvs[:, 0, 0] = 100.0
    else:
        hlcvs[:, 0, 1] = 100.0
    hlcvs[missing_index:, 0, :] = np.nan
    market_settings[COIN]["last_valid_index"] = missing_index - 1
    payload = _payload(config, (hlcvs, market_settings, btc_prices, timestamps))
    with pytest.raises(
        ValueError,
        match=rf"missing held-position valuation candle.*candle {missing_index}",
    ):
        execute_backtest(payload, config)


@pytest.mark.parametrize("delay", [0, 1])
def test_execution_audit_is_fill_only_and_refuses_to_overwrite_existing_output(tmp_path, delay):
    audit_path = tmp_path / "execution.csv"
    config = _config(delay=delay, audit_path=audit_path)
    inputs = _inputs()
    payload = _payload(config, inputs)
    assert not audit_path.exists(), "payload construction must not create simulation output"

    fills, _, _ = execute_backtest(payload, config)

    assert len(fills) > 0
    original = audit_path.read_bytes()
    with audit_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == [
            "order_id",
            "symbol",
            "pside",
            "order_type",
            "decision_index",
            "decision_close_timestamp_ms",
            "activation_index",
            "activation_timestamp_ms",
            "fill_index",
            "fill_candle_open_timestamp_ms",
            "fill_candle_close_timestamp_ms",
            "fill_qty",
            "fill_price",
        ]
        rows = list(reader)
    assert len(rows) == len(fills)
    assert len({row["order_id"] for row in rows}) == len(rows)
    for row, fill in zip(rows, fills, strict=True):
        decision = int(row["decision_index"])
        activation = int(row["activation_index"])
        fill_index = int(row["fill_index"])
        assert activation == decision + 1 + delay
        assert fill_index >= activation
        assert int(row["decision_close_timestamp_ms"]) == START_MS + (decision + 1) * BAR_MS
        assert int(row["activation_timestamp_ms"]) == START_MS + activation * BAR_MS
        assert fill_index == int(fill[FILL_INDEX])
        assert int(row["fill_candle_open_timestamp_ms"]) == int(fill[FILL_TIMESTAMP])
        assert int(row["fill_candle_close_timestamp_ms"]) == int(fill[FILL_TIMESTAMP]) + BAR_MS
        assert row["symbol"] == COIN
        assert row["pside"] == "long"
        assert row["order_type"] == str(fill[ORDER_TYPE])
        assert float(row["fill_qty"]) == pytest.approx(float(fill[FILL_QTY]))
        assert float(row["fill_price"]) == pytest.approx(float(fill[FILL_PRICE]))
    with pytest.raises(ValueError, match="cannot create execution audit"):
        execute_backtest(_payload(config, inputs), config)
    assert audit_path.read_bytes() == original
