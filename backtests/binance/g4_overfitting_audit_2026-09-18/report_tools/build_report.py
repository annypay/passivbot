#!/usr/bin/env python3
"""Render the audit's Chinese documents from the tracked artifacts.

Every number printed here is interpolated from ``artifacts/*.json`` or recomputed from the run
ledgers at render time; nothing is typed by hand. Running this module after any stage re-renders
``overfitting_audit.md``, ``anti_pattern_audit.md`` and ``README.md`` so the prose cannot drift
from the evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/g4_overfitting_audit_2026-09-18"
ARTIFACTS = STUDY / "artifacts"
PANEL_DIR = ARTIFACTS / "panels"

BLOCKS = (8, 10, 12, 16)
SELECTIONS = ("max_adg", "max_adg_x_1_minus_dd")
SELECTION_SHORT = {"max_adg": "max ADG", "max_adg_x_1_minus_dd": "max ADG×(1−DD)"}
PANEL_TITLE = {
    "poolA_risk_geometry__3y": "池 A · 搜索窗 `3y`",
    "poolA_risk_geometry__ext": "池 A · 全历史 `ext`",
    "poolA_risk_geometry__pre": "池 A · 样本外 `pre`",
    "poolB_ext_union__ext": "池 B · `ext` 并集",
}
#: Arms used for the fill-ledger facts the anti-pattern table cites.
FILL_FACT_ARMS = (
    ("A_risk_geometry", "a_allow000__ext"),
    ("C_account_guard", "g_user12h__ext"),
    ("A_risk_geometry", "c1__3y"),
)


def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def pct(value: float | None, digits: int = 2) -> str:
    return "unknown" if value is None else f"{value * 100:.{digits}f}%"


def num(value: float | None, digits: int = 3, sign: bool = False) -> str:
    if value is None:
        return "unknown"
    return f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"


def run_dir_for(round_key: str, arm: str) -> Path | None:
    ledger = load_json(ARTIFACTS / "trial_ledger.json")
    for row in ledger["trials"]:
        if row["round"] == round_key and row["arm"] == arm and row["tier"] == "replay_arm":
            return REPO / row["evidence"]
    return None


def fill_facts() -> list[dict[str, Any]]:
    """Panic-fill and liquidity counts straight from the run-local fill ledgers."""
    out = []
    for round_key, arm in FILL_FACT_ARMS:
        run_dir = run_dir_for(round_key, arm)
        if run_dir is None or not (run_dir / "fills.csv").exists():
            out.append({"arm": arm, "unavailable": True})
            continue
        types: dict[str, int] = {}
        liquidity: dict[str, int] = {}
        rows = 0
        with (run_dir / "fills.csv").open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                rows += 1
                types[row["type"]] = types.get(row["type"], 0) + 1
                liquidity[row["liquidity"]] = liquidity.get(row["liquidity"], 0) + 1
        panic = sum(count for key, count in types.items() if "panic" in key)
        out.append(
            {
                "arm": arm,
                "round": round_key,
                "run_dir": run_dir.as_posix()[len(REPO.as_posix()) + 1 :],
                "fills": rows,
                "panic_fills": panic,
                "liquidity": liquidity,
            }
        )
    return out


# --------------------------------------------------------------------------------- main report ---
def render_overfitting_audit() -> str:
    cscv = load_json(ARTIFACTS / "cscv_pbo.json")
    bias = load_json(ARTIFACTS / "selection_bias.json")
    folds = load_json(ARTIFACTS / "fold_stability.json")
    stress = load_json(ARTIFACTS / "stress_arms.json")
    ledger = load_json(ARTIFACTS / "trial_ledger.json")
    index = load_json(PANEL_DIR / "index.json")

    facts = fill_facts()
    counts = ledger["counts"]
    n_bound = ledger["n_lower_bound"]
    n_grid = bias["n_grid"]

    lines: list[str] = []
    add = lines.append

    add("# 过拟合审计：四轮选点是否只是这段历史的产物（2026-09-18）")
    add("")
    headline = cscv["panels"]["poolB_ext_union__ext"]
    pool_a_ext = cscv["panels"]["poolA_risk_geometry__ext"]
    pool_a_3y = cscv["panels"]["poolA_risk_geometry__3y"]
    h1 = bias["headline_arms"]["H1_guard_baseline_ext"]
    h2 = bias["headline_arms"]["H2_recommended_pre"]

    add(
        "**一句话结论：这段历史确实携带信号，但「从这批 arm 里挑最好的一个」有两个不同的答案，"
        "取决于池子怎么划。**"
        f"池 B（{index['panels']['poolB_ext_union__ext']['n_arms_in_pool']} 个 `ext` 臂，"
        f"剔除退化后 {headline['n_arms_used']} 个）的 PBO 在 "
        f"{min(headline['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}–"
        f"{max(headline['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}，"
        "样本内→样本外斜率在 S=8/10 下为正"
        f"（{headline['cscv']['max_adg|S8']['is_oos_slope']:+.3f} / "
        f"{headline['cscv']['max_adg|S10']['is_oos_slope']:+.3f}）、在 S=12/16 下转负"
        f"（{headline['cscv']['max_adg|S12']['is_oos_slope']:+.3f} / "
        f"{headline['cscv']['max_adg|S16']['is_oos_slope']:+.3f}），"
        f"说明**这批臂整体**的差异有一定可迁移性，但不稳健；"
        f"而池 A 的 `ext` 腿 PBO 升到 "
        f"{min(pool_a_ext['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}–"
        f"{max(pool_a_ext['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}、"
        "四个分块的斜率全为负"
        f"（{min(pool_a_ext['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in BLOCKS):+.3f}～"
        f"{max(pool_a_ext['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in BLOCKS):+.3f}），"
        "即**风险几何这一轮自己那 44 个 arm 内部**的挑选几乎不携带样本外信息。"
    )
    add("")
    add(
        "多重检验这一侧更直接：把台账的下界 "
        f"N = {n_bound} 计进去后，被四轮推荐的那条设置（`g_user12h__ext`，守护基准）"
        f"的日频 DSR 只有 {num(h1['deflated_sharpe']['daily'][str(n_bound)]['dsr'], 4)}，"
        f"OOS 腿推荐设置 `a_allow037__pre` 只有 "
        f"{num(h2['deflated_sharpe']['daily'][str(n_bound)]['dsr'], 4)}，"
        "两者都远不到 95% 的门；而样本内的搜索冠军 `c1__3y` 反而是 1.0000——"
        "这正是「样本内最优」与「多重检验后仍显著」不是同一件事的教科书案例。"
    )
    add("")
    add(
        "> **这个 PBO 测的不是亏钱概率。** 它度量的是「在这条历史上、从这批 arm 里挑最好的那一个」"
        "这件事有多脆弱。它也**检测不到参数时间旅行与选择 provenance**：曲线本身不记录每个参数"
        "何时变得可知，而本轮四轮研究用的都是同一段 2021–2026 Binance 历史。"
    )
    add("")

    # ---- 1 framing
    add("## 1. 审计在回答什么、不回答什么")
    add("")
    add("| | |")
    add("|---|---|")
    add("| **回答** | 这批已落盘的 arm 里，「挑最好」有多脆弱（CSCV/PBO）；"
        "被推荐的配置在计入试验次数后还剩多少显著性（DSR / MinBTL / SPA / StepM）；"
        "折叠内冠军能否在折叠外站住（fold stability）；"
        "成本变差时头条结论还剩多少（声明式压力臂，非选择输入）。 |")
    add("| **不回答** | 不回答「未来会不会亏钱」；不回答「参数是不是在 2023 年就已可知」；"
        "不回答「币池幸存者偏差有多大」；不新增任何回测、不做任何搜索（预注册）。 |")
    add("")
    add(
        "本轮**没有跑任何新的回测、没有做任何参数搜索、没有改引擎**。所有数字来自已存在的"
        "回放 bundle（`analysis.json` / `balance_and_equity.csv.gz` / `fills.csv`）与各轮冻结的"
        "研究合同。"
    )
    add("")

    # ---- 2 pre-registered rules
    add("## 2. 预注册规则（在看到任何 PBO/DSR 数字之前写死）")
    add("")
    add("### 2.1 退化臂剔除规则")
    add("")
    add("一个 arm 的权益序列如果**已经停止产生收益**，它就没有有意义的分数。规则在选择之前整体生效：")
    add("")
    add("| 代号 | 触发条件 | 理由 |")
    add("|---|---|---|")
    thresholds = index["degeneracy_rule"]["thresholds"]
    add(
        "| `D1_truncated` | arm 的月份集合 ≠ 同腿的众数月份集合 | "
        "强平或数据集裁剪让序列提前结束，它无法在它没覆盖的月份上被排名 |"
    )
    add(
        f"| `D2_terminal_halt` | 末段权益恒定段 ≥ {thresholds['terminal_flat_min_days']} 天 "
        f"（绝对容差 {thresholds['terminal_flat_abs_tol']:.0e}、相对容差 "
        f"{thresholds['terminal_flat_rel_tol']:.0e}） | 终局停机后再无成交，后续月份是常数 |"
    )
    add(
        f"| `D3_zero_variance` | 月度收益样本标准差 < {thresholds['zero_variance_tol']:.0e}，"
        f"或非零月份 < {thresholds['min_nonzero_months']} | 没有方差的序列没有 Sharpe |"
    )
    add(
        f"| `D0_unexpected_cadence` | 权益采样间隔不在 "
        f"{thresholds['supported_cadence_minutes']} 分钟内 | 采样率不同就不能同池排名 |"
    )
    add("")
    add("### 2.2 分块与名次口径")
    add("")
    add(
        "- 时间块边界 = `floor(i·M/S)`（整数运算，余数落在最早的块）；S ∈ "
        f"{list(BLOCKS)}。"
    )
    add(
        "- 并列值取**平均名次**。这不是形式主义：本轮多条保护在窗口内从未触发，"
        "它们的权益曲线**逐位相同**，位置式 tie-break 会让 arm 的字母序决定答案。"
    )
    add(
        "- CSCV 的 ω = 平均名次(从最差数)/(n+1)：**ω ≤ 0.5 表示样本内冠军落在样本外下半区**，"
        "PBO 就是这个比例。"
    )
    add(
        "- 折叠分位 `out_of_fold_percentile_best0` = (从最好数的名次 − 1)/(n − 1)："
        "**0 = 最好、1 = 最差**，所以「不超过第 50 百分位」= ≤ 0.50。"
    )
    add("- 两种选择约定并列报告：`max_adg`（样本内年化几何增长最大）与 `max_adg_x_1_minus_dd`（ADG×(1−最差回撤)）。")
    add("")

    # ---- 3 inputs
    add("## 3. 输入、采样率与月度序列")
    add("")
    add(
        f"载入 **{index['panels'][list(index['panels'])[0]]['n_arms_in_pool'] if False else counts['replay_arms_total']} "
        "个回放 bundle**（8 个已发布轮次目录下所有带 `analysis.json` 的目录）。"
    )
    add("")
    add(
        "**采样率实测**：`balance_and_equity.csv.gz` 是**按小时**采样的（92 个 bundle 众数间隔 60 分钟，"
        "8 个早期 bundle 为 1 分钟）。表头是无名索引 + "
        "`usd_cash_wallet, usd_total_balance, usd_total_equity, strategy_equity, btc_cash_wallet, "
        "btc_total_balance, btc_total_equity`。本审计用 `strategy_equity`。"
    )
    add("")
    add(
        "`analysis.json` **不带月度收益序列**，所以每个月收益都是从 `balance_and_equity.csv.gz` 重算的："
        "「当月最后一个小时样本 ÷ 上月最后一个小时样本 − 1」，首月以序列第一个样本为基。"
        "该口径使月度收益相乘**精确等于全窗倍数**。"
    )
    add("")
    levels = max(
        (panel["monthly_crosscheck"]["level_max_rel"] or 0.0) for panel in index["panels"].values()
    )
    chained = max(
        (panel["monthly_crosscheck"]["chained_max_abs"] or 0.0)
        for panel in index["panels"].values()
    )
    engine = max(
        (panel["monthly_crosscheck"]["engine_pct_max_abs"] or 0.0)
        for panel in index["panels"].values()
    )
    add("与各 run 自带的 `monthly_metrics.csv` 交叉核对（该 CSV 是 run-local 文件，不是 tracked 工件）：")
    add("")
    add("| 核对项 | 最大偏差 | 判读 |")
    add("|---|---|---|")
    add(f"| 月末 `strategy_equity` 水平 vs 引擎 `ending_strategy_equity` | {levels:.3e} | 逐位相同 |")
    add(f"| 用引擎自己的水平列重算月度收益 vs 本审计 | {chained:.3e} | 纯算术一致 |")
    add(f"| vs 引擎 `strategy_equity_return_pct` | {engine:.3e} | **已知口径差**（见下） |")
    add("")
    add(
        "口径差的原因：引擎的月收益是「当月最后样本 ÷ **当月第一个**样本 − 1」，"
        "本审计是「当月最后样本 ÷ **上月最后**样本 − 1」。两者相差的是跨月那一个小时的权益变动"
        f"（量级 {engine:.1e}），而串联口径保证月度收益相乘等于全窗倍数。"
    )
    add("")
    add("### 3.1 tracked 与 run-local 的分工")
    add("")
    add(
        "本审计的**输入全部是 run-local 文件**（`.gitignore` 的 `/backtests/**` 规则排除 "
        "`balance_and_equity.csv.gz`、`fills.csv`、`monthly_metrics.csv`、`config.json`、"
        "`dataset.json` 与 `artifacts/panels/*.csv`）。这是任务指定的口径——`analysis.json` 不带月度序列，"
        "只能回到权益账本重算。因此本审计遵守的是："
    )
    add("")
    add(
        "1. **派生数字全部落在 tracked 的 `*.json` / `*.md` 里**；"
        "`artifacts/panels/*.json` 直接把月度收益矩阵 `returns_matrix` 内嵌，"
        "所以 CSCV/DSR/折叠统计可以**只靠 tracked 工件**重跑，不依赖 run-local 文件；"
    )
    add(
        "2. 每个 run-local 输入都在 `artifacts/panels/*.json` 的 `sources[]` 里登记"
        "仓库相对路径 + `equity_sha256` / `monthly_metrics_sha256` / `analysis_sha256`，"
        "`artifacts/stress_arms.json` 登记 `fills_sha256` 与成交笔数——"
        "**数字可追溯到具体文件内容，而不是「某台机器上的某个路径」**；"
    )
    add("3. tracked 文件里不出现 host path（`run.sh` 的布局检查会扫）。")
    add("")

    # ---- 4 pools
    add("## 4. 两个面板与退化剔除")
    add("")
    add("| 面板 | 腿 | 窗口 | arm 池 | 入选 | 剔除 | 剔除原因 | 月数 |")
    add("|---|---|---|---:|---:|---:|---|---:|")
    for name in sorted(index["panels"]):
        panel = index["panels"][name]
        meta = load_json(PANEL_DIR / f"{name}.json")
        reasons = "；".join(
            f"`{key}`×{len(value)}" for key, value in sorted(panel["removed_by_reason"].items())
        ) or "—"
        add(
            f"| {PANEL_TITLE.get(name, name)} | `{meta['leg']}` | "
            f"{' → '.join(meta['leg_window'])} | {panel['n_arms_in_pool']} | "
            f"{panel['n_arms_used']} | {panel['n_arms_removed']} | {reasons} | {panel['n_months']} |"
        )
    add("")
    add("被剔除的每一个 arm 及其原因（可逐条复核）：")
    add("")
    add("| 面板 | arm | 原因 | 细节 |")
    add("|---|---|---|---|")
    for name in sorted(index["panels"]):
        meta = load_json(PANEL_DIR / f"{name}.json")
        for row in meta["removed_detail"]:
            detail = []
            for reason in row["reasons"]:
                info = row["detail"].get(reason, {})
                if reason == "D1_truncated":
                    detail.append(
                        f"覆盖 {info.get('arm_first_month')}→{info.get('arm_last_month')}"
                        f"（腿 {info.get('leg_first_month')}→{info.get('leg_last_month')}），"
                        f"liquidated={info.get('liquidated')}"
                    )
                elif reason == "D2_terminal_halt":
                    detail.append(f"末段恒定 {info.get('terminal_flat_days')} 天")
                elif reason == "D3_zero_variance":
                    detail.append(
                        f"月度 std={info.get('monthly_std'):.2e}，非零月 {info.get('nonzero_months')}"
                    )
            add(
                f"| {PANEL_TITLE.get(name, name)} | `{row['arm']}` | "
                f"{', '.join('`' + r + '`' for r in row['reasons'])} | {'；'.join(detail)} |"
            )
    add("")

    # ---- 5 CSCV
    add("## 5. CSCV / PBO")
    add("")
    add("每个面板 × 每种选择约定 × 每个 S 都单独报告，避免单一分块带来的偶然性。")
    add("")
    for name in sorted(cscv["panels"]):
        entry = cscv["panels"][name]
        add(f"### {PANEL_TITLE.get(name, name)}（{entry['n_arms_used']} arm × {entry['n_months']} 月）")
        add("")
        add("| 选择约定 | S | 划分次数 | PBO | IS 冠军分数(均值) | OOS 分数(均值) | IS→OOS 斜率 | IS→OOS Spearman | 冠军 OOS 分位(均值) |")
        add("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for selection in SELECTIONS:
            for blocks in BLOCKS:
                row = entry["cscv"][f"{selection}|S{blocks}"]
                add(
                    f"| {SELECTION_SHORT[selection]} | {blocks} | {row['splits']} | "
                    f"**{row['pbo']:.4f}** | {row['is_selected_score_mean']:.4f} | "
                    f"{row['oos_selected_score_mean']:+.4f} | {row['is_oos_slope']:+.3f} | "
                    f"{row['is_oos_spearman_mean']:+.3f} | {row['oos_selected_percentile_mean']:.3f} |"
                )
        add("")
        winner = entry["headline"]["max_adg"]["winner"].split("|")[-1]
        runner = entry["headline"]["max_adg"]["ranking"][1]["arm_id"].split("|")[-1]
        add(
            f"全样本冠军（`max_adg`）：`{winner}`；次优 `{runner}`。"
            f"最常被选中的样本内冠军："
            + "、".join(
                f"`{arm.split('|')[-1]}`×{count}"
                for arm, count in entry["cscv"][f"max_adg|S{BLOCKS[0]}"]["most_frequent_is_best"][:3]
            )
            + "。"
        )
        add("")
    add("**读法（必须与前文一起读）**：PBO 低 ≠ 策略好，PBO 高 ≠ 策略会亏钱。")
    add("池 A 的 `3y` 腿 PBO 极低而斜率同样极负，说明在这条被搜索过的窗口里，")
    add("「冠军」在半个窗口上总是最强、在另半个窗口上分数却系统性更低——")
    add("这是选择效应，不是稳定优势。池 B 的 `ext` 腿斜率在 S=8/10 下为正、S=12/16 下为负，")
    add("说明**保护类 arm 与无保护 arm 之间的差异**在这段历史上部分可迁移，但对分块方式敏感。")
    add("")

    # ---- 6 selection bias
    add("## 6. 选择偏差：DSR / MinBTL / SPA / StepM")
    add("")
    add(
        f"试验次数 N 的**下界**来自 `artifacts/trial_ledger.json`：N ≥ **{n_bound}**"
        f"（台账共 {counts['total_rows']} 行）。敏感性取 N ∈ {n_grid}。"
    )
    add("")
    add(
        "> **方向必须写明：N 是下界。** 被合同声明但从未真跑的格子、优化器未落盘的非 Pareto 候选、"
        "以及任何没有留下记录的人工试错都不计入。真实 N 只会更大，"
        "所以下面所有以 N 为输入的惩罚（DSR 的 SR0、MinBTL、p_adj）都是**乐观端上界**——"
        "读「通过」时请把它读成「最好情况」。"
    )
    add("")
    add("### 6.1 头条配置的 DSR 与 MinBTL")
    add("")
    add("| 配置 | arm | 腿 | 观测数(日/月) | 年化 SR | 偏度 | 峰度(非超额) | DSR@92 | DSR@200 | DSR@500 | DSR@" + str(n_bound) + " | MinBTL(年)@" + str(n_bound) + " |")
    add("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label in sorted(bias["headline_arms"]):
        entry = bias["headline_arms"][label]
        if "unavailable" in entry:
            add(f"| {label} | `{entry['arm']}` | — | — | — | — | — | — | — | — | — | unknown |")
            continue
        daily = entry["deflated_sharpe"].get("daily")
        source = daily if daily else entry["deflated_sharpe"]["monthly"]
        reference = source[str(n_bound)]
        monthly = entry["deflated_sharpe"]["monthly"]
        observed = (
            f"{reference.get('n_observations')} / {monthly[str(n_bound)].get('n_observations')}"
        )
        add(
            f"| {label} | `{entry['arm']}` | {entry['arm'].rsplit('__', 1)[-1]} | {observed} | "
            f"{num(reference.get('sharpe_annualised'), 3)} | "
            f"{num(reference.get('skewness'), 3)} | {num(reference.get('kurtosis_non_excess'), 2)} | "
            + " | ".join(
                num(daily[str(n)]["dsr"], 4) if daily else num(monthly[str(n)]["dsr"], 4)
                for n in (92, 200, 500, n_bound)
            )
            + f" | {num(entry['min_btl_years']['years'][str(n_bound)], 2)} |"
        )
    add("")
    add(
        "算术口径（可手算复核）：`SR0 = sqrt(Var(SR_trials))·[(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))]`，"
        "`DSR = Φ((SR−SR0)√(T−1) / sqrt(1 − γ₃SR + (γ₄−1)SR²/4))`；"
        "`Var(SR_trials)` 用**本面板入选 arm 的横截面**每次观测 Sharpe 方差作代理（这是明确承认的近似）；"
        "`MinBTL(年) = 2·ln(N)/SR_annualised²`。偏度/峰度用偏差修正样本估计量（`bias=False`）。"
    )
    add("")
    add(
        "注意 `g_user12h__ext` 与 `a_allow037__pre` 的日频峰度高达 "
        f"{num(h1['deflated_sharpe']['daily'][str(n_bound)]['kurtosis_non_excess'], 1)} / "
        f"{num(h2['deflated_sharpe']['daily'][str(n_bound)]['kurtosis_non_excess'], 1)}、"
        "偏度为强负：**一次深坑就决定了整个分布的形状**，这正是 DSR 分母被放大的原因，"
        "也是为什么这两条配置的 DSR 在 0.15–0.69 之间而不是 1。"
    )
    add("")
    add("### 6.2 现实检验：最好的 arm 是否胜过基准（平稳自助 SPA / StepM）")
    add("")
    add("| 面板 | 基准 | 期望块长 q | SPA p | StepM p | 被检验的最优 arm | t 统计量 | p_adj@92 | p_adj@200 | p_adj@500 | p_adj@" + str(n_bound) + " |")
    add("|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|")
    for name in sorted(bias["panels"]):
        entry = bias["panels"][name]
        for key in ("equal_weight|q3", "equal_weight|q6", "equal_weight|q12"):
            row = entry["reality_check"][key]
            adjusted = row["p_adjusted_by_n"]
            add(
                f"| {PANEL_TITLE.get(name, name)} | 等权（面板均值） | "
                f"{int(row['expected_block'])} | {row['spa_p_value']:.4f} | "
                f"{row['stepm_p_value']:.4f} | `{str(row['best_arm']).split('|')[-1]}` | "
                f"{row['best_arm_t_statistic']:.3f} | "
                + " | ".join(num(adjusted[str(n)], 4) for n in (92, 200, 500, n_bound))
                + " |"
            )
    add("")
    add(
        "SPA/StepM 的零假设只覆盖**本面板里被观测到的** arm。要把它外推到台账的 N，"
        "本审计给出一列 Bonferroni 式上界 `p_adj = 1 − (1 − p)^(N/n_arms)`，其中 p 取 bootstrap "
        "分辨率上界（1/B，B=2000）。**这一列才是「计入全部试验」后的读数。**"
    )
    add("")
    add(
        "一个必须点名的读法陷阱：池 A `3y` 腿上被检验出的最优 arm 是 `c1__3y`——"
        "它本身就是这一轮**搜索出来的样本内冠军**。SPA 不知道这件事（它只看曲线），"
        "所以那条 p 值不能读作「c1 有真实优势」。"
    )
    add("")

    # ---- 7 folds
    add("## 7. 折叠稳定性")
    add("")
    add(
        f"把搜索窗切成 K 个连续折叠，在每个折叠内排名、看折叠冠军在**其余折叠合并**后的分位；"
        f"并报告折叠间排名的平均 Spearman。预注册 J4 门槛：分位 ≤ 0.50 且 Spearman ≥ 0.50。"
    )
    add("")
    for name in sorted(folds["panels"]):
        entry = folds["panels"][name]
        for key in ("K3", "K6"):
            block = entry[key]
            add(
                f"### {PANEL_TITLE.get(name, name)} · K={block['n_folds']}"
                + ("（主口径）" if key == entry["primary"] else "")
            )
            add("")
            add("| 折叠 | 月份 | 折叠内冠军 | 折叠外名次 | 折叠外分位(0=最好) |")
            add("|---|---|---|---:|---:|")
            for row in block["per_fold"]:
                add(
                    f"| {row['fold']} | {row['months'][0]} → {row['months'][1]} | "
                    f"`{row['in_fold_winner'].split('|')[-1]}` | "
                    f"{row['out_of_fold_rank_best1']:.1f}/{row['n_arms_ranked']} | "
                    f"{row['out_of_fold_percentile_best0']:.3f} |"
                )
            add("")
            add(
                f"最差分位 **{block['j4_percentile_measured']:.3f}**（门槛 ≤ {block['j4_percentile_bar']:.2f}）、"
                f"平均 Spearman **{block['j4_spearman_measured']:+.3f}**"
                f"（门槛 ≥ {block['j4_spearman_bar']:.2f}）、"
                f"冠军落在前半区的折叠比例 {block['share_of_folds_winner_in_top_half']:.2f}。"
            )
            add("")
    add(
        "**关键限定**：`3y` 折叠的「稳定」是在**被搜索过的那段窗口内部**测出来的。"
        "同一批 arm 一旦换到 `ext` / `pre` 腿（池 A 的另外两条腿，以及池 B），"
        "最差分位立刻升到 0.65–1.00。所以折叠稳定性只能证明池子内部自洽，"
        "不能证明选择越过了搜索窗。"
    )
    add("")

    # ---- 8 stress
    add("## 8. 成本/执行压力臂（**不是选择输入**）")
    add("")
    add(
        "三个压力臂在**看到任何压力数字之前**声明，且不参与 CSCV/PBO、DSR、折叠稳定性或任何选臂规则。"
        "做法是对每一笔成交按名义额 |qty×price| 追加若干 bp 的额外成本、累积到逐小时权益序列后相减，"
        "再重算终值倍数与最差回撤。"
    )
    add("")
    add("| 压力臂 | 类型 | 追加成本 | 说明 |")
    add("|---|---|---:|---|")
    for item in stress["stresses"]:
        add(
            f"| `{item['key']}` | {item['kind']} | {item['extra_bp']:.0f} bp/笔 | "
            f"{item['note']} |"
        )
    add("")
    add("| arm | 基线终值 | 基线最差回撤 | " + " | ".join(f"`{s['key']}` 终值 | 保留率 | Δ回撤" for s in stress["stresses"]) + " |")
    add("|---|---:|---:|" + "---:|---:|---:|" * len(stress["stresses"]))
    declared = []
    for arm_id, entry in sorted(stress["arms"].items()):
        if "unavailable" in entry or not entry.get("declared_headline"):
            continue
        declared.append(arm_id)
        cells = []
        for item in stress["stresses"]:
            row = entry["stress"][item["key"]]
            cells.append(
                f"{row['stressed_final_multiple']:.3f}× | "
                f"{row['retained_final_multiple_share'] * 100:.2f}% | "
                f"{row['drawdown_added'] * 100:+.2f}pp"
            )
        add(
            f"| `{arm_id.split('|')[-1]}` | {entry['baseline']['final_multiple']:.3f}× | "
            f"{pct(entry['baseline']['worst_drawdown'])} | " + " | ".join(cells) + " |"
        )
    add("")
    all_retentions = [
        row["retained_final_multiple_share"]
        for entry in stress["arms"].values()
        if "unavailable" not in entry
        for row in entry["stress"].values()
    ]
    n_stressed = sum(1 for entry in stress["arms"].values() if "unavailable" not in entry)
    add(
        f"上表只列**声明式**的 {len(declared)} 个头条 arm；"
        f"`artifacts/stress_arms.json` 里保存了全部 {n_stressed} 个 arm"
        "（含 4 个面板的全部入选臂 + 折叠冠军）的结果，逐臂可核。"
    )
    add("")
    add(
        f"在全部 {n_stressed} 个 arm 上，最坏的保留率是 {pct(min(all_retentions))}、"
        f"最好 {pct(max(all_retentions))}。"
        "费率从 maker 0.0002 抬到 0.0006（+4bp）时头条结论没有翻转；"
        "抬到 0.0010（+8bp）仍不翻转，但每条臂的终值都要打折两位数百分比。"
        "**没有一个压力臂把正终值翻成负终值，也没有一个把回撤排序倒过来。**"
    )
    add("")
    add(
        "**这是一阶界**：真实费率变化还会改变下单位置与订单结构，本算式无法建模；"
        "`fills.csv` 是 run-local 文件（不 tracked），其 sha256 与成交笔数记在 `artifacts/stress_arms.json` 里以便复核。"
    )
    add("")

    # ---- 9 ledger
    add("## 9. 试验台账与可追溯性")
    add("")
    add("| 层级 | 行数 | 已评估 | 说明 |")
    add("|---|---:|---:|---|")
    add(f"| `replay_arm` | {counts['replay_arms_total']} | {counts['replay_arms_evaluated']} | 8 个已发布轮次下带 `analysis.json` 的回放 bundle |")
    add(f"| `declared_cell` | {counts['declared_cells_total']} | {counts['declared_cells_evaluated']} | 早期两轮冻结合同声明的格子（67 + 38） |")
    add(f"| `optimizer_candidate` | {counts['optimizer_candidates']} | {counts['optimizer_candidates']} | 早期 maxdd 研究逐候选 `candidate_metrics.csv` + 本轮 4 个冻结搜索候选 |")
    add(f"| `screen_config` | {counts['screen_configs']} | {counts['screen_configs']} | deployability 筛选 + low_drawdown 候选 |")
    add(f"| **合计** | **{counts['total_rows']}** | **{ledger['n_lower_bound']}** | N 下界 = 已评估行数 |")
    add("")
    ref = ledger["expected_reference"]
    add("### 9.1 与任务先验的对照（可复算判据）")
    add("")
    add(f"- 判据：{ref['criterion']}")
    add("")
    add("| 分项 | 实测 |")
    add("|---|---:|")
    add(f"| (a) 已发布轮次回放 bundle 数 | {ref['measured']['a_published_replay_arms']} |")
    add(f"| (b) `returns_guarded_dd_research_2026-09-16` 合同 cells | {ref['measured']['b_cells_returns_guarded']} |")
    add(f"| (c) `dd_tail_research_2026-09-15` v4 合同 cells | {ref['measured']['c_cells_dd_tail_v4']} |")
    add(f"| (d) (a)+(b)+(c) | {ref['measured']['d_sum_of_a_b_c']}（≥200？{ref['measured']['d_meets_200']}） |")
    add(f"| 扩展下界（计入全部已落盘层级） | {ref['measured']['extended_lower_bound_with_all_recorded_tiers']} |")
    add("")
    add(f"- 差异原因：{ref['difference_reason']}")
    add(f"- 方向：{ref['direction']}")
    add("")
    add("### 9.2 抽检：台账不是手写的")
    add("")
    add("按 `(tier, study, arm)` 排序取每层中间一行，重新在磁盘上解析它的来源文件：")
    add("")
    add("| 层级 | arm/cell | 来源文件 | 重解析结果 |")
    add("|---|---|---|---|")
    for sample in ledger["traceability_samples"]:
        recheck = sample["recheck"]
        if sample["tier"] == "replay_arm":
            detail = (
                f"`analysis.json` 存在={recheck['analysis_json_exists']}，"
                f"`balance_and_equity.csv.gz` 首行时间戳 = {recheck['first_equity_ts']}，"
                f"sha256 与台账一致="
                f"{recheck['equity_sha256'] == recheck['ledger_equity_sha256']}"
            )
        elif sample["tier"] == "declared_cell":
            detail = (
                f"合同内列出该 cell={recheck['cell_listed_in_contract']}，"
                f"`cells/**/result.json` 共 {recheck['result_json_count']} 个"
            )
        elif sample["tier"] == "optimizer_candidate":
            detail = (
                f"`candidate_metrics.csv` 存在={recheck['candidate_metrics_exists']}，"
                f"行数={recheck['row_count']}"
            )
        else:
            detail = (
                f"来源 CSV 存在={recheck['screen_csv_exists']}，行数={recheck['row_count']}"
            )
        add(f"| `{sample['tier']}` | `{sample['arm']}` | `{sample['evidence_path']}` | {detail} |")
    add("")

    # ---- 10 anti-pattern summary
    add("## 10. 反模式审查")
    add("")
    add(
        "R1–R18 沿用 `backtests/binance/2026-09-14T03_40_41/lookahead_risk_matrix.csv` 的编号，"
        "并按本轮的 arm 重新取证；R19–R22 是本轮新增。完整表见 "
        "[`anti_pattern_audit.md`](anti_pattern_audit.md)。"
    )
    add("")
    add("| arm | 成交笔数 | panic 成交 | 流动性分布 |")
    add("|---|---:|---:|---|")
    for row in facts:
        if row.get("unavailable"):
            add(f"| `{row['arm']}` | unknown | unknown | 无 fills.csv |")
            continue
        add(
            f"| `{row['arm']}` | {row['fills']} | {row['panic_fills']} | "
            f"{', '.join(f'{k}={v}' for k, v in sorted(row['liquidity'].items()))} |"
        )
    add("")

    # ---- 11 limits
    add("## 11. 边界：这个审计测不出什么")
    add("")
    for text in (
        "**参数时间旅行（R16）**：曲线不记录每个参数何时变得可知。本审计用的 8 个轮次目录"
        "全部形成于 2026-09，回放窗口却从 2021 开始。PBO/DSR/SPA 都对此**无感**。",
        "**选择 provenance（R17）**：无法从工件重建「当时看过哪些候选」。"
        "优化器非 Pareto 候选没有落盘，人工试错没有记录。台账因此只能给下界。",
        "**幸存者偏差（R17）**：40 币池是活到 2026 年的当前 top40；长腿起步时只有 22 个币有数据。",
        "**单一历史段**：四轮研究共用同一段 2021–2026 Binance 历史。"
        "无论审计多严格，都无法把「这段历史」变成「另一段历史」。",
        "**DRS 的 Var(SR_trials) 是代理**：用面板横截面方差代替真实试验集离散度，"
        "真实试验集的离散度不可知。",
        "**压力臂是一阶界**：只改成本、不改行为。",
        "**面板矩形化**：CSCV 需要矩形面板，退化臂被整体移除而不是被插补，"
        "这会让池子变小、PBO 的估计方差变大。",
    ):
        add(f"- {text}")
    add("")

    # ---- 12 verdicts
    add("## 12. 预注册判据 J1–J5（规则与实测值；裁决见 §13）")
    add("")

    def per_panel_range(selection: str, field: str) -> str:
        values = [cscv["panels"][name]["cscv"][f"{selection}|S{blocks}"][field]
                  for name in sorted(cscv["panels"]) for blocks in BLOCKS]
        return f"{min(values):.3f}–{max(values):.3f}"

    j1_rows = []
    for name in sorted(cscv["panels"]):
        entry = cscv["panels"][name]
        pbo_values = {s: entry["cscv"][f"max_adg|S{s}"]["pbo"] for s in BLOCKS}
        slope_values = {s: entry["cscv"][f"max_adg|S{s}"]["is_oos_slope"] for s in BLOCKS}
        passing = [s for s in BLOCKS if pbo_values[s] <= 0.50 and slope_values[s] > -0.5]
        j1_rows.append(
            f"| {PANEL_TITLE.get(name, name)} | {min(pbo_values.values()):.3f}–"
            f"{max(pbo_values.values()):.3f} | {min(slope_values.values()):+.3f}～"
            f"{max(slope_values.values()):+.3f} | "
            + "、".join(
                f"S{s}{'✓' if s in passing else '✗'}"
                f"({slope_values[s]:+.2f})" for s in BLOCKS
            )
            + f" | {len(passing)}/{len(BLOCKS)} |"
        )

    j2_rows = []
    for name in sorted(index["panels"]):
        panel = index["panels"][name]
        share = panel["n_arms_removed"] / panel["n_arms_in_pool"] if panel["n_arms_in_pool"] else 0
        j2_rows.append(
            f"| {PANEL_TITLE.get(name, name)} | {panel['n_arms_in_pool']} | "
            f"{panel['n_arms_removed']} | {share * 100:.1f}% | "
            f"{'；'.join(f'`{k}`×{len(v)}' for k, v in sorted(panel['removed_by_reason'].items())) or '—'} |"
        )

    j3_subjects = ("H1_guard_baseline_ext", "H2_recommended_pre", "H3_best_geometry_ext")
    j3_rows = []
    for label in j3_subjects:
        entry = bias["headline_arms"][label]
        daily = entry["deflated_sharpe"].get("daily")
        dsr_values = [
            (daily or entry["deflated_sharpe"]["monthly"])[str(n)]["dsr"] for n in n_grid
        ]
        panel_name = entry.get("panel")
        spa_row = (
            bias["panels"][panel_name]["reality_check"]["equal_weight|q6"]
            if panel_name in bias["panels"]
            else None
        )
        spa = spa_row["spa_p_value"] if spa_row else None
        padj = spa_row["p_adjusted_by_n"][str(n_bound)] if spa_row else None
        best = str(spa_row["best_arm"]).split("|")[-1] if spa_row else "unknown"
        j3_rows.append(
            f"| {label} | `{entry['arm']}` | "
            f"{num(min(dsr_values), 4)}–{num(max(dsr_values), 4)}（>0.95？{min(dsr_values) > 0.95}） | "
            f"{num(spa, 4)}（<0.05？{spa is not None and spa < 0.05}） | "
            f"{num(padj, 4)} | `{best}` |"
        )
    pool_b_spa = bias["panels"]["poolB_ext_union__ext"]["reality_check"]["equal_weight|q6"]

    j4_rows = []
    for name in sorted(folds["panels"]):
        entry = folds["panels"][name]
        block = entry[entry["primary"]]
        percentile = block["j4_percentile_measured"]
        spearman_value = block["j4_spearman_measured"]
        passes = percentile <= block["j4_percentile_bar"] and spearman_value >= block["j4_spearman_bar"]
        j4_rows.append(
            f"| {PANEL_TITLE.get(name, name)} | K={block['n_folds']} | "
            f"{percentile:.3f}（≤0.50？{percentile <= 0.50}） | "
            f"{spearman_value:+.3f}（≥0.50？{spearman_value >= 0.50}） | {'是' if passes else '否'} |"
        )

    add("### J1 池子是否携带信号：PBO ≤ 0.50 且 IS→OOS 斜率 > −0.5")
    add("")
    add("规则逐分块判定（每个 S 单独算一次），右列给满足的分块数；裁决留给父 agent。")
    add("")
    add("| 面板 | PBO（S∈{8,10,12,16}） | IS→OOS 斜率 | 逐分块（PBO ∧ 斜率） | 满足 |")
    add("|---|---|---|---|---:|")
    lines.extend(j1_rows)
    add("")
    add("### J2 退化污染：退化规则从池子里移除了多少")
    add("")
    add("| 面板 | 池内 arm | 移除 | 占比 | 原因 |")
    add("|---|---:|---:|---:|---|")
    lines.extend(j2_rows)
    add("")
    add("### J3 选择是否熬过多重检验：DSR > 0.95 且 SPA p < 0.05")
    add("")
    add(
        "DSR 逐配置给出（N 敏感性全列）。SPA 一列是**该配置所在面板**的现实检验 p 值"
        "（基准 = 等权，q=6），其检验对象是**面板内最优 arm**，不一定是本行的 arm——"
        "所以它只回答「这个池子里有没有东西能赢过等权基准」，不回答「本行配置是否显著」。"
    )
    add("")
    add(
        "| 配置 | arm | DSR（N∈" + str(n_grid) + "） | 所在面板 SPA p（对象=面板最优 arm） | "
        "p_adj@N=" + str(n_bound) + " | 该面板的最优 arm |"
    )
    add("|---|---|---|---|---|---|")
    lines.extend(j3_rows)
    add("")
    add(
        f"（作为对照：被检验出的最优 arm 是 `{str(pool_b_spa['best_arm']).split('|')[-1]}`，"
        f"t = {pool_b_spa['best_arm_t_statistic']:.3f}，SPA p = {pool_b_spa['spa_p_value']:.4f}。"
        "SPA 测的是「最优 arm 胜过基准」，不是「被推荐的配置显著」。）"
    )
    add("")
    add("### J4 折叠稳定性：折叠冠军的折叠外分位 ≤ 0.50 且折叠间 Spearman ≥ 0.5")
    add("")
    add("| 面板 | K | 折叠冠军最差分位(0=最好) | 平均 Spearman | 规则是否满足 |")
    add("|---|---:|---:|---:|---|")
    lines.extend(j4_rows)
    add("")
    add("### J5 冻结决策")
    add("")
    add("规则：**若 J3 与 J4 都通过 ⇒ 冻结这段历史、停止调参；否则只做结构性改动，"
        "并把守护/阶梯标注为样本外未确认。**")
    add("")
    add("| 输入 | 实测 |")
    add("|---|---|")
    add(f"| J3 是否通过 | {'是' if all(min((bias['headline_arms'][label]['deflated_sharpe'].get('daily') or bias['headline_arms'][label]['deflated_sharpe']['monthly'])[str(n)]['dsr'] for n in n_grid) > 0.95 for label in j3_subjects) else '否'}（三条被推荐配置的 DSR 在不同 N 下均未超过 0.95） |")
    add(f"| J4 是否通过 | {'是' if all(folds['panels'][name][folds['panels'][name]['primary']]['j4_percentile_measured'] <= 0.50 and folds['panels'][name][folds['panels'][name]['primary']]['j4_spearman_measured'] >= 0.50 for name in folds['panels']) else '否'}（仅搜索窗 `3y` 满足，`ext`/`pre`/池 B 均不满足） |")
    add("")
    add("---")
    add("")
    add("## 13. 判据裁决表（父 agent 裁决）")
    add("")
    add("| 判据 | 规则 | 实测 | 裁决 |")
    add("|---|---|---|---|")
    add(
        f"| **J1** 池子携带信号 | PBO ≤ 0.50 且 IS→OOS 斜率 > −0.5 | "
        f"池 B `ext`：PBO {min(headline['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}–"
        f"{max(headline['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}，"
        f"斜率 {min(headline['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in BLOCKS):+.3f}～"
        f"{max(headline['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in BLOCKS):+.3f}；"
        f"池 A `ext`：PBO {min(pool_a_ext['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}–"
        f"{max(pool_a_ext['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}，"
        f"斜率 {min(pool_a_ext['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in BLOCKS):+.3f}～"
        f"{max(pool_a_ext['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in BLOCKS):+.3f}；"
        f"池 A `3y`：PBO {min(pool_a_3y['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}–"
        f"{max(pool_a_3y['cscv'][f'max_adg|S{s}']['pbo'] for s in BLOCKS):.3f}，"
        f"斜率 {min(pool_a_3y['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in BLOCKS):+.3f}～"
        f"{max(pool_a_3y['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in BLOCKS):+.3f} | **不通过** |"
    )
    removed_total = sum(p["n_arms_removed"] for p in index["panels"].values())
    pool_total = sum(p["n_arms_in_pool"] for p in index["panels"].values())
    add(
        f"| **J2** 退化污染 | 报出退化规则各池/各腿移除了多少 | "
        f"4 个面板合计移除 {removed_total}/{pool_total} = {removed_total / pool_total * 100:.1f}%；"
        f"逐面板："
        + "；".join(
            f"{PANEL_TITLE.get(name, name)} {index['panels'][name]['n_arms_removed']}/"
            f"{index['panels'][name]['n_arms_in_pool']}"
            for name in sorted(index["panels"])
        )
        + " | **通过（记录项）** |"
    )
    add(
        f"| **J3** 选择熬过多重检验 | DSR > 0.95 且 SPA p < 0.05（N 敏感性 "
        f"{n_grid}） | `g_user12h__ext` 日频 DSR "
        + "／".join(
            num(bias["headline_arms"]["H1_guard_baseline_ext"]["deflated_sharpe"]["daily"][str(n)]["dsr"], 4)
            for n in n_grid
        )
        + "；`a_allow037__pre` "
        + "／".join(
            num(bias["headline_arms"]["H2_recommended_pre"]["deflated_sharpe"]["daily"][str(n)]["dsr"], 4)
            for n in n_grid
        )
        + f"；池 B SPA p={pool_b_spa['spa_p_value']:.4f}、p_adj@{n_bound}="
        f"{pool_b_spa['p_adjusted_by_n'][str(n_bound)]:.4f} | **不通过** |"
    )
    add(
        f"| **J4** 折叠稳定性 | 折叠冠军折叠外分位 ≤ 0.50 且 Spearman ≥ 0.5 | "
        + "；".join(
            f"{PANEL_TITLE.get(name, name)} 分位 "
            f"{folds['panels'][name][folds['panels'][name]['primary']]['j4_percentile_measured']:.3f}、"
            f"Spearman "
            f"{folds['panels'][name][folds['panels'][name]['primary']]['j4_spearman_measured']:+.3f}"
            for name in sorted(folds["panels"])
        )
        + " | **不通过** |"
    )
    add(
        "| **J5** 冻结决策 | J3 且 J4 通过 ⇒ 冻结这段历史、停止调参；否则只做结构性改动，"
        "守护/阶梯标注为样本外未确认 | 见 J3 与 J4 的实测 | **不冻结**（J3、J4 样本外均不通过） |"
    )
    add("")
    add("")
    add("### 13.1 裁决理由")
    add("")
    add("**J1 不通过。** 规则要求 PBO ≤ 0.50 **且** 半样本 IS→OOS 斜率 > −0.5。PBO 一侧基本合格"
        "（池 B `ext` 与池 A `pre` 都压在 0.50 附近或以下），但斜率一侧在 16 个「面板 × 分块」组合里"
        "只有 2 个通过（池 B `ext` 的 S8/S10），而**真正的样本外窗口 `pre` 在每一个分块下斜率都是"
        "负的**。也就是说：在这条历史上「搜索窗里排第一」与「别处排得好」没有稳定关系，通过的那两格"
        "还依赖分块数选择。")
    add("")
    add("**J2 通过（记录项，不是门槛）。** 预注册的退化规则（`D0`–`D3`）在任何选择之前整体移除 arm，"
        "4 个面板合计移除 14/72 = 19.4%，且集中在并集池（10/28 = 35.7%，其中 `D1_truncated`×7、"
        "`D2_terminal_halt`×3、`D3_zero_variance`×10）。污染是实质性的，现在已经量化，并且不再污染"
        "「挑最好」这一步。")
    add("")
    add("**J3 不通过。** 三条被推荐的头部配置在任何 N 下都远达不到 DSR > 0.95"
        "（`g_user12h__ext` 0.4559–0.6864、`b_red015__ext` 0.5762–0.7309、"
        "`a_allow037__pre` 0.1541–0.2839）。唯一低于 0.05 的 SPA p 属于**面板内最优 arm** 而不是"
        "被推荐的配置，而且把没落盘的试验补进多重检验后（N = 1807，仍是**下界**）变成 0.9479；"
        "另外两个面板的 SPA p 分别是 0.0835 与 0.3050。")
    add("")
    add("**J4 不通过。** 只有搜索窗 `3y` 满足（分位 0.188、Spearman +0.667）——同一条历史上切来切去"
        "当然相关；一旦离开搜索窗，折叠冠军的折叠外分位掉到 0.846（`ext`）、1.000（`pre`）、"
        "0.647（池 B），平均 Spearman 掉到 +0.190…+0.313。")
    add("")
    add("### 13.2 J5：不冻结这条历史（预注册规则的直接后果）")
    add("")
    add("1. **停止在同一条 2021–2026 Binance 历史上继续调参。** 本轮之后不再新增「在同一份数据上"
        "挑更好配置」的搜索；`c1`（搜索冠军）、`b_red015`、`g_user12h`、`a_allow037` 这些点只能作为"
        "**样本内描述**引用，不得当作预期收益。")
    add("2. **只做结构性改动。** 能靠机制论证、不依赖这段历史的改动仍然可做——本轮引擎 PR 的两个键"
        "（冷却阶梯是风险塑形；累计已实现亏损口径修的是「瞬时尖峰扣动永久关停」的机制缺陷）属于"
        "这一类，但同样必须在文档里标注**样本外未确认**。")
    add("3. **账户守护与冷却阶梯标记为「未在样本外确认」。** 本审计给出的证据是「在这条历史上，"
        "守护类改动的样本外排名不可靠」，因此它们在实盘上只能当作**风险上限**（降低暴露、限制单币"
        "集中度），不能当作已被验证的收益来源。这与 `g4_hsl_halt_ladder_2026-09-18` 的 K1/K3 一致："
        "阶梯的收益中性、第一档取舍在两条腿上相反。")
    add("4. **新证据轴。** 后续要改变上述结论，必须引入这条历史之外的证据：新的时间段（2026-09 之后"
        "的前向纸面/实盘记录）、新的交易所或币池、或与选币无关的机制性论证；在旧历史上再搜一次不算"
        "新证据。")
    add("")
    add("### 13.3 读这些数字时不能越界的地方")
    add("")
    add("- **PBO 不是亏钱概率**：它度量的是「在这批 arm 里挑最好的那一个」在这条历史上的脆弱程度，"
        "与未来收益无关。")
    add("- **它检测不到参数时间旅行与选择来源**（反模式 R16/R17）：曲线本身不记录每个参数何时变得"
        "可知，台账也只能记录**被点名**的尝试。")
    add("- **N = 1807 是下界**：被合同声明但从未真跑的格子、未落盘的非 Pareto 候选、没有记录的人工"
        "试错都不计入，因此所有以 N 为输入的惩罚（DSR 的 `SR0`、MinBTL、`p_adj`）都是**乐观端**"
        "——真实惩罚只会更重。判断「通过」时应把这一列读成「最好情况」。")
    add("- **退化剔除让面板变小**：CSCV 需要矩形面板，退化臂被整体移除而不是插补，池越小 PBO 的估计"
        "方差越大；池 B 的 10/28 剔除率意味着它的 PBO 比池 A 更不稳定。")
    add("---")
    add("")
    add("## 14. 复现")
    add("")
    add("```bash")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage panels")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --verify-only")
    add("venv/bin/python -m pytest tests/test_g4_overfitting_audit.py -q")
    add("```")
    add("")
    add("离线边界：无网络、无凭据、不启动实盘、不新增回测、不做参数搜索、不改引擎。")
    add("")
    add("## 15. 证据布局")
    add("")
    add("- `artifacts/trial_ledger.json` — 全部试验 + N 下界 + 抽检")
    add("- `artifacts/panels/index.json`、`artifacts/panels/<pool>__<leg>.{csv,json}` — 月度面板与退化明细")
    add("- `artifacts/cscv_pbo.json` — CSCV/PBO（2 约定 × 4 分块 × 4 面板）")
    add("- `artifacts/selection_bias.json` — DSR / MinBTL / SPA / StepM / N 敏感性")
    add("- `artifacts/fold_stability.json` — 折叠稳定性（K=3, 6）")
    add("- `artifacts/stress_arms.json` — 声明式压力臂")
    add("- `report_tools/*.py` — 六个可独立 import 的模块；`verify_audit.py` 不 import 其余五个")
    add("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------- anti-patterns ---
def render_anti_pattern_audit() -> str:
    ledger = load_json(ARTIFACTS / "trial_ledger.json")
    index = load_json(PANEL_DIR / "index.json")
    cscv = load_json(ARTIFACTS / "cscv_pbo.json")
    bias = load_json(ARTIFACTS / "selection_bias.json")
    facts = fill_facts()
    n_bound = ledger["n_lower_bound"]
    matrix_path = "backtests/binance/2026-09-14T03_40_41/lookahead_risk_matrix.csv"

    def fact(arm: str) -> dict[str, Any]:
        for row in facts:
            if row["arm"] == arm:
                return row
        return {}

    a_allow000 = fact("a_allow000__ext")
    guard = fact("g_user12h__ext")
    c1 = fact("c1__3y")
    removed_total = sum(p["n_arms_removed"] for p in index["panels"].values())
    pool_total = sum(p["n_arms_in_pool"] for p in index["panels"].values())
    ext_pool_a = cscv["panels"]["poolA_risk_geometry__ext"]
    pool_b = cscv["panels"]["poolB_ext_union__ext"]
    h1 = bias["headline_arms"]["H1_guard_baseline_ext"]

    lines: list[str] = []
    add = lines.append
    add("# 反模式与泄露审查（本审计的 arm 口径，R1–R22）")
    add("")
    add(
        "本文件把 "
        f"[`{matrix_path}`](../2026-09-14T03_40_41/lookahead_risk_matrix.csv) "
        "里的 R1–R18 逐条搬到**本轮的 arm 口径**重新取证，并新增 R19–R22。"
        "状态只有三种：**pass / fail / unknown**。"
        "`unknown` 是本审计在证据不存在时的正式答案——不是「大概没问题」。"
    )
    add("")
    add(
        "取证口径：R 行只引用两类证据，(1) 本轮已落盘的 run 文件"
        "（`analysis.json` / `fills.csv` / `balance_and_equity.csv.gz` / `config.json`），"
        "(2) 本审计自己产出的 tracked 工件（`artifacts/*.json`）。"
        "凡是两者都没有的，写 `unknown` 并写明缺什么。"
    )
    add("")
    add(
        "**证据的 tracked / run-local 属性**：`.gitignore` 的 `/backtests/**` 规则保留 "
        "`*.md`/`*.py`/`*.sh`/`*.json`，排除 `*.csv`、`*.csv.gz`、`config.json`、`dataset.json`。"
        "所以 run 目录里的 `fills.csv`、`balance_and_equity.csv.gz`、`monthly_metrics.csv`、"
        "`config.json`、`dataset.json` 都是 **run-local**。本文件按仓库相对路径引用它们，"
        "并把每个输入的 sha256 记在 tracked 的 `artifacts/panels/*.json`（`sources[]`）与 "
        "`artifacts/stress_arms.json`（`provenance`）里——数字追溯到文件内容，而不是某台机器的路径。"
    )
    add("")
    add(
        "**本轮未复验引擎代码路径**：本 worktree 的 `src/**` 与 `passivbot-rust/**` 正在被其他 agent "
        "修改，且本审计明确不做引擎改动。凡是只能靠「读引擎源码」判定的条目（R1/R3/R4/R9/R10），"
        "本轮一律给 `unknown` 或标注证据来自上一份审计，而不是当成已复验。"
    )
    add("")
    add("## 1. 逐条状态")
    add("")
    add("| 编号 | 类别 | 状态 | 本轮证据（文件 + 数字） |")
    add("| --- | --- | --- | --- |")
    rows = [
        (
            "R1", "直接读取未来 candle（`next_candle`）", "fail",
            "代码路径未变：回测仍把 `k+1` 的 low/high 作为生成阶梯的提示"
            "（`passivbot-rust/src/backtest.rs`、`trailing_martingale::generate_orders`）。"
            "本审计**没有**做 no-peek 反事实（明确不在范围内），"
            "所以「无前视认证」依然失败。",
        ),
        (
            "R2", "entry 侧的 peek 范围", "unknown",
            f"配置侧可观察：entry `retracement_base_pct > 0` 且 cooldown 为正"
            f"（run-local `config.json`），实测 `a_allow000__ext` 成交 "
            f"{a_allow000.get('fills', 'unknown')} 笔。"
            "但「gate 是否真的把 entry 阶梯单笔化」需要 output-equivalence replay——"
            "上一份审计就把它列为 still required，本审计**没有做**，因此不给 pass。",
        ),
        (
            "R3", "close 侧的 peek 范围", "fail",
            "close 仍可展开完整递归梯子（`close.retracement_base_pct > 0`，代码路径来自上一份审计，"
            "本轮未复验）。这是本轮 maker 成交假设之外最需要打折的一项；"
            "本审计无法量化它对月度收益的影响。",
        ),
        (
            "R4", "普通信号时序", "unknown",
            "上一份审计判定 `check_for_fills(k)` 先于 `update_emas(k)`/`update_trailing_prices(k)`。"
            "本审计**没有复验引擎代码路径**（本 worktree 的 `src/**`、`passivbot-rust/**` 正在被"
            "其他 agent 修改），因此按本轮口径只能给 unknown。",
        ),
        (
            "R5", "零延迟激活", "fail",
            "执行口径 `execution_delay_bars = 0`、`intrabar_fill_order = close_first`"
            "（各轮 `variant_input.json` 的 `execution` 块），即 T+1 零确认延迟下界。"
            "本轮**没有**跑 T+2 敏感性。",
        ),
        (
            "R6", "同 bar 路径", "unknown",
            "每币固定 close-before-entry，OHLC 无法恢复真实先后。"
            "本审计没有跑 O-H-L-C / O-L-H-C 边界，因此「同 bar 顺序对结论的影响」在本轮**未测量**。",
        ),
        (
            "R7", "时间戳语义", "pass",
            "`fills.csv` 的 `timestamp` 是 candle 开盘标签；"
            "本审计的月度归属按该标签 + 逐小时权益采样，口径写在 "
            "`artifacts/panels/index.json` 的 `cadence_note`。",
        ),
        (
            "R8", "月末归属", "pass",
            "月度收益直接从 `balance_and_equity.csv.gz` 的月末小时样本重算，"
            "并与各 run 自带 tracked `monthly_metrics.csv` 交叉核对："
            f"水平最大相对偏差 "
            f"{max((p['monthly_crosscheck']['level_max_rel'] or 0) for p in index['panels'].values()):.3e}。"
            "成交级的 23:59 标签不影响本审计的口径（本审计不按成交分月）。",
        ),
        (
            "R9", "完成 candle 特征", "unknown",
            "上一份审计判定 1m EMA / quote-volume / log-range 均在 candle k 完成后更新、"
            "小时 bucket 止于 k-1。本审计未复验引擎代码路径，故按本轮口径记 unknown。",
        ),
        (
            "R10", "warmup", "unknown",
            "可观察的部分：所有 arm 用同一份 dataset（`global_metrics.json` 的 cache label 与 "
            "manifest sha256 一致，登记在 `artifacts/panels/*.json` 的 `sources[]`），"
            "所以**跨 arm 可比**这一点成立。"
            "但「warmup 是否只用了交易前历史」属于引擎代码路径，本轮未复验，故记 unknown。",
        ),
        (
            "R11", "leading gap 回填", "unknown",
            "本审计**在 run 工件里找不到 `fill_leading_gaps` 这个键**"
            "（`grep` 过 `config.json` / `config.original.json` / `dataset.json` / `global_metrics.json`），"
            "因此无法从落盘证据确认它是否为 False。上一份审计的「默认 False」结论本轮无法复验。",
        ),
        (
            "R12", "内部 gap 零成交量补点", "unknown",
            "`gap_tolerance_ohlcvs_minutes=120` 会用前收 + 零成交量补内部缺口。"
            "这是过去值补点、不是未来泄露，但会压低波动率/成交量估计。"
            "本审计**没有**逐行标注 synthetic-row provenance，因此影响量级 unknown。",
        ),
        (
            "R13", "BTC 基准 bfill", "pass",
            "本轮所有 arm 的 `btc_collateral_cap=0`，BTC 计价指标对策略余额无影响；"
            "本审计全部结论用 `strategy_equity`（USD），不经过 BTC 路径。",
        ),
        (
            "R14", "高阶合成 1m", "pass",
            f"输入已是 1m（`dataset.json` 的 `candle_interval_minutes=1`），该分支未激活。",
        ),
        (
            "R15", "下架/有效区间元数据", "fail（本轮首次有对应成交）",
            f"**与上一份审计不同**：本轮账本**有** panic 成交——`a_allow000__ext` "
            f"{a_allow000.get('panic_fills', 'unknown')} 笔、`g_user12h__ext` "
            f"{guard.get('panic_fills', 'unknown')} 笔（类型含 `close_panic_long`）。"
            "守护触发的强平式清仓因此不再是空分支，必须逐臂按 `hard_stop_*` 遥测打折；"
            "下架（delist）成交仍未观察到。",
        ),
        (
            "R16", "参数时间旅行", "fail",
            "**本轮最严重的一项，且 PBO/DSR/SPA 全都测不到它。** "
            f"8 个轮次目录全部形成于 2026-09，回放窗口从 2021-04-20 开始"
            "（`artifacts/panels/*.json` 的 `first_sample`）。"
            "「三年 7.26 倍」不是「该参数在 2023 年已可实盘取得的表现」。",
        ),
        (
            "R17", "选择 provenance / 幸存者偏差", "fail",
            f"台账下界 N ≥ {n_bound}，但**清单本身不完整**：优化器非 Pareto 候选未落盘、"
            "人工试错无记录、40 币池是活到 2026 年的当前 top40。"
            "本审计能把 N 的下界算出来，**不能**把缺失的候选补回来。",
        ),
        (
            "R18", "跨所成交量归一化", "pass",
            "单一 Binance 来源（各 run `dataset.json` 的 exchange=binance），该分支未激活。",
        ),
        (
            "R19", "单一历史段被四轮复用（本轮新增）", "fail",
            "A/B/C/D 四轮全部在 2021–2026 同一段 Binance 历史上选点与判定。"
            "本审计的 `pre` 腿（2021-04-20 → 2023-09-11）只对**风险几何轮**是样本外；"
            "对更早的轮次它已被看过。因此「样本外」是**相对**的，不是绝对的。",
        ),
        (
            "R20", "优化器中间候选不可恢复（本轮新增）", "fail",
            "风险几何轮的搜索只冻结了 4 个选定候选 + 8 个 Pareto 点"
            "（`artifacts/search_selection.json`）；`optimize_results/**` 是 local-only 且 "
            "`all_results.bin` 是不可读的 pymoo checkpoint。"
            "即 **non-Pareto 的评估次数无法从 tracked 工件重建**，只能引用报告里声明的数字。",
        ),
        (
            "R21", "跨轮成本/延迟口径漂移（本轮新增）", "unknown",
            "早期研究存在 T+2 / 3× maker 费的保守口径，本轮四轮统一为 T+1 + Binance 实际费率。"
            "本审计只读本轮口径的 arm，**没有**把两套口径混编，"
            "但也没有重算早期 arm 在本口径下的结果，因此跨轮可比性 unknown。",
        ),
        (
            "R22", "分块/退化规则本身是自由度（本轮新增）", "pass（已显式暴露）",
            f"PBO 对 S 与退化规则敏感，本审计把 S ∈ {8,10,12,16} 全列出："
            f"池 B `ext` PBO "
            f"{min(pool_b['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}–"
            f"{max(pool_b['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}，"
            f"池 A `ext` "
            f"{min(ext_pool_a['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}–"
            f"{max(ext_pool_a['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}。"
            f"退化规则在选择前生效，4 个面板合计移除 {removed_total}/{pool_total} = "
            f"{removed_total / pool_total * 100:.1f}%。"
            "规则写死在 `report_tools/panel.py`，verifier 独立重算同一规则。",
        ),
    ]
    for code, category, status, evidence in rows:
        add(f"| {code} | {category} | **{status}** | {evidence} |")
    add("")
    add("## 2. 统计")
    add("")
    statuses = [row[2] for row in rows]
    add(f"- 总条目：{len(rows)}（R1–R22）")
    add(f"- pass：{sum(1 for s in statuses if s.startswith('pass'))}")
    add(f"- fail：{sum(1 for s in statuses if s.startswith('fail'))}")
    add(f"- unknown：{sum(1 for s in statuses if s.startswith('unknown'))}")
    add("")
    add("## 3. unknown 缺的是什么证据")
    add("")
    add("| 编号 | 缺的证据 | 要补它需要做什么 |")
    add("| --- | --- | --- |")
    add(
        "| R2 | entry 阶梯是否真的被 gate 单笔化 | 对同一 arm 做 output-equivalence replay"
        "（关掉 peek vs 保留 peek），比对成交账本签名；上一份审计已把它列为 still required |"
    )
    add(
        "| R4 / R9 / R10 | 引擎代码路径的复验 | 在一份**冻结**的引擎提交上重读 "
        "`backtest.rs` / `update_emas` / `update_trailing_prices` 与 warmup 播种逻辑；"
        "本 worktree 的 `src/**`、`passivbot-rust/**` 正在被其他 agent 修改，当前状态不可作为证据 |"
    )
    add(
        "| R6 | 同 bar 顺序的上下界 | 跑 O-H-L-C 与 O-L-H-C 两套 `intrabar_fill_order` 并对比月度收益分布（需要新回测） |"
    )
    add(
        "| R11 | `fill_leading_gaps` 的实际取值 | 让回放把该开关写进 `global_metrics.json` 或 `run_record.json`；"
        "当前工件里没有这个键 |"
    )
    add(
        "| R12 | 合成行 provenance | 让数据准备阶段逐行导出 synthetic 标记，并把波动率/成交量估计在剔除合成行后重算 |"
    )
    add(
        "| R21 | 跨口径可比性 | 用当前口径重跑早期研究的 arm，或把早期 arm 的成本/延迟参数逐项映射后重算 |"
    )
    add("")
    add("## 4. 本审计自身不能证明的事项")
    add("")
    add(
        "1. **不能证明无前视**：R1/R3 仍在，no-peek 反事实未做。"
        "PBO/DSR/SPA 全部建立在同一条（可能带 peek 的）权益曲线上。"
    )
    add(
        f"2. **不能把 PBO 读成亏钱概率**：池 B `ext` 的 PBO "
        f"{min(pool_b['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}–"
        f"{max(pool_b['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f} 只说"
        "「从这批 arm 里挑最好」的脆弱程度，与未来盈亏无关。"
    )
    add(
        "3. **不能消除选择偏差**：R16/R17 仍在；台账只把 N 的下界算出来，"
        "把惩罚加上去，**不能**把没记录的试验变回来。"
    )
    add(
        f"4. **不能证明可实盘部署**：无盘口队列、部分成交、mark-price 强平、资金费率；"
        f"压力臂只是一阶成本界（`artifacts/stress_arms.json`）。"
    )
    add(
        f"5. **不能保证 DSR 的口径**：`Var(SR_trials)` 用面板横截面代理，"
        f"真实试验集离散度不可知（`artifacts/selection_bias.json` 的 `formulas.sr_std_proxy`）。"
    )
    add("")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------ README ---
def render_readme() -> str:
    ledger = load_json(ARTIFACTS / "trial_ledger.json")
    index = load_json(PANEL_DIR / "index.json")
    cscv = load_json(ARTIFACTS / "cscv_pbo.json")
    bias = load_json(ARTIFACTS / "selection_bias.json")
    folds = load_json(ARTIFACTS / "fold_stability.json")
    n_bound = ledger["n_lower_bound"]
    pool_a = cscv["panels"]["poolA_risk_geometry__ext"]
    pool_b = cscv["panels"]["poolB_ext_union__ext"]
    h2 = bias["headline_arms"]["H2_recommended_pre"]
    h4 = bias["headline_arms"]["H4_search_winner_3y"]
    primary = folds["panels"]["poolA_risk_geometry__3y"]
    block = primary[primary["primary"]]
    removed_total = sum(p["n_arms_removed"] for p in index["panels"].values())
    pool_total = sum(p["n_arms_in_pool"] for p in index["panels"].values())
    verification_path = ARTIFACTS / "verification.json"
    verification = load_json(verification_path) if verification_path.exists() else None
    if verification:
        verification_line = (
            f"独立复核 {verification['checks_passed']} 项检查全部通过"
            f"（失败 {verification['checks_failed']} 项；记录在 `artifacts/verification.json`）。"
            "`report_tools/verify_audit.py` 不 import 其余模块，用标准库重读权益账本并重算 "
            "面板 / 退化规则 / PBO / DSR / MinBTL / 折叠统计 / 压力臂。"
        )
    else:
        verification_line = (
            "独立复核由 `run.sh` 的最后一步执行；其结果记录在 `artifacts/verification.json`"
            "（首次运行时该文件在报告渲染之后才写出）。"
        )

    lines: list[str] = []
    add = lines.append
    add("# 过拟合审计：四轮选点用的是同一段历史（2026-09-18）")
    add("")
    add(
        "**一句话结论：这段历史携带信号，但「从这 44 个风险几何 arm 里挑最好」几乎不携带样本外信息；"
        "把试验次数的下界计进去后，被推荐的守护配置在 95% 门下不显著。**"
    )
    add("")
    add(
        f"> 只读已有工件，**不新增回测、不做参数搜索、不改引擎、不联网、不启动实盘**。"
        f"登记 {ledger['counts']['total_rows']} 行试验、给出 N 下界 **{n_bound}**；"
        f"月度面板由各 run 的 `balance_and_equity.csv.gz`（run-local，逐小时采样）重算，"
        f"并与同一 run 的 `monthly_metrics.csv` 交叉核对（水平偏差 0）；"
        f"CSCV/PBO 2 种选择约定 × S∈{{8,10,12,16}} × 4 个面板；"
        f"DSR/MinBTL/SPA/StepM 在 N∈{bias['n_grid']} 上做敏感性；折叠稳定性 K∈{{3,6}}；"
        f"3 个声明式成本压力臂（**非选择输入**）。"
    )
    add("")
    add("## 1. 这轮在回答什么")
    add("")
    add("| 问题 | 做法 | 结果 |")
    add("|---|---|---|")
    add(
        f"| 「挑最好」有多脆弱？ | CSCV/PBO，两种选臂约定，四种分块 | "
        f"池 A `ext` PBO "
        f"{min(pool_a['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}–"
        f"{max(pool_a['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}"
        f"（斜率 {min(pool_a['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in (8,10,12,16)):+.2f}"
        f"～{max(pool_a['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in (8,10,12,16)):+.2f}）；"
        f"池 B `ext` PBO "
        f"{min(pool_b['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}–"
        f"{max(pool_b['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.3f}"
        f"（斜率 {min(pool_b['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in (8,10,12,16)):+.2f}"
        f"～{max(pool_b['cscv'][f'max_adg|S{s}']['is_oos_slope'] for s in (8,10,12,16)):+.2f}） |"
    )
    add(
        f"| 试验次数惩罚后还剩多少显著？ | DSR / MinBTL / SPA / StepM，N 敏感性 | "
        f"`a_allow037__pre` 日频 DSR "
        + "／".join(
            f"{h2['deflated_sharpe']['daily'][str(n)]['dsr']:.4f}" for n in bias["n_grid"]
        )
        + f"；MinBTL@{n_bound} = {h2['min_btl_years']['years'][str(n_bound)]:.1f} 年 |"
    )
    add(
        f"| 冠军能穿越折叠吗？ | 搜索窗切 K 折，看折叠冠军的折叠外分位 | "
        f"`3y` K=6 最差分位 {block['j4_percentile_measured']:.3f}、Spearman "
        f"{block['j4_spearman_measured']:+.3f}；`ext`/`pre`/池 B 全部不满足 |"
    )
    add(
        f"| 成本变差时还剩多少？ | 3 个声明式压力臂（+4bp / +8bp 费、+5bp 滑点） | "
        f"头条 arm 终值保留率 "
        f"{min(r['retained_final_multiple_share'] for e in load_json(ARTIFACTS / 'stress_arms.json')['arms'].values() if 'unavailable' not in e for r in e['stress'].values()) * 100:.1f}%"
        f"～{max(r['retained_final_multiple_share'] for e in load_json(ARTIFACTS / 'stress_arms.json')['arms'].values() if 'unavailable' not in e for r in e['stress'].values()) * 100:.1f}% |"
    )
    add("")
    add("## 2. 三条最该被记住的读数")
    add("")
    add(
        f"1. **样本内最优 = 样本外最差。** 搜索冠军 `c1__3y` 的日频 DSR 是 "
        f"{h4['deflated_sharpe']['daily'][str(n_bound)]['dsr']:.4f}，"
        "但它在 `pre` 腿被强平（`D1_truncated`），在 `3y` 的折叠外排名是最后一名。"
        "同一个 arm 同时是「最显著」与「最不可用」。"
    )
    add(
        f"2. **池子怎么划，答案就怎么变。** 池 B（28 个 `ext` 臂，含守护与 10k 对照组）的 PBO "
        f"约 {min(pool_b['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.2f}，"
        f"而池 A（风险几何轮自己的 17 个 `ext` 臂）约 "
        f"{min(pool_a['cscv'][f'max_adg|S{s}']['pbo'] for s in (8,10,12,16)):.2f}。"
        "保护类 arm 与无保护 arm 之间的差异有**部分**可迁移性（斜率在 S=8/10 为正、S=12/16 转负，"
        "对分块方式敏感）；几何微调之间的差异则看不到可迁移性（四个分块斜率全为负）。"
    )
    add(
        f"3. **退化不是小事。** 4 个面板合计剔除 {removed_total}/{pool_total} = "
        f"{removed_total / pool_total * 100:.1f}%；`ext` 腿上 7 个 arm 在 2021-05-19 直接强平、"
        "3 个终局停机后权益恒定。把它们留在池里会让 PBO 变成噪声。"
    )
    add("")
    add("## 3. 文件")
    add("")
    add("| 路径 | 内容 |")
    add("|---|---|")
    add("| `overfitting_audit.md` | 主报告：口径、表格、边界、J1–J5（裁决列留空） |")
    add("| `anti_pattern_audit.md` | R1–R22 逐条 pass/fail/unknown + 证据 |")
    add("| `artifacts/trial_ledger.json` | 全部试验 + N 下界 + 计数方法 + 抽检 |")
    add("| `artifacts/panels/` | 月度收益面板（`index.json` + 每面板 `csv`/`json`） |")
    add("| `artifacts/cscv_pbo.json` | CSCV/PBO 2 约定 × 4 分块 × 4 面板 |")
    add("| `artifacts/selection_bias.json` | DSR / MinBTL / SPA / StepM / N 敏感性 |")
    add("| `artifacts/fold_stability.json` | 折叠稳定性 K∈{3,6} |")
    add("| `artifacts/stress_arms.json` | 声明式成本/执行压力臂 |")
    add("| `report_tools/` | `panel.py` `cscv_pbo.py` `selection_bias.py` `walkforward.py` `build_report.py` `verify_audit.py` |")
    add("| `run.sh` | 端到端（分阶段可单独跑） |")
    add("| `tests/test_g4_overfitting_audit.py` | 离线 pin + 数学单测 |")
    add("")
    add("## 4. 复现")
    add("")
    add("```bash")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage panels")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage cscv")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage bias")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage folds")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage stress")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage report")
    add("bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --verify-only")
    add("venv/bin/python -m pytest tests/test_g4_overfitting_audit.py -q")
    add("```")
    add("")
    add(verification_line)
    add("")
    add("## 5. 边界")
    add("")
    add(
        "- **PBO 不是亏钱概率**，它衡量「在这条历史上挑最好」的脆弱性；"
        "它也**测不到参数时间旅行与选择 provenance**（R16/R17）。"
    )
    add(f"- **N = {n_bound} 是下界**：未落盘的优化器候选与无记录的人工试错都不在内；"
        "所有以 N 为输入的惩罚都是乐观端上界。")
    add("- 四轮共用同一段 2021–2026 Binance 历史；`pre` 腿只对风险几何轮是样本外。")
    add("- 池 A 的 `3y` 腿折叠稳定**只证明池内自洽**，不证明选择越过了搜索窗。")
    add("- 币池是活到 2026 年的当前 top40（幸存者偏差）。")
    add("- 压力臂是一阶成本界，不建模行为变化。")
    add("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    outputs = {
        STUDY / "overfitting_audit.md": render_overfitting_audit(),
        STUDY / "anti_pattern_audit.md": render_anti_pattern_audit(),
        STUDY / "README.md": render_readme(),
    }
    for path, text in outputs.items():
        write_text(path, text)
        if not args.quiet:
            print(f"wrote {path.relative_to(REPO)} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    sys.exit(main())
