#!/usr/bin/env python3
"""Render a bundle's `annual_analysis.md` under the repository report convention.

The convention lives in `docs/ai/runbooks/strategy_report.md` and its
machine-readable form is `backtests/report_spec/annual_analysis.py`. This tool
supplies only the study-specific parts — the `研究附录` fragment — and delegates the
section skeleton, the tables and the data conventions to the shared renderer.

Usage:
    venv/bin/python report_tools/render_annual_report.py --bundle <subdir>
    venv/bin/python report_tools/render_annual_report.py --result-dir <run dir>
        [--run-record <path>] [--appendix <fragment>] [--no-write-csv]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
REPORT_SPEC = REPO / "backtests" / "report_spec"
if str(REPORT_SPEC) not in sys.path:
    sys.path.insert(0, str(REPORT_SPEC))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import annual_analysis as spec  # noqa: E402  (canonical report convention)

STUDY = REPO / "backtests/binance/returns_guarded_dd_research_2026-09-16"
RUNS = STUDY / "cells"
ARTIFACTS = STUDY / "artifacts"
CONTRACT = STUDY / "research_contract.json"

# The three cells the study bundles, in the order the appendix presents them.
FOCUS_CELLS = ("published", "g5_tw090_ddf070", "g4_sma20_50")

FRAMING = """### 本专题的机制框架与边界

- **回撤不是独立的可优化项，而是与收益同源。** 本策略使用成本基础敞口
  (`calc_wallet_exposure = qty_to_cost(size, position.price) / balance`)，
  因此盯市回撤近似满足 `回撤 ≈ TWEL × (1 − 资产价值残留比)`。2025-02 → 2025-04 事件
  实测资产端残留约 0.51、`TWEL = 1.5`，预测 73.5%、实测 73.16%。
- **纯平仓路径调参对盯市回撤没有上界作用。** 下跌中价格处于成本下方，减仓挂单挂不出去，
  浮亏继续累积；平仓参数决定的是回弹时怎么分批走，不是回撤能有多深。
- **日线趋势闸门是生存工具，不是事件保护。** `backtest.entry_regime_gate` 只拦入场，
  绝不影响平仓、panic 与 auto-unstuck。30/60 日交叉滞后于顶部，对两个月内完成的暴跌
  只能事后响应；它的价值在于持续熊市里长期不新开仓。
- **闸门当前仅回测生效。** 该表由 Python 侧按「第 D 日只能看 D−1 日及更早的日线收盘」
  因果构造后传入 Rust 作为只读查表；没有 live 数据路径来重建它，live 调用方保持不设闸门。
- **本 bundle 没有候选锁定，也没有开封任何 holdout 窗口。** 它复现的是研究契约内
  选择窗口上的一个 cell，报告不构成未来收益预测或实盘建议。
"""


def load_json(path: Path) -> Any:
    with Path(path).open() as handle:
        return json.load(handle)


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run_dirs_under(results_base: Path) -> list[Path]:
    root = Path(results_base)
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/binance*/*") if p.is_dir() and p.name[:2].isdigit())


def resolve_run_dir(args: argparse.Namespace) -> Path:
    if args.result_dir:
        return Path(args.result_dir).resolve()
    if not args.bundle:
        raise SystemExit("pass --bundle <subdir> or --result-dir <path>")
    results_base = ARTIFACTS / args.bundle / "backtest_results"
    dirs = run_dirs_under(results_base)
    if not dirs:
        raise SystemExit(f"no run directory under {results_base}")
    if len(dirs) != 1:
        raise SystemExit(
            f"expected exactly one run directory under {results_base}, found {len(dirs)}: "
            f"{[str(path) for path in dirs]}. Pass --result-dir to choose one."
        )
    return dirs[0]


def resolve_run_record(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    if args.run_record:
        return load_json(Path(args.run_record))
    if args.bundle:
        candidate = ARTIFACTS / args.bundle / "run_record.json"
        if candidate.exists():
            return load_json(candidate)
    return {}


def cell_metrics(cell_id: str, window: str, scenario: str) -> dict[str, Any] | None:
    path = RUNS / window / scenario / cell_id / "result.json"
    if not path.exists():
        return None
    return load_json(path).get("metrics")


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "—"
    return f"{number:.{digits}f}"


def fmt_pct(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(value)


def cell_table(record: dict[str, Any]) -> str:
    """Position of this cell inside the study's full-window screen.

    Numbers come from the screening cell's own `result.json`, which is the same
    window, cost regime and universe as this bundle; the bundle's own minute-close
    figures live in 总体结果 above.
    """
    window = record.get("window", {}).get("name", "full")
    scenario = record.get("scenario", "C1_binance_actual")
    cell_id = record.get("cell_id")
    rows: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    focus = list(FOCUS_CELLS)
    if cell_id and cell_id not in focus:
        focus.append(cell_id)
    for focus_id in focus:
        metrics = cell_metrics(focus_id, window, scenario)
        if metrics is not None and focus_id not in seen:
            rows.append((focus_id, metrics))
            seen.add(focus_id)
    if not rows:
        return ""
    lines = [
        "### 本 cell 在专题屏幕中的位置",
        "",
        f"同一窗口（`{window}`）、同一执行与费率口径（`{scenario}`）下的 minute-close 指标，",
        "取自 `cells/<window>/<scenario>/<cell>/result.json`：",
        "",
        "| cell | MDD | CAGR | 期末倍数 | 最长水下（天） | fills |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row_id, metrics in rows:
        marker = "**" if row_id == cell_id else ""
        label = f"{marker}`{row_id}`{marker}"
        lines.append(
            "| {label} | {mdd} | {cagr} | {gain} | {uw} | {fills} |".format(
                label=label,
                mdd=fmt_pct(metrics.get("minute_close_mdd")),
                cagr=fmt_pct(metrics.get("cagr")),
                gain=fmt(metrics.get("gain_strategy_eq"), 3),
                uw=fmt(metrics.get("total_underwater_days"), 1),
                fills=int(metrics.get("fills") or 0),
            )
        )
    return "\n".join(lines) + "\n"


def convention_note(record: dict[str, Any], run_dir: Path) -> str:
    """State where this report's numbers come from, including the sampling difference."""
    cfg = load_json(run_dir / "config.json")
    divider = cfg.get("backtest", {}).get("balance_sample_divider")
    alignment = record.get("alignment") or {}
    lines = [
        "### 口径说明与 bundle 溯源",
        "",
        f"- 报告目录：`{run_dir.relative_to(REPO)}`",
        f"- 屏幕 cell：`{record.get('cell_id')}`，bundle：`{record.get('bundle')}`",
        f"- 生成配置：`{record.get('emitted_config')}`"
        f"（sha256 `{record.get('emitted_config_sha256')}`）",
        f"- 冻结 cell 配置 sha256：`{record.get('cell_config_sha256')}`",
        f"- 研究契约：`{CONTRACT.relative_to(REPO)}`"
        f"（`cell_matrix_sha256` 见该文件）",
        f"- 回测进程退出码：`{record.get('backtest_exit_code')}`，"
        f"artifact 状态：`{record.get('artifact_status')}`，"
        f"绘图：`{record.get('plotting')}`",
        "",
        "**数字口径**：",
        "",
        f"1. `总体结果` 的「最差回撤」取自 `analysis.json`，是**分钟收盘**口径，"
        f"显示为 2 位小数。",
        f"2. `自然年汇总` 与 `月度汇总` 里各期的「年内/月内权益最大回撤」由导出权益序列"
        f"（`balance_sample_divider = {divider}` 分钟）计算。",
        (
            "   该导出序列与 `analysis.json` 同为分钟分辨率，两者可直接比较。"
            if divider == 1
            else "   该导出序列比 `analysis.json` 粗，两套数字不应互相加减。"
        ),
    ]
    if alignment.get("checked"):
        status = "一致" if alignment.get("aligned") else "**不一致**"
        lines.extend(
            [
                "",
                f"- bundle 与屏幕 cell 的指标核对：{status}",
                f"  （比对项 {list((alignment.get('compared') or {}).keys())}，"
                f"差异项 {alignment.get('mismatches') or '无'}）",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "- bundle 与屏幕 cell 的指标核对：未执行"
                f"（`cell_result_found = {alignment.get('cell_result_found')}`）。",
            ]
        )
    return "\n".join(lines) + "\n"


def build_appendix(record: dict[str, Any], run_dir: Path) -> str:
    parts = [FRAMING]
    table = cell_table(record)
    if table:
        parts.append(table)
    parts.append(convention_note(record, run_dir))
    return "\n".join(parts).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", default=None, help="artifacts subdirectory name")
    parser.add_argument("--result-dir", default=None, help="completed run directory")
    parser.add_argument("--run-record", default=None)
    parser.add_argument("--appendix", default=None, help="override the appendix fragment path")
    parser.add_argument("--report-title", default=None)
    parser.add_argument(
        "--write-csv",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write annual/monthly/coin metric CSVs next to the report (default: yes)",
    )
    parser.add_argument("--out", default=None, help="defaults to <result-dir>/annual_analysis.md")
    args = parser.parse_args(argv)

    run_dir = resolve_run_dir(args)
    if not (run_dir / "analysis.json").exists():
        raise SystemExit(f"{run_dir} has no analysis.json; refusing to render a partial report")
    record = resolve_run_record(args, run_dir)

    if args.appendix:
        appendix = Path(args.appendix).read_text(encoding="utf-8")
    elif record:
        appendix = build_appendix(record, run_dir)
    else:
        appendix = FRAMING

    context = spec.build_context_from_artifacts(run_dir, record, appendix)
    # The convention labels a run by its directory. A tracked report never carries the
    # generating host's absolute path, so state it repository-relative, the way the sibling
    # studies' renderers do.
    context["result_label"] = (
        str(run_dir.relative_to(REPO)) if run_dir.is_relative_to(REPO) else str(run_dir)
    )
    if args.report_title:
        context["title"] = args.report_title
    report = spec.render_annual_analysis(context)
    problems = spec.assert_report_structure(report)
    if problems:
        for problem in problems:
            print(f"structure problem: {problem}", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else run_dir / "annual_analysis.md"
    out.write_text(report, encoding="utf-8")
    print(f"wrote {out.relative_to(REPO) if out.is_relative_to(REPO) else out}")
    if args.write_csv:
        tables = context["_tables"]
        tables["annual"].to_csv(run_dir / "annual_metrics.csv", index=False)
        tables["monthly"].to_csv(run_dir / "monthly_metrics.csv", index=False)
        tables["coins"].to_csv(run_dir / "coin_metrics.csv", index=False)
        print(f"wrote annual_metrics.csv ({len(tables['annual'])} rows)")
        print(f"wrote monthly_metrics.csv ({len(tables['monthly'])} rows)")
        print(f"wrote coin_metrics.csv ({len(tables['coins'])} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
