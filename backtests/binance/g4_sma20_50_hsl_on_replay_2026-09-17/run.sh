#!/usr/bin/env bash
# Produce both replay arms of the g4 "HSL on" study, render their deep analyses, check the
# bundle layouts and verify the reports independently.
#
#   bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh
#   bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh --force
#   bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh --verify-only
#   bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh --variant hsl_on
#
# The study replays the frozen parent config twice on the current engine: once unchanged
# (the paired HSL-OFF control) and once with the single declared change
# `bot.long.hsl.enabled: false -> true`. Both runs are served by the same frozen HLCV
# bundle, so the only difference the comparison can see is the declared change.
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

STUDY="backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17"
TOOLS="$STUDY/report_tools"
VARIANTS=(hsl_off_control hsl_on)

FORCE=0
VERIFY_ONLY=0
ONLY_VARIANT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --variant) ONLY_VARIANT="${2:?--variant needs a name}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

export PYTHONPATH="$REPO_ROOT/src"

if [ -n "$ONLY_VARIANT" ]; then
  RUN_VARIANTS=("$ONLY_VARIANT")
else
  RUN_VARIANTS=("${VARIANTS[@]}")
fi

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "== 1/6 freeze the parent config and this study's declared delta =="
  args=()
  [ "$FORCE" -eq 1 ] && args+=(--force)
  "$PY" "$TOOLS/build_variant_config.py" "${args[@]}" || exit 1

  for variant in "${RUN_VARIANTS[@]}"; do
    echo "== run the offline replay: $variant =="
    replay_args=(--variant "$variant")
    [ "$FORCE" -eq 1 ] && replay_args+=(--force)
    "$PY" "$TOOLS/run_variant.py" "${replay_args[@]}" || exit 1
  done

  echo "== render the deep analysis and its three metric tables =="
  for variant in "${RUN_VARIANTS[@]}"; do
    "$PY" "$TOOLS/generate_annual_report.py" --variant "$variant" || exit 1
  done
else
  echo "== verify-only: skipping freeze, replays and render =="
fi

echo "== layout check: every bundle against the convention =="
"$PY" - <<'PY' || exit 1
import sys
from pathlib import Path

sys.path.insert(0, str(Path("backtests/report_spec").resolve()))
sys.path.insert(0, str(Path("backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/report_tools").resolve()))
import annual_analysis as spec
import variant_spec as study

problems = []
for variant in study.VARIANTS:
    if not variant.runs_base.is_dir():
        problems.append(f"{variant.key}: no run directory under {study.relative(variant.runs_base)}")
        continue
    run = study.find_variant_run_dir(variant)
    config = spec.load_json(run / "config.json")
    found = spec.assert_bundle_layout(run, config=config, expect_plots=True)
    for problem in found:
        problems.append(f"{variant.key}: {problem}")
    report = (run / "annual_analysis.md").read_text(encoding="utf-8")
    problems.extend(f"{variant.key}: {problem}" for problem in spec.assert_report_structure(report))
    print(f"layout: {len(found)} problem(s) in {study.relative(run)}")
for problem in problems:
    print(f"  - {problem}")
raise SystemExit(1 if problems else 0)
PY

echo "== independent verification of both reports =="
for variant in "${RUN_VARIANTS[@]}"; do
  "$PY" "$TOOLS/verify_variant_report.py" --variant "$variant" || exit 1
done

echo
echo "done: $STUDY/artifacts/{hsl_off_control,hsl_on}/backtest_results/binance_*/binance/<UTC timestamp>/annual_analysis.md"