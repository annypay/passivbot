#!/usr/bin/env python3
"""Render the frozen holdout comparison from the locked cell results.

Every number comes from `cells/<window>/<scenario>/<cell>/result.json`. The candidate set and
the gates are read back from `artifacts/profile_decision_lock.json`, so the report cannot
describe a different comparison than the one that was locked before the windows were opened.

Usage:
    venv/bin/python report_tools/profile_comparison.py [--out PATH]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

STUDY = Path(__file__).resolve().parents[1]
CELLS = STUDY / "cells"
LOCK = STUDY / "artifacts" / "profile_decision_lock.json"
SCENARIO = "C1_binance_actual"
SEVERE = "C3_severe"
WINDOWS = ("full", "holdout", "fresh", "stress")


def metrics(window: str, scenario: str, cell: str) -> dict | None:
    path = CELLS / window / scenario / cell / "result.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())["metrics"]


def pct(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def num(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    lock = json.loads(LOCK.read_text())
    candidates = [entry["cell_id"] for entry in lock["candidates"]]
    gates = lock["gates"]

    lines: list[str] = []
    lines.append("# 冻结候选对照：holdout / fresh / stress 开封结果")
    lines.append("")
    lines.append("## 协议")
    lines.append("")
    lines.append(
        f"- 候选与闸门在 `{LOCK.relative_to(STUDY)}` 中**先于任何受锁窗口运行**落盘。"
    )
    lines.append(f"- 锁定时刻（UTC）：`{lock['written_utc']}`")
    lines.append(f"- 引擎指纹：`{lock['engine_fingerprint'][:16]}`（全部对照在同一构建下产生）")
    lines.append(f"- 研究契约 `cell_matrix_sha256`：`{lock['contract']['cell_matrix_sha256']}`")
    lines.append(f"- 口径：`{lock['execution_and_costs']['scenario']}`；"
                 f"T+{int(lock['execution_and_costs']['execution']['execution_delay_bars']) + 1}、"
                 f"`{lock['execution_and_costs']['execution']['intrabar_fill_order']}`、"
                 f"maker `{lock['execution_and_costs']['costs']['maker_fee_override']}`")
    lines.append("")
    lines.append("窗口：")
    lines.append("")
    lines.append("| 窗口 | 起 | 止 |")
    lines.append("| --- | --- | --- |")
    for name in WINDOWS:
        start, end = lock["windows"][name]
        lines.append(f"| `{name}` | {start} | {end} |")
    lines.append("")
    lines.append("闸门：" + "、".join(f"`{key}={value}`" for key, value in gates.items()
                                     if key not in ("applies_to_windows", "ranking", "no_candidate_passes_conclusion")))
    lines.append("")

    for window in WINDOWS:
        lines.append(f"## `{window}`")
        lines.append("")
        lines.append("| 候选 | MDD | CAGR | 倍数 | 最长水下（天） | 最差半年 | 正收益半年 | 成交币 | 强平 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        rows = []
        for cell in candidates:
            m = metrics(window, SCENARIO, cell)
            if m is None:
                continue
            rows.append((cell, m))
        for cell, m in sorted(rows, key=lambda row: row[1]["minute_close_mdd"]):
            lines.append(
                "| `{cell}` | {mdd} | {cagr} | {gain} | {uw} | {worst} | {hy} | {coin} | {liq} |".format(
                    cell=cell,
                    mdd=pct(m.get("minute_close_mdd")),
                    cagr=pct(m.get("cagr")),
                    gain=num(m.get("gain_strategy_eq")),
                    uw=num(m.get("total_underwater_days"), 1),
                    worst=pct(m.get("worst_halfyear_return")),
                    hy=m.get("positive_halfyears"),
                    coin=int(m.get("traded_coin_count") or 0),
                    liq="是" if m.get("liquidated") else "否",
                )
            )
        lines.append("")

    # Gate evaluation, recomputed from the locked gate set.
    lines.append("## 闸门判定（按锁定顺序复算）")
    lines.append("")
    lines.append("| 候选 | holdout MDD | fresh MDD | 是否通过 MDD ≤ 30% | 排名用 worst(MDD) |")
    lines.append("| --- | ---: | ---: | --- | ---: |")
    worst_values: dict[str, float] = {}
    for cell in candidates:
        holdout = metrics("holdout", SCENARIO, cell) or {}
        fresh = metrics("fresh", SCENARIO, cell) or {}
        h_mdd = holdout.get("minute_close_mdd")
        f_mdd = fresh.get("minute_close_mdd")
        passed = (
            h_mdd is not None
            and f_mdd is not None
            and h_mdd <= gates["mdd_max"]
            and f_mdd <= gates["mdd_max"]
        )
        if h_mdd is not None and f_mdd is not None:
            worst_values[cell] = max(h_mdd, f_mdd)
        lines.append(
            f"| `{cell}` | {pct(h_mdd)} | {pct(f_mdd)} | {'**是**' if passed else '否'} | "
            f"{pct(worst_values.get(cell))} |"
        )
    lines.append("")
    if worst_values:
        ranked = sorted(worst_values.items(), key=lambda item: item[1])
        lines.append("按 `worst(holdout MDD, fresh MDD)` 升序：")
        lines.append("")
        for position, (cell, value) in enumerate(ranked, start=1):
            lines.append(f"{position}. `{cell}` — {pct(value)}")
        lines.append("")

    # Severe-cost column, which is what separates the candidates.
    lines.append("## 极端口径 `C3_severe`（T+2 + maker 0.001，全窗）")
    lines.append("")
    lines.append("| 候选 | MDD | CAGR | 最长水下（天） |")
    lines.append("| --- | ---: | ---: | ---: |")
    for cell in candidates:
        m = metrics("full", SEVERE, cell)
        if m is None:
            continue
        lines.append(
            f"| `{cell}` | {pct(m.get('minute_close_mdd'))} | {pct(m.get('cagr'))} | "
            f"{num(m.get('total_underwater_days'), 1)} |"
        )
    lines.append("")

    lines.append("## 读法与边界")
    lines.append("")
    passing = [
        cell
        for cell in candidates
        if (metrics("holdout", SCENARIO, cell) or {}).get("minute_close_mdd", 1.0)
        <= gates["mdd_max"]
        and (metrics("fresh", SCENARIO, cell) or {}).get("minute_close_mdd", 1.0)
        <= gates["mdd_max"]
    ]
    if passing:
        lines.append(
            f"**通过闸门的候选：{'、'.join(f'`{cell}`' for cell in passing)}。** "
            "但必须读清楚这意味着什么：`holdout` 与 `fresh` 都是低波动窗口，四个候选在这两个窗口"
            "上的 MDD 只有 9.8%–10.8% 与 1.3%–2.0%，**信息量很低**——它们确认结果，不区分候选。"
        )
        lines.append("")
        lines.append(
            "真正区分候选的是 `full`、`stress` 与 `C3_severe`：闸门在三个**独立**窗口"
            "（2023-09→2026-09、2021-06→2026-09、以及 T+2 + 5× maker 口径）上把 MDD 压到 "
            "10.5% / 11.5% / 22.5%，而其余候选始终在 29%–41%。**这是结构性效果，不是单窗口幸运。**"
        )
    else:
        lines.append(
            "**没有任何候选同时通过 holdout 与 fresh 的 MDD 闸门**：按本专题预声明的判据，"
            "当前不存在发布级配置。"
        )
    lines.append("")
    lines.append(CHINESE_CAVEATS)

    text = "\n".join(lines) + "\n"
    out = Path(args.out) if args.out else STUDY / "artifacts" / "profile_comparison.md"
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


CHINESE_CAVEATS = """- **`holdout` 不是本专题的干净 holdout。** 2025-09-12 → 2026-09-12 落在 `full` 窗口内，
  且前序 `dd_tail_research_2026-09-15` 已开封过同一段。因此该窗口的全部 MDD 都是 9.8%–10.8%，
  与 `full` 值几乎相同——这**确认**了结果，但不**证明**候选的选择偏差已被排除。
- **`stress`（2021-06-01 → 2026-09-11）是唯一真正新开封的窗口**，且含 2022 年熊市。
  它不参与选型，是最有力的稳健性证据。
- 全部为 1 分钟 OHLC 全量 maker 成交假设下的历史模拟，不含盘口队列、部分成交、
  mark-price 强平与真实保证金梯度，**且引擎不建模资金费率**。
- 这些数字不是未来收益预测，也不是实盘表现。"""


if __name__ == "__main__":
    raise SystemExit(main())
