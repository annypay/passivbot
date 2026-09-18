import pytest

try:
    import passivbot_rust as pbr
except Exception:  # pragma: no cover
    pbr = None

pbr_is_stub = bool(getattr(pbr, "__is_stub__", False)) if pbr is not None else False


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_coin_drawdown_signal_binding_uses_caller_supplied_slot_count():
    out = pbr.hsl_coin_drawdown_signal(
        balance=100.0,
        n_positions=4,
        peak_realized=2.0,
        last_realized=-1.0,
        current_upnl=-2.5,
    )

    assert out == {
        "slot_budget": pytest.approx(25.0),
        "drawdown_usd": pytest.approx(5.5),
        "drawdown_raw": pytest.approx(0.22),
    }


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_red_episode_finalization_binding_returns_explicit_disposition():
    out = pbr.hsl_red_episode_finalization(
        restart_after_red_policy="threshold",
        stop_timestamp_ms=125_500,
        stop_equity=90.0,
        stop_peak_strategy_equity=100.0,
        previous_no_restart_peak_strategy_equity=120.0,
        drawdown_ema=0.10,
        red_threshold=0.20,
        no_restart_drawdown_threshold=0.90,
        cooldown_minutes_after_red=5.0,
    )

    assert out == {
        "no_restart_peak_strategy_equity": pytest.approx(120.0),
        "no_restart_drawdown_raw": pytest.approx(0.25),
        "no_restart_latched": False,
        "no_restart_reason": "none",
        "realized_loss_pct": pytest.approx(0.0),
        "halt_minutes": pytest.approx(5.0),
        "ladder_strikes": 1,
        "ladder_peak_equity": pytest.approx(0.0),
        "ladder_realized_pnl_peak": pytest.approx(0.0),
        "cooldown_until_ms": 425_500,
        "disposition": "cooldown",
    }


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_equity_hard_stop_rolling_peak_accepts_negative_values():
    tracker = pbr.EquityHardStopRollingPeak()

    assert tracker.update(1_000, -5.0, 1_000) == pytest.approx(-5.0)
    assert tracker.update(1_500, -2.0, 1_000) == pytest.approx(-2.0)
    assert tracker.update(2_100, -7.0, 1_000) == pytest.approx(-2.0)


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_equity_hard_stop_runtime_same_minute_recall_is_cached():
    runtime = pbr.EquityHardStopRuntime()
    kwargs = {
        "red_threshold": 0.25,
        "ema_span_minutes": 60.0,
        "tier_ratio_yellow": 0.5,
        "tier_ratio_orange": 0.75,
    }

    runtime.apply_sample(
        timestamp_ms=60_000,
        equity=100.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )
    first = runtime.apply_sample(
        timestamp_ms=120_000,
        equity=90.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )
    cached = runtime.apply_sample(
        timestamp_ms=120_500,
        equity=80.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )

    assert cached["elapsed_minutes"] == 0
    assert cached["changed"] is False
    assert cached["drawdown_raw"] == pytest.approx(first["drawdown_raw"])
    assert cached["drawdown_score"] == pytest.approx(first["drawdown_score"])
    assert runtime.drawdown_ema() == pytest.approx(first["drawdown_ema"])


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_equity_hard_stop_runtime_multi_minute_gap_matches_repeated_steps():
    kwargs = {
        "red_threshold": 0.25,
        "ema_span_minutes": 60.0,
        "tier_ratio_yellow": 0.5,
        "tier_ratio_orange": 0.75,
    }
    gap_runtime = pbr.EquityHardStopRuntime()
    iter_runtime = pbr.EquityHardStopRuntime()

    for runtime in (gap_runtime, iter_runtime):
        runtime.apply_sample(
            timestamp_ms=60_000,
            equity=100.0,
            peak_strategy_equity=100.0,
            **kwargs,
        )

    gap_step = gap_runtime.apply_sample(
        timestamp_ms=360_000,
        equity=90.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )
    for minute in range(2, 7):
        iter_runtime.apply_sample(
            timestamp_ms=minute * 60_000,
            equity=90.0,
            peak_strategy_equity=100.0,
            **kwargs,
        )

    assert gap_step["elapsed_minutes"] == 5
    assert gap_runtime.drawdown_ema() == pytest.approx(iter_runtime.drawdown_ema())


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_equity_hard_stop_runtime_can_replay_red_without_latching():
    runtime = pbr.EquityHardStopRuntime()
    kwargs = {
        "red_threshold": 0.25,
        "ema_span_minutes": 1.0,
        "tier_ratio_yellow": 0.5,
        "tier_ratio_orange": 0.75,
        "latch_red": False,
    }

    runtime.apply_sample(
        timestamp_ms=60_000,
        equity=100.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )
    red = runtime.apply_sample(
        timestamp_ms=120_000,
        equity=60.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )
    recovered = runtime.apply_sample(
        timestamp_ms=180_000,
        equity=100.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )

    assert red["tier"] == "red"
    assert red["red_latched"] is False
    assert recovered["tier"] == "green"
    assert runtime.red_latched() is False


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_equity_hard_stop_runtime_default_still_latches_red():
    runtime = pbr.EquityHardStopRuntime()
    kwargs = {
        "red_threshold": 0.25,
        "ema_span_minutes": 1.0,
        "tier_ratio_yellow": 0.5,
        "tier_ratio_orange": 0.75,
    }

    runtime.apply_sample(
        timestamp_ms=60_000,
        equity=100.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )
    red = runtime.apply_sample(
        timestamp_ms=120_000,
        equity=60.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )
    recovered = runtime.apply_sample(
        timestamp_ms=180_000,
        equity=100.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )

    assert red["tier"] == "red"
    assert red["red_latched"] is True
    assert recovered["tier"] == "red"
    assert runtime.red_latched() is True


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_equity_hard_stop_runtime_same_minute_replay_red_can_latch_current_sample():
    runtime = pbr.EquityHardStopRuntime()
    kwargs = {
        "red_threshold": 0.25,
        "ema_span_minutes": 1.0,
        "tier_ratio_yellow": 0.5,
        "tier_ratio_orange": 0.75,
    }

    runtime.apply_sample(
        timestamp_ms=60_000,
        equity=100.0,
        peak_strategy_equity=100.0,
        **kwargs,
        latch_red=False,
    )
    replay_red = runtime.apply_sample(
        timestamp_ms=120_000,
        equity=60.0,
        peak_strategy_equity=100.0,
        **kwargs,
        latch_red=False,
    )
    current_red = runtime.apply_sample(
        timestamp_ms=120_500,
        equity=60.0,
        peak_strategy_equity=100.0,
        **kwargs,
    )

    assert replay_red["tier"] == "red"
    assert replay_red["red_latched"] is False
    assert current_red["tier"] == "red"
    assert current_red["red_latched"] is True
    assert runtime.red_latched() is True


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_hsl_validate_halt_ladder_binding_accepts_disabled_and_valid_ladders():
    assert pbr.hsl_validate_halt_ladder([]) is None
    assert pbr.hsl_validate_halt_ladder([720.0, 1440.0]) is None
    # A zero rung means "no cooldown", not "ladder disabled".
    assert pbr.hsl_validate_halt_ladder([0.0]) is None
    assert pbr.hsl_validate_halt_ladder([720.0] * 32) is None


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
@pytest.mark.parametrize(
    "ladder",
    [
        [-1.0],
        [720.0, float("nan")],
        [float("inf")],
        [720.0] * 33,
    ],
)
def test_hsl_validate_halt_ladder_binding_rejects_invalid_ladders(ladder):
    with pytest.raises(ValueError):
        pbr.hsl_validate_halt_ladder(ladder)


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_hsl_ladder_cycle_observe_binding_matches_contract():
    started = pbr.hsl_ladder_cycle_observe(
        equity=100.0,
        realized_pnl=0.0,
        strikes=3,
        peak_equity=0.0,
        realized_pnl_peak=0.0,
    )
    assert started == {
        "reset": True,
        "strikes": 0,
        "peak_equity": pytest.approx(100.0),
        "realized_pnl_peak": pytest.approx(0.0),
    }

    holding = pbr.hsl_ladder_cycle_observe(
        equity=90.0,
        realized_pnl=-4.0,
        strikes=2,
        peak_equity=100.0,
        realized_pnl_peak=1.0,
    )
    assert holding == {
        "reset": False,
        "strikes": 2,
        "peak_equity": pytest.approx(100.0),
        "realized_pnl_peak": pytest.approx(1.0),
    }

    ratcheted = pbr.hsl_ladder_cycle_observe(
        equity=95.0,
        realized_pnl=3.0,
        strikes=2,
        peak_equity=100.0,
        realized_pnl_peak=1.0,
    )
    assert ratcheted["reset"] is False
    assert ratcheted["realized_pnl_peak"] == pytest.approx(3.0)

    with pytest.raises(ValueError):
        pbr.hsl_ladder_cycle_observe(
            equity=0.0,
            realized_pnl=0.0,
            strikes=0,
            peak_equity=0.0,
            realized_pnl_peak=0.0,
        )
    with pytest.raises(ValueError):
        pbr.hsl_ladder_cycle_observe(
            equity=100.0,
            realized_pnl=float("nan"),
            strikes=0,
            peak_equity=100.0,
            realized_pnl_peak=0.0,
        )


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_hsl_red_episode_finalization_binding_applies_ladder_and_loss_basis():
    out = pbr.hsl_red_episode_finalization(
        restart_after_red_policy="threshold",
        stop_timestamp_ms=200_000,
        stop_equity=90.0,
        stop_peak_strategy_equity=100.0,
        previous_no_restart_peak_strategy_equity=100.0,
        drawdown_ema=0.05,
        red_threshold=0.20,
        no_restart_drawdown_threshold=0.90,
        cooldown_minutes_after_red=5.0,
        halt_ladder_minutes=[720.0, 1440.0],
        ladder_strikes=1,
        ladder_peak_equity=1000.0,
        ladder_realized_pnl_peak=0.0,
        realized_pnl_now=-100.0,
        realized_loss_budget_pct=0.10,
    )

    assert out["halt_minutes"] == pytest.approx(1440.0)
    assert out["ladder_strikes"] == 2
    assert out["ladder_peak_equity"] == pytest.approx(1000.0)
    assert out["ladder_realized_pnl_peak"] == pytest.approx(0.0)
    assert out["realized_loss_pct"] == pytest.approx(0.10)
    assert out["no_restart_reason"] == "realized_loss"
    assert out["no_restart_latched"] is True
    assert out["cooldown_until_ms"] is None
    assert out["disposition"] == "no_restart"


@pytest.mark.skipif(
    pbr is None or pbr_is_stub,
    reason="passivbot_rust extension not available",
)
def test_hsl_red_episode_finalization_binding_empty_ladder_keeps_flat_cooldown():
    out = pbr.hsl_red_episode_finalization(
        restart_after_red_policy="threshold",
        stop_timestamp_ms=200_000,
        stop_equity=90.0,
        stop_peak_strategy_equity=100.0,
        previous_no_restart_peak_strategy_equity=100.0,
        drawdown_ema=0.05,
        red_threshold=0.20,
        no_restart_drawdown_threshold=0.90,
        cooldown_minutes_after_red=5.0,
        halt_ladder_minutes=[],
        ladder_strikes=4,
        ladder_peak_equity=0.0,
        ladder_realized_pnl_peak=0.0,
        realized_pnl_now=0.0,
        realized_loss_budget_pct=0.0,
    )

    assert out["halt_minutes"] == pytest.approx(5.0)
    assert out["cooldown_until_ms"] == 500_000
    assert out["ladder_strikes"] == 5
    assert out["realized_loss_pct"] == pytest.approx(0.0)
    assert out["no_restart_reason"] == "none"
