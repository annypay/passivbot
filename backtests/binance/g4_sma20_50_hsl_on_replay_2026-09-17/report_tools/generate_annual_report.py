#!/usr/bin/env python3
"""Render one g4 "HSL on" variant replay report and its three metric tables.

The report skeleton, the table schemas and the markdown renderer live in
`backtests/report_spec/annual_analysis.py`; the binding convention is
`docs/ai/runbooks/strategy_report.md`. This script supplies only:

* the artifact loaders and the facts derived from the replay's own ledger,
* the variant-specific scope lines,
* the comparison appendix: the declared single change, the three columns
  (tracked HSL-OFF evidence / paired control on the current engine / HSL on),
  the HSL runtime telemetry, what changed at the ledger level, the engine-drift
  control, and the boundaries.

Every number is read from a run directory or from an explicitly cited tracked artifact;
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

import variant_spec as study  # noqa: E402

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
    "close_grid_short",
    "close_trailing_short",
    "close_unstuck_short",
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
)

#: How each compared metric is formatted (`kind`, digits). Keys absent here fall back to a
#: plain number so a new metric cannot silently render as a percentage.
METRIC_FORMATS: dict[str, tuple[str, int]] = {
    "gain_strategy_eq": ("multiple", 6),
    "adg_strategy_eq": ("pct", 4),
    "mdg_strategy_eq": ("pct", 4),
    "drawdown_worst_strategy_eq": ("pct", 2),
    "drawdown_worst_mean_1pct_strategy_eq": ("pct", 2),
    "strategy_eq_recovery_days_max": ("days", 2),
    "peak_recovery_days_strategy_eq_long": ("days", 2),
    "sortino_ratio_strategy_eq": ("ratio", 4),
    "sharpe_ratio_strategy_eq": ("ratio", 4),
    "loss_profit_ratio": ("ratio", 4),
    "fills_count": ("count", 0),
    "fills_active_symbols_count": ("count", 0),
    "fills_count_entry": ("count", 0),
    "fills_count_close": ("count", 0),
    "fills_gap_longest_days": ("days", 2),
    "total_wallet_exposure_max": ("ratio", 4),
    "total_wallet_exposure_mean": ("ratio", 4),
    "position_held_days_max": ("days", 2),
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
    "peak_recovery_days_strategy_eq_long": "多头峰值恢复期（天）",
    "sortino_ratio_strategy_eq": "Sortino（策略权益）",
    "sharpe_ratio_strategy_eq": "Sharpe（策略权益）",
    "loss_profit_ratio": "亏损/盈利比",
    "fills_count": "成交笔数",
    "fills_active_symbols_count": "有成交的币数",
    "fills_count_entry": "入场成交",
    "fills_count_close": "减仓或平仓成交",
    "fills_gap_longest_days": "最长无成交间隔（天）",
    "total_wallet_exposure_max": "总钱包敞口最大",
    "total_wallet_exposure_mean": "总钱包敞口均值",
    "position_held_days_max": "最长持仓（天）",
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


def variant_of(key: str) -> study.Variant:
    try:
        return study.VARIANTS_BY_KEY[key]
    except KeyError:
        raise SystemExit(
            f"unknown variant {key!r}; expected one of {list(study.DEFAULT_VARIANT_ORDER)}"
        ) from None


def require_artifacts(result_dir: Path, variant: study.Variant) -> None:
    missing = [name for name in REQUIRED_ARTIFACTS if not (result_dir / name).exists()]
    if not variant.execution_audit_path.exists():
        missing.append(study.relative(variant.execution_audit_path))
    if missing:
        raise SystemExit(f"run directory {result_dir} is missing artifacts: {missing}")


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


def format_metric(key: str, value: Any) -> str:
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
    return f"{number:.{digits}f}"


def format_delta(key: str, left: Any, right: Any) -> str:
    """`right - left` in the metric's own unit (percentage points for `pct`)."""
    if left is None or right is None:
        return "n/a"
    try:
        a, b = float(left), float(right)
    except (TypeError, ValueError):
        return "n/a"
    if not (np.isfinite(a) and np.isfinite(b)):
        return "n/a"
    kind, digits = METRIC_FORMATS.get(key, ("number", 6))
    diff = b - a
    if kind == "count":
        return f"{int(round(diff)):+,}"
    if kind == "pct":
        return f"{diff * 100:+.{digits}f}pp"
    if kind == "usdt":
        return f"{diff:+,.{digits}f}"
    if kind == "multiple":
        return f"{diff:+.{digits}f}×"
    return f"{diff:+.{digits}f}"


def direction_from_type(order_type: Any) -> str:
    text = str(order_type)
    if "long" in text:
        return "long"
    if "short" in text:
        return "short"
    raise ValueError(f"cannot derive position side from order type {text!r}")

# --------------------------------------------------------------------------------------
# Facts derived from one replay ledger
# --------------------------------------------------------------------------------------


def ledger_facts(
    fills: pd.DataFrame, equity: pd.DataFrame, cfg: dict[str, Any], variant: study.Variant
) -> dict[str, Any]:
    minute_key = fills["timestamp"].dt.floor("min")
    per_minute = fills.groupby(minute_key).size()
    is_entry = fills["type"].astype(str).str.startswith("entry_")
    type_counts = fills["type"].astype(str).value_counts().to_dict()
    panic_mask = fills["type"].astype(str).str.contains(study.PANIC_FILL_MARKER)
    risk = cfg["bot"]["long"]["risk"]
    single_coin_cap = (
        float(risk["total_wallet_exposure_limit"])
        / float(risk["n_positions"])
        * (1.0 + float(risk["we_excess_allowance_pct"]))
    )
    wallet_exposure = fills["wallet_exposure"].abs()
    sample_lo = equity["timestamp"].iloc[0]
    sample_hi = equity["timestamp"].iloc[-1]
    outside = (fills["timestamp"] < sample_lo) | (fills["timestamp"] > sample_hi)
    panic = fills.loc[panic_mask]
    panic_by_coin = (
        panic.groupby("coin")
        .agg(fills=("coin", "size"), pnl=("pnl", "sum"), fees=("fee_paid", "sum"))
        .sort_values("fills", ascending=False)
    )
    return {
        "variant": variant.key,
        "minutes_with_multiple_fills": int((per_minute > 1).sum()),
        "max_fills_in_a_minute": int(per_minute.max()),
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
        "rows_twe_long_above_limit": int(
            (fills["twe_long"] > float(risk["total_wallet_exposure_limit"]) + 1e-12).sum()
        ),
        "max_abs_wallet_exposure": float(wallet_exposure.max()),
        "single_coin_cap": single_coin_cap,
        "rows_we_above_reference_cap": int((wallet_exposure > single_coin_cap + 1e-12).sum()),
        "top_symbol_share": float(fills["coin"].value_counts(normalize=True).iloc[0]),
        "first_fill": fills["timestamp"].iloc[0],
        "last_fill": fills["timestamp"].iloc[-1],
        "sample_first": sample_lo,
        "sample_last": sample_hi,
        "fills_outside_sample": int(outside.sum()),
        "pnl_outside_sample": float(
            fills.loc[outside, "pnl"].sum() + fills.loc[outside, "fee_paid"].sum()
        ),
        "distinct_fill_days": int(fills["timestamp"].dt.floor("D").nunique()),
        "panic_fill_count": int(panic_mask.sum()),
        "panic_types": sorted({str(t) for t in panic["type"].astype(str).unique()}),
        "panic_coin_count": int(panic["coin"].nunique()),
        "panic_pnl": float(panic["pnl"].sum()) if len(panic) else 0.0,
        "panic_fees": float(panic["fee_paid"].sum()) if len(panic) else 0.0,
        "panic_by_coin": panic_by_coin,
        "panic_first": panic["timestamp"].iloc[0] if len(panic) else None,
        "panic_last": panic["timestamp"].iloc[-1] if len(panic) else None,
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
        "columns": list(audit.columns),
    }


# --------------------------------------------------------------------------------------
# Facts derived from the three comparison columns
# --------------------------------------------------------------------------------------


def arm_facts(run_dir: Path) -> dict[str, Any]:
    """Load one arm's artifacts into the comparison's shape."""
    analysis = study.load_json(run_dir / "analysis.json")
    record_path = run_dir / "run_record.json"
    global_metrics_path = run_dir / "global_metrics.json"
    record = study.load_json(record_path) if record_path.exists() else {}
    global_metrics = study.load_json(global_metrics_path) if global_metrics_path.exists() else {}
    cfg_path = run_dir / "config.json"
    cfg = study.load_json(cfg_path) if cfg_path.exists() else {}
    engine = record.get("engine", {}) if record else {}
    variant_block = (record.get("variant") or {}) if record else {}
    hsl_enabled = variant_block.get("hsl_enabled")
    if hsl_enabled is None:
        hsl_enabled = bool((((cfg.get("bot") or {}).get("long") or {}).get("hsl") or {}).get("enabled"))
    return {
        "run_dir": run_dir,
        "label": study.relative(run_dir),
        "analysis": analysis,
        "analysis_sha256": study.sha256_file(run_dir / "analysis.json"),
        "analysis_source": "tracked" if not record else "local",
        "config": cfg,
        "record": record,
        "global_metrics": global_metrics,
        "engine_fingerprint": engine.get("expected_source_fingerprint")
        or (global_metrics.get("engine", {}) or {}).get("source_fingerprint"),
        "engine_stamp_matches": engine.get("source_fingerprint_matches_compiled_stamp"),
        "elapsed_s": global_metrics.get("backtest_elapsed_s"),
        "hsl_enabled": bool(hsl_enabled),
    }


def comparison_columns(hsl_on_run: Path, control_run: Path) -> list[dict[str, Any]]:
    baseline_metrics = study.load_json(study.TRACKED_BASELINE_RUN / "global_metrics.json")
    baseline = {
        "key": study.TRACKED_BASELINE_LABEL,
        "role": "tracked HSL-OFF 证据（旧引擎）",
        "run_dir": study.TRACKED_BASELINE_RUN,
        "analysis": study.load_json(study.TRACKED_BASELINE_RUN / "analysis.json"),
        "analysis_sha256": study.TRACKED_BASELINE_ANALYSIS_SHA256,
        "analysis_source": "tracked",
        "engine_fingerprint": (baseline_metrics.get("engine", {}) or {}).get("source_fingerprint"),
        "hsl_enabled": False,
    }
    control = dict(
        arm_facts(control_run), key="hsl_off_control", role="本次对照 HSL-OFF（当前引擎）"
    )
    hsl_on = dict(arm_facts(hsl_on_run), key="hsl_on", role="HSL ON（当前引擎）")
    return [baseline, control, hsl_on]


def comparison_table(columns: list[dict[str, Any]]) -> list[list[str]]:
    """One row per metric: tracked baseline | paired control | HSL on | ON-OFF delta."""
    baseline, control, hsl_on = columns
    rows: list[list[str]] = []
    for group, keys in study.COMPARISON_METRIC_GROUPS.items():
        rows.append([f"**{group}**", "", "", "", ""])
        for key in keys:
            rows.append(
                [
                    METRIC_LABELS.get(key, f"`{key}`"),
                    format_metric(key, baseline["analysis"].get(key)),
                    format_metric(key, control["analysis"].get(key)),
                    format_metric(key, hsl_on["analysis"].get(key)),
                    format_delta(key, control["analysis"].get(key), hsl_on["analysis"].get(key)),
                ]
            )
    return rows


def hsl_metrics_table(columns: list[dict[str, Any]]) -> list[list[str]]:
    baseline, control, hsl_on = columns
    rows: list[list[str]] = []
    for key in study.HSL_METRICS:
        rows.append(
            [
                METRIC_LABELS.get(key, f"`{key}`"),
                format_metric(key, baseline["analysis"].get(key)),
                format_metric(key, control["analysis"].get(key)),
                format_metric(key, hsl_on["analysis"].get(key)),
            ]
        )
    return rows


def engine_drift_rows(columns: list[dict[str, Any]]) -> list[list[str]]:
    """Tracked baseline vs paired control: same config, different engine revision."""
    baseline, control, _hsl_on = columns
    baseline_metrics = study.load_json(study.TRACKED_BASELINE_RUN / "global_metrics.json")
    control_record = control.get("record") or {}
    control_git = ((control_record.get("engine") or {}).get("git") or {}) if control_record else {}
    rows = [
        [
            "Rust source fingerprint",
            str(baseline.get("engine_fingerprint") or "n/a")[:16] + "…",
            str(control.get("engine_fingerprint") or "n/a")[:16] + "…",
            "",
        ],
        [
            "git HEAD",
            str((baseline_metrics.get("git", {}) or {}).get("head", "n/a"))[:12],
            str(control_git.get("head", "n/a"))[:12],
            "",
        ],
    ]
    for key in (
        "gain_strategy_eq",
        "drawdown_worst_strategy_eq",
        "strategy_eq_recovery_days_max",
        "sortino_ratio_strategy_eq",
        "fills_count",
        "total_wallet_exposure_max",
    ):
        rows.append(
            [
                METRIC_LABELS.get(key, f"`{key}`"),
                format_metric(key, baseline["analysis"].get(key)),
                format_metric(key, control["analysis"].get(key)),
                format_delta(key, baseline["analysis"].get(key), control["analysis"].get(key)),
            ]
        )
    return rows


def ledger_delta_facts(
    control_fills: pd.DataFrame, hsl_on_fills: pd.DataFrame
) -> dict[str, Any]:
    """Per-coin fill counts side by side: which coins the coin-scoped HSL changed."""
    control_counts = control_fills["coin"].value_counts()
    hsl_on_counts = hsl_on_fills["coin"].value_counts()
    coins = sorted(set(control_counts.index) | set(hsl_on_counts.index))
    rows = []
    for coin in coins:
        before = int(control_counts.get(coin, 0))
        after = int(hsl_on_counts.get(coin, 0))
        if before == after:
            continue
        rows.append({"coin": coin, "control": before, "hsl_on": after, "delta": after - before})
    rows.sort(key=lambda row: (-abs(row["delta"]), row["coin"]))
    return {
        "rows": rows,
        "changed_coin_count": len(rows),
        "stopped_coins": [row["coin"] for row in rows if row["hsl_on"] == 0 and row["control"] > 0],
        "started_coins": [row["coin"] for row in rows if row["control"] == 0 and row["hsl_on"] > 0],
        "unchanged_coin_count": len(coins) - len(rows),
        "coin_count": len(coins),
    }

# --------------------------------------------------------------------------------------
# Scope lines and appendix
# --------------------------------------------------------------------------------------


def scope_lines(
    result_dir: Path,
    variant: study.Variant,
    record: dict[str, Any],
    global_metrics: dict[str, Any],
    cfg: dict[str, Any],
    variant_input: dict[str, Any],
) -> list[str]:
    bt = cfg["backtest"]
    delay = int(bt["execution_delay_bars"])
    kind = cfg.get("live", {}).get("strategy_kind", "n/a")
    approved = cfg.get("live", {}).get("approved_coins", {}) or {}
    universe = record.get("universe", {})
    engine = record.get("engine", {})
    git = engine.get("git", {}) or {}
    modified = len(git.get("status_porcelain") or [])
    gate = record.get("entry_regime_gate") or {}
    hsl = record.get("hsl_block") or {}
    delta_text = "、".join(
        f"`{item['path']}: {item['from']} → {item['to']}`"
        for item in variant_input["declared_delta"]
    ) or "无"
    twe = float(cfg["bot"]["long"]["risk"]["total_wallet_exposure_limit"])
    n_positions = float(cfg["bot"]["long"]["risk"]["n_positions"])
    allowance = float(cfg["bot"]["long"]["risk"]["we_excess_allowance_pct"])
    lines = [
        f"- 变体：**{variant.key}**（{variant.description}）。本工件的父配置是"
        f"`{variant_input['parent_config']}`"
        f"（sha256 `{str(variant_input['parent_config_sha256'])[:16]}…`，即已发布门控 profile 的"
        f"冻结回放配置）；本变体的运行配置为 `{record.get('candidate_config_path')}`"
        f"（sha256 `{str(record.get('candidate_config_sha256', ''))[:16]}…`）。",
        f"- **声明的唯一改动**：{delta_text}。其余字段（含全部 `bot` / `live` / `coin_overrides` /"
        "`monitor` / `logging` 以及窗口、币池、数据集、执行与成本口径）与父配置逐路径一致，"
        "由 `report_tools/build_variant_config.py` 在冻结时逐字段校验，"
        "并由 `report_tools/run_variant.py` 在运行后对落盘配置复核。",
        f"- HSL 声明（本变体实际运行值）：`enabled = {hsl.get('enabled')}`、"
        f"`red_threshold = {hsl.get('red_threshold')}`、`tier_ratios = {hsl.get('tier_ratios')}`、"
        f"`ema_span_minutes = {hsl.get('ema_span_minutes')}`、"
        f"`cooldown_minutes_after_red = {hsl.get('cooldown_minutes_after_red')}`、"
        f"`no_restart_drawdown_threshold = {hsl.get('no_restart_drawdown_threshold')}`、"
        f"`restart_after_red_policy = {hsl.get('restart_after_red_policy')}`、"
        f"`orange_tier_mode = {hsl.get('orange_tier_mode')}`、"
        f"`panic_close_order_type = {hsl.get('panic_close_order_type')}`；"
        f"信号模式 `live.hsl_signal_mode = {(record.get('live_only_hsl') or {}).get('hsl_signal_mode')}`"
        "（coin+pside 独立控制器，RED 只 panic 受影响的币），回看窗口 "
        f"`live.pnls_max_lookback_days = {cfg['live'].get('pnls_max_lookback_days')}`。"
        "实盘专有行为（重启历史回放、冷却期内人工持仓策略）不在回测建模范围内，见附录「边界」。",
        f"- 策略与交易域：`live.strategy_kind = {kind}`；`live.approved_coins` long "
        f"{len(approved.get('long') or [])} 币 / short {len(approved.get('short') or [])} 币"
        f"（short 结构性不交易：`bot.short.risk.total_wallet_exposure_limit = "
        f"{cfg['bot']['short']['risk']['total_wallet_exposure_limit']}`）；"
        f"`hedge_mode = {cfg['live'].get('hedge_mode')}`；"
        f"槽位敞口预算 `TWE / n_positions = {twe / n_positions:.6f}`"
        f"（含超出额度 `{allowance}` 后 {twe / n_positions * (1 + allowance):.6f}）；"
        f"`dynamic_wel_by_tradability = {bt.get('dynamic_wel_by_tradability')}` 使 coin 模式 HSL 的分母"
        "与模拟入场预算使用同一套可交易性感知槽位。",
        f"- 入场择时闸门（与父配置一致）：`sma_fast_days = {gate.get('sma_fast_days')}`、"
        f"`sma_slow_days = {gate.get('sma_slow_days')}`、`confirm_days = {gate.get('confirm_days')}`、"
        f"`block_initial = {gate.get('block_initial')}`、`block_reentry = {gate.get('block_reentry')}`；"
        "回放中止条件包含「闸门必须启用且与该声明逐字段一致」，因此本工件不可能是未加闸门的运行。",
        f"- 数据为 {universe.get('exchange', 'binance')} USDT-M 永续合约 1 分钟 K 线；冻结篮子 "
        f"{universe.get('coin_count', 'n/a')} 币；数据集为 `{global_metrics['dataset']['cache_dir']}`"
        f"（`cache_hash = {str(global_metrics['dataset'].get('cache_hash'))[:16]}…`），"
        "本次回放未重建、未下载任何 K 线。",
        f"- 执行与成本口径：`execution_delay_bars={delay}`（T+{delay + 1}）、"
        f"`intrabar_fill_order={bt['intrabar_fill_order']}`、maker `{bt['maker_fee_override']}` / "
        f"taker `{bt['taker_fee_override']}`；HSL panic 平仓按 "
        f"`hsl_panic_close_order_type = {hsl.get('panic_close_order_type')}` 的交叉限价模型撮合"
        "（不是市价滑点模型）。",
        f"- 权益口径：`balance_sample_divider={bt['balance_sample_divider']}` 的 `strategy_equity`；"
        f"`btc_collateral_cap={bt['btc_collateral_cap']}`。报告规范：`{study.REPORT_CONVENTION}`；"
        "本次运行未禁用任何图表分组，逐币成交面板完整。",
        "- 对照列：结论来自**同一次引擎运行**下「HSL 关（配对对照）」与「HSL 开」两列的比较；"
        "父研究的 tracked 工件列由更早的引擎修订产出，只作数量级参照（漂移见附录）。",
        f"- 引擎溯源：Rust source fingerprint `{engine.get('expected_source_fingerprint')}`；"
        f"已编译扩展 stamp 与源码一致 = `{engine.get('source_fingerprint_matches_compiled_stamp')}`；"
        f"工作区当时{'含' if git.get('dirty') else '不含'}未提交改动（{modified} 项）。",
        "- 安全边界：本工件由本地离线回测生成，**未联网下载任何数据**、未使用凭证、未接触交易所账户、"
        f"未创建或撤销订单、未启动机器人；运行日志见 `{study.relative(variant.replay_log_path)}`。",
    ]
    return lines


def hsl_semantics_section(record: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    hsl = record.get("hsl_block") or {}
    red = float(hsl.get("red_threshold") or 0.0)
    tiers = hsl.get("tier_ratios") or {}
    yellow = red * float(tiers.get("yellow") or 0.0)
    orange = red * float(tiers.get("orange") or 0.0)
    return [
        "### HSL 语义与实际生效值",
        "",
        "- 触发口径：coin 模式下每个 `coin+pside` 有自己的控制器，"
        "`drawdown = (窗口内已实现 PnL 峰值 − (最新已实现 PnL + 当前 UPnL)) / 槽位预算`，"
        f"槽位预算 = `balance / n_positions`；本变体 `n_positions = "
        f"{cfg['bot']['long']['risk']['n_positions']}`、回看窗口 "
        f"`live.pnls_max_lookback_days = {cfg['live'].get('pnls_max_lookback_days')}`。",
        f"- 档位边界（占槽位预算）：YELLOW ≥ {yellow:.4f}、ORANGE ≥ {orange:.4f}、RED ≥ {red:.4f}；"
        f"ORANGE 行为 `{hsl.get('orange_tier_mode')}`。",
        f"- RED 行为：该 `coin+pside` 强制 panic 平仓（`{hsl.get('panic_close_order_type')}`）后停机；"
        f"`cooldown_minutes_after_red = {hsl.get('cooldown_minutes_after_red')}` 分钟后按 "
        f"`restart_after_red_policy` 重启；`no_restart_drawdown_threshold = "
        f"{hsl.get('no_restart_drawdown_threshold')}`（低于 `red_threshold` 会被下限钳制）"
        "表示跨重启累计回撤达到该比例才永久停机，本变体因此不会永久停机。",
        f"- 平滑：`ema_span_minutes = {hsl.get('ema_span_minutes')}`（不小于 1 分钟 K 线，平滑有效）；"
        "触发分数取 `min(raw, ema)`。",
    ]


def study_appendix(
    variant: study.Variant,
    columns: list[dict[str, Any]],
    hsl_on_ledger: dict[str, Any],
    control_ledger: dict[str, Any],
    ledger_delta: dict[str, Any],
    audit: dict[str, Any],
    variant_input: dict[str, Any],
    cfg: dict[str, Any],
    record: dict[str, Any],
) -> str:
    parts: list[str] = []
    baseline, control, hsl_on = columns

    parts.append("### 声明的唯一改动与身份证明")
    parts.append("")
    parts.append(
        "本变体相对父配置只改一个字段，其余逐路径一致："
        + "、".join(
            f"`{item['path']}`（{item['from']} → {item['to']}）"
            for item in variant_input["declared_delta"]
        )
        + "。冻结工具在写盘前校验：① 父配置 sha256 等于固定值；② 对照配置与父配置只差输出目录；"
        "③ 本变体与父配置只差输出目录与这一个字段；④ `bot.long.hsl` 除 `enabled` 外与父配置逐字段相同。"
    )
    parts.append("")
    parts.extend(hsl_semantics_section(record, cfg))
    parts.append("")

    parts.append("### 三列对比")
    parts.append("")
    parts.append(
        f"| 指标 | {baseline['role']} | {control['role']} | {hsl_on['role']} | Δ（HSL ON − 对照） |"
    )
    parts.append("| --- | --- | --- | --- | --- |")
    for row in comparison_table(columns):
        parts.append("| " + " | ".join(row) + " |")
    parts.append("")
    parts.append(
        f"对照列与 HSL ON 列是同一次引擎运行下的同配置配对，因此 Δ 列可读作 HSL 的净效应；"
        f"tracked 基线的 `analysis.json` sha256 `{study.TRACKED_BASELINE_ANALYSIS_SHA256[:16]}…` "
        "已固定在校验常量中。"
    )
    parts.append("")

    parts.append("### HSL 运行学明细")
    parts.append("")
    parts.append(f"| 指标 | {baseline['role']} | {control['role']} | {hsl_on['role']} |")
    parts.append("| --- | --- | --- | --- |")
    for row in hsl_metrics_table(columns):
        parts.append("| " + " | ".join(row) + " |")
    parts.append("")
    if hsl_on["analysis"].get("hard_stop_triggers"):
        parts.append(
            f"本变体在窗口内触发 RED **{int(hsl_on['analysis']['hard_stop_triggers'])}** 次，"
            f"重启 {int(hsl_on['analysis'].get('hard_stop_restarts') or 0)} 次；停机时长均值 "
            f"{format_metric('hard_stop_duration_minutes_mean', hsl_on['analysis'].get('hard_stop_duration_minutes_mean'))} 分钟、"
            f"最长 {format_metric('hard_stop_duration_minutes_max', hsl_on['analysis'].get('hard_stop_duration_minutes_max'))} 分钟；"
            f"panic 平仓损失合计 "
            f"{format_metric('hard_stop_panic_close_loss_sum', hsl_on['analysis'].get('hard_stop_panic_close_loss_sum'))} USDT。"
        )
    else:
        parts.append(
            "本变体在窗口内**未触发 RED**：`hard_stop_triggers = 0`，全部 HSL 运行学指标为 0，"
            "因此在本样本内「开启 HSL」与「不开启」在成交与权益路径上没有可观测差异。"
            "这不代表 HSL 无用：它是样本外的尾部保险，其触发条件（单个币槽位预算上的 15% 回撤）"
            "在本窗口内没有被行情触发。"
        )
    parts.append("")
    return "\n".join(parts)

def study_appendix_tail(
    variant: study.Variant,
    columns: list[dict[str, Any]],
    hsl_on_ledger: dict[str, Any],
    control_ledger: dict[str, Any],
    ledger_delta: dict[str, Any],
    audit: dict[str, Any],
) -> str:
    """Continuation of the appendix: ledger effects, audit, engine drift, boundaries, reading."""
    baseline, control, hsl_on = columns
    parts: list[str] = []

    parts.append("### 账本级变化（coin 模式停的是具体哪些币）")
    parts.append("")
    parts.append(
        f"- HSL ON 台账中 panic 成交 **{hsl_on_ledger['panic_fill_count']}** 笔"
        + (
            f"（类型 {', '.join('`' + t + '`' for t in hsl_on_ledger['panic_types'])}，覆盖 "
            f"{hsl_on_ledger['panic_coin_count']} 个币，PnL {hsl_on_ledger['panic_pnl']:+.6f} USDT、"
            f"手续费 {hsl_on_ledger['panic_fees']:+.6f} USDT）"
            if hsl_on_ledger["panic_fill_count"]
            else "（未触发 RED，故没有 panic 平仓）"
        )
        + f"；对照运行 panic 成交 {control_ledger['panic_fill_count']} 笔。",
    )
    parts.append(
        f"- 逐币成交数：{ledger_delta['coin_count']} 个币中 {ledger_delta['changed_coin_count']} 个不同、"
        f"{ledger_delta['unchanged_coin_count']} 个相同"
        + (
            f"；成交归零的币：{', '.join(ledger_delta['stopped_coins'])}"
            if ledger_delta["stopped_coins"]
            else ""
        )
        + (
            f"；新增成交的币：{', '.join(ledger_delta['started_coins'])}"
            if ledger_delta["started_coins"]
            else ""
        )
        + "。",
    )
    if ledger_delta["rows"]:
        parts.append("")
        parts.append("| 币 | 对照成交 | HSL ON 成交 | Δ |")
        parts.append("| --- | --- | --- | --- |")
        for row in ledger_delta["rows"][:15]:
            parts.append(
                f"| `{row['coin']}` | {row['control']:,} | {row['hsl_on']:,} | {row['delta']:+,} |"
            )
        if len(ledger_delta["rows"]) > 15:
            parts.append(f"| … | 其余 {len(ledger_delta['rows']) - 15} 个币 | | |")
    parts.append("")
    parts.append(
        "- 两个变体的台账特征（HSL ON / 对照）："
        f"成交 {hsl_on_ledger['fills_count']:,} / {control_ledger['fills_count']:,}；入场 "
        f"{hsl_on_ledger['entry_fill_count']:,} / {control_ledger['entry_fill_count']:,}；减仓或平仓 "
        f"{hsl_on_ledger['close_fill_count']:,} / {control_ledger['close_fill_count']:,}；最大总敞口 "
        f"{hsl_on_ledger['max_twe_long']:.6f} / {control_ledger['max_twe_long']:.6f}；最大单币敞口 "
        f"{hsl_on_ledger['max_abs_wallet_exposure']:.6f} / "
        f"{control_ledger['max_abs_wallet_exposure']:.6f}。"
    )
    parts.append("")

    parts.append("### 执行审计")
    parts.append("")
    parts.append(
        f"- 执行审计：`{audit.get('path')}`，{audit.get('rows')} 行，"
        f"`activation_index = decision_index + 1 + execution_delay_bars` 违例 "
        f"{audit.get('activation_identity_failures')} 行、成交早于激活 "
        f"{audit.get('fill_before_activation_failures')} 行。"
    )
    parts.append("")

    parts.append("### 引擎漂移对照（同配置、不同引擎）")
    parts.append("")
    parts.append(
        "父研究的 tracked 工件由更早的引擎修订产出（此后 `passivbot-rust/src` 累计 +436 行），"
        "因此本次补跑同配置对照：它既是 HSL 比较的基线列，也是「当前引擎能否复现 recorded 基线」的检验。"
    )
    parts.append("")
    parts.append("| 项 | tracked 基线（旧引擎） | 本次对照（当前引擎） | Δ |")
    parts.append("| --- | --- | --- | --- |")
    for row in engine_drift_rows(columns):
        parts.append("| " + " | ".join(row) + " |")
    parts.append("")

    parts.append("### 边界")
    parts.append("")
    for note in (
        "回测 HSL ≠ 实盘 HSL：实盘的重启历史回放与 `hsl_position_during_cooldown_policy`"
        "（冷却期内出现人工持仓的处理）不在回测建模范围内。",
        "panic 平仓按 `hsl_panic_close_order_type = limit` 的交叉限价模型撮合；实盘使用交易所真实"
        "盘口与滑点，成本不同。",
        "coin 模式触发依赖 `live.pnls_max_lookback_days` 的回看窗口与 `n_positions` 槽位预算，"
        "改变两者会改变触发频率；本报告只描述当前取值。",
        "`no_restart_drawdown_threshold = 1` 表示不会永久停机；更小的取值会产生不同的重启与停机行为，"
        "那是另一个变体。",
        "全部结论只在这一个三年窗口、单交易所、40 币、单一执行与成本口径下成立；"
        "「未触发 RED」是样本内事实，不是对样本外的承诺。",
    ):
        parts.append(f"- {note}")
    parts.append("")

    parts.append("### 对实盘的读数")
    parts.append("")
    if hsl_on["analysis"].get("hard_stop_triggers"):
        parts.append(
            "在本窗口内，开启 HSL 使策略权益最差回撤从 "
            f"{format_metric('drawdown_worst_strategy_eq', control['analysis'].get('drawdown_worst_strategy_eq'))} "
            f"变为 {format_metric('drawdown_worst_strategy_eq', hsl_on['analysis'].get('drawdown_worst_strategy_eq'))}"
            "（Δ "
            f"{format_delta('drawdown_worst_strategy_eq', control['analysis'].get('drawdown_worst_strategy_eq'), hsl_on['analysis'].get('drawdown_worst_strategy_eq'))}），"
            f"收益倍数从 {format_metric('gain_strategy_eq', control['analysis'].get('gain_strategy_eq'))} "
            f"变为 {format_metric('gain_strategy_eq', hsl_on['analysis'].get('gain_strategy_eq'))}；代价是 "
            f"{int(hsl_on['analysis']['hard_stop_triggers'])} 次 RED 停机，重启后再次触发比例 "
            f"{format_metric('hard_stop_post_restart_retrigger_pct', hsl_on['analysis'].get('hard_stop_post_restart_retrigger_pct'))}。"
            "是否在实盘开启取决于对该代价与尾部保护的权衡。"
        )
    else:
        parts.append(
            "在本窗口内 HSL **没有改变任何成交**（零触发、零 panic、零停机），因此这份证据不能证明它"
            "会改善或恶化历史成绩；它证明的是：在这套参数下，历史三年行情没有走到触发阈值。"
            "把它接到实盘的意义是样本外的尾部保护（单个币槽位预算 15% 回撤即停机），"
            "代价只在真正触发时发生。"
        )
    parts.append("")
    return "\n".join(parts)


def control_appendix(
    variant: study.Variant,
    columns: list[dict[str, Any]],
    hsl_on_report_hint: str,
    variant_input: dict[str, Any],
) -> str:
    parts = [
        "### 这份工件是对照组",
        "",
        f"本运行把父配置 `{variant_input['parent_config']}` 原样跑在**当前引擎**上（唯一改动是输出目录），"
        "作为 HSL 变体的配对基线：除声明 delta（"
        + "、".join(item["path"] for item in variant_input["declared_delta"])
        + "）之外的任何差异都不是本研究的自变量。",
        "",
        "| 项 | tracked 基线（旧引擎） | 本次对照（当前引擎） | Δ |",
        "| --- | --- | --- | --- |",
    ]
    for row in engine_drift_rows(columns):
        parts.append("| " + " | ".join(row) + " |")
    parts += [
        "",
        f"HSL ON 变体的完整对比与结论见 `{hsl_on_report_hint}` 的 `## 研究附录`。",
    ]
    return "\n".join(parts)

def artifacts_table(
    result_dir: Path, audit: dict[str, Any], equity: pd.DataFrame, analysis: dict[str, Any]
) -> list[tuple[str, str]]:
    return [
        ("annual_analysis.md", "本报告（固定章节骨架 + 研究附录）"),
        ("annual_metrics.csv", "自然年汇总表（本报告 `## 自然年汇总` 的数据源）"),
        ("monthly_metrics.csv", "月度汇总表（本报告 `## 月度汇总` 的数据源）"),
        ("coin_metrics.csv", "逐币汇总表（本报告 `## 按币种贡献` 的数据源）"),
        ("analysis.json", f"引擎指标（{len(analysis)} 项，含 `hard_stop_*` 运行学指标）"),
        ("config.json", "本变体的有效运行配置（冻结配置经管线净化后落盘）"),
        ("dataset.json", "数据集身份（冻结 bundle 的 cache_hash 与币池）"),
        ("fills.csv", "成交台账（本报告全部成交口径数字的唯一来源）"),
        ("balance_and_equity.csv.gz", f"权益序列（{len(equity):,} 行，报告采样端点）"),
        ("execution_audit.csv", f"流式执行审计（{audit.get('rows')} 行，逐笔决策/激活/成交序号）"),
        ("global_metrics.json", "引擎指纹、数据集逻辑哈希、耗时与环境"),
        ("run_record.json", "本变体的溯源记录（声明 delta、HSL 声明、对比列引用）"),
    ]


def verifiable_notes(
    result_dir: Path,
    variant: study.Variant,
    record: dict[str, Any],
    global_metrics: dict[str, Any],
    ledger: dict[str, Any],
    equity: pd.DataFrame,
    variant_input: dict[str, Any],
) -> list[str]:
    dataset = global_metrics["dataset"]
    dataset_hashes_match = all(
        dataset["data_hashes"].get(key) == dataset["manifest_hashes"].get(key)
        for key in ("hlcvs", "timestamps", "btc_usd_prices")
    )
    return [
        f"- 权益采样端点：`balance_and_equity.csv.gz` 覆盖 {equity['timestamp'].iloc[0]} → "
        f"{equity['timestamp'].iloc[-1]}（{len(equity):,} 行）；`fills.csv` 有 "
        f"{ledger['fills_outside_sample']} 笔成交发生在最后一个采样点之后（合计净 "
        f"{ledger['pnl_outside_sample']:+.6f} USDT），因此年度/月度表的净已实现 PnL 合计不等于"
        "全账本合计，两者差值只能由该端点差解释。",
        f"- 变体身份：父配置 `{variant_input['parent_config']}`"
        f"（sha256 `{str(variant_input['parent_config_sha256'])[:16]}…`）+ 本变体冻结配置 "
        f"`{record.get('candidate_config_path')}`"
        f"（sha256 `{str(record.get('candidate_config_sha256', ''))[:16]}…`）；声明 delta = "
        + "、".join(item["path"] for item in variant_input["declared_delta"])
        + "。",
        f"- 对照引用：tracked 基线运行 `{study.relative(study.TRACKED_BASELINE_RUN)}`"
        f"（`analysis.json` sha256 `{study.TRACKED_BASELINE_ANALYSIS_SHA256[:16]}…`，已固定为校验"
        "常量）；配对对照与 HSL ON 两个 bundle 都在本目录下。对比表的 tracked 列可由该 tracked "
        "`analysis.json` 复算；配对各列与 panic 台账数字需要本地产物，可由 `run.sh` 重跑复现。",
        f"- 冻结数据集：`{dataset['cache_dir']}`；三份数据的逻辑数组哈希与 bundle `manifest.json` "
        f"记录一致 = `{dataset_hashes_match}`（`hlcvs` = "
        f"`{str(dataset['data_hashes']['hlcvs'])[:16]}…`，shape `{dataset['array_shapes']['hlcvs']}`；"
        "gzip 字节流的 sha256 只作溯源，见 `global_metrics.json`）。",
        f"- Rust source fingerprint：`{record.get('engine', {}).get('expected_source_fingerprint')}`"
        f"（已编译扩展 stamp 与源码一致 = "
        f"`{record.get('engine', {}).get('source_fingerprint_matches_compiled_stamp')}`）。",
        f"- 运行日志：`{study.relative(variant.replay_log_path)}`；本报告与三张 CSV 的一致性由同目录 "
        "`report_tools/verify_variant_report.py` 独立复算校验（该脚本不 import 本渲染器）。",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=study.DEFAULT_VARIANT_ORDER)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument("--report-title", default=None)
    parser.add_argument(
        "--hsl-on-report-hint",
        default=None,
        help="path cited by the control report's appendix (defaults to the HSL ON report)",
    )
    args = parser.parse_args()

    variant = variant_of(args.variant)
    result_dir = study.find_variant_run_dir(variant, args.result_dir)
    require_artifacts(result_dir, variant)
    variant_input = study.load_json(study.VARIANT_INPUT_PATH)

    cfg = study.load_json(result_dir / "config.json")
    record = study.load_json(result_dir / "run_record.json")
    global_metrics = study.load_json(result_dir / "global_metrics.json")
    analysis = study.load_json(result_dir / "analysis.json")
    fills = spec.load_fills(result_dir)
    equity = spec.load_balance_equity(result_dir)

    annual = spec.build_period_table(equity, fills, "Y")
    monthly = spec.build_period_table(equity, fills, "M")
    coins = spec.build_coin_table(fills)
    ledger = ledger_facts(fills, equity, cfg, variant)
    audit = audit_facts(
        variant.execution_audit_path, int(cfg["backtest"]["execution_delay_bars"])
    )

    annual.to_csv(result_dir / "annual_metrics.csv", index=False)
    monthly.to_csv(result_dir / "monthly_metrics.csv", index=False)
    coins.to_csv(result_dir / "coin_metrics.csv", index=False)

    try:
        result_label = str(result_dir.relative_to(study.REPO))
    except ValueError:
        result_label = str(result_dir)

    control_variant = study.VARIANTS_BY_KEY["hsl_off_control"]
    hsl_on_variant = study.VARIANTS_BY_KEY["hsl_on"]
    control_dir = study.find_variant_run_dir(control_variant)
    hsl_on_dir = study.find_variant_run_dir(hsl_on_variant)
    columns = comparison_columns(hsl_on_dir, control_dir)
    control_fills = spec.load_fills(control_dir)
    if variant.key == "hsl_on":
        ledger_delta = ledger_delta_facts(control_fills, fills)
        control_ledger = ledger_facts(
            control_fills,
            spec.load_balance_equity(control_dir),
            study.load_json(control_dir / "config.json"),
            control_variant,
        )
        appendix = study_appendix(
            variant, columns, ledger, control_ledger, ledger_delta, audit, variant_input, cfg, record
        ) + "\n" + study_appendix_tail(
            variant, columns, ledger, control_ledger, ledger_delta, audit
        )
    else:
        hint = args.hsl_on_report_hint or f"{study.relative(hsl_on_dir)}/annual_analysis.md"
        appendix = control_appendix(variant, columns, hint, variant_input)

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
        "scope_lines": scope_lines(
            result_dir, variant, record, global_metrics, cfg, variant_input
        ),
        "appendix": appendix,
        "artifacts": artifacts_table(result_dir, audit, equity, analysis),
        "verifiable_notes": verifiable_notes(
            result_dir, variant, record, global_metrics, ledger, equity, variant_input
        ),
    }
    if variant.key == "hsl_on":
        context["interpretation_extra"] = [
            "本变体的实验变量是 `bot.long.hsl.enabled`：与配对对照之间的差异只能归因于它，"
            "其余字段（窗口、币池、数据集、执行与成本、入场闸门）逐路径一致。"
        ]
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
        f"variant={variant.key} samples={len(equity):,} fills={ledger['fills_count']:,} "
        f"panic_fills={ledger['panic_fill_count']}"
    )


if __name__ == "__main__":
    main()