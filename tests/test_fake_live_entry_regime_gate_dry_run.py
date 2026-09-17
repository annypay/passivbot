"""Offline dry run: the entry-regime gate against the fake exchange.

No network, no credentials, no exchange account. The tape is five UTC days of 1-minute
candles — two falling days, then three rising ones — and a run *starts* at the minute
being probed (`boot_index`), so no cycle loop is needed and the file takes seconds.

What this file proves end to end, through the real planning path:

1. **No daily history yet** (the tape's first minutes): the gate publishes a risk-off row
   and records the symbol as unavailable instead of opening a position — the fail-closed
   branch.
2. **A window the tape cannot fill**: risk-off, not an error, and no position.
3. **A 60-day warm-up** (a sparse one-candle-per-UTC-midnight tape): the boot pre-warm
   assembles the published depth and publishes a risk-on verdict *before* `bot.ready`, so
   the first cycle never plans without one.

What it deliberately does not attempt, and where that is covered instead:

- The *filled-leg* comparison (risk-off blocks an entry the same tape would otherwise
  take, risk-on allows it) is asserted at engine level in
  `tests/test_entry_regime_gate_engine_parity.py`. Driving it through this harness needs
  the fake clock to sit on a day boundary days away from the wall clock, and the staged
  live planner refuses that: `RuntimeError: staged planner precondition failed before
  market snapshot refresh: missing current-epoch surfaces=balance,fills,open_orders,
  positions` (`src/live/planning_gates.py`). That is a harness/clock limitation, not a
  gate behaviour, and it is recorded in `issue/0001-entry-regime-gate-live-wiring.md`.
- The manual smoke that shows the gate publishing into a longer tape lives in
  `scenarios/fake_live/entry_regime_gate_wiring_smoke.hjson` with
  `configs/fake_live_entry_regime_gate.hjson`.
"""

from __future__ import annotations

import hjson
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from tools.run_fake_live import _run_fake_case  # noqa: E402

SYMBOL = "BTC/USDT:USDT"
CONFIG_PATH = REPO / "configs" / "fake_live_entry_regime_gate.hjson"
MINUTE_MS = 60_000
MINUTES_PER_DAY = 1_440
# 2026-03-02T00:00:00Z: a UTC midnight, so day boundaries are unambiguous.
START_MS = 1_772_409_600_000
FALLING_DAYS = 2
TOTAL_DAYS = 5
NOON_MINUTE = 12 * 60
BOOT_EARLY = 2
BOOT_RISING = (TOTAL_DAYS - 1) * MINUTES_PER_DAY + NOON_MINUTE
# A sparse tape: one candle per UTC midnight, so the fake exchange's 1d aggregation is
# exactly the daily series the gate reads. 65 rows leave 64 closed days behind the boot
# row, more than the 60-day warm-up window.
DAILY_DAYS = 65
BOOT_LAST_DAY = DAILY_DAYS - 1


def _replay_candles() -> list[list]:
    """One candle per minute: two falling days, then three rising ones."""
    rows: list[list] = []
    price = 100.0
    for minute in range(TOTAL_DAYS * MINUTES_PER_DAY):
        day = minute // MINUTES_PER_DAY
        drift = (-0.010 if day < FALLING_DAYS else 0.010) / MINUTES_PER_DAY
        open_price = price
        price = round(price * (1.0 + drift), 6)
        rows.append(
            [
                START_MS + minute * MINUTE_MS,
                open_price,
                max(open_price, price),
                min(open_price, price),
                price,
                1.0,
            ]
        )
    return rows


def _daily_replay_candles(days: int = DAILY_DAYS) -> list[list]:
    """One rising candle per UTC midnight, spaced a full day apart."""
    rows: list[list] = []
    price = 100.0
    for day in range(days):
        open_price = price
        price = round(price * 1.01, 6)
        rows.append(
            [
                START_MS + day * MINUTE_MS * MINUTES_PER_DAY,
                open_price,
                max(open_price, price),
                min(open_price, price),
                price,
                1.0,
            ]
        )
    return rows


def _write_config(tmp_path: Path, *, gate_enabled: bool, **gate_overrides) -> Path:
    config = hjson.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    gate = config["live"]["entry_regime_gate"]
    gate["enabled"] = bool(gate_enabled)
    gate.update(gate_overrides)
    suffix = "_".join(f"{key}{value}" for key, value in gate_overrides.items())
    path = tmp_path / f"config_{'on' if gate_enabled else 'off'}{'_' + suffix if suffix else ''}.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _write_scenario(tmp_path: Path, *, boot_index: int) -> Path:
    scenario = {
        "name": "entry_regime_gate_dry_run",
        "exchange": "fake",
        "boot_index": int(boot_index),
        "account": {"balance": 10_000.0},
        "symbols": {
            SYMBOL: {
                "price_step": 0.01,
                "qty_step": 0.001,
                "min_qty": 0.001,
                "min_cost": 5.0,
                "maker_fee": 0.0002,
                "taker_fee": 0.00055,
            }
        },
        "replay": {"symbols": {SYMBOL: {"candles": _replay_candles()}}},
    }
    path = tmp_path / f"scenario_{boot_index}.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    return path


def _write_daily_scenario(tmp_path: Path, *, boot_index: int) -> Path:
    scenario = {
        "name": "entry_regime_gate_daily_warmup",
        "exchange": "fake",
        "boot_index": int(boot_index),
        "account": {"balance": 10_000.0},
        "symbols": {
            SYMBOL: {
                "price_step": 0.01,
                "qty_step": 0.001,
                "min_qty": 0.001,
                "min_cost": 5.0,
                "maker_fee": 0.0002,
                "taker_fee": 0.00055,
            }
        },
        "replay": {"symbols": {SYMBOL: {"candles": _daily_replay_candles()}}},
    }
    path = tmp_path / f"scenario_daily_{boot_index}.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    return path


async def _run(
    tmp_path: Path,
    *,
    gate_enabled: bool,
    boot_index: int,
    steps: int = 3,
    daily_tape: bool = False,
    **gate_overrides,
) -> Path:
    out = tmp_path / f"run_{'on' if gate_enabled else 'off'}_{boot_index}"
    scenario_path = (
        _write_daily_scenario(tmp_path, boot_index=boot_index)
        if daily_tape
        else _write_scenario(tmp_path, boot_index=boot_index)
    )
    return await _run_fake_case(
        config_path=str(
            _write_config(tmp_path, gate_enabled=gate_enabled, **gate_overrides)
        ),
        scenario_path=str(scenario_path),
        user=None,
        max_steps=steps,
        output_dir=out,
        log_level=1,
        snapshot_each_step=False,
        enforce_assertions=False,
    )


def _load(run_dir: Path, name: str):
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def _verdicts(run_dir: Path) -> list[dict]:
    events = _load(run_dir, "live_events.json")
    rows = events if isinstance(events, list) else events.get("events", [])
    return [row for row in rows if row.get("event_type") == "entry_regime.gate.verdict"]


def _fill_steps(run_dir: Path) -> list[dict]:
    return [
        step
        for step in _load(run_dir, "step_summaries.json")
        if int(step.get("fills") or 0) > 0
    ]


async def test_no_daily_history_yet_blocks_and_reports_the_symbol(tmp_path):
    """Fail closed: the tape has no completed day, so there is no evidence to read."""
    run_dir = await _run(tmp_path, gate_enabled=True, boot_index=BOOT_EARLY)

    verdicts = _verdicts(run_dir)
    assert verdicts, "the gate publishes a verdict even when it cannot decide"
    assert all(int(v["data"]["unavailable_count"]) >= 1 for v in verdicts)
    assert not _fill_steps(run_dir), "an unavailable symbol must not open a position"


async def test_a_window_the_tape_cannot_fill_is_risk_off_not_an_error(tmp_path):
    """Warm-up: fewer completed days than the slow window blocks instead of crashing."""
    run_dir = await _run(
        tmp_path, gate_enabled=True, boot_index=BOOT_RISING, sma_slow_days=400
    )

    verdicts = _verdicts(run_dir)
    assert verdicts, "the table is published even when the window cannot be filled"
    assert all(
        int(v["data"]["risk_off_sides"]) > 0
        or int(v["data"]["unavailable_count"]) > 0
        for v in verdicts
    )
    assert not _fill_steps(run_dir), "an undecidable window must not open a position"


def _events(run_dir: Path) -> list[dict]:
    events = _load(run_dir, "live_events.json")
    return events if isinstance(events, list) else events.get("events", [])


def _first_index(rows: list[dict], event_type: str) -> int | None:
    for index, row in enumerate(rows):
        if row.get("event_type") == event_type:
            return index
    return None


async def test_a_sixty_day_warmup_decides_before_the_bot_is_ready(tmp_path):
    """The boot pre-warm assembles 60 days and publishes before trading readiness."""
    run_dir = await _run(
        tmp_path,
        gate_enabled=True,
        boot_index=BOOT_LAST_DAY,
        steps=1,
        daily_tape=True,
    )

    rows = _events(run_dir)
    verdict_at = _first_index(rows, "entry_regime.gate.verdict")
    ready_at = _first_index(rows, "bot.ready")
    assert verdict_at is not None, "the pre-warm publishes a verdict"
    assert ready_at is not None, "the run reaches readiness"
    assert verdict_at < ready_at, "the verdict must exist before the bot is ready"

    data = rows[verdict_at]["data"]
    assert data["lookback_days"] == 60
    # The harness config gates a 2/3 SMA, so its floor is the 60-day warm-up window.
    assert data["required_days"] == 3
    assert data["min_history_days"] >= 50
    assert data["max_missing_days"] == 0
    assert data["unavailable_count"] == 0
    assert data["risk_on_sides"] == 1
