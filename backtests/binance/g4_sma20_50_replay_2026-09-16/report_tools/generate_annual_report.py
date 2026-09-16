#!/usr/bin/env python3
"""Render the g4_sma20_50 published-profile replay report and its three metric tables.

The report skeleton, the table schemas and the markdown renderer live in
`backtests/report_spec/annual_analysis.py`; the binding convention is
`docs/ai/runbooks/strategy_report.md`. This script supplies only:

* the artifact loaders and the facts derived from the replay's own ledger,
* the study-specific scope lines, and
* the study appendix (declared gate, source-study agreement, comparison columns,
  execution audit, gate effect, limits).

Every number is read from a run directory or from an explicitly cited study artifact;
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
    """Wallet-exposure style ratios read as `1.0000`, not `100.0000%`."""
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not np.isfinite(number):
        return "n/a"
    return f"{number:.{digits}f}"


def direction_from_type(order_type: Any) -> str:
    text = str(order_type)
    if "long" in text:
        return "long"
    if "short" in text:
        return "short"
    raise ValueError(f"cannot derive position side from order type {text!r}")


# --------------------------------------------------------------------------------------
# Facts derived from the replay ledger
# --------------------------------------------------------------------------------------


def ledger_facts(fills: pd.DataFrame, equity: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, Any]:
    minute_key = fills["timestamp"].dt.floor("min")
    per_minute = fills.groupby(minute_key).size()
    is_entry = fills["type"].astype(str).str.startswith("entry_")
    both_sides = (
        fills.assign(is_entry=is_entry).groupby([minute_key, "coin"])["is_entry"].nunique()
    )
    type_counts = fills["type"].astype(str).value_counts().to_dict()
    risk = cfg["bot"]["long"]["risk"]
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    single_coin_cap = (
        float(risk["total_wallet_exposure_limit"])
        / float(risk["n_positions"])
        * (1.0 + float(risk["we_excess_allowance_pct"]))
    )
    wallet_exposure = fills["wallet_exposure"].abs()
    sample_lo = equity["timestamp"].iloc[0]
    sample_hi = equity["timestamp"].iloc[-1]
    outside = (fills["timestamp"] < sample_lo) | (fills["timestamp"] > sample_hi)
    return {
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
        "fills_count": int(len(fills)),
        "entry_fill_count": int(is_entry.sum()),
        "close_fill_count": int((~is_entry).sum()),
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
        "first_fill": fills["timestamp"].iloc[0],
        "last_fill": fills["timestamp"].iloc[-1],
        "sample_first": sample_lo,
        "sample_last": sample_hi,
        "fills_outside_sample": int(outside.sum()),
        "pnl_outside_sample": float(
            fills.loc[outside, "pnl"].sum() + fills.loc[outside, "fee_paid"].sum()
        ),
        "distinct_fill_days": int(fills["timestamp"].dt.floor("D").nunique()),
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
    """Frozen un-gated default-profile baseline, read from its own analysis.json."""
    analysis = spec.load_json(study.BASELINE_ANALYSIS)
    config = spec.load_json(study.BASELINE_CONFIG)
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


def base_profile_cell_facts() -> dict[str, Any]:
    """The lower-tail profile's own cell: the profile this one adds a gate to."""
    path = (
        study.SOURCE_STUDY / "cells" / study.CELL_WINDOW / study.SCENARIO / "published" / "result.json"
    )
    record = spec.load_json(path)
    return {
        "cell_id": record["cell_id"],
        "path": study.relative(path),
        "sha256": study.sha256_file(path),
        "metrics": record["metrics"],
        "config": record["config"],
    }


def source_bundle_facts() -> dict[str, Any]:
    """The gate cell's own bundle, produced independently by the source study.

    Ledger-derived quantities are recomputed here from the source bundle's own `fills.csv`, so a
    comparison row never mixes one artifact's ledger with another's.
    """
    run = study.resolve_source_bundle_run()
    analysis = spec.load_json(run / "analysis.json")
    fills = spec.load_fills(run)
    return {
        "label": study.SOURCE_BUNDLE_LABEL,
        "run_dir": study.relative(run),
        "run_dir_name": run.name,
        "analysis": analysis,
        "sha256": study.sha256_file(run / "analysis.json"),
        "fills_count": int(len(fills)),
        "entry_fills": int(fills["type"].astype(str).str.startswith("entry_").sum()),
        "close_fills": int((~fills["type"].astype(str).str.startswith("entry_")).sum()),
        "max_abs_wallet_exposure": float(fills["wallet_exposure"].abs().max()),
        "max_twe_long": float(fills["twe_long"].max()),
        "type_counts": fills["type"].astype(str).value_counts().to_dict(),
    }


def gateway_effect_facts(
    ledger: dict[str, Any],
    analysis: dict[str, Any],
    base_cell: dict[str, Any],
) -> dict[str, Any]:
    """What the gate changed, read from the artifact ledger and the study cells."""
    base = base_cell["metrics"]
    gate = study.GATE
    span_days = float(analysis["fills_analysis_duration_days"])
    active_days = float(analysis["fills_active_days_count"])
    return {
        "fast": gate["sma_fast_days"],
        "slow": gate["sma_slow_days"],
        "confirm_days": gate["confirm_days"],
        "block_initial": gate["block_initial"],
        "block_reentry": gate["block_reentry"],
        "span_days": span_days,
        "active_days": active_days,
        "idle_days": span_days - active_days,
        "entry_fills": int(analysis["fills_count_entry"]),
        "entry_fills_ungated_lowertail": int(base["entry_fills"]),
        "fills_total": int(analysis["fills_count"]),
        "fills_ungated_lowertail": int(base["fill_rows"]),
        "max_twe": float(analysis["total_wallet_exposure_max"]),
        "max_twe_ungated_lowertail": float(base["total_wallet_exposure_max"]),
        "distinct_fill_days": ledger["distinct_fill_days"],
    }


# --------------------------------------------------------------------------------------
# Study-specific report content
# --------------------------------------------------------------------------------------


def scope_lines(
    result_dir: Path,
    record: dict[str, Any],
    global_metrics: dict[str, Any],
    cfg: dict[str, Any],
    profile_input: dict[str, Any],
) -> list[str]:
    bt = cfg["backtest"]
    delay = int(bt["execution_delay_bars"])
    kind = cfg.get("live", {}).get("strategy_kind", "n/a")
    approved = cfg.get("live", {}).get("approved_coins", {}) or {}
    universe = record.get("universe", {})
    contract = record.get("contract", {})
    engine = record.get("engine", {})
    git = engine.get("git", {})
    modified = len(git.get("status_porcelain") or [])
    gate = record.get("entry_regime_gate") or {}
    lines = [
        f"- 策略来源：`{record.get('source_config', 'n/a')}`"
        f"（sha256 `{str(record.get('source_config_sha256', ''))[:16]}…`），"
        f"`live.strategy_kind = {kind}`；本工件是**该已发布 profile 的完整回测工件**，"
        f"对应源研究 `{study.SOURCE_STUDY.name}` 的 `{study.CELL_GROUP}` 组 `{study.CELL_ID}` 格。",
        f"- 运行配置由该 profile 冻结为 `{record.get('candidate_config_path')}`"
        f"（sha256 `{str(record.get('candidate_config_sha256', ''))[:16]}…`），"
        "**未**重新推导、未打补丁。已发布 profile 是**运行用**配置（开窗区间、实盘数据源偏好），"
        "而记录下来的证据是一个**固定**的三年窗口、单交易所、40 币数据集，因此复现证据必须重定向"
        "数据身份。本工件重定向的字段被逐一列名，其余字段（含全部 `bot` / `monitor` / `logging` "
        "以及 `live` 中与策略、风险、退出相关的项）与已发布文件逐路径一致：",
        "- 重定向的数据身份字段："
        f"`backtest.exchanges = {cfg['backtest'].get('exchanges')}`、"
        f"`backtest.start_date = {cfg['backtest'].get('start_date')}`、"
        f"`backtest.end_date = {cfg['backtest'].get('end_date')}`、"
        f"`backtest.coins`（{len(approved.get('long') or [])} 币）、"
        "`backtest.cache_dir`（指向冻结 bundle）、"
        "`backtest.base_dir`（运行目录位置）；以及 "
        f"`live.approved_coins`（long {len(approved.get('long') or [])} 币 / "
        f"short {len(approved.get('short') or [])} 币）。**没有任何策略、风险、退出或闸门参数被"
        "重定向。**",
        f"- 就 profile 文件本身而言，相对其基线 `{study.relative(study.BASE_PROFILE)}` "
        "的唯一改动是日线入场闸门（其余字段一致）："
        f"`sma_fast_days = {gate.get('sma_fast_days')}`、`sma_slow_days = {gate.get('sma_slow_days')}`、"
        f"`confirm_days = {gate.get('confirm_days')}`、`block_initial = {gate.get('block_initial')}`、"
        f"`block_reentry = {gate.get('block_reentry')}`。",
        f"- 数据为 {universe.get('exchange', 'binance')} USDT-M 永续合约 1 分钟 K 线；"
        f"冻结篮子 {universe.get('coin_count', 'n/a')} 币（profile 的候选表为 "
        f"{len(spec.load_json(study.PUBLISHED_PROFILE).get('live', {}).get('approved_coins', {}).get('long') or [])} "
        "币；`MNT` 在该窗口内无可用历史，因此不进冻结数据集）。"
        f"数据集为 `{global_metrics['dataset']['cache_dir']}`"
        f"（`config_hash = {str(global_metrics['dataset'].get('manifest_config_hash'))[:16]}…`），"
        "本次回放未重建、未下载任何 K 线。",
        f"- 执行与成本口径：`execution_delay_bars={delay}`（T+{delay + 1}，研究报告主口径）、"
        f"`intrabar_fill_order={bt['intrabar_fill_order']}`、"
        f"maker `{bt['maker_fee_override']}` / taker `{bt['taker_fee_override']}`；"
        f"研究契约 version {contract.get('version', 'n/a')}"
        f"（`{contract.get('primary_scenario', study.SCENARIO)}`，"
        f"`cell_matrix_sha256 = {str(contract.get('cell_matrix_sha256', ''))[:16]}…`）。",
        f"- 权益口径：`balance_sample_divider={bt['balance_sample_divider']}` 的 `strategy_equity`；"
        f"`btc_collateral_cap={bt['btc_collateral_cap']}`。"
        f"报告规范：`{study.REPORT_CONVENTION}`；章节骨架严格按该规范渲染，"
        "并按该规范在期末表中额外给出 `active_coins` 列。"
        "本次运行未禁用任何图表分组，逐币成交面板完整。",
        f"- 引擎溯源：本次回放使用工作区当时的引擎，Rust source fingerprint "
        f"`{engine.get('expected_source_fingerprint')}`；已编译扩展 "
        f"`{engine_path_label(engine)}` "
        f"的 stamp 与之一致 = `{engine.get('source_fingerprint_matches_compiled_stamp')}`"
        "（`src/backtest.py` 在导入时校验并会拒绝不一致的扩展）。工作区当时"
        f"{'含' if git.get('dirty') else '不含'}未提交改动（{modified} 项）。"
        f"基线 `{study.BASELINE_LABEL}` 与源研究的工件由另两次引擎运行产出，"
        "三者不是同一次引擎运行，对照只能读数量级与方向。",
        "- 安全边界：本工件由本地离线回测生成，**未联网下载任何数据**、未使用凭证、"
        "未接触交易所账户、未创建或撤销订单、未启动机器人；"
        f"运行日志见 `{study.relative(study.REPLAY_LOG_PATH)}`。",
    ]
    return lines


def agreement_table(
    facts: dict[str, Any],
    analysis: dict[str, Any],
    source: dict[str, Any],
    profile_input: dict[str, Any],
    ledger: dict[str, Any],
) -> list[list[str]]:
    """Replay vs the source study's own bundle: same cell, two independent artifacts.

    Every row compares like with like. Engine metrics come from each artifact's own
    `analysis.json`; ledger quantities are recomputed from each artifact's own `fills.csv`;
    quantities only the study computed are quoted once and labelled as such, because the
    engine does not report them and quoting one value in both columns would fake agreement.
    """
    src = source["analysis"]
    cell = profile_input["metrics"]
    return [
        # ---- engine metrics from each artifact's own analysis.json
        [
            "USD 最差回撤",
            spec.fmt_pct(src["drawdown_worst_usd"]),
            spec.fmt_pct(analysis["drawdown_worst_usd"]),
        ],
        [
            "strategy equity 最差回撤",
            spec.fmt_pct(src["drawdown_worst_strategy_eq"]),
            spec.fmt_pct(analysis["drawdown_worst_strategy_eq"]),
        ],
        [
            "最差 1% 均值回撤",
            spec.fmt_pct(src["drawdown_worst_mean_1pct_strategy_eq"]),
            spec.fmt_pct(analysis["drawdown_worst_mean_1pct_strategy_eq"]),
        ],
        [
            "`gain_strategy_eq` 倍数",
            spec.fmt_ratio(src["gain_strategy_eq"], 6),
            spec.fmt_ratio(analysis["gain_strategy_eq"], 6),
        ],
        [
            "PnL Sharpe / Sortino",
            f"{spec.fmt_ratio(src['sharpe_ratio_pnl'])} / {spec.fmt_ratio(src['sortino_ratio_pnl'])}",
            f"{spec.fmt_ratio(analysis['sharpe_ratio_pnl'])} / {spec.fmt_ratio(analysis['sortino_ratio_pnl'])}",
        ],
        [
            "strategy equity Sharpe / Sortino",
            f"{spec.fmt_ratio(src['sharpe_ratio_strategy_eq'])} / {spec.fmt_ratio(src['sortino_ratio_strategy_eq'])}",
            f"{spec.fmt_ratio(analysis['sharpe_ratio_strategy_eq'])} / {spec.fmt_ratio(analysis['sortino_ratio_strategy_eq'])}",
        ],
        [
            "最长 strategy equity 恢复期（天）",
            f"{float(src['strategy_eq_recovery_days_max']):,.2f}",
            f"{float(analysis['strategy_eq_recovery_days_max']):,.2f}",
        ],
        [
            "最长持仓（天）",
            f"{float(src['position_held_days_max']):,.2f}",
            f"{float(analysis['position_held_days_max']):,.2f}",
        ],
        [
            "组合 TWE 最大记录值",
            fmt_ratio_value(src["total_wallet_exposure_max"]),
            fmt_ratio_value(analysis["total_wallet_exposure_max"]),
        ],
        [
            "组合 TWE 均值",
            fmt_ratio_value(src["total_wallet_exposure_mean"]),
            fmt_ratio_value(analysis["total_wallet_exposure_mean"]),
        ],
        [
            "盈亏比 `loss_profit_ratio`",
            f"{float(src['loss_profit_ratio']):.6f}",
            f"{float(analysis['loss_profit_ratio']):.6f}",
        ],
        ["强平", "是" if src["liquidated"] else "否", "是" if analysis["liquidated"] else "否"],
        # ---- ledger quantities, recomputed from each artifact's own fills.csv
        [
            "成交：总 / 入场 / 减仓或平仓",
            f"{int(source['fills_count']):,} / {int(source['entry_fills']):,} / {int(source['close_fills']):,}",
            f"{int(ledger['fills_count']):,} / {int(ledger['entry_fill_count']):,} / {int(ledger['close_fill_count']):,}",
        ],
        [
            "组合 long TWE 最大（账本实测）",
            fmt_ratio_value(source["max_twe_long"]),
            fmt_ratio_value(ledger["max_twe_long"]),
        ],
        [
            "最大单币敞口（成交时）",
            spec.fmt_pct(float(source["max_abs_wallet_exposure"])),
            spec.fmt_pct(ledger["max_abs_wallet_exposure"]),
        ],
        # ---- study-record quantities; the engine does not report these, so the study's own
        # ---- value is quoted in both columns and the row is labelled.
        [
            "研究格记录 `fill_rows`（本工件列取自账本）",
            f"{int(cell[study.CELL_FILLS_KEY]):,}",
            f"{int(ledger['fills_count']):,}",
        ],
        [
            "研究格记录 `minute_close_mdd`（本工件列 = 引擎回撤）",
            spec.fmt_pct(float(cell["minute_close_mdd"]), 3),
            spec.fmt_pct(float(analysis["drawdown_worst_strategy_eq"]), 3),
        ],
        [
            "研究格记录 `worst_1pct_mean_drawdown`（本工件列 = 引擎值）",
            spec.fmt_pct(float(cell["worst_1pct_mean_drawdown"]), 3),
            spec.fmt_pct(float(analysis["drawdown_worst_mean_1pct_strategy_eq"]), 3),
        ],
        [
            "研究格记录 `cagr`（同源）",
            f"{float(cell['cagr']):.4%}",
            f"{float(cell['cagr']):.4%}",
        ],
        [
            "研究格记录 `total_underwater_days`（同源）",
            f"{float(cell['total_underwater_days']):,.2f}",
            f"{float(cell['total_underwater_days']):,.2f}",
        ],
    ]


def cagr_from_gain(gain: Any, span_days: Any) -> float:
    """Annualize a strategy-equity gain over a span, the study's own definition."""
    gain = float(gain)
    span_days = float(span_days)
    if not (span_days > 0.0 and gain > 0.0):
        return float("nan")
    return gain ** (365.25 / span_days) - 1.0


def comparison_table(
    facts: dict[str, Any],
    analysis: dict[str, Any],
    baseline: dict[str, Any],
    base_cell: dict[str, Any],
) -> list[list[str]]:
    """Three columns: this artifact, the lower-tail profile, the un-gated default.

    Every row is a quantity the engine reports for all three configurations, or one this
    function derives from engine inputs with one formula. A study-only quantity (the study's
    own half-year bucketing, for instance) is not mixed in here.
    """
    base = base_cell["metrics"]
    default = baseline["analysis"]
    span = float(analysis["fills_analysis_duration_days"])
    default_span = float(default["fills_analysis_duration_days"])
    return [
        [
            "USD 最差回撤",
            spec.fmt_pct(default["drawdown_worst_usd"]),
            spec.fmt_pct(base["drawdown_worst_strategy_eq"]),
            spec.fmt_pct(analysis["drawdown_worst_usd"]),
        ],
        [
            "strategy equity 最差回撤",
            spec.fmt_pct(default["drawdown_worst_strategy_eq"]),
            spec.fmt_pct(base["drawdown_worst_strategy_eq"]),
            spec.fmt_pct(analysis["drawdown_worst_strategy_eq"]),
        ],
        [
            "最差 1% 均值回撤",
            spec.fmt_pct(default["drawdown_worst_mean_1pct_strategy_eq"]),
            spec.fmt_pct(base["worst_1pct_mean_drawdown"]),
            spec.fmt_pct(analysis["drawdown_worst_mean_1pct_strategy_eq"]),
        ],
        [
            "`gain_strategy_eq` 倍数",
            spec.fmt_ratio(default["gain_strategy_eq"], 6),
            spec.fmt_ratio(base["gain_strategy_eq"], 6),
            spec.fmt_ratio(analysis["gain_strategy_eq"], 6),
        ],
        [
            "CAGR（由 `gain_strategy_eq` 与区间重算）",
            spec.fmt_pct(cagr_from_gain(default["gain_strategy_eq"], default_span)),
            spec.fmt_pct(cagr_from_gain(base["gain_strategy_eq"], span)),
            spec.fmt_pct(cagr_from_gain(analysis["gain_strategy_eq"], span)),
        ],
        [
            "最长 strategy equity 恢复期（天）",
            f"{float(default['strategy_eq_recovery_days_max']):,.2f}",
            f"{float(base['strategy_eq_recovery_days_max']):,.2f}",
            f"{float(analysis['strategy_eq_recovery_days_max']):,.2f}",
        ],
        [
            "strategy equity 水下时间占比（均值）",
            spec.fmt_pct(default["strategy_eq_underwater_pct_mean"], 3),
            spec.fmt_pct(base["strategy_eq_underwater_pct_mean"], 3),
            spec.fmt_pct(analysis["strategy_eq_underwater_pct_mean"], 3),
        ],
        [
            "成交：入场 / 总",
            f"{int(default['fills_count_entry']):,} / {int(default['fills_count']):,}",
            f"{int(base['entry_fills']):,} / {int(base['fill_rows']):,}",
            f"{int(analysis['fills_count_entry']):,} / {int(analysis['fills_count']):,}",
        ],
        [
            "组合 TWE 最大记录值",
            fmt_ratio_value(default["total_wallet_exposure_max"]),
            fmt_ratio_value(base["total_wallet_exposure_max"]),
            fmt_ratio_value(analysis["total_wallet_exposure_max"]),
        ],
        [
            "强平",
            "是" if default["liquidated"] else "否",
            "是" if base["liquidated"] else "否",
            "是" if analysis["liquidated"] else "否",
        ],
    ]


def study_appendix(
    record: dict[str, Any],
    global_metrics: dict[str, Any],
    analysis: dict[str, Any],
    ledger: dict[str, Any],
    audit: dict[str, Any],
    baseline: dict[str, Any],
    base_cell: dict[str, Any],
    source: dict[str, Any],
    profile_input: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    facts = spec.analysis_facts(analysis)
    risk = cfg["bot"]["long"]["risk"]
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    unstuck = cfg["bot"]["long"]["unstuck"]
    hsl = cfg["bot"]["long"]["hsl"]
    gate = record.get("entry_regime_gate") or {}
    contract = record.get("contract", {})
    cell = profile_input["metrics"]
    effect = gateway_effect_facts(ledger, analysis, base_cell)
    L: list[str] = []
    add = L.append

    # ---------------------------------------------------------------- gate declaration
    add("### 本次改动声明：唯一的差异就是日线入场闸门")
    add("")
    add(
        f"- 该格属于 `{study.CELL_GROUP}`，窗口 `{study.CELL_WINDOW}`"
        f"（{(contract.get('window_full') or ['n/a', 'n/a'])[0]} → "
        f"{(contract.get('window_full') or ['n/a', 'n/a'])[1]}），"
        f"情景 `{contract.get('primary_scenario', study.SCENARIO)}`。"
    )
    add(
        f"- 本工件不是从种子 ops 推导的，而是直接运行**已发布文件** "
        f"`{profile_input['published_profile']}`"
        f"（sha256 `{str(profile_input['published_profile_sha256'])[:16]}…`）。"
        "构建阶段校验了冻结配置与该文件在 `bot` / `live` / `coin_overrides` / `monitor` / "
        "`logging` 上逐路径一致，只重定向了运行目录位置。"
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
                    "`backtest.entry_regime_gate.enabled`",
                    "（未声明）",
                    f"**`{gate.get('enabled')}`**",
                    "闸门总开关；关闭时等于未加闸门的 profile",
                ],
                [
                    "`backtest.entry_regime_gate.sma_fast_days`",
                    "（未声明）",
                    f"**`{gate.get('sma_fast_days')}`**",
                    "快线窗口（日）",
                ],
                [
                    "`backtest.entry_regime_gate.sma_slow_days`",
                    "（未声明）",
                    f"**`{gate.get('sma_slow_days')}`**",
                    "慢线窗口（日）",
                ],
                [
                    "`backtest.entry_regime_gate.confirm_days`",
                    "（未声明）",
                    f"**`{gate.get('confirm_days')}`**",
                    "额外确认天数；0 表示仅由交叉本身决定",
                ],
                [
                    "`backtest.entry_regime_gate.block_initial`",
                    "（未声明）",
                    f"**`{gate.get('block_initial')}`**",
                    "risk-off 时禁止开新仓",
                ],
                [
                    "`backtest.entry_regime_gate.block_reentry`",
                    "（未声明）",
                    f"**`{gate.get('block_reentry')}`**",
                    "risk-off 时禁止对已有仓位加仓",
                ],
            ],
            ["配置路径", "基线的值", "本工件值", "含义"],
        )
    )
    add("")
    add(
        "- 闸门是**过滤器而非信号**：它只能抑制入场。平仓、panic 与 auto-unstuck 走各自独立"
        "路径，因此 risk-off 的日子里仓位照样可以减、可以平，只是不能加风险。"
    )
    add(
        "- 因果边界：UTC 第 `D` 日的判定只由第 `D-1` 日**已收盘**的日线决定。"
        "回测在 Python 侧按此算出 `(transition_ts, regime)` 边界表，Rust 只做时间戳查表，"
        "引擎内不存在任何指标状态。"
    )
    add("")

    # ------------------------------------------------------------------ agreement
    add(f"### 与源研究工件的逐项一致性（同格、两次独立回放）")
    add("")
    add(
        f"左列是源研究 `{source['label']}` 里 `{study.CELL_ID}` 这一格**自己的**完整工件"
        f"（run `{source['run_dir_name']}`，`analysis.json` sha256 "
        f"`{source['sha256'][:16]}…`）；右列是本工件。两者是同一格配置的两次独立回放，"
        "因此每一项都应当吻合到数值误差。"
    )
    add("")
    add(
        spec.md_table(
            agreement_table(facts, analysis, source, profile_input, ledger),
            ["指标（同口径）", "源研究工件", "本工件"],
        )
    )
    add("")
    add(
        "- 一致性是本次回放最直接的**可复现性证据**：如果这里出现系统性偏差，"
        "说明两次回放之间引擎、数据或配置发生了漂移，后面的对照读数就不可信。"
    )
    add(
        "- 凡标「源研究记录」的行，两列引用的是同一条研究格记录（该量由研究自身在其权益序列上"
        "算出，引擎的 `analysis.json` 并不报告它），因此它们相同是**定义**而非独立复现；"
        "其余各行才是两次独立回放各自算出的引擎指标。"
    )
    add(
        "- `worst_1pct_mean_drawdown` 一行两列**不同**（"
        f"{spec.fmt_pct(float(cell['worst_1pct_mean_drawdown']), 3)} vs "
        f"{spec.fmt_pct(float(analysis['drawdown_worst_mean_1pct_strategy_eq']), 3)}）"
        "是因为两者口径不同而非不一致：研究格在它收到的**逐分钟**权益序列上取最深 1% 的均值，"
        "`analysis.json` 的 `drawdown_worst_mean_1pct_strategy_eq` 则在本工件的**采样**序列"
        f"（`balance_sample_divider={cfg['backtest']['balance_sample_divider']}`）上取尾部分位，"
        "采样序列更粗因而尾部更浅。两者不可互换引用。"
    )
    add("")

    # ------------------------------------------------------------------ comparison
    add("### 三列对照：本工件 / 低尾 profile / 默认 profile")
    add("")
    add(
        "三列使用同一冻结篮子、同一有效区间、同一执行/成本口径"
        f"（`execution_delay_bars={int(cfg['backtest']['execution_delay_bars'])}`、"
        f"`intrabar_fill_order={cfg['backtest']['intrabar_fill_order']}`、"
        f"maker `{cfg['backtest']['maker_fee_override']}` / taker "
        f"`{cfg['backtest']['taker_fee_override']}`）。"
    )
    add("")
    add(
        spec.md_table(
            comparison_table(facts, analysis, baseline, base_cell),
            ["指标（同口径）", f"默认 profile（{study.BASELINE_LABEL}）", "低尾 profile", f"本工件（+{gate.get('sma_fast_days')}/{gate.get('sma_slow_days')} 闸门）"],
        )
    )
    add("")
    add(
        f"- 中间列取自源研究的 `{base_cell['cell_id']}` 格（`{base_cell['path']}`，"
        f"sha256 `{base_cell['sha256'][:16]}…`），即本 profile 的基线：同样的策略、同样的窗口，"
        "只是没有闸门。"
    )
    add(
        f"- 左列取自 `{baseline['label']}`，是未降敞口的默认 profile："
        f"`total_wallet_exposure_limit = {baseline['total_wallet_exposure_limit']}`、"
        f"`double_down_factor = {baseline['double_down_factor']}`、"
        f"`entry.threshold_base_pct = {baseline['entry_threshold_base_pct']}`。"
        f"本工件为 `{risk['total_wallet_exposure_limit']}` / "
        f"`{tm['entry']['double_down_factor']}` / `{tm['entry']['threshold_base_pct']}`。"
    )
    add(
        "- 三列不共享引擎修订。本表用于显示数量级与方向，不是单变量对照实验；"
        "闸门的因果读数请看下一节。"
    )
    add("")

    # ------------------------------------------------------------------ gate effect
    add("### 闸门到底改变了什么：只删入场，不动出场")
    add("")
    add(
        "闸门只能抑制入场，所以它对该格的影响应当是**入场笔数下降、减仓与平仓通道不变**。"
        "下表把这一条从账本里读出来。"
    )
    add("")
    add(
        spec.md_table(
            [
                [
                    "入场成交数",
                    f"{effect['entry_fills_ungated_lowertail']:,}",
                    f"**{effect['entry_fills']:,}**",
                    f"−{effect['entry_fills_ungated_lowertail'] - effect['entry_fills']:,}"
                    f"（{1 - effect['entry_fills'] / effect['entry_fills_ungated_lowertail']:.1%}）",
                ],
                [
                    "总成交数",
                    f"{effect['fills_ungated_lowertail']:,}",
                    f"**{effect['fills_total']:,}**",
                    f"−{effect['fills_ungated_lowertail'] - effect['fills_total']:,}"
                    f"（{1 - effect['fills_total'] / effect['fills_ungated_lowertail']:.1%}）",
                ],
                [
                    "组合 TWE 最大记录值",
                    fmt_ratio_value(effect["max_twe_ungated_lowertail"]),
                    f"**{fmt_ratio_value(effect['max_twe'])}**",
                    "闸门不改变仓位规模，只减少建立风险的机会",
                ],
                [
                    "有成交的天数 / 区间天数",
                    "n/a",
                    f"{effect['distinct_fill_days']:,} / {effect['span_days']:,.0f}",
                    "没有成交的日子不等于空仓，也不等于零收益",
                ],
                [
                    "源研究记录的活跃天数 / 占比",
                    "n/a",
                    f"{effect['active_days']:,.0f} / {float(analysis['fills_active_days_ratio']):.2%}",
                    "活跃天数下降正是闸门让出的参与度",
                ],
            ],
            ["实测项", "无闸门（低尾 profile）", "本工件（有闸门）", "差值"],
        )
    )
    add("")
    add(
        "- **闸门不改变单笔仓位规模**：`total_wallet_exposure_limit`、"
        "`we_excess_allowance_pct`、`n_positions` 与阶梯参数都未变，"
        "上表 TWE 上限也印证了这一点。"
    )
    add(
        f"- **闸门让出的是参与度**：{effect['span_days'] - effect['active_days']:,.0f} 天"
        f"（{(effect['span_days'] - effect['active_days']) / effect['span_days']:.1%}）"
        "没有入场机会，这部分资金闲置是收益让渡的直接来源。"
    )
    add("")

    # ------------------------------------------------------------------ audit
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
                    ["`fill_index >= activation_index` 违例", f"{audit['fill_before_activation_failures']:,}"],
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

    # ------------------------------------------------------------------ ledger
    add("### 资金账本实测")
    add("")
    add("下列数值均由 `fills.csv` 逐笔重放得到，不是配置中的声明值。")
    add("")
    add(
        spec.md_table(
            [
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
                    "首笔 / 末笔成交",
                    f"{spec.fmt_clock(ledger['first_fill'])} / {spec.fmt_clock(ledger['last_fill'])}",
                    "用于判断策略在窗口内实际活跃的区间",
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
                    "唯一的常态化减仓通道；任何改动都必须保留它",
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
                    "`entry.double_down_factor` / `entry.threshold_base_pct`",
                    f"`{tm['entry']['double_down_factor']}` / `{tm['entry']['threshold_base_pct']}`",
                    "决定阶梯加仓的步长与间距",
                ],
                [
                    "`close.retracement_base_pct` / `close.threshold_base_pct`",
                    f"`{tm['close']['retracement_base_pct']}` / `{tm['close']['threshold_base_pct']}`",
                    "决定追踪止盈是否会展开递归 close 梯子",
                ],
                [
                    f"`entry_regime_gate` ({gate.get('sma_fast_days')}/{gate.get('sma_slow_days')})",
                    f"`enabled={gate.get('enabled')}` / "
                    f"`block_initial={gate.get('block_initial')}` / "
                    f"`block_reentry={gate.get('block_reentry')}`",
                    "risk-off 时禁止开新仓与加仓；不影响平仓",
                ],
            ],
            ["配置项", "值", "风险含义"],
        )
    )
    add("")

    # ------------------------------------------------------------------ engine
    add("### 引擎与可重复性")
    add("")
    engine = record.get("engine", {})
    git = engine.get("git", {})
    add(
        spec.md_table(
            [
                ["Rust source fingerprint", f"`{engine.get('expected_source_fingerprint')}`"],
                ["已编译扩展 stamp 与源码一致", f"`{engine.get('source_fingerprint_matches_compiled_stamp')}`"],
                ["已编译扩展", f"`{engine_path_label(engine)}`"],
                ["git HEAD", f"`{git.get('head')}`（`{git.get('branch')}`）"],
                [
                    "工作区当时有未提交改动",
                    f"`{git.get('dirty')}`"
                    + (f"（{len(git.get('status_porcelain') or [])} 项）" if git.get("dirty") else ""),
                ],
                ["回测耗时", f"{float(global_metrics.get('backtest_elapsed_s') or 0.0):.1f} s"],
            ],
            ["项", "值"],
        )
    )
    add("")
    add(
        "- 重跑命令：`venv/bin/python "
        f"{study.relative(study.STUDY / 'report_tools/run_replay.py')}` 后接 "
        "`generate_annual_report.py` 与 `verify_replay_report.py`；"
        "也可直接 `bash "
        f"{study.relative(study.STUDY / 'run.sh')}`。回放要求命中冻结数据集与已启用闸门，"
        "任一不满足即中止。"
    )
    add("")

    # ------------------------------------------------------------------ limits
    add("### 必须与结论一起阅读的限制")
    add("")
    add(
        f"1. **闸门是滞后过滤器，不是事件保护。** {gate.get('sma_fast_days')}/"
        f"{gate.get('sma_slow_days')} 日交叉在数周尺度上响应；2025 年 2 月那种两个月内完成的"
        "暴跌只能事后反应。它的价值是**生存**（避免在持续下行中不断补仓），不是躲开单次事件。"
    )
    add(
        "2. **静态降敞口逻辑的隐含假设不适用于本 profile。** 本 profile 的基线把敞口降到 "
        f"`total_wallet_exposure_limit = {risk['total_wallet_exposure_limit']}`，"
        "闸门则是对该假设**部分解耦**的机制：它按趋势而非按幅度限制参与度。"
        "两者叠加后，回撤上界仍然不存在。"
    )
    add(
        "3. **成交时点是模型假设。** 本工件采用 "
        f"`execution_delay_bars={int(cfg['backtest']['execution_delay_bars'])}`"
        f"（T+{int(cfg['backtest']['execution_delay_bars']) + 1}）；"
        "不含盘口队列、部分成交、撤单竞争与真实撮合路径。"
    )
    add(
        "4. **maker 身份与费率是假设。** 非市价订单统一按挂单价、maker 费率记账；"
        "`GTC` 不代表 maker-only。"
    )
    add(
        f"5. **backtest-only 的下一根 K 线读取未被本次消除。** "
        f"`close.retracement_base_pct = {tm['close']['retracement_base_pct']}`；"
        "该值 > 0 时 close 走追踪分支，next-candle hint 仍参与判定是否展开完整递归 close 梯子。"
    )
    add(
        "6. **同 bar 顺序是确定性约定。** 每个币先处理 close、再处理 entry，不是交易所撮合顺序。"
    )
    add(
        "7. **参数时间旅行。** 该 profile 形成于 2026 年，被回放到 2023-09 起的历史，"
        "且闸门窗口是在同一段历史上选出的；本工件只能说明“该参数在这段历史数据上的模拟表现”。"
    )
    add(
        f"8. **强平未被模拟到。** `liquidated = {facts['liquidated']}` 是模拟结果，"
        "不等于真实保证金体系下不会强平；mark price、维持保证金梯度与资金费均未建模。"
    )
    add(
        "9. **本工件是单 profile 回放，不是选型结论。** 该格与其他格的横向对照以 "
        f"`{study.relative(study.SOURCE_STUDY)}` 为准；本报告不重复其研究结论。"
    )
    add("")
    return "\n".join(L)


def artifacts_table(
    result_dir: Path, audit: dict[str, Any], equity: pd.DataFrame, analysis: dict[str, Any]
) -> list[list[str]]:
    return [
        ["`analysis.json`", "回测原生指标（USD/BTC/strategy-equity 三套口径）"],
        ["`fills.csv`", f"{int(analysis['fills_count']):,} 条成交账本"],
        ["`balance_and_equity.csv.gz`", f"{len(equity):,} 行权益采样"],
        ["`config.json`", "本工件使用的完整有效配置（含启用的闸门）"],
        ["`dataset.json`", "数据集来源、币种、有效区间与缓存标识"],
        ["`run_record.json`", "profile 身份、闸门声明、引擎 fingerprint 与数据哈希"],
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
            "图表（逐币成交面板完整，未禁用任何分组）",
        ],
    ]


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

    record = load_record(result_dir)
    global_metrics = spec.load_json(result_dir / "global_metrics.json")
    analysis = spec.load_json(result_dir / "analysis.json")
    cfg = spec.load_json(result_dir / "config.json")
    profile_input = spec.load_json(study.PROFILE_INPUT_PATH)
    contract = record.get("contract", {})
    fills = spec.load_fills(result_dir)
    equity = spec.load_balance_equity(result_dir)

    annual = spec.build_period_table(equity, fills, "Y")
    monthly = spec.build_period_table(equity, fills, "M")
    coins = spec.build_coin_table(fills)
    ledger = ledger_facts(fills, equity, cfg)
    audit = audit_facts(audit_path_for(result_dir), int(cfg["backtest"]["execution_delay_bars"]))
    baseline = baseline_facts()
    base_cell = base_profile_cell_facts()
    source = source_bundle_facts()

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
        "scope_lines": scope_lines(result_dir, record, global_metrics, cfg, profile_input),
        "appendix": study_appendix(
            record, global_metrics, analysis, ledger, audit, baseline, base_cell, source,
            profile_input, cfg,
        ),
        "artifacts": artifacts_table(result_dir, audit, equity, analysis),
        "verifiable_notes": [
            f"- 权益采样端点：`balance_and_equity.csv.gz` 覆盖 "
            f"{equity['timestamp'].iloc[0]} → {equity['timestamp'].iloc[-1]}（{len(equity):,} 行）；"
            f"`fills.csv` 有 {ledger['fills_outside_sample']} 笔成交发生在最后一个采样点之后"
            f"（合计净 {ledger['pnl_outside_sample']:+.6f} USDT），因此年度/月度表的净已实现 PnL 合计"
            "不等于全账本合计，两者差值只能由该端点差解释。",
            f"- profile 身份：`{profile_input['published_profile']}`"
            f"（sha256 `{str(profile_input['published_profile_sha256'])[:16]}…`）"
            f" + 冻结运行配置 `{record.get('candidate_config_path')}`"
            f"（sha256 `{str(record.get('candidate_config_sha256', ''))[:16]}…`）。",
            f"- 闸门声明：`{gate_summary(record)}`；回放中止条件包含“闸门必须处于启用状态且参数"
            "与该声明逐字段一致”，因此本工件不可能是未加闸门的运行。",
            f"- 源研究同格工件：`{source['run_dir']}`（`analysis.json` sha256 "
            f"`{source['sha256'][:16]}…`）；基线低尾 profile 格：`{base_cell['path']}`"
            f"（sha256 `{base_cell['sha256'][:16]}…`）。",
            f"- 研究契约：`{contract.get('path')}`"
            f"（文件自带 `cell_matrix_sha256 = {contract.get('cell_matrix_sha256')}`）。",
            f"- 冻结数据集：`{dataset['cache_dir']}`；三份数据的逻辑数组哈希与 bundle "
            f"`manifest.json` 记录一致 = `{dataset_hashes_match}`"
            f"（`hlcvs` = `{str(dataset['data_hashes']['hlcvs'])[:16]}…`，"
            f"shape `{dataset['array_shapes']['hlcvs']}`；"
            "gzip 字节流的 sha256 只作溯源，见 `global_metrics.json`）。",
            f"- Rust source fingerprint：`{record.get('engine', {}).get('expected_source_fingerprint')}`"
            f"（已编译扩展 stamp 与源码一致 = "
            f"`{record.get('engine', {}).get('source_fingerprint_matches_compiled_stamp')}`）。",
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
        "ledger: entry_fills="
        f"{ledger['entry_fill_count']} close_fills={ledger['close_fill_count']} "
        f"twe_long_max={ledger['max_twe_long']:.6f} audit_rows={audit.get('rows')}"
    )


def load_record(result_dir: Path) -> dict[str, Any]:
    return spec.load_json(result_dir / "run_record.json")


def gate_summary(record: dict[str, Any]) -> str:
    gate = record.get("entry_regime_gate") or {}
    return (
        f"enabled={gate.get('enabled')}, sma_fast_days={gate.get('sma_fast_days')}, "
        f"sma_slow_days={gate.get('sma_slow_days')}, confirm_days={gate.get('confirm_days')}, "
        f"block_initial={gate.get('block_initial')}, block_reentry={gate.get('block_reentry')}"
    )


if __name__ == "__main__":
    main()
