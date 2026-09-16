#!/usr/bin/env bash
# Reproduce the `hsl_npos1` deep-analysis artifact bundle.
#
#   bash backtests/binance/hsl_npos1_analysis_2026-09-16/run.sh                # full run
#   bash backtests/binance/hsl_npos1_analysis_2026-09-16/run.sh --force        # re-run the backtest
#   bash backtests/binance/hsl_npos1_analysis_2026-09-16/run.sh --report-only  # re-render + verify
#
# Three steps:
#
#   1. build_run_config.py    freeze the published profile into artifacts/hsl_npos1.config.json
#   2. run_backtest.py        run `python -m backtest` and file the run under artifacts/
#   3. render_hsl_npos1_report.py + verify_hsl_npos1.py
#
# Offline. The run config pins the repository's local HLCV catalog, so the backtest materializes
# candles from `caches/ohlcvs` and never contacts an exchange. No credentials, no account, no
# orders, no bot start.
#
# Output layout. The backtest names its run directory from the UTC completion timestamp, e.g.
# `2026-09-16T08_08_28`, and this study keeps the profile's own `base_dir`, so the run appears in
# `backtests/binance/<timestamp>/` and the runner then moves it into
# `artifacts/backtest_results/binance/<timestamp>/`. The report tooling picks the single completed
# run directory there and refuses to guess when there is more than one.
#
# Plotting. Per-coin fill panels are the memory peak of a run; the analytical artifacts are
# written before plotting starts, so `backtest.disable_plotting = coin_fills` keeps the summary
# figures and drops the panels.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-$REPO_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

STUDY="backtests/binance/hsl_npos1_analysis_2026-09-16"
TOOLS="$STUDY/report_tools"

MODE="full"
while [ $# -gt 0 ]; do
  case "$1" in
    --force) MODE="force" ;;
    --report-only) MODE="report-only" ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$MODE" = "report-only" ]; then
  echo "== render the report from the existing artifact =="
  PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/render_hsl_npos1_report.py"
  echo "== verify =="
  PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/verify_hsl_npos1.py"
  echo "done: see $STUDY/artifacts/backtest_results/binance/<UTC timestamp>/annual_analysis.md"
  exit 0
fi

echo "== 1/2 build the frozen run config =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/build_run_config.py"

echo "== 2/2 run the backtest =="
if [ "$MODE" = "force" ]; then
  PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/run_backtest.py" --force
else
  PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/run_backtest.py"
fi

echo "== 3/3 render the report and verify it =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/render_hsl_npos1_report.py"
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/verify_hsl_npos1.py"

echo "done: see $STUDY/artifacts/backtest_results/binance/<UTC timestamp>/annual_analysis.md"
