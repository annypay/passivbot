#!/usr/bin/env bash
# Reproduce the g4 @ TWE 3.0 risk-geometry study end to end.
#
#   bash backtests/binance/g4_twe300_risk_optimization_2026-09-17/run.sh
#   bash backtests/binance/g4_twe300_risk_optimization_2026-09-17/run.sh --stage A
#   bash backtests/binance/g4_twe300_risk_optimization_2026-09-17/run.sh --leg pre
#   bash backtests/binance/g4_twe300_risk_optimization_2026-09-17/run.sh --variant a_allow000__ext
#   bash backtests/binance/g4_twe300_risk_optimization_2026-09-17/run.sh --verify-only
#   bash backtests/binance/g4_twe300_risk_optimization_2026-09-17/run.sh --search-smoke
#   bash backtests/binance/g4_twe300_risk_optimization_2026-09-17/run.sh --search
#
# The study keeps the production changes of the previous rounds (10,000 USDT, TWE 3.0) and asks
# how to buy the same survival more cheaply: occupancy discipline (we_excess_allowance_pct = 0),
# cooldown rungs (12/24/48/72h), a cumulative terminal rung placed above a single crash's
# confirmation drawdown, lower exposure limits, and a risk-geometry-only parameter search that
# is validated on an out-of-sample window the search never sees.
#
# Offline only: no network, no credentials, no exchange account, no bot start.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

export PATH="$HOME/.cargo/bin:$PATH"
export VIRTUAL_ENV="${VIRTUAL_ENV:-$REPO_ROOT/venv}"
PY="${PYTHON:-$REPO_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

STUDY="backtests/binance/g4_twe300_risk_optimization_2026-09-17"
TOOLS="$STUDY/report_tools"
#: A long-leg run peaks near 5 GB of resident memory; the native window near 2.5 GB.
MIN_AVAILABLE_MB_LONG="${MIN_AVAILABLE_MB_LONG:-5200}"
MIN_AVAILABLE_MB_SHORT="${MIN_AVAILABLE_MB_SHORT:-2600}"

FORCE=0
VERIFY_ONLY=0
ONLY_VARIANT=""
ONLY_LEG=""
ONLY_STAGE=""
MODE="run"
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --variant) ONLY_VARIANT="${2:?--variant needs an arm key}"; shift 2 ;;
    --leg) ONLY_LEG="${2:?--leg needs 3y|ext|pre}"; shift 2 ;;
    --stage) ONLY_STAGE="${2:?--stage needs A|B|C}"; shift 2 ;;
    --search-smoke) MODE="search-smoke"; shift ;;
    --search) MODE="search"; shift ;;
    --select-only) MODE="select-only"; shift ;;
    --fallback-grid) MODE="fallback-grid"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

export PYTHONPATH="$REPO_ROOT/src"

available_mb() { free -m | awk 'NR==2{print $7}'; }

require_memory() {
  local need="$1" avail
  avail="$(available_mb)"
  if [ "${avail:-0}" -lt "$need" ]; then
    echo "FAIL: only ${avail} MB of memory is available; this leg needs about ${need} MB." >&2
    echo "      stop other work (or lower MIN_AVAILABLE_MB_*) and retry." >&2
    exit 1
  fi
}

#: Every declared arm, in declaration order, taken from the study's own registry so this script
#: cannot drift from `variant_input.json`.
all_arms() {
  "$PY" - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path("backtests/binance/g4_twe300_risk_optimization_2026-09-17/report_tools").resolve()))
import variant_spec as spec
for key in spec.RUN_VARIANT_ORDER:
    print(key)
PY
}

leg_of() { printf '%s' "${1##*__}"; }
stage_of() {
  "$PY" - "$1" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path("backtests/binance/g4_twe300_risk_optimization_2026-09-17/report_tools").resolve()))
import variant_spec as spec
print(spec.stage_of(spec.VARIANTS_BY_KEY[sys.argv[1]]))
PY
}

memory_for_leg() {
  case "$1" in
    pre|ext) printf '%s' "$MIN_AVAILABLE_MB_LONG" ;;
    *) printf '%s' "$MIN_AVAILABLE_MB_SHORT" ;;
  esac
}

has_run() {
  local arm="$1"
  compgen -G "$STUDY/artifacts/$arm/backtest_results/binance_${arm}/binance/*/analysis.json" > /dev/null
}

ARMS=()
while read -r arm; do
  [ -z "$arm" ] && continue
  if [ -n "$ONLY_VARIANT" ] && [ "$ONLY_VARIANT" != "$arm" ]; then continue; fi
  if [ -n "$ONLY_LEG" ] && [ "$ONLY_LEG" != "$(leg_of "$arm")" ]; then continue; fi
  if [ -n "$ONLY_STAGE" ] && [ "$ONLY_STAGE" != "$(stage_of "$arm")" ]; then continue; fi
  ARMS+=("$arm")
done < <(all_arms)

if [ "$MODE" != "run" ]; then
  case "$MODE" in
    search-smoke) exec "$PY" "$TOOLS/run_search.py" --smoke ;;
    search) exec "$PY" "$TOOLS/run_search.py" ;;
    select-only) exec "$PY" "$TOOLS/run_search.py" --select-only ;;
    fallback-grid) exec "$PY" "$TOOLS/run_search.py" --fallback-grid ;;
  esac
fi

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "== 1/5 freeze every arm's config and the pre-registered study input =="
  # The freeze step is a pure function of the pinned parent config and the frozen search
  # selection (when the search has run), so re-running it is idempotent.
  "$PY" "$TOOLS/build_variant_config.py" --force || exit 1

  echo "== 2/5 replay the arms offline (one bundle per arm, ${#ARMS[@]} arm(s) selected) =="
  for arm in "${ARMS[@]}"; do
    if [ "$FORCE" -eq 0 ] && has_run "$arm"; then
      echo "-- $arm: a completed run already exists (use --force to redo it)"
      continue
    fi
    need="$(memory_for_leg "$(leg_of "$arm")")"
    require_memory "$need"
    run_args=(--variant "$arm")
    [ "$FORCE" -eq 1 ] && run_args+=(--force)
    echo "-- $arm (leg=$(leg_of "$arm") available memory: $(available_mb) MB)"
    "$PY" "$TOOLS/run_variant.py" "${run_args[@]}" || exit 1
  done

  echo "== 3/5 render one deep analysis per arm =="
  for arm in "${ARMS[@]}"; do
    has_run "$arm" || { echo "-- $arm: no run to render"; continue; }
    "$PY" "$TOOLS/generate_annual_report.py" --variant "$arm" || exit 1
  done

  echo "== 3b/5 re-derive the episode trace the mechanics document cites =="
  # The trace is a pure function of the run's ledger and frozen config, so this step is
  # idempotent. It is skipped for filtered runs (the arm would not have been replayed) and when
  # the arm has no bundle yet; the verifier re-derives it independently either way.
  if [ -z "$ONLY_VARIANT" ] && [ -z "$ONLY_LEG" ] && [ -z "$ONLY_STAGE" ]; then
    if has_run b_red015__3y; then
      "$PY" "$TOOLS/trace_episode.py" --variant b_red015__3y --coin ZEC \
        --start 2026-06-04T16:01:00+00:00 --end 2026-06-05T07:15:00+00:00 || exit 1
    else
      echo "-- b_red015__3y: no run to trace"
    fi
  else
    echo "-- filtered run: skipping the episode trace"
  fi
else
  echo "== verify-only: skipping freeze, replay and render =="
fi

echo "== 4/5 layout check: every bundle against the report convention =="
"$PY" - <<'PY' || exit 1
import sys
from pathlib import Path

sys.path.insert(0, str(Path("backtests/report_spec").resolve()))
sys.path.insert(0, str(Path("backtests/binance/g4_twe300_risk_optimization_2026-09-17/report_tools").resolve()))
import annual_analysis as spec
import variant_spec as study

problems = []
for key in study.RUN_VARIANT_ORDER:
    variant = study.VARIANTS_BY_KEY[key]
    try:
        run = study.find_variant_run_dir(variant)
    except SystemExit:
        problems.append(f"{key}: no run directory yet")
        continue
    config = spec.load_json(run / "config.json")
    found = spec.assert_bundle_layout(run, config=config, expect_plots=True)
    report_path = run / "annual_analysis.md"
    if not report_path.exists():
        found.append("missing annual_analysis.md")
    else:
        found.extend(spec.assert_report_structure(report_path.read_text(encoding="utf-8")))
    for problem in found:
        problems.append(f"{key}: {problem}")
    print(f"layout: {len(found)} problem(s) in {study.relative(run)}")
for problem in problems:
    print(f"  - {problem}", file=sys.stderr)
raise SystemExit(1 if problems else 0)
PY

echo "== 5/5 independent verification, synthesis and its own verification =="
"$PY" "$TOOLS/verify_variant_report.py" --all --allow-missing || exit 1
"$PY" "$TOOLS/build_synthesis.py" --allow-missing || exit 1
"$PY" "$TOOLS/verify_synthesis.py" --allow-missing || exit 1

echo
echo "done: $STUDY/risk_geometry_analysis.md"
echo "      per-arm reports: $STUDY/artifacts/<arm>/backtest_results/binance_<arm>/binance/<UTC timestamp>/annual_analysis.md"
