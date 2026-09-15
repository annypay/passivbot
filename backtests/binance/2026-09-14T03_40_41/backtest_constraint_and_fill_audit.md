# 默认 Trailing Martingale 三年回测：仓位约束与限价成交机制审计

## 结论

**此前的 `annual_analysis.md` 正确记录了配置和绩效摘要，但不足以证明所有运行时约束及成交机制。** 它说明了 41 个候选币、`n_positions = 7`、long TWEL 为 1.5、每币成交时最大 WEL，以及 69,537 笔 maker / 0 笔 taker 的结果；它没有证明同一时刻实际持仓数、没有展示 WEL/TWEL 的逐笔门控与成交后口径，也没有披露严格 OHLC 触价规则及下一根 K 线的 backtest-only peek。

本次对原始 `fills.csv` 按文件记录顺序重放后的结果表明：

1. **41 并非同时持仓。** 41 个是候选池；Binance-only 有效数据为 40 个币，39 个币最终产生过成交。整个三年输出中，任一成交后的非零 long 仓位最多为 **7**，没有发现超过 7 槽的记录。
2. **7 槽是实际生效的正常目标，但不是一般意义上的无条件“历史仓位硬砍线”。** 编排器先保留已管理的非零仓位，再以 forager 填充空槽；因此实盘若接管了超过目标数的历史仓位，策略仍会管理它们。本次从空仓开始的回测输出实际没有超过 7 槽。
3. **WEL/TWEL 被用于发单前的风险规划与 entry 门控，但成交后账本不是“永远不超过名义值”的硬承诺。** 名义单币有效 WEL 为 29.357143%，账本中有 1,038 条成交后记录高于该数，最大为 29.601534%；所有这些记录仍低于单币数量裁剪代码的 1% 容忍参考线 29.650714%。TWEL 账本有 60 条略高于 1.5，最大 1.500168953（高 1.1264 bps）；最大案例可由同一分钟的 entry 手续费先扣、随后以较低余额记录 TWE 精确复核为门控前低于 1.5。
4. **“市场价格低于买入挂单价”只在这个回测模型的明确条件下判为成交。** 对已经存在的 long 买入限价单，必须有该分钟 K 线 `low < order.price`；对 long 卖出平仓限价单，必须有 `high > order.price`。二者都是严格不等式，等于挂单价并不成交。成交价格被直接记为挂单价并收 maker fee。
5. **不能把上述 simulated maker fill 当作 Binance 的成交保证。** 该模型没有订单簿、队列优先级、可成交量、部分成交、点差、网络延迟、撤单竞争或逐笔价格路径；并且订单构造明确获得下一根 1 分钟 K 线的 high/low 作为 backtest-only hint。结果应被视为策略逻辑和 K 线触价假设下的历史模拟，而非可直接复现的实盘成交路径。

本报告只审计现有离线回测工件和本仓库源码：没有重跑回测，没有访问任何交易所账户或凭据，也没有创建、取消或修改订单。原始绩效报告 `annual_analysis.md` 保持不变。

## 范围、输入与重放方法

| 项目 | 审计口径 |
| --- | --- |
| 策略 | `configs/examples/default_trailing_martingale_long.json` 的现有回测有效配置 |
| 数据 | 已有 Binance USDT-M 永续 1 分钟回测工件；有效区间为 2023-09-12 00:01 UTC 至 2026-09-11 23:59 UTC |
| 配置候选 / 有效数据 / 有成交币 | 41 / 40 / 39 |
| 初始资金 / long 风险参数 | 100,000 USDT / `n_positions = 7`、TWEL = 1.5、`we_excess_allowance_pct = 0.37`、`bounded` |
| 原始证据 | `config.json`、`dataset.json`、`fills.csv`、`analysis.json`、既有 `annual_analysis.md` 及 Rust 源码 |
| 支持数据表 | `constraint_fill_audit.csv`；每个数值均可由 `fills.csv` 和配置重算 |

重放时以 `fills.csv` 的**物理行顺序**作为同一时间点内的事件顺序：将每行的 `psize` 写回对应币种，并统计非零 long `psize` 的个数。`index` 列是回测 candle/minute 索引，不是连续的成交流水号；同一 `index` 可以有多笔成交，不能把它当作行号。

“槽位持续时间”取每次成交后的仓位状态，持续至下一次成交时间；累加窗口为 `[2023-09-12 00:01, 2026-09-11 23:59)` UTC，共 1,578,238 分钟。终点没有虚构一根回测结束后的额外分钟。这个方法能准确重建**输出成交事件之间**的持仓数，但不声称拥有逐笔市场事件。

## 原报告体现了什么，未体现什么

| 主题 | 原 `annual_analysis.md` 已体现 | 原报告没有证明的部分 |
| --- | --- | --- |
| 候选范围 | 41 候选、40 个 Binance 有效数据币、39 个有成交币，且指出 MNT 缺数据、GRAM 未成交 | 每次 forager 排名、候选过滤和选中顺序；这次工件没有逐 bar 的 ranking 诊断日志 |
| 槽位 | 配置中的 `n_positions = 7` | 是否真的从未同时持有第 8 个 long；本报告以逐笔状态重放补足这一证据 |
| WEL / TWEL | TWEL = 1.5、逐币成交时最大 WEL 和总敞口图 | 动态 WEL 的分母、entry 裁剪/组合 gate、名义门槛以上的记录数，以及“计划时”与“成交后”口径的差异 |
| 成交 | 69,537 maker、0 taker，以及成交/手续费/PnL 汇总 | `low < buy limit` / `high > sell limit` 的严格规则、同一根 K 线的确定性处理顺序、下一根 K 线 peek 和真实撮合缺失项 |
| 月度收益 | 月度余额、权益回撤、成交和已实现 PnL | 月度表不能从 2025-02 的 `-0.68% / -32.79% / 42.20% / 948` 这类聚合行反推出当时的槽位、订单是否先挂出、或逐笔触价路径 |

因此，原报告可以继续作为**绩效结果说明**，本报告才是对“约束是否实际进入回测”和“成交假设是什么”的补充论证。

## 约束在回测内的执行链

### 1. 候选池不是并发持仓清单

回测每根 K 线调用 Rust orchestrator。编排器首先把已有、受策略管理的非零仓位保持为 active；在 active 数量低于有效目标数时，才以强制 normal 标的和 forager 的候选评分填补空槽。forager 只从启用、可交易且具有所需特征的候选中排名；它不是“41 个币全部开仓”的循环。

回测配置开启 `dynamic_wel_by_tradability = true`。`Backtest::update_n_positions_and_wallet_exposure_limits()` 对可交易候选计数，并使用“截至当前见过的最大可交易数”作为增长型分母；long 有效槽位数为配置值与该分母的较小者。对使用自动 WEL 的币，运行时基础 WEL 为：

```text
runtime base WEL = long total_wallet_exposure_limit / effective_n_positions
```

在本策略的正常 7 槽口径下，基础 WEL 为 `1.5 / 7 = 0.214285714286`（21.428571%）。`bounded` 的 37% allowance 将其扩展为名义有效 WEL `0.293571428571`（29.357143%）。这说明 WEL 是由 TWEL、有效槽位和可交易性共同决定的运行时预算，不是把 41 等分后的 3.66%。

### 2. 槽位输出审计

| 成交后非零 long 数 | 状态持续分钟 | 占重放时间 |
| ---: | ---: | ---: |
| 0 | 71,634 | 4.5389% |
| 1 | 110,906 | 7.0272% |
| 2 | 120,460 | 7.6326% |
| 3 | 132,276 | 8.3812% |
| 4 | 154,185 | 9.7694% |
| 5 | 201,443 | 12.7638% |
| 6 | 326,287 | 20.6741% |
| 7 | 461,047 | 29.2128% |

最大值为 **7**。首次达到 7 槽的是 `fills.csv` 第 55 条记录：2023-09-12 17:18 UTC，INJ 的 `entry_initial_normal_long`，其 candle index 为 37,326。由此可见，策略既不是“全程满 7 槽”，也不是“41 币同时交易”；在约 29.21% 的可重放状态时间内处于 7 槽，其余时间因初始建仓、平仓、轮换、冷却或信号条件而低于该目标。

### 3. 单币 WEL：计划预算、容忍区间与成交后记录

| 指标 | 数值 | 含义 |
| --- | ---: | --- |
| 基础 WEL | 21.428571% | `1.5 / 7` |
| 名义有效 WEL | 29.357143% | 基础 WEL 乘以 `1 + 0.37` |
| 数量裁剪触发参考 | 29.650714% | 名义有效 WEL 的 101%；仅预计 WEL 严格超过此数时触发裁剪 |
| 成交后 WEL 高于基础 WEL | 8,640 条（12.425040%） | allowance 存在时，这不是异常判断依据 |
| 成交后 WEL 高于名义有效 WEL | 1,038 条（1.492730%） | 不能把名义有效 WEL 描述为绝对 post-fill 上限 |
| 成交后 WEL 高于 101% 参考 | 0 条 | 全部记录低于代码的单币裁剪触发参考 |
| 最大成交后 WEL | 29.601534% | ENA，2025-05-16 00:04 UTC，`close_unstuck_long`；比名义有效值高 0.832476% |

超出名义有效 WEL 的 1,038 条记录由 1,010 个 `entry_trailing_cropped_long`、1 个 `entry_trailing_normal_long` 和 27 个 `close_unstuck_long` 组成。最大 entry 记录为 WLD 的 29.420208%，只比名义有效 WEL 高 0.214820%；最大值则出现在 unstuck 减仓之后，说明账本是在成交后的剩余仓位和当时余额上观测，而不是在此处再次把持仓强制压回阈值。

源码中的 `entries::calc_cropped_reentry_qty()` 先计算“若该 entry 完全成交”的 WEL，但只有在它严格大于 `effective_limit * 1.01` 时才裁剪；裁剪后还要遵守数量步长及最小下单量。有效配置中 `position_exposure_enforcer_enabled = false` 且 `total_exposure_enforcer_enabled = false`，因此不存在一个每根 K 线都会把已持仓强制卖回 WEL/TWEL 阈值的自动修复器。

正确表述应为：**WEL 约束会影响 entry 的尺寸与是否继续加仓；它在此配置中带有 1% 预裁剪容忍，并且不是成交后逐时强制清仓上限。**

### 4. 组合 TWEL：entry gate 生效，但账本口径在成交后

配置启用 `total_exposure_entry_gate_enabled = true`。orchestrator 先收集全部候选 entry，基于输入时的余额、已有持仓均价/数量和候选成交价，计算“全部候选 entry 成交后”的总 WEL；若超过 long TWEL（本次为 1.5），会按确定性规则移除或缩小候选订单。该组合 gate 在正常 order construction 路径中调用，而不是仅由 Python 做简化估算。

| 指标 | 审计值 |
| --- | ---: |
| 名义 long TWEL entry cap | 1.500000000000 |
| 成交后 `twe_long > 1.5` 的账本行 | 60（0.086285%） |
| 超额记录的类型 | initial normal 1；trailing cropped 17；trailing normal 42 |
| 最大成交后 `twe_long` | 1.500168952892 |
| 最大超额 | 1.1264 bps（相对名义 cap） |
| 最大值记录 | 2025-06-13 01:21 UTC，RENDER，`entry_trailing_normal_long`，`fills.csv` 第 39,168 条，candle index 957,969 |

`Backtest::process_entry_fill_long()` 的顺序是先按 maker/taker rate 扣 entry fee、更新余额，再更新仓位，并以**更新后的余额**计算 `wallet_exposure` 和 `twe_long` 写入账本。组合 gate 则以订单规划时的单一输入余额做预计成交计算。这两个时间点不同，因此“gate 允许订单”与“每一笔成交后记录严格小于等于 cap”不是同一个命题。

最大值案例可以精确重算这一差异。2025-06-13 01:21 UTC 在同一分钟连续模拟了下表四笔 long entry：

| 顺序 | 币种 | 类型 | 手续费（USDT） | 成交后 `twe_long` |
| ---: | --- | --- | ---: | ---: |
| 1 | ARB | entry trailing normal | -18.3348681012 | 1.323445647595 |
| 2 | INJ | entry trailing cropped | -6.0741500400 | 1.359406597050 |
| 3 | NEAR | entry trailing normal | -18.6254044000 | 1.469681512853 |
| 4 | RENDER | entry trailing normal | -5.1490308800 | 1.500168952892 |

四笔 entry 没有已实现 PnL，合计手续费为 `-48.1834534212` USDT。最终记录余额为 422,473.7996768193 USDT；将最终仓位按同一分钟首笔成交前余额 422,521.9831302405 USDT 重新计价，得到：

```text
1.500168952892 * 422,473.7996768193 / 422,521.9831302405
= 1.499997877011
```

即低于 1.5。此项精确复核解释了**最大**记录的 1.1264 bps 成交后超额来自 entry 手续费记账时序，而非该分钟的组合 entry 规划在手续费前就越过 cap。对其余 59 条记录，本审计没有构造逐订单规划 trace，因此不把它们武断归因为同一单一原因；它们仍应被如实报告为成交后观测超额。

正确表述应为：**TWEL entry gate 在订单规划阶段约束预计组合敞口；本配置并不保证 `fills.csv` 的每个成交后 TWE 永远不高于 1.5。**

## 限价成交：回测规则、时间顺序与边界

### 严格 OHLC 触发条件

`Backtest::order_filled()` 对可交易币种的普通限价订单执行以下判定：

| 订单 | quantity 符号 | 成交条件 | 等于限价 |
| --- | ---: | --- | --- |
| long entry 买入限价单 | `qty > 0` | 当前 1 分钟 K 线 `low < order.price` | `low == price` 不成交 |
| long close 卖出限价单 | `qty < 0` | 当前 1 分钟 K 线 `high > order.price` | `high == price` 不成交 |

`Backtest::order_fill_execution()` 对非市价订单将成交价直接设为 `order.price`、使用 maker fee、标为 `liquidity = "maker"`。本次有效配置中 `market_orders_allowed = false`，账本也确实为 69,537 maker 与 0 taker。

所以问题中的说法需要精确化：

- 若“市场价格低于买入挂单价”具体指**该订单已经挂出期间，某根可交易的 1 分钟 K 线最低价严格低于买入限价**，则它在本回测中被判定为全额成交。
- K 线收盘价、任意时点口头所称的“市场价”都不是代码的直接谓词；必须满足订单已经存在、币种当时可交易，以及 `low < limit`。
- 对 long 平仓，应看 `high > sell limit`，不能把买入规则套用到卖出单。
- 严格 `<` / `>` 使“刚好触及限价”在此模型中不成交；但“触及后低/高出多少、是否有足够成交量、订单在队列中的位置”均没有数据支持。

### 每根 K 线的确定性回测顺序

在 `Backtest::run()` 的第 `k` 根 K 线，回测先检查已经存在的开放订单是否满足该根的成交条件；随后更新 EMA、余额、trailing 状态和运行时预算，最后调用 orchestrator 生成下一轮订单。因此，刚根据第 `k` 根生成的订单不会反向在同一个 `k` 内成交。

`check_for_fills()` 按币种索引依次处理；对**每一个币种**，先收集/处理 close fills，再收集/处理 entry fills。它不是交易所撮合的时间排序。原始账本中有：

| 路径敏感性指标 | 数值 |
| --- | ---: |
| 同一分钟含多笔 simulated fill 的分钟 | 4,927 |
| 单一分钟最多 fill 数 | 7（2023-09-17 20:42 UTC） |
| 同一币种、同一分钟同时含 entry 与 close 的分钟 | 57 |

这 57 个分钟尤其说明，仅知道该分钟的 high/low 无法证明现实中的先后路径；本回测使用上述确定性 close-before-entry 顺序。

### 下一根 K 线 peek

构建 orchestrator 输入时，backtest 会读取 `k + 1` 的 `low` 和 `high`，并放入 `SymbolInput.next_candle`。源码将它明确注释为 **“Backtest-only hint: next candle range for peek fill decisions”**；live 模式应传入 `None`，不会拥有下一根完成 K 线。

trailing-martingale 生成器可以用这个 hint 判断下一根 K 线是否会触及后续阶梯，并为 OHLC 模拟展开相应的 entry/close grid。实际成交仍发生在下一轮对该 candle 的 `order_filled()` 检查中；但“本轮提前知道下一根完整 high/low 来决定布置多深的阶梯”没有实盘等价物。

这不是对 `fills.csv` 的数据错误指控，而是回测模型的明确设计边界：它试图用单根 OHLC 近似可能发生的 intra-bar 连锁成交，但会使结果依赖未来 candle range，不能用来证明实时系统当时能以相同顺序、相同数量把这些网格订单全部挂出并成交。

## 与真实 Binance 永续成交的差异

| 维度 | 本次回测 | 真实交易所 |
| --- | --- | --- |
| 价格输入 | 1 分钟 OHLC；策略 order book 以该 bar close 作为 bid 与 ask | 连续 bid/ask、逐笔成交、盘口深度与价差 |
| 限价成交 | 严格 high/low 穿越即全额，以限价成交 | 需订单已经到达、具备价格优先/时间优先、对手量足够；可能部分成交或不成交 |
| maker 身份 | 非市价订单统一记 maker | GTC limit 可能立即成交而成为 taker；maker 取决于到达时盘口与撮合结果 |
| 多笔同 bar | 固定币种索引和 close-before-entry 顺序 | 由真实时间顺序、订单回报、撤单确认和撮合事件决定 |
| 递归阶梯 | 可使用下一根完成 K 线 high/low 的 backtest-only hint | 下单时无法知道下一根 K 线范围 |
| 延迟与竞争 | 无网络、排队、撤单竞争、成交量约束 | 这些因素都会影响成交率、均价、费用和仓位路径 |

因此，本回测的“全部 maker、挂单价全额成交”是一个**K 线级别的策略评估假设**，不是对真实 Binance 永续限价单成交率、maker 费率资格或滑点的预测。严格不等式避免了仅在限价上精确触碰时的乐观成交，但它不能抵消缺失订单簿和 next-candle peek 对结果产生的路径依赖。

## 可复核源码映射

| 结论 | 直接来源 |
| --- | --- |
| 每根 K 线先成交、后更新状态/预算、再生成订单 | `passivbot-rust/src/backtest.rs`，`Backtest::run()` |
| 动态有效槽位与运行时 WEL | `passivbot-rust/src/backtest.rs`，`update_n_positions_and_wallet_exposure_limits()` |
| 已持仓优先、forager 只填空槽 | `passivbot-rust/src/orchestrator.rs`，`compute_ideal_orders_with_workspace()` 的 active/forager 选择路径 |
| 单币 allowance、1% 裁剪触发、最小下单量约束 | `passivbot-rust/src/entries.rs`，`wallet_exposure_limit_with_allowance*()` 与 `calc_cropped_reentry_qty()` |
| 组合 entry 预计成交后的 TWEL gate | `passivbot-rust/src/orchestrator.rs`，`gate_entries_by_twel_deterministic()` 及其调用点 |
| 买入/卖出严格 OHLC 判定 | `passivbot-rust/src/backtest.rs`，`order_filled()` |
| 非市价订单以限价/maker 记账 | `passivbot-rust/src/backtest.rs`，`order_fill_execution()` |
| close-before-entry 的每币顺序 | `passivbot-rust/src/backtest.rs`，`check_for_fills()` |
| entry 手续费先扣、再记录 WEL/TWE | `passivbot-rust/src/backtest.rs`，`process_entry_fill_long()` |
| 下一根 K 线 backtest-only hint | `passivbot-rust/src/orchestrator.rs`，`SymbolInput.next_candle`；`passivbot-rust/src/backtest.rs` 的 orchestrator input 构造 |

## 最终判断

这份三年回测**确实**使用了 7 槽、运行时 WEL、TWEL entry gate、forager 选择和 Rust 的同一策略/风险编排内核；它不是忽略这些条件的“41 币独立相加”回测。原始成交账本进一步证明，在这次从空仓开始的模拟里实际同时持有的 long 从未超过 7。

但此前年度报告不足以支撑“所有持仓时刻绝不超过 29.357143% 单币 WEL 或 1.5 TWEL”这种更强的说法：单币 entry 逻辑有 1% 容忍和最小量约束，超额 enforcer 又被关闭；而组合账本值在 entry fee 扣除后记录。相应的成交后超额已在本报告和 `constraint_fill_audit.csv` 中量化，不能省略。

最后，`low < buy limit` / `high > sell limit` 是该回测中“有效成交”的定义，而不是 Binance 真实成交的充分证明。尤其是下一根 K 线 peek 与同一分钟多笔成交，使该结果适合用于识别策略参数与 K 线级路径风险，不适合直接承诺实盘将以相同价格、费率、成交数量和回撤轨迹复现。
