# 默认 Trailing Martingale 三年回测：前视、数据泄露与蜡烛边界审计

## 审计结论

**这份回测不能被认定为严格“无前视、可逐笔复现实盘”的证据。** 但需要把不同问题严格分开：

1. **没有发现普通订单的同 candle 信号回填。** 对于已经在 candle `k` 开始前存在的订单，回测先以 `k` 的高低价检查成交；然后才使用完成的 `k` candle 更新 EMA、成交量、波动率和 trailing 状态，再构造下一轮订单。因而“用 candle `k` 生成的新信号，又拿 `k` 的 HL 判定自己成交”的普通流程不存在。
2. **存在明确的 `k+1` 未来 candle 读取。** backtest 将下一根 candle 的 low/high 放进 `next_candle`，而 `trailing_martingale` 会据此选择生成一阶还是完整递归网格。这违反了最严格的“策略输入只可使用决策时已知信息”标准。
3. **该 future read 的净 PnL 影响尚未由现有工件证明。** live 路径传入 `next_candle = None` 时默认生成完整网格；backtest 则在判断下一根会触及网格时生成完整网格、否则只生成最邻近订单。这个设计意在避免为不会触及的阶梯保留无关订单，且在单调阶梯和理想撮合模型下可能与 live full-grid 结果等价。可是结果目录没有逐 bar 理想订单快照，也没有 no-peek 对照回测，不能把“设计上可能等价”写成“已证明没有收益影响”。
4. **更直接的实盘复现风险在 candle 边界。** 信号使用 candle `k` 的完整 OHLC，只能在该 candle 收盘后产生；回测却将该订单视为可覆盖完整的 `k+1` candle。现实中订单必须先经网络传输、交易所接受和订单簿排队。只用 OHLC 时，`T+1` 是零延迟的乐观下界；`T+2` 是排除下一根 candle 内下单时点不明的一种保守压力情景，并非声称实盘必然延迟两分钟。
5. **同根 K 线的 high/low 先后并不由 OHLC 给出。** 本次输出有 4,927 个分钟包含多笔 simulated fill，其中 57 个分钟同一币种同时有 entry 与 close。回测固定按币种索引，且对每个币先处理 close、再处理 entry；这是一种确定性模拟顺序，不是历史交易所事件顺序。
6. **特征本身没有发现直接未来收益泄露。** 1 分钟 EMA、forager 的 quote-volume、log-range 和策略波动率都由 `k` 的完成 candle 更新；小时波动率在整点只聚合到 `k-1`。本次标准 backtest 准备调用保持 `fill_leading_gaps=False`，因此 leading-gap backfill 不是本次已激活路径；结论仍依赖输入 candle 已真正完成，并受内部补点和逐行来源缺失的限制。
7. **研究解释存在已确认的时间旅行风险。** 当前默认配置的可见 Git 历史始于 2026-06，并在 2026-09 有更新，却被回放到 2023-09。它可以说明“当前参数对历史数据的模拟表现”，不能说明“这套准确参数在 2023 年已经可实盘运行并取得该收益”。参数是否曾依据本三年窗口选择，现有工件无法证明或排除。

本报告仅审计现有离线源码、现有 Binance 1 分钟回测工件及可见版本历史；没有重跑回测、访问账户、使用凭据或创建、取消、修改任何交易所订单。既有 `annual_analysis.md` 和 `backtest_constraint_and_fill_audit.md` 未作修改。

## 证据范围与术语

| 术语 | 本报告含义 |
| --- | --- |
| candle `k` | 标签为 `t` 的 Binance 1 分钟 candle，覆盖区间为 `[t, t + 1m)`；标签是开盘时刻 |
| candle `k` 完成 | 在 `t + 1m` 后，`O/H/L/C/V` 均可作为策略输入 |
| 既有订单 | 在 candle `k` 开始前已经存在于回测 `open_orders` 的订单 |
| `T+1` | 信号在 `k` 完成后，订单最早可能参与的下一根 candle；回测把它建模为从该根开始即完整可用 |
| `T+2` 压力情景 | 不使用 tick/订单接受时间时，让订单从再后一根完整 candle 才可成交的保守敏感性口径 |
| 前视泄露 | 在决策时读取尚未完成、尚未可知的未来市场或标签信息，并让其改变决策、订单或评估 |
| 特征泄露 | EMA、成交量、波动率、候选排名等特征使用了决策时不可得的未来 candle、未来返回或未来成员信息 |
| 数据质量偏差 | 并非使用未来值，但缺失补点、合成 K 线、币种幸存者或当前参数回放会使结果偏离可部署历史实验 |

结果工件为 Binance-only、1 分钟、40 个有效数据币种，要求区间为 2023-09-12 至 2026-09-12。有效配置的 long 参数包括：`n_positions = 7`、entry cooldown 24.1 分钟、entry retracement 0.0008、close retracement 0.0005；HSL 未启用。缓存元数据记录 36,288 分钟 warmup，实际交易由每币 valid range、warmup 和 trade-start 边界共同限制。

完整风险条目及状态见 `lookahead_risk_matrix.csv`。表中的状态必须按以下含义阅读：

- **confirmed**：源码或现有账本已直接证明。
- **no_direct_leak_found**：在本次追踪的路径中未发现未来输入；不等于对未读取模块作全仓库证明。
- **latent / possible / unresolved**：代码存在该分支、或现有工件缺少足够 provenance；不能写成已发生。
- **not active**：现有 1 分钟、Binance-only 工件不满足该分支的启用条件。

## 蜡烛边界：从 `t` 到 T+2 的信息流

### 正确的时间解释

```text
candle k 标签 t，市场形成 [t, t+1m) 的 O/H/L/C/V

在 [t, t+1m) 内：
    只有进入该分钟之前已成功挂出的订单，才可能真实参与该分钟撮合。

在 t+1m（candle k 已完成）：
    1. 回测检查既有订单是否被 candle k 的 H/L 严格穿越；
    2. 回测读取 candle k 的 C、H、L、V，更新 EMA、波动率、forager 特征和 trailing；
    3. 回测构造基于 candle k 的新信号与订单意图。

从 t+1m 起：
    实盘订单需经历发送 -> 交易所接受 -> 订单簿排队，接受时刻为 t+1m + delta。
    回测将它视为下一根 candle k+1 整段 [t+1m, t+2m) 均可成交。
```

因此，用户提出的原则是正确的：**由 `t` 时刻 candle 产生的信号绝不能反过来使用同一根 `t` candle 的 HL 来判定自身成交。** 当前普通回测循环的代码顺序满足这一最基本要求：`check_for_fills(k)` 在 `update_emas(k)`、`update_trailing_prices(k)` 和 `update_open_orders_all(k)` 之前执行。

但这不代表 T+1 的 OHLC 成交已经真实可复现。回测不知道交易所何时接受订单，也不知道 `k+1` 的 low/high 是发生在订单接受之前还是之后：

| 模式 | 对信号 `S(k)` 的成交假设 | 信息/执行保守性 |
| --- | --- | --- |
| 当前 OHLC 回测 | 订单在 `k+1` 的全分钟有效；若 HL 穿越则成交 | 乐观：等同零传输、零接受、零排队延迟 |
| T+1 逐笔回放 | 使用订单发送/接受时间，只允许接受后发生的 trades 成交 | 可验证：需要历史订单事件与逐笔交易/盘口 |
| T+2 OHLC 压力情景 | 不允许 `k+1` 触发；最早从 `k+2` 的完整分钟判定 | 保守：避免不知道 `k+1` 内下单时点的歧义 |

“实际很可能 T+2”不能作为固定事实。若策略在 candle 收盘后迅速收到数据且交易所快速接受订单，它可在 T+1 的一部分时间有效；若网络、批次限制、限频、重连、撤单确认或交易所拥堵发生，则可能晚于 T+1，甚至更久。没有订单接受时间戳和逐笔市场数据时，T+2 是应纳入的压力测试，不是历史真实执行时间。

### 当前时间戳的报告边界

回测为 orchestrator input 和 `fills.csv` 写入 `first_timestamp_ms + k * interval`，也就是 candle `k` 的**开盘标签** `t`。而 `k` 的 close/high/low 只有在 `t+1m` 后才完整可知。因此：

- `fills.csv` 中的 `timestamp` 应理解为“所属 candle 的开盘标签”，**不是**经交易所确认的精确成交时间。
- 对现有订单，真实成交可发生于 `[t, t+1m)` 任意位置；回测无法定位。
- 对由该 candle 产生的后续信号，经济意义上的决策时刻是 `t+1m`，而不是 CSV 显示的 `t`。
- 本账本有 896 条 fill 使用任何小时的 `:59` 标签，其中 3 条位于月份最后一分钟：2025-08-31 23:59 的 ENA entry、2025-11-30 23:59 的 FIL entry、2026-01-31 23:59 的 ENA unstuck close。它们可能在该分钟内的不同实际时点发生，因此按开盘标签做月度归属是报表约定，不能被误读为精确 wall-clock PnL 证据。

这是一项**时间归因/报告风险**。它不会自动制造普通订单的同 candle 前视，因为订单检查发生在特征更新之前；但若研究者把 `timestamp=t` 误读为“在 candle 一开始就根据完整 candle 作出决策”，就会错误地得出前视结论。

## 订单、HL 与 intra-bar 顺序

### 已存在订单的 HL 判定

本次所有 69,537 条记录都是普通 maker 模拟成交。对已经存在且标的可交易的订单：

| long 订单 | 数量符号 | 触发条件 |
| --- | ---: | --- |
| entry 买单 | `qty > 0` | `low[k] < limit_price` |
| close 卖单 | `qty < 0` | `high[k] > limit_price` |

这是严格不等式。`low == buy_limit` 或 `high == sell_limit` 不成交；一旦满足，回测按限价全额成交并计 maker fee。这个条件只能说明价格区间穿越了限价，不能说明订单在真实盘口中的到达、排队、可成交量、部分成交或 maker 资格。

### 同一根 candle 的路径无法从 OHLC 恢复

`check_for_fills()` 按币种索引遍历，对每一个币种固定执行：

```text
先处理该币种满足条件的 close orders
再处理该币种满足条件的 entry orders
再进入下一个币种
```

本次账本的量化结果为：

| 账本观察 | 数值 |
| --- | ---: |
| 总 fills | 69,537 |
| entry fills | 39,701 |
| close fills | 29,836 |
| 同一分钟有多笔 fill 的分钟数 | 4,927 |
| 同一币种同一分钟同时有 entry 与 close 的分钟数 | 57 |
| 单分钟最多 fills | 7 |

对于这 57 个分钟，真实价格可能是 `O -> H -> L -> C`，也可能是 `O -> L -> H -> C`，还可能多次往返。若 long close 的卖出限价位于 high 一侧、entry 的买入限价位于 low 一侧，二者谁先发生会影响：可平数量、重开仓是否应发生、平均开仓价、手续费、entry cooldown、trailing extrema reset、WEL/TWEL 和下一轮订单。回测的 close-before-entry 是一个稳定的规则，不是市场证据。

因此，不能以“HL 已实证触及”推导“这一分钟的完整 entry/close/PnL 顺序已实证”。HL 只能验证区间集合，不验证路径、到达时间或成交量。

## `next_candle`：确认的直接未来读取及其影响范围

### 代码路径

backtest 构造每个 `SymbolInput` 时，在 `k+1` 仍可交易的前提下直接读取：

```text
next_candle.low  = low[k + 1]
next_candle.high = high[k + 1]
```

`trailing_martingale::generate_orders()` 随后计算下一阶 entry 或 close，并调用与 fill 相同的严格比较：

```text
buy rung:  next.low  < order.price
sell rung: next.high > order.price
```

若 entry 的下一阶会被下一根触及，生成完整 entry grid；若任一 close rung 会被下一根触及，生成完整 recursive close grid。源码将这个字段明确标记为 **backtest-only hint**；live 传入 `None`。

这是一项客观的未来数据读取，故任何需要“仅使用当时信息”的研究审计都必须将它列为前视风险。

### 为什么不能直接把它等同于已证明的收益作弊

live 在 `next_candle = None` 的分支并不只挂一阶，而是默认生成完整网格。backtest 的分支可理解为：

```text
若 k+1 会触及某个完整网格阶梯：挂完整网格
若 k+1 不会触及任何相关阶梯：只保留最近一阶
```

在理想化的单调网格和全额 OHLC 成交模型中，后一种情况被省略的深阶本来就不会在 `k+1` 成交；前一种情况则与 live 的完整网格对齐。这给出了“它可能是等价模拟优化”的设计理由。

但当前工件缺少每个 bar 的 ideal-order snapshot、风险 gate 输入/输出和 no-peek 回放，仍有三项不能省略的边界：

1. 多订单的 WEL/TWEL gate、最小量、取消/重建和同 bar 排序可能使“生成集不同但最终 fills 相同”的直觉失效。
2. 对 close 路径，完整 recursive grid 的价格/数量与真实 exchange acknowledgement、部分成交和 close-before-entry 次序耦合，不能凭静态代码推导出三年净 PnL 完全不变。
3. 真实 live 并不会收到 `k+1` 的完整范围；即使最终 OHLC fills 与 full-grid 理论等价，订单数量、排队、撤单、maker/taker 和执行负担仍不会相同。

### 本配置对 entry 与 close 的不同影响

本配置的 entry `retracement_base_pct = 0.0008 > 0`。orchestrator 的 add-order gate 因 retracement 需要顺序 staging，且 cooldown 为正，最终只保留第一个增加仓位的 entry。故本次的 `next_candle` 不会使**最终 entry 订单集**保留多层同步加仓；这减小了其对 entry 路径的直接影响范围。

本配置的 close `retracement_base_pct = 0.0005 > 0`，而 close 不经过上述“只保留第一笔加仓单”的规则。它仍可因 `next_candle` 判断而展开完整 recursive close grid。因而本次未来读取的主要行为风险在 close ladder 的生成/模拟，而不是多阶 entry 同时挂出。

**审计结论：** `next_candle` 是已确认的直接前视输入；对本配置的 entry 最终订单集影响受 sequential staging 限制，对 close 仍具直接作用。尚未进行 no-peek/full-grid 反事实回放，故不能量化其对 11.087553 倍 USD gain、73.69% 回撤或各月 PnL 的净影响。

## 特征、数据与有效区间审计

### 未发现直接未来特征泄露的路径

`update_emas(k)` 位于 `check_for_fills(k)` 之后。对每个 valid candle：

- 价格 EMA 只用 `close[k]` 更新；
- quote-volume EMA 使用 `volume[k] * (high[k] + low[k] + close[k]) / 3`；
- log-range / 1 分钟 volatility EMA 使用 `ln(high[k] / low[k])`；
- 整点时的小时 bucket 聚合从上一个小时边界到 `k-1`，不把当前小时的未完成 candle 纳入；
- `update_trailing_prices(k)` 在已有订单成交判定后更新，若该币本根发生 fill 则重置 extrema，否则再吸收 `high[k]`、`low[k]` 和 `close[k]`。

这些特征的使用时点是 candle `k` 完成后、为后续订单构造服务。代码中没有发现以未来收益、`close[k+1]`、`high[k+1]` 或 `low[k+1]` 直接构建 EMA/forager 特征的路径。forager 的截面归一化是同一决策时刻各候选的比较，并非未来标签。

EMA 初值取每个币 first-valid candle，随后每币 trade start 被 warmup 限制；本工件 warmup 为 36,288 分钟。使用开始前历史进行 warmup 是可部署的，只要真实运行在开始交易前已积累相同历史，不构成未来信息。

### 缺口和合成数据

| 路径 | 时间性 | 本次结论 |
| --- | --- | --- |
| 内部 gap zero-candle | 使用之前的 close、zero volume | 不把未来值带回过去；但会降低估计波动/成交量，可能影响 forager 和 EMA |
| leading gap，默认 | 不合成前缀，起点推到第一根真实 candle | 防止 pre-listing 或缺失前缀被虚构为可交易 |
| `fill_leading_gaps=True` | 用第一根真实 candle 的 close 回填更早前缀 | 真正的未来值回填风险；但本次标准 backtest 准备调用保持默认 `False`，所以不是本次已激活路径 |
| archive day 边界 | leading/trailing 均不补 | 代码明确避免 listing/delisting 边界把未来/旧价格伪装成可交易 candle |
| higher timeframe 合成 1m | 用完整高阶 O/H/L/C 构造分钟路径 | bucket 内前视/路径失真风险；本次输入已是 1m，不是当前已激活路径 |
| BTC benchmark `.ffill().bfill()` | 若 benchmark 缺前缀可把首个未来值带回 | 有潜在泄露；本次 BTC collateral cap 为 0，限制其对 USDT 策略余额/仓位的影响，仍可能影响 BTC 计价分析 |

当前工件是 Binance-only、`dataset_override = false`、manifest 存在，且其 `preparation.source_selection` 与 `preparation.volume_normalization` 为空。这没有显示跨交易所 source selection 或 volume-ratio 归一化的已发生结果。配置虽启用 volume normalization，但单一 Binance 来源不存在跨所比例选择的证据。

不过，现有 `dataset.json` 不保存逐行 “真实 / internal synthetic / leading fill / archive / CCXT” 来源标签。因此正确结论是：**没有找到本次已发生的 synthetic-data 泄露证据；也不能仅凭当前工件证明每一根输入 candle 都是原始 Binance 行。**

### 上架、下架与交易池的未来信息

回测初始化时接收每币 first/last valid index，并扫描完整数组；若某币的最后 valid candle 距整体结尾超过一天，会预先登记为 delist。到达该末根时，持仓会走 panic/taker close。这个“已知未来数据终止点后在最后一根强制退出”的分支对严格历史仿真带有未来可得性信息。

本次 `fills.csv` 的类型仅包含 normal/trailing/cropped/unstuck entry/close，**没有** panic 或 delist fill，所以没有证据表明该分支改变了本次的已实现 PnL。它仍应作为其他包含下架币的回测的设计级风险。

更广义的 universe risk 同样存在：41 候选池和当前参数是后来形成的当前配置。即使回测正确地让后上市币在 first valid/trade start 后才可交易，它也没有包含当时可能存在、现已下架或不在当今候选列表中的完整历史 universe。该幸存者/选择偏差无法由单个结果目录消除。

## 参数时间旅行与样本外解释

本次回测把当前默认配置应用到 2023-09 至 2026-09 的整个窗口。可见 Git 历史中，这个配置路径最早可追踪的变更为 2026-06，最近变更为 2026-09-10；三年窗口的大部分时间早于这些配置版本。

这直接意味着：

- 不能把 2023-2026 的收益曲线称作“该精确配置当时已实盘可得的表现”；
- 不能以 2023 年的月度收益、回撤或择币结果证明当时的部署能力；
- 参数是否从这段历史、相邻市场或相同候选池的研究中挑选而来，当前回测目录没有 optimizer archive、日期化配置快照或选择记录，**未解决**；
- 即使单根 candle 的特征路径完全因果，使用已知未来后确定的候选池、参数或风险阈值，仍会产生样本内选择偏差。

这不是指控配置一定经过过拟合；它是对证据边界的严格说明。要主张可部署绩效，必须证明一个日期化冻结配置在该日期之后的数据上运行，而不是用今天的默认配置回放更早历史。

## 风险优先级与需要的验证

### 当前报告可以成立的结论

1. 当前模拟器的普通订单没有用信号 candle `k` 的 HL 让该信号在 `k` 内成交。
2. 已完成 candle 的 EMA、volume、log-range、小时波动率和 trailing 更新没有发现直接使用未来 candle 或未来收益。
3. `next_candle` 是确认的 `k+1` 直接读取；它在本配置 entry 上受 sequential staging 限制，在 close 上仍可影响完整网格生成。
4. T+1 全分钟可成交、同 bar close-before-entry 和全额 maker fill 都是模拟假设，而非历史实盘执行事实。
5. 当前配置回放至 2023 是时间旅行式研究解释，不能替代冻结策略的样本外结果。

### 当前报告不能成立的结论

1. 不能声称 `next_candle` 一定使收益被高估，也不能声称它已证明无影响。
2. 不能从 1 分钟 HL 证明订单在真实 Binance 上已提交、被接受、排到队列、全额成交或保持 maker。
3. 不能从 `fills.csv` 的 candle-open 标签恢复真实毫秒级成交顺序。
4. 不能仅凭 result metadata 证明不存在每一类 synthetic candle、benchmark bfill 或历史参数选择偏差。
5. 不能将当前三年年度/月度 PnL 当作 2023 年起可部署策略的审计收益。

### 验证阶梯

| 优先级 | 验证 | 能回答的问题 |
| ---: | --- | --- |
| 1 | no-peek/full-grid 反事实：backtest 不传 `next_candle`，按 live 默认 full-grid 生成，比较每根 ideal order、fills、Pnl、WEL/TWEL | `next_candle` 是否实际改变本配置三年输出 |
| 2 | T+2 延迟敏感性：信号 candle 完成后，不允许下一根 candle 成交 | 零延迟 T+1 假设对收益、回撤、槽位和风险敞口的敏感度 |
| 3 | intrabar 双路径界限：至少分别重放 `O->H->L->C` 与 `O->L->H->C`，并禁止不符合路径的 close/entry 组合 | 57 个同币 entry/close 分钟及多 fill 分钟的路径风险 |
| 4 | 历史 tick / order-book / acknowledgement 回放 | 订单在接受后能否成交、排队、部分成交、maker/taker、真实延迟 |
| 5 | 输入 provenance 审计：为每根 K 线保存真实/补点/来源标记，拒绝 leading bfill 进入策略 | gap、archive、CCXT 与合成数据是否改变特征或可交易性 |
| 6 | 日期冻结的 walk-forward：使用当时已存在的配置和候选池，只评估之后未参与选择的数据 | 参数、币池和策略版本的样本外有效性 |

## 最终判断

你强调的边界完全成立：**信号由 candle `k` 完整信息产生后，不能再用 `k` 的 HL 判定该新订单成交；并且在没有订单接受时刻和逐笔路径时，不能把 `k+1` 的整根 HL 当作无条件可成交区间。**

当前回测对第一点的普通订单顺序是正确的：先成交旧订单，再计算完成 candle 特征，再建新单。它没有把同 candle 信号直接回填成交。可是它仍带有 `k+1` range 的明确读取，并将新订单的可执行性简化为 T+1 全 bar、全额 maker fill；同一分钟内又以固定 close-before-entry 顺序代替真实路径。再加上当前 2026 配置对 2023 历史的追溯应用，这份结果最多可称为**当前策略在特定历史 OHLC 假设下的回放**，不能称为严格无前视的历史实盘复现或可保证的实际盈亏。
