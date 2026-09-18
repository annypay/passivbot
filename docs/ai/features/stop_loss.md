# Per-Coin Stop Loss Contract

Subsystem contract for `bot.<pside>.stop_loss`. User-facing behaviour is in
[docs/stop_loss.md](../../stop_loss.md); this file fixes what the engine promises.

## Scope

- **Owner:** the Rust orchestrator. Python owns configuration, exchange I/O and fill attribution; it
  never decides whether the stop fired.
- **Default:** disabled. With `stop_loss_enabled = false` the engine must produce a byte-identical
  order and fill sequence to a build without this feature (see Invariants).
- **Applies to:** one symbol and one position side. It is not an account-level control; the
  account-level control is `bot.<pside>.hsl.*`
  ([equity_hard_stop_loss.md](equity_hard_stop_loss.md)).

## Config surface

Grouped `bot.<pside>.stop_loss.{enabled, pct_from_avg_entry, cooldown_minutes, order_type}`, flattened
by `src/config/shared_bot.py` to `stop_loss_enabled`, `stop_loss_pct_from_avg_entry`,
`stop_loss_cooldown_minutes`, `stop_loss_order_type` in the per-symbol `BotParams`. Every backtest and
live bot-param bridge carries the keys; per-coin overrides are allowed through
`src/config/overrides.py`; the keys are excluded from the optimize bounds (`docs/configuration.md`).

Rust carries the same defaults as Python (`stop_loss_enabled = false`,
`stop_loss_pct_from_avg_entry = 0.15`, `stop_loss_cooldown_minutes = 1440.0`,
`stop_loss_order_type = "market"`), so a config that omits the group behaves identically on both
sides of the bridge.

## Rule

1. **Trigger.** With a non-zero position, `position.price > 0`, `stop_loss_pct_from_avg_entry > 0`
   and enabled, the stop fires when the orchestrator's current market price is at or below
   `position.price * (1 - pct)` for a long, or at or above `position.price * (1 + pct)` for a short.
   The condition is evaluated every sample and holds no state: a price that recovers above the level
   stops re-arming.
2. **Emission.** One `IdealOrder` with `qty = -|position.size|` (long) / `+|position.size|` (short)
   and `order_type = CloseStopLossLong | CloseStopLossShort`. It is a close order
   (`is_close_order_type`), a protective reducer (`is_protective_close_reducer`), and all
   position-increasing orders for that symbol-side are dropped in the same decision.
3. **Mode gate.** Not emitted in `TradingMode::Panic` (the panic close already flattens the scope)
   and not in `TradingMode::Manual` (the operator owns the position). Entry eligibility, entry gating
   and strategy-input availability do not suppress it.
4. **Fill tier.** `order_type = "market"` routes the order through the market execution path (taker
   fee plus `backtest.market_order_slippage_pct`) independently of `backtest.market_orders_allowed`,
   which governs strategy orders. `order_type = "limit"` rests at the stop level and fills only if
   the market trades back to it.
5. **Cooldown.** While `add_order_cooldown_active(now, last_stop_loss_fill_timestamp_ms,
   stop_loss_cooldown_minutes)` holds, position-increasing orders for that symbol-side are dropped
   through a narrow gate that does not trigger the entry-ladder staging rule. Closes are unaffected.

## Invariants

1. **Default-off identity.** `stop_loss_enabled = false` produces an identical order and fill
   sequence, and identical metrics, to a build without the feature on both the native and the long
   historical leg.
2. **Reduce-only.** The emitted quantity is the position's absolute size at the triggering sample,
   signed against the position. The stop never increases a position.
3. **No entries on the trigger sample.** The triggering decision cancels that symbol-side's entries,
   so the stop cannot add and exit in the same minute.
4. **No new persistent state.** Triggering depends only on the position average price and the current
   sample price, so it reproduces after a restart from exchange state. The cooldown depends only on
   the timestamp of the bot's own tagged stop-loss fill, which is rebuilt from fill history at
   restart.
5. **Panic priority.** Panic closes keep their exclusive reducer priority; the stop is never emitted
   while the account guard is panicking.
6. **Fail-closed configuration.** A negative distance, a non-positive distance while enabled, a
   non-boolean `enabled` and an unknown `order_type` are rejected at load, not defaulted away. On the
   per-coin override path an unknown `stop_loss.*` leaf is also rejected (`... is not overridable`);
   in a bot config *file* an unknown leaf under any shared group is dropped by the pre-existing,
   group-agnostic normalizer, so a misspelled leaf there silently keeps the default — spell the four
   keys exactly as documented.
7. **Not searched.** No stop-loss key may enter an optimize bound, because the optimization backends
   do not model the mechanism.

## Telemetry

- The stop-loss fill is identified by its order type in `fills.csv`
  (`close_stop_loss_long` / `close_stop_loss_short`), which is what the study's analysis reads.
- The live cooldown anchor is derived from the same tag on the bot's own fills; an unattributable
  fill does not start a cooldown.
