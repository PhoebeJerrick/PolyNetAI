from __future__ import annotations

from dataclasses import dataclass

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.spec import StrategyConfig


@dataclass(slots=True)
class RiskDecision:
    accepted: bool
    intent: OrderIntent | None
    reason: str = ""


def apply_risk_limits(features: FeatureSnapshot, intent: OrderIntent, config: StrategyConfig) -> RiskDecision:
    min_order = float(config.get("order_sizing.min_order_size", 2.0))
    max_order = float(config.get("order_sizing.max_order_size", 60.0))
    max_exposure = float(config.get("exposure.max_abs_exposure", 200.0))
    max_trades = int(config.get("exposure.max_strategy_trades_per_cycle", 12))

    clipped = intent.clipped(min_order, max_order)
    if clipped is None:
        return RiskDecision(False, None, "订单规模低于最小阈值")

    if features.net_position_value and abs(features.net_position_value) > max_exposure and clipped.action == "buy":
        if clipped.category not in {"hedge", "last_minute"}:
            return RiskDecision(False, None, "净敞口已超限，仅允许对冲或尾盘调整")

    if clipped.metadata.get("strategy_trades", 0) >= max_trades:
        return RiskDecision(False, None, "本周期策略成交次数已达上限")

    projected_exposure = abs(features.net_position_value)
    projected_delta = clipped.reference_price * clipped.shares
    if clipped.action == "buy":
        projected_exposure += projected_delta
    else:
        projected_exposure = max(0.0, projected_exposure - projected_delta)

    if projected_exposure > max_exposure and clipped.category not in {"hedge", "last_minute"}:
        return RiskDecision(False, None, "下单后净敞口超过风控阈值")

    return RiskDecision(True, clipped, "")
