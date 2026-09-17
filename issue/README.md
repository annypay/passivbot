# 实盘运行记录（issue/）

这里保存需要跨越实盘启动这段时间的施工、测试与排障记录。它存在的目的：后来的读者（人或 agent）能直接看出"启动时哪些行为是已知且已验证的"，而不必从日志尾部反推。

## 什么时候看它

- **上实盘之前**：先读最新记录里的 *实盘排障手册* 和下面的 *稳定前观察看板*。手册里写的每个字段，代码都真的会输出。
- **实盘运行期间**：发现异常时，到对应记录的「症状 → 处置」表里查，按表执行；如果表里没写，就在该记录里补一条带日期的说明。
- **改动之后**：新增一份编号记录，不要改写旧记录，并更新索引与观察看板。

## 规则

- 记录是**只追加的历史**。写错的地方用同文件内带日期的更正说明修，不要静默改写。
- 不写宿主路径、凭据、账户标识或私有主机。仓库相对路径、commit SHA、PR 号、日志片段可以写。
- 每条关于行为的断言都要指明它实现在哪、或在哪里被观察到。

## 记录索引

| 编号 | 主题 | 状态 | 相关 PR | 最后更新 |
| --- | --- | --- | --- | --- |
| `0000-session-log-2026-09-16.md` | 会话日志：研究证据、两个已发布 profile、以及改变门控实盘说法的审计 | 历史存档 | #1、#2、#3 | 2026-09-17 |
| `0002-binance-live-debug-2026-09-17.md` | 币安实盘调试与上线：离线预检、公开数据门控探测、认证只读账户探测、实盘启动（进行中） | 进行中 | 本轮 PR | 2026-09-17 |
| `0001-entry-regime-gate-live-wiring.md` | 入场择时门控接入实盘：单点求值、缺证据 fail-closed、verdict 事件、离线 dry run、预热 60 天 | 进行中 | #3、#5（stack 在 #2 之上） | 2026-09-17 |

## 稳定前观察看板

| 项 | 看什么 | 何时可关闭 |
| --- | --- | --- |
| W1 预热与判决 | `[regime_gate] warmup` 行：`lookback_days=60`、`required_days=50`、`history_days_min ≥ required_days`、`missing_days_max=0`；首个 `entry_regime.gate.verdict` 出现在 `bot.ready` 之前 | 首轮即得到 risk-on 判决，且四项深度字段符合预期 |
| W2 日线证据可用性 | `unavailable=` 保持 0；非 0 表示这些币的新仓被阻断，直到重试成功 | 连续一周没有 unavailable 的币 |
| W3 verdict 事件 | 每个 UTC 日出现一次 `entry_regime.gate.verdict`，计数合理 | 一周的事件与行情对得上 |
| W4 阻断是否合理 | 风险关闭日不进场、风险开启日恢复进场，且平仓完全不受影响 | 头两次完整的 regime 切换表现符合文档 |
| W5 反事实核对 | 门控版与无门控版的成交差异，只应来自被拦掉的那些进场 | 对一段 risk-off 区间与无门控证据做一次复核 |

## 快速命令

```bash
# 通过真实规划路径做门控的离线 dry run（无网络、无凭据）
venv/bin/python -m pytest tests/test_fake_live_entry_regime_gate_dry_run.py -q

# 在每个 regime 边界上做引擎/表逐点 parity，并跑隔离门控的反事实
venv/bin/python -m pytest tests/test_entry_regime_gate_engine_parity.py -q

# 门控单元与契约测试（两种声明写法、实盘 payload、事件）
venv/bin/python -m pytest tests/test_entry_regime_gate.py \
  tests/test_entry_regime_gate_config_survival.py \
  tests/test_live_entry_regime_gate.py -q

# 更长 tape 上的人工 smoke（只验证接线；能证明与不能证明什么见 0001）
venv/bin/python src/tools/run_fake_live.py \
  configs/fake_live_entry_regime_gate.hjson \
  scenarios/fake_live/entry_regime_gate_wiring_smoke.hjson \
  --max-steps 250 --output-dir artifacts/fake_live --snapshot-each-step
```

不改代码就停用门控：把配置所用声明（`live.entry_regime_gate`，或实盘回退读取的 `backtest.entry_regime_gate` 块）里的 `enabled` 置为 `false`。
