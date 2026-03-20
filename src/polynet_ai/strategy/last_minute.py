from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.spec import StrategyConfig


def build_last_minute_candidate(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if not features.is_last_minute:
        return []

    priority = int(config.priorities.get("last_minute", 20))
    intents: list[OrderIntent] = []
    if features.unrealized_up_pnl < 0 and features.up_held > 0:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="sell",
                shares=features.up_held,
                reference_price=features.price,
                category="last_minute",
                reason="最后一分钟强制平掉亏损 Up 方向",
                priority=priority,
            )
        )
    if features.unrealized_down_pnl < 0 and features.down_held > 0:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="sell",
                shares=features.down_held,
                reference_price=features.price,
                category="last_minute",
                reason="最后一分钟强制平掉亏损 Down 方向",
                priority=priority,
            )
        )
    if intents:
        return intents

    min_conf = float(config.get("last_minute.last_minute_min_confidence", 0.85))
    if features.confidence_proxy < min_conf:
        return []

    if features.cycle_net_profit >= 0 and features.net_direction in {"Up", "Down"}:
        target_outcome = "up" if features.net_direction == "Up" else "down"
    elif features.unrealized_up_pnl < features.unrealized_down_pnl:
        target_outcome = "down"
    else:
        target_outcome = "up"

    target_net = min(
        float(config.get("last_minute.max_tail_exposure", 40.0)),
        float(config.get("last_minute.tail_profit_scale", 0.35)) * abs(features.cycle_net_profit)
        + float(config.get("last_minute.tail_volatility_scale", 25.0)) * features.volatility,
    )
    held = features.up_held if target_outcome == "up" else features.down_held
    if held >= target_net:
        return []

    return [
        OrderIntent(
            market_id=features.market_id,
            cycle_id=features.cycle_id,
            outcome=target_outcome,
            action="buy",
            shares=max(0.0, target_net - held),
            reference_price=features.price,
            category="last_minute",
            reason="最后一分钟按盈利方向和波动率调整留仓",
            priority=priority,
        )
    ]
