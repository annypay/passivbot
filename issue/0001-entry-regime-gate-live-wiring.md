# 0001 — 入场择时门控接入实盘

状态：进行中（接线已落地，等待实盘头几周）
分支：`codex/entry-regime-gate-live-wiring`
Base：`codex/entry-regime-gate-and-gated-profile`（PR #2）
Pull request：[#3](https://github.com/annypay/passivbot/pull/3)
撰写者：2026-09-16/17 的 agent 会话

## 为什么有这份记录

日线入场择时门控当初是为回测加的，并随一个策略 profile 一起发布，但实盘交易从未真正咨询过它。把它接上会改变运行中 bot 的行为——在日线 regime 关闭期间，原本会发生的进场不再发生——所以在第一次实盘启动之前，把行为、证据与失败模式写在这里，并在实盘运行教会我们新东西之后持续更新。

## 接线之前错在哪

1. **引擎从不读实盘生成的表。** orchestrator 用来 AND 进 `wants_entries` 的是
   `SymbolSideInput.regime_allows_initial_entry` 与 `regime_allows_reentry`
   （`passivbot-rust/src/orchestrator.rs` 里 `should_generate_entries(...) && regime_allows_entries(...)` 的两处）。只有回测写它们（`passivbot-rust/src/backtest.rs` 的逐 bar 刷新循环）。实盘生成了表、也打了日志，但没有任何消费者。
2. **声明根本到不了 bot。** 实盘加载会移除整个 `backtest` 子树（`load_prepared_config(..., target="live")`），而门控声明在 `backtest.entry_regime_gate` 下——已发布 profile 与每个 tracked 证据包都是写在那里。于是即使配置文件声明了门控，实盘 bot 上的 `_entry_regime_gate_config()` 仍返回 `None`。实测：实盘加载后 `config` 完全没有 `backtest` 键，而 `_raw_effective["backtest"]` 仍保有该块。
3. **日线序列缺失时是 fail-open。** 实盘把该币记为 unavailable 后不发表，而 `EntryRegimeGateConfig::is_on` 对 disabled/缺席的表返回 `true`，于是该币继续按未门控策略交易。

## 这次改了什么

| 方面 | 改动 |
| --- | --- |
| 求值点 | `SymbolInput.regime_eval_ts_ms: Option<u64>`。存在时 orchestrator 在该时刻用 `bot_params.{long,short}.entry_regime_gate` 自行推导两个方向判决；缺席时沿用旧的布尔字段。 |
| 回测 | 逐 bar 刷新只写 bar 时间戳，于是实盘与回测跑同一份规则（`passivbot-rust/src/backtest.rs`）。 |
| 实盘 payload | 规划 symbol dict 带 `regime_eval_ts_ms`，由与建表共用的同一次时钟读取得到；未配置门控时为 `None`。 |
| 声明来源 | 先读 `live.entry_regime_gate`（现在是 schema 声明且 partially-open 的配置键：`src/config/schema.py`、`src/config/hydrate.py`）；只有 `backtest.entry_regime_gate` 块存在时，从实盘加载器保留的原始文档回读。解析到的来源会写进 `[regime_gate]` 日志行的 `source=`。 |
| 缺证据 | 为该 UTC 日写一条显式 risk-off 行（`enabled=true`、`regime=[0]`），由同一条规则阻断新风险；该币写日志，且这一轮不进缓存，下个周期重试。 |
| 可观测 | 每次重建表发布一个 `entry_regime.gate.verdict` 事件。 |
| Rust 测试 | `EntryRegimeGateConfig::{allows_initial, allows_reentry}` 及单测；orchestrator 侧覆盖时间戳路径与分方向标志。 |
| Python 测试 | fail-closed 行、verdict 事件、求值时间戳、两种声明写法，以及带反事实的引擎/表 parity 测试。 |
| dry run | 离线 harness 测试 + 人工 smoke 用的 config/scenario。 |

## 落地过程中定位到的根因

| 症状 | 根因 | 修法 |
| --- | --- | --- |
| `tests/test_live_entry_regime_gate.py` 全量 23 个红灯、单独运行全绿 | 新增的 `[regime_gate]` 日志行有 7 个参数却只有 6 个 `%s`：有 handler 格式化该记录时抛 `TypeError: not all arguments converted during string formatting`（pytest 的 logging 捕获会格式化） | 修格式串 |
| 同一文件还报 `asyncio.run() cannot be called from a running event loop` | 同步测试函数里用 `asyncio.run` 驱动协程，而 `pytest.ini` 是 `asyncio_mode = auto` | 改成 `async def` + `await`（36 个函数） |
| 实盘 bot 永远看不到声明的门控 | 实盘加载剥掉整个 `backtest` 子树 | 声明 live 键 + 原始文档回退 |

需要更正一处早先审计的推断：`BACKTEST_INHERITED_LIVE_KEYS` **从未**含有元组/字符串不匹配的问题。`*tuple(PARTIALLY_OPEN_CONFIG_PATHS)` 展开位于 `TEMPLATE_SYNC_PRESERVE_PATHS`，那里是正确的；`src/config/hydrate.py` 不需要那类改动。

## 交付

| 项 | 值 |
| --- | --- |
| 分支 | `codex/entry-regime-gate-live-wiring` |
| 撰写时 head | `8cc058afe`（其后紧接本记录的中文版重写提交） |
| Pull request | [#3](https://github.com/annypay/passivbot/pull/3)（base：`codex/entry-regime-gate-and-gated-profile`） |
| 父 PR | #2（门控、profile、证据）——合并它才能有干净的回退故事 |

## 撰写时的验证结果

- `cargo test --no-default-features --lib`：333 通过 / 0 失败；`cargo check --tests` 干净。
- `tests/test_entry_regime_gate_engine_parity.py`：引擎的 entry 判决在表产生的**每个边界（±1 分钟）**都与共享的 Python 表一致；未声明该标志的方向会忽略 risk-off 表；不带时间戳的调用方保持旧契约；并且在同一个 risk-off 时刻，门控开启时阻断、把同表 `enabled: false` 时会开仓——这就是把门控与策略隔离开的反事实。
- `tests/test_live_entry_regime_gate.py` 与 `tests/test_entry_regime_gate.py`：全绿，含 fail-closed 行、verdict 事件与两种声明写法。
- `tests/test_entry_regime_gate_config_survival.py`：`backtest.*` 与 `live.*` 两种写法都通过 `clean_config`、`prepare_config` 与脱敏 dump。
- `tests/test_fake_live_entry_regime_gate_dry_run.py`：两段离线 harness 运行。
- 全量 `pytest`：只剩 7 个既有的 `tests/test_cache_warmup_reuse.py` 失败。
- `src/tools/check_ai_docs.py`：0 error。`generate_live_event_registry.py --check`：最新。

## Dry run 及其边界

离线、无凭据、每次运行秒级：

```bash
venv/bin/python -m pytest tests/test_fake_live_entry_regime_gate_dry_run.py -q
```

通过真实规划路径证明两件事：没有任何已完成日线历史时，门控发布 risk-off 行并把该币记为 unavailable（fail-closed）；tape 填不满慢窗口时是 risk-off 而不是报错。两次运行都是零成交，这正是要点。

更长 tape 上的人工 smoke（第一次端到端证明"能发布"的那次运行）：

```bash
venv/bin/python src/tools/run_fake_live.py \
  configs/fake_live_entry_regime_gate.hjson \
  scenarios/fake_live/entry_regime_gate_wiring_smoke.hjson \
  --max-steps 250 --output-dir artifacts/fake_live --snapshot-each-step
```

那条 tape 是十天的小时级数据：238 步，发布 1 个 `entry_regime.gate.verdict`（`sma_fast_days=2, sma_slow_days=3, symbol_count=1, risk_on_sides=1, risk_off_sides=0, unavailable_count=0`），零成交——小时级行不是连续的分钟，策略自身的输入因此一直不可用，被验证的只有门控本身。

**明确记录的边界（不粉饰）：** 在这个 harness 里跑不通"下跌腿不开仓 vs 上涨腿开仓"的**成交级**对比。当 tape 跨越数天、而假时钟与墙上时钟相差数月时，staged live planner 会拒绝：
`RuntimeError: staged planner precondition failed before market snapshot refresh: missing current-epoch surfaces=balance,fills,open_orders,positions`
（`src/live/planning_gates.py:107`）。这是 harness/时钟的限制，不是门控行为，所以该隔离性质改在引擎层断言（上面的反事实测试）。将来若要重试，命令就是上面的人工 smoke 配一条 1 分钟 tape；如果 harness 将来有了跟随墙上时钟的时钟，成交级四步对比应该回到 `tests/test_fake_live_entry_regime_gate_dry_run.py`。

## 实盘排障手册

### 要看的字段

| 字段 | 位置 | 正常值 | 异常含义 |
| --- | --- | --- | --- |
| `sma`、`confirm_days` | `[regime_gate]` 日志行 | 配置的 20/50，`confirm_days=0` | 加载了别的配置 |
| `source` | `[regime_gate]` 日志行 | `live.entry_regime_gate` 或 `_raw_effective:backtest.entry_regime_gate` | 声明了门控却显示 `none`，说明门控没被读到 |
| `symbols` | `[regime_gate]` 日志行 | 已生成表的 approved 币数 | 小于多头交易域，说明某些币没读成功 |
| `risk_off_sides` | 日志行，以及事件里的 `risk_off_sides` | 非下跌趋势中为 0 | 连续多日偏高是门控在工作，不是故障 |
| `unavailable` / `unavailable_count` | 日志行与事件 | 0 | >0 表示这些币的新仓被阻断（fail-closed），直到重试成功——去查交易所的日线接口 |
| `unavailable_symbols` | 事件（有上限的样本） | 无 | 点名日线读取失败的币 |
| `day_start_ms`、`planning_ts_ms` | 事件 | `planning_ts_ms` 落在以 `day_start_ms` 开始的那一天内 | `planning_ts_ms` 早于 `day_start_ms` 说明时钟有问题 |
| `risk_on_sides` | 事件 | 方向数 − `risk_off_sides` − unavailable 的方向数 | 与日志行不一致说明计数在一轮中变了 |
| `regime_eval_ts_ms` | 规划 payload（debug profile） | 本轮的交易所时间 | `null` 表示未配置门控 |
| `_orchestrator_entry_regime_gate_tables` | 进程内状态 | 每个 approved 币/方向一条 | 缺某个币，说明它没拿到表 |
| `_orchestrator_entry_regime_gate_unavailable_symbols` | 进程内状态 | 空 | 重试名单；读取成功后会自行清空 |
| `_orchestrator_entry_regime_gate_cache` | 进程内状态 | 当前 UTC 日 | 停在过去的某一天说明重建没有完成 |

关于日志行：bot 自己的日志里会有它。在假交易所 harness 里，这一行会进控制台而不是 `fake_live.log`——harness 先挂上自己的文件 handler，随后 bot 配置 logging 时替换了根 handler；所以 harness 里的断言只读 `live_events.json`，绝不读那个文件。

### 症状 → 处置

| 症状 | 可能原因 | 处置 |
| --- | --- | --- |
| 启动后头几天完全不开仓 | warm-up：在 `sma_slow_days + confirm_days + 2` 个已完成日出现之前判决都是 risk-off | 20/50 下约 52 天的常态；用 `[regime_gate]` 行确认，不要靠关掉门控来"验证" |
| 只有一个币不开仓，其它正常 | 该币判决为 risk-off，或它的日线读取失败 | 看 `unavailable` 与事件里的 `unavailable_symbols`；反复失败属于交易所/K 线问题 |
| regime 看起来关闭的日子却开了仓 | 交易所提供的日线序列与对照的图表不同（交易所、收盘日边界或缺口） | 把该交易所该币的日收盘与此表的 `transition_ts` 逐日对照 |
| 完全没有 `[regime_gate]` 行 | 没有解析到门控 | 看 `source=`；只写 `backtest.entry_regime_gate` 的配置也能通过 `_raw` 解析，所以出现 `none` 就是真的没有该键 |
| 所有币的 `unavailable` 都非 0 | 日线接口或交易所时钟故障 | 按数据事故处理：这些币的风险按设计是关闭的 |
| 有日志行但没有 `entry_regime.gate.verdict` 事件 | 事件管线未 flush 或未路由 | 检查 live event pipeline 健康；日志行仍是兜底 |
| 启动时扩展 stamp 不匹配 | Rust 扩展比源码旧 | 重建扩展（bot 按设计拒绝不匹配的 stamp） |
| 改完配置后一切被阻断 | 改动的块不合法（例如 `gate_mode`） | 加载器会拒绝不支持的值；读启动报错 |

### 回退

1. 把配置所用声明里的 `enabled` 置为 `false`——门控停止过滤，表也不再生成。重载配置或重启 bot。
2. 或者 revert 本次接线的 commit 并重启：行为与本记录之前完全一致，门控只存在于回测。
3. 两种回退都不触碰 tracked 证据或已发布 profile。

## 观察项

见 `README.md` 的看板（W1–W5）。实盘第一周之后，这份记录应新增一段带日期的补充：第一个 risk-on 切换日期、任何 `unavailable` 事件，以及反事实核对（W5）是否成立。
