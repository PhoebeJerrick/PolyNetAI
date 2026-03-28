from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.entry_rules import hedge_entries
from polynet_ai.strategy.exit_rules import hedge_exits
from polynet_ai.strategy.last_minute import build_last_minute_candidate
from polynet_ai.strategy.spec import StrategyConfig

_SNAP_BASE = dict(
    strategy_trades=0,
    market_trades=0,
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


def test_hedge_entries_skipped_in_last_minute() -> None:
    cfg = StrategyConfig(
        raw={
            "exposure": {"hedge_trigger_value": 1.0, "hedge_scale": 0.5},
            "order_sizing": {"base_order_size": 5.0, "min_order_size": 1.0, "max_order_size": 50.0, "volatility_order_scale": 1.0},
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "priorities": {"hedge": 30},
        }
    )
    features = FeatureSnapshot(
        market_id="m",
        cycle_id="c",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        price=0.6,
        cycle_elapsed_seconds=280,
        is_last_minute=True,
        trend_bias=None,
        trend_strength=0.0,
        net_direction="Up",
        net_position=10.0,
        net_position_value=25.0,
        up_held=10.0,
        down_held=0.0,
        up_avg_price=0.5,
        down_avg_price=0.0,
        up_deviation=0.0,
        down_deviation=0.0,
        volatility=0.1,
        volatility_ratio=0.1,
        price_percentile=0.5,
        realized_pnl=0.0,
        unrealized_up_pnl=1.0,
        unrealized_down_pnl=0.0,
        cycle_net_profit=1.0,
        opening_vs_last_move=0.0,
        confidence_proxy=0.9,
        market_regime="range",
        up_last_price=0.6,
        down_last_price=0.4,
        **_SNAP_BASE,
    )
    assert hedge_entries(features, cfg) == []


def test_hedge_exits_skipped_in_last_minute() -> None:
    cfg = StrategyConfig(
        raw={
            "exposure": {"hedge_trigger_value": 1.0},
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "priorities": {"hedge": 30},
        }
    )
    features = FeatureSnapshot(
        market_id="m",
        cycle_id="c",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        price=0.7,
        cycle_elapsed_seconds=290,
        is_last_minute=True,
        trend_bias=None,
        trend_strength=0.0,
        net_direction="Up",
        net_position=5.0,
        net_position_value=30.0,
        up_held=10.0,
        down_held=0.0,
        up_avg_price=0.5,
        down_avg_price=0.0,
        up_deviation=0.0,
        down_deviation=0.0,
        volatility=0.1,
        volatility_ratio=0.1,
        price_percentile=0.5,
        realized_pnl=0.0,
        unrealized_up_pnl=2.0,
        unrealized_down_pnl=0.0,
        cycle_net_profit=5.0,
        opening_vs_last_move=0.0,
        confidence_proxy=0.9,
        market_regime="trend",
        up_last_price=0.7,
        down_last_price=0.3,
        **_SNAP_BASE,
    )
    assert hedge_exits(features, cfg) == []


def test_last_minute_preferred_leg_ratio_buys_favored_side() -> None:
    cfg = StrategyConfig(
        raw={
            "last_minute": {
                "last_minute_min_confidence": 0.5,
                "tail_profit_scale": 0.0,
                "tail_volatility_scale": 0.0,
                "max_tail_exposure": 40.0,
                "preferred_leg_min_ratio": 1.3,
            },
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "priorities": {"last_minute": 20},
        }
    )
    features = FeatureSnapshot(
        market_id="m",
        cycle_id="c",
        timestamp=datetime(2026, 1, 1, 12, 0, 0),
        price=0.55,
        cycle_elapsed_seconds=280,
        is_last_minute=True,
        trend_bias="up",
        trend_strength=0.8,
        net_direction="Up",
        net_position=-10.0,
        net_position_value=5.0,
        up_held=5.0,
        down_held=20.0,
        up_avg_price=0.9,
        down_avg_price=0.1,
        up_deviation=0.0,
        down_deviation=0.0,
        volatility=0.05,
        volatility_ratio=0.05,
        price_percentile=0.5,
        realized_pnl=0.0,
        unrealized_up_pnl=3.0,
        unrealized_down_pnl=1.0,
        cycle_net_profit=2.0,
        opening_vs_last_move=0.0,
        confidence_proxy=0.9,
        market_regime="range",
        up_last_price=0.55,
        down_last_price=0.45,
        **_SNAP_BASE,
    )
    intents = build_last_minute_candidate(features, cfg)
    assert len(intents) == 1
    assert intents[0].action == "buy"
    assert intents[0].outcome == "up"
    assert abs(intents[0].shares - 21.0) < 1e-6
