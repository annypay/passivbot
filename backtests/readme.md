# Backtest Research Evidence

This tree is version-controlled so a fresh checkout can trace **how a strategy profile was
chosen** without re-running multi-hour studies. It is research evidence, not a release
artifact: nothing here is installed, packaged, or required at runtime.

## What is tracked, and what is not

Tracked (small, hard to regenerate, human-readable):

- `*.md` — study reports, audits, and deep analyses.
- `*.py` and `*.sh` — the exact study scripts, plus the run entry point that reproduces a study.
- `*.json` — research contracts, candidate locks, manifests, per-configuration result and
  metric summaries, plus each artifact bundle's run record and `analysis.json`.
- `annual_analysis.md` — the rendered deep-analysis report for each tracked artifact bundle. Every such report follows `docs/ai/runbooks/strategy_report.md`: one fixed section skeleton, one table schema, one set of data conventions, so reports from different studies are directly comparable.

Not tracked (large, regenerable from the tracked inputs plus the local HLCV cache):

- `fills.csv`, `execution_audit.csv`, `balance_and_equity.csv.gz`
- `*.npy`, `*.npz`, `*.png`, `*.pyc`, `*.log`
- every per-run bookkeeping tree named `runs/`
- compiled runtime configs (`config.json`, `config.original.json`, `dataset.json`,
  `study_input_config.json`, `candidate.config.json`), which are large, carry the generating
  host's paths, and are reproducible from the study inputs.
- one-shot authoring scratch in a study's `report_tools/` (`_patch_*.py`, `_probe_*.py`,
  `_diag_*.py`, ...), which is written against the generating host and is not reproducible from
  the study input; the importable helper a study's tools load (`_study_module.py`) is kept.

The exact allowlist lives in the `/backtests` section of the repository `.gitignore`.
To check a path before committing:

```bash
git check-ignore -v backtests/binance/<study>/<path>
```

Files that are tracked may still contain absolute paths recorded at generation time (for
example `dataset.json`-derived entries inside a locked contract). Those are historical
records of the generating host; they do not affect reproduction. A study's own frozen run config
does not lean on that licence: it states where it writes its run directory and its execution audit
repository-relative, so a fresh checkout reproduces the run without editing a host path.

## Current studies

| Study | Question it answers |
| --- | --- |
| `binance/2026-09-14T03_*` | Baseline three-year backtest of the default long trailing-martingale profile, plus constraint/fill and look-ahead audits. |
| `binance/causal_comparison_2026-09-14` | Whether the balance/equity export and drawdown shapes survive causal (T+1) replay, and where the 2025 drawdown came from minute by minute. |
| `binance/low_drawdown_strategy_study_2026-09-14` | Whether a lower-drawdown local strategy candidate exists, with a locked holdout. |
| `binance/maxdd_strategy_research_2026-09-14` | Strategy-path and parameter search under a drawdown cap, plus walk-forward validation. |
| `binance/deployability_research_2026-09-15` | Multi-coin economics, execution stress, and portfolio assembly on a small universe. |
| `binance/dd_tail_research_2026-09-15` | Why the default profile draws down 73.69% and which configuration levers reduce the tail; produces the published lower-tail profile. |
| `binance/hsl_npos1_analysis_2026-09-16` | What the published HSL-enabled trailing-martingale example actually did over its own declared window (2021-04-20 .. 2026-09-12): the rule set traded for 30 days, hit a hard stop on 2021-05-19, and produced no further fills for the remaining 1,942 days. Its deep analysis is the reference for that profile. |
| `binance/returns_guarded_dd_research_2026-09-16` | Whether the three-year return multiple survives a ~30% drawdown cap: a 48-cell screen over ladder geometry, path-dependent stops, scale-out shape, and a new strictly causal daily-SMA entry-regime gate, plus the measured drawdown/return frontier. |
| `binance/g4_sma20_50_replay_2026-09-16` | Standalone deep analysis of the published gated profile: replays `trailing_martingale_twel100_ddf060_sma20_50.json` offline over the frozen bundle and reports it next to the un-gated profile and the study cell. |
| `binance/g4_sma20_50_hsl_on_replay_2026-09-17` | 打开 HSL 的变体重放：同一份冻结配置在当前引擎上跑两次（HSL 关的配对对照 + HSL 开），三列对比（tracked 基线 / 配对对照 / HSL 开）给出 HSL 的运行学、账本代价与「是否降低尾部回撤」的样本内结论。中文 README 见该 study 目录。 |

## Reproducing the published profiles

The published profiles are:

- `configs/examples/trailing_martingale_twel100_ddf060.json`, which differs from
  `configs/examples/default_trailing_martingale_long.json` in three behavioural parameters, one
  optimizer bound, and the three `backtest` keys that state the reported execution/cost contract.
- `configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json`, which is the profile above
  plus a daily 20/50 entry-regime gate, and nothing else. Its standalone artifact and deep analysis
  are `binance/g4_sma20_50_replay_2026-09-16/`.

See `docs/strategy_profiles.md` for the parameter tables, the evidence summary, and the
reproducibility boundaries. Both profiles are reproducible from the studies below: the lower-tail
profile from `dd_tail_research_2026-09-15`, and the gated profile from the `best_dd_reducer` bundle
of `returns_guarded_dd_research_2026-09-16` (study cell `g4_sma20_50`) or, standalone, from
`g4_sma20_50_replay_2026-09-16`.

```bash
# Return-preserving drawdown screen (48 cells, offline local HLCV).
cd "$(git rev-parse --show-toplevel)"
venv/bin/python backtests/binance/returns_guarded_dd_research_2026-09-16/report_tools/run_study.py \
  run --cells all --windows full --scenarios C1_binance_actual
venv/bin/python backtests/binance/returns_guarded_dd_research_2026-09-16/report_tools/render_tables.py

# Artifact bundles plus their deep-analysis reports, then verify both.
bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh
bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --cells seed --force
bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --verify-only

# Rebuild the locked candidate's artifact bundle, render its report, and verify the numbers.
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --label candidate

# Same for the baseline the candidate is compared against, and for any other profile.
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --baseline --label baseline
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --profile configs/examples/<name>.json

# Or run just the backtest for a profile itself, with its own window and fees.
passivbot backtest configs/examples/trailing_martingale_twel100_ddf060.json
passivbot backtest configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json

# Rebuild and verify the gated profile's bundle inside the study (study cell `g4_sma20_50`).
bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --cells best_dd_reducer --force

# The gated profile's standalone deep analysis, with its own bundle and verifier.
bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh
bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh --verify-only

# HSL variant (paired control + HSL on), with layout check and independent verification
bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh
bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh --verify-only
```

Every `run.sh` mode runs the frozen study window and the reported contract, so the bundles are
directly comparable with the study's own cells:

| Bundle | Bundle directory | Study cell it must reproduce |
| --- | --- | --- |
| Locked candidate | `artifacts/binance_actual_candidate/` | `cells/full/C1_binance_actual/combo_twel100_ddf060_ddthr0030` |
| Baseline profile | `artifacts/binance_actual_baseline/` | `cells/full/C1_binance_actual/baseline` |

## Run directory layout and dating

The backtest names its run directory from the **UTC completion timestamp**, so every run produces a
new dated directory and nothing is overwritten. `--label NAME` groups the run under a labeled
exchange directory, which is what keeps parallel bundles apart:

```
<study>/artifacts/binance_actual_candidate/backtest_results/binance_candidate/binance/2026-09-16T01_22_18/
<study>/artifacts/binance_actual_baseline/backtest_results/binance_baseline/binance/2026-09-16T01_24_14/
```

The tracked archive of that run is its `analysis.json` and `annual_analysis.md`; the fills ledger,
equity series, audit and figures stay local. A report is always rendered into the run directory it
describes, so the date on the folder is the date the run was produced, not the date it was last
edited.

Because a bundle holds one run, the report tooling picks the single run directory under it and
refuses to guess when there is more than one. Re-running into a bundle therefore replaces the run
directory rather than piling up beside it; point the tooling at a specific run with `--result-dir`
if you want to keep several.

`returns_guarded_dd_research_2026-09-16/run.sh` enforces that one-run-per-bundle rule: a
`--force` re-run clears the bundle's previous run directory before the new backtest
starts, because the run directory is named from the completion timestamp and a second
one would otherwise make the report tooling refuse to pick.

That study's bundles run the full figure set. The analytical artifacts (analysis,
config, fills, balance/equity, dataset) are written before the figure tail, so if the
tail is killed the bundle still renders and verifies; `run_record.json` records both
the process exit code and an `artifact_status`, and each run directory carries a copy
of the streamed `execution_audit.csv` that the verifier cross-checks against
`fills.csv`.

The verifier compares the artifact's window against the cell's window before claiming agreement, so
a bundle run over a different window reports that difference instead of failing three metric checks.
`passivbot backtest` on the same profile is a different exercise: it uses the profile's own window
(`end_date: now`) and its own fee overrides, so its numbers will not equal these.

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

## Report format

Deep analyses use the fixed section order, table columns, data conventions and **on-disk artifact
layout** defined in `docs/ai/runbooks/strategy_report.md`, rendered by
`backtests/report_spec/annual_analysis.py`.
`verify_annual_report.py` recomputes every reported number from the fills ledger and equity series
and checks that skeleton, so a report that drifts from the convention fails verification.

The runbook's "Artifact Persistence And On-Disk Format" section is the normative answer to where a
report, its figures, its fills panels and its dataset identity live. Its executable half is
`BUNDLE_REPORT_FILES`, `BUNDLE_LOCAL_FILES`, `BUNDLE_FIGURE_FILES`, `BUNDLE_PLOT_DIRS` and
`assert_bundle_layout` in `backtests/report_spec/annual_analysis.py`, checked by
`tests/test_annual_analysis_report_spec.py` and `tests/test_ai_docs.py`. HLCV arrays stay in
`caches/hlcvs_data/`; a run records their identity in its `dataset.json` instead of copying them. The frozen
reference sample is `binance/2026-09-14T03_25_14/annual_analysis.md`.

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