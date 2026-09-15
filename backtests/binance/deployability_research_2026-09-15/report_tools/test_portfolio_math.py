import pandas as pd
import pytest

from account_diagnostics import apply_cost_overlay, drawdown
from run_execution_and_portfolios import combine_accounts


def account(equity, turnover, gross):
    return pd.DataFrame({
        "equity": equity, "balance": equity, "adverse_hl_equity": equity,
        "gross_mark_notional": gross, "cost_notional": gross,
        "turnover_notional": turnover, "fees": [0.0] * len(equity),
    }, index=pd.date_range("2025-01-01", periods=len(equity), freq="min", tz="UTC"))


def test_combined_mdd_uses_summed_equity_not_average_individual_mdds():
    left = account([50000., 40000., 52000.], [0., 10., 20.], [0., 10., 20.])
    right = account([50000., 60000., 59000.], [0., 20., 40.], [0., 20., 40.])
    combined = combine_accounts(left, right)
    assert combined["equity"].tolist() == [100000, 100000, 111000]
    times = combined.index.astype("int64").to_numpy() // 1_000_000
    assert drawdown(combined["equity"].to_numpy(), times, 100000)["mdd"] == 0
    assert drawdown(left["equity"].to_numpy(), times, 50000)["mdd"] == pytest.approx(.2)


def test_gross_funding_and_exit_reserves_are_additive_without_netting_sleeves():
    left = account([50000., 49999., 50001.], [0., 100., 200.], [0., 200., 100.])
    right = account([50000., 50001., 50005.], [0., 300., 100.], [0., 500., 300.])
    combined = apply_cost_overlay(combine_accounts(left, right), extra_bps=2,
                                   funding_bps_per_8h=1)
    separately = sum(
        apply_cost_overlay(item, extra_bps=2, funding_bps_per_8h=1)["equity"]
        for item in (left, right))
    pd.testing.assert_series_equal(combined["equity"], separately)


def test_incompatible_sleeve_timeline_is_rejected():
    left = account([50000., 50001.], [0., 100.], [0., 100.])
    right = left.copy()
    right.index += pd.Timedelta(minutes=1)
    with pytest.raises(AssertionError, match="timelines differ"):
        combine_accounts(left, right)
