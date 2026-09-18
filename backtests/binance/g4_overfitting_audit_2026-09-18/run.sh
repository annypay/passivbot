#!/usr/bin/env bash
# Reproduce the g4 overfitting audit end to end.
#
#   bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh
#   bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage panels
#   bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage cscv
#   bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage bias
#   bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage folds
#   bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage stress
#   bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage report
#   bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --verify-only
#
# The audit asks whether the four published rounds (A risk geometry, B 10k replay, C account
# guard, D tail risk) selected parameters that only work on the one 2021-2026 Binance history
# they all share. It reads existing artifacts only: no backtest is started, no parameter is
# searched, no engine file is touched.
#
# Offline only: no network, no credentials, no exchange account, no bot start.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

export VIRTUAL_ENV="${VIRTUAL_ENV:-$REPO_ROOT/venv}"
PY="${PYTHON:-$REPO_ROOT/venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

STUDY="backtests/binance/g4_overfitting_audit_2026-09-18"
TOOLS="$STUDY/report_tools"
#: The audit loads ~100 hourly equity ledgers (~47k rows each) plus one fill ledger per stressed
#: arm. Measured peak RSS is under 1 GB; the guard is set well above that so it fails loudly on a
#: genuinely loaded box rather than mid-run.
MIN_AVAILABLE_MB="${MIN_AVAILABLE_MB:-1200}"

ONLY_STAGE=""
VERIFY_ONLY=0
FORCE_REPORT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --stage) ONLY_STAGE="${2:?--stage needs one of panels|cscv|bias|folds|stress|report}"; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    # `build_report.py` refuses to overwrite a report whose J1-J5 verdict column has already been
    # adjudicated by hand; this flag is the explicit opt-in to regenerate it anyway.
    --force-report) FORCE_REPORT=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPORT_ARGS=(--quiet)
[ "$FORCE_REPORT" -eq 1 ] && REPORT_ARGS+=(--force)

export PYTHONPATH="$REPO_ROOT/src"

available_mb() { free -m | awk 'NR==2{print $7}'; }

require_memory() {
  local need="$1" avail
  avail="$(available_mb)"
  if [ "${avail:-0}" -lt "$need" ]; then
    echo "FAIL: only ${avail} MB of memory is available; this audit needs about ${need} MB." >&2
    echo "      stop other work (or lower MIN_AVAILABLE_MB) and retry." >&2
    exit 1
  fi
}

want_stage() { [ -z "$ONLY_STAGE" ] || [ "$ONLY_STAGE" = "$1" ]; }

run_stage() {
  local name="$1"; shift
  echo "== $name =="
  "$@" || exit 1
}

require_memory "$MIN_AVAILABLE_MB"

if [ "$VERIFY_ONLY" -eq 1 ]; then
  run_stage "independent verification" "$PY" "$TOOLS/verify_audit.py"
  echo
  echo "done: $STUDY/overfitting_audit.md (verify-only did not re-render)"
  exit 0
fi

if want_stage panels; then
  run_stage "1/7 build panels and the trial ledger" "$PY" "$TOOLS/panel.py"
fi

if want_stage cscv; then
  run_stage "2/7 CSCV / PBO (2 selection conventions x S in {8,10,12,16} x 4 panels)" \
    "$PY" "$TOOLS/cscv_pbo.py"
fi

if want_stage bias; then
  run_stage "3/7 selection bias (DSR / MinBTL / SPA / StepM with N sensitivity)" \
    "$PY" "$TOOLS/selection_bias.py"
fi

if want_stage folds; then
  run_stage "4/7 fold stability (K in {3,6})" "$PY" "$TOOLS/walkforward.py" --stage folds
fi

if want_stage stress; then
  run_stage "5/7 declared cost/execution stress arms (NOT a selection input)" \
    "$PY" "$TOOLS/walkforward.py" --stage stress
fi

if want_stage report; then
  run_stage "6/7 render the Chinese reports from the tracked artifacts" \
    "$PY" "$TOOLS/build_report.py" "${REPORT_ARGS[@]}"
fi

if [ -z "$ONLY_STAGE" ]; then
  echo "== 6b/7 layout check: every cited artifact exists and no tracked file carries a host path =="
  "$PY" - <<'PY' || exit 1
import json
import re
import sys
from pathlib import Path

REPO = Path.cwd()
STUDY = REPO / "backtests/binance/g4_overfitting_audit_2026-09-18"
required = [
    "artifacts/trial_ledger.json",
    "artifacts/cscv_pbo.json",
    "artifacts/selection_bias.json",
    "artifacts/fold_stability.json",
    "artifacts/stress_arms.json",
    "artifacts/panels/index.json",
    "anti_pattern_audit.md",
    "overfitting_audit.md",
    "README.md",
    "run.sh",
    "report_tools/panel.py",
    "report_tools/cscv_pbo.py",
    "report_tools/selection_bias.py",
    "report_tools/walkforward.py",
    "report_tools/build_report.py",
    "report_tools/verify_audit.py",
]
problems = []
for rel in required:
    if not (STUDY / rel).exists():
        problems.append(f"missing {rel}")
for csv_path in sorted((STUDY / "artifacts/panels").glob("*.csv")):
    if csv_path.stat().st_size == 0:
        problems.append(f"empty {csv_path.name}")
index = json.loads((STUDY / "artifacts/panels/index.json").read_text(encoding="utf-8"))
for name in index["panels"]:
    path = STUDY / "artifacts/panels" / f"{name}.json"
    if not path.exists():
        problems.append(f"missing panel {name}.json")
        continue
    meta = json.loads(path.read_text(encoding="utf-8"))
    if "returns_matrix" not in meta:
        problems.append(f"panel {name}.json has no returns_matrix (not self-contained)")
        continue
    if len(meta["returns_matrix"]) != len(meta["arm_ids"]):
        problems.append(f"panel {name}.json: returns_matrix rows != arm_ids")
    for row in meta["returns_matrix"]:
        if len(row) != len(meta["months"]):
            problems.append(f"panel {name}.json: returns_matrix columns != months")
            break

#: Tracked files must be reproducible from a public clone: no absolute host paths, no
#: /home/<user>, no Windows drive letters.
host_pattern = re.compile(r"(/home/[A-Za-z0-9._-]+/|[A-Za-z]:\\\\|[A-Za-z]:/)")
for path in sorted(STUDY.rglob("*")):
    if not path.is_file():
        continue
    if path.suffix not in (".md", ".py", ".sh", ".json", ".csv"):
        continue
    if "artifacts/panels" in path.as_posix() and path.suffix == ".csv":
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    if host_pattern.search(text):
        problems.append(f"host path in {path.relative_to(STUDY)}")
    if "__pycache__" in path.as_posix():
        problems.append(f"cache file tracked under {path.relative_to(STUDY)}")

if problems:
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(f"layout check failed with {len(problems)} problem(s)")
print(f"layout: {len(required)} required files present, panels complete, no host paths")
if (STUDY / "artifacts/verification.json").exists():
    record = json.loads((STUDY / "artifacts/verification.json").read_text(encoding="utf-8"))
    print(
        f"        verification record on disk: {record['checks_passed']} passed, "
        f"{record['checks_failed']} failed"
    )
else:
    print("        verification record not written yet (step 7 produces it)")
PY
fi

if [ -z "$ONLY_STAGE" ]; then
  echo "== 7/7 independent verification (recomputes every headline number from scratch) =="
  "$PY" "$TOOLS/verify_audit.py" || exit 1

  # The verifier writes `artifacts/verification.json` last, and the report cites that check
  # count, so the report is re-rendered once more to keep the prose and the artifact in step.
  # Verification stays the final gate: the re-render cannot change any number it checked.
  echo "== 7b/7 re-render the report so it cites the fresh verification record =="
  "$PY" "$TOOLS/build_report.py" "${REPORT_ARGS[@]}" || exit 1
fi

echo
echo "done: $STUDY/overfitting_audit.md"
echo "      $STUDY/anti_pattern_audit.md"
echo "      $STUDY/README.md"
echo "      artifacts: $STUDY/artifacts/{trial_ledger,cscv_pbo,selection_bias,fold_stability,stress_arms}.json"
