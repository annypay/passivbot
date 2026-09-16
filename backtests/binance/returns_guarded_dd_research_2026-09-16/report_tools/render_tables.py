#!/usr/bin/env python3
"""Render the study's results tables straight from the cell result files.

Every number in the study reports comes from `cells/**/result.json`; this script
regenerates the tables so a report cannot drift from the artifacts it cites.

Usage:
    venv/bin/python report_tools/render_tables.py [--window full] [--scenario C1_binance_actual]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

STUDY = Path(__file__).resolve().parents[1]
CELLS = STUDY / "cells"

GROUPS = {
    "G0_reference": "种子 / 对照",
    "G1_geometry": "补仓阶梯与敞口",
    "G2_stop": "路径止损（unstuck）",
    "G3_scaleout": "分批减仓与平仓阈值",
    "G4_regime_gate": "日线趋势闸门",
    "G5_combo": "组合",
    "G5_combo_gated": "组合 + 闸门",
    "G6.0_probe": "多空探针",
    "G6.1_confound": "多空混淆对照",
    "G6.2_gate": "多空翻转闸门",
    "G6.3_short_budget": "多空短侧预算",
    "G6.4_short_geometry": "多空短侧几何",
    "G6.5_budgeted_flip": "多空预算受限翻转",
}


def rank_rows(records):
    """Rank cell records by the questions the next research round asks.

    Returns non-liquidated rows carrying a derived CAGR/MDD efficiency ratio. Anything that
    was liquidated is excluded: its return numbers describe a blown-up account.
    """
    rows = []
    for record in records:
        m = record["metrics"]
        mdd = float(m.get("minute_close_mdd") or 0.0)
        gain = float(m.get("gain_strategy_eq") or 0.0)
        days = float(m.get("series_days") or 0.0)
        if m.get("liquidated") or gain <= 0.0 or days <= 0.0:
            continue
        cagr = gain ** (365.25 / days) - 1.0
        rows.append(
            {
                "cell": record["cell_id"],
                "group": record["group"],
                "mdd": mdd,
                "cagr": cagr,
                "ratio": (cagr / mdd) if mdd > 0 else float("inf"),
                "gain": gain,
                "uw": float(m.get("total_underwater_days") or 0.0),
                "worst_hy": m.get("worst_halfyear_return"),
                "short_fills": float(m.get("fills_short") or 0.0),
            }
        )
    return rows


def render_ranking(records, top: int) -> str:
    rows = rank_rows(records)
    lines = ["", f"## 研究短名单（按用途排序，前 {top}）", ""]
    lines.append(
        f"可评估 cell {len(records)} 个，剔除强平后 {len(rows)} 个。"
        "**全部为同一窗口的 in-sample 结果，未开封任何 holdout。**"
        "本表用于指定下一轮测试标的，不是持久优势的证据。"
    )
    views = (
        ("按回撤效率（CAGR / MDD）", lambda row: row["ratio"], False),
        ("按 CAGR", lambda row: row["cagr"], False),
        (
            "按 CAGR（约束 MDD ≤ 30%）",
            lambda row: row["cagr"],
            True,
        ),
    )
    for title, key, capped in views:
        subset = [row for row in rows if row["mdd"] <= 0.30] if capped else rows
        subset = sorted(subset, key=key, reverse=True)[:top]
        lines.append("")
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| cell | 分组 | MDD | CAGR | CAGR/MDD | 倍数 | 最长水下 | 最差半年 | 空头成交 |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in subset:
            lines.append(
                "| `{cell}` | {group} | {mdd} | {cagr} | {ratio} | {gain} | {uw} | {worst} | {short} |".format(
                    cell=row["cell"],
                    group=GROUPS.get(row["group"], row["group"]),
                    mdd=fmt(row["mdd"]),
                    cagr=fmt(row["cagr"]),
                    ratio=fmt(row["ratio"], 2),
                    gain=fmt(row["gain"], 3),
                    uw=fmt(row["uw"], 1),
                    worst=fmt(row["worst_hy"]),
                    short=int(row["short_fills"]),
                )
            )
    return "\n".join(lines) + "\n"




def load_records(window: str, scenario: str) -> list[dict[str, Any]]:
    out = []
    root = CELLS / window / scenario
    if not root.is_dir():
        raise SystemExit(f"no cells under {root}")
    for path in sorted(root.glob("*/result.json")):
        out.append(json.loads(path.read_text()))
    return out


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:  # NaN
        return "—"
    return f"{number:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", default="full")
    parser.add_argument("--scenario", default="C1_binance_actual")
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--rank",
        action="store_true",
        help="additionally render the research shortlist ranking",
    )
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()

    records = load_records(args.window, args.scenario)
    if not records:
        raise SystemExit("no cell results found")

    lines: list[str] = []
    lines.append(f"窗口 `{args.window}` / 口径 `{args.scenario}`，共 {len(records)} 个 cell。")
    lines.append("")
    lines.append("| 分组 | cell | MDD | 最差1%均值 | CAGR | 倍数 | 最长水下 | 最差半年 | 成交币 | fills |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for record in sorted(
        records,
        key=lambda r: (r["group"], r["metrics"]["minute_close_mdd"]),
    ):
        m = record["metrics"]
        lines.append(
            "| {group} | `{cell}` | {mdd} | {w1} | {cagr} | {gain} | {uw} | {worst} | {coins} | {fills} |".format(
                group=GROUPS.get(record["group"], record["group"]),
                cell=record["cell_id"],
                mdd=fmt(m.get("minute_close_mdd")),
                w1=fmt(m.get("worst_1pct_mean_drawdown")),
                cagr=fmt(m.get("cagr")),
                gain=fmt(m.get("gain_strategy_eq"), 3),
                uw=fmt(m.get("total_underwater_days"), 1),
                worst=fmt(m.get("worst_halfyear_return")),
                coins=int(m.get("traded_coin_count") or 0),
                fills=int(m.get("fills") or 0),
            )
        )

    # Acceptance table: MDD <= 30% and CAGR >= 25%.
    passing = [
        r
        for r in records
        if r["metrics"]["minute_close_mdd"] <= 0.30 and r["metrics"]["cagr"] >= 0.25
    ]
    lines.append("")
    lines.append("**同时满足 `MDD <= 30%` 且 `CAGR >= 25%` 的 cell：**")
    lines.append("")
    if passing:
        lines.append("| cell | MDD | CAGR | 最长水下 |")
        lines.append("| --- | ---: | ---: | ---: |")
        for record in sorted(passing, key=lambda r: -r["metrics"]["cagr"]):
            m = record["metrics"]
            lines.append(
                f"| `{record['cell_id']}` | {fmt(m['minute_close_mdd'])} | "
                f"{fmt(m['cagr'])} | {fmt(m['total_underwater_days'], 1)} |"
            )
    else:
        lines.append("（无）")

    if args.rank:
        lines.append(render_ranking(records, args.top))

    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
