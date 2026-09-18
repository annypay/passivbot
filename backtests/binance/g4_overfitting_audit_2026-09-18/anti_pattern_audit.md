# 反模式与泄露审查（本审计的 arm 口径，R1–R22）

本文件把 [`backtests/binance/2026-09-14T03_40_41/lookahead_risk_matrix.csv`](../2026-09-14T03_40_41/lookahead_risk_matrix.csv) 里的 R1–R18 逐条搬到**本轮的 arm 口径**重新取证，并新增 R19–R22。状态只有三种：**pass / fail / unknown**。`unknown` 是本审计在证据不存在时的正式答案——不是「大概没问题」。

取证口径：R 行只引用两类证据，(1) 本轮已落盘的 run 文件（`analysis.json` / `fills.csv` / `balance_and_equity.csv.gz` / `config.json`），(2) 本审计自己产出的 tracked 工件（`artifacts/*.json`）。凡是两者都没有的，写 `unknown` 并写明缺什么。

**证据的 tracked / run-local 属性**：`.gitignore` 的 `/backtests/**` 规则保留 `*.md`/`*.py`/`*.sh`/`*.json`，排除 `*.csv`、`*.csv.gz`、`config.json`、`dataset.json`。所以 run 目录里的 `fills.csv`、`balance_and_equity.csv.gz`、`monthly_metrics.csv`、`config.json`、`dataset.json` 都是 **run-local**。本文件按仓库相对路径引用它们，并把每个输入的 sha256 记在 tracked 的 `artifacts/panels/*.json`（`sources[]`）与 `artifacts/stress_arms.json`（`provenance`）里——数字追溯到文件内容，而不是某台机器的路径。

**本轮未复验引擎代码路径**：本 worktree 的 `src/**` 与 `passivbot-rust/**` 正在被其他 agent 修改，且本审计明确不做引擎改动。凡是只能靠「读引擎源码」判定的条目（R1/R3/R4/R9/R10），本轮一律给 `unknown` 或标注证据来自上一份审计，而不是当成已复验。

## 1. 逐条状态

| 编号 | 类别 | 状态 | 本轮证据（文件 + 数字） |
| --- | --- | --- | --- |
| R1 | 直接读取未来 candle（`next_candle`） | **fail** | 代码路径未变：回测仍把 `k+1` 的 low/high 作为生成阶梯的提示（`passivbot-rust/src/backtest.rs`、`trailing_martingale::generate_orders`）。本审计**没有**做 no-peek 反事实（明确不在范围内），所以「无前视认证」依然失败。 |
| R2 | entry 侧的 peek 范围 | **unknown** | 配置侧可观察：entry `retracement_base_pct > 0` 且 cooldown 为正（run-local `config.json`），实测 `a_allow000__ext` 成交 45644 笔。但「gate 是否真的把 entry 阶梯单笔化」需要 output-equivalence replay——上一份审计就把它列为 still required，本审计**没有做**，因此不给 pass。 |
| R3 | close 侧的 peek 范围 | **fail** | close 仍可展开完整递归梯子（`close.retracement_base_pct > 0`，代码路径来自上一份审计，本轮未复验）。这是本轮 maker 成交假设之外最需要打折的一项；本审计无法量化它对月度收益的影响。 |
| R4 | 普通信号时序 | **unknown** | 上一份审计判定 `check_for_fills(k)` 先于 `update_emas(k)`/`update_trailing_prices(k)`。本审计**没有复验引擎代码路径**（本 worktree 的 `src/**`、`passivbot-rust/**` 正在被其他 agent 修改），因此按本轮口径只能给 unknown。 |
| R5 | 零延迟激活 | **fail** | 执行口径 `execution_delay_bars = 0`、`intrabar_fill_order = close_first`（各轮 `variant_input.json` 的 `execution` 块），即 T+1 零确认延迟下界。本轮**没有**跑 T+2 敏感性。 |
| R6 | 同 bar 路径 | **unknown** | 每币固定 close-before-entry，OHLC 无法恢复真实先后。本审计没有跑 O-H-L-C / O-L-H-C 边界，因此「同 bar 顺序对结论的影响」在本轮**未测量**。 |
| R7 | 时间戳语义 | **pass** | `fills.csv` 的 `timestamp` 是 candle 开盘标签；本审计的月度归属按该标签 + 逐小时权益采样，口径写在 `artifacts/panels/index.json` 的 `cadence_note`。 |
| R8 | 月末归属 | **pass** | 月度收益直接从 `balance_and_equity.csv.gz` 的月末小时样本重算，并与各 run 自带 tracked `monthly_metrics.csv` 交叉核对：水平最大相对偏差 0.000e+00。成交级的 23:59 标签不影响本审计的口径（本审计不按成交分月）。 |
| R9 | 完成 candle 特征 | **unknown** | 上一份审计判定 1m EMA / quote-volume / log-range 均在 candle k 完成后更新、小时 bucket 止于 k-1。本审计未复验引擎代码路径，故按本轮口径记 unknown。 |
| R10 | warmup | **unknown** | 可观察的部分：所有 arm 用同一份 dataset（`global_metrics.json` 的 cache label 与 manifest sha256 一致，登记在 `artifacts/panels/*.json` 的 `sources[]`），所以**跨 arm 可比**这一点成立。但「warmup 是否只用了交易前历史」属于引擎代码路径，本轮未复验，故记 unknown。 |
| R11 | leading gap 回填 | **unknown** | 本审计**在 run 工件里找不到 `fill_leading_gaps` 这个键**（`grep` 过 `config.json` / `config.original.json` / `dataset.json` / `global_metrics.json`），因此无法从落盘证据确认它是否为 False。上一份审计的「默认 False」结论本轮无法复验。 |
| R12 | 内部 gap 零成交量补点 | **unknown** | `gap_tolerance_ohlcvs_minutes=120` 会用前收 + 零成交量补内部缺口。这是过去值补点、不是未来泄露，但会压低波动率/成交量估计。本审计**没有**逐行标注 synthetic-row provenance，因此影响量级 unknown。 |
| R13 | BTC 基准 bfill | **pass** | 本轮所有 arm 的 `btc_collateral_cap=0`，BTC 计价指标对策略余额无影响；本审计全部结论用 `strategy_equity`（USD），不经过 BTC 路径。 |
| R14 | 高阶合成 1m | **pass** | 输入已是 1m（`dataset.json` 的 `candle_interval_minutes=1`），该分支未激活。 |
| R15 | 下架/有效区间元数据 | **fail（本轮首次有对应成交）** | **与上一份审计不同**：本轮账本**有** panic 成交——`a_allow000__ext` 23 笔、`g_user12h__ext` 29 笔（类型含 `close_panic_long`）。守护触发的强平式清仓因此不再是空分支，必须逐臂按 `hard_stop_*` 遥测打折；下架（delist）成交仍未观察到。 |
| R16 | 参数时间旅行 | **fail** | **本轮最严重的一项，且 PBO/DSR/SPA 全都测不到它。** 8 个轮次目录全部形成于 2026-09，回放窗口从 2021-04-20 开始（`artifacts/panels/*.json` 的 `first_sample`）。「三年 7.26 倍」不是「该参数在 2023 年已可实盘取得的表现」。 |
| R17 | 选择 provenance / 幸存者偏差 | **fail** | 台账下界 N ≥ 1807，但**清单本身不完整**：优化器非 Pareto 候选未落盘、人工试错无记录、40 币池是活到 2026 年的当前 top40。本审计能把 N 的下界算出来，**不能**把缺失的候选补回来。 |
| R18 | 跨所成交量归一化 | **pass** | 单一 Binance 来源（各 run `dataset.json` 的 exchange=binance），该分支未激活。 |
| R19 | 单一历史段被四轮复用（本轮新增） | **fail** | A/B/C/D 四轮全部在 2021–2026 同一段 Binance 历史上选点与判定。本审计的 `pre` 腿（2021-04-20 → 2023-09-11）只对**风险几何轮**是样本外；对更早的轮次它已被看过。因此「样本外」是**相对**的，不是绝对的。 |
| R20 | 优化器中间候选不可恢复（本轮新增） | **fail** | 风险几何轮的搜索只冻结了 4 个选定候选 + 8 个 Pareto 点（`artifacts/search_selection.json`）；`optimize_results/**` 是 local-only 且 `all_results.bin` 是不可读的 pymoo checkpoint。即 **non-Pareto 的评估次数无法从 tracked 工件重建**，只能引用报告里声明的数字。 |
| R21 | 跨轮成本/延迟口径漂移（本轮新增） | **unknown** | 早期研究存在 T+2 / 3× maker 费的保守口径，本轮四轮统一为 T+1 + Binance 实际费率。本审计只读本轮口径的 arm，**没有**把两套口径混编，但也没有重算早期 arm 在本口径下的结果，因此跨轮可比性 unknown。 |
| R22 | 分块/退化规则本身是自由度（本轮新增） | **pass（已显式暴露）** | PBO 对 S 与退化规则敏感，本审计把 S ∈ (8, 10, 12, 16) 全列出：池 B `ext` PBO 0.297–0.345，池 A `ext` 0.478–0.492。退化规则在选择前生效，4 个面板合计移除 14/72 = 19.4%。规则写死在 `report_tools/panel.py`，verifier 独立重算同一规则。 |

## 2. 统计

- 总条目：22（R1–R22）
- pass：6
- fail：8
- unknown：8

## 3. unknown 缺的是什么证据

| 编号 | 缺的证据 | 要补它需要做什么 |
| --- | --- | --- |
| R2 | entry 阶梯是否真的被 gate 单笔化 | 对同一 arm 做 output-equivalence replay（关掉 peek vs 保留 peek），比对成交账本签名；上一份审计已把它列为 still required |
| R4 / R9 / R10 | 引擎代码路径的复验 | 在一份**冻结**的引擎提交上重读 `backtest.rs` / `update_emas` / `update_trailing_prices` 与 warmup 播种逻辑；本 worktree 的 `src/**`、`passivbot-rust/**` 正在被其他 agent 修改，当前状态不可作为证据 |
| R6 | 同 bar 顺序的上下界 | 跑 O-H-L-C 与 O-L-H-C 两套 `intrabar_fill_order` 并对比月度收益分布（需要新回测） |
| R11 | `fill_leading_gaps` 的实际取值 | 让回放把该开关写进 `global_metrics.json` 或 `run_record.json`；当前工件里没有这个键 |
| R12 | 合成行 provenance | 让数据准备阶段逐行导出 synthetic 标记，并把波动率/成交量估计在剔除合成行后重算 |
| R21 | 跨口径可比性 | 用当前口径重跑早期研究的 arm，或把早期 arm 的成本/延迟参数逐项映射后重算 |

## 4. 本审计自身不能证明的事项

1. **不能证明无前视**：R1/R3 仍在，no-peek 反事实未做。PBO/DSR/SPA 全部建立在同一条（可能带 peek 的）权益曲线上。
2. **不能把 PBO 读成亏钱概率**：池 B `ext` 的 PBO 0.297–0.345 只说「从这批 arm 里挑最好」的脆弱程度，与未来盈亏无关。
3. **不能消除选择偏差**：R16/R17 仍在；台账只把 N 的下界算出来，把惩罚加上去，**不能**把没记录的试验变回来。
4. **不能证明可实盘部署**：无盘口队列、部分成交、mark-price 强平、资金费率；压力臂只是一阶成本界（`artifacts/stress_arms.json`）。
5. **不能保证 DSR 的口径**：`Var(SR_trials)` 用面板横截面代理，真实试验集离散度不可知（`artifacts/selection_bias.json` 的 `formulas.sr_std_proxy`）。

