# Backtest Research Evidence

This tree is version-controlled so a fresh checkout can trace **how a strategy profile was
chosen** without re-running multi-hour studies. It is research evidence, not a release
artifact: nothing here is installed, packaged, or required at runtime.

## What is tracked, and what is not

Tracked (small, hard to regenerate, human-readable):

- `*.md` — study reports, audits, and deep analyses.
- `*.py` and `*.sh` — the exact study scripts, plus the run entry point that reproduces a study.
- `*.json` — research contracts, candidate locks, manifests, per-configuration result and
  metric summaries.

Not tracked (large, regenerable from the tracked inputs plus the local HLCV cache):

- `fills.csv`, `execution_audit.csv`, `balance_and_equity.csv.gz`
- `*.npy`, `*.npz`, `*.png`, `*.pyc`, `*.log`
- every per-run bookkeeping tree named `runs/`
- compiled runtime configs (`config.json`, `config.original.json`, `dataset.json`,
  `study_input_config.json`, `candidate.config.json`), which are large, carry the generating
  host's paths, and are reproducible from the study inputs.

The exact allowlist lives in the `/backtests` section of the repository `.gitignore`.
To check a path before committing:

```bash
git check-ignore -v backtests/binance/<study>/<path>
```

Files that are tracked may still contain absolute paths recorded at generation time (for
example `dataset.json`-derived entries inside a locked contract). Those are historical
records of the generating host; they do not affect reproduction.

## Current studies

| Study | Question it answers |
| --- | --- |
| `binance/2026-09-14T03_*` | Baseline three-year backtest of the default long trailing-martingale profile, plus constraint/fill and look-ahead audits. |
| `binance/causal_comparison_2026-09-14` | Whether the balance/equity export and drawdown shapes survive causal (T+1) replay, and where the 2025 drawdown came from minute by minute. |
| `binance/low_drawdown_strategy_study_2026-09-14` | Whether a lower-drawdown local strategy candidate exists, with a locked holdout. |
| `binance/maxdd_strategy_research_2026-09-14` | Strategy-path and parameter search under a drawdown cap, plus walk-forward validation. |
| `binance/deployability_research_2026-09-15` | Multi-coin economics, execution stress, and portfolio assembly on a small universe. |
| `binance/dd_tail_research_2026-09-15` | Why the default profile draws down 73.69% and which configuration levers reduce the tail; produces the published lower-tail profile. |

## Reproducing the published profile

The published profile is `configs/examples/trailing_martingale_twel100_ddf060.json`, which
differs from `configs/examples/default_trailing_martingale_long.json` in three behavioural
parameters, one optimizer bound, and the three `backtest` keys that state the reported
execution/cost contract. See `docs/strategy_profiles.md` for the parameter table, the evidence
summary, and the reproducibility boundaries.

```bash
# Rebuild the candidate artifact bundle, render its report, and verify the numbers.
bash backtests/binance/dd_tail_research_2026-09-15/run.sh

# Or run just the backtest for the profile itself.
passivbot backtest configs/examples/trailing_martingale_twel100_ddf060.json
```

## Execution and cost contracts

Numbers in this tree are only comparable inside one contract. The contract in force for published
evidence is recorded in the study's `research_contract_v4.json`:

| Contract | Regime | Latency | Maker / taker per side | Status |
| --- | --- | --- | --- | --- |
| v4 | `v4_binance_actual` | T+1 (`execution_delay_bars = 0`) | `0.0002` / `0.0005` | **Current.** Binance USDT-M VIP0. |
| v3 | `v3_conservative` | T+2 (`execution_delay_bars = 1`) | `0.0006` / `0.0008` | Frozen stress reference. Retained, never mixed into a v4 table. |

Both profiles are maker-only (`live.market_orders_allowed = false`, HSL panic closing disabled), so
the taker fee and `market_order_slippage_pct` never bind in either contract. A market-order fill
would need both a code path that emits one and a slippage assumption alongside it; neither the
default profile nor the lower-tail profile has one.

## Evidence boundaries

Results in this tree are 1-minute OHLC simulations under documented execution assumptions.
They are not live-trading performance and not a return forecast. Before quoting any number,
read the study's own scope section; the audits under
`binance/2026-09-14T03_40_41/` state the fill model, the candle-boundary contract, and the
parameter-time-travel caveat that applies to every historical replay in this tree.

`binance/dd_tail_research_2026-09-15/analysis/` additionally carries the overfitting review:
`anti_pattern_audit.md` maps known backtest anti-patterns (look-ahead, survivorship, cost and
latency optimism, selection bias) onto this evidence, and `overfitting_audit.json` holds the
combinatorially symmetric cross-validation that estimates probability of backtest overfitting
(PBO) for the lever screen behind the published profile.