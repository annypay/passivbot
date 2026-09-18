# 单币止损（`bot.<pside>.stop_loss`）机制设计

本文件是 Phase B 的规范：引擎侧单币止损的配置面、语义、不变量、失败模式与验证计划。判定口径与结果在 Phase C 的报告中，本文件只定义"实现成什么样才算对"。

## 1. 目标与非目标

**目标**：给每个 symbol-side 一个可选的、以**在场均价**为基准的硬止损：价格触及 `均价 × (1 ∓ pct)` 时整仓 reduce-only 离场，并在随后的一段时间内禁止该 symbol-side 重新入场。

**非目标**（明确排除，避免范围蔓延）：

* **不做交易所常驻挂单（Track B）。** 用户的原始动机包含"bot 不在线时仍有一层保护"，但 `src/exchanges/*` 目前没有任何 stop 单支持（下单价型、查询、对账、断线恢复都要新增）。本轮只做引擎侧，缺口写进文档并在 `issue/0008` 登记，等引擎侧判定出来再评估。
* **不做触及式（intrabar touch）触发。** 引擎在每个采样点决策，看到的市场价格是当根 K 线的收盘价（`backtest.rs` 把 `bid = ask = close`），因此 bot 侧止损只能是**样本触发**。这比 Phase A 普查所用的 `low ≤ level`（触及触发）**更不容易被插针打中**，两者口径不同，Phase C 必须用引擎自身的语义判定，不能直接引用普查数字。
* **不做 `trigger_confirm`。** 原计划列了 `last_price` / `sample_close` 两个取值；在本引擎里二者恒等（回测只有收盘价，live 只有当前采样价），加这个键等于加一个永远不起作用的旋钮。真正的"tick 级触发"属于 Track B。→ 这是对已批准计划的一处**收窄**，在此显式记录。
* **不进入参数搜索。** 15% 与 1440 分钟是用户给定的机制参数，不做样本内择优；止损键不进 `optimize` 搜索边界（GPU/优化器代理不建模止损）。

## 2. 配置面

分组键（两侧独立，与 `hsl` / `unstuck` 同级）：

```json
"bot": {
  "long": {
    "stop_loss": {
      "enabled": false,
      "pct_from_avg_entry": 0.15,
      "cooldown_minutes": 1440.0,
      "order_type": "market"
    }
  }
}
```

扁平键（Rust `BotParams`，与 `hsl_*` 同族）：`stop_loss_enabled`、`stop_loss_pct_from_avg_entry`、`stop_loss_cooldown_minutes`、`stop_loss_order_type`。

| 键 | 默认 | 约束 |
| --- | --- | --- |
| `enabled` | `false` | 关闭时引擎行为必须与基准**逐位相同** |
| `pct_from_avg_entry` | `0.15` | `> 0`；`0` 视为未配置（不触发），负数拒绝 |
| `cooldown_minutes` | `1440` | `>= 0`；`0` 表示触发后不额外禁止入场 |
| `order_type` | `"market"` | `"market"` 或 `"limit"`；其它值拒绝 |

`order_type` 默认取 `market`，与仓库既有的 panic 默认一致（`EquityHardStopLossConfig::default().panic_close_order_type == "market"`），理由见 §4。

## 3. 语义

**触发条件**（每个采样点重新求值，无新增持久状态）：该 symbol-side 有持仓、`position.price > 0`、`pct > 0`，且当前市场价格穿越止损位：

* long：`market_price <= position.price × (1 − pct)`
* short：`market_price >= position.price × (1 + pct)`

价格回到止损位之上后不再重新挂单 —— 等价于"价格反抽即撤销的止损"，这是有意的：Phase A 显示样本内 75.68% 的触发在冷却到期时价格**高于**出场价，反抽即撤销避免了在反抽中被动成交。

**动作**：一张整仓 reduce-only 平仓单，`qty = ∓|position.size|`，通过 `OrderType::CloseStopLoss{Long,Short}` 表达；同时**清空该 symbol-side 本采样点的全部入场单**。

**成交**：

* `market`：按现有市价路径成交（taker 5bp + `market_order_slippage_pct`）。这是**悲观层**，也是唯一可以支撑结论的层。
* `limit`：挂在该位。若下一根 K 线没有回到该位（跳空穿价），订单**不会成交**，仓位继续下探 —— 这正是"止损挂单在瀑布里不成交"的真实失败模式，必须保留、不得用乐观成交抹平。

**冷却**：止损**成交**后，`cooldown_minutes` 内该 symbol-side 不允许任何加仓单。实现上复用既有的 `add_order_cooldown_active` 判定与 `last_stop_loss_fill_timestamp_ms` 字段，但走一个**只丢弃加仓单**的窄闸门，避免误触发 `keep_only_first_add_order`。

**与其它 reducer 的关系**：

* `panic` 独占：panic 分支不生成止损单（panic 已经清掉全部仓位），且 panic 在 reducer 竞争中保持最高优先级。
* `unstuck` / `twel` / `wel` 与止损同属保护性减仓集合：同一采样点只保留一个，整仓止损因减仓量最大而胜出。这避免"同一分钟既 unstuck 部分减仓又整仓止损"的双重成交。

**模式**：`Manual` 不生成止损单（Manual 语义是"该仓位由操作者接管"）；`Panic` 同上；`GracefulStop` / `TpOnly` / `Normal` 生成。止损**不受**入场资格（`entry_eligible` / `active` / `min_effective_cost`）影响 —— 这正是它的用途。

## 4. 为什么默认 `market`

用户设想的形态是"在交易所挂一张挂在均价 −15% 的单"。在交易所语义里那必须是一张 **stop 单**（触及触发后转市价或限价），而不是普通限价单：一张挂在 −15% 的普通卖单，在现价高于该位时是**可立即成交**的（会立刻按市价平仓），在现价低于该位时则只能等价格回到该位。bot 侧没有 stop 单类型，于是只剩两条路：

1. 采样触发 + 市价平仓（`market`）：离场几乎确定，代价是 taker 费与滑点，且在最坏的时点成交。与用户的意图一致。
2. 采样触发 + 在该位挂限价（`limit`）：成交价更好，但**可能根本不成交**（跳空穿价后不再回到该位），保护是不确定的。

Phase A 已经确定：只有悲观层能支撑结论。因此默认 `market`，`limit` 作为机制敏感性臂单列，两者都必须在 Phase C 报告中出现，不允许只报好看的那一层。

## 5. 不变量

1. **默认关闭逐位相同**：`stop_loss_enabled = false` 时，引擎产出的订单序列、成交序列与 31 项指标必须与基准臂完全一致（成交哈希 + 指标比对的脚本化证据，`3y` 与 `ext` 双例）。
2. **整仓、reduce-only**：止损单只减不增；数量恒为触发时刻的仓位绝对值。
3. **只减不加**：触发同一采样点内该 symbol-side 的入场单全部作废。
4. **无新增持久状态依赖**：触发判定只依赖仓位均价与当前采样价，重启后可复现；冷却只依赖"止损成交时间戳"这一可从交易所成交历史重建的量。
5. **fail-closed 的配置面**：per-coin override 里出现未知的 `stop_loss.*` 子键、非法 `order_type`、非正 `pct` 一律报错退出，不静默取默认值；该键不进入 `optimize` 边界。

## 6. 失败模式（必须写进文档，不许藏起来）

| 失败模式 | 说明 | 处理 |
| --- | --- | --- |
| 跳空穿价不成交 | `limit` 层在瀑布中不成交，仓位继续下探 | 保留为真实行为；判定以 `market` 层为准 |
| 采样滞后 | 每分钟才看一次价格，最坏情况比触及触发多亏一分钟的跌幅 | 由 Phase C 与普查口径的差异量化，作为 Track B 的量化动机 |
| 冷却期机会成本 | 24 小时内该币不能加仓，Phase A 显示 37/37 次触发都有加仓被取消（合计名义额 118,320） | 冷却期单独记账，不与止损收益混算 |
| 与 unstuck 抢成交 | ZEC 该 episode 25 笔减仓里 23 笔是 unstuck，两者会争同一批成交 | 保护性 reducer 集合互斥已处理；Phase C 报告两者被替换的次数 |
| 反抽即撤销 | 价格回到止损位之上后不再挂单，可能错过"本该在反抽中止损"的情形 | 有意的取舍，记录在案 |
| 重启后行为 | 冷却依赖成交时间戳；live 需从交易所成交历史或本地状态恢复 | Phase B2 在 live 状态里落该字段，Phase C 不做 live 验证 |

## 7. 验证计划

| 层 | 内容 |
| --- | --- |
| Rust 单元 | 枚举归类、`calc_stop_loss_close` 的边界（关闭/未触发/恰好触位/低于触位/short 镜像/limit 与 market 定价/均价非正）、冷却闸门 |
| Rust 集成 | 合成小样本上：触发即整仓平仓、同分钟入场全部作废、冷却窗口内无入场成交、窗口后可再入场、默认关闭零 `CloseStopLoss*` |
| Python 配置面 | 分组键 hydrate/规范化/校验、per-coin override、非法值报错、`optimize` 边界不含该键 |
| 默认关闭同一性 | `3y` 与 `ext` 双例的成交哈希与 31 项指标比对 |
| fake-live 场景 | 断言启动摘要、配置面与订单意图；跟随既有 fake-live 场景模式 |
| Phase C 回测 | 6 臂 × 3 腿 + 合成腿，判定 S0–S7 |

## 8. Phase C 臂设计

所有臂只改 `stop_loss` 与相关冷却，不动任何 alpha 参数（必须与 `a_allow000` 保持可比）。

| 臂 | enabled | pct | cooldown | order_type | 用途 |
| --- | --- | --- | --- | --- | --- |
| `s0_off` | false | – | – | – | 默认关闭同一性（S0） |
| `s1_sl15_cd1440` | true | 0.15 | 1440 | market | 用户给定机制（主臂） |
| `s2_sl15_cd240` | true | 0.15 | 240 | market | 冷却长度敏感性 |
| `s3_sl25_cd1440` | true | 0.25 | 1440 | market | 止损位敏感性（避开样本内高频触发区） |
| `s4_sl15_cd1440_limit` | true | 0.15 | 1440 | limit | 成交层敏感性（不成交风险） |
| `s5_sl15_cd0` | true | 0.15 | 0 | market | 隔离"止损"与"冷却"两个机制 |

腿：`3y`（原生窗）、`ext`（2021→2026，含真实尾部）、`pre`（2021-04→2023-09，样本外）；再加两条**合成崩塌腿**，数据集与注入契约直接取自上一轮的尾部风险研究
（`g4_tail_risk_research_2026-09-17/report_tools/{variant_spec.py,make_synthetic_collapse_bundle.py}`）：

| 合成腿 | bundle | 注入路径 |
| --- | --- | --- |
| `synth_a` | `binance__40_coins__2023-08-17_to_2026-09-12__synthetic_collapse_a` | 单币从自身峰值敞口时点起 3 天内跌到原价的 0.5%，其后 14 天维持 |
| `synth_b` | `binance__40_coins__2023-08-17_to_2026-09-12__synthetic_collapse_b` | 三币先后按同一契约崩塌 |

六个臂全部跑满五条腿（6 × 5 = 30 个 run）。合成腿是 S2/S5 的唯一证据来源：`3y` 与 `ext` 腿只能回答"止损在会反弹的历史里值不值"，回答不了"不反弹时它救了多少"。

判定（S0–S7）见 Phase C 报告；其中 S6 要求"止损收益必须在悲观层（market）成立"，S7 以 2026-06 作为錨点（基准月内回撤 20.10% / 月收益 +6.81% / `b_red015` 因触发守护而 −23.4%）。
