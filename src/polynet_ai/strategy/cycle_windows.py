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


_DEFAULT_PHASE_ENDS: tuple[float, float, float] = (70.0, 160.0, 240.0)


def phase_end_seconds_from_config(config: StrategyConfig) -> tuple[float, float, float]:
    """读取 ``cycle.phase_end_seconds_{1,2,3}``；若未配置或非法（非严格递增正数）则回退默认值。"""
    try:
        e1 = float(config.get("cycle.phase_end_seconds_1", _DEFAULT_PHASE_ENDS[0]))
        e2 = float(config.get("cycle.phase_end_seconds_2", _DEFAULT_PHASE_ENDS[1]))
        e3 = float(config.get("cycle.phase_end_seconds_3", _DEFAULT_PHASE_ENDS[2]))
    except (TypeError, ValueError):
        return _DEFAULT_PHASE_ENDS
    if not (0.0 < e1 < e2 < e3):
        return _DEFAULT_PHASE_ENDS
    return (e1, e2, e3)


def determine_phase(cycle_elapsed_seconds: float, config: StrategyConfig) -> int:
    """
    根据周期已过时间判断当前阶段（A+C联合方案）

    Args:
        cycle_elapsed_seconds: 周期已过时间（秒）
        config: 策略配置（``cycle.phase_end_seconds_*`` 定义各阶段上界，含等号）

    Returns:
        阶段编号：1, 2, 3, 4
        - 第一阶段：(0, phase_end_1]
        - 第二阶段：(phase_end_1, phase_end_2]
        - 第三阶段：(phase_end_2, phase_end_3]
        - 第四阶段：phase_end_3 之后至周期结束
    """
    e1, e2, e3 = phase_end_seconds_from_config(config)
    if cycle_elapsed_seconds <= e1:
        return 1
    if cycle_elapsed_seconds <= e2:
        return 2
    if cycle_elapsed_seconds <= e3:
        return 3
    return 4


def phase_elapsed_seconds(cycle_elapsed_seconds: float, config: StrategyConfig) -> float:
    """
    当前阶段内已过秒数：``cycle_elapsed_seconds - phase_start``，与 ``determine_phase`` 边界一致。

    - 阶段1：起点 0
    - 阶段2：起点 ``phase_end_seconds_1``
    - 阶段3：起点 ``phase_end_seconds_2``
    - 阶段4：起点 ``phase_end_seconds_3``
    """
    e1, e2, e3 = phase_end_seconds_from_config(config)
    t = max(0.0, float(cycle_elapsed_seconds))
    if t <= e1:
        return t
    if t <= e2:
        return t - e1
    if t <= e3:
        return t - e2
    return t - e3


def grid_align_net_direction_for_phase(
    features: FeatureSnapshot,
    config: StrategyConfig,
    phase: int,
) -> str:
    """
    网格「优势侧」对齐用的方向（与 net_direction 同形："Up"/"Down"/"平衡"/"空仓"）。

    - 阶段 3、4：哪一侧市价 > 该阶段配置的 ``grid.phase_{3,4}_advantage_price_threshold``
      则视为更有潜力获胜的一侧；若两侧均未过阈值（或异常下双侧均过），则回退为市价更高的一侧。
    - 其他阶段：沿用 ``features.net_direction``（净持仓方向）。
    """
    if phase not in (3, 4):
        return features.net_direction
    default_thr = 0.55 if phase == 3 else 0.65
    key = (
        "grid.phase_3_advantage_price_threshold"
        if phase == 3
        else "grid.phase_4_advantage_price_threshold"
    )
    try:
        thr = float(config.get(key, default_thr))
    except (TypeError, ValueError):
        thr = default_thr
    up_p = float(features.up_last_price)
    down_p = float(features.down_last_price)
    up_over = up_p > thr + 1e-12
    down_over = down_p > thr + 1e-12
    if up_over and not down_over:
        return "Up"
    if down_over and not up_over:
        return "Down"
    return "Up" if up_p >= down_p else "Down"


def is_rule_enabled_for_phase(
    config: StrategyConfig,
    *,
    section: str,
    rule: str,
    phase: int,
) -> bool:
    """
    判断某规则在指定阶段是否使能。

    配置路径：
    - rule_enablement.<section>.<rule>.enabled
    - rule_enablement.<section>.<rule>.phase_<n>
    """
    root = f"rule_enablement.{section}.{rule}"
    enabled = config.get(f"{root}.enabled", True)
    if isinstance(enabled, bool) and not enabled:
        return False
    phase_key = f"{root}.phase_{int(phase)}"
    phase_enabled = config.get(phase_key, None)
    if isinstance(phase_enabled, bool):
        return phase_enabled
    return True
