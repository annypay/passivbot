# 旧多币选参证据更正

新一轮逐分钟仓位/现金/权益交叉核对发现：
`maxdd_strategy_research_2026-09-14/report_tools/run_maxdd_parameter_study.py`
的 `prepare_window()` 将行情列重排为 `BTC, ETH, BNB, XRP, SOL`，
但 `src/backtest.py::prep_backtest_args()` 对币名排序，产生
`BNB, BTC, ETH, SOL, XRP` 的交易规则和币名列表。

这不是收益回撤口径的差别，而是行情列与币种/交易规则错配。首个新试跑出现
BNB 约 25,795、XRP 约 17.84 的模型成交价格；新审计拒绝了该试跑，
没有将其纳入结果排名。原始试跑保留在 `rejected_pilots/`。

影响范围是经过上述 `prepare_window()` 路径的多币基线、参数搜索、滚动验证和
MFE 对照。相关“3/6 合格”“TM 比 EMA 更值得部署”的选择推论均不能继续使用。
单币 ETH 没有多列排列问题，但它参与的跨轨道候选选择不能因此获得豁免。

旧的 `run_final_causal_replays.py` 直接使用 `prepare_hlcvs_mss()` 返回的规范顺序，
不经过该重排路径；其已对上真实币名的逐笔成交和分钟权益仍可作为特定参数的
回顾性描述。上游选参有效性、全局未见样本或部署资格不因此成立。

新研究在调用 Rust 前同步排序行情列与币名，并核对每笔成交的币名、方向、
真实 H/L 穿价、现金变化及整条分钟权益。冻结配置和判定门槛保持不变。
本次只是修复研究输入排列，不在看过结果后修改策略参数。
