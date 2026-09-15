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
change, plus one optimizer bound and three backtest-contract keys (see below):

| Path | Default | This profile | Effect |
| --- | ---: | ---: | --- |
| `bot.long.risk.total_wallet_exposure_limit` | `1.5` | `1.0` | Caps total cost-basis exposure at 100% of balance instead of 150%, directly limiting how much mark-to-market loss the book can carry. |
| `bot.long.strategy.trailing_martingale.entry.double_down_factor` | `0.94` | `0.6` | Each re-entry adds 60% of the current position instead of 94%, so saturating the exposure cap takes many more steps. |
| `bot.long.strategy.trailing_martingale.entry.threshold_base_pct` | `0.019` | `0.03` | Re-entry spacing widens from 1.9% to 3.0% below the average entry, thinning the ladder during a decline. |
| `optimize.bounds.long.risk.total_wallet_exposure_limit` | `[1.5, 1.5, 0.01]` | `[0.6, 1.5, 0.01]` | Not a behaviour change: widens the search bound so this profile's own exposure cap is inside its optimizer space. |
| `backtest.maker_fee_override` | `0.0004` | `0.0002` | States the reported cost contract (Binance USDT-M VIP0 maker, 0.02% per side). |
| `backtest.taker_fee_override` | `0.00055` | `0.0005` | Same contract, taker side (0.05% per side). Inert here: this profile never sends a market order. |
| `backtest.execution_delay_bars` | `1` | `0` | Nominal T+1 latency: a signal decoded from bar *t* may fill on bar *t+1*. |

`intrabar_fill_order` stays `close_first` in both.

Everything else is inherited unchanged: the 41-coin candidate universe, `n_positions = 7`,
`we_excess_allowance_pct = 0.37` (`bounded`), `unstuck` settings, `close` trailing logic, HSL
present but disabled, and `entry.ema_gate_mode = "all"`.

The three `backtest` keys exist so the shipped profile reproduces the published numbers when it is
run as-is. They are a cost/latency statement, not a behaviour change, and the taker side of that
statement is inert: `live.market_orders_allowed = false` and HSL panic closing is disabled, so no
code path in this profile can emit a taker fill, and slippage never applies.

`tests/test_strategy_profile_example.py` pins that delta: the profile fails validation if it
drifts away from the three behavioural parameters above, if it stops loading, if its backtest
contract stops matching the contract its evidence was produced under, or if the shipped values
fall outside their own optimizer bounds.

### Evidence

Evidence lives under `backtests/binance/dd_tail_research_2026-09-15/`, which is tracked in the
repository. Read `tail_drawdown_attribution.md` for the full lever screen and
`artifacts/binance_actual_candidate/backtest_results/binance/<run>/annual_analysis.md` for the
profile's own deep analysis. `analysis/anti_pattern_audit.md` records the overfitting and
anti-pattern review, and `analysis/overfitting_audit.json` holds the raw probability-of-backtest-
overfitting (PBO) numbers.

Both rows below use the same frozen dataset (40 coins with usable history out of the config's 41),
the same window (2023-09-12 → 2026-09-12) and the same execution/cost contract: nominal **T+1**
order latency with **Binance USDT-M VIP0** fees (maker `0.0002` / taker `0.0005` per side). That is
contract **v4** in `research_contract_v4.json`.

The two rows are not produced the same way, and the difference matters when reproducing them:

- **This profile** states contract v4 in its own `backtest` block, so
  `bash backtests/binance/dd_tail_research_2026-09-15/run.sh` reproduces its column with no
  override. Its artifact bundle, run record and verification live under
  `artifacts/binance_actual_candidate/`.
- **The default profile** is the study's baseline. Its column is the frozen baseline config
  evaluated under the same v4 contract, which the study applies explicitly; its own artifact
  bundle lives under `artifacts/binance_actual_baseline/`. The template file itself is unchanged:
  it still keeps its historical `maker_fee_override = 0.0004` and `taker_fee_override = 0.00055`
  at the same nominal T+1 latency. Those are the settings its own optimizer and parity tests were
  built around, and the study records the reported contract rather than editing the maintained
  default. The residual difference is small and one-directional: `0.0002` is the lower maker
  assumption, so the baseline column is marginally optimistic about the *baseline*, which makes
  the comparison below slightly conservative for this profile.

Both columns are reproducible from the tracked evidence:

```bash
bash backtests/binance/dd_tail_research_2026-09-15/run.sh              # this profile's column
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --baseline   # the default's column
```

| Metric | Default profile | This profile |
| --- | ---: | ---: |
| Worst minute-close equity drawdown | 73.16% | **32.92%** |
| Gain multiple | 12.2068x | 2.6097x |
| CAGR | +130.21% | +37.67% |
| Longest underwater | 171.9 days | 25.6 days |
| Worst half-year return | +20.73% | +11.37% |
| Positive half-years | 6 / 6 | 6 / 6 |
| Fills | 69,799 | 47,857 |

On a locked, never-before-opened one-year holdout (2025-09-12 → 2026-09-12, same contract) the
default profile drew down 20.96% at +134.96% CAGR while this profile drew down 10.68% at +30.09%
CAGR. The candidate was frozen in `holdout_candidate_lock.json` before that window was evaluated.

The trade is explicit: **lower tail risk at materially lower upside.** Read the trade-off as
risk-shape, not as an upgrade. The default profile earns roughly three times the multiple inside
this window; it also spends 172 days underwater versus 26, and its worst drawdown is 2.2x deeper.

#### Sensitivity: does the conclusion survive a harsher contract?

The same two profiles were replayed under the superseded **v3** contract (T+2 latency, maker
`0.0006` / taker `0.0008`, i.e. one extra bar of latency and 3x the maker fee). Only contract
numbers move between the two blocks; the ranking does not.

| Contract | Window | Default drawdown | Default CAGR | Profile drawdown | Profile CAGR |
| --- | --- | ---: | ---: | ---: | ---: |
| v4 (T+1, Binance VIP0) | full | 73.16% | +130.21% | 32.92% | +37.67% |
| v3 (T+2, 3x maker) | full | 80.64% | +82.25% | 31.01% | +42.14% |
| v4 (T+1, Binance VIP0) | holdout | 20.96% | +134.96% | 10.68% | +30.09% |
| v3 (T+2, 3x maker) | holdout | 80.93% | −27.55% | 13.90% | +33.55% |

Under the harsher contract the candidate looks slightly *better* (31.01% drawdown, +42.14% CAGR
over the full window; 13.90% / +33.55% on the holdout) while the default profile degrades sharply,
turning the holdout negative. The candidate is the less latency- and fee-sensitive of the two,
which is the expected consequence of trading a wider ladder at a lower exposure cap. Do not mix
rows from the two contract blocks in a single table — they are different simulations.

#### Selection fragility

The lever screen behind this profile searched 38 single-lever and combo variants plus the
baseline. A combinatorially symmetric cross-validation (CSCV) over those 39 curves gives a
probability of backtest overfitting (PBO) between **0.116 and 0.229** depending on the number of
blocks (8/10/12/16), with the in-sample-best curve's Sharpe falling from ~7.2–7.8 in sample to
~4.5 out of sample (slope −0.26 to −0.51). Read that as: the screen carries real signal, but
picking the single best cell from it is fragile. The profile is published as a documented
trade-off with a holdout behind it, not as the screen's optimum — the locked candidate is
deliberately mid-pack on CAGR.

Splitting the full window at the lock date makes the selection bias visible. Inside the
in-sample selection window (2023-09-12 → 2025-09-12, v4 contract) the default profile compounds at
+128.57% CAGR against this profile's +41.40% — a 3.1x advantage, and the same ratio the full window
shows. In the locked holdout the default profile goes to −27.55% CAGR while this profile stays at
+30.09%: the return edge that made the default look superior inside the sample does not survive
out of it, while this profile's edge is smaller but keeps its sign. Max drawdown keeps the same
ordering in both windows (73.16% vs 32.92% in-sample; 20.96% vs 10.68% on the holdout). The
in-sample return gap is substantially market beta, not durable skill; that is what the fragility
numbers below are measuring.

CSCV needs a rectangle, so the panel is the intersection of the months every curve covers: 20 of
the 37 monthly buckets, 2023-09 → 2025-05. The narrow window is not a choice — the `unstuck_off`
cell liquidates in April 2025 and stops producing returns, and the intersection is what survives.
That single curve is the only configuration whose monthly series ends early, and it is recorded
in `overfitting_audit.json` under `panel.truncated_cells`. The 20 surviving months still contain
the 2025 drawdown event, so the panel is not a quiet-period artifact: the baseline's worst month
(`2025-02`, −31.90%) and the crash month (`2025-03`, −19.26%) are both inside it. The cost of
the rectangle is the other direction — the panel ends in 2025-05 and therefore does not cover
the final sixteen months of the full window at all.

### Reproducibility Boundaries

Every number above is a 1-minute OHLC simulation under documented assumptions. None of it is a
return forecast, and the profile is not published as a recommendation to run live.

1. **Fill model.** Limit orders fill in full at their limit price and are booked as maker when
   the candle's high/low strictly crosses them. There is no order-book queue, partial fill,
   spread, or cancel race. Both profiles are maker-only, so the taker fee and the slippage
   setting never bind.
2. **Latency.** The reported contract is nominal T+1. A T+2 replay is retained as a stress
   reference; see the sensitivity table above.
3. **Backtest-only hint.** With `close.retracement_base_pct > 0` the close path can still use the
   next candle's high/low to decide whether to expand a recursive close ladder. Live has no such
   candle.
4. **Parameter time travel.** The parameters were selected from studies run in 2026 and replayed
   over history that starts in 2023. The holdout above is genuinely unseen, but the three-year
   window is not. The selection-fragility numbers above are the quantified version of this
   caveat.
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
  artifacts. Re-run `report_tools/audit_overfitting.py` to refresh the PBO numbers.
- Changing the reported contract means a new contract version, not an edit to the current one:
  add a `<regime>` block to `contract_regimes`, keep the old block frozen, and update the profile,
  the test, and this page together.
- To add a new variant: copy an existing profile, change only the parameters that define the
  trade-off, add a row to the table above, record the profile in
  `tests/test_strategy_profile_example.py`, and keep the study evidence under `backtests/`
  following `backtests/readme.md`.
- Strategy semantics themselves are contracts, not profile knobs. Read
  [Strategy Runtime Contracts](ai/features/strategy_runtime.md) before changing entry, close,
  risk, or unstuck behaviour, and
  [Validation](ai/validation.md) before publishing the result.