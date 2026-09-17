"""Shared causal daily entry-regime table (SMA fast/slow cross).

The backtest and the live runtime must agree on *when* the regime verdict for a UTC
day becomes knowable. That question is answered here once, by pure functions with no
data plumbing, so both callers feed the same numbers into the same arithmetic.

Causality contract
------------------
``on[d]`` is decided by daily closes through the end of day ``d - 1`` only:

* The verdict is an integer flag per UTC day, plus the timestamp boundaries where it
  flips. Boundaries land on the first bar of a UTC day, because a daily verdict
  cannot change inside a day.
* A day whose close is not yet known is not evidence. ``exclude_forming_last_day``
  keeps the final row of the series out of the evidence set, which is what a bar
  series needs when its last row is a UTC day still open at the series end.
* A day with no usable close is carried forward at the previous close, so a single
  missing day does not silently empty an SMA window. A window that spans a day with
  no evidence at all stays undefined, and an undefined window is risk-off.
* Before the slow window has filled there is no completed evidence, and risk-off is
  the conservative verdict.

Risk-off blocks entries; it never forces an exit. Closes, panic and auto-unstuck are
outside this module's authority.

The depth a *live* start must fetch before it can decide is decided here too
(`live_lookback_days`), so the number of completed days asked of the exchange is one
reviewed rule rather than a formula each caller re-derives.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MS_PER_UTC_DAY = 86_400_000
MS_PER_MINUTE = 60_000

REGIME_RISK_ON = 1
REGIME_RISK_OFF = 0

#: Completed daily rows a live pre-warm requests at minimum. The published 20/50 gate
#: lands here: exactly 1.2x its 50-day slow window.
LIVE_LOOKBACK_MIN_DAYS = 60
#: Completed days requested on top of what the filter needs, so one lagging or missing
#: recent daily candle cannot leave the slow SMA window undefined.
LIVE_LOOKBACK_MARGIN_DAYS = 10


@dataclass(frozen=True)
class DailyCloses:
    """Assembled closed UTC daily closes for one symbol, oldest first.

    ``day_ts`` are UTC-midnight start instants and ``closes`` carries one close per day.
    ``depth`` is what a caller reports as the assembled history: a request for more days
    than the exchange holds comes back shorter rather than failing, while the verdict
    rule needs `slow + confirm_days` of them.
    """

    day_ts: list[int]
    closes: list[float]

    @property
    def depth(self) -> int:
        return len(self.day_ts)


def live_lookback_days(slow: int, confirm_days: int = 0) -> int:
    """Completed daily rows a live pre-warm requests for one gate.

    The verdict needs ``slow + confirm_days`` completed days *in the fetched series*, so
    the request covers that plus a margin: a request that only just fills the window
    turns one lagging or missing recent day into a risk-off stretch of up to ``slow``
    days. The floor keeps the published 20/50 gate at 60 days, which is where the
    margin and the floor agree.
    """
    required = int(slow) + int(confirm_days)
    return max(LIVE_LOOKBACK_MIN_DAYS, required + LIVE_LOOKBACK_MARGIN_DAYS)


def utc_day_index(timestamps_ms) -> np.ndarray:
    """Integer UTC-day index per timestamp, from the timestamp alone."""
    minutes = np.asarray(timestamps_ms, dtype="int64") // MS_PER_MINUTE
    return (minutes // (MS_PER_UTC_DAY // MS_PER_MINUTE)).astype("int64")


def daily_closes_by_utc_day(close_values, day_index, n_days):
    """Last finite close per UTC day, plus a "day has a usable close" mask.

    Taking the last finite close makes a partial day equivalent to its close so far,
    so the caller must exclude any day that is still forming.
    """
    daily_close = np.full(n_days, np.nan, dtype="float64")
    seen = np.zeros(n_days, dtype=bool)
    finite = np.isfinite(close_values)
    if not finite.any():
        return daily_close, seen
    rows = np.flatnonzero(finite)
    row_days = np.asarray(day_index, dtype="int64")[rows]
    values = np.asarray(close_values, dtype="float64")[rows]
    order = np.argsort(row_days, kind="stable")
    row_days = row_days[order]
    values = values[order]
    boundaries = np.flatnonzero(np.diff(row_days)) + 1
    starts = np.concatenate(([0], boundaries))
    ends = np.concatenate((boundaries, [row_days.size]))
    for start, end in zip(starts, ends):
        day = int(row_days[start])
        daily_close[day] = float(values[end - 1])
        seen[day] = True
    return daily_close, seen


def regime_on_per_day(
    daily_close,
    complete,
    fast: int,
    slow: int,
    confirm_days: int = 0,
    *,
    exclude_forming_last_day: bool = True,
):
    """Risk-on flag per UTC day, decided by completed prior days only.

    ``exclude_forming_last_day`` drops the final row of the series from the evidence
    set. A backtest series ends inside a UTC day, so its last row is partial and must
    be excluded. A live series ends at the last closed day and has no forming row
    available, so the caller passes ``False`` and queries the verdict for the day in
    progress.
    """
    n_days = int(np.asarray(daily_close).size)
    on = np.zeros(n_days, dtype=bool)
    if n_days < 2:
        return on
    usable = np.asarray(complete, dtype=bool).copy()
    if exclude_forming_last_day:
        usable[-1] = False
    if not usable.any():
        return on
    filled = np.full(n_days, np.nan, dtype="float64")
    last = np.nan
    for day in range(n_days):
        if usable[day] and np.isfinite(daily_close[day]):
            last = float(daily_close[day])
        if usable[day] and np.isfinite(last):
            filled[day] = last
    valid = np.isfinite(filled)
    cumsum = np.concatenate(([0.0], np.cumsum(np.where(valid, filled, 0.0))))
    counts = np.concatenate(([0], np.cumsum(valid.astype("int64"))))
    sma_fast = np.full(n_days, np.nan, dtype="float64")
    sma_slow = np.full(n_days, np.nan, dtype="float64")
    for window, target in ((fast, sma_fast), (slow, sma_slow)):
        for end in range(window - 1, n_days):
            lo = end - window + 1
            if counts[end + 1] - counts[lo] == window:
                target[end] = (cumsum[end + 1] - cumsum[lo]) / window
    defined = np.isfinite(sma_fast) & np.isfinite(sma_slow)
    raw = defined & (sma_fast > sma_slow)
    span = max(0, int(confirm_days)) + 1
    for day in range(1, n_days):
        first = day - 1
        if first - span + 1 < 0:
            continue
        window = raw[first - span + 1 : first + 1]
        if window.size == span and bool(window.all()):
            on[day] = True
    return on


def regime_table_from_daily_closes(
    day_timestamps_ms,
    daily_closes,
    *,
    fast: int,
    slow: int,
    confirm_days: int = 0,
    exclude_forming_last_day: bool = True,
):
    """Verdict table from an already-day-bucketed series.

    ``day_timestamps_ms`` are the UTC-midnight start instants of closed days.
    Returns ``(transitions, regimes)``, ascending and starting with the first day's
    verdict.

    The days need not be consecutive. A day with no row is not evidence, so every
    SMA window containing it stays undefined and its consumers read risk-off; that
    is the same conservative answer as a gappy bar series, and it needs no extra
    guard here. Each stamp must still be a UTC midnight, because a stamp that is not
    a day boundary would place a verdict inside a day.
    """
    days = np.asarray(day_timestamps_ms, dtype="int64")
    closes = np.asarray(daily_closes, dtype="float64")
    if days.size != closes.size:
        raise ValueError("one close per UTC day is required")
    if days.size == 0:
        return [], []
    if np.any(days % MS_PER_UTC_DAY != 0):
        raise ValueError("day timestamps must be UTC midnights")
    if np.any(np.diff(days) <= 0):
        raise ValueError("day timestamps must be strictly ascending")
    index = (days // MS_PER_UTC_DAY).astype("int64")
    if index.size and int(index[0]) != 0:
        index = index - int(index[0])
    n_days = int(index[-1]) + 1 if index.size else 0
    bucketed, complete = daily_closes_by_utc_day(closes, index, n_days)
    on = regime_on_per_day(
        bucketed,
        complete,
        fast,
        slow,
        confirm_days,
        exclude_forming_last_day=exclude_forming_last_day,
    )
    transitions = []
    regimes = []
    for row in range(days.size):
        value = REGIME_RISK_ON if bool(on[int(index[row])]) else REGIME_RISK_OFF
        if not regimes or regimes[-1] != value:
            transitions.append(int(days[row]))
            regimes.append(value)
    return transitions, regimes


def regime_flag_for_day(
    day_timestamps_ms,
    daily_closes,
    query_ts_ms,
    *,
    fast: int,
    slow: int,
    confirm_days: int = 0,
) -> bool:
    """Live read path: may entries open at ``query_ts_ms``?

    The series must hold closed UTC days only, ending at the last day that has
    closed. The verdict for the UTC day containing ``query_ts_ms`` is read straight
    off the end of that series, because ``on[d]`` consumes closes through ``d - 1``
    and every close in the series precedes day ``d``.
    """
    days = np.asarray(day_timestamps_ms, dtype="int64")
    closes = np.asarray(daily_closes, dtype="float64")
    if days.size == 0:
        return False
    if days.size != closes.size:
        raise ValueError("one close per UTC day is required")
    query_day_start = (int(query_ts_ms) // MS_PER_UTC_DAY) * MS_PER_UTC_DAY
    last_day_start = int(days[-1])
    if last_day_start >= query_day_start:
        raise ValueError("daily series must end before the queried UTC day")
    # Append the queried day as an open row: it carries no close, so it cannot act as
    # evidence, and it gives `on` exactly one slot to answer for.
    extended_days = np.concatenate((days, [query_day_start]))
    extended_closes = np.concatenate((closes, [np.nan]))
    transitions, regimes = regime_table_from_daily_closes(
        extended_days,
        extended_closes,
        fast=fast,
        slow=slow,
        confirm_days=confirm_days,
        exclude_forming_last_day=False,
    )
    if not regimes:
        return False
    return bool(regimes[-1] == REGIME_RISK_ON)


def invert_regime_table(transitions, regimes):
    """Return the complement of a regime table.

    The complement is exactly ``1 - value``, so it is not the negation of the filter:
    it is the opposite filter's verdict. It stays causal because it is derived from
    the same already-completed daily closes, never from a future bar.
    """
    return list(transitions), [1 - int(value) for value in regimes]
