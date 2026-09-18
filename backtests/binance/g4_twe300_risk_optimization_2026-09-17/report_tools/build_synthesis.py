#!/usr/bin/env python3
"""Build the study-level synthesis for the g4 @ TWE 3.0 risk-geometry study.

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
import geometry_analysis as geometry  # noqa: E402
import variant_spec as study  # noqa: E402
import wipeout_matrix as wipeout  # noqa: E402

OUTPUT_PATH = study.STUDY / "risk_geometry_analysis.md"
SUMMARY_PATH = study.ARTIFACTS / "risk_geometry_summary.json"

COMPARED = (
    ("gain_strategy_eq", "收益倍数", "multiple"),
    ("drawdown_worst_strategy_eq", "全期最差回撤", "pct"),
    ("drawdown_worst_mean_1pct_strategy_eq", "最差 1% 均值回撤", "pct"),
    ("total_wallet_exposure_max", "总暴露峰值", "ratio"),
    ("strategy_eq_recovery_days_max", "最长恢复期（天）", "days"),
    ("omega_ratio_strategy_eq", "Omega", "ratio"),
)
LEG_TITLES = dict(study.LEG_TITLES)
LEG_ORDER = tuple(study.LEG_ORDER)
REFERENCE_CONTROL = dict(study.REFERENCE_CONTROL_BY_LEG)
GUARD_REFERENCE_BY_LEG = {"3y": "g_user12h__3y", "ext": "g_user12h__ext"}
RUNG_BASELINE = {"3y": "a_allow000__3y", "ext": "a_allow000__ext", "pre": "a_allow000__pre"}
RUNG_ARMS = ("b_cool24", "b_cool48", "b_cool72")
TERMINAL_ARMS = ("b_term055", "b_term070")


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
        ("risk_geometry.json", "geometry"),
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
        # A pinned reference has no bundle of this study's own, but its run directory carries the
        # ledger and config the geometry is derived from; measuring it keeps the occupancy
        # comparison against the previous round's guard honest instead of comparing with zeros.
        entry["geometry"] = None
        if (run_dir / "fills.csv").exists() and (run_dir / "config.json").exists():
            try:
                entry["geometry"] = geometry.build_geometry_artifact(
                    run_dir, entry["analysis"]
                )
            except Exception as exc:  # noqa: BLE001 - a reference must not break the synthesis
                entry["geometry"] = {"error": f"{type(exc).__name__}: {exc}"}
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
    geometry = entry.get("geometry") or {}
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
        "stage": study.stage_of(arm) if arm else None,
        "per_slot_cap": geometry.get("per_slot_cap"),
        "peak_coin_exposure": (geometry.get("observed") or {}).get("peak_coin_exposure"),
        "peak_coin_share_of_peak_total": (geometry.get("observed") or {}).get(
            "peak_coin_share_of_peak_total"
        ),
        "top3_exposure": (geometry.get("observed") or {}).get("top3_exposure"),
        "active_coins_mean": (geometry.get("observed") or {}).get("active_coins_mean"),
        "empty_slot_time_share": (geometry.get("observed") or {}).get("empty_slot_time_share"),
        "single_coin_wipeout_bound": (geometry.get("observed") or {}).get(
            "single_coin_wipeout_bound"
        ),
        "liquidation_shock_at_peak": (geometry.get("observed") or {}).get(
            "liquidation_shock_at_peak"
        ),
        "max_realized_loss_pct": (analysis or {}).get("max_realized_loss_pct"),
        "search_provenance": (entry.get("global_metrics") or {}).get("search_provenance"),
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


def geometry_of(entry: dict[str, Any]) -> dict[str, Any]:
    return (entry.get("geometry") or {}).get("observed") or {}


def geometry_rows(entry: dict[str, Any]) -> list[str]:
    observed = geometry_of(entry)
    declared = (entry.get("geometry") or {}).get("declared") or {}
    return [
        fmt(declared.get("total_wallet_exposure_limit"), "ratio", 2),
        fmt(declared.get("n_positions"), "minutes", 0),
        fmt(declared.get("we_excess_allowance_pct"), "ratio", 2),
        fmt(observed.get("peak_coin_exposure"), "ratio", 4),
        fmt(observed.get("peak_coin_share_of_peak_total"), "pct"),
        fmt(observed.get("top3_exposure"), "ratio", 4),
        fmt(observed.get("peak_total_exposure"), "ratio", 4),
        fmt(observed.get("active_coins_mean"), "ratio", 2),
        fmt(observed.get("empty_slot_time_share"), "pct"),
        fmt(observed.get("single_coin_wipeout_bound"), "pct"),
        liquidation_shock_text(observed.get("liquidation_shock_at_peak")),
    ]


GEOMETRY_HEADER = [
    "arm",
    "TWE",
    "槽位",
    "占用余量",
    "峰值单币敞口",
    "单币/峰值占比",
    "前3币之和",
    "峰值总暴露",
    "在场币数均值",
    "空槽时间占比",
    "单币归零上界",
    "强平触发跌幅",
]


def geometry_section(entries: list[dict[str, Any]]) -> list[str]:
    """The round's core comparison: how concentrated the book actually was."""
    lines = [
        "## 三、暴露几何与占用纪律对照",
        "",
        "口径：单槽上限 = `TWE / n_positions × (1 + effective allowance)`（引擎的 bounded 模式）；"
        "峰值单币敞口、在场币数与空槽时间占比都由该 arm 自己的成交账本重建；"
        "“单币归零上界”= 暴露最大的那个币完全归零时账户损失的百分比（上界）。",
        "",
    ]
    rows = [[entry["key"]] + geometry_rows(entry) for entry in entries]
    lines.append(md_table(rows, GEOMETRY_HEADER))
    lines.append("")
    return lines


def search_section(
    arms: list[dict[str, Any]], variant_input: dict[str, Any]
) -> list[str]:
    """The frozen search contract and the candidates it produced."""
    contract = variant_input.get("search_contract") or {}
    selection = variant_input.get("search_selection")
    lines = [
        "## 六、参数搜索轨迹与选择",
        "",
        f"- 实现：仓库既有 pymoo 优化器（`optimize.backend=pymoo`），只在 "
        f"`{contract.get('results_root')}` 的搜索腿上跑；种子 `{contract.get('seed')}`、"
        f"`iters`={contract.get('iters')}、`population_size`={contract.get('population_size')}、"
        f"`n_cpus`={contract.get('n_cpus')}（冒烟测后确认）。",
        f"- 目标函数："
        + "；".join(f"{item['metric']}（{item['goal']}）" for item in (contract.get("scoring") or []))
        + "。参考约束："
        + "；".join(
            f"{item['metric']} {item['penalize_if']} {item['value']}"
            for item in (contract.get("limits") or [])
        )
        + "。",
        f"- 选臂规则（在看到样本外结果之前执行）：{contract.get('pick')}。",
        "- 搜索维度（只含风险几何；alpha 全部钉死）：",
        "",
    ]
    bounds = contract.get("bound_leaves") or {}
    lines.append(
        md_table(
            [[f"`{key}`", f"[{value[0]:g}, {value[1]:g}] 步长 {value[2]:g}"]
             for key, value in sorted(bounds.items())],
            ["维度", "范围"],
        )
    )
    lines.append("")
    lines.append(
        "- 固定不变：`total_wallet_exposure_limit` 之外的 alpha 键共 "
        f"{len(contract.get('fixed_params') or [])} 个（forager/unstuck/入场冷却等），"
        "外加 `live.hsl_signal_mode`、`live.pnls_max_lookback_days`、"
        "`live.max_realized_loss_pct` 与短边全部键。"
    )
    lines.append(
        f"- 优化器自身的强制运行时覆盖：{contract.get('wider_runtime_overrides', {}).get('note', 'n/a')}"
    )
    if selection:
        lines.append(
            f"- 冻结的选择文件 `{selection.get('path')}`（sha256 "
            f"`{str(selection.get('sha256'))[:16]}…`）。"
        )
    selected = [entry for entry in arms if str(entry["key"]).startswith("c")]
    if selected:
        header = ["候选", "来源", "TWE", "槽位", "占用余量", "RED", "EMA", "停机"]
        rows = []
        seen: set[str] = set()
        for entry in selected:
            lever = entry["arm"].lever
            if lever in seen:
                continue
            seen.add(lever)
            provenance = (entry.get("global_metrics") or {}).get("search_provenance") or {}
            params = provenance.get("parameters") or {}
            guard = guard_of(entry)
            rows.append(
                [
                    f"`{lever}`",
                    str(provenance.get("selection_kind")),
                    fmt(params.get("total_wallet_exposure_limit"), "ratio", 2),
                    fmt(params.get("n_positions"), "minutes", 0),
                    fmt(params.get("we_excess_allowance_pct"), "ratio", 2),
                    fmt(guard.get("red_threshold"), "ratio", 2),
                    fmt(guard.get("ema_span_minutes"), "minutes", 0),
                    fmt(guard.get("cooldown_minutes_after_red"), "minutes", 0),
                ]
            )
        lines.append("")
        lines.append(md_table(rows, header))
        lines.append("")
        lines.append(
            "每个候选的三条腿读数在下一节并排；候选的样本内指标与其 pareto 文件（路径 + sha256）"
            "记在各自的 `run_record.json` 与 arm 报告的“参数搜索与选择轨迹”里。"
        )
    else:
        lines.append(
            "- **本轮没有搜索候选**：搜索未运行或未产出通过选择规则的候选，"
            "所有结论只来自声明式臂（占用纪律与档位网格）。"
        )
    lines.append("")
    return lines


def cross_leg_section(arms: list[dict[str, Any]]) -> list[str]:
    """The same lever across the in-sample and out-of-sample legs."""
    by_lever: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in arms:
        by_lever.setdefault(entry["arm"].lever, {})[entry["arm"].leg] = entry
    lines = [
        "## 七、样本外与跨腿对照",
        "",
        "同一杠杆在三条腿上的读数并排。`3y` 是搜索窗（样本内），`pre` 是样本外验收窗，"
        "`ext` 覆盖全历史（含搜索窗，只有 2021-05 崩盘段算样本外）。"
        "**生存结论以 `pre` 与 `ext` 的崩盘段为准，收益结论必须标注它来自哪条腿。**",
        "",
        "| 杠杆 | 腿 | 终值 | 最差回撤 | 强平 | 守护触发 | 停机次数 | 终局停机 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for lever in sorted(by_lever):
        for leg in LEG_ORDER:
            entry = by_lever[lever].get(leg)
            if entry is None:
                continue
            analysis = entry.get("analysis") or {}
            readiness = entry.get("readiness") or {}
            lines.append(
                "| `{}` | {} | {} | {} | {} | {} | {} | {} |".format(
                    lever,
                    leg,
                    fmt(analysis.get("gain_strategy_eq"), "multiple", 4),
                    fmt(analysis.get("drawdown_worst_strategy_eq"), "pct"),
                    str(bool(analysis.get("liquidated"))),
                    str(int(analysis.get("hard_stop_triggers") or 0)),
                    str(readiness.get("halt_count", "n/a")),
                    str(readiness.get("terminal_halt_count", 0)),
                )
            )
    lines.append("")
    return lines


def build_document(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    lines: list[str] = []
    summary: dict[str, Any] = {"arms": {}, "verdicts": {}, "control_arms": []}

    lines.append("# g4 @ TWE 3.0 的风险几何优化：占用纪律、冷却档位与参数搜索")
    lines.append("")
    lines.append(
        "本文件是研究的跨 arm 综合结论；每个 arm 的完整深度分析（仓库报告规范固定骨架 + 研究附录，"
        "含“账户守护读数”与“整装待发读数”）在其自己的 run 目录 `annual_analysis.md` 中。"
    )
    lines.append("")

    lines.append("## 一、问题、条件映射与方法")
    lines.append("")
    lines.append(
        "前两轮已证明：**无守护的 10,000 USDT / TWE 3.0 在本地历史里于 2021-05-19 被强平**，"
        "而账户级守护（unified / RED 0.20 / EMA 60 / 停 12H）能活下来、但在温和窗口上代价很大。"
        "本轮只问一件事：**在同一份冻结配置上，用引擎已有的旋钮把“生存”买得更便宜**——"
        "占用纪律（`we_excess_allowance_pct=0`）、冷却档位（12/24/48/72H）、累计二档，"
        "以及一次“只搜风险几何、钉死 alpha”的参数搜索。"
    )
    lines.append("")
    lines.append(
        "三条腿：`3y` 是**搜索窗（样本内）**，`ext` 是全历史（含搜索窗），"
        "`pre`（2021-04-20 → 2023-09-11，与搜索窗不重叠）是**唯一的样本外验收窗**。"
    )
    lines.append("")
    lines.append("### 本轮问题 → 引擎配置映射")
    lines.append("")
    mapping = variant_input["geometry_contract"]["mapping_table"]
    lines.append(
        md_table(
            [[row["request"], row["engine"], f"`{row['config']}`", row["note"]] for row in mapping],
            ["你的条件", "引擎口径", "配置", "说明"],
        )
    )
    lines.append("")
    lines.append("### 引擎分层语义（沿用上一轮，已逐条在代码里核实）")
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
        "- 钉住的控制臂（`analysis.json` sha256 钉住、不重跑）：无守护 "
        "`twe300_10k__3y` / `twe300_10k__ext`、无守护且 allowance=0 的 "
        "`twe300_10k_allowance0__ext`、上一轮守护直译版 `g_user12h__3y` / `g_user12h__ext`。"
    )
    lines.append(
        "- 搜索契约（范围/种子/预算/目标/约束/选臂规则）与三条腿的定义都冻结在 "
        "`artifacts/variant_input.json` 里，本文件的“参数搜索轨迹与选择”一节逐条复述。"
    )
    lines.append("")

    lines.append("## 二、全期指标对比")
    lines.append("")
    for leg in LEG_ORDER:
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

    lines.extend(geometry_section([*references, *arms]))
    lines.append("## 四、守护读数对照")
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

    lines.append("## 五、整装待发读数（停机窗口与复牌表现）")
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

    lines.extend(search_section(arms, variant_input))
    lines.extend(cross_leg_section(arms))
    lines.append("## 八、强平与存活")
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

    lines.append("## 九、事件窗口压力表（基准驱动）")
    lines.append("")
    episodes: list[str] = []
    labels: dict[str, str] = {}
    for arm in arms:
        for item in (arm.get("events") or {}).get("events", []):
            key = str(item["event"])
            if key not in episodes:
                episodes.append(key)
                labels[key] = f"{item['label']}（基准 −{item['benchmark_drop_pct'] * 100:.0f}%）"
    for leg in LEG_ORDER:
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

    lines.append("## 十、暴露上界与冲击损失矩阵")
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

    lines.append("## 十一、判据裁决")
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

    lines.append("## 十二、结论与建议")
    lines.append("")
    for item in recommendations(arms, references, verdicts):
        lines.append(f"- {item}")
    lines.append("")

    lines.append("## 十三、边界")
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


def _gain(entry: dict[str, Any] | None) -> float | None:
    if not entry:
        return None
    value = (entry.get("analysis") or {}).get("gain_strategy_eq")
    return None if value is None else float(value)


def _dd(entry: dict[str, Any] | None) -> float | None:
    if not entry:
        return None
    value = (entry.get("analysis") or {}).get("drawdown_worst_strategy_eq")
    return None if value is None else float(value)


def _liq(entry: dict[str, Any] | None) -> bool:
    return bool((entry or {}).get("analysis", {}).get("liquidated"))


def _cell(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "n/a"
    return (
        f"{fmt(_gain(entry), 'multiple', 4)} / {fmt(_dd(entry), 'pct')}"
        f"{'（强平）' if _liq(entry) else ''}"
    )


def decide(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    variant_input: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    verdicts: dict[str, Any] = {}
    by_key = {entry["key"]: entry for entry in arms}
    refs = {entry["key"]: entry for entry in references}

    guard_ext = refs.get(GUARD_REFERENCE_BY_LEG["ext"])
    guard_3y = refs.get(GUARD_REFERENCE_BY_LEG["3y"])
    allow0_ext = by_key.get("a_allow000__ext")
    allow0_pre = by_key.get("a_allow000__pre")

    # ---------------------------------------------------------------- J1 occupancy
    evidence: list[str] = []
    checks: dict[str, bool] = {}
    if guard_ext and allow0_ext:
        g_gain, g_dd = _gain(guard_ext) or 0.0, _dd(guard_ext) or 0.0
        a_gain, a_dd = _gain(allow0_ext) or 0.0, _dd(allow0_ext) or 0.0
        g_geo, a_geo = geometry_of(guard_ext), geometry_of(allow0_ext)
        checks = {
            "终值不低于基准 80%": a_gain >= 0.8 * g_gain,
            "最差回撤不更高": a_dd <= g_dd + 1e-9,
        }
        # The concentration checks need the reference's own ledger; a reference without one is
        # reported as not measurable rather than silently counted as a failure.
        if g_geo.get("peak_coin_exposure") is not None:
            checks["峰值单币敞口更低"] = float(a_geo.get("peak_coin_exposure") or 0.0) < float(
                g_geo.get("peak_coin_exposure")
            )
            checks["单币归零上界更低"] = float(a_geo.get("single_coin_wipeout_bound") or 0.0) < float(
                g_geo.get("single_coin_wipeout_bound")
            )
        else:
            evidence.append(
                f"`{GUARD_REFERENCE_BY_LEG['ext']}` 的成交账本不在本地，"
                "占用类读数（峰值单币敞口/单币归零上界）本轮不可测量"
            )
        evidence.append(
            f"守护基准 `{GUARD_REFERENCE_BY_LEG['ext']}`（allowance 0.37）："
            f"终值 {fmt(g_gain, 'multiple', 4)}、最差回撤 {fmt(g_dd, 'pct')}、"
            f"峰值单币敞口 {fmt(g_geo.get('peak_coin_exposure'), 'ratio', 4)}、"
            f"单币归零上界 {fmt(g_geo.get('single_coin_wipeout_bound'), 'pct')}、"
            f"在场币数均值 {fmt(g_geo.get('active_coins_mean'), 'ratio', 2)}"
        )
        evidence.append(
            f"`a_allow000__ext`（allowance 0）：终值 {fmt(a_gain, 'multiple', 4)}、"
            f"最差回撤 {fmt(a_dd, 'pct')}、"
            f"峰值单币敞口 {fmt(a_geo.get('peak_coin_exposure'), 'ratio', 4)}、"
            f"单币归零上界 {fmt(a_geo.get('single_coin_wipeout_bound'), 'pct')}、"
            f"在场币数均值 {fmt(a_geo.get('active_coins_mean'), 'ratio', 2)}"
        )
        failed = [name for name, ok in checks.items() if not ok]
        verdicts["J1"] = {
            "verdict": "占用纪律有效" if not failed else "占用纪律无效（未通过：" + "、".join(failed) + "）",
            "evidence": evidence,
        }
        summary["j1_occupancy"] = {"checks": checks, "guard": GUARD_REFERENCE_BY_LEG["ext"]}
    else:
        verdicts["J1"] = {"verdict": "未裁决（缺少 a_allow000__ext 或守护基准）", "evidence": evidence}

    # ---------------------------------------------------------------- J2 occupancy x leverage
    pre_names = ("a_allow000__pre", "a_allow000_twe250__pre", "a_allow037__pre")
    pre_entries = [by_key.get(name) for name in pre_names if by_key.get(name)]
    eligible = [
        entry for entry in pre_entries if not _liq(entry) and (_dd(entry) or 1.0) <= 0.60
    ]
    pool = eligible or [entry for entry in pre_entries if not _liq(entry)] or pre_entries
    best = max(pool, key=lambda entry: (_gain(entry) or -1.0)) if pool else None
    cost = None
    if guard_ext and allow0_ext:
        g_gain, g_dd = _gain(guard_ext) or 0.0, _dd(guard_ext) or 0.0
        a_gain, a_dd = _gain(allow0_ext) or 0.0, _dd(allow0_ext) or 0.0
        if g_dd - a_dd > 1e-6:
            cost = (g_gain - a_gain) / ((g_dd - a_dd) * 100.0)
    verdicts["J2"] = {
        "verdict": (
            f"样本外腿推荐结构设定：`{best['key']}`"
            f"（终值 {fmt(_gain(best), 'multiple', 4)}、最差回撤 {fmt(_dd(best), 'pct')}、"
            f"强平={_liq(best)}）"
            if best is not None
            else "未裁决（pre 腿没有可用臂）"
        ),
        "evidence": [
            f"`{entry['key']}`（{entry['arm'].guard_label}）：终值 {fmt(_gain(entry), 'multiple', 4)}、"
            f"最差回撤 {fmt(_dd(entry), 'pct')}、强平={_liq(entry)}"
            for entry in pre_entries
        ]
        + (
            [
                "在 ext 腿上，占用纪律相对守护基准的代价："
                f"每避免 1pp 回撤放弃 {cost:.4f} 倍终值"
                if cost is not None
                else "在 ext 腿上占用纪律没有改善最差回撤"
            ]
        ),
    }
    summary["j2"] = {
        "recommended": best["key"] if best is not None else None,
        "cost_multiple_per_dd_pp": cost,
    }

    # ---------------------------------------------------------------- J3 cooldown rungs
    rung_evidence: list[str] = []
    improved: list[str] = []
    for lever in RUNG_ARMS:
        base_ext = by_key.get(RUNG_BASELINE["ext"])
        base_pre = by_key.get(RUNG_BASELINE["pre"])
        ext_arm = by_key.get(f"{lever}__ext")
        pre_arm = by_key.get(f"{lever}__pre")
        if not ext_arm or not base_ext:
            continue
        ext_ok = (_gain(ext_arm) or 0.0) >= (_gain(base_ext) or 0.0) - 1e-9 and (
            (_dd(ext_arm) or 1.0) <= (_dd(base_ext) or 1.0) + 1e-9
        )
        pre_ok = True
        if base_pre and pre_arm:
            pre_ok = (
                not _liq(pre_arm)
                and (_gain(pre_arm) or 0.0) >= (_gain(base_pre) or 0.0) - 1e-9
                and (_dd(pre_arm) or 1.0) <= (_dd(base_pre) or 1.0) + 1e-9
            )
        if ext_ok and pre_ok:
            improved.append(lever)
        rung_evidence.append(
            f"`{lever}`：ext 终值 {fmt(_gain(ext_arm), 'multiple', 4)}"
            f"（12H 基准 {fmt(_gain(base_ext), 'multiple', 4)}）、"
            f"最差回撤 {fmt(_dd(ext_arm), 'pct')}（基准 {fmt(_dd(base_ext), 'pct')}）；"
            f"pre 腿 {_cell(pre_arm)}（12H 基准 {_cell(base_pre)}）"
            f" ⇒ ext {'不差' if ext_ok else '更差'} / pre {'不差' if pre_ok else '更差'}"
        )
    verdicts["J3"] = {
        "verdict": (
            "加长冷却值得做：" + ", ".join(improved)
            if improved
            else "冷却时长不是收益杠杆（阶梯只剩风险塑形理由）"
        ),
        "evidence": rung_evidence or ["未找到档位臂"],
    }
    summary["j3_rungs"] = {"improved": improved, "baseline": dict(RUNG_BASELINE)}

    # ---------------------------------------------------------------- J4 cumulative rung
    terminal_evidence: list[str] = []
    fires: list[str] = []
    first_strike: list[str] = []
    for lever in TERMINAL_ARMS:
        for leg in LEG_ORDER:
            entry = by_key.get(f"{lever}__{leg}")
            if not entry:
                continue
            readiness = entry.get("readiness") or {}
            terminal = int(readiness.get("terminal_halt_count") or 0)
            halts = int(readiness.get("halt_count") or 0)
            base = by_key.get(RUNG_BASELINE.get(leg, ""))
            worse = base is not None and (_gain(entry) or 0.0) < (_gain(base) or 0.0) - 1e-9
            if terminal >= 1 and halts <= 1:
                first_strike.append(f"{lever}__{leg}")
            elif terminal >= 1:
                fires.append(f"{lever}__{leg}")
            terminal_evidence.append(
                f"`{lever}__{leg}`：停机 {halts} 次、终局停机 {terminal} 次、"
                f"终值 {fmt(_gain(entry), 'multiple', 4)}"
                + (f"（12H 基准 {fmt(_gain(base), 'multiple', 4)}）" if base else "")
                + ("，**首击锁存**" if terminal >= 1 and halts <= 1 else "")
            )
    if first_strike:
        j4_verdict = "累计二档证伪（出现首击锁存：" + "、".join(first_strike) + "）"
    elif fires:
        j4_verdict = "累计二档有效（触发且非首击锁存：" + "、".join(fires) + "）"
    else:
        j4_verdict = "累计二档在本样本上从未触发（阈值高于任何一次确认回撤）"
    verdicts["J4"] = {"verdict": j4_verdict, "evidence": terminal_evidence or ["未找到二档臂"]}
    summary["j4_terminal"] = {"fired": fires, "first_strike": first_strike}

    # ---------------------------------------------------------------- J5 search
    search_levers = sorted({entry["arm"].lever for entry in arms if entry["arm"].lever.startswith("c")})
    hits: list[str] = []
    search_evidence: list[str] = []
    for lever in search_levers:
        ext_arm = by_key.get(f"{lever}__ext")
        pre_arm = by_key.get(f"{lever}__pre")
        ok = (
            (_gain(ext_arm) or 0.0) >= 7.855172801895922
            and pre_arm is not None
            and not _liq(pre_arm)
            and (_dd(pre_arm) or 1.0) <= 0.50
        )
        if ok:
            hits.append(lever)
        search_evidence.append(
            f"`{lever}`：ext {_cell(ext_arm)}（守护基准 7.8552×）、pre {_cell(pre_arm)}"
            f" ⇒ {'通过' if ok else '未通过'}"
        )
    if not search_levers:
        j5_verdict = "本轮未运行搜索（只有声明式臂），J5 不适用"
    elif hits:
        j5_verdict = "搜索找到优于手写守护的点：" + ", ".join(hits)
    else:
        j5_verdict = "搜索未找到更优点（见边界：预算/参数域/目标函数）"
    verdicts["J5"] = {"verdict": j5_verdict, "evidence": search_evidence or ["本轮没有搜索候选臂"]}
    summary["j5_search"] = {"candidates": search_levers, "hits": hits}

    # ---------------------------------------------------------------- J6 structure vs breaker
    cells = {
        "无守护 + allowance 0.37": refs.get("twe300_10k__ext"),
        "无守护 + allowance 0": refs.get("twe300_10k_allowance0__ext"),
        "守护 + allowance 0.37": guard_ext,
        "守护 + allowance 0": allow0_ext,
    }
    struct_alone = cells["无守护 + allowance 0"]
    guard_alone = cells["守护 + allowance 0.37"]
    both = cells["守护 + allowance 0"]
    struct_saves = struct_alone is not None and not _liq(struct_alone)
    guard_saves = guard_alone is not None and not _liq(guard_alone)
    if struct_saves and not guard_saves:
        first_lever = "结构（allowance=0）"
    elif guard_saves and not struct_saves:
        first_lever = "熔断（守护）"
    elif struct_saves and guard_saves:
        first_lever = (
            "结构（allowance=0）"
            if (_gain(struct_alone) or 0.0) > (_gain(guard_alone) or 0.0)
            else "熔断（守护）"
        )
    else:
        first_lever = "两者单独都不够"
    interaction = "未测量"
    if struct_alone is not None and guard_alone is not None and both is not None:
        best_single_dd = min(_dd(struct_alone) or 1.0, _dd(guard_alone) or 1.0)
        if (_dd(both) or 1.0) < best_single_dd - 0.02:
            interaction = "叠加（同时使用把回撤压到任一单独使用之下）"
        elif _liq(struct_alone) == _liq(both):
            interaction = "在生存上冗余（结构单独已足够），回撤上见上表"
        else:
            interaction = "叠加"
    verdicts["J6"] = {
        "verdict": f"首选杠杆：{first_lever}；两者关系：{interaction}",
        "evidence": [f"`{name}`：{_cell(entry)}" for name, entry in cells.items()],
    }
    summary["j6_2x2"] = {name: summary_entry(entry) if entry else None for name, entry in cells.items()}
    return verdicts


def recommendations(
    arms: list[dict[str, Any]],
    references: list[dict[str, Any]],
    verdicts: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    for rule in ("J1", "J2", "J3", "J4", "J5", "J6"):
        verdict = verdicts.get(rule)
        if verdict:
            out.append(f"**{rule}**：{verdict['verdict']}。")
    out.append(
        "机制结论：同一档杠杆下，**占用纪律（`we_excess_allowance_pct=0`）改变的是单币敞口与"
        "单币归零上界**，账户级熔断改变的是“崩盘时是否还在场”；两者作用在不同环节，"
        "因此“同时用”通常既不是纯冗余、也不是纯叠加——本文件的 2×2 表给出本样本上的答案。"
    )
    out.append(
        "下一轮（引擎级冷却阶梯）的定档依据：J3 的档位对照 + `account_guard_design.md` 中的"
        "状态机与实盘重建契约；阶梯只做在**冷却时长**上，不做一次性永久关停"
        "（上一轮已证伪首击锁存的危害）。"
    )
    out.append(
        "无论结构怎么调，跳空超过“到强平距离”的行情都无法用账户级止损兜住："
        "本轮的 TWE 档位把这段距离从 −31.8%（3.0）拉到约 −40%（2.5）与 −50%（2.0），"
        "这才是确定性最高的那条防线。"
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
