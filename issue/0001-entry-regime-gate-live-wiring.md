# 0001 — Entry-regime gate reaches live

Status: active (wiring landed, waiting for the first live weeks)
Branch: `codex/entry-regime-gate-live-wiring`
Base: `codex/entry-regime-gate-and-gated-profile` (PR #2)
Pull request: [#3](https://github.com/annypay/passivbot/pull/3), six commits
Owner at write time: agent session of 2026-09-16/17

## Why this record exists

The daily entry-regime gate was added for backtests and published with a strategy profile,
but live trading never actually consulted it. Wiring it changes what a running bot does —
entries that used to happen stop happening while the daily regime is off — so the
behaviour, its evidence and its failure modes are written down here before the first live
start, and this file is updated as the live run teaches us more.

## What was wrong before this change

1. **The engine never read the live table.** `SymbolSideInput.regime_allows_initial_entry`
   and `regime_allows_reentry` are what the orchestrator ANDs into `wants_entries`
   (`passivbot-rust/src/orchestrator.rs`, the `should_generate_entries(...) &&
   regime_allows_entries(...)` sites). Only the backtest wrote them
   (`passivbot-rust/src/backtest.rs`, the per-bar refresh loop). Live produced a table and
   logged it, but nothing consumed it.
2. **The declaration never reached the bot.** A live config load removes the whole
   `backtest` subtree (`load_prepared_config(..., target="live")`), and the gate was
   declared under `backtest.entry_regime_gate` — where the published profile and every
   tracked evidence bundle state it. So `_entry_regime_gate_config()` returned `None` on a
   live bot even when the file declared a gate. Observed directly: after a live load,
   `config` has no `backtest` key at all, while `_raw_effective["backtest"]` still holds
   the declared block.
3. **A missing daily series failed open.** The live path recorded the symbol as
   unavailable and published no table; `EntryRegimeGateConfig::is_on` answers `true` for a
   disabled/absent table, so the symbol traded the ungated strategy.

## What this change does

| Area | Change |
| --- | --- |
| Evaluation point | `SymbolInput.regime_eval_ts_ms: Option<u64>`. When present, the orchestrator derives both per-side verdicts from `bot_params.{long,short}.entry_regime_gate` at that instant; when absent it keeps the legacy booleans. |
| Backtest | The per-bar refresh now only sets the bar's timestamp, so live and backtest run the same rule (`passivbot-rust/src/backtest.rs`). |
| Live payload | The planning symbol dict carries `regime_eval_ts_ms`, computed from one clock read shared with the table build; `None` when no gate is configured. |
| Declaration source | `live.entry_regime_gate` (now a declared, partially-open config key: `src/config/schema.py`, `src/config/hydrate.py`) is read first; when only the `backtest.entry_regime_gate` block is present, it is read back from the raw document the live loader kept. The resolved source is reported in the `[regime_gate]` log line as `source=`. |
| Missing evidence | An explicit risk-off row for the UTC day (`enabled=true`, `regime=[0]`), so the normal rule blocks new risk; the symbol is logged and the pass is not cached, so the read retries next cycle. |
| Observability | One `entry_regime.gate.verdict` live event per rebuilt table. |
| Rust tests | `EntryRegimeGateConfig::{allows_initial, allows_reentry}` plus unit tests; orchestrator tests for the timestamp path and the per-side flags. |
| Python tests | Fail-closed rows, the verdict event, the eval timestamp, both declaration paths, and an engine/table parity test with a counterfactual. |
| Dry run | Offline harness tests plus a manual smoke config/scenario. |

## Root causes found while landing this

| Symptom | Root cause | Fix |
| --- | --- | --- |
| 23 tests in `tests/test_live_entry_regime_gate.py` red in a full run, green alone | The new `[regime_gate]` log line had seven arguments and six `%s`: `TypeError: not all arguments converted during string formatting` raised when a handler formats the record (pytest's logging capture does) | format string fixed |
| The same file also failed with `asyncio.run() cannot be called from a running event loop` | the tests drove a coroutine with `asyncio.run` from sync test functions while `pytest.ini` sets `asyncio_mode = auto` | converted to `async def` + `await` (36 functions) |
| A live bot never saw a declared gate | the live loader strips the whole `backtest` subtree | live key declared + raw-document fallback |

A mis-stated claim from an earlier audit is corrected here: the guard
`BACKTEST_INHERITED_LIVE_KEYS` never contained a tuple/string mismatch. The
`*tuple(PARTIALLY_OPEN_CONFIG_PATHS)` spread lives in `TEMPLATE_SYNC_PRESERVE_PATHS`,
where it is correct; `src/config/hydrate.py` needed no change of that kind.

## Delivery

| Item | Value |
| --- | --- |
| Branch | `codex/entry-regime-gate-live-wiring` |
| Head at write time | `b73eaf789` |
| Pull request | [#3](https://github.com/annypay/passivbot/pull/3) (base: `codex/entry-regime-gate-and-gated-profile`) |
| Parent PR | #2 (gate, profile, evidence) — must merge for a clean rollback story |

## Verification at write time

- `cargo test --no-default-features --lib`: 333 passed / 0 failed; `cargo check --tests` clean.
- `tests/test_entry_regime_gate_engine_parity.py`: the engine's entry verdict equals the
  shared Python table at every boundary it produced (±1 minute); a side that does not
  declare the flag ignores a risk-off table; a caller without a timestamp keeps the legacy
  contract; and at one risk-off instant the gate blocks the entry while the same table with
  `enabled: false` takes it (the counterfactual that isolates the gate).
- `tests/test_live_entry_regime_gate.py` and `tests/test_entry_regime_gate.py`: green,
  including the fail-closed row, the verdict event, and both declaration paths.
- `tests/test_entry_regime_gate_config_survival.py`: green for both `backtest.*` and
  `live.*` spellings through `clean_config`, `prepare_config` and the sanitized dump.
- `tests/test_fake_live_entry_regime_gate_dry_run.py`: two offline harness runs.
- Full `pytest`: only the 7 pre-existing `tests/test_cache_warmup_reuse.py` failures.
- `src/tools/check_ai_docs.py`: 0 errors. `generate_live_event_registry.py --check`: current.

## Dry run, and its boundary

Offline, no credentials, seconds per run:

```bash
venv/bin/python -m pytest tests/test_fake_live_entry_regime_gate_dry_run.py -q
```

Two behaviours are proven through the real planning path: with no completed daily history
the gate publishes a risk-off row and records the symbol as unavailable (fail-closed), and
a slow window the tape cannot fill is risk-off rather than an error. Both runs also show
zero fills, which is the point.

Manual smoke on a longer tape (the run that first demonstrated publication end to end):

```bash
venv/bin/python src/tools/run_fake_live.py \
  configs/fake_live_entry_regime_gate.hjson \
  scenarios/fake_live/entry_regime_gate_wiring_smoke.hjson \
  --max-steps 250 --output-dir artifacts/fake_live --snapshot-each-step
```

That tape is hourly over ten days; it produces 238 steps, one
`entry_regime.gate.verdict` with `sma_fast_days=2, sma_slow_days=3, symbol_count=1,
risk_on_sides=1, risk_off_sides=0, unavailable_count=0`, and no fills — hourly rows are
not contiguous minutes, so the strategy's own inputs stay unavailable and the gate is the
only thing being exercised.

**Boundary, recorded rather than papered over:** the *falling-leg versus rising-leg fill
comparison in this harness* is not reachable. A tape that spans days while the fake clock
sits months away from the wall clock makes the staged live planner refuse:
`RuntimeError: staged planner precondition failed before market snapshot refresh: missing
current-epoch surfaces=balance,fills,open_orders,positions`
(`src/live/planning_gates.py:107`). That is a harness/clock limitation, not a gate
behaviour, so the isolation property is asserted at engine level instead (the
counterfactual test above). The command to revisit it later is the manual smoke above with
a 1-minute tape; if the harness ever grows a clock that tracks the wall clock, the
four-step fill comparison belongs in `tests/test_fake_live_entry_regime_gate_dry_run.py`.

## Live debugging playbook

### Fields to look at

| Field | Where | Normal | What an abnormal value means |
| --- | --- | --- | --- |
| `sma`, `confirm_days` | `[regime_gate]` log line | the configured 20/50, `confirm_days=0` | a different config was loaded |
| `source` | `[regime_gate]` log line | `live.entry_regime_gate` or `_raw_effective:backtest.entry_regime_gate` | `none` on a config that declares a gate means the gate is not being read |
| `symbols` | `[regime_gate]` log line | number of approved symbols with a table | smaller than the traded universe for long means some symbol got no read |
| `risk_off_sides` | log line, and `risk_off_sides` in the event | 0 outside downtrends | a high number for many days is the gate working, not a fault |
| `unavailable` / `unavailable_count` | log line and event | 0 | >0 means those symbols are blocked (fail closed) until the retry succeeds — check the exchange daily endpoint |
| `unavailable_symbols` | event (bounded sample) | absent | names the symbols whose daily series failed |
| `day_start_ms`, `planning_ts_ms` | event | `planning_ts_ms` inside the day that starts at `day_start_ms` | `planning_ts_ms` before `day_start_ms` would mean a clock problem |
| `risk_on_sides` | event | sides − `risk_off_sides` − unavailable sides | mismatch with the log line means the counts moved mid-cycle |
| `regime_eval_ts_ms` | planning payload (debug profile) | the cycle's exchange time | `null` means no gate is configured |
| `_orchestrator_entry_regime_gate_tables` | process state | one entry per approved symbol/side | a missing symbol is one that never got a table |
| `_orchestrator_entry_regime_gate_unavailable_symbols` | process state | empty | the retry list; it clears itself when a read succeeds |
| `_orchestrator_entry_regime_gate_cache` | process state | the current UTC day | staying on a previous day means rebuilds are not completing |

Note on the log line: the bot's own log carries it. In the fake harness the line goes to
the console instead of `fake_live.log`, because the harness attaches its file handler
before the bot configures logging and that configuration replaces the root handlers — so
harness assertions read `live_events.json`, never that file.

### Symptom → action

| Symptom | Likely cause | Action |
| --- | --- | --- |
| No entries at all for the first days after start | warm-up: the verdict is risk-off until `sma_slow_days + confirm_days + 2` completed days exist | expected for ~52 days on 20/50; verify with the `[regime_gate]` line, not by disabling the gate |
| No entries for one symbol while others trade | that symbol's verdict is risk-off, or its daily read failed | check `unavailable` and the event's `unavailable_symbols`; a repeated failure is an exchange/candle problem |
| Entries happen on a day the regime looks off | the daily series the exchange served differs from the chart being compared (venue, closed-day boundary, or a gap) | compare the venue's daily closes for the symbol against the table's `transition_ts` |
| `[regime_gate]` line missing entirely | no gate resolved | check `source=`; a config that only has `backtest.entry_regime_gate` still resolves through `_raw`, so `none` means the key really is absent |
| `unavailable` non-zero for every symbol | the daily endpoint or the exchange clock is failing | treat as a data incident: risk is closed for those symbols by design |
| `entry_regime.gate.verdict` events missing but the log line present | event pipeline not flushed or routed | check the live-event pipeline health; the log line remains the fallback |
| Extension stamp mismatch on startup | the Rust extension is older than the sources | rebuild the extension (the bot refuses a mismatched stamp by design) |
| Everything blocked right after a config edit | the edited block is invalid (e.g. `gate_mode`) | the loader rejects unsupported values; read the startup error |

### Rollback

1. Set `enabled: false` in the declaration the config uses — the gate stops filtering, and
   the table stops being built. Reload the config or restart the bot.
2. Or revert the wiring commits and restart: the bot then behaves exactly as before this
   record, with the gate present only in backtests.
3. Neither rollback touches tracked evidence or the published profile.

## Watch items

See the board in `README.md` (W1–W5). This record gains a dated section after the first
live week with: the first risk-on transition date, any `unavailable` incidents, and whether
the counterfactual review (W5) held.
