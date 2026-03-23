from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.entry_rules import trend_entries
from polynet_ai.strategy.price_reference import outcome_reference_price
from polynet_ai.strategy.spec import StrategyConfig


def _feature_snapshot(**overrides: object) -> FeatureSnapshot:
    base: dict[str, object] = {
        "market_id": "m",
        "cycle_id": "c1",
        "timestamp": datetime(2026, 1, 1, 12, 0, 0),
        "price": 0.75,
        "cycle_elapsed_seconds": 120.0,
        "is_last_minute": False,
        "trend_bias": "up",
        "trend_strength": 1.0,
        "net_direction": "空仓",
        "net_position": 0.0,
        "net_position_value": 0.0,
        "up_held": 0.0,
        "down_held": 0.0,
        "up_avg_price": 0.5,
        "down_avg_price": 0.5,
        "up_deviation": 0.0,
        "down_deviation": 0.0,
        "volatility": 0.2,
        "volatility_ratio": 0.01,
        "price_percentile": 0.5,
        "realized_pnl": 0.0,
        "unrealized_up_pnl": 0.0,
        "unrealized_down_pnl": 0.0,
        "cycle_net_profit": 0.0,
        "opening_vs_last_move": 0.0,
        "confidence_proxy": 0.5,
        "market_regime": "trend",
        "strategy_trades": 1,
        "market_trades": 10,
        "up_last_price": 0.8,
        "down_last_price": 0.2,
        "up_market_vwap": 0.0,
        "down_market_vwap": 0.0,
        "up_market_n": 1,
        "down_market_n": 1,
        "up_market_high": 0.8,
        "up_market_low": 0.8,
        "down_market_high": 0.2,
        "down_market_low": 0.2,
        "tape_low": 0.0,
        "tape_high": 1.0,
    }
    base.update(overrides)
    return FeatureSnapshot(**base)


def test_outcome_reference_price_uses_outcome_specific_last_price() -> None:
    f = _feature_snapshot(price=0.8, up_last_price=0.8, down_last_price=0.2)
    assert outcome_reference_price(f, "up", infer_missing_with_binary_complement=True) == 0.8
    assert outcome_reference_price(f, "down", infer_missing_with_binary_complement=True) == 0.2


def test_outcome_reference_price_infers_missing_with_binary_complement() -> None:
    # up missing -> infer from down
    f = _feature_snapshot(price=0.3, up_last_price=0.0, down_last_price=0.2)
    # complement(0.2)=0.8 (clamped)
    assert outcome_reference_price(f, "up", infer_missing_with_binary_complement=True) == 0.8


def test_trend_entries_reference_price_follows_trend_bias() -> None:
    cfg = StrategyConfig(
        raw={
            "order_sizing": {"base_order_size": 5.0, "volatility_order_scale": 0.0},
            "trend": {"min_trend_strength": 0.0, "trend_price_edge": 0.01, "trend_scale": 0.0},
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "priorities": {"trend": 10},
        }
    )
    f = _feature_snapshot(
        trend_bias="down",
        trend_strength=1.0,
        down_deviation=0.1,
        up_deviation=0.0,
        price=0.8,  # should NOT be used for down
        up_last_price=0.8,
        down_last_price=0.2,
        up_held=0.0,
        down_held=0.0,
        net_position=0.0,
        net_position_value=0.0,
    )
    intents = trend_entries(f, cfg)
    assert len(intents) == 1
    assert intents[0].outcome == "down"
    assert intents[0].reference_price == 0.2

