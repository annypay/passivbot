# Per-Coin Stop Loss

`bot.<pside>.stop_loss` closes a single coin's whole position when that coin's own price crosses a
fixed distance from its average entry, then keeps that coin out of new entries for a cooldown. It is
the only risk control in this engine that reacts to one coin's price rather than to account equity,
and it is **off by default**.

```json
"bot": {
    "long": {
        "stop_loss": {
            "enabled": false,
            "pct_from_avg_entry": 0.15,
            "cooldown_minutes": 1440.0,
            "order_type": "market"
        }
    }
}
```

| key | default | meaning |
| --- | --- | --- |
| `enabled` | `false` | arm the stop on this side |
| `pct_from_avg_entry` | `0.15` | distance from the average entry, part-per-one; must be `> 0` while enabled |
| `cooldown_minutes` | `1440` | minutes after a stop-loss fill during which this coin may not add; `0` disables the block |
| `order_type` | `"market"` | `market` closes at the touched price; `limit` rests at the stop level |

Both sides are configured independently, exactly like `hsl` and `unstuck`, and the whole group can be
overridden per coin under `coin_overrides.<coin>.bot.<pside>.stop_loss.*`.

## What it does

1. **Trigger.** Once per decision sample, while the position is open and the average entry is known,
   the engine compares the price it sees with `average_entry x (1 - pct_from_avg_entry)` for longs
   (mirrored above the entry for shorts). A price that crosses the level triggers the stop; a price
   that moves back above it simply stops re-arming, so no persistent "armed" state is stored.
2. **Exit.** The whole position is closed with a single reduce-only order tagged
   `close_stop_loss_long` / `close_stop_loss_short`, and every pending entry order for that
   coin-side is cancelled in the same decision.
3. **Cooldown.** After the stop-loss fill, that coin-side may not add for `cooldown_minutes`. The
   anchor is the bot's own tagged stop-loss fill; a fill that cannot be attributed to the stop loss
   deliberately does not start a cooldown. Closes are never blocked.

The stop is not emitted while the account guard is in `panic` mode (the guard is already flattening
everything), and it is not emitted for a `manual` coin-side, where the operator owns the position.
When a stop-loss close and an unstuck or enforcer close compete for the same sample, only one is
emitted: the unstuck/enforcer reducer set is mutually exclusive, and the whole-position stop wins on
size.

## What it does not do

- **It does not place an exchange-resident stop order.** No exchange integration in this repository
  creates a stop order; the trigger lives in the engine's own sampling loop. Between samples, and
  whenever the bot is not running or not reachable, there is no protection at all. An account whose
  safety depends on protection during an outage needs an exchange-side order, which is a separate
  piece of work (tracked in `issue/0007-g4-twe300-sl-cooldown-2026-09-18.md`).
- **A `limit` stop can fail to exit.** Once triggered, the level sits above the market, so a `limit`
  order rests there and only fills if the price trades back up to it. A cascade that gaps through the
  level leaves the order unfilled and the position riding down. `market` is the tier that actually
  exits; it pays taker fees and `backtest.market_order_slippage_pct` in backtests.
- **It does not trigger on a wick.** The trigger price is the price the bot samples, not the candle
  low, so a spike that is not visible at a decision point does not fire it. This is the opposite
  trade-off from a real exchange stop, which does fire on the wick — see the account guard's
  `min(raw, ema)` smoothing in [equity_hard_stop_loss.md](equity_hard_stop_loss.md) for the same
  choice made on account equity.
- **It is not in the optimize bounds.** `docs/configuration.md` records why: the optimization
  backends do not model a per-coin stop, so a search over it would score an inert stop as an armed one.

## Cost, measured

A stop opts a coin out of the mechanism this strategy uses to recover from a drawdown: adding into
the dip and cropping into the bounce. On the pinned 3-year leg (`a_allow000`, 10,000 USDT, TWE 3.0,
allowance 0, unified guard RED 0.20 / EMA 60 / 12h), a 15% stop with a 24h cooldown:

- fired 37 times across the 418 episodes whose peak exposure reached 5% of the balance;
- 30 of those 37 positions (81%) ended **profitable** without it, for +1,379 USDT combined;
- realized -16,300 USDT (limit tier) / -17,976 USDT (market tier) on those exits, i.e. +17,680 /
  +19,356 USDT against holding them;
- and in 76% of the cases the price was back at or above the exit level when the cooldown expired.

The same history contains the counter-argument: tightening the *account-level* guard from RED 0.20
to 0.15 cost on that 3-year leg but was worth +2.0x terminal value and -26pp worst drawdown on the
longer 2021-2026 leg, which contains the 2021-2022 tail. Whether a stop pays depends on whether the
sample contains a real tail; that is what the study under
`backtests/binance/g4_twe300_sl_cooldown_2026-09-18/` measures, and its pre-registered verdicts are
in `issue/0007-g4-twe300-sl-cooldown-2026-09-18.md`. Nothing here is a recommendation to enable it.
