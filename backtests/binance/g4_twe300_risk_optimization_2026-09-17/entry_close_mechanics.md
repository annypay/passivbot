# 入场 / 加仓 / 离场机制说明书（`b_red015__3y`，含 ZEC 2026-06-05 案例）

这份文档回答三个问题：**① 加仓的仓位比例是怎么定的；② 离场逻辑是什么；③ `b_red015__3y` 在
2026-06-05 07:15Z 清掉 ZEC，是不是入场闸门（门控）在控制。** 所有公式都给出
`passivbot-rust/src` 的行号，所有数字都来自本 arm 自己的
`episode_trace.json` / `fills.csv` / `config.json` / `analysis.json`，并由
`report_tools/verify_variant_report.py::check_episode_trace` 从账本独立复算。

> 结论先行
>
> 1. **加仓数量 = `max(double_down_factor × 加仓前持仓, balance × 单槽额度 × initial_qty_pct)`**，
>    再按 `qty_step` 取整、以交易所最小下单量托底；额度不足时按"剩余额度"裁剪。
>    本 arm 的 `double_down_factor = 0.6` ⇒ **每笔加仓 = 加仓前持仓的 0.6 倍，即持仓按 ×1.6 几何增长**；
>    这正是"仓位比例看不懂"的来源：加仓不是余额的固定比例，而是**当前持仓的固定比例**。
> 2. **离场有四条独立路径**：止盈（`close_trailing_long`，平掉整槽规模的 84%）、
>    unstuck 保护性减仓（`close_unstuck_long`）、暴露强制回收（本 arm 关着）、
>    以及账户级 HSL 熔断（RED = panic 平掉整个作用域 + 冷却）。
> 3. **ZEC 那次不是门控。** 入场闸门只拦入场，
>    `orchestrator.rs:2533-2536` 原文写着 "Deliberately entry-only. Closes, panic, and auto-unstuck
>    keep their own independent paths"；这次清仓是 `unified` 作用域的 **RED 档 panic 平仓**：
>    2026-06-05 07:15Z 同一分钟 **5 个币**（INJ/NEAR/ONDO/RENDER/ZEC）被一起平掉，
>    合计已实现亏损 **10,105.15 USDT = 触发前余额的 −23.40%**。

## 1. 口径：先纠正一个常见误读

**账本里的 `wallet_exposure` 是"成本口径"，不是盯市口径**：

```
wallet_exposure = |psize| × pprice × c_mult / balance          # utils.rs:255-265
```

其中 `pprice` 是该币的**持仓均价**（加仓后按成本加权），`balance` 是账户总余额；
`backtest.rs:1241` 就是用这个函数写入每条成交记录的。因此：

- 价格上涨时，盯市权重会大于 `wallet_exposure`；`wallet_exposure` 只随"加仓成本/余额"变化。
- **单槽额度的裁剪也是对成本口径做的**：`utils.rs:267-280`
  （`calc_wallet_exposure_if_filled` 先算成交后的 `psize/pprice`，再算敞口），
  裁剪阈值在 `entries.rs:318`。
- ZEC 案例可以直接验证：最后一行 `|psize| × pprice / balance = 41.587 × 445.1253 / 43,193.56
  = 0.428569`，正好等于单槽额度 0.428571（见 §4）。

**单槽额度**（本次 `TWE 3.0 / n_positions 7 / allowance 0`）：

```
base  = total_wallet_exposure_limit / n_positions                     # entries.rs:30-38 调用处
eff   = min(raw_allowance, TWE/base − 1)   # bounded 模式；raw = we_excess_allowance_pct
额度  = base × (1 + eff)                                              # entries.rs:40-66
      = (3.0 / 7) × 1.0 = 0.428571
```

本 arm 的关键参数（`config.json` 原值）：

| 参数 | 值 | 参数 | 值 |
|---|---|---|---|
| `total_wallet_exposure_limit` | 3.0 | `entry.initial_qty_pct` | 0.0081 |
| `n_positions` | 7 | `entry.double_down_factor` | 0.6 |
| `we_excess_allowance_pct`（bounded） | 0.0 | `entry.threshold_base_pct` | 0.03 |
| 单槽额度 | **0.428571** | `entry.retracement_base_pct` | 0.0008 |
| `risk.entry_cooldown_minutes` | 24.1 | `close.qty_pct` | 0.84 |
| `qty_step` / `min_qty` / `min_cost` | 0.001 / 0.001 / 5.0 | `close.threshold_base_pct` | −0.0027 |
| `price_step` | 0.01 | `close.retracement_base_pct` | 0.0005 |

## 2. 加仓是怎么定的

### 2.1 数量公式（`entries.rs:340-367`）

```rust
// calc_reentry_qty（entries.rs:340-367）
max(
    calc_min_entry_qty(entry_price, exchange),                              // ① 最小下单量托底
    round_(max(
        position_size.abs() * double_down_factor,                           // ② 0.6 × 加仓前持仓
        cost_to_qty(balance, entry_price, c_mult)
            * effective_wallet_exposure_limit
            * entry_params.initial_qty_pct,                                 // ③ 初始入场规模托底
    ), qty_step)                                                            // ④ 取整到数量步长
)
```

- ② 是**几何增长项**：`加仓后持仓 = 加仓前持仓 × (1 + double_down_factor)`，本 arm ⇒ **×1.6**。
- ③ 是**初始入场规模的托底**：`balance × 单槽额度 × initial_qty_pct`（与 `entries.rs:68-89`
  的初始入场同式），保证"持仓很小时加仓也不会小于首仓量级"。
- ① 是交易所约束：`calc_min_entry_qty`（`entries.rs:91-114`）取
  `max(min_qty, min_cost / price)` 并向上取整到 `qty_step`。
- 初始入场（`entries.rs:68-89`）：`max(min_entry_qty, round_(balance × 额度 × initial_qty_pct, qty_step))`；
  本 arm 的 `analysis.json::entry_initial_balance_pct_long = 0.0034714` = `0.428571 × 0.0081`，
  与公式一致。

### 2.2 额度裁剪（`entries.rs:292-338`）

```rust
if wallet_exposure_if_filled > effective_wallet_exposure_limit * 1.01 {   // :318，1% 死区
    entry_qty_abs = interpolate(额度, [we_before, we_if_filled],
                                [持仓, 持仓 + 加仓量]) − 持仓;             // :320-324
    (…, max(round_(entry_qty_abs, qty_step), min_entry_qty))              // :325-331
}
```

- **只有"成交后敞口 > 额度 × 1.01"才裁**：额度内留 1% 死区，所以接近满槽时可能小幅超出。
- 裁剪量用**线性插值**落到额度上：等价于
  `裁剪量 = (额度 − we_before) × balance / 持仓均价`（本 arm 的两笔裁剪见 §4，误差 < 0.1%）。
- 裁剪发生时订单类型被改写成 `entry_trailing_cropped_long`（`entries.rs:153-165`），
  因此账本里 **`entry_*_cropped_*` 就是"被额度裁剪过的加仓"**。
- 注意：额度是**下单规划时**的约束，用的是当时的价格/余额；成交时的实际价格、`qty_step`
  取整与最小下单量托底都会让**实测**敞口与额度有小幅差异（Round D 的
  `risk_geometry.json::cap_overshoot_fill` 记录的正是这类成交）。

### 2.3 什么时候触发加仓（`entries.rs:610-729`）

Trailing martingale 的加仓是**"跌破阈值 + 从低点回升"**的双条件回落触发：

```rust
let threshold_pct   = entry.threshold_base_pct.max(0.0) * threshold_multiplier;      // :680
let retracement_pct = entry.retracement_base_pct.max(0.0) * retracement_multiplier;  // :681
if min_since_open < position.price * (1.0 - threshold_pct)                           // :707
   && max_since_min > min_since_open * (1.0 + retracement_pct)                       // :708
{ 挂单价 = min(bid, round_dn(position.price * (1.0 - threshold_pct + retracement_pct), price_step)) }  // :712-718
```

- `position.price` 就是**加仓前持仓均价**；本 arm 的朴素触发参考是
  `加仓前均价 × (1 − 0.03) = ×0.97`（§4 表里给了每笔的数值），再叠加"从低点回升 0.0008"。
- 两个"距离乘数"来自 `dynamic.rs:26-35`：
  `multiplier = max(1.0, 1 + 波动率_1h×w_1h + 波动率_1m×w_1m + 敞口比×w_we)`
  （本 arm 的权重：threshold 侧 4.58 / 17.68 / 1.007，retracement 侧 15.5 / 50.42 / 0.334）。
  乘数 ≥ 1，所以实际挂单价比 `×0.97` 更低；成交价往往又比挂单价更优（跳空/急跌时以更优价成交）。
- **加仓还要过这些闸门**：
  - 频率：`apply_add_order_gates`（`orchestrator.rs:610-629`）——距上次"增仓成交"不足
    `risk.entry_cooldown_minutes`（本 arm 24.1 分钟）就丢掉所有加仓单
    （`add_order_cooldown_active`，`:573-586`）；且本策略一次只保留**第一张**加仓单
    （`:588-593`、`:595-608`，因为 retracement 开启）。
  - EMA 带：`ema_gate_mode = "all"` ⇒ 加仓价还要被 EMA 带压低
    （`gate_reentry_price_bid`，`entries.rs:256-272`）。
  - 总暴露入场闸门（`total_exposure_entry_gate_enabled: true`）与 20/50 日线门控
    （只拦入场，见 §3.4）。
  - 账户级守护处于 Panic / TpOnly 时不产生任何入场（`should_generate_entries`，
    `orchestrator.rs:2523-2531`）。

## 3. 离场逻辑

本 arm 在 3 年腿上的成交类型分布（`fills.csv`）说明**实际生效的离场路径只有三条**：

| 类型 | 笔数 | 含义 |
|---|---|---|
| `close_trailing_long` | 9,565 | **止盈**（trailing 平仓） |
| `close_unstuck_long` | 1,242 | unstuck 保护性减仓 |
| `close_panic_long` | 5 | **账户级 RED 熔断**（一次触发 = 整个作用域的所有持仓） |
| `entry_trailing_cropped_long` | 90 | （入场侧）被额度裁剪的加仓 |

### 3.1 止盈数量与触发

```rust
// calc_close_qty（closes.rs:63-98）
full_psize = cost_to_qty(balance × 单槽额度, position.price, c_mult);      // 整槽规模
leftover   = max(0, |持仓| − full_psize);                                  // 超出整槽的部分
close_qty  = min(round_(|持仓|, qty_step),
                 max(calc_min_entry_qty(close_price, exchange),
                     round_up(full_psize × close_qty_pct + leftover, qty_step)));
if close_qty < |持仓| && |持仓| − close_qty < min_entry_qty { close_qty = |持仓| }   // 尘埃规则
```

即**每次止盈平掉"整槽规模 × 0.84 + 超出整槽的部分"，剩余不足最小下单量时一次平掉**。
触发在 `calc_trailing_close_long`（`closes.rs:357` 起）：阈值为
`close.threshold_base_pct + 敞口比×w + 波动率项`（`closes.rs:285-308`），
回落幅度为 `close.retracement_base_pct × 乘数`（`closes.rs:270-283`）。
本 arm 的 `close.threshold_base_pct = −0.0027` 为负 ⇒ 走"**开仓即挂**"分支
（`closes.rs:379-396`）：价格从开仓以来的最高点回落约 0.05%（0.0005 × 乘数）就止盈。

### 3.2 保护性减仓的择优（`orchestrator.rs:689-738`）

- 保护性减仓集合：panic、unstuck、TWEL（总暴露）/WEL（单币暴露）自动回收
  （`is_protective_close_reducer`，`:689-702`）。
- 同一币同一方向只保留一张，**更大的减仓优先**（`close_reducer_preference_cmp`，`:703-718`）。
- **panic 独占**：一旦选中 panic，其它减仓单全部清空（`:719-738`）。
- 本 arm 的 TWEL/WEL 回收与 wallet-exposure brake 都是关的
  （`position_exposure_enforcer_enabled=false`、`total_exposure_enforcer_enabled=false`、
  `wallet_exposure_brake_enabled=false`），unstuck 是开的
  （`threshold 0.466`、`close_pct 0.041`、`loss_allowance_pct 0.0052`、EMA 门控开）。

### 3.3 账户级熔断（HSL）——判据不是"浮亏 ≥ 阈值"

```rust
// equity_hard_stop_loss.rs
drawdown_raw   = max(0, 1 − equity / peak_strategy_equity)                  // :473
drawdown_ema   = raw + (minute_start_ema − raw) × (1−alpha)^minutes         // :482
drawdown_score = min(raw, ema)                                             // :485（含义见 :483-484）
red_active_now = drawdown_score + 1e-12 >= red_threshold                    // :490
red_latched    = …                                                          // :503-512
```

- **触发分是 `min(raw, EMA)`**：注释（`:483-484`）说明这是为了避免"恢复后 stale EMA 误触发"
  与"闪崩把仓砍在最低点"。
- 分层动作：ORANGE = 整个作用域 TpOnly（不产生任何新入场，含加仓）；RED = panic 平掉整个作用域
  再停机（`should_generate_entries`，`orchestrator.rs:2523-2531`）。
- panic 平仓单（`calc_panic_close`，`orchestrator.rs:2483-2521`）：**整仓数量**，
  long 的限价是 `round_dn(best_ask) − 1 tick`。本 arm `panic_close_order_type = "limit"`，
  所以它是一张**挂单**而不是市价单；触发瞬间的限价在跳空行情里会以跳空价成交。
- 停机与复牌：RED 确认扁平后进入 `cooldown_minutes_after_red`（本 arm 720 分钟）；
  `restart_after_red_policy = "threshold"` + `no_restart_drawdown_threshold = 1.0`
  ⇒ 允许复牌。注意**永久停机（no_restart）用的是 `max(raw, ema)`**
  （`no_restart_triggered`，`equity_hard_stop_loss.rs:206-218`），
  这正是 Round C/D 里"阈值 0.40 首击锁存、0.55/0.70 也在崩盘里锁存"的原因。
- 遥测口径：`hard_stop_panic_close_loss_drawdown_pct_*`
  = **同一次触发里所有 panic 平仓的已实现亏损之和 ÷ 事件起点的策略权益**
  （`backtest.rs:4350-4384`）。

### 3.4 入场闸门只拦入场（回答"是不是门控"）

```rust
// orchestrator.rs
/// Deliberately entry-only. Closes, panic, and auto-unstuck keep their own
/// independent paths, so a risk-off bar can still exit but cannot add risk.
fn regime_allows_entries(has_pos, allows_initial, allows_reentry) -> bool { … }   // :2533-2547
fn side_regime_verdict(s: &SymbolInput, pside) -> (bool, bool) { … }              // :2556
```

判决只在**入场生成处**被消费（`:2629-2635`、`:3795/3806`、`:3898/3909` 一带），
`TradingMode::Panic` 时 `should_generate_entries` 直接返回 false，但**平仓路径不受门控影响**。
⇒ **门控不可能造成任何清仓**。

## 4. ZEC 2026-06-05 案例（逐笔）

窗口：`2026-06-04T16:01Z → 2026-06-05T07:15Z`（`episode_trace.json::window`）。
这一轮 episode 共 **13 笔入场成交**：1 笔初始 + 10 笔普通加仓 + **2 笔被额度裁剪的加仓**。

| # | 时间 (UTC) | 类型 | 加仓前持仓 | ddf×持仓 | 初始入场托底 | 最小下单量 | 成交数量 | 成交价 | 加仓前均价 | 触发参考 ×0.97 | 成交后持仓 | 成本口径敞口 | 已裁剪 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 06-04 16:01:00 | `initial_normal` | 0.000 | 0.0000 | 0.2717 | 0.010 | **0.272** | 549.27 | — | — | 0.272 | 0.003475 | 否 |
| 2 | 06-04 17:19:00 | `trailing_normal` | 0.272 | 0.1632 | 0.2822 | 0.010 | **0.282** | 529.01 | 549.27 | 532.79 | 0.554 | 0.006944 | 否 |
| 3 | 06-04 17:50:00 | `trailing_normal` | 0.554 | 0.3324 | 0.2871 | 0.010 | **0.332** | 520.00 | 538.96 | 522.79 | 0.886 | 0.010959 | 否 |
| 4 | 06-04 21:15:00 | `trailing_normal` | 0.886 | 0.5316 | 0.2908 | 0.010 | **0.532** | 513.29 | 531.85 | 515.90 | 1.418 | 0.017310 | 否 |
| 5 | 06-04 21:41:00 | `trailing_normal` | 1.418 | 0.8508 | 0.3094 | 0.011 | **0.851** | 482.38 | 524.89 | 509.14 | 2.269 | 0.026857 | 否 |
| 6 | 06-04 22:07:00 | `trailing_normal` | 2.269 | 1.3614 | 0.3186 | 0.011 | **1.361** | 468.54 | 508.95 | 493.68 | 3.630 | 0.041688 | 否 |
| 7 | 06-04 22:35:00 | `trailing_normal` | 3.630 | 2.1780 | 0.3266 | 0.011 | **2.178** | 456.97 | 493.80 | 478.98 | 5.808 | 0.064836 | 否 |
| 8 | 06-04 23:19:00 | `trailing_normal` | 5.808 | 3.4848 | 0.3238 | 0.011 | **3.485** | 461.03 | 479.99 | 465.59 | 9.293 | 0.102205 | 否 |
| 9 | 06-04 23:45:00 | `trailing_normal` | 9.293 | 5.5758 | 0.3295 | 0.012 | **5.576** | 452.94 | 472.88 | 458.69 | 14.869 | 0.160946 | 否 |
| 10 | 06-05 00:47:00 | `trailing_normal` | 14.869 | 8.9214 | 0.3364 | 0.012 | **8.921** | 443.80 | 465.40 | 451.44 | 23.790 | 0.252968 | 否 |
| 11 | 06-05 01:13:00 | `trailing_normal` | 23.790 | 14.2740 | 0.3459 | 0.012 | **14.274** | 431.57 | 457.30 | 443.58 | 38.064 | 0.396220 | 否 |
| 12 | 06-05 02:11:00 | `trailing_cropped` | 38.064 | 22.8384 | 0.3560 | 0.012 | **3.323** | 419.40 | 447.65 | 434.22 | 41.387 | **0.428578** | **是** |
| 13 | 06-05 05:34:00 | `trailing_cropped` | 41.387 | 24.8322 | 0.3828 | 0.013 | **0.200** | 391.72 | 445.38 | 432.02 | 41.587 | **0.428569** | **是** |

读法：

1. **第 2–11 笔：每笔 = 加仓前持仓 × 0.6**（持仓 ×1.6）。第 2 笔是唯一例外：`0.6 × 0.272 = 0.1632`
   小于**初始入场托底** 0.2822，所以按托底成交 0.282（这让整条阶梯比纯几何略陡一点）。
2. **第 12、13 笔被单槽额度接管**：
   - 第 12 笔若全额成交，敞口会到 **0.6189**（> 额度 0.428571）⇒ 裁剪量
     `(0.428571 − 0.396174) × 43,009.89 / 419.40 = 3.32` ⇒ 成交 3.323，敞口落在 **0.428578**；
   - 第 13 笔只剩 `(0.428571 − 0.426755) × 43,193.56 / 391.72 = 0.200` ⇒ 成交 0.200，敞口 **0.428569**
     （未裁剪时本会到 **0.6520**）。
   即"额度用满后，加仓只能按剩余额度裁剪，几何增长再也用不上"。
3. 每笔成交价都**优于**朴素触发参考 `加仓前均价 × 0.97`（阈值乘数 ≥ 1 只会让挂单价更低，
   实际成交还常常更优），说明加仓确实是"越跌越买"的回落触发。

### 4.1 清仓那一笔

| 项 | 值 |
|---|---|
| 成交时间 / 类型 | `2026-06-05T07:15:00Z` / `close_panic_long` |
| 数量 | **−41.587**（= panic 前的全部持仓，`closes_full_position = true`） |
| 成交价 / 持仓均价 | 252.12 / **445.1253** ⇒ 该币相对均价 **−43.36%** |
| 该币已实现亏损 | **−8,026.51 USDT** |
| 同分钟同批被平的币 | INJ、NEAR、ONDO、RENDER、**ZEC**（5 个） |
| 同批合计已实现亏损 | **−10,105.15 USDT = 触发前余额 43,193.49 的 −23.40%** |
| 引擎遥测 | `hard_stop_triggers = 1`；触发时 `drawdown_raw = 0.2298`；确认时 `panic_close_loss_drawdown_pct_mean = 0.3003`；`flatten_time_minutes_mean = 2.0`；停机 720 分钟、`restarts = 1` |

### 4.2 「浮亏到余额 20% 左右」的直觉，与引擎口径的差别

- 你的直觉读法（相对**当前余额**的浮亏/ZEC 自身浮亏）不是触发判据。
  引擎的触发判据是 **`min(raw, EMA)` 相对"7 天滚动峰值策略权益"的回撤**
  （`live.pnls_max_lookback_days = 7.0`）：触发那一刻 `raw = 0.2298`，
  而本 arm 的 `red_threshold = 0.15`（`b_red015`）⇒ 0.2298 ≥ 0.15 触发。
  ZEC 自身相对均价的 −43.36% 只是"被平掉的那个币跌了多少"，不是触发原因。
- **触发 ≠ 成交**：RED 先出 panic 挂单，等成交确认"作用域已扁平"才算完成；
  本 arm 的确认耗时 2 分钟，而确认时的口径（同批已实现亏损 ÷ 事件起点权益）是 **0.3003**。
  也就是说，急跌里"确认后才动作"必然付出 **触发 → 确认** 的价差：
  账户在触发时回撤 22.98%，最后按 30.03% 兑现。
- 同一次触发里被平的**不止 ZEC**：`unified` 作用域会把整个账户的持仓一起 panic
  （本 arm 同一分钟 5 个币），所以"某币浮亏 20% 就被清仓"这个观察，
  实际是"账户回撤触发 + 整户清仓，ZEC 只是其中一员"。

## 5. 对策略设计的含义

1. **为什么加仓是 ×1.6 的几何**：`double_down_factor = 0.6` 是"加仓量 = 当前持仓的 60%"，
   于是持仓按 `0.272 → 0.554 → 0.886 → 1.418 → … → 38.064` 复利式膨胀（10 笔涨到 140 倍首仓）。
   这是 martingale 的收益来源，也是它的尾部来源：**成本口径敞口从 0.35% 涨到 39.6%，只用了 11 笔**。
   对仓位比例的直觉若按"余额的百分之几"去读，就会看不懂——它按的是**持仓的百分之几**。
2. **为什么单槽额度会在第 11 笔加仓后接管**：本 arm 的额度是账户余额的 42.86%
   （`TWE 3.0 / 7 槽 / allowance 0`）。持仓到 38.06 手时成本敞口已是 0.3962，
   下一笔几何加仓（22.84 手）会直接顶到 0.6189 ⇒ 只能裁剪到"剩余额度 ÷ 价格"
   （3.32 手、随后 0.20 手）。**额度一旦用满，几何增长被替换成"剩余额度插值"**，
   这也解释了为什么后期的加仓看起来"数量不成比例"。
3. **为什么"确认后才动作"必然付价差**：RED 的触发分是 `min(raw, EMA)`，
   它天然比 `raw` 慢（要等 EMA 追上），而 panic 又要等成交确认；
   在 2026-06-05 这种急跌里，触发 22.98% → 兑现 30.03%，中间那 7 个百分点就是
   "确认"的成本。想要更接近触发价成交，只能改机制（更快触发 / 触发即市价 / 预挂保护单），
   这属于引擎级改动，需要独立设计与回测。
4. **本 arm 的取舍**：`b_red015`（RED 0.15 + allowance 0）在整个 Round D 里是
   "同档杠杆下同时改善收益与回撤"的那一档（3 年腿 3.61×、全历史腿 4.3033× / 最差回撤 56.52%），
   代价是 5 次 panic 事件把浮亏变成实亏——本案例的 −8,026 USDT 就是其中一次。

## 6. 这份说明书怎么被复核

- 逐笔阶梯与 panic 群体由 `report_tools/trace_episode.py` 从 `fills.csv` + `config.json`
  （+ 冻结数据集的 `market_specific_settings.json`）重算，落盘为 run 目录里的 tracked 证据
  `episode_trace.json`；`run.sh` 的 `== 3b/5 ==` 步会幂等地重建它。
- `report_tools/verify_variant_report.py::check_episode_trace` **不读 trace 的数字来通过校验**：
  它调用 `trace_episode.derive_episode` 从账本独立重算整条阶梯与 panic 群体、
  逐字段比对记录值，并用一段独立的 pandas 算术（持仓自洽、×1.6 增长、裁剪落在额度上、
  panic 平掉整仓）再核一遍；任何不一致都判为 problem。
- 本文件引用的行号对应 `passivbot-rust/src/{entries,closes,orchestrator,utils,dynamic,equity_hard_stop_loss,backtest}.rs`。
