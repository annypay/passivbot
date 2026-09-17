# Live-run records

This directory holds the build, test and debugging record for work that has to survive a
live run. It exists so that a later reader — a human or an agent — can tell which
behaviour was known and verified when the bot was started, instead of re-deriving it
from a log tail.

## How to use it

- **Before a live start**: read the newest record's *Live debugging playbook* and the
  *Watch items* board below. Everything the playbook names is something the code
  actually emits.
- **During a live run**: if something looks wrong, go to the symptom table in the record
  that covers the subsystem, then follow the action. Add a dated entry to that record
  when you learn something the table does not say.
- **After a change**: add a new numbered record rather than editing an old one, and
  update the index and the watch board.

## Rules

- Records are append-only history. Correct a wrong statement with a dated note in the
  same file; do not silently rewrite it.
- No host paths, credentials, account identifiers or private hosts. Repository-relative
  paths, commit SHAs, PR numbers and log fragments are fine.
- Every claim about behaviour names where it is implemented or where it is observed.

## Records

| # | Subject | Status | Related PRs | Last updated |
| --- | --- | --- | --- | --- |
| `0000-session-log-2026-09-16.md` | Session log: research evidence, the two published profiles, the audit that changed the gate's live story | historical | #1, #2, #3 | 2026-09-17 |
| `0001-entry-regime-gate-live-wiring.md` | Entry-regime gate reaches live: single evaluation point, fail-closed evidence, verdict event, offline dry run | active | #3 (stacked on #2) | 2026-09-17 |

## Watch items until the gate is stable live

| Item | What to watch | Close when |
| --- | --- | --- |
| W1 warm-up | `[regime_gate]` shows `risk_off_sides` equal to the traded side count for the first `sma_slow_days + confirm_days + 2` days | the first risk-on verdict arrives as expected |
| W2 daily evidence availability | `unavailable=` stays 0; any non-zero value means entries for that symbol are blocked until the retry succeeds | a full week with no unavailable symbol |
| W3 verdict event | `entry_regime.gate.verdict` appears once per UTC day with plausible counts | a week of events lines up with the market |
| W4 blocked-entry sanity | entries stop on risk-off days and resume on risk-on days, without touching closes | the first two full regime transitions behave as documented |
| W5 counterfactual | the ungated profile's fills differ from the gated one only by entries it would have taken | a review of one risk-off stretch against the ungated evidence |

## Quick commands

```bash
# Offline dry run of the gate through the real planning path (no network, no credentials)
venv/bin/python -m pytest tests/test_fake_live_entry_regime_gate_dry_run.py -q

# Engine/table parity at every regime boundary, plus the counterfactual that isolates
# the gate from the strategy
venv/bin/python -m pytest tests/test_entry_regime_gate_engine_parity.py -q

# Gate unit and contract tests (both declaration spellings, live payload, event)
venv/bin/python -m pytest tests/test_entry_regime_gate.py \
  tests/test_entry_regime_gate_config_survival.py \
  tests/test_live_entry_regime_gate.py -q

# Manual smoke on a longer tape (wiring only; see 0001 for what it can and cannot show)
venv/bin/python src/tools/run_fake_live.py \
  configs/fake_live_entry_regime_gate.hjson \
  scenarios/fake_live/entry_regime_gate_wiring_smoke.hjson \
  --max-steps 250 --output-dir artifacts/fake_live --snapshot-each-step
```

To disable the gate without touching code, set `enabled: false` in the declaration the
config uses (`live.entry_regime_gate`, or the `backtest.entry_regime_gate` block that the
live path falls back to).
