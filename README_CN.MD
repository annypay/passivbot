![Passivbot](docs/images/pbot_logo_full.svg)

# 运行于 Bybit、OKX、Bitget、Bitunix、GateIO、Binance、Kucoin、Hyperliquid 和 WEEX 的交易机器人

:warning: **使用风险自担** :warning:

最新发布版本：**[v8.1.0](docs/release_notes_v8.1.0.md)**。
从 `master` 克隆的版本还包含 [Unreleased](CHANGELOG.md#unreleased) 中列出的后续变更。选择版本前请阅读[发布状态和版本含义](docs/releases.md)；本文档描述的是当前所查看版本。

> **从 v7 升级：**v8 的配置和策略存在不兼容变更。请勿使用未经审查的 v7 配置直接启动 v8 实盘。阅读 [v8.0.0 升级说明](docs/release_notes_v8.0.0.md)，并在需要保留 v7 trailing-grid 行为时使用明确的 `passivbot tool migrate-config-v7` 工具。

## 概述

Passivbot 是一个使用 Python 和 Rust 编写的加密货币交易机器人，旨在减少人工干预。

它在永续期货衍生品市场上运行，代表用户自动创建和取消限价买卖订单。它不试图预测未来价格走势，也不追随趋势；相反，它是一个逆势做市策略，使用价格区间、EMA 派生的市场背景和风险控制，在两个方向上为价格变动提供阻力，从而“服务市场”，帮助稳定价格。

订单规划由实盘交易和回测共同使用的 Rust 编排器计算，以兼顾速度和一致性。项目还包含一个优化器，可通过对不同候选配置迭代数千次回测，利用进化算法收敛到更优配置。

## 策略

默认的 `trailing_martingale` 策略以小额开仓开始，并可在价格向不利方向移动时加仓，但受配置的敞口和风险限制约束。再次入场会改变平均入场价格；平仓则寻求配置的利润率，或使用追踪确认。入场规模、距离和追踪行为均可配置。

也可使用 `ema_anchor` 策略。`trailing_grid_v7` 是仅供明确 v7 迁移使用的已弃用兼容策略。有关各策略的参数，请参阅[机器人配置指南](docs/config.bot.md)。

### 追踪订单

除网格入场和平仓外，Passivbot 还可配置使用追踪入场和追踪平仓。

对于追踪入场，机器人等待价格越过指定阈值，然后按定义的百分比回撤后才放置再次入场订单。类似地，追踪平仓会在价格先有利移动并达到阈值、再按定义百分比回撤后才放置平仓订单。与在固定距离平仓相比，这可能让机器人在市场显示反转迹象时更有效地锁定利润。

网格订单和追踪订单可以组合使用，机器人可将全部或部分仓位通过网格订单和/或追踪订单进行入场或平仓。

### Forager

Forager 功能会动态选择哪些已批准市场可以开仓。它先剔除相对交易量较低的候选项，再根据可配置的报价交易量、EMA 就绪度和 1 分钟对数区间波动率权重，对剩余市场排序。

### 解套机制

Passivbot 会通过随时间实现小额亏损来管理表现不佳或“被套”的仓位。当多个仓位被套时，机器人优先处理入场价格与当前市场价格差距最小的仓位以进行“解套”。系统通过确保账户余额不会低于历史峰值余额的一定百分比来限制亏损。

## 安装

请按以下步骤安装 Passivbot 及其依赖项。

Passivbot 支持 **Python 3.12 和 Python 3.14**。固定依赖集不支持 Python 3.13。

### 升级 v7 配置

V8 不会静默重新解释 v7 策略字段。为在将配置移至规范的 v8 结构时保留 v7 trailing-grid 行为，请运行：

```sh
passivbot tool migrate-config-v7 \
  path/to/config_v7.json \
  path/to/config_v8_trailing_grid_v7.json \
  --report path/to/v7_migration_report.json
```

该工具会写入已弃用的兼容策略 `trailing_grid_v7`；它不会将配置转换为新的 `trailing_martingale` 策略。如果不支持或含糊不清的字段需要人工审查，除非明确提供 `--allow-manual-review-output`，该命令会返回非零状态且不会写入输出配置。考虑用于实盘前，请审查报告并回测结果。新配置和新的优化工作应从 `configs/examples/default_trailing_martingale_long.json` 开始。

### 第 1 步：克隆仓库

首先，将 Passivbot 仓库克隆到本地机器：

```sh
git clone https://github.com/enarjord/passivbot.git
cd passivbot
```

这会检出 `master`。如需固定标签版本，请遵循[版本选择](docs/releases.md)。

### 第 2 步：安装 Rust

Passivbot 的部分组件使用 Rust。请按以下步骤安装 Rust：

访问 https://www.rust-lang.org/tools/install
按照说明安装 Rustup（Rust 安装程序和版本管理工具）。安装后，重新启动终端或命令提示符。

### 第 3 步：创建并激活虚拟环境

创建虚拟环境以管理依赖。

**Linux/macOS：**
```sh
# 将 python3.14 替换为已安装且受支持的可执行文件（python3.12 或 python3.14）。
PYTHON_BIN=python3.14
"$PYTHON_BIN" --version
"$PYTHON_BIN" -m venv venv
```

**Windows（命令提示符或 PowerShell）：**
```cmd
py -3.12 -m venv venv
```

激活虚拟环境：

**Linux/macOS：**
```sh
source venv/bin/activate
```

**Windows（命令提示符）：**
```cmd
venv\Scripts\activate
```

**Windows（PowerShell）：**
```powershell
.\venv\Scripts\Activate.ps1
```

### 第 4 步：安装 Passivbot

选择适合使用场景的安装档位：

- **仅实盘 VPS**：`python3 -m pip install -e .`
- **回测 / 优化 / 研究工作站**：`python3 -m pip install -e ".[full]"`
- **贡献 / 文档 / lint 工具**：`python3 -m pip install -e ".[dev]"`

所有档位都会构建 Rust 扩展并注册 `passivbot` 命令。

典型的仅实盘安装：

```sh
python3 -m pip install -e .
```

### 第 5 步（可选）：构建 Rust 扩展

Passivbot 会尝试自动构建所需的 Rust 扩展；也可进入 `passivbot-rust` 目录并使用 `maturin` 手动构建：

```sh
cd passivbot-rust
maturin develop --release
cd ..
```

如果检测到 Rust 源代码变更，则需要重新编译，Passivbot 会在启动时尝试完成此操作。要手动重新编译，请使用上面的命令。

### 第 6 步：添加 API 密钥

复制 API 密钥模板文件：

```sh
cp api-keys.json.example api-keys.json
```

将你的密钥添加到 api-keys.json。

### 第 7 步：运行 Passivbot

> **提示：**
> 为确保缓存文件夹名称兼容 Windows（即使不在 Windows 上），请设置环境变量 `WINDOWS_COMPATIBILITY=1`。
> 仅在某些场景需要，例如在 Docker（Linux）中运行，同时将 `caches` 文件夹挂载到 Windows 主机时。

使用默认设置启动机器人：

```sh
passivbot live -u {account_name_from_api-keys.json}
```

或使用 `configs/examples/default_trailing_martingale_long.json` 作为起点创建新的配置文件，并使用以下命令启动：

```sh
passivbot live path/to/config.json
```

为向后兼容，旧式直接脚本入口如 `python3 src/main.py ...`、`python3 src/backtest.py ...` 和 `python3 src/optimize.py ...` 仍可照常使用。

规范的硬编码默认值位于 `src/config/schema.py`。示例配置 `configs/examples/default_trailing_martingale_long.json` 提供了维护中的默认策略配置，因此建议将其复制后作为新配置的起点。

### 日志

Passivbot 在机器人、回测器和辅助工具中均使用 Python 的 `logging` 模块。

- 在 `passivbot live` 或 `passivbot backtest` 上使用 `--log-level {warning|info|debug|trace}` 或 `--log-level {0-3}`` 调整运行时详细程度：`0 = 仅警告`、`1 = 信息`、`2 = 调试`、`3 = 跟踪`。
- 在 `passivbot live` 上使用 `--verbose` 强制调试日志（`--log-level debug`）。
- 通过在配置顶层加入 `"logging": {"level": 2}` 持久化默认值。CLI 标志在该次运行中始终覆盖配置值。
- `passivbot live` 现在默认会在 `logs/` 下写入带时间戳的日志文件，并刷新 `logs/{user}.log` 作为当前运行的稳定别名。在 Windows 上没有创建符号链接的权限时，该路径会成为一个小型指针文件，Passivbot 的监控工具会自动跟随它。可通过 `config.logging.persist_to_file`、`config.logging.dir` 和 `config.logging` 中可选的轮换设置来控制。
- CandlestickManager 和其他子系统会继承所选日志级别，以便始终如一地检查 EMA 预热、数据获取和缓存行为。

### 运行多个机器人

当每个机器人使用独立的交易所账户或子账户时，支持在同一台机器上运行多个 Passivbot 实例。请勿在同一账户上运行相互竞争的机器人；参见[并发 Passivbot 保护](docs/live.md#concurrent-passivbot-protection)。每个进程共享相同的磁盘 OHLCV 缓存，candlestick manager 现在使用短生命周期、自愈锁并自动清理过期锁，因此一个停滞进程不会阻塞其他进程。无需手动删除锁文件；机器人会在启动时清理过期锁，并在获取锁超时时记录日志。

## Jupyter Lab

JupyterLab 是可选的独立安装。激活机器人虚拟环境后，在仓库根目录执行安装和启动：

```shell
python3 -m pip install jupyterlab
python3 -m jupyter lab
```

## 要求

- Python 3.12 或 3.14（不支持 Python 3.13）
- 仅实盘交易使用 `python3 -m pip install -e .`
- 回测、优化、下载器和高级工具使用 `python3 -m pip install -e ".[full]"`
- 在完整安装基础上增加贡献者工具时使用 `python3 -m pip install -e ".[dev]"`

## 示例配置

请从维护中的[示例配置](configs/examples/)开始，并按照[配置工作流](docs/config_workflow.md)为你的使用场景回测和调优。社区配置也可见于 https://pbconfigdb.scud.dedyn.io/.

## 文档

更详细的信息请参阅[文档索引](docs/index.md)。

常用入口：

- [发布状态与升级](docs/releases.md)
- [安装](docs/installation.md)
- [配置工作流](docs/config_workflow.md)
- [配置参考](docs/configuration.md)
- [故障排除](docs/troubleshooting.md)
- [回测](docs/backtesting.md)
- [优化](docs/optimizing.md)
- [指标参考](docs/metrics.md)
- [权益硬止损](docs/equity_hard_stop_loss.md)
- [监控输出](docs/monitor.md)

## 支持

[![Discord](https://img.shields.io/badge/Discord-7289DA?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/QAF2H2UmzZ)

[![Telegram](https://img.shields.io/badge/Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/passivbot_futures)

## 第三方链接、推荐与小费

**Hyperliquid Reference Vault**
Passivbot 的参考长期配置文件在 Hyperliquid Vault 上运行：
https://app.hyperliquid.xyz/vaults/0x490af7d4a048a81db0f677517ed6373565b42349

**Passivbot GUI**
Passivbot 的图形用户界面：
https://github.com/msei99/pbgui

**推荐：**
如使用以下推荐链接注册，我们将不胜感激：
https://accounts.binance.com/register?ref=TII4B07C
https://partner.bybit.com/b/passivbot
https://partner.bitget.com/bg/Y8FU1W
https://www.okx.com/join/PASSIVBOT
https://app.hyperliquid.xyz/join/PASSIVBOT
https://www.kucoin.com/r/broker/CX8QZQJX
https://www.weex.com/en/register?vipCode=ppc8

**关于 Binance 的说明**
为支持 Passivbot 的持续开发，请使用满足以下条件的 Binance 账户：
1) 创建日期晚于 2024-09-21；且
2) 满足以下任一条件：
  a) 创建时未使用推荐链接；或
  b) 创建时使用的推荐 ID 为：`"TII4B07C"`。

Passivbot 仅从满足这些条件的账户交易中获得佣金。

**BuyMeACoffee：**
https://www.buymeacoffee.com/enarjord

**捐赠：**
如果机器人盈利，可考虑通过捐赠表达对其开发的感谢：

- USDT 或 USDC，Binance Smart Chain BEP20：
0x4b7b5bf6bea228052b775c052843fde1c63ec530
- USDT 或 USDC，Arbitrum One：
0x4b7b5bf6bea228052b775c052843fde1c63ec530
- Zcash (ZEC)：
u1jlans93rrqusqx2wp5020aezyt0q22l4tuy7ezkna06fuyaa2gxzremf50wsj3k83a4cm0cncs6zt9urlpte7a3nzvq992z48jxzem455acmhmhhwfwjcjwl8z79vlznla0r3jln6ety565254h96whnllcmepmpqu3ft9hxtqvkn0m7

通过 Strike 接收 Bitcoin (BTC)：
enarjord@strike.me

## 许可证

这是在公共领域发布的自由且无负担的软件。

任何人均可出于任何目的，以商业或非商业方式，通过任何手段自由复制、修改、发布、使用、编译、销售或分发本软件，无论是源代码形式还是编译后的二进制形式。

在承认版权法的司法辖区内，作者将其对本软件享有的任何及全部版权权益奉献给公共领域。该奉献旨在惠及公众，并明确不利于作者的继承人和继任者。我们意图使该奉献成为永久放弃当前及未来全部权利的明确行为。

本软件按“原样”提供，不作任何明示或暗示的保证，包括但不限于对适销性、特定用途适用性及不侵权的保证。在任何情况下，作者均不对因本软件或其使用或其他处理而产生、由此引起或与之相关的任何索赔、损害或其他责任负责，无论该责任基于合同、侵权还是其他理论。

更多信息请参阅 <https://unlicense.org/>。