import json

import numpy as np
import pandas as pd
import pytest

from account_diagnostics import digest_json, save_json, sha256
from evidence_integrity import (
    execute_or_recover, halfyear_metrics, verify_artifact_hashes, verify_native_complete,
)


def test_consumer_rejects_changed_summary_even_with_unchanged_completion(tmp_path):
    summary = tmp_path / "summary.json"
    save_json(summary, {"funding_mdd": .2})
    original = {"summary.json": sha256(summary)}
    verify_artifact_hashes(tmp_path, original, ("summary.json",))
    save_json(summary, {"funding_mdd": .001})
    with pytest.raises(AssertionError, match="checksum differs"):
        verify_artifact_hashes(tmp_path, original, ("summary.json",))


def test_manifest_must_cover_consumed_artifacts(tmp_path):
    save_json(tmp_path / "config.json", {"x": 1})
    hashes = {"config.json": sha256(tmp_path / "config.json")}
    with pytest.raises(AssertionError, match="not covered"):
        verify_artifact_hashes(tmp_path, hashes, ("summary.json",))


def test_unidentified_incomplete_native_replay_is_not_recertified(tmp_path):
    save_json(tmp_path / "native_analysis.json", {"result": "old"})
    with pytest.raises(RuntimeError, match="lack pre-execution identity"):
        execute_or_recover(tmp_path, {"producer": "new"}, lambda: pytest.fail("must not run"))
    assert not (tmp_path / "execution_identity.json").exists()


def test_changed_producer_cannot_reuse_native_replay(tmp_path):
    save_json(tmp_path / "execution_identity.json", {"producer": "old"}, immutable=True)
    with pytest.raises(ValueError, match="immutable artifact differs"):
        execute_or_recover(tmp_path, {"producer": "new"}, lambda: pytest.fail("must not run"))


def test_preexecution_lock_is_written_before_execute(tmp_path):
    identity = {"producer": "same"}

    def fails_after_checking_lock():
        assert json.loads((tmp_path / "execution_identity.json").read_text()) == identity
        raise RuntimeError("deliberate fixture failure")

    with pytest.raises(RuntimeError, match="deliberate fixture"):
        execute_or_recover(tmp_path, identity, fails_after_checking_lock)


def test_native_manifest_rejects_execution_identity_change(tmp_path):
    save_json(tmp_path / "native_complete.json", {
        "execution_identity_sha256": digest_json({"producer": "old"}), "hashes": {},
    })
    with pytest.raises(AssertionError, match="different execution identity"):
        verify_native_complete(tmp_path, {"producer": "new"})


def test_halfyear_balance_mdd_does_not_turn_unrealized_gain_into_cash_peak():
    times = pd.to_datetime(["2024-01-01", "2024-06-30", "2024-07-01", "2024-12-31"], utc=True)
    account = pd.DataFrame({"equity": [100., 120., 120., 120.],
                            "balance": [100., 100., 100., 100.]}, index=times)
    halves = halfyear_metrics(account, 100., ["2024-01-01", "2024-07-01", "2025-01-01"])
    np.testing.assert_allclose(halves["balance_mdd"], 0.)
    np.testing.assert_allclose(halves["mdd"], 0.)
    assert halves["return"].tolist() == pytest.approx([.2, 0.])


def test_missing_halfyear_is_not_silently_dropped():
    account = pd.DataFrame({"equity": [100.], "balance": [100.]},
                            index=pd.to_datetime(["2024-01-01"], utc=True))
    with pytest.raises(ValueError, match="missing requested half-year"):
        halfyear_metrics(account, 100., ["2024-01-01", "2024-07-01", "2025-01-01"])
