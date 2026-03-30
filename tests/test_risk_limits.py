from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.risk.limits import apply_risk_limits
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
            "order_sizing": {"min_order_size": 2.0, "max_order_size": 60.0},
            "exposure": {"max_abs_exposure_value": 200.0, "max_strategy_trades_per_cycle": 12},
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
        **_FEATURE_SNAPSHOT_DEFAULTS,
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
            "exposure": {"max_abs_exposure_value": 200.0, "max_strategy_trades_per_cycle": 12},
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
            "exposure": {"max_abs_exposure_value": 200.0, "max_strategy_trades_per_cycle": 12},
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


def test_apply_risk_limits_uses_market_min_order_size_for_buy() -> None:
    config = StrategyConfig(
        raw={
            "order_sizing": {"min_order_size": 2.0, "max_order_size": 60.0},
            "exposure": {"max_abs_exposure_value": 200.0, "max_strategy_trades_per_cycle": 12},
            "execution": {
                "fee_rate": 0.002,
                "slippage_bps": 10,
                "market_limits": {"use_orderbook_min_order_size": True},
            },
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
        metadata={"account_cash": 100.0, "market_min_order_size": 5.0},
    )

    decision = apply_risk_limits(build_features(), intent, config)

    assert decision.accepted
    assert decision.intent is not None
    assert decision.intent.shares == 5.0


def test_apply_risk_limits_allows_forced_close_below_sell_min() -> None:
    config = StrategyConfig(
        raw={
            "order_sizing": {
                "min_order_size": 2.0,
                "max_order_size": 60.0,
                "sell": {"min_order_size": 5.0, "max_order_size": 60.0, "allow_close_below_min_order_size": True},
            },
            "exposure": {"max_abs_exposure_value": 200.0, "max_strategy_trades_per_cycle": 12},
            "execution": {"fee_rate": 0.002, "slippage_bps": 10},
            "capital": {"max_cash_utilization": 0.95, "min_cash_buffer": 0.0},
        }
    )
    features = build_features()
    features.up_held = 3.0
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="sell",
        shares=2.0,
        reference_price=0.5,
        category="stop_loss",
        reason="test",
        priority=10,
        metadata={},
    )

    decision = apply_risk_limits(features, intent, config)

    assert decision.accepted
    assert decision.intent is not None
    assert decision.intent.shares == 3.0
    assert decision.intent.metadata["sell_below_min_forced_close"] is True


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


def test_apply_risk_limits_blocks_when_order_interval_too_short() -> None:
    features = build_features()
    features.timestamp = datetime(2026, 3, 20, 12, 0, 3)
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=10.0,
        reference_price=0.5,
        category="grid",
        reason="test",
        priority=10,
        metadata={
            "account_cash": 100.0,
            "last_strategy_fill_at_up": datetime(2026, 3, 20, 12, 0, 2),
        },
    )
    decision = apply_risk_limits(features, intent, build_config())
    assert not decision.accepted
    assert "间隔" in decision.reason


def test_apply_risk_limits_blocks_when_same_outcome_price_move_too_small() -> None:
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=10.0,
        reference_price=0.502,  # 与上次成交价 0.50 相比移动 0.4%，小于 0.5% 阈値
        category="grid",
        reason="test",
        priority=10,
        metadata={"account_cash": 100.0, "last_strategy_fill_price_up": 0.50},
    )
    decision = apply_risk_limits(build_features(), intent, build_config())
    assert not decision.accepted
    assert "波动" in decision.reason


def test_apply_risk_limits_allows_when_same_outcome_price_move_large_enough() -> None:
    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=10.0,
        reference_price=0.52,
        category="grid",
        reason="test",
        priority=10,
        metadata={"account_cash": 100.0, "last_strategy_fill_price_up": 0.50},
    )
    decision = apply_risk_limits(build_features(), intent, build_config())
    assert decision.accepted


def _build_phase4_config() -> StrategyConfig:
    """Phase 4 专用配置：standard limit=40, phase 4 limit=60."""
    return StrategyConfig(
        raw={
            "order_sizing": {"min_order_size": 2.0, "max_order_size": 60.0},
            "exposure": {
                "max_abs_exposure_value": 40.0,
                "phase_4_max_abs_exposure_value": 60.0,
                "max_strategy_trades_per_cycle": 12,
            },
            "execution": {"fee_rate": 0.002, "slippage_bps": 10},
            "capital": {"max_cash_utilization": 0.95, "min_cash_buffer": 0.0},
        }
    )


def test_phase4_allows_buy_above_standard_limit_but_below_phase4_limit() -> None:
    """Phase 4 时净敞口超过标准上限（40）但低于 Phase 4 上限（60），应允许加仓。"""
    features = build_features()
    features.cycle_elapsed_seconds = 250.0   # Phase 4（>240s）
    features.net_position_value = 45.0       # 超过标准 40，但低于 Phase 4 上限 60

    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=10.0,
        reference_price=0.5,   # projected 净敞口 = 45 + 0.5*10 = 50 < 60
        category="grid",
        reason="phase4-加仓",
        priority=10,
        metadata={"account_cash": 200.0},
    )

    decision = apply_risk_limits(features, intent, _build_phase4_config())

    assert decision.accepted, f"Phase 4 应允许加仓，但被拒绝：{decision.reason}"


def test_phase4_rejects_buy_when_projected_exposure_exceeds_phase4_limit() -> None:
    """Phase 4 时下单后预期净敞口超过 Phase 4 上限（60），应拒绝。"""
    features = build_features()
    features.cycle_elapsed_seconds = 250.0   # Phase 4
    features.net_position_value = 55.0       # 低于 Phase 4 上限 60

    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=15.0,
        reference_price=0.5,   # projected 净敞口 = 55 + 0.5*15 = 62.5 > 60
        category="grid",
        reason="phase4-超额买入",
        priority=10,
        metadata={"account_cash": 200.0},
    )

    decision = apply_risk_limits(features, intent, _build_phase4_config())

    assert not decision.accepted
    assert "净敞口" in decision.reason


def test_non_phase4_still_enforces_standard_exposure_limit() -> None:
    """非 Phase 4 阶段，净敞口超过标准上限（40）时应被拒绝，Phase 4 扩大配置无效。"""
    features = build_features()
    features.cycle_elapsed_seconds = 30.0    # Phase 1
    features.net_position_value = 45.0       # 超过标准上限 40

    intent = OrderIntent(
        market_id="BTC",
        cycle_id="cycle-a",
        outcome="up",
        action="buy",
        shares=5.0,
        reference_price=0.5,
        category="grid",
        reason="phase1-超标",
        priority=10,
        metadata={"account_cash": 200.0},
    )

    decision = apply_risk_limits(features, intent, _build_phase4_config())

    assert not decision.accepted
    assert "净敞口" in decision.reason
