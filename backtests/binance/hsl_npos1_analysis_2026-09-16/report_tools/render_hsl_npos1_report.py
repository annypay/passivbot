#!/usr/bin/env python3
"""Render the `hsl_npos1` deep-analysis report and its three metric tables.

The report skeleton, the table schemas and the markdown renderer live in
`backtests/report_spec/annual_analysis.py`; the binding convention is
`docs/ai/runbooks/strategy_report.md`. This script supplies only:

* the artifact loaders and the facts derived from this run's own ledger,
* the study-specific scope lines, and
* the study appendix (engine mechanics as configured vs measured, drawdown anatomy, trade
  lifecycle and the hard stop, coin coverage and concentration, the labelled comparison against
  the pre-existing `hsl_npos1` study cell, and the execution boundaries).

Every number is read from the run directory or from an explicitly cited artifact; nothing is
hardcoded. Offline only.
"""

from __future__ import annotations

import argparse
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
import annual_analysis as spec  # noqa: E402  (canonical report convention)


def load_json(path: Path) -> Any:
    return spec.load_json(path)


def find_run_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    runs = [
        path for path in study.dated_run_dirs(study.RUNS_BASE) if (path / "analysis.json").exists()
    ]
    if not runs:
        raise SystemExit(f"no completed run directory under {study.relative(study.RUNS_BASE)}")
    if len(runs) > 1:
        raise SystemExit(
            f"expected exactly one completed run under {study.relative(study.RUNS_BASE)}, "
            f"found {[path.name for path in runs]}; pass --result-dir"
        )
    return runs[0]


def require_artifacts(result_dir: Path) -> None:
    missing = [name for name in study.REQUIRED_ARTIFACTS if not (result_dir / name).exists()]
    if missing:
        raise SystemExit(
            f"run directory {study.relative(result_dir)} is missing artifacts: {missing}"
        )


def audit_path_for(result_dir: Path) -> Path:
    local = result_dir / "execution_audit.csv"
    return local if local.exists() else study.EXECUTION_AUDIT_PATH


# --------------------------------------------------------------------------------------
# Facts derived from this run's own ledger
# --------------------------------------------------------------------------------------


def direction_from_type(order_type: Any) -> str:
    text = str(order_type)
    if "long" in text:
        return "long"
    if "short" in text:
        return "short"
    raise ValueError(f"cannot derive position side from order type {text!r}")


def classify_types(type_counts: dict[str, int]) -> dict[str, Any]:
    """Split order types into known templates and anything unrecognised."""
    unknown_entry = sorted(
        name
        for name in type_counts
        if str(name).startswith("entry_") and name not in study.ENTRY_TEMPLATES
    )
    unknown_close = sorted(
        name
        for name in type_counts
        if str(name).startswith("close_") and name not in study.CLOSE_TEMPLATES
    )
    return {"unknown_entry_types": unknown_entry, "unknown_close_types": unknown_close}


def reconstruct_positions(fills: pd.DataFrame) -> pd.DataFrame:
    """Rebuild the post-fill open-position count per side from the ledger order.

    `fills.csv` carries no `pside` column, so the side is read from the order type, the same
    convention the report uses for direction attribution. A row's `psize` is the position size
    *after* that fill, which makes the replay exact rather than inferred.
    """
    state: dict[str, float] = {}
    long_counts = np.empty(len(fills), dtype=int)
    for idx, row in enumerate(fills.itertuples(index=False)):
        side = direction_from_type(row.type)
        key = f"{row.coin}:{side}"
        size = float(row.psize)
        if size == 0.0:
            state.pop(key, None)
        else:
            state[key] = size
        long_counts[idx] = sum(
            1 for name, value in state.items() if name.endswith(":long") and value
        )
    out = fills.copy()
    out["nonzero_long_after_fill"] = long_counts
    return out


def coin_metrics_extras(fills: pd.DataFrame) -> pd.DataFrame:
    """Per-coin realized PnL split, exposure, and the first/last fill of each coin."""
    # Alphabetical by coin, so the CSV row order is stable and independent of the report's
    # descending-PnL presentation order.
    frames = []
    for coin, sub in fills.groupby("coin", sort=True):
        types = sub["type"].astype(str)
        entry = types.str.startswith("entry_")
        liquidity = sub["liquidity"].astype(str)
        frames.append(
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
                "first_fill_utc": sub["timestamp"].min().strftime("%Y-%m-%d %H:%M:%S+00:00"),
                "last_fill_utc": sub["timestamp"].max().strftime("%Y-%m-%d %H:%M:%S+00:00"),
            }
        )
    return pd.DataFrame(frames)


def coin_window_facts(fills: pd.DataFrame) -> list[dict]:
    """Per-coin first/last fill: the measured form of "the tradable universe grows over time"."""
    rows = []
    for coin, sub in fills.groupby("coin", sort=False):
        rows.append(
            {
                "coin": coin,
                "first_fill": sub["timestamp"].min().strftime("%Y-%m-%d"),
                "last_fill": sub["timestamp"].max().strftime("%Y-%m-%d"),
                "fills": int(len(sub)),
                "net_realized_pnl_usd": float(sub["pnl"].sum() + sub["fee_paid"].sum()),
            }
        )
    return sorted(rows, key=lambda row: (row["first_fill"], row["coin"]))


def worst_equity_drawdown(equity: pd.DataFrame) -> dict[str, Any]:
    """Peak-to-trough of the sampled strategy equity, with both endpoint timestamps."""
    values = equity["strategy_equity"].to_numpy(dtype=float)
    stamps = equity["timestamp"].to_numpy()
    peak = np.maximum.accumulate(values)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(peak > 0.0, values / peak, 1.0)
    trough_index = int(np.nanargmin(ratio))
    peak_index = int(np.nanargmax(values[: trough_index + 1])) if trough_index > 0 else 0
    return {
        "peak_index": peak_index,
        "trough_index": trough_index,
        "peak_value": float(values[peak_index]),
        "trough_value": float(values[trough_index]),
        "depth": float(1.0 - ratio[trough_index]),
        "peak_utc": pd.Timestamp(stamps[peak_index]).strftime("%Y-%m-%d %H:%M:%S+00:00"),
        "trough_utc": pd.Timestamp(stamps[trough_index]).strftime("%Y-%m-%d %H:%M:%S+00:00"),
    }


def positions_at(fills: pd.DataFrame, index: int) -> pd.DataFrame:
    """Open positions as of the fill at `index` (ledger replay up to and including it).

    `pprice` on a fill row is the position's average entry price after that fill, so the last
    row for a coin inside the replay carries the average cost of the still-open position.
    """
    if index < 0:
        return pd.DataFrame(columns=["coin", "qty", "cost"])
    state: dict[str, dict[str, float]] = {}
    for row in fills.iloc[: index + 1].itertuples(index=False):
        size = float(row.psize)
        if size == 0.0:
            state.pop(row.coin, None)
            continue
        state[row.coin] = {"qty": abs(size), "cost": float(row.pprice)}
    return pd.DataFrame(
        [{"coin": coin, **payload} for coin, payload in sorted(state.items())],
        columns=["coin", "qty", "cost"],
    )


def lifecycle_facts(fills: pd.DataFrame, equity: pd.DataFrame, analysis: dict[str, Any]) -> dict[str, Any]:
    """When the strategy was actually trading, versus the window it was given.

    This is the measured form of the report's central finding: a long declared window does not
    mean a long-lived strategy. Everything here is a timestamp or a row count from the ledger.
    """
    first = fills["timestamp"].min()
    last = fills["timestamp"].max()
    end = equity["timestamp"].iloc[-1]
    days = fills["timestamp"].dt.floor("D").nunique()
    panic = fills.loc[fills["type"].astype(str) == "close_panic_long"]
    idle_seconds = (end - last).total_seconds()
    window_seconds = (end - equity["timestamp"].iloc[0]).total_seconds()
    return {
        "first_fill_utc": first.strftime("%Y-%m-%d %H:%M:%S+00:00"),
        "last_fill_utc": last.strftime("%Y-%m-%d %H:%M:%S+00:00"),
        "active_days": int(days),
        "active_share_of_window": float(days / max(int(window_seconds // 86400), 1)),
        "idle_days_after_last_fill": float(idle_seconds / 86400.0),
        "idle_share_of_window": float(idle_seconds / window_seconds) if window_seconds else float("nan"),
        "panic_count": int(len(panic)),
        "panic_pnl": float(panic["pnl"].sum()) if len(panic) else 0.0,
        "panic_fees": float(panic["fee_paid"].sum()) if len(panic) else 0.0,
        "panic_utc": (
            panic["timestamp"].min().strftime("%Y-%m-%d %H:%M:%S+00:00") if len(panic) else None
        ),
        "panic_coins": sorted(panic["coin"].astype(str).unique().tolist()),
        "panic_realized_pnl_raw": float(panic["pnl"].sum()) if len(panic) else 0.0,
        "hard_stop_triggers": analysis.get("hard_stop_triggers"),
        "hard_stop_trigger_drawdown_mean": analysis.get("hard_stop_trigger_drawdown_mean"),
        "hard_stop_panic_close_loss_max": analysis.get("hard_stop_panic_close_loss_max"),
        "hard_stop_panic_close_loss_drawdown_pct_max": analysis.get(
            "hard_stop_panic_close_loss_drawdown_pct_max"
        ),
        "hard_stop_time_in_red_pct": analysis.get("hard_stop_time_in_red_pct"),
        "hard_stop_restarts": analysis.get("hard_stop_restarts"),
    }


def ledger_facts(
    fills: pd.DataFrame,
    equity: pd.DataFrame,
    dataset: dict[str, Any],
    cfg: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Everything the report says about measured behaviour, replayed from `fills.csv`."""
    positions = reconstruct_positions(fills)
    minute_key = fills["timestamp"].dt.floor("min")
    per_minute = fills.groupby(minute_key).size()
    both_sides = (
        fills.assign(is_entry=fills["type"].astype(str).str.startswith("entry_"))
        .groupby([minute_key, "coin"])["is_entry"]
        .nunique()
    )
    type_counts = {str(k): int(v) for k, v in fills["type"].astype(str).value_counts().items()}

    risk = cfg["bot"]["long"]["risk"]
    tm = cfg["bot"]["long"]["strategy"]["trailing_martingale"]
    hsl = cfg["bot"]["long"]["hsl"]
    unstuck = cfg["bot"]["long"]["unstuck"]
    forager = cfg["bot"]["long"]["forager"]

    total_wallet_exposure_limit = float(risk["total_wallet_exposure_limit"])
    n_positions = float(risk["n_positions"])
    single_coin_cap = total_wallet_exposure_limit / n_positions * (
        1.0 + float(risk["we_excess_allowance_pct"])
    )

    wallet_exposure = fills["wallet_exposure"].abs()
    twe_long = fills["twe_long"].astype(float)
    liquidity = fills["liquidity"].astype(str)

    drawdown = worst_equity_drawdown(equity)
    fills_before_trough = int(
        (fills["timestamp"] <= equity["timestamp"].iloc[drawdown["trough_index"]]).sum()
    )

    realized = float(fills["pnl"].sum())
    fees = float(fills["fee_paid"].sum())
    net_by_coin = fills.assign(net=fills["pnl"] + fills["fee_paid"]).groupby("coin")["net"].sum()
    positive_net = net_by_coin[net_by_coin > 0].sort_values(ascending=False)
    positive_total = float(positive_net.sum())
    # Share of the *profitable* coins' net PnL, so the number stays readable when the run's total
    # net PnL is negative; the report states which denominator it uses.
    concentration = (
        float(positive_net.head(3).sum() / positive_total)
        if positive_total > 0 and not positive_net.empty
        else float("nan")
    )
    risk_fill_counts = {name: int(type_counts.get(name, 0)) for name in study.RISK_FILL_TYPES}

    return {
        "fills_count": int(len(fills)),
        "nonzero_long_max": int(positions["nonzero_long_after_fill"].max()),
        "nonzero_long_counts": {
            int(key): int(value)
            for key, value in positions["nonzero_long_after_fill"]
            .value_counts()
            .sort_index()
            .items()
        },
        "minutes_with_multiple_fills": int((per_minute > 1).sum()),
        "max_fills_in_a_minute": int(per_minute.max()),
        "coin_minutes_with_entry_and_close": int((both_sides > 1).sum()),
        "type_counts": type_counts,
        "max_twe_long": float(twe_long.max()),
        "rows_twe_long_above_limit": int((twe_long > total_wallet_exposure_limit + 1e-12).sum()),
        "max_abs_wallet_exposure": float(wallet_exposure.max()),
        "single_coin_cap": single_coin_cap,
        "rows_we_above_reference_cap": int((wallet_exposure > single_coin_cap + 1e-12).sum()),
        "top_symbol_share": float(fills["coin"].value_counts(normalize=True).iloc[0]),
        "top_symbol": str(fills["coin"].value_counts().index[0]),
        "top_symbol_fills": int(fills["coin"].value_counts().iloc[0]),
        "realized_pnl_raw_usd": realized,
        "fees_signed_usd": fees,
        "net_realized_pnl_usd": realized + fees,
        "net_positive_coins": int((net_by_coin > 0).sum()),
        "net_negative_coins": int((net_by_coin <= 0).sum()),
        "top3_positive_share": concentration,
        "maker_fills": int((liquidity == "maker").sum()),
        "taker_fills": int((liquidity == "taker").sum()),
        "risk_fill_counts": risk_fill_counts,
        "risk_fill_total": int(sum(risk_fill_counts.values())),
        "entry_fills": int(fills["type"].astype(str).str.startswith("entry_").sum()),
        "config": {
            "n_positions": n_positions,
            "total_wallet_exposure_limit": total_wallet_exposure_limit,
            "we_excess_allowance_pct": float(risk["we_excess_allowance_pct"]),
            "position_exposure_enforcer_enabled": bool(
                risk["position_exposure_enforcer_enabled"]
            ),
            "position_exposure_enforcer_threshold": float(
                risk["position_exposure_enforcer_threshold"]
            ),
            "total_exposure_enforcer_enabled": bool(risk["total_exposure_enforcer_enabled"]),
            "total_exposure_enforcer_threshold": float(
                risk["total_exposure_enforcer_threshold"]
            ),
            "entry_cooldown_minutes": float(risk["entry_cooldown_minutes"]),
            "entry_initial_qty_pct": float(tm["entry"]["initial_qty_pct"]),
            "entry_initial_ema_dist": float(tm["entry"]["initial_ema_dist"]),
            "entry_threshold_base_pct": float(tm["entry"]["threshold_base_pct"]),
            "entry_threshold_we_weight": float(tm["entry"]["threshold_we_weight"]),
            "entry_threshold_volatility_1h_weight": float(
                tm["entry"]["threshold_volatility_1h_weight"]
            ),
            "entry_double_down_factor": float(tm["entry"]["double_down_factor"]),
            "close_qty_pct": float(tm["close"]["qty_pct"]),
            "close_threshold_base_pct": float(tm["close"]["threshold_base_pct"]),
            "close_threshold_we_weight": float(tm["close"]["threshold_we_weight"]),
            "close_threshold_volatility_1h_weight": float(
                tm["close"]["threshold_volatility_1h_weight"]
            ),
            "volatility_ema_span_1h": float(tm["volatility_ema_span_1h"]),
            "entry_ema_span_0": float(tm["entry"]["ema_span_0"]),
            "entry_ema_span_1": float(tm["entry"]["ema_span_1"]),
            "hsl_enabled": bool(hsl["enabled"]),
            "hsl_red_threshold": float(hsl["red_threshold"]),
            "hsl_ema_span_minutes": float(hsl["ema_span_minutes"]),
            "hsl_cooldown_minutes_after_red": float(hsl["cooldown_minutes_after_red"]),
            "hsl_no_restart_drawdown_threshold": float(hsl["no_restart_drawdown_threshold"]),
            "hsl_orange_tier_mode": str(hsl["orange_tier_mode"]),
            "hsl_panic_close_order_type": str(hsl["panic_close_order_type"]),
            "hsl_tier_ratios": dict(hsl["tier_ratios"]),
            "unstuck_enabled": bool(unstuck["enabled"]),
            "unstuck_threshold": float(unstuck["threshold"]),
            "unstuck_close_pct": float(unstuck["close_pct"]),
            "unstuck_loss_allowance_pct": float(unstuck["loss_allowance_pct"]),
            "unstuck_ema_dist": float(unstuck["ema_dist"]),
            "forager_volatility_ema_span_1m": float(forager["volatility_ema_span_1m"]),
            "forager_volume_drop_pct": float(forager["volume_drop_pct"]),
            "minimum_coin_age_days": float(cfg["live"]["minimum_coin_age_days"]),
            "leverage": float(cfg["live"]["leverage"]),
            "max_realized_loss_pct": float(cfg["live"]["max_realized_loss_pct"]),
            "dynamic_wel_by_tradability": bool(cfg["backtest"]["dynamic_wel_by_tradability"]),
        },
        "dataset": {
            "cache_dir_label": dataset.get("cache_dir_label"),
            "hlcv_cache_dir": dataset.get("hlcv_cache_dir"),
            "cache_hash": dataset.get("cache_hash"),
            "dataset_override": bool(dataset.get("dataset_override")),
            "coins": list(dataset.get("coins") or []),
            "requested_start_date": dataset.get("requested_start_date"),
            "requested_end_date": dataset.get("requested_end_date"),
            "content_hashes": dict(dataset.get("content_hashes") or {}),
            "materialization_schema_version": dataset.get("materialization_schema_version"),
        },
        "drawdown": {
            **drawdown,
            "fills_before_trough": fills_before_trough,
            "positions_at_trough": positions_at(fills, fills_before_trough - 1),
        },
        "lifecycle": lifecycle_facts(fills, equity, analysis),
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
        return {"present": False, "path": study.relative(path)}
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


def prior_run_facts() -> dict[str, Any]:
    """The pre-existing `hsl_npos1` study cell, read from its own artifacts.

    It is used only for a labelled cross-regime statement: different window, different coin
    basket. Its numbers never share a table with this report's numbers.
    """
    if not (study.PRIOR_RUN / "analysis.json").exists():
        return {"present": False, "label": study.PRIOR_RUN_LABEL}
    analysis = load_json(study.PRIOR_RUN / "analysis.json")
    config = load_json(study.PRIOR_RUN / "config.json")
    bt = config.get("backtest", {})
    return {
        "present": True,
        "label": study.PRIOR_RUN_LABEL,
        "effective_start": analysis.get("effective_start_date"),
        "effective_end": analysis.get("effective_end_date"),
        "n_days": float(analysis.get("n_days") or 0.0),
        "fills": int(analysis.get("fills_count") or 0),
        "gain_strategy_eq": float(analysis.get("gain_strategy_eq") or 0.0),
        "dd_strategy_eq": float(analysis.get("drawdown_worst_strategy_eq") or 0.0),
        "sharpe_pnl": float(analysis.get("sharpe_ratio_pnl") or 0.0),
        "coins": len(bt.get("coins", {}).get("binance") or []),
        "window": [bt.get("start_date"), bt.get("end_date")],
        "balance_sample_divider": int(bt.get("balance_sample_divider") or 0),
        "execution_delay_bars": int(bt.get("execution_delay_bars") or 0),
        "intrabar_fill_order": bt.get("intrabar_fill_order"),
    }


# --------------------------------------------------------------------------------------
# Study-specific report content
# --------------------------------------------------------------------------------------


def scope_lines(cfg: dict[str, Any], dataset: dict[str, Any], ledger: dict[str, Any]) -> list[str]:
    """The `## 口径与范围` bullets: provenance, every change, universe, contract, boundary."""
    bt = cfg["backtest"]
    delay = int(bt["execution_delay_bars"])
    kind = cfg.get("live", {}).get("strategy_kind", "n/a")
    effective = list(dataset.get("coins") or [])
    # The published profile is the authority for what was *requested*; the materialized dataset
    # is the authority for what actually ran. Both are read from files, never from a literal.
    published = study.load_json(study.SOURCE_CONFIG)
    requested = [
        str(coin)
        for coin in published.get("live", {}).get("approved_coins", {}).get("long") or []
    ]
    dropped = sorted(set(requested) - set(effective))
    conf = ledger["config"]
    return [
        f"- 策略来源：`configs/examples/hsl_npos1.json`"
        f"（sha256 `{study.SOURCE_CONFIG_SHA256[:16]}…`），`live.strategy_kind = {kind}`；"
        "本工件是该公开示例 profile 的**独立完整回测**，不是任何研究格的派生候选。",
        "- 运行配置的相对改动（唯一差异；`bot` 与 `coin_overrides` 子树在构建阶段已逐路径"
        "校验与示例相同）："
        f"`backtest.start_date` `2021-01-01` → **`{bt['start_date']}`**、"
        f"`backtest.end_date` `now` → **`{bt['end_date']}`**、"
        '`backtest.exchanges` `["binance", "bybit"]` → **`["binance"]`**、'
        f"`backtest.balance_sample_divider` `60` → **`{bt['balance_sample_divider']}`**、"
        f"`live.approved_coins` 的 long/short 两侧收敛为本地有数据的 {len(effective)} 币，"
        "并显式写入该 profile 未声明的 schema 默认执行键与冻结币篮。"
        f"`bot.long.risk.n_positions = {conf['n_positions']:.0f}`、"
        f"`total_wallet_exposure_limit = {conf['total_wallet_exposure_limit']}`、"
        "HSL、unstuck 与 forager 参数均为示例原值。",
        f"- 命名说明：文件名写作 `npos1`，但配置实际是 long "
        f"`n_positions = {conf['n_positions']:.0f}`；"
        "本报告一律以配置实测值为准，不把该 profile 当作单仓风险配置。",
        f"- 数据为 binance USDT-M 永续合约 1 分钟 K 线；示例 profile 声明 {len(requested)} 币，"
        f"本次运行实际使用 {len(effective)} 币；被剔除的币种："
        f"{', '.join(f'`{coin}`' for coin in dropped) if dropped else '无'}。"
        "剔除原因与范围：这些币种在仓库的本地 K 线目录中没有可用数据，"
        "因此从 `live.approved_coins` 与 `backtest.coins` 两侧同时移除；"
        "剔除发生在运行之前、不依赖回测结果，故不构成结果导向的选择。"
        f"缓存标识 `{dataset.get('cache_dir_label')}`"
        f"（`cache_hash = {dataset.get('cache_hash')}`），"
        "由回测从本地离线 K 线目录物化，本次运行未联网下载任何数据。",
        f"- 执行与成本口径：`execution_delay_bars={delay}`（名义因果 T+{delay + 1}）、"
        f"`intrabar_fill_order={bt['intrabar_fill_order']}`、"
        f"maker `{bt['maker_fee_override']}` / taker `{bt['taker_fee_override']}`、"
        f"`market_order_slippage_pct={bt.get('market_order_slippage_pct')}`。"
        "该 profile 原文未声明两个执行键，本工件显式写入 schema 默认值，"
        "以便把假设写进配置而不是留给读者推断。",
        f"- 权益口径：`balance_sample_divider={bt['balance_sample_divider']}` 的逐分钟采样序列，"
        f"与 `analysis.json` 同分辨率；`btc_collateral_cap={bt['btc_collateral_cap']}`，"
        "因此 `strategy_equity` 与 `usd_total_equity` 在本工件中一致"
        f"（实测 = `{ledger['strategy_equity_equals_usd_total_equity']}`）。",
        f"- 报告规范：`{study.REPORT_CONVENTION}`；章节骨架与冻结样板 "
        f"`{study.REFERENCE_REPORT}` 一致，并按该规范在期末表中额外给出 `active_coins` 列。",
        "- 安全边界：本工件由本地离线回测生成，**未联网下载任何数据**、未使用凭证、"
        "未接触交易所账户、未创建或撤销订单、未启动机器人。",
    ]


def appendix_engine(ledger: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    conf = ledger["config"]
    slot_distribution = ", ".join(
        f"{key} 仓:{value:,} 次" for key, value in list(ledger["nonzero_long_counts"].items())[:8]
    )
    return [
        "### 引擎与风险机制（配置声明 vs 本次实测）",
        "",
        "本表把示例 profile 的声明值与本次运行的实际测量值并排，用于区分“配置说会怎样”"
        "与“账本显示实际怎样”。左列读自运行配置，右列由 `fills.csv` 逐笔重放得到。",
        "",
        spec.md_table(
            [
                [
                    "最大并发 long 槽位",
                    f"`n_positions = {conf['n_positions']:.0f}`",
                    f"成交后非零 long 仓位最大值 **{ledger['nonzero_long_max']}**"
                    f"（分布 {slot_distribution}）",
                ],
                [
                    "组合 long 敞口上限",
                    f"`total_wallet_exposure_limit = {conf['total_wallet_exposure_limit']}`；"
                    f"TWEL 执行器 `{conf['total_exposure_enforcer_enabled']}`，"
                    f"阈值 `{conf['total_exposure_enforcer_threshold']}`",
                    f"实测 `twe_long` 最大值 **{ledger['max_twe_long']:.6f}**；"
                    f"高于 TWEL 的成交行 {ledger['rows_twe_long_above_limit']:,} / "
                    f"{ledger['fills_count']:,}",
                ],
                [
                    "单币敞口参考上限",
                    f"TWEL / n_positions × (1 + allowance) = "
                    f"{conf['total_wallet_exposure_limit']} / {conf['n_positions']:.0f} × "
                    f"(1 + {conf['we_excess_allowance_pct']}) ≈ "
                    f"{ledger['single_coin_cap'] * 100:.2f}%",
                    f"实测单币 `wallet_exposure` 最大值 **"
                    f"{ledger['max_abs_wallet_exposure'] * 100:.2f}%**；高于该参考上限的成交行 "
                    f"{ledger['rows_we_above_reference_cap']:,}",
                ],
                [
                    "单币敞口执行器",
                    f"`position_exposure_enforcer_enabled = "
                    f"{conf['position_exposure_enforcer_enabled']}`，阈值 "
                    f"`{conf['position_exposure_enforcer_threshold']}`",
                    f"`close_auto_reduce_wel_long` "
                    f"{ledger['risk_fill_counts']['close_auto_reduce_wel_long']:,} 笔、"
                    f"`close_auto_reduce_twel_long` "
                    f"{ledger['risk_fill_counts']['close_auto_reduce_twel_long']:,} 笔"
                    "—— 风险层真正介入过的直接证据",
                ],
                [
                    "入场节奏",
                    f"`initial_qty_pct = {conf['entry_initial_qty_pct']}`、"
                    f"`initial_ema_dist = {conf['entry_initial_ema_dist']}`、"
                    f"`threshold_base_pct = {conf['entry_threshold_base_pct']}`、"
                    f"`threshold_we_weight = {conf['entry_threshold_we_weight']}`、"
                    f"`threshold_volatility_1h_weight = "
                    f"{conf['entry_threshold_volatility_1h_weight']}`、"
                    f"`double_down_factor = {conf['entry_double_down_factor']}`",
                    f"入场 {ledger['entry_fills']:,} 笔 / 减仓平仓 "
                    f"{ledger['fills_count'] - ledger['entry_fills']:,} 笔；"
                    f"单仓平均持有 {float(facts['position_held_days_mean'] or 0.0):,.2f} 天，"
                    f"最长 {float(facts['position_held_days_max'] or 0.0):,.2f} 天",
                ],
                [
                    "HSL（高水位止损）",
                    f"`enabled = {conf['hsl_enabled']}`、"
                    f"`red_threshold = {conf['hsl_red_threshold']}`、"
                    f"`ema_span_minutes = {conf['hsl_ema_span_minutes']:.0f}`、"
                    f"`cooldown_minutes_after_red = "
                    f"{conf['hsl_cooldown_minutes_after_red']:.0f}`、"
                    f"`no_restart_drawdown_threshold = "
                    f"{conf['hsl_no_restart_drawdown_threshold']}`、"
                    f"`orange_tier_mode = {conf['hsl_orange_tier_mode']}`、"
                    f"`panic_close_order_type = {conf['hsl_panic_close_order_type']}`",
                    f"`close_panic_long` "
                    f"{ledger['risk_fill_counts']['close_panic_long']:,} 笔；"
                    f"三档比值 orange={conf['hsl_tier_ratios'].get('orange')} / "
                    f"yellow={conf['hsl_tier_ratios'].get('yellow')}。"
                    "panic 单型是配置字段，不是成交账本可观测的列",
                ],
                [
                    "解套（unstuck）",
                    f"`enabled = {conf['unstuck_enabled']}`、"
                    f"`threshold = {conf['unstuck_threshold']}`、"
                    f"`close_pct = {conf['unstuck_close_pct']}`、"
                    f"`loss_allowance_pct = {conf['unstuck_loss_allowance_pct']}`、"
                    f"`ema_dist = {conf['unstuck_ema_dist']}`",
                    f"`close_unstuck_long` "
                    f"{ledger['risk_fill_counts']['close_unstuck_long']:,} 笔"
                    f"（占全部成交 "
                    f"{ledger['risk_fill_counts']['close_unstuck_long'] / max(ledger['fills_count'], 1) * 100:.2f}%）",
                ],
            ],
            ["机制", "配置声明", "本次运行实测"],
        ),
        "",
        f"- 风险层成交（并非策略入场信号产生的减仓）合计 **{ledger['risk_fill_total']:,}** 笔，"
        f"占全部 {ledger['fills_count']:,} 笔成交的 "
        f"{ledger['risk_fill_total'] / max(ledger['fills_count'], 1) * 100:.2f}%。",
        "",
    ]


def appendix_lifecycle(ledger: dict[str, Any], analysis: dict[str, Any]) -> list[str]:
    """The finding that changes how every other number in this report must be read."""
    life = ledger["lifecycle"]
    facts = spec.analysis_facts(analysis)
    lines = [
        "### 交易生命周期与硬停（本报告的核心事实）",
        "",
        f"本次运行的有效区间是 {facts['effective_start_date']} 至 {facts['effective_end_date']}"
        f"（{float(facts['n_days'] or 0.0):,.2f} 天），但**策略只在其中极短的一段里交易过**：",
        "",
        spec.md_table(
            [
                [
                    "实际交易区间（首笔 → 末笔成交）",
                    f"{life['first_fill_utc']} → {life['last_fill_utc']}",
                ],
                ["有成交的自然日数", f"{life['active_days']} 天（占有效区间的 {life['active_share_of_window'] * 100:.2f}%）"],
                [
                    "末笔成交之后的静止时间",
                    f"{life['idle_days_after_last_fill']:,.2f} 天"
                    f"（占有效区间的 {life['idle_share_of_window'] * 100:.2f}%）",
                ],
                ["`close_panic_long` 笔数 / 时间", f"{life['panic_count']:,} 笔 / {life['panic_utc']}"],
                ["该次 panic 平仓涉及的币种", "、".join(life["panic_coins"])],
                [
                    "该次 panic 的已实现 PnL / 手续费",
                    f"{spec.fmt_money(life['panic_pnl'])} / {spec.fmt_money(life['panic_fees'])} USDT",
                ],
                [
                    "`analysis.json` 硬停计数 / 重启计数",
                    f"{life['hard_stop_triggers']} / {life['hard_stop_restarts']}",
                ],
                ["硬停触发时的回撤（均值）", spec.fmt_pct(life["hard_stop_trigger_drawdown_mean"])],
                [
                    "单次 panic 平仓的最大损失",
                    f"{spec.fmt_money(life['hard_stop_panic_close_loss_max'])} USDT",
                ],
                [
                    "该损失占账户权益的最大比例",
                    spec.fmt_pct(life["hard_stop_panic_close_loss_drawdown_pct_max"]),
                ],
                ["RED 状态时间占比", spec.fmt_pct(life["hard_stop_time_in_red_pct"])],
            ],
            ["项目", "数值"],
        ),
        "",
        f"- 静止期不是数据缺失：`backtest_completion_ratio = "
        f"{float(facts['backtest_completion_ratio'] or 0.0):.6f}`，"
        f"权益采样覆盖全部 {life['idle_days_after_last_fill']:,.2f} 天静止期且全程等于末笔成交后的余额。",
        f"- HSL 的 `no_restart_drawdown_threshold = "
        f"{ledger['config']['hsl_no_restart_drawdown_threshold']}` 与 "
        f"`live.max_realized_loss_pct = {ledger['config']['max_realized_loss_pct']}` "
        "都是该 profile 的示例原值，本工件未改动它们；"
        "上表只报告这次运行的事实，不对“为什么不重启”给出机制性断言。",
        "- 直接后果：`## 自然年汇总` 与 `## 月度汇总` 中 2022 年及以后的零值行表示**策略没有交易**，"
        "而不是“策略在震荡中打平”。所有把全期 gain 与全期回撤并列的读法，"
        "都必须先经过本节的交易区间。",
        "",
    ]
    return lines


def appendix_drawdown(ledger: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    dd = ledger["drawdown"]
    lines = [
        "### 回撤解剖（最差峰谷区间）",
        "",
        f"本次运行的 strategy-equity 最差回撤区间为 {dd['peak_utc']}"
        f"（{spec.fmt_money(dd['peak_value'])} USDT）→ {dd['trough_utc']}"
        f"（{spec.fmt_money(dd['trough_value'])} USDT），深度 **{spec.fmt_pct(dd['depth'])}**。"
        f"`analysis.json` 的原生 `drawdown_worst_strategy_eq` 为 "
        f"{spec.fmt_pct(facts['drawdown_worst_strategy_eq'])}，"
        f"`drawdown_worst_usd` 为 {spec.fmt_pct(facts['drawdown_worst_usd'])}，"
        f"最差 1% 均值回撤 {spec.fmt_pct(facts['drawdown_worst_mean_1pct_strategy_eq'])}。",
        "",
    ]
    trough_positions = dd["positions_at_trough"]
    if trough_positions is None or trough_positions.empty:
        lines.append("- 该时点之前没有未平仓位，回撤由已实现亏损构成。")
    else:
        lines.append(
            "按 `fills.csv` 的 `psize`/`pprice` 在谷底时点重建的未平仓位如下。"
            "`均价` 是该持仓成交后的平均入场价，"
            "`成本名义额 / 谷底权益` 用谷底采样点的权益作分母："
        )
        lines.append("")
        lines.append(
            spec.md_table(
                [
                    [
                        str(row.coin),
                        spec.fmt_money(row.qty * row.cost),
                        f"{row.cost:,.8f}",
                        f"{row.qty:,.4f}",
                        spec.fmt_pct(
                            (row.qty * row.cost) / dd["trough_value"]
                            if dd["trough_value"]
                            else float("nan")
                        ),
                    ]
                    for row in trough_positions.itertuples(index=False)
                ],
                ["币种", "建仓成本名义额", "均价", "数量", "成本名义额 / 谷底权益"],
            )
        )
    lines.append("")
    lines.append(
        f"- 谷底之前已有 {dd['fills_before_trough']:,} 笔成交，因此该时点的仓位是多币种、"
        "按不同入场价累积的结果，不是单笔事件。同分钟内的 H/L 先后无法从 1 分钟 K 线恢复，"
        "因此上表是**重建值**，不构成对该时点真实保证金或强平距离的结论。"
        f"该时点全部 {len(trough_positions)} 个持仓的成本名义额合计"
        f" {spec.fmt_money(float((trough_positions['qty'] * trough_positions['cost']).sum()))} USDT。"
    )
    lines.append(
        f"- 最长 PnL 峰值恢复期 {float(facts['strategy_eq_recovery_days_max'] or 0.0):,.2f} 天，"
        f"水下时间占比均值 {spec.fmt_pct(facts['strategy_eq_underwater_pct_mean'])}；"
        "这两项衡量的是“回撤持续多久”，与回撤深度同等重要。"
    )
    lines.append("")
    return lines


def appendix_coins(
    ledger: dict[str, Any], facts: dict[str, Any], dataset: dict[str, Any], coin_windows: list[dict]
) -> list[str]:
    conf = ledger["config"]
    lines = [
        "### 币种覆盖与集中度",
        "",
        f"数据集含 {len(dataset.get('coins') or [])} 币，而本次运行只有 "
        f"{int(facts['fills_active_symbols_count'] or 0)} 币产生过成交。"
        "下表按每个币种的**首次成交日期**排列；"
        f"冰点附近的槽位占用直接由可交易币数与 `n_positions = {conf['n_positions']:.0f}` 共同决定。",
        "",
        spec.md_table(
            [
                [
                    row["coin"],
                    row["first_fill"],
                    row["last_fill"],
                    f"{row['fills']:,}",
                    spec.fmt_money(row["net_realized_pnl_usd"]),
                ]
                for row in coin_windows
            ],
            ["币种", "首次成交", "末次成交", "成交数", "净已实现 PnL"],
        ),
        "",
        f"- {len(dataset.get('coins') or []) - int(facts['fills_active_symbols_count'] or 0)} "
        "个未产生成交的币种在本次运行里既没有入场也没有平仓，"
        "原因是策略在它们进入可交易区间之前就已经停止交易（见上一节）。",
        f"- 净已实现 PnL 为正 {ledger['net_positive_coins']} 个、为非正 "
        f"{ledger['net_negative_coins']} 个。",
        f"- 成交最集中的币种是 {ledger['top_symbol']}（{ledger['top_symbol_fills']:,} 笔，"
        f"占全部成交 {ledger['top_symbol_share'] * 100:.2f}%）；"
        f"净已实现 PnL 最高的三个正贡献币种合计占**全部正贡献币种净额**的 "
        f"{ledger['top3_positive_share'] * 100:.2f}%。"
        "单币占比高意味着结果对这种币的价格路径更敏感。",
        "",
    ]
    return lines


def appendix_prior(
    prior: dict[str, Any],
    cfg: dict[str, Any],
    dataset: dict[str, Any],
    ledger: dict[str, Any],
    facts: dict[str, Any],
) -> list[str]:
    lines = ["### 与既有 `hsl_npos1` 研究工件的对照", ""]
    if not prior.get("present"):
        lines.append(f"- 既有工件 `{study.PRIOR_RUN_LABEL}` 不在当前工作区，本对照省略。")
        lines.append("")
        return lines
    lines.append(
        "同一 profile 在本仓库已被回测为低回撤研究的一格。两者执行与成本口径相同、"
        "窗口与币篮不同，因此只做定性对照，绝不把两个数字放进同一张表做减法。"
    )
    lines.append("")
    lines.append(
        spec.md_table(
            [
                [
                    "窗口（请求）",
                    f"{prior['window'][0]} → {prior['window'][1]}",
                    f"{cfg['backtest']['start_date']} → {cfg['backtest']['end_date']}",
                ],
                [
                    "有效区间（实测）",
                    f"{prior['effective_start']} → {prior['effective_end']}",
                    f"{facts['effective_start_date']} → {facts['effective_end_date']}",
                ],
                ["币数", str(prior["coins"]), str(len(dataset.get("coins") or []))],
                [
                    "权益采样",
                    f"每 {prior['balance_sample_divider']} 分钟",
                    f"每 {cfg['backtest']['balance_sample_divider']} 分钟",
                ],
                [
                    "执行口径",
                    f"delay={prior['execution_delay_bars']}"
                    f"（T+{prior['execution_delay_bars'] + 1}）、"
                    f"{prior['intrabar_fill_order']}",
                    f"delay={cfg['backtest']['execution_delay_bars']}"
                    f"（T+{int(cfg['backtest']['execution_delay_bars']) + 1}）、"
                    f"{cfg['backtest']['intrabar_fill_order']}",
                ],
                ["成交数", f"{prior['fills']:,}", f"{ledger['fills_count']:,}"],
                [
                    "strategy-equity 最差回撤",
                    spec.fmt_pct(prior["dd_strategy_eq"]),
                    spec.fmt_pct(facts["drawdown_worst_strategy_eq"]),
                ],
                [
                    "`gain_strategy_eq`",
                    f"{prior['gain_strategy_eq']:.6f}",
                    f"{float(facts['gain_strategy_eq'] or 0.0):.6f}",
                ],
            ],
            ["口径项", f"既有研究格 `{study.PRIOR_RUN_LABEL}`", "本工件"],
        )
    )
    lines.append("")
    lines.append(
        f"- 既有工件见 `{study.PRIOR_RUN_LABEL}`，其选择结论写在该研究的 "
        "`low_drawdown_strategy_report.md` 中；本报告不重复也不改写它的排名结论。"
    )
    lines.append(
        "**这两行不能直接相减。** 既有工件的窗口起点在本次运行的硬停事件之后，"
        "所以它测到的是一个已重置引擎在硬停之后的表现；"
        "本工件测到的是同一个 profile 在硬停之前 30 天的表现加上硬停本身。"
        "把 25.57% 与 47.94% 并列成“谁更差”会同时忽略样本差异与止损事件差异。"
    )
    lines.append("")
    return lines


def appendix_boundaries(
    ledger: dict[str, Any], audit: dict[str, Any], cfg: dict[str, Any], facts: dict[str, Any]
) -> list[str]:
    delay = int(cfg["backtest"]["execution_delay_bars"])
    lines = [
        "### 执行与 look-ahead 边界",
        "",
        f"1. **执行时点是模型假设。** 本工件采用 `execution_delay_bars={delay}`（T+{delay + 1}）、"
        f"`intrabar_fill_order={cfg['backtest']['intrabar_fill_order']}`；"
        "它不含盘口队列、部分成交、撤单竞争与真实撮合路径。",
    ]
    if audit.get("present"):
        lines.append(
            f"   该假设的逐笔记录在 `{audit['path']}`：{audit['rows']:,} 行，"
            f"`activation_index == decision_index + 1 + {delay}` 的违例 "
            f"{audit['activation_identity_failures']} 行、成交早于激活的违例 "
            f"{audit['fill_before_activation_failures']} 行；"
            f"激活后等待根数的中位数 {audit['median_waited_bars']:.1f}，"
            f"最长 {audit['max_waited_bars']:,}。"
        )
    else:
        lines.append("   本次运行没有留下 `execution_audit.csv`，因此该假设无法逐笔复核。")
    lines.extend(
        [
            f"2. **maker 身份与费率是假设。** 本次 {ledger['maker_fills']:,} 笔成交全部记为 "
            f"maker、taker {ledger['taker_fills']:,} 笔。taker 为 0 时，"
            f"`backtest.taker_fee_override = {cfg['backtest']['taker_fee_override']}` 与 "
            f"`market_order_slippage_pct = {cfg['backtest'].get('market_order_slippage_pct')}` "
            "**对本次结果没有影响**；非市价订单统一按挂单价与 maker 费率记账，"
            "`GTC` 不代表 maker-only，真实排队与部分成交仍未被模拟。",
            f"3. **同 bar 顺序是确定性约定。** 本次有 "
            f"{ledger['minutes_with_multiple_fills']:,} 个分钟含多笔成交"
            f"（单分钟最多 {ledger['max_fills_in_a_minute']} 笔），"
            f"{ledger['coin_minutes_with_entry_and_close']:,} 个“同币同分钟既有 entry 又有 "
            "close”。仅凭 OHLC 无法恢复这些成交的真实先后路径。",
            "4. **参数时间旅行。** 该 profile 形成于 2026 年，"
            f"被回放到 {cfg['backtest']['start_date']} 起的历史；"
            "结论只能表述为“该参数在这段历史数据上的模拟表现”。",
            f"5. **强平未被模拟到。** `liquidated = {facts['liquidated']}` 是模拟结果，"
            "不等于真实保证金体系下不会强平；资金费率、保证金阶梯与维持保证金均不在回测内。"
            f"冰点区间内策略权益一度跌至峰值的 "
            f"{ledger['drawdown']['trough_value'] / ledger['drawdown']['peak_value'] * 100:.2f}%，"
            "真实杠杆与维持保证金约束未被建模。",
            f"6. **数据完成度。** `backtest_completion_ratio = "
            f"{float(facts['backtest_completion_ratio'] or 0.0):.6f}`，"
            f"回测天数 {float(facts['n_days'] or 0.0):,.2f} 天；"
            "该比值衡量的是请求区间的数据完整度，不是策略稳健性。",
            "",
        ]
    )
    return lines


def study_appendix(
    cfg: dict[str, Any],
    dataset: dict[str, Any],
    ledger: dict[str, Any],
    audit: dict[str, Any],
    prior: dict[str, Any],
    coin_windows: list[dict],
    analysis: dict[str, Any],
) -> str:
    facts = spec.analysis_facts(analysis)
    lines: list[str] = []
    lines += appendix_engine(ledger, facts)
    lines += appendix_drawdown(ledger, facts)
    lines += appendix_lifecycle(ledger, analysis)
    lines += appendix_coins(ledger, facts, dataset, coin_windows)
    lines += appendix_prior(prior, cfg, dataset, ledger, facts)
    lines += appendix_boundaries(ledger, audit, cfg, facts)
    return "\n".join(lines)


def artifacts_table(
    result_dir: Path, audit: dict[str, Any], equity: pd.DataFrame, analysis: dict[str, Any]
) -> list[list[str]]:
    return [
        ["`analysis.json`", "回测原生指标（USD / BTC / strategy-equity 三套口径）"],
        ["`fills.csv`", f"{int(analysis['fills_count']):,} 条成交账本"],
        ["`balance_and_equity.csv.gz`", f"{len(equity):,} 行逐分钟权益采样"],
        ["`config.json`", "本工件使用的完整有效配置（含生效币篮与执行键）"],
        ["`dataset.json`", "数据集来源、缓存标识、币种与内容哈希"],
        [
            f"`{audit.get('path', study.relative(study.EXECUTION_AUDIT_PATH))}`",
            f"{audit.get('rows', 0):,} 行逐笔执行审计" if audit.get("present") else "缺失",
        ],
        [
            "`annual_metrics.csv` / `monthly_metrics.csv` / `coin_metrics.csv`",
            "本报告三张汇总表的原始数据",
        ],
        [
            "`balance_and_equity.png` / `balance_and_equity_logy.png` / `drawdown.png` / "
            "`total_wallet_exposure.png` / `pnl_cumsum.png` / `hard_stop_drawdown.png`",
            "图表（per-coin 面板按 `backtest.disable_plotting = coin_fills` 关闭）",
        ],
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument("--report-title", default=None)
    args = parser.parse_args()

    result_dir = find_run_dir(args.result_dir)
    require_artifacts(result_dir)

    analysis = load_json(result_dir / "analysis.json")
    cfg = load_json(result_dir / "config.json")
    dataset = load_json(result_dir / "dataset.json")

    fills = spec.load_fills(result_dir)
    equity = spec.load_balance_equity(result_dir)

    annual = spec.build_period_table(equity, fills, "Y")
    monthly = spec.build_period_table(equity, fills, "M")
    coins = coin_metrics_extras(fills)
    ledger = ledger_facts(fills, equity, dataset, cfg, analysis)
    audit = audit_facts(audit_path_for(result_dir), int(cfg["backtest"]["execution_delay_bars"]))
    prior = prior_run_facts()
    coin_windows = coin_window_facts(fills)

    classify = classify_types(ledger["type_counts"])
    if classify["unknown_entry_types"] or classify["unknown_close_types"]:
        raise SystemExit(
            "unrecognised order types in the ledger: "
            f"{classify['unknown_entry_types']} / {classify['unknown_close_types']}. "
            "Review them before rendering a report that would otherwise classify them silently."
        )

    annual.to_csv(result_dir / "annual_metrics.csv", index=False)
    monthly.to_csv(result_dir / "monthly_metrics.csv", index=False)
    # The CSV keeps the frame's alphabetical order; the report's table and its 最高/最低 prose
    # need the convention's descending net-realized-PnL order.
    coins.to_csv(result_dir / "coin_metrics.csv", index=False)
    coins = coins.sort_values("net_realized_pnl_usd", ascending=False, kind="stable").reset_index(
        drop=True
    )

    try:
        result_label = str(result_dir.relative_to(study.REPO))
    except ValueError:
        result_label = str(result_dir)

    facts = spec.analysis_facts(analysis)
    equity_lo = equity["timestamp"].iloc[0]
    equity_hi = equity["timestamp"].iloc[-1]
    outside_mask = (fills["timestamp"] < equity_lo) | (fills["timestamp"] > equity_hi)
    outside_fills = int(outside_mask.sum())
    outside_pnl = float(
        fills.loc[outside_mask, "pnl"].sum() + fills.loc[outside_mask, "fee_paid"].sum()
    )
    life = ledger["lifecycle"]

    context: dict[str, Any] = {
        "analysis": analysis,
        "config": cfg,
        "annual": annual,
        "monthly": monthly,
        "coins": coins,
        "attribution": spec.build_attribution_table(fills),
        "run_record": {
            "candidate_id": study.STUDY_ID,
            "universe": {"exchange": dataset.get("exchange", "binance")},
        },
        "ledger": ledger,
        "result_label": result_label,
        "title_kind": "HSL 启用的 Trailing Martingale 策略深度分析",
        "scope_lines": scope_lines(cfg, dataset, ledger),
        "appendix": study_appendix(cfg, dataset, ledger, audit, prior, coin_windows, analysis),
        "artifacts": artifacts_table(result_dir, audit, equity, analysis),
        "verifiable_notes": [
            f"- 权益采样端点：`balance_and_equity.csv.gz` 覆盖 "
            f"{equity['timestamp'].iloc[0]} → {equity['timestamp'].iloc[-1]}"
            f"（{len(equity):,} 行）；`fills.csv` 有 {outside_fills} 笔成交发生在采样端点之外"
            f"（合计净 {outside_pnl:+.6f} USDT），因此年度/月度表的净已实现 PnL 合计与"
            "全账本合计的差值只能由该端点差解释。",
            f"- 交易区间：末笔成交 {life['last_fill_utc']}，其后 "
            f"{life['idle_days_after_last_fill']:,.2f} 天无任何成交，"
            "因此 2022 年起的零成交期间不是数据缺失。",
            f"- 源 profile：`configs/examples/hsl_npos1.json`（sha256 "
            f"`{study.SOURCE_CONFIG_SHA256}`）；运行配置为 "
            f"`{study.relative(study.CONFIG_PATH)}`。两者只在 `backtest` 的重定向键与"
            "`live.approved_coins` 上不同，由 `report_tools/build_run_config.py` 在构建阶段校验。",
            f"- 冻结数据集：`{dataset.get('cache_dir_label')}`"
            f"（`cache_hash = {dataset.get('cache_hash')}`，`dataset_override = "
            f"{bool(dataset.get('dataset_override'))}`）；`hlcvs` 内容哈希 "
            f"`{str((dataset.get('content_hashes') or {}).get('hlcvs', ''))[:16]}…`。"
            "该数据集由回测从本地离线 K 线目录物化，本次运行未下载数据。",
            "- 成交类型白名单：本次出现的全部订单类型都在 `report_tools/hsl_npos1_spec.py` 的 "
            "`ENTRY_TEMPLATES` / `CLOSE_TEMPLATES` 内；出现白名单外类型时渲染器会中止，"
            "而不是静默归类。",
            f"- 运行日志与执行审计：`{study.relative(study.RUN_LOG_PATH)}`、"
            f"`{audit.get('path', study.relative(study.EXECUTION_AUDIT_PATH))}`。",
            f"- 币篮：示例 profile 声明 "
            f"{len(study.load_json(study.SOURCE_CONFIG).get('live', {}).get('approved_coins', {}).get('long') or [])} 币、"
            f"本次运行实际使用 {len(dataset.get('coins') or [])} 币；"
            "被剔除的币种在本地 K 线目录中没有数据，详情见 `## 口径与范围`。",
            "- 本报告与三张汇总 CSV、`analysis.json` 的一致性由同目录 "
            "`report_tools/verify_hsl_npos1.py` 独立复算校验"
            "（该脚本不 import 渲染器）。",
        ],
        "interpretation_extra": [
            f"中心事实：策略只在 {life['first_fill_utc'][:10]} 至 "
            f"{life['last_fill_utc'][:10]} 之间交易过（{life['active_days']} 个自然日，"
            f"占有效区间的 {life['active_share_of_window'] * 100:.2f}%），"
            f"随后 {life['idle_days_after_last_fill']:,.2f} 天没有任何成交；"
            "全期回撤与 gain 都由这 30 天加一次硬停平仓决定。",
            f"该 profile 的 long `n_positions = {ledger['config']['n_positions']:.0f}`，"
            f"本次运行的实测最大非零 long 仓位数也是 {ledger['nonzero_long_max']}，"
            "冰点附近槽位被 10 个币同时占满。",
            f"风险层成交（`close_auto_reduce_*`、`close_unstuck_long`、`close_panic_long`）合计 "
            f"{ledger['risk_fill_total']:,} 笔，占全部成交的 "
            f"{ledger['risk_fill_total'] / max(ledger['fills_count'], 1) * 100:.2f}%；"
            "其中 panic 平仓是唯一一次由风险层驱动的全仓退出。",
        ],
    }
    if args.report_title:
        context["title"] = args.report_title
    report = spec.render_annual_analysis(context)
    problems = spec.assert_report_structure(report)
    if problems:
        raise SystemExit("report structure does not match the convention: " + "; ".join(problems))
    (result_dir / "annual_analysis.md").write_text(report, encoding="utf-8")
    # The convention's persisted layout is checked after writing, so this tool reports a complete
    # bundle or fails: `docs/ai/runbooks/strategy_report.md`, Rule 6.
    layout_problems = spec.assert_bundle_layout(result_dir, config=cfg)
    if layout_problems:
        raise SystemExit(
            "persisted bundle does not match the convention: " + "; ".join(layout_problems)
        )
    print(f"wrote {result_dir / 'annual_analysis.md'}")
    print(f"wrote {result_dir / 'annual_metrics.csv'} ({len(annual)} rows)")
    print(f"wrote {result_dir / 'monthly_metrics.csv'} ({len(monthly)} rows)")
    print(f"wrote {result_dir / 'coin_metrics.csv'} ({len(coins)} rows)")
    print(f"bundle layout: complete ({len(spec.expected_bundle_files(cfg))} expected files)")
    print(
        "ledger: nonzero_long_max="
        f"{ledger['nonzero_long_max']} twe_long_max={ledger['max_twe_long']:.6f} "
        f"audit_rows={audit.get('rows')} risk_fills={ledger['risk_fill_total']:,} "
        f"active_days={life['active_days']} idle_days={life['idle_days_after_last_fill']:,.2f}"
    )


if __name__ == "__main__":
    main()
