from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.router import StrategyRouter
from polynet_ai.strategy.spec import StrategyConfig

_FEATURE_SNAPSHOT_DEFAULTS = dict(
    strategy_trades=0,
    market_trades=0,
    up_last_price=0.0,
    down_last_price=0.0,
    up_market_vwap=0.0,
    down_market_vwap=0.0,
    up_market_n=0,
    down_market_n=0,
    up_market_high=0.0,
    up_market_low=0.0,
    down_market_high=0.0,
    down_market_low=0.0,
    tape_low=0.0,
    tape_high=1.0,
)


def build_config() -> StrategyConfig:
    return StrategyConfig(
        raw={
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
            "order_sizing": {"base_order_size": 8.0, "min_order_size": 2.0, "max_order_size": 60.0, "volatility_order_scale": 20.0},
            "trend": {"min_trend_strength": 0.35, "trend_price_edge": 0.03, "trend_scale": 0.15},
            "exposure": {"hedge_trigger_value": 50.0},
            "grid": {"grid_low_percentile": 0.25, "grid_high_percentile": 0.75},
            "mean_reversion": {"up_buy_deviation": 0.10, "down_buy_deviation": 0.10, "mean_reversion_sell_up_deviation": 0.20, "mean_reversion_sell_down_deviation": 0.20, "deviation_scale": 45.0},
            "profit_taking": {"take_profit_up_deviation": 0.20, "take_profit_down_deviation": 0.20, "take_profit_fraction": 0.35},
            "stop_loss": {"stop_loss_cycle_loss": 20.0, "stop_loss_fraction": 0.50},
            "last_minute": {"last_minute_min_confidence": 0.85, "tail_profit_scale": 0.35, "tail_volatility_scale": 25.0, "max_tail_exposure": 40.0},
        }
    )


def test_last_minute_has_higher_priority_than_take_profit() -> None:
    router = StrategyRouter(build_config())
    features = FeatureSnapshot(
        market_id="BTC",
        cycle_id="cycle-x",
        timestamp=datetime(2026, 3, 20, 12, 4, 20),
        price=0.9,
        cycle_elapsed_seconds=260,
        is_last_minute=True,
        trend_bias="up",
        trend_strength=0.5,
        net_direction="Up",
        net_position=20.0,
        net_position_value=18.0,
        up_held=15.0,
        down_held=5.0,
        up_avg_price=0.6,
        down_avg_price=0.4,
        up_deviation=0.5,
        down_deviation=-0.2,
        volatility=0.3,
        volatility_ratio=0.3,
        price_percentile=0.95,
        realized_pnl=12.0,
        unrealized_up_pnl=10.0,
        unrealized_down_pnl=-3.0,
        cycle_net_profit=9.0,
        opening_vs_last_move=0.2,
        confidence_proxy=0.9,
        market_regime="trend",
        **_FEATURE_SNAPSHOT_DEFAULTS,
    )
    decision = router.route(features)
    assert decision.selected is not None
    assert decision.selected.category == "last_minute"
    assert decision.selected.action == "sell"
    assert decision.selected.outcome == "down"


def test_stop_loss_beats_entry_signal() -> None:
    router = StrategyRouter(build_config())
    features = FeatureSnapshot(
        market_id="ETH",
        cycle_id="cycle-y",
        timestamp=datetime(2026, 3, 20, 12, 2, 0),
        price=0.3,
        cycle_elapsed_seconds=120,
        is_last_minute=False,
        trend_bias="up",
        trend_strength=0.6,
        net_direction="Up",
        net_position=12.0,
        net_position_value=80.0,
        up_held=12.0,
        down_held=3.0,
        up_avg_price=0.55,
        down_avg_price=0.4,
        up_deviation=-0.45,
        down_deviation=-0.25,
        volatility=0.4,
        volatility_ratio=0.7,
        price_percentile=0.1,
        realized_pnl=-5.0,
        unrealized_up_pnl=-12.0,
        unrealized_down_pnl=1.0,
        cycle_net_profit=-25.0,
        opening_vs_last_move=-0.2,
        confidence_proxy=0.4,
        market_regime="range",
        **_FEATURE_SNAPSHOT_DEFAULTS,
    )
    decision = router.route(features)
    assert decision.selected is not None
    assert decision.selected.category == "stop_loss"
    assert decision.selected.action == "sell"
