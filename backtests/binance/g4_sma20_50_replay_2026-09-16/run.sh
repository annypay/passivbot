#!/usr/bin/env bash
# Produce the g4_sma20_50 (gated profile) artifact bundle, check its layout, and verify
# its deep-analysis report.
#
#   bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh
#   bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh --force
#   bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh --verify-only
#
# The replay runs the **published** profile
# `configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json` as an offline backtest
# over the frozen HLCV bundle and renders the convention's deep analysis next to it. The run
# is complete on the convention's own terms: the full figure set, including the per-coin
# panels, with no disabled group.
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

STUDY="backtests/binance/g4_sma20_50_replay_2026-09-16"
TOOLS="$STUDY/report_tools"

FORCE=0
VERIFY_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

export PYTHONPATH="$REPO_ROOT/src"

if [ "$VERIFY_ONLY" -eq 0 ]; then
  echo "== 1/5 freeze the published profile as the run config and gate it =="
  args=()
  [ "$FORCE" -eq 1 ] && args+=(--force)
  "$PY" "$TOOLS/build_cell_config.py" "${args[@]}" || exit 1

  echo "== 2/5 run the offline replay against the frozen dataset =="
  replay_args=()
  [ "$FORCE" -eq 1 ] && replay_args+=(--force)
  "$PY" "$TOOLS/run_replay.py" "${replay_args[@]}" || exit 1

  echo "== 3/5 render the deep analysis and its three metric tables =="
  "$PY" "$TOOLS/generate_annual_report.py" || exit 1
else
  echo "== verify-only: skipping freeze, replay and render =="
fi

echo "== 4/5 check the run directory against the layout contract =="
"$PY" - <<'PY' || exit 1
import sys
from pathlib import Path

sys.path.insert(0, str(Path("backtests/report_spec").resolve()))
sys.path.insert(0, str(Path("backtests/binance/g4_sma20_50_replay_2026-09-16/report_tools").resolve()))
import annual_analysis as spec
import cell_spec as study

runs = sorted(
    path for path in study.RUNS_BASE.iterdir() if path.is_dir() and path.name[:2].isdigit()
)
if len(runs) != 1:
    raise SystemExit(f"expected exactly one run directory, found {[p.name for p in runs]}")
run = runs[0]
config = spec.load_json(run / "config.json")
problems = spec.assert_bundle_layout(run, config=config, expect_plots=True)
for problem in problems:
    print(f"  - {problem}")
print(f"layout: {len(problems)} problem(s) in {run}")
raise SystemExit(1 if problems else 0)
PY

echo "== 5/5 verify the report independently =="
"$PY" "$TOOLS/verify_replay_report.py" || exit 1

echo
echo "done: $STUDY/artifacts/backtest_results/binance/<UTC timestamp>/annual_analysis.md"
