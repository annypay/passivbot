"""Offline checks for the g4 @ TWE 3.0 account-guard study.

The study's claims are: every arm keeps the previous study's two declared production changes
(10,000 USDT, TWE 3.0) and adds an account-level guard expressed through HSL (`unified`,
threshold, peak window, EMA speed, halt length, terminal floor); the guard tiers mean what the
report says (ORANGE stops entries, RED flattens and halts); the halt table is derived from the
arm's own ledger rather than assumed (a halt is a flat stretch pinned by a panic fill, and a
halt with no fill after it is a *terminal* halt); and the synthesis restates the arms' numbers.
These tests pin the frozen artifacts and exercise the derived maths on synthetic inputs. No
backtest runs here, nothing touches the network, and checks that need local artifacts skip when
absent.

The tool modules are loaded through `importlib` under study-specific names: every study under
`backtests/**/report_tools/` names its registry `variant_spec.py`, so a plain `import
variant_spec` in a pytest session binds whichever study's module was imported first.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "backtests/binance/g4_twe300_account_guard_2026-09-17"
TOOLS = STUDY / "report_tools"


def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec = _load_tool("guard_variant_spec", "variant_spec.py")
events = _load_tool("guard_event_windows", "event_windows.py")


def _load_dependent_tool(name: str, filename: str):
    """Load a tool whose module body does `import variant_spec as study` without leaking it."""
    previous = sys.modules.get("variant_spec")
    previous_path = list(sys.path)
    sys.path.insert(0, str(TOOLS))
    sys.modules["variant_spec"] = spec
    try:
        module = _load_tool(name, filename)
        events_module = sys.modules.get("guard_event_windows")
        if events_module is not None:
            sys.modules.setdefault("event_windows", events_module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("variant_spec", None)
        else:
            sys.modules["variant_spec"] = previous
        sys.path[:] = previous_path


guard = _load_dependent_tool("guard_guard_analysis", "guard_analysis.py")
wipeout = _load_tool("guard_wipeout_matrix", "wipeout_matrix.py")


def load(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------------------
# Frozen inputs and arm declarations
# --------------------------------------------------------------------------------------


def test_pinned_parent_and_control_artifacts_still_match():
    assert spec.sha256_file(spec.SOURCE_CONFIG) == spec.SOURCE_CONFIG_SHA256
    assert spec.sha256_file(spec.SOURCE_PROFILE_INPUT) == spec.SOURCE_PROFILE_INPUT_SHA256
    for key, item in spec.REFERENCE_RUNS.items():
        path = Path(item["run_dir"]) / "analysis.json"
        if not path.exists():
            pytest.skip(f"control arm {key} is not present locally")
        assert spec.sha256_file(path) == item["analysis_sha256"], key


def test_variant_input_is_frozen_and_covers_every_arm():
    payload = load(spec.VARIANT_INPUT_PATH)
    assert payload["parent_config_sha256"] == spec.SOURCE_CONFIG_SHA256
    assert payload["run_order"] == list(spec.RUN_VARIANT_ORDER)
    assert {item["key"] for item in payload["arms"]} == set(spec.DEFAULT_VARIANT_ORDER)
    assert {item["id"] for item in payload["decision_rules"]} == {"J1", "J2", "J3", "J4", "J5"}
    assert len(payload["control_arms"]) == 2
    guard_contract = payload["guard_contract"]
    assert guard_contract["lookback_days"] == 7.0
    assert guard_contract["lookback_path"] == "live.pnls_max_lookback_days"
    assert "orange" in guard_contract["tier_semantics"]
    mapping = {row["request"]: row for row in guard_contract["mapping_table"]}
    assert "账户级" in mapping and "一周内" in mapping and "浮亏 20%" in mapping


def test_every_arm_keeps_the_previous_study_changes_and_adds_a_guard():
    parent = load(spec.SOURCE_CONFIG)
    for variant in spec.VARIANTS:
        frozen = load(variant.config_path)
        assert frozen["backtest"]["starting_balance"] == 10000, variant.key
        assert frozen["bot"]["long"]["risk"]["total_wallet_exposure_limit"] == 3.0, variant.key
        problems = spec.diff_subtrees(
            parent["backtest"],
            frozen["backtest"],
            "backtest",
            allowed=spec.allowed_backtest_paths(variant),
        )
        declared_paths: dict[str, tuple[str, ...]] = {}
        for dotted, _from, _to in variant.deltas:
            root = dotted.split(".")[0]
            declared_paths[root] = (*declared_paths.get(root, ()), dotted)
        for root in spec.IDENTITY_ROOTS:
            problems.extend(
                spec.diff_subtrees(
                    parent.get(root), frozen.get(root), root, allowed=declared_paths.get(root, ())
                )
            )
        for dotted, _from, to in variant.deltas:
            assert spec.get_path(frozen, dotted) == to, f"{variant.key}: {dotted}"
        assert problems == [], f"{variant.key}: {problems}"


def test_guard_parameters_are_the_requested_shape():
    params = spec.VARIANTS_BY_KEY["g_user12h__3y"].guard_params
    assert params["enabled"] is True
    assert params["scope"] == "unified"
    assert params["red_threshold"] == pytest.approx(0.20)
    assert params["cooldown_minutes_after_red"] == pytest.approx(720.0)
    assert params["lookback_days"] == pytest.approx(7.0)
    assert params["ema_span_minutes"] == pytest.approx(60.0)
    assert params["no_restart_drawdown_threshold"] == pytest.approx(1.0)
    # The engine's hard constraint, asserted for every arm so a bad config fails here first.
    for variant in spec.VARIANTS:
        arm = variant.guard_params
        if not arm["enabled"]:
            continue
        assert arm["red_threshold"] <= arm["no_restart_drawdown_threshold"] <= 1.0, variant.key
        assert 0.0 < arm["orange_threshold"] < arm["red_threshold"], variant.key
        assert 0.0 < arm["yellow_ratio"] < arm["orange_ratio"] < 1.0, variant.key
        assert arm["ema_span_minutes"] > 0.0, variant.key
        assert 0.0 < arm["lookback_days"] <= spec.PARENT_PNLS_LOOKBACK_DAYS, variant.key


def test_the_guard_arms_cover_the_designed_alternatives():
    assert spec.VARIANTS_BY_KEY["g_user24h__3y"].guard_params[
        "cooldown_minutes_after_red"
    ] == pytest.approx(1440.0)
    assert spec.VARIANTS_BY_KEY["g_ladder40__3y"].guard_params[
        "no_restart_drawdown_threshold"
    ] == pytest.approx(0.40)
    soft = spec.VARIANTS_BY_KEY["g_soft_orange__3y"].guard_params
    assert soft["orange_threshold"] == pytest.approx(0.20, abs=1e-3)
    assert soft["red_threshold"] == pytest.approx(0.35)
    orange_only = spec.VARIANTS_BY_KEY["g_orange_only__3y"].guard_params
    assert orange_only["orange_threshold"] == pytest.approx(0.20, abs=1e-3)
    assert orange_only["red_threshold"] == pytest.approx(0.60)
    slow = spec.VARIANTS_BY_KEY["g_slow_ema__3y"].guard_params
    assert slow["ema_span_minutes"] == pytest.approx(720.0)


def test_guard_label_is_human_readable():
    label = spec.VARIANTS_BY_KEY["g_soft_orange__ext"].guard_label
    assert "0.35" in label and "0.20" in label and "24H" in label and "7 天" in label


# --------------------------------------------------------------------------------------
# Halt-window derivation
# --------------------------------------------------------------------------------------


def synthetic_ledger(
    *, panic: bool, flat_hours: int = 12, resume_up: bool = True, trade_after: bool = True
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """An equity series with one flat stretch, optionally explained by a panic close."""
    index = pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC")
    values = np.concatenate(
        [
            np.linspace(10_000.0, 8_000.0, 12),
            np.full(flat_hours, 8_000.0),
            (
                np.linspace(8_001.0, 9_000.0, 48 - 12 - flat_hours)
                if resume_up
                else np.linspace(8_001.0, 7_000.0, 48 - 12 - flat_hours)
            ),
        ]
    )
    equity = pd.DataFrame({"usd_total_equity": values}, index=index)
    rows = [
        {
            "timestamp": index[0],
            "coin": "AAA",
            "wallet_exposure": 0.5,
            "twe_long": 2.0,
            "usd_total_balance": 10_000.0,
            "pnl": 0.0,
            "fee_paid": 0.0,
            "type": "entry_initial_normal_long",
        },
        {
            "timestamp": index[11],
            "coin": "AAA",
            "wallet_exposure": 0.0,
            "twe_long": 0.0,
            "usd_total_balance": 8_000.0,
            "pnl": -2_000.0,
            "fee_paid": -1.0,
            "type": "close_panic_long" if panic else "close_grid_long",
        },
    ]
    if trade_after:
        rows.append(
            {
                "timestamp": index[12 + flat_hours],
                "coin": "AAA",
                "wallet_exposure": 0.4,
                "twe_long": 1.6,
                "usd_total_balance": 8_000.0,
                "pnl": 0.0,
                "fee_paid": -0.5,
                "type": "entry_initial_normal_long",
            }
        )
    return equity, pd.DataFrame(rows)


def synthetic_terminal_ledger() -> tuple[pd.DataFrame, pd.DataFrame]:
    """A panic close with no fill afterwards: the latched halt never restarts."""
    index = pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC")
    values = np.concatenate([np.linspace(10_000.0, 8_000.0, 12), np.full(36, 8_000.0)])
    equity = pd.DataFrame({"usd_total_equity": values}, index=index)
    fills = pd.DataFrame(
        [
            {
                "timestamp": index[0],
                "coin": "AAA",
                "wallet_exposure": 0.5,
                "twe_long": 2.0,
                "usd_total_balance": 10_000.0,
                "pnl": 0.0,
                "fee_paid": 0.0,
                "type": "entry_initial_normal_long",
            },
            {
                "timestamp": index[11],
                "coin": "AAA",
                "wallet_exposure": 0.0,
                "twe_long": 0.0,
                "usd_total_balance": 8_000.0,
                "pnl": -2_000.0,
                "fee_paid": -1.0,
                "type": "close_panic_long",
            },
        ]
    )
    return equity, fills


def synthetic_idle_after_panic_ledger() -> tuple[pd.DataFrame, pd.DataFrame]:
    """A real halt early on, then an unrelated flat stretch much later in the same run."""
    index = pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC")
    values = np.concatenate(
        [
            np.linspace(10_000.0, 8_000.0, 12),
            np.full(12, 8_000.0),
            np.linspace(8_001.0, 8_500.0, 6),
            np.full(12, 8_500.0),
            np.linspace(8_501.0, 8_900.0, 6),
        ]
    )
    equity = pd.DataFrame({"usd_total_equity": values}, index=index)
    fills = pd.DataFrame(
        [
            {"timestamp": index[0], "coin": "AAA", "pnl": 0.0, "fee_paid": 0.0,
             "wallet_exposure": 0.5, "twe_long": 2.0, "usd_total_balance": 10_000.0,
             "type": "entry_initial_normal_long"},
            {"timestamp": index[11], "coin": "AAA", "pnl": -2_000.0, "fee_paid": -1.0,
             "wallet_exposure": 0.0, "twe_long": 0.0, "usd_total_balance": 8_000.0,
             "type": "close_panic_long"},
            {"timestamp": index[24], "coin": "AAA", "pnl": 0.0, "fee_paid": -0.5,
             "wallet_exposure": 0.4, "twe_long": 1.6, "usd_total_balance": 8_001.0,
             "type": "entry_initial_normal_long"},
            {"timestamp": index[30], "coin": "AAA", "pnl": 0.0, "fee_paid": -0.5,
             "wallet_exposure": 0.0, "twe_long": 0.0, "usd_total_balance": 8_500.0,
             "type": "close_grid_long"},
            {"timestamp": index[42], "coin": "AAA", "pnl": 0.0, "fee_paid": -0.5,
             "wallet_exposure": 0.4, "twe_long": 1.6, "usd_total_balance": 8_501.0,
             "type": "entry_initial_normal_long"},
        ]
    )
    return equity, fills


def test_flat_run_inventory_sees_every_long_flat_stretch():
    equity, _fills = synthetic_ledger(panic=True, flat_hours=12)
    runs = guard.flat_runs(equity, min_flat_minutes=360)
    assert len(runs) == 1
    assert guard.flat_run_inventory(equity)["count"] == 1


def test_only_panic_explained_flat_stretches_count_as_halts():
    equity, fills = synthetic_ledger(panic=True, flat_hours=12)
    halts = guard.halt_windows(equity, fills)
    assert len(halts) == 1
    # The declining segment ends exactly at the flat value, so the flat run covers index 11..23
    # (12 hourly gaps) — the same one-sample slack the real arms show on hourly sampling.
    assert halts[0].minutes == pytest.approx(720.0)
    assert halts[0].post_halt_return_30d is not None and halts[0].post_halt_return_30d > 0

    equity_no_panic, fills_no_panic = synthetic_ledger(panic=False, flat_hours=12)
    assert guard.halt_windows(equity_no_panic, fills_no_panic) == []


def test_a_take_profit_exit_is_not_a_halt():
    equity, fills = synthetic_ledger(panic=False, flat_hours=12)
    halts = guard.halt_windows(equity, fills)
    assert halts == []
    assert guard.flat_run_inventory(equity)["count"] == 1


def test_halt_readiness_cross_checks_engine_telemetry():
    equity, fills = synthetic_ledger(panic=True, flat_hours=12)
    halts = guard.halt_windows(equity, fills, declared_halt_minutes=720.0)
    analysis = {
        "hard_stop_triggers": 1,
        "hard_stop_restarts": 1,
        "hard_stop_duration_minutes_mean": 720.0,
        "hard_stop_duration_minutes_max": 720.0,
        "hard_stop_time_in_red_pct": 720.0 / (47 * 60),
    }
    summary = guard.readiness_summary(
        halts, equity, analysis, declared={"cooldown_minutes_after_red": 720.0}
    )
    assert summary["halt_count"] == 1
    assert summary["cross_check_problems"] == []
    # The window is measured on the hourly equity grid, so "one sample short" is expected; a
    # window that disagrees by more than two samples, a window count the engine does not report,
    # or a halt time the engine never spent in RED are the shapes that must fail here.
    grid_slack = guard.readiness_summary(
        halts, equity, {**analysis, "hard_stop_duration_minutes_max": 720.0},
        declared={"cooldown_minutes_after_red": 720.0},
    )
    assert grid_slack["cross_check_problems"] == []
    count_mismatch = guard.readiness_summary(
        halts, equity, {**analysis, "hard_stop_triggers": 2, "hard_stop_restarts": 2},
        declared={"cooldown_minutes_after_red": 720.0},
    )
    assert count_mismatch["cross_check_problems"]
    short_halt = guard.readiness_summary(
        halts,
        equity,
        {**analysis, "hard_stop_duration_minutes_max": 1440.0, "hard_stop_time_in_red_pct": 1440.0 / (47 * 60)},
        declared={"cooldown_minutes_after_red": 1440.0},
    )
    assert short_halt["cross_check_problems"]


def test_a_terminal_halt_is_derived_from_the_last_panic_to_the_end_of_the_run():
    equity, fills = synthetic_terminal_ledger()
    halts = guard.halt_windows(
        equity, fills, declared_halt_minutes=720.0, terminal_halt_minutes=2160.0
    )
    assert len(halts) == 1
    halt = halts[0]
    assert halt.terminal is True
    assert halt.resume_equity_usd is None
    assert halt.post_halt_return_7d is None and halt.post_halt_return_30d is None
    # The flat stretch starts at index 11 and runs to the end of the series: 36 hourly gaps.
    assert halt.minutes == pytest.approx(2160.0)
    assert halt.declared_minutes == pytest.approx(2160.0)
    assert halt.excess_minutes == pytest.approx(0.0)
    summary = guard.readiness_summary(
        halts,
        equity,
        {
            "hard_stop_triggers": 1,
            "hard_stop_restarts": 0,
            "hard_stop_duration_minutes_max": 2160.0,
            "hard_stop_time_in_red_pct": 2160.0 / (47 * 60),
        },
        declared={"cooldown_minutes_after_red": 720.0},
    )
    assert summary["terminal_halt_count"] == 1
    assert summary["cross_check_problems"] == []
    assert summary["cross_check_notes"]
    # A terminal window with restarts reported by the engine is a contradiction.
    contradicted = guard.readiness_summary(
        halts,
        equity,
        {"hard_stop_triggers": 1, "hard_stop_restarts": 1, "hard_stop_duration_minutes_max": 720.0},
        declared={"cooldown_minutes_after_red": 720.0},
    )
    assert contradicted["cross_check_problems"]


def test_a_flat_stretch_long_after_a_panic_is_not_a_halt():
    """Regression: a 24-hour panic link used to count ordinary idle stretches as halts."""
    equity, fills = synthetic_idle_after_panic_ledger()
    assert guard.flat_run_inventory(equity)["count"] == 2
    halts = guard.halt_windows(equity, fills)
    assert len(halts) == 1
    assert halts[0].minutes == pytest.approx(720.0)


def test_idle_time_after_the_declared_cooldown_is_a_note_not_a_problem():
    equity, fills = synthetic_ledger(panic=True, flat_hours=24)
    halts = guard.halt_windows(equity, fills, declared_halt_minutes=720.0)
    assert halts[0].minutes == pytest.approx(1440.0)
    assert halts[0].excess_minutes == pytest.approx(720.0)
    assert halts[0].shortfall_minutes == pytest.approx(0.0)
    summary = guard.readiness_summary(
        halts,
        equity,
        {
            "hard_stop_triggers": 1,
            "hard_stop_restarts": 1,
            "hard_stop_duration_minutes_max": 720.0,
            "hard_stop_time_in_red_pct": 720.0 / (47 * 60),
        },
        declared={"cooldown_minutes_after_red": 720.0},
    )
    assert summary["cross_check_problems"] == []
    assert summary["idle_after_halt_minutes_max"] == pytest.approx(720.0)
    assert summary["cross_check_notes"]


def test_losing_after_resume_is_reported_as_a_negative_reading():
    equity, fills = synthetic_ledger(panic=True, flat_hours=12, resume_up=False)
    halts = guard.halt_windows(equity, fills)
    assert halts and halts[0].post_halt_return_7d is not None
    assert halts[0].post_halt_return_7d < 0


# --------------------------------------------------------------------------------------
# Wipe-out matrix on the guarded arms (declared TWE 3.0)
# --------------------------------------------------------------------------------------


def test_matrix_accepts_a_three_times_exposure_peak():
    fills = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
                "coin": "AAA",
                "wallet_exposure": 0.58,
                "twe_long": 2.9,
                "usd_total_balance": 10_000.0,
                "pnl": 0.0,
                "fee_paid": 0.0,
                "type": "entry_initial_normal_long",
            }
        ]
    )
    matrix = wipeout.build_matrix(
        "test",
        analysis={"total_wallet_exposure_max": 2.9},
        per_coin=wipeout.per_coin_max_exposure(fills),
        fills=fills,
        shock_levels=spec.SHOCK_LEVELS,
        declared_twe=3.0,
        start_balance_usd=10_000.0,
        starting_balance_usd=10_000.0,
        liquidation_threshold=0.05,
    )
    assert wipeout.assert_identities(matrix) == []
    assert matrix.liquidation_shock == pytest.approx(0.95 / 2.9, rel=1e-9)


# --------------------------------------------------------------------------------------
# Rendered arms (only when the local artifacts exist)
# --------------------------------------------------------------------------------------


def rendered_arms() -> list[spec.Variant]:
    found = []
    for key in spec.RUN_VARIANT_ORDER:
        variant = spec.VARIANTS_BY_KEY[key]
        try:
            spec.find_variant_run_dir(variant)
        except SystemExit:
            continue
        found.append(variant)
    return found


def test_every_rendered_arm_matches_the_report_convention():
    sys.path.insert(0, str(REPO / "backtests/report_spec"))
    import annual_analysis as convention

    arms = rendered_arms()
    if not arms:
        pytest.skip("no rendered arm bundle is present locally")
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        report = (run_dir / "annual_analysis.md").read_text(encoding="utf-8")
        assert convention.assert_report_structure(report) == [], variant.key
        assert "账户守护读数" in report and "整装待发读数" in report


def test_tracked_evidence_carries_no_host_paths():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no run bundle is present locally")
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        for name in (
            "run_record.json",
            "global_metrics.json",
            "guard_readiness.json",
            "annual_analysis.md",
        ):
            text = (run_dir / name).read_text(encoding="utf-8")
            assert "/home/" not in text, f"{variant.key}/{name}"
            assert "C:\\" not in text, f"{variant.key}/{name}"


def test_recorded_guard_telemetry_matches_the_declared_arm():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no run bundle is present locally")
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        payload = load(run_dir / "guard_readiness.json")
        recorded = payload["guard"]
        declared = variant.guard_params
        for key, expected in declared.items():
            assert key in recorded, f"{variant.key}: {key}"
            if isinstance(expected, str):
                assert recorded[key] == expected, f"{variant.key}: {key}"
            else:
                assert float(recorded[key]) == pytest.approx(float(expected)), f"{variant.key}: {key}"
        analysis = load(run_dir / "analysis.json")
        recomputed = guard.build_guard_artifact(run_dir, analysis)
        assert payload["halt_count"] == recomputed["halt_count"], variant.key
        assert payload["cross_check_problems"] == [], variant.key


def test_rendered_guard_artifact_echoes_the_frozen_config():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no rendered arm bundle is present locally")
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        payload = load(run_dir / "guard_readiness.json")
        declared = payload["declared_guard"]
        assert declared["source"] == "config.json", variant.key
        assert float(declared["cooldown_minutes_after_red"]) == pytest.approx(
            float(variant.guard_params["cooldown_minutes_after_red"])
        ), variant.key
        assert declared["hsl_signal_mode"] == variant.guard_params["scope"], variant.key
        assert float(declared["pnls_max_lookback_days"]) == pytest.approx(
            float(variant.guard_params["lookback_days"])
        ), variant.key


def test_every_terminal_halt_is_recorded_as_one():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no rendered arm bundle is present locally")
    terminal_arms = set()
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        payload = load(run_dir / "guard_readiness.json")
        telemetry = payload["telemetry"]
        restarts = float(telemetry.get("hard_stop_restarts") or 0.0)
        triggers = float(telemetry.get("hard_stop_triggers") or 0.0)
        if restarts < triggers:
            terminal_arms.add(variant.key)
            assert payload["terminal_halt_count"] == triggers - restarts, variant.key
        else:
            assert payload["terminal_halt_count"] == 0, variant.key
        for halt in payload["halts"]:
            if halt["terminal"]:
                assert halt["resume_equity_usd"] is None, variant.key
                assert halt["post_halt_return_30d"] is None, variant.key
                assert abs(
                    float(halt["minutes"]) - float(telemetry["hard_stop_duration_minutes_max"])
                ) <= 120.0, variant.key
            else:
                assert float(halt["minutes"]) + 120.0 >= float(halt["declared_minutes"]), variant.key
    assert "g_ladder40__ext" in terminal_arms or "g_ladder40__ext" not in {
        variant.key for variant in arms
    }


def test_synthesis_restates_the_arms_numbers():
    summary_path = spec.ARTIFACTS / "account_guard_summary.json"
    synthesis_path = spec.STUDY / "account_guard_analysis.md"
    if not summary_path.exists() or not synthesis_path.exists():
        pytest.skip("the synthesis has not been built locally")
    summary = load(summary_path)
    text = synthesis_path.read_text(encoding="utf-8")
    assert "## 八、判据裁决" in text
    assert "终局停机" in text and "逐次停机明细" in text
    for key, entry in (summary.get("arms") or {}).items():
        variant = spec.VARIANTS_BY_KEY.get(key)
        if variant is None:
            continue
        run_dir = spec.find_variant_run_dir(variant)
        analysis = load(run_dir / "analysis.json")
        assert entry["gain_strategy_eq"] == pytest.approx(
            float(analysis["gain_strategy_eq"]), rel=1e-12
        )
        assert bool(entry["liquidated"]) == bool(analysis["liquidated"])
        assert entry["guard"] == variant.guard_params
        readiness_path = run_dir / "guard_readiness.json"
        if readiness_path.exists():
            readiness = load(readiness_path)
            assert entry["terminal_halt_count"] == readiness["terminal_halt_count"]
            assert entry["complete_halt_count"] == readiness["complete_halt_count"]
            assert entry["guard_cross_check_problems"] == []
            for halt in readiness["halts"]:
                assert str(halt["start"])[:16] in text, f"{key}: {halt['start']}"
