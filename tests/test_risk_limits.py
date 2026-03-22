from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.risk.limits import apply_risk_limits
from polynet_ai.strategy.spec import StrategyConfig


def build_config() -> StrategyConfig:
    return StrategyConfig(
        raw={
            "order_sizing": {"min_order_size": 2.0, "max_order_size": 60.0},
            "exposure": {"max_abs_exposure": 200.0, "max_strategy_trades_per_cycle": 12},
            "execution": {"fee_rate": 0.002, "slippage_bps": 10},
            "capital": {"max_cash_utilization": 0.95, "min_cash_buffer": 25.0},
        }
    )


def build_features() -> FeatureSnapshot:
    return FeatureSnapshot(
        market_id="BTC",
        cycle_id="cycle-a",
        timestamp=datetime(2026, 3, 20, 12, 0, 0),
        price=0.5,
        cycle_elapsed_seconds=30.0,
        is_last_minute=False,
        trend_bias="up",
        trend_strength=0.4,
        net_direction="空仓",
        net_position=0.0,
        net_position_value=0.0,
        up_held=0.0,
        down_held=0.0,
        up_avg_price=0.0,
        down_avg_price=0.0,
        up_deviation=0.0,
        down_deviation=0.0,
        volatility=0.1,
        volatility_ratio=0.1,
        price_percentile=0.5,
        realized_pnl=0.0,
        unrealized_up_pnl=0.0,
        unrealized_down_pnl=0.0,
        cycle_net_profit=0.0,
        opening_vs_last_move=0.0,
        confidence_proxy=0.5,
        market_regime="range",
    )


def test_apply_risk_limits_clips_buy_by_available_cash() -> None:
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=60.0,
        reference_price=0.5,
        category="grid",
        reason="test",
        priority=10,
        metadata={"account_cash": 20.0},
    )

    decision = apply_risk_limits(build_features(), intent, build_config())

    assert not decision.accepted
    assert decision.reason == "可用现金不足，无法满足最小下单量"


def test_apply_risk_limits_reduces_buy_size_to_affordable_cash() -> None:
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=60.0,
        reference_price=0.5,
        category="grid",
        reason="test",
        priority=10,
        metadata={"account_cash": 40.0},
    )

    decision = apply_risk_limits(build_features(), intent, build_config())

    assert decision.accepted
    assert decision.intent is not None
    assert 2.0 <= decision.intent.shares < 60.0
    assert decision.intent.metadata["cash_limited"] is True


def test_apply_risk_limits_enforces_min_order_size_for_buys() -> None:
    config = StrategyConfig(
        raw={
            "order_sizing": {"min_order_size": 5.0, "max_order_size": 60.0},
            "exposure": {"max_abs_exposure": 200.0, "max_strategy_trades_per_cycle": 12},
            "execution": {"fee_rate": 0.002, "slippage_bps": 10},
            "capital": {"max_cash_utilization": 0.95, "min_cash_buffer": 0.0},
        }
    )
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=3.0,
        reference_price=0.5,
        category="grid",
        reason="test",
        priority=10,
        metadata={"account_cash": 100.0},
    )

    decision = apply_risk_limits(build_features(), intent, config)

    assert decision.accepted
    assert decision.intent is not None
    assert decision.intent.shares == 5.0


def test_apply_risk_limits_allows_small_sells_below_buy_minimum() -> None:
    config = StrategyConfig(
        raw={
            "order_sizing": {"min_order_size": 5.0, "max_order_size": 60.0},
            "exposure": {"max_abs_exposure": 200.0, "max_strategy_trades_per_cycle": 12},
            "execution": {"fee_rate": 0.002, "slippage_bps": 10},
            "capital": {"max_cash_utilization": 0.95, "min_cash_buffer": 0.0},
        }
    )
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="sell",
        shares=2.5,
        reference_price=0.5,
        category="take_profit",
        reason="test",
        priority=10,
        metadata={"account_cash": 100.0},
    )

    features = build_features()
    features.up_held = 2.5
    decision = apply_risk_limits(features, intent, config)

    assert decision.accepted
    assert decision.intent is not None
    assert decision.intent.shares == 2.5


def test_apply_risk_limits_uses_available_cash_over_total_cash() -> None:
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=20.0,
        reference_price=0.5,
        category="grid",
        reason="test",
        priority=10,
        metadata={"account_cash": 100.0, "account_available_cash": 10.0},
    )

    decision = apply_risk_limits(build_features(), intent, build_config())

    assert not decision.accepted
    assert decision.reason == "可用现金不足，无法满足最小下单量"


def test_apply_risk_limits_blocks_sell_when_pending_sell_already_reserves_position() -> None:
    features = build_features()
    features.up_held = 3.0
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="sell",
        shares=2.0,
        reference_price=0.5,
        category="take_profit",
        reason="test",
        priority=10,
        metadata={"pending_up_sell_shares": 3.0},
    )

    decision = apply_risk_limits(features, intent, build_config())

    assert not decision.accepted
    assert decision.reason == "可卖仓位不足，存在待确认卖单"


def test_apply_risk_limits_clips_sell_to_unreserved_position() -> None:
    features = build_features()
    features.up_held = 5.0
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="sell",
        shares=4.0,
        reference_price=0.5,
        category="take_profit",
        reason="test",
        priority=10,
        metadata={"pending_up_sell_shares": 2.0},
    )

    decision = apply_risk_limits(features, intent, build_config())

    assert decision.accepted
    assert decision.intent is not None
    assert decision.intent.shares == 3.0
    assert decision.intent.metadata["pending_sell_limited"] is True
