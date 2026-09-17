#!/usr/bin/env bash
# Reproduce the g4 @ TWE 3.0 / 10,000 USDT replay end to end.
#
#   bash backtests/binance/g4_twe300_10k_replay_2026-09-17/run.sh
#   bash backtests/binance/g4_twe300_10k_replay_2026-09-17/run.sh --verify-only
#   bash backtests/binance/g4_twe300_10k_replay_2026-09-17/run.sh --variant twe300_10k__ext
#   bash backtests/binance/g4_twe300_10k_replay_2026-09-17/run.sh --leg ext
#   bash backtests/binance/g4_twe300_10k_replay_2026-09-17/run.sh --force
#
# The study replays the frozen g4 profile with two declared production changes (starting
# capital 100,000 -> 10,000 and total wallet exposure limit 1.0 -> 3.0) plus a hard-floor arm
# and two structural alternatives, on the profile's native window and on the 5.4-year local
# history. Each arm gets a deep analysis in its own run directory; the cross-arm answer lands
# in `twe300_10k_analysis.md`.
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

STUDY="backtests/binance/g4_twe300_10k_replay_2026-09-17"
TOOLS="$STUDY/report_tools"
#: A long-leg run peaks near 5 GB of resident memory; refuse to start one without headroom.
MIN_AVAILABLE_MB="${MIN_AVAILABLE_MB:-5200}"
#: Arms in execution order: the cheaper native-window leg first, then the 5.4-year stress leg
#: (where liquidation is expected), so a failure there cannot cost the native-window evidence.
ORDER_3Y=(twe100_10k__3y twe300_10k__3y twe300_10k_floor__3y twe300_10k_allowance0__3y)
ORDER_EXT=(
  twe100_10k__ext twe300_10k__ext twe300_10k_floor__ext twe300_10k_allowance0__ext
  twe300_10k_never__ext
)

FORCE=0
VERIFY_ONLY=0
ONLY_VARIANT=""
ONLY_LEG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --variant) ONLY_VARIANT="${2:?--variant needs an arm key}"; shift 2 ;;
    --leg) ONLY_LEG="${2:?--leg needs 3y|ext}"; shift 2 ;;
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

has_run() {
  local arm="$1"
  compgen -G "$STUDY/artifacts/$arm/backtest_results/binance_${arm}/binance/*/analysis.json" > /dev/null
}

selected_arms() {
  case "$1" in
    3y) printf '%s\n' "${ORDER_3Y[@]}" ;;
    ext) printf '%s\n' "${ORDER_EXT[@]}" ;;
  esac
}

ARMS=()
for leg in 3y ext; do
  [ -n "$ONLY_LEG" ] && [ "$ONLY_LEG" != "$leg" ] && continue
  while read -r arm; do
    [ -z "$arm" ] && continue
    if [ -n "$ONLY_VARIANT" ] && [ "$ONLY_VARIANT" != "$arm" ]; then continue; fi
    ARMS+=("$arm")
  done < <(selected_arms "$leg")
done
if [ -n "$ONLY_VARIANT" ] && [ "${#ARMS[@]}" -eq 0 ]; then
  echo "FAIL: $ONLY_VARIANT is not a runnable arm of this study" >&2
  exit 2
fi

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "== 1/6 freeze every arm's config and the pre-registered study input =="
  # The freeze step is a pure function of the pinned parent config, so re-running it is
  # idempotent: identical content produces identical configs and identical hashes.
  "$PY" "$TOOLS/build_variant_config.py" --force || exit 1

  echo "== 2/6 replay the arms offline (one bundle per arm) =="
  for arm in "${ARMS[@]}"; do
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

  echo "== 3/6 live-admission appendix (offline, from the frozen market settings) =="
  for arm in "${ARMS[@]}"; do
    has_run "$arm" || { echo "-- $arm: no run to summarise"; continue; }
    "$PY" "$TOOLS/affordability.py" --variant "$arm" || exit 1
  done

  echo "== 4/6 render one deep analysis per arm =="
  for arm in "${ARMS[@]}"; do
    has_run "$arm" || { echo "-- $arm: no run to render"; continue; }
    "$PY" "$TOOLS/generate_annual_report.py" --variant "$arm" || exit 1
  done
else
  echo "== verify-only: skipping freeze, replay, affordability and render =="
fi

echo "== 5/6 layout check: every bundle against the report convention =="
"$PY" - <<'PY' || exit 1
import sys
from pathlib import Path

sys.path.insert(0, str(Path("backtests/report_spec").resolve()))
sys.path.insert(0, str(Path("backtests/binance/g4_twe300_10k_replay_2026-09-17/report_tools").resolve()))
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

echo "== 6/6 independent verification, synthesis and its own verification =="
"$PY" "$TOOLS/verify_variant_report.py" --all --allow-missing || exit 1
"$PY" "$TOOLS/build_synthesis.py" --allow-missing || exit 1
"$PY" "$TOOLS/verify_synthesis.py" --allow-missing || exit 1

echo
echo "done: $STUDY/twe300_10k_analysis.md"
echo "      per-arm reports: $STUDY/artifacts/<arm>/backtest_results/binance_<arm>/binance/<UTC timestamp>/annual_analysis.md"
