"""阶段性动态优先级：在分阶段基础优先级之上按仓位与阶段做微调。"""

from __future__ import annotations

from dataclasses import replace

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.cycle_windows import calculate_position_percentage, determine_phase
from polynet_ai.strategy.spec import StrategyConfig


def compute_dynamic_priority(
    priority: int,
    category: str,
    action: str,
    features: FeatureSnapshot,
    config: StrategyConfig,
) -> int:
    """返回应用 dynamic_priority 后的优先级数值（数值越小越优先）。"""
    if category == "stop_loss":
        return int(priority)

    phase = determine_phase(features.cycle_elapsed_seconds, config)
    pos_pct = calculate_position_percentage(features, config)

    if phase == 1:
        thr = float(config.get("dynamic_priority.phase_1_position_threshold", 0.65))
        boost = int(config.get("dynamic_priority.phase_1_boost", 0))
        if boost <= 0 or pos_pct >= thr:
            return int(priority)
        if action == "buy" and category in ("opening", "mean_reversion"):
            return max(1, int(priority) - boost)
        return int(priority)

    if phase == 2:
        sell_thr = float(config.get("dynamic_priority.phase_2_position_threshold", 0.50))
        low_thr = float(config.get("dynamic_priority.phase_2_low_position_threshold", 0.25))
        boost = int(config.get("dynamic_priority.phase_2_boost", 0))
        if boost > 0 and pos_pct < low_thr:
            if action == "buy":
                return max(1, int(priority) - boost)
        if boost > 0 and pos_pct > sell_thr:
            if action == "sell" and category in ("grid", "take_profit"):
                return max(1, int(priority) - boost)
        return int(priority)

    if phase == 3:
        thr = float(config.get("dynamic_priority.phase_3_position_threshold", 0.75))
        trend_b = int(config.get("dynamic_priority.phase_3_trend_boost", 0))
        grid_b = int(config.get("dynamic_priority.phase_3_grid_boost", 0))
        hedge_b = int(config.get("dynamic_priority.phase_3_hedge_boost", 0))
        tp_sell_deboost = int(config.get("dynamic_priority.phase_3_take_profit_sell_deboost", 0))
        grid_sell_deboost = int(config.get("dynamic_priority.phase_3_grid_sell_deboost", 0))
        buy_deboost_when_full = int(config.get("dynamic_priority.phase_3_buy_deboost_when_full", 0))
        tp_sell_boost_when_full = int(config.get("dynamic_priority.phase_3_take_profit_sell_boost_when_full", 0))
        grid_sell_boost_when_full = int(config.get("dynamic_priority.phase_3_grid_sell_boost_when_full", 0))
        if pos_pct >= thr:
            if action == "buy" and category in {"trend", "hedge", "grid", "mean_reversion", "opening"}:
                return int(priority) + max(0, buy_deboost_when_full)
            if action == "sell" and category == "take_profit":
                return max(1, int(priority) - max(0, tp_sell_boost_when_full))
            if action == "sell" and category == "grid":
                return max(1, int(priority) - max(0, grid_sell_boost_when_full))
            return int(priority)
        if action == "buy" and category == "trend" and trend_b > 0:
            return max(1, int(priority) - trend_b)
        if action == "buy" and category == "hedge" and hedge_b > 0:
            return max(1, int(priority) - hedge_b)
        if action == "buy" and category == "grid" and grid_b > 0:
            return max(1, int(priority) - grid_b)
        if action == "sell" and category == "take_profit" and tp_sell_deboost > 0:
            return int(priority) + tp_sell_deboost
        if action == "sell" and category == "grid" and grid_sell_deboost > 0:
            return int(priority) + grid_sell_deboost
        return int(priority)

    return int(priority)


def adjust_priority_by_phase(intent: OrderIntent, features: FeatureSnapshot, config: StrategyConfig) -> None:
    """就地调整 intent.priority（供单元测试与外部诊断）。"""
    intent.priority = compute_dynamic_priority(
        intent.priority,
        intent.category,
        intent.action,
        features,
        config,
    )


def apply_dynamic_priorities_to_candidates(
    candidates: list[OrderIntent],
    features: FeatureSnapshot,
    config: StrategyConfig,
) -> list[OrderIntent]:
    """对候选列表返回新列表，逐项应用 compute_dynamic_priority。"""
    out: list[OrderIntent] = []
    for c in candidates:
        new_p = compute_dynamic_priority(c.priority, c.category, c.action, features, config)
        if new_p != c.priority:
            out.append(replace(c, priority=new_p))
        else:
            out.append(c)
    return out
