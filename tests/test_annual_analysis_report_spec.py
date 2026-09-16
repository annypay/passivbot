"""Pin the strategy-research report convention.

`backtests/report_spec/annual_analysis.py` owns the fixed section skeleton, the metric-table
schemas and the renderer. `docs/ai/runbooks/strategy_report.md` documents the same contract for
humans, and `verify_annual_report.py` enforces it on real artifacts. These tests keep the
machine-readable half honest: the skeleton order, the table columns, and the edge cases a
single-sided or short-window run produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
REPORT_SPEC = REPO / "backtests" / "report_spec"
if str(REPORT_SPEC) not in sys.path:
    sys.path.insert(0, str(REPORT_SPEC))

import annual_analysis as spec  # noqa: E402


def equity_frame(start: str, periods: int, freq: str, base: float = 100_000.0) -> pd.DataFrame:
    stamps = pd.date_range(start=start, periods=periods, freq=freq, tz="UTC")
    balance = [base * (1.0 + 0.001 * index) for index in range(periods)]
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "usd_total_balance": balance,
            "usd_total_equity": balance,
            "strategy_equity": balance,
        }
    )


def fills_frame(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """rows: (timestamp, type, liquidity). A short-side run also passes `*_short` types."""
    return pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(stamp, tz="UTC"),
                "coin": "BTC",
                "pnl": 10.0,
                "fee_paid": -0.5,
                "type": order_type,
                "liquidity": liquidity,
                "wallet_exposure": 0.1,
                "twe_long": 0.1,
                "twe_short": 0.0,
                "psize": 1.0,
                "price": 100.0,
                "qty": 1.0,
            }
            for stamp, order_type, liquidity in rows
        ]
    )


def make_context(long_types: list[str], short_types: list[str] | None = None, periods: int = 40, freq: str = "30D") -> dict:
    rows = [(f"2024-01-{index + 1:02d}", order_type, "maker") for index, order_type in enumerate(long_types)]
    rows += [(f"2024-02-{index + 1:02d}", order_type, "maker") for index, order_type in enumerate(short_types or [])]
    fills = fills_frame(rows)
    equity = equity_frame("2024-01-01", periods, freq)
    return {
        "analysis": {
            "n_days": 1200.0,
            "effective_start_date": "2024-01-01",
            "effective_end_date": "2027-04-14",
            "backtest_completion_ratio": 1.0,
            "liquidated": False,
            "fills_count": len(fills),
            "drawdown_worst_strategy_eq": 0.1,
            "drawdown_worst_usd": 0.1,
            "drawdown_worst_mean_1pct_strategy_eq": 0.05,
            "gain_strategy_eq": 1.5,
            "gain_usd": 1.5,
            "sharpe_ratio_pnl": 0.4,
            "sortino_ratio_pnl": 0.4,
            "strategy_eq_recovery_days_max": 3.0,
            "position_held_days_max": 2.0,
            "strategy_eq_underwater_pct_mean": 0.01,
        },
        "config": {
            "backtest": {"balance_sample_divider": 1, "candle_interval_minutes": 1, "btc_collateral_cap": 0.0},
            "live": {"approved_coins": {"long": ["BTC"], "short": ["BTC"]}, "hedge_mode": False},
        },
        "annual": spec.build_period_table(equity, fills, "Y"),
        "monthly": spec.build_period_table(equity, fills, "M"),
        "coins": spec.build_coin_table(fills),
        "attribution": spec.build_attribution_table(fills),
        "run_record": {"candidate_id": "unit_test_profile", "universe": {"exchange": "binance", "coin_count": 1}},
        "result_label": "backtests/unit/test",
    }


def test_renderer_emits_the_fixed_skeleton_in_order():
    context = make_context(["entry_initial_normal_long", "close_grid_long"])
    report = spec.render_annual_analysis(context)
    assert spec.assert_report_structure(report) == []
    headings = spec.report_headings(report, include_detail=True)
    fixed = [h for h in headings if h in spec.REPORT_SECTIONS]
    assert fixed == list(spec.REPORT_SECTIONS)


def test_per_year_detail_sections_match_annual_rows():
    context = make_context(["entry_initial_normal_long", "close_grid_long"], periods=420, freq="2D")
    report = spec.render_annual_analysis(context)
    details = spec.detail_headings(report)
    assert len(details) == len(context["annual"])
    for heading, (_, row) in zip(details, context["annual"].iterrows()):
        assert heading == f"### {row['period']} 年明细（{row['coverage']}）"


def test_period_csv_schema_is_the_documented_one():
    context = make_context(["entry_initial_normal_long"])
    assert list(context["annual"].columns) == list(spec.PERIOD_CSV_COLUMNS)
    assert list(context["monthly"].columns) == list(spec.PERIOD_CSV_COLUMNS)
    # active_coins is the deliberate deviation kept last; the reference report's own last
    # column is preserved just before it.
    assert spec.PERIOD_CSV_COLUMNS[-1] == "active_coins"
    assert spec.PERIOD_CSV_COLUMNS[-2] == "max_abs_wallet_exposure_at_fill"
    assert list(context["coins"].columns) == list(spec.COIN_CSV_COLUMNS)


def test_attribution_table_always_has_two_rows():
    context = make_context(["entry_initial_normal_long", "close_grid_long"])
    report = spec.render_annual_analysis(context)
    attribution = spec.report_headings(report)
    assert spec.HEAD_ATTRIBUTION in attribution
    assert [row["direction"] for row in context["attribution"]] == ["多头", "空头"]
    # A long-only book keeps the short row and says why it is empty.
    assert "配置允许但本次未被触发" in report
    assert "`live.approved_coins.short` 配置了 1 个币" in report


def test_long_only_book_reports_the_unconfigured_case_too():
    context = make_context(["entry_initial_normal_long"])
    context["config"]["live"]["approved_coins"]["short"] = []
    report = spec.render_annual_analysis(context)
    assert "本方向**未配置**" in report


def test_two_sided_book_reports_both_directions_without_the_empty_note():
    context = make_context(["entry_initial_normal_long"], ["entry_initial_normal_short", "close_grid_short"])
    report = spec.render_annual_analysis(context)
    assert "空头成交为 0" not in report
    short_row = next(row for row in context["attribution"] if row["direction"] == "空头")
    assert short_row["fills_count"] == 2


def test_maker_only_book_says_taker_fees_do_not_bind():
    context = make_context(["entry_initial_normal_long"])
    report = spec.render_annual_analysis(context)
    assert "均为 maker 成交" in report
    assert "taker 费率不参与本次结果" in report


def test_report_states_the_sampling_resolution_from_the_config():
    context = make_context(["entry_initial_normal_long"])
    context["config"]["backtest"]["balance_sample_divider"] = 60
    report = spec.render_annual_analysis(context)
    assert "每 60 分钟采样" in report
    assert "逐分钟" not in report


def test_appendix_sits_between_the_skeleton_and_verifiable_data():
    context = make_context(["entry_initial_normal_long"])
    context["appendix"] = "### 研究专属小节\n\n内容"
    report = spec.render_annual_analysis(context)
    headings = spec.report_headings(report)
    assert spec.HEAD_APPENDIX in headings
    assert headings.index(spec.HEAD_MONTHLY) < headings.index(spec.HEAD_APPENDIX)
    assert headings.index(spec.HEAD_APPENDIX) < headings.index(spec.HEAD_VERIFIABLE)
    assert spec.assert_report_structure(report) == []


def test_interpretation_covers_the_four_required_observations():
    context = make_context(["entry_initial_normal_long", "close_grid_long"])
    report = spec.render_annual_analysis(context)
    interpretation = report.split(spec.HEAD_INTERPRETATION, 1)[1]
    assert "gain_usd" in interpretation
    assert "收益最好的自然年" in interpretation and "年内回撤最深的是" in interpretation
    assert "最差 1% 均值回撤" in interpretation
    assert "maker" in interpretation and "taker" in interpretation
    assert "不构成对未来的预测" in interpretation


def test_structure_gate_rejects_a_report_missing_a_section():
    context = make_context(["entry_initial_normal_long"])
    report = spec.render_annual_analysis(context)
    broken = report.replace(spec.HEAD_ATTRIBUTION, "## 别的章节")
    problems = spec.assert_report_structure(broken)
    assert any("多空成交归因" in problem for problem in problems)


def test_structure_gate_rejects_reordered_sections():
    context = make_context(["entry_initial_normal_long"])
    report = spec.render_annual_analysis(context)
    blocks = report.split("\n\n")
    scope = next(index for index, block in enumerate(blocks) if block.strip() == spec.HEAD_SCOPE)
    overall = next(index for index, block in enumerate(blocks) if block.strip() == spec.HEAD_OVERALL)
    blocks[scope], blocks[overall] = blocks[overall], blocks[scope]
    problems = spec.assert_report_structure("\n\n".join(blocks))
    assert any("out of order" in problem for problem in problems)


def test_report_numbers_come_from_the_context_not_from_constants():
    context = make_context(["entry_initial_normal_long"])
    context["analysis"]["gain_usd"] = 3.25
    context["analysis"]["drawdown_worst_strategy_eq"] = 0.5
    report = spec.render_annual_analysis(context)
    assert "3.250000" in report
    assert "50.00%" in report


def test_renderer_requires_a_complete_context():
    with pytest.raises(ValueError):
        spec.render_annual_analysis({"analysis": {}})