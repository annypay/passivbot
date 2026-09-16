#!/usr/bin/env python3
"""Verify the `hsl_npos1` deep-analysis report against its own artifacts.

This script deliberately does **not** import the renderer
(`report_tools/render_hsl_npos1_report.py`). It recomputes every number the report publishes
from `fills.csv`, `balance_and_equity.csv.gz`, `analysis.json`, `config.json` and
`dataset.json` with its own code, then compares the markdown tables cell by cell. A renderer
regression therefore has to disagree with an independent implementation, not merely with
itself.

Checks:

1. artifact completeness and the frozen run contract (`config.json` / `dataset.json` vs
   `hsl_npos1_spec`);
2. the section skeleton and the per-year detail headings
   (`backtests/report_spec/annual_analysis.py`, which owns the convention);
3. the three metric CSVs against independently recomputed tables, including column schemas;
4. the report's `## 总体结果`, `## 多空成交归因`, `## 按币种贡献`, `## 月度汇总` and per-year detail
   blocks against the recomputed values;
5. `analysis.json` self-consistency and the zero-BTC-collateral ledger identity.

Exit code is non-zero when anything fails, and the last line reads `failed: N`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hsl_npos1_spec as study  # noqa: E402

REPORT_SPEC_DIR = study.REPO / "backtests" / "report_spec"
if str(REPORT_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(REPORT_SPEC_DIR))
import annual_analysis as spec  # noqa: E402  (owns REPORT_SECTIONS and the CSV schemas)

PROBLEMS: list[str] = []


def fail(message: str) -> None:
    PROBLEMS.append(message)


def check(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def approx(left: Any, right: Any, rel_tol: float = 1e-9, abs_tol: float = 1e-6) -> bool:
    """Close enough for a re-derivation of the same series.

    Tolerances are relative first because the ledger carries five- and six-figure USDT values;
    an absolute-only comparison would flag every large money cell as a mismatch.
    """
    try:
        return math.isclose(float(left), float(right), rel_tol=rel_tol, abs_tol=abs_tol)
    except (TypeError, ValueError):
        return False


def money_close(left: Any, right: Any) -> bool:
    return approx(left, right, rel_tol=1e-6, abs_tol=1e-4)


def ratio_close(left: Any, right: Any) -> bool:
    return approx(left, right, rel_tol=1e-6, abs_tol=1e-9)


# --------------------------------------------------------------------------------------
# Loaders (independent of the renderer)
# --------------------------------------------------------------------------------------


def load_fills(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(result_dir / "fills.csv")
    frame = frame.loc[:, [c for c in frame.columns if not c.startswith("Unnamed")]]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def load_equity(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(result_dir / "balance_and_equity.csv.gz", compression="gzip")
    frame = frame.rename(columns={frame.columns[0]: "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in frame.columns:
        if column != "timestamp":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def as_text(value: Any) -> str:
    """`''` for empty cells: pandas reads an empty CSV cell back as NaN."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value)


# --------------------------------------------------------------------------------------
# Independent recomputation of the report's tables
# --------------------------------------------------------------------------------------


def max_drawdown(values: pd.Series) -> float:
    array = values.to_numpy(dtype=float)
    array = array[np.isfinite(array)]
    if array.size < 2:
        return float("nan")
    peak = np.maximum.accumulate(array)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(peak > 0.0, array / peak, 1.0)
    return float(1.0 - np.nanmin(ratio))


def period_slices(equity: pd.DataFrame, fills: pd.DataFrame, frequency: str):
    fmt = "%Y" if frequency == "Y" else "%Y-%m"
    equity = equity.assign(_period=equity["timestamp"].dt.strftime(fmt))
    fills = fills.assign(_period=fills["timestamp"].dt.strftime(fmt))
    keys = sorted(equity["_period"].unique())
    for position, key in enumerate(keys):
        if position == 0:
            coverage = spec.COVERAGE_START
        elif position == len(keys) - 1:
            coverage = spec.COVERAGE_END
        else:
            coverage = spec.COVERAGE_FULL
        yield position, key, coverage, equity.loc[equity["_period"] == key], fills.loc[fills["_period"] == key]


def recompute_period_table(equity: pd.DataFrame, fills: pd.DataFrame, frequency: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, key, coverage, eq, sub in period_slices(equity, fills, frequency):
        balance = eq["usd_total_balance"].dropna()
        total = eq["usd_total_equity"].dropna()
        strat = eq["strategy_equity"].dropna()
        types = sub["type"].astype(str)
        entry = types.str.startswith("entry_")
        liquidity = sub["liquidity"].astype(str)
        if balance.empty:
            start_balance = end_balance = float("nan")
        else:
            start_balance, end_balance = float(balance.iloc[0]), float(balance.iloc[-1])
        start_total, end_total = float(total.iloc[0]), float(total.iloc[-1])
        start_strat, end_strat = float(strat.iloc[0]), float(strat.iloc[-1])
        rows.append(
            {
                "period": key,
                "sample_start_utc": str(eq["timestamp"].iloc[0]).replace("T", " ", 1),
                "sample_end_utc": str(eq["timestamp"].iloc[-1]).replace("T", " ", 1),
                "coverage": coverage,
                "starting_total_balance_usd": start_balance,
                "ending_total_balance_usd": end_balance,
                "total_balance_return_pct": (end_balance / start_balance - 1.0),
                "starting_total_equity_usd": start_total,
                "ending_total_equity_usd": end_total,
                "total_equity_return_pct": (end_total / start_total - 1.0),
                "starting_strategy_equity": start_strat,
                "ending_strategy_equity": end_strat,
                "strategy_equity_return_pct": (end_strat / start_strat - 1.0),
                "max_intraperiod_equity_drawdown_pct": max_drawdown(eq["usd_total_equity"]),
                "max_intraperiod_strategy_equity_drawdown_pct": max_drawdown(eq["strategy_equity"]),
                "fills_count": int(len(sub)),
                "entry_fills_count": int(entry.sum()),
                "reduction_or_close_fills_count": int((~entry).sum()),
                "long_fills_count": int(types.str.contains("long").sum()),
                "short_fills_count": int(types.str.contains("short").sum()),
                "maker_fills_count": int((liquidity == "maker").sum()),
                "taker_fills_count": int((liquidity == "taker").sum()),
                "realized_pnl_raw_usd": float(sub["pnl"].sum()) if len(sub) else 0.0,
                "fees_signed_usd": float(sub["fee_paid"].sum()) if len(sub) else 0.0,
                "net_realized_pnl_usd": (
                    float(sub["pnl"].sum() + sub["fee_paid"].sum()) if len(sub) else 0.0
                ),
                "max_abs_wallet_exposure_at_fill": (
                    float(sub["wallet_exposure"].abs().max()) if len(sub) else float("nan")
                ),
                "active_coins": ",".join(sorted(sub["coin"].astype(str).unique())) if len(sub) else "",
            }
        )
    return pd.DataFrame(rows)


def recompute_coin_table(fills: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for coin, sub in fills.groupby("coin", sort=False):
        types = sub["type"].astype(str)
        entry = types.str.startswith("entry_")
        liquidity = sub["liquidity"].astype(str)
        rows.append(
            {
                "coin": coin,
                "fills_count": int(len(sub)),
                "entry_fills_count": int(entry.sum()),
                "reduction_or_close_fills_count": int((~entry).sum()),
                "maker_fills_count": int((liquidity == "maker").sum()),
                "taker_fills_count": int((liquidity == "taker").sum()),
                "realized_pnl_raw_usd": float(sub["pnl"].sum()),
                "fees_signed_usd": float(sub["fee_paid"].sum()),
                "net_realized_pnl_usd": float(sub["pnl"].sum() + sub["fee_paid"].sum()),
                "max_abs_wallet_exposure_at_fill": float(sub["wallet_exposure"].abs().max()),
                "first_fill_utc": str(sub["timestamp"].min()).replace("T", " ", 1),
                "last_fill_utc": str(sub["timestamp"].max()).replace("T", " ", 1),
            }
        )
    table = pd.DataFrame(rows)
    return table.sort_values("coin", kind="stable").reset_index(drop=True)


def recompute_attribution(fills: pd.DataFrame) -> list[dict[str, Any]]:
    types = fills["type"].astype(str)
    rows = []
    for label, mask in (("多头", types.str.contains("long")), ("空头", types.str.contains("short"))):
        sub = fills.loc[mask]
        rows.append(
            {
                "direction": label,
                "fills_count": int(len(sub)),
                "entry_fills_count": int(sub["type"].astype(str).str.startswith("entry_").sum()),
                "realized_pnl_raw_usd": float(sub["pnl"].sum()) if len(sub) else 0.0,
                "fees_signed_usd": float(sub["fee_paid"].sum()) if len(sub) else 0.0,
                "net_realized_pnl_usd": (
                    float(sub["pnl"].sum() + sub["fee_paid"].sum()) if len(sub) else 0.0
                ),
                "max_abs_wallet_exposure_at_fill": (
                    float(sub["wallet_exposure"].abs().max()) if len(sub) else float("nan")
                ),
            }
        )
    return rows


# --------------------------------------------------------------------------------------
# Markdown parsing and formatting
# --------------------------------------------------------------------------------------


def parse_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    tables: list[tuple[list[str], list[list[str]]]] = []
    header: list[str] | None = None
    rows: list[list[str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            if header is not None:
                tables.append((header, rows))
                header, rows = None, []
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if all(cell and set(cell) <= {"-", ":"} for cell in cells):
            continue
        if header is None:
            header = cells
        else:
            rows.append(cells)
    if header is not None:
        tables.append((header, rows))
    return tables


def table_with_header(text: str, header: list[str]) -> list[list[str]] | None:
    for found_header, rows in parse_tables(text):
        if found_header == header:
            return rows
    return None


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


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def check_contract(result_dir: Path) -> tuple[dict, dict, dict, list[str]]:
    # The convention's persisted-layout contract, checked before anything is read from it, so a
    # half-written bundle fails as a layout problem instead of as a confusing missing key.
    for problem in spec.assert_bundle_layout(result_dir):
        fail(f"bundle layout: {problem}")
    analysis = load_json(result_dir / "analysis.json")
    cfg = load_json(result_dir / "config.json")
    dataset = load_json(result_dir / "dataset.json")

    bt = cfg.get("backtest", {})
    expected_scalars = (
        ("execution_delay_bars", study.EXECUTION["execution_delay_bars"]),
        ("intrabar_fill_order", study.EXECUTION["intrabar_fill_order"]),
        ("maker_fee_override", study.COSTS["maker_fee_override"]),
        ("taker_fee_override", study.COSTS["taker_fee_override"]),
        ("balance_sample_divider", study.BALANCE_SAMPLE_DIVIDER),
        ("candle_interval_minutes", study.CANDLE_INTERVAL_MINUTES),
        ("btc_collateral_cap", 0.0),
    )
    for key, expected in expected_scalars:
        got = bt.get(key)
        if isinstance(expected, str):
            check(got == expected, f"backtest.{key} drift: {got!r} != {expected!r}")
        else:
            check(approx(got, expected), f"backtest.{key} drift: {got!r} != {expected!r}")
    check(bt.get("exchanges") == ["binance"], f"exchanges drift: {bt.get('exchanges')}")
    check(
        str(bt.get("start_date")) == study.REQUESTED_START,
        f"start_date drift: {bt.get('start_date')} != {study.REQUESTED_START}",
    )
    check(
        str(bt.get("end_date")) == study.REQUESTED_END[:10],
        f"end_date drift: {bt.get('end_date')} != {study.REQUESTED_END[:10]}",
    )

    # The dataset is the authority for the run basket: `backtest.coins` is consumed during
    # preparation and is not part of the written run config.
    effective = list(dataset.get("coins") or [])
    check(bool(effective), "materialized dataset declares no coins")
    check(
        sorted(dataset.get("side_membership", {}).get("long") or []) == sorted(effective),
        "dataset side_membership.long differs from the materialized coin list",
    )
    for coin in study.EXCLUDED_COINS:
        check(coin not in effective, f"excluded coin {coin} is in the run basket")
        check(
            coin not in (cfg.get("live", {}).get("approved_coins", {}).get("long") or []),
            f"excluded coin {coin} is still in live.approved_coins.long",
        )
    check(
        sorted(cfg.get("live", {}).get("approved_coins", {}).get("long") or []) == sorted(effective),
        "live.approved_coins.long and the materialized dataset disagree",
    )
    check(
        not (cfg.get("live", {}).get("approved_coins", {}).get("short") or []),
        "this profile is long-only; live.approved_coins.short must stay empty",
    )
    check(
        bool(dataset.get("dataset_override")) is False,
        "dataset_override should be False: the run materializes from the local catalog",
    )
    check(
        approx(analysis.get("backtest_completion_ratio"), 1.0),
        f"backtest_completion_ratio = {analysis.get('backtest_completion_ratio')}",
    )
    check(not analysis.get("liquidated"), "run simulated a liquidation")
    return analysis, cfg, dataset, effective


def check_ledger_identity(equity: pd.DataFrame) -> None:
    check(
        bool(
            np.allclose(
                equity["strategy_equity"].to_numpy(dtype=float),
                equity["usd_total_equity"].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            )
        ),
        "strategy_equity != usd_total_equity although btc_collateral_cap = 0",
    )


def compare_period_table(name: str, csv_path: Path, recomputed: pd.DataFrame) -> pd.DataFrame | None:
    if not csv_path.exists():
        fail(f"{name}: {csv_path.name} is missing")
        return None
    csv = pd.read_csv(csv_path)
    check(
        list(csv.columns) == list(spec.PERIOD_CSV_COLUMNS),
        f"{name}: columns differ from the convention: {list(csv.columns)}",
    )
    check(len(csv) == len(recomputed), f"{name}: rows {len(csv)} != recomputed {len(recomputed)}")
    if len(csv) != len(recomputed):
        return None
    for index in range(len(csv)):
        check(
            as_text(csv["period"].iloc[index]) == str(recomputed["period"].iloc[index]),
            f"{name}: period[{index}] {csv['period'].iloc[index]!r} != "
            f"{recomputed['period'].iloc[index]!r}",
        )
        check(
            as_text(csv["coverage"].iloc[index]) == str(recomputed["coverage"].iloc[index]),
            f"{name}: coverage[{index}] mismatch",
        )
        check(
            as_text(csv["active_coins"].iloc[index]) == str(recomputed["active_coins"].iloc[index]),
            f"{name}: active_coins[{index}] {csv['active_coins'].iloc[index]!r} != "
            f"{recomputed['active_coins'].iloc[index]!r}",
        )
    for column in (
        "fills_count",
        "entry_fills_count",
        "reduction_or_close_fills_count",
        "long_fills_count",
        "short_fills_count",
        "maker_fills_count",
        "taker_fills_count",
    ):
        for index in range(len(csv)):
            check(
                int(csv[column].iloc[index]) == int(recomputed[column].iloc[index]),
                f"{name}: {column}[{index}] {csv[column].iloc[index]} != "
                f"{recomputed[column].iloc[index]}",
            )
    for column in (
        "starting_total_balance_usd",
        "ending_total_balance_usd",
        "starting_total_equity_usd",
        "ending_total_equity_usd",
        "starting_strategy_equity",
        "ending_strategy_equity",
        "realized_pnl_raw_usd",
        "fees_signed_usd",
        "net_realized_pnl_usd",
    ):
        for index in range(len(csv)):
            check(
                money_close(csv[column].iloc[index], recomputed[column].iloc[index]),
                f"{name}: {column}[{index}] {csv[column].iloc[index]} != "
                f"{recomputed[column].iloc[index]}",
            )
    for column in (
        "total_balance_return_pct",
        "total_equity_return_pct",
        "strategy_equity_return_pct",
        "max_intraperiod_equity_drawdown_pct",
        "max_intraperiod_strategy_equity_drawdown_pct",
    ):
        for index in range(len(csv)):
            check(
                ratio_close(csv[column].iloc[index], recomputed[column].iloc[index]),
                f"{name}: {column}[{index}] {csv[column].iloc[index]} != "
                f"{recomputed[column].iloc[index]}",
            )
    return csv


def check_year_details(report: str, annual: pd.DataFrame) -> None:
    details = spec.detail_headings(report)
    check(len(details) == len(annual), f"per-year details {len(details)} != annual rows {len(annual)}")
    sections = report.split("\n### ")
    for heading, (_, row) in zip(details, annual.iterrows()):
        check(
            heading == f"### {row['period']} 年明细（{row['coverage']}）",
            f"year detail heading mismatch: {heading}",
        )
        body = next((part for part in sections if part.startswith(heading[4:])), None)
        if body is None:
            fail(f"no body found for year detail {heading}")
            continue
        check(
            str(row["sample_start_utc"]) in body and str(row["sample_end_utc"]) in body,
            f"{heading}: sample interval {row['sample_start_utc']} .. {row['sample_end_utc']} "
            "is not stated in the block",
        )


def check_monthly_table(report: str, monthly: pd.DataFrame) -> None:
    header = [
        "月份",
        "覆盖",
        "最终余额",
        "余额收益率",
        "权益收益率",
        "月内权益最大回撤",
        "成交数",
        "净已实现 PnL",
    ]
    rows = table_with_header(report, header)
    if rows is None:
        fail("report has no 月度汇总 table")
        return
    check(len(rows) == len(monthly), f"月度汇总 rows {len(rows)} != recomputed {len(monthly)}")
    for index in range(min(len(rows), len(monthly))):
        cells = rows[index]
        row = monthly.iloc[index]
        expected = [
            str(row["period"]),
            str(row["coverage"]),
            fmt_money(row["ending_total_balance_usd"]),
            fmt_pct(row["total_balance_return_pct"]),
            fmt_pct(row["strategy_equity_return_pct"]),
            fmt_pct(row["max_intraperiod_strategy_equity_drawdown_pct"]),
            f"{int(row['fills_count']):,}",
            fmt_money(row["net_realized_pnl_usd"]),
        ]
        for column, (got, want) in enumerate(zip(cells, expected)):
            check(
                got == want,
                f"月度汇总 row {index} column {header[column]}: {got!r} != {want!r}",
            )


def check_coin_table(result_dir: Path, report: str, recomputed: pd.DataFrame) -> None:
    csv_path = result_dir / "coin_metrics.csv"
    if not csv_path.exists():
        fail("coin_metrics.csv is missing")
        return
    csv = pd.read_csv(csv_path)
    # The CSV is alphabetical by coin; the report table is descending by net PnL. Read the CSV
    # in its own documented order and index it by coin for the report comparison.
    csv = csv.sort_values("coin", kind="stable").reset_index(drop=True)
    check(
        list(csv.columns) == list(spec.COIN_CSV_COLUMNS),
        f"coin_metrics.csv columns differ from the convention: {list(csv.columns)}",
    )
    check(
        list(csv["coin"]) == list(recomputed["coin"]),
        f"coin_metrics.csv coin order differs: {list(csv['coin'])} vs {list(recomputed['coin'])}",
    )
    check(len(csv) == len(recomputed), f"coin_metrics.csv rows {len(csv)} != {len(recomputed)}")
    if len(csv) != len(recomputed):
        return
    for index in range(len(csv)):
        for column in ("fills_count", "entry_fills_count", "reduction_or_close_fills_count"):
            check(
                int(csv[column].iloc[index]) == int(recomputed[column].iloc[index]),
                f"coin_metrics.csv {column}[{index}] mismatch",
            )
        for column in ("net_realized_pnl_usd", "fees_signed_usd", "realized_pnl_raw_usd"):
            check(
                money_close(csv[column].iloc[index], recomputed[column].iloc[index]),
                f"coin_metrics.csv {column}[{index}] mismatch: "
                f"{csv[column].iloc[index]} != {recomputed[column].iloc[index]}",
            )

    header = [
        "币种",
        "成交数",
        "入场",
        "减仓或平仓",
        "已实现 PnL",
        "手续费",
        "净已实现 PnL",
        "成交时最大绝对钱包敞口",
    ]
    rows = table_with_header(report, header)
    if rows is None:
        fail("report has no 按币种贡献 table")
        return
    check(len(rows) == len(csv), f"按币种贡献 rows {len(rows)} != coin rows {len(csv)}")

    # The convention sorts the report table by net realized PnL, descending; the CSV is
    # alphabetical. Both orderings are verified against the independently computed values.
    by_coin = {str(row["coin"]): row for _, row in csv.iterrows()}
    nets = []
    for index, cells in enumerate(rows):
        coin = cells[0]
        if coin not in by_coin:
            fail(f"按币种贡献 row {index}: unknown coin {coin!r}")
            continue
        row = by_coin[coin]
        nets.append(float(row["net_realized_pnl_usd"]))
        check(
            cells[1] == f"{int(row['fills_count']):,}",
            f"按币种贡献 {coin} fills: {cells[1]}",
        )
        check(
            cells[2] == f"{int(row['entry_fills_count']):,}",
            f"按币种贡献 {coin} entries: {cells[2]}",
        )
        check(
            cells[3] == f"{int(row['reduction_or_close_fills_count']):,}",
            f"按币种贡献 {coin} reductions: {cells[3]}",
        )
        check(
            cells[4] == fmt_money(row["realized_pnl_raw_usd"]),
            f"按币种贡献 {coin} realized: {cells[4]}",
        )
        check(cells[5] == fmt_money(row["fees_signed_usd"]), f"按币种贡献 {coin} fees: {cells[5]}")
        check(
            cells[6] == fmt_money(row["net_realized_pnl_usd"]),
            f"按币种贡献 {coin} net: {cells[6]}",
        )
    check(
        nets == sorted(nets, reverse=True),
        "按币种贡献 must be sorted by net realized PnL descending",
    )
    # The section's prose must name the first and last rows of its own table, which is the
    # order the convention requires (descending net realized PnL).
    if rows:
        head_coin, tail_coin = rows[0][0], rows[-1][0]
        section = report.split(spec.HEAD_COIN_CONTRIBUTION, 1)[-1].split("## ", 1)[0]
        check(
            f"净贡献最高 {head_coin}" in section,
            f"按币种贡献 prose does not name {head_coin} as the top net contributor",
        )
        check(
            f"最低 {tail_coin}" in section,
            f"按币种贡献 prose does not name {tail_coin} as the lowest net contributor",
        )


def check_attribution(report: str, recomputed: list[dict[str, Any]]) -> None:
    header = [
        "方向",
        "成交数",
        "入场数",
        "已实现 PnL",
        "手续费",
        "净已实现 PnL",
        "成交时最大绝对钱包敞口",
    ]
    rows = table_with_header(report, header)
    if rows is None:
        fail("report has no 多空成交归因 table")
        return
    check(len(rows) == 2, f"多空成交归因 must have exactly two rows, found {len(rows)}")
    for index, expected in enumerate(recomputed[: len(rows)]):
        cells = rows[index]
        check(cells[0] == expected["direction"], f"归因 row {index} direction {cells[0]}")
        check(
            cells[1] == f"{expected['fills_count']:,}",
            f"归因 {expected['direction']} fills: {cells[1]} != {expected['fills_count']:,}",
        )
        check(
            cells[2] == f"{expected['entry_fills_count']:,}",
            f"归因 {expected['direction']} entries: {cells[2]}",
        )
        check(
            cells[3] == fmt_money(expected["realized_pnl_raw_usd"]),
            f"归因 {expected['direction']} realized: {cells[3]}",
        )
        check(
            cells[4] == fmt_money(expected["fees_signed_usd"]),
            f"归因 {expected['direction']} fees: {cells[4]}",
        )
        check(
            cells[5] == fmt_money(expected["net_realized_pnl_usd"]),
            f"归因 {expected['direction']} net pnl: {cells[5]}",
        )


def check_overall_table(
    report: str, analysis: dict, annual: pd.DataFrame, equity: pd.DataFrame
) -> None:
    rows = table_with_header(report, ["项目", "数值"])
    if rows is None:
        fail("report has no 总体结果 table")
        return
    values = {row[0]: row[1] for row in rows if len(row) >= 2}
    facts = spec.analysis_facts(analysis)

    def require(label: str, expected: str) -> None:
        if label not in values:
            fail(f"总体结果 is missing row {label!r}")
            return
        check(values[label] == expected, f"总体结果 {label}: {values[label]!r} != {expected!r}")

    require("USD gain 倍数（分析指标）", fmt_ratio(facts["gain_usd"], 6))
    require(
        "USD 最差回撤 / strategy equity 最差回撤",
        f"{fmt_pct(facts['drawdown_worst_usd'])} / {fmt_pct(facts['drawdown_worst_strategy_eq'])}",
    )
    require("最差 1% 均值回撤", fmt_pct(facts["drawdown_worst_mean_1pct_strategy_eq"]))
    require(
        "PnL Sharpe / Sortino",
        f"{fmt_ratio(facts['sharpe_ratio_pnl'])} / {fmt_ratio(facts['sortino_ratio_pnl'])}",
    )
    require(
        "起始 / 最终 USD 总余额",
        f"{fmt_money(annual['starting_total_balance_usd'].iloc[0])} / "
        f"{fmt_money(annual['ending_total_balance_usd'].iloc[-1])} USDT",
    )
    require(
        "成交数 / 强平",
        f"{int(facts['fills_count']):,} / {'是' if facts['liquidated'] else '否'}",
    )

    recomputed_dd = max_drawdown(equity["strategy_equity"])
    check(
        ratio_close(recomputed_dd, facts["drawdown_worst_strategy_eq"]),
        f"recomputed strategy-equity drawdown {recomputed_dd} != analysis.json "
        f"{facts['drawdown_worst_strategy_eq']}",
    )
    check(
        ratio_close(max_drawdown(equity["usd_total_equity"]), facts["drawdown_worst_usd"]),
        "recomputed USD drawdown != analysis.json drawdown_worst_usd",
    )


def check_analysis_consistency(analysis: dict, fills: pd.DataFrame) -> None:
    types = fills["type"].astype(str)
    expected = {
        "fills_count": len(fills),
        "fills_count_entry": int(types.str.startswith("entry_").sum()),
        "fills_count_close": int((~types.str.startswith("entry_")).sum()),
        "fills_count_long": int(types.str.contains("long").sum()),
        "fills_count_short": int(types.str.contains("short").sum()),
        "fills_active_symbols_count": int(fills["coin"].nunique()),
    }
    for key, value in expected.items():
        check(
            int(analysis.get(key, -1)) == int(value),
            f"analysis.json {key} = {analysis.get(key)} != recomputed {value}",
        )


def check_lifecycle_block(report: str, fills: pd.DataFrame, equity: pd.DataFrame, analysis: dict) -> None:
    """The lifecycle section is the report's central claim, so it is recomputed too."""
    head = "### 交易生命周期与硬停（本报告的核心事实）"
    if head not in report:
        fail("report has no lifecycle section")
        return
    section = report.split(head, 1)[1].split("\n### ", 1)[0]
    rows = table_with_header(section, ["项目", "数值"])
    if rows is None:
        fail("lifecycle section has no 项目/数值 table")
        return
    values = {row[0]: row[1] for row in rows if len(row) >= 2}

    first = fills["timestamp"].min()
    last = fills["timestamp"].max()
    active_days = int(fills["timestamp"].dt.floor("D").nunique())
    idle_days = (equity["timestamp"].iloc[-1] - last).total_seconds() / 86400.0
    panic = fills.loc[fills["type"].astype(str) == "close_panic_long"]

    expected_span = (
        f"{first.strftime('%Y-%m-%d %H:%M:%S+00:00')} → {last.strftime('%Y-%m-%d %H:%M:%S+00:00')}"
    )
    if "实际交易区间（首笔 → 末笔成交）" not in values:
        fail("lifecycle table is missing the trading span row")
    else:
        check(
            values["实际交易区间（首笔 → 末笔成交）"] == expected_span,
            f"lifecycle span: {values['实际交易区间（首笔 → 末笔成交）']!r} != {expected_span!r}",
        )
    if "有成交的自然日数" in values:
        check(
            values["有成交的自然日数"].startswith(f"{active_days} 天"),
            f"lifecycle active days: {values['有成交的自然日数']!r}",
        )
    else:
        fail("lifecycle table is missing the active-days row")
    if "`close_panic_long` 笔数 / 时间" in values:
        check(
            values["`close_panic_long` 笔数 / 时间"].startswith(f"{len(panic):,} 笔"),
            f"lifecycle panic row: {values['`close_panic_long` 笔数 / 时间']!r}",
        )
    else:
        fail("lifecycle table is missing the panic row")
    if "`analysis.json` 硬停计数 / 重启计数" in values:
        expected = f"{analysis.get('hard_stop_triggers')} / {analysis.get('hard_stop_restarts')}"
        check(
            values["`analysis.json` 硬停计数 / 重启计数"] == expected,
            f"lifecycle hard-stop row: {values['`analysis.json` 硬停计数 / 重启计数']!r} != {expected!r}",
        )
    else:
        fail("lifecycle table is missing the hard-stop row")
    check(
        idle_days > 0 and f"{idle_days:,.2f}" in section,
        f"lifecycle section does not state the {idle_days:,.2f}-day idle span",
    )
    check(
        len(panic) == 0 or all(coin in section for coin in panic["coin"].astype(str).unique()),
        "lifecycle section does not name every coin in the panic close",
    )


def check_no_placeholder_numbers(report: str) -> None:
    """A `nan%`/`nan USDT` in prose means a derived value silently degraded."""
    for pattern in ("nan%", "nan USDT", "nan 笔", "nan,", "nan 天"):
        check(pattern not in report, f"report contains an unresolved placeholder: {pattern!r}")


def check_scope_claims(result_dir: Path, report: str, cfg: dict, dataset: dict) -> None:
    scope = report.split(spec.HEAD_OVERALL)[0]
    for token in (study.SOURCE_CONFIG_SHA256[:16], str(dataset.get("cache_hash"))):
        check(token in scope, f"## 口径与范围 does not state {token!r}")
    check(
        str(cfg["backtest"]["start_date"]) in scope,
        "## 口径与范围 does not state the run start_date",
    )
    check(
        str(cfg["backtest"]["balance_sample_divider"]) in scope,
        "## 口径与范围 does not state balance_sample_divider",
    )
    for coin in study.EXCLUDED_COINS:
        check(coin in scope, f"## 口径与范围 does not explain the excluded coin {coin}")
    check("未联网下载任何数据" in scope, "## 口径与范围 is missing the offline-safety statement")
    check("maker" in scope and "taker" in scope, "## 口径与范围 does not state the cost contract")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", default=None)
    args = parser.parse_args(argv)

    if args.result_dir:
        result_dir = Path(args.result_dir).resolve()
    else:
        runs = [
            path for path in study.dated_run_dirs(study.RUNS_BASE) if (path / "analysis.json").exists()
        ]
        if not runs:
            raise SystemExit(f"no completed run under {study.relative(study.RUNS_BASE)}")
        result_dir = runs[-1]

    print(f"result dir: {study.relative(result_dir)}")
    report_path = result_dir / "annual_analysis.md"
    if not report_path.exists():
        print("FAILED: annual_analysis.md is missing", file=sys.stderr)
        print("failed: 1")
        return 1
    report = report_path.read_text(encoding="utf-8")

    # 1. skeleton, persisted layout, and the frozen contract
    for problem in spec.assert_report_structure(report):
        fail(f"skeleton: {problem}")
    analysis, cfg, dataset, effective = check_contract(result_dir)

    # 2. artifacts, independently loaded
    fills = load_fills(result_dir)
    equity = load_equity(result_dir)
    check_ledger_identity(equity)
    check_analysis_consistency(analysis, fills)

    # 3. tables
    annual = recompute_period_table(equity, fills, "Y")
    monthly = recompute_period_table(equity, fills, "M")
    coins = recompute_coin_table(fills)
    attribution = recompute_attribution(fills)
    # The annual summary table stacks two rows per year (sample interval, then the figures), so
    # it is checked through the CSV plus the per-year detail blocks below.
    compare_period_table("annual_metrics.csv", result_dir / "annual_metrics.csv", annual)
    compare_period_table("monthly_metrics.csv", result_dir / "monthly_metrics.csv", monthly)
    check_year_details(report, annual)
    check_monthly_table(report, monthly)
    check_coin_table(result_dir, report, coins)
    check_attribution(report, attribution)

    # 4. overall block and scope claims
    check_overall_table(report, analysis, annual, equity)
    check_lifecycle_block(report, fills, equity, analysis)
    check_no_placeholder_numbers(report)
    check_scope_claims(result_dir, report, cfg, dataset)

    if PROBLEMS:
        for problem in PROBLEMS:
            print(f"FAILED: {problem}", file=sys.stderr)
        print(f"failed: {len(PROBLEMS)}")
        return 1
    print(
        f"ok: {len(annual)} annual periods, {len(monthly)} months, {len(coins)} coins, "
        f"{len(fills):,} fills, {len(effective)} dataset coins"
    )
    print("failed: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
