"""Offline checks for the g4 @ TWE 3.0 risk-geometry study.

The study's claims are: every arm keeps the production changes of the previous rounds (10,000
USDT starting capital, and an exposure geometry declared as `total_wallet_exposure_limit`,
`n_positions` and `we_excess_allowance_pct`); the occupancy-discipline arms set the allowance to
zero so no coin may spend an empty slot's budget; the cooldown rungs, the cumulative terminal
rung and the realized-loss brake are the only expressible ladder substitutes; the out-of-sample
leg is served by the `intersection` override and never overlaps the search window; the parameter
search is declared (bounds, seed, budget, pick rule, fine-tune selectors) and its candidates are
frozen with the pareto file they came from; and the geometry numbers a report cites are derived
from the arm's own ledger. These tests pin the frozen artifacts and exercise the derived maths on
synthetic inputs. No backtest runs here, nothing touches the network, and checks that need local
artifacts skip when absent.

The tool modules are loaded through `importlib` under study-specific names: every study under
`backtests/**/report_tools/` names its registry `variant_spec.py`, so a plain `import
variant_spec` in a pytest session binds whichever study's module was imported first.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "backtests/binance/g4_twe300_risk_optimization_2026-09-17"
TOOLS = STUDY / "report_tools"

#: The study tools already loaded here, by file name, and the plain module names their bodies
#: import them by. Every study under `backtests/**/report_tools/` names its registry
#: `variant_spec.py`, so `import variant_spec as study` inside a tool body must resolve to *this*
#: study's module rather than to whichever study a pytest session happened to import first.
_LOADED_TOOLS: dict[str, object] = {}
_SIBLING_ALIASES = {
    "variant_spec.py": "variant_spec",
    "event_windows.py": "event_windows",
    "wipeout_matrix.py": "wipeout_matrix",
    "geometry_analysis.py": "geometry_analysis",
}


def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _LOADED_TOOLS[filename] = module
    return module


spec = _load_tool("geometry_variant_spec", "variant_spec.py")
events = _load_tool("geometry_event_windows", "event_windows.py")


def _load_dependent_tool(name: str, filename: str):
    """Load a tool whose module body imports its sibling tools by their plain names.

    The aliases are registered *before* the module body runs and removed again afterwards, so a
    tool binds the modules this file already loaded and no other study's test can pick up a
    `variant_spec` that is not this study's.
    """
    previous_path = list(sys.path)
    replaced = {alias: sys.modules.get(alias) for alias in _SIBLING_ALIASES.values()}
    sys.path.insert(0, str(TOOLS))
    for sibling, alias in _SIBLING_ALIASES.items():
        module = _LOADED_TOOLS.get(sibling)
        if module is not None:
            sys.modules[alias] = module
    try:
        return _load_tool(name, filename)
    finally:
        for alias, module in replaced.items():
            if module is None:
                sys.modules.pop(alias, None)
            else:
                sys.modules[alias] = module
        sys.path[:] = previous_path


wipeout = _load_tool("geometry_wipeout_matrix", "wipeout_matrix.py")
geometry = _load_dependent_tool("geometry_geometry_analysis", "geometry_analysis.py")


def load(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------------------
# Frozen inputs, references and arm declarations
# --------------------------------------------------------------------------------------


def test_pinned_parent_and_reference_artifacts_still_match():
    assert spec.sha256_file(spec.SOURCE_CONFIG) == spec.SOURCE_CONFIG_SHA256
    assert spec.sha256_file(spec.SOURCE_PROFILE_INPUT) == spec.SOURCE_PROFILE_INPUT_SHA256
    for key, item in spec.REFERENCE_RUNS.items():
        path = Path(item["run_dir"]) / "analysis.json"
        if not path.exists():
            pytest.skip(f"reference arm {key} is not present locally")
        assert spec.sha256_file(path) == item["analysis_sha256"], key


def test_variant_input_is_frozen_and_covers_every_arm():
    payload = load(spec.VARIANT_INPUT_PATH)
    assert payload["parent_config_sha256"] == spec.SOURCE_CONFIG_SHA256
    assert payload["run_order"] == list(spec.RUN_VARIANT_ORDER)
    assert {item["key"] for item in payload["arms"]} == set(spec.DEFAULT_VARIANT_ORDER)
    assert {item["id"] for item in payload["decision_rules"]} == {"J1", "J2", "J3", "J4", "J5", "J6"}
    assert [leg["key"] for leg in payload["legs"]] == list(spec.LEG_ORDER)
    assert payload["search_leg"] == spec.SEARCH_LEG == "3y"
    assert sorted(payload["reference_kinds"]) == ["guard", "no_guard", "no_guard_allow0"]
    geometry_contract = payload["geometry_contract"]
    assert geometry_contract["allowance_grid"] == list(spec.ARM_ALLOWANCE_GRID)
    assert geometry_contract["artifact"] == "risk_geometry.json"
    assert "total_wallet_exposure_limit / n_positions" in geometry_contract["slot_share"]
    mapping = {row["request"]: row for row in geometry_contract["mapping_table"]}
    assert "不去占用空槽的额度" in mapping
    assert "冷却阶梯要接在冷却时长上" in mapping


def test_search_contract_is_declared_and_bounded():
    payload = load(spec.VARIANT_INPUT_PATH)
    contract = payload["search_contract"]
    assert contract["backend"] == "pymoo"
    assert contract["seed"] == spec.SEARCH_SEED
    assert contract["iters"] == spec.SEARCH_ITERS
    assert contract["candidate_legs"] == list(spec.SEARCH_CANDIDATE_LEGS)
    assert contract["pick"] == dict(spec.SEARCH_PICK)
    leaves = spec.search_bound_leaves()
    assert set(leaves) == set(spec.SEARCH_PARAM_PATHS)
    assert set(spec.SEARCH_PARAM_FLAT_KEYS) == set(spec.SEARCH_PARAM_PATHS)
    assert len(set(spec.SEARCH_PARAM_FLAT_KEYS.values())) == len(spec.SEARCH_PARAM_FLAT_KEYS)
    for key, (low, high, step) in leaves.items():
        assert low < high and step > 0, key
    assert leaves["we_excess_allowance_pct"][0] == 0.0
    assert leaves["total_wallet_exposure_limit"][0] >= 2.0
    assert set(contract["fine_tune_params"]) == set(spec.SEARCH_FINE_TUNE_PARAMS)
    assert len(spec.SEARCH_FINE_TUNE_PARAMS) == len(leaves) == 6
    # The six fine-tune selectors must be exactly the searched dimensions' config paths.
    assert set(spec.SEARCH_FINE_TUNE_PARAMS) == set(spec.SEARCH_PARAM_PATHS.values())
    for cell in spec.SEARCH_FALLBACK_CELLS:
        assert set(cell) == set(spec.SEARCH_PARAM_PATHS)
        for key, value in cell.items():
            low, high, _step = leaves[key]
            assert low - 1e-9 <= float(value) <= high + 1e-9, (key, value)


def test_every_arm_keeps_the_baseline_and_declares_its_geometry():
    parent = load(spec.SOURCE_CONFIG)
    for variant in spec.VARIANTS:
        frozen = load(variant.config_path)
        assert frozen["backtest"]["starting_balance"] == 10000, variant.key
        risk = frozen["bot"]["long"]["risk"]
        assert 1.0 <= risk["total_wallet_exposure_limit"] <= 3.0, variant.key
        assert float(risk["n_positions"]).is_integer() and risk["n_positions"] >= 1, variant.key
        assert 0.0 <= risk["we_excess_allowance_pct"] <= spec.PARENT_ALLOWANCE_PCT, variant.key
        assert risk["we_excess_allowance_mode"] == "bounded", variant.key
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
            assert spec.numeric_equal(spec.get_path(frozen, dotted), to), f"{variant.key}: {dotted}"
        assert problems == [], f"{variant.key}: {problems}"


def test_stage_a_covers_the_allowance_grid_and_stage_b_keeps_it_at_zero():
    stage_a = {
        key: variant
        for key, variant in spec.VARIANTS_BY_KEY.items()
        if spec.stage_of(variant) == "A"
    }
    stage_b = {
        key: variant
        for key, variant in spec.VARIANTS_BY_KEY.items()
        if spec.stage_of(variant) == "B"
    }
    assert {variant.lever for variant in stage_a.values()} == set(spec.STAGE_A_LEVERS)
    assert {variant.lever for variant in stage_b.values()} == set(spec.STAGE_B_LEVERS)
    allowances = {variant.declared_allowance_pct for variant in stage_a.values()}
    assert allowances == set(spec.ARM_ALLOWANCE_GRID)
    for key, variant in stage_b.items():
        assert variant.declared_allowance_pct == 0.0, key
    # Stage B is the ladder-substitute grid: fixed rungs, a cumulative rung and a loss brake.
    assert spec.VARIANTS_BY_KEY["b_cool72__3y"].guard_params[
        "cooldown_minutes_after_red"
    ] == pytest.approx(4320.0)
    assert spec.VARIANTS_BY_KEY["b_term070__3y"].guard_params[
        "no_restart_drawdown_threshold"
    ] == pytest.approx(0.70)
    assert spec.VARIANTS_BY_KEY["b_red015__3y"].guard_params["red_threshold"] == pytest.approx(0.15)
    assert spec.VARIANTS_BY_KEY["b_ema15__3y"].guard_params["ema_span_minutes"] == pytest.approx(15.0)
    loss_gate = load(spec.VARIANTS_BY_KEY["b_lossgate050__3y"].config_path)
    assert loss_gate["live"]["max_realized_loss_pct"] == pytest.approx(0.5)


def test_guard_parameters_are_inside_the_engine_contract():
    for variant in spec.VARIANTS:
        guard = variant.guard_params
        assert guard["enabled"] is True, variant.key
        assert guard["red_threshold"] <= guard["no_restart_drawdown_threshold"] <= 1.0, variant.key
        assert 0.0 < guard["orange_threshold"] < guard["red_threshold"], variant.key
        assert 0.0 < guard["yellow_ratio"] < guard["orange_ratio"] < 1.0, variant.key
        assert guard["ema_span_minutes"] > 0.0, variant.key
        assert guard["cooldown_minutes_after_red"] >= 0.0, variant.key
        assert 0.0 < guard["lookback_days"] <= spec.PARENT_PNLS_LOOKBACK_DAYS, variant.key


def test_out_of_sample_leg_is_served_by_its_own_override_window():
    pre = spec.DATASETS["pre"]
    ext = spec.DATASETS["ext"]
    assert pre.override_mode == "intersection"
    assert ext.override_mode == "dataset"
    assert pre.rel_path == ext.rel_path, "the out-of-sample leg reuses the long bundle"
    assert pre.window[0] == ext.window[0]
    assert pre.window[1] < spec.DATASETS["3y"].window[0], "pre must not reach into the search window"
    frozen = load(spec.VARIANTS_BY_KEY["a_allow000__pre"].config_path)
    backtest = frozen["backtest"]
    assert backtest["hlcvs_data_override_mode"] == "intersection"
    assert backtest["start_date"] == pre.window[0]
    assert backtest["end_date"] == pre.window[1]


# --------------------------------------------------------------------------------------
# Geometry maths on synthetic inputs
# --------------------------------------------------------------------------------------


def synthetic_config(
    *, twe: float = 3.0, slots: float = 7.0, allowance: float = 0.0
) -> dict:
    return {
        "bot": {
            "long": {
                "risk": {
                    "total_wallet_exposure_limit": twe,
                    "n_positions": slots,
                    "we_excess_allowance_pct": allowance,
                    "we_excess_allowance_mode": "bounded",
                }
            }
        },
        "backtest": {"starting_balance": 10_000.0, "liquidation_threshold": 0.05},
    }


def synthetic_fills(rows: list[tuple[str, str, float, float, float]]) -> pd.DataFrame:
    """(timestamp, coin, psize, wallet_exposure, balance) -> a fills frame."""
    return pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(stamp, tz="UTC"),
                "coin": coin,
                "psize": psize,
                "qty": psize,
                "price": 100.0,
                "wallet_exposure": exposure,
                "usd_total_balance": balance,
                "twe_long": 1.0,
                "type": "entry_initial_normal_long" if psize else "close_grid_long",
            }
            for stamp, coin, psize, exposure, balance in rows
        ]
    )


def test_bounded_allowance_matches_the_engine_helper():
    sys.path.insert(0, str(REPO / "src"))
    try:
        from risk_limits import effective_we_excess_allowance_pct as engine_helper
    except ImportError:  # pragma: no cover - the engine package is always present in-repo
        pytest.skip("src/risk_limits.py is not importable here")
    for twe, slots, allowance in ((3.0, 7.0, 0.0), (3.0, 7.0, 0.37), (2.0, 5.0, 0.37), (1.0, 10.0, 0.4)):
        base = twe / slots
        expected = engine_helper(
            wallet_exposure_limit=base,
            risk_we_excess_allowance_pct=allowance,
            total_wallet_exposure_limit=twe,
            risk_we_excess_allowance_mode="bounded",
        )
        actual = geometry.bounded_effective_allowance(
            raw_allowance=allowance,
            wallet_exposure_limit=base,
            total_wallet_exposure_limit=twe,
            mode="bounded",
        )
        assert actual == pytest.approx(expected), (twe, slots, allowance)


def test_geometry_declaration_uses_the_bounded_cap():
    declared = geometry.geometry_declaration(synthetic_config(twe=3.0, slots=7.0, allowance=0.0))
    assert declared["slot_share"] == pytest.approx(3.0 / 7.0)
    assert declared["effective_allowance_pct"] == pytest.approx(0.0)
    assert declared["per_slot_cap"] == pytest.approx(3.0 / 7.0)
    with_allowance = geometry.geometry_declaration(
        synthetic_config(twe=3.0, slots=7.0, allowance=0.37)
    )
    assert with_allowance["per_slot_cap"] == pytest.approx(3.0 / 7.0 * 1.37)


def test_occupancy_timeline_counts_open_positions_over_time():
    fills = synthetic_fills(
        [
            ("2024-01-01 00:00", "AAA", 1.0, 0.4, 10_000.0),
            ("2024-01-01 06:00", "BBB", 1.0, 0.4, 10_000.0),
            ("2024-01-01 12:00", "AAA", 0.0, 0.0, 10_000.0),
            ("2024-01-02 00:00", "BBB", 0.0, 0.0, 10_000.0),
        ]
    )
    segments = geometry.occupancy_timeline(fills)
    assert [(start.hour, end.hour, active) for start, end, active in segments] == [
        (0, 6, 1),
        (6, 12, 2),
        (12, 0, 1),
    ]
    summary = geometry.occupancy_summary(
        fills,
        window_start=pd.Timestamp("2024-01-01", tz="UTC"),
        window_end=pd.Timestamp("2024-01-02", tz="UTC"),
        n_positions=7.0,
    )
    assert summary["active_coins_peak"] == 2.0
    assert summary["window_minutes"] == pytest.approx(1440.0)
    assert summary["time_with_any_position_share"] == pytest.approx(1.0)
    # 1 coin for 12h, 2 coins for 6h, 1 coin for 12h => 30 coin-hours over 24h.
    assert summary["active_coins_mean"] == pytest.approx(30.0 / 24.0)
    assert summary["empty_slot_time_share"] == pytest.approx((7.0 - 1.25) / 7.0)


def test_geometry_checks_flag_a_real_violation_and_tolerate_execution_slack():
    declared = geometry.geometry_declaration(synthetic_config())
    within_slack = {
        "peak_coin_exposure": declared["per_slot_cap"] * 1.03,
        "peak_total_exposure": 2.9,
        "active_coins_peak": 7.0,
        "mean_total_exposure": 1.0,
        "empty_slot_time_share": 0.5,
        "time_with_any_position_share": 0.9,
    }
    assert geometry.geometry_checks(declared, dict(within_slack)) == []
    beyond_slack = dict(within_slack)
    beyond_slack["peak_coin_exposure"] = declared["per_slot_cap"] * 1.20
    assert geometry.geometry_checks(declared, beyond_slack)
    too_many = dict(within_slack)
    too_many["active_coins_peak"] = 9.0
    assert geometry.geometry_checks(declared, too_many)
    above_limit = dict(within_slack)
    above_limit["peak_total_exposure"] = 3.5
    assert geometry.geometry_checks(declared, above_limit)


def test_cap_overshoot_fill_names_the_fill_that_broke_the_cap():
    fills = synthetic_fills(
        [
            ("2024-01-01 00:00", "AAA", 1.0, 0.40, 10_000.0),
            ("2024-01-01 01:00", "AAA", 2.0, 0.4434, 10_000.0),
        ]
    )
    record = geometry.cap_overshoot_fill(fills, "AAA")
    assert record["wallet_exposure"] == pytest.approx(0.4434)
    assert record["qty"] == pytest.approx(2.0)
    assert geometry.cap_overshoot_fill(fills, "ZZZ") is None


# --------------------------------------------------------------------------------------
# Search selection and fallback
# --------------------------------------------------------------------------------------


def test_search_candidate_deltas_are_declared_against_the_parent():
    params = {
        "total_wallet_exposure_limit": 2.5,
        "n_positions": 8,
        "we_excess_allowance_pct": 0.2,
        "hsl_red_threshold": 0.2,
        "hsl_ema_span_minutes": 60.0,
        "hsl_cooldown_minutes_after_red": 1440.0,
    }
    deltas = spec.search_candidate_deltas(params)
    paths = [path for path, _from, _to in deltas]
    assert len(paths) == len(set(paths)), "a candidate must declare each config path once"
    mapping = {path: to for path, _from, to in deltas}
    assert mapping[spec.TOTAL_WALLET_EXPOSURE_LIMIT_PATH] == pytest.approx(2.5)
    assert mapping[spec.N_POSITIONS_PATH] == 8
    assert mapping[spec.ALLOWANCE_PATH] == pytest.approx(0.2)
    assert mapping["bot.long.hsl.red_threshold"] == pytest.approx(0.2)
    assert mapping["bot.long.hsl.ema_span_minutes"] == pytest.approx(60.0)
    assert mapping["bot.long.hsl.cooldown_minutes_after_red"] == pytest.approx(1440.0)
    assert mapping[spec.STARTING_BALANCE_PATH] == spec.ARM_STARTING_BALANCE
    assert mapping[spec.PNLS_LOOKBACK_PATH] == spec.GUARD_LOOKBACK_DAYS
    assert mapping["live.hsl_signal_mode"] == "unified"
    for _path, source, target in deltas:
        assert source != target, "no-op deltas must be dropped"
    # A candidate equal to the parent's own values declares no exposure/guard change at all.
    parent_params = {
        "total_wallet_exposure_limit": 1.0,
        "n_positions": spec.PARENT_N_POSITIONS,
        "we_excess_allowance_pct": spec.PARENT_ALLOWANCE_PCT,
        "hsl_red_threshold": 0.15,
        "hsl_ema_span_minutes": 720.0,
        "hsl_cooldown_minutes_after_red": 2160.0,
    }
    parent_deltas = {path for path, _from, _to in spec.search_candidate_deltas(parent_params)}
    assert spec.TOTAL_WALLET_EXPOSURE_LIMIT_PATH not in parent_deltas
    assert spec.ALLOWANCE_PATH not in parent_deltas
    assert "bot.long.hsl.red_threshold" not in parent_deltas


def test_frozen_search_selection_matches_the_contract():
    if not spec.SEARCH_SELECTION_PATH.exists():
        pytest.skip("the search has not produced a selection locally")
    payload = load(spec.SEARCH_SELECTION_PATH)
    contract = load(spec.VARIANT_INPUT_PATH)["search_contract"]
    leaves = spec.search_bound_leaves()
    selected = payload["selected"]
    assert selected, "a selection file must carry at least one candidate"
    assert len(selected) <= int(contract["pick"]["max_candidates"])
    levers = [item["lever"] for item in selected]
    assert len(set(levers)) == len(levers)
    for item in selected:
        assert set(item["parameters"]) == set(spec.SEARCH_PARAM_PATHS)
        for key, value in item["parameters"].items():
            low, high, _step = leaves[key]
            assert low - 1e-9 <= float(value) <= high + 1e-9, (item["lever"], key, value)
        if item.get("selection_kind") == "fallback_grid_cell":
            assert item.get("pareto_path") is None
        else:
            assert item.get("pareto_path") and item.get("pareto_sha256")
    if payload["mode"] == "search":
        for item in selected:
            metrics = item.get("metrics") or {}
            if metrics.get("liquidated") is True:
                pytest.fail(f"{item['lever']} was selected although it liquidated")
            drawdown = metrics.get("drawdown_worst_strategy_eq")
            if drawdown is not None:
                assert float(drawdown) <= float(contract["pick"]["drawdown_max"]) + 1e-9


def test_search_derived_arms_exist_only_with_a_selection():
    selection_exists = spec.SEARCH_SELECTION_PATH.exists()
    search_arms = [v for v in spec.VARIANTS if v.lever.startswith("c")]
    if not selection_exists:
        assert search_arms == []
        return
    selected = load(spec.SEARCH_SELECTION_PATH)["selected"]
    assert len(search_arms) == len(selected) * len(spec.SEARCH_CANDIDATE_LEGS)
    for variant in search_arms:
        assert spec.stage_of(variant) == "C"
        assert variant.leg in spec.SEARCH_CANDIDATE_LEGS


# --------------------------------------------------------------------------------------
# Rendered arms and synthesis (only when the local artifacts exist)
# --------------------------------------------------------------------------------------


def rendered_arms() -> list[spec.Variant]:
    """Arms whose run directory exists *and* has been rendered (reports are local artifacts)."""
    found = []
    for key in spec.RUN_VARIANT_ORDER:
        variant = spec.VARIANTS_BY_KEY[key]
        try:
            run_dir = spec.find_variant_run_dir(variant)
        except SystemExit:
            continue
        if not (run_dir / "annual_analysis.md").exists():
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
        for section in (
            "账户守护读数",
            "整装待发读数",
            "暴露几何与占用纪律",
            "参数搜索与选择轨迹",
            "样本外读数",
        ):
            assert section in report, f"{variant.key}: {section}"


def test_rendered_geometry_artifact_matches_the_ledger():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no rendered arm bundle is present locally")
    checked = 0
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        path = run_dir / "risk_geometry.json"
        if not path.exists():
            continue
        checked += 1
        recorded = load(path)
        analysis = load(run_dir / "analysis.json")
        recomputed = geometry.build_geometry_artifact(run_dir, analysis)
        assert recorded["declared"] == recomputed["declared"], variant.key
        for key, value in recomputed["observed"].items():
            assert recorded["observed"][key] == pytest.approx(value), f"{variant.key}: {key}"
        assert recorded["per_coin"] == recomputed["per_coin"], variant.key
        assert recorded["cross_check_problems"] == [], variant.key
        assert recorded["per_slot_cap"] == pytest.approx(variant.per_slot_cap), variant.key
        # The declared cap must hold up to the measured execution slack (relative + absolute).
        peak = recorded["observed"]["peak_coin_exposure"]
        assert geometry.cap_within_tolerance(recorded["per_slot_cap"], peak), variant.key
        if variant.declared_twe >= 3.0 and variant.declared_allowance_pct == 0.0:
            # Sanity band: the study measured at most 0.0191 absolute overshoot on a full-size cap.
            assert peak - recorded["per_slot_cap"] <= geometry.PER_SLOT_CAP_ABSOLUTE_SLACK + 1e-9
    if not checked:
        pytest.skip("risk_geometry.json is written by the renderer")


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
            "risk_geometry.json",
            "annual_analysis.md",
        ):
            text = (run_dir / name).read_text(encoding="utf-8")
            assert "/home/" not in text, f"{variant.key}/{name}"
            assert "C:\\" not in text, f"{variant.key}/{name}"


def test_synthesis_restates_the_arms_numbers():
    summary_path = spec.ARTIFACTS / "risk_geometry_summary.json"
    synthesis_path = spec.STUDY / "risk_geometry_analysis.md"
    if not summary_path.exists() or not synthesis_path.exists():
        pytest.skip("the synthesis has not been built locally")
    summary = load(summary_path)
    text = synthesis_path.read_text(encoding="utf-8")
    for heading in (
        "## 三、暴露几何与占用纪律对照",
        "## 六、参数搜索轨迹与选择",
        "## 七、样本外与跨腿对照",
        "## 十一、判据裁决",
        "## 十三、边界",
    ):
        assert heading in text, heading
    assert "占用纪律" in text and "we_excess_allowance_pct" in text
    verdicts = summary.get("verdicts") or {}
    for rule in ("J1", "J2", "J3", "J4", "J5", "J6"):
        assert rule in verdicts, rule
        assert str(verdicts[rule]["verdict"]) in text, rule
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
        geometry_path = run_dir / "risk_geometry.json"
        if geometry_path.exists():
            recorded = load(geometry_path)
            assert entry["per_slot_cap"] == pytest.approx(recorded["per_slot_cap"])
            assert entry["peak_coin_exposure"] == pytest.approx(
                recorded["observed"]["peak_coin_exposure"]
            )


# --------------------------------------------------------------------------------------
# 验证器与 episode 追踪
# --------------------------------------------------------------------------------------

TRACE_ARM = "b_red015__3y"
#: Everything `check_episode_trace` re-derives a recorded trace from: the run's dumped config, its
#: analysis telemetry, the fill ledger, the equity series, and the trace itself.
TRACE_BUNDLE_FILES = (
    "config.json",
    "analysis.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
    "episode_trace.json",
)

verifier = _load_dependent_tool("geometry_variant_verifier", "verify_variant_report.py")


def _different_value(value):
    """A value that must differ from `value` under the study's own equality."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return float(value) + 1.0
    return f"{value}-tampered"


@pytest.fixture
def traced_episode(tmp_path):
    """A scratch copy of the one arm that carries a trace, so a test may perturb it in place."""
    variant = spec.VARIANTS_BY_KEY[TRACE_ARM]
    try:
        source = spec.find_variant_run_dir(variant)
    except SystemExit:
        pytest.skip(f"the {TRACE_ARM} bundle is not present locally")
    missing = [name for name in TRACE_BUNDLE_FILES if not (source / name).exists()]
    if missing:
        pytest.skip(f"{TRACE_ARM} carries no {', '.join(missing)}")
    run_dir = tmp_path / TRACE_ARM
    run_dir.mkdir()
    for name in TRACE_BUNDLE_FILES:
        shutil.copy2(source / name, run_dir / name)
    return variant, run_dir, load(run_dir / "config.json"), load(run_dir / "episode_trace.json")


def test_tracked_episode_trace_verifies_clean(traced_episode):
    variant, run_dir, config, _recorded = traced_episode
    assert verifier.check_episode_trace(run_dir, config, variant) == []


def test_episode_trace_check_catches_a_perturbed_ladder_fill(traced_episode):
    variant, run_dir, config, recorded = traced_episode
    assert len(recorded["ladder"]) > 3, "the traced episode must have a fourth ladder fill"
    recorded["ladder"][3]["ledger_qty"] = recorded["ladder"][3]["ledger_qty"] * 1.05
    spec.write_json(run_dir / verifier.TRACE_NAME, recorded)
    problems = verifier.check_episode_trace(run_dir, config, variant)
    assert any("ladder[3].ledger_qty" in problem for problem in problems), problems


def test_episode_trace_check_reports_a_cross_check_problem(traced_episode):
    variant, run_dir, config, recorded = traced_episode
    recorded["cross_check_problems"] = ["injected arithmetic failure"]
    spec.write_json(run_dir / verifier.TRACE_NAME, recorded)
    problems = verifier.check_episode_trace(run_dir, config, variant)
    assert any(
        "cross-check" in problem and "injected arithmetic failure" in problem
        for problem in problems
    ), problems


def test_episode_trace_is_optional_tracked_evidence():
    """Arms without a trace are not defective, but a recorded trace is scanned like the rest."""
    assert verifier.TRACE_NAME == "episode_trace.json"
    assert verifier.TRACE_NAME in verifier.TRACKED_EVIDENCE
    assert verifier.TRACE_NAME in verifier.OPTIONAL_TRACKED_EVIDENCE
    assert verifier.TRACE_NAME not in verifier.REQUIRED_LOCAL


def test_hydrated_defaults_are_tolerated_but_a_declared_key_change_is_not(traced_episode):
    """The tolerance is narrow: current-engine default injection only, and never silent."""
    variant, _run_dir, config, _recorded = traced_episode
    frozen = load(variant.config_path)
    notes: list[str] = []
    assert verifier.check_arm_identity(variant, config, config, notes=notes) == []
    assert {
        "bot.long.hsl.halt_ladder_minutes",
        "bot.long.hsl.realized_loss_budget_pct",
        "bot.short.hsl.halt_ladder_minutes",
        "bot.short.hsl.realized_loss_budget_pct",
    } <= set(notes)
    for path in notes:
        # Tolerated paths are exactly the ones neither the dump nor the frozen arm carries; the
        # frozen arm's own declaration is compared strictly however the engine hydrates it.
        with pytest.raises(KeyError):
            spec.get_path(frozen, path)

    declared = {dotted for dotted, _from, _to in variant.deltas}
    hsl = frozen["bot"]["long"]["hsl"]
    key = next(name for name in sorted(hsl) if f"bot.long.hsl.{name}" not in declared)
    tampered = json.loads(json.dumps(config))
    tampered["bot"]["long"]["hsl"][key] = _different_value(tampered["bot"]["long"]["hsl"][key])
    problems = verifier.check_arm_identity(variant, tampered, tampered)
    assert any(f"bot.long.hsl.{key}" in problem for problem in problems), problems
