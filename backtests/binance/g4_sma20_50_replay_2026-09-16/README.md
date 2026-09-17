# g4_sma20_50 重放：已发布的门控 profile

这个 study 只重放一件事并报告它：**已发布的** profile
`configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json`，把它作为离线回测跑在冻结的
三年数据集上，并在旁边渲染出约定要求的完整深度分析。

它之所以存在，是因为
[`returns_guarded_dd_research_2026-09-16`](../returns_guarded_dd_research_2026-09-16/) 的门控
cell `g4_sma20_50` 变成了一个被跟踪的 profile。在那之前，这份配置唯一的 artifact 只是一个
*study cell*：一条结果记录，加上 study 自己树里的一个 bundle。本目录是那个已发布文件的独立
artifact，读者因此可以从磁盘上的 profile 直接走到它的证据，而不必读一份 48 格筛选。

## 这次重放与 study cell 有何不同

重放跑的是已发布的 profile，并且**不重建任何策略配置**：它从不从 seed 加 ops 推导配置，也不
改动任何策略、风险、退出或门控参数。builder 用证明而不是断言来保证这一点：它逐路径比较冻结
配置与所跟踪的 profile，并点名每一处差异。

那些被点名的差异属于**数据身份**，无法避免。已发布的 profile 是一份*运行用*配置：开放式窗口
（`start_date = 2021-04-20`、`end_date = "now"`）、41 个双向 approved 币、两个交易所作为数据
源。而已记录的证据是一份*固定*的三年、单交易所、40 币数据集（`MNT` 在窗口内没有可用历史）。
若忠实地按运行窗口去跑，所需的 warmup 超出冻结 bundle 所持有的数据——它会 miss 缓存并从网络
重建数据集，那既超出本 study 的离线边界，也是悄悄地换了研究对象。重定向如下：

| 键 | 已发布 profile | 本次重放 | 原因 |
| --- | --- | --- | --- |
| `backtest.base_dir` | `backtests` | 本 study 的 `artifacts/backtest_results` | run 目录写在哪里 |
| `backtest.exchanges` | `["binance", "bybit"]` | `["binance"]` | 证据是单交易所的 |
| `backtest.start_date` | `2021-04-20` | `2023-09-12` | 证据的窗口 |
| `backtest.end_date` | `"now"` | `2026-09-12` | 证据的窗口 |
| `backtest.coins` | 未设置（41 个候选） | 冻结的 40 个 | 证据的币池 |
| `backtest.cache_dir` | 未设置 | 冻结 bundle | 使该次运行无法重建或抓取数据 |
| `live.approved_coins` | 41 多 / 41 空 | 40 多 / 0 空 | 证据实际交易的币池 |

门控是**被检查**而不是被假设的，方向与「无门控」守卫相反：`backtest.entry_regime_gate` 必须
存在、必须 `enabled`，且必须逐字段匹配所声明的 20/50 参数。这很关键，因为这份 profile 的无门控
运行是*另一个策略*——最差回撤约 29%，而不是约 10.5%。静默丢失门控的运行会直接失败，而不是产出
一份关于错误策略的报告。

重放还拒绝报告一次走了网络的运行：它要求 run 日志显示冻结 bundle 从缓存载入，且不含任何抓取
标记。

## 产物

| 路径 | 内容 |
| --- | --- |
| `artifacts/g4_sma20_50.config.json` | 本次重放实际运行的冻结配置：已发布 profile，只重定向了上面那些数据身份键。 |
| `artifacts/profile_input.json` | 重放必须复现的 study cell 指标，外加 profile/契约哈希。 |
| `artifacts/execution_audit.csv` | 每笔成交一行，流式记录决策/激活/成交的 bar 序号。 |
| `artifacts/logs/replay_run.log` | 本次运行回测自身的日志。 |
| `artifacts/backtest_results/binance/binance/<UTC timestamp>/` | run 目录：`annual_analysis.md`、三份指标 CSV、`analysis.json`、`fills.csv`、权益序列、图表与逐币成交面板。 |

HLCV 数组绝不复制到这里。该次运行的 `dataset.json` 记录它读了哪个 bundle，而 verifier 用**内容**
证明身份：它重算本次运行 bundle 与源 study bundle 的逻辑数组哈希并要求两者相等，而不是比较缓存
目录名。这个区别并不迂腐——本机在同一数据集的三个字节级相同的别名下持有它，且各自
`config_hash` 名不同（`…__2c11e36fd7fc3806`、`…__8300950b42789a26`、`…__b7430cfe3649eab0`），
数组哈希全都匹配，而加载器可能解析到其中任何一个。名字相等是错误的判据；数组相等才是对的。

## 复现

```bash
bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh
```

该脚本冻结配置、跑重放、渲染报告、按
[`strategy_report.md`](../../../docs/ai/runbooks/strategy_report.md) 的布局契约检查 run 目录，
并独立校验报告。`--verify-only` 重新检查已有 bundle；`--force` 从头重跑。

仅离线。重放从不下载 K 线：它要求冻结 bundle 存在，并且在该次运行解析到别的数据集时中止。

## 如何读这份报告

报告遵循
[`strategy_report.md`](../../../docs/ai/runbooks/strategy_report.md) 的章节骨架，并增加一份
study 附录，写四件骨架里没有位置的事：

1. **所声明的门控**，以及它只能抑制进场这一约束——平仓、panic 与 auto-unstuck 走各自的路径。
2. **与源 study 自身 artifact 的一致性**（同一个 cell）。同一份配置的两次独立重放；此处若出现
   系统性漂移，会推翻其它所有解读。
3. **三列对比**：低尾部 profile（同一策略，无门控）与无门控的默认 profile。
4. **门控究竟改变了什么**，从成交台账读出：进场变少，退出不变，仓位大小不受影响。

离线边界：无网络、无凭据、无交易所账户、无下单、不启动 bot。对外材料必须只凭本仓库即可复现。