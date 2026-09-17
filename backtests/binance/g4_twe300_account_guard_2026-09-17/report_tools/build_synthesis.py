#!/usr/bin/env python3
"""Build the study-level synthesis for the g4 @ TWE 3.0 account-guard study.

Every arm has its own deep analysis in its run directory (report convention, Rule 1); this
document answers the questions the study was commissioned for:

1. does the requested account-level guard ("within a week, a 20% floating loss halts for 12h;
   a second 20% loss on the remaining balance escalates to 24h") keep the TWE 3.0 arm alive —
   in particular on the 5.4-year leg where the unguarded arm was liquidated on 2021-05-19?
2. what does the guard cost on the benign native window (terminal wealth, worst drawdown,
   time out of the market), and is the account "ready to go again" after each halt?
3. what is the closest expressible form of the requested escalation ladder, and what would an
   engine-level ladder add (design documented separately)?

It reads only tracked evidence plus each arm's own side artifacts, and writes
`account_guard_analysis.md` and `artifacts/account_guard_summary.json`.

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

OUTPUT_PATH = study.STUDY / "account_guard_analysis.md"
SUMMARY_PATH = study.ARTIFACTS / "account_guard_summary.json"

COMPARED = (
    ("gain_strategy_eq", "收益倍数", "multiple"),
    ("drawdown_worst_strategy_eq", "全期最差回撤", "pct"),
    ("drawdown_worst_mean_1pct_strategy_eq", "最差 1% 均值回撤", "pct"),
    ("total_wallet_exposure_max", "总暴露峰值", "ratio"),
    ("strategy_eq_recovery_days_max", "最长恢复期（天）", "days"),
    ("omega_ratio_strategy_eq", "Omega", "ratio"),
)
LEG_TITLES = {
    "3y": "原生窗口腿（2023-09-12 → 2026-09-12）",
    "ext": "历史压力腿（2021-04-20 → 2026-09-13，2021-05-19 崩盘在其中）",
}
REFERENCE_CONTROL = {"3y": "twe300_10k__3y", "ext": "twe300_10k__ext"}


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
        ("guard_readiness.json", "readiness"),
    ):
        path = run_dir / name
        evidence[target] = study.load_json(path) if path.exists() else None
    return evidence


def event_table_for(run_dir: Path, dataset_path: Path) -> dict[str, Any] | None:
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
            "readiness": None,
            "arm": None,
        }
        tail = run_dir / "tail_risk_events.json"
        entry["events"] = (
            study.load_json(tail)
            if tail.exists()
            else event_table_for(run_dir, study.DATASETS[item["leg"]].path)
        )
        wipeout_path = run_dir / "tail_risk_wipeout.json"
        if wipeout_path.exists():
            entry["wipeout"] = study.load_json(wipeout_path)
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
    if kind == "hours":
        return f"{number:.1f}"
    if kind == "minutes":
        return f"{number:,.0f}"
    return f"{number:.4f}"


def cagr(analysis: dict[str, Any] | None) -> float | None:
    """Annualised growth; a liquidated arm's truncated window has no meaningful CAGR."""
    if not analysis or analysis.get("liquidated"):
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


def guard_of(entry: dict[str, Any]) -> dict[str, Any]:
    arm = entry.get("arm")
    if arm is not None:
        return arm.guard_params
    return {
        "enabled": False,
        "scope": "coin",
        "red_threshold": 0.15,
        "orange_threshold": 0.1125,
        "ema_span_minutes": 720.0,
        "cooldown_minutes_after_red": 2160.0,
        "lookback_days": 30.0,
        "no_restart_drawdown_threshold": 1.0,
        "restart_after_red_policy": "threshold",
        "panic_close_order_type": "limit",
    }


def summary_entry(entry: dict[str, Any]) -> dict[str, Any]:
    arm = entry.get("arm")
    analysis = entry.get("analysis") or {}
    record = entry.get("record") or {}
    ruin = ((entry.get("wipeout") or {}).get("ruin")) or {}
    readiness = entry.get("readiness") or {}
    guard = guard_of(entry)
    return {
        "leg": arm.leg if arm else entry.get("leg") or "3y",
        "run_dir": study.relative(entry["run_dir"]),
        "guard": guard,
        "guard_label": arm.guard_label if arm else "无守护（对照，上一轮）",
        "starting_balance": arm.starting_balance if arm else 10000.0,
        "declared_twe": arm.declared_twe if arm else 3.0,
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
        "liquidated": bool(analysis.get("liquidated")),
        "effective_end_date": (record.get("window") or {}).get("effective_end_date")
        or analysis.get("effective_end_date"),
        "hard_stop_triggers": analysis.get("hard_stop_triggers"),
        "hard_stop_restarts": analysis.get("hard_stop_restarts"),
        "hard_stop_time_in_orange_pct": analysis.get("hard_stop_time_in_orange_pct"),
        "hard_stop_time_in_red_pct": analysis.get("hard_stop_time_in_red_pct"),
        "hard_stop_panic_close_loss_sum": analysis.get("hard_stop_panic_close_loss_sum"),
        "hard_stop_post_restart_retrigger_pct": analysis.get(
            "hard_stop_post_restart_retrigger_pct"
        ),
        "halt_count": readiness.get("halt_count"),
        "complete_halt_count": readiness.get("complete_halt_count"),
        "terminal_halt_count": readiness.get("terminal_halt_count"),
        "total_halt_hours": readiness.get("total_halt_hours"),
        "max_halt_hours": (
            readiness.get("max_halt_minutes") / 60.0
            if readiness.get("max_halt_minutes") is not None
            else None
        ),
        "declared_halt_hours": declared_halt_hours(readiness),
        "out_of_market_vs_declared_ratio": readiness.get("out_of_market_vs_declared_ratio"),
        "idle_after_halt_minutes_max": readiness.get("idle_after_halt_minutes_max"),
        "guard_cross_check_problems": list(readiness.get("cross_check_problems") or []),
        "halts": list(readiness.get("halts") or []),
        "post_halt_return_7d_mean": readiness.get("post_halt_return_7d_mean"),
        "post_halt_return_30d_mean": readiness.get("post_halt_return_30d_mean"),
        "immediate_retriggers_within_7d": readiness.get("immediate_retriggers_within_7d"),
    }


def declared_halt_hours(readiness: dict[str, Any]) -> float | None:
    """Total minutes the guard itself promised to keep the account out, in hours."""
    declared = [
        float(halt["declared_minutes"])
        for halt in (readiness.get("halts") or [])
        if halt.get("declared_minutes") is not None
    ]
    if not declared:
        return None
    return sum(declared) / 60.0


def build_document(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    lines: list[str] = []
    summary: dict[str, Any] = {"arms": {}, "verdicts": {}, "control_arms": []}

    lines.append("# g4 @ TWE 3.0 的账户级守护实测：一周 −20% 熔断与阶梯升级")
    lines.append("")
    lines.append(
        "本文件是研究的跨 arm 综合结论；每个 arm 的完整深度分析（仓库报告规范固定骨架 + 研究附录，"
        "含“账户守护读数”与“整装待发读数”）在其自己的 run 目录 `annual_analysis.md` 中。"
    )
    lines.append("")

    lines.append("## 一、问题、条件映射与方法")
    lines.append("")
    lines.append(
        "上一轮已证明：**无守护的 10,000 USDT / TWE 3.0 在本地历史里于 2021-05-19 被强平**"
        "（窗口开始后 29.5 天）。本轮给同一配置加一层账户级守护，回答它能不能“大难不死、还能整装待发”。"
    )
    lines.append("")
    lines.append("### 你的条件 → 引擎配置映射")
    lines.append("")
    mapping = variant_input["guard_contract"]["mapping_table"]
    lines.append(
        md_table(
            [[row["request"], row["engine"], f"`{row['config']}`", row["note"]] for row in mapping],
            ["你的条件", "引擎口径", "配置", "说明"],
        )
    )
    lines.append("")
    lines.append("### 引擎分层语义（已逐条在代码里核实）")
    lines.append("")
    lines.append(
        md_table(
            [
                [tier, behaviour]
                for tier, behaviour in variant_input["guard_contract"]["tier_semantics"].items()
            ],
            ["档位", "行为"],
        )
    )
    lines.append("")
    lines.append(
        f"- 峰值窗口由 `{variant_input['guard_contract']['lookback_path']}` 控制，本轮守护臂统一设为 "
        f"{variant_input['guard_contract']['lookback_days']:.0f} 天（=“一周内”）。"
    )
    lines.append(
        "- 判定规则："
        + "；".join(
            f"**{rule['id']}** {rule['rule']}" for rule in variant_input["decision_rules"]
        )
    )
    lines.append(
        "- 控制臂为上一轮的两条无守护运行（`twe300_10k__3y` / `twe300_10k__ext`），其 `analysis.json` "
        "sha256 已钉住、不重跑。"
    )
    lines.append("")

    lines.append("## 二、全期指标对比")
    lines.append("")
    for leg in ("3y", "ext"):
        leg_arms = arms_of(arms, leg)
        control = control_of(leg, arms, references)
        if not leg_arms and control is None:
            continue
        lines.append(f"### {LEG_TITLES[leg]}")
        lines.append("")
        header = ["arm", "守护", "强平"] + [label for _k, label, _kind in COMPARED] + ["CAGR"]
        rows: list[list[str]] = []
        entries = list(leg_arms)
        if control is not None and control.get("arm") is None:
            entries = [control, *entries]
        for entry in entries:
            analysis = entry.get("analysis") or {}
            label = (
                entry["arm"].guard_label
                if entry.get("arm") is not None
                else "无守护（上一轮控制臂）"
            )
            rows.append(
                [entry["key"], label, str(bool(analysis.get("liquidated")))]
                + [fmt(analysis.get(key), kind) for key, _l, kind in COMPARED]
                + ["n/a（强平）" if analysis.get("liquidated") else fmt(cagr(analysis), "pct")]
            )
        lines.append(md_table(rows, header))
        lines.append("")

    lines.append("## 三、守护读数对照")
    lines.append("")
    header = [
        "arm",
        "作用域",
        "RED",
        "ORANGE",
        "EMA(min)",
        "停机(min)",
        "窗口(天)",
        "永久停机阈值",
        "触发",
        "停机次数",
        "终局停机",
        "停机总时长(h)",
        "橙/红占比",
        "panic 实亏 USDT",
        "复触发%",
    ]
    rows = []
    for entry in [*references, *arms]:
        analysis = entry.get("analysis") or {}
        readiness = entry.get("readiness") or {}
        guard = guard_of(entry)
        rows.append(
            [
                entry["key"],
                str(guard.get("scope")),
                f"{guard.get('red_threshold', 0.0):.2f}",
                f"{guard.get('orange_threshold', 0.0):.4f}",
                f"{guard.get('ema_span_minutes', 0.0):.0f}",
                f"{guard.get('cooldown_minutes_after_red', 0.0):.0f}",
                f"{guard.get('lookback_days', 0.0):.0f}",
                f"{guard.get('no_restart_drawdown_threshold', 1.0):.2f}",
                str(int(analysis.get("hard_stop_triggers") or 0)),
                str(readiness.get("halt_count", "n/a")),
                str(readiness.get("terminal_halt_count", 0)),
                fmt(readiness.get("total_halt_hours"), "hours"),
                " / ".join(
                    fmt(analysis.get(key), "pct")
                    for key in ("hard_stop_time_in_orange_pct", "hard_stop_time_in_red_pct")
                ),
                fmt(analysis.get("hard_stop_panic_close_loss_sum"), "usd"),
                fmt(analysis.get("hard_stop_post_restart_retrigger_pct"), "pct"),
            ]
        )
    lines.append(md_table(rows, header))
    lines.append("")

    lines.append("## 四、整装待发读数（停机窗口与复牌表现）")
    lines.append("")
    header = [
        "arm",
        "停机次数",
        "终局停机",
        "声明停机(h)",
        "离场总时长(h)",
        "离场/声明",
        "最长额外空仓(min)",
        "占窗口%",
        "复牌后 7 天均值",
        "复牌后 30 天均值",
        "7 天内再次停机",
    ]
    rows = []
    for entry in arms:
        readiness = entry.get("readiness") or {}
        rows.append(
            [
                entry["key"],
                str(readiness.get("halt_count", 0)),
                str(readiness.get("terminal_halt_count", 0)),
                fmt(declared_halt_hours(readiness), "hours"),
                fmt(readiness.get("total_halt_hours"), "hours"),
                (
                    "n/a"
                    if readiness.get("out_of_market_vs_declared_ratio") is None
                    else f"{readiness['out_of_market_vs_declared_ratio']:.2f}"
                ),
                fmt(readiness.get("idle_after_halt_minutes_max"), "minutes"),
                fmt(readiness.get("halt_share_of_window_pct"), "pct"),
                fmt(readiness.get("post_halt_return_7d_mean"), "pct"),
                fmt(readiness.get("post_halt_return_30d_mean"), "pct"),
                str(readiness.get("immediate_retriggers_within_7d", 0)),
            ]
        )
    lines.append(md_table(rows, header))
    lines.append("")
    lines.append(
        "口径：停机窗口 = **紧跟 panic 平仓（≤180 分钟，覆盖约 2 分钟的强平延迟与 60 分钟采样格）**"
        "之后、权益恒定（容差 1e-6 USDT）且无成交的连续段；结束后有成交 = 完整停机，"
        "窗口止于复牌成交；结束后再无成交 = **终局停机**（守护锁存），窗口止于回测结束。"
        "窗口长度与 `cooldown_minutes_after_red`（终局停机则与 `hard_stop_duration_minutes_max`）比较："
        "比声明短超过两个采样格（120 分钟）才算不一致，比声明长则记为“停机结束后等不到入场信号的空仓时间”。"
        "每个 arm 的逐次明细在其自己的 `annual_analysis.md` 的“整装待发读数”表里。"
        "表里的“停机起点回撤”是**账户权益相对全期运行峰值**的回撤（与本报告其它回撤读数同口径），"
        "不是引擎触发用的“7 天滚动策略权益峰值”口径（后者见 `hard_stop_trigger_drawdown_mean`）："
        "两次急跌（2021-05-19、2021-05-23）在 20% 触发线被击穿、但 panic 平仓确认时全期回撤已经更深，"
        "所以两个口径不可混读。"
    )
    lines.append("")
    detail = [entry for entry in arms if (entry.get("readiness") or {}).get("halts")]
    if detail:
        lines.append("### 逐次停机明细（每个 arm 的每一次停机）")
        lines.append("")
        header = [
            "arm",
            "开始",
            "结束",
            "时长(h)",
            "声明(h)",
            "超出(h)",
            "停机起点回撤",
            "停机时权益 USDT",
            "复牌后 7 天",
            "复牌后 30 天",
        ]
        rows = []
        for entry in detail:
            for halt in (entry.get("readiness") or {}).get("halts") or []:
                rows.append(
                    [
                        entry["key"],
                        str(halt["start"])[:16],
                        str(halt["end"])[:16],
                        f"{halt['hours']:.1f}",
                        "n/a"
                        if halt.get("declared_minutes") is None
                        else f"{halt['declared_minutes'] / 60.0:.1f}",
                        "n/a"
                        if halt.get("excess_minutes") is None
                        else f"{halt['excess_minutes'] / 60.0:.1f}",
                        fmt(halt.get("drawdown_at_start"), "pct"),
                        fmt(halt.get("equity_usd"), "usd"),
                        "n/a（终局停机）"
                        if halt.get("terminal")
                        else fmt(halt.get("post_halt_return_7d"), "pct"),
                        "n/a（终局停机）"
                        if halt.get("terminal")
                        else fmt(halt.get("post_halt_return_30d"), "pct"),
                    ]
                )
        lines.append(md_table(rows, header))
        lines.append("")

    lines.append("## 五、强平与存活")
    lines.append("")
    header = ["arm", "是否强平", "存活天数/生效结束", "最差回撤", "总暴露峰值", "终值倍数"]
    rows = []
    for entry in [*references, *arms]:
        analysis = entry.get("analysis") or {}
        record = entry.get("record") or {}
        ruin = (entry.get("wipeout") or {}).get("ruin") or {}
        rows.append(
            [
                entry["key"],
                str(bool(analysis.get("liquidated"))),
                str(
                    (record.get("window") or {}).get("effective_end_date")
                    or analysis.get("effective_end_date")
                ),
                fmt(analysis.get("drawdown_worst_strategy_eq"), "pct"),
                fmt(
                    ruin.get("peak_total_exposure", analysis.get("total_wallet_exposure_max")),
                    "ratio",
                ),
                fmt(analysis.get("gain_strategy_eq"), "multiple", 4),
            ]
        )
        summary["arms"][entry["key"]] = summary_entry(entry)
    lines.append(md_table(rows, header))
    lines.append("")
    terminated = [
        entry["key"]
        for entry in arms
        if ((entry.get("readiness") or {}).get("terminal_halt_count") or 0) > 0
    ]
    if terminated:
        lines.append(
            "注意：**“未强平”不等于“还活着”**。"
            + "、".join(f"`{key}`" for key in terminated)
            + " 都没有触及 5% 强平地板，但守护被锁存后账户再未交易——它的终值是“退出时的那一版”，"
            "而不是“继续经营的终值”。判别两者要看下一节的终局停机列。"
        )
        lines.append("")

    lines.append("## 六、事件窗口压力表（基准驱动）")
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

    lines.append("## 七、暴露上界与冲击损失矩阵")
    lines.append("")
    header = [
        "arm",
        "声明 TWE",
        "总暴露峰值",
        "最大单币敞口",
        "前 3 币之和",
        "−20% 需几币",
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
                liquidation_shock_text(ruin.get("liquidation_shock_at_peak")),
            ]
        )
    lines.append(md_table(rows, header))
    lines.append("")
    lines.append("### 冲击损失矩阵（占账户比例，上界）")
    lines.append("")
    header = ["arm", "范围"] + [
        f"−{int(shock * 100)}%" for shock in variant_input["shock_levels"]
    ]
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

    lines.append("## 八、判据裁决")
    lines.append("")
    verdicts = decide(arms, references, variant_input, summary)
    for rule in variant_input["decision_rules"]:
        lines.append(f"### {rule['id']}｜{rule['rule']}")
        lines.append("")
        matches = [
            (key, value)
            for key, value in verdicts.items()
            if key == rule["id"] or key.startswith(f"{rule['id']}_")
        ]
        if not matches:
            lines.append("- 裁决：**未裁决（缺少证据）**")
        else:
            for key, verdict in matches:
                lines.append(f"- 裁决（{key}）：**{verdict['verdict']}**")
                for item in verdict.get("evidence", []):
                    lines.append(f"  - 依据：{item}")
        lines.append("")
    summary["verdicts"] = verdicts

    lines.append("## 九、结论与建议")
    lines.append("")
    for item in recommendations(arms, references, verdicts):
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 十、边界")
    lines.append("")
    for item in variant_input["honesty_boundaries"]:
        lines.append(f"- {item}")
    lines.append(
        "- 引擎级增强（阶梯冷却、按剩余余额重设的第二级、冷却后部分复入、与日线闸门联动）的设计与"
        "风险写在 `account_guard_design.md`，需要独立 PR + 实盘重建契约 + 测试矩阵。"
    )
    lines.append("")
    return "\n".join(lines), summary


def liquidation_shock_text(shock: Any) -> str:
    if shock is None:
        return "n/a"
    if float(shock) >= 1.0:
        return ">100%（价格归零也不足以触及地板）"
    return f"−{float(shock) * 100:.2f}%"


def decide(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    verdicts: dict[str, Any] = {}
    by_key = {entry["key"]: entry for entry in arms}

    survivors: list[str] = []
    j1_evidence: list[str] = []
    control_ext = control_of("ext", arms, references)
    if control_ext is not None:
        analysis = control_ext.get("analysis") or {}
        j1_evidence.append(
            f"控制臂 {control_ext['key']}（无守护）：强平={bool(analysis.get('liquidated'))}，"
            f"最差回撤 {fmt(analysis.get('drawdown_worst_strategy_eq'), 'pct')}，"
            f"生效结束 "
            f"{(control_ext.get('record') or {}).get('window', {}).get('effective_end_date')}"
        )
    for entry in arms_of(arms, "ext"):
        analysis = entry["analysis"] or {}
        readiness = entry.get("readiness") or {}
        liquidated = bool(analysis.get("liquidated"))
        if not liquidated:
            survivors.append(entry["key"])
        terminal = int(readiness.get("terminal_halt_count") or 0)
        first_halts = readiness.get("halts") or []
        terminal_start = str(first_halts[0].get("start", ""))[:16] if first_halts else ""
        j1_evidence.append(
            f"{entry['key']}（{entry['arm'].guard_label}）：强平={liquidated}，"
            f"最差回撤 {fmt(analysis.get('drawdown_worst_strategy_eq'), 'pct')}，"
            f"终值 {fmt(analysis.get('gain_strategy_eq'), 'multiple', 4)}"
            + (
                f"，但**终局停机**在 {terminal_start} 之后"
                f"再未交易（停机 {fmt(readiness.get('total_halt_hours'), 'hours')}，"
                f"占窗口 {fmt(readiness.get('halt_share_of_window_pct'), 'pct')}）"
                if terminal
                else f"，完整停机 {readiness.get('halt_count', 0)} 次、"
                f"其中 {readiness.get('immediate_retriggers_within_7d', 0)} 次在 7 天内再次触发"
            )
        )
    verdicts["J1"] = {
        "verdict": (
            f"有守护臂活下来：{', '.join(survivors)}"
            if survivors
            else "所有守护臂在 5.4 年腿上仍被强平：在 TWE 3.0 这一档账户级止损来不及"
        ),
        "evidence": j1_evidence,
    }
    summary.setdefault("j1", {})["ext_survivors"] = survivors

    control_3y = control_of("3y", arms, references)
    base_dd = float(
        (control_3y or {}).get("analysis", {}).get("drawdown_worst_strategy_eq") or 0.0
    )
    base_cagr = cagr((control_3y or {}).get("analysis"))
    passing: list[str] = []
    evidence: list[str] = []
    for entry in arms_of(arms, "3y"):
        analysis = entry["analysis"] or {}
        dd = analysis.get("drawdown_worst_strategy_eq")
        arm_cagr = cagr(analysis)
        ok = (
            dd is not None
            and base_dd > 0
            and float(dd) <= 0.8 * base_dd
            and arm_cagr is not None
            and base_cagr is not None
            and arm_cagr >= base_cagr - 0.30
        )
        if ok:
            passing.append(entry["key"])
        evidence.append(
            f"{entry['key']}（{entry['arm'].guard_label}）：最差回撤 {fmt(dd, 'pct')}"
            f"（基线 {fmt(base_dd, 'pct')}），CAGR {fmt(arm_cagr, 'pct')}"
            f"（基线 {fmt(base_cagr, 'pct')}）⇒ {'通过' if ok else '未通过'}"
        )
    verdicts["J2"] = {
        "verdict": (
            f"通过：{', '.join(passing)}"
            if passing
            else "3 年腿没有守护 arm 同时满足回撤 ≤ 基线 80% 与 CAGR ≥ 基线 −30pp"
        ),
        "evidence": evidence,
    }
    summary["j2"] = {
        "baseline_drawdown": base_dd,
        "baseline_cagr": base_cagr,
        "passing": passing,
    }

    for leg in ("3y", "ext"):
        short = by_key.get(f"g_user12h__{leg}")
        long = by_key.get(f"g_user24h__{leg}")
        if not short or not long:
            continue
        short_analysis = short["analysis"] or {}
        long_analysis = long["analysis"] or {}
        short_dd = float(short_analysis.get("drawdown_worst_strategy_eq") or 0.0)
        long_dd = float(long_analysis.get("drawdown_worst_strategy_eq") or 0.0)
        short_liq = bool(short_analysis.get("liquidated"))
        long_liq = bool(long_analysis.get("liquidated"))
        short_readiness = short.get("readiness") or {}
        long_readiness = long.get("readiness") or {}
        short_r7 = short_readiness.get("post_halt_return_7d_mean")
        long_r7 = long_readiness.get("post_halt_return_7d_mean")
        short_r30 = short_readiness.get("post_halt_return_30d_mean")
        long_r30 = long_readiness.get("post_halt_return_30d_mean")
        # The pre-registered rule names three surfaces: liquidation, worst drawdown and the
        # post-resume readings. A missing post-resume reading (no complete halt) drops out of the
        # comparison instead of counting as "not worse".
        survival_ok = (not long_liq) or short_liq
        drawdown_ok = long_dd <= short_dd + 1e-9
        resume_ok = (
            True
            if short_r7 is None or long_r7 is None
            else float(long_r7) >= float(short_r7) - 1e-9
        )
        better = survival_ok and drawdown_ok and resume_ok
        verdicts[f"J3_{leg}"] = {
            "verdict": (
                "24H 停机不更差 ⇒ 阶梯升级值得做"
                if better
                else "24H 停机更差 ⇒ 阶梯升级需要额外证据支撑"
            ),
            "evidence": [
                f"12H：强平={short_liq}，最差回撤 {fmt(short_dd, 'pct')}，"
                f"停机 {fmt(short_readiness.get('total_halt_hours'), 'hours')} 小时，"
                f"复牌后 7/30 天 {fmt(short_r7, 'pct')} / {fmt(short_r30, 'pct')}，"
                f"终值 {fmt(short_analysis.get('gain_strategy_eq'), 'multiple', 4)}",
                f"24H：强平={long_liq}，最差回撤 {fmt(long_dd, 'pct')}，"
                f"停机 {fmt(long_readiness.get('total_halt_hours'), 'hours')} 小时，"
                f"复牌后 7/30 天 {fmt(long_r7, 'pct')} / {fmt(long_r30, 'pct')}，"
                f"终值 {fmt(long_analysis.get('gain_strategy_eq'), 'multiple', 4)}",
                f"判定口径：强平（{'不更差' if survival_ok else '更差'}）、"
                f"最差回撤（{'不更差' if drawdown_ok else '更差'}）、"
                f"复牌后 7 天（{'不更差' if resume_ok else '更差'}）",
            ],
        }

    for leg in ("3y", "ext"):
        literal = by_key.get(f"g_user12h__{leg}")
        if literal is None:
            continue
        literal_dd = float((literal["analysis"] or {}).get("drawdown_worst_strategy_eq") or 0.0)
        literal_gain = float((literal["analysis"] or {}).get("gain_strategy_eq") or 0.0)
        best: list[str] = []
        evidence = []
        for key in (f"g_soft_orange__{leg}", f"g_orange_only__{leg}"):
            entry = by_key.get(key)
            if entry is None:
                continue
            dd = float((entry["analysis"] or {}).get("drawdown_worst_strategy_eq") or 0.0)
            gain = float((entry["analysis"] or {}).get("gain_strategy_eq") or 0.0)
            dominates = dd <= literal_dd and gain >= literal_gain
            if dominates:
                best.append(key)
            evidence.append(
                f"{key}：回撤 {fmt(dd, 'pct')} vs 直译版 {fmt(literal_dd, 'pct')}；"
                f"终值 {gain:.4f}× vs {literal_gain:.4f}× ⇒ {'双优' if dominates else '未双优'}"
            )
        verdicts[f"J4_{leg}"] = {
            "verdict": (
                f"分级方案更优：{', '.join(best)}"
                if best
                else "分级方案未在回撤与终值上同时优于直译版"
            ),
            "evidence": evidence,
        }

    for leg in ("3y", "ext"):
        slow = by_key.get(f"g_slow_ema__{leg}")
        control = control_of(leg, arms, references)
        if not slow or control is None:
            continue
        triggers = int((slow["analysis"] or {}).get("hard_stop_triggers") or 0)
        same_gain = math.isclose(
            float((slow["analysis"] or {}).get("gain_strategy_eq") or 0.0),
            float((control.get("analysis") or {}).get("gain_strategy_eq") or 0.0),
            rel_tol=1e-9,
        )
        fires = triggers >= 3
        verdicts[f"J5_{leg}"] = {
            "verdict": (
                "触发速度不是瓶颈（慢 EMA 也触发了）"
                if fires
                else "慢 EMA 触发不足 ⇒ 触发速度是必要条件"
            ),
            "evidence": [
                f"{slow['key']}（EMA {slow['arm'].guard_params['ema_span_minutes']:.0f} 分钟）："
                f"触发 {triggers} 次，强平={bool((slow['analysis'] or {}).get('liquidated'))}，"
                f"终值 {fmt((slow['analysis'] or {}).get('gain_strategy_eq'), 'multiple', 4)}"
                + ("（与无守护控制臂逐位相同）" if same_gain else ""),
                f"控制臂 {control['key']}：终值 "
                f"{fmt((control.get('analysis') or {}).get('gain_strategy_eq'), 'multiple', 4)}",
            ],
        }
    return verdicts


def recommendations(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    verdicts: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    j1 = verdicts.get("J1", {})
    out.append(f"账户级守护的最终裁决：{j1.get('verdict', '未裁决')}。")
    for leg in ("3y", "ext"):
        j4 = verdicts.get(f"J4_{leg}")
        if j4:
            out.append(f"{leg} 腿“分级 vs 直译”：{j4['verdict']}。")
        j3 = verdicts.get(f"J3_{leg}")
        if j3:
            out.append(f"{leg} 腿“12H vs 24H”：{j3['verdict']}。")
    out.append(
        "机制结论：账户级守护要起作用，前提是**触发要快**（EMA 60 分钟级）并且**在加仓链条上先动手**"
        "（ORANGE/TpOnly 停加仓）；因为 TWE 3.0 满仓时到强平地板只有约 −31.8%——如果崩盘是跳空式的，"
        "任何账户级止损都来不及，真正决定生死的仍是暴露上界本身。"
    )
    out.append(
        "本次新增的一条硬教训：`no_restart_drawdown_threshold` 的锁存判定发生在**平仓确认那一刻**、"
        "用的是 `max(raw, EMA)`，所以急跌里“先到 20% 触发、确认时已经跌过 40%”会把带终局阈值的臂"
        "**在第一次触发就永久关停**——`g_ladder40__ext` 就是这样：活下来了，但也彻底退出了。"
        "要长期经营，终局阈值必须严格高于“一次急跌的确认回撤”，或者改成“累计已实现亏损”口径。"
    )
    out.append(
        "组合建议（按优先级）：① 先把 TWE 收到可控范围（本轮全部 arm 都在 TWE 3.0 这一档，属高风险实验）；"
        "② 若保留 TWE 3.0，至少加“−20% 停加仓 + 更深阈值清仓 + 永久地板”这套分级守护，"
        "并把永久地板设在“确认回撤”之上；"
        "③ 想要真正的“大难不死”，结构性去风险（`we_excess_allowance_pct=0`、下调 TWE）比任何熔断都可靠。"
    )
    out.append(
        "本轮的阶梯只是近似（引擎的冷却时长是单一常量）；引擎级阶梯、冷却后部分复入、与日线闸门联动见 "
        "`account_guard_design.md`，需要独立 PR。"
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
    summary["control_arms"] = [
        {
            "key": reference["key"],
            "label": reference["label"],
            "leg": reference["leg"],
            "run_dir": study.relative(reference["run_dir"]),
            "analysis_sha256": reference["analysis_sha256"],
            "pinned_sha256": reference["pinned_sha256"],
            "matches_pin": reference["analysis_sha256"] == reference["pinned_sha256"],
            "liquidated": bool((reference.get("analysis") or {}).get("liquidated")),
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
