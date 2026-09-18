#!/usr/bin/env bash
# Reproduce the g4 @ TWE 3.0 HSL halt-ladder study end to end.
#
#   bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh
#   bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh --stage L1
#   bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh --leg pre
#   bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh --variant l0_off__ext
#   bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh --verify-only
#
# The study replays two brand-new, default-off engine features on the previous round's validated
# `a_allow000` geometry (allowance 0, TWE 3.0, unified guard RED 0.20 / EMA 60 min / 12h halt):
# the halt ladder `bot.long.hsl.halt_ladder_minutes` and the cumulative realized-loss budget
# `bot.long.hsl.realized_loss_budget_pct`. There is **no parameter search** in this round: the
# four arms are pre-registered in `report_tools/variant_spec.py` and the criteria in
# `halt_ladder_design.md` §7.
#
# Offline only: no network, no credentials, no exchange account, no bot start.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

export PATH="$HOME/.cargo/bin:$PATH"
export VIRTUAL_ENV="${VIRTUAL_ENV:-$REPO_ROOT/venv}"
PY="${PYTHON:-$REPO_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

STUDY="backtests/binance/g4_hsl_halt_ladder_2026-09-18"
TOOLS="$STUDY/report_tools"
#: A long-leg run peaks near 5 GB of resident memory; the native window near 2.6 GB. Only one leg
#: may run at a time on this host, so the guard is checked before every arm.
MIN_AVAILABLE_MB_LONG="${MIN_AVAILABLE_MB_LONG:-5200}"
MIN_AVAILABLE_MB_SHORT="${MIN_AVAILABLE_MB_SHORT:-2600}"

FORCE=0
VERIFY_ONLY=0
ONLY_VARIANT=""
ONLY_LEG=""
ONLY_STAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --variant) ONLY_VARIANT="${2:?--variant needs an arm key}"; shift 2 ;;
    --leg) ONLY_LEG="${2:?--leg needs 3y|ext|pre}"; shift 2 ;;
    --stage) ONLY_STAGE="${2:?--stage needs L0|L1|L2|L3}"; shift 2 ;;
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
sys.path.insert(0, str(Path("backtests/binance/g4_hsl_halt_ladder_2026-09-18/report_tools").resolve()))
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
sys.path.insert(0, str(Path("backtests/binance/g4_hsl_halt_ladder_2026-09-18/report_tools").resolve()))
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

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "== 1/5 freeze every arm's config and the pre-registered study input =="
  # A pure function of the pinned parent config and the declared arms: re-running it is idempotent.
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
else
  echo "== verify-only: skipping freeze, replay and render =="
fi

echo "== 4/5 layout check: every selected bundle against the report convention =="
# The check is scoped to the arms this invocation selected (`--variant/--leg/--stage`), so a
# filtered run can complete; an unfiltered run still checks all twelve.
"$PY" - "${ARMS[@]}" <<'PY' || exit 1
import sys
from pathlib import Path

sys.path.insert(0, str(Path("backtests/report_spec").resolve()))
sys.path.insert(0, str(Path("backtests/binance/g4_hsl_halt_ladder_2026-09-18/report_tools").resolve()))
import annual_analysis as spec
import variant_spec as study

keys = sys.argv[1:] or list(study.RUN_VARIANT_ORDER)
problems = []
for key in keys:
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

echo "== 5/5 independent verification of every arm with a bundle =="
# Unfiltered runs verify the whole registry (`--all --allow-missing`, as before). A filtered run
# verifies exactly the arms it selected, so one arm can be taken through the pipeline on its own.
if [ -n "$ONLY_VARIANT$ONLY_LEG$ONLY_STAGE" ]; then
  for arm in "${ARMS[@]}"; do
    "$PY" "$TOOLS/verify_variant_report.py" --variant "$arm" --allow-missing || exit 1
  done
else
  "$PY" "$TOOLS/verify_variant_report.py" --all --allow-missing || exit 1
fi

echo
echo "done: per-arm reports: $STUDY/artifacts/<arm>/backtest_results/binance_<arm>/binance/<UTC timestamp>/annual_analysis.md"
echo "      cross-arm readings: $STUDY/README.md (results section) and $STUDY/halt_ladder_design.md (frozen criteria)"
