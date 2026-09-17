#!/usr/bin/env python3
"""Build the study-level synthesis for the g4 @ TWE 3.0 / 10,000 USDT replay.

Every arm has its own deep analysis in its run directory (report convention, Rule 1); this
document is the cross-arm answer to the questions the study was commissioned for:

1. at TWE 3.0 with 10,000 USDT, how deep is the drawdown and does the account survive the
   local history at all (the engine ends a run at `starting_balance * liquidation_threshold`)?
2. what does the hard floor (`unified` HSL at `red_threshold=0.10`, and its terminal variant)
   buy, and what does it cost in return and recovery time?
3. what changes from the 10x smaller starting balance alone, and what is the structural
   alternative (`we_excess_allowance_pct=0`)?
4. can a 10,000 USDT live account even afford an initial entry per coin (offline appendix)?

It reads only tracked evidence plus each arm's own side artifacts, and writes
`twe300_10k_analysis.md` and `artifacts/twe300_10k_summary.json`.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import event_windows as events  # noqa: E402
import variant_spec as study  # noqa: E402
import wipeout_matrix as wipeout  # noqa: E402

OUTPUT_PATH = study.STUDY / "twe300_10k_analysis.md"
SUMMARY_PATH = study.ARTIFACTS / "twe300_10k_summary.json"

COMPARED = (
    ("gain_strategy_eq", "收益倍数", "multiple"),
    ("adg_strategy_eq", "平均日增长", "pct"),
    ("drawdown_worst_strategy_eq", "全期最差回撤", "pct"),
    ("drawdown_worst_mean_1pct_strategy_eq", "最差 1% 均值回撤", "pct"),
    ("expected_shortfall_1pct_strategy_eq", "最差 1% 条件期望", "pct"),
    ("total_wallet_exposure_max", "总暴露峰值", "ratio"),
    ("strategy_eq_recovery_days_max", "最长恢复期（天）", "days"),
    ("omega_ratio_strategy_eq", "Omega", "ratio"),
)
LEG_TITLES = {
    "3y": "原生窗口腿（2023-09-12 → 2026-09-12，与发布 profile 同数据集）",
    "ext": "历史压力腿（2021-04-20 → 2026-09-13，dataset override）",
}
REFERENCE_CONTROL = {"3y": "ref_published_3y", "ext": "ref_tailrisk_off_ext"}


def load_evidence(variant: study.Variant) -> dict[str, Any] | None:
    try:
        run_dir = study.find_variant_run_dir(variant)
    except SystemExit:
        return None
    evidence: dict[str, Any] = {"key": variant.key, "arm": variant, "run_dir": run_dir}
    for name, target in (
        ("analysis.json", "analysis"),
        ("run_record.json", "record"),
        ("global_metrics.json", "global_metrics"),
        ("tail_risk_events.json", "events"),
        ("tail_risk_wipeout.json", "wipeout"),
    ):
        path = run_dir / name
        evidence[target] = study.load_json(path) if path.exists() else None
    affordability = study.ARTIFACTS / f"affordability_{variant.key}.json"
    evidence["affordability"] = study.load_json(affordability) if affordability.exists() else None
    return evidence


def event_table_for(run_dir: Path, dataset_path: Path) -> dict[str, Any] | None:
    """Event table for a pinned anchor that predates this study, from its local ledger."""
    if not (run_dir / "fills.csv").exists() or not (run_dir / "balance_and_equity.csv.gz").exists():
        return None
    equity = events.load_equity(run_dir)
    fills = events.load_fills(run_dir)
    benchmark = events.load_benchmark(dataset_path)
    rule = study.EVENT_RULE
    episodes = events.detect_episodes(
        benchmark,
        lookback_days=int(rule["lookback_days"]),
        threshold=float(rule["threshold"]),
        min_gap_days=int(rule["min_gap_days"]),
        labels=study.EVENT_LABELS,
    )
    return {
        "arm": run_dir.name,
        "events": events.arm_event_table(equity, fills, episodes, synthetic=False),
        "recomputed_by_synthesis": True,
    }


def reference_evidence() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, item in study.REFERENCE_RUNS.items():
        run_dir = Path(item["run_dir"])
        analysis_path = run_dir / "analysis.json"
        if not analysis_path.exists():
            continue
        entry = {
            "key": key,
            "label": item["label"],
            "role": item["role"],
            "leg": item["leg"],
            "run_dir": run_dir,
            "analysis": study.load_json(analysis_path),
            "analysis_sha256": study.sha256_file(analysis_path),
            "pinned_sha256": item["analysis_sha256"],
            "events": None,
            "wipeout": None,
            "arm": None,
            "affordability": None,
        }
        tail = run_dir / "tail_risk_events.json"
        if tail.exists():
            entry["events"] = study.load_json(tail)
        else:
            entry["events"] = event_table_for(run_dir, study.DATASETS[item["leg"]].path)
        out.append(entry)
    return out


def fmt(value: Any, kind: str, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "n/a"
    if kind == "pct":
        return f"{number * 100:.{digits}f}%"
    if kind == "multiple":
        return f"{number:.{digits}f}×"
    if kind == "days":
        return f"{number:.{digits}f}"
    if kind == "usd":
        return f"{number:,.0f}"
    return f"{number:.4f}"


def cagr(analysis: dict[str, Any] | None) -> float | None:
    """Annualised growth, or None for a liquidated arm.

    A liquidated run stops at the floor within days, so annualising its truncated window
    produces a meaningless number; the study reports `n/a` and the survival days instead.
    """
    if not analysis:
        return None
    if analysis.get("liquidated"):
        return None
    gain = analysis.get("gain_strategy_eq")
    days = analysis.get("n_days")
    try:
        gain = float(gain)
        days = float(days)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(gain) and math.isfinite(days)) or gain <= 0 or days <= 0:
        return None
    return gain ** (365.25 / days) - 1.0


def worst_event_drawdown(events_payload: dict[str, Any] | None) -> float | None:
    if not events_payload:
        return None
    values = [
        float(item["max_drawdown"])
        for item in events_payload.get("events", [])
        if item.get("max_drawdown") is not None
    ]
    return max(values) if values else None


def event_drawdown_map(events_payload: dict[str, Any] | None) -> dict[str, float]:
    if not events_payload:
        return {}
    return {
        str(item["event"]): float(item["max_drawdown"])
        for item in events_payload.get("events", [])
        if item.get("max_drawdown") is not None
    }


def liquidation_shock_text(shock: Any) -> str:
    """Format the distance to the engine's floor; >100% means price alone cannot reach it."""
    if shock is None:
        return "n/a"
    if float(shock) >= 1.0:
        return ">100%（价格归零也不足以触及地板）"
    return f"−{float(shock) * 100:.2f}%"


def md_table(rows: list[list[str]], header: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def arms_of(arms: list[dict[str, Any]], leg: str) -> list[dict[str, Any]]:
    return [arm for arm in arms if arm["arm"].leg == leg]


def control_of(
    leg: str, arms: list[dict[str, Any]], references: list[dict[str, Any]]
) -> dict[str, Any] | None:
    name = REFERENCE_CONTROL.get(leg)
    for arm in arms:
        if arm["key"] == name:
            return arm
    for reference in references:
        if reference["key"] == name:
            return reference
    return None


def summary_entry(entry: dict[str, Any]) -> dict[str, Any]:
    arm = entry.get("arm")
    analysis = entry.get("analysis") or {}
    record = entry.get("record") or {}
    ruin = ((entry.get("wipeout") or {}).get("ruin")) or {}
    liquidation = record.get("liquidation") or {}
    return {
        "leg": arm.leg if arm else entry.get("leg") or "3y",
        "lever": arm.lever if arm else entry["key"],
        "run_dir": study.relative(entry["run_dir"]),
        "starting_balance": (arm.starting_balance if arm else record.get("capital", {}).get("starting_balance")),
        "declared_twe": (arm.declared_twe if arm else ruin.get("declared_twe")),
        "per_slot_cap": (arm.per_slot_cap if arm else None),
        "gain_strategy_eq": analysis.get("gain_strategy_eq"),
        "cagr": cagr(analysis),
        "drawdown_worst_strategy_eq": analysis.get("drawdown_worst_strategy_eq"),
        "drawdown_worst_mean_1pct_strategy_eq": analysis.get(
            "drawdown_worst_mean_1pct_strategy_eq"
        ),
        "worst_event_drawdown": worst_event_drawdown(entry.get("events")),
        "peak_total_exposure": ruin.get(
            "peak_total_exposure", analysis.get("total_wallet_exposure_max")
        ),
        "liquidation_shock_at_peak": ruin.get("liquidation_shock_at_peak"),
        "worst_coin_exposure": ruin.get("worst_coin_exposure"),
        "coin_exposure_limit": None,
        "liquidated": bool(analysis.get("liquidated")),
        "effective_end_date": (record.get("window") or {}).get("effective_end_date")
        or analysis.get("effective_end_date"),
        "days_to_liquidation": liquidation.get("days_to_liquidation"),
        "liquidation_floor_usd": liquidation.get("floor_usd"),
        "hard_stop_triggers": analysis.get("hard_stop_triggers"),
        "hard_stop_panic_close_loss_sum": analysis.get("hard_stop_panic_close_loss_sum"),
        "affordable_coins": (entry.get("affordability") or {}).get("affordable_count"),
        "affordability_coin_count": (entry.get("affordability") or {}).get("coin_count"),
    }


def build_document(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    lines: list[str] = []
    summary: dict[str, Any] = {"arms": {}, "verdicts": {}, "reference_arms": []}

    lines.append("# g4 门控 profile @ TWE 3.0 / 10,000 USDT：回测与尾部实测")
    lines.append("")
    lines.append(
        "本文件是研究的跨 arm 综合结论；每个 arm 的完整深度分析（仓库报告规范固定骨架 + 研究附录）"
        "在其自己的 run 目录 `annual_analysis.md` 中，两者数字同源、可独立复算。"
    )
    lines.append("")

    lines.append("## 一、问题与方法")
    lines.append("")
    lines.append(
        "把已发布的 g4 门控 profile 在**同一份冻结数据**上重跑，把总暴露上限从 1.0 提到 **3.0**、"
        "起始资金从 100,000 降到 **10,000 USDT**，量出账户能撑到什么程度、什么时候被强平；"
        "并按上一轮研究的建议，加一条硬底线（HSL `unified` + `red_threshold=0.10`）做对照。"
    )
    lines.append("")
    for dataset in variant_input["datasets"]:
        lines.append(
            f"- 数据集 `{dataset['key']}`：`{dataset['path']}`，窗口 "
            f"{dataset['window'][0]} → {dataset['window'][1]}。"
        )
    capital = variant_input["capital_contract"]
    liquidation = variant_input["liquidation_contract"]
    lines.append(
        f"- 资金契约：起始资金 {capital['arm_starting_balance']:,} USDT（发布 profile "
        f"{capital['parent_starting_balance']:,}），由 `{capital['starting_balance_path']}` 驱动全部"
        "按余额缩放的仓位。"
    )
    lines.append(
        f"- 强平契约：`{liquidation['threshold_path']}` = {liquidation['threshold']} ⇒ 引擎地板 = "
        "起始资金 × 5%；**触发时引擎在该根 K 线结束回测**（`liquidated=true`），其后数值无意义。"
    )
    lines.append(
        "- 判定规则：" + "；".join(f"**{rule['id']}** {rule['rule']}" for rule in variant_input["decision_rules"])
    )
    lines.append(
        "- 所有 arm 都通过运行时开关 `--disable_plotting coin_fills` 关闭逐币成交面板（本机内存约 "
        "7.8 GB）；摘要图与全部数据产物不受影响。"
    )
    lines.append("")

    lines.append("## 二、全期指标对比")
    lines.append("")
    for leg in ("3y", "ext"):
        leg_arms = arms_of(arms, leg)
        if not leg_arms and not (leg == "3y"):
            continue
        lines.append(f"### {LEG_TITLES[leg]}")
        lines.append("")
        header = ["arm", "起始资金", "TWE"] + [label for _k, label, _kind in COMPARED] + [
            "CAGR",
            "强平",
        ]
        rows: list[list[str]] = []
        if leg == "3y":
            for reference in references:
                if reference["leg"] != leg:
                    continue
                rows.append(
                    [f"（锚点）{reference['key']}", "100,000", "1.0"]
                    + [fmt(reference["analysis"].get(key), kind) for key, _l, kind in COMPARED]
                    + [
                        fmt(cagr(reference["analysis"]), "pct"),
                        str(bool(reference["analysis"].get("liquidated"))),
                    ]
                )
        for arm in leg_arms:
            analysis = arm["analysis"] or {}
            variant = arm["arm"]
            rows.append(
                [
                    variant.key,
                    f"{variant.starting_balance:,.0f}",
                    f"{variant.declared_twe:.1f}",
                ]
                + [fmt(analysis.get(key), kind) for key, _l, kind in COMPARED]
                + [
                    "n/a（强平，未满窗口）" if analysis.get("liquidated") else fmt(cagr(analysis), "pct"),
                    str(bool(analysis.get("liquidated"))),
                ]
            )
        lines.append(md_table(rows, header))
        lines.append("")
    lines.append(
        "锚点 `ref_published_3y`（100k / TWE 1.0，原生窗）与 `ref_tailrisk_off_ext`"
        "（100k / TWE 1.0，5.4 年腿）来自前两个 study 的 tracked 运行，其 `analysis.json` 的 "
        "sha256 钉在 `variant_spec.py` 里由验证器复核，本 study 不重跑它们。"
    )
    lines.append("")

    lines.append("## 三、强平读数")
    lines.append("")
    header = ["arm", "起始资金", "TWE", "单槽上界", "总暴露峰值", "强平触发跌幅", "是否强平", "存活天数", "生效结束"]
    rows = []
    for entry in [*references, *arms]:
        arm = entry.get("arm")
        ruin = (entry.get("wipeout") or {}).get("ruin") or {}
        analysis = entry.get("analysis") or {}
        record = entry.get("record") or {}
        liquidation = record.get("liquidation") or {}
        rows.append(
            [
                entry["key"],
                f"{(arm.starting_balance if arm else 100000):,.0f}",
                f"{(arm.declared_twe if arm else 1.0):.1f}",
                f"{(arm.per_slot_cap if arm else 0.1957):.4f}",
                fmt(ruin.get("peak_total_exposure"), "ratio"),
                liquidation_shock_text(ruin.get("liquidation_shock_at_peak")),
                str(bool(analysis.get("liquidated"))),
                "n/a"
                if liquidation.get("days_to_liquidation") is None
                else f"{liquidation['days_to_liquidation']:.1f}",
                str((record.get("window") or {}).get("effective_end_date") or analysis.get("effective_end_date")),
            ]
        )
        candidate = summary_entry(entry)
        if arm is not None:
            candidate["coin_exposure_limit"] = ruin.get("worst_coin_exposure")
        summary["arms"][entry["key"]] = candidate
    lines.append(md_table(rows, header))
    lines.append("")
    lines.append(
        "“强平触发跌幅”是**在实测峰值暴露处**用峰值时刻余额与引擎 5% 地板反解出来的：它回答"
        "“离被强平还差多少行情”，不含路径中的加仓与减仓；`n/a` 表示该 arm 从未建立敞口或余额已在地板。"
    )
    lines.append("")

    lines.append("## 四、事件窗口压力表（基准驱动）")
    lines.append("")
    episodes: list[str] = []
    labels: dict[str, str] = {}
    for arm in arms:
        for item in (arm.get("events") or {}).get("events", []):
            key = str(item["event"])
            if key not in episodes:
                episodes.append(key)
                labels[key] = f"{item['label']}（基准 −{item['benchmark_drop_pct'] * 100:.0f}%）"
    for leg in ("3y", "ext"):
        leg_arms = arms_of(arms, leg)
        if not leg_arms or not episodes:
            continue
        lines.append(f"### {LEG_TITLES[leg]}：事件内最大回撤")
        lines.append("")
        header = ["事件"] + [arm["arm"].key for arm in leg_arms]
        rows = []
        for event in episodes:
            cells = [labels.get(event, event)]
            for arm in leg_arms:
                value = event_drawdown_map(arm.get("events")).get(event)
                cells.append(fmt(value, "pct") if value is not None else "n/a")
            rows.append(cells)
        lines.append(md_table(rows, header))
        lines.append("")
    lines.append("### 控制组事件明细")
    lines.append("")
    for leg in ("3y", "ext"):
        control = control_of(leg, arms, references)
        if control is None or not control.get("events"):
            continue
        lines.append(f"#### {LEG_TITLES[leg]}｜控制组 `{control['key']}`")
        if control["events"].get("recomputed_by_synthesis"):
            lines.append("")
            lines.append("（该锚点没有本研究的 side artifact，事件表由综合脚本用其本地账本复算。）")
        lines.append("")
        header = ["事件", "基准击穿", "基准跌幅", "事件内最大回撤", "到谷天数", "到恢复天数", "水下天数", "窗口收益"]
        rows = []
        for item in control["events"]["events"]:
            rows.append(
                [
                    item["label"],
                    f"{item['breach_start']} → {item['breach_end']}",
                    f"−{item['benchmark_drop_pct'] * 100:.1f}%",
                    fmt(item.get("max_drawdown"), "pct"),
                    fmt(item.get("days_to_trough"), "days", 1),
                    fmt(item.get("days_to_recovery"), "days", 1)
                    if item.get("days_to_recovery") is not None
                    else "未恢复",
                    fmt(item.get("underwater_days"), "days", 0),
                    fmt(item.get("window_return"), "pct"),
                ]
            )
        lines.append(md_table(rows, header))
        lines.append("")

    lines.append("## 五、资金规模对照（10k vs 100k）")
    lines.append("")
    header = ["腿", "arm", "起始资金", "TWE", "收益倍数", "最差回撤", "CAGR", "对 100k 基线的 ΔCAGR"]
    rows = []
    for leg in ("3y", "ext"):
        control = control_of(leg, arms, references)
        base_cagr = cagr((control or {}).get("analysis"))
        for entry in [*arms_of(arms, leg)]:
            analysis = entry["analysis"] or {}
            arm_cagr = cagr(analysis)
            rows.append(
                [
                    leg,
                    entry["arm"].key,
                    f"{entry['arm'].starting_balance:,.0f}",
                    f"{entry['arm'].declared_twe:.1f}",
                    fmt(analysis.get("gain_strategy_eq"), "multiple", 4),
                    fmt(analysis.get("drawdown_worst_strategy_eq"), "pct"),
                    fmt(arm_cagr, "pct"),
                    "n/a"
                    if base_cagr is None or arm_cagr is None
                    else f"{(arm_cagr - base_cagr) * 100:+.2f}pp",
                ]
            )
    lines.append(md_table(rows, header))
    lines.append("")
    lines.append(
        "同一腿内 `twe100_10k` 与 100k 锚点的差异只来自资金规模（仓位按余额等比缩放，"
        "差异应来自最小下单量/步长取整与费用占比）；`twe300_10k` 与 `twe100_10k` 的差异才是 TWE 的净效应。"
    )
    lines.append("")

    lines.append("## 六、暴露上界与冲击损失矩阵")
    lines.append("")
    header = [
        "arm",
        "声明 TWE",
        "总暴露峰值",
        "最大单币敞口",
        "前 3 币之和",
        "−20% 需几币",
        "−50% 需几币",
        "−80% 需几币",
        "强平触发跌幅",
    ]
    rows = []
    for entry in [*references, *arms]:
        payload = entry.get("wipeout")
        if not payload:
            continue
        ruin = payload["ruin"]
        rows.append(
            [
                entry["key"],
                fmt(ruin.get("declared_twe"), "ratio"),
                fmt(ruin["peak_total_exposure"], "ratio"),
                fmt(ruin["worst_coin_exposure"], "ratio"),
                fmt(ruin["top3_exposure"], "ratio"),
                str(ruin["coins_to_breach_20pct"]),
                str(ruin["coins_to_breach_50pct"]),
                str(ruin["coins_to_breach_80pct"]),
                liquidation_shock_text(ruin.get("liquidation_shock_at_peak")),
            ]
        )
    lines.append(md_table(rows, header))
    lines.append("")
    lines.append("### 冲击损失矩阵（占账户比例，上界）")
    lines.append("")
    header = ["arm", "范围"] + [f"−{int(shock * 100)}%" for shock in variant_input["shock_levels"]]
    rows = []
    for entry in [*references, *arms]:
        payload = entry.get("wipeout")
        if not payload:
            continue
        for scope in wipeout.SCOPE_ORDER:
            cells = [
                entry["key"] if scope == wipeout.SCOPE_ORDER[0] else "",
                wipeout.SCOPE_LABELS[scope],
            ]
            for shock in payload["shock_levels"]:
                found = next(
                    item
                    for item in payload["matrix"]
                    if item["scope"] == scope and item["shock"] == shock
                )
                cells.append(f"{found['loss_fraction'] * 100:.1f}%")
            rows.append(cells)
    lines.append(md_table(rows, header))
    lines.append("")

    lines.append("## 七、HSL 熔断：收益代价与运行学")
    lines.append("")
    header = ["arm", "作用域", "red 阈值", "触发", "重启", "panic 实亏 USDT", "停机均值(min)", "复触发%"]
    rows = []
    for entry in [*references, *arms]:
        analysis = entry.get("analysis") or {}
        record = entry.get("record") or {}
        hsl = record.get("hsl_block") or {}
        mode = (record.get("live_hsl") or {}).get("hsl_signal_mode") or (
            "coin" if entry.get("arm") is None else "n/a"
        )
        if entry.get("arm") is None:
            hsl = {"enabled": entry["key"] == "ref_hsl_coin_3y", "red_threshold": 0.15}
        rows.append(
            [
                entry["key"],
                str(mode),
                "—" if not hsl.get("enabled") else str(hsl.get("red_threshold")),
                str(int(analysis.get("hard_stop_triggers") or 0)),
                str(int(analysis.get("hard_stop_restarts") or 0)),
                fmt(analysis.get("hard_stop_panic_close_loss_sum"), "usd"),
                fmt(analysis.get("hard_stop_duration_minutes_mean"), "days", 1),
                fmt(analysis.get("hard_stop_post_restart_retrigger_pct"), "pct"),
            ]
        )
    lines.append(md_table(rows, header))
    lines.append("")

    lines.append("## 八、实盘准入（10,000 USDT）")
    lines.append("")
    header = ["arm", "起始资金", "TWE", "单笔初始入场 USDT", "可入场币数", "全部 40 币所需最低余额 USDT"]
    rows = []
    for entry in arms:
        payload = entry.get("affordability")
        if not payload:
            continue
        rows.append(
            [
                entry["key"],
                f"{payload['starting_balance_usd']:,.0f}",
                f"{payload['total_wallet_exposure_limit']:.2f}",
                f"{payload['entry_cost_usd']:,.2f}",
                f"{payload['affordable_count']}/{payload['coin_count']}",
                "n/a"
                if payload.get("min_balance_for_all_coins_usd") is None
                else f"{payload['min_balance_for_all_coins_usd']:,.0f}",
            ]
        )
    if rows:
        lines.append(md_table(rows, header))
    else:
        lines.append("（尚未生成 affordability 附录；运行 `report_tools/affordability.py --variant <arm>`）")
    lines.append("")
    lines.append(
        "离线近似：用冻结 bundle 的 `min_cost`/`min_qty`/`qty_step`/`contractSize` 与该 arm 成交价"
        "中位数复算；实盘以 `effective_min_cost`（按当前价与交易所合约数据）为准。"
    )
    lines.append("")

    lines.append("## 九、判据裁决")
    lines.append("")
    verdicts = decide(arms, references, variant_input, summary)
    for rule in variant_input["decision_rules"]:
        lines.append(f"### {rule['id']}｜{rule['rule']}")
        lines.append("")
        verdict = verdicts.get(rule["id"])
        if verdict is None:
            lines.append("- 裁决：**未裁决（缺少证据）**")
        else:
            lines.append(f"- 裁决：**{verdict['verdict']}**")
            for item in verdict.get("evidence", []):
                lines.append(f"  - 依据：{item}")
        lines.append("")
    summary["verdicts"] = verdicts

    lines.append("## 十、建议")
    lines.append("")
    for item in recommendations(arms, references, verdicts, summary):
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 十一、边界")
    lines.append("")
    for item in variant_input["honesty_boundaries"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines), summary


def decide(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    verdicts: dict[str, Any] = {}
    by_key = {entry["key"]: entry for entry in arms}

    # J1: did the TWE 3.0 baseline get liquidated on either leg?
    j1_evidence = []
    j1_triggered = False
    for leg in ("3y", "ext"):
        entry = by_key.get(f"twe300_10k__{leg}")
        if not entry:
            continue
        analysis = entry["analysis"] or {}
        record = entry.get("record") or {}
        liquidation = record.get("liquidation") or {}
        liquidated = bool(analysis.get("liquidated"))
        j1_triggered = j1_triggered or liquidated
        j1_evidence.append(
            f"{entry['key']}：最差回撤 {fmt(analysis.get('drawdown_worst_strategy_eq'), 'pct')}、"
            f"强平={liquidated}"
            + (
                f"（存活 {liquidation.get('days_to_liquidation'):.1f} 天，生效结束 "
                f"{(record.get('window') or {}).get('effective_end_date')}）"
                if liquidated and liquidation.get("days_to_liquidation") is not None
                else ""
            )
        )
    verdicts["J1"] = {
        "verdict": "TWE 3.0 在本地历史内被强平（该杠杆不可实盘）" if j1_triggered else "TWE 3.0 在本地历史内未被强平",
        "evidence": j1_evidence or ["缺少 TWE 3.0 基线 arm"],
    }

    # J2: does the hard floor cut the worst drawdown to <=60% of the baseline without liquidating?
    for leg in ("3y", "ext"):
        base = by_key.get(f"twe300_10k__{leg}")
        floor_arm = by_key.get(f"twe300_10k_floor__{leg}")
        if not base or not floor_arm:
            continue
        base_dd = (base["analysis"] or {}).get("drawdown_worst_strategy_eq")
        floor_dd = (floor_arm["analysis"] or {}).get("drawdown_worst_strategy_eq")
        base_cagr = cagr(base["analysis"])
        floor_cagr = cagr(floor_arm["analysis"])
        liquidated = bool((floor_arm["analysis"] or {}).get("liquidated"))
        evidence = [
            f"{base['key']}：最差回撤 {fmt(base_dd, 'pct')}，CAGR {fmt(base_cagr, 'pct')}",
            f"{floor_arm['key']}：最差回撤 {fmt(floor_dd, 'pct')}，CAGR {fmt(floor_cagr, 'pct')}，强平={liquidated}",
        ]
        if base_cagr is not None and floor_cagr is not None:
            evidence.append(f"ΔCAGR {(floor_cagr - base_cagr) * 100:+.2f}pp/年")
        reduced = (
            base_dd is not None
            and floor_dd is not None
            and float(base_dd) > 0
            and float(floor_dd) <= 0.6 * float(base_dd)
        )
        verdicts[f"J2_{leg}"] = {
            "verdict": (
                "硬底线把最差回撤压到基线 60% 以下且未强平"
                if reduced and not liquidated
                else "硬底线未同时满足“回撤 ≤ 基线 60%”与“未强平”"
            ),
            "evidence": evidence,
        }
        summary.setdefault("j2", {})[leg] = {
            "baseline_drawdown": base_dd,
            "floor_drawdown": floor_dd,
            "delta_cagr_pp": None if (base_cagr is None or floor_cagr is None) else (floor_cagr - base_cagr) * 100.0,
            "floor_liquidated": liquidated,
        }

    # J3: is the 10k scale effect negligible against the published 100k anchor?
    for leg, anchor_key in (("3y", "ref_published_3y"), ("ext", "ref_tailrisk_off_ext")):
        control = control_of(leg, arms, references)
        anchor = next((r for r in references if r["key"] == anchor_key), None)
        scale_arm = by_key.get(f"twe100_10k__{leg}")
        if scale_arm is None or anchor is None:
            continue
        scale_cagr = cagr(scale_arm["analysis"])
        anchor_cagr = cagr(anchor["analysis"])
        scale_dd = (scale_arm["analysis"] or {}).get("drawdown_worst_strategy_eq")
        anchor_dd = (anchor["analysis"] or {}).get("drawdown_worst_strategy_eq")
        delta_cagr = (
            None if (scale_cagr is None or anchor_cagr is None) else (scale_cagr - anchor_cagr) * 100.0
        )
        delta_dd = (
            None if (scale_dd is None or anchor_dd is None) else (float(scale_dd) - float(anchor_dd)) * 100.0
        )
        negligible = (
            delta_cagr is not None
            and delta_dd is not None
            and abs(delta_cagr) <= 1.0
            and abs(delta_dd) <= 1.0
        )
        verdicts[f"J3_{leg}"] = {
            "verdict": (
                "资金规模效应可忽略（ΔCAGR 与 Δ最差回撤均 ≤ 1pp）"
                if negligible
                else "资金规模效应不可忽略，已量化"
            ),
            "evidence": [
                f"{scale_arm['key']}（10k / TWE 1.0）：最差回撤 {fmt(scale_dd, 'pct')}，CAGR {fmt(scale_cagr, 'pct')}",
                f"{anchor_key}（100k / TWE 1.0）：最差回撤 {fmt(anchor_dd, 'pct')}，CAGR {fmt(anchor_cagr, 'pct')}",
                f"ΔCAGR {'n/a' if delta_cagr is None else f'{delta_cagr:+.2f}pp'}，"
                f"Δ最差回撤 {'n/a' if delta_dd is None else f'{delta_dd:+.2f}pp'}",
            ],
        }

    # J4: is the structural alternative better than the hard floor on drawdown, without liquidation?
    for leg in ("3y", "ext"):
        allowance_arm = by_key.get(f"twe300_10k_allowance0__{leg}")
        floor_arm = by_key.get(f"twe300_10k_floor__{leg}")
        if not allowance_arm or not floor_arm:
            continue
        allowance_dd = (allowance_arm["analysis"] or {}).get("drawdown_worst_strategy_eq")
        floor_dd = (floor_arm["analysis"] or {}).get("drawdown_worst_strategy_eq")
        allowance_liq = bool((allowance_arm["analysis"] or {}).get("liquidated"))
        better = (
            allowance_dd is not None
            and floor_dd is not None
            and not allowance_liq
            and float(allowance_dd) <= float(floor_dd)
        )
        verdicts[f"J4_{leg}"] = {
            "verdict": (
                "结构性方案（allowance=0）在不发生强平的前提下回撤不劣于硬底线"
                if better
                else "结构性方案未能在回撤上优于硬底线（或同样被强平）"
            ),
            "evidence": [
                f"{allowance_arm['key']}：最差回撤 {fmt(allowance_dd, 'pct')}，"
                f"CAGR {fmt(cagr(allowance_arm['analysis']), 'pct')}，强平={allowance_liq}",
                f"{floor_arm['key']}：最差回撤 {fmt(floor_dd, 'pct')}，"
                f"CAGR {fmt(cagr(floor_arm['analysis']), 'pct')}",
            ],
        }

    # J5: is the terminal variant cheaper than the plain hard floor on terminal wealth?
    never_arm = by_key.get("twe300_10k_never__ext")
    floor_ext = by_key.get("twe300_10k_floor__ext")
    if never_arm and floor_ext:
        never_gain = float((never_arm["analysis"] or {}).get("gain_strategy_eq") or 0.0)
        floor_gain = float((floor_ext["analysis"] or {}).get("gain_strategy_eq") or 0.0)
        verdicts["J5"] = {
            "verdict": (
                "终止式 arm 终值低于硬底线 50%：只应作为人工决策后的最后一档"
                if floor_gain > 0 and never_gain < 0.5 * floor_gain
                else "终止式 arm 的终值不低于硬底线 50%"
            ),
            "evidence": [
                f"{never_arm['key']}：收益倍数 {never_gain:.4f}×，强平="
                f"{bool((never_arm['analysis'] or {}).get('liquidated'))}，"
                f"最长恢复期 {fmt((never_arm['analysis'] or {}).get('strategy_eq_recovery_days_max'), 'days')} 天",
                f"{floor_ext['key']}：收益倍数 {floor_gain:.4f}×，强平="
                f"{bool((floor_ext['analysis'] or {}).get('liquidated'))}",
            ],
        }
    return verdicts


def recommendations(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    verdicts: dict[str, Any],
    summary: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    j1 = verdicts.get("J1", {}).get("verdict", "")
    if "被强平" in j1:
        out.append(
            "**TWE 3.0 在本地历史内被强平**：该杠杆不能直接上实盘；任何沿用它的方案必须先解决"
            "“在强平之前介入”，而不是等引擎地板。"
        )
    else:
        out.append(
            "TWE 3.0 在本地历史内未被强平，但最差回撤已被放大到发布 profile 的数倍；"
            "是否接受这个量级是资金决策，不是策略细节。"
        )
    for leg in ("3y", "ext"):
        verdict = verdicts.get(f"J2_{leg}")
        if verdict:
            out.append(f"{leg} 腿硬底线裁决：{verdict['verdict']}。")
    for leg in ("3y", "ext"):
        verdict = verdicts.get(f"J3_{leg}")
        if verdict:
            out.append(f"{leg} 腿资金规模裁决：{verdict['verdict']}。")
    out.append(
        "组合级熔断（`unified`）在 TWE 3.0 下会明显更频繁触发：它按策略净值计算回撤，"
        "满仓时约 −3.3% 行情即触发 10% 阈值，因此它的作用是“压低尾部 + 付出停牌成本”，"
        "不解决“高杠杆本身不划算”的问题。"
    )
    out.append(
        "结构性替代（`we_excess_allowance_pct=0`）把单槽上界从 58.7% 降到 42.9%，"
        "不需要停牌、也不引入 panic 实亏；如果只想要一个“不需要盯盘”的稳健改动，优先它。"
    )
    out.append(
        "实盘准入：10,000 USDT 下仍可能有币的初始入场低于交易所最小名义价值（见第八节），"
        "上线前用 `passivbot tool entry-regime-probe --balance 10000` 复核一次，"
        "或在 `live.filter_by_min_effective_cost` 保持开启的前提下接受这些币不参与。"
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-missing", action="store_true", help="write with the arms present")
    args = parser.parse_args(argv)

    variant_input = study.load_json(study.VARIANT_INPUT_PATH)
    arms: list[dict[str, Any]] = []
    missing: list[str] = []
    for key in study.RUN_VARIANT_ORDER:
        evidence = load_evidence(study.VARIANTS_BY_KEY[key])
        if evidence is None or evidence.get("analysis") is None:
            missing.append(key)
            continue
        arms.append(evidence)
    references = reference_evidence()
    if missing and not args.allow_missing:
        print("FAIL: synthesis needs every declared arm", file=sys.stderr)
        for key in missing:
            print(f"  - missing run for {key}", file=sys.stderr)
        return 1

    document, summary = build_document(arms, references, variant_input)
    summary["generated_by"] = "report_tools/build_synthesis.py"
    summary["missing_arms"] = missing
    summary["reference_arms"] = [
        {
            "key": reference["key"],
            "label": reference["label"],
            "leg": reference["leg"],
            "run_dir": study.relative(reference["run_dir"]),
            "analysis_sha256": reference["analysis_sha256"],
            "pinned_sha256": reference["pinned_sha256"],
            "matches_pin": reference["analysis_sha256"] == reference["pinned_sha256"],
            "events_recomputed_by_synthesis": bool(
                (reference.get("events") or {}).get("recomputed_by_synthesis")
            ),
        }
        for reference in references
    ]
    OUTPUT_PATH.write_text(document, encoding="utf-8")
    study.write_json(SUMMARY_PATH, summary)
    print(f"wrote {study.relative(OUTPUT_PATH)}")
    print(f"wrote {study.relative(SUMMARY_PATH)}")
    print(f"arms={len(arms)} missing={len(missing)} references={len(references)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
