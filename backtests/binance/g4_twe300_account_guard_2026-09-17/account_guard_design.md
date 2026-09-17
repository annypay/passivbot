# 账户级风控的引擎化设计：从"固定熔断"到"阶梯式守卫"

这份文档是本研究的**设计部分**，回答"要把你描述的那种账户级硬风控真正做进引擎，需要改什么、风险在哪、先做什么"。
所有关于现有能力的判断都来自对当前代码的逐条核对（`passivbot-rust/src/equity_hard_stop_loss.rs`、
`passivbot-rust/src/backtest.rs`、`passivbot-rust/src/orchestrator.rs`、`src/config/schema.py`），
不是推测。

## 1. 现有引擎已经能做什么（能力地图）

| 能力 | 现状 | 出处 |
| --- | --- | --- |
| 账户级作用域 | `live.hsl_signal_mode ∈ {coin, pside, unified}`；`unified` = 整个账户 | `src/backtest.py::_resolve_backtest_hsl_signal_mode` |
| 触发指标 | `drawdown_score = min(raw, EMA)`；`raw = 1 − equity/peak` | `equity_hard_stop_loss.rs` |
| 峰值窗口 | 滚动窗口 `live.pnls_max_lookback_days`（可设 7 天） | `backtest.rs::update_hard_stop_state_pside_at_boundary` |
| 第二套峰值 | `no_restart_peak_strategy_equity`：**跨 episode / 跨重启持久** | 同上 + `RedEpisodeFinalization` |
| 分层动作 | YELLOW 仅遥测；**ORANGE = 整个作用域 TpOnly（不产生任何新入场，含加仓；只走止盈）**；**RED = Panic 平掉整个作用域 + 停机** | `orchestrator.rs::should_generate_entries/should_generate_closes`、`backtest.rs::apply_hard_stop_mode_overrides` |
| 停机 | `cooldown_minutes_after_red`（**单一常量**），冷却期内该作用域不交易 | `equity_hard_stop_loss.rs` |
| 终止 | `restart_after_red_policy=threshold` + `no_restart_drawdown_threshold`（用 `max(raw, EMA)` 判定，可被尖峰锁存）；`never` = 一律终止 | `equity_hard_stop_loss.rs::no_restart_triggered` |
| episode 重置 | 作用域确认平仓后，滚动峰值与 EMA 一并清空（`rolling_peak_strategy_pnl.clear()`）⇒ **第二次触发天然以"剩余余额"为基准** | `backtest.rs` |
| 实盘重建 | HSL 状态由交易所状态 + 成交历史 + 配置重建，本地文件只是诊断 | `docs/ai/features/equity_hard_stop_loss.md` |

**结论**：你描述的"一周内浮亏 20% → 熔断 12H"和"第二次按剩余余额再算 20%"**已经可以表达**（`unified` + `pnls_max_lookback_days=7` + `red_threshold=0.20` + 小 `ema_span_minutes` + `cooldown=720`）。
唯一不能表达的是**"第二次升级为 24H"这个阶梯本身**——停机时长是单一常量。

## 2. 缺口与近似

| 你的要求 | 现状 | 本研究的近似 |
| --- | --- | --- |
| 第二次触发后停机更久（12H → 24H → …） | 不可表达 | ① 全局 24H（`cooldown=1440`）；② `no_restart_drawdown_threshold` = 累计回撤到某水平后**永久**停机（比 24H 更严） |
| "浮亏"仅指未实现亏损 | 触发用"策略权益回撤"（含已实现亏损） | 更保守；若要严格区分需要新指标（见 §3） |
| 冷却期内部分复入（半仓恢复） | 不可表达（冷却期完全不交易） | 无；可用更短冷却 + ORANGE 停加仓近似 |
| 与日线闸门联动（闸门 risk-off 时不复牌） | 不可表达 | 无；两者独立生效 |

## 3. 引擎级方案（若采纳）：`bot.<pside>.guard`

设计目标：**默认行为与现状完全一致**，只有显式配置才启用；与现有 HSL 共用 episode/panic 通道，避免两套熔断互相打架。

```jsonc
"bot": { "long": { "guard": {
  "enabled": false,                       // 默认关闭；关闭时引擎行为与今天逐位相同
  "scope": "unified",                     // coin | pside | unified（复用 HSL 的作用域语义）
  "lookback_days": 7.0,                   // 峰值窗口
  "measure": "equity_drawdown",           // equity_drawdown（现状）| unrealized_only（新增指标）
  "trigger_pct": 0.20,                    // 第一级：触发清仓+停机
  "ema_span_minutes": 60.0,               // 触发速度
  "orange_pct": 0.20,                     // 停加仓阈值（TpOnly），可与 trigger 同级或更低
  "halt_ladder_minutes": [720, 1440, 2880], // 阶梯：第 1/2/3 次触发分别停机多久
  "ladder_reset": "new_high",             // new_high（创新高后清零）| rolling_days（N 天后清零）| never
  "terminal_drawdown_threshold": 0.45,    // 累计（跨重启持久峰值）达到即永久停机
  "reentry_scale_after_halt": 1.0,        // 复牌后暴露上限缩放（0.5 = 半仓恢复）
  "require_gate_risk_on_to_resume": false // 复牌前是否要求入场闸门 risk-on
}}}
```

**状态机**（每个作用域一份，跨重启可由交易所状态重建）：

```
peak  = 滚动窗口内最高策略权益（lookback_days）
raw   = 1 − equity / peak ;  score = min(raw, ema)
orange_active = score ≥ orange_pct            → 作用域 TpOnly（停加仓、只止盈）
if score ≥ trigger_pct and not halted:
      strike_index = strikes_this_cycle + 1
      halt_minutes = halt_ladder_minutes[min(strike_index, len(ladder)) - 1]
      Panic 平掉作用域 → 进入冷却 halt_minutes
episode 结束（作用域确认平仓）:
      strikes_this_cycle += 1
      滚动峰值/EMA 清空（现状行为；下一级的基准 = 剩余权益）
      if 累计回撤（跨重启持久峰值）≥ terminal_drawdown_threshold: 永久停机（no_restart 锁存）
      else 冷却到期后按 reentry_scale_after_halt 恢复
新周期开始（创新高，或 ladder_reset=rolling_days 到期）: strikes_this_cycle = 0
```

**实盘重建契约**（必须与现有 HSL 一致，否则不能上线）：
- 峰值、EMA、`strikes_this_cycle`、终止锁存全部由**成交历史 + 当前权益 + 配置 + 当前时间**重建；本地文件只能是诊断。
- 成交历史不完整时 fail-closed：保持保护态并重试（沿用 `live.risk_input_max_attempts` 与 HSL replay 的既有路径）。
- `hsl-startup-preview` / `live-config-preflight` 需要展示重建出的 `strikes_this_cycle` 与下一级停机时长。

**测试矩阵**：
1. Rust 单测：阶梯递进（第 1/2/3 次）、`ladder_reset` 三种模式、终止阈值、`reentry_scale`、边界（ladder 长度耗尽、`halt_minutes=0`、`orange_pct ≥ trigger_pct` 的拒绝）。
2. 现有 parity 套件：Metal/GPU 与 exact Rust 的 HSL 追踪一致性测试需覆盖新字段（`docs/equity_hard_stop_loss_reference.md` 里的 M3 一致性用例）。
3. Python：配置 schema + normalize + CLI 覆盖 + `live-config-preflight`；`tests/test_unstucking_safeguards.py` 的 pside/unified 终结覆盖需要增加阶梯分支。
4. 回测级：本研究的 arm 矩阵模式（同数据集、同一冻结配置 + 声明改动）直接复用，验证"阶梯 vs 固定"的差异。
5. 兼容性：`enabled=false` 时逐位一致（可用现有 `tests/` 的确定性回放断言）。

**风险与回滚**：这是 trading-critical 的 Rust 行为变更 → 独立 PR、默认关闭、`CHANGELOG` + `docs/equity_hard_stop_loss*.md` 同步、以及"关闭时逐位一致"的回归；上线前用 `hsl-startup-preview` 在真实账户上核对重建结果。

## 4. 决策门槛（先做实验，再决定改不改引擎）

1. 如果本研究发现**分级近似（−20% 停加仓 + 更深阈值清仓 + 永久地板）**能在两条腿上同时满足"活着 + 可接受代价"，那么阶梯的必要性下降：先用配置解决。
2. 如果 5.4 年腿上**所有守护臂仍被强平**（说明崩盘快到任何"确认后动作"的机制都来不及），那么引擎级阶梯也救不了——真正的解法是**结构性降低暴露上界**，而不是加更多熔断层。
3. 只有一种情况值得做引擎级阶梯：分级近似在温和窗口表现良好、但在历史腿上"差一点点"（例如只差一次更长的冷却就能躲过第二波）。这个判断标准在 `account_guard_analysis.md` 的判据 J1–J5 里已经预注册。

## 5. 运营层（不改引擎也有效的三层）

1. **结构性上限**：`total_wallet_exposure_limit` / `n_positions` / `we_excess_allowance_pct`——唯一"确定"压低尾部的手段（前两轮实测）。
2. **盘外留存**：只把一部分净值放在交易所（operating float）+ 按规则划出利润；它不改变策略的收益风险比，但把"交易所/稳定币/极端事件"的敞口按比例缩小。
3. **人工/外部熔断**：把账户级守护的触发与"人工复核清单"绑定（例如触发后必须人工确认再复牌），这部分无法用回测证明，只能写进 runbook。
