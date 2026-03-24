from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.spec import StrategyConfig


def cycle_seconds_remaining(features: FeatureSnapshot, config: StrategyConfig) -> float:
    cycle_sec = float(config.get("cycle.cycle_seconds", 300))
    return max(0.0, cycle_sec - float(features.cycle_elapsed_seconds))


def rule_disabled_in_cycle_tail(features: FeatureSnapshot, config: StrategyConfig, section: str) -> bool:
    """当本周期剩余时间 <= 配置秒数时，该小节对应策略规则不使能（买卖均不触发）。"""
    raw = config.get(f"{section}.disable_within_seconds_before_end", None)
    if raw is None:
        return False
    try:
        window = float(raw)
    except (TypeError, ValueError):
        return False
    if window <= 0:
        return False
    return cycle_seconds_remaining(features, config) <= window
