# 0002 — 币安实盘调试与上线（2026-09-17 起）

状态：**进行中，停在阶段 C（余额为 0，无法进入实盘启动）**
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
| 初始仓位比例 | `bot.long.strategy.trailing_martingale.entry.initial_qty_pct = 0.0081` |

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

### 资金需求（需要你决定，方案见下）

`initial_qty_pct = 0.0081`（初始敞口 = 余额的 0.81%），所以「首单能否过 min_cost」直接由余额决定：

| 想覆盖的币 | min_cost | 需要的最低余额 |
| --- | --- | --- |
| 只要 5 USDT 档（约 35 币） | 5 | ≈ **620 USDT** |
| 加上 20 USDT 档（ETH/BCH/LINK/LTC） | 20 | ≈ **2,500 USDT** |
| 加上 BTC | 50 | ≈ **6,200 USDT** |

余额更小时不会报错，但 `filter_by_min_effective_cost=true` 会**静默**把对应币剔出可交易集合 —— 这会让实盘币池小于 40 币证据口径，必须记录。

三个选项：

- **(a) 入金 ≥ 6,200 USDT**：最接近 g4 证据的 40 币口径，推荐。
- **(b) 入金 1,000–2,500 USDT**：接受 BTC（可能还有部分 20 USDT 档币）被跳过，币池约 30–39 币，属可接受的偏离但要写进记录。
- **(c) 按小资金放大 `initial_qty_pct` 等仓位参数**：这是**改策略**，等于放弃 g4 证据的仓位口径，不建议直接上实盘；若要，须重新回测+深度分析再上线。

## 未决与后续

1. **入金**（阻塞项）：按上面 (a)/(b)/(c) 选定后我再执行阶段 D。
2. **密钥轮换**：对话中已暴露，建议在币安后台轮换或至少确认无提现权限 + 绑 IP。
3. **HSL 关闭**：上线后账户级没有自动兜底；是否需要打开 `bot.long.hsl.enabled` 或设置 `live.max_realized_loss_pct` 作为最后防线，请决定。
4. **TON**：实盘实际币池为 39 币（TON 合约不可交易）；如要保持配置与运行一致，可在私有运行配置里替换它。
5. **长期部署**：本机 WSL + tmux 跑通后再考虑 systemd / Docker live profile / VPS（VPS 需要单独授权）。