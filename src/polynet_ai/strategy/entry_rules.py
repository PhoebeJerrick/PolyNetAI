from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.spec import StrategyConfig


def _base_size(config: StrategyConfig, features: FeatureSnapshot) -> float:
    return float(config.get("order_sizing.base_order_size", 5.0)) + (
        features.volatility_ratio * float(config.get("order_sizing.volatility_order_scale", 10.0))
    )


def trend_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if features.is_last_minute or not features.trend_bias:
        return []
    if features.trend_strength < float(config.get("trend.min_trend_strength", 0.35)):
        return []

    price_edge = float(config.get("trend.trend_price_edge", 0.03))
    deviation = features.up_deviation if features.trend_bias == "up" else features.down_deviation
    if deviation < price_edge:
        return []

    size = _base_size(config, features) + abs(features.net_position) * float(config.get("trend.trend_scale", 0.15))
    return [
        OrderIntent(
            market_id=features.market_id,
            cycle_id=features.cycle_id,
            outcome=features.trend_bias,
            action="buy",
            shares=size,
            reference_price=features.price,
            category="trend",
            reason="趋势确认后顺势加仓",
            priority=int(config.priorities.get("trend", 80)),
        )
    ]


def hedge_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    trigger = float(config.get("exposure.hedge_trigger_value", 50.0))
    exposure = abs(features.net_position_value)
    if exposure < trigger or features.net_direction in {"空仓", "平衡"}:
        return []

    opposite = "down" if features.net_direction == "Up" else "up"
    excess = max(0.0, exposure - trigger)
    size = _base_size(config, features) + excess * float(config.get("exposure.hedge_scale", 0.15))
    return [
        OrderIntent(
            market_id=features.market_id,
            cycle_id=features.cycle_id,
            outcome=opposite,
            action="buy",
            shares=size,
            reference_price=features.price,
            category="hedge",
            reason="净敞口过大，执行反向对冲买入",
            priority=int(config.priorities.get("hedge", 40)),
        )
    ]


def grid_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if features.is_last_minute or features.market_regime != "range":
        return []
    if abs(features.net_position) > float(config.get("exposure.max_grid_net_position", 20.0)):
        return []

    low = float(config.get("grid.grid_low_percentile", 0.25))
    high = float(config.get("grid.grid_high_percentile", 0.75))
    size = _base_size(config, features)
    intents: list[OrderIntent] = []
    if features.price_percentile <= low:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="buy",
                shares=size,
                reference_price=features.price,
                category="grid",
                reason="震荡区间低位买入 Up",
                priority=int(config.priorities.get("grid", 60)),
            )
        )
    if features.price_percentile >= high:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="buy",
                shares=size,
                reference_price=features.price,
                category="grid",
                reason="震荡区间高位买入 Down",
                priority=int(config.priorities.get("grid", 60)),
            )
        )
    return intents


def mean_reversion_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if features.is_last_minute:
        return []
    up_threshold = float(config.get("mean_reversion.up_buy_deviation", 0.10))
    down_threshold = float(config.get("mean_reversion.down_buy_deviation", 0.10))
    deviation_scale = float(config.get("mean_reversion.deviation_scale", 45.0))
    intents: list[OrderIntent] = []
    if features.up_deviation >= up_threshold:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="buy",
                shares=_base_size(config, features) + features.up_deviation * deviation_scale,
                reference_price=features.price,
                category="mean_reversion",
                reason="Up 价格显著高于均价，执行追高买入",
                priority=int(config.priorities.get("mean_reversion", 70)),
            )
        )
    if features.down_deviation <= -down_threshold:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="buy",
                shares=_base_size(config, features) + abs(features.down_deviation) * deviation_scale,
                reference_price=features.price,
                category="mean_reversion",
                reason="Down 价格显著低于均价，执行均值回归买入",
                priority=int(config.priorities.get("mean_reversion", 70)),
            )
        )
    return intents


def build_entry_candidates(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    candidates: list[OrderIntent] = []
    candidates.extend(hedge_entries(features, config))
    candidates.extend(grid_entries(features, config))
    candidates.extend(mean_reversion_entries(features, config))
    candidates.extend(trend_entries(features, config))
    return candidates
