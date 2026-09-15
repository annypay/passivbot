#!/usr/bin/env bash
# Reproduce an artifact bundle and its deep-analysis report under the study's reported contract.
#
#   bash backtests/binance/dd_tail_research_2026-09-15/run.sh                  # locked candidate
#   bash backtests/binance/dd_tail_research_2026-09-15/run.sh --baseline       # default profile
#   bash backtests/binance/dd_tail_research_2026-09-15/run.sh --profile PATH   # any profile
#
# Every bundle runs the frozen study window and the reported execution/cost contract, so its
# numbers are comparable with the study's own cells. The candidate is additionally rebuilt by
# applying `holdout_candidate_lock.json` to the published default profile, and is the only bundle
# the verifier expects to carry the locked ops. Each bundle gets its own directory under
# `artifacts/`.
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
BASELINE_PROFILE="configs/examples/default_trailing_martingale_long.json"
BASELINE_CELL="cells/full/C1_binance_actual/baseline/result.json"
CANDIDATE_CELL="cells/full/C1_binance_actual/combo_twel100_ddf060_ddthr0030/result.json"

MODE="candidate"
PROFILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --baseline)
      MODE="baseline"
      PROFILE="$BASELINE_PROFILE"
      shift
      ;;
    --profile)
      MODE="profile"
      PROFILE="${2:?--profile needs a config path}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$MODE" in
  candidate) SUBDIR="binance_actual_candidate" ;;
  baseline) SUBDIR="binance_actual_baseline" ;;
  profile) SUBDIR="binance_actual_$(basename "$PROFILE" .json)" ;;
esac

PY_ARGS=(--artifacts-subdir "$SUBDIR" --study-window full)
# Candidate mode rebuilds the candidate from the lock; the other modes run the profile as-is.
# Both must name a config explicitly, otherwise the tool falls back to the locked-ops baseline.
if [ "$MODE" = "candidate" ]; then
  PY_ARGS+=(--candidate-config "$BASELINE_PROFILE" --apply-locked-ops)
else
  PY_ARGS+=(--candidate-config "$PROFILE")
fi

echo "== 1/3 run the backtest under the reported contract ($MODE) =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/run_candidate_artifacts.py" "${PY_ARGS[@]}"

echo "== 2/3 render annual/monthly/coin tables and the report =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/generate_annual_report.py" --artifacts-subdir "$SUBDIR"

echo "== 3/3 verify report against artifacts =="
VERIFY_ARGS=(--artifacts-subdir "$SUBDIR")
if [ "$MODE" = "candidate" ]; then
  VERIFY_ARGS+=(--study-cell "$CANDIDATE_CELL" --expect-locked-ops)
else
  # A reference profile reproduces the baseline geometry, not the locked candidate.
  VERIFY_ARGS+=(--study-cell "$BASELINE_CELL" --no-expect-locked-ops)
fi
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/verify_annual_report.py" "${VERIFY_ARGS[@]}"

echo "done: see backtests/binance/dd_tail_research_2026-09-15/artifacts/$SUBDIR/backtest_results/binance/<run>/annual_analysis.md"