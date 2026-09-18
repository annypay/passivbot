# g4 @ TWE 3.0 的引擎级冷却阶梯与累计已实现亏损口径：声明式重放验证（2026-09-18）

**一句话结论：两个键都按契约生效，也都没有买回收益。**默认关闭时引擎与改动前**逐位一致**（`ext` 腿 45,644 笔成交哈希相同，而该腿上守护触发了 4 次）；冷却阶梯确实按第 N 次触发递进（`ext` 腿 4 次停机平均 1260 分钟 = (720+1440×3)/4），但终值只 +0.24%、最差回撤逐位相同，样本外 `pre` 腿甚至略差；0.30 的累计已实现亏损预算确实按已实现回吐锁存（锁存时累计回吐 67.4%），代价是**第一次停机就永久退出**（0.3399×）。真正的差异来自**第一档**：第一档 12H/24H 的取舍在两条腿上给出相反答案（`ext` 24H 更好、`pre` 12H 更好），这正是本轮过拟合审计要说明的问题。

> 四个**预注册**臂（`l0_off` / `l1_ladder` / `l2_budget` / `l3_fixed24`）在三条腿
> （上一轮的搜索窗 `3y`、全历史 `ext`、样本外 `pre`）上重放两个默认关闭的引擎改动：
> 冷却阶梯 `bot.long.hsl.halt_ladder_minutes` 与累计已实现亏损预算
> `bot.long.hsl.realized_loss_budget_pct`。**本轮不新增任何参数搜索**：没有搜索契约、没有取值域、
> 没有候选注册表，四个臂与判据在 [`halt_ladder_design.md`](halt_ladder_design.md) §7 冻结。
> 完整的键、公式、周期与重建契约见该设计冻结点。

## 1. 这轮在验证什么（设计冻结点 §7 的四条判据）

两个改动都**默认关闭**，关闭时引擎行为与今天逐位相同。四条预注册判据：

| 判据 | 臂 | 变量 | 预注册判据 |
|---|---|---|---|
| **K0** 默认关闭回归 | `l0_off` | 两个新键都是默认值（`[]`、`0.0`） | 与上一轮同几何臂（`a_allow000`，钉住的 tracked evidence）**逐位一致**：成交签名 sha256、终值、最差回撤、全部 `hard_stop_*` 读数 |
| **K1** 阶梯生效 | `l1_ladder` | `halt_ladder_minutes = [720, 1440]` | 第 2 次触发看到 1440（`hard_stop_duration_minutes_max`），档位可分辨且不低于 `l0_off` |
| **K2** 累计口径 | `l2_budget` | `no_restart_drawdown_threshold = 1` + `realized_loss_budget_pct = 0.30` | 不再因瞬时尖峰锁存；若锁存则 `no_restart_reason == "realized_loss"` 且 `hard_stop_realized_loss_halt_pct_max >= 0.30` 可复算 |
| **K3** 阶梯 vs 单纯加长冷却 | `l3_fixed24` | `halt_ladder_minutes = [1440]` | 与 `l1_ladder` 的逐位差异**只能**由档位选择解释：`strikes_max = n` 时两臂的冷却必须分别是 `(720+1440×(n−1))/n` 与 1440 |

> **K3 判据勘误（预注册后、看到任何结果之前修正）**：初稿写成"`strikes_max <= 1` 时两臂必须逐位
> 相同"是错的——`strikes_max == 1` 时 `l1_ladder` 用第一档 720、`l3_fixed24` 用 1440，两臂本就应当
> 不同。可检验的陈述是上表这一条：差异必须完全由档位选择解释。实测确实如此，而且差异集中在
> **第一档**：`ext` 腿上把第一档从 12H 改成 24H，停机次数从 4 降到 3、终值 2.3057× → 2.7662×；
> 但 `pre` 腿上同一改动让终值从 0.6104× 掉到 0.5888×。两条腿给出相反答案。

两个键的机制（细节见设计冻结点 §2/§3）：

- **阶梯**：第 N 次 RED 停机取 `ladder[min(N, len) - 1]`，耗尽后夹在最后一档；**周期**定义为
  作用域策略权益未回到本周期峰值的那段时间，权益重新达到周期峰值时计数与峰值一起清零。
  阶梯启用时 `cooldown_minutes_after_red` 被忽略。
- **累计已实现亏损**：在 episode 终结（平仓确认）时判定
  `max(0, cycle_realized_pnl_peak − realized_pnl_now) / ladder_cycle_peak_equity >= budget`；
  只在 `restart_after_red_policy == "threshold"` 下与瞬时回撤判据取 OR，
  `no_restart_drawdown_threshold = 1` 即关掉瞬时口径（该键父配置本已在上界）。

`analysis.json` 本轮新增两个读数：`hard_stop_ladder_strikes_max`（任何停机周期里达到的最高档位
下标，阶梯关闭时为 0）与 `hard_stop_realized_loss_halt_pct_max`（锁存永久停机的累计已实现回吐，
未锁存时为 0）；`hard_stop_duration_minutes_max` 现在报**实际使用的那一档**。

## 2. 四个臂与三条腿

四个臂都继承上一轮验证过的 `a_allow000` 几何：`we_excess_allowance_pct = 0`、TWE 3.0、
`n_positions = 7`、10,000 USDT 起始资金、账户守护 unified / RED 0.20 / EMA 60 分钟 / 停 12H /
7 天峰值窗口。每个臂的完整 `(path, from, to)` 声明表冻结在 `artifacts/variant_input.json`。

| 臂 | 声明式改动（相对冻结父配置） | 目的 |
|---|---|---|
| `l0_off` | `halt_ladder_minutes: [] → []`、`realized_loss_budget_pct: 0.0 → 0.0` | 默认关闭的回归证据（K0） |
| `l1_ladder` | `halt_ladder_minutes: [] → [720, 1440]` | 阶梯：第 1/2 次及以后 12H / 24H（K1） |
| `l2_budget` | `no_restart_drawdown_threshold: 1 → 1`、`realized_loss_budget_pct: 0.0 → 0.30` | 累计已实现亏损口径（K2） |
| `l3_fixed24` | `halt_ladder_minutes: [] → [1440]` | 对照：把阶梯换成单一 24H 档（K3） |

| 腿 | 窗口 | 角色 |
|---|---|---|
| `3y` | 2023-09-12 → 2026-09-12 | 原生窗口（**上一轮**参数搜索所用的窗；本轮不搜索，几何在该窗上是样本内） |
| `ext` | 2021-04-20 → 2026-09-13 | 全历史（含上一轮搜索窗；只有 2021-05 崩盘段算样本外） |
| `pre` | 2021-04-20 → 2023-09-11 | **样本外验收窗**（同一份长 bundle 经 `intersection` override 切出，与搜索窗不重叠） |

共 4 臂 × 3 腿 = 12 个 run，每个 run 一份深度分析（`annual_analysis.md`）。

## 3. 复现命令

```bash
# 端到端（冻结 → 逐臂离线重放 → 渲染 → 布局检查 → 独立复核）
bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh

# 只冻结（12 份 arm 配置 + variant_input.json）
venv/bin/python backtests/binance/g4_hsl_halt_ladder_2026-09-18/report_tools/build_variant_config.py --force

# 单臂 / 单腿 / 单阶段（每个臂一个阶段：L0 / L1 / L2 / L3）
bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh --variant l0_off__ext
bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh --leg pre
bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh --stage L1

# 只复核已有 run（不重放）
bash backtests/binance/g4_hsl_halt_ladder_2026-09-18/run.sh --verify-only
venv/bin/python backtests/binance/g4_hsl_halt_ladder_2026-09-18/report_tools/verify_variant_report.py --all --allow-missing
```

离线边界与内存：无网络、无凭据、不启动实盘；本机约 7.8 GB 内存，`run.sh` 在每条腿开跑前用
`free -m` 检查（长腿 ≥ 5200 MB、原生 3 年腿 ≥ 2600 MB），**同一时刻只跑一条腿**。
所有臂关闭逐币成交面板（`--disable_plotting coin_fills`，内存峰值），报告逐臂声明。

## 4. 结果

十二条腿的数字都来自 `artifacts/<arm>/backtest_results/.../analysis.json`；默认关闭的一致性另有
独立复算 `artifacts/default_off_identity.json`（`report_tools/default_off_identity.py`，可重跑）。

### 4.1 `3y` 腿（上一轮的搜索窗）：四个臂逐位相同

| 读数 | 四个臂（`l0_off` / `l1_ladder` / `l2_budget` / `l3_fixed24`） |
|---|---|
| `gain_strategy_eq` | 4.68256（四臂相同） |
| `drawdown_worst_strategy_eq` | 0.230521（四臂相同） |
| `hard_stop_triggers` / `hard_stop_restarts` | 0 / 0（四臂相同） |
| 成交笔数 | 28,550（四臂相同） |

这一几何在 `3y` 腿上**没有任何守护活动**（`we_excess_allowance_pct = 0` 把回撤压到 RED 之下），
所以阶梯与预算在那里无从生效——它是 K0 的"无停机分支"，**不能**用来验证阶梯。这一条本身值得记录：
上一轮把 3y 当搜索窗时，守护参数在该窗上其实是惰性的。

### 4.2 `ext` 腿（全历史）：两个键都生效，但都没有买回收益

| 读数 | `l0_off` | `l1_ladder` | `l2_budget` | `l3_fixed24` |
|---|---|---|---|---|
| `halt_ladder_minutes` | `[]` | `[720, 1440]` | `[]` | `[1440]` |
| `realized_loss_budget_pct` | 0.0 | 0.0 | 0.30 | 0.0 |
| `gain_strategy_eq` | **2.30016** | **2.30568** | **0.339865** | **2.76619** |
| `drawdown_worst_strategy_eq` | 0.765622 | 0.765622 | 0.678543 | 0.768474 |
| `adg_strategy_eq` | 0.0004225 | 0.0004237 | −0.0005471 | 0.0005161 |
| `hard_stop_triggers` / `restarts` | 4 / 4 | 4 / 4 | **1 / 0** | 3 / 3 |
| `hard_stop_duration_minutes_mean` | 720 | **1260** | 2,797,150 | 1440 |
| `hard_stop_duration_minutes_max` | 720 | **1440** | 2,797,150（终局） | 1440 |
| `hard_stop_ladder_strikes_max` | 4 | 4 | 1 | 3 |
| `hard_stop_realized_loss_halt_pct_max` | 0 | 0 | **0.673817** | 0 |
| `hard_stop_time_in_red_pct` | 0.00102 | 0.00178 | **0.98502** | 0.00152 |
| `hard_stop_panic_close_loss_sum` | 11,077.5 | 11,089.1 | 7,120.45 | 9,046.18 |

**K0（默认关闭逐位一致）通过。** 独立复算：`l0_off` 与上一轮同几何臂在**两条腿**上 31 项指标与成交
磁带内容哈希全部相同——`3y` 28,550 笔、`ext` **45,644 笔**（sha256 `790a9ef0…`），而 `ext` 腿上守护
确实触发了 4 次。这不是"没触发所以看不出来"，而是"触发了 4 次仍然逐位相同"。

**K1（阶梯生效）机制通过、效果中性。** 平均停机时长 1260 分钟正好等于 `(720+1440×3)/4`、
`max = 1440`，说明第 1 次用 720、其后都用 1440；`strikes_max = 4` 说明四次停机落在**同一个**阶梯
周期里（权益始终没有回到周期峰值，符合契约）。但终值只从 2.30016 抬到 2.30568（+0.24%），最差回撤
**逐位相同**（0.765622），停机次数也没减少。也就是说：在本轮这条腿上，阶梯既没有改进收益、也没有
塑形风险——设计件 §7 的"风险塑形"预期**没有得到确认**。

**K2（累计已实现亏损口径）机制通过、标定值被证伪。** 锁存被明确归因到累计口径
（`no_restart_drawdown_threshold = 1` 让瞬时口径不可能触发，而
`hard_stop_realized_loss_halt_pct_max = 0.673817 > 0`），口径本身按契约工作。但：

1. **预算只在停机确认那一刻被检查**，因此它挡不住比预算更深的回吐：预算 0.30，实际锁存时累计回吐
   已经是 **67.4%**。把它读成"最多亏 30%"是错的。
2. **0.30 的预算在这条腿上等价于"第一次停机就永久退出"**：`restarts = 0`、
   `time_in_red_pct = 0.985`、终值 **0.3399×**（对照 `l0_off` 的 2.3002×）。
3. 它的回撤读数更低（0.6785 < 0.7656）**不是优点**：账户在 2021 年就停止交易了——与上一轮
   `no_restart_drawdown_threshold = 0.40/0.55/0.70` 的失败模式完全一样（0.3399× / 0.2910×）。
   换口径**没有**修好"永久关停代价过高"，它修的是**归因**：现在扣动关停的是一次已经实现的回吐，
   而不是一次未实现的尖峰。

**K3（阶梯 vs 单纯加长冷却）两者不等价，差异由第一档主导。** 两臂的冷却都收敛到 1440
（`l3_fixed24` 从第一次就是 1440），但 `l3_fixed24` 只触发 3 次而不是 4 次，终值 2.76619 vs 2.30568。
即：**把第一档从 12H 改成 24H 比"后面递增"更重要**；本轮 `[720, 1440]` 的读数因此基本等于平面 12H
（2.30568 vs 2.30016，最差回撤逐位相同），而平面 24H 的读数（2.76619）正是上一轮 J3 表里的 2.7662×。

### 4.3 `pre` 腿（真正的样本外窗）：第一档的取舍反转

| 读数 | `l0_off` | `l1_ladder` | `l2_budget` | `l3_fixed24` |
|---|---|---|---|---|
| `gain_strategy_eq` | **0.610443** | **0.607839** | **0.339865** | **0.588841** |
| `drawdown_worst_strategy_eq` | 0.765622 | 0.765622 | 0.678543 | 0.768474 |
| `hard_stop_triggers` / `restarts` | 3 / 3 | 3 / 3 | **1 / 0** | 3 / 3 |
| `hard_stop_duration_minutes_mean` | 720 | **1200** | 1,216,030 | 1440 |
| `hard_stop_ladder_strikes_max` | 3 | 3 | 1 | 3 |
| `hard_stop_realized_loss_halt_pct_max` | 0 | 0 | **0.673817** | 0 |
| 成交笔数 | 17,155 | 17,131 | 453 | 17,016 |

- 阶梯在样本外**略差于平面 12H**（0.6078 vs 0.6104），平均冷却 1200 = (720+1440+1440)/3 说明档位
  按契约生效；最差回撤三者逐位相同。
- 平面 24H 在样本外**明显更差**（0.5888 vs 0.6104）——与上一轮 J3 的 `pre` 腿读数（0.6104 → 0.5888）
  完全一致。也就是说："第一档取 24H 更好"这个结论只在 `ext` 腿上成立，在真正的样本外窗上是反的。
- `l2_budget` 的 0.339865× 在两条腿上相同：永久停机发生在 2021 年，因此 2021-04→2023-09 的样本外窗
  整段都是停机后的死区。

### 4.4 判据裁决

| 判据 | 结果 | 依据 |
|---|---|---|
| K0 默认关闭逐位一致 | **通过** | `artifacts/default_off_identity.json`：两条腿 31 项指标 + 成交哈希全等（`ext` 45,644 笔、4 次停机） |
| K1 阶梯按档位生效 | **机制通过；"不低于 `l0_off`" 的门槛在 `pre` 上未达** | 档位可分辨：`ext` 均值 1260 = (720+1440×3)/4、`pre` 均值 1200 = (720+1440+1440)/3，最差回撤两条腿都逐位相同；终值 `ext` +0.24%（达标）、`pre` −0.43%（**未达标**） |
| K2 累计口径按契约锁存 | **机制通过、标定被证伪** | `realized_loss_halt_pct_max = 0.6738` 且归因明确；但 0.30 的预算导致第一次停机即永久退出（0.3399×，两条腿相同） |
| K3 阶梯 ≠ 单纯加长冷却 | **通过（不等价，且结论随腿反转）** | `ext`：3 次触发 / 2.76619×；`pre`：0.5888× vs `l1_ladder` 0.6078×；差异全部来自第一档 |

### 4.5 本轮对配置模板的结论

1. `halt_ladder_minutes` 可以发布（默认关闭、机制与重建契约都成立），但**没有**证据支持把它当作
   改进手段或风险塑形手段：`ext` 上 +0.24%、`pre` 上 −0.43%（K1 预注册的"不低于 `l0_off`"门槛在
   样本外未达），最差回撤两条腿都逐位相同。按预注册口径，K1 的**效果**一侧在本轮是**未通过**的，
   只有**机制**一侧通过。
2. 第一档的取值**在两条腿上给出相反答案**（`ext` 24H 更好、`pre` 12H 更好），因此本轮的证据
   **不支持**把推荐值从 12H 改成 24H；设计件 §2 里"最高档取 24H"的说法应读作"上限"，不是"更优"。
3. `realized_loss_budget_pct` 可以发布（默认关闭、口径正确），但**没有**经过标定的推荐值：按本轮
   实测，≤0.30 的预算在这条历史上会在第一次停机就永久关停账户。文档与 `CHANGELOG` 按"口径修正、
   预算需自行标定"的措辞写，不给默认推荐值。
4. **样本量警告**：`ext` 腿 4 次守护触发、`pre` 腿 3 次、`3y` 腿 0 次。上面所有"更好/更差"的读数
   都建立在这几次之上（2.3002 vs 2.3057 这种差异远小于参数不确定性），按本轮过拟合审计
   （`g4_overfitting_audit_2026-09-18`）的结论，两个键都应标注 **out-of-sample 未确认**。

## 5. 证据布局

- 每个臂：`artifacts/<arm>/backtest_results/binance_<arm>/binance/<UTC 时间戳>/`
  - tracked：`annual_analysis.md`、`analysis.json`、`run_record.json`、`global_metrics.json`、
    `tail_risk_events.json`、`tail_risk_wipeout.json`、`guard_readiness.json`、
    `risk_geometry.json`、三份 metric CSV、`dataset.json`
  - local-only：`fills.csv`、`balance_and_equity.csv.gz`、PNG 图、日志、执行审计
- 跨臂：`artifacts/variant_input.json`（预注册的臂表、父配置派生、判据、边界）+ 本 README 的结果一节
- 契约：[`halt_ladder_design.md`](halt_ladder_design.md)（配置键、公式、周期、重建契约、测试矩阵、§7 判据）

## 6. 边界（必须一起读）

- **本轮没有任何参数搜索**：四个臂全部预注册，看到任何一条腿的结果之后不得新增臂、改档位或调阈值。
- **阶梯是风险塑形，不是收益改进**：上一轮已实测加长冷却在两条腿上都不更优
  （ext 腿 12H/24H/48H/72H 终值 2.3002× / 2.7662× / 2.0828× / 1.8666×；pre 腿单调变差
  0.6104× → 0.4832×），所以最高档取证据支持的 24H，且默认关闭。
- **触发次数很少**：5.4 年腿一共只有 4–5 次守护触发，`pre` 腿只有 2.4 年；涉及“第 N 次触发”的
  结论都建立在这几次之上，必须标注样本量。
- **周期峰值受滚动窗口约束**：`live.pnls_max_lookback_days = 7` 天；窗口外的旧高点不参与周期判定
  ——与既有 no-restart 峰值一致，不是本轮新引入的近似。
- **复牌时点（`resume_condition`）明确排除**，留待单独实验；`orange_tier_mode` 与
  `reentry_scale_after_halt` 本轮同样不碰。
- 币池是活到 2026 年的当前 top40（幸存者偏差）；长腿 2021-04-20 起交易时仅 22 个币有数据。
- 未建模：真实资金费率、滑点枯竭、API 断连、交易所/稳定币对手方风险；引擎不模拟保证金占用。
