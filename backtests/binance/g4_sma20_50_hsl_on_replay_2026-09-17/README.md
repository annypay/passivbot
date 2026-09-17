# g4_sma20_50 开启 HSL 的变体重放（含同引擎对照）

这个 study 只回答一个问题：**在已经发布的 g4 门控 profile 上把 HSL（Equity Hard Stop Loss）打开，三年回测成绩会怎样？** 它重放父研究 `g4_sma20_50_replay_2026-09-16` 的冻结配置，只改一个字段——`bot.long.hsl.enabled: false → true`——并把结果与 HSL 关闭的记录逐项比对。

## 为什么要跑对照组

父研究的 tracked 工件是用**更早的引擎修订**产出的（此后 `passivbot-rust/src` 累计 +436 行）。如果直接拿开 HSL 的运行与那份记录比较，任何差异都可能来自引擎而不是 HSL。所以本 study 在**当前引擎**上把未改动的配置也跑一遍，作为配对对照：

| 列 | 配置 | 引擎 | 作用 |
| --- | --- | --- | --- |
| tracked 基线 | 父配置（HSL 关） | 旧修订 | 已发布记录的参照 |
| 本次对照 `hsl_off_control` | 父配置（HSL 关），只改输出目录 | 当前 | 隔离引擎漂移 |
| 被测 `hsl_on` | 父配置 + `bot.long.hsl.enabled=true` | 当前 | 实验组 |

实测结论之一：**对照与 tracked 基线在同口径指标上完全一致**（收益倍数、最差回撤、成交数、恢复期等），说明那 436 行引擎改动没有改变这套配置的回测结果；因此 Δ 列可以读作 HSL 的净效应。

## 方法

- 配置冻结：`report_tools/build_variant_config.py` 校验父配置 sha256（固定为 `5c8ab5edad9b571c…`），再逐字段证明两份变体配置只差「输出目录」（对照）与「输出目录 + 上述一个字段」（被测），并校验 `bot.long.hsl` 其余字段与父配置完全相同。
- 重放：`report_tools/run_variant.py` 调用标准入口 `src/backtest.py`，强制检查：命中冻结 HLCV bundle（无任何网络抓取标记）、币池为冻结 40 币、闸门仍为启用的 20/50、`bot.long.hsl.enabled` 与本变体声明一致、执行审计行数等于成交数。
- 报告：`report_tools/generate_annual_report.py` 用仓库报告规范渲染固定骨架，并在 `## 研究附录` 给出三列对比、HSL 运行学明细、账本级变化、引擎漂移对照、边界与实盘读数。
- 校验：`report_tools/verify_variant_report.py` 不 import 渲染器，从 `fills.csv`、`balance_and_equity.csv.gz`、三份 `analysis.json` 独立复算报告中的表与数字，并重查声明 delta 与冻结数据集哈希。

离线、无凭据、不下单：全程只读本地冻结数据集，未联网。

## 结果（2023-09-12 → 2026-09-12，40 币，long-only，T+1，maker 0.0002 / taker 0.0005）

| 指标 | tracked 基线（旧引擎） | 本次对照（HSL 关） | HSL 开 | Δ（开 − 关） |
| --- | --- | --- | --- | --- |
| 策略权益增长倍数 `gain_strategy_eq` | 2.088645× | 2.088645× | **1.841606×** | **−0.247038×** |
| 全期策略权益最差回撤 | 10.52% | 10.52% | 10.52% | −0.00pp |
| 最差 1% 均值回撤 | 6.85% | 6.85% | 7.31% | +0.46pp |
| 最长策略权益恢复期 | 40.21 天 | 40.21 天 | **114.14 天** | +73.93 天 |
| Sortino（策略权益） | 0.0644 | 0.0644 | 0.0538 | −0.0105 |
| Sharpe（策略权益） | 0.0707 | 0.0707 | 0.0625 | −0.0083 |
| 亏损/盈利比 | 0.1909 | 0.1909 | 0.3228 | +0.1319 |
| 成交笔数 | 28,506 | 28,506 | 28,012 | −494 |
| HSL 触发 / 重启 | 0 / 0 | 0 / 0 | **6 / 6** | +6 / +6 |
| 处于 RED 的采样占比 | 0 | 0 | 0.82% | +0.82pp |
| 停机时长（均值 / 最长） | – | – | **2,160 / 2,160 分钟** | 每次都是完整 36h 冷却 |
| panic 平仓损失合计 | 0 | 0 | **26,376.61 USDT** | +26,376.61 |
| 触发时回撤分数均值 | – | – | 0.2333（阈值 0.15） | – |
| 重启后再次触发 RED | – | – | 0.00% | – |

账本级：HSL 开的那一版有 **6 笔 `close_panic_long`**（覆盖 ZEC×2、AAVE、INJ、ENA、LINK），合计 PnL −26,346.92 USDT、手续费 −29.69 USDT；39 个币里 22 个成交数不同，减少最多的是 AAVE −179、INJ −160、LTC −60、ZEC −46。

**读法**：在本窗口、这套 HSL 参数（coin 模式、`red_threshold=0.15`、`cooldown=2160min`、`no_restart=1`、`panic=limit`）下，开启 HSL **降低了收益、恶化了恢复期与风险调整指标，却没有降低全期最差回撤**（10.52% 不变），代价是 6 次 36 小时停机与约 2.64 万 USDT 的 panic 亏损。之所以没降最差回撤：coin 模式停的是「单个币相对槽位预算 15% 回撤」的局部事件，而组合层面的最差回撤由其它路径造成；触发时的实际回撤分数均值 0.2333 远高于阈值 0.15，说明停机往往发生在币已经深跌之后。

这不是「HSL 无用」的结论：它是**样本内**事实，说明这组参数在历史三年里没有起到保护作用，反而付出了代价。是否上实盘、是否要改阈值/冷却/信号模式（`pside`/`unified`）或改成组合级信号，需要另开变体研究。

## 复现

```bash
bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh              # 完整：冻结 + 两次回放 + 渲染 + 布局检查 + 独立校验
bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh --verify-only  # 只复跑布局检查与独立校验
bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh --force        # 重跑两个变体
bash backtests/binance/g4_sma20_50_hsl_on_replay_2026-09-17/run.sh --variant hsl_on  # 只跑被测臂
```

前置：本地存在冻结数据集 `caches/hlcvs_data/binance__40_coins__2023-08-17_to_2026-09-12__8300950b42789a26`；全程离线。两次回放各约 1.5–2 分钟（对照 93.8s、HSL 开 106.5s，含逐币成交面板）。

## 产物

```
artifacts/g4_sma20_50_hsl_off_control.config.json   # 冻结对照配置（tracked）
artifacts/g4_sma20_50_hsl_on.config.json            # 冻结被测配置（tracked）
artifacts/variant_input.json                        # 声明 delta、固定哈希、对比列与指标清单（tracked）
artifacts/hsl_off_control/backtest_results/binance_control/binance/<UTC 时间戳>/   # 对照 bundle
artifacts/hsl_on/backtest_results/binance_hsl_on/binance/<UTC 时间戳>/             # 被测 bundle
```

每个 bundle 里 `annual_analysis.md` 与 `analysis.json` 随仓库跟踪（可复算报告中的数字）；`fills.csv`、`balance_and_equity.csv.gz`、图表与三张汇总 CSV 属本地可再生产物，见 `backtests/readme.md` 的跟踪规则。

## 边界

- 回测 HSL ≠ 实盘 HSL：重启历史回放、`live.hsl_position_during_cooldown_policy`（冷却期内人工持仓处理）不在回测建模范围内。
- panic 平仓按 `hsl_panic_close_order_type = limit` 的交叉限价模型撮合，实盘用真实盘口与滑点。
- coin 模式触发依赖 `live.pnls_max_lookback_days`（30 天）与 `n_positions`（7）槽位预算；`backtest.dynamic_wel_by_tradability=true` 使分母使用可交易性感知槽位。
- `no_restart_drawdown_threshold = 1` 表示不会永久停机；改小会得到不同的停机/重启行为。
- 单一三年窗口、单交易所、40 币、单一执行与成本口径；「未降低最差回撤」是样本内结论，不是对样本外的承诺。