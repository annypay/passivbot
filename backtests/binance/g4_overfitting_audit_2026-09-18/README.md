# 过拟合审计：四轮选点用的是同一段历史（2026-09-18）

**一句话结论：这段历史携带信号，但「从这 44 个风险几何 arm 里挑最好」几乎不携带样本外信息；把试验次数的下界计进去后，被推荐的守护配置在 95% 门下不显著。**

> 只读已有工件，**不新增回测、不做参数搜索、不改引擎、不联网、不启动实盘**。登记 1812 行试验、给出 N 下界 **1807**；月度面板由各 run 的 `balance_and_equity.csv.gz`（run-local，逐小时采样）重算，并与同一 run 的 `monthly_metrics.csv` 交叉核对（水平偏差 0）；CSCV/PBO 2 种选择约定 × S∈{8,10,12,16} × 4 个面板；DSR/MinBTL/SPA/StepM 在 N∈[92, 200, 500, 1807] 上做敏感性；折叠稳定性 K∈{3,6}；3 个声明式成本压力臂（**非选择输入**）。

## 1. 这轮在回答什么

| 问题 | 做法 | 结果 |
|---|---|---|
| 「挑最好」有多脆弱？ | CSCV/PBO，两种选臂约定，四种分块 | 池 A `ext` PBO 0.478–0.492（斜率 -2.16～-1.61）；池 B `ext` PBO 0.297–0.345（斜率 -0.93～+0.83） |
| 试验次数惩罚后还剩多少显著？ | DSR / MinBTL / SPA / StepM，N 敏感性 | `a_allow037__pre` 日频 DSR 0.2839／0.2419／0.2004／0.1541；MinBTL@1807 = 63.5 年 |
| 冠军能穿越折叠吗？ | 搜索窗切 K 折，看折叠冠军的折叠外分位 | `3y` K=6 最差分位 0.188、Spearman +0.667；`ext`/`pre`/池 B 全部不满足 |
| 成本变差时还剩多少？ | 3 个声明式压力臂（+4bp / +8bp 费、+5bp 滑点） | 头条 arm 终值保留率 81.0%～95.3% |

## 2. 三条最该被记住的读数

1. **样本内最优 = 样本外最差。** 搜索冠军 `c1__3y` 的日频 DSR 是 1.0000，但它在 `pre` 腿被强平（`D1_truncated`），在 `3y` 的折叠外排名是最后一名。同一个 arm 同时是「最显著」与「最不可用」。
2. **池子怎么划，答案就怎么变。** 池 B（28 个 `ext` 臂，含守护与 10k 对照组）的 PBO 约 0.30，而池 A（风险几何轮自己的 17 个 `ext` 臂）约 0.48。保护类 arm 与无保护 arm 之间的差异有**部分**可迁移性（斜率在 S=8/10 为正、S=12/16 转负，对分块方式敏感）；几何微调之间的差异则看不到可迁移性（四个分块斜率全为负）。
3. **退化不是小事。** 4 个面板合计剔除 14/72 = 19.4%；`ext` 腿上 7 个 arm 在 2021-05-19 直接强平、3 个终局停机后权益恒定。把它们留在池里会让 PBO 变成噪声。

## 3. 文件

| 路径 | 内容 |
|---|---|
| `overfitting_audit.md` | 主报告：口径、表格、边界、J1–J5（裁决已由父 agent 填入）|
| `anti_pattern_audit.md` | R1–R22 逐条 pass/fail/unknown + 证据 |
| `artifacts/trial_ledger.json` | 全部试验 + N 下界 + 计数方法 + 抽检 |
| `artifacts/panels/` | 月度收益面板（`index.json` + 每面板 `csv`/`json`） |
| `artifacts/cscv_pbo.json` | CSCV/PBO 2 约定 × 4 分块 × 4 面板 |
| `artifacts/selection_bias.json` | DSR / MinBTL / SPA / StepM / N 敏感性 |
| `artifacts/fold_stability.json` | 折叠稳定性 K∈{3,6} |
| `artifacts/stress_arms.json` | 声明式成本/执行压力臂 |
| `report_tools/` | `panel.py` `cscv_pbo.py` `selection_bias.py` `walkforward.py` `build_report.py` `verify_audit.py` |
| `run.sh` | 端到端（分阶段可单独跑） |
| `tests/test_g4_overfitting_audit.py` | 离线 pin + 数学单测 |

## 4. 复现

```bash
bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh
bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage panels
bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage cscv
bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage bias
bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage folds
bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage stress
bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --stage report
bash backtests/binance/g4_overfitting_audit_2026-09-18/run.sh --verify-only
venv/bin/python -m pytest tests/test_g4_overfitting_audit.py -q
```

独立复核 1549 项检查全部通过（失败 0 项；记录在 `artifacts/verification.json`）。`report_tools/verify_audit.py` 不 import 其余模块，用标准库重读权益账本并重算 面板 / 退化规则 / PBO / DSR / MinBTL / 折叠统计 / 压力臂。

`build_report.py` 默认**不会覆盖已裁决的 `overfitting_audit.md`**（检测到 §13 裁决列已填就跳过并打印提示；要重渲染需显式 `--force`），所以重跑 `run.sh` 不会抹掉裁决结论。

## 5. 边界

- **PBO 不是亏钱概率**，它衡量「在这条历史上挑最好」的脆弱性；它也**测不到参数时间旅行与选择 provenance**（R16/R17）。
- **N = 1807 是下界**：未落盘的优化器候选与无记录的人工试错都不在内；所有以 N 为输入的惩罚都是乐观端上界。
- 四轮共用同一段 2021–2026 Binance 历史；`pre` 腿只对风险几何轮是样本外。
- 池 A 的 `3y` 腿折叠稳定**只证明池内自洽**，不证明选择越过了搜索窗。
- 币池是活到 2026 年的当前 top40（幸存者偏差）。
- 压力臂是一阶成本界，不建模行为变化。

