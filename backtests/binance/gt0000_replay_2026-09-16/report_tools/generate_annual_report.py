#!/usr/bin/env python3
"""Render the g3_cth0000 deep-analysis report and its three metric tables.

The report skeleton, the table schemas and the markdown renderer live in
`backtests/report_spec/annual_analysis.py`; the binding convention is
`docs/ai/runbooks/strategy_report.md`. This script supplies only:

* the artifact loaders and the facts derived from the replay's own ledger,
* the study-specific scope lines, and
* the study appendix (declared change, baseline comparison, execution audit, ledger).

Every number is read from the run directory or from an explicitly cited study cell;
nothing is hardcoded. Offline only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cell_spec as study  # noqa: E402

REPORT_SPEC_DIR = study.REPO / "backtests" / "report_spec"
if str(REPORT_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(REPORT_SPEC_DIR))
import annual_analysis as spec  # noqa: E402  (canonical report convention)

#: Ledger order types the study expects; anything else is reported as unrecognised.
ENTRY_TEMPLATES = {
    "entry_initial_normal_long",
    "entry_initial_partial_long",
    "entry_grid_normal_long",
    "entry_trailing_normal_long",
    "entry_trailing_cropped_long",
    "entry_initial_normal_short",
    "entry_initial_partial_short",
    "entry_grid_normal_short",
    "entry_trailing_normal_short",
    "entry_trailing_cropped_short",
}
CLOSE_TEMPLATES = {
    "close_grid_long",
    "close_trailing_long",
    "close_unstuck_long",
    "close_panic_long",
    "close_auto_reduce_wel_long",
    "close_auto_reduce_twel_long",
    "close_panic_short",
    "close_auto_reduce_wel_short",
    "close_auto_reduce_twel_short",
}

REQUIRED_ARTIFACTS = (
    "analysis.json",
    "config.json",
    "dataset.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
    "execution_audit.csv",
)


def load_json(path: Path) -> Any:
    return spec.load_json(path)


def find_run_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    if not study.RUNS_BASE.is_dir():
        raise SystemExit(f"no run directory under {study.RUNS_BASE}")
    runs = sorted(
        path
        for path in study.RUNS_BASE.iterdir()
        if path.is_dir() and path.name[:2].isdigit()
    )
    if not runs:
        raise SystemExit(f"no dated run directory under {study.RUNS_BASE}")
    if len(runs) != 1:
        raise SystemExit(
            f"expected exactly one run directory under {study.RUNS_BASE}, found "
            f"{[path.name for path in runs]}; pass --result-dir"
        )
    return runs[0]


def require_artifacts(result_dir: Path) -> None:
    missing = [name for name in REQUIRED_ARTIFACTS if not (result_dir / name).exists()]
    fallback = study.ARTIFACTS / "execution_audit.csv"
    if "execution_audit.csv" in missing and fallback.exists():
        missing.remove("execution_audit.csv")
    if missing:
        raise SystemExit(f"run directory {result_dir} is missing artifacts: {missing}")


def audit_path_for(result_dir: Path) -> Path:
    local = result_dir / "execution_audit.csv"
    if local.exists():
        return local
    return study.ARTIFACTS / "execution_audit.csv"


def fmt_ratio_value(value: Any, digits: int = 4) -> str:
    """Wallet-exposure style ratios read as `1.0000`, not `100.0000%`.

    Exposure limits are configured as bare ratios (`total_wallet_exposure_limit = 1.0`),
    so the report compares like with like instead of mixing percent and multiple forms.
    """
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not np.isfinite(number):
        return "n/a"
    return f"{number:.{digits}f}"


# --------------------------------------------------------------------------------------
# Facts derived from the replay ledger
# --------------------------------------------------------------------------------------


def direction_from_type(order_type: Any) -> str:
    text = str(order_type)
    if "long" in text:
        return "long"
    if "short" in text:
        return "short"
    raise ValueError(f"cannot derive position side from order type {text!r}")


def reconstruct_positions(fills: pd.DataFrame) -> pd.DataFrame:
    """Rebuild the post-fill non-zero position count per side from the ledger order.

    `fills.csv` carries no `pside` column, so the side is read from the order type
    (`entry_*_long` / `close_*_long`), which is the same convention the report uses for
    direction attribution.
    """
    state: dict[tuple[str, str], float] = {}
    long_counts = np.empty(len(fills), dtype=int)
    for idx, row in enumerate(fills.itertuples(index=False)):
        side = direction_from_type(row.type)
        key = (row.coin, side)
        size = float(row.psize)
        if size == 0.0:
            state.pop(key, None)
        else:
            state[key] = size
        long_counts[idx] = sum(
            1 for (coin, position_side), value in state.items() if position_side == "long" and value
        )
    out = fills.copy()
    out["nonzero_long_after_fill"] = long_counts
    return out


def ledger_facts(fills: pd.DataFrame, equity: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, Any]:
    positions = reconstruct_positions(fills)
    minute_key = fills["timestamp"].dt.floor("min")
    per_minute = fills.groupby(minute_key).size()
    both_sides = (
        fills.assign(is_entry=fills["type"].astype(str).str.startswith("entry_"))
        .groupby([minute_key, "coin"])["is_entry"]
        .nunique()
    )
    type_counts = fills["type"].astype(str).value_counts().to_dict()
    risk = cfg["bot"]["long"]["risk"]
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    single_coin_cap = float(risk["total_wallet_exposure_limit"]) / float(risk["n_positions"]) * (
        1.0 + float(risk["we_excess_allowance_pct"])
    )
    wallet_exposure = fills["wallet_exposure"].abs()
    return {
        "nonzero_long_max": int(positions["nonzero_long_after_fill"].max()),
        "nonzero_long_counts": {
            int(key): int(value)
            for key, value in positions["nonzero_long_after_fill"].value_counts().sort_index().items()
        },
        "minutes_with_multiple_fills": int((per_minute > 1).sum()),
        "max_fills_in_a_minute": int(per_minute.max()),
        "coin_minutes_with_entry_and_close": int((both_sides > 1).sum()),
        "unknown_entry_types": sorted(
            t for t in type_counts if str(t).startswith("entry_") and t not in ENTRY_TEMPLATES
        ),
        "unknown_close_types": sorted(
            t for t in type_counts if str(t).startswith("close_") and t not in CLOSE_TEMPLATES
        ),
        "type_counts": type_counts,
        "max_twe_long": float(fills["twe_long"].max()),
        "max_twe_short": float(fills["twe_short"].max()),
        "rows_twe_long_above_limit": int(
            (fills["twe_long"] > float(risk["total_wallet_exposure_limit"]) + 1e-12).sum()
        ),
        "max_abs_wallet_exposure": float(wallet_exposure.max()),
        "single_coin_cap": single_coin_cap,
        "rows_we_above_reference_cap": int((wallet_exposure > single_coin_cap + 1e-12).sum()),
        "top_symbol_share": float(fills["coin"].value_counts(normalize=True).iloc[0]),
        "close_threshold_base_pct": float(tm["close"]["threshold_base_pct"]),
        "entry_threshold_base_pct": float(tm["entry"]["threshold_base_pct"]),
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
        return {"present": False, "path": str(path)}
    audit = pd.read_csv(path)
    audit = audit.loc[:, ~audit.columns.str.startswith("Unnamed")]
    delay_ok = audit["activation_index"] == audit["decision_index"] + 1 + execution_delay_bars
    fill_ok = audit["fill_index"] >= audit["activation_index"]
    return {
        "present": True,
        "path": study.relative(path),
        "rows": int(len(audit)),
        "activation_identity_failures": int((~delay_ok).sum()),
        "fill_before_activation_failures": int((~fill_ok).sum()),
        "distinct_fill_indices": int(audit["fill_index"].nunique()),
        "max_waited_bars": int((audit["fill_index"] - audit["activation_index"]).max()),
        "median_waited_bars": float((audit["fill_index"] - audit["activation_index"]).median()),
    }


def baseline_facts() -> dict[str, Any]:
    """Frozen baseline numbers, read from the baseline run's own analysis.json."""
    analysis = load_json(study.BASELINE_ANALYSIS)
    config = load_json(study.BASELINE_CONFIG)
    tm = config["bot"]["long"]["strategy"]["trailing_martingale"]
    risk = config["bot"]["long"]["risk"]
    return {
        "label": study.BASELINE_LABEL,
        "analysis": analysis,
        "total_wallet_exposure_limit": float(risk["total_wallet_exposure_limit"]),
        "double_down_factor": float(tm["entry"]["double_down_factor"]),
        "entry_threshold_base_pct": float(tm["entry"]["threshold_base_pct"]),
        "close_threshold_base_pct": float(tm["close"]["threshold_base_pct"]),
    }


# --------------------------------------------------------------------------------------
# Study-specific report content
# --------------------------------------------------------------------------------------


def scope_lines(
    result_dir: Path,
    record: dict[str, Any],
    global_metrics: dict[str, Any],
    cfg: dict[str, Any],
) -> list[str]:
    bt = cfg["backtest"]
    delay = int(bt["execution_delay_bars"])
    kind = cfg.get("live", {}).get("strategy_kind", "n/a")
    approved = cfg.get("live", {}).get("approved_coins", {}) or {}
    universe = record.get("universe", {})
    contract = record.get("contract", {})
    ops = record.get("candidate_ops") or []
    engine = record.get("engine", {})
    git = engine.get("git", {})
    modified = len(git.get("status_porcelain") or [])
    lines = [
        f"- 策略来源：`{record.get('source_config', 'n/a')}`"
        f"（sha256 `{str(record.get('source_config_sha256', ''))[:16]}…`），"
        f"`live.strategy_kind = {kind}`；本工件是该 profile 上 `{study.CELL_GROUP}` 组"
        f" `{study.CELL_ID}` 这一格的**完整回测工件**。",
        f"- 运行配置逐字段取自该研究格的 `{study.relative(study.CELL_RESULT_PATH)}`"
        f"（sha256 `{study.sha256_file(study.CELL_RESULT_PATH)[:16]}…`），并冻结为 "
        f"`{record.get('candidate_config_path')}`"
        f"（sha256 `{str(record.get('candidate_config_sha256', ''))[:16]}…`）；"
        "未按种子 profile 重新推导，以保证与已记录的一格逐字节一致。",
    ]
    if ops:
        lines.append("- 相对其种子 profile 的唯一改动（其余字段逐条一致，已由门控校验）：")
        for op in ops:
            lines.append(f"  - `{op['path']}`：`{op['from']}` → **`{op['to']}`**")
    lines.append(
        f"- 数据为 {universe.get('exchange', 'binance')} USDT-M 永续合约 1 分钟 K 线；"
        f"冻结篮子 {universe.get('coin_count', 'n/a')} 币（配置声明 "
        f"{len(approved.get('long') or [])} 币；无有效数据的币种不进入冻结数据集）。"
        f"数据集为 `{global_metrics['dataset']['cache_dir']}`"
        f"（`config_hash = {str(global_metrics['dataset'].get('manifest_config_hash'))[:16]}…`），"
        "本次回放未重建、未下载任何 K 线。"
    )
    lines.append(
        f"- 执行与成本口径：`execution_delay_bars={delay}`（T+{delay + 1}，"
        "研究报告主口径）、"
        f"`intrabar_fill_order={bt['intrabar_fill_order']}`、"
        f"maker `{bt['maker_fee_override']}` / taker `{bt['taker_fee_override']}`；"
        f"研究契约 version {contract.get('version', 'n/a')}"
        f"（`{contract.get('primary_scenario', study.SCENARIO)}`，"
        f"`cell_matrix_sha256 = {str(contract.get('cell_matrix_sha256', ''))[:16]}…`）。"
    )
    lines.append(
        f"- 权益口径：`balance_sample_divider={bt['balance_sample_divider']}` 的 `strategy_equity`；"
        f"`btc_collateral_cap={bt['btc_collateral_cap']}`。"
        f"报告规范：`{study.REPORT_CONVENTION}`；章节骨架严格按该规范渲染，"
        "并按该规范在期末表中额外给出 `active_coins` 列。"
    )
    lines.append(
        f"- 引擎溯源：本次回放使用工作区当时的引擎，Rust source fingerprint "
        f"`{engine.get('expected_source_fingerprint')}`；已编译扩展 "
        f"`{engine_path_label(engine)}` "
        f"的 stamp 与之一致 = `{engine.get('source_fingerprint_matches_compiled_stamp')}`"
        f"（`src/backtest.py` 在导入时校验并会拒绝不一致的扩展）。工作区当时"
        f"{'含' if git.get('dirty') else '不含'}未提交改动（{modified} 项）。"
        f"基线 `{study.BASELINE_LABEL}` 由另一次引擎修订产出，两者不是同一次引擎运行。"
    )
    lines.append(
        "- 安全边界：本工件由本地离线回测生成，**未联网下载任何数据**、未使用凭证、"
        "未接触交易所账户、未创建或撤销订单、未启动机器人；"
        f"运行日志见 `{study.relative(study.REPLAY_LOG_PATH)}`。"
    )
    return lines


def study_appendix(
    record: dict[str, Any],
    global_metrics: dict[str, Any],
    analysis: dict[str, Any],
    coins: pd.DataFrame,
    ledger: dict[str, Any],
    audit: dict[str, Any],
    baseline: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    facts = spec.analysis_facts(analysis)
    risk = cfg["bot"]["long"]["risk"]
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    unstuck = cfg["bot"]["long"]["unstuck"]
    hsl = cfg["bot"]["long"]["hsl"]
    ops = record.get("candidate_ops") or []
    contract = record.get("contract", {})
    base = baseline["analysis"]
    L: list[str] = []
    add = L.append

    add("### 本次改动声明与研究契约")
    add("")
    add(
        f"- 该格属于 `{study.CELL_GROUP}`，窗口 `{study.CELL_WINDOW}`"
        f"（{(contract.get('window_full') or ['n/a', 'n/a'])[0]} → "
        f"{(contract.get('window_full') or ['n/a', 'n/a'])[1]}），"
        f"情景 `{contract.get('primary_scenario', study.SCENARIO)}`。"
    )
    add(
        f"- 研究契约：`{contract.get('path')}`（version {contract.get('version')}，"
        f"`cell_matrix_sha256 = {contract.get('cell_matrix_sha256')}`）。"
    )
    add("")
    add(
        spec.md_table(
            [
                [
                    f"`{op['path']}`",
                    f"`{op['from']}`",
                    f"**`{op['to']}`**",
                    "关闭信号的基准阈值；置 0 表示不再要求最小追踪回撤即可触发 close 梯子"
                    if op["path"].endswith("close.threshold_base_pct")
                    else "",
                ]
                for op in ops
            ],
            ["改动路径", "种子值", "本工件值", "含义"],
        )
    )
    add("")
    add(
        "- 本表是把该格相对其种子 profile 的全部改动；除此之外没有第二处差异"
        "（构建阶段的等价性门控逐路径校验过 `bot` 子树）。"
    )
    add("")

    add(f"### 与基线 {study.BASELINE_LABEL} 的同口径对照")
    add("")
    add(
        "两列使用同一冻结篮子、同一有效区间、同一执行/成本口径"
        f"（`execution_delay_bars={int(cfg['backtest']['execution_delay_bars'])}`、"
        f"`intrabar_fill_order={cfg['backtest']['intrabar_fill_order']}`、"
        f"maker `{cfg['backtest']['maker_fee_override']}` / taker "
        f"`{cfg['backtest']['taker_fee_override']}`）。"
    )
    add("")
    add(
        spec.md_table(
            [
                [
                    "USD 最差回撤",
                    spec.fmt_pct(base["drawdown_worst_usd"]),
                    spec.fmt_pct(facts["drawdown_worst_usd"]),
                ],
                [
                    "strategy equity 最差回撤",
                    spec.fmt_pct(base["drawdown_worst_strategy_eq"]),
                    spec.fmt_pct(facts["drawdown_worst_strategy_eq"]),
                ],
                [
                    "最差 1% 均值回撤",
                    spec.fmt_pct(base["drawdown_worst_mean_1pct_strategy_eq"]),
                    spec.fmt_pct(facts["drawdown_worst_mean_1pct_strategy_eq"]),
                ],
                [
                    "`gain_usd` 倍数",
                    spec.fmt_ratio(base["gain_usd"], 6),
                    spec.fmt_ratio(facts["gain_usd"], 6),
                ],
                [
                    "`gain_strategy_eq` 倍数",
                    spec.fmt_ratio(base["gain_strategy_eq"], 6),
                    spec.fmt_ratio(facts["gain_strategy_eq"], 6),
                ],
                [
                    "PnL Sharpe / Sortino",
                    f"{spec.fmt_ratio(base['sharpe_ratio_pnl'])} / {spec.fmt_ratio(base['sortino_ratio_pnl'])}",
                    f"{spec.fmt_ratio(facts['sharpe_ratio_pnl'])} / {spec.fmt_ratio(facts['sortino_ratio_pnl'])}",
                ],
                [
                    "最长 PnL 峰值恢复期（天）",
                    f"{float(base['strategy_eq_recovery_days_max']):,.2f}",
                    f"{float(facts['strategy_eq_recovery_days_max']):,.2f}",
                ],
                [
                    "最长持仓（天）",
                    f"{float(base['position_held_days_max']):,.2f}",
                    f"{float(facts['position_held_days_max']):,.2f}",
                ],
                [
                    "成交：总 / 入场 / 减仓或平仓",
                    f"{int(base['fills_count']):,} / {int(base['fills_count_entry']):,} / {int(base['fills_count_close']):,}",
                    f"{int(facts['fills_count']):,} / {int(facts['fills_count_entry']):,} / {int(facts['fills_count_close']):,}",
                ],
                [
                    "组合 TWE 最大记录值",
                    fmt_ratio_value(base["total_wallet_exposure_max"]),
                    fmt_ratio_value(analysis.get("total_wallet_exposure_max")),
                ],
                [
                    "强平",
                    "是" if base["liquidated"] else "否",
                    "是" if facts["liquidated"] else "否",
                ],
            ],
            ["指标（同口径）", "基线 2026-09-14T03_40_41", f"本工件 {study.CELL_ID}"],
        )
    )
    add("")
    add(
        "- 口径细节：两列的 `drawdown_worst_*`、`gain_*`、`sharpe_ratio_pnl`、`sortino_ratio_pnl`、"
        "`strategy_eq_recovery_days_max` 均取自各自 `analysis.json`；"
        "`PnL Sharpe / Sortino` 两行是 **PnL** 比率，不能与本研究 cell 使用的 "
        "`sortino_ratio_strategy_eq` 混读。"
    )
    add(
        f"- 基线列取自 `{study.BASELINE_LABEL}/analysis.json`；两列的执行与成本口径相同，"
        "但策略参数不同：基线配置为 `total_wallet_exposure_limit = "
        f"{baseline['total_wallet_exposure_limit']}`、`double_down_factor = "
        f"{baseline['double_down_factor']}`、`entry.threshold_base_pct = "
        f"{baseline['entry_threshold_base_pct']}`、`close.threshold_base_pct = "
        f"{baseline['close_threshold_base_pct']}`；本工件为 "
        f"`{risk['total_wallet_exposure_limit']}` / `{tm['entry']['double_down_factor']}` / "
        f"`{tm['entry']['threshold_base_pct']}` / `{tm['close']['threshold_base_pct']}`。"
    )
    add(
        "- 两列也不共享引擎修订：基线由另一次构建产出。本表用于显示数量级与方向，"
        "不能当成单变量对照实验。"
    )
    add("")

    add("### 逐笔执行审计与时序")
    add("")
    if audit.get("present"):
        add(f"审计文件：`{audit['path']}`（{audit['rows']:,} 行）。")
        add("")
        add(
            spec.md_table(
                [
                    [
                        "`activation_index = decision_index + 1 + execution_delay_bars` 违例",
                        f"{audit['activation_identity_failures']:,}",
                    ],
                    [
                        "`fill_index >= activation_index` 违例",
                        f"{audit['fill_before_activation_failures']:,}",
                    ],
                    ["独立 `fill_index` 数", f"{audit['distinct_fill_indices']:,}"],
                    [
                        "激活到成交的等待 bar 数（中位 / 最大）",
                        f"{audit['median_waited_bars']:.1f} / {audit['max_waited_bars']:,}",
                    ],
                ],
                ["审计项", "数值"],
            )
        )
        add("")
        add(
            "- 两条恒等式的违例都必须为 0；否则说明订单在生成它的 bar 内就成交了，"
            "该工件不能作为因果执行证据。"
        )
    else:
        add("- 本工件缺少逐笔执行审计文件，执行时序未通过审计。")
    add("")

    add("### 资金账本实测")
    add("")
    add("下列数值均由 `fills.csv` 逐笔重放得到，不是配置中的声明值。")
    add("")
    add(
        spec.md_table(
            [
                [
                    "成交后非零 long 仓位最大值",
                    f"**{ledger['nonzero_long_max']}**"
                    f"（仅 {ledger['nonzero_long_counts'].get(ledger['nonzero_long_max'], 0)} 次成交事件）",
                    f"配置 `n_positions = {int(risk['n_positions'])}`；槽位数不是硬上限，"
                    "门控约束的是成交后预计成本敞口总和",
                ],
                [
                    "组合 long TWE 最大记录值",
                    fmt_ratio_value(ledger["max_twe_long"]),
                    f"配置 `total_wallet_exposure_limit = {risk['total_wallet_exposure_limit']}`",
                ],
                [
                    "组合 TWE 高于该上限的成交行数",
                    f"{ledger['rows_twe_long_above_limit']:,}",
                    "entry 手续费先扣、后记 TWE，成交后允许极小超出",
                ],
                [
                    "成交时单币绝对敞口最大值",
                    spec.fmt_pct(ledger["max_abs_wallet_exposure"], 4),
                    f"参考上限 = TWEL / 槽位 × (1 + allowance) ≈ {ledger['single_coin_cap']:.4f}",
                ],
                [
                    "单币敞口高于该参考上限的成交行数",
                    f"{ledger['rows_we_above_reference_cap']:,} / {int(facts['fills_count']):,}",
                    "可交易币数减少时有效槽位下降、单币预算被动变大",
                ],
                [
                    "单分钟多笔成交 / 单分钟最多成交",
                    f"{ledger['minutes_with_multiple_fills']:,} / {ledger['max_fills_in_a_minute']}",
                    "同 bar 顺序是模拟约定，不是交易所事件顺序",
                ],
                [
                    "同币同分钟既有 entry 又有 close",
                    f"{ledger['coin_minutes_with_entry_and_close']:,}",
                    "仅凭 OHLC 无法恢复同 bar 内真实先后路径",
                ],
                [
                    "未识别的 entry / close 订单类型",
                    f"{len(ledger['unknown_entry_types'])} / {len(ledger['unknown_close_types'])}",
                    "非空说明出现了预期外的订单类型，需要人工复核",
                ],
                [
                    "最高贡献币种成交占比",
                    spec.fmt_pct(ledger["top_symbol_share"]),
                    "集中度参考；逐币种结果受候选资格与上市时间影响",
                ],
                [
                    "`strategy_equity` 与 `usd_total_equity` 全程相等",
                    "是" if ledger["strategy_equity_equals_usd_total_equity"] else "否",
                    "为否时报告中的 strategy equity 口径不能直接当 USD 权益读",
                ],
            ],
            ["实测项", "数值", "参照"],
        )
    )
    add("")
    add("成交后非零 long 仓位分布（按成交事件计数；同一状态可跨多根 K 线持续）：")
    add("")
    add(
        spec.md_table(
            [[str(key), f"{value:,}"] for key, value in sorted(ledger["nonzero_long_counts"].items())],
            ["成交后非零 long 数", "成交事件数"],
        )
    )
    add("")
    add("订单类型分布：")
    add("")
    add(
        spec.md_table(
            [[t, f"{c:,}"] for t, c in sorted(ledger["type_counts"].items(), key=lambda kv: -kv[1])],
            ["订单类型", "成交数"],
        )
    )
    add("")
    add("关键风险开关（来自本工件的 `config.json`）：")
    add("")
    add(
        spec.md_table(
            [
                ["`hsl.enabled`", f"`{hsl['enabled']}`", "无权益硬止损" if not hsl["enabled"] else "启用"],
                [
                    "`unstuck.enabled` / `threshold` / `close_pct` / `loss_allowance_pct`",
                    f"`{unstuck['enabled']}` / `{unstuck['threshold']}` / `{unstuck['close_pct']}` / `{unstuck['loss_allowance_pct']}`",
                    "唯一的常态化减仓通道",
                ],
                [
                    "`unstuck.ema_gating_enabled` / `ema_dist`",
                    f"`{unstuck['ema_gating_enabled']}` / `{unstuck['ema_dist']}`",
                    "深跌时价格远离 EMA，该通道可能长期不触发",
                ],
                [
                    "`position_exposure_enforcer_enabled` / `total_exposure_enforcer_enabled`",
                    f"`{risk['position_exposure_enforcer_enabled']}` / `{risk['total_exposure_enforcer_enabled']}`",
                    "均为 false 时没有逐 K 线强制压回阈值的修复器",
                ],
                [
                    "`total_exposure_entry_gate_enabled`",
                    f"`{risk['total_exposure_entry_gate_enabled']}`",
                    "订单规划阶段约束成交后预计组合敞口",
                ],
                [
                    "`we_excess_allowance_pct` / `mode`",
                    f"`{risk['we_excess_allowance_pct']}` / `{risk['we_excess_allowance_mode']}`",
                    "单币预算上限相对基础 WEL 的放大幅度",
                ],
                [
                    "`entry.double_down_factor` / `entry.threshold_base_pct`",
                    f"`{tm['entry']['double_down_factor']}` / `{tm['entry']['threshold_base_pct']}`",
                    "决定阶梯加仓的步长与间距",
                ],
                [
                    "`close.retracement_base_pct` / `close.threshold_base_pct`",
                    f"`{tm['close']['retracement_base_pct']}` / `{tm['close']['threshold_base_pct']}`",
                    "决定追踪止盈是否会展开递归 close 梯子",
                ],
            ],
            ["配置项", "值", "风险含义"],
        )
    )
    add("")

    add("### 引擎与可重复性")
    add("")
    engine = record.get("engine", {})
    git = engine.get("git", {})
    add(
        spec.md_table(
            [
                ["Rust source fingerprint", f"`{engine.get('expected_source_fingerprint')}`"],
                [
                    "已编译扩展 stamp 与源码一致",
                    f"`{engine.get('source_fingerprint_matches_compiled_stamp')}`",
                ],
                [
                    "已编译扩展",
                    f"`{engine_path_label(engine)}`",
                ],
                ["git HEAD", f"`{git.get('head')}`（`{git.get('branch')}`）"],
                [
                    "工作区当时有未提交改动",
                    f"`{git.get('dirty')}`" + (f"（{len(git.get('status_porcelain') or [])} 项）" if git.get("dirty") else ""),
                ],
                [
                    "回测耗时 / 峰值内存",
                    f"{float(global_metrics.get('backtest_elapsed_s') or 0.0):.1f} s / "
                    f"{float(record.get('peak_rss_gb') or 0.0):.2f} GB"
                    if record.get("peak_rss_gb")
                    else f"{float(global_metrics.get('backtest_elapsed_s') or 0.0):.1f} s",
                ],
            ],
            ["项", "值"],
        )
    )
    add("")
    add(
        "- 重跑命令：`venv/bin/python "
        f"{study.relative(study.STUDY / 'report_tools/run_replay.py')}` 后接 "
        f"`generate_annual_report.py` 与 `verify_replay_report.py`；"
        "回放要求命中冻结数据集，未命中即中止。"
    )
    add("")

    add("### 必须与结论一起阅读的限制")
    add("")
    delay = int(cfg["backtest"]["execution_delay_bars"])
    add(
        f"1. **成交时点是模型假设。** 本工件采用 `execution_delay_bars={delay}`（T+{delay + 1}）；"
        "不含盘口队列、部分成交、撤单竞争与真实撮合路径。"
    )
    add(
        "2. **maker 身份与费率是假设。** 非市价订单统一按挂单价、maker 费率记账；"
        "`GTC` 不代表 maker-only。"
    )
    add(
        f"3. **backtest-only 的下一根 K 线读取未被本次消除。** "
        f"`close.retracement_base_pct = {tm['close']['retracement_base_pct']}`；"
        "该值 > 0 时 close 走追踪分支，next-candle hint 仍参与判定是否展开完整递归 close 梯子。"
    )
    add(
        "4. **同 bar 顺序是确定性约定。** 每个币先处理 close、再处理 entry，"
        "不是交易所撮合顺序；上表给出了同分钟多笔与同币同分钟 entry+close 的实测数量。"
    )
    add(
        "5. **参数时间旅行。** 该 profile 与本次改动都形成于 2026 年，被回放到 2023-09 起的历史；"
        "只能说明“该参数在这段历史数据上的模拟表现”。"
    )
    add(
        f"6. **强平未被模拟到。** `liquidated = {facts['liquidated']}` 是模拟结果，"
        "不等于真实保证金体系下不会强平。"
    )
    add(
        "7. **本工件是单格回放，不是研究结论本身。** 该格是否优于其他格、"
        "是否通过研究契约的 gate，以 "
        f"`{study.relative(study.SOURCE_STUDY)}` 的对照结果为准；本报告不重复其选型结论。"
    )
    add("")
    return "\n".join(L)


def artifacts_table(
    result_dir: Path, audit: dict[str, Any], equity: pd.DataFrame, analysis: dict[str, Any]
) -> list[list[str]]:
    rows = [
        ["`analysis.json`", "回测原生指标（USD/BTC/strategy-equity 三套口径）"],
        ["`fills.csv`", f"{int(analysis['fills_count']):,} 条成交账本"],
        ["`balance_and_equity.csv.gz`", f"{len(equity):,} 行权益采样"],
        ["`config.json`", "本工件使用的完整有效配置"],
        ["`dataset.json`", "数据集来源、币种、有效区间与缓存标识"],
        ["`run_record.json`", "格定义、改动 ops、引擎 fingerprint 与数据哈希"],
        ["`global_metrics.json`", "引擎、环境、git 状态与冻结点数据 sha256"],
        [
            f"`{audit.get('path', 'execution_audit.csv')}`",
            f"{audit.get('rows', 0):,} 行逐笔执行审计" if audit.get("present") else "缺失",
        ],
        [
            "`annual_metrics.csv` / `monthly_metrics.csv` / `coin_metrics.csv`",
            "本报告三张汇总表的原始数据",
        ],
        [
            "`balance_and_equity.png` / `balance_and_equity_logy.png` / `drawdown.png` / "
            "`total_wallet_exposure.png` / `pnl_cumsum.png` / `fills_plots/`",
            "图表",
        ],
    ]
    return rows


def engine_path_label(engine: dict[str, Any]) -> str:
    """The compiled extension behind this run, stated repository-relative.

    A tracked report must not carry the generating host's absolute path, and the extension
    normally sits in the checkout's own `venv`, so the relative form is the informative one.
    `study.relative` returns the recorded path unchanged when it lies outside the repository, and
    `n/a` stands in when the run recorded none.
    """
    recorded = engine.get("compiled_path") or engine.get("preferred_compiled_path")
    if not recorded:
        return "n/a"
    return study.relative(Path(str(recorded)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument("--report-title", default=None)
    args = parser.parse_args()

    result_dir = find_run_dir(args.result_dir)
    require_artifacts(result_dir)

    record = load_json(result_dir / "run_record.json")
    global_metrics = load_json(result_dir / "global_metrics.json")
    analysis = load_json(result_dir / "analysis.json")
    cfg = load_json(result_dir / "config.json")
    contract = record.get("contract", {})
    fills = spec.load_fills(result_dir)
    equity = spec.load_balance_equity(result_dir)

    annual = spec.build_period_table(equity, fills, "Y")
    monthly = spec.build_period_table(equity, fills, "M")
    coins = spec.build_coin_table(fills)
    ledger = ledger_facts(fills, equity, cfg)
    audit = audit_facts(audit_path_for(result_dir), int(cfg["backtest"]["execution_delay_bars"]))
    baseline = baseline_facts()

    annual.to_csv(result_dir / "annual_metrics.csv", index=False)
    monthly.to_csv(result_dir / "monthly_metrics.csv", index=False)
    coins.to_csv(result_dir / "coin_metrics.csv", index=False)

    try:
        result_label = str(result_dir.relative_to(study.REPO))
    except ValueError:
        result_label = str(result_dir)

    dataset = global_metrics["dataset"]
    dataset_hashes_match = all(
        dataset["data_hashes"].get(key) == dataset["manifest_hashes"].get(key)
        for key in ("hlcvs", "timestamps", "btc_usd_prices")
    )
    equity_lo = equity["timestamp"].iloc[0]
    equity_hi = equity["timestamp"].iloc[-1]
    outside_mask = (fills["timestamp"] < equity_lo) | (fills["timestamp"] > equity_hi)
    outside_sample_fills = int(outside_mask.sum())
    outside_sample_pnl = float(
        fills.loc[outside_mask, "pnl"].sum() + fills.loc[outside_mask, "fee_paid"].sum()
    )

    context: dict[str, Any] = {
        "analysis": analysis,
        "config": cfg,
        "annual": annual,
        "monthly": monthly,
        "coins": coins,
        "attribution": spec.build_attribution_table(fills),
        "run_record": record,
        "ledger": ledger,
        "result_label": result_label,
        "scope_lines": scope_lines(result_dir, record, global_metrics, cfg),
        "appendix": study_appendix(
            record, global_metrics, analysis, coins, ledger, audit, baseline, cfg
        ),
        "artifacts": artifacts_table(result_dir, audit, equity, analysis),
        "verifiable_notes": [
            f"- 权益采样端点：`balance_and_equity.csv.gz` 覆盖 "
            f"{equity['timestamp'].iloc[0]} → {equity['timestamp'].iloc[-1]}（{len(equity):,} 行）；"
            f"`fills.csv` 有 {outside_sample_fills} 笔成交发生在最后一个采样点之后"
            f"（合计净 {outside_sample_pnl:+.6f} USDT），因此年度/月度表的净已实现 PnL 合计"
            "不等于全账本合计，两者差值只能由该端点差解释。",
            f"- 格定义：`{study.relative(study.CELL_RESULT_PATH)}`"
            f"（sha256 `{study.sha256_file(study.CELL_RESULT_PATH)[:16]}…`）"
            f" + `{record.get('candidate_config_path')}`"
            f"（sha256 `{str(record.get('candidate_config_sha256', ''))[:16]}…`）。",
            f"- 研究契约：`{contract.get('path')}`"
            f"（文件自带 `cell_matrix_sha256 = {contract.get('cell_matrix_sha256')}`）。"
            "契约的 `version`/`amendments` 在组合格追加后未同步重算，本报告只引用文件记录值，"
            "不据此宣称该哈希等于当前 cells 列表的内容哈希。",
            f"- 冻结数据集：`{dataset['cache_dir']}`；三份数据的逻辑数组哈希与 bundle "
            f"`manifest.json` 记录一致 = `{dataset_hashes_match}`"
            f"（`hlcvs` = `{str(dataset['data_hashes']['hlcvs'])[:16]}…`，"
            f"shape `{dataset['array_shapes']['hlcvs']}`；"
            "gzip 字节流的 sha256 只作溯源，见 `global_metrics.json`）。",
            f"- Rust source fingerprint：`{record.get('engine', {}).get('expected_source_fingerprint')}`"
            f"（已编译扩展 stamp 与源码一致 = "
            f"`{record.get('engine', {}).get('source_fingerprint_matches_compiled_stamp')}`；"
            "`src/backtest.py` 导入时会校验并拒绝不一致的扩展）。",
            f"- 运行日志：`{study.relative(study.REPLAY_LOG_PATH)}`；"
            "本报告与三张 CSV 的一致性由同目录 `report_tools/verify_replay_report.py` "
            "独立复算校验。",
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
    print(
        "ledger: nonzero_long_max="
        f"{ledger['nonzero_long_max']} twe_long_max={ledger['max_twe_long']:.6f} "
        f"audit_rows={audit.get('rows')}"
    )


if __name__ == "__main__":
    main()
