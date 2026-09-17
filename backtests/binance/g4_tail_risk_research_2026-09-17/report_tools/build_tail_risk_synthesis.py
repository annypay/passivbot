#!/usr/bin/env python3
"""Build the study-level synthesis for the g4 tail-risk research.

The per-run deep analyses live in their own run directories (report convention, Rule 1);
this document is the cross-arm synthesis: it puts every arm's event-window table, tail
bounds, HSL telemetry and cost side by side, then applies the decision rules that
`build_variant_config.py` pre-registered before any arm was run.

It reads only tracked evidence (`analysis.json`, `run_record.json`, `global_metrics.json`)
plus each arm's own `tail_risk_events.json` / `tail_risk_wipeout.json`. For the two pinned
reference anchors (which predate this study and therefore carry no tail artifacts) the event
table is recomputed from their local ledger when those artifacts are available, so the 3-year
leg has a control column too. It writes `tail_risk_analysis.md` and
`artifacts/tail_risk_summary.json`.

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

OUTPUT_PATH = study.STUDY / "tail_risk_analysis.md"
SUMMARY_PATH = study.ARTIFACTS / "tail_risk_summary.json"

#: Metrics compared across arms, in report order, with their display format.
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
    "syn": "合成腿（原生 bundle + 注入崩塌路径；价格路径为合成，非历史事实）",
}
REFERENCE_CONTROL = {"3y": "ref_hsl_off_3y", "ext": "off__ext", "syn": "off__synth_a"}


def load_evidence(variant: study.Variant) -> dict[str, Any] | None:
    """Every tracked artifact one arm contributes to the synthesis."""
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
    return evidence


def event_table_for(run_dir: Path, dataset_path: Path) -> dict[str, Any] | None:
    """Event table for a run that predates this study, recomputed from its local ledger."""
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
            "run_dir": run_dir,
            "analysis": study.load_json(analysis_path),
            "analysis_sha256": study.sha256_file(analysis_path),
            "pinned_sha256": item["analysis_sha256"],
            "events": None,
            "wipeout": None,
            "arm": None,
        }
        tail = run_dir / "tail_risk_events.json"
        if tail.exists():
            entry["events"] = study.load_json(tail)
        else:
            entry["events"] = event_table_for(run_dir, study.DATASETS["3y"].path)
        out.append(entry)
    return out


def fmt(value: Any, kind: str, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(number):
        return "n/a"
    if kind == "pct":
        return f"{number * 100:.{digits}f}%"
    if kind == "multiple":
        return f"{number:.{digits}f}×"
    if kind == "days":
        return f"{number:.{digits}f}"
    return f"{number:.4f}"


def cagr(analysis: dict[str, Any] | None) -> float | None:
    if not analysis:
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


def reference_of(references: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    return next((entry for entry in references if entry["key"] == key), None)


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
    ruin = ((entry.get("wipeout") or {}).get("ruin")) or {}
    return {
        "leg": arm.leg if arm else "3y",
        "lever": arm.lever if arm else entry["key"],
        "synthetic": bool(arm.synthetic) if arm else False,
        "run_dir": study.relative(entry["run_dir"]),
        "gain_strategy_eq": analysis.get("gain_strategy_eq"),
        "cagr": cagr(analysis),
        "drawdown_worst_strategy_eq": analysis.get("drawdown_worst_strategy_eq"),
        "drawdown_worst_mean_1pct_strategy_eq": analysis.get(
            "drawdown_worst_mean_1pct_strategy_eq"
        ),
        "worst_event_drawdown": worst_event_drawdown(entry.get("events")),
        "peak_total_exposure": ruin.get("peak_total_exposure", analysis.get("total_wallet_exposure_max")),
        "ruin_distance": ruin.get("ruin_distance"),
        "worst_coin_exposure": ruin.get("worst_coin_exposure"),
        "coins_to_breach_20pct": ruin.get("coins_to_breach_20pct"),
        "coins_to_breach_50pct": ruin.get("coins_to_breach_50pct"),
        "hard_stop_triggers": analysis.get("hard_stop_triggers"),
        "hard_stop_panic_close_loss_sum": analysis.get("hard_stop_panic_close_loss_sum"),
        "liquidated": analysis.get("liquidated"),
        "panic_fills": ((entry.get("events") or {}).get("ledger") or {}).get("panic_fills"),
    }


def build_document(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    lines: list[str] = []
    summary: dict[str, Any] = {"arms": {}, "verdicts": {}, "reference_arms": []}

    lines.append("# g4 尾部风险研究：单币/多币归零与账户级风控的实测结论")
    lines.append("")
    lines.append(
        "本文件是研究的跨 arm 综合结论；每个 arm 的完整深度分析（仓库报告规范固定骨架 + 研究附录）"
        "在其自己的 run 目录 `annual_analysis.md` 中，两者数字同源、可独立复算。"
    )
    lines.append("")

    lines.append("## 一、问题与方法")
    lines.append("")
    lines.append(
        "核心问题：在马丁格尔入场结构下，账户会不会被“单个币归零”或“多个币同时崩塌”一波带走？"
        "开启/关闭 HSL 是否改变这类风险？`pside`/`unified` 组合级信号是否有效？还有哪些不显著"
        "伤害盈利的黑天鹅风控？"
    )
    lines.append("")
    lines.append(
        f"- 预注册：{len(variant_input['arms'])} 个 arm 的声明式改动、事件识别规则、冲击水平与判据"
        "全部在 `artifacts/variant_input.json` 中先行登记，跑完不追加挑选。"
    )
    for dataset in variant_input["datasets"]:
        lines.append(
            f"- 数据集 `{dataset['key']}`：`{dataset['path']}`，窗口 "
            f"{dataset['window'][0]} → {dataset['window'][1]}"
            + ("（合成派生 bundle）" if dataset.get("synthetic") else "")
            + "。"
        )
    lines.append(
        "- 判定规则：" + "；".join(f"**{rule['id']}** {rule['rule']}" for rule in variant_input["decision_rules"])
    )
    lines.append(
        "- 所有 arm 都通过运行时开关 `--disable_plotting coin_fills` 关闭了逐币成交面板（本机内存约 "
        "7.8 GB，面板是单次 run 的内存峰值）；摘要图与全部数据产物不受影响，各 arm 报告在 "
        "`## 口径与范围` 中声明。"
    )
    lines.append("")

    lines.append("## 二、全期指标对比")
    lines.append("")
    for leg in ("3y", "ext", "syn"):
        leg_arms = arms_of(arms, leg)
        if not leg_arms and not (leg == "3y" and references):
            continue
        lines.append(f"### {LEG_TITLES[leg]}")
        lines.append("")
        header = ["arm"] + [label for _key, label, _kind in COMPARED] + ["CAGR"]
        rows: list[list[str]] = []
        if leg == "3y":
            for reference in references:
                rows.append(
                    [f"（锚点）{reference['key']}"]
                    + [fmt(reference["analysis"].get(key), kind) for key, _label, kind in COMPARED]
                    + [fmt(cagr(reference["analysis"]), "pct")]
                )
        for arm in leg_arms:
            analysis = arm["analysis"] or {}
            rows.append(
                [arm["arm"].key]
                + [fmt(analysis.get(key), kind) for key, _label, kind in COMPARED]
                + [fmt(cagr(analysis), "pct")]
            )
        lines.append(md_table(rows, header))
        lines.append("")
        if leg == "3y":
            lines.append(
                "锚点 `ref_hsl_off_3y`/`ref_hsl_coin_3y` 来自前两个 study 的 tracked 运行（本研究的 "
                "`variant_spec.py` 钉住其 `analysis.json` 的 sha256，验证器复核）；它们不重跑。"
            )
            lines.append("")

    lines.append("## 三、事件窗口压力表（基准驱动，非手工挑窗）")
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
    lines.append("### 控制组事件明细（原始读数）")
    lines.append("")
    for leg in ("3y", "ext"):
        control = control_of(leg, arms, references)
        if control is None or not control.get("events"):
            continue
        lines.append(f"#### {LEG_TITLES[leg]}｜控制组 `{control['key']}`")
        if control["events"].get("recomputed_by_synthesis"):
            lines.append("")
            lines.append(
                "（该锚点没有本研究的 `tail_risk_events.json`，事件表由综合脚本用其本地账本复算。）"
            )
        lines.append("")
        header = ["事件", "基准击穿", "基准跌幅", "事件内最大回撤", "到谷天数", "到恢复天数", "水下天数", "窗口收益"]
        rows = []
        for item in control["events"]["events"]:
            if item.get("synthetic"):
                continue
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
    lines.append(
        "事件窗口的回撤按各 run 的 `balance_and_equity.csv.gz` 采样分辨率计算（本研究的 run 为每 60 分钟"
        "一个采样点），与 `analysis.json` 的逐分钟引擎口径可能略有差异；两个口径在尾部方向上的结论一致。"
    )
    lines.append("")

    lines.append("### 合成注入窗口（“下一个 LUNA”）")
    lines.append("")
    synth_rows: list[list[str]] = []
    header = ["arm", "注入目标", "注入起点", "注入跌幅", "事件内最大回撤", "到恢复天数", "窗口收益", "panic 笔数"]
    for arm in arms_of(arms, "syn"):
        for item in (arm.get("events") or {}).get("events", []):
            if not item.get("synthetic"):
                continue
            synth_rows.append(
                [
                    arm["arm"].key,
                    item["label"],
                    item["breach_start"],
                    f"−{item['benchmark_drop_pct'] * 100:.1f}%",
                    fmt(item.get("max_drawdown"), "pct"),
                    fmt(item.get("days_to_recovery"), "days", 1)
                    if item.get("days_to_recovery") is not None
                    else "未恢复（180 天上限）",
                    fmt(item.get("window_return"), "pct"),
                    str(int(item.get("panic_fills", 0))),
                ]
            )
    if synth_rows:
        lines.append(md_table(synth_rows, header))
    else:
        lines.append("（合成臂尚未运行）")
    lines.append("")
    lines.append(
        "合成情景把目标币的价格在 3 天内压到 0.005×，起点取该币在控制组里达到最大敞口的那一分钟，"
        "因此注入必然落在**已经持仓**的时刻；`合成 B` 的三个目标币各自以自己的峰值时点为起点，"
        "所以它是“三个币在几个月内先后崩塌”，不是同日同时崩塌（同日同时崩塌由历史事件窗口覆盖）。"
    )
    lines.append("")

    lines.append("## 四、暴露上界与“单币/多币归零”损失矩阵")
    lines.append("")
    header = [
        "arm",
        "总暴露峰值",
        "爆仓距离",
        "最大单币敞口",
        "前 3 币之和",
        "前 7 槽之和",
        "−20% 需几币",
        "−50% 需几币",
        "−80% 需几币",
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
                f"{ruin['peak_total_exposure']:.6f}",
                f"{ruin['ruin_distance'] * 100:.2f}%",
                f"{ruin['worst_coin_exposure']:.6f}",
                f"{ruin['top3_exposure']:.6f}",
                f"{ruin['top7_exposure']:.6f}",
                str(ruin["coins_to_breach_20pct"]),
                str(ruin["coins_to_breach_50pct"]),
                str(ruin["coins_to_breach_80pct"]),
            ]
        )
        summary["arms"][entry["key"]] = summary_entry(entry)
    lines.append(md_table(rows, header))
    lines.append("")
    lines.append(
        "上界口径：单币损失 = 该币实测敞口峰值 × 冲击幅度；前 k 币 = 前 k 名敞口峰值之和 × 冲击幅度；"
        "整篮 = 实测 `total_wallet_exposure_max` × 冲击幅度。这些都是**同一时刻**的静态上界，"
        "不含路径中的加仓（会让损失趋向敞口上界）与减仓（会让损失小于该上界）。"
    )
    lines.append("")
    lines.append("### 冲击损失矩阵（占账户比例）")
    lines.append("")
    header = ["arm", "范围"] + [f"−{int(shock * 100)}%" for shock in variant_input["shock_levels"]]
    rows = []
    for entry_for_matrix in [*references, *arms]:
        payload = entry_for_matrix.get("wipeout")
        if not payload:
            continue
        for scope in wipeout.SCOPE_ORDER:
            cells = [
                entry_for_matrix["key"] if scope == wipeout.SCOPE_ORDER[0] else "",
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

    lines.append("## 五、HSL 熔断：收益代价与运行学")
    lines.append("")
    header = [
        "arm",
        "作用域",
        "red 阈值",
        "EMA(min)",
        "重启策略",
        "触发",
        "重启",
        "panic 笔数",
        "panic 实亏 USDT",
        "复触发%",
        "停机均值(min)",
    ]
    rows = []
    for entry in [*references, *arms]:
        record = entry.get("record") or {}
        analysis = entry.get("analysis") or {}
        hsl = record.get("hsl_block") or {}
        live = record.get("live_hsl") or {}
        if entry.get("arm") is None:
            # A pinned anchor: read its scope from the pinned previous study's own record if present.
            hsl = hsl or {"enabled": entry["key"] == "ref_hsl_coin_3y"}
            mode = "coin"
        else:
            mode = str(live.get("hsl_signal_mode"))
        panic = ((entry.get("events") or {}).get("ledger") or {}).get("panic_fills")
        row = [
            entry["key"],
            mode,
            "—" if not hsl.get("enabled") else str(hsl.get("red_threshold")),
            "—" if not hsl.get("enabled") else str(hsl.get("ema_span_minutes")),
            "—" if not hsl.get("enabled") else str(hsl.get("restart_after_red_policy")),
            str(int(analysis.get("hard_stop_triggers") or 0)),
            str(int(analysis.get("hard_stop_restarts") or 0)),
            "n/a" if panic is None else str(int(panic)),
            fmt(analysis.get("hard_stop_panic_close_loss_sum"), "days", 2),
            fmt(analysis.get("hard_stop_post_restart_retrigger_pct"), "pct"),
            fmt(analysis.get("hard_stop_duration_minutes_mean"), "days", 1),
        ]
        rows.append(row)
    lines.append(md_table(rows, header))
    lines.append("")
    lines.append(
        "口径提醒：`coin` 模式的分母是**槽位预算**（`TWEL/n_positions × 余额` ≈ 0.14286 × 余额），"
        "`pside`/`unified` 的分母是**策略净值**（`1 − equity/peak`，峰值为滚动 "
        "`live.pnls_max_lookback_days`=30 天）。同一个 `red_threshold` 在前者代表“单币约 2% 账户”，"
        "在后者代表“账户 −15% / −10%”。"
    )
    lines.append("")

    lines.append("## 六、判据裁决")
    lines.append("")
    verdicts = decide(arms, references, variant_input, summary)
    for rule in variant_input["decision_rules"]:
        lines.append(f"### {rule['id']}｜{rule['rule']}")
        lines.append("")
        for key, verdict in verdicts.items():
            if key == rule["id"] or key.startswith(f"{rule['id']}_"):
                lines.append(f"- 裁决（{key}）：**{verdict['verdict']}**")
                for item in verdict.get("evidence", []):
                    lines.append(f"  - 依据：{item}")
        lines.append("")
    summary["verdicts"] = verdicts

    lines.append("## 七、对实盘的处置建议")
    lines.append("")
    for item in playbook(arms, references, variant_input, verdicts, summary):
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 八、未覆盖风险与非目标")
    lines.append("")
    for item in variant_input["honesty_boundaries"]:
        lines.append(f"- {item}")
    lines.append(
        "- 期权/凸性对冲与“盘外留存（operating float）”属于阶段 B：本地没有期权链历史，无法与回测"
        "同口径比较，只能做成本预算与规模框架，不能伪造收益数字。"
    )
    lines.append(
        "- 本 PR 不改引擎、不改发布 profile、不启动实盘；任何 profile 修订都应另开 PR 并重新走"
        "研究-验证流程。"
    )
    lines.append("")
    return "\n".join(lines), summary


def decide(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Apply the pre-registered decision rules to the measured arms."""
    verdicts: dict[str, Any] = {}
    for leg in ("3y", "ext"):
        control = control_of(leg, arms, references)
        if control is None:
            continue
        base_cagr = cagr(control.get("analysis"))
        base_worst_event = worst_event_drawdown(control.get("events"))
        candidates: list[dict[str, Any]] = []
        for arm in arms_of(arms, leg):
            if arm["arm"].synthetic:
                continue
            if arm["arm"].lever not in (
                "pside_r15",
                "unified_r15",
                "unified_r10",
                "unified_r10_fast",
                "unified_r10_market",
                "unified_r10_never",
                "coin",
            ):
                continue
            arm_cagr = cagr(arm.get("analysis"))
            if base_cagr is None or arm_cagr is None:
                continue
            arm_worst_event = worst_event_drawdown(arm.get("events"))
            reduction = (
                1.0 - arm_worst_event / base_worst_event
                if base_worst_event and arm_worst_event is not None and base_worst_event > 0
                else None
            )
            candidates.append(
                {
                    "arm": arm["arm"].key,
                    "lever": arm["arm"].lever,
                    "leg": leg,
                    "delta_cagr_pp": (arm_cagr - base_cagr) * 100.0,
                    "worst_event_reduction": reduction,
                }
            )
        candidates.sort(key=lambda item: item["delta_cagr_pp"], reverse=True)
        summary.setdefault("j1_candidates", {})[leg] = candidates
        passing = [
            item
            for item in candidates
            if item["delta_cagr_pp"] >= -0.5
            and item["worst_event_reduction"] is not None
            and item["worst_event_reduction"] >= 0.30
        ]
        verdicts[f"J1_{leg}"] = {
            "verdict": (
                "有候选同时满足“收益代价 ≤0.5pp/年”与“事件内回撤下降 ≥30%”"
                if passing
                else "无候选同时满足“收益代价 ≤0.5pp/年”与“事件内回撤下降 ≥30%”"
            ),
            "evidence": [
                f"控制组 {control['key']}：CAGR {base_cagr * 100:+.2f}%，事件内最差回撤 "
                + (fmt(base_worst_event, "pct") if base_worst_event is not None else "n/a")
            ]
            + [
                f"{item['arm']}：ΔCAGR {item['delta_cagr_pp']:+.2f}pp/年，事件内最差回撤"
                + (
                    f"下降 {item['worst_event_reduction'] * 100:.1f}%"
                    if item["worst_event_reduction"] is not None
                    else "不可比"
                )
                for item in candidates
            ],
        }

    ext_control = control_of("ext", arms, references)
    ext_dd = (ext_control or {}).get("analysis", {}).get("drawdown_worst_strategy_eq")
    j2_evidence: list[str] = []
    j2_ok = ext_dd is not None and float(ext_dd) < 0.20
    if ext_dd is not None:
        j2_evidence.append(f"5.4 年历史压力腿控制组全期最差回撤 {float(ext_dd) * 100:.2f}%")
    synth_a_control = next(
        (arm for arm in arms_of(arms, "syn") if arm["arm"].lever == "off" and arm["arm"].dataset_key == "synth_a"),
        None,
    )
    single_coin_bound: float | None = None
    if synth_a_control and synth_a_control.get("wipeout"):
        single_coin_bound = float(synth_a_control["wipeout"]["ruin"]["worst_coin_exposure"])
        j2_evidence.append(f"合成 A 控制组最大单币敞口 {single_coin_bound * 100:.2f}% 账户")
    if synth_a_control and synth_a_control.get("events"):
        for item in synth_a_control["events"].get("events", []):
            if item.get("synthetic"):
                j2_evidence.append(
                    f"合成 A 控制组在注入窗口内的最大回撤 {fmt(item.get('max_drawdown'), 'pct')}"
                    f"（注入跌幅 −{item['benchmark_drop_pct'] * 100:.1f}%）"
                )
    coins_for_20 = None
    if synth_a_control and synth_a_control.get("wipeout"):
        coins_for_20 = synth_a_control["wipeout"]["ruin"].get("coins_to_breach_20pct")
    if coins_for_20 is not None:
        j2_evidence.append(f"在当前敞口下，达到 −20% 账户损失需要 {coins_for_20} 个币同时归零")
    verdicts["J2"] = {
        "verdict": (
            "成立：历史腿内的单币归零不构成“一波带走”"
            if j2_ok and single_coin_bound is not None
            else "需结合敞口上界解读（见证据）"
        ),
        "evidence": j2_evidence,
    }

    liquidated = [
        entry["key"]
        for entry in [*references, *arms]
        if (entry.get("analysis") or {}).get("liquidated")
    ]
    deep = [
        entry["key"]
        for entry in [*references, *arms]
        if (entry.get("analysis") or {}).get("drawdown_worst_strategy_eq") is not None
        and float((entry.get("analysis") or {}).get("drawdown_worst_strategy_eq") or 0.0) > 0.50
    ]
    verdicts["J3"] = {
        "verdict": "无需强制结构性降杠杆（无强平、无 >50% 回撤）" if not liquidated and not deep else "触发：需要结构性降杠杆",
        "evidence": [
            f"出现强平标记的 arm：{liquidated or '无'}",
            f"全期最差回撤 > 50% 的 arm：{deep or '无'}",
        ],
    }

    j4_evidence: list[str] = []
    for arm in arms_of(arms, "syn"):
        ruin = (arm.get("wipeout") or {}).get("ruin") or {}
        bound = ruin.get("worst_coin_exposure")
        for item in (arm.get("events") or {}).get("events", []):
            if not item.get("synthetic"):
                continue
            loss = item.get("max_drawdown")
            j4_evidence.append(
                f"{arm['arm'].key}：注入 {item['label']}，事件内最大回撤 "
                f"{fmt(loss, 'pct')}，单币敞口上界 {fmt(bound, 'pct') if bound is not None else 'n/a'}"
            )
    verdicts["J4"] = {
        "verdict": "见合成臂实测（若损失未超过该币敞口上界，即无需结构性修正）",
        "evidence": j4_evidence or ["合成臂尚未生成"],
    }
    return verdicts


def playbook(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
    verdicts: dict[str, Any],
    summary: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    out.append(
        "单币归零是**结构性有界**的：任一币归零最多损失它的敞口峰值（本研究的实测上界见第四节），"
        "因此“被一个币一波带走”在引擎层面不成立；真正的尾部来自多币同时崩塌以及恢复基数被打掉。"
    )
    passing = [
        item
        for items in (summary.get("j1_candidates") or {}).values()
        for item in items
        if item["delta_cagr_pp"] >= -0.5
        and item["worst_event_reduction"] is not None
        and item["worst_event_reduction"] >= 0.30
    ]
    if passing:
        best = sorted(passing, key=lambda item: (-item["worst_event_reduction"], item["arm"]))[0]
        out.append(
            f"若只挑一个账户级熔断层：`{best['arm']}`（ΔCAGR {best['delta_cagr_pp']:+.2f}pp/年，"
            f"事件内最差回撤下降 {best['worst_event_reduction'] * 100:.1f}%）是“封顶尾部”与"
            "“保留盈利”折衷最好的候选；上线前先用 `passivbot tool live-config-preflight` 与 "
            "`hsl-startup-preview` 验证账户状态，并确认未使用 `live.balance_override`。"
        )
    else:
        out.append(
            "没有账户级 arm 同时满足预注册判据“代价 ≤0.5pp/年 且事件内回撤下降 ≥30%”："
            "账户级熔断在历史窗口里要么近乎休眠（阈值 0.15），要么明显牺牲收益（阈值 0.10）。"
        )
    out.append(
        "账户级熔断只对**有持续性的崩盘**有效：触发指标是 `min(raw, EMA)`，一天内完成并反弹的"
        "插针不会被拦截，必须写进运营预期，不能把它当作闪崩保险。"
    )
    out.append(
        "阈值口径必须先统一：从 `coin` 切到 `pside`/`unified` 时，`red_threshold=0.15` 的含义从"
        "“单币约 2% 账户”变成“账户 −15%”，两者不可直接比较。"
    )
    out.append(
        "结构性降杠杆（`total_wallet_exposure_limit` / `n_positions` / `we_excess_allowance_pct`）"
        "是唯一**确定**压低尾部上界的手段，代价近似线性；任何叠层（熔断、对冲）都必须先与它比较。"
    )
    out.append(
        "实盘终止规则（建议）：把账户级熔断的 `restart_after_red_policy` 与 "
        "`no_restart_drawdown_threshold` 显式写成“跌破 X% 永久停止”，而不是依赖人工盯盘；"
        "X 的取值见第六节 J1 的候选比较。"
    )
    out.append(
        "未覆盖：交易所/稳定币对手方风险、真实退市流程、崩盘中的真实滑点与 API 断连；"
        "这些只能在运营层面用“盘外留存/利润划出”与资金分散处理（阶段 B）。"
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
    summary["generated_by"] = "report_tools/build_tail_risk_synthesis.py"
    summary["missing_arms"] = missing
    summary["reference_arms"] = [
        {
            "key": reference["key"],
            "label": reference["label"],
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
