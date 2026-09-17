# Strategy Runtime Contracts

## Canonical And Legacy Strategy Schemas

The canonical strategy kind is `trailing_martingale`. The released v7 `trailing_grid` schema is not
aliased to it. Do not add compatibility migrations, duplicate strategy names, or silent shims for
removed fields unless the user explicitly asks for released-version compatibility.

`trailing_grid_v7` is the explicit compatibility exception. It is a normal Rust strategy kind,
deprecated from introduction, and exists only for configs converted by
`passivbot tool migrate-config-v7`. Keep its v7-only fields under
`bot.<side>.strategy.trailing_grid_v7`; do not add them to `trailing_martingale` or shared
`BotParams` unless a shared runtime function truly requires it.

The legacy pre-V8 `pb_multi` root shape (`TWE_long`, `TWE_short`,
`universal_live_config`, and related top-level fields) is not a supported V8 config input or
migration contract. Residual flavor detection, formatter code, and narrow unit fixtures are stale
internal compatibility surfaces: ignore them when adding current config fields, and do not infer
production or live support from their presence. The only supported pre-V8 strategy migration path
is the explicit `passivbot tool migrate-config-v7` workflow for normalized V7 trailing-grid input.

With `entry_cooldown_minutes = 0.0`, `trailing_grid_v7` preserves v7's simultaneous grid-entry
ladder even when a later trailing leg uses retracement. Its recursive generator stops expansion
before stacking retracement-dependent trailing orders. Positive entry cooldowns still stage at
most one position-adding order and apply their configured post-fill delay.

Removed v7 trailing-grid concepts:

- `entry_trailing_grid_ratio`
- `close_trailing_grid_ratio`
- `close_grid_markup_start`
- `close_grid_markup_end`
- linear `markup_start` to `markup_end` TP grids
- separate entry grid spacing vs trailing threshold knobs

Canonical config path:

```text
bot.<side>.strategy.trailing_martingale
```

Deprecated v7 compatibility config path:

```text
bot.<side>.strategy.trailing_grid_v7
```

Optimizer selector contract:

- `optimize.fixed_params` and `--fine_tune_params` use dotted config-path selectors.
- `long.*` / `short.*` selectors are aliases for `bot.long.*` / `bot.short.*`.
- Selectors match path segments by prefix or suffix, not partial substring. Use `long.strategy`
  to match the whole active strategy subtree, `long.strategy.close` to match only
  `bot.long.strategy.<active_strategy>.close.*`, or a leaf selector such as
  `we_excess_allowance_pct` to match every bound ending with that parameter name.
- `*` is allowed as a one-segment wildcard, for example `*.strategy.close`.
- Do not use flattened underscore selector names such as `long_entry_*` in current user-facing docs
  or agent instructions.
- When `--fine_tune_params` is combined with `--start`, the starting configs are anchor configs:
  non-tuned optimizer-bound bot params are fixed from the selected anchor, while the fine-tune
  selectors remain tunable. Base-config policy fields, including boolean toggles such as
  `bot.<side>.hsl.enabled`, still win over anchors. Anchor and seed values outside
  `optimize.bounds` are clamped into bounds with aggregated source/key logging. Without
  `--fine_tune_params`, `--start` remains seed-only.

Seed evaluation contract:

- Shared ingestion normalizes, clamps, quantizes, and deduplicates starting configs before each
  backend applies its seed policy.
- CPU optimization exact-Rust evaluates every deduplicated seed before population trimming.
- GPU `seed_bootstrap.mode=auto` exact-evaluates small pools and full-history proxy-screens pools
  larger than `max_exact`. Screened pools exact-Rust validate a capped, constraint-aware diverse
  proxy set; only that subset enters the authoritative exact archive.
- Seed-bootstrap exact evaluations are recorded separately from the GPU evolutionary
  `optimize.iters` budget. Exact fitness seeds the archive and vector-only initial sampling; exact
  and proxy fitness must never share an NSGA objective matrix.
- An incomplete bootstrap checkpoint owns its normalized seed plan. Resume does not depend on the
  original seed files, including for anchored fine-tuning, and must recover any exact seed result
  already flushed to durable history.

Timeframe-specific EMA spans use explicit horizon suffixes in canonical config names. Use `_1m`
for 1-minute candle inputs and `_1h` for 1-hour candle inputs, for example
`volatility_ema_span_1m`, `volatility_ema_span_1h`, `forager_volume_ema_span_1m`, and
`forager_volatility_ema_span_1m`. Generic strategy EMA spans such as `ema_span_0` and `ema_span_1`
remain unsuffixed because their timeframe comes from the strategy's base candle stream.

## Forager Readiness Stages

Live health distinguishes three stages:

1. The candidate universe contains approved, age-eligible, cost-eligible symbols.
2. A rankable candidate has the required real-candle quote-volume and log-range
   features.
3. Each Rust-plannable action has the strategy, trailing, and risk inputs it
   consumes; availability may differ by position side and entry/close branch.

A flat candidate with unavailable ranking features remains visible as a forager
candidate but is marked `forager.rankable=false`; this is distinct from an
active symbol losing a required strategy input. The Rust payload still fails
closed for an unrankable candidate. Monitor health must preserve that stage
distinction so operators do not mistake a bounded candidate-ranking exclusion
for an exchange market becoming intrinsically nontradable. Before the current
EMA bundle completes—or after a replacement bundle fails—eligible candidates
remain visible with `forager.rankable=false` and
`ema_bundle_unevaluated`; monitor health must not infer readiness from empty
failure sets. Bundle readiness is scoped to the exact evaluated symbol set, so
a newly age- or cost-eligible candidate remains unevaluated until a subsequent
bundle includes it. Forager candidate ranking continues to use completed-candle
metrics; held and explicitly normal symbols nevertheless retain any open-tail
log-range projections required by their active strategy. Live orchestration
passes completed-candle forager metrics separately from strategy EMA maps so a
shared span cannot leak a projected strategy value into coin ranking.

## Trailing Martingale Semantics

Trailing-martingale price spans belong to `entry.ema_span_0/1`. Schema v8.4.0 migrates
old strategy-root leaves before hydration and before file/inline override merges; explicit
new leaves win with warnings on conflicting values. Public optimizer bounds share the nested
paths; internal optimizer keys and Metal columns retain `ema_span_0/1`. Entry subtree selectors
include the horizons. EMA-anchor and trailing-grid-v7 paths are unchanged. Volatility horizons
remain shared by entries and closes.

Entries and closes use threshold/retracement fields.

- `retracement_base_pct <= 0.0`: trailing disabled, use passive recursive limit-order behavior.
- `retracement_base_pct > 0.0`: threshold is the required excursion, retracement is the confirmation move.
- Trailing extrema reset after any fill for the same coin+pside.
- Passivbot tracks trailing state itself from candle inputs; exchange-native trailing order types are not part of the core contract.

Live trailing availability is a Rust planning input, not a Python reconciliation policy. Python
sets `trailing_available=false` only for the affected coin+position-side while retaining a
structurally valid but inert bundle. Rust omits only an entry or close branch whose active strategy
configuration actually consumes trailing extrema. The other branch, the other position side,
panic, and independent risk reducers continue when their own inputs are complete. The resulting
Rust ideal set remains authoritative: reconciliation cancels resting orders absent from that set
and never preserves them merely because trailing reconstruction is pending.

Entry thresholds and retracements are multiplicative distances. Positive volatility or wallet
exposure weights widen them.

`bot.<side>.strategy.trailing_martingale.entry.ema_gate_mode` controls which entry orders are
capped/floored by the EMA entry band:

- `disabled`: no emitted entry order is capped/floored by EMA. Flat initial entries rest at current
  best bid/ask; re-entries use normal threshold/retracement logic.
- `all`: initial entries, partial initial entries, and re-entries are capped/floored by EMA.
- `initial`: normal initial and partial initial entries are capped/floored by EMA. Re-entries are
  not EMA capped/floored. This is the default and preserves the previous canonical behavior.
- `reentry`: flat initial entries are not capped/floored by EMA; re-entries are capped/floored by
  EMA.

The value is fixed config, not an optimizer parameter. In one-way mode, if both sides are flat and
both long and short are otherwise eligible, the long-vs-short tie-break still uses the EMA entry
band distance even when `ema_gate_mode = "disabled"`. Candles and EMA bands are therefore required
for that tie-break, and missing EMA inputs must fail loudly.

Close thresholds are additive so they can intentionally cross through break-even or negative markup
as wallet exposure rises. Close retracement is volatility-weighted and intentionally has no
wallet-exposure modifier.

Auto-unstuck has its own EMA trigger toggle:
`bot.<side>.unstuck.ema_gating_enabled`. It defaults to `true`. When false, auto-unstuck skips the
EMA trigger/readiness check but still requires `unstuck.enabled`, loss allowance, exposure
threshold, close sizing, and valid market/exchange inputs.

`bot.<side>.unstuck.ema_span_0` and `ema_span_1` independently define the unstuck price EMA
band, using the same base candle stream and unrounded geometric-mean third span as the strategy
band. Long eligibility uses the upper band; short eligibility uses the lower band. Strategy EMA
changes must not change the unstuck band. Missing required unstuck EMAs follow the existing scoped
input-unavailable contract; disabled gating does not require them or extend warmup. Live loading
requests unstuck-only spans for held sides separately, preserving usable strategy spans when an
unstuck horizon is unavailable. Live warmup uses that same held/static eligibility and does not
expand flat forager candidates to an unused unstuck horizon. Monitor bands are independently
available for each consumer.

Schema v8.3.0 materializes missing unstuck spans from the effective active strategy before default
hydration, including coin overrides and external-file/inline precedence. Explicit unstuck spans
win. New optimizer bounds copy fixed legacy bounds, or freeze at the migrated starting values for
varying legacy ranges unless supplied; a warning explains that independent genes cannot preserve
the old coupled search. New defaults expose tunable spans.
Apple MPS models a separate price EMA band for unstuck in all directional and multicoin kernels.
It uses the same seeded recurrence, floating horizons and candle-interval scaling as exact Rust.
Coin overrides win over candidate globals, and temporal replay persists the independent band.

The opt-in optimizer override `couple_unstuck_ema_spans` derives each side/coin's unstuck pair
from its effective strategy, after mirroring and fixed/scenario overrides. It removes redundant
unstuck span genes and materializes explicit runtime spans in saved candidates and scenarios.
GPU packing preserves the dependency on candidate strategy genes while retaining strategy coin
pins. This is optimizer configuration finalization, not a live/backtest coupling mode.

## Entry-Regime Gate

`backtest.entry_regime_gate` optionally blocks entries from a precomputed,
strictly causal daily SMA crossover. It is a **filter, not a signal**: it can only
suppress entry orders.

```text
backtest.entry_regime_gate = {
  enabled, sma_fast_days, sma_slow_days,
  block_initial, block_reentry, confirm_days, require_long_only,
}
```

Semantics and invariants:

- The regime in force during UTC day `D` is decided by daily closes through the end
  of day `D - 1`. Days before the slow window has filled are risk-off, because the
  filter has no completed evidence yet.
- The gate compresses to `(transition_ts, regime)` boundaries at the first bar of
  each UTC day and is replayed in Rust as a timestamp lookup. No indicator state is
  computed in the engine, so no arithmetic path can make it read a future bar.
- `block_initial` suppresses opening a new position; `block_reentry` suppresses
  adding to an existing one. Both default to true.
- Closes, panic closes, auto-unstuck, and every protective reducer are unaffected.
  A risk-off bar can always exit; it simply cannot add risk.
- An absent block, `enabled = false`, or a side-absent table is a no-op, and a
  disabled gate costs one branch per bar.
- The per-side flags are refreshed for every symbol on every bar inside
  `get_orchestrator_input_cached`. They must not be populated while the orchestrator
  input cache is built, because that path runs once and the flags would freeze at
  their first-bar value.
- `src/entry_regime.py` owns the verdict arithmetic and is shared by the backtest
  and the live path, so the two cannot drift. The backtest buckets its own 1-minute
  bars into UTC days; live reads `timeframe="1d"` from the candlestick manager, which
  aligns those buckets to the epoch and caps the range at the last finalized bucket.
  Daily candles are therefore closed days only, and the engine still treats the last
  series row as non-evidence.
- The live planning path publishes one boundary at the start of the current UTC day:
  the verdict cannot change inside a day, so the whole table is that day's flag. A
  configuration that declares neither `block_initial` nor `block_reentry` never
  consults the filter and needs no daily evidence; otherwise both the backtest and
  live attach a table to every approved side.
- Live enforces the same rule through the same code. The planning payload carries
  `SymbolInput.regime_eval_ts_ms` (the cycle's exchange time; omitted when no gate is
  configured), and the orchestrator evaluates `bot_params.{long,short}.entry_regime_gate`
  at that instant, so live and backtest share one implementation of the rule — the
  backtest passes each bar's timestamp instead. A payload without a timestamp keeps the
  legacy `regime_allows_*` booleans, which is what producers that predate the table send.
- Where live reads the declaration. A live config resolves `live.entry_regime_gate`
  first, then the `backtest.entry_regime_gate` block recorded in the raw document, which
  is where the published profile and the tracked evidence bundles state it; the live
  config loader strips the rest of the `backtest` subtree, so that fallback reads the raw
  document rather than the loaded config. Both spellings describe one gate and the live
  key wins when a config carries both.
- Failure is fail-closed. A symbol whose daily series is missing, unaligned or too short
  for the slow window gets an explicit risk-off row (`enabled = true`, `regime = [0]`) for
  the UTC day, which blocks new risk through exactly the rule a computed risk-off day
  uses. The symbol is logged, and the pass is not cached, so the next planning cycle
  retries the read.
- Warm-up is a fetch depth, not a waiting period. The verdict is defined once the
  assembled series holds `sma_slow_days + confirm_days` completed days, and a live start
  asks the exchange for them itself: `live_lookback_days` (`src/entry_regime.py`) requests
  `max(60, sma_slow_days + confirm_days + 10)` completed daily rows — 60 days for the
  published 20/50 gate — so the first planning cycle can decide rather than starting
  risk-off and waiting for days to accumulate. A request for more days than the exchange
  holds comes back shorter, so a symbol whose history is genuinely too short stays
  risk-off.
- A day the exchange did not return is not evidence: every SMA window spanning it stays
  undefined and reads risk-off. That is why the request carries a margin — a request that
  only just fills the slow window turns one lagging or missing recent day into a risk-off
  stretch of up to `sma_slow_days` days.
- Live assembles that evidence once at startup. `prewarm_entry_regime_gate` runs before
  `bot.ready`, retries a failed read once, and leaves the settled per-UTC-day table for the
  first cycle to reuse. It never aborts startup: a symbol that stays unavailable keeps the
  fail-closed row the planning path publishes anyway.
- Each rebuilt table publishes one `entry_regime.gate.verdict` event carrying the SMA
  parameters, the UTC day, the planning timestamp, the counts of risk-on sides, risk-off
  sides and unavailable symbols, and the assembled warm-up depth (`lookback_days`,
  `required_days`, `min_history_days`, `max_missing_days`). Live rejects
  `gate_mode = "invert_for_short"`: that is the long/short-flip research mode, not a
  tradeable live configuration.
- The master (`symbol=None`) params always carry a disabled gate, and the protective
  panic path clears the table before it plans, because panic closes and never opens.
- The key must stay declared in the schema template **and** listed in
  `PARTIALLY_OPEN_CONFIG_PATHS` (`src/config/hydrate.py`). `clean_config` rebuilds a
  config from the template and only visits template keys, so a `backtest.*` key
  outside the template is dropped whenever a config is loaded through the normal
  pipeline — which is the path the CLI uses and therefore the path artifact bundles
  and their reports come from. Both declarations are required: the template entry
  makes the subtree visited, and the partially-open entry makes the subtree's own
  keys survive instead of being rebuilt from the template. A config that declares no
  gate must not gain one, so the template default is an empty table rather than a
  populated one.

## Live/Backtest Market Slippage Boundary

Exact Rust backtest order construction must not consume a future candle range.
Expansion hints are derived from current position state and preserve the shared
sequential-entry and close-recursion contracts. Candle-k fills concern already
active orders; candle-k features generate later intent. Simulation-only order
latency and fill-order sensitivity are documented in `../../backtesting.md` and
must not enter the live policy surface.

`backtest.market_order_slippage_pct` is a backtest simulation knob only. Live orchestrator input
must not read or forward it; live callers intentionally omit `market_order_slippage_pct` and rely
on Rust's `0.0` serde default for live loss projections. Live market-order execution uses the
current bid/ask snapshot plus exchange-side behavior, controlled by `live.market_orders_allowed`
and `live.market_order_near_touch_threshold`.

## Close Recursion Contract

Close orders are computed recursively when the close threshold depends on wallet exposure:

1. Compute the next close from current position exposure.
2. Size it up to `close.qty_pct`.
3. Simulate that close as filled.
4. Recompute wallet-exposure ratio.
5. Repeat until the position is exhausted or the ladder is complete.

If `close.retracement_base_pct <= 0.0` and `close.threshold_we_weight == 0.0`, recursive closes all
have the same price. Rust intentionally emits one full-position close in that case; `close.qty_pct`
is effectively moot because multiple same-price slices would be redundant.

## Close Reducer Compatibility

For each coin and position side, Rust selects at most one protective reducer per ideal-order batch.
Active panic, TWEL/WEL exposure-repair, and auto-unstuck intents are consolidated by keeping the
largest loss-admissible absolute reduction after final position/minimum sizing, not by summing
their quantities. If the realized-loss gate blocks the largest non-panic intent, Rust tries the
next-largest intent. This prevents a small auto-reduce from suppressing a materially larger unstuck
close while retaining a smaller safety fallback when the larger close would exceed the loss
budget. A full-position HSL panic is therefore largest and remains exclusive; equal-size ties keep
panic first and otherwise prefer the closest-to-fill candidate.

A selected non-panic reducer may coexist with ordinary grid, trailing, or EMA-anchor closes. Its
quantity is reserved first; if aggregate close quantity would exceed the position, ordinary closes
are trimmed furthest-from-fill first against the remaining quantity. Aggregate reduce-only quantity
must never exceed the position after quantity-step and effective-minimum handling. The cumulative
realized-loss gate participates in this allocation, checks the final reducer size, and spends the
shared batch allowance on final reducer quantities largest-first rather than in symbol iteration
order. It then evaluates ordinary closes after the selected reducers. Live reconciliation reapplies
the same aggregate cap against current exchange position size, still trimming ordinary closes
before the reducer if the position shrank after planning.
This contract is shared by every strategy kind, including `trailing_grid_v7`.

## Source Of Truth

Rust owns strategy dispatch and order behavior. If Python docs, adapters, or tests imply old
`trailing_grid` behavior, update those surfaces to match Rust rather than adding Python-side
behavior patches.

## Validation

- Rust unit tests cover strategy dispatch, entries, closes, recursion, risk, and unstuck behavior.
- Python tests use the verified real extension when asserting Rust output.
- Live/backtest parity tests compare optimized behavior with a simple reference contract.
- Config migration/schema tests reject removed fields outside `trailing_grid_v7`.
- Behaviorally relevant changes include a bounded real backtest smoke.

## Key Code And Tests

- `passivbot-rust/src/orchestrator.rs`
- `passivbot-rust/src/entries.rs`
- `passivbot-rust/src/closes.rs`
- `tests/test_orchestrator_json_api.py`
- `tests/test_orchestrator_integration.py`
- `tests/test_auto_unstuck_allowance.py`
