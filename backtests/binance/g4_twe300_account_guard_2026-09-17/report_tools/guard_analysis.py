#!/usr/bin/env python3
"""Account-guard analytics: halt windows and the "ready to go again" reading.

The engine reports the account-level guard as aggregate telemetry (`hard_stop_*`), but the
question this study is commissioned for is per-halt: how long was the account *out of the
market*, what did the engine's own cooldown claim, and how did the account behave after each
restart? Those are derived here from the arm's own equity series, fill ledger and frozen
config:

* a **flat run** is a maximal stretch where `usd_total_equity` does not move (no positions, no
  fees) and no fill happens;
* a **halt window** is a flat run that is *pinned by a panic close*: the engine's RED tier
  flattens the scope with `close_panic_*` fills, so a flat run that starts within a few
  sampling intervals after such a fill, that contains no fill of its own, is the account
  sitting out the cooldown. (An ordinary flat stretch -- a risk-off gate, or a normal
  take-profit exit -- has no panic fill in front of it and is therefore not a halt. A long
  panic link would count such idle stretches, so the link is deliberately tight.)
* a **terminal halt** is a halt window with no fill after it: the halt latched
  (`restart_after_red_policy = "threshold"` with a `no_restart_drawdown_threshold` the
  drawdown score reached) and the account never traded again. Its length is therefore the
  rest of the run, and the engine reports it as `hard_stop_duration_minutes_max`.

Two different lengths matter and are reported separately:

* the **engine's declared cooldown** (`bot.<pside>.hsl.cooldown_minutes_after_red`, and for a
  terminal halt the engine's own `hard_stop_duration_minutes_max`), which is what the config
  promises;
* the **derived out-of-market window**, which ends at the next fill and is therefore never
  shorter than the cooldown: excess minutes are the time the account stayed flat after the
  cooldown had already expired because no entry signal arrived.

Each window also reports the equity change 7 and 30 days after the resume, so "整装待发" is
measured rather than asserted.

Everything is a pure function over the run directory (equity series, fills, `config.json`) and
`analysis.json`, so the renderer and the independent verifier compute the same table from the
same inputs.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

import event_windows as events

MS_PER_MINUTE = 60_000
#: Smallest flat stretch counted as a halt (the shortest declared halt in this study is 12h).
DEFAULT_MIN_FLAT_MINUTES = 360
#: How long after a panic close the halt may start. The engine's own flatten telemetry is about
#: two minutes and the equity series is sampled every 60 minutes, so three sampling intervals
#: covers the flatten plus the sampling grid while staying far below an ordinary idle stretch.
DEFAULT_PANIC_LINK_MINUTES = 180.0
#: Duration comparison tolerance: two sampling intervals (the flat window is measured on the
#: hourly equity grid, so it can differ from the engine's exact halt by up to that much).
DEFAULT_DURATION_TOLERANCE_MINUTES = 120.0
#: Tolerance on the aggregate red-time cross-check, as a share of the expected halt minutes.
TELEMETRY_TOTAL_TOLERANCE_PCT = 0.15
FLAT_TOLERANCE_USD = 1e-6
CONFIG_NAME = "config.json"


@dataclass(frozen=True)
class HaltWindow:
    """One derived halt: the flat stretch plus what happened around it."""

    start: pd.Timestamp
    end: pd.Timestamp
    minutes: float
    equity_usd: float
    previous_peak_usd: float | None
    drawdown_at_start: float | None
    resume_equity_usd: float | None
    post_halt_return_7d: float | None
    post_halt_return_30d: float | None
    terminal: bool = False
    declared_minutes: float | None = None
    shortfall_minutes: float | None = None
    excess_minutes: float | None = None

    def row(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "minutes": self.minutes,
            "hours": self.minutes / 60.0,
            "equity_usd": self.equity_usd,
            "drawdown_at_start": self.drawdown_at_start,
            "resume_equity_usd": self.resume_equity_usd,
            "terminal": self.terminal,
            "declared_minutes": self.declared_minutes,
            "shortfall_minutes": self.shortfall_minutes,
            "excess_minutes": self.excess_minutes,
            "post_halt_return_7d": self.post_halt_return_7d,
            "post_halt_return_30d": self.post_halt_return_30d,
        }


def declared_guard_from_config(run_dir: Path) -> dict[str, Any]:
    """The guard the engine actually ran with, read from the run's own frozen config.

    This keeps the halt table a pure function of the run directory: the verifier recomputes the
    declared cooldown from the same file the engine was served instead of trusting a label.
    """
    path = Path(run_dir) / CONFIG_NAME
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    blocks = {
        pside: (((config.get("bot") or {}).get(pside) or {}).get("hsl") or {})
        for pside in ("long", "short")
    }
    enabled = [pside for pside, block in blocks.items() if block.get("enabled")]
    pside = enabled[0] if enabled else "long"
    block = blocks.get(pside) or blocks.get("long") or {}
    live = config.get("live") or {}
    return {
        "enabled_pside": pside,
        "cooldown_minutes_after_red": block.get("cooldown_minutes_after_red"),
        "red_threshold": block.get("red_threshold"),
        "restart_after_red_policy": block.get("restart_after_red_policy"),
        "no_restart_drawdown_threshold": block.get("no_restart_drawdown_threshold"),
        "ema_span_minutes": block.get("ema_span_minutes"),
        "hsl_signal_mode": live.get("hsl_signal_mode"),
        "pnls_max_lookback_days": live.get("pnls_max_lookback_days"),
        "source": CONFIG_NAME,
    }


def sampling_minutes(equity: pd.DataFrame) -> float | None:
    """The modal spacing of the equity series, which bounds every duration comparison."""
    if equity.empty or "usd_total_equity" not in equity:
        return None
    index = equity["usd_total_equity"].dropna().index
    if len(index) < 3:
        return None
    gaps = pd.Series(index).diff().dt.total_seconds().dropna() / 60.0
    gaps = gaps[gaps > 0]
    if gaps.empty:
        return None
    return float(gaps.mode().iloc[0])


def flat_runs(
    equity: pd.DataFrame,
    *,
    min_flat_minutes: float = DEFAULT_MIN_FLAT_MINUTES,
    tolerance: float = FLAT_TOLERANCE_USD,
) -> list[tuple[pd.Timestamp, pd.Timestamp, float]]:
    """Maximal stretches where the equity column does not move."""
    if equity.empty or "usd_total_equity" not in equity:
        return []
    series = equity["usd_total_equity"].dropna()
    if series.empty:
        return []
    values = series.to_numpy(dtype="float64")
    out: list[tuple[pd.Timestamp, pd.Timestamp, float]] = []
    start_index = 0
    for index in range(1, len(values) + 1):
        if index == len(values) or abs(values[index] - values[start_index]) > tolerance:
            if index - 1 > start_index:
                minutes = (
                    series.index[index - 1] - series.index[start_index]
                ).total_seconds() / 60.0
                if minutes >= min_flat_minutes:
                    out.append((series.index[start_index], series.index[index - 1], float(values[start_index])))
            start_index = index
    return out


def halt_windows(
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    *,
    min_flat_minutes: float = DEFAULT_MIN_FLAT_MINUTES,
    horizons_days: Sequence[int] = (7, 30),
    panic_marker: str = "panic",
    panic_link_minutes: float = DEFAULT_PANIC_LINK_MINUTES,
    declared_halt_minutes: float | None = None,
    terminal_halt_minutes: float | None = None,
) -> list[HaltWindow]:
    """Flat runs that an account-level panic close explains, with their resume readings.

    A flat run counts as a halt only when a panic fill happened within ``panic_link_minutes``
    before it started (the engine's flatten takes about two minutes), nothing was traded inside
    it, and -- for a complete halt -- the account traded again afterwards. A flat run with no
    fill after it is a **terminal** halt: the latched RED state never restarted, so the window
    runs to the end of the series and is compared against the engine's own
    ``hard_stop_duration_minutes_max`` instead of the configured cooldown.
    """
    if equity.empty:
        return []
    series = equity["usd_total_equity"].dropna()
    if series.empty:
        return []
    running_peak = series.cummax()
    if fills.empty or "timestamp" not in fills:
        return []
    fill_times = sorted(fills["timestamp"])
    panic_times = sorted(
        fills.loc[fills["type"].astype(str).str.contains(panic_marker), "timestamp"]
    )
    if not panic_times:
        return []
    link = pd.Timedelta(minutes=panic_link_minutes)
    out: list[HaltWindow] = []
    for start, end, value in flat_runs(equity, min_flat_minutes=min_flat_minutes):
        preceding_panic = next((timestamp for timestamp in reversed(panic_times) if timestamp <= start), None)
        if preceding_panic is None or (start - preceding_panic) > link:
            continue
        # The resume fill can coincide with the run's last flat sample (the account is still
        # flat at that instant), so only fills strictly inside the run disqualify it.
        if any(start < timestamp < end for timestamp in fill_times):
            continue
        resume = next((timestamp for timestamp in fill_times if timestamp >= end), None)
        terminal = resume is None
        if terminal:
            # The halt never restarted: the account sat flat until the run ended.
            end = max(end, series.index[-1])
        peak_before = (
            float(running_peak.loc[:start].max()) if not running_peak.loc[:start].empty else None
        )
        drawdown = None if not peak_before else max(0.0, 1.0 - value / peak_before)
        # The fill timestamp rarely coincides with an equity sample (the series is sampled
        # every `balance_sample_divider` minutes), so read the value in force at the resume.
        resume_equity = float(series.asof(resume)) if resume is not None else None
        returns: dict[int, float | None] = {}
        for horizon in horizons_days:
            target = end + pd.Timedelta(days=horizon)
            window = series.loc[end:target]
            if window.empty or not resume_equity:
                returns[horizon] = None
                continue
            returns[horizon] = float(window.iloc[-1] / resume_equity - 1.0)
        minutes = float((end - start).total_seconds() / 60.0)
        declared = terminal_halt_minutes if terminal else declared_halt_minutes
        if declared is None:
            declared = declared_halt_minutes
        shortfall = None if declared is None else max(0.0, float(declared) - minutes)
        excess = None if declared is None else max(0.0, minutes - float(declared))
        out.append(
            HaltWindow(
                start=start,
                end=end,
                minutes=minutes,
                equity_usd=value,
                previous_peak_usd=peak_before,
                drawdown_at_start=drawdown,
                resume_equity_usd=resume_equity,
                post_halt_return_7d=returns.get(7),
                post_halt_return_30d=returns.get(30),
                terminal=terminal,
                declared_minutes=None if declared is None else float(declared),
                shortfall_minutes=shortfall,
                excess_minutes=excess,
            )
        )
    return out


def flat_run_inventory(
    equity: pd.DataFrame, *, min_flat_minutes: float = DEFAULT_MIN_FLAT_MINUTES
) -> dict[str, Any]:
    """All long flat stretches, for transparency about what was *not* counted as a halt."""
    runs = flat_runs(equity, min_flat_minutes=min_flat_minutes)
    return {
        "count": len(runs),
        "total_minutes": sum(
            (end - start).total_seconds() / 60.0 for start, end, _value in runs
        ),
        "longest_minutes": max(
            ((end - start).total_seconds() / 60.0 for start, end, _value in runs), default=0.0
        ),
        "note": (
            "包含所有 ≥6 小时的权益恒定段（也含闸门 risk-off、正常止盈后的空仓期）；"
            "只有紧跟在 panic 平仓之后的段才计入停机窗口。"
        ),
    }


def readiness_summary(
    halts: Sequence[HaltWindow],
    equity: pd.DataFrame,
    analysis: dict[str, Any],
    *,
    declared: dict[str, Any] | None = None,
    tolerance_minutes: float = DEFAULT_DURATION_TOLERANCE_MINUTES,
) -> dict[str, Any]:
    """Aggregate the halt table and cross-check it against the engine's own telemetry.

    ``cross_check_problems`` only fires on relations that cannot hold if the derivation and the
    engine describe the same run: a window count that disagrees with `hard_stop_triggers`, a
    window shorter than the cooldown the guard enforced, a terminated latch the engine does not
    report (or the reverse), or a total halt time that disagrees with the engine's own
    `hard_stop_time_in_red_pct`. A window *longer* than the cooldown is expected -- it is the
    flat time until the next fill -- and is reported as ``excess_minutes`` instead.
    """
    declared = declared or {}
    series = equity["usd_total_equity"].dropna() if not equity.empty else pd.Series(dtype=float)
    span_minutes = (
        (series.index[-1] - series.index[0]).total_seconds() / 60.0 if len(series) > 1 else 0.0
    )
    total_minutes = sum(halt.minutes for halt in halts)
    complete = [halt for halt in halts if not halt.terminal]
    terminal = [halt for halt in halts if halt.terminal]
    post30 = [halt.post_halt_return_30d for halt in halts if halt.post_halt_return_30d is not None]
    post7 = [halt.post_halt_return_7d for halt in halts if halt.post_halt_return_7d is not None]
    immediate_retriggers = sum(
        1
        for previous, following in zip(halts, halts[1:])
        if (following.start - previous.end).total_seconds() / 86400.0 <= 7.0
    )
    telemetry = {
        key: value for key, value in analysis.items() if key.startswith("hard_stop_")
    }
    triggers = int(analysis.get("hard_stop_triggers") or 0)
    restarts = float(analysis.get("hard_stop_restarts") or 0.0)
    max_minutes = float(analysis.get("hard_stop_duration_minutes_max") or 0.0)
    cooldown = declared.get("cooldown_minutes_after_red")
    cooldown = None if cooldown is None else float(cooldown)
    problems: list[str] = []
    notes: list[str] = []
    if triggers and len(halts) != triggers:
        problems.append(
            f"derived {len(halts)} panic-pinned halt window(s) but the engine reports "
            f"{triggers} trigger(s)"
        )
    for halt in halts:
        if halt.shortfall_minutes is not None and halt.shortfall_minutes > tolerance_minutes:
            problems.append(
                f"halt starting {halt.start.isoformat()} lasts {halt.minutes:.0f} min, i.e. "
                f"{halt.shortfall_minutes:.0f} min less than the declared halt "
                f"{halt.declared_minutes:.0f} min, beyond the {tolerance_minutes:.0f}-minute "
                "sampling tolerance"
            )
    if cooldown is not None:
        for halt in halts:
            if halt.declared_minutes is None:
                problems.append(
                    f"halt starting {halt.start.isoformat()} has no declared halt length to "
                    f"compare against the frozen cooldown {cooldown:.0f} min"
                )
            elif not halt.terminal and abs(halt.declared_minutes - cooldown) > 1e-9:
                problems.append(
                    f"halt starting {halt.start.isoformat()} declares a "
                    f"{halt.declared_minutes:.0f}-min halt but the frozen config declares "
                    f"{cooldown:.0f} min"
                )
            elif halt.terminal and halt.declared_minutes < cooldown - 1e-9:
                problems.append(
                    f"terminal halt starting {halt.start.isoformat()} declares "
                    f"{halt.declared_minutes:.0f} min, less than the frozen cooldown "
                    f"{cooldown:.0f} min"
                )
    if terminal and restarts >= triggers:
        problems.append(
            f"derived {len(terminal)} terminal halt window(s) but the engine reports "
            f"{restarts:.0f} restart(s) for {triggers} trigger(s)"
        )
    if not terminal and restarts < triggers:
        problems.append(
            f"the engine reports {triggers - restarts:.0f} halt(s) without a restart but no "
            "terminal halt window was derived"
        )
    if triggers:
        expected_total = cooldown * len(complete) if cooldown else 0.0
        expected_total += sum(halt.minutes for halt in terminal)
        if (
            cooldown is not None
            and not terminal
            and max_minutes > cooldown + tolerance_minutes
        ):
            problems.append(
                f"the engine reports a longest halt of {max_minutes:.0f} min but no terminal "
                f"halt was derived and the declared cooldown is only {cooldown} min"
            )
        reported_total = float(analysis.get("hard_stop_time_in_red_pct") or 0.0) * span_minutes
        if expected_total > 0 and abs(reported_total - expected_total) > max(
            tolerance_minutes, expected_total * TELEMETRY_TOTAL_TOLERANCE_PCT
        ):
            problems.append(
                f"derived total halt time {expected_total:.0f} min disagrees with the engine's "
                f"time in RED {reported_total:.0f} min (hard_stop_time_in_red_pct × window)"
            )
    excess = [halt.excess_minutes for halt in complete if halt.excess_minutes is not None]
    if excess and max(excess) > tolerance_minutes:
        notes.append(
            f"{sum(1 for value in excess if value > tolerance_minutes)} of {len(complete)} "
            f"complete halt(s) stayed flat for longer than the cooldown after it expired "
            f"(max {max(excess):.0f} min): the account was waiting for an entry signal, not "
            "halted by the guard."
        )
    if terminal:
        notes.append(
            "终局停机：守护被锁存，账户在清仓后再未交易，停机窗口到回测结束为止。"
        )
    return {
        "halt_count": len(halts),
        "terminal_halt_count": len(terminal),
        "complete_halt_count": len(complete),
        "flat_run_inventory": flat_run_inventory(equity),
        "sampling_minutes": sampling_minutes(equity),
        "declared_guard": declared,
        "total_halt_minutes": total_minutes,
        "total_halt_hours": total_minutes / 60.0,
        "halt_share_of_window_pct": (
            total_minutes / span_minutes if span_minutes > 0 else None
        ),
        "mean_halt_minutes": (total_minutes / len(halts)) if halts else 0.0,
        "max_halt_minutes": max((halt.minutes for halt in halts), default=0.0),
        "idle_after_halt_minutes_mean": float(np.mean(excess)) if excess else None,
        "idle_after_halt_minutes_max": max(excess) if excess else None,
        "out_of_market_vs_declared_ratio": (
            (sum(halt.minutes for halt in complete) / (cooldown * len(complete)))
            if complete and cooldown
            else None
        ),
        "post_halt_return_7d_mean": float(np.mean(post7)) if post7 else None,
        "post_halt_return_30d_mean": float(np.mean(post30)) if post30 else None,
        "immediate_retriggers_within_7d": immediate_retriggers,
        "telemetry": telemetry,
        "cross_check_problems": problems,
        "cross_check_notes": notes,
        "halts": [halt.row() for halt in halts],
        "method": (
            "停机窗口 = 紧跟 panic 平仓（≤180 分钟，覆盖约 2 分钟的强平延迟与 60 分钟采样格）"
            "之后、权益恒定（容差 1e-6 USDT）且无成交的连续段；结束后有成交 = 完整停机，"
            "窗口止于复牌成交；结束后再无成交 = 终局停机（守护锁存），窗口止于回测结束。"
            "窗口长度与 `cooldown_minutes_after_red`（终局停机则与 hard_stop_duration_minutes_max）"
            "比较，超出部分记为停机结束后等待入场信号的空仓时间。"
        ),
    }


def build_guard_artifact(
    run_dir: Path, analysis: dict[str, Any], *, min_flat_minutes: float = DEFAULT_MIN_FLAT_MINUTES
) -> dict[str, Any]:
    """Derive the halt table for one arm from its own artifacts."""
    run_dir = Path(run_dir)
    equity = events.load_equity(run_dir)
    fills = events.load_fills(run_dir)
    declared = declared_guard_from_config(run_dir)
    declared_halt = declared.get("cooldown_minutes_after_red")
    terminal_halt = analysis.get("hard_stop_duration_minutes_max")
    halts = halt_windows(
        equity,
        fills,
        min_flat_minutes=min_flat_minutes,
        declared_halt_minutes=None if declared_halt is None else float(declared_halt),
        terminal_halt_minutes=None if terminal_halt is None else float(terminal_halt),
    )
    summary = readiness_summary(halts, equity, analysis, declared=declared)
    summary["arm_dir"] = run_dir.name
    return summary


def write_csv(rows: Sequence[dict[str, Any]], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(list(rows)).to_csv(path, index=False)
    return path
