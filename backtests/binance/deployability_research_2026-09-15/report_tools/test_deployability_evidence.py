import numpy as np
import pandas as pd
import pytest

from analyze_deployability_evidence import monthly_risk, reduction_economics
from test_account_diagnostics import frame


def test_month_local_drawdown_does_not_hide_inherited_global_drawdown():
    index = pd.date_range("2025-01-31 23:58", periods=4, freq="min", tz="UTC")
    account = pd.DataFrame({
        "equity": [100., 80., 81., 82.], "balance": [100.] * 4,
        "adverse_hl_equity": [100., 80., 81., 82.],
        "gross_mark_notional": [0.] * 4, "turnover_notional": [0.] * 4,
    }, index=index)
    result = monthly_risk(account, 100.)
    assert result["mdd"].tolist() == pytest.approx([.2, 0.])
    assert result["worst_since_inception_drawdown_in_month"].tolist() == pytest.approx([.2, .19])
    assert not result["full_calendar_month"].any()


def test_month_risk_refuses_a_missing_minute():
    index = pd.to_datetime(["2025-01-01 00:00", "2025-01-01 00:02"], utc=True)
    with pytest.raises(AssertionError, match="complete minute timeline"):
        monthly_risk(pd.DataFrame(index=index), 100.)


def test_reducer_followup_does_not_double_count_an_entry_or_call_pending_order_fresh():
    fills = frame([
        (0, 0, 0, 1000, 2, 100, 2, 100, "entry_grid_normal_long"),
        (2, -5, 0, 995, -.5, 90, 1.5, 100, "close_auto_reduce_wel_long"),
        (3, -5, 0, 990, -.5, 90, 1, 100, "close_auto_reduce_wel_long"),
        (4, 0, 0, 990, 1, 90, 2, 95, "entry_grid_normal_long"),
        (5, 0, 0, 990, 1, 90, 3, 280 / 3, "entry_grid_normal_long"),
    ])
    audit = pd.DataFrame({"symbol": ["ETH"] * 5, "fill_index": [0, 2, 3, 4, 5],
                          "decision_index": [-2, 0, 1, 2, 3]})
    types, followups, metrics = reduction_economics(fills, audit, {"ETH": 1.})
    assert len(followups) == 1
    assert followups["decided_before_reduction_fill"].tolist() == [True]
    assert metrics["entry_within_5m_of_partial_reducer_notional_share"] == pytest.approx(90 / 380)
    assert metrics["protective_close_gross_loss_usdt"] == 10.
    assert types["net_cash_pnl_usdt"].sum() == pytest.approx(-10.)
