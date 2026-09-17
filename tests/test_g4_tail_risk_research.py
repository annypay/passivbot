"""Offline checks for the g4 tail-risk ("wipe-out") study.

The study's claims are: every arm is the frozen parent config plus a declared list of changes
on a frozen dataset; the event table and the wipe-out matrix are derived, not hand-picked; and
the synthesis restates the arms' own tracked numbers. These tests pin the frozen artifacts and
exercise the derived-table maths on synthetic inputs. No backtest runs here, nothing touches
the network, and every check that needs a local-only artifact skips when it is absent.

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
STUDY = REPO / "backtests/binance/g4_tail_risk_research_2026-09-17"
TOOLS = STUDY / "report_tools"


def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


events = _load_tool("tail_risk_event_windows", "event_windows.py")
spec = _load_tool("tail_risk_variant_spec", "variant_spec.py")
wipeout = _load_tool("tail_risk_wipeout_matrix", "wipeout_matrix.py")


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
        assert spec.sha256_file(path) == item["analysis_sha256"]


def test_variant_input_is_frozen_and_covers_every_arm():
    payload = load(spec.VARIANT_INPUT_PATH)
    assert payload["parent_config_sha256"] == spec.SOURCE_CONFIG_SHA256
    assert payload["run_order"] == list(spec.RUN_VARIANT_ORDER)
    assert payload["reused_arm_keys"] == list(spec.REUSED_ARM_KEYS)
    declared = {item["key"] for item in payload["arms"]}
    assert declared == set(spec.DEFAULT_VARIANT_ORDER)
    assert {item["id"] for item in payload["decision_rules"]} == {"J1", "J2", "J3", "J4"}
    assert payload["event_rule"]["threshold"] == spec.EVENT_RULE["threshold"]
    assert payload["shock_levels"] == list(spec.SHOCK_LEVELS)


def test_every_arm_config_is_the_parent_plus_its_declared_changes():
    parent = load(spec.SOURCE_CONFIG)
    for variant in spec.VARIANTS:
        frozen = load(variant.config_path)
        allowed = (
            spec.ALLOWED_DATASET_DIFF_PATHS
            if variant.dataset.override_mode
            else spec.ALLOWED_CONFIG_DIFF_PATHS
        )
        problems = spec.diff_subtrees(
            parent["backtest"], frozen["backtest"], "backtest", allowed=allowed
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
            assert spec.get_path(frozen, dotted) == to
        assert problems == [], f"{variant.key}: {problems}"


def test_arm_keys_and_legs_are_consistent():
    assert len({variant.key for variant in spec.VARIANTS}) == len(spec.VARIANTS)
    legs = spec.variant_legs()
    assert set(legs) == {"3y", "ext", "syn"}
    assert set(spec.REUSED_ARM_KEYS) == {"off__3y", "coin__3y"}
    assert set(spec.RUN_VARIANT_ORDER) == set(spec.DEFAULT_VARIANT_ORDER) - set(
        spec.REUSED_ARM_KEYS
    )
    for key in ("off__synth_a", "unified_r10__synth_a", "off__synth_b", "unified_r10__synth_b"):
        assert spec.VARIANTS_BY_KEY[key].synthetic is True
    for key in ("unified_r10__3y", "twe_090__ext"):
        assert spec.VARIANTS_BY_KEY[key].synthetic is False


def test_every_arm_declares_its_runtime_plot_flag():
    for key in ("off__ext", "off__3y", "unified_r10__synth_a"):
        variant = spec.VARIANTS_BY_KEY[key]
        assert spec.runtime_flags(variant) == ("--disable_plotting", "coin_fills")
        assert spec.declared_plot_groups(variant) == {"coin_fills"}


def test_disabled_plot_groups_parses_both_spellings():
    assert spec.disabled_plot_groups("coin_fills") == {"coin_fills"}
    assert spec.disabled_plot_groups(["coin_fills"]) == {"coin_fills"}
    assert spec.disabled_plot_groups(False) == set()
    assert spec.disabled_plot_groups(None) == set()
    assert spec.disabled_plot_groups(True) == {
        "all",
        "summary",
        "balance",
        "twe",
        "pnl",
        "hard_stop",
        "coin_fills",
    }
    assert "balance" in spec.disabled_plot_groups("summary")


# --------------------------------------------------------------------------------------
# Event-window maths
# --------------------------------------------------------------------------------------


def synthetic_benchmark() -> pd.DataFrame:
    """A calm series with one 35% three-day crash and one 8% dip."""
    index = pd.date_range("2022-01-01", periods=400, freq="D", tz="UTC")
    price = np.full(index.size, 100.0)
    price[110:113] = [100.0, 80.0, 65.0]
    price[113:300] = 65.0
    price[300:308] = np.linspace(65.0, 60.0, 8)
    price[308:] = 60.0
    return pd.DataFrame({"btc_usd_price": price}, index=index)


def test_detect_episodes_finds_the_crash_and_ignores_the_small_dip():
    episodes = events.detect_episodes(
        synthetic_benchmark(), lookback_days=7, threshold=-0.15, min_gap_days=30
    )
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.drop_pct > 0.25
    assert episode.label in ("", "未标注事件")


def test_episode_labels_attach_by_overlap():
    episodes = events.detect_episodes(
        synthetic_benchmark(),
        lookback_days=7,
        threshold=-0.15,
        min_gap_days=30,
        labels=(("合成崩盘", "2022-04-01", "2022-05-31"),),
    )
    assert episodes[0].label == "合成崩盘"


def test_window_metrics_measures_a_known_drawdown():
    index = pd.date_range("2022-01-01", periods=100, freq="D", tz="UTC")
    values = np.concatenate([np.full(10, 100.0), np.linspace(90.0, 60.0, 20), np.full(70, 60.0)])
    equity = pd.DataFrame({"strategy_equity": values}, index=index)
    metrics = events.window_metrics(
        equity, start_of_day("2022-01-01"), start_of_day("2022-04-10")
    )
    assert metrics["max_drawdown"] == pytest.approx(0.40, abs=1e-6)
    assert metrics["underwater_days"] > 0


def test_arm_event_row_reports_absence_instead_of_nan():
    index = pd.date_range("2022-01-01", periods=100, freq="D", tz="UTC")
    equity = pd.DataFrame({"strategy_equity": np.full(100, 100.0)}, index=index)
    fills = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2022-01-05"], utc=True),
            "wallet_exposure": [0.05],
            "twe_long": [0.05],
            "pnl": [1.0],
            "fee_paid": [-0.1],
            "coin": ["AAA"],
            "type": ["entry_initial_normal_long"],
        }
    )
    episode = events.Episode(
        breach_start_ms=start_of_day("2022-02-01"),
        breach_end_ms=start_of_day("2022-02-02"),
        peak_ms=start_of_day("2022-01-25"),
        peak_price=100.0,
        trough_ms=start_of_day("2022-02-10"),
        trough_price=80.0,
        drop_pct=0.2,
        label="测试事件",
    )
    row = events.arm_event_row(episode, equity, fills)
    assert row["twe_peak"] is None
    assert row["panic_fills"] == 0
    assert row["max_drawdown"] == pytest.approx(0.0)


# --------------------------------------------------------------------------------------
# Wipe-out matrix
# --------------------------------------------------------------------------------------


def synthetic_fills(per_coin: dict[str, float], balance: float = 100_000.0) -> pd.DataFrame:
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


def build(per_coin: dict[str, float], peak_total: float) -> wipeout.WipeoutMatrix:
    return wipeout.build_matrix(
        "test",
        analysis={"total_wallet_exposure_max": peak_total},
        per_coin=wipeout.per_coin_max_exposure(synthetic_fills(per_coin)),
        fills=synthetic_fills(per_coin),
        shock_levels=spec.SHOCK_LEVELS,
        start_balance_usd=100_000.0,
    )


def test_wipeout_matrix_bounds_a_single_coin_and_the_basket():
    matrix = build({"AAA": 0.1957, "BBB": 0.19, "CCC": 0.18}, peak_total=0.6)
    assert matrix.scope_exposure(wipeout.SCOPE_WORST_COIN) == pytest.approx(0.1957)
    assert matrix.loss_fraction(wipeout.SCOPE_WORST_COIN, 1.0) == pytest.approx(0.1957)
    assert matrix.loss_fraction(wipeout.SCOPE_BASKET_PEAK, 1.0) == pytest.approx(0.6)
    assert matrix.ruin_distance == pytest.approx(0.4)
    assert wipeout.assert_identities(matrix) == []
    assert matrix.coins_to_breach(0.2) == 2
    # three coins sum to 0.5657, so breaching -50% needs all three; -60% is out of reach.
    assert matrix.coins_to_breach(0.5) == 3
    assert matrix.coins_to_breach(0.6) is None


def test_wipeout_identities_reject_impossible_exposure():
    matrix = build({"AAA": 0.99}, peak_total=1.5)
    problems = wipeout.assert_identities(matrix)
    assert any("exceeds 1.0" in problem for problem in problems)


def test_engine_peak_exposure_can_exceed_one_by_the_bounded_allowance():
    matrix = build({"AAA": 0.196}, peak_total=1.0000022)
    assert wipeout.assert_identities(matrix) == []


def test_per_coin_exposure_prefers_the_ledger():
    fills = synthetic_fills({"AAA": 0.1, "BBB": 0.2})
    assert wipeout.per_coin_max_exposure(fills) == [("BBB", 0.2), ("AAA", 0.1)]


# --------------------------------------------------------------------------------------
# Report and synthesis consistency (only when the local artifacts exist)
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


def test_tracked_evidence_carries_no_host_paths():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no run bundle is present locally")
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        for name in ("run_record.json", "global_metrics.json", "annual_analysis.md"):
            text = (run_dir / name).read_text(encoding="utf-8")
            assert "/home/" not in text, f"{variant.key}/{name}"
            assert "C:\\" not in text, f"{variant.key}/{name}"


def test_arm_event_and_wipeout_artifacts_are_recomputable():
    arms = rendered_arms()
    if not arms:
        pytest.skip("no run bundle is present locally")
    for variant in arms:
        run_dir = spec.find_variant_run_dir(variant)
        for name in ("tail_risk_events.json", "tail_risk_wipeout.json"):
            assert (run_dir / name).exists(), f"{variant.key}/{name}"
        payload = load(run_dir / "tail_risk_wipeout.json")
        ruin = payload["ruin"]
        assert 0.0 <= ruin["ruin_distance"] <= 1.0
        assert ruin["worst_coin_exposure"] <= ruin["top3_exposure"] <= ruin["top7_exposure"]


def test_synthesis_restates_the_arms_numbers():
    summary_path = STUDY / "artifacts/tail_risk_summary.json"
    synthesis_path = STUDY / "tail_risk_analysis.md"
    if not summary_path.exists() or not synthesis_path.exists():
        pytest.skip("the synthesis has not been built locally")
    summary = load(summary_path)
    text = synthesis_path.read_text(encoding="utf-8")
    assert "## 六、判据裁决" in text
    for key, entry in (summary.get("arms") or {}).items():
        variant = spec.VARIANTS_BY_KEY[key]
        run_dir = spec.find_variant_run_dir(variant)
        analysis = load(run_dir / "analysis.json")
        assert entry["gain_strategy_eq"] == pytest.approx(
            float(analysis["gain_strategy_eq"]), rel=1e-12
        )
        assert entry["peak_total_exposure"] == pytest.approx(
            float(analysis["total_wallet_exposure_max"]), rel=1e-9
        )
        assert entry["liquidated"] == bool(analysis["liquidated"])
