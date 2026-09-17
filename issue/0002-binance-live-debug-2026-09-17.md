# 0002 — 币安实盘调试与上线（2026-09-17 起）

状态：**进行中 —— 阶段 A/B/C 全绿；已完成零余额启动冒烟（见阶段 D）；等待入金后执行正式启动**
分支：`codex/binance-live-debug`（基于 `codex/entry-regime-gate-warmup`，即 PR #5）
Pull request：[#6](https://github.com/annypay/passivbot/pull/6)（本轮的探针工具、代理修复与本记录）
实盘策略：`backtests/binance/g4_sma20_50_replay_2026-09-16/artifacts/g4_sma20_50.config.json`（公共证据文件，运行期间**零改动**，账号用 `-u` 覆盖）
运行账号：`binance_live`（账户名与密钥只存在于 `api-keys.json`，该文件被 `.gitignore:423` 忽略；**本记录永不含密钥**）
运行环境：本机 WSL，tmux 会话 `pblive`，入口 `passivbot live <config> -u <account>`（与 README 第 6/7 步、容器契约一致）

授权边界（本轮用户明确授权）：写入密钥到 `api-keys.json`；认证只读探测；探测全绿后按原配置全量 40 币 long-only 启动实盘。命中硬停条件则停在阶段 C 并报告。

## 安全前提（必读）

- 密钥曾出现在对话文本中，**视为已泄露**：上线后尽快在币安后台轮换；轮换后只需重填 `api-keys.json`，命令不变。
- 建议密钥权限：仅 USDT-M Futures、读取 + 交易、**禁提现**、绑定 IP；不要给现货/划转权限。
- 仓库纪律：密钥只允许存在于 `api-keys.json`；文档、日志、commit、PR 一律不含密钥。
- 已核验：`git log --all -S<key>` 与 `-S<secret>` 命中数均为 **0**（密钥从未进入仓库历史）。

## 阶段 A：离线预检（无网络、无凭据）

| 时间 | 命令 | 结果 | 判断 |
| --- | --- | --- | --- |
| 2026-09-17 | `passivbot tool live-config-preflight <g4 config> --compare configs/examples/trailing_martingale_twel100_ddf060_sma20_50.json` | `ok=true`，`error_count=0`，`warning_count=0`；`identity.user=bybit_01`（模板名）、`exchange=null`、`exchange_source=not_in_config` | 配置本身可用于实盘；**必须**用 `-u binance_live` 覆盖账号 |
| 2026-09-17 | 同上 `diff` 段 | 与已发布 profile 仅 3 处差异：`backtest.exchanges`（binance+bybit → binance）、`live.approved_coins.long` 41 → 40、`live.approved_coins.short` 41 → 0 | 实盘 = 单交易所、40 币、只做多；与 g4 证据口径一致。`backtest.*` 在 live 加载时被剥离 |
| 2026-09-17 | `passivbot tool hsl-startup-preview <g4 config>` | 双侧 `hsl.enabled=false`；`signal_mode=coin` | HSL（权益硬止损）关闭 → 上线后**无自动 panic 兜底**，见「未决」 |
| 2026-09-17 | `python -c "import passivbot_rust, rust_utils; rust_utils.verify_loaded_runtime_extension()"` | `runtime_compiled_source_stamp == expected_source_fingerprint == 70c3ecda…`；`sha256=d9c40766…` | 扩展与源码一致，启动不会因 stamp 失配被拒 |
| 2026-09-17 | `git check-ignore -v api-keys.json`、`git ls-files api-keys.json` | 命中 `.gitignore:423`；tracked 计数 0 | 密钥文件不会被提交 |

实盘生效的运行值（取自 g4 配置的 `live` / `bot.long`）：

| 项 | 值 |
| --- | --- |
| approved_coins | 40 币，仅 long；`minimum_coin_age_days=60` 会过滤上市不足 60 天的币 |
| `n_positions` / TWE | 7 / `1.0`（账户级总敞口上限 = 1 倍余额） |
| `leverage` / 保证金模式 | 10 / cross，one-way（`hedge_mode=false`） |
| 下单 | `market_orders_allowed=false`、TIF=GTC → 纯 maker；`ema_gate_mode=all` |
| 门控 | 20/50、`confirm_days=0`、`block_initial/reentry=true`；启动预热 60 天（PR #5） |
| HSL / max_realized_loss_pct | 关闭 / 1（等于关闭） |
| 初始仓位比例 | `bot.long.strategy.trailing_martingale.entry.initial_qty_pct = 0.0081` —— 注意这是**每仓位敞口预算**（`TWE/n_positions=1/7`）的系数，不是余额的系数；实际首单 ≈ 余额的 0.1585%（含 `we_excess_allowance_pct=0.37`） |

## 阶段 B：公开数据探测（真实网络、**无凭据**）

工具：`passivbot tool entry-regime-probe`（本 PR 新增：只读、公网未认证行情、不下单；用 `utils.load_ccxt_instance` 建客户端，因此代理与自定义端点与实盘同一路径）。

```bash
venv/bin/passivbot tool entry-regime-probe \
  backtests/binance/g4_sma20_50_replay_2026-09-16/artifacts/g4_sma20_50.config.json \
  --out /tmp/dsh-live/b1_entry_regime_probe.json
```

窗口 `2026-07-18 .. 2026-09-16`（61 个已收盘日）；门控 `required_days=50`、`lookback_days=60`。

| 指标 | 值 |
| --- | --- |
| 币数 | 40（全部来自 `live.approved_coins.long`） |
| risk_on | **36** |
| risk_off | **3**：CC、GRAM、ONDO |
| unavailable | **1**：TON —— `inactive_linear_swap_market`（币安 USDT-M 上 `TON/USDT:USDT` 存在但 `active=false`，live 会把该币判为不可交易并跳过） |
| depth_min / missing_days_max | **61 / 0** |
| min_cost 分布 | 5 USDT（多数）、20（BCH、ETH、LINK、LTC）、50（BTC） |

判断：

1. **PR #5 的 60 天预热在真实币安数据上被完整满足**：没有任何币深度不足或出现缺口，所有可交易币 `depth=61`。
2. 当日 36/39 个可交易币为 risk-on → 上线首日具备进场条件；CC/GRAM/ONDO 为 risk-off（真实下跌趋势），被拦是对的。
3. TON 无需从配置里删除：live 解析到不可交易合约会跳过它；但「40 币」在实盘实际是 39 币，记录在案。
4. 网络：直连币安会被重置／超时，必须走代理。用 `HTTPS_PROXY=http://127.0.0.1:10808` 探测成功（即 README_CN「WSL 镜像网络的代理配置」场景）。

## 阶段 C：认证只读探测（用密钥）

只读，**未创建、未撤销任何订单**。密钥落盘：`api-keys.json` 新增 `binance_live`（`exchange=binance`，key/secret 各 64 位）；写入脚本用后即删。

1）官方只读探针：

```bash
venv/bin/passivbot tool ticker-endpoint-probe --users binance_live --account-only \
  --skip-order-book --skip-ohlcv --out /tmp/dsh-live/c1_account_probe.json
```

`account_critical_health`：**3/3 成功、0 失败**（balance / positions / open orders）；`time_sync`：时钟偏移 **12 ms**；`load_markets` 1.77s，897 个合约。

2）账户快照（同一套客户端，只读）：

| 项 | 值 | 判断 |
| --- | --- | --- |
| USDT 余额 | **0.0**（另有 0.0125 USDC） | **硬停：不满足交易条件** |
| 持仓 | 0 | 账户空仓 ✓ |
| 挂单（`fapi/v1/openOrders`） | 0 | 无遗留挂单，不会触发并发 Passivbot 保护 ✓ |
| 持仓模式 | one-way（`dualSidePosition=false`） | 与 `hedge_mode=false` 一致 ✓ |
| 多资产保证金 | false | 与 USD 计价一致 ✓ |
| 手续费 | maker `0.0002` / taker `0.0005` | **与证据假设的 v4 契约完全一致** ✓ |

**阶段 C 结论：密钥、权限、时钟、账户状态全部就绪，唯一缺口是余额为 0 → 不进入阶段 D。**

### 阶段 C 顺带发现并修复的问题：实盘客户端不继承代理

- 现象：`passivbot tool ticker-endpoint-probe` 在设置 `HTTPS_PROXY` 的情况下仍然 `RequestTimeout`（30s）并报了现货域名的私有端点，说明它根本没走代理。
- 根因：`utils.load_ccxt_instance` 会在检测到代理环境变量时设置 `aiohttp_trust_env=True`（研究/下载器路径✓），但 **live bot 自己的 `CCXTBot._build_ccxt_config()` 没有这一项**，而 README_CN 明确承诺「配置了标准代理环境变量时，Passivbot 的 CCXT REST 客户端会自动继承它们」。文档与实现不一致。
- 处理：本 PR 让 live 客户端也遵循该契约（`ccxt_should_trust_environment()`，用户显式设置仍优先），并加单测 `tests/test_ccxt_bot_proxy_trust.py`。
- 当下也可用账户级开关绕过：`api-keys.json` 的 `binance_live` 里加 `"aiohttp_trust_env": true`（本轮就是这么让探针跑通的）；代码修复后该字段不再是必需，但保留无害。

## 阶段 D：实盘启动（**待入金后执行**）

未执行。启动命令已定：

```bash
tmux new -s pblive
venv/bin/passivbot live \
  backtests/binance/g4_sma20_50_replay_2026-09-16/artifacts/g4_sma20_50.config.json \
  -u binance_live --log-level info
```

启动后 30 分钟检查清单（执行时逐项填写）：

1. `[regime_gate] warmup lookback_days=60 required_days=50 history_days_min≥50 missing_days_max=0 unavailable=…`；
2. `entry_regime.gate.verdict` 事件出现在 `bot.ready` **之前**；
3. `bot.ready` 与启动耗时、EMA readiness；
4. 无异常撤单、挂单全为 maker、`n_positions ≤ 7`、总敞口 ≤ 1.0×；
5. `passivbot tool live-smoke-report --brief --exchange binance --user binance_live` 全绿；无 error 级事件、无并发保护停机。

### 资金需求（**已更正**，需要你决定，方案见下）

> **更正（2026-09-17，同日）**：本节初版给出「620 / 2,500 / 6,200 USDT」是**错的**。当时把首单规模当成了 `余额 × initial_qty_pct`，漏掉了两个乘数：每个仓位的敞口预算 `wallet_exposure_limit = total_wallet_exposure_limit / n_positions`，以及超出额度乘数 `(1 + we_excess_allowance)`。正确的判定式见下，真实数字由探针重算（下表）。初版数字偏小约 7.8 倍，据此入金会导致**实际可交易币数远少于预期**。

实盘的首单规模与准入门槛（代码位置：`passivbot-rust/src/entries.rs::calc_initial_entry_qty` 与 `src/passivbot.py::effective_min_cost_is_low_enough`）：

```text
首单成本 = 余额 × (TWE / n_positions) × (1 + 有效超出额度) × initial_qty_pct
准入判定 = 首单成本 ≥ effective_min_cost[symbol]        # live.filter_by_min_effective_cost=true 时
```

对 g4 配置：`TWE=1.0`、`n_positions=7`、`we_excess_allowance_pct=0.37`（bounded）、`initial_qty_pct=0.0081`
→ 乘数 = `(1/7) × 1.37 × 0.0081 = 0.00158529`，即**余额的 0.1585%**。

`effective_min_cost` 不是配置里的 `min_cost`，而是「按当前价、按 qty_step **向上取整后**的可执行最小名义额」，通常高于 `min_cost`（例如 BTC：`min_cost=50`，但 `qty_step=0.001 BTC` → 实际约 **76.3 USDT**）。

用真实币安行情跑出的门槛（2026-09-17，探针命令见下）：

| 余额 | 通过准入的币数 | 说明 |
| --- | --- | --- |
| < 3,157 | **0/39** | 低于最便宜的 ALGO（3,157），`filter_by_min_effective_cost` 会把**所有**币剔除，bot 只打一条 "No symbols are approved due to min effective cost too high" 警告，**什么都不交易** |
| 3,200 | ~5/39 | 只有 ALGO/ARB/KAS/ATOM/DOGE 这类 |
| 4,000 | 30/39 | 缺 BTC/ETH/LINK/BCH/LTC/AAVE 等 9 个 |
| 5,000 | 33/39 | |
| 10,000 | 34/39 | |
| 13,810 | 38/39 | 只缺 BTC |
| **48,132** | **39/39** | 全币池（BTC 决定上限） |

单个币的门槛（前几名）：BTC **48,132**、ETH 13,800、LINK 12,669、BCH 12,645、LTC 12,633、AAVE 7,648、AVAX 4,758、BNB 4,575、UNI 4,264。

复现命令（公网、无凭据）：

```bash
passivbot tool entry-regime-probe \
  backtests/binance/g4_sma20_50_replay_2026-09-16/artifacts/g4_sma20_50.config.json \
  --balance 20000 --out /tmp/dsh-live/b3_probe_funding.json
# 每币输出 effective_min_cost / min_balance_required / affordable_at_balance，
# 汇总输出 min_balance_for_all_coins 与 affordable_coins=N/40
```

三个选项（按更正后的数字）：

- **(a) 入金 ≥ 48,200 USDT**：39/39 全币池，最接近 g4 证据口径，推荐。
- **(b) 入金 13,800–20,000 USDT**：38/39，仅 BTC 被跳过（BTC 的 qty_step 门槛最高）。
- **(c) 入金 4,000–5,000 USDT**：30–33 币，缺的是 min_cost 20 档与 AAVE；偏离证据口径较大，需要写进记录并接受。
- **不足 3,157 USDT 不要启动**：不会"少交易几个币"，而是**完全不会交易**。
- 任何放大 `initial_qty_pct`（或提高 `n_positions` 之外的其他仓位参数）来适配小资金的做法都等于**改策略**，须重新回测 + 深度分析，不建议直接上实盘。

## 阶段 D 冒烟：零余额启动（未成交，风险为零）

在**不入金**的前提下验证实盘启动链路本身（认证、行情、首次 K 线、对账、风险输入、fail-closed 行为）与监控工具。零余额意味着任何下单都会被风险输入闸门挡住，因此本次冒烟不可能产生成交。

```bash
tmux new-session -d -s pblive -c /home/mrseven2204/passivbot \
  "venv/bin/passivbot live backtests/binance/g4_sma20_50_replay_2026-09-16/artifacts/g4_sma20_50.config.json \
     -u binance_live --log-level info > /tmp/dsh-live/d1_bootstrap_console.log 2>&1"
```

时间线（UTC）：

| 时刻 | 事件 | 结果 |
| --- | --- | --- |
| 03:49:26 | `runtime.started`、配置加载 | `[config] changed live.user bybit_01 -> binance_live` ✓ `-u` 覆盖生效 |
| 03:49:26 | 市场加载 | 命中本地缓存；`[config] skipping unsupported markets for approved_coins: coins=TON` → **live 币池 39 币**，与阶段 B 探针一致 |
| 03:49:28 | forager 列表 | `counts=long:39,short:0`；short 侧因零敞口关闭 |
| 03:49:30 | 全币种挂单扫描 | `scoped_orders=0 broad_orders=0 extra_orders=0` ✓ |
| 03:50:00 | `account-ready` / `active-candle-ready` | 33.82s / 34.03s；`trading-ready candle warmup skipped: no positions/open orders` |
| 03:50–03:56 | 39 币首次 K 线拉取 | 公网行情、经代理，全部成功 |
| 03:55:46 | `risk.input.status` deferred | `reason=current_balance_unavailable balance_raw=0.0 retry_count=9 max_attempts=10` |
| 03:56:49 | `risk.input.status` failed → `bot.stopped` | `action=stop_without_restart` → `RiskInputUnavailable` → `FatalBotException`（`stage=risk_input_readiness`），**进程自行退出** |

结论：

1. **链路全通**：160/160 远程调用成功、0 失败；账户关键面（balance/positions/open_orders）0 失败；monitor 写入 513 条结构化事件。
2. **零成交**：复检账户为 持仓 0、挂单 0、`positionAmt` 非零 0 —— 本次冒烟没有触碰任何仓位。
3. **fail-closed 按设计生效**：余额为 0 时 bot 拒绝启动并自行退出，符合「不得伪造交易输入」的核心规则；重试 10 次（每次 60s）后 `stop_without_restart`。
4. **门控预取排在风险输入就绪之后**：本次 `entry_regime.gate.verdict` 事件 0 条、`bot.ready` 0 条，因为预取在 `risk_input_recovery.wait_for_startup()` 之后，余额为 0 时不会执行。因此「`[regime_gate] warmup` 先于 `bot.ready`」这条**尚未在真实运行中观察到**（由 fake-live 端到端用例 + 阶段 B 真实数据探针间接覆盖），入金后作为启动清单第 1 项确认。这是一个明确的证据边界，不当作已验证。
5. **账户级副作用（已发生，需知悉）**：passivbot 启动时按设计调用 `set_position_mode(True)`，把币安账户的**持仓模式从单向切到双向**（复检 `dualSidePosition=true`；冒烟前为 false）。`live.hedge_mode=false` 只表示「不请求同币种同时持多空」，short 侧 TWE=0 所以不会开空。若要改回单向，需在无持仓、无挂单时操作（币安限制）。

证据与复现：

```bash
ls -l logs/binance_live.log                                   # 指向本次带时间戳日志
passivbot tool live-smoke-report monitor/binance/binance_live --brief
passivbot tool live-event-query monitor/binance/binance_live --problem-events --limit 12 --compact
```

`live-smoke-report`（只扫本次 bot 目录）：`account_critical_remote_calls` 159/159 成功、**0 限流**；`attention_count=8` = 4 条 problem events + 4 条日志匹配，全部来自上面那条零余额链路，没有其它异常。

### 生产预取方法对真实交易所的验证（无凭据、无订单）

为把「门控预热在真实数据上可用」从**探针间接证明**推进到**生产代码直接证明**，用真实 ccxt 公开客户端构造运行时自己的 `CandlestickManager`，在一个裸 `Passivbot` 实例上直接调用生产方法 `prewarm_entry_regime_gate()`（不启动 bot、无凭据、无订单）：

```python
raw = json.load(open(g4_config))                     # 原始文档，门控走 backtest.* 回退
client = load_ccxt_instance("binance")               # 公开 REST，代理生效
cm = CandlestickManager(exchange=client, exchange_name="binance", cache_dir="caches", archive_enabled=False)
bot = Passivbot.__new__(Passivbot)                   # 裸实例 + 真实 CM
bot.config, bot.coin_overrides, bot.cm = raw, {}, cm
bot.approved_coins_minus_ignored_coins = {"long": {39 个可交易 symbol}, "short": set()}
bot.get_exchange_time = lambda: now_ms
bot._emit_entry_regime_gate_verdict = capture
summary = await bot.prewarm_entry_regime_gate()
```

实测输出（2026-09-17，真实币安日线）：

```text
[regime_gate] sma=20/50 confirm_days=0 source=backtest.entry_regime_gate symbols=39   lookback_days=60 required_days=50 history_days_min=61 missing_days_max=0 risk_off_sides=3 unavailable=0
[regime_gate] warmup lookback_days=60 required_days=50 symbols=39 history_days_min=61   missing_days_max=0 risk_on_sides=36 risk_off_sides=3 unavailable=0 elapsed_ms=33616.76
```

| 观察项 | 值 | 意义 |
| --- | --- | --- |
| `lookback_days` / `required_days` | 60 / 50 | PR #5 的派生深度 |
| `history_days_min` / `missing_days_max` | 61 / 0 | 39 个币的日线全部取满、无缺口 |
| `risk_on_sides` / `risk_off_sides` | 36 / 3 | risk-off 为 CC、GRAM、ONDO |
| `unavailable` | 0 | 没有币因取数失败被 fail-closed 挡住 |
| `elapsed_ms` | 33.6s | 39 币串行日线取数（经代理）≈ 0.86s/币，启动清单可用这个量级预期 |
| 缓存 | `_orchestrator_entry_regime_gate_cache` 已结算 | 首周期直接复用，不再取数 |
| TON | `inactive_linear_swap_market` | 与阶段 B 探针一致，live 币池 39 币 |

**交叉验证**：本方法给出的 36 risk-on / 3 risk-off 与独立探针（`entry-regime-probe`，另一条代码路径）**完全一致**，且事件 payload 四个新字段（`lookback_days`/`required_days`/`min_history_days`/`max_missing_days`）齐全。

**仍未在真实运行中观察到的**：`[regime_gate] warmup` 行相对于 `bot.ready` 的**先后顺序**。零余额时预取不会执行（见上节），因此该顺序目前只有 fake-live 端到端用例覆盖；入金后的启动清单第 1–2 项即为此确认。

### 启动后的持续记录与监控（每轮照做）

```bash
passivbot tool live-smoke-report monitor/binance/binance_live --brief        # 账户关键面/限流/attention
passivbot tool live-event-query monitor/binance/binance_live --problem-events --limit 12 --compact
passivbot tool live-event-query monitor/binance/binance_live   --event-type entry_regime.gate.verdict --compact                            # 每日门控判决
tail -n 50 logs/binance_live.log                                              # 文本日志（符号链接）
```

每轮把「进程存活、cycle/fill/position、门控判决变化、error 级事件、账户挂单类型」记进本文件的时间线，并注明命令与结果。

### 入金后的启动清单（顺序按本次实测修正）

1. `[regime_gate] warmup lookback_days=60 required_days=50 history_days_min≥50 missing_days_max=0 unavailable=…`；
2. `entry_regime.gate.verdict` 出现在 `bot.ready` **之前**；
3. `bot.ready` 与启动耗时、EMA readiness、对账结果；
4. 挂单全为 maker、`n_positions ≤ 7`、总敞口 ≤ 1.0×；
5. `passivbot tool live-smoke-report monitor/binance/binance_live --brief` 的 `account_critical_remote_calls` 无失败、无限流。
   （注意：该工具**没有** `--exchange/--user` 参数，必须用位置参数指向 bot 目录；本轮实测踩到过。）

## 未决与后续

1. **入金**（阻塞项）：按上面 (a)/(b)/(c) 选定后我再执行阶段 D。
2. **密钥轮换**：对话中已暴露，建议在币安后台轮换或至少确认无提现权限 + 绑 IP。
3. **HSL 关闭**：上线后账户级没有自动兜底；是否需要打开 `bot.long.hsl.enabled` 或设置 `live.max_realized_loss_pct` 作为最后防线，请决定。
4. **TON**：实盘实际币池为 39 币（TON 合约不可交易）——本次冒烟已实测确认 live 会跳过它；如要保持配置与运行一致，可在私有运行配置里替换它。
6. **账户持仓模式已被切到双向**（阶段 D 冒烟所致，属 passivbot 设计行为）；若你希望保持单向，需要另行改回并确认没有其它工具依赖。
5. **长期部署**：本机 WSL + tmux 跑通后再考虑 systemd / Docker live profile / VPS（VPS 需要单独授权）。