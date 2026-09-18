#!/usr/bin/env python3
"""Default-off regression: the new engine must reproduce the previous round's arm exactly.

The two new keys are default-disabled (`halt_ladder_minutes = []`,
`realized_loss_budget_pct = 0.0`). With them disabled the engine has to behave exactly as it did
before the change, so `l0_off__<leg>` and the previous round's `a_allow000__<leg>` — same 10,000
USDT, TWE 3.0, 7 slots, `we_excess_allowance_pct = 0`, unified guard RED 0.20 / EMA 60 / 12 h,
`no_restart_drawdown_threshold = 1`, same dataset and window — must agree on the fill tape and on
every comparable metric.

Both sides are recomputed here from the run directories; nothing is read from either study's own
report. A non-empty `differences` list means the claim must not be published.

Offline only. No network, no credentials, no bot start.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
NEW_STUDY = REPO / "backtests/binance/g4_hsl_halt_ladder_2026-09-18"
OLD_STUDY = REPO / "backtests/binance/g4_twe300_risk_optimization_2026-09-17"

#: Metrics compared between the two runs. Every name exists in both `analysis.json` files.
COMPARED_METRICS = (
    "gain_usd",
    "gain_strategy_eq",
    "gain_btc",
    "adg_strategy_eq",
    "adg_strategy_eq_w",
    "drawdown_worst_strategy_eq",
    "drawdown_worst_mean_1pct_strategy_eq",
    "drawdown_worst_ema_strategy_eq",
    "sharpe_ratio_strategy_eq",
    "sortino_ratio_strategy_eq",
    "calmar_ratio_strategy_eq",
    "expected_shortfall_1pct_strategy_eq",
    "backtest_completion_ratio",
    "liquidated",
    "entry_interval_hours_mean",
    "hard_stop_triggers",
    "hard_stop_restarts",
    "hard_stop_time_in_yellow_pct",
    "hard_stop_time_in_orange_pct",
    "hard_stop_time_in_red_pct",
    "hard_stop_duration_minutes_mean",
    "hard_stop_duration_minutes_max",
    "hard_stop_trigger_drawdown_mean",
    "hard_stop_panic_close_loss_sum",
    "hard_stop_panic_close_loss_max",
    "hard_stop_panic_close_loss_drawdown_pct_min",
    "hard_stop_panic_close_loss_drawdown_pct_mean",
    "hard_stop_panic_close_loss_drawdown_pct_max",
    "hard_stop_flatten_time_minutes_mean",
    "hard_stop_post_restart_retrigger_pct",
    "hard_stop_halt_to_restart_equity_loss_pct",
)

#: Keys that legitimately exist on only one side because this round added them. They cannot be
#: evidence of change: the previous round's backend never wrote them.
ROUND_NEW_METRICS = (
    "hard_stop_ladder_strikes_max",
    "hard_stop_realized_loss_halt_pct_max",
)

TOLERANCE = 1e-9


def load_json(path: Path) -> Any:
    with path.open() as handle:
        return json.load(handle)


def run_dir(study: Path, arm: str) -> Path:
    candidates = sorted(
        (study / "artifacts" / arm / "backtest_results").glob(f"binance_{arm}/binance/*")
    )
    dirs = [path for path in candidates if (path / "analysis.json").exists()]
    if not dirs:
        raise SystemExit(f"no completed run for {arm} under {study}")
    return dirs[-1]


def fill_signature(run: Path) -> dict[str, Any]:
    """A content hash of the fill tape, independent of row order and of host paths."""
    lines = (run / "fills.csv").read_text(encoding="utf-8").splitlines()
    columns = {name: index for index, name in enumerate(lines[0].split(","))}

    def field(row: list[str], *candidates: str) -> str:
        for candidate in candidates:
            if candidate in columns:
                return row[columns[candidate]]
        raise SystemExit(f"fills.csv is missing any of {candidates}")

    digest = hashlib.sha256()
    rows = 0
    qty_sum = 0.0
    notional_sum = 0.0
    timestamps: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        row = line.split(",")
        timestamp = field(row, "timestamp")
        coin = field(row, "coin", "symbol")
        qty = float(field(row, "qty"))
        price = float(field(row, "price"))
        order_type = field(row, "type", "order_type")
        digest.update(f"{timestamp}|{coin}|{order_type}|{qty!r}|{price!r}\n".encode())
        rows += 1
        qty_sum += qty
        notional_sum += qty * price
        timestamps.append(timestamp)
    return {
        "rows": rows,
        "qty_sum": round(qty_sum, 6),
        "notional_sum": round(notional_sum, 6),
        "first_timestamp": min(timestamps) if timestamps else None,
        "last_timestamp": max(timestamps) if timestamps else None,
        "sha256": digest.hexdigest(),
    }


def compare_metrics(new: dict[str, Any], old: dict[str, Any], label: str) -> list[str]:
    problems: list[str] = []
    for key in COMPARED_METRICS:
        new_value = new.get(key)
        old_value = old.get(key)
        if new_value is None or old_value is None:
            problems.append(f"{label}: {key} missing (new={new_value!r} old={old_value!r})")
            continue
        if isinstance(new_value, bool) or isinstance(old_value, bool):
            if bool(new_value) != bool(old_value):
                problems.append(f"{label}: {key} {new_value!r} != {old_value!r}")
            continue
        if isinstance(new_value, (int, float)) and isinstance(old_value, (int, float)):
            if math.isnan(float(new_value)) or math.isnan(float(old_value)):
                problems.append(f"{label}: {key} is NaN")
            elif abs(float(new_value) - float(old_value)) > TOLERANCE:
                problems.append(f"{label}: {key} {float(new_value)!r} != {float(old_value)!r}")
            continue
        if str(new_value) != str(old_value):
            problems.append(f"{label}: {key} {new_value!r} != {old_value!r}")
    return problems


def compare_leg(leg: str) -> dict[str, Any]:
    new_run = run_dir(NEW_STUDY, f"l0_off__{leg}")
    old_run = run_dir(OLD_STUDY, f"a_allow000__{leg}")
    new_metrics = load_json(new_run / "analysis.json")
    old_metrics = load_json(old_run / "analysis.json")
    new_fills = fill_signature(new_run)
    old_fills = fill_signature(old_run)
    problems = compare_metrics(new_metrics, old_metrics, leg)
    for key in sorted(set(new_fills) | set(old_fills)):
        if new_fills.get(key) != old_fills.get(key):
            problems.append(
                f"{leg}: fills.{key} {new_fills.get(key)!r} != {old_fills.get(key)!r}"
            )
    return {
        "leg": leg,
        "new_arm": f"l0_off__{leg}",
        "old_arm": f"a_allow000__{leg}",
        "new_run": str(new_run.relative_to(REPO)),
        "old_run": str(old_run.relative_to(REPO)),
        "metrics_compared": list(COMPARED_METRICS),
        "tolerance": TOLERANCE,
        "new_fills": new_fills,
        "old_fills": old_fills,
        "guard_activity_on_this_leg": {
            "hard_stop_triggers": new_metrics.get("hard_stop_triggers"),
            "hard_stop_restarts": new_metrics.get("hard_stop_restarts"),
            "hard_stop_duration_minutes_max": new_metrics.get("hard_stop_duration_minutes_max"),
        },
        "differences": problems,
    }


def main(argv: list[str] | None = None) -> int:
    legs = list(argv[1:]) if argv and len(argv) > 1 else ["3y", "ext"]
    report = {
        "claim": (
            "两个新键取默认值（halt_ladder_minutes=[] 且 realized_loss_budget_pct=0.0）时，新引擎"
            "与改动前的同几何臂逐位一致：成交磁带内容哈希与全部可比指标都相同。"
        ),
        "why_these_two_arms": (
            "l0_off 与上一轮的 a_allow000 几何相同：10,000 USDT、TWE 3.0、7 槽、"
            "we_excess_allowance_pct=0、unified 守护 RED 0.20 / EMA 60 / 12H、"
            "no_restart_drawdown_threshold=1，同一数据集与窗口（逐键比对只差 backtest.base_dir，"
            "那是各自 study 的输出目录）。"
        ),
        "excluded_from_comparison": {
            key: (
                "本轮新增的指标：上一轮的后端根本不写这个键，缺键不是行为差异。"
                + (
                    " 该指标计数与是否配置阶梯无关，因此默认关闭时它也可能非 0。"
                    if key == "hard_stop_ladder_strikes_max"
                    else ""
                )
            )
            for key in ROUND_NEW_METRICS
        },
        "legs": [compare_leg(leg) for leg in legs],
    }
    report["differences"] = [problem for leg in report["legs"] for problem in leg["differences"]]
    report["verdict"] = "identical" if not report["differences"] else "differs"
    out = NEW_STUDY / "artifacts/default_off_identity.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    for leg in report["legs"]:
        activity = leg["guard_activity_on_this_leg"]
        print(
            f"{leg['leg']}: {leg['new_arm']} vs {leg['old_arm']} -> "
            f"{len(leg['differences'])} difference(s); fills={leg['new_fills']['rows']} "
            f"sha256={leg['new_fills']['sha256'][:16]} "
            f"guard_triggers={activity['hard_stop_triggers']}"
        )
    if report["differences"]:
        for problem in report["differences"]:
            print(f"  - {problem}")
        return 1
    print("default-off identity holds")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
