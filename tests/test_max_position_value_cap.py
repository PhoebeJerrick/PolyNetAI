from __future__ import annotations

from datetime import datetime

import pytest

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.risk.limits import apply_risk_limits
from polynet_ai.strategy.spec import StrategyConfig


_BASE_FEATURES = dict(
    market_id="m",
    cycle_id="c",
    timestamp=datetime(2026, 1, 1, 0, 0, 0),
    price=0.5,
    cycle_elapsed_seconds=100.0,  # phase2
    is_last_minute=False,
    trend_bias=None,
    trend_strength=0.0,
    net_direction="平衡",
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
    strategy_trades=0,
    market_trades=10,
    up_last_price=0.5,
    down_last_price=0.5,
    up_market_vwap=0.5,
    down_market_vwap=0.5,
    up_market_n=3,
    down_market_n=3,
    up_market_high=0.55,
    up_market_low=0.45,
    down_market_high=0.55,
    down_market_low=0.45,
    tape_low=0.45,
    tape_high=0.55,
    up_signal_basis_price=0.5,
    down_signal_basis_price=0.5,
    reentry_armed=False,
    up_reentry_armed=False,
    down_reentry_armed=False,
)


def _cfg(max_position_value: float) -> StrategyConfig:
    # 把所有与“是否通过”有关的外部风控都关掉，专注测试 max_position_value cap。
    return StrategyConfig(
        raw={
            "position": {"max_position_value": max_position_value},
            "capital": {"max_cash_utilization": 1.0, "min_cash_buffer": 0.0},
            "exposure": {"max_abs_exposure_value": 1e9, "hedge_trigger_value": 1e9},
            "grid": {"grid_exit_fraction": 0.25},
            "order_sizing": {
                "buy": {"base_order_size": 5.0, "min_order_size": 1.0, "max_order_size": 1000.0},
                "sell": {"min_order_size": 1.0, "max_order_size": 1000.0, "allow_close_below_min_order_size": True},
            },
            "execution": {
                "fee_rate": 0.0,
                "slippage_bps": 0.0,
                "min_seconds_between_orders": 0.0,
                "execution": {},
                "min_same_outcome_price_move_ratio": 0.0,
                "max_same_direction_buy_fills_per_second": 0,
                "enforce_sell_min_order_size": False,
            },
            "dynamic_priority": {
                "phase_1_position_threshold": 0.65,
                "phase_1_boost": 15,
                "phase_2_position_threshold": 0.40,
                "phase_2_low_position_threshold": 0.25,
                "phase_2_boost": 15,
                "phase_3_position_threshold": 0.85,
                "phase_3_trend_boost": 25,
                "phase_3_grid_boost": 15,
            },
            "priorities": {
                "risk": 10,
                "last_minute": 20,
                "stop_loss": 30,
                "hedge": 40,
                "take_profit": 40,
                "opening": 52,
                "grid": 60,
                "mean_reversion": 70,
                "trend": 80,
            },
        }
    )


def _make_features(up_held: float, up_avg_price: float) -> FeatureSnapshot:
    v = dict(_BASE_FEATURES)
    v["up_held"] = up_held
    v["up_avg_price"] = up_avg_price
    v["net_direction"] = "Up" if up_held > 0 else "空仓"
    v["net_position"] = up_held
    v["net_position_value"] = up_held * up_avg_price - v.get("down_held", 0.0) * v.get("down_avg_price", 0.0)
    return FeatureSnapshot(**v)


def test_buy_shares_clipped_by_max_position_value() -> None:
    cfg = _cfg(100.0)
    # 当前 up_value = 90，剩余可加仓价值=10
    features = _make_features(up_held=90.0, up_avg_price=1.0)

    intent = OrderIntent(
        market_id="m",
        cycle_id="c",
        outcome="up",
        action="buy",
        shares=30.0,
        reference_price=1.0,
        category="grid",
        reason="test",
        priority=10,
        metadata={
            "account_cash": 200.0,
            "account_available_cash": 200.0,
            "strategy_trades": 0,
        },
    )
    decision = apply_risk_limits(features, intent, cfg)
    assert decision.accepted is True
    assert decision.intent is not None
    # shares 从 30 被裁剪到 10（remaining_value / reference_price）
    assert abs(decision.intent.shares - 10.0) < 1e-9
    assert decision.intent.metadata.get("max_position_value_limited") is True


def test_buy_blocked_when_max_position_value_filled() -> None:
    cfg = _cfg(100.0)
    # 当前 up_value = 100，剩余可加仓价值=0
    features = _make_features(up_held=100.0, up_avg_price=1.0)

    intent = OrderIntent(
        market_id="m",
        cycle_id="c",
        outcome="up",
        action="buy",
        shares=5.0,
        reference_price=1.0,
        category="grid",
        reason="test",
        priority=10,
        metadata={
            "account_cash": 200.0,
            "account_available_cash": 200.0,
            "strategy_trades": 0,
        },
    )
    decision = apply_risk_limits(features, intent, cfg)
    assert decision.accepted is False
    assert "超过最大仓位红线" in decision.reason

