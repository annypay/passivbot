"""Regression coverage for the account-level wallet-exposure entry brake.

The brake scales new-entry exposure from the strategy-equity drawdown against its
running peak. It must never alter close sizing, and configs that do not enable it
must keep their pre-brake behaviour.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from backtest import _resolve_backtest_wallet_exposure_brake
from config.validate import validate_wallet_exposure_brake


def _bot_side(*, enabled: bool, start: float = 0.15, full: float = 0.45, min_scale: float = 0.25):
    return {
        "risk": {
            "wallet_exposure_brake_enabled": enabled,
            "wallet_exposure_brake_start_drawdown": start,
            "wallet_exposure_brake_full_drawdown": full,
            "wallet_exposure_brake_min_scale": min_scale,
        }
    }


def _config(long_side, short_side):
    return {"bot": {"long": long_side, "short": short_side}}


def test_brake_defaults_to_disabled_when_both_sides_are_disabled():
    config = _config(_bot_side(enabled=False), _bot_side(enabled=False))
    assert _resolve_backtest_wallet_exposure_brake(config) == {
        "enabled": False,
        "start_drawdown": 0.15,
        "full_drawdown": 0.45,
        "min_scale": 0.25,
    }


def test_brake_resolves_enabled_geometry_from_the_enabled_side():
    config = _config(
        _bot_side(enabled=True, start=0.10, full=0.40, min_scale=0.30),
        _bot_side(enabled=False),
    )
    assert _resolve_backtest_wallet_exposure_brake(config) == {
        "enabled": True,
        "start_drawdown": 0.10,
        "full_drawdown": 0.40,
        "min_scale": 0.30,
    }


def test_brake_takes_the_stricter_geometry_when_both_sides_enable_it():
    config = _config(
        _bot_side(enabled=True, start=0.20, full=0.60, min_scale=0.40),
        _bot_side(enabled=True, start=0.10, full=0.50, min_scale=0.20),
    )
    assert _resolve_backtest_wallet_exposure_brake(config) == {
        "enabled": True,
        "start_drawdown": 0.10,
        "full_drawdown": 0.50,
        "min_scale": 0.20,
    }


def test_brake_resolver_accepts_configs_without_the_fields():
    config = {"bot": {"long": {"risk": {}}, "short": {"risk": {}}}}
    assert _resolve_backtest_wallet_exposure_brake(config)["enabled"] is False


def test_brake_resolver_rejects_inverted_drawdown_geometry():
    config = _config(_bot_side(enabled=True, start=0.50, full=0.20), _bot_side(enabled=False))
    with pytest.raises(ValueError, match="start_drawdown < full_drawdown"):
        _resolve_backtest_wallet_exposure_brake(config)


def test_brake_resolver_rejects_out_of_range_min_scale():
    config = _config(_bot_side(enabled=True, min_scale=1.5), _bot_side(enabled=False))
    with pytest.raises(ValueError, match="min_scale"):
        _resolve_backtest_wallet_exposure_brake(config)


@pytest.mark.parametrize(
    "patch, error, match",
    [
        ({"wallet_exposure_brake_enabled": 1}, TypeError, "must be a bool"),
        ({"wallet_exposure_brake_start_drawdown": -0.1}, ValueError, "must be >= 0"),
        ({"wallet_exposure_brake_full_drawdown": 0.05}, ValueError, "must exceed"),
        ({"wallet_exposure_brake_min_scale": 0.0}, ValueError, r"in \(0, 1\]"),
        ({"wallet_exposure_brake_min_scale": float("nan")}, ValueError, "must be finite"),
    ],
)
def test_validate_wallet_exposure_brake_rejects_bad_values(patch, error, match):
    side = _bot_side(enabled=True)
    side["risk"].update(patch)
    config = _config(side, _bot_side(enabled=False))
    with pytest.raises(error, match=match):
        validate_wallet_exposure_brake(config)


def test_validate_wallet_exposure_brake_accepts_valid_config():
    config = _config(_bot_side(enabled=True), _bot_side(enabled=False))
    validate_wallet_exposure_brake(deepcopy(config))


def test_validate_wallet_exposure_brake_requires_a_risk_dict():
    config = {"bot": {"long": {}, "short": {}}}
    with pytest.raises(TypeError, match="bot.long.risk must be a dict"):
        validate_wallet_exposure_brake(config)
