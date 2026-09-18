#!/usr/bin/env bash
# Reproduce the g4 @ TWE 3.0 single-coin stop-loss study end to end.
#
#   bash backtests/binance/g4_twe300_sl_cooldown_2026-09-18/run.sh
#   bash backtests/binance/g4_twe300_sl_cooldown_2026-09-18/run.sh --stage S1
#   bash backtests/binance/g4_twe300_sl_cooldown_2026-09-18/run.sh --leg synth_a
#   bash backtests/binance/g4_twe300_sl_cooldown_2026-09-18/run.sh --variant s1_sl15_cd1440__3y
#   bash backtests/binance/g4_twe300_sl_cooldown_2026-09-18/run.sh --verify-only
#
# The study replays one brand-new, default-off engine key on the previous round's validated
# `a_allow000` geometry: `bot.<pside>.stop_loss` (average-entry stop, whole-position reduce-only
# close, entry cooldown after it fills). There is **no parameter search**: the six arms are
# pre-registered in `report_tools/variant_spec.py` and the criteria are declared in
# `report_tools/sl_replay.py` (`THRESHOLDS`).
#
# Offline only: no network, no credentials, no exchange account, no bot start.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-$REPO_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

STUDY="backtests/binance/g4_twe300_sl_cooldown_2026-09-18"
DRIVER="$STUDY/report_tools/sl_replay.py"

# One leg at a time on this host: the long legs peak near 5 GB resident, the native window near
# 2.6 GB. `sl_replay.py run` enforces the same guard per arm.
VERIFY_ONLY=0
FORCE=0
PASSTHROUGH=()
while [ $# -gt 0 ]; do
  case "$1" in
    --verify-only) VERIFY_ONLY=1; shift ;;
    --force) FORCE=1; PASSTHROUGH+=(--force); shift ;;
    --variant|--leg|--stage) PASSTHROUGH+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

available_mb() { free -m | awk 'NR==2{print $7}'; }

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "== 1/4 build: derive the parent and gate it =="
  "$PY" "$DRIVER" build --check || exit 1
  "$PY" "$DRIVER" build || exit 1

  echo "== 2/4 replay: available memory $(available_mb) MB =="
  if [ "$FORCE" -eq 0 ]; then
    "$PY" "$DRIVER" run "${PASSTHROUGH[@]}" || exit 1
  else
    "$PY" "$DRIVER" run "${PASSTHROUGH[@]}" --force || exit 1
  fi
else
  echo "== verify-only: skipping build and replay =="
fi

echo "== 3/4 identity: s0_off must reproduce the published anchors =="
"$PY" "$DRIVER" identity --legs 3y ext pre || exit 1

echo "== 4/4 analyse: S0-S7 verdicts =="
"$PY" "$DRIVER" analyse --strict
ANALYSE_STATUS=$?

echo
echo "PENDING (declared, not delivered this round): generate_annual_report.py, event_windows.py."
echo "The report ships without them; see report_tools/README.md."
exit "$ANALYSE_STATUS"
