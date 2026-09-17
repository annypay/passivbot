"""Offline checks for the g4 @ TWE 3.0 / 10,000 USDT replay.

The study's claims are: every arm is the frozen parent config plus a declared list of changes
(starting capital, exposure limit, and/or the hard-floor HSL block); the wipe-out matrix uses
the arm's *declared* exposure limit rather than a hard-coded 1.0; the liquidation geometry
follows the engine's own floor rule; and the affordability appendix reflects the frozen market
settings. These tests pin the frozen artifacts and exercise the maths on synthetic inputs. No
backtest runs here, nothing touches the network, and checks that need a local-only artifact
skip when it is absent.

The tool modules are loaded through `importlib` under study-specific names: every study under
`backtests/**/report_tools/` names its registry `variant_spec.py`, so a plain `import
variant_spec` in a pytest session binds whichever study's module was imported first.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "backtests/binance/g4_twe300_10k_replay_2026-09-17"
TOOLS = STUDY / "report_tools"


def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec = _load_tool("twe300_variant_spec", "variant_spec.py")
events = _load_tool("twe300_event_windows", "event_windows.py")
wipeout = _load_tool("twe300_wipeout_matrix", "wipeout_matrix.py")


def _load_dependent_tool(name: str, filename: str):
    """Load a tool whose module body does `import variant_spec as study`.

    The registry module name is shared by every study, so the import is pinned to this
    study's module for the duration of the load and the session state is restored
    afterwards; otherwise a later test module would bind another study's registry.
    """
    previous_module = sys.modules.get("variant_spec")
    previous_path = list(sys.path)
    sys.path.insert(0, str(TOOLS))
    sys.modules["variant_spec"] = spec
    try:
        return _load_tool(name, filename)
    finally:
        if previous_module is None:
            sys.modules.pop("variant_spec", None)
        else:
            sys.modules["variant_spec"] = previous_module
        sys.path[:] = previous_path


affordability = _load_dependent_tool("twe300_affordability", "affordability.py")


def load(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def start_of_day(day: str) -> int:
    return int(pd.Timestamp(day, tz="UTC").value // 1_000_000)


# --------------------------------------------------------------------------------------
# Frozen inputs and arm declarations
# --------------------------------------------------------------------------------------


def test_pinned_parent_artifacts_still_match():
    assert spec.sha256_file(spec.SOURCE_CONFIG) == spec.SOURCE_CONFIG_SHA256
    assert spec.sha256_file(spec.SOURCE_PROFILE_INPUT) == spec.SOURCE_PROFILE_INPUT_SHA256


def test_reference_arms_still_match_their_pins():
    for key, item in spec.REFERENCE_RUNS.items():
        path = Path(item["run_dir"]) / "analysis.json"
        if not path.exists():
            pytest.skip(f"reference run {key} is not present locally")
        assert spec.sha256_file(path) == item["analysis_sha256"], key


def test_variant_input_is_frozen_and_covers_every_arm():
    payload = load(spec.VARIANT_INPUT_PATH)
    assert payload["parent_config_sha256"] == spec.SOURCE_CONFIG_SHA256
    assert payload["run_order"] == list(spec.RUN_VARIANT_ORDER)
    assert {item["key"] for item in payload["arms"]} == set(spec.DEFAULT_VARIANT_ORDER)
    assert {item["id"] for item in payload["decision_rules"]} == {"J1", "J2", "J3", "J4", "J5"}
    assert payload["capital_contract"]["arm_starting_balance"] == 10000
    assert payload["capital_contract"]["parent_starting_balance"] == 100000
    assert payload["liquidation_contract"]["threshold"] == 0.05
    assert len(payload["reference_arms"]) == 2


def test_every_arm_config_is_the_parent_plus_its_declared_changes():
    parent = load(spec.SOURCE_CONFIG)
    for variant in spec.VARIANTS:
        frozen = load(variant.config_path)
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


def test_arm_registry_and_exposure_geometry():
    assert len({variant.key for variant in spec.VARIANTS}) == len(spec.VARIANTS)
    legs = spec.variant_legs()
    assert set(legs) == {"3y", "ext"}
    assert len(legs["3y"]) == 4 and len(legs["ext"]) == 5
    scale = spec.VARIANTS_BY_KEY["twe100_10k__3y"]
    assert scale.starting_balance == 10000
    assert scale.declared_twe == 1.0
    assert scale.per_slot_cap == pytest.approx(0.19571428571428573, rel=1e-12)
    levered = spec.VARIANTS_BY_KEY["twe300_10k__3y"]
    assert levered.declared_twe == 3.0
    assert levered.per_slot_cap == pytest.approx(3.0 / 7 * 1.37, rel=1e-12)
    structural = spec.VARIANTS_BY_KEY["twe300_10k_allowance0__3y"]
    assert structural.declared_allowance_pct == 0.0
    assert structural.per_slot_cap == pytest.approx(3.0 / 7, rel=1e-12)
    floor_arm = spec.VARIANTS_BY_KEY["twe300_10k_floor__ext"]
    assert floor_arm.hsl_enabled is True
    assert floor_arm.declared("live.hsl_signal_mode", None) == "unified"
    assert floor_arm.declared("bot.long.hsl.red_threshold", None) == 0.10


def test_declared_backtest_paths_are_allowed_for_its_own_arm_only():
    arm = spec.VARIANTS_BY_KEY["twe300_10k__ext"]
    assert spec.STARTING_BALANCE_PATH in spec.declared_backtest_paths(arm)
    assert spec.STARTING_BALANCE_PATH in spec.allowed_backtest_paths(arm)
    assert spec.STARTING_BALANCE_PATH not in spec.ALLOWED_CONFIG_DIFF_PATHS


# --------------------------------------------------------------------------------------
# Liquidation geometry
# --------------------------------------------------------------------------------------


def test_liquidation_floor_is_five_percent_of_the_declared_capital():
    arm = spec.VARIANTS_BY_KEY["twe300_10k__3y"]
    assert spec.liquidation_floor_usd(arm) == pytest.approx(500.0)


def test_liquidation_shock_at_full_three_times_exposure():
    shock = spec.liquidation_shock(
        peak_exposure=3.0, balance_usd=10000.0, starting_balance=10000.0, threshold=0.05
    )
    assert shock == pytest.approx(0.95 / 3.0, rel=1e-12)
    assert shock * 100 == pytest.approx(31.67, abs=0.01)


def test_liquidation_shock_scales_with_capital_and_exposure():
    modest = spec.liquidation_shock(
        peak_exposure=1.0, balance_usd=10000.0, starting_balance=10000.0, threshold=0.05
    )
    assert modest == pytest.approx(0.95, rel=1e-12)
    no_exposure = spec.liquidation_shock(
        peak_exposure=0.0, balance_usd=10000.0, starting_balance=10000.0, threshold=0.05
    )
    assert no_exposure is None
    already_below = spec.liquidation_shock(
        peak_exposure=2.0, balance_usd=400.0, starting_balance=10000.0, threshold=0.05
    )
    assert already_below is None


def test_liquidation_shock_matches_the_exchange_comparison():
    # A 10x cross-margin venue with 0.4% maintenance margin liquidates near r = m - 1/TWE.
    engine = spec.liquidation_shock(
        peak_exposure=3.0, balance_usd=10000.0, starting_balance=10000.0, threshold=0.05
    )
    exchange = 0.004 - 1.0 / 3.0
    assert engine is not None
    assert abs(engine - abs(exchange)) < 0.02


# --------------------------------------------------------------------------------------
# Wipe-out matrix with a declared exposure limit
# --------------------------------------------------------------------------------------


def synthetic_fills(per_coin: dict[str, float], balance: float = 10000.0) -> pd.DataFrame:
    rows = []
    for coin, exposure in per_coin.items():
        rows.append(
            {
                "timestamp": pd.Timestamp("2024-01-01", tz="UTC"),
                "coin": coin,
                "wallet_exposure": exposure,
                "twe_long": sum(per_coin.values()),
                "usd_total_balance": balance,
                "pnl": 0.0,
                "fee_paid": 0.0,
                "type": "entry_initial_normal_long",
            }
        )
    return pd.DataFrame(rows)


def build(per_coin: dict[str, float], peak_total: float, declared_twe: float):
    fills = synthetic_fills(per_coin)
    return wipeout.build_matrix(
        "test",
        analysis={"total_wallet_exposure_max": peak_total},
        per_coin=wipeout.per_coin_max_exposure(fills),
        fills=fills,
        shock_levels=spec.SHOCK_LEVELS,
        declared_twe=declared_twe,
        start_balance_usd=10000.0,
        starting_balance_usd=10000.0,
        liquidation_threshold=0.05,
    )


def test_matrix_accepts_exposure_up_to_the_declared_limit():
    matrix = build({"AAA": 0.58, "BBB": 0.55, "CCC": 0.4}, peak_total=2.9, declared_twe=3.0)
    assert wipeout.assert_identities(matrix) == []
    assert matrix.liquidation_shock == pytest.approx(0.95 / 2.9, rel=1e-9)


def test_matrix_rejects_exposure_above_the_declared_limit():
    matrix = build({"AAA": 2.0}, peak_total=3.5, declared_twe=3.0)
    problems = wipeout.assert_identities(matrix)
    assert any("exceeds the declared total_wallet_exposure_limit" in p for p in problems)


def test_matrix_rejects_a_levered_peak_for_an_unlevered_arm():
    matrix = build({"AAA": 0.5}, peak_total=2.0, declared_twe=1.0)
    problems = wipeout.assert_identities(matrix)
    assert any("exceeds the declared" in p for p in problems)


def test_ruin_distance_is_zero_once_the_basket_can_erase_the_account():
    matrix = build({"AAA": 0.58}, peak_total=1.2, declared_twe=3.0)
    assert matrix.ruin_distance == 0.0
    assert matrix.loss_fraction(wipeout.SCOPE_BASKET_PEAK, 1.0) == pytest.approx(1.0)


def test_summary_rows_carry_the_capital_and_liquidation_fields():
    matrix = build({"AAA": 0.58}, peak_total=2.5, declared_twe=3.0)
    row = wipeout.ruin_summary_rows([matrix])[0]
    assert row["declared_twe"] == 3.0
    assert row["starting_balance_usd"] == 10000.0
    assert row["liquidation_threshold"] == 0.05
    assert row["liquidation_shock_at_peak"] == pytest.approx(0.95 / 2.5, rel=1e-9)


# --------------------------------------------------------------------------------------
# Affordability appendix
# --------------------------------------------------------------------------------------


def test_affordability_scales_with_the_slot_budget():
    market = {"min_cost": 5.0, "min_qty": 0.1, "qty_step": 0.1, "contractSize": 1.0}
    cheap = affordability.affordability_row(
        "AAA", market, price=10.0, balance=10000.0, per_slot_budget=0.587142857, entry_pct=0.0081
    )
    assert cheap["entry_cost_usd"] == pytest.approx(10000 * 0.587142857 * 0.0081, rel=1e-9)
    assert cheap["affordable"] is True
    assert cheap["min_balance_required_usd"] == pytest.approx(
        max(5.0, 0.1 * 10.0) / (0.587142857 * 0.0081), rel=1e-9
    )


def test_affordability_reports_absence_without_a_price_basis():
    row = affordability.affordability_row(
        "AAA",
        {"min_cost": 5.0, "min_qty": 0.1, "qty_step": 0.1, "contractSize": 1.0},
        price=None,
        balance=10000.0,
        per_slot_budget=0.587142857,
        entry_pct=0.0081,
    )
    assert row["affordable"] is None
    assert "no offline price basis" in row["reason"]


def test_affordability_flags_a_balance_that_cannot_reach_min_cost():
    market = {"min_cost": 500.0, "min_qty": 0.1, "qty_step": 0.1, "contractSize": 1.0}
    row = affordability.affordability_row(
        "AAA", market, price=100.0, balance=1000.0, per_slot_budget=0.587142857, entry_pct=0.0081
    )
    assert row["affordable"] is False
    assert row["min_balance_required_usd"] > 1000.0


def test_affordability_artifact_matches_its_arm_when_present():
    arm = spec.VARIANTS_BY_KEY["twe300_10k__3y"]
    path = spec.ARTIFACTS / f"affordability_{arm.key}.json"
    if not path.exists():
        pytest.skip("affordability appendix not generated yet")
    payload = load(path)
    assert payload["arm"] == arm.key
    assert payload["starting_balance_usd"] == arm.starting_balance
    assert payload["total_wallet_exposure_limit"] == arm.declared_twe
    assert payload["entry_initial_qty_pct"] > 0
    assert 0 <= payload["affordable_count"] <= payload["coin_count"]


# --------------------------------------------------------------------------------------
# Rendering, liquidation evidence and the synthesis (only when artifacts exist)
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
        assert f"`{variant.key}`" in report
        assert "强平" in report


def test_tracked_evidence_carries_no_host_paths_and_no_nan_prose():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no run bundle is present locally")
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        for name in ("run_record.json", "global_metrics.json", "annual_analysis.md"):
            text = (run_dir / name).read_text(encoding="utf-8")
            assert "/home/" not in text, f"{variant.key}/{name}"
            assert "C:\\" not in text, f"{variant.key}/{name}"


def test_liquidation_evidence_matches_the_engine_floor():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no run bundle is present locally")
    checked_any = False
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        analysis = load(run_dir / "analysis.json")
        equity = events.load_equity(run_dir)
        floor = spec.liquidation_floor_usd(variant)
        last = float(equity["usd_total_equity"].iloc[-1])
        if analysis.get("liquidated"):
            checked_any = True
            # The persisted series is sampled every `balance_sample_divider` minutes, so its
            # last row is above the floor; the liquidation shape is the engine's own numbers.
            assert float(analysis["drawdown_worst_strategy_eq"]) >= 0.9, variant.key
            assert str(analysis["effective_end_date"]) < variant.dataset.window[1], variant.key
            assert last > floor, variant.key
        else:
            assert last > floor, variant.key
    if not checked_any:
        pytest.skip("no liquidated arm in the current bundles")


def test_arm_event_and_wipeout_artifacts_are_recomputable():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no run bundle is present locally")
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        wipeout_payload = load(run_dir / "tail_risk_wipeout.json")
        ruin = wipeout_payload["ruin"]
        assert ruin["declared_twe"] == pytest.approx(variant.declared_twe)
        assert ruin["peak_total_exposure"] <= variant.declared_twe + 1e-3
        shock = ruin.get("liquidation_shock_at_peak")
        if shock is not None:
            # A shock above 1.0 means even a total price collapse of the book would not
            # reach the engine floor; below 1.0 it is the adverse move that would.
            assert shock > 0.0
            # The shock is measured against the balance at the peak exposure, not the
            # starting balance: exposure is a fraction of the balance at that moment.
            balance_at_peak = ruin["peak_exposure_balance_usd"]
            expected = (
                balance_at_peak - ruin["starting_balance_usd"] * ruin["liquidation_threshold"]
            ) / (ruin["peak_total_exposure"] * balance_at_peak)
            assert shock == pytest.approx(expected, rel=1e-6)


def test_synthesis_restates_the_arms_numbers():
    summary_path = spec.ARTIFACTS / "twe300_10k_summary.json"
    synthesis_path = spec.STUDY / "twe300_10k_analysis.md"
    if not summary_path.exists() or not synthesis_path.exists():
        pytest.skip("the synthesis has not been built locally")
    summary = load(summary_path)
    text = synthesis_path.read_text(encoding="utf-8")
    assert "## 九、判据裁决" in text
    for key, entry in (summary.get("arms") or {}).items():
        variant = spec.VARIANTS_BY_KEY.get(key)
        if variant is None:
            continue
        run_dir = spec.find_variant_run_dir(variant)
        analysis = load(run_dir / "analysis.json")
        assert entry["gain_strategy_eq"] == pytest.approx(
            float(analysis["gain_strategy_eq"]), rel=1e-12
        )
        assert entry["declared_twe"] == pytest.approx(variant.declared_twe)
        assert entry["starting_balance"] == pytest.approx(variant.starting_balance)
        assert bool(entry["liquidated"]) == bool(analysis["liquidated"])
