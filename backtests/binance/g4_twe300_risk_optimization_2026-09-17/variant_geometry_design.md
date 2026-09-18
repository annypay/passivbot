# 风险几何与参数搜索：本轮设计与下一轮引擎阶梯的定档依据

这份文档是本研究的**设计部分**：它说明本轮为什么只动"风险几何"、搜索契约是怎么冻结的、
占用纪律（`we_excess_allowance_pct = 0`）在引擎里的确切含义，以及下一轮引擎级冷却阶梯要做什么、
档位怎么定。所有关于引擎行为的判断都来自对当前代码的逐条核对，不是推测。

## 1. 本轮只动"风险几何"，不动 alpha

前三轮已经把结论收敛到一句话：**TWE 3.0 / 10,000 USDT 在本地历史里会被强平，账户级守护能救回来但很贵。**
本轮要回答的是"能不能用更便宜的方式买到同一份生存"，因此把变量严格限制在**暴露几何**与**守护几何**上：

| 维度 | 路径 | 为什么它是"风险几何" |
|---|---|---|
| 总暴露上限 | `bot.long.risk.total_wallet_exposure_limit` | 直接决定到强平地板的距离（满仓 3.0 约 −31.8%、2.5 约 −40%、2.0 约 −50%） |
| 槽位 | `bot.long.risk.n_positions` | 与上限共同决定均分额度 `TWE / n_positions` |
| 占用余量 | `bot.long.risk.we_excess_allowance_pct` | 允许单币吃掉**空槽**的额度；=0 时每币只有自己的均分额度 |
| 触发线 / 速度 / 冷却 | `bot.long.hsl.red_threshold` / `ema_span_minutes` / `cooldown_minutes_after_red` | 账户级守护的三个时间尺度 |

**没有动**的东西：`forager`（选币）、`strategy.trailing_martingale`（网格与 DDF）、`unstuck`、
`risk.entry_cooldown_minutes`、入场闸门（20/50 日均线）、执行与成本契约、币池、资金规模。
搜索配置用 `-ft/--fine-tune-params` 只放行上述六个维度：优化器在 hydration 时会把引擎默认 bounds
展开进来，只有 `-ft` 能把其余全部 bounds 固定到当前配置值；运行日志会打印 tunable/fixed 两个集合，
`run_search.py` 在运行后核对 tunable 集合必须恰好是这六个（否则整轮搜索判失败）。

## 2. 占用纪律的确切语义（`we_excess_allowance_pct = 0`）

引擎在**下单规划时**把每个币的上限算成：

```
base_limit         = total_wallet_exposure_limit / n_positions
effective_allowance = min(raw_allowance, total_wallet_exposure_limit / base_limit - 1)   # bounded 模式
per_coin_cap       = base_limit * (1 + effective_allowance)
```

`src/risk_limits.py::effective_we_excess_allowance_pct` 是这段话的实现；在本研究的取值域内
`total/base - 1 = n_positions - 1`，永远大于 raw，所以 raw 就是生效值。于是：

- `allowance = 0.37`（父配置）⇒ 单币上限 = 均分额度 × 1.37（TWE 3.0 / 7 槽 ⇒ 0.5871）；
- `allowance = 0` ⇒ 单币上限 = 均分额度（⇒ 0.4286）。

"不占用空槽的额度"因此有可检验的含义：**单币敞口上限与单币归零上界同时下降**，
而账户的总暴露上界不变。本研究用 `risk_geometry.json` 把这两件事分开记录：
`per_slot_cap`（声明值）、`peak_coin_exposure`（实测峰值）、`single_coin_wipeout_bound`（该币归零的账户损失上界）、
`peak_coin_share_of_peak_total`、`active_coins_mean` 与 `empty_slot_time_share`（占用本身）。

**一个必须一起读的实测细节**：单槽上限是"规划时"的约束，而成交账本记录的是成交时的
`wallet_exposure`。盯市漂移与**交易所最小下单量**（额度被裁到 0 时仍会成交最小可交易数量）
会让实测值略高于上限——本轮 allowance=0 的臂最高超出 3.5%，造成它的那笔成交被记录在
`cap_overshoot_fill` 里（例如 AVAX 的 1.0 张最小加仓）。几何不变量因此用 5% 的执行余量校验，
报告同时给出实测超出比例；这不是"上限没生效"，而是"上限 + 执行摩擦"。

## 3. 三条腿：搜索窗、全历史、样本外

| 腿 | 窗口 | override | 角色 |
|---|---|---|---|
| `3y` | 2023-09-12 → 2026-09-12 | 父配置自带（`intersection`） | **搜索窗（样本内）** |
| `ext` | 2021-04-20 → 2026-09-13 | `dataset`（服务整段 bundle） | 全历史；含搜索窗，只有 2021-05 崩盘段属于样本外 |
| `pre` | 2021-04-20 → 2023-09-11 | **`intersection`**（按配置窗口切片） | **样本外验收窗**，与搜索窗不重叠 |

`pre` 腿复用长 bundle：`dataset` 模式会服务整段 bundle（`src/hlcvs_override.py`：`effective_end_ts = dataset_end_ts`），
只有 `intersection` 模式才会切成 `min(requested_end, dataset_end)`，因此样本外腿必须用 `intersection`，
而且不新增任何数据。`build_variant_config.py` 会校验：`intersection` 腿的 bundle 必须**覆盖**声明窗口。

## 4. 搜索契约（冻结在 `artifacts/variant_input.json`）

| 项 | 值 |
|---|---|
| 实现 | 仓库既有 pymoo 优化器（`optimize.backend=pymoo`），只在 `3y` 腿上运行 |
| 维度（6） | TWE ∈ [2.0, 3.0] 步长 0.05；`n_positions` ∈ [5, 10] 步长 1；allowance ∈ [0, 0.37] 步长 0.01；RED ∈ [0.15, 0.35] 步长 0.01；EMA ∈ [15, 120] 步长 15；冷却 ∈ [720, 4320] 步长 720 |
| 目标 | `adg_strategy_eq` 最大；`drawdown_worst_strategy_eq` 最小（双目标 Pareto，不用复合分） |
| 约束 | 惩罚 `drawdown_worst_strategy_eq > 0.60`；惩罚 `backtest_completion_ratio < 0.99`（强平/早停） |
| 预算 | `seed=20260917`、`iters=1024`、`population_size=64`（pymoo 世代 = iters/人口 = 16）、`n_cpus≤2` |
| 选臂规则 | 先过滤 `completion ≥ 0.99` 且 `dd ≤ 0.50`；再取 max ADG、min DD、拐点 `max ADG×(1−DD)`；去重后 ≤6 个候选 |
| 兜底 | 若优化器在本机内存/离线门失败 ⇒ 改用 `SEARCH_FALLBACK_CELLS` 的 6 个声明式联合格点（并在报告里声明走的是兜底路径） |

候选冻结后，每个候选在三条腿上各重放一次（`c*__3y` / `c*__ext` / `c*__pre`），
provenance（pareto 文件路径 + sha256 + 指标 + 选择的种类）写进 `run_record.json` 与报告的
"参数搜索与选择轨迹"。**看到样本外结果之后不再搜索**（预注册）。

## 5. 下一轮：引擎级冷却阶梯（本轮只定档，不实现）

本轮能表达的只有"固定冷却档位"（12/24/48/72H）、"累计阈值二档"（`restart_after_red_policy=threshold`
+ `no_restart_drawdown_threshold`）与"实现亏损刹车"（`live.max_realized_loss_pct`，panic 豁免）。
真正的"第 N 次触发停更久"必须在引擎里加状态：

```
halt_ladder_minutes: [720, 1440, 4320]      # 第 1/2/3 次触发
ladder_reset:        "pre_strike_peak"      # 回到本周期的触发前峰值即清零（可重建）
```

实现范围（独立 PR，默认关闭，Rust + 实盘重建契约 + 测试矩阵）：

1. **Rust**：`BotParams.hsl_halt_ladder_minutes: Vec<f64>`；`HardStopRuntime.strikes_this_cycle`；
   `evaluate_red_episode_finalization` 按下标选冷却；`ladder_reset` 用现有峰值追踪判定。
2. **实盘重建**：`strikes_this_cycle` 必须能从成交历史的 panic episode tape（自上次"触发前峰值"以来）
   重建，沿用 `docs/ai/features/equity_hard_stop_loss.md` 的 invariant 6/8 与 readiness 路径；
   `hsl-startup-preview` / `live-config-preflight` 要展示重建出的档位。
3. **测试**：Rust 单测（阶梯递进、reset、ladder 耗尽、`halt_minutes=0`、与 terminal 锁存的交互）、
   parity 套件覆盖新字段、Python 配置归一化 + 回放计数、fake-live 场景（两次触发看到 12H → 24H）。
4. **档位取值**来自本轮 J3 的裁决：若更长档位在 ext/pre 两条腿上都不差于 12H，最高档取证据支持的
   最大档；若所有档位都不优于 12H，则阶梯只剩"风险塑形"理由，最高档取 24H 并明确写进文档。

**为什么不做成一次性永久关停**：上一轮已实测 `no_restart_drawdown_threshold=0.40` 在第一次触发就被
RAW 尖峰锁存（确认回撤 46.9%），账户活到窗口末却 1942 天零交易——"未强平"不等于"还活着"。
阶梯要接在**冷却时长**上，不是接在"关停"上。

## 6. 决策门（本轮的产出如何被使用）

1. **J1/J2**：占用纪律是否有效、推荐的结构设定（TWE × allowance）——直接决定下一步配置模板。
2. **J3**：冷却档位——直接决定下一轮引擎阶梯的 `halt_ladder_minutes` 取值。
3. **J4**：累计二档是否可用（若再次首击锁存 ⇒ 永久停机改用已实现亏损口径，属于引擎改动）。
4. **J5**：搜索是否找到优于手写守护的点——若否，说明"搜索预算/参数域/目标函数"需要重新设计，
   而不是继续在同一域里加预算。
5. **J6**：结构 vs 熔断的首选杠杆与叠加性——决定"先做什么"的顺序。
