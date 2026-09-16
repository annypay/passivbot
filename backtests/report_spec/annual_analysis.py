#!/usr/bin/env python3
"""Canonical renderer for strategy-research deep analyses.

This module owns two things that must not drift apart:

1. the **fixed section skeleton** of a research deep analysis
   (`REPORT_SECTIONS`), and
2. the **metric-table schemas** that back its tables (`PERIOD_CSV_COLUMNS`,
   `COIN_CSV_COLUMNS`).

The binding convention, including the deliberate deviations from the frozen
reference report, is documented in `docs/ai/runbooks/strategy_report.md`.

Reference sample: `backtests/binance/2026-09-14T03_25_14/annual_analysis.md`.

Every number is derived from an artifact directory or an explicitly supplied
context; nothing is hardcoded. Offline only: no network access, no credentials,
no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------------
# Contract: section order and table schemas
# --------------------------------------------------------------------------------------

# The headings below are literal and positionally fixed, except the multi-coin contribution
# section (omitted for a single-coin study) and the study appendix (omitted when the study has
# nothing to add). Per-year detail headings are generated as
# f"{year} 明细（{coverage}）" and sit between the annual summary and the monthly summary.
HEAD_SCOPE = "## 口径与范围"
HEAD_OVERALL = "## 总体结果"
HEAD_ATTRIBUTION = "## 多空成交归因"
HEAD_ANNUAL = "## 自然年汇总"
HEAD_MONTHLY = "## 月度汇总"
HEAD_COIN_CONTRIBUTION = "## 按币种贡献"
HEAD_VERIFIABLE = "## 可复核数据"
HEAD_INTERPRETATION = "## 结果解读"
HEAD_APPENDIX = "## 研究附录"

# Positionally fixed part of the skeleton, in order, excluding the per-year detail
# headings and the appendix (which may be absent).
REPORT_SECTIONS = (
    HEAD_SCOPE,
    HEAD_OVERALL,
    HEAD_ATTRIBUTION,
    HEAD_ANNUAL,
    HEAD_MONTHLY,
    HEAD_VERIFIABLE,
    HEAD_INTERPRETATION,
)

#: Full skeleton including the optional sections, in document order.
OPTIONAL_SECTIONS = (HEAD_COIN_CONTRIBUTION, HEAD_APPENDIX)

#: One row per natural year or per calendar month. `active_coins` is an intentional
#: deviation from the frozen reference report, which ended with
#: `max_abs_wallet_exposure_at_fill`; both are reported, `active_coins` last.
PERIOD_CSV_COLUMNS = (
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
    "max_abs_wallet_exposure_at_fill",
    "active_coins",
)

COIN_CSV_COLUMNS = (
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
)

COVERAGE_START = "起始非完整"
COVERAGE_END = "结束非完整"
COVERAGE_FULL = "完整"

#: Analysis-json keys copied into the rendered report.
FACT_KEYS = (
    "drawdown_worst_strategy_eq",
    "drawdown_worst_usd",
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


# --------------------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------------------


def fmt_money(value: Any) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value):,.2f}"


def fmt_pct(value: Any, digits: int = 2) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value) * 100:.{digits}f}%"


def fmt_ratio(value: Any, digits: int = 4) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def fmt_iso(stamp: Any) -> str:
    """Second-resolution UTC timestamp with a space separator and explicit offset.

    pandas' `str(Timestamp)` uses this shape and `Timestamp.isoformat()` uses `T`; using one
    of them in the tables and the other in a CSV makes `to_csv` re-parse the column and
    silently change the separator. Space form is the canonical one everywhere.
    """
    if stamp is None or (isinstance(stamp, float) and not math.isfinite(stamp)):
        return ""
    text = str(stamp)
    if not text:
        return ""
    return text.replace("T", " ", 1)


def fmt_clock(stamp: Any) -> str:
    """`YYYY-MM-DD HH:MM` in UTC, used in the summary tables."""
    text = fmt_iso(stamp)
    if not text:
        return ""
    return text[:16].replace("T", " ")


def md_table(rows: list[list[str]], header: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Artifact loading
# --------------------------------------------------------------------------------------


def load_json(path: Path) -> Any:
    with Path(path).open() as handle:
        return json.load(handle)


def load_fills(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(Path(result_dir) / "fills.csv")
    frame = frame.loc[:, ~frame.columns.str.startswith("Unnamed")]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def load_balance_equity(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(Path(result_dir) / "balance_and_equity.csv.gz", compression="gzip")
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


# --------------------------------------------------------------------------------------
# Period and coin tables
# --------------------------------------------------------------------------------------


def period_slice(
    frame: pd.DataFrame, column: str, start: pd.Timestamp, end_exclusive: pd.Timestamp
) -> pd.Series:
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
    types = sub["type"].astype(str)
    entry_mask = types.str.startswith("entry_")
    long_mask = types.str.contains("long")
    short_mask = types.str.contains("short")
    liquidity = sub["liquidity"].astype(str)
    maker_mask = liquidity == "maker"
    taker_mask = liquidity == "taker"

    def first_last(series: pd.Series) -> tuple[float, float]:
        if series.empty:
            return float("nan"), float("nan")
        return float(series.iloc[0]), float(series.iloc[-1])

    start_balance, end_balance = first_last(balance)
    start_total, end_total = first_last(total)
    start_strat, end_strat = first_last(strat)
    realized = float(sub["pnl"].sum()) if not sub.empty else 0.0
    fees = float(sub["fee_paid"].sum()) if not sub.empty else 0.0
    if sub.empty:
        active = ""
        exposure = float("nan")
    else:
        active = ",".join(sorted(sub["coin"].astype(str).unique()))
        exposure = float(sub["wallet_exposure"].abs().max())
    return {
        "period": label,
        "sample_start_utc": fmt_iso(equity.loc[balance.index[0], "timestamp"]) if not balance.empty else "",
        "sample_end_utc": fmt_iso(equity.loc[balance.index[-1], "timestamp"]) if not balance.empty else "",
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
        "max_abs_wallet_exposure_at_fill": exposure,
        "active_coins": active,
    }


def last_bin_end(last_sample: pd.Timestamp, freq: str) -> pd.Timestamp:
    """Cap the final bin after the last sample so no empty trailing period is invented.

    The reference report for a run ending 2026-09-11 has a final annual row ending
    `2026-09-11T23:00`, not `2026-10-01`; the equity slice already clamps to the last
    sample, and this bound keeps the label honest for the fill window as well.
    """
    return last_sample + pd.DateOffset(seconds=1)


def build_period_table(equity: pd.DataFrame, fills: pd.DataFrame, freq: str) -> pd.DataFrame:
    """One row per natural year (`Y`) or calendar month (`M`).

    Bin edges start at the first sample so a leading partial period is labelled with its
    own year/month instead of being merged into the next bin.
    """
    first = equity["timestamp"].iloc[0]
    last = equity["timestamp"].iloc[-1]
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
    return pd.DataFrame(rows, columns=PERIOD_CSV_COLUMNS)


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
                "first_fill_utc": fmt_iso(sub["timestamp"].iloc[0]),
                "last_fill_utc": fmt_iso(sub["timestamp"].iloc[-1]),
            }
        )
    table = pd.DataFrame(rows, columns=COIN_CSV_COLUMNS)
    # Stable sort so equal net-PnL coins keep ledger order; the verifier sorts identically.
    return table.sort_values("net_realized_pnl_usd", ascending=False, kind="stable").reset_index(drop=True)


def build_attribution_table(fills: pd.DataFrame) -> list[dict[str, Any]]:
    """One row per direction, always two rows, so the layout is config-independent."""
    types = fills["type"].astype(str)
    rows = []
    for label, mask in (
        ("多头", types.str.contains("long")),
        ("空头", types.str.contains("short")),
    ):
        sub = fills.loc[mask]
        if sub.empty:
            rows.append(
                {
                    "direction": label,
                    "fills_count": 0,
                    "entry_fills_count": 0,
                    "realized_pnl_raw_usd": 0.0,
                    "fees_signed_usd": 0.0,
                    "net_realized_pnl_usd": 0.0,
                    "max_abs_wallet_exposure_at_fill": float("nan"),
                }
            )
            continue
        entry_mask = sub["type"].astype(str).str.startswith("entry_")
        realized = float(sub["pnl"].sum())
        fees = float(sub["fee_paid"].sum())
        rows.append(
            {
                "direction": label,
                "fills_count": int(len(sub)),
                "entry_fills_count": int(entry_mask.sum()),
                "realized_pnl_raw_usd": realized,
                "fees_signed_usd": fees,
                "net_realized_pnl_usd": realized + fees,
                "max_abs_wallet_exposure_at_fill": float(sub["wallet_exposure"].abs().max()),
            }
        )
    return rows
# --------------------------------------------------------------------------------------
# Derived facts
# --------------------------------------------------------------------------------------


def analysis_facts(analysis: dict[str, Any]) -> dict[str, Any]:
    return {key: analysis.get(key) for key in FACT_KEYS}


def direction_facts(cfg: dict[str, Any], attribution: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe why a direction has no fills, without guessing at exchange behaviour."""
    live = cfg.get("live", {})
    approved = live.get("approved_coins", {}) or {}
    short_symbols = list(approved.get("short") or [])
    return {
        "short_configured": len(short_symbols) > 0,
        "short_symbols_count": len(short_symbols),
        "hedge_mode": live.get("hedge_mode"),
        "has_short_fills": any(
            row["direction"] == "空头" and row["fills_count"] > 0 for row in attribution
        ),
        "has_long_fills": any(
            row["direction"] == "多头" and row["fills_count"] > 0 for row in attribution
        ),
    }


def build_facts(context: dict[str, Any]) -> dict[str, Any]:
    analysis = context["analysis"]
    cfg = context["config"]
    facts = analysis_facts(analysis)
    annual = context["annual"]
    monthly = context["monthly"]
    coins = context["coins"]
    attribution = context["attribution"]
    bt = cfg.get("backtest", {})
    dividend = int(bt.get("balance_sample_divider") or 1)

    total_fills = int(facts["fills_count"] or 0)
    maker_total = int(annual["maker_fills_count"].sum())
    taker_total = int(annual["taker_fills_count"].sum())
    n_days = float(facts["n_days"] or 0.0)
    title_span = f"{n_days:,.2f} 天"
    years = (n_days / 365.25) if n_days else 0.0

    start_equity = float(annual["starting_strategy_equity"].iloc[0])
    end_equity = float(annual["ending_strategy_equity"].iloc[-1])
    worst_dd = float(facts["drawdown_worst_strategy_eq"] or float("nan"))
    worst_year = annual.loc[annual["max_intraperiod_strategy_equity_drawdown_pct"].idxmax()]
    best_year = annual.loc[annual["strategy_equity_return_pct"].idxmax()]
    worst_month = monthly.loc[monthly["max_intraperiod_strategy_equity_drawdown_pct"].idxmax()]
    best_month = monthly.loc[monthly["strategy_equity_return_pct"].idxmax()]
    full_years = int((annual["coverage"] == COVERAGE_FULL).sum())

    return {
        "facts": facts,
        "dividend": dividend,
        "sampling": "逐分钟" if dividend == 1 else f"每 {dividend} 分钟",
        "sampling_note": (
            "逐分钟采样，与 `analysis.json` 同分辨率"
            if dividend == 1
            else f"每 {dividend} 分钟采样（比 `analysis.json` 粗）"
        ),
        "total_fills": total_fills,
        "maker_total": maker_total,
        "taker_total": taker_total,
        "all_maker": taker_total == 0 and maker_total > 0,
        "title_span": title_span,
        "years": years,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "equity_growth_pct": (end_equity / start_equity - 1.0) if start_equity else float("nan"),
        "worst_dd": worst_dd,
        "worst_year": worst_year,
        "best_year": best_year,
        "worst_month": worst_month,
        "best_month": best_month,
        "full_years": full_years,
        "net_pnl_total": float(coins["net_realized_pnl_usd"].sum()),
        "fees_total": float(coins["fees_signed_usd"].sum()),
        "realized_total": float(coins["realized_pnl_raw_usd"].sum()),
        "positive_coins": int((coins["net_realized_pnl_usd"] > 0).sum()),
        "coin_count": int(len(coins)),
        "positive_months": int((monthly["strategy_equity_return_pct"] > 0).sum()),
        "month_count": int(len(monthly)),
        "directions": direction_facts(cfg, attribution),
        "strategy_equity_equals_usd_total_equity": bool(context.get("ledger", {}).get("strategy_equity_equals_usd_total_equity", True)),
    }


# --------------------------------------------------------------------------------------
# Section renderers
# --------------------------------------------------------------------------------------


def render_scope(context: dict[str, Any], derived: dict[str, Any]) -> str:
    facts = derived["facts"]
    lines = [HEAD_SCOPE, ""]
    lines.extend(context.get("scope_lines") or [])
    for note in context.get("scope_notes") or []:
        lines.append(f"- {note}")
    lines.append(
        f"- 有效区间（UTC）：{facts['effective_start_date']} 至 {facts['effective_end_date']}；"
        f"回测天数 {float(facts['n_days'] or 0.0):,.2f}；数据完成度 "
        f"{float(facts['backtest_completion_ratio'] or 0.0) * 100:.2f}%。"
    )
    lines.append(
        f"- 余额/权益来自{derived['sampling']}采样序列（`balance_sample_divider="
        f"{derived['dividend']}`，{derived['sampling_note']}）；成交、PnL 与手续费按 `fills.csv` "
        "的成交标签归属。"
    )
    lines.append(
        "- `fee_paid` 按源文件保留带符号值；净已实现 PnL = `pnl + fee_paid`。非 `entry*` 成交归为"
        "减仓/平仓，其中可能包含解套或风险减仓。"
    )
    lines.append(
        "- `fills.csv` 的 `timestamp` 是所属 1 分钟 candle 的**开盘标签**，不是交易所确认的精确"
        "成交时刻；按时点归属是本报告的报表约定。"
    )
    lines.append("- 本报告为历史模拟分析，不能代表未来收益、实际成交价格或流动性。")
    lines.append("")
    return "\n".join(lines)


def render_overall(context: dict[str, Any], derived: dict[str, Any]) -> str:
    facts = derived["facts"]
    annual = context["annual"]
    run_record = context.get("run_record") or {}
    cfg = context["config"]
    lines = [HEAD_OVERALL, ""]
    rows = [
        ["结果目录", str(context.get("result_label") or "")],
        [
            "数据源 / K 线粒度",
            f"{run_record.get('universe', {}).get('exchange', 'n/a')} / "
            f"{cfg.get('backtest', {}).get('candle_interval_minutes', 'n/a')} 分钟",
        ],
        [
            "有效区间（UTC）",
            f"{facts['effective_start_date']} 至 {facts['effective_end_date']}",
        ],
        [
            "回测天数 / 数据完成度",
            f"{float(facts['n_days'] or 0.0):,.2f} 天 / "
            f"{float(facts['backtest_completion_ratio'] or 0.0) * 100:.2f}%",
        ],
        [
            "起始 / 最终 USD 总余额",
            f"{fmt_money(annual['starting_total_balance_usd'].iloc[0])} / "
            f"{fmt_money(annual['ending_total_balance_usd'].iloc[-1])} USDT",
        ],
        [
            "起始 / 最终 USD 总权益",
            f"{fmt_money(annual['starting_total_equity_usd'].iloc[0])} / "
            f"{fmt_money(annual['ending_total_equity_usd'].iloc[-1])} USDT",
        ],
        ["USD gain 倍数（分析指标）", fmt_ratio(facts["gain_usd"], 6)],
        [
            "USD 最差回撤 / strategy equity 最差回撤",
            f"{fmt_pct(facts['drawdown_worst_usd'])} / {fmt_pct(facts['drawdown_worst_strategy_eq'])}",
        ],
        ["最差 1% 均值回撤", fmt_pct(facts["drawdown_worst_mean_1pct_strategy_eq"])],
        [
            "PnL Sharpe / Sortino",
            f"{fmt_ratio(facts['sharpe_ratio_pnl'])} / {fmt_ratio(facts['sortino_ratio_pnl'])}",
        ],
        [
            "最长 PnL 峰值恢复期 / 最长持仓",
            f"{float(facts['strategy_eq_recovery_days_max'] or 0.0):,.2f} 天 / "
            f"{float(facts['position_held_days_max'] or 0.0):,.2f} 天",
        ],
        [
            "成交数 / 强平",
            f"{derived['total_fills']:,} / {'是' if facts['liquidated'] else '否'}",
        ],
    ]
    if context.get("extra_overall_rows"):
        rows.extend(context["extra_overall_rows"])
    lines.append(md_table(rows, ["项目", "数值"]))
    lines.append("")
    for note in context.get("overall_notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def render_attribution(context: dict[str, Any], derived: dict[str, Any]) -> str:
    attribution = context["attribution"]
    lines = [HEAD_ATTRIBUTION, ""]
    rows = []
    for row in attribution:
        rows.append(
            [
                row["direction"],
                f"{row['fills_count']:,}",
                f"{row['entry_fills_count']:,}",
                fmt_money(row["realized_pnl_raw_usd"]),
                fmt_money(row["fees_signed_usd"]),
                fmt_money(row["net_realized_pnl_usd"]),
                fmt_pct(row["max_abs_wallet_exposure_at_fill"]),
            ]
        )
    lines.append(
        md_table(
            rows,
            ["方向", "成交数", "入场数", "已实现 PnL", "手续费", "净已实现 PnL", "成交时最大绝对钱包敞口"],
        )
    )
    lines.append("")
    directions = derived["directions"]
    if not directions["has_short_fills"]:
        if directions["short_configured"]:
            detail = (
                f"`live.approved_coins.short` 配置了 {directions['short_symbols_count']} 个币，"
                f"`live.hedge_mode = {directions['hedge_mode']}`"
            )
            lines.append(
                f"- 空头成交为 0：本方向**配置允许但本次未被触发**（{detail}），不是「未配置」。"
                "该行保留以维持两行结构，数值为 0。"
            )
        else:
            lines.append(
                "- 空头成交为 0：`live.approved_coins.short` 为空，本方向**未配置**。"
                "该行保留以维持两行结构，数值为 0。"
            )
    if not directions["has_long_fills"]:
        lines.append("- 多头成交为 0：该行保留以维持两行结构，数值为 0。")
    if derived["all_maker"]:
        lines.append(
            f"- 全部 {derived['total_fills']:,} 笔模拟成交均为 maker 成交，taker 成交 {derived['taker_total']} 笔；"
            "因此结果对限价单成交假设与 maker 费率敏感，taker 费率不参与本次结果。"
        )
    elif derived["maker_total"] and derived["taker_total"]:
        lines.append(
            f"- maker / taker 成交分别为 {derived['maker_total']:,} / {derived['taker_total']:,} 笔；"
            "taker 成交会同时受费率与滑点假设影响。"
        )
    lines.append("")
    return "\n".join(lines)


def render_annual(context: dict[str, Any], derived: dict[str, Any]) -> str:
    annual = context["annual"]
    lines = [HEAD_ANNUAL, ""]
    rows = []
    for _, row in annual.iterrows():
        rows.append(
            [
                str(row["period"]),
                str(row["coverage"]),
                fmt_clock(row["sample_start_utc"]),
                fmt_money(row["starting_total_balance_usd"]),
                fmt_money(row["ending_total_balance_usd"]),
                fmt_pct(row["total_balance_return_pct"]),
                fmt_pct(row["strategy_equity_return_pct"]),
                fmt_pct(row["max_intraperiod_strategy_equity_drawdown_pct"]),
                f"{int(row['fills_count']):,}",
                fmt_money(row["net_realized_pnl_usd"]),
            ]
        )
    lines.append(
        md_table(
            rows,
            [
                "年份",
                "覆盖",
                "采样区间",
                "起始余额",
                "最终余额",
                "余额收益率",
                "权益收益率",
                "年内权益最大回撤",
                "成交数",
                "净已实现 PnL",
            ],
        )
    )
    lines.append("")
    lines.append(
        f"- 覆盖 {derived['full_years']} 个完整自然年；首末年为非完整年（共 {len(annual)} 个自然年行）。"
    )
    lines.append(
        f"- 全期净已实现 PnL = {fmt_money(derived['net_pnl_total'])} USDT"
        f"（已实现 {fmt_money(derived['realized_total'])} + 手续费 {fmt_money(derived['fees_total'])}）。"
    )
    lines.append("")
    for _, row in annual.iterrows():
        lines.append(f"### {row['period']} 年明细（{row['coverage']}）")
        lines.append("")
        detail = [
            ["采样区间（UTC）", f"{fmt_iso(row['sample_start_utc'])} 至 {fmt_iso(row['sample_end_utc'])}"],
            [
                "USD 总余额：起始 → 最终",
                f"{fmt_money(row['starting_total_balance_usd'])} → {fmt_money(row['ending_total_balance_usd'])}",
            ],
            [
                "USD 总权益：起始 → 最终",
                f"{fmt_money(row['starting_total_equity_usd'])} → {fmt_money(row['ending_total_equity_usd'])}",
            ],
            [
                "策略权益：起始 → 最终",
                f"{fmt_money(row['starting_strategy_equity'])} → {fmt_money(row['ending_strategy_equity'])}",
            ],
            [
                "余额 / 权益 / 策略权益收益率",
                f"{fmt_pct(row['total_balance_return_pct'])} / {fmt_pct(row['total_equity_return_pct'])} / "
                f"{fmt_pct(row['strategy_equity_return_pct'])}",
            ],
            [
                "年内权益最大回撤 / strategy equity 最大回撤",
                f"{fmt_pct(row['max_intraperiod_equity_drawdown_pct'])} / "
                f"{fmt_pct(row['max_intraperiod_strategy_equity_drawdown_pct'])}",
            ],
            [
                "成交：总计 / 入场 / 减仓或平仓",
                f"{int(row['fills_count']):,} / {int(row['entry_fills_count']):,} / "
                f"{int(row['reduction_or_close_fills_count']):,}",
            ],
            [
                "方向：多头 / 空头；maker / taker",
                f"{int(row['long_fills_count']):,} / {int(row['short_fills_count']):,}；"
                f"{int(row['maker_fills_count']):,} / {int(row['taker_fills_count']):,}",
            ],
            [
                "已实现 PnL / 手续费 / 净已实现 PnL",
                f"{fmt_money(row['realized_pnl_raw_usd'])} / {fmt_money(row['fees_signed_usd'])} / "
                f"{fmt_money(row['net_realized_pnl_usd'])}",
            ],
            ["成交时最大绝对钱包敞口", fmt_pct(row["max_abs_wallet_exposure_at_fill"])],
        ]
        if derived["coin_count"] > 1:
            detail.append(["产生交易的币种数", str(len(str(row["active_coins"]).split(",")) if row["active_coins"] else 0)])
        lines.append(md_table(detail, ["项目", "数值"]))
        lines.append("")
    return "\n".join(lines)


def render_monthly(context: dict[str, Any], derived: dict[str, Any]) -> str:
    monthly = context["monthly"]
    lines = [HEAD_MONTHLY, ""]
    rows = []
    for _, row in monthly.iterrows():
        rows.append(
            [
                str(row["period"]),
                str(row["coverage"]),
                fmt_money(row["ending_total_balance_usd"]),
                fmt_pct(row["total_balance_return_pct"]),
                fmt_pct(row["strategy_equity_return_pct"]),
                fmt_pct(row["max_intraperiod_strategy_equity_drawdown_pct"]),
                f"{int(row['fills_count']):,}",
                fmt_money(row["net_realized_pnl_usd"]),
            ]
        )
    lines.append(
        md_table(
            rows,
            ["月份", "覆盖", "最终余额", "余额收益率", "权益收益率", "月内权益最大回撤", "成交数", "净已实现 PnL"],
        )
    )
    lines.append("")
    lines.append(
        f"- 月内权益最大回撤最大的月份是 {derived['worst_month']['period']}"
        f"（{fmt_pct(derived['worst_month']['max_intraperiod_strategy_equity_drawdown_pct'])}），"
        f"收益最好的月份是 {derived['best_month']['period']}"
        f"（{fmt_pct(derived['best_month']['strategy_equity_return_pct'])}）。"
    )
    lines.append(
        f"- 正收益月份 {derived['positive_months']} / {derived['month_count']}。"
    )
    lines.append("")
    return "\n".join(lines)


def render_coin_contribution(context: dict[str, Any], derived: dict[str, Any]) -> str:
    """Multi-coin extension. Omitted for a single-coin study like the reference."""
    coins = context["coins"]
    if derived["coin_count"] <= 1:
        return ""
    lines = [HEAD_COIN_CONTRIBUTION, ""]
    rows = []
    for _, row in coins.iterrows():
        rows.append(
            [
                str(row["coin"]),
                f"{int(row['fills_count']):,}",
                f"{int(row['entry_fills_count']):,}",
                f"{int(row['reduction_or_close_fills_count']):,}",
                fmt_money(row["realized_pnl_raw_usd"]),
                fmt_money(row["fees_signed_usd"]),
                fmt_money(row["net_realized_pnl_usd"]),
                fmt_pct(row["max_abs_wallet_exposure_at_fill"]),
            ]
        )
    lines.append(
        md_table(
            rows,
            ["币种", "成交数", "入场", "减仓或平仓", "已实现 PnL", "手续费", "净已实现 PnL", "成交时最大绝对钱包敞口"],
        )
    )
    lines.append("")
    top = coins.iloc[0]
    bottom = coins.iloc[-1]
    lines.append(
        f"- {derived['coin_count']} 个币种产生过成交，其中 {derived['positive_coins']} 个净已实现 PnL 为正、"
        f"{derived['coin_count'] - derived['positive_coins']} 个为负。"
    )
    lines.append(
        f"- 净贡献最高 {top['coin']}（{fmt_money(top['net_realized_pnl_usd'])} USDT），"
        f"最低 {bottom['coin']}（{fmt_money(bottom['net_realized_pnl_usd'])} USDT）。"
    )
    lines.append("- 逐币种结果受候选资格、上市时间和策略选择影响，不能单独解释为独立策略表现。")
    lines.append("")
    return "\n".join(lines)


def render_verifiable(context: dict[str, Any], derived: dict[str, Any]) -> str:
    artifacts = context.get("artifacts") or []
    lines = [HEAD_VERIFIABLE, ""]
    if artifacts:
        lines.append(md_table([[name, desc] for name, desc in artifacts], ["文件", "内容"]))
        lines.append("")
    for note in context.get("verifiable_notes") or []:
        text = str(note).strip()
        lines.append(text if text.startswith("- ") else f"- {text}")
    lines.append(
        "- 本报告与三张汇总 CSV、`analysis.json` 的一致性由 `report_tools/verify_annual_report.py` "
        "独立复算校验，并检查本规范的固定章节骨架是否完整。"
    )
    lines.append("")
    return "\n".join(lines)


def render_interpretation(context: dict[str, Any], derived: dict[str, Any]) -> str:
    """Four required observations, all derived; studies may append their own sentences."""
    annual = context["annual"]
    facts = derived["facts"]
    lines = [HEAD_INTERPRETATION, ""]
    lines.append(
        f"- 余额从 {fmt_money(annual['starting_total_balance_usd'].iloc[0])} USDT 增至 "
        f"{fmt_money(annual['ending_total_balance_usd'].iloc[-1])} USDT；策略权益从 "
        f"{fmt_money(derived['start_equity'])} 增至 {fmt_money(derived['end_equity'])} USDT，"
        f"约增长 {fmt_pct(derived['equity_growth_pct'])}。分析指标 `gain_usd` 为 "
        f"{fmt_ratio(facts['gain_usd'], 6)} 倍，`gain_strategy_eq` 为 "
        f"{fmt_ratio(facts['gain_strategy_eq'], 6)} 倍；该指标使用尾部日度权益，因此不应与采样终点直接等同。"
    )
    lines.append(
        f"- 分段归因：收益最好的自然年是 {derived['best_year']['period']} 年"
        f"（权益 {fmt_pct(derived['best_year']['strategy_equity_return_pct'])}，"
        f"余额 {fmt_pct(derived['best_year']['total_balance_return_pct'])}）；"
        f"年内回撤最深的是 {derived['worst_year']['period']} 年"
        f"（{fmt_pct(derived['worst_year']['max_intraperiod_strategy_equity_drawdown_pct'])}）。"
        f"全期非完整年 {len(annual) - derived['full_years']} 个，其余为完整年。"
    )
    lines.append(
        f"- 风险特征：全期 USD 最差回撤 {fmt_pct(facts['drawdown_worst_usd'])}，strategy equity 最差回撤 "
        f"{fmt_pct(facts['drawdown_worst_strategy_eq'])}，最差 1% 均值回撤 "
        f"{fmt_pct(facts['drawdown_worst_mean_1pct_strategy_eq'])}；最长 PnL 峰值恢复期 "
        f"{float(facts['strategy_eq_recovery_days_max'] or 0.0):,.2f} 天，水下时间占比均值 "
        f"{fmt_pct(facts['strategy_eq_underwater_pct_mean'])}。PnL Sharpe {fmt_ratio(facts['sharpe_ratio_pnl'])} / "
        f"Sortino {fmt_ratio(facts['sortino_ratio_pnl'])}，风险调整后的收益质量需要与回撤一并阅读。"
    )
    attribution = context["attribution"]
    long_row = next((row for row in attribution if row["direction"] == "多头"), None)
    short_row = next((row for row in attribution if row["direction"] == "空头"), None)
    if long_row is None or short_row is None:
        # A context that drops a direction is a caller bug, not a rendering case: the
        # attribution table is always two rows, so the interpretation must be too.
        raise ValueError("attribution must contain exactly the 多头 and 空头 rows")
    lines.append(
        f"- 成交结构：多头 {long_row['fills_count']:,} 笔、空头 {short_row['fills_count']:,} 笔；"
        f"maker {derived['maker_total']:,} / taker {derived['taker_total']:,}。"
        f"多头净已实现 PnL {fmt_money(long_row['net_realized_pnl_usd'])} USDT，"
        f"空头 {fmt_money(short_row['net_realized_pnl_usd'])} USDT。"
    )
    lines.append(
        "- 本报告不构成对未来的预测；全部结论只在上述有效区间、执行口径与成本假设下成立。"
    )
    for sentence in context.get("interpretation_extra") or []:
        lines.append(f"- {sentence}")
    lines.append("")
    return "\n".join(lines)


def render_appendix(context: dict[str, Any], derived: dict[str, Any]) -> str:
    """Single mount point for study-specific sections; absent when nothing is supplied."""
    appendix = (context.get("appendix") or "").strip()
    if not appendix:
        return ""
    return f"{HEAD_APPENDIX}\n\n{appendix}\n"


def render_annual_analysis(context: dict[str, Any]) -> str:
    """Render the full report. `context` is documented in `docs/ai/runbooks/strategy_report.md`."""
    required = ("analysis", "config", "annual", "monthly", "coins", "attribution")
    missing = [key for key in required if key not in context]
    if missing:
        raise ValueError(f"report context is missing required keys: {missing}")
    derived = build_facts(context)
    parts = [
        context.get("title") or default_title(context, derived),
        "",
        render_scope(context, derived),
        render_overall(context, derived),
        render_attribution(context, derived),
        render_annual(context, derived),
        render_monthly(context, derived),
        render_coin_contribution(context, derived),
    ]
    sections = context.get("sections") or {}
    for key, renderer in (
        ("strategy_scope", None),
        ("risk_measurement", None),
        ("execution_boundaries", None),
        ("sensitivity", None),
    ):
        text = (sections.get(key) or "").strip()
        if text:
            parts.append(text + "\n")
    appendix = render_appendix(context, derived)
    if appendix:
        parts.append(appendix)
    parts.append(render_verifiable(context, derived))
    parts.append(render_interpretation(context, derived))
    text = "\n".join(part for part in parts if part is not None)
    return text if text.endswith("\n") else text + "\n"


def default_title(context: dict[str, Any], derived: dict[str, Any]) -> str:
    run_record = context.get("run_record") or {}
    label = run_record.get("candidate_id") or context.get("result_label") or "backtest"
    exchange = str((run_record.get("universe", {}) or {}).get("exchange", "binance"))
    kind = context.get("title_kind") or "策略深度分析"
    span = derived["title_span"]
    return f"# {kind}：`{label}`（{exchange.capitalize()} 永续 1 分钟 {span}回测）"


# --------------------------------------------------------------------------------------
# Structure gate
# --------------------------------------------------------------------------------------


def report_headings(text: str, include_detail: bool = False) -> list[str]:
    """Headings in document order; level-3 per-year details are included on request."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            out.append(stripped)
        elif include_detail and re.match(r"^### \d{4} 年明细（.+）$", stripped):
            out.append(stripped)
    return out


def detail_headings(text: str) -> list[str]:
    """Per-year detail sections, which are level-3 headings in the reference report."""
    return [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^### \d{4} 年明细（.+）$", line.strip())
    ]


def assert_report_structure(text: str) -> list[str]:
    """Return a list of structural problems; empty means the skeleton is intact."""
    problems: list[str] = []
    headings = report_headings(text)
    fixed = [HEAD_SCOPE, HEAD_OVERALL, HEAD_ATTRIBUTION, HEAD_ANNUAL, HEAD_MONTHLY]
    cursor = 0
    for heading in fixed:
        if heading not in headings:
            problems.append(f"missing section {heading!r}")
            continue
        index = headings.index(heading)
        if index < cursor:
            problems.append(f"section {heading!r} is out of order")
        cursor = index
    detail = detail_headings(text)
    if not detail:
        problems.append("missing per-year detail sections")
    else:
        sequence = report_headings(text, include_detail=True)
        annual_index = sequence.index(HEAD_ANNUAL) if HEAD_ANNUAL in sequence else -1
        monthly_index = sequence.index(HEAD_MONTHLY) if HEAD_MONTHLY in sequence else len(sequence)
        for heading in detail:
            position = sequence.index(heading)
            if not (annual_index < position < monthly_index):
                problems.append(f"per-year detail {heading!r} is outside the annual/monthly block")
        if detail != sorted(detail):
            problems.append("per-year detail sections are not in chronological order")
    if HEAD_APPENDIX in headings:
        appendix_position = headings.index(HEAD_APPENDIX)
        if HEAD_MONTHLY in headings and appendix_position < headings.index(HEAD_MONTHLY):
            problems.append("研究附录 must follow the fixed report skeleton")
        if HEAD_VERIFIABLE in headings and appendix_position > headings.index(HEAD_VERIFIABLE):
            problems.append("研究附录 must precede 可复核数据")
    tail = [HEAD_VERIFIABLE, HEAD_INTERPRETATION]
    tail_positions = [headings.index(h) for h in tail if h in headings]
    for heading in tail:
        if heading not in headings:
            problems.append(f"missing section {heading!r}")
    if len(tail_positions) == 2 and tail_positions != sorted(tail_positions):
        problems.append("可复核数据 must precede 结果解读")
    if tail_positions and tail_positions[-1] != len(headings) - 1:
        problems.append("结果解读 must be the last section")
    return problems
# --------------------------------------------------------------------------------------
# Standalone CLI
# --------------------------------------------------------------------------------------


def build_context_from_artifacts(
    result_dir: Path,
    run_record: dict[str, Any] | None = None,
    appendix: str | None = None,
    report_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result_dir = Path(result_dir)
    analysis = load_json(result_dir / "analysis.json")
    cfg = load_json(result_dir / "config.json")
    fills = load_fills(result_dir)
    equity = load_balance_equity(result_dir)
    annual = build_period_table(equity, fills, "Y")
    monthly = build_period_table(equity, fills, "M")
    coins = build_coin_table(fills)
    context: dict[str, Any] = {
        "analysis": analysis,
        "config": cfg,
        "annual": annual,
        "monthly": monthly,
        "coins": coins,
        "attribution": build_attribution_table(fills),
        "run_record": run_record or {},
        "result_label": str(result_dir),
        "appendix": appendix or "",
    }
    context.update(report_notes or {})
    context["_tables"] = {"annual": annual, "monthly": monthly, "coins": coins, "equity": equity}
    return context


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", required=True, help="completed backtest artifact directory")
    parser.add_argument(
        "--run-record",
        default=None,
        help="run_record.json for the bundle; optional but the report cites it when present",
    )
    parser.add_argument("--report-title", default=None, help="override the derived title")
    parser.add_argument("--appendix", default=None, help="markdown fragment for the 研究附录 section")
    parser.add_argument("--out", default=None, help="defaults to <result-dir>/annual_analysis.md")
    parser.add_argument(
        "--write-csv",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write annual/monthly/coin metric CSVs next to the report (default: yes)",
    )
    args = parser.parse_args(argv)

    result_dir = Path(args.result_dir).resolve()
    run_record = load_json(args.run_record) if args.run_record else {}
    appendix = Path(args.appendix).read_text(encoding="utf-8") if args.appendix else None
    context = build_context_from_artifacts(result_dir, run_record, appendix)
    if args.report_title:
        context["title"] = args.report_title
    report = render_annual_analysis(context)
    problems = assert_report_structure(report)
    if problems:
        for problem in problems:
            print(f"structure problem: {problem}", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else result_dir / "annual_analysis.md"
    out.write_text(report, encoding="utf-8")
    print(f"wrote {out}")
    if args.write_csv:
        tables = context["_tables"]
        tables["annual"].to_csv(result_dir / "annual_metrics.csv", index=False)
        tables["monthly"].to_csv(result_dir / "monthly_metrics.csv", index=False)
        tables["coins"].to_csv(result_dir / "coin_metrics.csv", index=False)
        print(f"wrote annual_metrics.csv ({len(tables['annual'])} rows)")
        print(f"wrote monthly_metrics.csv ({len(tables['monthly'])} rows)")
        print(f"wrote coin_metrics.csv ({len(tables['coins'])} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())