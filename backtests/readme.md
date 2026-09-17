# 回测研究证据（backtests/）

这棵树纳入版本控制，是为了让一份全新的 checkout 不必重跑数小时的研究，就能追溯**某个策略
profile 是怎么被选出来的**。它是研究证据，不是发布产物：这里没有任何东西会被安装、打包，或
在运行时被依赖。

## 跟踪什么，不跟踪什么

跟踪（体积小、难以再生、人类可读）：

- `*.md` —— study 报告、审计与深度分析。
- `*.py` 与 `*.sh` —— study 的原始脚本，以及复现该 study 的运行入口。
- `*.json` —— 研究契约、候选锁定、manifest、逐配置的结果与指标摘要，以及每个 artifact
  bundle 的 run record 与 `analysis.json`。
- `annual_analysis.md` —— 每个被跟踪 artifact bundle 渲染出的深度分析报告。所有这类报告都
  遵循 `docs/ai/runbooks/strategy_report.md`：一套固定章节骨架、一套表格 schema、一套数据
  口径，因此不同 study 的报告可以直接并排比较。

不跟踪（体积大，可由被跟踪输入加本地 HLCV 缓存再生）：

- `fills.csv`、`execution_audit.csv`、`balance_and_equity.csv.gz`
- `*.npy`、`*.npz`、`*.png`、`*.pyc`、`*.log`
- 每个逐次运行的记账目录 `runs/`
- 编译后的运行时配置（`config.json`、`config.original.json`、`dataset.json`、
  `study_input_config.json`、`candidate.config.json`）：它们体积大、携带生成主机的路径，
  且可由 study 输入再生。
- study 的 `report_tools/` 里一次性写作的草稿（`_patch_*.py`、`_probe_*.py`、`_diag_*.py`
  等）：它们针对生成主机而写，无法由 study 输入再生；study 工具所 import 的可复用 helper
  （`_study_module.py`）保留。

精确的 allowlist 位于仓库 `.gitignore` 的 `/backtests` 段。提交前检查某个路径：

```bash
git check-ignore -v backtests/binance/<study>/<path>
```

被跟踪的文件里仍可能含有生成时记录的绝对路径（例如锁定契约里由 `dataset.json` 派生出的
条目）。它们是生成主机的历史记录，不影响复现。study 自己的冻结运行配置不依赖这项豁免：它把
自己写 run 目录与 execution audit 的位置写成仓库相对路径，因此一份全新的 checkout 无需改动
任何主机路径就能复现该次运行。

## 现有 study

| Study | 它回答的问题 |
| --- | --- |
| `binance/2026-09-14T03_*` | 默认多头 trailing martingale profile 的三年基线回测，外加约束/成交审计与 look-ahead 审计。 |
| `binance/causal_comparison_2026-09-14` | 余额/权益导出与回撤形态能否经受因果（T+1）重放，以及 2025 年那次回撤逐分钟来自哪里。 |
| `binance/low_drawdown_strategy_study_2026-09-14` | 是否存在回撤更低的局部策略候选，并带锁定的 holdout。 |
| `binance/maxdd_strategy_research_2026-09-14` | 回撤上限约束下的策略路径与参数搜索，外加 walk-forward 验证。 |
| `binance/deployability_research_2026-09-15` | 小币池上的多币经济性、执行压力测试与组合装配。 |
| `binance/dd_tail_research_2026-09-15` | 默认 profile 为何回撤 73.69%，哪些配置杠杆能削掉尾部；产出了已发布的低尾部 profile。 |
| `binance/hsl_npos1_analysis_2026-09-16` | 已发布的 HSL 版 trailing martingale 示例在其自身声明的窗口（2021-04-20 .. 2026-09-12）里究竟做了什么：该规则集交易了 30 天，在 2021-05-19 触发硬止损，其后 1,942 天再无成交。它的深度分析是该 profile 的参照。 |
| `binance/returns_guarded_dd_research_2026-09-16` | 三年收益倍数能否在约 30% 的回撤上限下存活：覆盖阶梯几何、路径依赖止损、减仓形态，以及新增的严格因果日线 SMA 入场择时门控的 48 格筛选，外加实测的回撤/收益边界。 |
| `binance/g4_sma20_50_replay_2026-09-16` | 对已发布门控 profile 的独立深度分析：离线重放 `trailing_martingale_twel100_ddf060_sma20_50.json` 于冻结 bundle 之上，并与无门控 profile 及 study cell 并排报告。 |

## 复现已发布的 profile

已发布的 profile 是：

- `configs/examples/trailing_martingale_twel100_ddf060.json`：与
  `configs/examples/default_trailing_martingale_long.json` 相差三个行为参数、一个优化器
  边界，以及三个陈述所报告执行/成本契约的 `backtest` 键。
- `configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json`：在上面这份之上只增加
  一条日线 20/50 入场择时门控，别无其它。它的独立 artifact 与深度分析在
  `binance/g4_sma20_50_replay_2026-09-16/`。

参数表、证据摘要与可复现边界见 `docs/strategy_profiles.md`。两份 profile 都可从下面的 study
复现：低尾部 profile 来自 `dd_tail_research_2026-09-15`，门控 profile 来自
`returns_guarded_dd_research_2026-09-16` 的 `best_dd_reducer` bundle（study cell
`g4_sma20_50`），或以独立形式来自 `g4_sma20_50_replay_2026-09-16`。

```bash
# 保收益的回撤筛选（48 格，离线本地 HLCV）。
cd "$(git rev-parse --show-toplevel)"
venv/bin/python backtests/binance/returns_guarded_dd_research_2026-09-16/report_tools/run_study.py \
  run --cells all --windows full --scenarios C1_binance_actual
venv/bin/python backtests/binance/returns_guarded_dd_research_2026-09-16/report_tools/render_tables.py

# artifact bundle 及其深度分析报告，然后校验两者。
bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh
bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --cells seed --force
bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --verify-only

# 重建锁定候选的 artifact bundle、渲染其报告，并校验数字。
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --label candidate

# 候选所对比的基线同理，其它任何 profile 也一样。
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --baseline --label baseline
bash backtests/binance/dd_tail_research_2026-09-15/run.sh --profile configs/examples/<name>.json

# 或者只跑某个 profile 自身的回测，用它自己的窗口与手续费。
passivbot backtest configs/examples/trailing_martingale_twel100_ddf060.json
passivbot backtest configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json

# 在 study 内重建并校验门控 profile 的 bundle（study cell `g4_sma20_50`）。
bash backtests/binance/returns_guarded_dd_research_2026-09-16/run.sh --cells best_dd_reducer --force

# 门控 profile 的独立深度分析：它有自己的 bundle 与 verifier。
bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh
bash backtests/binance/g4_sma20_50_replay_2026-09-16/run.sh --verify-only
```

每种 `run.sh` 模式都跑冻结的 study 窗口与所报告的成本契约，因此这些 bundle 与该 study 自己的
cell 可以直接比较：

| Bundle | Bundle 目录 | 它必须复现的 study cell |
| --- | --- | --- |
| 锁定候选 | `artifacts/binance_actual_candidate/` | `cells/full/C1_binance_actual/combo_twel100_ddf060_ddthr0030` |
| 基线 profile | `artifacts/binance_actual_baseline/` | `cells/full/C1_binance_actual/baseline` |

## run 目录的布局与日期

回测用 **UTC 完成时间戳**给 run 目录命名，因此每次运行都产生一个新的带日期目录，不会被覆盖。
`--label NAME` 把该次运行归入一个带标签的交易所目录，这正是并行 bundle 互不干扰的原因：

```
<study>/artifacts/binance_actual_candidate/backtest_results/binance_candidate/binance/2026-09-16T01_22_18/
<study>/artifacts/binance_actual_baseline/backtest_results/binance_baseline/binance/2026-09-16T01_24_14/
```

该次运行被跟踪的存档是它的 `analysis.json` 与 `annual_analysis.md`；成交台账、权益序列、审计与
图表留在本地。报告总是渲染进它所描述的那个 run 目录，所以目录上的日期是这次运行产出的日期，
不是它最后一次被编辑的日期。

因为一个 bundle 只装一次运行，报告工具会挑出它下面唯一的 run 目录，并且在多于一个时拒绝猜测。
因此重跑进同一个 bundle 是替换那个 run 目录，而不是在旁边堆积；若想保留多次，用 `--result-dir`
把工具指向某个具体的 run。

`returns_guarded_dd_research_2026-09-16/run.sh` 强制这条「一个 bundle 一次运行」的规则：
`--force` 重跑会先清掉该 bundle 上一个 run 目录，因为 run 目录由完成时间戳命名，多出来的第二个
会让报告工具拒绝选择。

该 study 的 bundle 跑完整套图表。分析类 artifact（analysis、config、fills、balance/equity、
dataset）在图表尾部之前写出，所以即使图表阶段被杀掉，bundle 仍能渲染与校验；
`run_record.json` 同时记录进程退出码与 `artifact_status`，每个 run 目录还带一份流式写出的
`execution_audit.csv`，verifier 会把它与 `fills.csv` 交叉核对。

verifier 在声称一致之前，会先比较 artifact 的窗口与 cell 的窗口，因此窗口不同的 bundle 会报告
这一差异，而不是在三个指标检查上失败。对同一 profile 跑 `passivbot backtest` 是另一件事：它用
profile 自己的窗口（`end_date: now`）与自己的手续费覆盖，所以它的数字不会等于这里的数字。

## 执行与成本契约

这棵树里的数字只在同一份契约内可比。已发布证据所生效的契约记录在 study 的
`research_contract_v4.json` 里：

| 契约 | Regime | 延迟 | 每方向 maker / taker | 状态 |
| --- | --- | --- | --- | --- |
| v4 | `v4_binance_actual` | T+1（`execution_delay_bars = 0`） | `0.0002` / `0.0005` | **当前。** Binance USDT-M VIP0。 |
| v3 | `v3_conservative` | T+2（`execution_delay_bars = 1`） | `0.0006` / `0.0008` | 冻结的压力参照。保留，但绝不混入 v4 的表格。 |

两份 profile 都是 maker-only（`live.market_orders_allowed = false`，HSL panic closing 关闭），
所以在两份契约里 taker 手续费与 `market_order_slippage_pct` 都不会生效。一笔市价单成交既需要有
代码路径去发出它，也需要同时给出滑点假设；默认 profile 与低尾部 profile 都没有这条路径。

## 报告格式

深度分析使用 `docs/ai/runbooks/strategy_report.md` 定义的固定章节顺序、表格列、数据口径与
**磁盘上的 artifact 布局**，由 `backtests/report_spec/annual_analysis.py` 渲染。
`verify_annual_report.py` 从成交台账与权益序列重算报告里的每个数字，并检查该骨架，因此偏离
约定的报告无法通过校验。

runbook 的「Artifact Persistence And On-Disk Format」一节是「报告、图表、成交面板与数据集
身份各自放在哪里」的规范性答案。它的可执行部分就是 `backtests/report_spec/annual_analysis.py`
里的 `BUNDLE_REPORT_FILES`、`BUNDLE_LOCAL_FILES`、`BUNDLE_FIGURE_FILES`、`BUNDLE_PLOT_DIRS`
与 `assert_bundle_layout`，由 `tests/test_annual_analysis_report_spec.py` 与
`tests/test_ai_docs.py` 检查。HLCV 数组留在 `caches/hlcvs_data/`；一次运行把它们的身份记进自己
的 `dataset.json`，而不是复制它们。冻结的参照样本是
`binance/2026-09-14T03_25_14/annual_analysis.md`。

## 证据边界

这棵树里的结果是带文档化执行假设的 1 分钟 OHLC 模拟。它们不是实盘业绩，也不是收益预测。引用
任何数字之前，先读该 study 自己的口径章节；`binance/2026-09-14T03_40_41/` 下的审计陈述了成交
模型、K 线边界契约，以及适用于本树每一次历史重放的参数时间旅行警告。

`binance/dd_tail_research_2026-09-15/analysis/` 另带过拟合复核：`anti_pattern_audit.md` 把已知
的回测反模式（look-ahead、幸存者偏差、成本与延迟乐观、选择偏差）映射到这份证据上；
`overfitting_audit.json` 存放组合对称交叉验证，用于估计已发布 profile 背后杠杆筛选的回测过拟合
概率（PBO）。