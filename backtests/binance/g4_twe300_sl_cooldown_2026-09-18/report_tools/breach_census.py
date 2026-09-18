#!/usr/bin/env python3
"""Breach census with a cooldown-length comparison: after the price breaks `avg x (1 - level)`,
does it come back above that level within 24h, and within 48h?

`candle_excursion.py` answers this for one cooldown at a time (default 1440 minutes). This tool runs
it for the cooldowns the stop-loss proposal cares about and prints them side by side, so the
"the stop sold the low" question is answered at two horizons instead of one.

The heavy lifting (streaming the run's own frozen bundle, the breach definition, the spike/good-exit
thresholds) stays in `candle_excursion.py`; this tool only drives it twice and joins the censuses on
the stop level. It therefore needs the same memory headroom as that tool (~3 GB) and takes about as
long as two of its runs.

Offline only. No network, no credentials, no bot start.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import episode_ledger as ledger  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_twe300_sl_cooldown_2026-09-18"
TOOL = Path(__file__).resolve().parent / "candle_excursion.py"
#: The cooldowns compared: the proposal's 24h, and 48h as the longer-horizon control.
DEFAULT_COOLDOWNS = (1440.0, 2880.0)


def run_excursion(args: argparse.Namespace, cooldown: float, out: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(TOOL),
        "--arm",
        args.arm,
        "--min-exposure",
        str(args.min_exposure),
        "--min-qty",
        str(args.min_qty),
        "--cooldown-minutes",
        str(cooldown),
        "--out",
        str(out),
        "--census-only",
    ]
    for level in args.level:
        command += ["--level", str(level)]
    for coin in args.coin:
        command += ["--coin", coin]
    completed = subprocess.run(command, cwd=str(REPO), capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"candle_excursion.py --cooldown-minutes {cooldown} failed:\n{completed.stderr[-2000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", default=ledger.BASELINE_ARM)
    parser.add_argument("--coin", action="append", default=[])
    parser.add_argument("--level", action="append", type=float, default=[])
    parser.add_argument("--min-exposure", type=float, default=0.05)
    parser.add_argument("--min-qty", type=float, default=0.0)
    parser.add_argument("--cooldown-minutes", action="append", type=float, default=[])
    parser.add_argument("--out", default=None, help="combined report path (tracked JSON)")
    args = parser.parse_args(argv)
    cooldowns = tuple(args.cooldown_minutes) or DEFAULT_COOLDOWNS

    reports: dict[float, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for cooldown in cooldowns:
            reports[cooldown] = run_excursion(args, cooldown, Path(tmp) / f"cd_{cooldown:.0f}.json")

    rows: list[dict[str, Any]] = []
    for level_pct in reports[cooldowns[0]]["levels"]:
        row: dict[str, Any] = {"level_pct": level_pct}
        for cooldown in cooldowns:
            census = next(
                (
                    item
                    for item in reports[cooldown]["census"]
                    if abs(float(item["level_pct"]) - float(level_pct)) < 1e-9
                ),
                None,
            )
            key = f"cd{int(cooldown)}"
            row[key] = (
                None
                if census is None
                else {
                    "breaches": census.get("breaches", 0),
                    "share_of_episodes_pct": census.get("share_of_episodes_pct"),
                    "recovered_above_level_within_cooldown_pct": census.get("recovered_share_pct"),
                    "spike_exit_share_pct": census.get("spike_exit_share_pct"),
                    "good_exit_share_pct": census.get("good_exit_share_pct"),
                    "median_further_drop_pct": census.get("median_further_drop_pct"),
                    "median_close_at_expiry_vs_level_pct": census.get(
                        "median_close_at_expiry_vs_level_pct"
                    ),
                    "share_close_at_expiry_below_level_pct": census.get(
                        "share_close_at_expiry_below_level_pct"
                    ),
                    "breached_episodes_net_positive_pct": census.get(
                        "breached_episodes_net_positive_pct"
                    ),
                }
            )
        rows.append(row)

    payload = {
        "arm": args.arm,
        "cooldowns": list(cooldowns),
        "levels": reports[cooldowns[0]]["levels"],
        "episodes_measured": reports[cooldowns[0]]["episodes_measured"],
        "coins": reports[cooldowns[0]]["coins"],
        "candles_source": reports[cooldowns[0]]["candles_source"],
        "thresholds": reports[cooldowns[0]]["thresholds"],
        "method": (
            "对每个止损位报告两个冷却口径（默认 24h 与 48h）：触发次数、触发后冷却窗口内收盘价回到"
            "该位之上的比例（= 「止损卖在低点」）、继续下跌中位数、冷却到期价相对该位的中位数。"
            "蜡烛与跌破定义由 candle_excursion.py 提供，本工具只驱动它两次并按键对齐。"
        ),
        "rows": rows,
    }
    out = Path(args.out) if args.out else STUDY / "artifacts/optimism" / f"{args.arm}__breach_census.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"wrote {out}")
    print(f"episodes measured={payload['episodes_measured']} coins={len(payload['coins'])}")
    header = f"{'level':>7} " + " ".join(f"{'cd' + str(int(cd)) + ' recov%':>13}" for cd in cooldowns) + \
        " " + " ".join(f"{'cd' + str(int(cd)) + ' medDrop%':>16}" for cd in cooldowns)
    print(header)
    for row in rows:
        cells = []
        for cooldown in cooldowns:
            entry = row[f"cd{int(cooldown)}"]
            cells.append("            -" if not entry or not entry["breaches"]
                         else f"{entry['recovered_above_level_within_cooldown_pct']:>13.2f}")
        for cooldown in cooldowns:
            entry = row[f"cd{int(cooldown)}"]
            cells.append("               -" if not entry or not entry["breaches"]
                         else f"{entry['median_further_drop_pct'] * 100:>16.2f}")
        print(f"{row['level_pct']:>7.2f} " + " ".join(cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
