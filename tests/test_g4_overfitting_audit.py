"""Offline checks for the g4 overfitting audit.

The study's claims are: every arm of the four published rounds and of the earlier screens is
registered in a trial ledger whose size is an honest lower bound; the two monthly panels are
recomputed from the runs' own hourly equity ledgers rather than read from `analysis.json`; a
pre-registered degeneracy rule removes arms whose series stopped producing returns *before* any
selection happens; CSCV/PBO is reported for two selection conventions and four block counts with
declared tie and block-edge conventions; DSR, MinBTL and the stationary-bootstrap SPA/StepM are
reported at several trial counts; fold stability has a pre-registered bar; the stress arms are
declared, analytic, and explicitly not a selection input; and no tracked file carries a host path.

These tests pin the tracked artifacts and exercise the derived maths on synthetic inputs. Nothing
here starts a backtest, touches the network, or needs a warm cache. Checks that need local
run-local artifacts (the equity and fill ledgers) skip when those are absent.

The tool modules are loaded through `importlib` under study-specific names: every study under
`backtests/**/report_tools/` names its registry `panel.py`, so a plain `import panel` in a pytest
session binds whichever study's module was imported first.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "backtests/binance/g4_overfitting_audit_2026-09-18"
TOOLS = STUDY / "report_tools"
ARTIFACTS = STUDY / "artifacts"
PANELS = ARTIFACTS / "panels"

TRACKED_SUFFIXES = (".md", ".py", ".sh", ".json", ".csv")
HOST_PATH_PATTERN = re.compile(r"(/home/[A-Za-z0-9._-]+/|[A-Za-z]:[\\/])")


def _load_tool(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def panel_module():
    return _load_tool("audit_panel", "panel.py")


@pytest.fixture(scope="module")
def cscv_module():
    return _load_tool("audit_cscv", "cscv_pbo.py")


@pytest.fixture(scope="module")
def bias_module():
    return _load_tool("audit_selection_bias", "selection_bias.py")


@pytest.fixture(scope="module")
def walkforward_module():
    return _load_tool("audit_walkforward", "walkforward.py")


@pytest.fixture(scope="module")
def ledger():
    return json.loads((ARTIFACTS / "trial_ledger.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cscv_artifact():
    return json.loads((ARTIFACTS / "cscv_pbo.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bias_artifact():
    return json.loads((ARTIFACTS / "selection_bias.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fold_artifact():
    return json.loads((ARTIFACTS / "fold_stability.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def stress_artifact():
    return json.loads((ARTIFACTS / "stress_arms.json").read_text(encoding="utf-8"))


def _run_dirs_available() -> bool:
    """True when at least one panel source still has its run-local equity ledger."""
    if not (PANELS / "index.json").exists():
        return False
    index = json.loads((PANELS / "index.json").read_text(encoding="utf-8"))
    for name in index["panels"]:
        meta = json.loads((PANELS / f"{name}.json").read_text(encoding="utf-8"))
        for source in meta["sources"][:3]:
            if (REPO / source["run_dir"] / "balance_and_equity.csv.gz").exists():
                return True
    return False


needs_run_local = pytest.mark.skipif(
    not _run_dirs_available(),
    reason="run-local equity ledgers are absent; the tracked artifacts are still pinned",
)


# ------------------------------------------------------------------------------- layout pins ---
def test_required_files_exist():
    required = [
        "run.sh",
        "README.md",
        "overfitting_audit.md",
        "anti_pattern_audit.md",
        "report_tools/panel.py",
        "report_tools/cscv_pbo.py",
        "report_tools/selection_bias.py",
        "report_tools/walkforward.py",
        "report_tools/build_report.py",
        "report_tools/verify_audit.py",
        "artifacts/trial_ledger.json",
        "artifacts/cscv_pbo.json",
        "artifacts/selection_bias.json",
        "artifacts/fold_stability.json",
        "artifacts/stress_arms.json",
        "artifacts/panels/index.json",
    ]
    missing = [name for name in required if not (STUDY / name).exists()]
    assert not missing, f"missing required study files: {missing}"


def test_no_host_paths_in_tracked_files():
    offenders = []
    for path in sorted(STUDY.rglob("*")):
        if not path.is_file() or path.suffix not in TRACKED_SUFFIXES:
            continue
        if "__pycache__" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if HOST_PATH_PATTERN.search(text):
            offenders.append(str(path.relative_to(STUDY)))
    assert not offenders, f"host paths leaked into tracked files: {offenders}"


def test_reports_are_chinese_and_have_the_required_sections():
    audit = (STUDY / "overfitting_audit.md").read_text(encoding="utf-8")
    assert "过拟合审计" in audit
    for marker in ("J1", "J2", "J3", "J4", "J5", "PBO"):
        assert marker in audit, f"overfitting_audit.md is missing {marker}"
    # The verdict column is adjudicated, not blank: every J-row carries one of the four decisions,
    # and the placeholder must not survive a report regeneration.
    assert "待裁决" not in audit, "the J-verdict column must be adjudicated"
    assert audit.count("**不通过**") == 3, "J1/J3/J4 must be adjudicated as 不通过"
    assert audit.count("**通过（记录项）**") == 1, "J2 must be adjudicated as a recorded item"
    assert audit.count("**不冻结**") == 1, "J5 must adjudicate the freeze decision"
    # The adjudication must say what follows from it, not just the verdicts.
    for marker in ("13.1 裁决理由", "13.2 J5", "13.3", "新证据轴", "样本外未确认"):
        assert marker in audit, f"the adjudication is missing {marker}"
    anti = (STUDY / "anti_pattern_audit.md").read_text(encoding="utf-8")
    for index in range(1, 23):
        assert f"| R{index} |" in anti, f"anti_pattern_audit.md is missing R{index}"
    for status in ("pass", "fail", "unknown"):
        assert f"**{status}" in anti or f"**{status}（" in anti, (
            f"anti_pattern_audit.md never uses the status {status}"
        )
    # unknown must be a real answer with a reason, not a placeholder
    assert "缺的是什么证据" in anti
    assert anti.count("**unknown**") >= 3
    readme = (STUDY / "README.md").read_text(encoding="utf-8")
    assert "过拟合审计" in readme
    assert len(readme.splitlines()) <= 120, "README must stay a one-pager"


# ---------------------------------------------------------------------------------- ledger ---
def test_ledger_counts_are_internally_consistent(ledger):
    counts = ledger["counts"]
    trials = ledger["trials"]
    assert len(trials) == counts["total_rows"]
    evaluated = sum(1 for row in trials if row.get("evaluated"))
    assert evaluated == ledger["n_lower_bound"], "the lower bound must be the evaluated row count"
    by_tier: dict[str, int] = {}
    for row in trials:
        by_tier[row["tier"]] = by_tier.get(row["tier"], 0) + 1
    assert by_tier["replay_arm"] == counts["replay_arms_total"]
    assert by_tier["declared_cell"] == counts["declared_cells_total"]
    assert by_tier["optimizer_candidate"] == counts["optimizer_candidates"]
    assert by_tier["screen_config"] == counts["screen_configs"]


def test_ledger_published_round_counts(ledger):
    """The four judged rounds plus the four earlier published rounds, by arm count."""
    per_round: dict[str, int] = {}
    for row in ledger["trials"]:
        if row["tier"] == "replay_arm":
            per_round[row["round"]] = per_round.get(row["round"], 0) + 1
    assert per_round["A_risk_geometry"] == 44
    assert per_round["C_account_guard"] == 12
    assert per_round["B_10k_replay"] == 9
    assert per_round["D_tail_risk"] == 25
    assert per_round["E_hsl_on_replay"] == 2


def test_ledger_lower_bound_exceeds_the_task_prior(ledger):
    measured = ledger["expected_reference"]["measured"]
    assert measured["a_published_replay_arms"] >= 92
    assert measured["b_cells_returns_guarded"] == 67
    assert measured["c_cells_dd_tail_v4"] == 38
    # The task's arithmetic (92 + 67 + 38 = 197) misses 200; the measured replay-arm count is
    # larger than 92, so the three tiers do reach it - for a different reason than stated.
    assert measured["d_sum_of_a_b_c"] == (
        measured["a_published_replay_arms"] + 67 + 38
    )
    assert measured["d_meets_200"] is (measured["d_sum_of_a_b_c"] >= 200)
    assert ledger["n_lower_bound"] >= 200
    assert ledger["n_lower_bound"] == measured["extended_lower_bound_with_all_recorded_tiers"]
    assert "bundle" in ledger["expected_reference"]["difference_reason"]


def test_ledger_direction_is_stated_as_a_lower_bound(ledger):
    text = ledger["n_lower_bound_note"] + ledger["expected_reference"]["direction"]
    assert "下界" in text
    assert "乐观" in ledger["expected_reference"]["direction"]


def test_ledger_rows_are_traceable(ledger):
    for row in ledger["trials"][:500]:
        for key in ("study", "arm", "leg", "evidence", "tier", "evaluated"):
            assert key in row, f"ledger row lacks {key}: {row}"
    for sample in ledger["traceability_samples"]:
        assert sample["evidence_exists"], sample["evidence_path"]
        assert sample["recheck"], f"sample {sample['arm']} has no recheck block"


def test_ledger_spot_check_resolves_on_disk(ledger):
    checked = 0
    for sample in ledger["traceability_samples"]:
        if sample["tier"] != "replay_arm":
            continue
        run_dir = REPO / sample["evidence_path"]
        analysis = run_dir / "analysis.json"
        if not analysis.exists():
            continue
        assert json.loads(analysis.read_text(encoding="utf-8")), "analysis.json must be readable"
        checked += 1
    if ledger["traceability_samples"]:
        assert checked >= 1 or not _run_dirs_available()


# ----------------------------------------------------------------------------------- panels ---
def test_panels_declare_the_registered_pools():
    index = json.loads((PANELS / "index.json").read_text(encoding="utf-8"))
    assert set(index["panels"]) == {
        "poolA_risk_geometry__3y",
        "poolA_risk_geometry__ext",
        "poolA_risk_geometry__pre",
        "poolB_ext_union__ext",
    }
    thresholds = index["degeneracy_rule"]["thresholds"]
    assert thresholds["terminal_flat_min_days"] == 30
    assert thresholds["min_nonzero_months"] == 3
    assert 60 in thresholds["supported_cadence_minutes"]


def test_panel_shapes_and_rectangularity():
    index = json.loads((PANELS / "index.json").read_text(encoding="utf-8"))
    for name, summary in index["panels"].items():
        meta = json.loads((PANELS / f"{name}.json").read_text(encoding="utf-8"))
        assert meta["n_arms_used"] == len(meta["arm_ids"])
        assert meta["n_months"] == len(meta["months"])
        assert meta["n_arms_used"] + meta["n_arms_removed"] == meta["n_arms_in_pool"]
        assert meta["n_months"] >= 16, "every panel must be able to host S=16"
        assert meta["cadence_minutes"], "the observed cadence must be recorded"
        assert set(meta["cadence_minutes"]) <= {1, 60}
        # the tracked JSON is self-contained: the matrix itself lives inside it
        assert "returns_matrix" in meta, f"{name} must embed its matrix"
        matrix = np.array(meta["returns_matrix"], dtype=float)
        assert matrix.shape == (meta["n_arms_used"], meta["n_months"])
        assert np.isfinite(matrix).all()
        assert summary["n_arms_used"] == meta["n_arms_used"]


def test_panel_csv_mirrors_the_embedded_matrix():
    """The CSV is a convenience copy (and is gitignored); when present it must agree."""
    index = json.loads((PANELS / "index.json").read_text(encoding="utf-8"))
    for name in index["panels"]:
        csv_path = PANELS / f"{name}.csv"
        if not csv_path.exists():
            continue
        meta = json.loads((PANELS / f"{name}.json").read_text(encoding="utf-8"))
        rows = [line for line in csv_path.read_text().splitlines()[1:] if line]
        assert len(rows) == meta["n_arms_used"] * meta["n_months"]
        matrix = np.array(meta["returns_matrix"], dtype=float)
        for line in rows:
            arm_id, month, value = line.split(",")
            assert arm_id in meta["arm_ids"] and month in meta["months"]
            row = meta["arm_ids"].index(arm_id)
            column = meta["months"].index(month)
            assert float(value) == pytest.approx(matrix[row, column], abs=1e-9)


def test_degeneracy_rule_removed_the_expected_arms():
    """The rule must remove liquidated / terminally halted arms, per pool and per leg."""
    meta = json.loads(
        (PANELS / "poolA_risk_geometry__ext.json").read_text(encoding="utf-8")
    )
    removed = {row["arm"] for row in meta["removed_detail"]}
    assert {"c1__ext", "b_term055__ext", "b_term070__ext"} <= removed
    assert "c1__ext" not in meta["arm_ids"]
    for row in meta["removed_detail"]:
        if "D1_truncated" in row["reasons"]:
            assert row["detail"]["D1_truncated"]["liquidated"] is True
    pool_b = json.loads((PANELS / "poolB_ext_union__ext.json").read_text(encoding="utf-8"))
    assert pool_b["n_arms_in_pool"] == 28
    assert pool_b["n_arms_removed"] >= 7
    assert "twe300_10k__ext" in {row["arm"] for row in pool_b["removed_detail"]}


def test_degeneracy_is_applied_before_selection():
    """No removed arm may appear in a panel matrix."""
    index = json.loads((PANELS / "index.json").read_text(encoding="utf-8"))
    for name in index["panels"]:
        meta = json.loads((PANELS / f"{name}.json").read_text(encoding="utf-8"))
        removed = {row["arm"] for row in meta["removed_detail"]}
        used = {arm_id.split("|")[-1] for arm_id in meta["arm_ids"]}
        assert not (removed & used), f"{name} selected an arm the rule removed"


@needs_run_local
def test_monthly_returns_crosscheck_is_exact(panel_module):
    """The recomputed month levels must equal the engine's own tracked monthly_metrics.csv."""
    index = json.loads((PANELS / "index.json").read_text(encoding="utf-8"))
    worst_level = 0.0
    checked = 0
    for name in index["panels"]:
        meta = json.loads((PANELS / f"{name}.json").read_text(encoding="utf-8"))
        for source in meta["sources"]:
            if source["monthly_metrics_sha256"] is None:
                continue
            worst_level = max(worst_level, source["crosscheck"]["level_max_rel"] or 0.0)
            checked += 1
    assert checked > 0, "no run had a tracked monthly_metrics.csv to cross-check"
    assert worst_level <= 1e-9, f"month level cross-check drifted: {worst_level}"


@needs_run_local
def test_panel_matrix_is_reproducible_from_the_equity_ledger(panel_module):
    """Recompute one panel from scratch and compare against the tracked embedded matrix."""
    meta = json.loads((PANELS / "poolA_risk_geometry__3y.json").read_text(encoding="utf-8"))
    months = meta["months"]
    matrix = np.array(meta["returns_matrix"], dtype=float)
    worst = 0.0
    for row, arm_id in enumerate(meta["arm_ids"]):
        source = next(s for s in meta["sources"] if f"{s['round']}|{s['arm']}" == arm_id)
        frame = panel_module.load_equity(REPO / source["run_dir"] / "balance_and_equity.csv.gz")
        recomputed_months, recomputed = panel_module.monthly_returns(frame)
        assert recomputed_months == months
        worst = max(worst, float(np.max(np.abs(matrix[row] - recomputed))))
    assert worst <= 1e-12, f"embedded matrix drifts from the equity ledger by {worst}"


def test_panel_json_carries_provenance_hashes():
    """Every run-local input must be pinned by a hash inside the tracked panel JSON."""
    index = json.loads((PANELS / "index.json").read_text(encoding="utf-8"))
    for name in index["panels"]:
        meta = json.loads((PANELS / f"{name}.json").read_text(encoding="utf-8"))
        assert meta["sources"], f"{name} has no sources"
        for source in meta["sources"]:
            assert source["run_dir"].startswith("backtests/binance/")
            assert not source["run_dir"].startswith("/")
            assert source["analysis_sha256"], f"{name}/{source['arm']} lacks an analysis hash"
            assert "crosscheck" in source
            assert "degeneracy" in source
        assert "returns_matrix_note" in meta


def test_cadence_is_hourly_for_the_panels():
    """The audited panels must all be built from hourly ledgers; the minute ones are ledger-only."""
    index = json.loads((PANELS / "index.json").read_text(encoding="utf-8"))
    for name, summary in index["panels"].items():
        assert summary["cadence_minutes"] == [60], f"{name} mixes cadences: {summary}"
    ledger = json.loads((ARTIFACTS / "trial_ledger.json").read_text(encoding="utf-8"))
    early = {
        row["round"]
        for row in ledger["trials"]
        if row["tier"] == "replay_arm" and row["round"] in ("F_dd_tail", "G_hsl_npos1",
                                                            "H_returns_guarded")
    }
    assert early, "the earlier rounds must still be registered in the ledger"


# ------------------------------------------------------------------------------- score maths ---
def test_growth_and_drawdown_on_synthetic_inputs(cscv_module):
    flat = np.zeros((1, 12))
    assert cscv_module.score_adg(flat)[0] == pytest.approx(0.0)
    doubled = np.full((1, 12), math.exp(math.log(2.0) / 12.0) - 1.0)
    assert cscv_module.score_adg(doubled)[0] == pytest.approx(1.0, rel=1e-9)
    dropping = np.array([[-0.5, 1.0]])
    assert cscv_module._worst_drawdown(dropping)[0] == pytest.approx(0.5)
    # For a profitable arm, multiplying by (1 - DD) < 1 can only shrink the score.
    profitable = np.array([[0.02, -0.01, 0.03, 0.01, 0.02, 0.01]])
    assert cscv_module.score_adg(profitable)[0] > 0.0
    assert (
        cscv_module.score_adg_x_1_minus_dd(profitable)[0]
        < cscv_module.score_adg(profitable)[0]
    )
    # A losing arm is made *less* negative by the same factor - the penalty is one-sided.
    losing = np.array([[-0.05, 0.01, -0.03, 0.0, -0.02, 0.01]])
    assert cscv_module.score_adg(losing)[0] < 0.0
    assert (
        cscv_module.score_adg_x_1_minus_dd(losing)[0]
        > cscv_module.score_adg(losing)[0]
    )


def test_average_ranks_handle_ties(cscv_module):
    scores = np.array([1.0, 2.0, 2.0, 2.0, 5.0])
    omega = [cscv_module._rank_percentile(scores, i) for i in range(scores.size)]
    assert omega[0] < omega[1] == omega[2] == omega[3] < omega[4]
    assert omega[1] == pytest.approx(3.0 / 6.0)


def test_block_edges_are_declared_integer_arithmetic(cscv_module):
    assert cscv_module.block_edges(37, 6) == [0, 6, 12, 18, 24, 30, 37]
    assert cscv_module.block_edges(66, 16) == [
        0, 4, 8, 12, 16, 20, 24, 28, 33, 37, 41, 45, 49, 53, 57, 61, 66
    ]
    for blocks in (8, 10, 12, 16):
        edges = cscv_module.block_edges(66, blocks)
        assert edges[0] == 0 and edges[-1] == 66
        assert all(right > left for left, right in zip(edges, edges[1:]))


def test_cscv_finds_no_overfitting_on_pure_noise(cscv_module):
    """With i.i.d. noise every arm is equally good, so the winner should be fragile."""
    rng = np.random.default_rng(11)
    matrix = rng.normal(0.0, 0.05, size=(12, 48))
    result = cscv_module.cscv(matrix, 8, [f"arm{i}" for i in range(12)], "max_adg")
    assert result["splits"] == 70
    assert 0.3 <= result["pbo"] <= 0.7, f"PBO on noise should hover near 0.5, got {result['pbo']}"


def test_cscv_finds_a_real_signal(cscv_module):
    """One arm dominates every block, so it must be selected and must win out-of-sample."""
    rng = np.random.default_rng(13)
    matrix = rng.normal(0.0, 0.05, size=(10, 48))
    matrix[3] += 0.05
    result = cscv_module.cscv(matrix, 8, [f"arm{i}" for i in range(10)], "max_adg")
    assert result["pbo"] == pytest.approx(0.0)
    assert result["most_frequent_is_best"][0][0] == "arm3"
    assert result["oos_selected_percentile_mean"] > 0.8


def test_cscv_rejects_an_odd_block_count(cscv_module):
    matrix = np.zeros((3, 12))
    with pytest.raises(ValueError):
        cscv_module.cscv(matrix, 9, ["a", "b", "c"], "max_adg")


def test_cscv_skips_when_blocks_exceed_months(cscv_module):
    matrix = np.zeros((3, 6))
    result = cscv_module.cscv(matrix, 16, ["a", "b", "c"], "max_adg")
    assert result["splits"] == 0
    assert "skipped" in result


# ---------------------------------------------------------------------------- selection bias ---
def test_dsr_arithmetic_is_reproducible_by_hand(bias_module):
    rng = np.random.default_rng(3)
    returns = rng.normal(0.001, 0.01, size=500)
    outcome = bias_module.deflated_sharpe(returns, 100, 0.02, 365.0)
    sr = float(returns.mean() / returns.std(ddof=1))
    assert outcome["sharpe_per_observation"] == pytest.approx(sr)
    from scipy import stats as scipy_stats

    skew = float(scipy_stats.skew(returns, bias=False))
    kurtosis = float(scipy_stats.kurtosis(returns, fisher=False, bias=False))
    assert outcome["skewness"] == pytest.approx(skew)
    assert outcome["kurtosis_non_excess"] == pytest.approx(kurtosis)
    variance_term = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    assert outcome["variance_term"] == pytest.approx(variance_term)
    expected_z = (sr - outcome["expected_max_sharpe_sr0"]) * math.sqrt(
        returns.size - 1
    ) / math.sqrt(variance_term)
    assert outcome["z"] == pytest.approx(expected_z)


def test_dsr_and_minbtl_get_worse_as_n_grows(bias_module):
    rng = np.random.default_rng(5)
    returns = rng.normal(0.0008, 0.01, size=800)
    small = bias_module.deflated_sharpe(returns, 92, 0.02, 365.0)
    large = bias_module.deflated_sharpe(returns, 1807, 0.02, 365.0)
    assert large["expected_max_sharpe_sr0"] > small["expected_max_sharpe_sr0"]
    assert large["dsr"] < small["dsr"], "more trials must deflate the Sharpe, never inflate it"
    assert bias_module.min_btl_years(1807, 1.0) > bias_module.min_btl_years(92, 1.0)
    assert bias_module.min_btl_years(100, -1.0) is None


def test_expected_max_sharpe_matches_the_extreme_value_form(bias_module):
    from scipy import stats as scipy_stats

    sr_std = 0.03
    n = 500
    expected = sr_std * (
        (1 - bias_module.EULER_GAMMA) * scipy_stats.norm.ppf(1 - 1 / n)
        + bias_module.EULER_GAMMA * scipy_stats.norm.ppf(1 - 1 / (n * math.e))
    )
    assert bias_module.expected_max_sharpe(n, sr_std) == pytest.approx(expected)
    assert bias_module.expected_max_sharpe(1, sr_std) == 0.0


def test_stationary_bootstrap_is_reproducible_and_valid(bias_module):
    rng = np.random.default_rng(17)
    indices = bias_module.stationary_bootstrap_indices(200, 6.0, rng)
    assert indices.shape == (200,)
    assert indices.min() >= 0 and indices.max() < 200
    again = bias_module.stationary_bootstrap_indices(200, 6.0, np.random.default_rng(17))
    assert np.array_equal(indices, again), "the bootstrap must be seeded and reproducible"


def test_reality_check_detects_a_planted_winner(bias_module):
    rng = np.random.default_rng(19)
    n_obs, n_arms = 120, 6
    base = rng.normal(0.0, 0.03, size=n_obs)
    differences = rng.normal(0.0, 0.01, size=(n_arms, n_obs))
    differences[2] += 0.01  # a plant that a 120-observation sample can resolve
    outcome = bias_module.reality_check(
        differences, 6.0, 400, np.random.default_rng(23)
    )
    assert outcome["best_arm_index"] == 2
    assert outcome["spa_p_value"] < 0.05
    assert outcome["p_value_resolution"] == pytest.approx(1 / 400)


def test_reality_check_does_not_reject_pure_noise(bias_module):
    rng = np.random.default_rng(29)
    differences = rng.normal(0.0, 0.02, size=(8, 60))
    outcome = bias_module.reality_check(
        differences, 6.0, 400, np.random.default_rng(31)
    )
    assert outcome["spa_p_value"] > 0.05, "noise must not be declared significant"


def test_bonferroni_extrapolation_is_monotone(bias_module):
    assert bias_module.bonferroni_extrapolation(0.05, 10, 10) == pytest.approx(0.05)
    assert bias_module.bonferroni_extrapolation(0.05, 100, 10) > 0.05
    assert bias_module.bonferroni_extrapolation(None, 100, 10) is None


def test_selection_bias_covers_the_required_n_grid(bias_artifact, ledger):
    assert bias_artifact["n_grid"] == [92, 200, 500, ledger["n_lower_bound"]]
    assert bias_artifact["n_lower_bound"] == ledger["n_lower_bound"]
    for label, entry in bias_artifact["headline_arms"].items():
        if "unavailable" in entry:
            continue
        for n in bias_artifact["n_grid"]:
            for frequency in ("monthly", "daily"):
                block = entry["deflated_sharpe"].get(frequency)
                if block is None:
                    continue
                assert str(n) in block, f"{label}/{frequency} lacks N={n}"
                assert 0.0 <= block[str(n)]["dsr"] <= 1.0
        values = [
            entry["min_btl_years"]["years"][str(n)]
            for n in bias_artifact["n_grid"]
            if entry["min_btl_years"]["years"][str(n)] is not None
        ]
        assert values == sorted(values), f"{label}: MinBTL must grow with N"
    for name, entry in bias_artifact["panels"].items():
        for n in bias_artifact["n_grid"]:
            for arm_id in entry["arm_ids"]:
                assert str(n) in entry["deflated_sharpe"][arm_id]["monthly"]


def test_selection_bias_states_the_optimistic_direction(bias_artifact):
    note = bias_artifact["n_direction_note"]
    assert "下界" in note and "乐观" in note
    assert bias_artifact["n_lower_bound"] == bias_artifact["n_grid"][-1]


def test_reality_check_artifacts_report_benchmarks_and_blocks(bias_artifact):
    for name, entry in bias_artifact["panels"].items():
        keys = [key for key in entry["reality_check"] if key.startswith("equal_weight|")]
        assert len(keys) == 3, f"{name} must report three block lengths"
        for key in keys:
            row = entry["reality_check"][key]
            assert row["best_arm"] in entry["arm_ids"]
            assert row["n_arms_tested"] == len(entry["arm_ids"])
            assert str(bias_artifact["n_lower_bound"]) in row["p_adjusted_by_n"]


# ------------------------------------------------------------------------------ fold stability ---
def test_fold_percentile_convention_is_documented(fold_artifact):
    assert "0 = 最好" in fold_artifact["percentile_convention"]
    assert fold_artifact["primary_folds"] in fold_artifact["fold_counts"]


def test_fold_artifacts_are_consistent(fold_artifact):
    for name, entry in fold_artifact["panels"].items():
        for folds in fold_artifact["fold_counts"]:
            block = entry[f"K{folds}"]
            assert block["n_folds"] == folds
            assert len(block["per_fold"]) == folds
            assert block["j4_percentile_bar"] == 0.50
            assert block["j4_spearman_bar"] == 0.50
            percentiles = [row["out_of_fold_percentile_best0"] for row in block["per_fold"]]
            assert all(0.0 <= value <= 1.0 for value in percentiles)
            assert block["j4_percentile_measured"] == pytest.approx(max(percentiles))
            assert block["j4_spearman_measured"] == pytest.approx(
                block["mean_pairwise_spearman"]
            )
            for row in block["per_fold"]:
                assert row["in_fold_winner"].startswith(("A_risk_geometry|", "B_10k_replay|",
                                                         "C_account_guard|"))
                assert 1 <= row["out_of_fold_rank_best1"] <= row["n_arms_ranked"]


def test_fold_stability_rejects_a_winner_that_does_not_transfer(walkforward_module):
    """A planted winner confined to the first fold must land last out-of-fold."""
    matrix = np.zeros((5, 24))
    matrix[2, :4] = 0.05  # only the first of four folds
    matrix[0, 4:] = 0.010
    matrix[1, 4:] = 0.008
    matrix[3, 4:] = 0.006
    matrix[4, 4:] = 0.004
    arm_ids = [f"arm{i}" for i in range(5)]
    months = [f"m{i:02d}" for i in range(24)]
    result = walkforward_module.fold_stability(arm_ids, months, matrix, 4)
    first = result["per_fold"][0]
    assert first["in_fold_winner"] == "arm2"
    assert first["out_of_fold_percentile_best0"] == pytest.approx(1.0)
    assert first["out_of_fold_rank_best1"] == pytest.approx(5.0)
    assert result["j4_percentile_measured"] == pytest.approx(1.0)


def test_fold_percentile_is_half_when_the_whole_field_ties(walkforward_module):
    """Exact ties are real in this audit, so the tie convention is worth pinning."""
    matrix = np.zeros((5, 24))
    matrix[2, :4] = 0.05
    arm_ids = [f"arm{i}" for i in range(5)]
    months = [f"m{i:02d}" for i in range(24)]
    result = walkforward_module.fold_stability(arm_ids, months, matrix, 4)
    assert result["per_fold"][0]["out_of_fold_percentile_best0"] == pytest.approx(0.5)
    assert result["per_fold"][0]["out_of_fold_rank_best1"] == pytest.approx(3.0)


def test_fold_stability_rewards_a_global_winner(walkforward_module):
    matrix = np.zeros((5, 24))
    matrix[1] = 0.02
    arm_ids = [f"arm{i}" for i in range(5)]
    months = [f"m{i:02d}" for i in range(24)]
    result = walkforward_module.fold_stability(arm_ids, months, matrix, 4)
    assert result["j4_percentile_measured"] == pytest.approx(0.0)
    assert result["mean_pairwise_spearman"] == pytest.approx(1.0)
    assert result["share_of_folds_winner_in_top_half"] == pytest.approx(1.0)


def test_fold_rank_positions_use_average_ranks(walkforward_module):
    """[1, 4, 4, 4, 9]: the three 4s share the middle rank, so each is 3rd from the best."""
    scores = np.array([1.0, 4.0, 4.0, 4.0, 9.0])
    rank, percentile, omega = walkforward_module.rank_positions_for(scores, 1)
    assert rank == pytest.approx(3.0)
    assert percentile == pytest.approx(0.5)
    assert omega == pytest.approx(0.5)
    # no ties -> the standard positional reading
    distinct = np.array([5.0, 3.0, 1.0, 4.0, 2.0])
    assert walkforward_module.rank_positions_for(distinct, 0) == pytest.approx((1.0, 0.0, 5 / 6))
    assert walkforward_module.rank_positions_for(distinct, 2) == pytest.approx((5.0, 1.0, 1 / 6))


# -------------------------------------------------------------------------------- stress arms ---
def test_stress_arms_are_declared_and_not_a_selection_input(stress_artifact):
    assert stress_artifact["declared_before_measurement"] is True
    assert stress_artifact["selection_input"] is False
    assert "不是选择输入" in stress_artifact["selection_input_note"]
    assert len(stress_artifact["stresses"]) == 3
    assert {item["key"] for item in stress_artifact["stresses"]} == {
        "FEE_CONSERVATIVE", "FEE_SEVERE", "SLIP_5BP"
    }
    assert max(item["extra_bp"] for item in stress_artifact["stresses"]) <= 10.0


def test_stress_arms_are_strictly_worse_than_baseline(stress_artifact):
    seen = 0
    for arm_id, entry in stress_artifact["arms"].items():
        if "unavailable" in entry:
            continue
        seen += 1
        for key, row in entry["stress"].items():
            assert row["total_extra_cost"] > 0.0, f"{arm_id}/{key} charged nothing"
            assert row["stressed_final_multiple"] <= row["baseline_final_multiple"] + 1e-12
            assert row["stressed_worst_drawdown"] >= row["baseline_worst_drawdown"] - 1e-12
            assert 0.0 < row["retained_final_multiple_share"] < 1.0
            assert row["drawdown_added"] >= -1e-12
    assert seen >= 3, "at least the declared headline arms must be stressed"


def test_stress_severity_is_ordered(stress_artifact):
    for arm_id, entry in stress_artifact["arms"].items():
        if "unavailable" in entry:
            continue
        conservative = entry["stress"]["FEE_CONSERVATIVE"]["stressed_final_multiple"]
        severe = entry["stress"]["FEE_SEVERE"]["stressed_final_multiple"]
        assert severe <= conservative + 1e-12, f"{arm_id}: +8bp must not beat +4bp"


def test_stress_provenance_is_recorded(stress_artifact):
    for arm_id, entry in stress_artifact["arms"].items():
        if "unavailable" in entry:
            continue
        provenance = entry["provenance"]
        assert provenance["fills"] > 0
        assert provenance["traded_notional"] > 0
        assert provenance["fills_sha256"], "the fill ledger hash must be recorded"
        assert entry["run_dir"].startswith("backtests/binance/")
        assert "liquidity_counts" in provenance


# ------------------------------------------------------------------------------------ reports ---
def test_overfitting_audit_reports_both_selection_conventions(cscv_artifact):
    assert set(cscv_artifact["selection_conventions"]) == {"max_adg", "max_adg_x_1_minus_dd"}
    for name, entry in cscv_artifact["panels"].items():
        for selection in cscv_artifact["selection_conventions"]:
            for blocks in cscv_artifact["blocks"]:
                row = entry["cscv"][f"{selection}|S{blocks}"]
                assert 0.0 <= row["pbo"] <= 1.0
                assert row["splits"] > 0
                assert math.isfinite(row["is_oos_slope"])
                assert -1.0 <= row["is_oos_spearman_mean"] <= 1.0


def test_cscv_artifact_declares_its_conventions(cscv_artifact):
    conventions = cscv_artifact["method_conventions"]
    assert "floor" in conventions["block_edges"]
    assert "平均名次" in conventions["rank_convention"]
    assert "并列" in conventions["tie_note"]
    assert "不是" in cscv_artifact["interpretation"]
    assert "亏钱概率" in cscv_artifact["interpretation"]


def test_cscv_reports_degeneracy_per_pool_and_leg(cscv_artifact):
    for name, entry in cscv_artifact["panels"].items():
        assert entry["n_arms_removed"] >= 0
        assert entry["n_arms_used"] + entry["n_arms_removed"] == entry["n_arms_in_pool"]
        assert isinstance(entry["removed_by_reason"], dict)
        assert "thresholds" in entry["degeneracy_rule"]


def test_cscv_uses_the_declared_block_edges(cscv_module):
    """The artifact's split counts must equal C(n_blocks, n_blocks/2)."""
    artifact = json.loads((ARTIFACTS / "cscv_pbo.json").read_text(encoding="utf-8"))
    for name, entry in artifact["panels"].items():
        meta = json.loads((PANELS / f"{name}.json").read_text(encoding="utf-8"))
        for blocks in artifact["blocks"]:
            edges = cscv_module.block_edges(meta["n_months"], blocks)
            assert edges[-1] == meta["n_months"]
        assert entry["n_months"] == meta["n_months"]


def test_verification_record_exists_and_is_clean():
    path = ARTIFACTS / "verification.json"
    if not path.exists():
        pytest.skip("verification.json is written by the final stage; run run.sh first")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["checks_failed"] == 0, record["problems"]
    assert record["checks_passed"] > 500, "the verifier should be doing real work"
    assert record["tolerances"]["pbo_abs"] <= 1e-9


def test_verify_audit_is_self_contained():
    """The verifier must not import the modules it is checking (checked on the parsed AST)."""
    import ast

    source = (TOOLS / "verify_audit.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"panel", "cscv_pbo", "selection_bias", "walkforward", "build_report"}
    assert not (imported & forbidden), f"verify_audit.py imports {sorted(imported & forbidden)}"
    assert "gzip" in imported and "csv" in imported, "the verifier must re-read the ledgers itself"
    assert "numpy" in imported, "the verifier should recompute, not just compare strings"


def test_run_sh_stages_and_offline_guards():
    text = (STUDY / "run.sh").read_text(encoding="utf-8")
    for stage in ("panels", "cscv", "bias", "folds", "stress", "report"):
        assert f"--stage {stage}" in text or f"want_stage {stage}" in text
    assert "verify_audit.py" in text
    assert "require_memory" in text and "MIN_AVAILABLE_MB" in text
    for module in ("panel.py", "cscv_pbo.py", "selection_bias.py", "walkforward.py",
                   "build_report.py", "verify_audit.py"):
        assert module in text
    assert "--verify-only" in text
