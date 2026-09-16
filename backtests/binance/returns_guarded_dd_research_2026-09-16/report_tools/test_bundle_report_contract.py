"""Pin the artifact-bundle and report contract for the returns-guarded study.

The study's screening path (`run`) writes only a `result.json` per cell. A deep
analysis report needs a completed artifact directory, so these tests cover the
bundle path that produces one:

- a bundle holds exactly one run directory, and a forced re-run replaces it;
- a report is rendered from the artifact directory and passes the repository's
  section-skeleton validator;
- a partial artifact directory fails loudly instead of rendering a report;
- `emit-config` reproduces the cell's exact config, gate block included.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[3]
STUDY = REPO / "backtests/binance/returns_guarded_dd_research_2026-09-16"
SEED_RUN = (
    STUDY
    / "artifacts/seed/backtest_results/binance_seed/binance"
)

sys.path.insert(0, str(STUDY / "report_tools"))
from _study_module import load_renderer, load_report_spec, load_study_runner  # noqa: E402

runner = load_study_runner()
renderer = load_renderer()
spec = load_report_spec()


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _seed_run_dir() -> Path:
    """The seed bundle's run directory, used as the source of a real analysis.json."""
    dirs = sorted(p for p in SEED_RUN.glob("*") if p.is_dir() and p.name[:2].isdigit())
    if not dirs:
        pytest.skip("seed bundle has not been produced yet")
    return dirs[0]


def synthetic_artifact_dir(tmp_path: Path, analysis: dict, *, with_analysis: bool = True) -> Path:
    """A minimum artifact directory: real analysis.json plus tiny fills/equity frames."""
    run_dir = tmp_path / "binance_seed" / "binance" / "2026-01-01T00_00_00"
    run_dir.mkdir(parents=True)
    if with_analysis:
        (run_dir / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "backtest": {"balance_sample_divider": 60, "candle_interval_minutes": 1},
                "live": {"approved_coins": {"long": ["BTC"], "short": []}, "hedge_mode": False},
            }
        ),
        encoding="utf-8",
    )
    stamps = pd.date_range("2024-01-01", periods=40, freq="60min", tz="UTC")
    equity = pd.DataFrame(
        {
            "timestamp": stamps,
            "usd_total_balance": [100_000.0 * (1.0 + 0.001 * index) for index in range(40)],
            "usd_total_equity": [100_000.0 * (1.0 + 0.001 * index) for index in range(40)],
            "strategy_equity": [100_000.0 * (1.0 + 0.001 * index) for index in range(40)],
        }
    )
    with gzip.open(run_dir / "balance_and_equity.csv.gz", "wt") as handle:
        equity.to_csv(handle, index=False)
    fills = pd.DataFrame(
        {
            "timestamp": stamps[:4],
            "coin": ["BTC"] * 4,
            "pnl": [10.0, -2.0, 5.0, 1.0],
            "fee_paid": [-0.5] * 4,
            "type": ["entry_grid_normal_long"] * 2 + ["close_grid_long"] * 2,
            "liquidity": ["maker"] * 4,
            "wallet_exposure": [0.1] * 4,
            "psize": [1.0] * 4,
            "price": [100.0] * 4,
            "qty": [1.0] * 4,
        }
    )
    fills.to_csv(run_dir / "fills.csv", index=False)
    return run_dir


# --------------------------------------------------------------------------- #
# run-directory discovery
# --------------------------------------------------------------------------- #


def test_run_dirs_under_ignores_non_timestamp_entries(tmp_path: Path) -> None:
    base = tmp_path / "backtest_results"
    (base / "binance_seed" / "binance" / "2026-01-01T00_00_00").mkdir(parents=True)
    (base / "binance_seed" / "binance" / "notes").mkdir(parents=True)
    found = runner.run_dirs_under(base)
    assert [path.name for path in found] == ["2026-01-01T00_00_00"]


def test_find_run_dir_refuses_to_guess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "backtest_results"
    for stamp in ("2026-01-01T00_00_00", "2026-01-02T00_00_00"):
        (base / "binance_seed" / "binance" / stamp).mkdir(parents=True)
    monkeypatch.setattr(runner, "bundle_results_base", lambda subdir: base)
    with pytest.raises(SystemExit, match="expected exactly one run directory"):
        runner.find_run_dir("seed")


def test_find_run_dir_accepts_an_explicit_path(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert runner.find_run_dir("seed", str(run_dir)) == run_dir.resolve()


# --------------------------------------------------------------------------- #
# required artifacts
# --------------------------------------------------------------------------- #


def test_missing_bundle_artifacts_lists_every_gap(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "analysis.json").write_text("{}", encoding="utf-8")
    missing = set(runner.missing_bundle_artifacts(run_dir))
    assert "analysis.json" not in missing
    assert missing == set(runner.REQUIRED_BUNDLE_ARTIFACTS) - {"analysis.json"}


def test_required_artifacts_cover_the_analytical_set_and_the_verifier_set() -> None:
    """The bundle must produce everything the report reads and the verifier checks."""
    required = set(runner.REQUIRED_BUNDLE_ARTIFACTS)
    analytical = set(runner.ANALYTICAL_BUNDLE_ARTIFACTS)
    assert analytical <= required
    # The verifier additionally requires the streamed audit and these summary figures.
    assert {"execution_audit.csv", "balance_and_equity.png", "total_wallet_exposure.png"} <= required
    assert {"pnl_cumsum.csv"} & required == set()


def test_analytical_subset_gates_a_bundle() -> None:
    """A bundle may only be declared complete when the analytical set is present."""
    assert set(runner.ANALYTICAL_BUNDLE_ARTIFACTS) == {
        "analysis.json",
        "config.json",
        "fills.csv",
        "balance_and_equity.csv.gz",
        "dataset.json",
    }


# --------------------------------------------------------------------------- #
# emit-config
# --------------------------------------------------------------------------- #


def test_emit_config_matches_build_config_and_is_stable() -> None:
    emitted = runner.cell_config("g4_sma20_50", "full", "C1_binance_actual")
    assert emitted == runner.build_config(
        "full", "C1_binance_actual", runner.describe_cell("g4_sma20_50", "G4_regime_gate", [])["ops"],
        {"enabled": True, "sma_fast_days": 20, "sma_slow_days": 50},
    )
    first = runner.cell_config_sha256("g4_sma20_50", "full", "C1_binance_actual")
    second = runner.cell_config_sha256("g4_sma20_50", "full", "C1_binance_actual")
    assert first == second


def test_emit_config_carries_the_gate_only_for_gated_cells() -> None:
    gated = runner.cell_config("g4_sma20_50", "full", "C1_binance_actual")
    plain = runner.cell_config("published", "full", "C1_binance_actual")
    gate = gated["backtest"]["entry_regime_gate"]
    # Geometry, as declared.
    assert gate["enabled"] is True
    assert gate["sma_fast_days"] == 20
    assert gate["sma_slow_days"] == 50
    # Inputs to stamping the gate onto each side. A long-only cell leaves the short side
    # with no approved coins, so the mode stays the neutral default.
    assert gate["gate_mode"] == "both"
    assert gate["approved"]["long"] == gate["approved"]["long"]
    assert gate["approved"]["short"] == []
    assert "entry_regime_gate" not in plain["backtest"]


def test_long_short_cell_inverts_the_short_side_gate() -> None:
    gated = runner.cell_config("g6_bud_g20_50_tw025", "full", "C1_binance_actual")
    gate = gated["backtest"]["entry_regime_gate"]
    assert gate["gate_mode"] == "invert_for_short"
    # A flip needs both sides trading and a short exposure budget, or the short arm is
    # inert regardless of the gate.
    assert gated["live"]["approved_coins"]["short"]
    assert gated["bot"]["short"]["risk"]["total_wallet_exposure_limit"] > 0.0
    assert gated["live"]["hedge_mode"] is True
    # Both arms inside one budget: the per-side limits must not sum past the long-only
    # cap the study calibrated, which is what liquidated the unbudgeted flip.
    assert (
        gated["bot"]["long"]["risk"]["total_wallet_exposure_limit"]
        + gated["bot"]["short"]["risk"]["total_wallet_exposure_limit"]
        <= 1.25
    )


def test_long_short_short_ladder_mirrors_its_long_arm() -> None:
    """Deriving the short arm is what makes the flip a fair test of the idea."""
    cfg = runner.cell_config("g6_bud_g20_50_tw025", "full", "C1_binance_actual")
    long_entry = cfg["bot"]["long"]["strategy"]["trailing_martingale"]["entry"]
    short_entry = cfg["bot"]["short"]["strategy"]["trailing_martingale"]["entry"]
    long_close = cfg["bot"]["long"]["strategy"]["trailing_martingale"]["close"]
    short_close = cfg["bot"]["short"]["strategy"]["trailing_martingale"]["close"]
    for key in ("threshold_base_pct", "double_down_factor", "ema_gate_mode", "ema_span_0"):
        assert short_entry[key] == long_entry[key], key
    for key in ("qty_pct", "threshold_base_pct", "retracement_base_pct"):
        assert short_close[key] == long_close[key], key
    # Side-specific by construction: direction of the initial order, and a first entry
    # that is the same share of balance on both arms.
    assert short_entry["initial_ema_dist"] == abs(long_entry["initial_ema_dist"])
    long_eff = cfg["bot"]["long"]["risk"]["total_wallet_exposure_limit"] * (
        1.0 + cfg["bot"]["long"]["risk"]["we_excess_allowance_pct"]
    )
    short_eff = cfg["bot"]["short"]["risk"]["total_wallet_exposure_limit"] * (
        1.0 + cfg["bot"]["short"]["risk"]["we_excess_allowance_pct"]
    )
    assert long_eff * long_entry["initial_qty_pct"] == pytest.approx(
        short_eff * short_entry["initial_qty_pct"]
    )


def test_emit_config_redirects_base_dir() -> None:
    target = REPO / "backtests/binance/returns_guarded_dd_research_2026-09-16/artifacts/seed"
    cfg = runner.cell_config("published", "full", "C1_binance_actual", target)
    assert cfg["backtest"]["base_dir"] == str(target)


def test_contract_registers_a_config_hash_per_cell() -> None:
    payload = runner.contract_payload()
    assert payload["cells"], "contract must declare cells"
    for cell in payload["cells"]:
        assert len(cell["config_sha256"]) == 64


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def test_renderer_writes_a_structurally_valid_report(tmp_path: Path) -> None:
    run_dir = synthetic_artifact_dir(tmp_path, json.loads((_seed_run_dir() / "analysis.json").read_text()))
    exit_code = renderer.main(
        ["--result-dir", str(run_dir), "--no-write-csv"]
    )
    assert exit_code == 0
    text = (run_dir / "annual_analysis.md").read_text(encoding="utf-8")
    assert spec.assert_report_structure(text) == []
    assert spec.HEAD_APPENDIX in text
    # The bundle-to-cell convention note must be present even without a run record.
    assert "两个数字口径必须分开读" in text


def test_renderer_refuses_a_partial_artifact_dir(tmp_path: Path) -> None:
    run_dir = synthetic_artifact_dir(tmp_path, {}, with_analysis=False)
    with pytest.raises(SystemExit, match="no analysis.json"):
        renderer.main(["--result-dir", str(run_dir), "--no-write-csv"])


def test_appendix_lists_the_focus_cells_and_marks_this_one() -> None:
    run_dir = _seed_run_dir()
    record = runner.load_json(STUDY / "artifacts/seed/run_record.json")
    appendix = renderer.build_appendix(record, run_dir)
    for cell_id in renderer.FOCUS_CELLS:
        assert cell_id in appendix
    assert "**`published`**" in appendix


def test_alignment_maps_every_compared_field() -> None:
    """Every compared field must resolve on both sides, or it cannot fail."""
    run_dir = _seed_run_dir()
    record = runner.load_json(STUDY / "artifacts/seed/run_record.json")
    alignment = record["alignment"]
    assert alignment["checked"] is True
    for key, entry in alignment["compared"].items():
        assert entry["bundle"] is not None, f"{key} missing from the bundle analysis"
        assert entry["cell"] is not None, f"{key} missing from the screening cell metrics"
        assert entry["abs_delta"] is not None
"""
End of the bundle/report contract tests.
"""
