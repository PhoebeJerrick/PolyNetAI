from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.cycle_windows import determine_phase
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
) -> tuple[str, float]:
    """
    第四阶段 / 文档第六节 Last Minute：优势侧（满足其一即可）

    - 该侧份额 ≥ 另一侧 × preferred_leg_min_ratio
    - 否则：UP/DOWN 市价较高者为优势侧（双侧无仓或接近平衡时亦用市价决胜，便于尾部建仓）

    Returns:
        (direction, confidence) — direction 为 "up" / "down"
    """
    ratio = max(1.0, float(config.get("last_minute.preferred_leg_min_ratio", 1.2)))
    up_h = float(features.up_held)
    down_h = float(features.down_held)
    up_p = float(features.up_last_price)
    down_p = float(features.down_last_price)

    if down_h > 1e-12 and up_h >= ratio * down_h:
        return ("up", 0.85)
    if up_h > 1e-12 and down_h >= ratio * up_h:
        return ("down", 0.85)
    if up_h <= 1e-12 and down_h <= 1e-12:
        return ("up", 0.7) if up_p >= down_p else ("down", 0.7)
    if down_h <= 1e-12:
        return ("up", 0.8)
    if up_h <= 1e-12:
        return ("down", 0.8)
    return ("up", 0.75) if up_p >= down_p else ("down", 0.75)


def build_last_minute_candidate(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if not features.is_last_minute:
        return []

    phase = determine_phase(features.cycle_elapsed_seconds, config)
    priority = int(config.priority_for("last_minute", phase))
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

    winning_dir, _confidence = determine_winning_direction(features, config)

    min_conf = float(config.get("last_minute.last_minute_min_confidence", 0.85))
    if features.confidence_proxy < min_conf:
        return []

    # 使用确认的获胜方向（替代原有的方向选择逻辑）
    target_outcome = winning_dir

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
