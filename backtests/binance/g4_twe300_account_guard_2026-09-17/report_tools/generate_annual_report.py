#!/usr/bin/env python3
"""Render one arm's deep analysis for the g4 @ TWE 3.0 account-guard study.

The section skeleton, the metric tables and the interpretation rules are the shared
convention (`backtests/report_spec/annual_analysis.py`); this tool supplies what is specific
to the study:

* `## 口径与范围` lines: the arm's declared changes, its leg and frozen dataset, the sampling
  resolution, the disabled figure group on the long leg, the offline boundary;
* `## 研究附录`: the exposure bounds ("wipe-out distance"), the shock-loss matrix, the derived
  event table, the HSL runtime telemetry, the cost of the circuit breaker, the honesty
  boundaries and the reading for the live decision;
* two machine-readable side artifacts next to the report (`tail_risk_events.json`,
  `tail_risk_wipeout.json`) that the synthesis and the independent verifier read.

Offline only. No network, no credentials, no exchange account, no bot start.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPORT_SPEC_DIR = Path(__file__).resolve().parents[3] / "report_spec"
TOOLS_DIR = Path(__file__).resolve().parent
for path in (str(REPORT_SPEC_DIR), str(TOOLS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import annual_analysis as spec  # noqa: E402  (canonical report convention)
import event_windows as events  # noqa: E402
import guard_analysis as guard  # noqa: E402
import variant_spec as study  # noqa: E402
import wipeout_matrix as wipeout  # noqa: E402

REQUIRED_ARTIFACTS = (
    "analysis.json",
    "config.json",
    "dataset.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
)
EVENTS_ARTIFACT = "tail_risk_events.json"
WIPEOUT_ARTIFACT = "tail_risk_wipeout.json"
GUARD_ARTIFACT = "guard_readiness.json"
SYNTHESIS_PATH = study.STUDY / "tail_risk_analysis.md"

#: How each compared metric is formatted (`kind`, digits).
METRIC_FORMATS: dict[str, tuple[str, int]] = {
    "gain_strategy_eq": ("multiple", 6),
    "adg_strategy_eq": ("pct", 4),
    "mdg_strategy_eq": ("pct", 4),
    "drawdown_worst_strategy_eq": ("pct", 2),
    "drawdown_worst_mean_1pct_strategy_eq": ("pct", 2),
    "strategy_eq_recovery_days_max": ("days", 2),
    "peak_recovery_days_strategy_eq": ("days", 2),
    "strategy_eq_underwater_pct_mean": ("pct", 2),
    "sortino_ratio_strategy_eq": ("ratio", 4),
    "sharpe_ratio_strategy_eq": ("ratio", 4),
    "omega_ratio_strategy_eq": ("ratio", 4),
    "sterling_ratio_strategy_eq": ("ratio", 4),
    "expected_shortfall_1pct_strategy_eq": ("pct", 4),
    "loss_profit_ratio": ("ratio", 4),
    "fills_count": ("count", 0),
    "fills_count_entry": ("count", 0),
    "fills_count_close": ("count", 0),
    "fills_active_symbols_count": ("count", 0),
    "exposure_mean_ratio_usd": ("ratio", 6),
    "high_exposure_days_max_long": ("days", 2),
    "total_wallet_exposure_max": ("ratio", 6),
    "total_wallet_exposure_mean": ("ratio", 6),
    "backtest_completion_ratio": ("ratio", 4),
    "n_days": ("days", 2),
    "hard_stop_triggers": ("count", 0),
    "hard_stop_triggers_long": ("count", 0),
    "hard_stop_triggers_per_year": ("ratio", 4),
    "hard_stop_restarts": ("count", 0),
    "hard_stop_restarts_long": ("count", 0),
    "hard_stop_time_in_yellow_pct": ("pct", 4),
    "hard_stop_time_in_orange_pct": ("pct", 4),
    "hard_stop_time_in_red_pct": ("pct", 4),
    "hard_stop_duration_minutes_mean": ("minutes", 2),
    "hard_stop_duration_minutes_max": ("minutes", 2),
    "hard_stop_flatten_time_minutes_mean": ("minutes", 2),
    "hard_stop_trigger_drawdown_mean": ("ratio", 4),
    "hard_stop_panic_close_loss_sum": ("usdt", 4),
    "hard_stop_panic_close_loss_max": ("usdt", 4),
    "hard_stop_panic_close_loss_drawdown_pct_min": ("pct", 4),
    "hard_stop_panic_close_loss_drawdown_pct_mean": ("pct", 4),
    "hard_stop_panic_close_loss_drawdown_pct_max": ("pct", 4),
    "hard_stop_post_restart_retrigger_pct": ("pct", 2),
    "hard_stop_halt_to_restart_equity_loss_pct": ("pct", 4),
}
METRIC_LABELS: dict[str, str] = {
    "gain_strategy_eq": "策略权益增长倍数 `gain_strategy_eq`",
    "adg_strategy_eq": "平均日增长 `adg_strategy_eq`",
    "mdg_strategy_eq": "平均日回撤 `mdg_strategy_eq`",
    "drawdown_worst_strategy_eq": "全期策略权益最差回撤",
    "drawdown_worst_mean_1pct_strategy_eq": "最差 1% 均值回撤",
    "strategy_eq_recovery_days_max": "最长策略权益恢复期（天）",
    "strategy_eq_underwater_pct_mean": "平均水下比例",
    "expected_shortfall_1pct_strategy_eq": "最差 1% 条件期望损失",
    "omega_ratio_strategy_eq": "Omega（策略权益）",
    "sterling_ratio_strategy_eq": "Sterling（策略权益）",
    "sortino_ratio_strategy_eq": "Sortino（策略权益）",
    "sharpe_ratio_strategy_eq": "Sharpe（策略权益）",
    "loss_profit_ratio": "亏损/盈利比",
    "fills_count": "成交笔数",
    "fills_count_entry": "入场成交",
    "fills_count_close": "减仓或平仓成交",
    "fills_active_symbols_count": "有成交的币数",
    "exposure_mean_ratio_usd": "平均敞口/余额",
    "high_exposure_days_max_long": "高敞口天数（最长连续）",
    "total_wallet_exposure_max": "总钱包敞口最大",
    "total_wallet_exposure_mean": "总钱包敞口均值",
    "backtest_completion_ratio": "回测完成度",
    "n_days": "回测天数",
    "hard_stop_triggers": "HSL 触发次数",
    "hard_stop_triggers_long": "多头 HSL 触发次数",
    "hard_stop_triggers_per_year": "HSL 触发次数/年",
    "hard_stop_restarts": "HSL 冷却后重启次数",
    "hard_stop_restarts_long": "多头 HSL 重启次数",
    "hard_stop_time_in_yellow_pct": "处于 YELLOW 的采样占比",
    "hard_stop_time_in_orange_pct": "处于 ORANGE 的采样占比",
    "hard_stop_time_in_red_pct": "处于 RED 的采样占比",
    "hard_stop_duration_minutes_mean": "停机时长均值（分钟）",
    "hard_stop_duration_minutes_max": "停机时长最大值（分钟）",
    "hard_stop_flatten_time_minutes_mean": "平仓耗时均值（分钟）",
    "hard_stop_trigger_drawdown_mean": "触发时回撤分数均值",
    "hard_stop_panic_close_loss_sum": "panic 平仓损失合计（USDT）",
    "hard_stop_panic_close_loss_max": "单次 panic 平仓最大损失（USDT）",
    "hard_stop_panic_close_loss_drawdown_pct_min": "panic 损失/回撤 最小",
    "hard_stop_panic_close_loss_drawdown_pct_mean": "panic 损失/回撤 均值",
    "hard_stop_panic_close_loss_drawdown_pct_max": "panic 损失/回撤 最大",
    "hard_stop_post_restart_retrigger_pct": "重启后再次触发 RED 的比例",
    "hard_stop_halt_to_restart_equity_loss_pct": "停机→重启期间权益损失",
}

#: Metrics the study appendix prints as its own "tail panel".
TAIL_PANEL_METRICS = (
    "gain_strategy_eq",
    "adg_strategy_eq",
    "drawdown_worst_strategy_eq",
    "drawdown_worst_mean_1pct_strategy_eq",
    "expected_shortfall_1pct_strategy_eq",
    "total_wallet_exposure_max",
    "total_wallet_exposure_mean",
    "strategy_eq_recovery_days_max",
    "strategy_eq_underwater_pct_mean",
    "omega_ratio_strategy_eq",
    "sterling_ratio_strategy_eq",
    "sortino_ratio_strategy_eq",
    "fills_count",
    "fills_active_symbols_count",
    "backtest_completion_ratio",
    "n_days",
)


def fail(problems: list[str], headline: str) -> None:
    print(f"FAIL: {headline}", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    raise SystemExit(1)


def arm_of(key: str) -> study.Variant:
    try:
        return study.VARIANTS_BY_KEY[key]
    except KeyError:
        raise SystemExit(
            f"unknown arm {key!r}; expected one of {list(study.DEFAULT_VARIANT_ORDER)}"
        ) from None


def require_artifacts(result_dir: Path, variant: study.Variant) -> None:
    missing = [name for name in REQUIRED_ARTIFACTS if not (result_dir / name).exists()]
    if not variant.execution_audit_path.exists():
        missing.append(study.relative(variant.execution_audit_path))
    if missing:
        fail(missing, f"run directory {result_dir} is missing artifacts")
    for name in ("run_record.json", "global_metrics.json"):
        if not (result_dir / name).exists():
            fail([name], f"{study.relative(result_dir)} was not collected by run_variant.py")


def fmt_metric(key: str, value: Any) -> str:
    if value is None:
        return "n/a"
    kind, digits = METRIC_FORMATS.get(key, ("number", 6))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(number):
        return "n/a"
    if kind == "count":
        return f"{int(round(number)):,}"
    if kind == "pct":
        return f"{number * 100:.{digits}f}%"
    if kind == "usdt":
        return f"{number:+,.{digits}f}"
    if kind == "multiple":
        return f"{number:.{digits}f}×"
    if kind == "days":
        return f"{number:.{digits}f}"
    return f"{number:.{digits}f}"


def ledger_facts(
    fills: pd.DataFrame, equity: pd.DataFrame, cfg: dict[str, Any], variant: study.Variant
) -> dict[str, Any]:
    """Facts derived from one arm's own ledger, in the convention's units."""
    risk = cfg["bot"]["long"]["risk"]
    hsl = cfg["bot"]["long"].get("hsl") or {}
    single_slot_cap = (
        float(risk["total_wallet_exposure_limit"])
        / float(risk["n_positions"])
        * (1.0 + float(risk["we_excess_allowance_pct"]))
    )
    exposure = fills["wallet_exposure"].abs()
    panic_mask = fills["type"].astype(str).str.contains(study.PANIC_FILL_MARKER)
    panic = fills.loc[panic_mask]
    reference = (
        study.REFERENCE_RUNS["twe300_10k__3y"]
        if variant.leg == "3y"
        else study.REFERENCE_RUNS["twe300_10k__ext"]
    )
    starting_balance = float(cfg["backtest"].get("starting_balance") or 0.0)
    liquidation_threshold = float(cfg["backtest"].get("liquidation_threshold") or 0.0)
    peak_balance = None
    if "twe_long" in fills and "usd_total_balance" in fills and not fills.empty:
        clean = fills.dropna(subset=["twe_long", "usd_total_balance"])
        if not clean.empty:
            peak_balance = float(clean.loc[clean["twe_long"].idxmax(), "usd_total_balance"])
    peak_exposure = float(fills["twe_long"].max()) if "twe_long" in fills else None
    return {
        "variant": variant.key,
        "fills_count": int(fills.shape[0]),
        "panic_fill_count": int(panic.shape[0]),
        "panic_loss_usd": float(panic["pnl"].fillna(0.0).sum()) if not panic.empty else 0.0,
        "panic_fees_usd": float(panic["fee_paid"].fillna(0.0).sum()) if not panic.empty else 0.0,
        "panic_coins": sorted(panic["coin"].unique().tolist()) if not panic.empty else [],
        "single_slot_exposure_cap": single_slot_cap,
        "exposure_max": float(exposure.max()) if not exposure.empty else 0.0,
        "twe_peak_at_fills": peak_exposure,
        "starting_balance": starting_balance,
        "declared_twe": float(risk["total_wallet_exposure_limit"]),
        "declared_allowance_pct": float(risk["we_excess_allowance_pct"]),
        "n_positions": float(risk["n_positions"]),
        "liquidation_threshold": liquidation_threshold,
        "liquidation_floor_usd": starting_balance * liquidation_threshold,
        "peak_exposure_balance_usd": peak_balance,
        "liquidation_shock_at_peak": study.liquidation_shock(
            peak_exposure=peak_exposure or 0.0,
            balance_usd=peak_balance or 0.0,
            starting_balance=starting_balance,
            threshold=liquidation_threshold,
        ),
        "taker_fills_count": int(fills["liquidity"].astype(str).str.contains("taker").sum()),
        "maker_fills_count": int(fills["liquidity"].astype(str).str.contains("maker").sum()),
        "strategy_equity_equals_usd_total_equity": bool(
            np.allclose(
                equity["strategy_equity"].to_numpy(dtype="float64"),
                equity["usd_total_equity"].to_numpy(dtype="float64"),
                rtol=0.0,
                atol=1e-6,
            )
        ),
        "hsl_block": hsl,
        "hsl_signal_mode": (cfg.get("live") or {}).get("hsl_signal_mode"),
        "guard": variant.guard_params,
        "guard_label": variant.guard_label,
        "pnls_max_lookback_days": (cfg.get("live") or {}).get("pnls_max_lookback_days"),
        "max_realized_loss_pct": (cfg.get("live") or {}).get("max_realized_loss_pct"),
        "synthetic": variant.synthetic,
        "reference_arm": reference["label"] if reference else None,
    }


def audit_facts(path: Path, execution_delay_bars: int) -> dict[str, Any]:
    import csv

    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = reader.fieldnames or []
    latencies: list[float] = []
    for row in rows[:200000]:
        value = row.get("latency_ms") or row.get("created_latency_ms")
        if value in (None, ""):
            continue
        try:
            latencies.append(float(value))
        except ValueError:
            continue
    return {
        "rows": len(rows),
        "columns": list(columns),
        "execution_delay_bars": execution_delay_bars,
        "latency_ms_mean": float(np.mean(latencies)) if latencies else None,
        "latency_ms_max": float(np.max(latencies)) if latencies else None,
    }


def events_artifact(result_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = study.load_json(result_dir / EVENTS_ARTIFACT)
    return payload.get("events", []), payload.get("rule", {})


def wipeout_artifact(result_dir: Path) -> dict[str, Any]:
    return study.load_json(result_dir / WIPEOUT_ARTIFACT)


def guard_artifact(result_dir: Path) -> dict[str, Any]:
    return study.load_json(result_dir / GUARD_ARTIFACT)


def build_tail_artifacts(
    result_dir: Path, variant: study.Variant
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Derive the event table and the wipe-out matrix for this arm and persist them."""
    equity = events.load_equity(result_dir)
    fills = events.load_fills(result_dir)
    analysis = study.load_json(result_dir / "analysis.json")
    benchmark = events.load_benchmark(variant.dataset.path)
    rule = study.EVENT_RULE
    episodes = events.detect_episodes(
        benchmark,
        lookback_days=int(rule["lookback_days"]),
        threshold=float(rule["threshold"]),
        min_gap_days=int(rule["min_gap_days"]),
        labels=study.EVENT_LABELS,
    )
    table = events.arm_event_table(equity, fills, episodes, synthetic=False)
    summary = events.full_window_summary(equity, fills)
    panic_mask = fills["type"].astype(str).str.contains(study.PANIC_FILL_MARKER)
    panic = fills.loc[panic_mask]
    events_payload = {
        "arm": variant.key,
        "leg": variant.leg,
        "synthetic": variant.synthetic,
        "ledger": {
            "fills_count": int(fills.shape[0]),
            "panic_fills": int(panic.shape[0]),
            "panic_loss_usd": float(panic["pnl"].fillna(0.0).sum()) if not panic.empty else 0.0,
            "panic_fees_usd": float(panic["fee_paid"].fillna(0.0).sum()) if not panic.empty else 0.0,
            "panic_coins": sorted(panic["coin"].unique().tolist()) if not panic.empty else [],
            "taker_fills": int(fills["liquidity"].astype(str).str.contains("taker").sum()),
            "maker_fills": int(fills["liquidity"].astype(str).str.contains("maker").sum()),
        },
        "rule": {
            **rule,
            "labels": [list(item) for item in study.EVENT_LABELS],
            "measurement": (
                "事件窗口 = 基准事件前高点到该 arm 自身策略权益恢复原高点（最长 180 天）；"
                "回撤基于 balance_and_equity.csv.gz 的采样分辨率"
            ),
        },
        "benchmark": {
            "episodes": [
                {
                    "key": episode.key,
                    "label": episode.label,
                    "breach_start": pd.Timestamp(
                        episode.breach_start_ms, unit="ms", tz="UTC"
                    ).strftime("%Y-%m-%d"),
                    "breach_end": pd.Timestamp(episode.breach_end_ms, unit="ms", tz="UTC").strftime(
                        "%Y-%m-%d"
                    ),
                    "trough": pd.Timestamp(episode.trough_ms, unit="ms", tz="UTC").strftime(
                        "%Y-%m-%d"
                    ),
                    "drop_pct": episode.drop_pct,
                }
                for episode in episodes
            ],
            "count": len(episodes),
        },
        "events": table,
        "full_window": {
            key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
            for key, value in summary.items()
        },
    }
    study.write_json(result_dir / EVENTS_ARTIFACT, events_payload)

    start_balance = float(equity["usd_total_balance"].iloc[0]) if not equity.empty else None
    per_coin = wipeout.per_coin_max_exposure(fills)
    config_json = study.load_json(result_dir / "config.json")
    matrix = wipeout.build_matrix(
        variant.key,
        analysis=analysis,
        per_coin=per_coin,
        fills=fills,
        shock_levels=study.SHOCK_LEVELS,
        declared_twe=float(config_json["bot"]["long"]["risk"]["total_wallet_exposure_limit"]),
        start_balance_usd=start_balance,
        starting_balance_usd=float(config_json["backtest"].get("starting_balance") or 0.0),
        liquidation_threshold=float(config_json["backtest"].get("liquidation_threshold") or 0.0),
    )
    problems = wipeout.assert_identities(matrix)
    if problems:
        fail(problems, f"{variant.key}: the wipe-out matrix violates its own identities")
    wipeout_payload = {
        "arm": variant.key,
        "leg": variant.leg,
        "synthetic": variant.synthetic,
        "per_coin_max_exposure": [
            {"coin": coin, "max_abs_wallet_exposure": value} for coin, value in matrix.per_coin
        ],
        "config_risk": variant_risk_block(result_dir),
        "capital": {
            "starting_balance": float(config_json["backtest"].get("starting_balance") or 0.0),
            "liquidation_threshold": float(
                config_json["backtest"].get("liquidation_threshold") or 0.0
            ),
            "liquidation_floor_usd": float(
                config_json["backtest"].get("starting_balance") or 0.0
            )
            * float(config_json["backtest"].get("liquidation_threshold") or 0.0),
        },
        "survival": {
            "liquidated": bool(analysis.get("liquidated")),
            "effective_end_date": analysis.get("effective_end_date"),
        },
        "ruin": wipeout.ruin_summary_rows([matrix])[0],
        "matrix": matrix.rows(),
        "shock_levels": list(study.SHOCK_LEVELS),
        "warnings": [],
    }
    study.write_json(result_dir / WIPEOUT_ARTIFACT, wipeout_payload)

    guard_payload = guard.build_guard_artifact(result_dir, analysis)
    guard_payload["arm"] = variant.key
    guard_payload["guard"] = variant.guard_params
    guard_payload["guard_label"] = variant.guard_label
    study.write_json(result_dir / GUARD_ARTIFACT, guard_payload)
    return (
        table,
        events_payload,
        wipeout_payload,
        guard_payload,
        variant_risk_block(result_dir),
    )


def variant_risk_block(result_dir: Path) -> dict[str, Any]:
    cfg = study.load_json(result_dir / "config.json")
    risk = cfg["bot"]["long"]["risk"]
    return {key: risk.get(key) for key in study.PARENT_RISK}


def scope_lines(
    result_dir: Path,
    variant: study.Variant,
    record: dict[str, Any],
    global_metrics: dict[str, Any],
    cfg: dict[str, Any],
    facts: dict[str, Any],
    variant_input: dict[str, Any],
) -> list[str]:
    dataset = global_metrics["dataset"]
    deltas = record["declared_delta"]
    leg_note = (
        "原生窗口腿（与发布 profile 同数据集/同窗口）"
        if variant.leg == "3y"
        else "5.4 年历史压力腿（同一冻结 bundle 的 dataset override）"
        if variant.leg == "ext"
        else "合成情景腿（派生 bundle 的价格路径注入，非历史事实）"
    )
    disabled = global_metrics.get("disabled_plot_groups") or []
    lines = [
        f"- 本 arm：`{variant.key}`（杠杆 `{variant.lever}`，{leg_note}）。{variant.description}",
        f"- 声明的改动（相对冻结父配置，逐路径断言，共 {len(deltas)} 条）："
        + (
            "；".join(f"`{item['path']}` {item['from']!r}→{item['to']!r}" for item in deltas)
            if deltas
            else "无（控制组）"
        )
        + "。",
        f"- 冻结数据集：`{dataset['cache_dir']}`（leg=`{variant.leg}`，override_mode="
        f"`{dataset.get('override_mode')}`，manifest config_hash=`{dataset.get('manifest_config_hash')}`）。",
        f"- 生效窗口：{cfg['backtest']['start_date']} → {cfg['backtest']['end_date']}"
        f"（`analysis.json` 记录 {record['window'].get('effective_start_date')} → "
        f"{record['window'].get('effective_end_date')}）。",
        f"- 执行与成本契约：`execution_delay_bars`={cfg['backtest']['execution_delay_bars']}、"
        f"`intrabar_fill_order`={cfg['backtest']['intrabar_fill_order']}、maker 费率 "
        f"{cfg['backtest']['maker_fee_override']}、taker 费率 {cfg['backtest']['taker_fee_override']}、"
        f"`market_orders_allowed`={'True' if cfg.get('live', {}).get('market_orders_allowed') else 'False'}。",
        f"- 敞口结构：`n_positions`={facts['risk']['n_positions']}、"
        f"`total_wallet_exposure_limit`={facts['risk']['total_wallet_exposure_limit']}、"
        f"`we_excess_allowance_pct`={facts['risk']['we_excess_allowance_pct']} ⇒ 单槽敞口上界 "
        f"{facts['ledger']['single_slot_exposure_cap']:.6f}。",
        f"- 资金与强平契约：起始资金 `backtest.starting_balance`="
        f"{facts['ledger']['starting_balance']:,.0f} USDT（发布 profile 为 "
        f"{study.PARENT_STARTING_BALANCE:,}）、`backtest.liquidation_threshold`="
        f"{facts['ledger']['liquidation_threshold']} ⇒ 引擎强平地板 = "
        f"{facts['ledger']['liquidation_floor_usd']:,.0f} USDT；触发时引擎在该根 K 线结束回测。",
        f"- HSL：`enabled`={facts['ledger']['hsl_block'].get('enabled')}、作用域 "
        f"`live.hsl_signal_mode`=`{facts['ledger']['hsl_signal_mode']}`、"
        f"`red_threshold`={facts['ledger']['hsl_block'].get('red_threshold')}、"
        f"`ema_span_minutes`={facts['ledger']['hsl_block'].get('ema_span_minutes')}、"
        f"`restart_after_red_policy`={facts['ledger']['hsl_block'].get('restart_after_red_policy')}、"
        f"`panic_close_order_type`={facts['ledger']['hsl_block'].get('panic_close_order_type')}。",
        f"- 入场闸门：20/50 日均线闸门保持开启（`block_initial`/`block_reentry` 均为 True），"
        "逐币独立判定；本 arm 未改动闸门。",
        f"- 采样：余额/权益序列按 `backtest.balance_sample_divider`="
        f"{cfg['backtest']['balance_sample_divider']} 分钟采样，事件表与冲击矩阵的回撤口径与该分辨率一致；"
        f"`analysis.json` 的回撤按引擎逐分钟口径计算，两者可能略有差异。",
        f"- 成交时间戳是所属 1 分钟 K 线的开盘标签，不是交易所确认的成交时刻；"
        f"成交方向由 `type` 中的 `long`/`short` 判定。本次 taker 成交 "
        f"{facts['ledger']['taker_fills_count']:,} 笔，maker 成交 {facts['ledger']['maker_fills_count']:,} 笔。",
    ]
    if disabled:
        lines.append(
            f"- 本 arm 关闭了图像组 `{', '.join(disabled)}`：逐币成交面板是本机内存峰值（约 2.5 GB 增量），"
            "关闭后仍保留全部摘要图、全部数据产物与执行审计。"
        )
    lines.append(
        "- 离线边界：本 arm 由 `src/backtest.py` 在本地冻结 bundle 上重放，无网络、无凭据、"
        "无交易所账户、未启动任何实盘进程；运行日志里没有取数标记。"
    )
    lines.append(
        f"- 参考锚点：`{variant_input['reference_arms'][0]['key']}`"
        f"（{variant_input['reference_arms'][0]['role']}）与 "
        f"`{variant_input['reference_arms'][1]['key']}`（{variant_input['reference_arms'][1]['role']}）"
        "均为已完成 run 的冻结证据，本 arm 不与引擎漂移混淆。"
    )
    return lines


def guard_section(ledger: dict[str, Any], analysis: dict[str, Any]) -> list[str]:
    """The account-guard configuration and its engine telemetry."""
    params = ledger.get("guard") or {}
    if not params.get("enabled"):
        return [
            "### 账户守护读数",
            "",
            "- 本 arm 未启用账户级守护（HSL 关闭）：没有任何账户级熔断、也没有“停止加仓”的橙色档，"
            "是用于对照的无守护臂。",
            "",
        ]
    rows = [
        ["作用域 `live.hsl_signal_mode`", str(params.get("scope"))],
        ["清仓+停机阈值（RED）", f"{params.get('red_threshold'):.2f}"],
        ["停加仓阈值（ORANGE）", f"{params.get('orange_threshold'):.4f}"],
        ["停机时长 `cooldown_minutes_after_red`", f"{params.get('cooldown_minutes_after_red'):.0f} 分钟"],
        ["峰值窗口 `live.pnls_max_lookback_days`", f"{params.get('lookback_days'):.1f} 天"],
        ["EMA 跨度 `ema_span_minutes`", f"{params.get('ema_span_minutes'):.0f} 分钟"],
        ["永久停机阈值 `no_restart_drawdown_threshold`", f"{params.get('no_restart_drawdown_threshold'):.2f}"],
        ["重启策略 `restart_after_red_policy`", str(params.get("restart_after_red_policy"))],
        ["panic 下单类型", str(params.get("panic_close_order_type"))],
        ["触发次数 `hard_stop_triggers`", str(int(analysis.get("hard_stop_triggers") or 0))],
        ["重启次数 `hard_stop_restarts`", str(int(analysis.get("hard_stop_restarts") or 0))],
        ["处于 YELLOW / ORANGE / RED 的采样占比", _time_in_tiers(analysis)],
        ["停机时长（均值 / 最长，分钟）", _halt_minutes(analysis)],
        ["panic 平仓实亏合计（USDT）", fmt_metric("hard_stop_panic_close_loss_sum", analysis.get("hard_stop_panic_close_loss_sum"))],
        ["停机→重启期间的权益损失", fmt_metric("hard_stop_halt_to_restart_equity_loss_pct", analysis.get("hard_stop_halt_to_restart_equity_loss_pct"))],
        ["重启后再次触发 RED 的比例", fmt_metric("hard_stop_post_restart_retrigger_pct", analysis.get("hard_stop_post_restart_retrigger_pct"))],
    ]
    return [
        "### 账户守护读数",
        "",
        spec.md_table(rows, ["项目", "数值"]),
        "",
        "- 引擎语义：YELLOW 仅遥测；**ORANGE = 整个作用域进入 TpOnly（不产生任何新入场、含加仓，"
        "只走止盈路径）**；**RED = 先 Panic 平掉整个作用域，再停机**。因此“熔断”= 实现亏损 + 离场。",
        "- 触发指标是 `min(raw, EMA)`；`no_restart_drawdown_threshold` 用 `max(raw, EMA)` 判定，"
        "可被 RAW 尖峰锁存，是更硬的一层。锁存判定发生在**平仓确认那一刻**，而不是触发那一刻，"
        "所以一次急跌里“先到 20% 触发、确认时已跌到 40% 以上”会把带终局阈值的臂直接锁死。",
        "",
    ]


def readiness_section(guard_payload: dict[str, Any]) -> list[str]:
    """Per-halt table: how long the account was out and how it behaved after resuming."""
    halts = guard_payload.get("halts") or []
    lines = [
        "### 整装待发读数（停机窗口与复牌表现）",
        "",
        f"- 推导口径：{guard_payload.get('method', 'n/a')}",
        f"- 停机次数 {guard_payload.get('halt_count', 0)}"
        + (
            f"（其中终局停机 {guard_payload['terminal_halt_count']} 次）"
            if guard_payload.get("terminal_halt_count")
            else ""
        )
        + f"，合计 {guard_payload.get('total_halt_hours', 0.0):.1f} 小时"
        + (
            f"（占窗口 {guard_payload['halt_share_of_window_pct'] * 100:.2f}%）"
            if guard_payload.get("halt_share_of_window_pct") is not None
            else ""
        )
        + "。",
    ]
    if guard_payload.get("sampling_minutes"):
        lines.append(
            f"- 权益采样间隔 {guard_payload['sampling_minutes']:.0f} 分钟：窗口长度与引擎停机时长的"
            "比较容差取两个采样格（120 分钟），因此“窗口不短于声明停机时长”是硬校验，超出部分"
            "单独记为停机结束后等待入场信号的空仓时间。"
        )
    if guard_payload.get("idle_after_halt_minutes_mean") is not None:
        ratio = guard_payload.get("out_of_market_vs_declared_ratio")
        ratio_text = (
            f"完整停机臂的离场总时长是声明停机时长的 {ratio:.2f} 倍"
            "（1.00 = 恰好按声明复牌；略低于 1 是一个采样格的滞后，明显高于 1 的部分是停机结束后"
            "等不到入场信号的空仓时间）。"
            if ratio
            else "离场总时长与声明停机时长一致。"
        )
        lines.append(
            f"- 停机结束后仍空仓的时间：均值 {guard_payload['idle_after_halt_minutes_mean']:.0f} 分钟、"
            f"最长 {guard_payload['idle_after_halt_minutes_max']:.0f} 分钟；{ratio_text}"
        )
    if guard_payload.get("post_halt_return_7d_mean") is not None:
        lines.append(
            f"- 复牌后 7 天平均收益 {guard_payload['post_halt_return_7d_mean'] * 100:+.2f}%、"
            f"30 天平均收益 {guard_payload['post_halt_return_30d_mean'] * 100:+.2f}%"
            "（正 = 停机后市场继续给机会，负 = 停机期间错过/复牌即遇第二波）。"
        )
    if guard_payload.get("immediate_retriggers_within_7d"):
        lines.append(
            f"- 7 天内再次停机的次数：{guard_payload['immediate_retriggers_within_7d']}"
            "（这是“整装待发”失败的模式：复牌后立刻又被击穿）。"
        )
    if not halts:
        lines.append("- 本 arm 没有推导出停机窗口（未触发守护，或从未在亏损后离场）。")
    else:
        header = [
            "开始",
            "结束",
            "时长(小时)",
            "声明停机(小时)",
            "超出声明(小时)",
            "停机时权益 USDT",
            "停机起点回撤",
            "复牌后 7 天",
            "复牌后 30 天",
        ]
        rows = [
            [
                str(halt["start"])[:16],
                str(halt["end"])[:16],
                f"{halt['hours']:.1f}",
                "n/a"
                if halt.get("declared_minutes") is None
                else f"{halt['declared_minutes'] / 60.0:.1f}",
                "n/a"
                if halt.get("excess_minutes") is None
                else f"{halt['excess_minutes'] / 60.0:.1f}",
                f"{halt['equity_usd']:,.0f}",
                "n/a"
                if halt.get("drawdown_at_start") is None
                else f"{halt['drawdown_at_start'] * 100:.2f}%",
                "n/a"
                if halt.get("post_halt_return_7d") is None
                else f"{halt['post_halt_return_7d'] * 100:+.2f}%",
                "n/a"
                if halt.get("post_halt_return_30d") is None
                else f"{halt['post_halt_return_30d'] * 100:+.2f}%",
            ]
            for halt in halts
        ]
        lines.extend(["", spec.md_table(rows, header)])
        if any(halt.get("terminal") for halt in halts):
            lines.append(
                "- **终局停机**（上表复牌列为 `n/a` 的那一行）：守护被锁存，账户清仓后再未交易，"
                "窗口一直延伸到回测结束——这一行衡量的是“这一轮彻底退出”，而不是“休息后重来”。"
            )
        lines.append(
            "- 表中的“停机起点回撤”是**账户权益相对本 arm 全期运行峰值**的回撤（与本报告其它回撤读数"
            "同口径）；引擎的触发口径是“7 天滚动策略权益峰值”的回撤（`hard_stop_trigger_drawdown_mean`）。"
            "急跌里 20% 触发线被击穿、但 panic 平仓确认时全期回撤更深，两个口径不可混读。"
        )
    notes = guard_payload.get("cross_check_notes") or []
    if notes:
        lines.extend([""] + [f"- {note}" for note in notes])
    problems = guard_payload.get("cross_check_problems") or []
    if problems:
        lines.extend(["", "**推导与引擎遥测不一致（需人工复核）**："] + [f"- {p}" for p in problems])
    lines.append("")
    return lines


def _time_in_tiers(analysis: dict[str, Any]) -> str:
    return " / ".join(
        fmt_metric(key, analysis.get(key))
        for key in (
            "hard_stop_time_in_yellow_pct",
            "hard_stop_time_in_orange_pct",
            "hard_stop_time_in_red_pct",
        )
    )


def _halt_minutes(analysis: dict[str, Any]) -> str:
    mean = analysis.get("hard_stop_duration_minutes_mean")
    worst = analysis.get("hard_stop_duration_minutes_max")
    if mean is None and worst is None:
        return "n/a"
    return f"{0.0 if mean is None else float(mean):.0f} / {0.0 if worst is None else float(worst):.0f}"


def tail_panel_section(analysis: dict[str, Any]) -> list[str]:
    rows = [[METRIC_LABELS.get(key, key), fmt_metric(key, analysis.get(key))] for key in TAIL_PANEL_METRICS]
    rows.append(["是否被强平 `liquidated`", str(analysis.get("liquidated"))])
    return ["### 尾部面板", "", spec.md_table(rows, ["指标", "数值"]), ""]


def capital_section(ledger: dict[str, Any], analysis: dict[str, Any]) -> list[str]:
    """The capital / leverage / liquidation geometry of this arm, in one place."""
    starting = ledger.get("starting_balance") or 0.0
    twe = ledger.get("declared_twe") or 0.0
    slot = ledger.get("single_slot_exposure_cap") or 0.0
    threshold = ledger.get("liquidation_threshold") or 0.0
    floor = ledger.get("liquidation_floor_usd") or 0.0
    peak = float(analysis.get("total_wallet_exposure_max") or 0.0)
    peak_balance = ledger.get("peak_exposure_balance_usd")
    shock = ledger.get("liquidation_shock_at_peak")
    # A real 10x cross-margin venue liquidates near equity <= maintenance * notional; at
    # notional = exposure * balance that is r = maintenance - 1/exposure. Report it in the
    # same frame as the engine line (the realised peak) and, for reference, at full TWE.
    maintenance = 0.004
    exchange_shock_realized = (
        (maintenance - 1.0 / peak) if peak > 0 else None
    )
    exchange_shock_full = (maintenance - 1.0 / twe) if twe > 0 else None
    rows = [
        ["起始资金 USDT", f"{starting:,.0f}"],
        ["总暴露上限 TWE", f"{twe:.2f}"],
        ["单槽敞口上界（含 0.37 超额允许）", f"{slot:.6f}"],
        ["实测总暴露峰值", f"{peak:.6f}"],
        ["峰值时刻的余额 USDT", "n/a" if peak_balance is None else f"{peak_balance:,.2f}"],
        ["引擎强平阈值 `liquidation_threshold`", f"{threshold}"],
        ["引擎强平地板 USDT", f"{floor:,.0f}"],
        [
            "在实测峰值暴露处的强平触发跌幅",
            "n/a"
            if shock is None
            else (
                ">100%（价格归零也不足以触及地板）"
                if shock >= 1.0
                else f"−{shock * 100:.2f}%"
            ),
        ],
        [
            "同样暴露下 10× 全仓保证金的交易所强平跌幅（维护保证金 0.4%）",
            "n/a"
            if exchange_shock_realized is None
            else f"−{abs(exchange_shock_realized) * 100:.2f}%",
        ],
        [
            "参考：若打到满仓（TWE 上限）时的交易所强平跌幅",
            "n/a" if exchange_shock_full is None else f"−{abs(exchange_shock_full) * 100:.2f}%",
        ],
        ["是否被强平 `liquidated`", str(analysis.get("liquidated"))],
    ]
    return [
        "### 资本、杠杆与强平几何",
        "",
        spec.md_table(rows, ["项目", "数值"]),
        "",
        "- 引擎在 `权益 ≤ 起始资金 × liquidation_threshold` 时置 `liquidated=true`、把末值钉在"
        "地板上并**在该根 K 线结束回测**；因此强平 arm 的报告只覆盖到强平之前，其后数值无意义。",
        "- 引擎不模拟保证金占用与分层维持保证金：表中的“交易所强平跌幅”只是以 10× 全仓、"
        "维护保证金 0.4% 为例的对照，真实分层保证金与强平费用未建模。",
        "",
    ]


def liquidation_section(
    ledger: dict[str, Any], analysis: dict[str, Any], record: dict[str, Any]
) -> list[str]:
    """Whether the run survived, and if not, when and at what loss."""
    floor = ledger.get("liquidation_floor_usd") or 0.0
    starting = ledger.get("starting_balance") or 0.0
    liquidated = bool(analysis.get("liquidated"))
    window = record.get("window") or {}
    lines = ["### 强平读数", ""]
    if liquidated:
        lines.append(
            f"- **本 arm 被强平**：引擎在权益跌到 {floor:,.0f} USDT（起始资金的 "
            f"{ledger.get('liquidation_threshold')}）时结束回测，等价于本金的 "
            f"−{(1 - floor / starting) * 100:.1f}%。"
        )
        lines.append(
            f"- 生效区间 {window.get('effective_start_date')} → {window.get('effective_end_date')}"
            f"（声明窗口到 {record.get('window', {}).get('end_date')}）；"
            "强平之后的行情与该账户无关。"
        )
        lines.append(
            "- 强平前的最差回撤与收益指标只描述“存活期间”，不能与未强平 arm 的全期指标直接比较。"
        )
    else:
        lines.append(
            f"- 本 arm 未触发强平：全程权益高于地板 {floor:,.0f} USDT（起始资金的 "
            f"{ledger.get('liquidation_threshold')}）。"
        )
        shock = ledger.get("liquidation_shock_at_peak")
        if shock is not None:
            if shock >= 1.0:
                lines.append(
                    "- 在实测峰值暴露与当时的余额下，**即使持仓价格全部归零也不足以触及地板**"
                    "（余额相对敞口足够厚，只能被持续亏损而非单次行情归零打到地板）。"
                )
            else:
                lines.append(
                    f"- 在实测峰值暴露与当时的余额下，行情再下跌 **{shock * 100:.2f}%** 才会触及"
                    "该地板（未考虑路径中的加仓与减仓）。"
                )
    lines.append("")
    return lines


def wipeout_section(wipeout_payload: dict[str, Any]) -> list[str]:
    ruin = wipeout_payload["ruin"]
    per_coin = wipeout_payload["per_coin_max_exposure"][:5]
    risk = wipeout_payload.get("config_risk") or {}
    try:
        slot_cap = (
            float(risk["total_wallet_exposure_limit"])
            / float(risk["n_positions"])
            * (1.0 + float(risk["we_excess_allowance_pct"]))
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        slot_cap = None
    declared_twe = ruin.get("declared_twe")
    peak = ruin["peak_total_exposure"]
    lines = [
        "### 暴露上界与爆仓距离（单币/多币归零）",
        "",
        f"- 声明总暴露上限 `total_wallet_exposure_limit` = "
        f"{'n/a' if declared_twe is None else f'{declared_twe:.2f}'}；实测峰值 = {peak:.6f}"
        + (
            f"（上限的 {peak / declared_twe * 100:.1f}%）"
            if declared_twe
            else ""
        )
        + "。",
        f"- 爆仓距离（整篮在该峰值处归零后剩余权益比例）= **{ruin['ruin_distance'] * 100:.2f}%**"
        + (
            "（TWE>1 时该值恒为 0：净值先被强平地板截断，而不是被“归零”截断）"
            if declared_twe is not None and declared_twe > 1.0
            else ""
        )
        + "。",
        f"- 本 arm 实测最大单币敞口 = {ruin['worst_coin_exposure']:.6f}（{ruin['worst_coin']}），"
        f"理论单槽上界 `TWEL/n_positions×(1+allowance)` = "
        f"{slot_cap:.6f}。" if slot_cap is not None else "",
        f"- 同时归零的临界币数：达到 −20% 账户损失需 "
        f"{_fmt_count(ruin['coins_to_breach_20pct'])} 个币同时归零，达到 −50% 需 "
        f"{_fmt_count(ruin['coins_to_breach_50pct'])} 个，达到 −80% 需 "
        f"{_fmt_count(ruin['coins_to_breach_80pct'])} 个（按各币实测敞口峰值之和计算，属上界）。",
        f"- 在实测峰值暴露处的强平触发跌幅 = "
        + (
            (
                "**>100%**（价格归零也不足以触及地板）"
                if ruin["liquidation_shock_at_peak"] >= 1.0
                else f"**−{ruin['liquidation_shock_at_peak'] * 100:.2f}%**"
            )
            + f"（引擎地板 "
            f"{ruin['starting_balance_usd'] * (ruin['liquidation_threshold'] or 0):,.0f} USDT）"
            if ruin.get("liquidation_shock_at_peak")
            else "n/a"
        )
        + "。",
        "",
        "前 5 名单币敞口峰值：",
        "",
        spec.md_table(
            [[item["coin"], f"{item['max_abs_wallet_exposure']:.6f}"] for item in per_coin],
            ["币种", "单币敞口峰值"],
        ),
        "",
        "冲击损失矩阵（损失以“峰值暴露时刻的余额”为基数；全为**上界**，不含路径中的加仓与减仓）：",
        "",
    ]
    header = ["冲击范围"] + [f"价格损失 {int(shock * 100)}%" for shock in wipeout_payload["shock_levels"]]
    rows: list[list[str]] = []
    for scope in wipeout.SCOPE_ORDER:
        cells = [wipeout.SCOPE_LABELS[scope]]
        for shock in wipeout_payload["shock_levels"]:
            entry = next(
                item
                for item in wipeout_payload["matrix"]
                if item["scope"] == scope and item["shock"] == shock
            )
            cells.append(
                f"{entry['loss_fraction'] * 100:.1f}%"
                + (
                    f"（{entry['loss_usd']:,.0f} USDT）"
                    if entry.get("loss_usd") is not None
                    else ""
                )
            )
        rows.append(cells)
    lines.extend([spec.md_table(rows, header), ""])
    return [line for line in lines if line is not None]


def _fmt_count(value: Any) -> str:
    return "n/a" if value is None else f"{int(value)}"


def event_section(events_payload: dict[str, Any]) -> list[str]:
    items = sorted(
        events_payload["events"],
        key=lambda item: float(item["benchmark_drop_pct"]),
        reverse=True,
    )
    rows: list[list[str]] = []
    for item in items:
        drawdown = item.get("max_drawdown")
        exposure = item.get("twe_peak")
        label = item["label"]
        if label == "未标注事件":
            label = f"未标注事件（{item['event']} 起）"
        rows.append(
            [
                label,
                f"{item['breach_start']} → {item['breach_end']}",
                f"−{item['benchmark_drop_pct'] * 100:.1f}%",
                f"{drawdown * 100:.2f}%" if drawdown is not None else "n/a",
                f"{item['days_to_trough']:.1f}" if item.get("days_to_trough") is not None else "n/a",
                f"{item['days_to_recovery']:.1f}"
                if item.get("days_to_recovery") is not None
                else "未恢复",
                f"{item['underwater_days']:.0f}" if item.get("underwater_days") is not None else "n/a",
                f"{(item.get('window_return') or 0.0) * 100:+.2f}%"
                if item.get("window_return") is not None
                else "n/a",
                "无成交（窗口内无暴露）"
                if exposure is None
                else f"{exposure:.3f}" + ("（窗口内无成交）" if not item.get("fills") else ""),
                f"{int(item.get('panic_fills', 0))}",
            ]
        )
    header = [
        "事件",
        "基准击穿窗口",
        "基准跌幅",
        "本 arm 事件内最大回撤",
        "到谷天数",
        "到恢复天数",
        "水下天数",
        "窗口收益",
        "暴露峰值（成交样本）",
        "panic 笔数",
    ]
    no_exposure = sum(1 for item in items if item.get("twe_peak") is None)
    return [
        "### 事件窗口压力表（基准驱动，非手工挑窗）",
        "",
        f"- 事件识别规则：BTC {events_payload['rule']['lookback_days']} 日对数收益 ≤ "
        f"{events_payload['rule']['threshold']}，相邻间隔 < {events_payload['rule']['min_gap_days']} 天合并；"
        "共识别 " + str(events_payload["benchmark"]["count"]) + " 个事件，按基准跌幅降序排列。",
        "- 每个事件的测量窗口从基准事件前高点开始，到本 arm 策略权益恢复该高点为止（最长 180 天）。",
        f"- 其中 {no_exposure} 个事件在本 arm 的成交账本里没有任何成交（当时闸门关闭或该币池尚未上市），"
        "因此事件内回撤为 0 —— 这不是“抗跌”，而是“当时不在场”，两者必须在解读时区分。",
        "",
        spec.md_table(rows, header),
        "",
    ]


def hsl_section(analysis: dict[str, Any], ledger: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    hsl = ledger["hsl_block"]
    risk = cfg["bot"]["long"]["risk"]
    lookback = (cfg.get("live") or {}).get("pnls_max_lookback_days")
    slot_budget = (
        float(risk["total_wallet_exposure_limit"]) / float(risk["n_positions"])
        if float(risk["n_positions"])
        else None
    )
    semantics = [
        "### HSL 运行学与熔断代价",
        "",
        f"- 触发指标是 `min(raw, EMA)`：raw = 1 − 策略净值/滚动峰值，峰值窗口为 "
        f"`live.pnls_max_lookback_days` = {lookback} 天（即回撤以近 {lookback} 天峰值为基准，不是历史最高点）；"
        f"EMA 平滑窗口 `ema_span_minutes` = {hsl.get('ema_span_minutes')} 分钟。",
        f"- 分母口径随作用域变化：`coin` 模式的分母是**槽位预算**（余额 × TWEL/n_positions = "
        f"{slot_budget:.6f} × 余额），而 `pside`/`unified` 的分母是**策略净值本身**。同一个 "
        f"`red_threshold` 在两种口径下代表完全不同的损失水平。",
    ]
    if not hsl.get("enabled"):
        return semantics + [
            "- 本 arm 未启用 HSL：`hard_stop_*` 遥测全为 0，账户没有任何账户级熔断层，"
            "尾部完全由敞口上界与闸门承担。",
            "",
        ]
    rows = [
        [METRIC_LABELS.get(key, key), fmt_metric(key, analysis.get(key))]
        for key in study.HSL_METRICS
    ]
    lines = semantics + [
        f"- 生效作用域 `live.hsl_signal_mode` = `{ledger['hsl_signal_mode']}`，"
        f"`red_threshold` = {hsl.get('red_threshold')}，`restart_after_red_policy` = "
        f"{hsl.get('restart_after_red_policy')}（`no_restart_drawdown_threshold` = "
        f"{hsl.get('no_restart_drawdown_threshold')}），`cooldown_minutes_after_red` = "
        f"{hsl.get('cooldown_minutes_after_red')}，`panic_close_order_type` = "
        f"{hsl.get('panic_close_order_type')}。",
        f"- 账本侧 panic 成交 {ledger['panic_fill_count']:,} 笔，实亏 "
        f"{ledger['panic_loss_usd']:+,.2f} USDT（手续费 {ledger['panic_fees_usd']:+,.2f}），"
        f"涉及 {len(ledger['panic_coins'])} 个币：{', '.join(ledger['panic_coins'][:12]) or '无'}。",
        "- 该机制对**有持续性的**下跌有效；对一天内完成并反弹的插针/跳空，raw 尖峰会被 EMA 平滑掉，"
        "因此不能用它当作闪崩保险。",
        "",
        spec.md_table(rows, ["指标", "数值"]),
        "",
    ]
    return lines


def boundaries_section(variant: study.Variant, variant_input: dict[str, Any]) -> list[str]:
    lines = ["### 边界与诚实声明", ""]
    for item in variant_input["honesty_boundaries"]:
        lines.append(f"- {item}")
    if variant.leg == "ext":
        lines.extend(
            [
                "- 长腿自 2021-04-20 起交易，当时只有 22 个币有数据（HBAR 自 2021-05-15 起，"
                "其余币更晚），币池是“活到 2026 年的当前 top40”，因此存在幸存者偏差；"
                "2021-05 事件落在闸门预热/首次转正的边界上，只能作旁证。",
                "- 该腿是同一配置在更早市场、更窄币池上的双重样本外检验，不是发布窗口的证据。",
            ]
        )
    if variant.synthetic:
        lines.append(
            "- 合成臂的价格路径是注入的：结论只能读作“若发生同类崩塌，机制会如何反应”，"
            "不能读作“历史上发生过”。"
        )
    lines.append(
        "- 本报告不是预测；所有数字都来自 `analysis.json`、`fills.csv`、`balance_and_equity.csv.gz` "
        "与其派生文件。"
    )
    lines.append("")
    return lines


def live_reading_section(
    variant: study.Variant, analysis: dict[str, Any], wipeout_payload: dict[str, Any], ledger: dict[str, Any]
) -> list[str]:
    ruin = wipeout_payload["ruin"]
    lines = ["### 对实盘决策的读数", ""]
    lines.append(
        f"- 该 arm 的历史最差回撤 {fmt_metric('drawdown_worst_strategy_eq', analysis.get('drawdown_worst_strategy_eq'))}、"
        f"最差 1% 均值回撤 {fmt_metric('drawdown_worst_mean_1pct_strategy_eq', analysis.get('drawdown_worst_mean_1pct_strategy_eq'))}、"
        f"收益 {fmt_metric('gain_strategy_eq', analysis.get('gain_strategy_eq'))}。"
    )
    lines.append(
        f"- 敞口上界给出**结构性**保证：单币归零 ≤ {ruin['worst_coin_exposure'] * 100:.2f}% 账户"
        f"（理论单槽上界 {ledger['single_slot_exposure_cap'] * 100:.2f}%），实测总暴露峰值 "
        f"{ruin['peak_total_exposure']:.4f} × 总暴露上限 {ledger['declared_twe']:.2f}。"
    )
    if analysis.get("liquidated"):
        lines.append(
            f"- **本 arm 已被强平**：权益跌到 {ledger['liquidation_floor_usd']:,.0f} USDT 后引擎"
            "结束回测。这条证据的含义是：该杠杆/资金组合在本地历史内**不能**靠自身机制活下来，"
            "任何硬底线必须先解决“在强平之前介入”的问题。"
        )
    else:
        shock = ruin.get("liquidation_shock_at_peak")
        lines.append(
            "- 未触发强平；在实测峰值暴露处仍需行情下跌 "
            + (f"**{shock * 100:.2f}%** " if shock else "n/a ")
            + f"才会触及引擎地板 {ledger['liquidation_floor_usd']:,.0f} USDT。"
        )
    if ledger["hsl_block"].get("enabled"):
        lines.append(
            f"- 启用的是 `{ledger['hsl_signal_mode']}` 作用域："
            + (
                "只有单币级熔断，无法阻止组合级同时崩塌。"
                if ledger["hsl_signal_mode"] == "coin"
                else "组合/账户级熔断会在策略净值回撤达到阈值时清空该作用域并进入冷却，"
                "它对有持续性的崩盘有效，对一天内完成并反弹的闪崩无效。"
            )
            + f"本 arm 的实亏代价 {ledger['panic_loss_usd']:+,.2f} USDT。"
        )
    else:
        lines.append(
            "- 未启用任何账户级熔断：TWE 3.0 下这是“无兜底”的对照组，用于量化风险本身。"
        )
    lines.append("")
    return lines


def artifacts_table(
    result_dir: Path, audit: dict[str, Any], equity: pd.DataFrame, analysis: dict[str, Any]
) -> list[tuple[str, str]]:
    rows = [
        ("annual_analysis.md", "本报告（固定章节骨架 + 研究附录）"),
        ("annual_metrics.csv", "自然年指标表（本地，可由本工具重建）"),
        ("monthly_metrics.csv", "月度指标表（本地，可由本工具重建）"),
        ("coin_metrics.csv", "逐币指标表（本地，可由本工具重建）"),
        ("analysis.json", "引擎原生指标（tracked）"),
        ("fills.csv", "成交账本（本地）"),
        ("balance_and_equity.csv.gz", f"余额/权益采样序列（{len(equity):,} 行，本地）"),
        ("config.json", "本次 run 的生效配置（本地）"),
        ("dataset.json", "数据集身份（本地）"),
        (EVENTS_ARTIFACT, "事件窗口表（本 arm，tracked）"),
        (WIPEOUT_ARTIFACT, "暴露上界与冲击损失矩阵（本 arm，tracked）"),
        (GUARD_ARTIFACT, "账户守护停机窗口与复牌读数（本 arm，tracked）"),
        ("run_record.json", "本 arm 的声明改动与数据集身份（tracked）"),
        ("global_metrics.json", "引擎/环境/数据集哈希（tracked）"),
    ]
    if audit["rows"]:
        rows.append(("execution_audit.csv", f"逐笔执行审计（{audit['rows']:,} 行，本地）"))
    rows.append(
        (
            "fills_plots/",
            "逐币成交面板（本地；长腿为控制内存已关闭该图组）"
            if (result_dir / "fills_plots").is_dir()
            else "逐币成交面板（本 arm 已关闭该图组以控制内存）",
        )
    )
    return rows


def verifiable_notes(
    result_dir: Path,
    variant: study.Variant,
    record: dict[str, Any],
    global_metrics: dict[str, Any],
    ledger: dict[str, Any],
    variant_input: dict[str, Any],
    facts: dict[str, Any],
    audit: dict[str, Any],
) -> list[str]:
    dataset = global_metrics["dataset"]
    notes = [
        f"- 冻结父配置：`{record['source_config']}`（sha256 `{record['source_config_sha256']}`）；"
        f"本 arm 配置 `{record['candidate_config_path']}`（sha256 "
        f"`{record['candidate_config_sha256']}`）。",
        f"- 数据集身份：`{dataset['cache_dir']}`，manifest config_hash "
        f"`{dataset['manifest_config_hash']}`，`hlcvs` 逻辑哈希 "
        f"`{dataset['manifest_hashes'].get('hlcvs')}`（本次未重算整块 4 GB 数组，"
        f"full_array_hash={dataset.get('full_array_hash')}）。",
        f"- 引擎指纹：`{record['engine'].get('expected_source_fingerprint')}`；"
        f"编译戳一致={record['engine'].get('source_fingerprint_matches_compiled_stamp')}；"
        f"加载戳一致={record['engine'].get('source_fingerprint_matches_loaded_stamp')}。",
        f"- 执行审计：`{audit['rows']:,}` 行，与 `analysis.json.fills_count`="
        f"{int(facts['analysis_fills_count']):,} 一致。",
        f"- 参考锚点："
        + "；".join(
            f"`{item['key']}`= {item['run_dir']}（analysis sha256 `{item['analysis_sha256']}`）"
            for item in variant_input["reference_arms"]
        )
        + "。",
        f"- 研究级综合结论：`{study.relative(SYNTHESIS_PATH)}`（跨 arm 对比、事件表、"
        "Pareto 取舍与判据裁决）。",
        f"- 独立复算：`report_tools/verify_variant_report.py --variant {variant.key}`"
        "（该脚本不 import 本渲染器，重算事件表、冲击矩阵、守护遥测与停机窗口）。",
        f"- 账户守护参数：{ledger['guard_label']}；峰值窗口 "
        f"`live.pnls_max_lookback_days`={ledger.get('pnls_max_lookback_days')}。",
    ]
    if global_metrics.get("disabled_plot_groups"):
        notes.append(
            f"- 关闭的图像组：{', '.join(global_metrics['disabled_plot_groups'])}"
            "（口径与范围已声明）。"
        )
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=study.DEFAULT_VARIANT_ORDER)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument("--report-title", default=None)
    parser.add_argument(
        "--skip-tail-artifacts",
        action="store_true",
        help="reuse the existing tail_risk_*.json instead of recomputing them",
    )
    args = parser.parse_args(argv)

    variant = arm_of(args.variant)
    if variant.key in study.REUSED_ARM_KEYS:
        fail(
            [f"{variant.key} is a pinned reference arm without a bundle of its own"],
            "reference arms are reported through the synthesis, not rendered here",
        )
    result_dir = study.find_variant_run_dir(variant, args.result_dir)
    require_artifacts(result_dir, variant)
    variant_input = study.load_json(study.VARIANT_INPUT_PATH)

    if args.skip_tail_artifacts:
        event_rows, events_payload = events_artifact(result_dir)
        wipeout_payload = wipeout_artifact(result_dir)
        guard_payload = guard_artifact(result_dir)
    else:
        (
            event_rows,
            events_payload,
            wipeout_payload,
            guard_payload,
            _risk,
        ) = build_tail_artifacts(result_dir, variant)

    cfg = study.load_json(result_dir / "config.json")
    record = study.load_json(result_dir / "run_record.json")
    global_metrics = study.load_json(result_dir / "global_metrics.json")
    analysis = study.load_json(result_dir / "analysis.json")
    fills = spec.load_fills(result_dir)
    equity = spec.load_balance_equity(result_dir)

    annual = spec.build_period_table(equity, fills, "Y")
    monthly = spec.build_period_table(equity, fills, "M")
    coins = spec.build_coin_table(fills)
    attribution = spec.build_attribution_table(fills)
    ledger = ledger_facts(fills, equity, cfg, variant)
    audit = audit_facts(
        variant.execution_audit_path, int(cfg["backtest"]["execution_delay_bars"])
    )
    facts = {
        "risk": variant_risk_block(result_dir),
        "ledger": ledger,
        "analysis_fills_count": int(analysis.get("fills_count") or 0),
    }
    if facts["analysis_fills_count"] != audit["rows"]:
        fail(
            [
                f"execution audit rows {audit['rows']} != analysis fills_count "
                f"{facts['analysis_fills_count']}"
            ],
            f"{variant.key}: the execution audit does not describe this run",
        )

    annual.to_csv(result_dir / "annual_metrics.csv", index=False)
    monthly.to_csv(result_dir / "monthly_metrics.csv", index=False)
    coins.to_csv(result_dir / "coin_metrics.csv", index=False)

    try:
        result_label = str(result_dir.relative_to(study.REPO))
    except ValueError:
        result_label = str(result_dir)

    appendix_parts = [
        "### 声明的改动与身份证明",
        "",
        spec.md_table(
            [
                [f"`{item['path']}`", f"`{item['from']}`", f"`{item['to']}`"]
                for item in record["declared_delta"]
            ]
            or [["（无：控制组，只有输出位置不同）", "—", "—"]],
            ["配置路径", "从", "到"],
        ),
        "",
        *tail_panel_section(analysis),
        *capital_section(ledger, analysis),
        *liquidation_section(ledger, analysis, record),
        *wipeout_section(wipeout_payload),
        *event_section(events_payload),
        *guard_section(ledger, analysis),
        *hsl_section(analysis, ledger, cfg),
        *readiness_section(guard_payload),
        *boundaries_section(variant, variant_input),
        *live_reading_section(variant, analysis, wipeout_payload, ledger),
    ]
    appendix = "\n".join(line for line in appendix_parts if line is not None)

    context: dict[str, Any] = {
        "analysis": analysis,
        "config": cfg,
        "annual": annual,
        "monthly": monthly,
        "coins": coins,
        "attribution": attribution,
        "run_record": record,
        "ledger": ledger,
        "result_label": result_label,
        "scope_lines": scope_lines(
            result_dir, variant, record, global_metrics, cfg, facts, variant_input
        ),
        "appendix": appendix,
        "artifacts": artifacts_table(result_dir, audit, equity, analysis),
        "verifiable_notes": verifiable_notes(
            result_dir, variant, record, global_metrics, ledger, variant_input, facts, audit
        ),
        "interpretation_extra": [
            f"本 arm 的杠杆是 `{variant.lever}`（{study.LEVERS[variant.lever].label}）："
            f"{study.LEVERS[variant.lever].hypothesis}",
            f"资金口径：起始资金 {ledger['starting_balance']:,.0f} USDT、总暴露上限 "
            f"{ledger['declared_twe']:.2f}、单槽上界 {ledger['single_slot_exposure_cap']:.6f}；"
            f"引擎强平地板 {ledger['liquidation_floor_usd']:,.0f} USDT"
            + (
                "，本 arm 已触发强平。"
                if analysis.get("liquidated")
                else "，本 arm 未触发强平。"
            ),
            "尾部读数的口径是：事件表按基准驱动的事件窗口测量，冲击矩阵按实测敞口峰值给出上界，"
            "两者都不含对未来的推断。",
        ],
    }
    if args.report_title:
        context["title"] = args.report_title
    report = spec.render_annual_analysis(context)
    problems = spec.assert_report_structure(report)
    if problems:
        fail(problems, "report structure does not match the convention")
    (result_dir / "annual_analysis.md").write_text(report, encoding="utf-8")
    print(f"wrote {study.relative(result_dir / 'annual_analysis.md')}")
    print(f"wrote {study.relative(result_dir / 'annual_metrics.csv')} ({len(annual)} rows)")
    print(f"wrote {study.relative(result_dir / 'monthly_metrics.csv')} ({len(monthly)} rows)")
    print(f"wrote {study.relative(result_dir / 'coin_metrics.csv')} ({len(coins)} rows)")
    print(
        f"arm={variant.key} leg={variant.leg} samples={len(equity):,} fills={len(fills):,} "
        f"events={events_payload['benchmark']['count']} "
        f"twe_peak={wipeout_payload['ruin']['peak_total_exposure']:.6f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
