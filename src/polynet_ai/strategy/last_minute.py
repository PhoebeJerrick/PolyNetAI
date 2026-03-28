from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.spec import StrategyConfig
from polynet_ai.strategy.price_reference import outcome_reference_price


def _favored_outcome_by_unrealized(features: FeatureSnapshot) -> str:
    """浮盈更高的一侧为优势侧；完全平局时用市价更高的一侧。"""
    up_u = float(features.unrealized_up_pnl)
    down_u = float(features.unrealized_down_pnl)
    if up_u > down_u + 1e-12:
        return "up"
    if down_u > up_u + 1e-12:
        return "down"
    if float(features.up_last_price) >= float(features.down_last_price):
        return "up"
    return "down"


def determine_winning_direction(
    features: FeatureSnapshot, config: StrategyConfig
) -> tuple[str | None, float]:
    """
    使用更严格的条件确认获胜方向（A+C联合方案 — 第四阶段改进）

    三个条件必须同时满足：
    1. 优势侧仓位价值 ≥ 另一侧 × ratio_threshold
    2. 优势侧浮盈 > 劣势侧浮盈 × pnl_ratio_threshold
    3. 趋势强度 ≥ min_trend_strength 且趋势方向与优势侧一致

    Returns:
        (direction, confidence) — direction 为 "up"/"down"/None, confidence 为 0.0~0.9
    """
    ratio_threshold = float(config.get("last_minute.direction_ratio_threshold", 1.5))
    pnl_ratio_threshold = float(config.get("last_minute.pnl_ratio_threshold", 2.0))
    min_trend_strength = float(config.get("last_minute.min_trend_strength_for_direction", 0.6))

    up_value = features.up_held * features.up_avg_price
    down_value = features.down_held * features.down_avg_price
    up_pnl = features.unrealized_up_pnl
    down_pnl = features.unrealized_down_pnl

    def _check(fav_val: float, oth_val: float, fav_pnl: float, oth_pnl: float, bias: str) -> bool:
        if oth_val > 1e-12 and fav_val < oth_val * ratio_threshold:
            return False
        if oth_val <= 1e-12 and fav_val <= 1e-12:
            return False
        # 浮盈条件：劣势侧浮盈 <= 0 时只要优势侧浮盈 > 0 即满足
        if oth_pnl > 1e-12:
            if fav_pnl <= oth_pnl * pnl_ratio_threshold:
                return False
        elif fav_pnl <= 1e-12:
            return False
        if features.trend_strength < min_trend_strength:
            return False
        if features.trend_bias != bias:
            return False
        return True

    if _check(up_value, down_value, up_pnl, down_pnl, "up"):
        return ("up", 0.9)
    if _check(down_value, up_value, down_pnl, up_pnl, "down"):
        return ("down", 0.9)
    return (None, 0.0)


def build_last_minute_candidate(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if not features.is_last_minute:
        return []

    priority = int(config.priorities.get("last_minute", 20))
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    up_ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
    down_ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
    intents: list[OrderIntent] = []
    if features.unrealized_up_pnl < 0 and features.up_held > 0:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="sell",
                shares=features.up_held,
                reference_price=up_ref,
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
                reference_price=down_ref,
                category="last_minute",
                reason="最后一分钟强制平掉亏损 Down 方向",
                priority=priority,
            )
        )
    if intents:
        return intents

    # A+C联合方案：使用严格的方向确认逻辑
    winning_dir, _confidence = determine_winning_direction(features, config)
    if winning_dir is None:
        # 无法确认获胜方向 → 保守策略：不加仓
        return []

    min_conf = float(config.get("last_minute.last_minute_min_confidence", 0.85))
    if features.confidence_proxy < min_conf:
        return []

    # 使用确认的获胜方向（替代原有的方向选择逻辑）
    target_outcome = winning_dir

    conservative_exp = float(config.get("last_minute.conservative_max_exposure", 10.0))
    max_exp = float(config.get("last_minute.max_tail_exposure", 40.0))
    tail_formula = min(
        max_exp,
        float(config.get("last_minute.tail_profit_scale", 0.35)) * abs(features.cycle_net_profit)
        + float(config.get("last_minute.tail_volatility_scale", 25.0)) * features.volatility,
    )
    target_net = tail_formula
    applied_preferred_ratio = False

    ratio = float(config.get("last_minute.preferred_leg_min_ratio", 1.0))
    if ratio > 1.0 + 1e-12:
        favored = _favored_outcome_by_unrealized(features)
        fav_held = features.up_held if favored == "up" else features.down_held
        oth_held = features.down_held if favored == "up" else features.up_held
        if oth_held > 1e-12:
            min_favored_held = ratio * oth_held
            if fav_held + 1e-12 < min_favored_held:
                target_outcome = favored
                target_net = min(max_exp, max(min_favored_held, tail_formula))
                applied_preferred_ratio = True

    held = features.up_held if target_outcome == "up" else features.down_held
    if held >= target_net:
        return []

    reason = (
        "最后一分钟按优势侧份额比例与波动率调整留仓"
        if applied_preferred_ratio
        else "最后一分钟按盈利方向和波动率调整留仓"
    )

    return [
        OrderIntent(
            market_id=features.market_id,
            cycle_id=features.cycle_id,
            outcome=target_outcome,
            action="buy",
            shares=max(0.0, target_net - held),
            reference_price=up_ref if target_outcome == "up" else down_ref,
            category="last_minute",
            reason=reason,
            priority=priority,
        )
    ]
