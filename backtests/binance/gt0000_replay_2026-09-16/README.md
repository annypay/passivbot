# g3_cth0000 离线重放：完整 artifact bundle 与深度分析

这个 study 把冻结的回撤研究（`backtests/binance/returns_guarded_dd_research_2026-09-16`，
cell `g3_cth0000`，group `G3_scaleout`，window `full`，scenario `C1_binance_actual`）中
**一个被声明的 cell** 重放成一个**完整的回测 artifact bundle**，并用仓库的策略研究报告约定
（`docs/ai/runbooks/strategy_report.md`，渲染器 `backtests/report_spec/annual_analysis.py`）
渲染出来，使结果可以与 `backtests/binance/2026-09-14T03_40_41/annual_analysis.md` 并排阅读。

study cell 自身只存了一份指标 `result.json`。完整 bundle（`analysis.json`、`fills.csv`、权益
序列、图表、三份汇总 CSV 与一份深度分析报告）需要走标准回测入口，而
`report_tools/run_replay.py` 驱动的正是它。

## 安全边界

仅离线，而且是强制而非声称的：

* 不抓取网络、不用凭据、不连交易所账户、不创建或撤销订单、不启动 bot；运行日志是
  `artifacts/logs/replay_run.log`；
* 重放必须由冻结的 1 分钟 HLCV bundle
  `caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__8300950b42789a26` 提供数据
  ——若产出的 `dataset.json` 指向别的数据集，`report_tools/run_replay.py` 判定该次运行失败，
  并会拿 `manifest.json` 重新校验该 bundle 的逻辑数组哈希；
* 冻结的运行配置就是 study cell 的 `result.json` 里记录的那一份；构建期的闸门证明它等于
  `configs/examples/trailing_martingale_twel100_ddf060.json` 加上恰好那一个被声明的 op，否则
  拒绝写出。

## 所报告的改动（相对 seed profile 的全部 delta）

| 路径 | Seed | 本 artifact |
| --- | --- | --- |
| `bot.long.strategy.trailing_martingale.close.threshold_base_pct` | `-0.0027` | **`0.0`** |

执行/成本 regime：`execution_delay_bars = 0`（T+1）、`intrabar_fill_order = close_first`、
maker `0.0002` / taker `0.0005` —— 即研究契约的 `C1_binance_actual` scenario。

## 引擎身份

本次重放用的是运行时工作区里的引擎。Rust 源码指纹与已编译扩展的 stamp 记录在
`artifacts/backtest_results/binance/<run>/global_metrics.json` 里，并被报告引用。重放运行时
工作区带有未提交改动；这一点被记录下来而不是藏起来，也意味着冻结基线 `2026-09-14T03_40_41`
是由另一个引擎修订产出的（报告在两者对比处说明了这点）。

## 内容清单

| 路径 | 内容 |
| --- | --- |
| `artifacts/g3_cth0000.config.json` | 逐字节一致的运行配置，取自 study cell 的 `result.json` |
| `artifacts/cell_input.json` | 重放必须复现的 study cell 指标，外加契约/seed 哈希 |
| `artifacts/execution_audit.csv` | 流式写出的逐笔成交执行溯源（不跟踪；可再生） |
| `artifacts/logs/replay_run.log` | 本次重放的完整回测日志 |
| `artifacts/backtest_results/binance/<UTC run>/` | artifact bundle：`analysis.json`、`config.json`、`dataset.json`、`fills.csv`、`balance_and_equity.csv.gz`、PNG 图表、`fills_plots/`、`run_record.json`、`global_metrics.json` |
| `artifacts/backtest_results/binance/<UTC run>/annual_analysis.md` | 深度分析报告（固定骨架 + study 附录） |
| `.../annual_metrics.csv`、`.../monthly_metrics.csv`、`.../coin_metrics.csv` | 报告背后的表格 |
| `report_tools/cell_spec.py` | 单一事实来源：路径、所声明的 ops、冻结数据集、契约常量 |
| `report_tools/build_cell_config.py` | 冻结配置并运行 seed 等价闸门 |
| `report_tools/run_replay.py` | 跑回测、强制冻结数据集闸门、写出溯源信息 |
| `report_tools/generate_annual_report.py` | 渲染报告与三份 CSV |
| `report_tools/verify_replay_report.py` | 独立重算与断言检查 |

run 目录由回测自身用 UTC 完成时间戳命名，所以重跑会落到一个新的带日期目录，不会覆盖这一份。

## 复现

```bash
cd <repo root>
venv/bin/python backtests/binance/gt0000_replay_2026-09-16/report_tools/build_cell_config.py
venv/bin/python backtests/binance/gt0000_replay_2026-09-16/report_tools/run_replay.py
venv/bin/python backtests/binance/gt0000_replay_2026-09-16/report_tools/generate_annual_report.py
venv/bin/python backtests/binance/gt0000_replay_2026-09-16/report_tools/verify_replay_report.py
```

不带 `--force` 时 `build_cell_config.py` 拒绝覆盖冻结输入。若要收集一次已经产出过的运行（例如
在一轮很晚才发生的绘图失败之后），给 `run_replay.py` 传 `--reuse-run <timestamp-directory>`。

## 校验

`report_tools/verify_replay_report.py` 用不 import 报告生成器的代码，从 `fills.csv`、
`balance_and_equity.csv.gz`、`execution_audit.csv`、该次运行的 `config.json`/`dataset.json`
与冻结 bundle 重算报告里的每个数字，并检查渲染出的报告确实包含这些值。它还重新实现 bundle 的
逻辑数组哈希，并拿 `manifest.json` 重新哈希一个冻结 artifact。

警告（某条无法完全验证的断言）会被打印，默认不让运行失败；`--fail-on-warnings` 把它们变成
失败。该 study 工具的回归覆盖是 `tests/test_gt0000_replay_report.py`。

已知的端点效应，如实报告而不抹平：权益采样器的最后一行不是回测的最后一分钟，所以少数成交可能
落在最后一个权益采样之后。期间表格止于最后一个采样，报告说明了由此产生的「期间表格净 PnL 合计
与完整成交台账合计」之间的差异。