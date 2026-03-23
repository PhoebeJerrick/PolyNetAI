from __future__ import annotations

from datetime import datetime, timedelta

from polynet_ai.domain.models import TradeEvent
from polynet_ai.domain.state_engine import StateEngine
from polynet_ai.strategy.features import build_feature_snapshot
from polynet_ai.strategy.router import StrategyRouter
from polynet_ai.strategy.spec import StrategyConfig


def _router_config() -> StrategyConfig:
    return StrategyConfig(
        raw={
            "cycle": {"cycle_seconds": 300, "last_minute_seconds": 30},
            "opening_entry": {
                "enabled": True,
                "window_seconds": 30.0,
                "vwap_epsilon": 0.01,
                "range_low_fraction": 0.35,
                "min_range_width": 0.02,
                "infer_missing_with_binary_complement": True,
            },
            "order_sizing": {
                "base_order_size": 5.0,
                "min_order_size": 2.0,
                "max_order_size": 60.0,
                "volatility_order_scale": 5.0,
            },
            "trend": {"min_trend_strength": 0.5, "trend_price_edge": 0.03, "trend_scale": 0.05},
            "exposure": {"hedge_trigger_value": 50.0, "max_grid_net_position": 20.0},
            "grid": {"grid_low_percentile": 0.25, "grid_high_percentile": 0.75},
            "mean_reversion": {
                "up_buy_deviation": 0.10,
                "down_buy_deviation": 0.10,
                "mean_reversion_sell_up_deviation": 0.20,
                "mean_reversion_sell_down_deviation": 0.20,
                "deviation_scale": 45.0,
            },
            "profit_taking": {
                "take_profit_up_deviation": 0.20,
                "take_profit_down_deviation": 0.20,
                "take_profit_fraction": 0.35,
            },
            "stop_loss": {"stop_loss_cycle_loss": 20.0, "stop_loss_fraction": 0.50},
            "last_minute": {
                "last_minute_min_confidence": 0.85,
                "tail_profit_scale": 0.35,
                "tail_volatility_scale": 25.0,
                "max_tail_exposure": 40.0,
            },
            "priorities": {
                "risk": 10,
                "last_minute": 20,
                "stop_loss": 30,
                "hedge": 40,
                "take_profit": 50,
                "opening": 52,
                "grid": 60,
                "mean_reversion": 70,
                "trend": 80,
            },
        }
    )


def test_opening_entry_buys_lower_priced_outcome() -> None:
    engine = StateEngine()
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    engine.apply_market_trade(
        TradeEvent(market_id="m", cycle_id="c", timestamp=t0, price=0.55, shares=10.0, outcome="up")
    )
    engine.apply_market_trade(
        TradeEvent(market_id="m", cycle_id="c", timestamp=t0 + timedelta(seconds=2), price=0.42, shares=10.0, outcome="down")
    )
    features = build_feature_snapshot(engine, cycle_seconds=300, last_minute_seconds=30)
    decision = StrategyRouter(_router_config()).route(features, strategy_trades=0)
    assert decision.selected is not None
    assert decision.selected.category == "opening"
    assert decision.selected.outcome == "down"
    assert decision.selected.action == "buy"


def test_opening_entry_defers_until_weak_side_has_market_prints() -> None:
    engine = StateEngine()
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    engine.apply_market_trade(
        TradeEvent(market_id="m", cycle_id="c", timestamp=t0, price=0.62, shares=10.0, outcome="up")
    )
    features = build_feature_snapshot(engine, cycle_seconds=300, last_minute_seconds=30)
    decision = StrategyRouter(_router_config()).route(features, strategy_trades=0)
    assert decision.selected is None

    engine.apply_market_trade(
        TradeEvent(market_id="m", cycle_id="c", timestamp=t0 + timedelta(seconds=3), price=0.38, shares=10.0, outcome="down")
    )
    features = build_feature_snapshot(engine, cycle_seconds=300, last_minute_seconds=30)
    decision = StrategyRouter(_router_config()).route(features, strategy_trades=0)
    assert decision.selected is not None
    assert decision.selected.category == "opening"
    assert decision.selected.outcome == "down"


def test_opening_entry_not_triggered_after_window() -> None:
    engine = StateEngine()
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    engine.apply_market_trade(
        TradeEvent(market_id="m", cycle_id="c", timestamp=t0, price=0.40, shares=10.0, outcome="up")
    )
    engine.apply_market_trade(
        TradeEvent(market_id="m", cycle_id="c", timestamp=t0, price=0.38, shares=10.0, outcome="down")
    )
    late = t0 + timedelta(seconds=45)
    engine.apply_market_trade(
        TradeEvent(market_id="m", cycle_id="c", timestamp=late, price=0.39, shares=10.0, outcome="down")
    )
    features = build_feature_snapshot(engine, cycle_seconds=300, last_minute_seconds=30)
    assert features.cycle_elapsed_seconds > 30.0
    decision = StrategyRouter(_router_config()).route(features, strategy_trades=0)
    assert decision.selected is None or decision.selected.category != "opening"
