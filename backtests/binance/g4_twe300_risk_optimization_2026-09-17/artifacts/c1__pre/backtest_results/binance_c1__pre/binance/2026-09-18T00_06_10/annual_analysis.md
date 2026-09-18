# 策略深度分析：`g4_account_guard_c1__pre`（Binance 永续 1 分钟 29.54 天回测）

## 口径与范围

- 本 arm：`c1__pre`（杠杆 `c1`，样本外腿（同一冻结 bundle 的 intersection override，窗口在搜索窗之前，搜索与选臂都看不到它））。搜索候选（max_adg）：TWE 2.80 / allowance 0.21 / n_pos 6 / RED 0.30 / EMA 75 / 停 72H
- 声明的改动（相对冻结父配置，逐路径断言，共 10 条）：`backtest.starting_balance` 100000→10000；`bot.long.hsl.enabled` False→True；`live.hsl_signal_mode` 'coin'→'unified'；`live.pnls_max_lookback_days` 30.0→7.0；`bot.long.risk.total_wallet_exposure_limit` 1.0→2.8；`bot.long.risk.n_positions` 7→6；`bot.long.risk.we_excess_allowance_pct` 0.37→0.21；`bot.long.hsl.red_threshold` 0.15→0.3；`bot.long.hsl.ema_span_minutes` 720.0→75.0；`bot.long.hsl.cooldown_minutes_after_red` 2160.0→4320.0。
- 冻结数据集：`caches/hlcvs_data/binance__40_coins__2021-03-25_to_2026-09-13__a02b6ae1c140f2b7`（leg=`pre`，override_mode=`intersection`，manifest config_hash=`a02b6ae1c140f2b77c92d88e4292426dd3e8d4c0c287f2671cd3c9668d639483`）。
- 生效窗口：2021-04-20T00:00:00 → 2023-09-11T00:00:00（`analysis.json` 记录 2021-04-20T00:01:00Z → 2021-05-19T12:54:00Z）。
- 执行与成本契约：`execution_delay_bars`=0、`intrabar_fill_order`=close_first、maker 费率 0.0002、taker 费率 0.0005、`market_orders_allowed`=False。
- 敞口结构：`n_positions`=6、`total_wallet_exposure_limit`=2.8、`we_excess_allowance_pct`=0.21 ⇒ 单槽敞口上界 0.564667。
- 资金与强平契约：起始资金 `backtest.starting_balance`=10,000 USDT（发布 profile 为 100,000）、`backtest.liquidation_threshold`=0.05 ⇒ 引擎强平地板 = 500 USDT；触发时引擎在该根 K 线结束回测。
- HSL：`enabled`=True、作用域 `live.hsl_signal_mode`=`unified`、`red_threshold`=0.3、`ema_span_minutes`=75.0、`restart_after_red_policy`=threshold、`panic_close_order_type`=limit。
- 入场闸门：20/50 日均线闸门保持开启（`block_initial`/`block_reentry` 均为 True），逐币独立判定；本 arm 未改动闸门。
- 采样：余额/权益序列按 `backtest.balance_sample_divider`=60 分钟采样，事件表与冲击矩阵的回撤口径与该分辨率一致；`analysis.json` 的回撤按引擎逐分钟口径计算，两者可能略有差异。
- 成交时间戳是所属 1 分钟 K 线的开盘标签，不是交易所确认的成交时刻；成交方向由 `type` 中的 `long`/`short` 判定。本次 taker 成交 0 笔，maker 成交 400 笔。
- 本 arm 关闭了图像组 `coin_fills`：逐币成交面板是本机内存峰值（约 2.5 GB 增量），关闭后仍保留全部摘要图、全部数据产物与执行审计。
- 离线边界：本 arm 由 `src/backtest.py` 在本地冻结 bundle 上重放，无网络、无凭据、无交易所账户、未启动任何实盘进程；运行日志里没有取数标记。
- 参考锚点：`twe300_10k__3y`（无守护的 TWE 3.0 臂：守护买到的生存与放弃的收益都以它为基准）与 `twe300_10k__ext`（无守护臂在 5.4 年腿上被强平，是本轮所有生存结论的对照）均为已完成 run 的冻结证据，本 arm 不与引擎漂移混淆。
- 有效区间（UTC）：2021-04-20T00:01:00Z 至 2021-05-19T12:54:00Z；回测天数 29.54；数据完成度 3.38%。
- 余额/权益来自每 60 分钟采样序列（`balance_sample_divider=60`，每 60 分钟采样（比 `analysis.json` 粗））；成交、PnL 与手续费按 `fills.csv` 的成交标签归属。
- `fee_paid` 按源文件保留带符号值；净已实现 PnL = `pnl + fee_paid`。非 `entry*` 成交归为减仓/平仓，其中可能包含解套或风险减仓。
- `fills.csv` 的 `timestamp` 是所属 1 分钟 candle 的**开盘标签**，不是交易所确认的精确成交时刻；按时点归属是本报告的报表约定。
- 本报告为历史模拟分析，不能代表未来收益、实际成交价格或流动性。

## 总体结果

| 项目 | 数值 |
| --- | --- |
| 结果目录 | backtests/binance/g4_twe300_risk_optimization_2026-09-17/artifacts/c1__pre/backtest_results/binance_c1__pre/binance/2026-09-18T00_06_10 |
| 数据源 / K 线粒度 | binance / 1 分钟 |
| 有效区间（UTC） | 2021-04-20T00:01:00Z 至 2021-05-19T12:54:00Z |
| 回测天数 / 数据完成度 | 29.54 天 / 3.38% |
| 起始 / 最终 USD 总余额 | 10,000.00 / 10,604.28 USDT |
| 起始 / 最终 USD 总权益 | 10,000.00 / 9,761.69 USDT |
| USD gain 倍数（分析指标） | 0.711828 |
| USD 最差回撤 / strategy equity 最差回撤 | 95.31% / 95.31% |
| 最差 1% 均值回撤 | 95.31% |
| PnL Sharpe / Sortino | 1.5629 / 0.0000 |
| 最长 PnL 峰值恢复期 / 最长持仓 | 24.00 天 / 1.01 天 |
| 成交数 / 强平 | 400 / 是 |


## 多空成交归因

| 方向 | 成交数 | 入场数 | 已实现 PnL | 手续费 | 净已实现 PnL | 成交时最大绝对钱包敞口 |
| --- | --- | --- | --- | --- | --- | --- |
| 多头 | 400 | 299 | 615.58 | -13.40 | 602.18 | 56.47% |
| 空头 | 0 | 0 | 0.00 | 0.00 | 0.00 | n/a |

- 空头成交为 0：`live.approved_coins.short` 为空，本方向**未配置**。该行保留以维持两行结构，数值为 0。
- 全部 400 笔模拟成交均为 maker 成交，taker 成交 0 笔；因此结果对限价单成交假设与 maker 费率敏感，taker 费率不参与本次结果。

## 自然年汇总

| 年份 | 覆盖 | 采样区间 | 起始余额 | 最终余额 | 余额收益率 | 权益收益率 | 年内权益最大回撤 | 成交数 | 净已实现 PnL |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2021 | 起始非完整 | 2021-04-20 00:01 | 10,000.00 | 10,604.28 | 6.04% | -2.38% | 8.10% | 389 | 604.28 |

- 覆盖 0 个完整自然年；首末年为非完整年（共 1 个自然年行）。
- 全期净已实现 PnL = 602.18 USDT（已实现 615.58 + 手续费 -13.40）。

### 2021 年明细（起始非完整）

| 项目 | 数值 |
| --- | --- |
| 采样区间（UTC） | 2021-04-20 00:01:00+00:00 至 2021-05-19 12:01:00+00:00 |
| USD 总余额：起始 → 最终 | 10,000.00 → 10,604.28 |
| USD 总权益：起始 → 最终 | 10,000.00 → 9,761.69 |
| 策略权益：起始 → 最终 | 10,000.00 → 9,761.69 |
| 余额 / 权益 / 策略权益收益率 | 6.04% / -2.38% / -2.38% |
| 年内权益最大回撤 / strategy equity 最大回撤 | 8.10% / 8.10% |
| 成交：总计 / 入场 / 减仓或平仓 | 389 / 288 / 101 |
| 方向：多头 / 空头；maker / taker | 389 / 0；389 / 0 |
| 已实现 PnL / 手续费 / 净已实现 PnL | 615.58 / -11.30 / 604.28 |
| 成交时最大绝对钱包敞口 | 51.59% |
| 产生交易的币种数 | 13 |

## 月度汇总

| 月份 | 覆盖 | 最终余额 | 余额收益率 | 权益收益率 | 月内权益最大回撤 | 成交数 | 净已实现 PnL |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2021-04 | 起始非完整 | 10,000.00 | 0.00% | 0.00% | 0.00% | 0 | 0.00 |
| 2021-05 | 结束非完整 | 10,604.28 | 6.04% | -2.38% | 8.10% | 389 | 604.28 |

- 月内权益最大回撤最大的月份是 2021-05（8.10%），收益最好的月份是 2021-04（0.00%）。
- 正收益月份 0 / 2。

## 按币种贡献

| 币种 | 成交数 | 入场 | 减仓或平仓 | 已实现 PnL | 手续费 | 净已实现 PnL | 成交时最大绝对钱包敞口 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ADA | 53 | 40 | 13 | 191.69 | -2.62 | 189.07 | 33.75% |
| AAVE | 62 | 44 | 18 | 96.10 | -1.48 | 94.62 | 7.14% |
| XLM | 57 | 43 | 14 | 72.19 | -2.16 | 70.03 | 56.47% |
| AVAX | 64 | 47 | 17 | 67.58 | -1.10 | 66.48 | 10.06% |
| SOL | 50 | 38 | 12 | 51.00 | -1.75 | 49.24 | 52.62% |
| XRP | 32 | 26 | 6 | 38.95 | -1.64 | 37.31 | 51.82% |
| DOT | 15 | 12 | 3 | 35.45 | -0.48 | 34.98 | 8.77% |
| ZEC | 17 | 12 | 5 | 25.47 | -0.45 | 25.02 | 8.90% |
| DOGE | 23 | 15 | 8 | 24.78 | -0.30 | 24.47 | 2.28% |
| ATOM | 11 | 8 | 3 | 10.26 | -0.17 | 10.09 | 2.27% |
| BCH | 2 | 1 | 1 | 1.08 | -0.02 | 1.06 | 0.46% |
| ETH | 2 | 1 | 1 | 1.03 | -0.02 | 1.01 | 0.44% |
| LTC | 12 | 12 | 0 | 0.00 | -1.20 | -1.20 | 56.47% |

- 13 个币种产生过成交，其中 12 个净已实现 PnL 为正、1 个为负。
- 净贡献最高 ADA（189.07 USDT），最低 LTC（-1.20 USDT）。
- 逐币种结果受候选资格、上市时间和策略选择影响，不能单独解释为独立策略表现。

## 研究附录

### 声明的改动与身份证明

| 配置路径 | 从 | 到 |
| --- | --- | --- |
| `backtest.starting_balance` | `100000` | `10000` |
| `bot.long.hsl.enabled` | `False` | `True` |
| `live.hsl_signal_mode` | `coin` | `unified` |
| `live.pnls_max_lookback_days` | `30.0` | `7.0` |
| `bot.long.risk.total_wallet_exposure_limit` | `1.0` | `2.8` |
| `bot.long.risk.n_positions` | `7` | `6` |
| `bot.long.risk.we_excess_allowance_pct` | `0.37` | `0.21` |
| `bot.long.hsl.red_threshold` | `0.15` | `0.3` |
| `bot.long.hsl.ema_span_minutes` | `720.0` | `75.0` |
| `bot.long.hsl.cooldown_minutes_after_red` | `2160.0` | `4320.0` |

### 尾部面板

| 指标 | 数值 |
| --- | --- |
| 策略权益增长倍数 `gain_strategy_eq` | 0.711828× |
| 平均日增长 `adg_strategy_eq` | -1.1267% |
| 全期策略权益最差回撤 | 95.31% |
| 最差 1% 均值回撤 | 95.31% |
| 最差 1% 条件期望损失 | 95.1916% |
| 总钱包敞口最大 | 2.341819 |
| 总钱包敞口均值 | 0.017559 |
| 最长策略权益恢复期（天） | 24.00 |
| 平均水下比例 | 3.28% |
| Omega（策略权益） | 0.0463 |
| Sterling（策略权益） | -0.0118 |
| Sortino（策略权益） | -0.0237 |
| 成交笔数 | 400 |
| 有成交的币数 | 13 |
| 回测完成度 | 0.0338 |
| 回测天数 | 29.54 |
| 是否被强平 `liquidated` | True |

### 资本、杠杆与强平几何

| 项目 | 数值 |
| --- | --- |
| 起始资金 USDT | 10,000 |
| 总暴露上限 TWE | 2.80 |
| 单槽敞口上界（含 0.37 超额允许） | 0.564667 |
| 实测总暴露峰值 | 2.341819 |
| 峰值时刻的余额 USDT | 10,602.18 |
| 引擎强平阈值 `liquidation_threshold` | 0.05 |
| 引擎强平地板 USDT | 500 |
| 在实测峰值暴露处的强平触发跌幅 | −40.69% |
| 同样暴露下 10× 全仓保证金的交易所强平跌幅（维护保证金 0.4%） | −42.30% |
| 参考：若打到满仓（TWE 上限）时的交易所强平跌幅 | −35.31% |
| 是否被强平 `liquidated` | True |

- 引擎在 `权益 ≤ 起始资金 × liquidation_threshold` 时置 `liquidated=true`、把末值钉在地板上并**在该根 K 线结束回测**；因此强平 arm 的报告只覆盖到强平之前，其后数值无意义。
- 引擎不模拟保证金占用与分层维持保证金：表中的“交易所强平跌幅”只是以 10× 全仓、维护保证金 0.4% 为例的对照，真实分层保证金与强平费用未建模。

### 强平读数

- **本 arm 被强平**：引擎在权益跌到 500 USDT（起始资金的 0.05）时结束回测，等价于本金的 −95.0%。
- 生效区间 2021-04-20T00:01:00Z → 2021-05-19T12:54:00Z（声明窗口到 2023-09-11）；强平之后的行情与该账户无关。
- 强平前的最差回撤与收益指标只描述“存活期间”，不能与未强平 arm 的全期指标直接比较。

### 暴露上界与爆仓距离（单币/多币归零）

- 声明总暴露上限 `total_wallet_exposure_limit` = 2.80；实测峰值 = 2.341819（上限的 83.6%）。
- 爆仓距离（整篮在该峰值处归零后剩余权益比例）= **0.00%**（TWE>1 时该值恒为 0：净值先被强平地板截断，而不是被“归零”截断）。
- 本 arm 实测最大单币敞口 = 0.564690（XLM），理论单槽上界 `TWEL/n_positions×(1+allowance)` = 0.564667。
- 同时归零的临界币数：达到 −20% 账户损失需 1 个币同时归零，达到 −50% 需 1 个，达到 −80% 需 2 个（按各币实测敞口峰值之和计算，属上界）。
- 在实测峰值暴露处的强平触发跌幅 = **−40.69%**（引擎地板 500 USDT）。

前 5 名单币敞口峰值：

| 币种 | 单币敞口峰值 |
| --- | --- |
| XLM | 0.564690 |
| LTC | 0.564662 |
| SOL | 0.526190 |
| XRP | 0.518179 |
| ADA | 0.337488 |

冲击损失矩阵（损失以“峰值暴露时刻的余额”为基数；全为**上界**，不含路径中的加仓与减仓）：

| 冲击范围 | 价格损失 20% | 价格损失 40% | 价格损失 60% | 价格损失 80% | 价格损失 100% |
| --- | --- | --- | --- | --- | --- |
| 暴露最大的单个币归零 | 11.3%（1,197 USDT） | 22.6%（2,395 USDT） | 33.9%（3,592 USDT） | 45.2%（4,790 USDT） | 56.5%（5,987 USDT） |
| 暴露最大的 3 个币同时归零 | 33.1%（3,510 USDT） | 66.2%（7,021 USDT） | 99.3%（10,531 USDT） | 100.0%（10,602 USDT） | 100.0%（10,602 USDT） |
| 全部 7 个槽位同时归零（峰值之和，上界） | 54.0%（5,727 USDT） | 100.0%（10,602 USDT） | 100.0%（10,602 USDT） | 100.0%（10,602 USDT） | 100.0%（10,602 USDT） |
| 实测总暴露峰值处整篮归零 | 46.8%（4,966 USDT） | 93.7%（9,931 USDT） | 100.0%（10,602 USDT） | 100.0%（10,602 USDT） | 100.0%（10,602 USDT） |

### 事件窗口压力表（基准驱动，非手工挑窗）

- 事件识别规则：BTC 7 日对数收益 ≤ -0.15，相邻间隔 < 30 天合并；共识别 13 个事件，按基准跌幅降序排列。
- 每个事件的测量窗口从基准事件前高点开始，到本 arm 策略权益恢复该高点为止（最长 180 天）。
- 其中 12 个事件在本 arm 的成交账本里没有任何成交（当时闸门关闭或该币池尚未上市），因此事件内回撤为 0 —— 这不是“抗跌”，而是“当时不在场”，两者必须在解读时区分。

| 事件 | 基准击穿窗口 | 基准跌幅 | 本 arm 事件内最大回撤 | 到谷天数 | 到恢复天数 | 水下天数 | 窗口收益 | 暴露峰值（成交样本） | panic 笔数 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2021-05 中国挖矿禁令/五月崩盘（闸门预热期，仅参考） | 2021-04-21 → 2021-06-22 | −52.9% | 8.10% | 29.5 | 未恢复 | 6 | -2.38% | 2.342 | 0 |
| 2022-05 LUNA/UST 崩盘与三箭传染 | 2022-04-11 → 2022-05-14 | −51.8% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 2022-05 LUNA/UST 崩盘与三箭传染 | 2022-06-13 → 2022-06-19 | −39.5% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 未标注事件（2026-02-03 起） | 2026-02-03 → 2026-02-06 | −29.6% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 未标注事件（2021-12-09 起） | 2021-12-09 → 2021-12-09 | −26.5% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 2022-11 FTX 崩盘 | 2022-11-09 → 2022-11-14 | −25.9% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 未标注事件（2022-08-19 起） | 2022-08-19 → 2022-08-19 | −23.1% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 未标注事件（2021-09-13 起） | 2021-09-13 → 2021-09-13 | −22.7% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 2024-08 日元套息平仓 | 2024-08-04 → 2024-08-07 | −20.9% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 2026 近端窗口 | 2026-06-05 → 2026-06-07 | −20.6% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 2025-02 关税冲击与山寨下跌 | 2025-03-09 → 2025-03-09 | −19.0% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 未标注事件（2022-01-21 起） | 2022-01-21 → 2022-01-23 | −18.6% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |
| 未标注事件（2022-02-22 起） | 2022-02-22 → 2022-02-23 | −16.4% | n/a | n/a | 未恢复 | n/a | n/a | 无成交（窗口内无暴露） | 0 |

### 账户守护读数

| 项目 | 数值 |
| --- | --- |
| 作用域 `live.hsl_signal_mode` | unified |
| 清仓+停机阈值（RED） | 0.30 |
| 停加仓阈值（ORANGE） | 0.2250 |
| 停机时长 `cooldown_minutes_after_red` | 4320 分钟 |
| 峰值窗口 `live.pnls_max_lookback_days` | 7.0 天 |
| EMA 跨度 `ema_span_minutes` | 75 分钟 |
| 永久停机阈值 `no_restart_drawdown_threshold` | 1.00 |
| 重启策略 `restart_after_red_policy` | threshold |
| panic 下单类型 | limit |
| 触发次数 `hard_stop_triggers` | 0 |
| 重启次数 `hard_stop_restarts` | 0 |
| 处于 YELLOW / ORANGE / RED 的采样占比 | 0.0118% / 0.0024% / 0.0000% |
| 停机时长（均值 / 最长，分钟） | 0 / 0 |
| panic 平仓实亏合计（USDT） | +0.0000 |
| 停机→重启期间的权益损失 | 0.0000% |
| 重启后再次触发 RED 的比例 | 0.00% |

- 引擎语义：YELLOW 仅遥测；**ORANGE = 整个作用域进入 TpOnly（不产生任何新入场、含加仓，只走止盈路径）**；**RED = 先 Panic 平掉整个作用域，再停机**。因此“熔断”= 实现亏损 + 离场。
- 触发指标是 `min(raw, EMA)`；`no_restart_drawdown_threshold` 用 `max(raw, EMA)` 判定，可被 RAW 尖峰锁存，是更硬的一层。锁存判定发生在**平仓确认那一刻**，而不是触发那一刻，所以一次急跌里“先到 20% 触发、确认时已跌到 40% 以上”会把带终局阈值的臂直接锁死。

### HSL 运行学与熔断代价

- 触发指标是 `min(raw, EMA)`：raw = 1 − 策略净值/滚动峰值，峰值窗口为 `live.pnls_max_lookback_days` = 7.0 天（即回撤以近 7.0 天峰值为基准，不是历史最高点）；EMA 平滑窗口 `ema_span_minutes` = 75.0 分钟。
- 分母口径随作用域变化：`coin` 模式的分母是**槽位预算**（余额 × TWEL/n_positions = 0.466667 × 余额），而 `pside`/`unified` 的分母是**策略净值本身**。同一个 `red_threshold` 在两种口径下代表完全不同的损失水平。
- 生效作用域 `live.hsl_signal_mode` = `unified`，`red_threshold` = 0.3，`restart_after_red_policy` = threshold（`no_restart_drawdown_threshold` = 1），`cooldown_minutes_after_red` = 4320，`panic_close_order_type` = limit。
- 账本侧 panic 成交 0 笔，实亏 +0.00 USDT（手续费 +0.00），涉及 0 个币：无。
- 该机制对**有持续性的**下跌有效；对一天内完成并反弹的插针/跳空，raw 尖峰会被 EMA 平滑掉，因此不能用它当作闪崩保险。

| 指标 | 数值 |
| --- | --- |
| HSL 触发次数 | 0 |
| 多头 HSL 触发次数 | 0 |
| HSL 触发次数/年 | 0.0000 |
| HSL 冷却后重启次数 | 0 |
| 多头 HSL 重启次数 | 0 |
| 处于 YELLOW 的采样占比 | 0.0118% |
| 处于 ORANGE 的采样占比 | 0.0024% |
| 处于 RED 的采样占比 | 0.0000% |
| 停机时长均值（分钟） | 0.00 |
| 停机时长最大值（分钟） | 0.00 |
| 平仓耗时均值（分钟） | 0.00 |
| 触发时回撤分数均值 | 0.0000 |
| panic 平仓损失合计（USDT） | +0.0000 |
| 单次 panic 平仓最大损失（USDT） | +0.0000 |
| panic 损失/回撤 最小 | 0.0000% |
| panic 损失/回撤 均值 | 0.0000% |
| panic 损失/回撤 最大 | 0.0000% |
| 重启后再次触发 RED 的比例 | 0.00% |
| 停机→重启期间权益损失 | 0.0000% |

### 整装待发读数（停机窗口与复牌表现）

- 推导口径：停机窗口 = 紧跟 panic 平仓（≤180 分钟，覆盖约 2 分钟的强平延迟与 60 分钟采样格）之后、权益恒定（容差 1e-6 USDT）且无成交的连续段；结束后有成交 = 完整停机，窗口止于复牌成交；结束后再无成交 = 终局停机（守护锁存），窗口止于回测结束。窗口长度与 `cooldown_minutes_after_red`（终局停机则与 hard_stop_duration_minutes_max）比较，超出部分记为停机结束后等待入场信号的空仓时间。
- 停机次数 0，合计 0.0 小时（占窗口 0.00%）。
- 权益采样间隔 60 分钟：窗口长度与引擎停机时长的比较容差取两个采样格（120 分钟），因此“窗口不短于声明停机时长”是硬校验，超出部分单独记为停机结束后等待入场信号的空仓时间。
- 本 arm 没有推导出停机窗口（未触发守护，或从未在亏损后离场）。

### 暴露几何与占用纪律

| 项目 | 数值 |
| --- | --- |
| 总暴露上限 `total_wallet_exposure_limit` | 2.80 |
| 槽位 `n_positions` | 6 |
| 占用余量 `we_excess_allowance_pct`（原始 / 生效） | 0.21 / 0.21（`bounded` 模式） |
| 均分额度 `TWE / n_positions` | 0.4667 |
| 单槽敞口上限 | 0.5647 |
| 峰值单币敞口（实测） | 0.5647（超出上限 +0.004%） |
| 峰值单币 / 峰值总暴露 | 24.11% |
| 前 3 币峰值之和 | 1.6555 |
| 总暴露（峰值 / 均值） | 2.3418 / 0.0176 |
| 在场币数（时间加权均值 / 峰值） | 0.67 / 6 |
| 空槽时间占比（按均值口径） | 88.91% |
| 有仓位时间占比 | 16.11% |
| 单币完全归零的损失上界 | 56.47% 账户 |
| 峰值暴露处到强平地板的跌幅 | 40.69% |

- 推导口径：几何读数全部来自本 arm 的成交账本、权益序列与冻结配置：单槽上限用引擎的 bounded 模式公式（min(raw, TWE/(TWE/n) − 1)）；在场币数按成交时间戳分段做时间加权，窗口取权益序列首末采样（未持仓的时间计入分母）；单币归零上界 = 单币峰值敞口。单槽上限是“下单规划时”的约束，成交时的 wallet_exposure 会因为盯市漂移与交易所最小下单量（额度被裁到 0 时仍会成交最小可交易数量）而略高于上限，因此该不变量用 5% 的执行余量校验，实际超出比例与造成它的那笔成交记在 observed.per_slot_cap_excess_pct 与 cap_overshoot_fill。
- 读法：**单槽上限是“下单规划时”的约束**（引擎按当时的余额计算），成交账本记录的是成交时的`wallet_exposure`，两者之间有盯市漂移与交易所最小下单量的差，因此本表同时给出实测超出比例；几何不变量（单币敞口 ≤ 上限 × 1.05 + 0.02、总量 ≤ 总暴露上限、在场币数 ≤ `n_positions`）在生成与复核时都校验；本轮实测的最大绝对超出是 0.0191（最紧的上限 TWE 2.5/7 槽），因此绝对余量按 0.02 声明。
- “空槽时间占比”按时间加权的在场币数折算：它衡量的是**未把额度用满的时间比例**，与“是否持仓”是两个不同读数（后者见“有仓位时间占比”）。

### 参数搜索与选择轨迹

| 搜索维度（只含风险几何） | 范围与步长 |
| --- | --- |
| `hsl_cooldown_minutes_after_red` | [720, 4320] 步长 720 |
| `hsl_ema_span_minutes` | [15, 120] 步长 15 |
| `hsl_red_threshold` | [0.15, 0.35] 步长 0.01 |
| `n_positions` | [5, 10] 步长 1 |
| `total_wallet_exposure_limit` | [2, 3] 步长 0.05 |
| `we_excess_allowance_pct` | [0, 0.37] 步长 0.01 |

- 搜索实现：仓库既有 pymoo 优化器（`optimize.backend=pymoo`），搜索腿 `3y`，种子 20260917、`iters`=1024、`population_size`=64、`n_cpus`=2；目标函数 adg_strategy_eq（max）；drawdown_worst_strategy_eq（min）。
- 参考约束（`optimize.limits`）：drawdown_worst_strategy_eq greater_than 0.6；backtest_completion_ratio less_than 0.99。
- 选择规则（看到样本外结果之前执行）：{'completion_min': 0.99, 'drawdown_max': 0.5, 'max_candidates': 6}；候选腿 ['3y', 'ext', 'pre']。
- 钉死不变（`optimize.fixed_params`）：`long_forager_score_weights_ema_readiness`、`long_forager_score_weights_volatility`、`long_forager_score_weights_volume`、`long_forager_volatility_ema_span_1m`、`long_forager_volume_drop_pct`、`long_forager_volume_ema_span_1m` 等共 15 个 alpha 键；`live.hsl_signal_mode`/`live.pnls_max_lookback_days`/`live.max_realized_loss_pct`与短边全部键也不在搜索域内。
- 优化器自身的强制运行时覆盖：优化器默认把两侧 restart_after_red_policy 固定为 always（optimize.fixed_runtime_overrides），本轮保留该默认并在记录里声明
- 冻结的选择文件：`backtests/binance/g4_twe300_risk_optimization_2026-09-17/artifacts/search_selection.json`（sha256 `04b50b488b13b5e4…`，候选 4 个）。
- **本 arm 来自搜索候选**（max_adg）：`hsl_cooldown_minutes_after_red`=4320.0、`hsl_ema_span_minutes`=75.0、`hsl_red_threshold`=0.3、`n_positions`=6、`total_wallet_exposure_limit`=2.8、`we_excess_allowance_pct`=0.21。
- 候选选择时的样本内读数：adg_strategy_eq=0.001797745644869897、backtest_completion_ratio=1.0、drawdown_worst_strategy_eq=0.25014117981359607、liquidated=False。
- 证据来源：`optimize_results/2026-09-17T11_27_42_binance_1096days_40_coins_d2a338b5/pareto/0ef47947cbbc7407b71200508cdaa4300e876ef0713c4a8e3f7794f6f6e3976f.json`（sha256 `e15e45b074dc3f73…`，存在=True）。

### 样本外读数（pre 腿）

- 本 arm 所在腿：**样本外腿（2021-04-20 → 2023-09-11，与搜索窗不重叠）**；窗口 2021-04-20 → 2023-09-11；角色：样本外。
- 本 arm 在该腿上的读数：终值 0.7118×、最差回撤 95.31%、强平=True。
- **本腿是唯一的样本外验收窗**：参数搜索与候选选择只看到 3y 窗（2023-09-12 → 2026-09-12），本腿（2021-04-20 → 2023-09-11）在其中都不可见，所以本腿上的生存/回撤读数是搜索结论的样本外证据。
- 同一条 lever 在三条腿上的并排读数由综合文档给出（`backtests/binance/g4_twe300_risk_optimization_2026-09-17/risk_geometry_analysis.md` →“样本外与跨腿对照”）；本报告只声明本腿的证据等级。

### 边界与诚实声明

- 搜索是**样本内**的：参数只在原生 3 年窗（2023-09-12 → 2026-09-12）上搜索，任何“更优”结论都必须由样本外 pre 腿（2021-04-20 → 2023-09-11）与 ext 腿确认；看到样本外结果后不得再搜索（预注册）。
- 任何账户级止损都挡不住“超过到强平距离”的跳空：满仓 TWE 3.0 到地板只有约 −31.8%、TWE 2.5 约 −40%、TWE 2.0 约 −50%（随暴露峰值变化），所以结构性上限仍是唯一确定性的尾部防线。
- 引擎的冷却时长是单一常量，无法表达“第 N 次触发停更久”的阶梯；本轮用固定档位（12/24/48/72H）为下一轮引擎级阶梯定档，阶梯本身需要独立 PR（Rust + 实盘重建契约 + 测试）。
- `no_restart_drawdown_threshold` 的锁存判定发生在**平仓确认那一刻**、用 `max(raw, EMA)`；上一轮实测一次急跌的确认回撤约 0.47，因此 0.40 会在首击锁死。本轮只测 0.55/0.70，并在报告里核对是否出现首击锁存。
- `live.max_realized_loss_pct < 1` 会拦截**非 panic** 的亏损平仓（panic 豁免，`orchestrator.rs::close_passes_realized_loss_gate`），但 backtest 不导出拦截计数，因此该臂只能报终值/回撤/成交差异，机制归因属于未测量部分。
- 占用几何（在场的币数、空槽时间占比）由成交账本重建，时间权重基于成交时间戳的分段积分，采样分辨率是两笔成交之间的间隔，不是逐分钟；报告给出推导口径。
- 历史范围有限：币池是活到 2026 年的当前 top40（幸存者偏差），长腿 2021-04-20 起交易、当时仅 22 个币有数据；样本外腿只有 2.4 年、5.4 年腿一共只有 5 次守护触发，涉及触发次数的结论必须标注样本量。
- 未建模：真实资金费率、滑点枯竭、API 断连、交易所/稳定币对手方风险；引擎不模拟保证金占用。
- 本报告不是预测；所有数字都来自 `analysis.json`、`fills.csv`、`balance_and_equity.csv.gz` 与其派生文件。

### 对实盘决策的读数

- 该 arm 的历史最差回撤 95.31%、最差 1% 均值回撤 95.31%、收益 0.711828×。
- 敞口上界给出**结构性**保证：单币归零 ≤ 56.47% 账户（理论单槽上界 56.47%），实测总暴露峰值 2.3418 × 总暴露上限 2.80。
- **本 arm 已被强平**：权益跌到 500 USDT 后引擎结束回测。这条证据的含义是：该杠杆/资金组合在本地历史内**不能**靠自身机制活下来，任何硬底线必须先解决“在强平之前介入”的问题。
- 启用的是 `unified` 作用域：组合/账户级熔断会在策略净值回撤达到阈值时清空该作用域并进入冷却，它对有持续性的崩盘有效，对一天内完成并反弹的闪崩无效。本 arm 的实亏代价 +0.00 USDT。

## 可复核数据

| 文件 | 内容 |
| --- | --- |
| annual_analysis.md | 本报告（固定章节骨架 + 研究附录） |
| annual_metrics.csv | 自然年指标表（本地，可由本工具重建） |
| monthly_metrics.csv | 月度指标表（本地，可由本工具重建） |
| coin_metrics.csv | 逐币指标表（本地，可由本工具重建） |
| analysis.json | 引擎原生指标（tracked） |
| fills.csv | 成交账本（本地） |
| balance_and_equity.csv.gz | 余额/权益采样序列（709 行，本地） |
| config.json | 本次 run 的生效配置（本地） |
| dataset.json | 数据集身份（本地） |
| tail_risk_events.json | 事件窗口表（本 arm，tracked） |
| tail_risk_wipeout.json | 暴露上界与冲击损失矩阵（本 arm，tracked） |
| guard_readiness.json | 账户守护停机窗口与复牌读数（本 arm，tracked） |
| run_record.json | 本 arm 的声明改动与数据集身份（tracked） |
| global_metrics.json | 引擎/环境/数据集哈希（tracked） |
| execution_audit.csv | 逐笔执行审计（400 行，本地） |
| fills_plots/ | 逐币成交面板（本 arm 已关闭该图组以控制内存） |

- 冻结父配置：`backtests/binance/g4_sma20_50_replay_2026-09-16/artifacts/g4_sma20_50.config.json`（sha256 `5c8ab5edad9b571cab32a91e82013268ad99ff746a49131cb37bf225b2908bd9`）；本 arm 配置 `backtests/binance/g4_twe300_risk_optimization_2026-09-17/artifacts/g4_c1__pre.config.json`（sha256 `81bdfa9f0c7a4dd4a81c29439286df414c71f5c2aa62983520840f0f60683541`）。
- 数据集身份：`caches/hlcvs_data/binance__40_coins__2021-03-25_to_2026-09-13__a02b6ae1c140f2b7`，manifest config_hash `a02b6ae1c140f2b77c92d88e4292426dd3e8d4c0c287f2671cd3c9668d639483`，`hlcvs` 逻辑哈希 `a5a3a50b5ded74c62f27441132f565e400398fa09f954a468726f4ccfe782dd6`（本次未重算整块 4 GB 数组，full_array_hash=False）。
- 引擎指纹：`70c3ecda8aad9cafe40b80ea75cf52cb5cd730ae8d8d17340c3b2b8d6b2d1a76`；编译戳一致=True；加载戳一致=False。
- 执行审计：`400` 行，与 `analysis.json.fills_count`=400 一致。
- 参考锚点：`twe300_10k__3y`= backtests/binance/g4_twe300_10k_replay_2026-09-17/artifacts/twe300_10k__3y/backtest_results/binance_twe300_10k__3y/binance/2026-09-17T08_18_53（analysis sha256 `58d65d52312db205e544f97b9bbabd944455d141f8001b5df80f3503a208aec4`）；`twe300_10k__ext`= backtests/binance/g4_twe300_10k_replay_2026-09-17/artifacts/twe300_10k__ext/backtest_results/binance_twe300_10k__ext/binance/2026-09-17T08_25_04（analysis sha256 `72be330e8d5cb14ba261447eb51c7509c40c81868ba82c06623e172313495331`）；`twe300_10k_allowance0__3y`= backtests/binance/g4_twe300_10k_replay_2026-09-17/artifacts/twe300_10k_allowance0__3y/backtest_results/binance_twe300_10k_allowance0__3y/binance/2026-09-17T08_23_12（analysis sha256 `c7a55dfc92989ef0f4527e710103b406face13bf968780587230649e153eb482`）；`twe300_10k_allowance0__ext`= backtests/binance/g4_twe300_10k_replay_2026-09-17/artifacts/twe300_10k_allowance0__ext/backtest_results/binance_twe300_10k_allowance0__ext/binance/2026-09-17T08_32_07（analysis sha256 `c07aa89ceefa8d68e0932e425a3a8e823053a1cfe820da263904325d9be4c3aa`）；`g_user12h__3y`= backtests/binance/g4_twe300_account_guard_2026-09-17/artifacts/g_user12h__3y/backtest_results/binance_g_user12h__3y/binance/2026-09-17T09_22_55（analysis sha256 `f6b9fdb9ac1b0a83c36cffb9e4dbf6216ee0162b47f8a2ee57d65ec73ed9e223`）；`g_user12h__ext`= backtests/binance/g4_twe300_account_guard_2026-09-17/artifacts/g_user12h__ext/backtest_results/binance_g_user12h__ext/binance/2026-09-17T09_36_13（analysis sha256 `25f6cb32768f274273851bdb34e88e2f917d4093b6a9be155a1981c424d0d00d`）。
- 研究级综合结论：`backtests/binance/g4_twe300_risk_optimization_2026-09-17/risk_geometry_analysis.md`（跨 arm 对比、事件表、Pareto 取舍与判据裁决）。
- 独立复算：`report_tools/verify_variant_report.py --variant c1__pre`（该脚本不 import 本渲染器，重算事件表、冲击矩阵、守护遥测与停机窗口）。
- 账户守护参数：unified：0.30 清仓+停 72H（橙 0.22 停加仓，7 天窗口，EMA 75 分钟）；峰值窗口 `live.pnls_max_lookback_days`=7.0。
- 暴露几何：TWE 2.8 / `n_positions` 6 / `we_excess_allowance_pct` 0.21 ⇒ 单槽上限 0.5647；占用读数见“暴露几何与占用纪律”。
- 关闭的图像组：coin_fills（口径与范围已声明）。
- 本报告与三张汇总 CSV、`analysis.json` 的一致性由 `report_tools/verify_annual_report.py` 独立复算校验，并检查本规范的固定章节骨架是否完整。

## 结果解读

- 余额从 10,000.00 USDT 增至 10,604.28 USDT；策略权益从 10,000.00 增至 9,761.69 USDT，约增长 -2.38%。分析指标 `gain_usd` 为 0.711828 倍，`gain_strategy_eq` 为 0.711828 倍；该指标使用尾部日度权益，因此不应与采样终点直接等同。
- 分段归因：收益最好的自然年是 2021 年（权益 -2.38%，余额 6.04%）；年内回撤最深的是 2021 年（8.10%）。全期非完整年 1 个，其余为完整年。
- 风险特征：全期 USD 最差回撤 95.31%，strategy equity 最差回撤 95.31%，最差 1% 均值回撤 95.31%；最长 PnL 峰值恢复期 24.00 天，水下时间占比均值 3.28%。PnL Sharpe 1.5629 / Sortino 0.0000，风险调整后的收益质量需要与回撤一并阅读。
- 成交结构：多头 400 笔、空头 0 笔；maker 389 / taker 0。多头净已实现 PnL 602.18 USDT，空头 0.00 USDT。
- 本报告不构成对未来的预测；全部结论只在上述有效区间、执行口径与成本假设下成立。
- 本 arm 的杠杆是 `c1`（搜索候选（max_adg）：TWE 2.80 / 槽位 6 / 占用余量 0.21）：搜索窗内的最优点不一定在崩盘腿上更优——这正是样本外验收窗要回答的问题
- 资金口径：起始资金 10,000 USDT、总暴露上限 2.80、单槽上界 0.564667；引擎强平地板 500 USDT，本 arm 已触发强平。
- 尾部读数的口径是：事件表按基准驱动的事件窗口测量，冲击矩阵按实测敞口峰值给出上界，两者都不含对未来的推断。
