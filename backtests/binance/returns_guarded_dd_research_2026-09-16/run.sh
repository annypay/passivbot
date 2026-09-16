#!/usr/bin/env bash
# Produce the study's artifact bundles, check their layout, and verify their reports.
#
#   bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh
#   bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --cells seed
#   bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --verify-only
#   bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --force
#   bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --audit-gates
#
# Each bundle runs the frozen study window and the reported execution/cost contract, so a
# bundle's numbers are comparable with the study's own cells and with the report convention in
# `docs/ai/runbooks/strategy_report.md`.
#
# Bundles run the full figure set, including the per-coin panels, so each run directory is
# complete on the convention's own terms rather than relying on a disabled group to explain a
# missing panel. The per-coin panels are the memory peak and get killed on a small host: the
# analytical artifacts and the figures are written before that point, `run_record.json` records
# the process exit code next to an `artifact_status`, and `check_bundle_layout.py` proves the
# directory is complete anyway.
#
# A study cell is not a locked candidate: the candidate lock this study writes exists to fix the
# holdout comparison, not to gate a published profile, so verification runs with
# --no-expect-locked-ops and --no-require-lock.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

# maturin needs cargo, and a non-interactive shell does not read ~/.cargo/env.
export PATH="$HOME/.cargo/bin:$PATH"
export VIRTUAL_ENV="${VIRTUAL_ENV:-$REPO_ROOT/venv}"
PY="${PYTHON:-$REPO_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

STUDY="backtests/binance/returns_guarded_dd_research_2026-09-16"
TOOLS="$STUDY/report_tools"
RUNNER="$TOOLS/run_study.py"
DEFAULT_BUNDLES=(seed best_capped best_dd_reducer long_short_flip)

BUNDLES=("${DEFAULT_BUNDLES[@]}")
VERIFY_ONLY=0
FORCE=0
AUDIT_GATES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --cells)
      shift
      BUNDLES=()
      while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do
        BUNDLES+=("$1")
        shift
      done
      ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --force) FORCE=1; shift ;;
    --audit-gates) AUDIT_GATES=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$AUDIT_GATES" -eq 1 ]; then
  echo "== gate causality audit (time boundary and complement) =="
  for cell in g4_sma20_50 g4_sma30_60 g4_sma30_60_initial_only g4_sma10_30_confirm3 g4_sma60_120; do
    PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/audit_gate_causality.py" --cell "$cell" --coins 4
  done
fi

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "== 1/3 produce artifact bundles and render reports =="
  args=(--cells "${BUNDLES[@]}")
  [ "$FORCE" -eq 1 ] && args+=(--force)
  PYTHONPATH="$REPO_ROOT/src" "$PY" "$RUNNER" bundle "${args[@]}" || exit 1
fi

echo "== 2/3 check each run directory against the layout contract =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/check_bundle_layout.py" || exit 1

echo "== 3/3 verify each report independently =="
PYTHONPATH="$REPO_ROOT/src" "$PY" "$TOOLS/verify_bundles.py" || exit 1

echo
echo "done: reports verified under $STUDY/artifacts/<bundle>/backtest_results/binance_<bundle>/binance/<UTC timestamp>/annual_analysis.md"
echo "comparison: $STUDY/artifacts/profile_comparison.md"
