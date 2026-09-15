#!/usr/bin/env python3
from pathlib import Path

import pandas as pd


ROOT = Path("backtests/binance/causal_comparison_2026-09-14")
OUTPUT = ROOT / "causal_backtest_comparison_report.md"
SCENARIO_NAMES = {
    "B0": "原引擎严格复现",
    "C1": "修正引擎 T+1 / close-first",
    "C2": "修正引擎 T+1 / entry-first",
    "C3": "修正引擎 T+2 / close-first",
    "C4": "修正引擎 T+2 / entry-first",
}


def money(value):
    return f"{float(value):,.2f}"


def percent(value):
    return f"{100.0 * float(value):,.2f}%"


def number(value, digits=3):
    return f"{float(value):,.{digits}f}"


def integer(value):
    return f"{int(value):,}"


def table(headers, rows):
    result = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    result.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(result)


def delta_text(frame, left, right):
    a = frame.loc[left]
    b = frame.loc[right]
    return (
        f"{left}→{right}：最终权益 {money(b.final_equity - a.final_equity)} USDT"
        f"（{percent(b.final_equity / a.final_equity - 1.0)}），"
        f"最大回撤变化 {100.0 * (b.minute_close_max_drawdown - a.minute_close_max_drawdown):+.2f} 个百分点，"
        f"成交数变化 {int(b.fills - a.fills):+,}。"
    )


def render():
    scenario = pd.read_csv(ROOT / "scenario_comparison.csv").set_index("scenario")
    expected = list(SCENARIO_NAMES)
    missing = [name for name in expected if name not in scenario.index]
    if missing:
        raise RuntimeError(f"Missing completed scenarios: {missing}")
    annual = pd.read_csv(ROOT / "annual_metrics.csv")
    monthly = pd.read_csv(ROOT / "monthly_metrics.csv")
    coins = pd.read_csv(ROOT / "coin_metrics.csv")
    provenance = pd.read_csv(ROOT / "data_provenance_audit.csv")
    execution = pd.read_csv(ROOT / "execution_boundary_summary.csv").set_index("scenario")
    price_boundaries = pd.read_csv(ROOT / "fill_price_boundary_summary.csv").set_index("scenario")
    examples = pd.read_csv(ROOT / "execution_boundary_examples.csv")

    scenario_rows = []
    for name in expected:
        row = scenario.loc[name]
        scenario_rows.append(
            [
                name,
                SCENARIO_NAMES[name],
                money(row.final_balance),
                money(row.final_equity),
                percent(row.equity_return),
                percent(row.minute_close_max_drawdown),
                integer(row.fills),
                money(row.signed_fees),
                integer(row.ending_positions),
                integer(row.max_slots),
                number(row.max_twel_long, 6),
            ]
        )

    annual_rows = []
    for _, row in annual.iterrows():
        annual_rows.append(
            [
                row.scenario,
                row.period,
                percent(row.balance_return),
                percent(row.equity_return),
                percent(row.minute_close_drawdown),
                integer(row.fills),
                money(row.net_realized_pnl),
            ]
        )

    month_rows = []
    for name in expected:
        group = monthly.loc[monthly.scenario == name]
        best = group.loc[group.equity_return.idxmax()]
        worst = group.loc[group.equity_return.idxmin()]
        deepest = group.loc[group.minute_close_drawdown.idxmax()]
        month_rows.append(
            [
                name,
                f"{best.period} / {percent(best.equity_return)}",
                f"{worst.period} / {percent(worst.equity_return)}",
                f"{deepest.period} / {percent(deepest.minute_close_drawdown)}",
            ]
        )

    coin_rows = []
    for name in expected:
        group = coins.loc[coins.scenario == name].sort_values("net_realized_pnl")
        worst = group.iloc[0]
        best = group.iloc[-1]
        top5 = group.nlargest(5, "net_realized_pnl").net_realized_pnl.sum()
        total = group.net_realized_pnl.sum()
        coin_rows.append(
            [
                name,
                f"{best.coin} / {money(best.net_realized_pnl)}",
                f"{worst.coin} / {money(worst.net_realized_pnl)}",
                percent(top5 / total) if total else "n/a",
                integer((group.net_realized_pnl < 0).sum()),
            ]
        )
    execution_rows = []
    for name in expected[1:]:
        row = execution.loc[name]
        execution_rows.append(
            [
                name,
                integer(row.audit_rows),
                f"{int(row.min_activation_delay_bars)}–{int(row.max_activation_delay_bars)}",
                integer(row.fills_on_activation),
                integer(row.fills_after_activation),
                integer(row.max_resting_age_bars),
                integer(row.preactivation_fill_rows),
                integer(row.duplicate_order_id_rows),
                integer(row.invalid_fill_interval_rows),
            ]
        )

    provenance_totals = provenance[
        [
            "declared_valid_rows",
            "matched_valid_v2_rows",
            "unmatched_rows",
            "mismatching_valid_rows",
            "checksummed_v2_chunks",
            "excluded_leading_storage_rows",
        ]
    ].sum()
    b0 = scenario.loc["B0"]
    c1 = scenario.loc["C1"]
    c2 = scenario.loc["C2"]
    c3 = scenario.loc["C3"]
    c4 = scenario.loc["C4"]
    accounting_rows = [
        [
            name,
            money(scenario.loc[name].gross_realized_pnl),
            money(scenario.loc[name].signed_fees),
            money(scenario.loc[name].net_realized_pnl),
            money(scenario.loc[name].final_unrealized_pnl),
            money(scenario.loc[name].gross_traded_notional_usdt),
        ]
        for name in expected
    ]
    drawdown_rows = [
        [
            name,
            scenario.loc[name].max_drawdown_peak_candle,
            scenario.loc[name].max_drawdown_trough_candle,
            money(scenario.loc[name].max_drawdown_trough_equity),
            scenario.loc[name].max_drawdown_recovery_candle,
            number(scenario.loc[name].max_drawdown_peak_to_recovery_days, 2),
        ]
        for name in expected
    ]
    risk_duration_rows = [
        [
            name,
            integer(scenario.loc[name].slot_limit_excess_fill_rows),
            integer(scenario.loc[name].slot_limit_excess_label_minutes),
            percent(scenario.loc[name].full_slots_label_fraction),
            number(scenario.loc[name].position_held_days_max, 2),
            number(scenario.loc[name].equity_peak_recovery_days, 2),
        ]
        for name in expected
    ]
    constraint_examples = []
    for name in ("C3", "C4"):
        events = pd.read_csv(ROOT / name / "constraint_events.csv")
        audit = pd.read_csv(ROOT / name / "execution_boundary_audit.csv")
        selected = [
            ("首次第八槽", events.loc[events.held_slots_after_fill > 7].iloc[0]),
            ("最大记录 WEL", events.loc[events.wallet_exposure.idxmax()]),
            ("最大 TWEL", events.loc[events.twe_long.idxmax()]),
        ]
        for event, row in selected:
            provenance_row = audit.iloc[int(row.ledger_row)]
            constraint_examples.append({
                "scenario": name,
                "event": event,
                "ledger_row": int(row.ledger_row),
                "fill_candle": row.timestamp,
                "coin": row.coin,
                "order_type": row.type,
                "qty": row.qty,
                "position_size_after": row.psize,
                "wallet_exposure": row.wallet_exposure,
                "twel": row.twe_long,
                "held_slots_after_fill": int(row.held_slots_after_fill),
                "order_id": int(provenance_row.order_id),
                "decision_index": int(provenance_row.decision_index),
                "activation_index": int(provenance_row.activation_index),
                "fill_index": int(provenance_row.fill_index),
            })
    pd.DataFrame(constraint_examples).to_csv(ROOT / "constraint_event_examples.csv", index=False)

    lines = [
        "# 默认 Trailing Martingale 三年因果回测对照与前视风险复核",
        "",
        "## 结论摘要",
        "",
        f"- 原始基线 B0 已在冻结配置、冻结 40 币 HLCV、冻结市场规则和原 Rust 二进制下严格复现："
        f"{integer(b0.fills)} 条成交逐行一致，最终余额 {money(b0.final_balance)} USDT，"
        f"分钟收盘权益 {money(b0.final_equity)} USDT。",
        f"- 修正后最接近原设定的 C1 最终权益为 {money(c1.final_equity)} USDT，"
        f"相对 B0 变化 {money(c1.final_equity - b0.final_equity)} USDT"
        f"（{percent(c1.final_equity / b0.final_equity - 1.0)}）。这是所有因果执行修正的净影响，"
        "不能把差额全部归因于单一 `next_candle` 读取。",
        f"- T+1 下，成交先后假设从 close-first 改为 entry-first 后，最终权益变化 "
        f"{money(c2.final_equity - c1.final_equity)} USDT；T+2 下对应变化 "
        f"{money(c4.final_equity - c3.final_equity)} USDT。这量化了只有 H/L/C 而没有逐笔路径时的"
        "同分钟顺序敏感性。",
        f"- 额外延迟一根完整 1 分钟 bar 后，close-first 的最终权益变化 "
        f"{money(c3.final_equity - c1.final_equity)} USDT，entry-first 的变化 "
        f"{money(c4.final_equity - c2.final_equity)} USDT。T+2 是压力情景，不是实测延迟，"
        "也不是收益下界。",
        "- 五个场景都完整运行到右端边界且没有触发模拟强平；这只说明当前简化保证金与 candle "
        "模型没有触发 liquidation，不代表真实交易所路径不会先强平。",
        "- 不能把风险边界简化为 STOP/HSL。此次冻结配置的 HSL 关闭；真实风险还包括深度套牢、"
        "跨币相关性、订单未实际挂入或排队、部分成交、延迟撤单、资金费率、费率/交易规则漂移、"
        "历史币池与参数的回溯选择，以及分钟内价格路径不可识别。",
        "",
        "## 实验设计与不可变输入",
        "",
        "- 交易窗口：2023-09-12 00:00 UTC 至 2026-09-12 00:00 UTC（右端不含）。",
        "- 数据：Binance USDT-M 永续合约 1 分钟 H/L/C/V；没有 Open，也没有逐笔成交或订单簿。",
        "- 配置：原默认 trailing-martingale long 策略，41 个候选币、有效数据集 40 币、"
        "7 个 long 槽、TWEL 1.5、原始 WEL/allowance/cooldown/手续费/精度与 100,000 USDT 初始资金。",
        "- MNT 不在这份冻结 Binance-only 数据集中；GRAM 虽在 40 币数据集中，但各场景均无成交，"
        "所以实际成交币数都是 39。没有把后来缺失的币悄悄当作零收益。",
        "- 冻结配置 maker 费率 0.0004（0.04%/次成交），taker 费率 0.00055；"
        "`market_orders_allowed=false`，所有实际模拟成交为 maker。市场单滑点设置在本次没有被使用。",
        "- B0 使用原始 Rust 运行时；C1-C4 使用修正后运行时。所有场景复用同一冻结数据与市场设置，"
        "没有重新优化、调参或按结果挑选币种。",
        "",
        table(
            [
                "场景",
                "执行模型",
                "最终余额",
                "最终权益",
                "权益收益",
                "最大回撤",
                "成交数",
                "手续费",
                "期末持仓",
                "最大槽位",
                "最大 TWEL",
            ],
            scenario_rows,
        ),
        "",
        "余额是已实现账户余额；权益按每分钟收盘价标记。二者之差是期末未实现盈亏，"
        "不能把最终余额直接当成可立即无滑点退出后的净资产。",
        "",
        "## 资金与成本核算",
        "",
        table(
            ["场景", "毛已实现 PnL", "带符号手续费", "净已实现 PnL", "期末未实现 PnL", "累计成交名义金额"],
            accounting_rows,
        ),
        "",
        "所有金额单位为 USDT。逐行满足：100,000 + 毛已实现 PnL + 带符号手续费 = 最终余额；"
        "最终余额 + 期末未实现 PnL = 最终权益。这里的累计成交名义金额把买入与卖出分别累加，"
        "并非某一时刻的持仓、所需保证金或可执行市场容量。最终 7 个持仓没有被假定按收盘价"
        "无滑点强制平掉，因此净资产中仍有浮动盈亏。",
        "",
        "## 因果修正内容",
        "",
        "1. Rust 回测订单决策不再读取 `k+1` candle 的 high/low/tradable 状态；订单扩展提示仅来自"
        "当前已知仓位状态，并同时覆盖冷路径与缓存路径。",
        "2. 移除利用完整历史数组提前知道 last-valid 的完美退市退出。已有仓位在下一时刻缺少估值时"
        "显式失败，不使用未来最后一根、陈旧价或零价兜底。",
        "3. 新增稳定订单身份、resting/pending create/cancel 生命周期。delay=0 的订单最早在信号"
        "candle 结束后的 k+1 生效；delay=1 最早在 k+2 生效。未生效撤单期间旧单仍可能成交，"
        "已成交订单不得被延迟快照复活。",
        "4. close-first 与 entry-first 只排序 candle 边界前已经 active 的订单；当根 candle 新生成"
        "的平仓或重新入场不能回填进同一 candle。",
        "5. reduce-only 按成交时实际库存裁剪，避免延迟旧单反向开仓或超额平仓。",
        "6. 成交仍采用严格 `low < buy_limit`、`high > sell_limit`；价格恰好相等不成交。"
        "fill 时间戳仍是 1 分钟 candle-open 标签，不伪造精确毫秒成交时间。",
        "7. BTC 基准改为只向过去取值的有限前向对齐；开头无已知价格、真实有效行价格非法、"
        "或缺口超过配置容忍度时显式失败。显式 source directory 模式不会越界读取其他缓存或联网。",
        "8. 尚未上市/尚无真实 candle 的币不再从未来 first-valid candle 借价格或提前初始化"
        "指标；首次真实 minute/hour 观测到达时才给相应 EMA 赋予样本权重。尚无观测不等于零波动。",
        "",
        "## 对照差异",
        "",
        f"- {delta_text(scenario, 'B0', 'C1')}",
        f"- {delta_text(scenario, 'C1', 'C2')}",
        f"- {delta_text(scenario, 'C1', 'C3')}",
        f"- {delta_text(scenario, 'C2', 'C4')}",
        f"- {delta_text(scenario, 'C3', 'C4')}",
        "",
        "这些差异是路径依赖的组合结果：订单激活时间改变后，仓位、余额、候选排序、槽位占用、"
        "后续挂单价格和风险预算都会联动变化。因此不能用成交数量或单笔差异做线性外推。",
        "特别是此次复用的 BTC 序列没有更换，所有币的 last-valid 都到数据末端：BTC 对齐修复"
        "及缺失估值失败合同对其他数据集很重要，但不能据此声称它们导致了本表的收益变化。"
        "订单扩展、初始可用性、首单时序等修正都包含在 C1-B0 的净差异中。",
        "",
        "## 年度表现",
        "",
        table(
            ["场景", "年份", "余额收益", "权益收益", "分钟收盘最大回撤", "成交数", "净已实现 PnL"],
            annual_rows,
        ),
        "",
        "2023 和 2026 都是不完整自然年；年度收益不可与完整年份直接横向等权比较。",
        "与旧报告的年度表不同，这里以真实初始资金及上期最后一个分钟收盘值为期初，"
        "回撤也改用分钟序列，而非小时采样。因此 B0 虽然逐笔成交完全相同，年度回撤仍可能"
        "高于旧表（例如 2024 年从旧表约 35.18% 变为分钟口径 40.06%），这不是重新交易造成的差异。",
        "",
        "## 月度尾部与路径敏感性",
        "",
        table(["场景", "最佳权益月份", "最差权益月份", "月内最深回撤"], month_rows),
        "",
        "完整逐月结果见 `monthly_metrics.csv`。月度回撤使用分钟收盘权益，仍未覆盖分钟内部可能"
        "更深的瞬时回撤。",
        "",
        "## 最深回撤发生与恢复",
        "",
        "![日末权益与当日最深分钟收盘回撤](scenario_equity_comparison.png)",
        "",
        table(
            ["场景", "前峰 candle（UTC）", "谷底 candle（UTC）", "谷底权益", "恢复该峰 candle（UTC）", "峰至恢复天数"],
            drawdown_rows,
        ),
        "",
        "表中日期均是分钟标签，权益在该分钟收盘才可知；不是精确成交时刻。"
        "最深回撤这一轮的恢复天数，与全期所有权益峰中最长恢复时间、已实现 PnL 峰值恢复时间"
        "是不同指标，不能互换。回撤 73%–79% 即便随后恢复，也意味着此前峰值净资产的大部分"
        "在谷底已经浮亏；不能靠最终余额好看来忽略中途追加保证金、减仓或被迫退出的风险。",
        f"尤其 C3 的谷底权益仅 {money(c3.max_drawdown_trough_equity)} USDT，"
        f"不仅回吐此前盈利，还比 100,000 USDT 初始资金低 "
        f"{percent(1 - c3.max_drawdown_trough_equity / c3.starting_balance)}。"
        "这比单独展示“三年最终仍然盈利”更能反映持有过程。",
        "",
        "## 币种贡献与集中度",
        "",
        table(["场景", "最大净贡献", "最小净贡献", "前五币净贡献占比", "净亏损币数"], coin_rows),
        "",
        "币种贡献不是 40 个彼此独立策略的相加：7 槽、总敞口、余额和 forager 选择使币种之间"
        "相互竞争资金与入场资格。改变某个币的成交路径会改变其他币之后是否能进入。",
        "",
        "## 槽位、WEL/TWEL 与限价成交",
        "",
        table(
            ["场景", "最大槽位", "期末持仓", "最大 WEL", "WEL 超有效阈值行", "最大 TWEL", "TWEL>1.5 行"],
            [
                [
                    name,
                    integer(scenario.loc[name].max_slots),
                    integer(scenario.loc[name].ending_positions),
                    number(scenario.loc[name].max_wel_long, 6),
                    integer(scenario.loc[name].wel_above_effective_rows),
                    number(scenario.loc[name].max_twel_long, 6),
                    integer(scenario.loc[name].twel_above_limit_rows),
                ]
                for name in expected
            ],
        ),
        "",
        f"名义单币 WEL = 1.5 / 7 = {percent(c1.nominal_wel_long)}；"
        f"加上 37% bounded allowance 后的有效阈值约 {percent(c1.effective_wel_long)}。"
        "以上 WEL 是成交账本记录的该币成本敞口/钱包余额，TWEL 是总成本敞口口径，"
        "不是交易所杠杆倍数，也不是每秒市价盯市敞口。",
        "",
        "槽位约束作用于策略编排，但延迟撤单和同分钟多笔 resting order 成交可能改变实际成交后的"
        "短时敞口。B0/C1/C2 的 TWEL 超限属于很小的成交后边界偏差，包含手续费对分母的影响；"
        "C3/C4 则实际出现 8 个持仓、TWEL 1.756/1.794，说明额外一根 bar 的撤单/替换延迟能够造成"
        "显著的计划外并发敞口。7 槽和 TWEL 1.5 都不是交易所提供的硬保证。",
        "",
        table(
            ["场景", "成交后>7槽行", ">7槽标签分钟", "恰好7槽标签占比", "最长持仓天数", "最长权益恢复天数"],
            risk_duration_rows,
        ),
        "",
        "槽位持续时间采用每分钟全部模拟成交后的状态延续至下一个成交分钟，故称“标签分钟”；"
        "当分钟内先开后平时的瞬时第八槽不一定占满一分钟。超限行是成交后快照计数，"
        "不是独立超限事件次数。逐条可追到各场景 `constraint_events.csv`，时长口径见"
        "`slot_occupancy.csv`。成交价、数量、余额和持仓变化可相互核对，不应把所有超限都归咎于"
        "手续费，也不能把多一槽直接解读为实盘必然超过一槽。",
        "",
        "冻结配置启用的是入场总敞口 gate；单币/总敞口 enforcer 均关闭，HSL 也关闭，"
        "entry cooldown 为 24.1 分钟。入场 gate 限制的是决策时看到的组合，不会自动撤销"
        "尚未生效的旧指令；cooldown 也不等于交易所成交锁。故本次没有增加硬止损来改善结果，"
        "也没有在模拟成交时重新按未来状态优化原订单大小。",
        "",
        table(
            ["场景", "事件", "成交 candle（UTC）", "币", "该币 WEL", "TWEL", "槽位"],
            [
                [
                    row["scenario"], row["event"], row["fill_candle"], row["coin"],
                    percent(row["wallet_exposure"]), number(row["twel"], 6),
                    row["held_slots_after_fill"],
                ]
                for row in constraint_examples
            ],
        ),
        "",
        "第八槽、最大单币 WEL、最大 TWEL 是三个独立极值，不是同一个时刻。"
        "例如首次第八槽时 TWEL 仅约 0.0263，说明“币种槽位超限”和“资金敞口超限”不能互相替代。"
        "详见 `constraint_event_examples.csv` 中对应 ledger row、order ID 和决策/激活索引。",
        "",
        "限价成交仅在分钟 H/L 严格穿越订单价时判定。这个规则比 `<=`/`>=` 更保守地处理恰好触价，"
        "但仍无法证明订单此前已被交易所接受、具有足够排队优先级、成交量充足、保持 maker 身份"
        "或一次性全部成交。close-first/entry-first 仅是两种确定性情景，不是真实 O-H-L-C 路径。",
        "",
        "## 执行边界审计",
        "",
        table(
            [
                "场景",
                "审计成交行",
                "激活延迟 bars",
                "激活即成交",
                "激活后成交",
                "最大 resting bars",
                "提前成交",
                "重复 order ID",
                "非 60 秒区间",
            ],
            execution_rows,
        ),
        "",
        "每条 C1-C4 成交均与运行时审计中的币种、方向、订单类型、candle index、candle-open "
        "时间戳、数量和价格逐行核对。`activation_index - decision_index` 为 1 表示 T+1，"
        "为 2 表示额外延迟一根 bar 的 T+2。`resting_age_bars` 大于 0 表示订单激活后继续"
        "跨 bar 挂单，不能解读为真实交易所排队时长。",
        "",
        table(
            ["场景", "买成交", "卖成交", "严格穿越 H/L 的 maker", "仅恰好相等", "未穿越却成交", "末端 sentinel 成交"],
            [
                [
                    name, integer(price_boundaries.loc[name].buy_rows),
                    integer(price_boundaries.loc[name].sell_rows),
                    integer(price_boundaries.loc[name].maker_strict_hl_cross_rows),
                    integer(price_boundaries.loc[name].maker_equality_only_rows),
                    integer(price_boundaries.loc[name].maker_non_cross_rows),
                    integer(price_boundaries.loc[name].end_sentinel_fill_rows),
                ]
                for name in expected
            ],
        ),
        "",
        "上表不是只根据引擎规则作出推断：从冻结 HLCV 按每条成交的 index 和币列重新读取 high/low，"
        "以 signed qty 判买卖方向后逐条判定。所有成交都处于对应币的声明有效期，"
        "没有将最后一行 2026-09-12 00:00 的 sentinel 用作成交 candle。",
        "",
        table(
            ["场景", "信号 close 边界", "订单激活边界", "首次成交 candle-open", "该 candle-close"],
            [
                [
                    row.scenario, row.decision_close_timestamp_utc,
                    row.activation_timestamp_utc, row.fill_candle_open_timestamp_utc,
                    row.fill_candle_close_timestamp_utc,
                ]
                for row in examples.loc[examples.example == "first_fill"].itertuples()
            ],
        ),
        "",
        "例如 T+1 在某分钟 close 边界生成的意图可从同一物理边界开始的下一分钟参与成交，"
        "T+2 则再等 60 秒；这不是在已经结束的信号分钟回填成交。冷却期及回溯比较继续沿用"
        "一致的 candle 标签时钟，而审计另外记录真实可知边界。由于只有分钟桶，无法证明"
        "24.1 分钟冷却和实盘逐毫秒事件完全一致。运行时审计是 fill-only："
        "不会记录全部未成交 create/cancel 请求，因而不能据其 order ID 跳号推断撤单次数、"
        "平均撤单确认延迟或“撤单待生效期间成交”的精确数量。",
        "",
        "## 数据来源与蜡烛边界证据",
        "",
        f"- 冻结数据声明有效行：{integer(provenance_totals.declared_valid_rows)}；"
        f"与本地 Binance v2 数据逐行匹配：{integer(provenance_totals.matched_valid_v2_rows)}；"
        f"有效行 mismatch：{integer(provenance_totals.mismatching_valid_rows)}；"
        f"unmatched：{integer(provenance_totals.unmatched_rows)}。",
        f"- 共核验 {integer(provenance_totals.checksummed_v2_chunks)} 个带 checksum 的 v2 chunk。"
        f"{integer(provenance_totals.excluded_leading_storage_rows)} 个上市前 leading storage placeholder"
        " 位于声明有效区间之外，不参与可交易期。",
        "- checksum 和逐行匹配证明当前冻结数据与本地 v2 内容一致；它们不能单独证明每一行最初"
        "必然来自 Binance archive 还是官方公开 API。此次无需补缺口，未进行网络请求。",
        "- 数据只有 H/L/C/V。`fills.timestamp` 是 candle-open 标签；价格在该分钟内何时触及、"
        "先触及 high 还是 low、跨币先后顺序均不可由数据恢复。",
        "",
        "## 风险边界",
        "",
        f"- 各场景分钟收盘最大回撤范围为 "
        f"{percent(scenario.minute_close_max_drawdown.min())} 至 "
        f"{percent(scenario.minute_close_max_drawdown.max())}。这已经说明“没有强平”并不等于风险可控。",
        f"- B0 的最长权益峰值恢复期为 {number(b0.equity_peak_recovery_days, 2)} 天，"
        f"最长 PnL 峰值恢复期为 {number(b0.pnl_peak_recovery_days, 2)} 天，"
        f"最长单仓持有为 {number(b0.position_held_days_max, 2)} 天。三者口径不同。",
        "- HSL/STOP 在本研究配置中关闭，因此不能把它视为当前结果的风险边界。即使未来启用，"
        "HSL 也只是一个基于已有价格和可执行订单的退出机制，不能消除跳空、延迟、流动性、"
        "交易所故障、全市场相关性和模型误差。",
        "- C3/C4 只增加一根完整 bar 的订单生命周期延迟，没有模拟随机网络延迟、撮合队列、"
        "部分成交、拒单或断线。它们是敏感性压力情景，不是实盘收益的可信下界。",
        "",
        "## 仍未消除的偏差",
        "",
        "- 当前 2026 配置和币池回放至 2023，属于回溯研究，不是当时可获得配置的严格 walk-forward"
        " 或样本外证据。",
        "- 未建模订单簿深度、排队位置、部分成交、撤单回报延迟、真实 maker/taker 转换和滑点。",
        "- 未新增资金费率、历史手续费层级、交易规则/最小下单量变化和账户级保证金细节。",
        "- 1 分钟 OHLC 不能确定分钟内路径，也不能确定多个币种之间的真实事件顺序。",
        "- 分钟收盘权益回撤不是分钟内最坏权益；高杠杆或高敞口路径仍可能在 candle 内更危险。",
        "",
        "## 可复核工件",
        "",
        "- `B0/` 至 `C4/`：各场景独立配置、成交、分析、分钟权益与运行身份。",
        "- `scenario_comparison.csv`：总指标及相对 B0 差异。",
        "- `annual_metrics.csv`、`monthly_metrics.csv`、`coin_metrics.csv`：年度、月度和币种明细。",
        "- `execution_boundary_audit.csv`：运行时订单决策、激活和成交边界汇总。",
        "- `execution_boundary_summary.csv`、`execution_boundary_examples.csv`：边界统计与可读时间实例。",
        "- `fill_price_boundary_summary.csv`：逐笔与冻结 H/L、有效期和末端边界核对。",
        "- `constraint_event_examples.csv`：第八槽、最大 WEL/TWEL 的可定位实例。",
        "- `data_provenance_audit.csv`：冻结有效区间与本地 Binance v2 数据交叉核验。",
        "- `reproducibility_metadata.json`：原始/修正运行时、数据、场景、验证与交付物身份。",
        "- `scenario_equity_comparison.png`：日末权益与当日最深分钟收盘回撤，保留分钟级回撤尖峰。",
        "- `report_tools/`：本次离线重放、指标计算、独立成交核对和报告生成脚本；从仓库根目录运行。",
        "",
        "已有完整结果时，从仓库根目录重新计算报告：",
        "",
        "```bash",
        "tools=backtests/binance/causal_comparison_2026-09-14/report_tools",
        'PYTHONPATH=src venv/bin/python "$tools/analyze_causal_replays.py" report-data',
        'PYTHONPATH=src venv/bin/python "$tools/verify_causal_study.py"',
        'PYTHONPATH=src venv/bin/python "$tools/render_causal_report.py"',
        'PYTHONPATH=src venv/bin/python "$tools/finalize_causal_metadata.py"',
        "```",
        "",
        "脚本会读取固定 study 目录；逐条 H/L 核对需要约 2 GB 数据数组内存。"
        "不要在已完成的 B0/C1-C4 目录上直接启动重放：创建冲突会被拒绝。"
        "B0 属于原运行时身份，新引擎不能冒充原基线。",
        "",
        "本报告能够证明修正后的历史模拟不再依赖已识别的下一根 candle 决策输入，并量化固定成交"
        "顺序和额外一根 bar 延迟的敏感性；它不能证明历史实盘一定会获得任一场景的对应盈亏。",
        "",
    ]
    OUTPUT.write_text("\n".join(lines), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    render()
