# Strategy Profiles And Variants

A strategy profile is a maintained config under `configs/examples/` that fixes an
operator-facing trade-off. Profiles keep the same strategy kind and differ only in the
parameters that move that trade-off, so the difference between two profiles is reviewable as a
short diff rather than a rewritten config.

Use [Config Workflow](config_workflow.md) to copy a profile into `configs/private/` before
editing it for an account.

## Shipped Profiles

| Profile | Character |
| --- | --- |
| `configs/examples/default_trailing_martingale_long.json` | Maintained default. Long-only, 7 slots, `total_wallet_exposure_limit = 1.5`, fast re-entry ladder, HSL disabled. |
| `configs/examples/trailing_martingale_twel100_ddf060.json` | Lower-tail variant of the default. Same universe, same slots, same exit logic; lower exposure cap and a slower, wider re-entry ladder. |

## Lower-Tail Variant: `trailing_martingale_twel100_ddf060.json`

### What Changes

Relative to `default_trailing_martingale_long.json`, exactly three behavioural parameters
change (plus one optimizer bound, see below):

| Path | Default | This profile | Effect |
| --- | ---: | ---: | --- |
| `bot.long.risk.total_wallet_exposure_limit` | `1.5` | `1.0` | Caps total cost-basis exposure at 100% of balance instead of 150%, directly limiting how much mark-to-market loss the book can carry. |
| `bot.long.strategy.trailing_martingale.entry.double_down_factor` | `0.94` | `0.6` | Each re-entry adds 60% of the current position instead of 94%, so saturating the exposure cap takes many more steps. |
| `bot.long.strategy.trailing_martingale.entry.threshold_base_pct` | `0.019` | `0.03` | Re-entry spacing widens from 1.9% to 3.0% below the average entry, thinning the ladder during a decline. |
| `optimize.bounds.long.risk.total_wallet_exposure_limit` | `[1.5, 1.5, 0.01]` | `[0.6, 1.5, 0.01]` | Not a behaviour change: widens the search bound so this profile's own exposure cap is inside its optimizer space. |

Everything else is inherited unchanged: the 41-coin candidate universe, `n_positions = 7`,
`we_excess_allowance_pct = 0.37` (`bounded`), `unstuck` settings, `close` trailing logic, HSL
present but disabled, `entry.ema_gate_mode = "all"`, and the backtest section.

`tests/test_strategy_profile_example.py` pins that delta: the profile fails validation if it
drifts away from the three behavioural parameters above, if it stops loading, or if the shipped
values fall outside their own optimizer bounds.

### Evidence

Evidence lives under `backtests/binance/dd_tail_research_2026-09-15/`, which is tracked in the
repository. Read `tail_drawdown_attribution.md` for the full lever screen and
`artifacts/backtest_results/binance/<run>/annual_analysis.md` for the profile's own deep
analysis.

Both rows below use the same frozen 40-coin basket, the same window (2023-09-12 → 2026-09-12)
and the same execution/cost contract (T+2 order latency, maker `0.0006` / taker `0.0008`).

The shipped profile does **not** change any `backtest` setting: it keeps the template's
`maker_fee_override`, `taker_fee_override` and nominal T+1 latency. The contract above is what the
study that produced these numbers enforced, so reproducing the evidence means also applying those
overrides rather than running the profile with its own defaults. At nominal T+1 and the template's
lower maker fee the candidate's outcome is better, not worse — the study deliberately reports the
conservative contract.

| Metric | Default profile | This profile |
| --- | ---: | ---: |
| Worst minute-close equity drawdown | 80.64% | 31.01% |
| Gain multiple | 6.0558x | 2.8722x |
| CAGR | +82.25% | +42.14% |
| Longest underwater | 179.0 days | 1.9 days |
| Worst half-year return | -5.69% | +6.45% |
| Positive half-years | 5 / 6 | 6 / 6 |
| Fills | 68,266 | 52,797 |

On a locked, never-before-opened one-year holdout (2025-09-12 → 2026-09-12, same contract) the
default profile drew down 80.93% at -27.55% CAGR while this profile drew down 13.90% at +33.55%
CAGR. The candidate was frozen in `holdout_candidate_lock.json` before that window was evaluated.

The trade is explicit: **lower tail risk at materially lower upside.** Read the trade-off as
risk-shape, not as an upgrade.

### Reproducibility Boundaries

Every number above is a 1-minute OHLC simulation under documented assumptions. None of it is a
return forecast, and the profile is not published as a recommendation to run live.

1. **Fill model.** Limit orders fill in full at their limit price and are booked as maker when
   the candle's high/low strictly crosses them. There is no order-book queue, partial fill,
   spread, or cancel race.
2. **Latency.** The evidence contract uses T+2 (one extra completed bar before an order can
   fill). Nominal T+1 results are more optimistic; see the sensitivity table in the study
   report.
3. **Backtest-only hint.** With `close.retracement_base_pct > 0` the close path can still use the
   next candle's high/low to decide whether to expand a recursive close ladder. Live has no such
   candle.
4. **Parameter time travel.** The parameters were selected from studies run in 2026 and replayed
   over history that starts in 2023. The holdout above is genuinely unseen, but the three-year
   window is not.
5. **Liquidation.** `liquidated = false` is a simulation outcome, not a margin guarantee: mark
   price, maintenance-margin tiers, funding, and venue behavior are not modelled.

### Maintenance And Iteration

- Changing any of the three behavioural parameters changes the risk shape. After a change,
  re-run the study rather than only the profile backtest, because the published summary numbers
  and the tests' pinned delta both become stale.
- To refresh the evidence:

  ```bash
  bash backtests/binance/dd_tail_research_2026-09-15/run.sh
  ```

  The script rebuilds the candidate config from the locked ops, runs the backtest, renders
  `annual_analysis.md` plus the three metric tables, and verifies the report against the
  artifacts.
- To add a new variant: copy an existing profile, change only the parameters that define the
  trade-off, add a row to the table above, record the profile in
  `tests/test_strategy_profile_example.py`, and keep the study evidence under `backtests/`
  following `backtests/readme.md`.
- Strategy semantics themselves are contracts, not profile knobs. Read
  [Strategy Runtime Contracts](ai/features/strategy_runtime.md) before changing entry, close,
  risk, or unstuck behaviour, and
  [Validation](ai/validation.md) before publishing the result.
