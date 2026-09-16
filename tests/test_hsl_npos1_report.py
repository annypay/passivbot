"""Pin the `hsl_npos1` standalone deep analysis: frozen inputs, ledger math, report claims.

The study's own tooling lives under
`backtests/binance/hsl_npos1_analysis_2026-09-16/report_tools/`. These tests keep four things
honest without re-running the backtest:

1. the freeze gate actually rejects a drifted source profile,
2. the basket policy actually keeps an excluded coin out of both the run config and the
   materialized dataset,
3. the ledger derivations the report cites reproduce on synthetic fills,
4. a tampered metric CSV is actually rejected by the independent verifier.

The end-to-end checks run only when the artifact is present on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "backtests/binance/hsl_npos1_analysis_2026-09-16"
TOOLS = STUDY / "report_tools"
REPORT_SPEC = REPO / "backtests" / "report_spec"
for extra in (TOOLS, REPORT_SPEC):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import annual_analysis as spec  # noqa: E402
import hsl_npos1_spec as study  # noqa: E402
import build_run_config as builder  # noqa: E402
import render_hsl_npos1_report as report_tool  # noqa: E402
import verify_hsl_npos1 as verifier  # noqa: E402


def _artifact_run_dir() -> Path | None:
    if not study.RUNS_BASE.is_dir():
        return None
    runs = [
        path for path in study.dated_run_dirs(study.RUNS_BASE) if (path / "analysis.json").exists()
    ]
    return runs[-1] if runs else None


def _requires_artifacts() -> Path:
    run_dir = _artifact_run_dir()
    if run_dir is None:
        pytest.skip("no hsl_npos1 artifact on disk; run report_tools/run_backtest.py first")
    return run_dir


# --------------------------------------------------------------------------------------
# Freeze gate and basket policy
# --------------------------------------------------------------------------------------


def test_source_profile_hash_matches_the_frozen_gate():
    assert study.sha256_file(study.SOURCE_CONFIG) == study.SOURCE_CONFIG_SHA256


def test_run_config_is_the_profile_plus_only_allowlisted_changes():
    if not study.CONFIG_PATH.is_file():
        pytest.skip("run config has not been built yet")
    source = study.load_json(study.SOURCE_CONFIG)
    config = study.load_json(study.CONFIG_PATH)

    assert study.compare_subtrees(source.get("bot"), config.get("bot"), "bot") == []
    assert (
        study.compare_subtrees(
            source.get("coin_overrides"), config.get("coin_overrides"), "coin_overrides"
        )
        == []
    )
    differing = study.differing_paths(source.get("backtest"), config.get("backtest"), "backtest")
    unexpected = [
        key
        for key in differing
        if not builder._under_allowed_key(key) and key != "backtest.coins.binance"
    ]
    assert unexpected == [], unexpected


def test_excluded_coin_is_pruned_from_the_whole_config():
    if not study.CONFIG_PATH.is_file():
        pytest.skip("run config has not been built yet")
    config = study.load_json(study.CONFIG_PATH)
    for coin in study.EXCLUDED_COINS:
        assert coin not in (config["backtest"]["coins"]["binance"])
        assert coin not in config["live"]["approved_coins"]["long"]
    # The run must never declare a short side for this long-only profile.
    assert config["live"]["approved_coins"]["short"] == []


def test_build_config_rejects_a_drifted_source(monkeypatch):
    monkeypatch.setattr(study, "SOURCE_CONFIG_SHA256", "0" * 64)
    with pytest.raises(SystemExit) as excinfo:
        builder.build_config()
    assert "source profile drift" in str(excinfo.value)


def test_build_config_reports_the_declared_requested_coins():
    config, _ops, notes = builder.build_config()
    published = study.load_json(study.SOURCE_CONFIG)
    requested = published["live"]["approved_coins"]["long"]
    assert notes["requested_coins"] == requested
    assert set(notes["effective_coins"]) == set(requested) - set(study.EXCLUDED_COINS)
    assert notes["dropped_coins"] == sorted(set(requested) - set(notes["effective_coins"]))
    assert config["backtest"]["coins"]["binance"] == notes["effective_coins"]


# --------------------------------------------------------------------------------------
# Ledger derivations on synthetic fills
# --------------------------------------------------------------------------------------


def synthetic_fills() -> pd.DataFrame:
    minute = pd.Timestamp("2024-01-01T00:00:00Z")
    rows = [
        ("BTC", "entry_initial_normal_long", 1.0, -0.5, 0.10, "maker", minute),
        ("ETH", "entry_initial_normal_long", 2.0, -0.4, 0.20, "maker", minute),
        ("BTC", "entry_grid_normal_long", 3.0, -0.3, 0.25, "maker", minute + pd.Timedelta(minutes=5)),
        ("BTC", "close_auto_reduce_wel_long", 2.0, -0.1, 0.20, "maker", minute + pd.Timedelta(minutes=5)),
        ("ETH", "close_panic_long", 0.0, 1.5, 0.0, "maker", minute + pd.Timedelta(minutes=6)),
        ("BTC", "close_unstuck_long", 0.0, -2.0, 0.0, "maker", minute + pd.Timedelta(minutes=7)),
    ]
    frame = pd.DataFrame(
        [
            {
                "timestamp": stamp,
                "coin": coin,
                "type": order_type,
                "liquidity": liquidity,
                "psize": psize,
                "pnl": pnl,
                "fee_paid": fee,
                "wallet_exposure": abs(psize) / 10.0,
                "twe_long": 0.10 * abs(psize),
                "twe_short": 0.0,
                "pprice": 100.0,
            }
            for coin, order_type, psize, fee, pnl, liquidity, stamp in rows
        ]
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def synthetic_equity() -> pd.DataFrame:
    stamps = pd.date_range("2024-01-01T00:00:00Z", periods=6, freq="2min")
    values = [100.0, 101.0, 99.0, 102.0, 95.0, 96.0]
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "usd_total_balance": values,
            "usd_total_equity": values,
            "strategy_equity": values,
        }
    )


def synthetic_config() -> dict:
    return {
        "backtest": {"dynamic_wel_by_tradability": True},
        "live": {
            "minimum_coin_age_days": 60.0,
            "leverage": 10.0,
            "max_realized_loss_pct": 1.0,
        },
        "bot": {
            "long": {
                "forager": {"volatility_ema_span_1m": 225.0, "volume_drop_pct": 0.57},
                "risk": {
                    "n_positions": 10.0,
                    "total_wallet_exposure_limit": 1.25,
                    "we_excess_allowance_pct": 0.37,
                    "position_exposure_enforcer_enabled": True,
                    "position_exposure_enforcer_threshold": 0.994,
                    "total_exposure_enforcer_enabled": True,
                    "total_exposure_enforcer_threshold": 1.0,
                    "entry_cooldown_minutes": 0.0,
                },
                "strategy": {
                    "trailing_martingale": {
                        "volatility_ema_span_1h": 1690.0,
                        "entry": {
                            "ema_span_0": 770.0,
                            "ema_span_1": 210.0,
                            "initial_qty_pct": 0.0276,
                            "initial_ema_dist": 0.0097,
                            "threshold_base_pct": 0.033,
                            "threshold_we_weight": 0.135,
                            "threshold_volatility_1h_weight": 2.4,
                            "double_down_factor": 0.73,
                        },
                        "close": {
                            "qty_pct": 0.1,
                            "threshold_base_pct": 0.006,
                            "threshold_we_weight": -0.004,
                            "threshold_volatility_1h_weight": 1.0,
                        },
                    }
                },
                "unstuck": {
                    "enabled": True,
                    "threshold": 0.408,
                    "close_pct": 0.078,
                    "loss_allowance_pct": 0.0102,
                    "ema_dist": -0.07,
                },
                "hsl": {
                    "enabled": True,
                    "red_threshold": 0.2,
                    "ema_span_minutes": 60.0,
                    "cooldown_minutes_after_red": 0.0,
                    "no_restart_drawdown_threshold": 1.0,
                    "orange_tier_mode": "tp_only_with_active_entry_cancellation",
                    "panic_close_order_type": "limit",
                    "tier_ratios": {"orange": 0.75, "yellow": 0.5},
                },
            }
        },
    }


def synthetic_dataset() -> dict:
    return {
        "coins": ["BTC", "ETH"],
        "cache_hash": "deadbeefdeadbeef",
        "cache_dir_label": "binance__2_coins__test",
        "dataset_override": False,
        "content_hashes": {"hlcvs": "0" * 64},
    }


def test_reconstruct_positions_tracks_open_long_slots():
    fills = synthetic_fills()
    positions = report_tool.reconstruct_positions(fills)
    # entry BTC, entry ETH, then BTC grows, BTC reduces, ETH closes, BTC closes
    assert positions["nonzero_long_after_fill"].tolist() == [1, 2, 2, 2, 1, 0]


def test_reconstruct_positions_rejects_an_unknown_order_type():
    fills = synthetic_fills()
    fills.loc[0, "type"] = "entry_mystery"
    with pytest.raises(ValueError):
        report_tool.reconstruct_positions(fills)
    with pytest.raises(ValueError):
        report_tool.direction_from_type("entry_mystery")


def test_classify_types_flags_an_unknown_close_type():
    known = report_tool.classify_types({"close_grid_long": 3})
    assert known == {"unknown_entry_types": [], "unknown_close_types": []}
    unknown = report_tool.classify_types({"close_grid_long": 1, "close_mystery_long": 1})
    assert unknown["unknown_close_types"] == ["close_mystery_long"]


def test_ledger_facts_report_measured_values():
    fills = synthetic_fills()
    equity = synthetic_equity()
    ledger = report_tool.ledger_facts(fills, equity, synthetic_dataset(), synthetic_config(), {})

    assert ledger["fills_count"] == 6
    assert ledger["nonzero_long_max"] == 2
    assert ledger["entry_fills"] == 3
    assert ledger["max_abs_wallet_exposure"] == pytest.approx(0.3)
    assert ledger["single_coin_cap"] == pytest.approx(1.25 / 10.0 * 1.37)
    # Minute 0 holds two fills, minute 5 holds two.
    assert ledger["minutes_with_multiple_fills"] == 2
    assert ledger["max_fills_in_a_minute"] == 2
    assert ledger["risk_fill_counts"]["close_panic_long"] == 1
    assert ledger["risk_fill_counts"]["close_unstuck_long"] == 1
    assert ledger["risk_fill_counts"]["close_auto_reduce_wel_long"] == 1
    assert ledger["risk_fill_total"] == 3
    # Every fill is maker in this fixture.
    assert ledger["maker_fills"] == 6 and ledger["taker_fills"] == 0


def test_worst_equity_drawdown_locates_peak_and_trough():
    equity = synthetic_equity()
    dd = report_tool.worst_equity_drawdown(equity)
    assert dd["peak_value"] == pytest.approx(102.0)
    assert dd["trough_value"] == pytest.approx(95.0)
    assert dd["depth"] == pytest.approx(1.0 - 95.0 / 102.0)
    assert dd["peak_utc"].startswith("2024-01-01 00:06")
    assert dd["trough_utc"].startswith("2024-01-01 00:08")


def test_positions_at_replays_the_open_book():
    fills = synthetic_fills()
    open_book = report_tool.positions_at(fills, 3)
    assert list(open_book["coin"]) == ["BTC", "ETH"]
    assert open_book.loc[open_book["coin"] == "ETH", "qty"].iloc[0] == pytest.approx(2.0)
    # After the ETH panic close only BTC remains.
    assert list(report_tool.positions_at(fills, 4)["coin"]) == ["BTC"]
    assert report_tool.positions_at(fills, -1).empty


def test_coin_metrics_extras_is_alphabetical_and_sorted_for_the_report():
    coins = report_tool.coin_metrics_extras(synthetic_fills())
    assert list(coins["coin"]) == ["BTC", "ETH"]
    sorted_for_report = coins.sort_values(
        "net_realized_pnl_usd", ascending=False, kind="stable"
    ).reset_index(drop=True)
    assert sorted_for_report["net_realized_pnl_usd"].is_monotonic_decreasing


def test_lifecycle_facts_measure_the_idle_span():
    fills = synthetic_fills()
    equity = synthetic_equity()
    life = report_tool.lifecycle_facts(
        fills,
        equity,
        {"hard_stop_triggers": 1, "hard_stop_restarts": 0},
    )
    assert life["panic_count"] == 1
    assert life["panic_coins"] == ["ETH"]
    assert life["active_days"] == 1
    assert life["hard_stop_triggers"] == 1
    assert life["hard_stop_restarts"] == 0
    assert life["idle_days_after_last_fill"] > 0


# --------------------------------------------------------------------------------------
# Verifier helpers catch tampering
# --------------------------------------------------------------------------------------


def test_verifier_money_tolerance_is_relative_first():
    assert verifier.money_close(1_234_567.89, 1_234_567.89 + 0.5)
    assert not verifier.money_close(1_234_567.89, 1_234_567.89 + 100.0)
    assert verifier.ratio_close(0.4794055473580811, 0.4794055473580812)
    assert not verifier.ratio_close(0.4794055, 0.4799)


def test_verifier_treats_empty_csv_cells_as_empty_strings():
    import math

    assert verifier.as_text(float("nan")) == ""
    assert verifier.as_text(None) == ""
    assert verifier.as_text(2026) == "2026"


def test_verifier_parses_markdown_tables():
    text = (
        "## 总体结果\n\n"
        "| 项目 | 数值 |\n| --- | --- |\n| 成交数 / 强平 | 10 / 否 |\n\n"
        "## 月度汇总\n\n| 月份 | 覆盖 |\n| --- | --- |\n| 2024-01 | 完整 |\n"
    )
    tables = verifier.parse_tables(text)
    assert verifier.table_with_header(text, ["项目", "数值"]) == [["成交数 / 强平", "10 / 否"]]
    assert verifier.table_with_header(text, ["月份", "覆盖"]) == [["2024-01", "完整"]]
    assert verifier.table_with_header(text, ["没有", "这张表"]) is None
    assert len(tables) == 2


def test_verifier_rejects_a_tampered_annual_csv(tmp_path):
    run_dir = _requires_artifacts()
    for name in ("fills.csv", "balance_and_equity.csv.gz", "annual_metrics.csv", "coin_metrics.csv"):
        (tmp_path / name).write_bytes((run_dir / name).read_bytes())
    tampered = pd.read_csv(tmp_path / "annual_metrics.csv")
    tampered.loc[0, "net_realized_pnl_usd"] = float(tampered.loc[0, "net_realized_pnl_usd"]) + 1.0
    tampered.to_csv(tmp_path / "annual_metrics.csv", index=False)

    problems_before = len(verifier.PROBLEMS)
    verifier.PROBLEMS.clear()
    try:
        annual = verifier.recompute_period_table(
            verifier.load_equity(tmp_path), verifier.load_fills(tmp_path), "Y"
        )
        verifier.compare_period_table("annual_metrics.csv", tmp_path / "annual_metrics.csv", annual)
        assert any("net_realized_pnl_usd" in problem for problem in verifier.PROBLEMS)
    finally:
        verifier.PROBLEMS.clear()
        assert len(verifier.PROBLEMS) == 0
        del problems_before


# --------------------------------------------------------------------------------------
# End-to-end (artifact present)
# --------------------------------------------------------------------------------------


def test_artifact_passes_its_verifier():
    run_dir = _artifact_run_dir()
    if run_dir is None:
        pytest.skip("no hsl_npos1 artifact on disk")
    completed = subprocess.run(
        [sys.executable, str(TOOLS / "verify_hsl_npos1.py")],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-4000:]
    assert "failed: 0" in completed.stdout


def test_report_matches_the_convention():
    run_dir = _requires_artifacts()
    report = (run_dir / "annual_analysis.md").read_text(encoding="utf-8")
    assert spec.assert_report_structure(report) == []

    annual = pd.read_csv(run_dir / "annual_metrics.csv")
    monthly = pd.read_csv(run_dir / "monthly_metrics.csv")
    coins = pd.read_csv(run_dir / "coin_metrics.csv")
    assert list(annual.columns) == list(spec.PERIOD_CSV_COLUMNS)
    assert list(monthly.columns) == list(spec.PERIOD_CSV_COLUMNS)
    assert list(coins.columns) == list(spec.COIN_CSV_COLUMNS)

    details = spec.detail_headings(report)
    assert len(details) == len(annual)
    for heading, (_, row) in zip(details, annual.iterrows()):
        assert heading == f"### {row['period']} 年明细（{row['coverage']}）"


def test_artifact_records_the_reported_contract():
    run_dir = _requires_artifacts()
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    dataset = json.loads((run_dir / "dataset.json").read_text(encoding="utf-8"))
    analysis = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))

    assert config["backtest"]["execution_delay_bars"] == study.EXECUTION["execution_delay_bars"]
    assert config["backtest"]["intrabar_fill_order"] == study.EXECUTION["intrabar_fill_order"]
    assert config["backtest"]["maker_fee_override"] == study.COSTS["maker_fee_override"]
    assert config["backtest"]["taker_fee_override"] == study.COSTS["taker_fee_override"]
    assert config["backtest"]["btc_collateral_cap"] == 0.0
    assert analysis["backtest_completion_ratio"] == 1.0
    assert analysis["liquidated"] is False
    assert dataset["dataset_override"] is False

    for coin in study.EXCLUDED_COINS:
        assert coin not in dataset["coins"]
        assert coin not in config["live"]["approved_coins"]["long"]


def test_lifecycle_claim_holds_on_the_artifact():
    """The report's central finding, re-derived from the ledger rather than quoted."""
    run_dir = _requires_artifacts()
    fills = verifier.load_fills(run_dir)
    equity = verifier.load_equity(run_dir)
    active_days = int(fills["timestamp"].dt.floor("D").nunique())
    idle_days = (
        equity["timestamp"].iloc[-1] - fills["timestamp"].max()
    ).total_seconds() / 86400.0

    # If the strategy ever trades again for a long stretch, this assertion is the tripwire that
    # forces the report's 核心事实 section to be re-reviewed instead of silently going stale.
    assert idle_days > 365.0, f"idle span collapsed to {idle_days:.2f} days"
    assert active_days < len(fills), "active-day accounting broke"
    panic = fills.loc[fills["type"].astype(str) == "close_panic_long"]
    assert len(panic) >= 1
    assert panic["timestamp"].dt.floor("D").nunique() == 1
