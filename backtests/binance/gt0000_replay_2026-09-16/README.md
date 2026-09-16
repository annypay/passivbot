# g3_cth0000 offline replay: full artifact bundle and deep analysis

This study replays **one declared cell** of the frozen drawdown research
(`backtests/binance/returns_guarded_dd_research_2026-09-16`, cell `g3_cth0000`, group
`G3_scaleout`, window `full`, scenario `C1_binance_actual`) as a **complete backtest artifact
bundle**, and renders it with the repository's strategy-research report convention
(`docs/ai/runbooks/strategy_report.md`, renderer `backtests/report_spec/annual_analysis.py`) so the
result can be read side by side with `backtests/binance/2026-09-14T03_40_41/annual_analysis.md`.

The study cell itself only stored a metrics `result.json`. A full bundle (`analysis.json`,
`fills.csv`, the equity series, plots, the three summary CSVs and a deep-analysis report) requires
the canonical backtest entrypoint, which is what `report_tools/run_replay.py` drives.

## Safety boundary

Offline only, and enforced rather than asserted:

* no network fetch, no credentials, no exchange account, no order creation/cancellation, no bot
  start; the run log is `artifacts/logs/replay_run.log`;
* the replay must be served by the frozen 1-minute HLCV bundle
  `caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__8300950b42789a26`
  — `report_tools/run_replay.py` fails the run if the produced `dataset.json` names a different
  dataset, and it re-verifies the bundle's logical array hashes against `manifest.json`;
* the frozen run config is the one recorded in the study cell's `result.json`; a build-time gate
  proves it is `configs/examples/trailing_martingale_twel100_ddf060.json` plus exactly the one
  declared op, and refuses to write it otherwise.

## Reported change (the whole delta against the seed profile)

| Path | Seed | This artifact |
| --- | --- | --- |
| `bot.long.strategy.trailing_martingale.close.threshold_base_pct` | `-0.0027` | **`0.0`** |

Execution/cost regime: `execution_delay_bars = 0` (T+1), `intrabar_fill_order = close_first`,
maker `0.0002` / taker `0.0005` — the research contract's `C1_binance_actual` scenario.

## Engine identity

The replay ran with the worktree engine present at run time. The Rust source fingerprint and the
compiled extension stamp are recorded in
`artifacts/backtest_results/binance/<run>/global_metrics.json` and cited by the report. The
worktree carried uncommitted changes when the replay ran; that is recorded, not hidden, and it
means the frozen baseline `2026-09-14T03_40_41` was produced by a different engine revision
(the report says so where the two are compared).

## Contents

| Path | Content |
| --- | --- |
| `artifacts/g3_cth0000.config.json` | byte-exact run config, taken from the study cell's `result.json` |
| `artifacts/cell_input.json` | the study-cell metrics the replay must reproduce, plus contract/seed hashes |
| `artifacts/execution_audit.csv` | streamed per-fill execution provenance (not tracked; regenerable) |
| `artifacts/logs/replay_run.log` | full backtest log of the replay |
| `artifacts/backtest_results/binance/<UTC run>/` | the artifact bundle: `analysis.json`, `config.json`, `dataset.json`, `fills.csv`, `balance_and_equity.csv.gz`, PNG plots, `fills_plots/`, `run_record.json`, `global_metrics.json` |
| `artifacts/backtest_results/binance/<UTC run>/annual_analysis.md` | the deep-analysis report (fixed skeleton + study appendix) |
| `.../annual_metrics.csv`, `.../monthly_metrics.csv`, `.../coin_metrics.csv` | the tables behind the report |
| `report_tools/cell_spec.py` | single source of truth: paths, declared ops, frozen dataset, contract constants |
| `report_tools/build_cell_config.py` | freezes the config and runs the seed-equivalence gate |
| `report_tools/run_replay.py` | runs the backtest, enforces the frozen-dataset gate, writes provenance |
| `report_tools/generate_annual_report.py` | renders the report and the three CSVs |
| `report_tools/verify_replay_report.py` | independent recomputation and claim checking |

The run directory is named from the UTC completion timestamp by the backtest itself, so a re-run
lands in a new dated directory rather than overwriting this one.

## Reproduce

```bash
cd <repo root>
venv/bin/python backtests/binance/gt0000_replay_2026-09-16/report_tools/build_cell_config.py
venv/bin/python backtests/binance/gt0000_replay_2026-09-16/report_tools/run_replay.py
venv/bin/python backtests/binance/gt0000_replay_2026-09-16/report_tools/generate_annual_report.py
venv/bin/python backtests/binance/gt0000_replay_2026-09-16/report_tools/verify_replay_report.py
```

`build_cell_config.py` refuses to overwrite the frozen inputs without `--force`. To collect a run
that was already produced (for example after a plotting failure late in a run), pass
`run_replay.py --reuse-run <timestamp-directory>`.

## Verify

`report_tools/verify_replay_report.py` recomputes every reported number from `fills.csv`,
`balance_and_equity.csv.gz`, `execution_audit.csv`, the run's `config.json`/`dataset.json` and the
frozen bundle, with code that does not import the report generator, and checks that the rendered
report actually contains those values. It also re-implements the bundle's logical-array hash and
re-hashes one frozen artifact against `manifest.json`.

Warnings (a claim that could not be fully verified) are printed and do not fail the run by
default; `--fail-on-warnings` turns them into failures. Regression coverage for the study tooling
is `tests/test_gt0000_replay_report.py`.

Known endpoint effect, reported rather than smoothed over: the equity sampler's last row is not the
last backtest minute, so a few fills can sit after the final equity sample. The period tables stop
at the last sample, and the report states the resulting difference between the period-table net
PnL total and the full fills-ledger total.
