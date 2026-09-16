# Strategy Research Report Convention

A **strategy research deep analysis** is the report that ships next to a completed backtest
artifact bundle. Every such report uses the same section skeleton, the same table
columns, the same data conventions and the same on-disk layout, so two reports from different
studies can be read side by side and their bundles can be verified the same way.

Frozen reference sample: `../../../backtests/binance/2026-09-14T03_25_14/annual_analysis.md`. It is the
authority for wording and layout; it is never edited. The renderer that implements this convention
is `../../../backtests/report_spec/annual_analysis.py`; `REPORT_SECTIONS`, the column constants and
the bundle-layout constants in that module are the machine-readable form of this document.

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

## Artifact Persistence And On-Disk Format

A deep analysis is only authoritative if its numbers can be re-derived from what was left on disk.
This section is the normative answer to **where every artifact class is written and what it is
named**. It applies to every study, whether or not that study ships its own tooling.

### Rule 1 — One report, one run, one directory

The report is always named annual_analysis.md. "Annual" is the convention's name, not a claim
about the window length: a 30-day run and a 5-year run produce the same file name. It is written
into the **run directory it describes**, never into a separate reports tree, never into the study
root, and never in place over another run.

That run directory is named by the backtest from its **UTC completion timestamp**, so every run
produces its own dated directory and nothing is overwritten:

```
backtests/<exchange>/<study>/artifacts/<bundle>/backtest_results/<exchange>[_label]/<exchange>/<UTC timestamp>/
```

Two shapes exist and both are valid:

- **nested** — studies with their own `artifacts/` tree, the normal case, shape above;
- **flat** — studies that keep the profile's own `backtest.base_dir`:
  `backtests/<exchange>/<UTC timestamp>/`.

Because the directory name carries the run timestamp, the date on a report's folder is the date that
run was produced, not the date the report was last re-rendered.

### Rule 2 — Every artifact class has one name and one location

| Artifact class | Exact path | Written by |
|---|---|---|
| Deep analysis report | `<run dir>/annual_analysis.md` | report tool / shared renderer |
| Annual table | `<run dir>/annual_metrics.csv` | report tool / shared renderer |
| Monthly table | `<run dir>/monthly_metrics.csv` | report tool / shared renderer |
| Per-coin table | `<run dir>/coin_metrics.csv` | report tool / shared renderer |
| Native metrics | `<run dir>/analysis.json` | backtest |
| Fill ledger | `<run dir>/fills.csv` | backtest |
| Sampled equity | `<run dir>/balance_and_equity.csv.gz` | backtest |
| Effective run config | `<run dir>/config.json` | backtest |
| Dataset identity | `<run dir>/dataset.json` | backtest |
| Summary figures | `<run dir>/<figure>.png` | backtest |
| Per-coin fill panels | `<run dir>/fills_plots/<COIN>.png` | backtest |
| Frozen run config | `<study>/artifacts/<name>.config.json` | study tooling |
| Run log | `<study>/artifacts/logs/<name>.log` | study tooling |

The streamed execution audit is the one artifact whose path a study chooses: it is written
wherever `backtest.execution_audit_path` points (the reference studies use
`<study>/artifacts/<bundle>/execution_audit.csv`, or `<study>/artifacts/execution_audit.csv` for a
single-bundle study). The run's own `config.json` records that path, and the layout check verifies
it resolves to a real file rather than pinning a name.

Figure names are the backtest's, not the study's: `balance_and_equity.png`,
`balance_and_equity_logy.png`, `drawdown.png`, `total_wallet_exposure.png`, `pnl_cumsum.png`,
`hard_stop_drawdown.png` when hard-stop data exists, and `scenario_equity_comparison.png` for a
multi-scenario suite run. `backtest.disable_plotting` drops figure **groups** — per-coin panels are
the memory peak of a long run, so a study may disable `coin_fills` and keep the summary figures, but
it must say which groups it disabled in `## 口径与范围`.

Never invent a parallel name for an artifact that already has one, and never move a report out of
its run directory to build a "reports" folder. When a study needs extra evidence (an audit matrix, a
stress table, a separate analysis note), add it next to the report in the same run directory and
reference it from `## 可复核数据`.

### Rule 3 — The dataset stays in the cache; the run records its identity

HLCV arrays are never copied into a study or a run directory. They live in
`caches/hlcvs_data/<exchange>__<coins>__<start>_to_<end>__<cache_hash>/`, and the run's
`dataset.json` records the identity that reproduces them: `cache_dir_label`, `cache_hash`, the coin
list, the requested window, and the `content_hashes` of `hlcvs`, `timestamps` and `btc_usd_prices`.

A report cites that identity in `## 可复核数据`. "Which data was this?" is answered by
`dataset.json`, never by a path copied out of prose. When a run materializes its dataset from the
local candle catalog rather than from a pre-built bundle, the report says so, and the run must not
have downloaded anything.

### Rule 4 — Re-runs get their own directory; bundles hold exactly one

The report tooling picks the single run directory under a bundle and **refuses to guess** when there
is more than one. A re-run therefore either replaces the bundle's run directory or is filed under a
new `--label NAME` group:

```
backtest_results/binance_candidate/binance/<UTC timestamp>/
backtest_results/binance_baseline/binance/<UTC timestamp>/
```

Use a label to keep parallel bundles, or a candidate/baseline pair, apart. Do not point report
tooling at an ambiguous bundle; pass `--result-dir` explicitly when several runs must coexist.

### Rule 5 — Tracked evidence versus reproducible output

The tracked/local split is the repository's, defined by the `/backtests` rules in
`../../../.gitignore` and summarized in `../../../backtests/readme.md`. Two consequences bind a study:

- **Tracked:** `*.md`, `*.py`, `*.sh`, and JSON that is not a compiled config. A run's
  `analysis.json` is tracked, so the numbers a report cites survive a fresh checkout.
- **Local and regenerable:** `fills.csv`, `execution_audit.csv`, `balance_and_equity.csv.gz`,
  `*.npy`, `*.npz`, `*.png`, `*.pyc`, `*.log`, the three metric CSVs, and the compiled configs
  `config.json`, `config.original.json`, `dataset.json`, `study_input_config.json`,
  `candidate.config.json`, `emitted.config.json`.

Never paste host-specific absolute paths into tracked evidence. A compiled config that records the
generating host's paths stays local; when a report needs provenance, cite the source config's
repository-relative path plus its sha256.

### Rule 6 — The layout is checkable, and is checked

`backtests/report_spec/annual_analysis.py` owns the executable form of Rules 1–3 as
`BUNDLE_REPORT_FILES`, `BUNDLE_LOCAL_FILES`, `BUNDLE_FIGURE_FILES`, `BUNDLE_PLOT_DIRS` and
`assert_bundle_layout`. A study's `run.sh` or the shared CLI must fail rather than report success
when the persisted bundle is incomplete, and a study's verifier re-checks the layout as well as the
numbers. Changing this document without changing those constants — or the reverse — is a defect.

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
  (report the `live.approved_coins.<side>` count and `live.hedge_mode`), or **not configured**.
  Never drop the row and never claim the direction is unavailable.
- **Maker-only books.** When `taker_fills_count` is zero, say so explicitly and state that the
  taker fee and `market_order_slippage_pct` therefore do not affect the result.

## Metric Table Files

The annual and monthly tables share one schema, one row per period:

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

The per-coin table is written for every study; it carries `coin`, the fill split, the PnL split,
`max_abs_wallet_exposure_at_fill`, and the first/last fill timestamps. It is ordered alphabetically
by `coin`, while the report's `## 按币种贡献` table is ordered by net realized PnL descending. Both
orders are fixed so that a diff of either artifact is meaningful.

**Deliberate deviation from the reference.** The reference report's period CSVs end at
`max_abs_wallet_exposure_at_fill`. This convention adds `active_coins` as the final column so a
period row states how many coins traded. The reference's other 25 columns keep their exact names
and order, so a period row is still comparable column by column.

## Report-Level Rules

- **Never mix execution or cost contracts in one table.** Each contract regime is stated where its
  numbers appear, and cross-regime comparisons are made in prose, not by subtracting table cells.
- **Every number is derived from the artifact directory** or from an explicitly cited study cell.
  A hand-typed figure is a defect, not a convenience.
- **A derived value with no meaningful value is never printed as `nan`.** Report `n/a`, or restate
  the claim with a denominator that exists. A `nan%` or `nan USDT` in prose is a defect.
- **State the contract, do not imply it.** Latency, `intrabar_fill_order`, maker and taker rates,
  and the research-contract version all appear in `## 口径与范围`.
- **State where the strategy was actually active.** A long window does not imply a long-lived
  strategy. When fills are concentrated in a fraction of the window — a halt, a hard stop, a late
  start, or a universe that only becomes tradable partway — the report says so explicitly and names
  the first and last fill, so zero-fill periods are not misread as flat performance.
- **Keep the skeleton machine-checkable.** `report_spec.assert_report_structure` validates the
  section order and `assert_bundle_layout` validates the persisted files, so a renderer regression
  fails verification instead of shipping a report that quietly stopped matching this convention.

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
write a report whose skeleton or persisted bundle fails validation.

To regenerate a report for an existing run without re-running the backtest, call the same tooling
with that run directory: the rewrite happens **in that run directory**.

## Validation

Run the study's own report and verifier:

```bash
bash backtests/binance/dd_tail_research_2026-09-15/run.sh              # candidate bundle
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --baseline   # baseline bundle
```

`verify_annual_report.py` recomputes every reported number from the fills ledger and equity series
with code that does not import the renderer, then checks the section skeleton, the per-year detail
sections, the persisted bundle layout, and that the three metric CSVs match the schemas above. The
CSVs themselves are local-only (gitignored) build products, so that last check is what keeps the
writer and this document from drifting apart. Regression coverage for the convention itself is
`../../../tests/test_annual_analysis_report_spec.py`; the persistence rules above are additionally
pinned by `../../../tests/test_ai_docs.py`.
