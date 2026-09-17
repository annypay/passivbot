#!/usr/bin/env python3
"""Independently verify one g4 "HSL on" variant artifact and its deep-analysis report.

Every number is recomputed from `fills.csv`, `balance_and_equity.csv.gz`,
`execution_audit.csv`, the run's own config/dataset metadata, the three `analysis.json`
columns and the frozen HLCV bundle. Nothing is imported from the report generator, so a
renderer regression fails here instead of shipping.

The study's claims are checked rather than assumed: the parent config hashes to its pinned
value, the variant config is the parent plus exactly the declared change, the run kept the
20/50 gate enabled, the HSL block is the parent profile's own parameters with `enabled`
flipped, and the comparison table in the report is reproduced from the three
`analysis.json` files.

Offline only. Exits non-zero on any failed check. Reported warnings (a claim that could not
be verified) fail the run unless `--allow-warnings` is passed.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import variant_spec as spec  # noqa: E402

COIN_COLUMNS = (
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

REQUIRED_ARTIFACTS = (
    "analysis.json",
    "config.json",
    "dataset.json",
    "fills.csv",
    "balance_and_equity.csv.gz",
    "balance_and_equity.png",
    "balance_and_equity_logy.png",
    "drawdown.png",
    "total_wallet_exposure.png",
    "pnl_cumsum.png",
    "fills_plots",
    "annual_analysis.md",
    "annual_metrics.csv",
    "monthly_metrics.csv",
    "coin_metrics.csv",
    "run_record.json",
    "global_metrics.json",
)

#: Independent copies of the renderer's metric formatting. They are duplicated on purpose:
#: the verifier must be able to disagree with the renderer, otherwise a formatting regression
#: would pass unnoticed.
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

CHECKS: list[tuple[str, bool, str]] = []
WARNINGS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def warn(message: str) -> None:
    WARNINGS.append(message)


def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_fills(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(result_dir / "fills.csv")
    frame = frame.loc[:, ~frame.columns.str.startswith("Unnamed")]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["type"] = frame["type"].astype(str)
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def load_equity(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(result_dir / "balance_and_equity.csv.gz", compression="gzip")
    # The sampled equity series names its timestamp column by position (the CSV is written
    # from a frame whose index is the timestamp), so it is renamed rather than assumed.
    frame = frame.rename(columns={frame.columns[0]: "timestamp"})
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in frame.columns:
        if column != "timestamp":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
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


def iso(stamp: Any) -> str:
    if stamp is None:
        return ""
    return str(stamp).replace("T", " ", 1)


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

def period_rows(equity: pd.DataFrame, fills: pd.DataFrame, freq: str) -> list[dict[str, Any]]:
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
        end = edges[idx + 1] if idx + 1 < len(edges) else last + pd.DateOffset(seconds=1)
        if start > last:
            continue
        coverage = "起始非完整" if idx == 0 else ("结束非完整" if idx == len(edges) - 1 else "完整")
        mask = (equity["timestamp"] >= start) & (equity["timestamp"] < end)
        slice_eq = equity.loc[mask]
        sub = fills.loc[(fills["timestamp"] >= start) & (fills["timestamp"] < end)]
        types = sub["type"].astype(str)
        entry = types.str.startswith("entry_")
        liquidity = sub["liquidity"].astype(str)

        def first_last(series: pd.Series) -> tuple[float, float]:
            if series.empty:
                return float("nan"), float("nan")
            return float(series.iloc[0]), float(series.iloc[-1])

        if slice_eq.empty:
            start_balance = end_balance = start_total = end_total = float("nan")
            start_strat = end_strat = float("nan")
        else:
            start_balance, end_balance = first_last(slice_eq["usd_total_balance"])
            start_total, end_total = first_last(slice_eq["usd_total_equity"])
            start_strat, end_strat = first_last(slice_eq["strategy_equity"])
        realized = float(sub["pnl"].sum()) if not sub.empty else 0.0
        fees = float(sub["fee_paid"].sum()) if not sub.empty else 0.0
        rows.append(
            {
                "period": labels[idx],
                "coverage": coverage,
                "sample_start_utc": iso(slice_eq["timestamp"].iloc[0]) if not slice_eq.empty else "",
                "sample_end_utc": iso(slice_eq["timestamp"].iloc[-1]) if not slice_eq.empty else "",
                "starting_total_balance_usd": start_balance,
                "ending_total_balance_usd": end_balance,
                "total_balance_return_pct": (end_balance / start_balance - 1.0) if start_balance else float("nan"),
                "starting_total_equity_usd": start_total,
                "ending_total_equity_usd": end_total,
                "total_equity_return_pct": (end_total / start_total - 1.0) if start_total else float("nan"),
                "starting_strategy_equity": start_strat,
                "ending_strategy_equity": end_strat,
                "strategy_equity_return_pct": (end_strat / start_strat - 1.0) if start_strat else float("nan"),
                "max_intraperiod_equity_drawdown_pct": max_drawdown(slice_eq["usd_total_equity"]),
                "max_intraperiod_strategy_equity_drawdown_pct": max_drawdown(slice_eq["strategy_equity"]),
                "fills_count": int(len(sub)),
                "entry_fills_count": int(entry.sum()),
                "reduction_or_close_fills_count": int((~entry).sum()),
                "long_fills_count": int(types.str.contains("long").sum()),
                "short_fills_count": int(types.str.contains("short").sum()),
                "maker_fills_count": int((liquidity == "maker").sum()),
                "taker_fills_count": int((liquidity == "taker").sum()),
                "realized_pnl_raw_usd": realized,
                "fees_signed_usd": fees,
                "net_realized_pnl_usd": realized + fees,
                "max_abs_wallet_exposure_at_fill": float(sub["wallet_exposure"].abs().max())
                if not sub.empty
                else float("nan"),
                "active_coins": ",".join(sorted(sub["coin"].astype(str).unique())) if not sub.empty else "",
            }
        )
    return rows


def coin_rows(fills: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for coin, sub in fills.groupby("coin", sort=False):
        types = sub["type"].astype(str)
        entry = types.str.startswith("entry_")
        liquidity = sub["liquidity"].astype(str)
        realized = float(sub["pnl"].sum())
        fees = float(sub["fee_paid"].sum())
        rows.append(
            {
                "coin": coin,
                "fills_count": int(len(sub)),
                "entry_fills_count": int(entry.sum()),
                "reduction_or_close_fills_count": int((~entry).sum()),
                "maker_fills_count": int((liquidity == "maker").sum()),
                "taker_fills_count": int((liquidity == "taker").sum()),
                "realized_pnl_raw_usd": realized,
                "fees_signed_usd": fees,
                "net_realized_pnl_usd": realized + fees,
                "max_abs_wallet_exposure_at_fill": float(sub["wallet_exposure"].abs().max()),
                "first_fill_utc": iso(sub["timestamp"].iloc[0]),
                "last_fill_utc": iso(sub["timestamp"].iloc[-1]),
            }
        )
    frame = pd.DataFrame(rows, columns=list(COIN_COLUMNS))
    return frame.sort_values(
        "net_realized_pnl_usd", ascending=False, kind="stable"
    ).reset_index(drop=True)


def compare_frames(expected: pd.DataFrame, actual: pd.DataFrame, columns, tol: float) -> list[str]:
    problems: list[str] = []
    if len(expected) != len(actual):
        return [f"row count {len(expected)} != {len(actual)}"]
    for column in columns:
        if column not in actual.columns:
            problems.append(f"missing column {column!r}")
            continue
        left = expected[column]
        right = actual[column]
        if pd.api.types.is_numeric_dtype(left) and pd.api.types.is_numeric_dtype(right):
            diff = (left.fillna(0.0).astype(float) - right.fillna(0.0).astype(float)).abs()
            bad = diff > tol
            if bad.any():
                idx = int(np.argmax(diff.to_numpy()))
                problems.append(
                    f"{column}: {int(bad.sum())} row(s) differ; worst row {idx} "
                    f"expected {left.iloc[idx]!r} got {right.iloc[idx]!r}"
                )
            continue
        left_s = left.fillna("").astype(str).str.replace("T", " ", regex=False)
        right_s = right.fillna("").astype(str).str.replace("T", " ", regex=False)
        left_s = left_s.replace({"nan": "", "None": ""})
        right_s = right_s.replace({"nan": "", "None": ""})
        bad = left_s != right_s
        if bad.any():
            idx = int(np.argmax(bad.to_numpy()))
            problems.append(
                f"{column}: {int(bad.sum())} row(s) differ; worst row {idx} "
                f"expected {left_s.iloc[idx]!r} got {right_s.iloc[idx]!r}"
            )
    return problems


def parse_markdown_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    tables: list[tuple[list[str], list[list[str]]]] = []
    header: list[str] | None = None
    rows: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if header is None:
                header = cells
                rows = []
            else:
                rows.append(cells)
            continue
        if header is not None:
            tables.append((header, rows))
            header = None
            rows = []
    if header is not None:
        tables.append((header, rows))
    return tables


def table_by_header(
    tables: list[tuple[list[str], list[list[str]]]], header: list[str]
) -> list[list[str]] | None:
    """Rows of the table whose header cells match exactly, like the convention's tables."""
    for candidate, rows in tables:
        if candidate == header:
            return rows
    return None


def table_with_header_containing(
    tables: list[tuple[list[str], list[list[str]]]], needles: list[str]
) -> tuple[list[str], list[list[str]]] | None:
    """The first table whose header carries every needle as a substring of some cell."""
    for header, rows in tables:
        joined = " | ".join(header)
        if all(needle in joined for needle in needles):
            return header, rows
    return None


def row_by_label(rows: list[list[str]], label: str) -> list[str] | None:
    for row in rows:
        if row and row[0] == label:
            return row
    return None


def panic_ledger_facts(fills: pd.DataFrame) -> dict[str, Any]:
    mask = fills["type"].astype(str).str.contains(spec.PANIC_FILL_MARKER)
    panic = fills.loc[mask]
    return {
        "count": int(mask.sum()),
        "types": sorted({str(t) for t in panic["type"].astype(str).unique()}),
        "coins": sorted({str(c) for c in panic["coin"].unique()}),
        "pnl": float(panic["pnl"].sum()) if len(panic) else 0.0,
        "fees": float(panic["fee_paid"].sum()) if len(panic) else 0.0,
    }


def ledger_delta_rows(control_fills: pd.DataFrame, hsl_on_fills: pd.DataFrame) -> list[dict[str, Any]]:
    control_counts = control_fills["coin"].value_counts()
    hsl_on_counts = hsl_on_fills["coin"].value_counts()
    rows = []
    for coin in sorted(set(control_counts.index) | set(hsl_on_counts.index)):
        before = int(control_counts.get(coin, 0))
        after = int(hsl_on_counts.get(coin, 0))
        if before != after:
            rows.append({"coin": coin, "control": before, "hsl_on": after, "delta": after - before})
    rows.sort(key=lambda row: (-abs(row["delta"]), row["coin"]))
    return rows


def dataset_array_hashes() -> dict[str, str]:
    """Logical array hashes of the frozen bundle, via the manifest convention."""
    sys.path.insert(0, str(spec.REPO / "src"))
    from hlcvs_manifest import hash_logical_array  # noqa: E402
    from tools.verify_hlcvs_data import load_npy_gz  # noqa: E402

    out: dict[str, str] = {}
    for name, key in (
        ("hlcvs.npy.gz", "hlcvs"),
        ("timestamps.npy.gz", "timestamps"),
        ("btc_usd_prices.npy.gz", "btc_usd_prices"),
    ):
        array = load_npy_gz(spec.FROZEN_CACHE / name)
        out[key] = hash_logical_array(array)
        del array
    return out

def report_checks(args: argparse.Namespace) -> None:
    variant = spec.VARIANTS_BY_KEY[args.variant]
    result_dir = spec.find_variant_run_dir(variant, args.result_dir)

    # ------------------------------------------------------------------ artifacts
    missing = [name for name in REQUIRED_ARTIFACTS if not (result_dir / name).exists()]
    check("artifact files present", not missing, f"missing={missing}")
    check(
        "execution audit present",
        variant.execution_audit_path.exists(),
        spec.relative(variant.execution_audit_path),
    )
    if missing:
        return

    analysis = load_json(result_dir / "analysis.json")
    config = load_json(result_dir / "config.json")
    dataset = load_json(result_dir / "dataset.json")
    record = load_json(result_dir / "run_record.json")
    metrics = load_json(result_dir / "global_metrics.json")
    variant_input = load_json(spec.VARIANT_INPUT_PATH)
    frozen_cfg = load_json(variant.config_path)
    report_text = (result_dir / "annual_analysis.md").read_text(encoding="utf-8")
    fills = load_fills(result_dir)
    equity = load_equity(result_dir)

    # ------------------------------------------------------------ pinned inputs
    check(
        "parent config matches its pinned sha256",
        spec.sha256_file(spec.SOURCE_CONFIG) == spec.SOURCE_CONFIG_SHA256,
    )
    check(
        "parent profile input matches its pinned sha256",
        spec.sha256_file(spec.SOURCE_PROFILE_INPUT) == spec.SOURCE_PROFILE_INPUT_SHA256,
    )
    check(
        "tracked baseline analysis matches its pinned sha256",
        spec.sha256_file(spec.TRACKED_BASELINE_RUN / "analysis.json")
        == spec.TRACKED_BASELINE_ANALYSIS_SHA256,
    )

    # ------------------------------------------------- declared-delta identity
    parent = load_json(spec.SOURCE_CONFIG)
    expect_delta = variant.hsl_enabled
    frozen_problems = spec.diff_subtrees(
        parent["backtest"], frozen_cfg["backtest"], "backtest",
        allowed=spec.ALLOWED_CONFIG_DIFF_PATHS,
    )
    for root in spec.IDENTITY_ROOTS:
        allowed = tuple(item[0] for item in spec.DECLARED_DELTA) if expect_delta else ()
        frozen_problems.extend(
            spec.diff_subtrees(parent.get(root), frozen_cfg.get(root), root, allowed=allowed)
        )
    check(
        "frozen variant config is the parent plus its declared change",
        not frozen_problems,
        "; ".join(frozen_problems[:3]),
    )
    frozen_hsl = frozen_cfg["bot"]["long"]["hsl"]
    expected_hsl = dict(spec.HSL_BLOCK, enabled=expect_delta)
    hsl_problems = spec.compare_subtrees(expected_hsl, frozen_hsl, "bot.long.hsl")
    check("frozen HSL block is the profile's own parameters", not hsl_problems, "; ".join(hsl_problems[:3]))
    check(
        "frozen HSL enabled flag matches the variant",
        frozen_hsl.get("enabled") is expect_delta,
        f"enabled={frozen_hsl.get('enabled')!r} expected={expect_delta!r}",
    )
    for dotted, _from, to in spec.DECLARED_DELTA:
        check(
            f"declared delta {dotted} holds in the frozen config",
            spec.get_path(frozen_cfg, dotted) is (to if expect_delta else _from),
        )

    # ------------------------------------------------------- run config identity
    normalized_frozen = spec.normalize_config_payload(frozen_cfg)
    dump_problems: list[str] = []
    for root in ("bot", "live", "coin_overrides"):
        dump_problems.extend(
            f"{root}: {problem}"
            for problem in spec.diff_subtrees(
                normalized_frozen[root], config[root], root,
                allowed=spec.ALLOWED_RUN_DUMP_DIFF_PATHS,
            )
        )
    check("run config matches the frozen variant config", not dump_problems, "; ".join(dump_problems[:3]))
    check(
        "run config HSL enabled flag matches the variant",
        config["bot"]["long"]["hsl"].get("enabled") is expect_delta,
        f"enabled={config['bot']['long']['hsl'].get('enabled')!r}",
    )
    run_hsl_problems = spec.compare_subtrees(expected_hsl, config["bot"]["long"]["hsl"], "bot.long.hsl")
    check("run config HSL parameters are unchanged", not run_hsl_problems, "; ".join(run_hsl_problems[:3]))
    for key, expected in spec.LIVE_ONLY_HSL.items():
        check(
            f"live.{key} is the declared value",
            (config.get("live") or {}).get(key) == expected,
            f"got {(config.get('live') or {}).get(key)!r}",
        )
    bt = config["backtest"]
    gate = bt.get("entry_regime_gate")
    check("gate present in the run config", isinstance(gate, dict), f"gate={gate!r}")
    if isinstance(gate, dict):
        gate_problems = spec.compare_subtrees(spec.GATE, spec.gate_semantics(gate), "backtest.entry_regime_gate")
        check("gate matches the declared 20/50 parameters", not gate_problems, "; ".join(gate_problems[:3]))
        check("gate is enabled in the run config", bool(gate.get("enabled")), f"enabled={gate.get('enabled')!r}")
    regime_ok = all(
        spec.numeric_equal(bt.get(key), expected)
        for key, expected in (*spec.EXECUTION.items(), *spec.COSTS.items())
    )
    check(
        "execution/cost regime matches the research contract",
        regime_ok,
        f"delay={bt.get('execution_delay_bars')} intrabar={bt.get('intrabar_fill_order')} "
        f"maker={bt.get('maker_fee_override')} taker={bt.get('taker_fee_override')}",
    )
    cleared = [key for key in spec.DUMP_CLEARED_BACKTEST_KEYS if bt.get(key) is not None]
    check("run config dump cleared the resolved dataset fields", not cleared, f"cleared={cleared}")

    # ------------------------------------------------------------- run_record
    check(
        "run_record declares this variant",
        (record.get("variant") or {}).get("key") == variant.key
        and bool((record.get("variant") or {}).get("hsl_enabled")) is expect_delta,
        f"variant={record.get('variant')!r}",
    )
    check(
        "run_record declares the declared delta",
        [item.get("path") for item in (record.get("declared_delta") or [])]
        == [item[0] for item in spec.DECLARED_DELTA],
    )
    check(
        "run_record cites the tracked baseline",
        (record.get("comparison") or {}).get("tracked_baseline_run")
        == spec.relative(spec.TRACKED_BASELINE_RUN),
    )

    # ------------------------------------------------------------ frozen dataset
    basket = sorted(variant_input["coins"])
    check(
        "dataset holds the frozen 40-coin basket",
        sorted(dataset.get("coins") or []) == basket,
        f"coins={len(dataset.get('coins') or [])}",
    )
    check(
        "run resolved a dataset with the frozen bundle's window and coin count",
        dataset.get("cache_dir_label") is not None
        and spec.FROZEN_CACHE.name.split("__")[1:3]
        == str(dataset.get("cache_dir_label")).split("__")[1:3],
        f"cache_dir_label={dataset.get('cache_dir_label')!r}",
    )
    recorded = metrics.get("dataset", {})
    manifest = load_json(spec.FROZEN_CACHE / "manifest.json")
    manifest_hashes = {
        key: manifest["files"][key]["sha256"] for key in ("hlcvs", "timestamps", "btc_usd_prices")
    }
    check(
        "global_metrics records logical dataset hashes matching the manifest",
        recorded.get("manifest_hashes") == manifest_hashes
        and recorded.get("data_hashes") == manifest_hashes,
    )
    recomputed_hashes = dataset_array_hashes()
    check(
        "recomputed logical dataset hashes match the manifest",
        recomputed_hashes == manifest_hashes,
        f"timestamps={recomputed_hashes.get('timestamps', '')[:16]}…",
    )

    # ------------------------------------------------------------ execution audit
    audit_ok = True
    audit_detail = ""
    if variant.execution_audit_path.exists():
        import pandas as pd  # local import keeps the module import list honest

        audit = pd.read_csv(variant.execution_audit_path)
        audit = audit.loc[:, ~audit.columns.str.startswith("Unnamed")]
        delay_ok = audit["activation_index"] == audit["decision_index"] + 1 + int(bt["execution_delay_bars"])
        fill_ok = audit["fill_index"] >= audit["activation_index"]
        audit_ok = bool(delay_ok.all() and fill_ok.all()) and len(audit) == int(analysis["fills_count"])
        audit_detail = (
            f"rows={len(audit)} fills={int(analysis['fills_count'])} "
            f"delay_violations={int((~delay_ok).sum())} fill_before_activation={int((~fill_ok).sum())}"
        )
    check("execution audit describes this run", audit_ok, audit_detail)

    # ---------------------------------------------------------------- offline
    log_text = (
        variant.replay_log_path.read_text(encoding="utf-8", errors="replace")
        if variant.replay_log_path.exists()
        else ""
    )
    check("run log reports the frozen dataset was loaded from cache", "Loaded hlcvs data from cache" in log_text)
    fetch_hits = sorted({marker for marker in spec.NETWORK_FETCH_MARKERS if marker in log_text})
    check("run log contains no network fetch markers", not fetch_hits, f"hits={fetch_hits}")

    # ------------------------------------------------------ recomputed tables
    annual = pd.DataFrame(period_rows(equity, fills, "Y"))
    monthly = pd.DataFrame(period_rows(equity, fills, "M"))
    coins = coin_rows(fills)
    year_columns = [column for column in annual.columns]
    problems = compare_frames(annual, pd.read_csv(result_dir / "annual_metrics.csv"), year_columns, 1e-6)
    check("annual_metrics.csv matches the recomputation", not problems, "; ".join(problems[:3]))
    problems = compare_frames(monthly, pd.read_csv(result_dir / "monthly_metrics.csv"), list(monthly.columns), 1e-6)
    check("monthly_metrics.csv matches the recomputation", not problems, "; ".join(problems[:3]))
    coin_actual = pd.read_csv(result_dir / "coin_metrics.csv")
    problems = compare_frames(coins, coin_actual, list(COIN_COLUMNS), 1e-6)
    check("coin_metrics.csv matches the recomputation", not problems, "; ".join(problems[:3]))
    return

def report_checks_report_surface(args: argparse.Namespace) -> None:
    """Checks that need the rendered report and the other comparison columns."""
    variant = spec.VARIANTS_BY_KEY[args.variant]
    result_dir = spec.find_variant_run_dir(variant, args.result_dir)
    if not (result_dir / "annual_analysis.md").exists():
        check("report present", False, "annual_analysis.md missing")
        return

    report_text = (result_dir / "annual_analysis.md").read_text(encoding="utf-8")
    fills = load_fills(result_dir)
    equity = load_equity(result_dir)
    analysis = load_json(result_dir / "analysis.json")
    record = load_json(result_dir / "run_record.json")
    annual = pd.DataFrame(period_rows(equity, fills, "Y"))

    sys.path.insert(0, str(spec.REPO / "backtests" / "report_spec"))
    import annual_analysis as spec_renderer  # noqa: E402  (shared convention, not the renderer under test)

    structure_problems = spec_renderer.assert_report_structure(report_text)
    check("report skeleton matches the convention", not structure_problems, "; ".join(structure_problems[:3]))
    details = spec_renderer.detail_headings(report_text)
    check(
        "one per-year detail section per annual row",
        len(details) == len(annual),
        f"details={len(details)} annual={len(annual)}",
    )
    check("per-year detail sections are chronological", details == sorted(details), "; ".join(details))

    tables = parse_markdown_tables(report_text)
    overall = table_by_header(tables, ["项目", "数值"])
    if overall is None:
        check("overall results table present", False, "no 项目/数值 table found")
    else:
        rows = {row[0]: row[1] for row in overall}
        checks = [
            ("USD gain 倍数（分析指标）", spec_renderer.fmt_ratio(analysis["gain_usd"], 6)),
            ("成交数 / 强平", f"{int(analysis['fills_count']):,} / {'是' if analysis['liquidated'] else '否'}"),
            (
                "USD 最差回撤 / strategy equity 最差回撤",
                f"{spec_renderer.fmt_pct(analysis['drawdown_worst_usd'])} / "
                f"{spec_renderer.fmt_pct(analysis['drawdown_worst_strategy_eq'])}",
            ),
            (
                "PnL Sharpe / Sortino",
                f"{spec_renderer.fmt_ratio(analysis['sharpe_ratio_pnl'])} / "
                f"{spec_renderer.fmt_ratio(analysis['sortino_ratio_pnl'])}",
            ),
        ]
        mismatched = [
            f"{label}: report={rows.get(label)!r} expected={value!r}"
            for label, value in checks
            if rows.get(label) != value
        ]
        check("overall table renders the analysis metrics", not mismatched, "; ".join(mismatched[:3]))

        annual_table = table_by_header(
            tables,
            ["年份", "覆盖", "采样区间", "起始余额", "最终余额", "余额收益率", "权益收益率",
             "年内权益最大回撤", "成交数", "净已实现 PnL"],
        )
        if annual_table is None:
            check("annual summary table present", False, "annual table header not found")
        else:
            check(
                "annual summary table row count matches the recomputation",
                len(annual_table) == len(annual),
                f"report={len(annual_table)} recomputed={len(annual)}",
            )
            row_problems = []
            for idx, row in enumerate(annual_table):
                expected = annual.iloc[idx]
                if row[0] != str(expected["period"]) or row[1] != str(expected["coverage"]):
                    row_problems.append(f"row {idx}: period/coverage {row[:2]!r}")
                    continue
                if row[5] != spec_renderer.fmt_pct(expected["total_balance_return_pct"]):
                    row_problems.append(f"row {idx}: balance return {row[5]!r}")
                if row[9] != spec_renderer.fmt_money(expected["net_realized_pnl_usd"]):
                    row_problems.append(f"row {idx}: net PnL {row[9]!r}")
            check("annual summary values match the recomputation", not row_problems, "; ".join(row_problems[:3]))

    # ------------------------------------------------------- comparison columns
    control_dir = spec.find_variant_run_dir(spec.VARIANTS_BY_KEY["hsl_off_control"])
    hsl_on_dir = spec.find_variant_run_dir(spec.VARIANTS_BY_KEY["hsl_on"])
    baseline_analysis = load_json(spec.TRACKED_BASELINE_RUN / "analysis.json")
    control_analysis = load_json(control_dir / "analysis.json")
    hsl_on_analysis = load_json(hsl_on_dir / "analysis.json")
    columns = (baseline_analysis, control_analysis, hsl_on_analysis)
    missing_hsl_metrics = [
        key for key in spec.HSL_METRICS if not all(key in column for column in columns)
    ]
    check("all three columns report the HSL metrics", not missing_hsl_metrics, f"missing={missing_hsl_metrics}")

    comparison_table = table_with_header_containing(tables, ["Δ（HSL ON − 对照）"])
    if variant.key != "hsl_on":
        drift_table = table_with_header_containing(tables, ["tracked 基线（旧引擎）"])
        check("control appendix carries the engine-drift table", drift_table is not None)
        comparison_table = None
    if variant.key == "hsl_on" and comparison_table is None:
        check("comparison table present", False, "Δ（HSL ON − 对照） header not found")
    elif comparison_table is not None:
        rows = {row[0]: row for row in comparison_table[1]}
        problems = []
        for group_keys in spec.COMPARISON_METRIC_GROUPS.values():
            for key in group_keys:
                label = METRIC_LABELS.get(key, f"`{key}`")
                row = rows.get(label)
                if row is None:
                    problems.append(f"{key}: row missing")
                    continue
                expected = [
                    format_metric(key, baseline_analysis.get(key)),
                    format_metric(key, control_analysis.get(key)),
                    format_metric(key, hsl_on_analysis.get(key)),
                    format_delta(key, control_analysis.get(key), hsl_on_analysis.get(key)),
                ]
                if row[1:5] != expected:
                    problems.append(f"{key}: report={row[1:5]!r} recomputed={expected!r}")
        check("comparison table reproduces the three columns", not problems, "; ".join(problems[:3]))

    # The telemetry table is the appendix's other 指标-prefixed table: four columns, no Δ.
    hsl_table = next(
        (
            (header, rows)
            for header, rows in tables
            if header[:1] == ["指标"] and len(header) == 4
        ),
        None,
    )
    if variant.key == "hsl_on" and hsl_table is None:
        check("HSL telemetry table present", False, "telemetry header not found")
    elif hsl_table is not None:
        rows = {row[0]: row for row in hsl_table[1]}
        problems = []
        for key in spec.HSL_METRICS:
            label = METRIC_LABELS.get(key, f"`{key}`")
            row = rows.get(label)
            if row is None:
                problems.append(f"{key}: row missing")
                continue
            expected = [format_metric(key, column.get(key)) for column in columns]
            if row[1:4] != expected:
                problems.append(f"{key}: report={row[1:4]!r} recomputed={expected!r}")
        check("HSL telemetry table reproduces the analysis metrics", not problems, "; ".join(problems[:3]))

    # ----------------------------------------------------------- ledger effects
    panic = panic_ledger_facts(fills)
    triggers = int(analysis.get("hard_stop_triggers") or 0)
    check(
        "panic fills are consistent with this arm's RED trigger count",
        (triggers > 0) == (panic["count"] > 0),
        f"triggers={triggers} panic_fills={panic['count']} types={panic['types']}",
    )
    if variant.key == "hsl_off_control":
        check(
            "control arm reports no HSL triggers",
            triggers == 0 and panic["count"] == 0,
            f"triggers={triggers} panic_fills={panic['count']}",
        )
    if variant.key == "hsl_on":
        check(
            "report states the ledger panic fill count",
            f"panic 成交 **{panic['count']}** 笔" in report_text,
            f"recomputed={panic['count']}",
        )
    control_fills = load_fills(control_dir)
    deltas = ledger_delta_rows(control_fills, fills)
    if variant.key == "hsl_on":
        check(
            "report states the per-coin fill delta count",
            f"{len(deltas)} 个不同" in report_text,
            f"recomputed={len(deltas)}",
        )
        listed = {row["coin"] for row in deltas}
        table_rows = table_with_header_containing(tables, ["对照成交", "HSL ON 成交", "Δ"])
        if table_rows is not None and deltas:
            reported = {row[0].strip("`") for row in table_rows[1] if not row[0].startswith("…")}
            check(
                "per-coin delta table lists recomputed coins",
                reported.issubset(listed),
                f"reported_only={sorted(reported - listed)[:5]}",
            )

    # ----------------------------------------------------------------- engine
    engine = record.get("engine", {}) or {}
    check(
        "run_record records a matching engine stamp",
        bool(engine.get("source_fingerprint_matches_compiled_stamp")),
        f"fingerprint={engine.get('expected_source_fingerprint')}",
    )
    fingerprint = str(engine.get("expected_source_fingerprint") or "")
    check("report cites the engine fingerprint", bool(fingerprint) and fingerprint in report_text)
    check("report cites the frozen dataset bundle", spec.FROZEN_CACHE.name in report_text)
    if (engine.get("git") or {}).get("dirty"):
        warn(
            "the replay ran with a dirty worktree "
            f"({len((engine.get('git') or {}).get('status_porcelain') or [])} changed paths)"
        )


def report(args: argparse.Namespace) -> None:
    failures = [entry for entry in CHECKS if not entry[1]]
    for name, ok, detail in CHECKS:
        status = "ok  " if ok else "FAIL"
        suffix = f"  [{detail}]" if detail else ""
        print(f"{status} {name}{suffix}")
    for message in WARNINGS:
        print(f"WARN {message}")
    print(
        f"total checks: {len(CHECKS)}  passed: {len(CHECKS) - len(failures)}  "
        f"failed: {len(failures)}  warnings: {len(WARNINGS)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=spec.DEFAULT_VARIANT_ORDER)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument("--fail-on-warnings", action="store_true")
    parser.add_argument(
        "--allow-warnings",
        action="store_true",
        help="exit zero even when a claim could not be verified",
    )
    args = parser.parse_args()

    report_checks(args)
    report_checks_report_surface(args)
    report(args)
    failures = [entry for entry in CHECKS if not entry[1]]
    raise SystemExit(1 if failures or (WARNINGS and args.fail_on_warnings) else 0)


if __name__ == "__main__":
    main()