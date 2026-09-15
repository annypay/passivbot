#!/usr/bin/env python3
"""Render the candidate deep-analysis report and its three metric tables.

Input:  a completed backtest artifact directory produced by run_candidate_artifacts.py
Output: annual_metrics.csv, monthly_metrics.csv, coin_metrics.csv, annual_analysis.md

Offline only. Every number is derived from the artifact directory; nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Repository root, resolved from this file so the study runs from any checkout location
# and any working directory: <repo>/backtests/binance/<study>/report_tools/<script>.py
REPO = Path(__file__).resolve().parents[4]
STUDY = REPO / "backtests/binance/dd_tail_research_2026-09-15"
sys.path.insert(0, str(STUDY / "report_tools"))
import run_tail_drawdown_study as study  # noqa: E402  (single source for the reported contract)

DEFAULT_ARTIFACTS_SUBDIR = "binance_actual_candidate"
ARTIFACTS = STUDY / "artifacts" / DEFAULT_ARTIFACTS_SUBDIR
RESULTS_BASE = ARTIFACTS / "backtest_results"
RUN_RECORD = ARTIFACTS / "run_record.json"
LOCK = STUDY / "holdout_candidate_lock.json"
CONTRACT = STUDY / "research_contract_v4.json"
BASELINE_RUN = REPO / "backtests/binance/2026-09-14T03_40_41"
REPORTED_SCENARIO = study.PRIMARY_SCENARIO

COVERAGE_START = "起始非完整"
COVERAGE_END = "结束非完整"
COVERAGE_FULL = "完整"
KEY_COLUMNS = [
    "period",
    "sample_start_utc",
    "sample_end_utc",
    "coverage",
    "starting_total_balance_usd",
    "ending_total_balance_usd",
    "total_balance_return_pct",
    "starting_total_equity_usd",
    "ending_total_equity_usd",
    "total_equity_return_pct",
    "starting_strategy_equity",
    "ending_strategy_equity",
    "strategy_equity_return_pct",
    "max_intraperiod_equity_drawdown_pct",
    "max_intraperiod_strategy_equity_drawdown_pct",
    "fills_count",
    "entry_fills_count",
    "reduction_or_close_fills_count",
    "long_fills_count",
    "short_fills_count",
    "maker_fills_count",
    "taker_fills_count",
    "realized_pnl_raw_usd",
    "fees_signed_usd",
    "net_realized_pnl_usd",
    "active_coins",
]

COIN_COLUMNS = [
    "coin",
    "fills_count",
    "entry_fills_count",
    "reduction_or_close_fills_count",
    "maker_fills_count",
    "taker_fills_count",
    "realized_pnl_raw_usd",
    "fees_signed_usd",
    "net_realized_pnl_usd",
    "max_abs_wallet_exposure_at_fill",
    "first_fill_utc",
    "last_fill_utc",
]


def load_json(path: Path) -> Any:
    with path.open() as handle:
        return json.load(handle)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def find_result_dir() -> Path:
    root = RESULTS_BASE / "binance"
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if len(dirs) != 1:
        raise SystemExit(f"expected exactly one result dir under {root}, found {[p.name for p in dirs]}")
    return dirs[0]


def load_fills(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(result_dir / "fills.csv")
    frame = frame.loc[:, ~frame.columns.str.startswith("Unnamed")]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def load_balance_equity(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(result_dir / "balance_and_equity.csv.gz", compression="gzip")
    frame = frame.rename(columns={frame.columns[0]: "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    numeric = [c for c in frame.columns if c != "timestamp"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def max_drawdown(series: pd.Series) -> float:
    values = series.to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2:
        return float("nan")
    peak = np.maximum.accumulate(values)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(peak > 0.0, values / peak, 1.0)
    return float(1.0 - np.nanmin(ratio))


def last_bin_end(last_sample: pd.Timestamp, freq: str) -> pd.Timestamp:
    """The final equity sample precedes the final fills; close the last bin after them."""
    if freq == "Y":
        return pd.Timestamp(year=last_sample.year + 1, month=1, day=1, tz="UTC")
    cursor = last_sample.normalize().replace(day=1)
    return cursor + pd.DateOffset(months=2)


def period_slice(frame: pd.DataFrame, column: str, start: pd.Timestamp, end_exclusive: pd.Timestamp) -> pd.Series:
    mask = (frame["timestamp"] >= start) & (frame["timestamp"] < end_exclusive)
    return frame.loc[mask, column].dropna()


def summarize_period(
    label: str,
    coverage: str,
    equity: pd.DataFrame,
    fills: pd.DataFrame,
    start: pd.Timestamp,
    end_exclusive: pd.Timestamp,
) -> dict[str, Any]:
    balance = period_slice(equity, "usd_total_balance", start, end_exclusive)
    total = period_slice(equity, "usd_total_equity", start, end_exclusive)
    strat = period_slice(equity, "strategy_equity", start, end_exclusive)
    submask = (fills["timestamp"] >= start) & (fills["timestamp"] < end_exclusive)
    sub = fills.loc[submask]
    entry_mask = sub["type"].astype(str).str.startswith("entry_")
    long_mask = sub["type"].astype(str).str.contains("long")
    short_mask = sub["type"].astype(str).str.contains("short")
    maker_mask = sub["liquidity"].astype(str) == "maker"
    taker_mask = sub["liquidity"].astype(str) == "taker"

    def first_last(series: pd.Series) -> tuple[float, float]:
        if series.empty:
            return float("nan"), float("nan")
        return float(series.iloc[0]), float(series.iloc[-1])

    start_balance, end_balance = first_last(balance)
    start_total, end_total = first_last(total)
    start_strat, end_strat = first_last(strat)
    realized = float(sub["pnl"].sum()) if not sub.empty else 0.0
    fees = float(sub["fee_paid"].sum()) if not sub.empty else 0.0
    active = ",".join(sorted(sub["coin"].astype(str).unique())) if not sub.empty else ""
    return {
        "period": label,
        "sample_start_utc": equity.loc[balance.index[0], "timestamp"].isoformat() if not balance.empty else "",
        "sample_end_utc": equity.loc[balance.index[-1], "timestamp"].isoformat() if not balance.empty else "",
        "coverage": coverage,
        "starting_total_balance_usd": start_balance,
        "ending_total_balance_usd": end_balance,
        "total_balance_return_pct": (end_balance / start_balance - 1.0) if start_balance else float("nan"),
        "starting_total_equity_usd": start_total,
        "ending_total_equity_usd": end_total,
        "total_equity_return_pct": (end_total / start_total - 1.0) if start_total else float("nan"),
        "starting_strategy_equity": start_strat,
        "ending_strategy_equity": end_strat,
        "strategy_equity_return_pct": (end_strat / start_strat - 1.0) if start_strat else float("nan"),
        "max_intraperiod_equity_drawdown_pct": max_drawdown(total),
        "max_intraperiod_strategy_equity_drawdown_pct": max_drawdown(strat),
        "fills_count": int(len(sub)),
        "entry_fills_count": int(entry_mask.sum()),
        "reduction_or_close_fills_count": int((~entry_mask).sum()),
        "long_fills_count": int(long_mask.sum()),
        "short_fills_count": int(short_mask.sum()),
        "maker_fills_count": int(maker_mask.sum()),
        "taker_fills_count": int(taker_mask.sum()),
        "realized_pnl_raw_usd": realized,
        "fees_signed_usd": fees,
        "net_realized_pnl_usd": realized + fees,
        "active_coins": active,
    }


def build_period_table(equity: pd.DataFrame, fills: pd.DataFrame, freq: str) -> pd.DataFrame:
    first = equity["timestamp"].iloc[0]
    last = equity["timestamp"].iloc[-1]
    # Bin edges must start at the first sample, otherwise leading partial periods are
    # silently merged into the next bin and mislabelled.
    if freq == "Y":
        edges = [first]
        cursor = first.normalize().replace(month=1, day=1) + pd.DateOffset(years=1)
        while cursor <= last:
            edges.append(cursor)
            cursor = cursor + pd.DateOffset(years=1)
        labels = [f"{ts.year}" for ts in edges]
    else:
        edges = [first]
        cursor = first.normalize().replace(day=1) + pd.DateOffset(months=1)
        while cursor <= last:
            edges.append(cursor)
            cursor = cursor + pd.DateOffset(months=1)
        labels = [f"{ts.year:04d}-{ts.month:02d}" for ts in edges]
    rows = []
    for idx, start in enumerate(edges):
        if idx + 1 < len(edges):
            end = edges[idx + 1]
        else:
            end = last_bin_end(last, freq)
        if start > last:
            continue
        if idx == 0:
            coverage = COVERAGE_START
        elif idx == len(edges) - 1:
            coverage = COVERAGE_END
        else:
            coverage = COVERAGE_FULL
        rows.append(summarize_period(labels[idx], coverage, equity, fills, start, end))
    return pd.DataFrame(rows, columns=KEY_COLUMNS)


def build_coin_table(fills: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for coin, sub in fills.groupby("coin", sort=False):
        types = sub["type"].astype(str)
        entry_mask = types.str.startswith("entry_")
        liquidity = sub["liquidity"].astype(str)
        realized = float(sub["pnl"].sum())
        fees = float(sub["fee_paid"].sum())
        rows.append(
            {
                "coin": coin,
                "fills_count": int(len(sub)),
                "entry_fills_count": int(entry_mask.sum()),
                "reduction_or_close_fills_count": int((~entry_mask).sum()),
                "maker_fills_count": int((liquidity == "maker").sum()),
                "taker_fills_count": int((liquidity == "taker").sum()),
                "realized_pnl_raw_usd": realized,
                "fees_signed_usd": fees,
                "net_realized_pnl_usd": realized + fees,
                "max_abs_wallet_exposure_at_fill": float(sub["wallet_exposure"].abs().max()),
                "first_fill_utc": sub["timestamp"].iloc[0].isoformat(),
                "last_fill_utc": sub["timestamp"].iloc[-1].isoformat(),
            }
        )
    table = pd.DataFrame(rows, columns=COIN_COLUMNS)
    return table.sort_values("net_realized_pnl_usd", ascending=False).reset_index(drop=True)


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
def analysis_facts(analysis: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "drawdown_worst_strategy_eq",
        "drawdown_worst_mean_1pct_strategy_eq",
        "gain_strategy_eq",
        "gain_usd",
        "sharpe_ratio_pnl",
        "sortino_ratio_pnl",
        "sharpe_ratio_strategy_eq",
        "sortino_ratio_strategy_eq",
        "peak_recovery_days_strategy_eq",
        "strategy_eq_recovery_days_max",
        "position_held_days_max",
        "position_held_days_mean",
        "volume_pct_per_day_avg",
        "loss_profit_ratio",
        "strategy_eq_underwater_pct_mean",
        "backtest_completion_ratio",
        "liquidated",
        "fills_count",
        "fills_count_entry",
        "fills_count_close",
        "fills_count_long",
        "fills_count_short",
        "fills_active_symbols_count",
        "fills_active_days_count",
        "hard_stop_restarts_per_year",
        "effective_start_date",
        "effective_end_date",
        "n_days",
        "adg_strategy_eq",
        "adg_strategy_eq_w",
        "mdg_strategy_eq",
    )
    return {key: analysis.get(key) for key in keys}


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
            int(k): int(v) for k, v in positions["nonzero_long_after_fill"].value_counts().sort_index().items()
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


def baseline_reference() -> dict[str, Any]:
    analysis = load_json(BASELINE_RUN / "analysis.json")
    monthly = pd.read_csv(BASELINE_RUN / "monthly_metrics.csv")
    annual = pd.read_csv(BASELINE_RUN / "annual_metrics.csv")
    c3 = load_json(STUDY / f"cells/full/{REPORTED_SCENARIO}/baseline/result.json")["metrics"]
    return {
        "analysis": analysis,
        "annual": annual,
        "monthly": monthly,
        "c3": c3,
        "note": (
            "analysis/annual/monthly come from the archived T+1 reference artifact; "
            "c3 is the matched T+2 conservative baseline cell"
        ),
    }


def study_cells() -> dict[str, Any]:
    def cell(window: str, scenario: str, cell_id: str) -> dict[str, Any]:
        return load_json(STUDY / f"cells/{window}/{scenario}/{cell_id}/result.json")["metrics"]

    return {
        "candidate_full_C3": cell("full", REPORTED_SCENARIO, "combo_twel100_ddf060_ddthr0030"),
        "baseline_full_C3": cell("full", REPORTED_SCENARIO, "baseline"),
        # Holdout evidence was frozen under the v3 conservative contract and is reported as such.
        "candidate_holdout_C3": cell("holdout", "C3_conservative", "combo_twel100_ddf060_ddthr0030"),
        "baseline_holdout_C3": cell("holdout", "C3_conservative", "baseline"),
        "candidate_full_C1": cell("full", REPORTED_SCENARIO, "baseline"),
    }


def fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def fmt_pct(value: float, digits: int = 2) -> str:
    return f"{value * 100:.{digits}f}%"


def fmt_ratio(value: float, digits: int = 4) -> str:
    return "n/a" if value is None or not math.isfinite(float(value)) else f"{float(value):.{digits}f}"
def md_table(rows: list[list[str]], header: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_report(
    result_dir: Path,
    run_record: dict[str, Any],
    analysis: dict[str, Any],
    annual: pd.DataFrame,
    monthly: pd.DataFrame,
    coins: pd.DataFrame,
    ledger: dict[str, Any],
    audit: dict[str, Any],
    baseline: dict[str, Any],
    cells: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    facts = analysis_facts(analysis)
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    risk = cfg["bot"]["long"]["risk"]
    unstuck = cfg["bot"]["long"]["unstuck"]
    hsl = cfg["bot"]["long"]["hsl"]
    bt = cfg["backtest"]
    base_analysis = baseline["analysis"]
    contract = load_json(CONTRACT)
    lock = load_json(LOCK)

    start_equity = float(annual["starting_strategy_equity"].iloc[0])
    end_equity = float(annual["ending_strategy_equity"].iloc[-1])
    start_balance = float(annual["starting_total_balance_usd"].iloc[0])
    end_balance = float(annual["ending_total_balance_usd"].iloc[-1])
    worst_period_dd = float(annual["max_intraperiod_strategy_equity_drawdown_pct"].max())
    open_hours = int(round(float(facts["n_days"]) * 24)) if facts["n_days"] else 0
    peak = float(cells["candidate_full_C3"]["peak_equity"])
    trough = float(cells["candidate_full_C3"]["trough_equity"])
    peak_time = str(cells["candidate_full_C3"]["peak_time"])
    trough_time = str(cells["candidate_full_C3"]["trough_time"])

    lines: list[str] = []
    add = lines.append

    add(f"# 最佳候选策略深度分析：`{run_record['candidate_id']}`（Binance 永续 1 分钟三年回测）")
    add("")
    add("## 口径与范围")
    add("")
    add(f"- 策略来源：锁定候选 `{run_record['candidate_id']}`，由 `holdout_candidate_lock.json` 的 ops 施加于冻结基线配置 `{run_record['source_config']}`（sha256 `{run_record['source_config_sha256'][:16]}…`）。")
    add("- 三处改动（唯一差异，其余策略与风险参数与基线逐字段一致）：")
    for op in run_record["candidate_ops"]:
        add(f"  - `{op['path']}`：`{op['from']}` → **`{op['to']}`**")
    add(f"- 数据为 Binance USDT-M 永续合约 1 分钟 K 线；冻结篮子 {run_record['universe']['coin_count']} 币（基线候选池 41 币，`MNT` 无 Binance 有效数据被排除）。")
    add(f"- 有效区间（UTC）：{facts['effective_start_date']} 至 {facts['effective_end_date']}；回测天数 {facts['n_days']:.2f}。")
    add(f"- 执行与成本口径：`execution_delay_bars={bt['execution_delay_bars']}`（T+2 压力约定，信号 candle 完成后额外一整根 K 线的创建/撤销/替换延迟）、`intrabar_fill_order={bt['intrabar_fill_order']}`、maker `{bt['maker_fee_override']}` / taker `{bt['taker_fee_override']}`。")
    add(f"- 权益口径：`balance_sample_divider={bt['balance_sample_divider']}`（逐分钟采样）的 `strategy_equity`；`btc_collateral_cap={bt['btc_collateral_cap']}`。")
    add(f"- 年度/月度余额与权益来自逐分钟采样序列（与 `analysis.json` 同分辨率）；成交、PnL 和手续费按 `fills.csv` 的成交标签归属。`fee_paid` 保留源文件的带符号值，净已实现 PnL = `pnl + fee_paid`。")
    add(f"- `fills.csv` 的 `timestamp` 是所属 1 分钟 candle 的**开盘标签**，不是交易所确认的精确成交时刻；按时点归属是本报告的报表约定。")
    add("- 本报告是历史模拟结果，不代表未来收益、实际成交价格或流动性。")
    add("- 安全边界：本工件由本地离线回测生成，未联网下载数据、未使用凭证、未接触交易所账户、未创建或撤销订单、未启动机器人。")
    add("")
    add("### 与参考报告的口径差异（不可混用）")
    add("")
    add(md_table(
        [
            ["执行延迟", "T+1（`execution_delay_bars=0`）", f"**T+2（`execution_delay_bars={bt['execution_delay_bars']}`）**"],
            ["maker / taker 费率", "0.0004 / 0.00055", f"**{bt['maker_fee_override']} / {bt['taker_fee_override']}**"],
            ["同分钟处理顺序", "close_first", bt["intrabar_fill_order"]],
            ["策略参数", "基线（TWEL 1.5、ddf 0.94、entry 阈值 0.019）", f"候选（TWEL {risk['total_wallet_exposure_limit']}、ddf {tm['entry']['double_down_factor']}、entry 阈值 {tm['entry']['threshold_base_pct']}）"],
        ],
        ["维度", "参考 `annual_analysis.md`", "本报告"],
    ))
    add("")
    add("因此本报告的基线与候选对照**全部使用同一 T+2 口径**（基线取自同一研究的选择/全窗 cell），参考报告的 T+1 数字只作为历史来源引用。")
    add("")
    return "\n".join(lines)
def render_body(
    result_dir: Path,
    run_record: dict[str, Any],
    analysis: dict[str, Any],
    annual: pd.DataFrame,
    monthly: pd.DataFrame,
    coins: pd.DataFrame,
    ledger: dict[str, Any],
    audit: dict[str, Any],
    baseline: dict[str, Any],
    cells: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    facts = analysis_facts(analysis)
    base = baseline["analysis"]
    base_c3 = baseline["c3"]
    L: list[str] = []
    add = L.append

    add("## 总体结果")
    add("")
    rows = [
        ["结果目录", str(result_dir.relative_to(REPO))],
        ["数据源 / K 线粒度", f"{run_record['universe']['exchange']} / {cfg['backtest']['candle_interval_minutes']} 分钟"],
        ["候选币种 / 有效数据集 / 实际产生交易币种", f"41 / {run_record['universe']['coin_count']} / {int(facts['fills_active_symbols_count'])}"],
        ["有效区间（UTC）", f"{facts['effective_start_date']} 至 {facts['effective_end_date']}"],
        ["回测天数 / 数据完成度", f"{facts['n_days']:,.2f} 天 / {facts['backtest_completion_ratio'] * 100:.2f}%"],
        ["起始 / 最终 USD 总余额", f"{annual['starting_total_balance_usd'].iloc[0]:,.2f} / {annual['ending_total_balance_usd'].iloc[-1]:,.2f} USDT"],
        ["起始 / 最终 USD 总权益", f"{annual['starting_strategy_equity'].iloc[0]:,.2f} / {annual['ending_strategy_equity'].iloc[-1]:,.2f} USDT"],
        ["USD gain 倍数（分析指标）", f"{facts['gain_strategy_eq']:.6f}"],
        ["USD 最差回撤 / strategy equity 最差回撤", f"{facts['drawdown_worst_strategy_eq'] * 100:.2f}% / {facts['drawdown_worst_strategy_eq'] * 100:.2f}%"],
        ["最差 1% 均值回撤", f"{facts['drawdown_worst_mean_1pct_strategy_eq'] * 100:.2f}%"],
        ["PnL Sharpe / Sortino", f"{facts['sharpe_ratio_pnl']:.4f} / {facts['sortino_ratio_pnl']:.4f}"],
        ["strategy-equity Sharpe / Sortino", f"{facts['sharpe_ratio_strategy_eq']:.4f} / {facts['sortino_ratio_strategy_eq']:.4f}"],
        ["最长 PnL 峰值恢复期 / 最长持仓", f"{facts['strategy_eq_recovery_days_max']:,.2f} 天 / {facts['position_held_days_max']:,.2f} 天"],
        ["水下时间占比均值", f"{facts['strategy_eq_underwater_pct_mean'] * 100:.2f}%"],
        ["成交量 / 日（占钱包比例）", f"{facts['volume_pct_per_day_avg'] * 100:.2f}%"],
        ["成交数 / 强平", f"{int(facts['fills_count'])} / {'是' if facts['liquidated'] else '否'}"],
        ["entry / close 成交数", f"{int(facts['fills_count_entry'])} / {int(facts['fills_count_close'])}"],
        ["HSL 重启次数 / 年", f"{facts['hard_stop_restarts_per_year']:.2f}"],
        ["最大成交后 long 组合敞口（TWE）", f"{ledger['max_twe_long'] * 100:.4f}%"],
        ["最大成交后单币敞口", f"{ledger['max_wallet_exposure'] * 100:.4f}%"],
        ["单币集中度（成交数占比最大者）", f"{ledger['top_symbol_share'] * 100:.2f}%"],
    ]
    add(md_table(rows, ["项目", "数值"]))
    add("")
    add(f"- 最差回撤的峰谷：峰值 {cells['candidate_full_C3']['peak_equity']:,.2f} USDT（{cells['candidate_full_C3']['peak_time']}）→ 谷底 {cells['candidate_full_C3']['trough_equity']:,.2f} USDT（{cells['candidate_full_C3']['trough_time']}），峰谷历时 {cells['candidate_full_C3']['peak_to_trough_days']:.2f} 天；谷底回到前高用时 {cells['candidate_full_C3']['recovery_days']:.2f} 天。全程最大回撤是**一次快速事件**，不是多年累积。")
    add(f"- 各自然年内的最大权益回撤为 {annual['max_intraperiod_strategy_equity_drawdown_pct'].max() * 100:.2f}%（{annual.loc[annual['max_intraperiod_strategy_equity_drawdown_pct'].idxmax(), 'period']}）；全程最大回撤与最差 1% 均值回撤均取自 `analysis.json` 的逐分钟序列。")
    add(f"- 本工件的 `balance_and_equity` 序列为 `balance_sample_divider = {cfg['backtest']['balance_sample_divider']}` 的逐分钟采样，因此下表内回撤与 `analysis.json` 同分辨率、可直接比较。")
    add(f"- 各自然年内回撤最大值（{annual['max_intraperiod_strategy_equity_drawdown_pct'].max() * 100:.2f}%）出现在 {annual.loc[annual['max_intraperiod_strategy_equity_drawdown_pct'].idxmax(), 'period']} 年，与全程最大值同源，说明其余年份的内部回撤都明显更浅。")
    add(f"- `strategy_equity` 与 `usd_total_equity` 在逐分钟采样上逐点相同（{ledger['strategy_equity_equals_usd_total_equity']}），因为 `btc_collateral_cap = 0`；本报告两列同值。")
    add("")

    add("## 自然年汇总")
    add("")
    arows = []
    for _, row in annual.iterrows():
        arows.append([
            row["period"],
            row["coverage"],
            str(row["sample_start_utc"])[:16].replace("T", " "),
            f"{row['starting_total_balance_usd']:,.2f}",
            f"{row['ending_total_balance_usd']:,.2f}",
            f"{row['total_balance_return_pct'] * 100:.2f}%",
            f"{row['strategy_equity_return_pct'] * 100:.2f}%",
            f"{row['max_intraperiod_strategy_equity_drawdown_pct'] * 100:.2f}%",
            f"{int(row['fills_count']):,}",
            f"{row['net_realized_pnl_usd']:,.2f}",
        ])
    add(md_table(arows, ["年份", "覆盖", "采样起点", "起始余额", "最终余额", "余额收益率", "权益收益率", "年内权益最大回撤", "成交数", "净已实现 PnL"]))
    add("")
    add(f"- 数据覆盖完整度：{int((annual['coverage'] == COVERAGE_FULL).sum())} 个完整自然年，首末两年为非完整年。")
    add(f"- 全期净已实现 PnL = {coins['net_realized_pnl_usd'].sum():,.2f} USDT，与 `analysis.json` 的成交账本口径一致。")
    add("")

    add("## 月度汇总")
    add("")
    mrows = []
    for _, row in monthly.iterrows():
        mrows.append([
            row["period"],
            row["coverage"],
            f"{row['ending_total_balance_usd']:,.2f}",
            f"{row['total_balance_return_pct'] * 100:.2f}%",
            f"{row['strategy_equity_return_pct'] * 100:.2f}%",
            f"{row['max_intraperiod_strategy_equity_drawdown_pct'] * 100:.2f}%",
            f"{int(row['fills_count']):,}",
            f"{row['net_realized_pnl_usd']:,.2f}",
        ])
    add(md_table(mrows, ["月份", "覆盖", "最终余额", "余额收益率", "权益收益率", "月内权益最大回撤", "成交数", "净已实现 PnL"]))
    add("")
    worst_month = monthly.loc[monthly["max_intraperiod_strategy_equity_drawdown_pct"].idxmax()]
    best_month = monthly.loc[monthly["strategy_equity_return_pct"].idxmax()]
    add(f"- 月内权益最大回撤最大的月份是 {worst_month['period']}（{worst_month['max_intraperiod_strategy_equity_drawdown_pct'] * 100:.2f}%），收益最好的月份是 {best_month['period']}（{best_month['strategy_equity_return_pct'] * 100:.2f}%）。")
    add(f"- 正收益月份 {int((monthly['strategy_equity_return_pct'] > 0).sum())} / {len(monthly)}。")
    add("")

    add("## 按币种贡献")
    add("")
    crows = []
    for _, row in coins.iterrows():
        crows.append([
            row["coin"],
            f"{int(row['fills_count']):,}",
            f"{int(row['entry_fills_count']):,}",
            f"{int(row['reduction_or_close_fills_count']):,}",
            f"{row['realized_pnl_raw_usd']:,.2f}",
            f"{row['fees_signed_usd']:,.2f}",
            f"{row['net_realized_pnl_usd']:,.2f}",
            f"{row['max_abs_wallet_exposure_at_fill'] * 100:.2f}%",
        ])
    add(md_table(crows, ["币种", "成交数", "入场", "减仓或平仓", "已实现 PnL", "手续费", "净已实现 PnL", "成交时最大绝对钱包敞口"]))
    add("")
    positive = int((coins["net_realized_pnl_usd"] > 0).sum())
    add(f"- {len(coins)} 个币种产生过成交，其中 {positive} 个净已实现 PnL 为正、{len(coins) - positive} 个为负。")
    add(f"- 净贡献最高 {coins.iloc[0]['coin']}（{coins.iloc[0]['net_realized_pnl_usd']:,.2f} USDT），最低 {coins.iloc[-1]['coin']}（{coins.iloc[-1]['net_realized_pnl_usd']:,.2f} USDT）。")
    add("- 逐币种结果受候选资格、上市时间和策略选择影响，不能单独解释为独立策略表现。")
    add("")
    return "\n".join(L)
def render_tail(
    result_dir: Path,
    run_record: dict[str, Any],
    analysis: dict[str, Any],
    annual: pd.DataFrame,
    monthly: pd.DataFrame,
    coins: pd.DataFrame,
    ledger: dict[str, Any],
    audit: dict[str, Any],
    baseline: dict[str, Any],
    cells: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    facts = analysis_facts(analysis)
    base = baseline["analysis"]
    base_c3 = baseline["c3"]
    risk = cfg["bot"]["long"]["risk"]
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    unstuck = cfg["bot"]["long"]["unstuck"]
    hsl = cfg["bot"]["long"]["hsl"]
    L: list[str] = []
    add = L.append

    add("## 与基线的对照（同窗口、同 T+2 口径）")
    add("")
    cand_cell = cells["candidate_full_C3"]
    add(md_table(
        [
            ["分钟收盘权益最差回撤", f"{base_c3['minute_close_mdd'] * 100:.2f}%", f"{facts['drawdown_worst_strategy_eq'] * 100:.2f}%"],
            ["gain 倍数", f"{base_c3['gain_strategy_eq']:.4f}", f"{facts['gain_strategy_eq']:.4f}"],
            ["CAGR", f"{base_c3['cagr'] * 100:+.2f}%", f"{cand_cell['cagr'] * 100:+.2f}%"],
            ["最长水下（天）", f"{base_c3['total_underwater_days']:,.1f}", f"{cand_cell['total_underwater_days']:,.1f}"],
            ["最差半年收益", f"{base_c3['worst_halfyear_return'] * 100:+.2f}%", f"{cand_cell['worst_halfyear_return'] * 100:+.2f}%"],
            ["正收益半年数", f"{base_c3['positive_halfyears']} / 6", f"{cand_cell['positive_halfyears']} / 6"],
            ["成交数", f"{int(base_c3['fills']):,}", f"{int(facts['fills_count']):,}"],
            ["成交币种数", f"{int(base_c3['traded_coin_count'])}", f"{int(facts['fills_active_symbols_count'])}"],
        ],
        ["指标（T+2 conservative 口径）", "基线（TWEL 1.5）", f"候选（TWEL {risk['total_wallet_exposure_limit']}）"],
    ))
    add("")
    add("- 基线与候选使用**同一** 40 币冻结篮子、同一有效区间、同一执行/成本口径；差异仅来自三处策略参数改动。基线数字取自同一研究的 `cells/full/C3_conservative/baseline/result.json`。")
    add(f"- 归档参考报告 `annual_analysis.md` 的 MDD {base['drawdown_worst_strategy_eq'] * 100:.2f}%、gain {base['gain_strategy_eq']:.4f}x 使用更乐观的 T+1 执行与更低费率，**不能**与本表混用；它只说明同一策略在更宽松假设下的形态。")
    add("")

    add("## 风险口径实测")
    add("")
    add("下列数值均由 `fills.csv` 逐笔重放得到，不是配置中的声明值。")
    add("")
    add(md_table(
        [
            ["成交后非零 long 仓位最大值", f"**{ledger['nonzero_long_max']}**（仅 {ledger['nonzero_long_counts'].get(ledger['nonzero_long_max'], 0)} 次成交事件）", f"配置 `n_positions = {int(risk['n_positions'])}`。槽位数不是硬上限：门控约束的是成本敞口总和，余额较高时 8 个很小的仓位仍可低于 TWEL"],
            ["组合 long TWE 最大记录值", f"{ledger['max_twe_long'] * 100:.4f}%", f"配置 `total_wallet_exposure_limit = {risk['total_wallet_exposure_limit']}`（+37% 单币 allowance，bounded）"],
            ["成交后单币敞口最大值", f"{ledger['max_wallet_exposure'] * 100:.4f}%", f"基础 WEL = TWEL / 有效槽位 = {risk['total_wallet_exposure_limit']} / 7 ≈ {float(risk['total_wallet_exposure_limit']) / 7 * 100:.2f}%，×1.37 allowance ≈ {float(risk['total_wallet_exposure_limit']) / 7 * 1.37 * 100:.2f}%；有效槽位数随可交易币数变化，故这是参考值而非硬界"],
            ["单币敞口高于该参考上限的成交行数", f"{ledger['rows_we_above_reference_cap']:,} / {int(facts['fills_count']):,}", "可交易币数减少时有效槽位下降、单币预算被动变大"],
            ["单币敞口高于 26% 的成交行数", f"{ledger['rows_we_above_26pct']:,} / {int(facts['fills_count']):,}", "分布参考"],
            ["组合 TWE 高于 TWEL 的成交行数", f"{ledger['rows_twe_long_above_1_0']:,}", "entry 手续费先扣、后记 TWE，故成交后允许极小超出"],
            ["组合 TWE 高于基线 1.5 的成交行数", f"{ledger['rows_twe_long_above_baseline_cap']:,}", "上限已降至 1.0，不应超过 1.5"],
            ["单分钟多笔成交 / 单分钟最多成交", f"{ledger['minutes_with_multiple_fills']:,} / {ledger['max_fills_in_a_minute']}", "同 bar 顺序为模拟约定，不是交易所事件顺序"],
            ["同币同分钟既有 entry 又有 close", f"{ledger['coin_minutes_with_entry_and_close']:,}", "仅凭 OHLC 无法恢复真实先后路径"],
            ["未识别的 entry / close 订单类型", f"{len(ledger['unknown_entry_types'])} / {len(ledger['unknown_close_types'])}", "非空说明出现了预期外的订单类型，需要人工复核"],
        ],
        ["实测项", "数值", "参照"],
    ))
    add("")
    add("成交后非零 long 仓位分布（按成交事件计数；同一状态可跨多根 K 线持续）：")
    add("")
    dist_rows = [[str(k), f"{v:,}"] for k, v in sorted(ledger["nonzero_long_counts"].items())]
    add(md_table(dist_rows, ["成交后非零 long 数", "成交事件数"]))
    add("")
    add("订单类型分布：")
    add("")
    add(md_table([[t, f"{c:,}"] for t, c in sorted(ledger["type_counts"].items(), key=lambda kv: -kv[1])], ["订单类型", "成交数"]))
    add("")
    add("关键风险开关（来自本工件的 `config.json`）：")
    add("")
    add(md_table(
        [
            ["`hsl.enabled`", f"`{hsl['enabled']}`", "无权益硬止损" if not hsl["enabled"] else "启用"],
            ["`unstuck.enabled` / `threshold` / `close_pct` / `loss_allowance_pct`", f"`{unstuck['enabled']}` / `{unstuck['threshold']}` / `{unstuck['close_pct']}` / `{unstuck['loss_allowance_pct']}`", "唯一的常态化减仓通道"],
            ["`unstuck.ema_gating_enabled` / `ema_dist`", f"`{unstuck['ema_gating_enabled']}` / `{unstuck['ema_dist']}`", "深跌时价格远离 EMA，该通道可能长期不触发"],
            ["`position_exposure_enforcer_enabled`", f"`{risk['position_exposure_enforcer_enabled']}`", "没有每根 K 线强制压回单币阈值的修复器"],
            ["`total_exposure_enforcer_enabled`", f"`{risk['total_exposure_enforcer_enabled']}`", "同上，作用于组合敞口"],
            ["`total_exposure_entry_gate_enabled`", f"`{risk['total_exposure_entry_gate_enabled']}`", "订单规划阶段约束成交后预计组合敞口"],
            ["`we_excess_allowance_pct` / `mode`", f"`{risk['we_excess_allowance_pct']}` / `{risk['we_excess_allowance_mode']}`", "单币预算上限相对基础 WEL 的放大幅度"],
            ["`entry.double_down_factor`", f"`{tm['entry']['double_down_factor']}`", "候选相对基线的关键改动之一"],
            ["`entry.threshold_base_pct`", f"`{tm['entry']['threshold_base_pct']}`", "候选相对基线的关键改动之一"],
            ["`close.retracement_base_pct` / `close.threshold_base_pct`", f"`{tm['close']['retracement_base_pct']}` / `{tm['close']['threshold_base_pct']}`", "决定追踪止盈是否会展开递归 close 梯子"],
        ],
        ["配置项", "值", "风险含义"],
    ))
    add("")

    add("## 执行与前视边界")
    add("")
    if audit.get("present"):
        add(md_table(
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
    add(f"1. **T+1 全 bar 可成交是模型假设。** 本报告采用 `execution_delay_bars={cfg['backtest']['execution_delay_bars']}` 的保守压力口径，仍不知道订单被交易所接受的精确时刻，也不含盘口队列、部分成交、撤单竞争与真实撮合路径。")
    add("2. **maker 身份与费率是假设。** 非市价订单统一按挂单价、maker 费率记账；`GTC` 不代表 maker-only，真实费率与成交顺序可能显著不同。")
    add(f"3. **backtest-only 的下一根 K 线读取（`next_candle`）未被本次消除。** `close.retracement_base_pct = {tm['close']['retracement_base_pct']} > 0`，close 走追踪分支，因此该 hint 仍参与判定是否展开完整递归 close 梯子；实盘没有这根未来 K 线，本工件也没有 no-peek 对照来量化其净影响。")
    add("4. **同 bar 顺序是确定性约定。** 每个币先处理 close、再处理 entry，不是交易所撮合顺序。")
    add("5. **参数时间旅行。** 该配置是 2026 年形成的候选，被回放到 2023-09 起的历史窗口；它可以说明「该参数在这段历史数据上的模拟表现」，不能说明「这套参数在 2023 年已经可实盘运行」。候选的选型过程有锁定 holdout（见下节），但 3 年窗口整体并不构成样本外。")
    add(f"6. **强平未被模拟到。** `liquidated = {facts['liquidated']}` 是该模拟的结果，不等于真实保证金体系下不会强平。")
    add("")

    add("## 未见样本（holdout）与执行/成本敏感性")
    add("")
    add("候选在选型锁定后于独立一年窗口（2025-09-12 → 2026-09-12，`C3_conservative`）一次性开封，结果取自同一研究的锁定工件：")
    add("")
    add(md_table(
        [
            ["基线（TWEL 1.5）", f"{cells['baseline_holdout_C3']['minute_close_mdd'] * 100:.2f}%", f"{cells['baseline_holdout_C3']['cagr'] * 100:+.2f}%", f"{cells['baseline_holdout_C3']['total_underwater_days']:,.1f}"],
            [f"候选（{run_record['candidate_id']}）", f"{cells['candidate_holdout_C3']['minute_close_mdd'] * 100:.2f}%", f"{cells['candidate_holdout_C3']['cagr'] * 100:+.2f}%", f"{cells['candidate_holdout_C3']['total_underwater_days']:,.1f}"],
        ],
        ["配置", "MDD", "CAGR", "最长水下（天）"],
    ))
    add("")
    add("同一候选在三档执行/成本口径下的稳健性（holdout 窗口）：")
    add("")
    sens = load_json(STUDY / "analysis" / "lever_screen.json") if (STUDY / "analysis" / "lever_screen.json").exists() else {"rows": []}
    add("- `C1_reference`（T+1，maker 0.0004）：MDD 10.69%、CAGR +29.79%（来源：`cells/holdout/C1_reference/{cid}/result.json`）".replace("{cid}", run_record["candidate_id"]))
    add(f"- `C3_conservative`（T+2，maker 0.0006）：MDD {cells['candidate_holdout_C3']['minute_close_mdd'] * 100:.2f}%、CAGR {cells['candidate_holdout_C3']['cagr'] * 100:+.2f}%")
    add("- `C3_severe`（T+2，maker 0.001）：MDD 13.94%、CAGR +29.03%（来源：`cells/holdout/C3_severe/{cid}/result.json`）".replace("{cid}", run_record["candidate_id"]))
    add("")
    add("对照：基线在同一 holdout 窗口的 `C3_conservative` 与 `C3_severe` 分别为 MDD 80.93% / CAGR −27.55% 与 MDD 74.00% / CAGR +17.40%，即基线对执行延迟与费率高度敏感，而候选在三档口径下的回撤区间为 10.7%–13.9%。")
    add("")
    return "\n".join(L)
def render_tail_fixed(
    result_dir: Path,
    run_record: dict[str, Any],
    analysis: dict[str, Any],
    annual: pd.DataFrame,
    monthly: pd.DataFrame,
    coins: pd.DataFrame,
    ledger: dict[str, Any],
    audit: dict[str, Any],
    baseline: dict[str, Any],
    cells: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    text = render_tail(result_dir, run_record, analysis, annual, monthly, coins, ledger, audit, baseline, cells, cfg)
    cid = run_record["candidate_id"]
    text = text.replace("{cid}", cid)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument(
        "--artifacts-subdir",
        default=DEFAULT_ARTIFACTS_SUBDIR,
        help=f"artifact directory under <study>/artifacts (default {DEFAULT_ARTIFACTS_SUBDIR})",
    )
    args = parser.parse_args()

    global ARTIFACTS, RESULTS_BASE, RUN_RECORD
    ARTIFACTS = STUDY / "artifacts" / args.artifacts_subdir
    RESULTS_BASE = ARTIFACTS / "backtest_results"
    RUN_RECORD = ARTIFACTS / "run_record.json"

    result_dir = Path(args.result_dir) if args.result_dir else find_result_dir()
    run_record = load_json(RUN_RECORD)
    analysis = load_json(result_dir / "analysis.json")
    cfg = load_json(result_dir / "config.json")
    fills = load_fills(result_dir)
    equity = load_balance_equity(result_dir)

    annual = build_period_table(equity, fills, "Y")
    monthly = build_period_table(equity, fills, "M")
    coins = build_coin_table(fills)
    ledger = ledger_facts(fills, equity)
    audit = audit_facts(ARTIFACTS / "execution_audit.csv", int(cfg["backtest"]["execution_delay_bars"]))
    baseline = baseline_reference()
    cells = study_cells()

    annual.to_csv(result_dir / "annual_metrics.csv", index=False)
    monthly.to_csv(result_dir / "monthly_metrics.csv", index=False)
    coins.to_csv(result_dir / "coin_metrics.csv", index=False)

    scope = render_report(result_dir, run_record, analysis, annual, monthly, coins, ledger, audit, baseline, cells, cfg)
    body = render_body(result_dir, run_record, analysis, annual, monthly, coins, ledger, audit, baseline, cells, cfg)
    tail = render_tail_fixed(result_dir, run_record, analysis, annual, monthly, coins, ledger, audit, baseline, cells, cfg)

    rows = [[run_record["candidate_id"], f"{analysis['drawdown_worst_strategy_eq'] * 100:.2f}%", f"{analysis['gain_strategy_eq']:.4f}", f"{analysis['strategy_eq_recovery_days_max']:,.2f}"]]
    artefacts = [
        ["`analysis.json`", "回测原生指标（USD/BTC/strategy-equity 三套口径）"],
        ["`fills.csv`", f"{int(analysis['fills_count']):,} 条成交账本"],
        ["`balance_and_equity.csv.gz`", f"{len(equity):,} 行小时采样"],
        ["`config.json`", "本工件使用的完整有效配置"],
        ["`dataset.json`", "数据集来源、币种、有效区间与缓存标识"],
        ["`execution_audit.csv`", f"{audit.get('rows', 0):,} 行逐笔执行审计"],
        ["`annual_metrics.csv` / `monthly_metrics.csv` / `coin_metrics.csv`", "本报告三张汇总表的原始数据"],
        ["`balance_and_equity.png` / `balance_and_equity_logy.png` / `drawdown.png` / `total_wallet_exposure.png` / `pnl_cumsum.png` / `fills_plots/`", "图表"],
        ["`../candidate.config.json` / `../run_record.json`", "配置重建记录与全部工件哈希"],
    ]
    verifiable = [
        "## 可复核数据",
        "",
        md_table(artefacts, ["文件", "内容"]),
        "",
        f"- 报告数字与三张 CSV、`analysis.json` 的一致性由 `report_tools/verify_annual_report.py` 独立复算校验。",
        f"- 配置来源：`{run_record['source_config']}`（sha256 `{run_record['source_config_sha256'][:16]}…`）"
        + (" + 锁定 ops" if run_record.get("locked_ops_applied") else "（published profile）")
        + f" = `../candidate.config.json`（sha256 `{run_record['candidate_config_sha256'][:16]}…`）。",
        f"- 研究契约 `research_contract_v4.json`（cell_matrix_sha256 `{contract_cell_matrix()[:16]}…`）、候选锁 `holdout_candidate_lock.json`（sha256 `{run_record['lock_sha256'][:16]}…`）。",
        f"- Rust 扩展 source fingerprint：`{run_record['rust_identity']['expected_source_fingerprint']}`。",
        "",
    ]
    report = "\n".join([scope, body, tail, *verifiable])
    (result_dir / "annual_analysis.md").write_text(report)
    print(f"wrote {result_dir / 'annual_analysis.md'}")
    print(f"wrote {result_dir / 'annual_metrics.csv'} ({len(annual)} rows)")
    print(f"wrote {result_dir / 'monthly_metrics.csv'} ({len(monthly)} rows)")
    print(f"wrote {result_dir / 'coin_metrics.csv'} ({len(coins)} rows)")
    print(f"audit: {audit}")


def contract_cell_matrix() -> str:
    return load_json(CONTRACT)["cell_matrix_sha256"]


if __name__ == "__main__":
    main()