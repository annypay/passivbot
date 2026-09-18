# 引擎级冷却阶梯与累计已实现亏损停机：契约冻结

这份文档是引擎改动的**设计冻结件**：它把两个改动写成可实现的契约（配置键、公式、状态机、
重建契约、测试矩阵），并说明每一处取值的实测依据。所有依据都来自本仓库此前轮次的实测，
不是推断；没有依据的地方本文档显式标注为"待验证"。

两个改动都**默认关闭**，关闭时引擎行为与今天逐位相同。

| 改动 | 配置键 | 默认 | 依据 |
|---|---|---|---|
| ① 冷却阶梯 | `bot.<pside>.hsl.halt_ladder_minutes` | `[]`（关闭） | 上一轮的定档结论：阶梯只作为**风险塑形**，最高档 24H |
| ② 永久停机改累计已实现亏损口径 | `bot.<pside>.hsl.realized_loss_budget_pct` | `0.0`（关闭） | 瞬时回撤尖峰做永久地板在这段历史里没有可用取值区间 |

## 1. 为什么做这两件事

**① 的收益理由已经被证伪，保留它的理由只有一个：风险塑形。** 固定档位对照（同一几何、
同一条腿）显示加长冷却不更优——`ext` 腿 12H/24H/48H/72H 的终值分别是 2.3002× / 2.7662× /
2.0828× / 1.8666×，最差回撤单调变差 76.56% → 76.85% → 77.10% → 79.29%；样本外 `pre` 腿终值
单调变差 0.6104× → 0.5888× → 0.5554× → 0.4832×。因此阶梯的价值只能是"连续触发时少复入几次"，
所以：

- 最高档取 **24H**（`[720, 1440]`），不取 48/72H——档位越高实测越差，取证据支持的**最小**非零档；
- 默认关闭，文档与 `CHANGELOG` 必须写明这是风险塑形、不是收益改进。

**② 的正当性不依赖这段历史。** 现在的永久停机判据是 `no_restart_triggered`
（`passivbot-rust/src/equity_hard_stop_loss.rs:206-218`），用 **`max(drawdown_raw, drawdown_ema)`**
与阈值比较，而这两个量取自 **panic 平仓确认那一刻**的权益。急跌里"先击穿触发线、确认时已经
跌过 40%"是常态，于是"永久地板"实际上是被一次瞬时未实现回撤尖峰扣动的：

- 上一轮实测 `no_restart_drawdown_threshold = 0.40` 在**第一次触发**就被击穿（确认回撤 46.9%），
  账户活到窗口末却 1942 天零交易——"未强平"不等于"还活着"；
- 本轮实测阈值 0.55 在 `ext` 腿**首击锁存**（终值 0.3399×）、0.70 在**第二次触发**锁存
  （0.2910×），而同一几何下不锁存的臂最差回撤是 76.56%；
- 合起来：任何低于约 0.77 的"瞬时回撤口径永久地板"都会在这段历史里把账户关停，
  而该键的合法上界是 1.0——**这个口径几乎没有可用取值区间**。

同一轮还给出了"确认时点"的量级：ZEC 2026-06-05 那次 RED，触发时原始回撤是 22.98%
（`hard_stop_trigger_drawdown_mean`），panic 挂单成交、确认扁平时的已实现亏损相当于事件起点
策略权益的 30.03%（`hard_stop_panic_close_loss_drawdown_pct_mean`）。**触发 → 确认**之间
有 2 分钟（`hard_stop_flatten_time_minutes_mean`），而永久地板只在这 2 分钟的终点采样一次。

**一个不可逆的永久关停不该由一次瞬时未实现回撤尖峰触发。** 这是设计缺陷，不是参数没调好，
所以修它不需要先证明它在回测上更赚钱。

## 2. 改动 ①：冷却阶梯 `halt_ladder_minutes`

### 2.1 配置

```jsonc
"bot": { "long": { "hsl": {
  "halt_ladder_minutes": [720, 1440]   // 第 1/2 次触发的冷却分钟数；默认 [] = 关闭
}}}
```

| 项 | 值 |
|---|---|
| 类型 | `list[float]`（分钟），逐项 `>= 0` |
| 默认 | `[]`（关闭：`cooldown_minutes_after_red` 照今天工作） |
| 长度上界 | 32（超过即拒绝；阶梯不是无界状态） |
| 扁平键别名 | `hsl_halt_ladder_minutes` |
| 作用域 | 与 HSL 的 `signal_mode` 一致（`coin` 模式下可被逐币覆盖） |

校验：

- 每一项必须是有限数且 `>= 0`；含负数、`NaN`、`inf` 直接报错，不做静默截断；
- 空列表 = 关闭，不是"零冷却"；
- 允许某一档为 `0`（该档位触发后无冷却，与今天 `cooldown_minutes_after_red = 0` 的
  `halted_no_cooldown` 语义一致）；
- 阶梯启用时 `cooldown_minutes_after_red` **被忽略**（阶梯是它的替代品），但该键仍必须合法。

### 2.2 档位选择

```
strike_index = min(strikes_this_cycle, len(ladder))      # 1-based
halt_minutes = ladder[strike_index - 1]
```

- 阶梯耗尽后**夹在最后一档**（既不回绕也不越界）：`[720, 1440]` 的第 3 次及以后都是 24H；
- 冷却起点仍是"确认扁平的那笔成交时间戳"（invariant 3 不变）；
- `halt_minutes = 0` ⇒ `cooldown_until_ms = None`，处置为 `halted_no_cooldown`；
- 永久停机锁存时冷却无效（优先级不变：`no_restart` > 冷却）。

### 2.3 周期与重置

阶梯需要一个"周期"概念，否则计数器只会一直涨。周期定义为**作用域权益未回到峰值的那段时间**：

```
ladder_cycle_peak_equity:  本周期开始时的作用域策略权益（持久标量）
每次样本:
    if equity >= ladder_cycle_peak_equity:      # 回到"触发前峰值"
        strikes_this_cycle  = 0
        ladder_cycle_peak_equity = equity
        cycle_realized_pnl_peak  = realized_pnl_now
每次 RED episode 终结（平仓确认）:
    strikes_this_cycle += 1
```

- **为什么不能复用 HSL 的 `drawdown_raw == 0`**：HSL 的 drawdown 跟踪器在**每个 episode 结束**
  都会重置（invariant 1），而 RED episode 正是以"确认扁平"结束的——用 `drawdown_raw == 0`
  当重置条件会让计数器在每次停机后立刻归零，阶梯永远停在第 1 档。因此周期峰值必须是
  **跨 episode 的独立持久标量**。
- 重置条件是"创新高"（设计文档里叫 `new_high` / `pre_strike_peak`）：权益回到本周期的峰值即清零。
  峰值跟踪器在配置了 `live.pnls_max_lookback_days` 时是滚动窗口峰值，因此"峰值"跟随该窗口。
- 阶梯**不**新增 `ladder_reset` 选择键：只有一条规则，减少状态与重建面的同时保留可重建性。

### 2.4 与既有键的关系

| 既有键 | 阶梯启用后 |
|---|---|
| `cooldown_minutes_after_red` | 被忽略（阶梯替代） |
| `red_threshold` / `ema_span_minutes` / `tier_ratios` | 不变：触发与分档完全不变 |
| `no_restart_drawdown_threshold` | 不变：永久停机优先级最高，锁存时冷却无意义 |
| `panic_close_order_type` | 不变 |

## 3. 改动 ②：累计已实现亏损口径

### 3.1 配置

```jsonc
"bot": { "long": { "hsl": {
  "restart_after_red_policy": "threshold",
  "no_restart_drawdown_threshold": 1,      // 关掉瞬时口径（合法上界 = 1.0）
  "realized_loss_budget_pct": 0.30         // 累计已实现亏损预算；默认 0.0 = 关闭
}}}
```

| 项 | 值 |
|---|---|
| 类型 | `float`，`0 <= x <= 1` |
| 默认 | `0.0`（关闭） |
| 扁平键别名 | `hsl_realized_loss_budget_pct` |
| 语义 | `> 0` 时启用；永久停机在"累计已实现亏损达到预算"时锁存 |

### 3.2 公式

在 RED episode 终结（平仓确认）时：

```
realized_loss_usd  = max(0, cycle_realized_pnl_peak - realized_pnl_now)
reference_equity   = max(ladder_cycle_peak_equity, EPS)
realized_loss_pct  = realized_loss_usd / reference_equity
latch              = policy == "never"
                     or (policy == "threshold"
                         and (drawdown_rule or realized_loss_pct >= realized_loss_budget_pct))
```

| 量 | 含义 | 来源 |
|---|---|---|
| `cycle_realized_pnl_peak` | 本周期内作用域**已实现** PnL 的最高值 | 新持久标量，随周期重置 |
| `realized_pnl_now` | 作用域当前的累计已实现 PnL（沿用 `hsl_signal_mode` 的口径） | 现有样本输入 |
| `reference_equity` | 周期起点的作用域策略权益（= 上面那 2.2 的周期峰值） | 现有周期状态 |

要点：

- **只用已实现量**：分母是周期起点的权益（一个已经发生过的数字），分子只随**成交**变化，
  盯市漂移与未实现浮亏**不进入**这个判据。这正是"累计"的正确口径。
- **累计语义**：同一周期内多次触发的已实现亏损会累加（`cycle_realized_pnl_peak` 不在停机时清零），
  所以第 N 次触发的判定天然包含前 N−1 次的亏损——而不是"每次按剩余余额重新起算"。
- **与瞬时口径并存**：`realized_loss_budget_pct > 0` 时两个判据是 `OR`。要"换成"累计口径，
  把 `no_restart_drawdown_threshold` 设到合法上界 1.0 即可（该值下瞬时判据实际不可能先触发）。
- **`restart_after_red_policy` 仍是总开关**：`always` 永远不锁存，`never` 永远锁存，
  `threshold` 才比较两个判据。
- 判据命中哪一条会写进停机事件与状态（`no_restart_reason ∈ {policy_never, drawdown, realized_loss}`），
  日志与 `hsl-startup-preview` 都要能看出来。

## 4. 实盘重建契约

这是硬约束（`docs/ai/features/equity_hard_stop_loss.md` invariant 6：重启只用交易所状态、
成交/PnL 历史、K 线、配置与当前时间重建，本地锁存文件只是诊断）：

| 状态 | 重建方式 |
|---|---|
| `strikes_this_cycle` | 由**保留的 RED episode tape** 计数：从本周期起点（权益峰值被重新达到的那一刻）以来已确认终结的 RED 停机次数 |
| `ladder_cycle_peak_equity` | 由与既有 no-restart 峰值同源的权益样本序列取运行最大 |
| `cycle_realized_pnl_peak` | 由同一序列上的作用域已实现 PnL 取运行最大 |
| 周期起点 | 重建序列中最后一次"权益 >= 当时运行最大"的样本 |

- 重建窗口与既有 no-restart 峰值**同源同界**（`live.pnls_max_lookback_days`），不新增数据需求；
- 成交历史不完整时沿用既有 fail-closed 路径（保持保护态、按 `live.risk_input_max_attempts` 重试），
  **不**用当前时间或本地文件替代；
- 进程内运行时重置（`_equity_hard_stop_reset_after_restart` 之类）保留周期状态，
  与 `no_restart_peak_strategy_equity` 的现有待遇一致；
- `hsl-startup-preview` / `live-config-preflight` 必须展示重建出的 `strikes_this_cycle`、
  下一档停机分钟数与 `realized_loss_budget_pct` 的当前余量。

## 5. 代码边界

纯策略数学继续放在 Rust，且**只**扩展既有的纯转移函数，不把状态搬进 Python：

| 位置 | 改动 |
|---|---|
| `passivbot-rust/src/equity_hard_stop_loss.rs` | `RedEpisodeFinalizationContext`（把现有 9 个位置参数收进结构体）+ 阶梯选档 + 累计亏损判据 + 新状态字段与重置规则；纯函数，无 I/O |
| `passivbot-rust/src/python.rs` | `hsl_red_episode_finalization` 关键字参数与返回键扩展（新参数带默认值，旧调用点仍可用） |
| `passivbot-rust/src/backtest.rs` | 每个作用域的运行状态新增 3 个字段；每次样本的重置判定；两处终结调用点（pside / coin）；新增两个分析指标 |
| `src/passivbot_hsl.py` | 配置解析与校验、周期状态、终结调用点、历史重建、锁存载荷、状态日志 |
| `src/config/*` | schema 默认值、扁平别名、CLI 帮助、校验与钳制、逐币覆盖路径 |
| `src/tools/*` | `hsl-startup-preview` / `live-config-preflight` 展示重建值 |

GPU/金属代理路径（`src/optimization/gpu/*`）**不动**，代价必须写明：

- 代理口径**不模拟**这两个键——它只知道平面冷却与瞬时回撤口径。因此**不要**把
  `halt_ladder_minutes` / `realized_loss_budget_pct` 放进任何被优化的维度：优化器会以为自己在评估
  一个阶梯策略，实际评估的是平面冷却策略。本轮及以后的搜索都把它们钉死。
- 新增的两个分析指标（`hard_stop_ladder_strikes_max`、`hard_stop_realized_loss_halt_pct_max`）
  只出现在**精确后端**的 `analysis.json` 里，不进 GPU 指标注册表（该注册表有一致性测试，
  且代理不产出这两个量）。引用它们时必须说明来源是精确重放。

## 6. 测试矩阵

1. **Rust 单测**（`equity_hard_stop_loss.rs`）
   - 阶梯：第 1/2/3 次分别取 720 / 1440 / 1440（夹紧）；`[0]` 的 `halted_no_cooldown`；
     `[]` 时逐位等同旧行为；含负数/`NaN`/超长列表被拒绝；
   - 周期：权益回到峰值清零、未回到峰值时累加、跨 episode 保持（不被 episode 重置清掉）；
   - 累计亏损：`realized_loss_pct` 公式、`reference_equity` 取周期峰值、
     `policy ∈ {always, threshold, never}` 三态、`budget = 0` 时逐位等同旧行为；
   - 优先级：`no_restart` 锁存时 `cooldown_until_ms = None`；`reason` 取值正确。
2. **回测级**：同数据集重放，`halt_ladder_minutes = []` 与改动前逐位一致；
   启用 `[720, 1440]` 时第二次停机的 `hard_stop_duration_minutes_max` 变成 1440。
3. **Python**：配置 schema / 归一化 / CLI 覆盖 / 逐币覆盖 / `live-config-preflight`；
   把 `tests/test_unstucking_safeguards.py` 里那份 `hsl_no_restart_triggered` 的纯 Python 镜像
   扩到新口径（镜像必须与 Rust 同步，否则测试失去意义）。
4. **fake-live 场景**：两次触发看到 12H → 24H；累计亏损达标时锁存并写出 `reason`。
5. **重建**：构造"已停机一次"的成交历史，重启后 `strikes_this_cycle == 1`、下一档 = 1440。

## 7. 验证 study 的预注册判据

| 臂 | 变量 | 预注册判据 |
|---|---|---|
| `L0_off` | 父配置 + 两个新键默认值 | 与上一轮同臂逐位一致（成交签名、终值、最差回撤、`hard_stop_*` 全等）——默认关闭的回归证据 |
| `L1_ladder` | `halt_ladder_minutes = [720, 1440]` | 第二次触发看到 1440；阶梯档位在图上可分辨（`hard_stop_duration_minutes_max` 单调不减） |
| `L2_budget` | `no_restart_drawdown_threshold = 1` + `realized_loss_budget_pct = 0.30` | 不再因瞬时尖峰锁存；若锁存，`no_restart_reason == "realized_loss"` 且累计口径可复算 |
| `L3_fixed24` | `halt_ladder_minutes = [1440]`（对照） | 与 `L1` 的差异只来自"第几次触发"——用于把阶梯效应与"单纯加长冷却"分开 |

三条腿沿用上一轮的窗口（搜索窗 `3y`、全历史 `ext`、样本外 `pre`），币池与资金规模不变。
**这一轮不新增参数搜索**，四个臂都是声明式的。

## 8. 不做的事与已知边界

- 不改 `resume_condition`（复牌时点）：那需要单独实验，本轮不碰。
- 不改 `orange_tier_mode` / 分级软刹车：上一轮已证伪。
- 不做 `reentry_scale_after_halt`：与复牌条件是同一类实验。
- 阶梯的周期峰值受 `live.pnls_max_lookback_days` 的滚动窗口约束：窗口外的旧高点不参与重建，
  这是与既有 no-restart 峰值**一致**的已知边界，不是新引入的近似。
- 两个键都是 trading-critical 的 Rust 行为变更：独立 PR、默认关闭、`CHANGELOG` 与
  `docs/equity_hard_stop_loss*.md` 同步；上线前用 `hsl-startup-preview` 在真实账户上核对重建结果。

## 9. 实测回填（2026-09-18，四个预注册臂 × 三条腿）

数字与逐条裁决见 [`README.md`](README.md) §4；这里只记结论，以及**对上面契约的修正**。

| 判据 | 结果 |
|---|---|
| K0 默认关闭逐位一致 | **通过**：`ext` 腿 45,644 笔成交哈希与 31 项指标全等（该腿守护触发 4 次） |
| K1 阶梯按档位生效 | **机制通过**；效果一侧未通过——`ext` +0.24%、`pre` −0.43%，最差回撤两条腿逐位相同 |
| K2 累计口径按契约锁存 | **机制通过**；标定被证伪——0.30 的预算在第一次停机就永久退出（0.3399×，两条腿相同） |
| K3 阶梯 ≠ 单纯加长冷却 | **通过**：差异完全由第一档解释 |

三处必须写进文档的修正：

1. **§2.3 的"最高档取 24H"应读作上限，不是更优**：第一档 12H/24H 的取舍在两条腿上给出**相反**
   答案（`ext` 24H 2.7662× > 12H 2.3002×；`pre` 24H 0.5888× < 12H 0.6104×）。因此本轮的证据
   不支持把推荐档位从 12H 改成 24H，只支持"不要超过 24H"。
2. **§3.2 的 `realized_loss_budget_pct = 0.30` 只是示例，不是推荐值**：实测里预算只在停机确认那一刻
   被检查，所以 0.30 的预算在累计回吐已经 67.4% 时才锁存，并且立刻永久关停账户。它修的是**归因**
   （不再由未实现尖峰扣动），不是"永久关停代价过高"这个更根本的问题——后者与上一轮
   `no_restart_drawdown_threshold` 的失败模式相同。
3. **§7 的臂名与实际冻结名一致**（`l0_off` / `l1_ladder` / `l2_budget` / `l3_fixed24`，全小写）。

样本量：`ext` 4 次守护触发、`pre` 3 次、`3y` 0 次；上面所有差异都建立在这几次之上，按本轮
`g4_overfitting_audit_2026-09-18` 的结论应标注 out-of-sample 未确认。
