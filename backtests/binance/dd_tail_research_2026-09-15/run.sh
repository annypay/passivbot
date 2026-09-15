#!/usr/bin/env bash
# Reproduce the candidate artifact bundle and its deep-analysis report.
#
#   bash backtests/binance/dd_tail_research_2026-09-15/run.sh
#
# The scripts resolve the repository root from their own location, but the run happens
# from the repository root because the HLCV cache root (`caches/hlcvs_data`) is a
# repository-relative path. Requires the local HLCV cache for the study window; a cache
# miss re-materializes from the offline store rather than downloading.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-$REPO_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

TOOLS="backtests/binance/dd_tail_research_2026-09-15/report_tools"

echo "== 1/3 rebuild candidate config and run the backtest =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/run_candidate_artifacts.py" "$@"

echo "== 2/3 render annual/monthly/coin tables and the report =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/generate_annual_report.py"

echo "== 3/3 verify report against artifacts =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/verify_annual_report.py"

echo "done: see backtests/binance/dd_tail_research_2026-09-15/artifacts/backtest_results/binance/<run>/annual_analysis.md"
