from pathlib import Path

from tools.check_ai_docs import check_ai_docs


AI_DOCS_DIR = Path(__file__).resolve().parents[1] / "docs" / "ai"


def test_ai_documentation_has_no_structural_errors():
    errors = [issue for issue in check_ai_docs() if issue.level == "error"]
    assert errors == []


def test_compatibility_routes_point_to_canonical_documents():
    expected_routes = {
        "commands.md": "`runbooks/commands.md`",
        "pr_auto_review_loop.md": "`runbooks/pr_review.md`",
        "code_review_prompt.md": "`validation.md`",
        "principles.yaml": "canonical_document: docs/ai/principles.md",
    }

    for route_name, canonical_reference in expected_routes.items():
        compatibility_route = AI_DOCS_DIR / route_name
        assert compatibility_route.is_file()
        assert canonical_reference in compatibility_route.read_text(encoding="utf-8")


def test_pr_review_contract_preserves_scheduler_and_verdict_semantics():
    contract = " ".join(
        (AI_DOCS_DIR / "runbooks" / "pr_review.md").read_text(encoding="utf-8").split()
    )

    required_contracts = [
        "digests of CI and review/comment metadata",
        "exact base, head, and effective merge-base identities",
        "Scope completed-review records by reviewer and those identities",
        "The target-relative production, test, configuration, and contract diff is unchanged",
        "records the old and new heads, target SHA, inspected delta, validation",
        (
            "Re-fetch the exact base and head and recompute the effective merge base immediately "
            "before posting"
        ),
        (
            "Every completed review records the reviewer identity, exact base, head, and effective "
            "merge-base SHAs"
        ),
        "This marker records completion by that reviewer, not approval",
        (
            "A requested draft review remains advisory and uses `COMMENT` unless formal approval "
            "of the draft was explicitly requested"
        ),
        "Distinguish a valid empty decision from malformed producer output and unavailable input",
        "Reject proposals that preserve, synthesize, or reinterpret strategy intent outside Rust",
    ]

    for required_contract in required_contracts:
        assert required_contract in contract


def test_trading_contract_boundaries_remain_explicit():
    principles = " ".join((AI_DOCS_DIR / "principles.md").read_text(encoding="utf-8").split())
    architecture = " ".join((AI_DOCS_DIR / "architecture.md").read_text(encoding="utf-8").split())
    error_contract = " ".join(
        (AI_DOCS_DIR / "error_contract.md").read_text(encoding="utf-8").split()
    )

    assert "A Rust ideal-order result is atomic current intent" in principles
    assert "not identical raw-data availability" in principles
    assert "An absent ideal authorizes cancellation only within such a batch" in architecture
    assert "A malformed Rust ideal-order batch is fatal before reconciliation" in error_contract


def test_release_hygiene_trigger_is_always_routed():
    principles = " ".join((AI_DOCS_DIR / "principles.md").read_text(encoding="utf-8").split())
    router = " ".join((AI_DOCS_DIR / "README.md").read_text(encoding="utf-8").split())
    release_runbook = AI_DOCS_DIR / "runbooks" / "release.md"

    assert "50 top-level user-facing entries" in principles
    assert "14 days since the latest stable tag with at least 10" in principles
    assert "Ask for explicit permission" in principles
    assert "Version selection, release trigger, release preparation, or publication" in router
    assert release_runbook.is_file()


REPORT_SPEC = Path(__file__).resolve().parents[1] / "backtests" / "report_spec"


#: Sentences the persistence contract must keep. Each one is a rule that would silently disappear
#: from an agent's instructions if the prose were edited without re-reading it.
PERSISTENCE_CONTRACTS = (
    # the section exists at all
    "## Artifact Persistence And On-Disk Format",
    # Rule 1: one report, one run, one directory
    "The report is always named annual_analysis.md",
    "not a claim about the window length",
    "never into a separate reports tree",
    "the date on a report's folder is the date that run was produced",
    # Rule 2: fixed names and locations, figures included
    "| Deep analysis report | `<run dir>/annual_analysis.md` |",
    "| Summary figures | `<run dir>/<figure>.png` |",
    "| Per-coin fill panels | `<run dir>/fills_plots/<COIN>.png` |",
    "Figure names are the backtest's, not the study's",
    "never move a report out of its run directory",
    # Rule 3: dataset identity instead of copies
    "HLCV arrays are never copied into a study or a run directory",
    "records the identity that reproduces them",
    # Rule 4: one run per bundle
    "refuses to guess",
    # Rule 5: tracked versus local
    "Tracked evidence versus reproducible output",
    "Never paste host-specific absolute paths into tracked evidence",
    # Rule 6: enforced in code
    "assert_bundle_layout",
    # report-level rules the persisted format depends on
    "is never printed as `nan`",
    "State where the strategy was actually active",
)


def test_deep_analysis_persistence_contract_stays_routed_and_complete():
    """The on-disk format is a contract, not a convention of habit."""
    runbook = " ".join(
        (AI_DOCS_DIR / "runbooks" / "strategy_report.md").read_text(encoding="utf-8").split()
    )
    for contract in PERSISTENCE_CONTRACTS:
        assert contract in runbook, contract

    # It must stay reachable from the two places an agent actually reads first.
    router = " ".join((AI_DOCS_DIR / "README.md").read_text(encoding="utf-8").split())
    assert "where its artifacts, figures and dataset" in router

    agents = " ".join(
        (Path(__file__).resolve().parents[1] / "AGENTS.md").read_text(encoding="utf-8").split()
    )
    assert "dated run directory they describe" in agents
    assert "a layout-check failure as an incomplete bundle" in agents


def test_bundle_layout_constants_match_the_documented_names():
    """Rule 6 names the executable contract; the module must define exactly those names."""
    import sys

    if str(REPORT_SPEC) not in sys.path:
        sys.path.insert(0, str(REPORT_SPEC))
    import annual_analysis as spec

    assert spec.BUNDLE_REPORT_FILES == (
        "annual_analysis.md",
        "annual_metrics.csv",
        "monthly_metrics.csv",
        "coin_metrics.csv",
    )
    for name in (
        "analysis.json",
        "config.json",
        "dataset.json",
        "fills.csv",
        "balance_and_equity.csv.gz",
    ):
        assert name in spec.BUNDLE_LOCAL_FILES
    # The audit path is study-configured, so it must not be pinned as a run-directory file.
    assert "execution_audit.csv" not in spec.BUNDLE_LOCAL_FILES
    assert spec.BUNDLE_PLOT_DIRS == ("fills_plots",)
    assert "drawdown.png" in spec.BUNDLE_FIGURE_FILES
    assert set(spec.BUNDLE_FIGURE_GROUPS) == {"balance", "twe", "pnl", "hard_stop"}
