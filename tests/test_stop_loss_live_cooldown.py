"""Unit tests for the live stop-loss cooldown anchor.

``_get_last_stop_loss_fill_timestamps`` only reports the newest fill this bot tagged as its stop
loss for each symbol-side; Rust owns the cooldown comparison. These tests pin the selection rules
(own tag only, newest, per symbol-side, inside the lookback) and the disabled/zero-cooldown
short-circuits, without building a live account.
"""

import types

from passivbot import Passivbot

GET_LAST_STOP_LOSS_FILLS = Passivbot._get_last_stop_loss_fill_timestamps
LOOKBACK_MINUTES = Passivbot._entry_cooldown_fill_lookback_minutes

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
NOW_MS = 1_800_000_000_000
LONG_TAG = "close_stop_loss_long"
SHORT_TAG = "close_stop_loss_short"


def _fill(
    timestamp,
    *,
    symbol=BTC,
    pside="long",
    order_type=LONG_TAG,
):
    return types.SimpleNamespace(
        timestamp=timestamp,
        symbol=symbol,
        position_side=pside,
        pb_order_type=order_type,
    )


def _minutes_ago(minutes):
    return NOW_MS - int(minutes * 60_000)


class _PnlsManager:
    """Minimal FillEventsManager stand-in: oldest-first list plus the ``start_ms`` filter."""

    def __init__(self, events):
        self._events = list(events)
        self.calls = []

    def get_events(self, start_ms=None, **kwargs):
        self.calls.append(start_ms)
        if start_ms is None:
            return list(self._events)
        return [
            event
            for event in self._events
            if int(getattr(event, "timestamp", 0) or 0) >= start_ms
        ]


class _Bot:
    """The smallest object the getter needs: no account, no full Passivbot construction."""

    def __init__(
        self,
        events,
        *,
        enabled=True,
        cooldown=1440.0,
        disabled_psides=(),
        with_manager=True,
        exchange_time=NOW_MS,
    ):
        self._pnls_manager = _PnlsManager(events) if with_manager else None
        self._enabled = enabled
        self._cooldown = cooldown
        self._disabled_psides = set(disabled_psides)
        self._exchange_time = exchange_time
        self.exchange_time_calls = 0
        self._entry_cooldown_fill_lookback_minutes = LOOKBACK_MINUTES

    def bp(self, pside, key, symbol=None):
        if key == "stop_loss_enabled":
            return self._enabled and pside not in self._disabled_psides
        if key == "stop_loss_cooldown_minutes":
            return self._cooldown
        raise KeyError(f"unexpected bot param: {key}")

    def get_exchange_time(self):
        self.exchange_time_calls += 1
        return self._exchange_time


def _manager(bot):
    return bot._pnls_manager


def test_newest_tagged_fill_wins_per_symbol_side():
    bot = _Bot(
        [
            _fill(_minutes_ago(100)),
            _fill(_minutes_ago(50)),
            _fill(_minutes_ago(10)),
            _fill(_minutes_ago(20), pside="short", order_type=SHORT_TAG),
        ]
    )

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": _minutes_ago(10), "short": _minutes_ago(20)}}


def test_ordinary_closes_do_not_start_a_cooldown():
    """A close that cannot be attributed to the stop loss deliberately anchors nothing."""
    bot = _Bot(
        [
            _fill(_minutes_ago(30), order_type="close_grid_long"),
            _fill(_minutes_ago(20), order_type="close_unstuck_long"),
            _fill(_minutes_ago(10), order_type="entry_trailing_normal_long"),
            _fill(_minutes_ago(5), order_type="close_panic_long"),
        ]
    )

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": None, "short": None}}


def test_side_suffix_must_match_the_fill_side():
    bot = _Bot(
        [
            # A long tag on the short side is malformed evidence: it must not anchor short.
            _fill(_minutes_ago(10), pside="short", order_type=LONG_TAG),
        ]
    )

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": None, "short": None}}


def test_unsuffixed_stop_loss_tag_is_not_accepted():
    """The accepted names are exactly the two engine order types, suffix included."""
    bot = _Bot([_fill(_minutes_ago(10), order_type="close_stop_loss")])

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": None, "short": None}}


def test_order_type_matching_is_case_insensitive():
    bot = _Bot([_fill(_minutes_ago(10), order_type="Close_Stop_Loss_Long")])

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": _minutes_ago(10), "short": None}}


def test_the_scan_starts_at_the_cooldown_lookback():
    bot = _Bot([_fill(_minutes_ago(10))], cooldown=1440.0)

    GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    lookback = LOOKBACK_MINUTES(1440.0)
    assert lookback == 1441
    assert _manager(bot).calls == [NOW_MS - lookback * 60_000]


def test_the_lookback_follows_the_largest_configured_cooldown():
    bot = _Bot([_fill(_minutes_ago(10))], cooldown=60.0)

    GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    lookback = LOOKBACK_MINUTES(60.0)
    assert lookback == 61
    assert _manager(bot).calls == [NOW_MS - lookback * 60_000]


def test_events_older_than_the_lookback_are_excluded():
    bot = _Bot([_fill(_minutes_ago(LOOKBACK_MINUTES(1440.0) + 10))])

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": None, "short": None}}


def test_disabled_stop_loss_returns_none_without_scanning():
    bot = _Bot([_fill(_minutes_ago(10))], enabled=False)

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": None, "short": None}}
    assert _manager(bot).calls == []


def test_zero_cooldown_returns_none_without_scanning():
    bot = _Bot([_fill(_minutes_ago(10))], cooldown=0.0)

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": None, "short": None}}
    assert _manager(bot).calls == []


def test_missing_manager_returns_none():
    bot = _Bot([_fill(_minutes_ago(10))], with_manager=False)

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": None, "short": None}}


def test_one_disabled_side_does_not_disable_the_other():
    bot = _Bot(
        [
            _fill(_minutes_ago(10)),
            _fill(_minutes_ago(11), pside="short", order_type=SHORT_TAG),
        ],
        disabled_psides=("short",),
    )

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": _minutes_ago(10), "short": None}}


def test_symbols_outside_the_request_are_not_reported():
    bot = _Bot([_fill(_minutes_ago(10), symbol=ETH)])

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC], now_ms=NOW_MS)

    assert out == {BTC: {"long": None, "short": None}}


def test_each_requested_symbol_is_anchored_independently():
    bot = _Bot(
        [
            _fill(_minutes_ago(10), symbol=BTC),
            _fill(_minutes_ago(12), symbol=ETH),
        ]
    )

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC, ETH], now_ms=NOW_MS)

    assert out == {
        BTC: {"long": _minutes_ago(10), "short": None},
        ETH: {"long": _minutes_ago(12), "short": None},
    }


def test_now_defaults_to_exchange_time():
    bot = _Bot([_fill(_minutes_ago(10))], exchange_time=NOW_MS)

    out = GET_LAST_STOP_LOSS_FILLS(bot, [BTC])

    assert bot.exchange_time_calls == 1
    assert out == {BTC: {"long": _minutes_ago(10), "short": None}}
