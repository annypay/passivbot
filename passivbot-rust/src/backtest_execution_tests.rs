use super::*;
use ndarray::{Array1, Array3};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};

fn params(rows: usize, coins: usize) -> BacktestParams {
    BacktestParams {
        starting_balance: 10_000.0,
        maker_fee: 0.0,
        taker_fee: 0.0005,
        coins: (0..coins).map(|i| format!("COIN{i}")).collect(),
        active_coin_indices: None,
        first_timestamp_ms: 0,
        requested_start_timestamp_ms: 0,
        first_valid_indices: vec![0; coins],
        last_valid_indices: vec![rows - 1; coins],
        warmup_minutes: vec![0; coins],
        trade_start_indices: vec![0; coins],
        global_warmup_bars: 0,
        btc_collateral_cap: 0.0,
        btc_collateral_ltv_cap: None,
        metrics_only: true,
        skip_btc_analysis: true,
        filter_by_min_effective_cost: true,
        dynamic_wel_by_tradability: true,
        hedge_mode: true,
        max_realized_loss_pct: 1.0,
        pnls_max_lookback_days: 30.0,
        liquidation_threshold: 0.05,
        equity_hard_stop_loss: EquityHardStopLossConfig::default(),
        market_orders_allowed: false,
        market_order_near_touch_threshold: 0.001,
        market_order_slippage_pct: 0.0005,
        forager_score_hysteresis_pct: 0.02,
        execution_delay_bars: 0,
        intrabar_fill_order: IntrabarFillOrder::CloseFirst,
        execution_audit_path: None,
        candle_interval_minutes: 1,
    }
}

fn candles(rows: usize, coins: usize) -> Array3<f64> {
    Array3::from_shape_fn((rows, coins, 4), |(_, _, f)| match f {
        HIGH => 110.0,
        LOW => 90.0,
        CLOSE => 100.0,
        _ => 1.0,
    })
}

fn backtest<'a>(
    data: &'a Array3<f64>,
    btc: &'a Array1<f64>,
    params: &BacktestParams,
) -> Backtest<'a> {
    let mut bot = BotParamsPair::default();
    for side in [&mut bot.long, &mut bot.short] {
        side.n_positions = 1;
        side.total_wallet_exposure_limit = 1.0;
        side.wallet_exposure_limit = 1.0;
        side.ema_span_0 = 2.0;
        side.ema_span_1 = 3.0;
        side.entry_initial_qty_pct = 0.01;
        side.entry_grid_double_down_factor = 1.0;
        side.entry_grid_spacing_pct = 0.02;
        side.close_grid_qty_pct = 0.25;
        side.close_trailing_threshold_pct = 0.01;
        side.risk_entry_cooldown_minutes = 0.0;
        side.risk_wel_enforcer_enabled = false;
        side.risk_twel_enforcer_enabled = false;
        side.filter_volume_ema_span_1m = 2.0;
        side.filter_volatility_ema_span_1m = 2.0;
    }
    let exchange = ExchangeParams {
        qty_step: 0.01,
        price_step: 0.01,
        min_qty: 0.01,
        min_cost: 1.0,
        c_mult: 1.0,
        maker_fee: 0.0,
        taker_fee: 0.0005,
    };
    Backtest::new(
        data.view(),
        btc.view(),
        vec![bot; params.coins.len()],
        vec![exchange; params.coins.len()],
        params,
    )
}

fn order(pside: usize, entry: bool, qty: f64, price: f64) -> BacktestOrder {
    let sign = if (pside == LONG) == entry { 1.0 } else { -1.0 };
    BacktestOrder {
        order: Order {
            qty: sign * qty,
            price,
            order_type: match (pside, entry) {
                (LONG, true) => OrderType::EntryGridNormalLong,
                (SHORT, true) => OrderType::EntryGridNormalShort,
                (LONG, false) => OrderType::CloseGridLong,
                _ => OrderType::CloseGridShort,
            },
        },
        execution_type: orchestrator::ExecutionType::Limit,
        lifecycle: None,
    }
}

fn desired(pside: usize, entries: Vec<BacktestOrder>, closes: Vec<BacktestOrder>) -> OpenOrders {
    let mut result = OpenOrders::new(1);
    let bundle = if pside == LONG {
        &mut result.long[0]
    } else {
        &mut result.short[0]
    };
    bundle.entries = entries;
    bundle.closes = closes;
    result
}

fn position(bt: &Backtest, pside: usize) -> Position {
    if pside == LONG {
        bt.positions.long[0]
    } else {
        bt.positions.short[0]
    }
}

fn hold(bt: &mut Backtest, pside: usize, qty: f64) {
    let position = Position {
        size: if pside == LONG { qty } else { -qty },
        price: 100.0,
    };
    if pside == LONG {
        bt.positions.long[0] = position;
    } else {
        bt.positions.short[0] = position;
    }
}

fn entries<'a>(bt: &'a Backtest<'_>, pside: usize) -> &'a [BacktestOrder] {
    if pside == LONG {
        &bt.open_orders.long[0].entries
    } else {
        &bt.open_orders.short[0].entries
    }
}

#[test]
fn t_plus_one_and_two_never_self_fill_and_unchanged_intent_keeps_identity() {
    let data = candles(6, 1);
    let btc = Array1::from_elem(6, 20_000.0);
    for delay in [0, 1] {
        for pside in [LONG, SHORT] {
            let mut p = params(6, 1);
            p.execution_delay_bars = delay;
            let mut bt = backtest(&data, &btc, &p);
            let create = || desired(pside, vec![order(pside, true, 1.0, 100.0)], vec![]);
            bt.reconcile_open_orders(0, create()).unwrap();
            let original = entries(&bt, pside)[0].lifecycle.unwrap();
            bt.check_for_fills(0).unwrap();
            assert!(bt.fills.is_empty());
            bt.reconcile_open_orders(1, create()).unwrap();
            let unchanged = entries(&bt, pside)[0].lifecycle.unwrap();
            assert_eq!(original.id, unchanged.id);
            assert_eq!(unchanged.active_from, 1 + delay);
            bt.check_for_fills(1).unwrap();
            assert_eq!(bt.fills.len(), usize::from(delay == 0));
            bt.check_for_fills(2).unwrap();
            bt.check_for_fills(3).unwrap();
            assert_eq!(bt.fills.len(), 1, "consumed order must never resurrect");
            assert_eq!(bt.fills[0].index, delay + 1);
            assert_eq!(bt.fills[0].timestamp_ms, (delay as u64 + 1) * 60_000);
            let increase_clock = if pside == LONG {
                bt.last_increase_fill_timestamp_long[0]
            } else {
                bt.last_increase_fill_timestamp_short[0]
            };
            assert_eq!(increase_clock, Some(bt.fills[0].timestamp_ms));
            assert_eq!(position(&bt, pside).size.abs(), 1.0);
        }
    }
}

#[test]
fn delayed_cancel_and_replace_remain_fillable_until_effective_boundary() {
    let mut data = candles(7, 1);
    data[[1, 0, HIGH]] = 100.0;
    data[[1, 0, LOW]] = 100.0;
    let btc = Array1::from_elem(7, 20_000.0);
    for pside in [LONG, SHORT] {
        let mut p = params(7, 1);
        p.execution_delay_bars = 1;
        let mut bt = backtest(&data, &btc, &p);
        let initial_price = if pside == LONG { 95.0 } else { 105.0 };
        let replacement_price = if pside == LONG { 96.0 } else { 104.0 };
        bt.reconcile_open_orders(
            0,
            desired(pside, vec![order(pside, true, 1.0, initial_price)], vec![]),
        )
        .unwrap();
        let original_id = entries(&bt, pside)[0].lifecycle.unwrap().id;
        bt.check_for_fills(1).unwrap();
        bt.reconcile_open_orders(
            1,
            desired(
                pside,
                vec![order(pside, true, 2.0, replacement_price)],
                vec![],
            ),
        )
        .unwrap();
        assert_eq!(entries(&bt, pside).len(), 2);
        assert_eq!(entries(&bt, pside)[0].lifecycle.unwrap().cancel_at, Some(3));
        bt.check_for_fills(2).unwrap();
        assert_eq!(
            bt.fills.len(),
            1,
            "old order remains executable at candle 2"
        );
        assert_eq!(bt.fills[0].fill_qty.abs(), 1.0);
        assert!(entries(&bt, pside)
            .iter()
            .all(|o| o.lifecycle.unwrap().id != original_id));
        bt.reconcile_open_orders(2, desired(pside, vec![], vec![]))
            .unwrap();
        bt.check_for_fills(3).unwrap();
        assert_eq!(
            bt.fills.len(),
            2,
            "replacement's later cancel is not retroactive"
        );
        assert_eq!(bt.fills[1].fill_qty.abs(), 2.0);
        bt.check_for_fills(4).unwrap();
        bt.check_for_fills(5).unwrap();
        assert_eq!(bt.fills.len(), 2);
    }
}

#[test]
fn reinstated_cancelled_intent_gets_new_identity_and_due_cancel_precedes_fills() {
    let data = candles(7, 1);
    let btc = Array1::from_elem(7, 20_000.0);
    let mut p = params(7, 1);
    p.execution_delay_bars = 1;
    let mut bt = backtest(&data, &btc, &p);
    let make = || desired(LONG, vec![order(LONG, true, 1.0, 100.0)], vec![]);
    bt.reconcile_open_orders(0, make()).unwrap();
    let original_id = entries(&bt, LONG)[0].lifecycle.unwrap().id;
    bt.reconcile_open_orders(1, desired(LONG, vec![], vec![]))
        .unwrap();
    bt.reconcile_open_orders(2, make()).unwrap();
    assert_eq!(entries(&bt, LONG).len(), 2);
    assert_ne!(entries(&bt, LONG)[1].lifecycle.unwrap().id, original_id);
    bt.check_for_fills(3).unwrap();
    assert!(
        bt.fills.is_empty(),
        "cancel effective at candle 3 precedes its fill check"
    );
    bt.check_for_fills(4).unwrap();
    assert_eq!(bt.fills.len(), 1);
    assert_eq!(bt.fills[0].index, 4);
}

#[test]
fn both_fill_orders_use_only_resting_orders_and_cap_reduce_only_inventory() {
    let data = candles(5, 1);
    let btc = Array1::from_elem(5, 20_000.0);
    for pside in [LONG, SHORT] {
        for ordering in [IntrabarFillOrder::CloseFirst, IntrabarFillOrder::EntryFirst] {
            let mut p = params(5, 1);
            p.intrabar_fill_order = ordering;
            let mut bt = backtest(&data, &btc, &p);
            hold(&mut bt, pside, 1.0);
            bt.reconcile_open_orders(
                0,
                desired(
                    pside,
                    vec![order(pside, true, 1.0, 100.0)],
                    vec![
                        order(pside, false, 3.0, 100.0),
                        order(pside, false, 4.0, 100.0),
                    ],
                ),
            )
            .unwrap();
            bt.check_for_fills(1).unwrap();
            let closes: Vec<_> = bt
                .fills
                .iter()
                .filter(|f| orchestrator::is_close_order_type(f.order_type))
                .collect();
            assert_eq!(closes.len(), 1);
            let expected_close = if ordering == IntrabarFillOrder::CloseFirst {
                1.0
            } else {
                2.0
            };
            assert_eq!(closes[0].fill_qty.abs(), expected_close);
            assert_eq!(position(&bt, pside).size.abs(), 2.0 - expected_close);
            assert_eq!(bt.fills.len(), 2);
            bt.check_for_fills(2).unwrap();
            assert_eq!(
                bt.fills.len(),
                2,
                "no new strategy orders may fill this candle"
            );
        }
    }
}

#[test]
fn flat_reduce_only_is_rejected_before_entry_and_never_closes_fresh_position() {
    let data = candles(5, 1);
    let btc = Array1::from_elem(5, 20_000.0);
    for pside in [LONG, SHORT] {
        for ordering in [IntrabarFillOrder::CloseFirst, IntrabarFillOrder::EntryFirst] {
            let mut p = params(5, 1);
            p.intrabar_fill_order = ordering;
            let mut bt = backtest(&data, &btc, &p);
            bt.reconcile_open_orders(
                0,
                desired(
                    pside,
                    vec![order(pside, true, 1.0, 100.0)],
                    vec![order(pside, false, 10.0, 100.0)],
                ),
            )
            .unwrap();
            bt.check_for_fills(1).unwrap();
            bt.check_for_fills(2).unwrap();
            assert_eq!(bt.fills.len(), 1);
            assert_eq!(position(&bt, pside).size.abs(), 1.0);
        }
    }
}

#[test]
fn flatten_discards_pending_reduce_only_orders_before_same_bar_reentry() {
    let data = candles(6, 1);
    let btc = Array1::from_elem(6, 20_000.0);
    for pside in [LONG, SHORT] {
        let p = params(6, 1);
        let mut bt = backtest(&data, &btc, &p);
        hold(&mut bt, pside, 1.0);
        bt.reconcile_open_orders(
            0,
            desired(
                pside,
                vec![order(pside, true, 1.0, 100.0)],
                vec![order(pside, false, 1.0, 100.0)],
            ),
        )
        .unwrap();
        let bundle = if pside == LONG {
            &mut bt.open_orders.long[0]
        } else {
            &mut bt.open_orders.short[0]
        };
        let mut stale_pending = order(pside, false, 1.0, 100.0);
        stale_pending.lifecycle = Some(OrderLifecycle {
            id: 99,
            decision_index: 0,
            active_from: 3,
            cancel_at: None,
        });
        bundle.closes.insert(0, stale_pending);
        bt.check_for_fills(1).unwrap();
        assert_eq!(bt.fills.len(), 2);
        bt.check_for_fills(3).unwrap();
        assert_eq!(bt.fills.len(), 2);
        assert_eq!(position(&bt, pside).size.abs(), 1.0);
    }
}

#[test]
fn strict_limit_equality_does_not_fill_and_multiple_resting_rungs_fill_once() {
    let data = candles(6, 1);
    let btc = Array1::from_elem(6, 20_000.0);
    for pside in [LONG, SHORT] {
        let p = params(6, 1);
        let mut bt = backtest(&data, &btc, &p);
        let equality = if pside == LONG { 90.0 } else { 110.0 };
        bt.reconcile_open_orders(
            0,
            desired(
                pside,
                vec![
                    order(pside, true, 1.0, 99.0),
                    order(pside, true, 2.0, 101.0),
                    order(pside, true, 3.0, equality),
                ],
                vec![],
            ),
        )
        .unwrap();
        bt.check_for_fills(1).unwrap();
        assert_eq!(bt.fills.len(), 2);
        assert_eq!(position(&bt, pside).size.abs(), 3.0);
        bt.check_for_fills(2).unwrap();
        assert_eq!(bt.fills.len(), 2);
        assert_eq!(entries(&bt, pside).len(), 1);
    }
}

#[test]
fn pending_and_cancel_inflight_orders_remain_visible_to_hysteresis_and_hsl() {
    let data = candles(5, 1);
    let btc = Array1::from_elem(5, 20_000.0);
    let mut p = params(5, 1);
    p.execution_delay_bars = 1;
    let mut bt = backtest(&data, &btc, &p);
    for pside in [LONG, SHORT] {
        bt.reconcile_open_orders(
            0,
            desired(pside, vec![order(pside, true, 1.0, 80.0)], vec![]),
        )
        .unwrap();
        assert!(bt.has_blocking_open_orders_pside(pside));
        let hysteresis = bt.forager_hysteresis_state_from_open_orders();
        assert!(if pside == LONG {
            hysteresis.incumbent_long
        } else {
            hysteresis.incumbent_short
        }
        .contains(&0));
        bt.reconcile_open_orders(1, desired(pside, vec![], vec![]))
            .unwrap();
        assert!(bt.has_blocking_open_orders_pside(pside));
        bt.check_for_fills(3).unwrap();
        assert!(!bt.has_blocking_open_orders_pside(pside));
    }
}

fn snapshot_orders(
    bt: &Backtest,
) -> Vec<(
    usize,
    usize,
    u64,
    u64,
    OrderType,
    u64,
    usize,
    usize,
    Option<usize>,
)> {
    let mut result = Vec::new();
    for (pside, bundles) in [(LONG, &bt.open_orders.long), (SHORT, &bt.open_orders.short)] {
        for (idx, bundle) in bundles.iter().enumerate() {
            for o in bundle.entries.iter().chain(&bundle.closes) {
                let l = o.lifecycle.unwrap();
                result.push((
                    pside,
                    idx,
                    o.order.qty.to_bits(),
                    o.order.price.to_bits(),
                    o.order.order_type,
                    l.id,
                    l.decision_index,
                    l.active_from,
                    l.cancel_at,
                ));
            }
        }
    }
    result
}

fn advance(bt: &mut Backtest, k: usize) {
    bt.current_step = k;
    bt.validate_held_position_valuation(k).unwrap();
    bt.check_for_fills(k).unwrap();
    bt.update_emas(k);
    bt.update_rounded_balance(k);
    bt.update_trailing_prices(k);
    bt.update_n_positions_and_wallet_exposure_limits(k);
    bt.update_open_orders_all(k).unwrap();
}

#[test]
fn future_suffix_endpoints_and_unavailable_placeholders_cannot_change_prefix() {
    let a = candles(12, 3);
    let mut b = candles(15, 3);
    for k in 0..15 {
        for idx in 0..3 {
            if k > 5 || idx == 2 {
                for field in 0..4 {
                    b[[k, idx, field]] *= 10.0 + k as f64;
                }
            }
        }
    }
    let btc_a = Array1::from_elem(12, 20_000.0);
    let btc_b = Array1::from_elem(15, 20_000.0);
    for delay in [0, 1] {
        for ordering in [IntrabarFillOrder::CloseFirst, IntrabarFillOrder::EntryFirst] {
            let mut pa = params(12, 3);
            pa.first_valid_indices[2] = 8;
            pa.trade_start_indices[2] = 8;
            pa.last_valid_indices[0] = 8;
            pa.execution_delay_bars = delay;
            pa.intrabar_fill_order = ordering;
            let mut pb = pa.clone();
            pb.last_valid_indices = vec![14; 3];
            let mut ba = backtest(&a, &btc_a, &pa);
            let mut bb = backtest(&b, &btc_b, &pb);
            assert!(ba.emas[2].long[0].is_nan());
            assert!(bb.emas[2].long[0].is_nan());
            for k in 1..=5 {
                advance(&mut ba, k);
                advance(&mut bb, k);
                assert_eq!(
                    snapshot_orders(&ba),
                    snapshot_orders(&bb),
                    "prefix order divergence at {k}"
                );
                assert_eq!(
                    format!("{:?}", ba.fills),
                    format!("{:?}", bb.fills),
                    "prefix fills at {k}"
                );
                let fresh_a = ba.build_orchestrator_input_iter(k, None, None, 0..3);
                let fresh_b = bb.build_orchestrator_input_iter(k, None, None, 0..3);
                assert_eq!(
                    serde_json::to_value(&fresh_a.symbols).unwrap(),
                    serde_json::to_value(&fresh_b.symbols).unwrap()
                );
                assert!(fresh_a.symbols.iter().all(|s| s.next_candle.is_none()));
                let cached_a = ba.get_orchestrator_input_cached(k, None, None);
                let cached_b = bb.get_orchestrator_input_cached(k, None, None);
                assert_eq!(
                    serde_json::to_value(&cached_a.symbols).unwrap(),
                    serde_json::to_value(&cached_b.symbols).unwrap()
                );
                assert_eq!(
                    serde_json::to_value(&fresh_a.symbols).unwrap(),
                    serde_json::to_value(&cached_a.symbols).unwrap()
                );
                assert!(cached_a.symbols.iter().all(|s| s.next_candle.is_none()));
                assert!(cached_a.symbols[2].order_book.bid.is_nan());
                ba.orchestrator_input_cache = Some(cached_a);
                bb.orchestrator_input_cache = Some(cached_b);
            }
            assert!(
                !ba.fills.is_empty(),
                "fixture must exercise fills, not only idle decisions"
            );
        }
    }
}

#[test]
fn state_hints_match_live_held_expansion_flat_next_only_and_cooldown() {
    let data = candles(8, 1);
    let btc = Array1::from_elem(8, 20_000.0);
    for pside in [LONG, SHORT] {
        let p = params(8, 1);
        let mut bt = backtest(&data, &btc, &p);
        let flat = bt.build_orchestrator_input_iter(0, None, None, 0..1);
        let hints = flat.peek_hints.as_ref().unwrap();
        assert!(hints.expand_grid_long.is_empty() && hints.expand_grid_short.is_empty());
        let output = orchestrator::compute_ideal_orders(&flat).unwrap();
        assert_eq!(
            output
                .orders
                .iter()
                .filter(|o| !orchestrator::is_close_order_type(o.order_type)
                    && (o.pside == orchestrator::PositionSide::Long) == (pside == LONG))
                .count(),
            1
        );
        hold(&mut bt, pside, 10.0);
        let input = bt.build_orchestrator_input_iter(0, None, None, 0..1);
        let hints = input.peek_hints.as_ref().unwrap();
        assert!(if pside == LONG {
            &hints.expand_grid_long
        } else {
            &hints.expand_grid_short
        }
        .contains(&0));
        assert!(if pside == LONG {
            &hints.expand_close_long
        } else {
            &hints.expand_close_short
        }
        .contains(&0));
        let side = if pside == LONG {
            &mut bt.bot_params[0].long
        } else {
            &mut bt.bot_params[0].short
        };
        side.risk_entry_cooldown_minutes = 2.0;
        if pside == LONG {
            bt.last_increase_fill_timestamp_long[0] = Some(60_000);
        } else {
            bt.last_increase_fill_timestamp_short[0] = Some(60_000);
        }
        for k in [1, 2, 3] {
            let input = bt.build_orchestrator_input_iter(k, None, None, 0..1);
            assert_eq!(input.timestamp_ms, k as u64 * 60_000);
            let output = orchestrator::compute_ideal_orders(&input).unwrap();
            let count = output
                .orders
                .iter()
                .filter(|o| {
                    !orchestrator::is_close_order_type(o.order_type)
                        && (o.pside == orchestrator::PositionSide::Long) == (pside == LONG)
                })
                .count();
            assert_eq!(
                count,
                usize::from(k == 3),
                "cooldown on candle labels at {k}"
            );
        }
    }
}

#[test]
fn prelisting_ema_seed_waits_for_first_real_candle_and_warmup_is_not_shortened_by_end() {
    fn range_state(emas: &EMAs) -> [(f64, f64, f64); 6] {
        [
            (
                emas.log_range_long,
                emas.log_range_long_num,
                emas.log_range_long_den,
            ),
            (
                emas.log_range_short,
                emas.log_range_short_num,
                emas.log_range_short_den,
            ),
            (
                emas.volatility_ema_1m_long,
                emas.volatility_ema_1m_long_num,
                emas.volatility_ema_1m_long_den,
            ),
            (
                emas.volatility_ema_1m_short,
                emas.volatility_ema_1m_short_num,
                emas.volatility_ema_1m_short_den,
            ),
            (
                emas.volatility_ema_1h_long,
                emas.volatility_ema_1h_long_num,
                emas.volatility_ema_1h_long_den,
            ),
            (
                emas.volatility_ema_1h_short,
                emas.volatility_ema_1h_short_num,
                emas.volatility_ema_1h_short_den,
            ),
        ]
    }

    let mut a = candles(10, 1);
    let mut b = a.clone();
    for k in 0..4 {
        for f in 0..4 {
            b[[k, 0, f]] *= 1000.0;
        }
    }
    a[[4, 0, CLOSE]] = 105.0;
    b[[4, 0, CLOSE]] = 105.0;
    for data in [&mut a, &mut b] {
        data[[5, 0, HIGH]] = 120.0;
        data[[5, 0, LOW]] = 80.0;
    }
    let btc = Array1::from_elem(10, 20_000.0);
    let mut p = params(10, 1);
    p.first_timestamp_ms = 55 * 60_000;
    p.first_valid_indices[0] = 4;
    p.warmup_minutes[0] = 10;
    p.last_valid_indices[0] = 7;
    let mut ba = backtest(&a, &btc, &p);
    let mut bb = backtest(&b, &btc, &p);
    let alphas = [0.25, 0.4, 0.1, 0.2, 0.3, 0.5];
    for bt in [&mut ba, &mut bb] {
        bt.needs_log_range_long = true;
        bt.needs_log_range_short = true;
        bt.needs_volatility_ema_1m_long = true;
        bt.needs_volatility_ema_1m_short = true;
        bt.needs_volatility_ema_1h_long = true;
        bt.needs_volatility_ema_1h_short = true;
        let alpha = &mut bt.ema_alphas[0];
        alpha.log_range_alpha_long = alphas[0];
        alpha.log_range_alpha_short = alphas[1];
        alpha.volatility_ema_1m_alpha_long = alphas[2];
        alpha.volatility_ema_1m_alpha_short = alphas[3];
        alpha.volatility_ema_1h_alpha_long = alphas[4];
        alpha.volatility_ema_1h_alpha_short = alphas[5];
    }
    assert_eq!(ba.coin_trade_start_idx[0], 14);
    for k in 1..4 {
        ba.update_emas(k);
        bb.update_emas(k);
        assert!(!ba.ema_seeded[0] && !bb.ema_seeded[0]);
        assert!(ba.emas[0].long[0].is_nan() && bb.emas[0].long[0].is_nan());
        for bt in [&ba, &bb] {
            for (value, numerator, denominator) in range_state(&bt.emas[0]) {
                assert!(value.is_nan());
                assert_eq!(numerator, 0.0);
                assert_eq!(denominator, 0.0);
            }
        }
    }
    ba.update_emas(4);
    bb.update_emas(4);
    assert_eq!(ba.emas[0].long, [105.0; 3]);
    assert_eq!(ba.emas[0].long, bb.emas[0].long);
    let first_range = (110.0_f64 / 90.0).ln();
    for bt in [&ba, &bb] {
        for (i, (value, numerator, denominator)) in range_state(&bt.emas[0]).into_iter().enumerate()
        {
            if i < 4 {
                assert!((value - first_range).abs() < 1e-12, "range family {i}");
                assert!((numerator - alphas[i] * first_range).abs() < 1e-12);
                assert_eq!(denominator, alphas[i]);
            } else {
                // No completed bucket contains a genuine listing candle yet.
                assert!(value.is_nan());
                assert_eq!((numerator, denominator), (0.0, 0.0));
            }
        }
    }
    ba.update_emas(5);
    bb.update_emas(5);
    let second_range = (120.0_f64 / 80.0).ln();
    for bt in [&ba, &bb] {
        for (i, (value, numerator, denominator)) in range_state(&bt.emas[0]).into_iter().enumerate()
        {
            let alpha = alphas[i];
            let (expected_num, expected_den) = if i < 4 {
                (
                    alpha * second_range + (1.0 - alpha) * alpha * first_range,
                    alpha + (1.0 - alpha) * alpha,
                )
            } else {
                // The 01:00 boundary closes a bucket containing only real candle 4.
                (alpha * first_range, alpha)
            };
            assert!((numerator - expected_num).abs() < 1e-12, "range family {i}");
            assert!((denominator - expected_den).abs() < 1e-12);
            assert!((value - expected_num / expected_den).abs() < 1e-12);
        }
    }
    assert_eq!(range_state(&ba.emas[0]), range_state(&bb.emas[0]));
    assert!(!ba.coin_is_tradeable_at(0, 7));

    let available_from_start = backtest(&a, &btc, &params(10, 1));
    assert!(
        range_state(&available_from_start.emas[0])
            .iter()
            .all(|state| *state == (0.0, 0.0, 1.0)),
        "preserve initialization for symbols already present on the first dataset row"
    );
}

#[test]
fn state_derived_hints_expand_recursive_closes_but_retracement_entries_stay_sequential() {
    let data = candles(8, 1);
    let btc = Array1::from_elem(8, 20_000.0);
    for pside in [LONG, SHORT] {
        let mut bt = backtest(&data, &btc, &params(8, 1));
        hold(&mut bt, pside, 80.0);
        let strategy = if pside == LONG {
            &mut bt.strategy_params[0].long
        } else {
            &mut bt.strategy_params[0].short
        };
        let StrategyParams::TrailingMartingale(tm) = strategy else {
            unreachable!()
        };
        tm.close.qty_pct = 0.25;
        tm.close.threshold_base_pct = 0.05;
        tm.close.threshold_we_weight = 0.1;
        tm.close.retracement_base_pct = 0.0;
        let fresh = bt.build_orchestrator_input_iter(0, None, None, 0..1);
        let cached = bt.get_orchestrator_input_cached(0, None, None);
        let a = orchestrator::compute_ideal_orders(&fresh).unwrap();
        let b = orchestrator::compute_ideal_orders(&cached).unwrap();
        assert_eq!(
            serde_json::to_value(&a.orders).unwrap(),
            serde_json::to_value(&b.orders).unwrap()
        );
        let closes: Vec<_> = a
            .orders
            .iter()
            .filter(|o| {
                orchestrator::is_close_order_type(o.order_type)
                    && (o.pside == orchestrator::PositionSide::Long) == (pside == LONG)
            })
            .collect();
        assert!(
            closes.len() > 1,
            "held passive closes must recurse without future H/L"
        );
        assert!((closes.iter().map(|o| o.qty.abs()).sum::<f64>() - 80.0).abs() < 1e-9);

        let strategy = if pside == LONG {
            &mut bt.strategy_params[0].long
        } else {
            &mut bt.strategy_params[0].short
        };
        let StrategyParams::TrailingMartingale(tm) = strategy else {
            unreachable!()
        };
        tm.entry.retracement_base_pct = 0.02;
        let trailing = TrailingPriceBundle {
            min_since_open: 80.0,
            max_since_min: 100.0,
            max_since_open: 120.0,
            min_since_max: 100.0,
        };
        if pside == LONG {
            bt.trailing_prices.long[0] = trailing;
        } else {
            bt.trailing_prices.short[0] = trailing;
        }
        let input = bt.build_orchestrator_input_iter(1, None, None, 0..1);
        let output = orchestrator::compute_ideal_orders(&input).unwrap();
        let count = output
            .orders
            .iter()
            .filter(|o| {
                !orchestrator::is_close_order_type(o.order_type)
                    && (o.pside == orchestrator::PositionSide::Long) == (pside == LONG)
            })
            .count();
        assert_eq!(
            count, 1,
            "retracement entries cannot expand into a simultaneous ladder"
        );
    }
}

#[test]
fn unavailable_backtest_books_are_explicit_flat_only_and_live_json_stays_strict() {
    let data = candles(8, 1);
    let btc = Array1::from_elem(8, 20_000.0);
    let mut p = params(8, 1);
    p.first_valid_indices[0] = 4;
    let mut bt = backtest(&data, &btc, &p);
    let mut input = bt.build_orchestrator_input_iter(1, None, None, 0..1);
    assert!(input.symbols[0].backtest_market_data_unavailable);
    assert!(input.symbols[0].order_book.bid.is_nan());
    assert!(orchestrator::compute_ideal_orders(&input)
        .unwrap()
        .orders
        .is_empty());
    input.symbols[0].backtest_market_data_unavailable = false;
    assert!(orchestrator::compute_ideal_orders(&input).is_err());
    input.symbols[0].backtest_market_data_unavailable = true;
    input.symbols[0].long.position = Position {
        size: 1.0,
        price: 100.0,
    };
    assert!(orchestrator::compute_ideal_orders(&input).is_err());
    input.symbols[0].long.position = Position::default();
    input.symbols[0].tradable = true;
    assert!(orchestrator::compute_ideal_orders(&input).is_err());

    let valid =
        backtest(&data, &btc, &params(8, 1)).build_orchestrator_input_iter(1, None, None, 0..1);
    let mut json = serde_json::to_value(&valid).unwrap();
    assert!(json["symbols"][0]
        .get("backtest_market_data_unavailable")
        .is_none());
    json["symbols"][0]["backtest_market_data_unavailable"] = serde_json::json!(true);
    assert!(serde_json::from_value::<orchestrator::OrchestratorInput>(json).is_err());
}

#[test]
fn wrong_sign_reduce_only_is_rejected_without_increasing_inventory() {
    let data = candles(4, 1);
    let btc = Array1::from_elem(4, 20_000.0);
    for pside in [LONG, SHORT] {
        let mut bt = backtest(&data, &btc, &params(4, 1));
        hold(&mut bt, pside, 1.0);
        let mut close = order(pside, false, 2.0, 100.0);
        close.order.qty = -close.order.qty;
        bt.reconcile_open_orders(0, desired(pside, vec![], vec![close]))
            .unwrap();
        bt.check_for_fills(1).unwrap();
        assert_eq!(position(&bt, pside).size.abs(), 1.0);
        assert!(bt.fills.is_empty());
    }
}

fn artifact_path(label: &str) -> PathBuf {
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target")
        .join("execution-test-artifacts");
    std::fs::create_dir_all(&base).unwrap();
    base.join(format!(
        "{label}-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, AtomicOrdering::Relaxed)
    ))
}

#[test]
fn audit_streams_actual_fill_provenance_and_refuses_existing_path() {
    let data = candles(6, 1);
    let btc = Array1::from_elem(6, 20_000.0);
    let path = artifact_path("audit.csv");
    let mut p = params(6, 1);
    p.first_timestamp_ms = 1_709_251_140_000; // Month-end 23:59 candle label.
    p.execution_delay_bars = 1;
    p.execution_audit_path = Some(path.to_string_lossy().into_owned());
    let mut bt = backtest(&data, &btc, &p);
    bt.execution_audit = Some(ExecutionAuditWriter::new(path.to_str().unwrap()).unwrap());
    hold(&mut bt, LONG, 1.0);
    bt.reconcile_open_orders(
        0,
        desired(LONG, vec![], vec![order(LONG, false, 20.0, 100.0)]),
    )
    .unwrap();
    bt.reconcile_open_orders(
        1,
        desired(LONG, vec![], vec![order(LONG, false, 20.0, 100.0)]),
    )
    .unwrap();
    bt.check_for_fills(1).unwrap();
    assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 1);
    bt.check_for_fills(2).unwrap();
    drop(bt.execution_audit.take());
    let csv = std::fs::read_to_string(&path).unwrap();
    let lines: Vec<_> = csv.lines().collect();
    assert_eq!(lines.len(), 2);
    let fields: Vec<_> = lines[1].split(',').collect();
    assert_eq!(fields[4], "0");
    assert_eq!(
        fields[5].parse::<u64>().unwrap(),
        p.first_timestamp_ms + 60_000
    );
    assert_eq!(fields[6], "2");
    assert_eq!(
        fields[7].parse::<u64>().unwrap(),
        p.first_timestamp_ms + 120_000
    );
    assert_eq!(fields[8], "2");
    assert_eq!(
        fields[9].parse::<u64>().unwrap(),
        p.first_timestamp_ms + 120_000
    );
    assert_eq!(
        fields[10].parse::<u64>().unwrap(),
        p.first_timestamp_ms + 180_000
    );
    assert_eq!(fields[11], "-1");
    assert_eq!(fields[12], "100");
    let mut collision = backtest(&data, &btc, &p);
    assert!(collision
        .run()
        .err()
        .unwrap()
        .contains("cannot create execution audit"));
    assert_eq!(std::fs::read_to_string(&path).unwrap(), csv);
    std::fs::remove_file(path).unwrap();
}

#[test]
fn audit_missing_parent_and_write_failures_propagate() {
    let data = candles(5, 1);
    let btc = Array1::from_elem(5, 20_000.0);
    let absent = artifact_path("missing-parent").join("audit.csv");
    let mut p = params(5, 1);
    p.execution_audit_path = Some(absent.to_string_lossy().into_owned());
    assert!(backtest(&data, &btc, &p)
        .run()
        .err()
        .unwrap()
        .contains("cannot create execution audit"));
    let path = artifact_path("read-only.csv");
    std::fs::write(&path, b"preserve").unwrap();
    let mut bt = backtest(&data, &btc, &params(5, 1));
    bt.execution_audit = Some(ExecutionAuditWriter {
        path: path.to_string_lossy().into_owned(),
        writer: BufWriter::new(File::open(&path).unwrap()),
    });
    bt.reconcile_open_orders(
        0,
        desired(LONG, vec![order(LONG, true, 1.0, 100.0)], vec![]),
    )
    .unwrap();
    assert!(bt
        .check_for_fills(1)
        .unwrap_err()
        .contains("cannot flush execution audit"));
    drop(bt);
    assert_eq!(std::fs::read(&path).unwrap(), b"preserve");
    std::fs::remove_file(path).unwrap();
    assert!(ExecutionAuditWriter::new("").is_err());
}

#[test]
fn concurrent_audit_creation_has_exactly_one_winner() {
    let path = artifact_path("concurrent.csv");
    let gate = std::sync::Arc::new(std::sync::Barrier::new(2));
    let mut workers = Vec::new();
    for _ in 0..2 {
        let path = path.clone();
        let gate = gate.clone();
        workers.push(std::thread::spawn(move || {
            gate.wait();
            ExecutionAuditWriter::new(path.to_str().unwrap()).is_ok()
        }));
    }
    assert_eq!(
        workers
            .into_iter()
            .map(|w| usize::from(w.join().unwrap()))
            .sum::<usize>(),
        1
    );
    assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 1);
    std::fs::remove_file(path).unwrap();
}

#[test]
fn depleted_balance_rejects_further_entries_without_replanning_their_size() {
    let data = candles(5, 1);
    let btc = Array1::from_elem(5, 20_000.0);
    let mut p = params(5, 1);
    p.starting_balance = 1.0;
    let mut bt = backtest(&data, &btc, &p);
    bt.exchange_params_list[0].maker_fee = 0.02;
    bt.reconcile_open_orders(
        0,
        desired(
            LONG,
            vec![order(LONG, true, 1.0, 100.0), order(LONG, true, 3.0, 100.0)],
            vec![],
        ),
    )
    .unwrap();
    bt.check_for_fills(1).unwrap();
    assert_eq!(bt.fills.len(), 1);
    assert_eq!(
        bt.fills[0].fill_qty, 1.0,
        "accepted order keeps its decision-boundary size"
    );
    assert!(bt.balance.usd_total_balance < 0.0);
    assert!(entries(&bt, LONG).is_empty());
}

#[test]
#[cfg(unix)]
fn audit_unwritable_directory_creation_is_rejected() {
    use std::os::unix::fs::PermissionsExt;
    let dir = artifact_path("unwritable");
    std::fs::create_dir(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o500)).unwrap();
    let path = dir.join("audit.csv");
    let outcome = ExecutionAuditWriter::new(path.to_str().unwrap());
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    // A privileged test runner may bypass permissions; creation failure is
    // exercised unconditionally by the missing-parent and read-only-file tests.
    if outcome.is_ok() {
        drop(outcome);
        std::fs::remove_file(&path).unwrap();
    } else {
        assert!(outcome
            .err()
            .unwrap()
            .contains("cannot create execution audit"));
    }
    std::fs::remove_dir(dir).unwrap();
}

#[test]
fn delayed_actions_do_not_execute_on_exclusive_end_sentinel() {
    let data = candles(4, 1);
    let btc = Array1::from_elem(4, 20_000.0);
    let mut p = params(4, 1);
    p.execution_delay_bars = 1;
    let mut bt = backtest(&data, &btc, &p);
    bt.reconcile_open_orders(
        1,
        desired(LONG, vec![order(LONG, true, 1.0, 100.0)], vec![]),
    )
    .unwrap();
    let (fills, _) = bt.run().unwrap();
    assert!(fills.is_empty());
    assert_eq!(bt.current_step, 2);
}
