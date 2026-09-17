"""Offline checks for the g4 "HSL on" variant study.

The study's scientific claim is that its two replay arms are the frozen parent config plus
exactly one declared change, so these tests pin the frozen artifacts and the pinned parent
hashes. No backtest runs here and nothing touches the network; a drift in the parent
config, the pinned baseline analysis or the declared delta fails immediately.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17"
TOOLS = STUDY / "report_tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import variant_spec as spec  # noqa: E402


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_pinned_parent_artifacts_still_match():
    assert spec.sha256_file(spec.SOURCE_CONFIG) == spec.SOURCE_CONFIG_SHA256
    assert spec.sha256_file(spec.SOURCE_PROFILE_INPUT) == spec.SOURCE_PROFILE_INPUT_SHA256
    assert (
        spec.sha256_file(spec.TRACKED_BASELINE_RUN / "analysis.json")
        == spec.TRACKED_BASELINE_ANALYSIS_SHA256
    )


def test_control_config_is_the_parent_with_the_output_location_moved():
    parent = load(spec.SOURCE_CONFIG)
    control = load(spec.VARIANTS_BY_KEY["hsl_off_control"].config_path)
    problems = spec.diff_subtrees(
        parent["backtest"], control["backtest"], "backtest",
        allowed=spec.ALLOWED_CONFIG_DIFF_PATHS,
    )
    for root in spec.IDENTITY_ROOTS:
        problems.extend(spec.diff_subtrees(parent[root], control[root], root))
    assert problems == []
    assert control["backtest"]["base_dir"] != parent["backtest"]["base_dir"]
    assert control["bot"]["long"]["hsl"]["enabled"] is False


def test_hsl_on_config_adds_exactly_the_declared_delta():
    parent = load(spec.SOURCE_CONFIG)
    hsl_on = load(spec.VARIANTS_BY_KEY["hsl_on"].config_path)
    allowed = tuple(item[0] for item in spec.DECLARED_DELTA)
    problems = spec.diff_subtrees(
        parent["backtest"], hsl_on["backtest"], "backtest",
        allowed=spec.ALLOWED_CONFIG_DIFF_PATHS,
    )
    for root in spec.IDENTITY_ROOTS:
        problems.extend(
            spec.diff_subtrees(parent[root], hsl_on[root], root, allowed=allowed)
        )
    assert problems == []
    assert spec.get_path(hsl_on, "bot.long.hsl.enabled") is True
    assert spec.get_path(parent, "bot.long.hsl.enabled") is False


def test_both_variants_keep_the_profile_hsl_parameters():
    for variant in spec.VARIANTS:
        config = load(variant.config_path)
        expected = dict(spec.HSL_BLOCK, enabled=variant.hsl_enabled)
        assert spec.compare_subtrees(expected, config["bot"]["long"]["hsl"], "bot.long.hsl") == []
        for key, value in spec.LIVE_ONLY_HSL.items():
            assert config["live"][key] == value


def test_variants_keep_the_frozen_contract_and_the_gate():
    for variant in spec.VARIANTS:
        config = load(variant.config_path)
        backtest = config["backtest"]
        for key, expected in (*spec.EXECUTION.items(), *spec.COSTS.items()):
            assert backtest[key] == expected
        assert backtest["exchanges"] == [spec.DATA_EXCHANGE]
        assert (backtest["start_date"], backtest["end_date"]) == spec.EVIDENCE_WINDOW
        assert backtest["cache_dir"][spec.DATA_EXCHANGE] == spec.relative(spec.FROZEN_CACHE)
        assert spec.compare_subtrees(
            spec.GATE, spec.gate_semantics(backtest["entry_regime_gate"]), "gate"
        ) == []
        assert len(backtest["coins"][spec.DATA_EXCHANGE]) == spec.EVIDENCE_COIN_COUNT


def test_variant_input_declares_the_comparison():
    payload = load(spec.VARIANT_INPUT_PATH)
    assert payload["parent_config"] == spec.relative(spec.SOURCE_CONFIG)
    assert payload["parent_config_sha256"] == spec.SOURCE_CONFIG_SHA256
    assert [item["path"] for item in payload["declared_delta"]] == [
        item[0] for item in spec.DECLARED_DELTA
    ]
    assert payload["coin_count"] == spec.EVIDENCE_COIN_COUNT
    keys = {item["key"] for item in payload["variants"]}
    assert keys == set(spec.DEFAULT_VARIANT_ORDER)
    groups = payload["comparison_metric_groups"]
    assert set(groups) == set(spec.COMPARISON_METRIC_GROUPS)
    for group, metrics in groups.items():
        assert metrics == list(spec.COMPARISON_METRIC_GROUPS[group])


def test_the_comparison_metrics_exist_in_the_tracked_baseline():
    analysis = load(spec.TRACKED_BASELINE_RUN / "analysis.json")
    missing = [key for key in spec.COMPARISON_METRIC_KEYS if key not in analysis]
    assert missing == []
    missing_hsl = [key for key in spec.HSL_METRICS if key not in analysis]
    assert missing_hsl == []