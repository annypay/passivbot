"""Pin the g3_cth0000 replay study: frozen inputs, ledger math and report claims.

The study's own tooling lives under
`backtests/binance/gt0000_replay_2026-09-16/report_tools/`. These tests keep three things
honest without re-running the backtest:

1. the frozen run config is still the published seed profile plus the one declared op,
2. the ledger/period derivations the report cites reproduce on synthetic fills,
3. a tampered metric CSV or metric record is actually rejected.

The end-to-end check runs only when the replay artifact is present on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "backtests/binance/gt0000_replay_2026-09-16/report_tools"
REPORT_SPEC = REPO / "backtests" / "report_spec"
for extra in (TOOLS, REPORT_SPEC):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import annual_analysis as spec  # noqa: E402
import cell_spec as study  # noqa: E402
import generate_annual_report as report_tool  # noqa: E402
import verify_replay_report as verifier  # noqa: E402

FROZEN_CONFIG = study.CONFIG_PATH
CELL_INPUT = study.CELL_INPUT_PATH


def _requires_frozen_inputs() -> None:
    if not FROZEN_CONFIG.exists() or not CELL_INPUT.exists():
        pytest.skip("run report_tools/build_cell_config.py first")


def _artifact_run_dir() -> Path | None:
    if not study.RUNS_BASE.is_dir():
        return None
    runs = sorted(
        path
        for path in study.RUNS_BASE.iterdir()
        if path.is_dir() and (path / "analysis.json").exists()
    )
    return runs[-1] if runs else None


# --------------------------------------------------------------------------------------
# Frozen inputs
# --------------------------------------------------------------------------------------


def test_frozen_config_is_seed_profile_plus_the_declared_op():
    _requires_frozen_inputs()
    config = study.load_json(FROZEN_CONFIG)
    seed = study.load_json(study.SEED_CONFIG_PATH)
    problems = study.compare_subtrees(seed["bot"], config["bot"], "bot")
    patched = study.get_path(config, study.PATCH_PATH)
    differing = sorted(
        key
        for key in study.flatten(seed["bot"], "bot")
        if not study.numeric_equal(
            study.flatten(seed["bot"], "bot")[key], study.flatten(config["bot"], "bot")[key]
        )
    )
    assert differing == [study.PATCH_PATH], problems
    assert patched == 0.0


def test_cell_input_matches_the_study_cell_record():
    _requires_frozen_inputs()
    cell_input = study.load_json(CELL_INPUT)
    record = study.load_json(study.CELL_RESULT_PATH)
    assert cell_input["cell_id"] == record["cell_id"] == study.CELL_ID
    assert cell_input["ops"] == record["ops"]
    assert int(cell_input["metrics"][study.CELL_FILLS_KEY]) == int(record["metrics"]["fill_rows"])
    assert cell_input["coins"] == sorted(record["coins"])
    for key in study.CELL_METRIC_KEYS:
        if key in cell_input["metrics"]:
            assert cell_input["metrics"][key] == pytest.approx(record["metrics"][key])


def test_cell_input_records_the_reported_contract():
    _requires_frozen_inputs()
    cell_input = study.load_json(CELL_INPUT)
    contract = study.load_json(study.CONTRACT_PATH)
    assert cell_input["execution"] == study.EXECUTION
    assert cell_input["costs"] == study.COSTS
    assert cell_input["contract"]["cell_matrix_sha256"] == contract["cell_matrix_sha256"]
    assert cell_input["contract"]["seed_config_sha256"] == contract["seed"]["config_sha256"]
    assert cell_input["coins"] == sorted(study.load_json(study.FROZEN_CACHE / "coins.json"))


# --------------------------------------------------------------------------------------
# Ledger derivations on synthetic fills
# --------------------------------------------------------------------------------------


def synthetic_fills() -> pd.DataFrame:
    minute = pd.Timestamp("2024-01-01T00:00:00Z")
    rows = [
        ("BTC", "entry_initial_normal_long", 1.0, -0.5, 0.2, "maker", minute),
        ("ETH", "entry_trailing_normal_long", 2.0, -0.4, 0.3, "maker", minute),
        # Slightly over the configured limit: the ledger must still read it as 1.2.
        ("BTC", "entry_trailing_normal_long", 3.0, -0.3, 0.2, "maker", minute + pd.Timedelta(minutes=5)),
        # Same minute, slightly above the configured wallet-exposure limit: the ledger must
        # read the raw value (1.02) and count the row.
        ("BTC", "entry_trailing_normal_long", 3.4, -0.1, 0.2, "maker", minute + pd.Timedelta(minutes=5)),
        ("BTC", "close_trailing_long", 0.0, 1.5, 0.0, "maker", minute + pd.Timedelta(minutes=6)),
        # ETH's entry and close share minute 0, so one coin-minute holds both sides.
        ("ETH", "close_unstuck_long", 0.0, -2.0, 0.0, "maker", minute),
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
                "wallet_exposure": abs(psize) / 100.0,
                "twe_long": 0.3 * abs(psize),
                "twe_short": 0.0,
            }
            for coin, order_type, psize, fee, pnl, liquidity, stamp in rows
        ]
    )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def synthetic_equity(fills: pd.DataFrame) -> pd.DataFrame:
    stamps = pd.date_range("2024-01-01T00:00:00Z", periods=4, freq="2min")
    values = [100.0, 101.0, 99.0, 102.0]
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
        "backtest": {
            "balance_sample_divider": 2,
            "candle_interval_minutes": 1,
            "btc_collateral_cap": 0.0,
            "execution_delay_bars": 0,
            "intrabar_fill_order": "close_first",
            "maker_fee_override": 0.0002,
            "taker_fee_override": 0.0005,
        },
        "bot": {
            "long": {
                "risk": {
                    "total_wallet_exposure_limit": 1.0,
                    "n_positions": 7.0,
                    "we_excess_allowance_pct": 0.37,
                    "position_exposure_enforcer_enabled": False,
                    "total_exposure_enforcer_enabled": False,
                    "total_exposure_entry_gate_enabled": True,
                    "we_excess_allowance_mode": "bounded",
                },
                "strategy": {
                    "trailing_martingale": {
                        "entry": {"double_down_factor": 0.6, "threshold_base_pct": 0.03},
                        "close": {"retracement_base_pct": 0.0005, "threshold_base_pct": 0.0},
                    }
                },
                "unstuck": {
                    "enabled": True,
                    "threshold": 0.466,
                    "close_pct": 0.041,
                    "loss_allowance_pct": 0.0052,
                    "ema_gating_enabled": True,
                    "ema_dist": -0.0269,
                },
                "hsl": {"enabled": False},
            }
        },
    }


def test_reconstruct_positions_tracks_slots_per_side():
    fills = synthetic_fills().sort_values("timestamp").reset_index(drop=True)
    positions = report_tool.reconstruct_positions(fills)
    # entry, entry, close ETH, entry, extra entry, close BTC (sorted by timestamp)
    assert positions["nonzero_long_after_fill"].tolist() == [1, 2, 1, 1, 1, 0]


def test_reconstruct_positions_rejects_an_unknown_order_type():
    fills = synthetic_fills()
    fills.loc[0, "type"] = "entry_mystery"
    with pytest.raises(ValueError):
        report_tool.reconstruct_positions(fills)


def test_ledger_facts_report_measured_values():
    fills = synthetic_fills().sort_values("timestamp").reset_index(drop=True)
    equity = synthetic_equity(fills)
    ledger = report_tool.ledger_facts(fills, equity, synthetic_config())
    assert ledger["nonzero_long_max"] == 2
    assert ledger["max_twe_long"] == pytest.approx(0.3 * 3.4)
    assert ledger["rows_twe_long_above_limit"] == 1
    # Minute 0 holds three fills (BTC entry, ETH entry, ETH close); minute 5 holds two.
    assert ledger["minutes_with_multiple_fills"] == 2
    assert ledger["max_fills_in_a_minute"] == 3
    assert ledger["coin_minutes_with_entry_and_close"] == 1
    assert ledger["unknown_entry_types"] == []
    assert ledger["unknown_close_types"] == []
    assert ledger["max_abs_wallet_exposure"] == pytest.approx(0.034)
    assert ledger["single_coin_cap"] == pytest.approx(1.0 / 7.0 * 1.37)
    assert ledger["strategy_equity_equals_usd_total_equity"] is True


def test_audit_facts_ignore_a_missing_file(tmp_path):
    facts = report_tool.audit_facts(tmp_path / "absent.csv", 0)
    assert facts["present"] is False


def test_audit_facts_count_violations(tmp_path):
    path = tmp_path / "execution_audit.csv"
    pd.DataFrame(
        [
            {"decision_index": 0, "activation_index": 1, "fill_index": 1},
            {"decision_index": 5, "activation_index": 6, "fill_index": 9},
        ]
    ).to_csv(path, index=False)
    facts = report_tool.audit_facts(path, 0)
    assert facts["present"] is True
    assert facts["rows"] == 2
    assert facts["activation_identity_failures"] == 0
    assert facts["fill_before_activation_failures"] == 0
    assert facts["median_waited_bars"] == pytest.approx(1.5)
    assert facts["max_waited_bars"] == 3

    broken = pd.DataFrame([{"decision_index": 0, "activation_index": 2, "fill_index": 1}])
    broken.to_csv(path, index=False)
    facts = report_tool.audit_facts(path, 0)
    assert facts["activation_identity_failures"] == 1
    assert facts["fill_before_activation_failures"] == 1


# --------------------------------------------------------------------------------------
# Verifier catches tampering
# --------------------------------------------------------------------------------------


def test_compare_frames_rejects_a_tampered_cell():
    expected = pd.DataFrame({"coin": ["BTC", "ETH"], "net_realized_pnl_usd": [10.0, -1.0]})
    actual = expected.copy()
    assert verifier.compare_frames(expected, actual, ["coin", "net_realized_pnl_usd"], 1e-6) == []
    actual.loc[1, "net_realized_pnl_usd"] = -1.5
    problems = verifier.compare_frames(expected, actual, ["coin", "net_realized_pnl_usd"], 1e-6)
    assert problems and "net_realized_pnl_usd" in problems[0]
    assert verifier.compare_frames(expected, actual.iloc[:1], ["coin"], 1e-6)


def test_markdown_table_helpers_read_the_report_shape():
    text = (
        "## 总体结果\n\n"
        "| 项目 | 数值 |\n| --- | --- |\n| 成交数 / 强平 | 10 / 否 |\n\n"
        "## 月度汇总\n\n| 月份 | 覆盖 |\n| --- | --- |\n| 2024-01 | 完整 |\n"
    )
    tables = verifier.parse_markdown_tables(text)
    assert verifier.table_by_header(tables, ["项目", "数值"]) == [["成交数 / 强平", "10 / 否"]]
    assert verifier.table_by_header(tables, ["月份", "覆盖"]) == [["2024-01", "完整"]]
    assert verifier.table_by_header(tables, ["没有", "这张表"]) is None


def test_logical_array_hash_matches_the_bundle_convention():
    import hashlib

    import numpy as np

    array = np.arange(12, dtype=np.float32).reshape(3, 4)
    hasher = hashlib.sha256()
    hasher.update(str(array.dtype).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(json.dumps([3, 4], separators=(",", ":")).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(np.ascontiguousarray(array).data)
    assert verifier.logical_array_hash(array) == hasher.hexdigest()


# --------------------------------------------------------------------------------------
# End-to-end (artifact present)
# --------------------------------------------------------------------------------------


def test_replay_artifact_passes_its_verifier():
    result_dir = _artifact_run_dir()
    if result_dir is None:
        pytest.skip("no replay artifact on disk")
    completed = subprocess.run(
        [sys.executable, str(TOOLS / "verify_replay_report.py")],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    assert "failed: 0" in completed.stdout


def test_replay_report_matches_the_convention():
    result_dir = _artifact_run_dir()
    if result_dir is None:
        pytest.skip("no replay artifact on disk")
    report = (result_dir / "annual_analysis.md").read_text(encoding="utf-8")
    assert spec.assert_report_structure(report) == []
    annual = pd.read_csv(result_dir / "annual_metrics.csv")
    assert list(annual.columns) == list(spec.PERIOD_CSV_COLUMNS)
    details = spec.detail_headings(report)
    assert len(details) == len(annual)
    for heading, (_, row) in zip(details, annual.iterrows()):
        assert heading == f"### {row['period']} 年明细（{row['coverage']}）"
