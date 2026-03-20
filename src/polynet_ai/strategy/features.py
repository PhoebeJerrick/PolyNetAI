from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.domain.settlement import settlement_summary
from polynet_ai.domain.state_engine import StateEngine


def _deviation(price: float, avg_price: float) -> float:
    if avg_price <= 1e-10:
        return 0.0
    return (price - avg_price) / avg_price


def _price_percentile(price: float, low: float, high: float) -> float:
    if high - low <= 1e-10:
        return 0.5
    return max(0.0, min(1.0, (price - low) / (high - low)))


def _confidence_proxy(net_profit: float, price_move: float, volatility: float) -> float:
    signal = abs(net_profit) + abs(price_move)
    if volatility <= 1e-10:
        return 0.5
    return max(0.0, min(1.0, signal / (signal + volatility)))


def build_feature_snapshot(
    engine: StateEngine,
    cycle_seconds: int,
    last_minute_seconds: int,
) -> FeatureSnapshot:
    if engine.state is None or engine.state.last_event_timestamp is None:
        raise RuntimeError("state engine has no market context")

    state = engine.state
    summary = settlement_summary(state)
    timestamp = state.last_event_timestamp
    cycle_start = state.cycle_start or timestamp
    elapsed = max(0.0, (timestamp - cycle_start).total_seconds())
    volatility = state.price_range()
    opening = state.opening_price or state.last_price
    price_move = state.last_price - opening
    volatility_ratio = volatility / opening if opening > 1e-10 else 0.0
    trend_bias = None
    if state.consecutive_outcome_count >= 2:
        trend_bias = state.consecutive_outcome
    trend_strength = state.consecutive_outcome_count / max(1, state.market_trades + state.strategy_trades)
    net_position_value = state.net_position_value()
    market_regime = "trend" if trend_strength >= 0.35 or abs(price_move) > volatility * 0.5 else "range"
    return FeatureSnapshot(
        market_id=state.market_id,
        cycle_id=state.cycle_id,
        timestamp=timestamp,
        price=state.last_price,
        cycle_elapsed_seconds=elapsed,
        is_last_minute=elapsed >= max(0, cycle_seconds - last_minute_seconds),
        trend_bias=trend_bias,
        trend_strength=trend_strength,
        net_direction=state.net_direction(),
        net_position=state.net_position(),
        net_position_value=net_position_value,
        up_held=state.up_position.held,
        down_held=state.down_position.held,
        up_avg_price=state.up_position.avg_price,
        down_avg_price=state.down_position.avg_price,
        up_deviation=_deviation(state.last_price, state.up_position.avg_price),
        down_deviation=_deviation(state.last_price, state.down_position.avg_price),
        volatility=volatility,
        volatility_ratio=volatility_ratio,
        price_percentile=_price_percentile(state.last_price, state.low_price, state.high_price),
        realized_pnl=state.up_position.realized_pnl + state.down_position.realized_pnl,
        unrealized_up_pnl=summary.unrealized_up_pnl,
        unrealized_down_pnl=summary.unrealized_down_pnl,
        cycle_net_profit=summary.cycle_net_profit,
        opening_vs_last_move=price_move,
        confidence_proxy=_confidence_proxy(summary.cycle_net_profit, price_move, volatility),
        market_regime=market_regime,
    )
