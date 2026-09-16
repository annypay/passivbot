# `hsl_npos1` 独立回测与深度分析

本目录是 `configs/examples/hsl_npos1.json` 的**独立完整回测工件**与配套的深度分析报告，
报告范式为 `docs/ai/runbooks/strategy_report.md`，冻结样板为
`backtests/binance/2026-09-14T03_40_41/annual_analysis.md`。

## 仓库内此前是否已经回测过这个策略？

**回测过，但都是研究流程的一格，没有任何深度分析报告。** 具体如下：

| 位置 | 窗口 | 口径 | 是否有 `annual_analysis.md` |
|---|---|---|---|
| `backtests/binance/low_drawdown_strategy_study_2026-09-14/selection/hsl_npos1/C1/binance/2026-09-14T08_18_04/` | 2023-09-12 → 2025-09-12（2 年） | 19 币、逐分钟采样、causal T+1 `close_first` | 无 |
| `backtests/binance/low_drawdown_strategy_study_2026-09-14/smoke/hsl_npos1/C1/` | 更短的 smoke | 同上 | 无 |
| `backtests/binance/maxdd_strategy_research_2026-09-14/` | — | 只把该文件当作 `base_config_path` / seed 派生候选 | 无 |
| `backtests/binance/deployability_research_2026-09-15/` | — | 同上（`candidate_configs/*.json` 的 seed） | 无 |

缺口是两条：**(a)** 没有任何一个回测覆盖该 profile 声明的 2021 年起的长窗口；
**(b)** 没有任何符合规范骨架的深度分析报告。本目录补上这两条。

## 回测契约（唯一口径）

| 项 | 值 |
|---|---|
| 源配置 | `configs/examples/hsl_npos1.json`（sha256 `9b3075f2…`，由 `hsl_npos1_spec.SOURCE_CONFIG_SHA256` 门控） |
| 有效区间（实测） | 2021-04-20T00:01:00Z → 2026-09-12T23:59:00Z（1,972.00 天，完成度 100%） |
| 币篮 | 19 币（示例声明 20 币，`XAUT` 被剔除，理由见下） |
| 数据 | binance USDT-M 永续 1 分钟 K 线，缓存 `binance__19_coins__2021-03-25_to_2026-09-13__05e5c8b1f0a3b056` |
| 执行 | `execution_delay_bars=0`（名义因果 T+1）、`intrabar_fill_order=close_first` |
| 成本 | maker `0.0004` / taker `0.00055`；`market_order_slippage_pct=0.0005` |
| 权益 | `balance_sample_divider=1`（逐分钟，与 `analysis.json` 同分辨率）、`btc_collateral_cap=0.0` |
| 绘图 | `backtest.disable_plotting=coin_fills`（只关 per-coin 面板） |

除上表与 `live.approved_coins` 外，`bot` 与 `coin_overrides` 子树与示例逐字段相同；
`build_run_config.py` 在写配置前会校验这一点，并在源文件 sha256 变化时直接失败。

### 为什么剔除 `XAUT`

`XAUT` 是 2026 年才在 Binance USDT-M 上市的代币化黄金永续，本地 K 线目录没有它的历史。
如果在 `live.approved_coins` 里保留它，回测在准备阶段会去解析它的市场元数据并从网络拉取，
再把一段 **2026-03-26 起、由 gap-fill 合成** 的序列写进数据集。这会同时破坏两件事：

1. 本工件的“离线”声明；
2. 读者对“窗口内每个币都可交易”的理解。

因此 `hsl_npos1_spec.EXCLUDED_COINS` 把它显式排除，并同时从 `live.approved_coins` 与
`backtest.coins` 两侧移除；验证器会断言它不在币篮、不在 `live.approved_coins`、
也不在物化后的数据集里。剔除发生在运行之前，不依赖任何回测结果。

## 复现

```bash
# 完整流程：构建配置 → 回测 → 渲染报告 → 独立校验
bash backtests/binance/hsl_npos1_analysis_2026-09-16/run.sh

# 已有工件时只重渲染并校验
bash backtests/binance/hsl_npos1_analysis_2026-09-16/run.sh --report-only
```

单步（等价）：

```bash
PYTHONPATH=src ./venv/bin/python backtests/binance/hsl_npos1_analysis_2026-09-16/report_tools/build_run_config.py
PYTHONPATH=src ./venv/bin/python backtests/binance/hsl_npos1_analysis_2026-09-16/report_tools/run_backtest.py
PYTHONPATH=src ./venv/bin/python backtests/binance/hsl_npos1_analysis_2026-09-16/report_tools/render_hsl_npos1_report.py
PYTHONPATH=src ./venv/bin/python backtests/binance/hsl_npos1_analysis_2026-09-16/report_tools/verify_hsl_npos1.py
```

回归测试：

```bash
./venv/bin/python -m pytest tests/test_hsl_npos1_report.py -q
```

## 安全边界

离线。回测从仓库本地 K 线目录 `caches/ohlcvs` 物化数据集，不下载数据、不使用凭证、
不接触交易所账户、不创建或撤销订单、不启动机器人。

这条声明有**两次教训**，都值得记住：

1. 只要 `live.approved_coins` 里还留着本地没有数据的币种，回测准备阶段就会去解析它的
   市场元数据并联网下载（本目录最初的试运行正因为保留 `XAUT` 而触发了
   `Binance monthly archive fetch`）。因此剔除必须同时作用于 `live.approved_coins`。
2. `run.log` 里可能出现“本次回测之后”的联网记录——例如回测跑完后命令行再做一次市场发现
   时打印的 `Loading markets` / `Fetched ...`。联网步骤与回测**同进程**时才说明回测不干净，
   所以核对时要同时看日志**时间戳**与它在文件中的位置，而不是只 grep 关键词。

## 产物

```
artifacts/
  hsl_npos1.config.json                    冻结的运行配置（含生效币篮与执行键）
  execution_audit.csv                      逐笔执行审计（决策/激活/成交索引）
  logs/run.log                             回测日志
  backtest_results/binance/2026-09-16T08_13_48/
    annual_analysis.md                     本目录的深度分析报告
    annual_metrics.csv / monthly_metrics.csv / coin_metrics.csv
    analysis.json / fills.csv / balance_and_equity.csv.gz / config.json / dataset.json
    *.png                                  权益、回撤、敞口、PnL、硬停图
```

## 报告的核心结论

策略在给它的 1,972 天里只交易了 30 天（2021-04-20 → 2021-05-19），
2021-05-19 的 `close_panic_long` 一次平掉 10 个持仓，
`analysis.json` 记录硬停触发 1 次、重启 0 次；其后 1,942.46 天没有任何成交。
全期 gain `0.825823`、strategy-equity 最差回撤 `47.94%`、
最长 PnL 峰值恢复期 `1,942.62 天` 都由这 30 天加那一次硬停决定。

**这与既有 2 年研究格的结论不冲突、也不能直接相减**：该研究格的窗口起点在硬停事件之后。
这一点在报告的 `### 与既有 hsl_npos1 研究工件的对照` 中有明确标注。
