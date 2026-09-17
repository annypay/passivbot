#!/usr/bin/env bash
# Reproduce the g4 tail-risk ("wipe-out") study end to end.
#
#   bash backtests/binance/g4_tail_risk_research_2026-09-17/run.sh
#   bash backtests/binance/g4_tail_risk_research_2026-09-17/run.sh --force
#   bash backtests/binance/g4_tail_risk_research_2026-09-17/run.sh --verify-only
#   bash backtests/binance/g4_tail_risk_research_2026-09-17/run.sh --variant unified_r10__3y
#   bash backtests/binance/g4_tail_risk_research_2026-09-17/run.sh --leg ext
#   bash backtests/binance/g4_tail_risk_research_2026-09-17/run.sh --skip-synthetic
#
# The study replays the frozen g4 profile on two frozen HLCV bundles (its native 3-year
# window and the 5.4-year local history) plus two synthetic collapse bundles, renders one
# deep analysis per arm, checks every bundle against the report convention and recomputes
# the numbers with an independent verifier. The cross-arm conclusion lands in
# `tail_risk_analysis.md`.
#
# Offline only: no network, no credentials, no exchange account, no bot start.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

# maturin needs cargo, and a non-interactive shell does not read ~/.cargo/env.
export PATH="$HOME/.cargo/bin:$PATH"
export VIRTUAL_ENV="${VIRTUAL_ENV:-$REPO_ROOT/venv}"
PY="${PYTHON:-$REPO_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

STUDY="backtests/binance/g4_tail_risk_research_2026-09-17"
TOOLS="$STUDY/report_tools"
#: A long-leg run peaks near 5.0 GB of resident memory; refuse to start one without headroom.
MIN_AVAILABLE_MB="${MIN_AVAILABLE_MB:-5200}"
#: Arms in execution order. The 5.4-year leg runs last: it is the memory-hungry leg, and a
#: failure there must not cost the cheaper evidence.
ORDER_3Y=(
  pside_r15__3y unified_r15__3y unified_r10__3y unified_r10_fast__3y
  unified_r10_market__3y unified_r10_never__3y twe_090__3y allowance_000__3y
  twel_enforcer_070__3y
)
ORDER_SYNTH=(off__synth_a unified_r10__synth_a off__synth_b unified_r10__synth_b)
ORDER_EXT=(
  off__ext coin__ext pside_r15__ext unified_r15__ext unified_r10__ext
  unified_r10_fast__ext unified_r10_market__ext unified_r10_never__ext twe_090__ext
  allowance_000__ext twel_enforcer_070__ext max_realized_050__ext
)

FORCE=0
VERIFY_ONLY=0
SKIP_SYNTHETIC=0
ONLY_VARIANT=""
ONLY_LEG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --skip-synthetic) SKIP_SYNTHETIC=1; shift ;;
    --variant) ONLY_VARIANT="${2:?--variant needs an arm key}"; shift 2 ;;
    --leg) ONLY_LEG="${2:?--leg needs 3y|ext|syn}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

export PYTHONPATH="$REPO_ROOT/src"

available_mb() { free -m | awk 'NR==2{print $7}'; }

require_memory() {
  local avail
  avail="$(available_mb)"
  if [ "${avail:-0}" -lt "$MIN_AVAILABLE_MB" ]; then
    echo "FAIL: only ${avail} MB of memory is available; an arm needs about ${MIN_AVAILABLE_MB} MB." >&2
    echo "      stop other work (or lower MIN_AVAILABLE_MB) and retry." >&2
    exit 1
  fi
}

arm_dir() { echo "$STUDY/artifacts/$1"; }

has_run() {
  local arm="$1"
  compgen -G "$(arm_dir "$arm")/backtest_results/binance_${arm}/binance/*/analysis.json" > /dev/null
}

selected_arms() {
  local leg="$1"
  case "$leg" in
    3y) printf '%s\n' "${ORDER_3Y[@]}" ;;
    syn) printf '%s\n' "${ORDER_SYNTH[@]}" ;;
    ext) printf '%s\n' "${ORDER_EXT[@]}" ;;
  esac
}

ARMS_TO_RUN=()
for leg in 3y syn ext; do
  [ -n "$ONLY_LEG" ] && [ "$ONLY_LEG" != "$leg" ] && continue
  if [ "$leg" = "syn" ] && [ "$SKIP_SYNTHETIC" -eq 1 ]; then continue; fi
  while read -r arm; do
    [ -z "$arm" ] && continue
    if [ -n "$ONLY_VARIANT" ] && [ "$ONLY_VARIANT" != "$arm" ]; then continue; fi
    ARMS_TO_RUN+=("$arm")
  done < <(selected_arms "$leg")
done
if [ -n "$ONLY_VARIANT" ] && [ "${#ARMS_TO_RUN[@]}" -eq 0 ]; then
  echo "FAIL: $ONLY_VARIANT is not a runnable arm of this study" >&2
  exit 2
fi

if [ "$VERIFY_ONLY" -eq 0 ]; then
  if [ "$SKIP_SYNTHETIC" -eq 0 ] && { [ -z "$ONLY_LEG" ] || [ "$ONLY_LEG" = "syn" ]; }; then
    echo "== 1/7 build the synthetic collapse bundles (derived copies of the native bundle) =="
    synth_args=()
    [ "$FORCE" -eq 1 ] && synth_args+=(--force)
    require_memory
    "$PY" "$TOOLS/make_synthetic_collapse_bundle.py" "${synth_args[@]}" || exit 1
  else
    echo "== 1/7 synthetic bundles: skipped =="
  fi

  echo "== 2/7 freeze every arm's config and the pre-registered study input =="
  # The freeze step is a pure function of the pinned parent config, so re-running it is
  # idempotent: identical content produces identical configs and identical hashes. It runs
  # after the synthetic build so `variant_input.json` records the derived bundles.
  "$PY" "$TOOLS/build_variant_config.py" --force || exit 1

  echo "== 3/7 replay the arms offline (one bundle per arm) =="
  for arm in "${ARMS_TO_RUN[@]}"; do
    if [ "$FORCE" -eq 0 ] && has_run "$arm"; then
      echo "-- $arm: a completed run already exists (use --force to redo it)"
      continue
    fi
    require_memory
    run_args=(--variant "$arm")
    [ "$FORCE" -eq 1 ] && run_args+=(--force)
    echo "-- $arm (available memory: $(available_mb) MB)"
    "$PY" "$TOOLS/run_variant.py" "${run_args[@]}" || exit 1
  done

  echo "== 4/7 render one deep analysis per arm =="
  for arm in "${ARMS_TO_RUN[@]}"; do
    has_run "$arm" || { echo "-- $arm: no run to render"; continue; }
    "$PY" "$TOOLS/generate_annual_report.py" --variant "$arm" || exit 1
  done
else
  echo "== verify-only: skipping freeze, replay and render =="
fi

echo "== 5/7 layout check: every bundle against the report convention =="
"$PY" - <<'PY' || exit 1
import sys
from pathlib import Path

sys.path.insert(0, str(Path("backtests/report_spec").resolve()))
sys.path.insert(0, str(Path("backtests/binance/g4_tail_risk_research_2026-09-17/report_tools").resolve()))
import annual_analysis as spec
import variant_spec as study

problems = []
checked = 0
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
    checked += 1
    for problem in found:
        problems.append(f"{key}: {problem}")
    print(f"layout: {len(found)} problem(s) in {study.relative(run)}")
for problem in problems:
    print(f"  - {problem}", file=sys.stderr)
raise SystemExit(1 if problems else 0)
PY

echo "== 6/7 independent verification of every arm =="
"$PY" "$TOOLS/verify_variant_report.py" --all --allow-missing || exit 1

echo "== 7/7 cross-arm synthesis and its consistency check =="
"$PY" "$TOOLS/build_tail_risk_synthesis.py" --allow-missing || exit 1
"$PY" "$TOOLS/verify_tail_risk_synthesis.py" || exit 1

echo
echo "done: $STUDY/tail_risk_analysis.md"
echo "      per-arm reports: $STUDY/artifacts/<arm>/backtest_results/binance_<arm>/binance/<UTC timestamp>/annual_analysis.md"
