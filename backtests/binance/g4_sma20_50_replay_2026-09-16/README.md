# g4_sma20_50 Replay: The Published Gated Profile

This study replays one thing and reports on it: the **published** profile
`configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json`, run as an offline backtest
over the frozen three-year dataset, with the convention's full deep analysis rendered next to it.

It exists because the gate cell `g4_sma20_50` of
[`returns_guarded_dd_research_2026-09-16`](../returns_guarded_dd_research_2026-09-16/) became a
tracked profile. Before that, the only artifact for this configuration was a *study cell* — a
result record plus a bundle inside the study's own tree. This directory is the standalone
artifact for the shipped file, so a reader can go from the profile on disk to its evidence
without reading a 48-cell screen.

## What Makes This Replay Different From The Study Cell

The replay runs the published profile and **rebuilds no strategy configuration**: it never derives
the config from a seed plus ops, and it patches no strategy, risk, exit or gate parameter. The
builder proves that instead of asserting it, by comparing the frozen config against the tracked
profile path by path and naming every difference.

Those named differences are **data identity**, and they are unavoidable. The published profile is an
*operational* config: an open-ended window (`start_date = 2021-04-20`, `end_date = "now"`), 41
candidate coins approved on both sides, and two exchanges as data sources. The recorded evidence is a
*fixed* three-year, single-exchange, 40-coin dataset (`MNT` has no usable history in the window). A
run that honoured the operational window would need more warmup than the frozen bundle holds — it
would miss the cache and rebuild the dataset from the network, which is both outside this study's
offline boundary and a silent change of subject. The retargets are:

| Key | Published profile | This replay | Why |
| --- | --- | --- | --- |
| `backtest.base_dir` | `backtests` | this study's `artifacts/backtest_results` | where the run directory is written |
| `backtest.exchanges` | `["binance", "bybit"]` | `["binance"]` | the evidence is single-exchange |
| `backtest.start_date` | `2021-04-20` | `2023-09-12` | the evidence's window |
| `backtest.end_date` | `"now"` | `2026-09-12` | the evidence's window |
| `backtest.coins` | unset (41 candidates) | the frozen 40 | the evidence's universe |
| `backtest.cache_dir` | unset | the frozen bundle | so the run cannot rebuild or fetch |
| `live.approved_coins` | 41 long / 41 short | 40 long / 0 short | the evidence's traded universe |

The gate is checked rather than assumed, in the opposite direction from a "no gate" guard:
`backtest.entry_regime_gate` must be present, must be `enabled`, and must match the declared
20/50 parameters field by field. This matters because an ungated run of this profile is a *different
strategy* — roughly 29% worst drawdown instead of roughly 10.5%. A run that silently lost its gate
fails rather than producing a report about the wrong strategy.

The replay also refuses to report a run that went to the network: it requires the run log to show the
frozen bundle being loaded from cache and to contain no fetch markers.

## Artifacts

| Path | Contents |
| --- | --- |
| `artifacts/g4_sma20_50.config.json` | The frozen config the replay ran: the published profile with only the data-identity keys above retargeted. |
| `artifacts/profile_input.json` | The study-cell metrics the replay must reproduce, plus profile/contract hashes. |
| `artifacts/execution_audit.csv` | One row per fill, streaming decision/activation/fill bar indices. |
| `artifacts/logs/replay_run.log` | The backtest's own log for this run. |
| `artifacts/backtest_results/binance/binance/<UTC timestamp>/` | The run directory: `annual_analysis.md`, the three metric CSVs, `analysis.json`, `fills.csv`, the equity series, the figures and the per-coin fill panels. |

HLCV arrays are never copied here. The run's `dataset.json` records which bundle it read, and the
verifier proves identity by **content**: it recomputes the logical array hashes of both this run's
bundle and the source study's bundle and requires them to be equal, rather than comparing cache
directory names. That distinction is not pedantic — this machine holds three byte-identical aliases
of the same dataset under different `config_hash` names
(`…__2c11e36fd7fc3806`, `…__8300950b42789a26`, `…__b7430cfe3649eab0`), all with matching array
hashes, and the loader may resolve any of them. Name equality would be the wrong test; array equality
is the right one.

## Reproduce

```bash
bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh
```

The script freezes the config, runs the replay, renders the report, checks the run directory
against the layout contract in
[`strategy_report.md`](../../../docs/ai/runbooks/strategy_report.md), and verifies the report
independently. `--verify-only` re-checks an existing bundle; `--force` re-runs from scratch.

Offline only. The replay never downloads candles: it requires the frozen bundle to be present
and aborts if the run resolves a different dataset.

## Reading The Report

The report follows the section skeleton in
[`strategy_report.md`](../../../docs/ai/runbooks/strategy_report.md) and adds a study appendix
with four things the skeleton has no slot for:

1. **The declared gate** and its constraint that the gate can only suppress entries — closes,
   panic and auto-unstuck keep their own paths.
2. **Agreement with the source study's own artifact** for the same cell. Two independent
   replays of one configuration; systematic drift here would invalidate every other reading.
3. **Three-column comparison** against the lower-tail profile (the same strategy, ungated) and
   the un-gated default profile.
4. **What the gate actually changed**, read from the fill ledger: entries fall, exits do not,
   and position sizing is untouched.

Offline-only boundary: no network, no credentials, no exchange account, no order, no bot start.
Public-facing material must stay reproducible from this repository alone.
