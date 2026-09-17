"""Entry-regime gate: causal construction, config validation, and semantics.

The gate is a precomputed, strictly causal daily-SMA filter that may only *block*
entries. These tests pin the properties that make it safe to enable in a backtest:

- the daily regime in force during UTC day D is decided by completed closes up to
  the end of day D-1, so no bar can read its own day's close;
- an absent or disabled block is a no-op;
- invalid geometry fails loudly rather than silently disabling the filter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import backtest  # noqa: E402
import entry_regime  # noqa: E402

MINUTES_PER_DAY = 1440


def _series(daily_closes: list[float]) -> tuple[np.ndarray, np.ndarray]:
    """Minute timestamps and closes whose daily close equals ``daily_closes``."""
    timestamps = (
        np.arange(len(daily_closes) * MINUTES_PER_DAY, dtype="int64") * 60_000
    )
    closes = np.repeat(np.asarray(daily_closes, dtype="float64"), MINUTES_PER_DAY)
    return timestamps, closes


class TestConfigResolution:
    def test_absent_block_is_disabled(self):
        assert backtest._entry_regime_gate_config({}) is None
        assert backtest._entry_regime_gate_config({"backtest": {}}) is None

    def test_disabled_block_is_ignored(self):
        config = {"backtest": {"entry_regime_gate": {"enabled": False}}}
        assert backtest._entry_regime_gate_config(config) is None

    def test_defaults_are_30_60_blocking_both(self):
        config = {"backtest": {"entry_regime_gate": {"enabled": True}}}
        resolved = backtest._entry_regime_gate_config(config)
        assert resolved is not None
        assert resolved["fast"] == 30
        assert resolved["slow"] == 60
        assert resolved["block_initial"] is True
        assert resolved["block_reentry"] is True
        assert resolved["confirm_days"] == 0

    def test_inverted_window_is_rejected(self):
        config = {
            "backtest": {
                "entry_regime_gate": {
                    "enabled": True,
                    "sma_fast_days": 60,
                    "sma_slow_days": 30,
                }
            }
        }
        with pytest.raises(ValueError, match="sma_fast_days < sma_slow_days"):
            backtest._entry_regime_gate_config(config)

    def test_non_integer_window_is_rejected(self):
        config = {
            "backtest": {
                "entry_regime_gate": {
                    "enabled": True,
                    "sma_fast_days": "many",
                    "sma_slow_days": 60,
                }
            }
        }
        with pytest.raises(ValueError, match="must be integers"):
            backtest._entry_regime_gate_config(config)


class TestCausality:
    """The regime for day D may only use closes through day D-1."""

    def test_regime_ignores_its_own_day_close(self):
        # Layout: 11 strictly rising days, then one catastrophic day.
        #   day 10 is the last rising day (close 200.0)
        #   day 11 is the crash (close 1.0)
        #   day 12 only exists so day 11 counts as a *completed* day.
        rising = [100.0 + 10.0 * index for index in range(11)]
        daily = rising + [1.0, 500.0]
        crash_day = len(rising)  # 11
        assert daily[crash_day] == 1.0

        timestamps, closes = _series(daily)
        transitions, regimes = backtest._entry_regime_transitions(
            timestamps, closes, fast=3, slow=5
        )
        crash_start_ms = int(timestamps[crash_day * MINUTES_PER_DAY])

        # Causally the crash day's own close is not yet evidence, so the regime it
        # starts under is decided by days 6..10: sma3 = 190 > sma5 = 180 -> risk-on.
        # A peeking filter would have already flipped risk-off here.
        assert _replay(transitions, regimes, crash_start_ms) is True, (
            "the crash day's own close must not be observable on that day"
        )
        # The crash only becomes evidence for the following day.
        next_day_ms = crash_start_ms + 24 * 60 * 60 * 1000
        assert _replay(transitions, regimes, next_day_ms) is False, (
            "a completed crash day must flip the regime on the following day"
        )

    def test_boundaries_after_the_first_land_on_utc_midnights(self):
        """A daily regime can only change at a day boundary.

        The first boundary may anchor at the series' first bar, because a series can start
        mid-day; every later one must be a UTC midnight.
        """
        daily = [100.0 + 10.0 * (index % 5) for index in range(40)]
        timestamps, closes = _series(daily)
        transitions, _regimes = backtest._entry_regime_transitions(
            timestamps, closes, fast=3, slow=6
        )
        assert transitions, "expected at least one boundary"
        for position, stamp in enumerate(transitions):
            if position == 0:
                assert stamp == int(timestamps[0]) or stamp % (MINUTES_PER_DAY * 60_000) == 0
                continue
            assert stamp % (MINUTES_PER_DAY * 60_000) == 0, (
                f"boundary {stamp} is not a UTC midnight"
            )

    def test_inverted_table_is_the_exact_complement(self):
        """The short side must be gated off exactly when the long side is gated on."""
        daily = [100.0 + 10.0 * (index % 7) for index in range(40)]
        timestamps, closes = _series(daily)
        transitions, regimes = backtest._entry_regime_transitions(
            timestamps, closes, fast=3, slow=6
        )
        inv_transitions, inv_regimes = backtest._invert_regime_table(transitions, regimes)
        assert inv_transitions == list(transitions)
        assert inv_regimes == [1 - int(value) for value in regimes]
        assert all(value in (0, 1) for value in inv_regimes)

    def test_complement_is_causal_too(self):
        """Inverting a causal table keeps it causal: same instants, no new information."""
        daily = [100.0 + 10.0 * index for index in range(11)] + [1.0, 500.0]
        timestamps, closes = _series(daily)
        transitions, regimes = backtest._entry_regime_transitions(
            timestamps, closes, fast=3, slow=5
        )
        inv_transitions, inv_regimes = backtest._invert_regime_table(transitions, regimes)
        crash_day = 11
        crash_start_ms = int(timestamps[crash_day * MINUTES_PER_DAY])
        # The crash day's close is not yet evidence, so at its start the short side must be
        # blocked (the long side's regime is on, so its complement is off).
        assert _replay(inv_transitions, inv_regimes, crash_start_ms) is False
        next_day_ms = crash_start_ms + 24 * 60 * 60 * 1000
        assert _replay(inv_transitions, inv_regimes, next_day_ms) is True

    def test_a_gap_voids_every_window_that_still_contains_it(self):
        """A missing day must not be skipped or forward-filled inside an SMA window.

        Forward-filling a zero would move the average; dropping the day would shorten the
        window. Either would quietly answer a different question than the declared one, so
        a window containing the gap must produce no signal. The gate is prior-day based, so
        the effect appears one day after the gap and lasts while the gap stays in range.
        """
        fast, slow = 3, 5
        daily = [100.0 + 5.0 * index for index in range(24)]
        timestamps, closes = _series(daily)
        gap_day = 12
        start = gap_day * MINUTES_PER_DAY
        closes[start : start + MINUTES_PER_DAY] = float("nan")

        transitions, regimes = backtest._entry_regime_transitions(
            timestamps, closes, fast=fast, slow=slow
        )

        def state_on(day: int) -> bool:
            return _replay(transitions, regimes, int(timestamps[day * MINUTES_PER_DAY]))

        # The gap day's own close is missing, so the day that follows is the first to see
        # it inside the window. The signal must then stay off while the gap remains in
        # range: a skipped or zero-filled day would keep it on.
        for day in range(gap_day + 1, gap_day + slow):
            assert state_on(day) is False, f"day {day} still has the gap inside the window"

        # The signal must come back once the gap has left the window, which proves it was
        # treated as absent evidence rather than as a permanent regime change. The exact
        # recovery day depends on how the series start aligns with UTC days, so a range is
        # asserted instead of a single day.
        recovery = [
            day
            for day in range(gap_day + slow, gap_day + slow + 3)
            if state_on(day)
        ]
        assert recovery, "the signal never recovered after the gap left the window"

    def test_first_days_are_risk_off(self):
        daily = [100.0] * 20
        timestamps, closes = _series(daily)
        transitions, regimes = backtest._entry_regime_transitions(
            timestamps, closes, fast=3, slow=5
        )
        # Flat prices never produce fast > slow, so every boundary is risk-off.
        assert all(regime == 0 for regime in regimes)

    def test_transitions_are_strictly_ascending(self):
        daily = [100.0 + (index % 7) for index in range(40)]
        timestamps, closes = _series(daily)
        transitions, regimes = backtest._entry_regime_transitions(
            timestamps, closes, fast=3, slow=6
        )
        assert transitions == sorted(transitions)
        assert len(set(transitions)) == len(transitions)
        assert len(transitions) == len(regimes)

    def test_boundaries_land_on_utc_day_starts(self):
        daily = [100.0 + (index % 5) for index in range(30)]
        timestamps, closes = _series(daily)
        transitions, _regimes = backtest._entry_regime_transitions(
            timestamps, closes, fast=2, slow=4
        )
        for stamp in transitions:
            assert stamp % (MINUTES_PER_DAY * 60_000) == 0


def _replay(transitions: list[int], regimes: list[int], stamp: int) -> bool:
    state = False
    for transition, regime in zip(transitions, regimes):
        if transition <= stamp:
            state = bool(regime)
        else:
            break
    return state


class TestGateApplication:
    def _bot_params(self, coins: list[str]) -> list[dict]:
        return [{"long": {}, "short": {}} for _ in coins]

    def test_gate_is_attached_to_both_sides(self):
        coins = ["AAA", "BBB"]
        daily = [100.0 + (index % 6) for index in range(40)]
        timestamps, closes = _series(daily)
        hlcvs = np.zeros((closes.size, len(coins), 4), dtype="float64")
        hlcvs[:, :, 2] = closes[:, None]

        params = self._bot_params(coins)
        summary = backtest._apply_entry_regime_gate(
            params,
            coins,
            hlcvs,
            timestamps,
            {
                "fast": 2,
                "slow": 4,
                "block_initial": True,
                "block_reentry": False,
                "require_long_only": False,
                "confirm_days": 0,
            },
        )
        assert summary["enabled"] is True
        for entry in params:
            for pside in ("long", "short"):
                gate = entry[pside]["entry_regime_gate"]
                assert gate["enabled"] is True
                assert gate["block_initial"] is True
                assert gate["block_reentry"] is False
                assert gate["transition_ts"] == sorted(gate["transition_ts"])

    def test_require_long_only_leaves_short_ungated(self):
        coins = ["AAA"]
        daily = [100.0 + (index % 6) for index in range(40)]
        timestamps, closes = _series(daily)
        hlcvs = np.zeros((closes.size, 1, 4), dtype="float64")
        hlcvs[:, 0, 2] = closes

        params = self._bot_params(coins)
        backtest._apply_entry_regime_gate(
            params,
            coins,
            hlcvs,
            timestamps,
            {
                "fast": 2,
                "slow": 4,
                "block_initial": True,
                "block_reentry": True,
                "require_long_only": True,
                "confirm_days": 0,
            },
        )
        assert "entry_regime_gate" in params[0]["long"]
        assert "entry_regime_gate" not in params[0]["short"]

    def test_mismatched_lengths_fail_loudly(self):
        coins = ["AAA", "BBB"]
        timestamps, closes = _series([100.0] * 10)
        hlcvs = np.zeros((closes.size, len(coins), 4), dtype="float64")
        hlcvs[:, :, 2] = closes[:, None]
        # One bot-params entry for two coins: the gate must refuse rather than
        # silently gate only the first coin.
        with pytest.raises(ValueError, match="one bot-params entry per coin"):
            backtest._apply_entry_regime_gate(
                self._bot_params(["AAA"]),
                coins,
                hlcvs,
                timestamps,
                {
                    "fast": 2,
                    "slow": 4,
                    "block_initial": True,
                    "block_reentry": True,
                    "require_long_only": False,
                    "confirm_days": 0,
                },
            )

    def test_non_3d_hlcvs_fail_loudly(self):
        timestamps, closes = _series([100.0] * 10)
        with pytest.raises(ValueError, match="3-D HLCV array"):
            backtest._apply_entry_regime_gate(
                self._bot_params(["AAA"]),
                ["AAA"],
                closes,
                timestamps,
                {
                    "fast": 2,
                    "slow": 4,
                    "block_initial": True,
                    "block_reentry": True,
                    "require_long_only": False,
                    "confirm_days": 0,
                },
            )


class TestLiveWarmup:
    """The live pre-warm depth and the threshold it must clear.

    A live start has to fetch the daily evidence itself, so the depth it asks for is a
    reviewed rule: enough completed days for the verdict, plus a margin. The threshold
    itself is `slow + confirm_days` completed days *in the fetched series*, which is what
    these cases pin.
    """

    def test_published_gate_asks_for_sixty_days(self):
        assert entry_regime.live_lookback_days(50) == 60

    @pytest.mark.parametrize(
        "slow,confirm_days,expected",
        [
            (10, 0, 60),
            (30, 3, 60),
            (50, 0, 60),
            (60, 0, 70),
            (100, 20, 130),
            (200, 5, 215),
        ],
    )
    def test_the_request_always_exceeds_what_the_filter_needs(
        self, slow, confirm_days, expected
    ):
        assert entry_regime.live_lookback_days(slow, confirm_days) == expected
        assert expected > slow + confirm_days

    @pytest.mark.parametrize(
        "n_days,confirm_days,expected",
        [
            (49, 0, False),
            (50, 0, True),
            (54, 5, False),
            (55, 5, True),
        ],
    )
    def test_the_verdict_needs_slow_plus_confirmation_completed_days(
        self, n_days, confirm_days, expected
    ):
        day_ms = entry_regime.MS_PER_UTC_DAY
        today = 20_000 * day_ms
        days = [today - (n_days - index) * day_ms for index in range(n_days)]
        closes = [100.0 + index for index in range(n_days)]
        verdict = entry_regime.regime_flag_for_day(
            days,
            closes,
            today + 3_600_000,
            fast=20,
            slow=50,
            confirm_days=confirm_days,
        )
        assert verdict is expected

    def test_the_container_reports_the_assembled_depth(self):
        series = entry_regime.DailyCloses([0, 86_400_000], [1.0, 2.0])
        assert series.depth == 2
        assert series.closes == [1.0, 2.0]
