# g4 @ TWE 3.0 单币止损研究：工具与复现

本目录的四个文件就是这一轮的全部工具。**这一轮没有参数搜索**：六个臂全部在
`variant_spec.py` 里预注册，`pct_from_avg_entry = 0.15` 与 `cooldown_minutes = 1440` 是操作者
给定的机制参数，不是被调优出来的最优点。

| 文件 | 作用 |
| --- | --- |
| `variant_spec.py` | 唯一事实来源：冻结父配置、派生路径、五条腿的数据集 pin、三个对照 run 的 pin、六个臂的声明 |
| `sl_replay.py` | 驱动器：`build` / `run` / `identity` / `analyse` 四个子命令 |
| `candle_source.py` `episode_ledger.py` `candle_excursion.py` `sl_counterfactual.py` | Phase A 的证据工具（见 `../optimism_audit.md`），不参与回放 |
| `README.md` | 本文件 |

## 与上一轮（HSL 冷却阶梯）工具布局的偏离

上一轮把工具拆成 `variant_spec.py` / `build_variant_config.py` / `run_variant.py` /
`default_off_identity.py` / `verify_variant_report.py` / `run.sh` 六个文件。本轮只有一个机制、一个
作者、没有搜索阶段，因此把四道闸门收进一个 `sl_replay.py` 的四个子命令：闸门与被闸的对象放在一起，
比分散在四个文件里更难被绕过。功能上没有减少——`build` 做上一轮 `build_variant_config.py` 的全部
门禁，`identity` 等价于 `default_off_identity.py`，`analyse` 等价于 `verify_variant_report.py` 的
独立复算（它只读 run 目录，不读 `build` 阶段的中间产物）。**未交付**：上一轮的
`generate_annual_report.py`（年报渲染）与 `event_windows.py`（基准事件窗口表）；本轮的判定不依赖
它们，README 在此显式记为 PENDING，而不是静默通过。

## 复现

```bash
# 0. 环境：先确认引擎侧已编译（Phase B），且可用内存足够
bash backtests/binance/g4_twe300_sl_cooldown_2026-09-18/run.sh --verify-only   # 只跑闸门与判定

# 1. 派生父配置并写出 30 个臂配置（不改任何文件时用 --check）
venv/bin/python backtests/binance/g4_twe300_sl_cooldown_2026-09-18/report_tools/sl_replay.py build --check

# 2. 回放 30 个 run（逐臂内存闸门：pre/ext 需 ≥5200 MB，3y/synth_* 需 ≥2600 MB）
bash backtests/binance/g4_twe300_sl_cooldown_2026-09-18/run.sh

# 3. 默认关闭逐位一致
venv/bin/python backtests/binance/g4_twe300_sl_cooldown_2026-09-18/report_tools/sl_replay.py identity --legs 3y ext pre

# 4. S0–S7 判定
venv/bin/python backtests/binance/g4_twe300_sl_cooldown_2026-09-18/report_tools/sl_replay.py analyse
```

## 臂与腿

六个臂（`s0_off` / `s1_sl15_cd1440` / `s2_sl15_cd240` / `s3_sl25_cd1440` /
`s4_sl15_cd1440_limit` / `s5_sl15_cd0`）× 五条腿（`3y` / `ext` / `pre` / `synth_a` / `synth_b`）
= 30 个 run，键为 `<lever>__<leg>`。

| 腿 | 数据集 | 窗口 | override |
| --- | --- | --- | --- |
| `3y` | `binance__40_coins__2023-08-17_to_2026-09-12__8300950b42789a26` | 2023-09-12 → 2026-09-12 | – |
| `ext` | `binance__40_coins__2021-03-25_to_2026-09-13__a02b6ae1c140f2b7` | 2021-04-20 → 2026-09-13 | `dataset` |
| `pre` | 同上 | 2021-04-20 → 2023-09-11 | `intersection` |
| `synth_a` | `…__synthetic_collapse_a` | 2023-09-12 → 2026-09-12 | `dataset` |
| `synth_b` | `…__synthetic_collapse_b` | 2023-09-12 → 2026-09-12 | `dataset` |

合成腿的注入契约（由上一轮的 `make_synthetic_collapse_bundle.py` 执行）：目标币从自身峰值敞口时点
起、3 天内跌到原价的 0.5%，其后 14 天维持；`synth_a` 注入 1 个币，`synth_b` 注入 3 个币。它们是
"不反弹"路径的唯一来源，也是 S2/S5 的唯一证据。

## 判定（阈值在 `sl_replay.py` 的 `THRESHOLDS` 里声明，先于回放固定）

| 判据 | 内容 | 通过线 |
| --- | --- | --- |
| S0 | 默认关闭逐位一致 | `fills.csv` sha256 相同 + 指标零差异 + 零止损成交（`3y` 与 `ext`） |
| S1 | 机制：触发即整仓离场、冷却内不再加仓 | ≥1 次止损成交且冷却内 0 笔入场 |
| S2 | 合成单币崩塌腿的尾部改善（**只用市价档**） | 最差回撤改善 ≥ 5pp |
| S3 | 真实腿代价 | 终值退化 ≤ 25% 且最差回撤不恶化 |
| S4 | 限价档不得优于市价档（穿价不成交） | limit 档最差回撤 ≥ market 档 |
| S5 | 尾部收益必须在悲观（市价）档成立 | 同 S2 |
| S6 | 插针代价 | 触发里"卖在低点"占比 < 25%（取 Phase A 普查） |
| S7 | 2026-06 锚点 | 报数，不设通过线 |

缺少证据时一律输出 `pass: null`（例如启用臂一次止损都没触发时，不当作"0 次触发"），`--strict`
在任一行未完成时返回 1。

## 诚实边界

见 `variant_spec.py` 的 `HONESTY_BOUNDARIES`，其中两条最要紧：合成腿的注入目标币取自上一轮的
baseline run 而不是本研究的 control 臂；冻结配置声明 `market_orders_allowed = false`，而止损的
market 档刻意不受该开关约束（保护性平仓，不是策略单）。
