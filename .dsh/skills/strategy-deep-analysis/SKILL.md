---
name: strategy-deep-analysis
description: Run a backtest and persist a strategy-research deep analysis (report, metric tables, figures, fills plots) in the repository's canonical artifact-bundle format.
whenToUse: Use when asked to backtest a config profile or study cell and produce a deep analysis, annual_analysis.md, or an artifact bundle with its figures and dataset identity; also use when asked where such a report, image, or dataset belongs on disk.
---

# Strategy deep analysis

Produce a completed artifact bundle plus the report that describes it, in the layout the repository
already uses.

## The contract is not in this file

`docs/ai/runbooks/strategy_report.md` is the single normative source for:

- the fixed report section skeleton;
- every artifact's exact path and file name, including figures and `fills_plots/`;
- where the dataset lives and how a run records its identity;
- the tracked-versus-local split;
- the data conventions and report-level rules.

Read it before starting. `backtests/report_spec/annual_analysis.py` is its executable half:
`REPORT_SECTIONS`, `PERIOD_CSV_COLUMNS`, `COIN_CSV_COLUMNS`, `BUNDLE_*` and `assert_bundle_layout`.
If this skill and that runbook ever disagree, the runbook wins.

## Procedure

1. **Decide the run contract before running anything.** Window, coin basket, execution and cost
   parameters, and the equity sampling resolution. Write them into a config the run actually uses,
   and keep `bot`/`coin_overrides` byte-equal to the published profile when the task is to analyse
   that profile; only retarget `backtest` keys plus `live.approved_coins`.
2. **Pin the run.** Hash the source profile and fail loudly when it changes. Never hand-edit the
   example profile to make a run work.
3. **Run offline.** Point the run at the local HLCV catalog so it materializes candles locally.
   A cache miss must re-materialize; it must not download.
4. **File the run, do not scatter it.** The backtest names its run directory from the UTC
   completion timestamp; the report and its tables are written **into that directory**.
5. **Render with the shared renderer.** Delegate the skeleton and tables to
   `backtests/report_spec/annual_analysis.py`; supply only study-specific scope lines and appendix.
6. **Verify with independent code.** A verifier that does not import the renderer recomputes every
   published number from the ledger and equity series and re-checks the bundle layout.
7. **State the negatives.** Which figure groups were disabled and why; which coins were excluded
   and why; where the strategy was actually active; which direction had no fills.

## Failure modes this repo has already hit

Each of these shipped or nearly shipped a wrong report. Check for them explicitly.

- **A dataset override that is not a bundle.** `backtest.hlcvs_data_dir` must point at a
  materialized bundle (a directory with `manifest.json`), not at the raw candle catalog.
- **A coin the local data cannot serve.** Leaving such a coin in `live.approved_coins` makes the
  run resolve its market metadata and fetch it, and writes a synthetic, gap-filled series into the
  dataset. Exclude it from `live.approved_coins` *and* `backtest.coins`, before the run.
- **Reading `backtest.coins` after the run.** Preparation consumes it; the written run config no
  longer has it. The materialized `dataset.json` is the authority for the run basket.
- **A misleading window.** A run that trades for a small fraction of its window (halt, hard stop,
  late start, universe that becomes tradable partway) must say so, or zero-fill periods read as
  flat performance.
- **`nan` in prose.** A derived value with no meaningful value is reported as `n/a` or restated
  with a denominator that exists; it is never printed as `nan%` or `nan USDT`.
- **Two orders for one artifact.** A CSV's row order and the report table's row order are both
  fixed; check both instead of assuming one.
- **Cells compared with an absolute tolerance only.** Ledger values are five and six figures, so
  comparisons need a relative tolerance.
- **A study tool module named like another study's.** Study tools are imported by basename, so give
  them study-unique module names.
- **Trusting a keyword grep for "offline".** Log lines from a post-run step in the same process can
  look like a fetch. Check timestamps and position, not just words.

## Validation

```bash
bash backtests/<exchange>/<study>/run.sh --report-only   # re-render and verify
pytest tests/test_annual_analysis_report_spec.py tests/test_ai_docs.py -q
PYTHONPATH=src python src/tools/check_ai_docs.py
```

The verifier must print `failed: 0`, and the layout check must report a complete bundle. Do not
report the analysis as finished while either is failing.
