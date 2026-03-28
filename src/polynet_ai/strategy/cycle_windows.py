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


def calculate_position_percentage(features: FeatureSnapshot, config: StrategyConfig) -> float:
    """
    计算当前仓位占目标仓位的百分比（A+C联合方案）

    Returns:
        仓位百分比（0.0-1.0+），值可能超过1.0表示超配
    """
    up_value = features.up_held * features.up_avg_price
    down_value = features.down_held * features.down_avg_price
    total = up_value + down_value
    max_value = float(config.get("position.max_position_value", 85.0))
    if max_value <= 0:
        return 0.0
    return total / max_value


def determine_phase(cycle_elapsed_seconds: float) -> int:
    """
    根据周期已过时间判断当前阶段（A+C联合方案）

    Args:
        cycle_elapsed_seconds: 周期已过时间（秒）

    Returns:
        阶段编号：1, 2, 3, 4
        - 第一阶段（0-70s）：趋势低吸加仓，目标65%仓位
        - 第二阶段（70-160s）：网格减仓为主
        - 第三阶段（160-240s）：顺势加仓+对冲
        - 第四阶段（240-290s）：确认方向加仓
    """
    if cycle_elapsed_seconds <= 70:
        return 1
    elif cycle_elapsed_seconds <= 160:
        return 2
    elif cycle_elapsed_seconds <= 240:
        return 3
    else:
        return 4
