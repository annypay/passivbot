# Strategy Research Report Convention

A **strategy research deep analysis** is the report that ships next to a completed backtest
artifact bundle. Every such report uses the same section skeleton, the same table
columns and the same data conventions, so two reports from different studies can be read side by
side.

Frozen reference sample: `../../../backtests/binance/2026-09-14T03_25_14/annual_analysis.md`. It is the
authority for wording and layout; it is never edited. The renderer that implements this convention
is `../../../backtests/report_spec/annual_analysis.py`; `REPORT_SECTIONS` and the column constants
in that module are the machine-readable form of this document.

## Section Skeleton

These sections appear in this exact order. Per-year detail sections and the optional study appendix
slot in where marked; nothing else may be inserted.

| # | Section | Required content |
|---|---|---|
| 1 | `## 口径与范围` | Strategy/config provenance and hashes; every parameter changed relative to the baseline; data source and candle interval; frozen universe size and how it differs from the configured list; execution and cost contract; equity/sampling convention; the `fills.csv` timestamp caveat; the simulation disclaimer; the offline-safety boundary. |
| 2 | `## 总体结果` | Single two-column table `项目 / 数值`: result directory, data source and candle interval, effective window, days and data completion, starting/final USD total balance, starting/final USD total equity, `gain_usd`, USD worst drawdown / strategy-equity worst drawdown, worst-1% mean drawdown, PnL Sharpe / Sortino, longest PnL peak recovery / longest position hold, fill count / liquidation flag. |
| 3 | `## 多空成交归因` | Table `方向 / 成交数 / 入场数 / 已实现 PnL / 手续费 / 净已实现 PnL / 成交时最大绝对钱包敞口`, always exactly two rows (多头, 空头). |
| 4 | `## 自然年汇总` | Table `年份 / 覆盖 / 采样区间 / 起始余额 / 最终余额 / 余额收益率 / 权益收益率 / 年内权益最大回撤 / 成交数 / 净已实现 PnL`, one row per natural year, ascending. |
| 5 | `### <年> 明细（<覆盖状态>）` | One level-3 section per natural year, in the same order, each a two-column `项目 / 数值` table: sample interval, USD total balance start → end, USD total equity start → end, strategy equity start → end, the three return percentages, intra-year equity and strategy-equity max drawdown, fills total/entry/reduction, direction and maker/taker counts, realized PnL / fees / net realized PnL, max absolute wallet exposure at fill. |
| 6 | `## 月度汇总` | Table `月份 / 覆盖 / 最终余额 / 余额收益率 / 权益收益率 / 月内权益最大回撤 / 成交数 / 净已实现 PnL`, one row per calendar month, ascending. |
| 7 | `## 按币种贡献` | Multi-coin studies only. Table `币种 / 成交数 / 入场 / 减仓或平仓 / 已实现 PnL / 手续费 / 净已实现 PnL / 成交时最大绝对钱包敞口`, sorted by net realized PnL descending. Omitted for a single-coin study, as in the reference. |
| 8 | `## 研究附录` | Optional. The single mount point for study-specific sections (for example: agreement with the study's primary contract, baseline comparison, measured risk caps, execution and look-ahead boundaries, holdout and cost sensitivity). Omitted when the study has nothing to add. |
| 9 | `## 可复核数据` | Table of the artifact files and what each contains, plus the provenance hashes (source config, candidate config, research contract and cell-matrix hash, candidate lock, Rust source fingerprint). |
| 10 | `## 结果解读` | Prose observations. Must cover all four of: (a) balance and strategy-equity growth with the `gain_*` multiples and the warning that they use tail daily equity, (b) per-period attribution naming the best year and the worst-drawdown year, (c) risk: worst drawdown(s), worst-1% mean drawdown, longest recovery, underwater share, and the risk-adjusted ratios, (d) trade structure: long vs short fills and maker vs taker fills with their net PnL. Ends with an explicit statement that the report is not a forecast. |

`## 可复核数据` and `## 结果解读` are always last, in that order.

## Data Conventions

- **Time.** All timestamps are UTC. Table timestamps use `YYYY-MM-DD HH:MM`; the per-year detail
  tables use `YYYY-MM-DD HH:MM:SS+00:00` with a **space** separator, not `T`. Use one form
  everywhere: `Timestamp.isoformat()` emits `T`, and `to_csv` re-parses a `T`-form string back into
  a datetime and rewrites it with a space, so a mixed convention makes a CSV disagree with the
  report that describes it.
- **Sampling.** The balance/equity series is sampled at `backtest.balance_sample_divider` minutes.
  The report states the actual value and whether that is the same resolution as `analysis.json`;
  do not copy the reference's "hourly" wording, which describes that run's configuration.
- **Fill attribution.** Fills are attributed to their `fills.csv` timestamp, which is the **open
  label of the 1-minute candle** the fill belongs to, not a confirmed exchange execution time.
- **Fees.** `fee_paid` keeps its source sign. Net realized PnL = `pnl + fee_paid`.
- **Entry vs reduction.** Types starting with `entry_` count as entries; everything else counts as
  reduction or close, which may include unstuck or risk-reducing closes.
- **Coverage.** Exactly three states: `起始非完整` (first period), `完整` (middle periods),
  `结束非完整` (last period). The last bin is capped after the final sample so no empty trailing
  period is invented.
- **Direction.** A fill is 多头 or 空头 by whether its type contains `long` or `short`.
- **One-sided books.** The attribution table always has two rows. When a direction has zero fills,
  keep the row and state which case it is: the direction is **configured but was not triggered**
  (report `live.approved_coins.<side>` count and `live.hedge_mode`), or **not configured**. Never
  drop the row and never claim the direction is unavailable.
- **Maker-only books.** When `taker_fills_count` is zero, say so explicitly and state that the
  taker fee and `market_order_slippage_pct` therefore do not affect the result.

## Metric Table Files

`annual_metrics.csv` and `monthly_metrics.csv` share one schema, one row per period:

```
period, sample_start_utc, sample_end_utc, coverage,
starting_total_balance_usd, ending_total_balance_usd, total_balance_return_pct,
starting_total_equity_usd, ending_total_equity_usd, total_equity_return_pct,
starting_strategy_equity, ending_strategy_equity, strategy_equity_return_pct,
max_intraperiod_equity_drawdown_pct, max_intraperiod_strategy_equity_drawdown_pct,
fills_count, entry_fills_count, reduction_or_close_fills_count,
long_fills_count, short_fills_count, maker_fills_count, taker_fills_count,
realized_pnl_raw_usd, fees_signed_usd, net_realized_pnl_usd,
max_abs_wallet_exposure_at_fill, active_coins
```

`coin_metrics.csv` is written for every study; it carries `coin`, the fill split, the PnL split,
`max_abs_wallet_exposure_at_fill`, and the first/last fill timestamps.

**Deliberate deviation from the reference.** The reference report's period CSVs end at
`max_abs_wallet_exposure_at_fill`. This convention adds `active_coins` as the final column so a
period row states how many coins traded. The reference's other 25 columns keep their exact names
and order, so a period row is still comparable column by column.

## Report-Level Rules

- **Never mix execution or cost contracts in one table.** Each contract regime is stated where its
  numbers appear, and cross-regime comparisons are made in prose, not by subtracting table cells.
- **Every number is derived from the artifact directory** or from an explicitly cited study cell.
  A hand-typed figure is a defect, not a convenience.
- **State the contract, do not imply it.** Latency, `intrabar_fill_order`, maker and taker rates,
  and the research-contract version all appear in `## 口径与范围`.
- **Keep the skeleton machine-checkable.** `report_spec.assert_report_structure` validates the
  section order, and `verify_annual_report.py` runs it, so a renderer regression fails verification
  instead of shipping a report that quietly stopped matching this convention.

## Rendering

For a study that has its own report tool (the normal case), that tool supplies the study-specific
scope lines and appendix and delegates the skeleton and tables to the shared renderer. The reference
implementation is
`../../../backtests/binance/dd_tail_research_2026-09-15/report_tools/generate_annual_report.py`; copy that pattern for a new study.

For a completed artifact directory with no study tooling, the shared module renders it directly:

```bash
PYTHONPATH=src python backtests/report_spec/annual_analysis.py \
  --result-dir backtests/<exchange>/<study>/artifacts/<bundle>/backtest_results/<exchange>/<run> \
  --run-record backtests/<exchange>/<study>/artifacts/<bundle>/run_record.json
```

It writes the rendered report file plus the three CSVs into the result directory and refuses to
write a report whose skeleton fails validation.

## Where the report is written

A report is rendered into the **run directory it describes**, never into a separate reports tree and
never in place over another run. The backtest names that directory from the UTC completion
timestamp, so every run produces a dated directory:

```
backtests/<exchange>/<study>/artifacts/<bundle>/backtest_results/<exchange>[_label]/<exchange>/<UTC timestamp>/
  annual_analysis.md      # this convention
  annual_metrics.csv      # the tables behind it
  monthly_metrics.csv
  coin_metrics.csv
  analysis.json           # the numbers it cites
  fills.csv, balance_and_equity.csv.gz, execution_audit.csv, *.png, fills_plots/
```

`--label NAME` on the study's `run.sh` groups the run under a labeled exchange directory, so
parallel bundles and re-runs stay identifiable while every run keeps its own timestamped
directory. Because the folder name carries the run timestamp, the date on a report's
folder is the date that run was produced — a report re-rendered later still lives in its own run
directory. To regenerate a report for an existing run without re-running the backtest:

```bash
PYTHONPATH=src python <study>/report_tools/generate_annual_report.py --artifacts-subdir <bundle>
```

which rewrites the report and the three CSVs **in that run directory**.

## Validation

Run the study's own report and verifier:

```bash
bash backtests/binance/dd_tail_research_2026-09-15/run.sh              # candidate bundle
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --baseline   # baseline bundle
```

`verify_annual_report.py` recomputes every reported number from the fills ledger and equity series
with code that does not import the renderer, then checks the section skeleton, the per-year detail
sections, and that the three metric CSVs match the schemas above. The CSVs themselves are
local-only (gitignored) build products, so that last check is what keeps the writer and this
document from drifting apart. Regression coverage
for the convention itself is `../../../tests/test_annual_analysis_report_spec.py`.