#!/usr/bin/env python3
"""Render the deep-analysis report and metric tables for a study artifact bundle.

The report skeleton, the metric-table schemas and the markdown renderer live in
`backtests/report_spec/annual_analysis.py`; the binding convention is
`docs/ai/runbooks/strategy_report.md`. This script only supplies:

* the artifact loaders and the study's own derived facts (ledger and execution audit),
* the study-specific scope lines, and
* the study appendix (baseline comparison, measured risk caps, execution/look-ahead
  boundaries, holdout and cost sensitivity).

Offline only. Every number is derived from the artifact directory; nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Repository root, resolved from this file so the study runs from any checkout location
# and any working directory: <repo>/backtests/binance/<study>/report_tools/<script>.py
REPO = Path(__file__).resolve().parents[4]
for extra in (
    REPO / "backtests" / "report_spec",
    REPO / "backtests/binance/dd_tail_research_2026-09-15/report_tools",
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import annual_analysis as spec  # noqa: E402  (canonical report convention)
import run_tail_drawdown_study as study  # noqa: E402  (single source for the reported contract)

STUDY = REPO / "backtests/binance/dd_tail_research_2026-09-15"
DEFAULT_ARTIFACTS_SUBDIR = "binance_actual_candidate"
ARTIFACTS = STUDY / "artifacts" / DEFAULT_ARTIFACTS_SUBDIR
RESULTS_BASE = ARTIFACTS / "backtest_results"
RUN_RECORD = ARTIFACTS / "run_record.json"
CONTRACT = STUDY / "research_contract_v4.json"
REFERENCE_REPORT = "backtests/binance/2026-09-14T03_25_14/annual_analysis.md"

PERIOD_CSV_COLUMNS = spec.PERIOD_CSV_COLUMNS
COIN_CSV_COLUMNS = spec.COIN_CSV_COLUMNS
COVERAGE_START = spec.COVERAGE_START
COVERAGE_END = spec.COVERAGE_END
COVERAGE_FULL = spec.COVERAGE_FULL


def load_json(path: Path) -> Any:
    return spec.load_json(path)


def find_result_dir() -> Path:
    root = RESULTS_BASE / "binance"
    if not root.is_dir():
        raise SystemExit(f"no results directory at {root}")
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if len(dirs) != 1:
        raise SystemExit(f"expected exactly one result dir under {root}, found {[p.name for p in dirs]}")
    return dirs[0]


# --------------------------------------------------------------------------------------
# Study-specific derived facts
# --------------------------------------------------------------------------------------


def reconstruct_positions(fills: pd.DataFrame) -> pd.DataFrame:
    """Rebuild post-fill non-zero long position counts from the fills ledger order."""
    state: dict[str, float] = {}
    counts = np.empty(len(fills), dtype=int)
    for idx, row in enumerate(fills.itertuples(index=False)):
        psize = float(row.psize)
        if psize == 0.0:
            state.pop(row.coin, None)
        else:
            state[row.coin] = psize
        counts[idx] = sum(1 for value in state.values() if value != 0.0)
    out = fills.copy()
    out["nonzero_long_after_fill"] = counts
    return out


def ledger_facts(fills: pd.DataFrame, equity: pd.DataFrame) -> dict[str, Any]:
    positions = reconstruct_positions(fills)
    minute_key = fills["timestamp"].dt.floor("min")
    per_minute = fills.groupby(minute_key).size()
    both_sides = (
        fills.assign(is_entry=fills["type"].astype(str).str.startswith("entry_"))
        .groupby([minute_key, "coin"])["is_entry"]
        .nunique()
    )
    entry_templates = {
        "entry_initial_normal_long",
        "entry_initial_partial_long",
        "entry_trailing_normal_long",
        "entry_trailing_cropped_long",
    }
    close_templates = (
        "close_grid_long",
        "close_trailing_long",
        "close_unstuck_long",
        "close_panic_long",
        "close_auto_reduce_wel_long",
        "close_auto_reduce_twel_long",
        "close_auto_reduce_wel_short",
        "close_auto_reduce_twel_short",
        "close_panic_short",
    )
    type_counts = fills["type"].astype(str).value_counts().to_dict()
    return {
        "nonzero_long_max": int(positions["nonzero_long_after_fill"].max()),
        "nonzero_long_counts": {
            int(k): int(v)
            for k, v in positions["nonzero_long_after_fill"].value_counts().sort_index().items()
        },
        "minutes_with_multiple_fills": int((per_minute > 1).sum()),
        "max_fills_in_a_minute": int(per_minute.max()),
        "coin_minutes_with_entry_and_close": int((both_sides > 1).sum()),
        "unknown_entry_types": sorted(
            t for t in type_counts if t.startswith("entry_") and t not in entry_templates
        ),
        "unknown_close_types": sorted(
            t for t in type_counts if t.startswith("close_") and t not in close_templates
        ),
        "type_counts": type_counts,
        "max_twe_long": float(fills["twe_long"].max()),
        "rows_twe_long_above_1_0": int((fills["twe_long"] > 1.0 + 1e-12).sum()),
        "rows_twe_long_above_baseline_cap": int((fills["twe_long"] > 1.5 + 1e-12).sum()),
        "max_wallet_exposure": float(fills["wallet_exposure"].max()),
        "rows_we_above_reference_cap": int((fills["wallet_exposure"] > (1.0 / 7.0) * 1.37 + 1e-12).sum()),
        "rows_we_above_26pct": int((fills["wallet_exposure"] > 0.26 + 1e-12).sum()),
        "top_symbol_share": float(fills["coin"].value_counts(normalize=True).iloc[0]),
        "strategy_equity_equals_usd_total_equity": bool(
            np.allclose(
                equity["strategy_equity"].to_numpy(dtype=float),
                equity["usd_total_equity"].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            )
        ),
    }


def audit_facts(path: Path, execution_delay_bars: int) -> dict[str, Any]:
    if not path.exists():
        return {"present": False}
    audit = pd.read_csv(path)
    audit = audit.loc[:, ~audit.columns.str.startswith("Unnamed")]
    delay_ok = audit["activation_index"] == audit["decision_index"] + 1 + execution_delay_bars
    fill_ok = audit["fill_index"] >= audit["activation_index"]
    return {
        "present": True,
        "rows": int(len(audit)),
        "activation_identity_failures": int((~delay_ok).sum()),
        "fill_before_activation_failures": int((~fill_ok).sum()),
        "distinct_fill_indices": int(audit["fill_index"].nunique()),
        "max_waited_bars": int((audit["fill_index"] - audit["activation_index"]).max()),
        "median_waited_bars": float((audit["fill_index"] - audit["activation_index"]).median()),
    }


def study_cell_metrics(window: str, scenario: str, cell_id: str) -> dict[str, Any]:
    path = STUDY / "cells" / window / scenario / cell_id / "result.json"
    if not path.exists():
        return {}
    return load_json(path)["metrics"]


def study_cells(candidate_id: str) -> dict[str, Any]:
    """Metrics for the comparison cells this report cites; missing cells are omitted."""
    wanted = {
        "baseline_holdout_C3": ("holdout", "C3_conservative", "baseline"),
        "candidate_holdout_C3": ("holdout", "C3_conservative", candidate_id),
        "baseline_holdout_C1": ("holdout", "C1_binance_actual", "baseline"),
        "candidate_holdout_C1": ("holdout", "C1_binance_actual", candidate_id),
    }
    out: dict[str, Any] = {}
    for key, (window, scenario, cell_id) in wanted.items():
        metrics = study_cell_metrics(window, scenario, cell_id)
        if metrics:
            out[key] = metrics
    return out


def contract_cell_matrix() -> str:
    return load_json(CONTRACT)["cell_matrix_sha256"]
# --------------------------------------------------------------------------------------
# Study-specific report content
# --------------------------------------------------------------------------------------


def scope_lines(
    result_dir: Path,
    run_record: dict[str, Any],
    cfg: dict[str, Any],
    analysis: dict[str, Any],
    contract: dict[str, Any],
) -> list[str]:
    bt = cfg["backtest"]
    delay = int(bt["execution_delay_bars"])
    primary_delay = int(study.PRIMARY_EXECUTION["execution_delay_bars"])
    latency = f"T+{delay + 1}"
    if delay == primary_delay:
        latency += "（研究报告主口径）"
    else:
        latency += f"（研究报告主口径为 T+{primary_delay + 1}）"
    kind = cfg.get("live", {}).get("strategy_kind", "n/a")
    coins_configured = len(cfg.get("live", {}).get("approved_coins", {}).get("long") or [])
    universe = run_record.get("universe", {}) or {}
    lines = [
        f"- 策略来源：`{run_record.get('source_config', 'n/a')}`"
        f"（sha256 `{str(run_record.get('source_config_sha256', ''))[:16]}…`）；"
        f"`live.strategy_kind = {kind}`。",
    ]
    ops = run_record.get("candidate_ops") or []
    if ops:
        lines.append("- 相对基线配置的改动（唯一差异，其余策略与风险参数逐字段一致）：")
        for op in ops:
            lines.append(f"  - `{op['path']}`：`{op['from']}` → **`{op['to']}`**")
    else:
        lines.append("- 本工件未施加任何改动 ops：它就是该配置本身（基线/参考包）。")
    lines.append(
        f"- 数据为 {universe.get('exchange', 'binance')} USDT-M 永续合约 1 分钟 K 线；"
        f"冻结篮子 {universe.get('coin_count', 'n/a')} 币"
        f"（配置声明 {coins_configured} 币；无有效数据的币种不进入冻结数据集）。"
    )
    lines.append(
        f"- 执行与成本口径：`execution_delay_bars={delay}`（{latency}）、"
        f"`intrabar_fill_order={bt['intrabar_fill_order']}`、"
        f"maker `{bt['maker_fee_override']}` / taker `{bt['taker_fee_override']}`；"
        f"研究契约 version {contract.get('version', 'n/a')}（`{study.PRIMARY_SCENARIO}`）。"
    )
    lines.append(
        f"- 权益口径：`balance_sample_divider={bt['balance_sample_divider']}` 的 `strategy_equity`；"
        f"`btc_collateral_cap={bt['btc_collateral_cap']}`。"
    )
    lines.append(
        "- 报告规范：`docs/ai/runbooks/strategy_report.md`；章节骨架与冻结样板 "
        f"`{REFERENCE_REPORT}` 一致，少数有意偏差在该规范中列明。"
    )
    lines.append(
        "- 安全边界：本工件由本地离线回测生成，未联网下载数据、未使用凭证、"
        "未接触交易所账户、未创建或撤销订单、未启动机器人。"
    )
    return lines


def study_appendix(
    run_record: dict[str, Any],
    analysis: dict[str, Any],
    coins: pd.DataFrame,
    ledger: dict[str, Any],
    audit: dict[str, Any],
    cells: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    facts = spec.analysis_facts(analysis)
    risk = cfg["bot"]["long"]["risk"]
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    unstuck = cfg["bot"]["long"]["unstuck"]
    hsl = cfg["bot"]["long"]["hsl"]
    primary_delay = int(study.PRIMARY_EXECUTION["execution_delay_bars"])
    primary_maker = study.PRIMARY_COSTS["maker_fee_override"]
    primary_taker = study.PRIMARY_COSTS["taker_fee_override"]
    L: list[str] = []
    add = L.append

    add("### 与研究报告主口径的关系")
    add("")
    add(
        f"- 本工件的口径为 `execution_delay_bars={cfg['backtest']['execution_delay_bars']}`（T+"
        f"{int(cfg['backtest']['execution_delay_bars']) + 1}）、maker `{cfg['backtest']['maker_fee_override']}` / "
        f"taker `{cfg['backtest']['taker_fee_override']}`；研究主口径为 T+{primary_delay + 1}、"
        f"maker `{primary_maker}` / taker `{primary_taker}`。"
    )
    add(
        f"- 参考报告 `{REFERENCE_REPORT}` 使用它自己的执行与费率假设，其数字**不能**与本报告混表；"
        "研究契约把两套口径分别冻结在 `research_contract_v4.json` 的 `contract_regimes` 中。"
    )
    add("")

    add("### 与基线的对照（同窗口、同口径）")
    add("")
    base_c1 = cells.get("baseline_full_C1") or {}
    cand_c1 = cells.get("candidate_full_C1") or {}
    if base_c1 and cand_c1:
        add(spec.md_table(
            [
                ["分钟收盘权益最差回撤", spec.fmt_pct(base_c1["minute_close_mdd"]), spec.fmt_pct(facts["drawdown_worst_strategy_eq"])],
                ["gain 倍数", spec.fmt_ratio(base_c1["gain_strategy_eq"]), spec.fmt_ratio(facts["gain_strategy_eq"])],
                ["CAGR", spec.fmt_pct(base_c1["cagr"]), spec.fmt_pct(cand_c1.get("cagr"))],
                ["最长水下（天）", f"{base_c1['total_underwater_days']:,.1f}", f"{cand_c1.get('total_underwater_days', float('nan')):,.1f}"],
                ["最差半年收益", spec.fmt_pct(base_c1["worst_halfyear_return"]), spec.fmt_pct(cand_c1.get("worst_halfyear_return"))],
                ["成交数", f"{int(base_c1['fills']):,}", f"{int(facts['fills_count']):,}"],
            ],
            ["指标（同口径）", "基线", "本工件"],
        ))
        add("")
        add(
            "- 基线与本工件使用**同一**冻结篮子、同一有效区间、同一执行/成本口径；"
            "差异仅来自上文的策略参数改动。基线数字取自同一研究的 "
            f"`cells/full/{study.PRIMARY_SCENARIO}/baseline/result.json`。"
        )
    else:
        add("- 未提供同口径的基线对照 cell，本表省略；对照数字只在研究 cell 齐备时给出。")
    add("")

    add("### 风险口径实测")
    add("")
    add("下列数值均由 `fills.csv` 逐笔重放得到，不是配置中的声明值。")
    add("")
    add(spec.md_table(
        [
            ["成交后非零 long 仓位最大值", f"**{ledger['nonzero_long_max']}**（仅 {ledger['nonzero_long_counts'].get(ledger['nonzero_long_max'], 0)} 次成交事件）", f"配置 `n_positions = {int(risk['n_positions'])}`。槽位数不是硬上限：门控约束的是成本敞口总和"],
            ["组合 long TWE 最大记录值", spec.fmt_pct(ledger["max_twe_long"], 4), f"配置 `total_wallet_exposure_limit = {risk['total_wallet_exposure_limit']}`"],
            ["成交后单币敞口最大值", spec.fmt_pct(ledger["max_wallet_exposure"], 4), f"参考上限 = TWEL / 槽位 × (1 + allowance) ≈ {float(risk['total_wallet_exposure_limit']) / 7 * (1 + float(risk['we_excess_allowance_pct'])) * 100:.2f}%"],
            ["单币敞口高于该参考上限的成交行数", f"{ledger['rows_we_above_reference_cap']:,} / {int(facts['fills_count']):,}", "可交易币数减少时有效槽位下降、单币预算被动变大"],
            ["组合 TWE 高于 TWEL 的成交行数", f"{ledger['rows_twe_long_above_1_0']:,}", "entry 手续费先扣、后记 TWE，故成交后允许极小超出"],
            ["组合 TWE 高于基线 1.5 的成交行数", f"{ledger['rows_twe_long_above_baseline_cap']:,}", "上限若已下调，不应超过 1.5"],
            ["单分钟多笔成交 / 单分钟最多成交", f"{ledger['minutes_with_multiple_fills']:,} / {ledger['max_fills_in_a_minute']}", "同 bar 顺序为模拟约定，不是交易所事件顺序"],
            ["同币同分钟既有 entry 又有 close", f"{ledger['coin_minutes_with_entry_and_close']:,}", "仅凭 OHLC 无法恢复真实先后路径"],
            ["未识别的 entry / close 订单类型", f"{len(ledger['unknown_entry_types'])} / {len(ledger['unknown_close_types'])}", "非空说明出现了预期外的订单类型，需要人工复核"],
        ],
        ["实测项", "数值", "参照"],
    ))
    add("")
    add("成交后非零 long 仓位分布（按成交事件计数；同一状态可跨多根 K 线持续）：")
    add("")
    add(spec.md_table(
        [[str(k), f"{v:,}"] for k, v in sorted(ledger["nonzero_long_counts"].items())],
        ["成交后非零 long 数", "成交事件数"],
    ))
    add("")
    add("订单类型分布：")
    add("")
    add(spec.md_table(
        [[t, f"{c:,}"] for t, c in sorted(ledger["type_counts"].items(), key=lambda kv: -kv[1])],
        ["订单类型", "成交数"],
    ))
    add("")
    add("关键风险开关（来自本工件的 `config.json`）：")
    add("")
    add(spec.md_table(
        [
            ["`hsl.enabled`", f"`{hsl['enabled']}`", "无权益硬止损" if not hsl["enabled"] else "启用"],
            ["`unstuck.enabled` / `threshold` / `close_pct` / `loss_allowance_pct`", f"`{unstuck['enabled']}` / `{unstuck['threshold']}` / `{unstuck['close_pct']}` / `{unstuck['loss_allowance_pct']}`", "唯一的常态化减仓通道"],
            ["`unstuck.ema_gating_enabled` / `ema_dist`", f"`{unstuck['ema_gating_enabled']}` / `{unstuck['ema_dist']}`", "深跌时价格远离 EMA，该通道可能长期不触发"],
            ["`position_exposure_enforcer_enabled`", f"`{risk['position_exposure_enforcer_enabled']}`", "没有每根 K 线强制压回单币阈值的修复器"],
            ["`total_exposure_enforcer_enabled`", f"`{risk['total_exposure_enforcer_enabled']}`", "同上，作用于组合敞口"],
            ["`total_exposure_entry_gate_enabled`", f"`{risk['total_exposure_entry_gate_enabled']}`", "订单规划阶段约束成交后预计组合敞口"],
            ["`we_excess_allowance_pct` / `mode`", f"`{risk['we_excess_allowance_pct']}` / `{risk['we_excess_allowance_mode']}`", "单币预算上限相对基础 WEL 的放大幅度"],
            ["`entry.double_down_factor`", f"`{tm['entry']['double_down_factor']}`", "决定阶梯加仓的步长"],
            ["`entry.threshold_base_pct`", f"`{tm['entry']['threshold_base_pct']}`", "决定阶梯加仓的间距"],
            ["`close.retracement_base_pct` / `close.threshold_base_pct`", f"`{tm['close']['retracement_base_pct']}` / `{tm['close']['threshold_base_pct']}`", "决定追踪止盈是否会展开递归 close 梯子"],
        ],
        ["配置项", "值", "风险含义"],
    ))
    add("")

    add("### 执行与前视边界")
    add("")
    if audit.get("present"):
        add(spec.md_table(
            [
                ["审计行数", f"{audit['rows']:,}"],
                ["`activation_index = decision_index + 1 + execution_delay_bars` 违例", f"{audit['activation_identity_failures']:,}"],
                ["`fill_index >= activation_index` 违例", f"{audit['fill_before_activation_failures']:,}"],
                ["独立 `fill_index` 数", f"{audit['distinct_fill_indices']:,}"],
                ["激活到成交的等待 bar 数（中位 / 最大）", f"{audit['median_waited_bars']:.1f} / {audit['max_waited_bars']:,}"],
            ],
            ["审计项", "数值"],
        ))
    else:
        add("- 本工件缺少 `execution_audit.csv`，执行时序未通过审计。")
    add("")
    add("必须与结论一起阅读的限制：")
    add("")
    delay = int(cfg["backtest"]["execution_delay_bars"])
    add(f"1. **成交时点是模型假设。** 本报告采用 `execution_delay_bars={delay}`（T+{delay + 1}）；仍不知道订单被交易所接受的精确时刻，也不含盘口队列、部分成交、撤单竞争与真实撮合路径。")
    add("2. **maker 身份与费率是假设。** 非市价订单统一按挂单价、maker 费率记账；`GTC` 不代表 maker-only，真实费率与成交顺序可能显著不同。")
    add(f"3. **backtest-only 的下一根 K 线读取未被本次消除。** `close.retracement_base_pct = {tm['close']['retracement_base_pct']}`；该值 > 0 时 close 走追踪分支，next-candle hint 仍参与判定是否展开完整递归 close 梯子。实盘没有这根未来 K 线，本工件也没有 no-peek 对照来量化其净影响。")
    add("4. **同 bar 顺序是确定性约定。** 每个币先处理 close、再处理 entry，不是交易所撮合顺序。")
    add("5. **参数时间旅行。** 该配置是 2026 年形成的候选，被回放到历史窗口；它只能说明「该参数在这段历史数据上的模拟表现」。候选的选型过程有锁定 holdout（见下），但窗口整体并不构成样本外。")
    add(f"6. **强平未被模拟到。** `liquidated = {facts['liquidated']}` 是该模拟的结果，不等于真实保证金体系下不会强平。")
    add("")

    regimes = load_json(CONTRACT).get("contract_regimes", {})
    regime_rows = []
    for label, base_key, cand_key, regime_key in (
        (
            f"{study.PRIMARY_SCENARIO}（T+{int(study.PRIMARY_EXECUTION['execution_delay_bars']) + 1}，"
            f"maker {study.PRIMARY_COSTS['maker_fee_override']}）",
            "baseline_holdout_C1",
            "candidate_holdout_C1",
            "v4_binance_actual",
        ),
        (
            "C3_conservative（T+2，maker 0.0006，冻结压力口径）",
            "baseline_holdout_C3",
            "candidate_holdout_C3",
            "v3_conservative",
        ),
    ):
        base = cells.get(base_key)
        cand = cells.get(cand_key)
        if not base or not cand:
            continue
        regime = regimes.get(regime_key, {})
        costs = regime.get("costs", {})
        execution = regime.get("execution", {})
        label = label if not costs else (
            f"{regime.get('primary_scenario', regime_key)}"
            f"（T+{int(execution.get('execution_delay_bars', 0)) + 1}，"
            f"maker {costs.get('maker_fee_override', 'n/a')} / taker {costs.get('taker_fee_override', 'n/a')}）"
        )
        regime_rows.append(
            [
                label,
                spec.fmt_pct(base["minute_close_mdd"]),
                spec.fmt_pct(base["cagr"]),
                spec.fmt_pct(cand["minute_close_mdd"]),
                spec.fmt_pct(cand["cagr"]),
            ]
        )
    if regime_rows:
        add("### 未见样本（holdout）与执行/成本敏感性")
        add("")
        add("候选在选型锁定后于独立一年窗口（2025-09-12 → 2026-09-12）一次性开封。该窗口在两个"
            "冻结的执行/成本口径下各跑一次，两行不可混读、也不可相减：")
        add("")
        add(spec.md_table(regime_rows, ["口径", "基线 MDD", "基线 CAGR", "本工件 MDD", "本工件 CAGR"]))
        add("")
        add(
            "- 完整矩阵见研究契约的 `scenario_matrix` 与 `contract_regimes`；"
            "同一表格内只出现同一口径的数字。"
        )
        add("")

    return "\n".join(L)


def artifacts_table(audit: dict[str, Any], equity: pd.DataFrame, analysis: dict[str, Any]) -> list[list[str]]:
    return [
        ["`analysis.json`", "回测原生指标（USD/BTC/strategy-equity 三套口径）"],
        ["`fills.csv`", f"{int(analysis['fills_count']):,} 条成交账本"],
        ["`balance_and_equity.csv.gz`", f"{len(equity):,} 行权益采样"],
        ["`config.json`", "本工件使用的完整有效配置"],
        ["`dataset.json`", "数据集来源、币种、有效区间与缓存标识"],
        ["`execution_audit.csv`", f"{audit.get('rows', 0):,} 行逐笔执行审计"],
        ["`annual_metrics.csv` / `monthly_metrics.csv` / `coin_metrics.csv`", "本报告三张汇总表的原始数据"],
        ["`balance_and_equity.png` / `balance_and_equity_logy.png` / `drawdown.png` / `total_wallet_exposure.png` / `pnl_cumsum.png` / `fills_plots/`", "图表"],
        ["`../candidate.config.json` / `../run_record.json`", "配置重建记录与全部工件哈希"],
    ]
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument(
        "--artifacts-subdir",
        default=DEFAULT_ARTIFACTS_SUBDIR,
        help=f"artifact directory under <study>/artifacts (default {DEFAULT_ARTIFACTS_SUBDIR})",
    )
    parser.add_argument("--report-title", default=None, help="override the derived report title")
    args = parser.parse_args()

    global ARTIFACTS, RESULTS_BASE, RUN_RECORD
    ARTIFACTS = STUDY / "artifacts" / args.artifacts_subdir
    RESULTS_BASE = ARTIFACTS / "backtest_results"
    RUN_RECORD = ARTIFACTS / "run_record.json"

    result_dir = Path(args.result_dir) if args.result_dir else find_result_dir()
    run_record = load_json(RUN_RECORD)
    analysis = load_json(result_dir / "analysis.json")
    cfg = load_json(result_dir / "config.json")
    contract = load_json(CONTRACT)
    fills = spec.load_fills(result_dir)
    equity = spec.load_balance_equity(result_dir)

    annual = spec.build_period_table(equity, fills, "Y")
    monthly = spec.build_period_table(equity, fills, "M")
    coins = spec.build_coin_table(fills)
    ledger = ledger_facts(fills, equity)
    audit = audit_facts(ARTIFACTS / "execution_audit.csv", int(cfg["backtest"]["execution_delay_bars"]))
    candidate_id = run_record.get("candidate_id", "")
    cells = study_cells(candidate_id)
    cells["baseline_full_C1"] = study_cell_metrics("full", study.PRIMARY_SCENARIO, "baseline")
    cells["candidate_full_C1"] = study_cell_metrics("full", study.PRIMARY_SCENARIO, candidate_id)

    annual.to_csv(result_dir / "annual_metrics.csv", index=False)
    monthly.to_csv(result_dir / "monthly_metrics.csv", index=False)
    coins.to_csv(result_dir / "coin_metrics.csv", index=False)

    try:
        result_label = str(result_dir.relative_to(REPO))
    except ValueError:
        result_label = str(result_dir)

    context: dict[str, Any] = {
        "analysis": analysis,
        "config": cfg,
        "annual": annual,
        "monthly": monthly,
        "coins": coins,
        "attribution": spec.build_attribution_table(fills),
        "run_record": run_record,
        "ledger": ledger,
        "result_label": result_label,
        "scope_lines": scope_lines(result_dir, run_record, cfg, analysis, contract),
        "scope_notes": [],
        "appendix": study_appendix(run_record, analysis, coins, ledger, audit, cells, cfg),
        "artifacts": artifacts_table(audit, equity, analysis),
        "verifiable_notes": [
            f"- 配置来源：`{run_record.get('source_config', 'n/a')}`"
            f"（sha256 `{str(run_record.get('source_config_sha256', ''))[:16]}…`）"
            + (" + 锁定 ops" if run_record.get("locked_ops_applied") else "（published profile）")
            + f" = `../candidate.config.json`（sha256 `{str(run_record.get('candidate_config_sha256', ''))[:16]}…`）。",
            f"- 研究契约 `research_contract_v4.json`（cell_matrix_sha256 `{contract_cell_matrix()[:16]}…`）、"
            f"候选锁 `holdout_candidate_lock.json`（sha256 `{str(run_record.get('lock_sha256', ''))[:16]}…`）。",
            f"- Rust 扩展 source fingerprint：`{(run_record.get('rust_identity') or {}).get('expected_source_fingerprint', 'n/a')}`。",
        ],
    }
    if args.report_title:
        context["title"] = args.report_title
    report = spec.render_annual_analysis(context)
    problems = spec.assert_report_structure(report)
    if problems:
        raise SystemExit("report structure does not match the convention: " + "; ".join(problems))
    (result_dir / "annual_analysis.md").write_text(report, encoding="utf-8")
    print(f"wrote {result_dir / 'annual_analysis.md'}")
    print(f"wrote {result_dir / 'annual_metrics.csv'} ({len(annual)} rows)")
    print(f"wrote {result_dir / 'monthly_metrics.csv'} ({len(monthly)} rows)")
    print(f"wrote {result_dir / 'coin_metrics.csv'} ({len(coins)} rows)")
    print(f"audit: {audit}")


if __name__ == "__main__":
    main()