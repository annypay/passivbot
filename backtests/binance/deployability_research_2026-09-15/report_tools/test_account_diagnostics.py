import numpy as np
import pandas as pd
import pytest

from account_diagnostics import (
    FILL_COLUMNS, MINUTE_MS, apply_cost_overlay, holding_diagnostics,
    normalize_fills, reconstruct_account, drawdown,
)


T0 = 1_740_000_000_000


def frame(rows):
    values = []
    for minute, pnl, fee, cash, qty, price, size, entry, kind in rows:
        values.append([
            minute, T0 + minute * MINUTE_MS, "ETH", pnl, fee, cash, 0, cash,
            50000, qty, price, size, entry, kind, "maker", 0, 0, 0, 0,
        ])
    return normalize_fills(pd.DataFrame(values, columns=FILL_COLUMNS))


def test_fifo_partial_closes_keep_hedge_sides_separate_and_censor_open_lot():
    fills = frame([
        (0, 0, -0.1, 999.9, 1, 100, 1, 100, "entry_initial_normal_long"),
        (1, 0, -0.1, 999.8, -1, 100, -1, 100, "entry_initial_normal_short"),
        (2, 0, -0.1, 999.7, 1, 110, 2, 105, "entry_grid_normal_long"),
        (3, 5, -0.1, 1004.6, -1, 110, 1, 105, "close_grid_long"),
        (4, 5, -0.1, 1009.5, 1, 95, 0, 0, "close_grid_short"),
    ])
    lots, episodes, summary = holding_diagnostics(fills, T0 + 10 * MINUTE_MS, {"ETH": 1})
    assert lots["holding_minimum_minutes"].tolist() == [2, 2]
    assert summary["open_censored_episodes"] == 1
    assert episodes.loc[episodes["closed"], "position_side"].tolist() == ["short"]
    assert summary["unallocated_open_entry_fees_usdt"] == pytest.approx(-0.1)
    assert lots["allocated_average_cost_pnl_usdt"].sum() == pytest.approx(10.0)


def test_subminute_unknown_ordering_is_not_fabricated():
    fills = frame([
        (0, 0, 0, 1000, 1, 100, 1, 100, "entry_initial_normal_long"),
        (1, 1, 0, 1001, -1, 101, 0, 0, "close_grid_long"),
    ])
    lots, _, summary = holding_diagnostics(fills, T0 + 2 * MINUTE_MS, {"ETH": 1})
    assert lots["holding_minimum_minutes"].iloc[0] == 0
    assert lots["holding_maximum_minutes"].iloc[0] == 2
    assert summary["regular_close_notional_under_1m_share"] == 1.0


def test_wrong_position_chain_fails():
    fills = frame([(0, 0, 0, 1000, 1, 100, 2, 100, "entry_initial_normal_long")])
    with pytest.raises(AssertionError, match="position chain"):
        holding_diagnostics(fills, T0 + MINUTE_MS, {"ETH": 1})


def test_account_reconstructs_simultaneous_hedge_and_fill_cash():
    fills = frame([
        (1, 0, -0.1, 999.9, 1, 100, 1, 100, "entry_initial_normal_long"),
        (1, 0, -0.1, 999.8, -1, 100, -1, 100, "entry_initial_normal_short"),
        (3, 5, -0.1, 1004.7, -1, 105, 0, 0, "close_grid_long"),
    ])
    times = np.array([T0 + i * MINUTE_MS for i in range(5)])
    market = np.array([[[110, 90, price, 1]] for price in (100, 101, 102, 105, 103)])
    expected = np.array([1000, 999.8, 999.8, 999.7, 1001.7])
    raw = np.column_stack((times, expected, expected / 50000, expected))
    account, coins, metrics = reconstruct_account(raw, fills, times, market, ["ETH"],
                                                  {"ETH": 1}, 1000)
    np.testing.assert_allclose(account["balance"], [1000, 999.8, 999.8, 1004.7, 1004.7])
    assert metrics["equity_reconstruction_max_residual_usdt"] < 1e-8
    assert account["gross_mark_notional"].iloc[2] == pytest.approx(204)
    assert account["adverse_hl_equity"].iloc[2] == pytest.approx(979.8)
    assert coins["exposure_time_share"].iloc[0] == 1


def test_prefill_cash_does_not_use_future_fee():
    fills = frame([(2, 0, -1, 999, 1, 100, 1, 100, "entry_initial_normal_long")])
    times = np.arange(4) * MINUTE_MS + T0
    market = np.array([[[101, 99, 100, 1]]] * 4)
    expected = np.array([1000, 1000, 999, 999])
    raw = np.column_stack((times, expected, expected / 50000, expected))
    account, _, _ = reconstruct_account(raw, fills, times, market, ["ETH"], {"ETH": 1}, 1000)
    assert account["balance"].tolist() == [1000, 1000, 999, 999]


def test_overlay_charges_both_sides_and_terminal_flattening():
    account = pd.DataFrame({
        "equity": [1000.0, 1000, 1000], "balance": [1000.0] * 3,
        "adverse_hl_equity": [900.0] * 3,
        "gross_mark_notional": [0.0, 100.0, 200.0],
        "turnover_notional": [0.0, 100.0, 100.0],
    })
    result = apply_cost_overlay(account, extra_bps=10, funding_bps_per_8h=48,
                                terminal_exit_bps=10)
    assert result["equity"].iloc[-1] == pytest.approx(1000 - .2 - .001 - .2)
    assert account["equity"].iloc[-1] == 1000


def test_account_drawdown_includes_initial_and_unrecovered_tail():
    times = np.arange(4) * MINUTE_MS + T0
    result = drawdown(np.array([100.0, 90.0, 105.0, 84.0]), times, 100)
    assert result["mdd"] == pytest.approx(.2)
    assert result["recovery_ms"] is None
    assert result["terminal_underwater_days"] == pytest.approx(1 / 1440)


def test_canonical_coin_order_moves_prices_together_with_coin_labels():
    from run_deployability_study import canonicalize_state

    original = np.array([[[110, 90, 100, 1], [11, 9, 10, 1], [220, 180, 200, 1]]])
    state = {"coins": ["ETH", "BNB", "BTC"], "hlcvs_basket": original}
    canonical = canonicalize_state(state)
    assert canonical["coins"] == ["BNB", "BTC", "ETH"]
    assert canonical["hlcvs_basket"][0, :, 2].tolist() == [10, 200, 100]
    assert state["coins"] == ["ETH", "BNB", "BTC"]
    assert original[0, :, 2].tolist() == [100, 10, 200]
