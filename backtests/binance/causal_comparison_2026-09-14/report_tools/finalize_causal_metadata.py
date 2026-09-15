"""Persist identities of the delivered study and its final local source files."""

import hashlib
import json
from pathlib import Path
import subprocess

import passivbot_rust
from rust_utils import verify_loaded_runtime_extension


ROOT = Path("backtests/binance/causal_comparison_2026-09-14")
SOURCE_FILES = [
    "CHANGELOG.md",
    "docs/backtesting.md",
    "docs/ai/features/strategy_runtime.md",
    "passivbot-rust/src/backtest.rs",
    "passivbot-rust/src/backtest_execution_tests.rs",
    "passivbot-rust/src/orchestrator.rs",
    "passivbot-rust/src/python.rs",
    "passivbot-rust/src/types.rs",
    "src/backtest.py",
    "src/config/schema.py",
    "src/config/validate.py",
    "src/config_utils.py",
    "src/hlcv_preparation.py",
    "src/hlcvs_override.py",
    "src/optimization/backends/gpu_backend.py",
    "src/optimization/gpu/service.py",
    "tests/test_backtest_causal_integration.py",
    "tests/test_backtest_execution_settings.py",
    "tests/test_backtest_cli_dataset_offline.py",
    "tests/test_backtest_maker_fee_override.py",
    "tests/test_hlcvs_dataset_override.py",
    "tests/test_hlcv_preparation.py",
    "tests/test_realized_loss_gate.py",
    "tests/optimization/test_gpu_service.py",
]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finalize():
    runtime = verify_loaded_runtime_extension()
    frozen = json.loads((ROOT / "frozen_inputs.json").read_text())
    identities = {
        scenario: json.loads((ROOT / scenario / "run_identity.json").read_text())
        for scenario in ("B0", "C1", "C2", "C3", "C4")
    }
    assert identities["B0"]["runtime_source_fingerprint"] == frozen["rust_source_fingerprint"]
    assert identities["B0"]["runtime_artifact_sha256"] == frozen["rust_artifact_sha256"]
    for scenario, identity in identities.items():
        if scenario != "B0":
            assert identity["runtime_source_fingerprint"] == runtime["expected_source_fingerprint"]
            assert identity["runtime_artifact_sha256"] == runtime["runtime_compiled_sha256"]
        assert identity["source_manifest_sha256"] == frozen["manifest_sha256"]
        assert identity["dataset_logical_hashes"] == frozen["dataset_logical_hashes"]
        assert identity["effective_shape"] == [1614529, 40, 4]
        assert identity["network_disabled"]
    artifacts = [
        path
        for path in ROOT.iterdir()
        if path.is_file()
        and path.suffix in (".csv", ".png", ".md", ".json")
        and path.name != "reproducibility_metadata.json"
    ]
    artifacts += list((ROOT / "report_tools").glob("*.py"))
    for scenario, identity in identities.items():
        artifacts += list((ROOT / scenario).glob("*.csv"))
        artifacts += [ROOT / scenario / "minute_equity.npy", ROOT / scenario / "run_identity.json"]
        directory = Path(identity["result_dir"])
        artifacts += [directory / name for name in ("config.json", "dataset.json", "fills.csv", "analysis.json")]
    metadata = {
        "study": "default_trailing_martingale_causal_comparison",
        "base_git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_file_sha256": {name: sha256(Path(name)) for name in SOURCE_FILES},
        "source_scope_note": "Shared files retain unrelated pre-existing edits; no changes were committed or published.",
        "original_runtime": {
            "source_fingerprint": frozen["rust_source_fingerprint"],
            "artifact_sha256": frozen["rust_artifact_sha256"],
        },
        "causal_runtime": {
            "source_fingerprint": runtime["expected_source_fingerprint"],
            "artifact_sha256": runtime["runtime_compiled_sha256"],
        },
        "dataset": {
            "manifest_sha256": frozen["manifest_sha256"],
            "shape": [1614529, 40, 4],
            "fields": ["high", "low", "close", "volume"],
            "window_start_utc": "2023-09-12T00:00:00Z",
            "window_end_exclusive_utc": "2026-09-12T00:00:00Z",
            "declared_valid_rows": 55747336,
            "matched_binance_v2_rows": 55747336,
            "mismatching_valid_rows": 0,
            "unmatched_valid_rows": 0,
            "checksummed_v2_chunks": 1320,
        },
        "scenarios": identities,
        "baseline_reproduction": json.loads((ROOT / "baseline_reproduction.json").read_text()),
        "validation": {
            "rust_library_tests_passed": 316,
            "python_focused_tests_passed": 655,
            "python_focused_tests_skipped": 63,
            "real_extension_causal_cases_passed": 20,
            "real_extension_cases_included_in_python_focused_count": True,
            "cargo_check_tests_passed": True,
            "offline_cli_cases_included_in_python_focused_count": 4,
            "ai_docs_errors": 0,
            "ai_docs_warnings": 2,
            "network_disabled_for_all_full_replays": True,
        },
        "deliverables_sha256": {
            str(path.relative_to(ROOT)): sha256(path) for path in sorted(set(artifacts))
        },
    }
    (ROOT / "reproducibility_metadata.json").write_text(
        json.dumps(metadata, indent=2, allow_nan=False) + "\n"
    )
    print("Recorded", len(metadata["deliverables_sha256"]), "artifact hashes.")


if __name__ == "__main__":
    finalize()
