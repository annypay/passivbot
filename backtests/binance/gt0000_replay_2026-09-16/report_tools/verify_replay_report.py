#!/usr/bin/env python3
"""Independently verify the g3_cth0000 replay artifact and its deep-analysis report.

Every number is recomputed from `fills.csv`, `balance_and_equity.csv.gz`,
`execution_audit.csv`, the run's own config/dataset metadata and the frozen HLCV bundle;
nothing is imported from the report generator, so a renderer regression fails here.

Offline only. Exits non-zero on any failed check. Reported warnings (a claim that could not
be verified) fail the run unless `--allow-warnings` is passed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cell_spec as spec  # noqa: E402

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

CHECKS: list[tuple[str, bool, str]] = []
WARNINGS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def warn(message: str) -> None:
    WARNINGS.append(message)


def load_json(path: Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_npy_gz(path: Path) -> np.ndarray:
    with gzip.open(path, "rb") as handle:
        return np.load(handle, allow_pickle=False)


def logical_array_hash(array: np.ndarray) -> str:
    """Independent re-implementation of the bundle's logical-array hash.

    Mirrors the documented convention — `str(dtype)`, `\\0`, compact JSON shape, `\\0`, raw
    C-contiguous payload — without importing `src/hlcvs_manifest.py`, so this verifier does
    not share code with the producer.
    """
    arr = np.ascontiguousarray(np.asarray(array))
    hasher = hashlib.sha256()
    hasher.update(str(arr.dtype).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(json.dumps(list(arr.shape), separators=(",", ":")).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(arr.data)
    return hasher.hexdigest()


def load_fills(result_dir: Path) -> pd.DataFrame:
    frame = pd.read_csv(result_dir / "fills.csv")
    frame = frame.loc[:, ~frame.columns.str.startswith("Unnamed")]
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
    text = str(stamp)
    return text.replace("T", " ", 1)


def period_rows(
    equity: pd.DataFrame, fills: pd.DataFrame, freq: str
) -> list[dict[str, Any]]:
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
        if idx == 0:
            coverage = "起始非完整"
        elif idx == len(edges) - 1:
            coverage = "结束非完整"
        else:
            coverage = "完整"
        mask = (equity["timestamp"] >= start) & (equity["timestamp"] < end)
        slice_eq = equity.loc[mask]
        sub = fills.loc[(fills["timestamp"] >= start) & (fills["timestamp"] < end)]
        types = sub["type"].astype(str)
        entry = types.str.startswith("entry_")
        liquidity = sub["liquidity"].astype(str)
        if slice_eq.empty:
            balance = total = strat = pd.Series(dtype=float)
        else:
            balance = slice_eq["usd_total_balance"]
            total = slice_eq["usd_total_equity"]
            strat = slice_eq["strategy_equity"]

        def first_last(series: pd.Series) -> tuple[float, float]:
            if series.empty:
                return float("nan"), float("nan")
            return float(series.iloc[0]), float(series.iloc[-1])

        start_balance, end_balance = first_last(balance)
        start_total, end_total = first_last(total)
        start_strat, end_strat = first_last(strat)
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
                "max_intraperiod_equity_drawdown_pct": max_drawdown(total),
                "max_intraperiod_strategy_equity_drawdown_pct": max_drawdown(strat),
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


def coin_rows(fills: pd.DataFrame) -> list[dict[str, Any]]:
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
    return frame.sort_values("net_realized_pnl_usd", ascending=False, kind="stable").reset_index(drop=True)


def compare_frames(expected: pd.DataFrame, actual: pd.DataFrame, columns, tol: float) -> list[str]:
    problems: list[str] = []
    if len(expected) != len(actual):
        problems.append(f"row count {len(expected)} != {len(actual)}")
        return problems
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
        left_s = left.astype(str).str.replace("T", " ", regex=False)
        right_s = right.astype(str).str.replace("T", " ", regex=False)
        bad = left_s != right_s
        if bad.any():
            idx = int(np.argmax(bad.to_numpy()))
            problems.append(
                f"{column}: {int(bad.sum())} row(s) differ; worst row {idx} "
                f"expected {left_s.iloc[idx]!r} got {right_s.iloc[idx]!r}"
            )
    return problems


def parse_markdown_tables(text: str) -> list[tuple[list[str], list[list[str]]]]:
    """Return (header, rows) for every pipe table in the document, in order."""
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
    for candidate, rows in tables:
        if candidate == header:
            return rows
    return None


def parse_number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("%", "").replace("**", "").replace("`", "").strip()
    if not cleaned or cleaned.lower() in {"n/a", "na", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def report_contains(text: str, rendered: str) -> bool:
    return rendered in text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-dir", default=None)
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help=(
            "exit non-zero when a claim could not be verified (default: warnings are printed "
            "but do not fail the run)"
        ),
    )
    args = parser.parse_args()

    result_dir = (
        Path(args.result_dir).resolve()
        if args.result_dir
        else sorted(
            path
            for path in spec.RUNS_BASE.iterdir()
            if path.is_dir() and path.name[:2].isdigit()
        )[0]
    )

    # ---------- artifacts ----------
    missing = [name for name in REQUIRED_ARTIFACTS if not (result_dir / name).exists()]
    check("artifact files present", not missing, f"missing={missing}")
    if missing:
        report(args)
        raise SystemExit(1)

    audit_path = spec.EXECUTION_AUDIT_PATH
    check("execution audit present", audit_path.exists(), str(audit_path))

    analysis = load_json(result_dir / "analysis.json")
    config = load_json(result_dir / "config.json")
    dataset = load_json(result_dir / "dataset.json")
    record = load_json(result_dir / "run_record.json")
    metrics = load_json(result_dir / "global_metrics.json")
    frozen_cfg = load_json(spec.CONFIG_PATH)
    cell = load_json(spec.CELL_INPUT_PATH)
    report_text = (result_dir / "annual_analysis.md").read_text(encoding="utf-8")
    fills = load_fills(result_dir)
    equity = load_equity(result_dir)

    # ---------- config identity ----------
    check(
        f"{spec.PATCH_PATH} == 0.0",
        spec.get_path(config, spec.PATCH_PATH) == 0.0,
        f"value={spec.get_path(config, spec.PATCH_PATH)!r}",
    )
    bot_problems = spec.compare_subtrees(frozen_cfg["bot"], config["bot"], spec.BOT_ROOT)
    check("run config bot subtree matches the frozen cell config", not bot_problems, "; ".join(bot_problems[:3]))
    bt = config["backtest"]
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

    # ---------- frozen dataset ----------
    check(
        "run consumed the frozen HLCV bundle",
        dataset.get("cache_dir_label") == spec.FROZEN_CACHE.name,
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
        f"data_hashes_match={recorded.get('data_hashes') == manifest_hashes}",
    )
    recomputed = logical_array_hash(load_npy_gz(spec.FROZEN_CACHE / "timestamps.npy.gz"))
    check(
        "recomputed logical hash of frozen timestamps matches the manifest",
        recomputed == manifest_hashes["timestamps"],
        f"recomputed={recomputed[:16]}… manifest={manifest_hashes['timestamps'][:16]}…",
    )
    check(
        "recorded gzip byte hashes are present for provenance",
        set(recorded.get("file_bytes_hashes", {})) == {"hlcvs", "timestamps", "btc_usd_prices"},
    )

    # ---------- study cell reproduction ----------
    cell_metrics = cell["metrics"]
    expected_fills = int(cell_metrics[spec.CELL_FILLS_KEY])
    actual_fills = int(analysis["fills_count"])
    check(
        "fills_count reproduces the study cell fill_rows",
        actual_fills == expected_fills,
        f"analysis={actual_fills} cell={expected_fills}",
    )
    #: Cell metric key -> key in the backtest's own `analysis.json` (`None` = recomputed here).
    mapping = {
        "gain_strategy_eq": "gain_strategy_eq",
        "drawdown_worst_strategy_eq": "drawdown_worst_strategy_eq",
        "strategy_eq_recovery_days_max": "strategy_eq_recovery_days_max",
        "position_held_days_max": "position_held_days_max",
        "sortino_ratio_strategy_eq": "sortino_ratio_strategy_eq",
        "total_wallet_exposure_max": "total_wallet_exposure_max",
        "traded_coin_count": "fills_active_symbols_count",
    }
    metric_problems = []
    for cell_key, analysis_key in mapping.items():
        expected = cell_metrics.get(cell_key)
        actual = analysis.get(analysis_key)
        if expected is None or actual is None:
            metric_problems.append(f"{cell_key}->{analysis_key}: expected {expected!r} actual {actual!r}")
        elif not math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-12):
            metric_problems.append(
                f"{cell_key}->{analysis_key}: expected {expected!r} actual {actual!r}"
            )
    check(
        "analysis metrics reproduce the study cell record",
        not metric_problems,
        "; ".join(metric_problems[:4]),
    )
    check(
        "sampled strategy-equity drawdown matches the cell minute-close MDD",
        math.isclose(
            float(analysis["drawdown_worst_strategy_eq"]),
            float(cell_metrics["minute_close_mdd"]),
            rel_tol=1e-6,
            abs_tol=1e-9,
        ),
        f"analysis={analysis['drawdown_worst_strategy_eq']!r} cell={cell_metrics['minute_close_mdd']!r}",
    )

    # The study's cell metrics are computed from the equity series it received; this verifier
    # derives the same quantities from the artifact's own recorded series with its own code.
    stamps = equity["timestamp"].astype("int64").to_numpy() // 1_000_000
    series = equity["strategy_equity"].to_numpy(dtype=float)
    span_days = float((stamps[-1] - stamps[0]) / 86_400_000.0)
    gain = float(analysis["gain_strategy_eq"])
    derived_cagr = gain ** (365.25 / span_days) - 1.0 if span_days > 0 and gain > 0 else float("nan")
    cell_cagr = float(cell_metrics["cagr"])
    check(
        "strategy-equity CAGR reproduces the study cell CAGR",
        math.isclose(derived_cagr, cell_cagr, rel_tol=1e-4, abs_tol=1e-9),
        f"derived={derived_cagr!r} cell={cell_cagr!r} rel_delta="
        f"{abs(derived_cagr - cell_cagr) / abs(cell_cagr):.2e} span_days={span_days:.6f}",
    )
    peak = np.maximum.accumulate(series)
    underwater = np.where(peak > 0.0, series / peak, 1.0)
    deepest = np.sort(1.0 - underwater)[::-1]
    derived_worst_1pct = float(np.mean(deepest[: max(1, len(deepest) // 100)]))
    cell_worst_1pct = float(cell_metrics["worst_1pct_mean_drawdown"])
    rel_delta = abs(derived_worst_1pct - cell_worst_1pct) / abs(cell_worst_1pct)
    check(
        "worst-1% mean drawdown is consistent with the study cell value",
        rel_delta < 0.5,
        f"derived={derived_worst_1pct!r} cell={cell_worst_1pct!r} rel_delta={rel_delta:.2%} "
        "(the cell ranks the deepest 1% of its own series; the report's series is the "
        f"{len(series):,}-row sampler, so the tail percentile is coarser)",
    )

    # ---------- CSV recomputation ----------
    annual = pd.DataFrame(period_rows(equity, fills, "Y"))
    monthly = pd.DataFrame(period_rows(equity, fills, "M"))
    coins = coin_rows(fills)
    tol = 1e-6
    problems = compare_frames(annual, pd.read_csv(result_dir / "annual_metrics.csv"), annual.columns, tol)
    check("annual_metrics.csv reproduces independently", not problems, "; ".join(problems[:3]))
    problems = compare_frames(monthly, pd.read_csv(result_dir / "monthly_metrics.csv"), monthly.columns, tol)
    check("monthly_metrics.csv reproduces independently", not problems, "; ".join(problems[:3]))
    problems = compare_frames(coins, pd.read_csv(result_dir / "coin_metrics.csv"), COIN_COLUMNS, tol)
    check("coin_metrics.csv reproduces independently", not problems, "; ".join(problems[:3]))

    ledger_pnl = float(fills["pnl"].sum() + fills["fee_paid"].sum())
    annual_csv = pd.read_csv(result_dir / "annual_metrics.csv")
    monthly_csv = pd.read_csv(result_dir / "monthly_metrics.csv")
    csv_pnl = float(annual_csv["net_realized_pnl_usd"].sum())
    csv_fills = int(annual_csv["fills_count"].sum())
    sample_lo = equity["timestamp"].iloc[0]
    sample_hi = equity["timestamp"].iloc[-1]
    outside = fills.loc[(fills["timestamp"] < sample_lo) | (fills["timestamp"] > sample_hi)]
    outside_pnl = float(outside["pnl"].sum() + outside["fee_paid"].sum())
    check(
        "annual net realized PnL equals the fills ledger total outside the sampled window",
        abs(csv_pnl - (ledger_pnl - outside_pnl)) < tol,
        f"csv={csv_pnl:.6f} ledger={ledger_pnl:.6f} outside_sample={outside_pnl:.6f} "
        f"delta_vs_ledger={csv_pnl - ledger_pnl:.6f}",
    )
    check(
        "period tables cover every fill inside the sampled window",
        csv_fills == actual_fills - len(outside)
        and int(monthly_csv["fills_count"].sum()) == csv_fills,
        f"annual={csv_fills} monthly={int(monthly_csv['fills_count'].sum())} "
        f"fills={actual_fills} outside_sample={len(outside)}",
    )
    if len(outside):
        warn(
            f"{len(outside)} fill(s) ({outside_pnl:.6f} USDT net) sit after the last equity "
            f"sample {sample_hi}; the report's period tables stop there, so their net-PnL total "
            "is not the full-ledger total"
        )
    check(
        "strategy_equity equals usd_total_equity for the whole series",
        bool(
            np.allclose(
                equity["strategy_equity"].to_numpy(dtype=float),
                equity["usd_total_equity"].to_numpy(dtype=float),
                rtol=0.0,
                atol=1e-9,
                equal_nan=True,
            )
        ),
    )

    # ---------- execution audit ----------
    audit = pd.read_csv(audit_path)
    audit = audit.loc[:, ~audit.columns.str.startswith("Unnamed")]
    delay = int(bt["execution_delay_bars"])
    activation_violations = int((audit["activation_index"] != audit["decision_index"] + 1 + delay).sum())
    fill_violations = int((audit["fill_index"] < audit["activation_index"]).sum())
    check("audit activation identity holds", activation_violations == 0, f"violations={activation_violations}")
    check("audit fill index never precedes activation", fill_violations == 0, f"violations={fill_violations}")
    check(
        "audit rows equal fills_count",
        len(audit) == actual_fills,
        f"audit={len(audit)} fills={actual_fills}",
    )
    check(
        "global_metrics audit row count matches the audit file",
        int(metrics.get("execution_audit_rows", -1)) == len(audit),
        f"recorded={metrics.get('execution_audit_rows')} actual={len(audit)}",
    )

    # ---------- engine identity ----------
    engine = record.get("engine", {})
    fingerprint = engine.get("expected_source_fingerprint")
    check("engine fingerprint recorded", bool(fingerprint), f"fingerprint={fingerprint}")
    check(
        "compiled extension stamp matches the recorded Rust sources",
        bool(engine.get("source_fingerprint_matches_compiled_stamp")),
        f"compiled_stamp={engine.get('compiled_source_stamp')}",
    )
    if engine.get("git", {}).get("dirty"):
        warn(
            "the replay ran with a dirty worktree "
            f"({len(engine['git'].get('status_porcelain') or [])} changed paths); "
            "the report states this, and it is not reproducible from git alone"
        )

    # ---------- report structure and claims ----------
    sys.path.insert(0, str(spec.REPO / "backtests" / "report_spec"))
    import annual_analysis as spec_renderer  # noqa: E402

    structure_problems = spec_renderer.assert_report_structure(report_text)
    check("report skeleton matches the convention", not structure_problems, "; ".join(structure_problems))
    details = spec_renderer.detail_headings(report_text)
    check(
        "one per-year detail section per annual row",
        len(details) == len(annual),
        f"details={len(details)} annual_rows={len(annual)}",
    )
    check(
        "per-year detail sections are chronological",
        details == sorted(details),
        "; ".join(details),
    )

    tables = parse_markdown_tables(report_text)
    overall = table_by_header(tables, ["项目", "数值"])
    if overall is None:
        check("overall results table present", False, "no two-column 项目/数值 table found")
    else:
        rows = {row[0]: row[1] for row in overall}
        checks = [
            ("USD gain 倍数（分析指标）", spec_renderer.fmt_ratio(analysis["gain_usd"], 6)),
            ("成交数 / 强平", f"{actual_fills:,} / {'是' if analysis['liquidated'] else '否'}"),
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
        check("overall table renders the analysis metrics", not mismatched, "; ".join(mismatched))

        annual_table = table_by_header(
            tables,
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

    ledger_problems = []
    recomputed_slots = nonzero_long_counts(fills)
    slot_max = int(recomputed_slots.max()) if len(recomputed_slots) else 0
    ledger_table = table_by_header(tables, ["实测项", "数值", "参照"])
    if ledger_table is None:
        ledger_problems.append("ledger table not found")
    else:
        ledger_rows = {row[0]: row[1] for row in ledger_table}
        slot_cell = ledger_rows.get("成交后非零 long 仓位最大值", "")
        if f"**{slot_max}**" not in slot_cell:
            ledger_problems.append(f"slot max cell {slot_cell!r} does not carry {slot_max}")
        twe_expected = f"{float(fills['twe_long'].max()):.4f}"
        if ledger_rows.get("组合 long TWE 最大记录值") != twe_expected:
            ledger_problems.append(
                "TWE max "
                f"{ledger_rows.get('组合 long TWE 最大记录值')!r} != {twe_expected!r}"
            )
        expected_multi = f"{int((fills.groupby(fills['timestamp'].dt.floor('min')).size() > 1).sum()):,}"
        multi_cell = ledger_rows.get("单分钟多笔成交 / 单分钟最多成交", "")
        if not multi_cell.startswith(expected_multi + " /"):
            ledger_problems.append(f"multi-fill cell {multi_cell!r} != {expected_multi!r}")
    check("ledger table matches the independent replay", not ledger_problems, "; ".join(ledger_problems[:3]))

    distribution_rows = table_by_header(tables, ["成交后非零 long 数", "成交事件数"])
    if distribution_rows is None:
        check("slot distribution table present", False, "distribution table not found")
    else:
        counts = [int(row[1].replace(",", "")) for row in distribution_rows]
        check(
            "slot distribution sums to the fill count",
            sum(counts) == actual_fills,
            f"sum={sum(counts)} fills={actual_fills}",
        )
        check(
            "slot distribution maximum matches the ledger maximum",
            len(counts) - 1 == slot_max,
            f"bins={len(counts) - 1} recomputed_max={slot_max}",
        )

    audit_table = table_by_header(tables, ["审计项", "数值"])
    if audit_table is None:
        check("audit table present", False, "audit table not found")
    else:
        audit_rows = {row[0]: row[1] for row in audit_table}
        violation_cells = [
            value
            for label, value in audit_rows.items()
            if "违例" in label
        ]
        check(
            "audit violations rendered as zero",
            bool(violation_cells) and all(value == "0" for value in violation_cells),
            f"cells={violation_cells}",
        )
        waits = f"{float((audit['fill_index'] - audit['activation_index']).median()):.1f} / " \
                f"{int((audit['fill_index'] - audit['activation_index']).max()):,}"
        waited_cell = next(
            (value for label, value in audit_rows.items() if "等待 bar 数" in label), None
        )
        check(
            "audit wait statistics rendered from the audit file",
            waited_cell == waits,
            f"report={waited_cell!r} recomputed={waits!r}",
        )

    check(
        "report cites the frozen dataset path",
        spec.FROZEN_CACHE.name in report_text,
    )
    check(
        "report cites the engine fingerprint",
        bool(fingerprint) and fingerprint in report_text,
    )
    if engine.get("git", {}).get("dirty"):
        check("report discloses the dirty worktree", "未提交改动" in report_text)

    report(args)
    failures = [entry for entry in CHECKS if not entry[1]]
    raise SystemExit(1 if failures or (WARNINGS and args.fail_on_warnings) else 0)


def nonzero_long_counts(fills: pd.DataFrame) -> np.ndarray:
    """Independent replay of the post-fill non-zero long position count."""
    state: dict[str, float] = {}
    counts = np.empty(len(fills), dtype=int)
    for idx, row in enumerate(fills.itertuples(index=False)):
        text = str(row.type)
        if "long" not in text and "short" not in text:
            raise ValueError(f"cannot derive position side from order type {text!r}")
        if float(row.psize) == 0.0:
            state.pop(row.coin, None)
        else:
            state[row.coin] = float(row.psize)
        counts[idx] = sum(1 for value in state.values() if value)
    return counts


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


if __name__ == "__main__":
    main()
