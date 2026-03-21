from __future__ import annotations

from dataclasses import dataclass, replace

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
    fee_rate = float(config.get("execution.fee_rate", 0.002))
    slippage_bps = float(config.get("execution.slippage_bps", 10.0))
    max_cash_utilization = float(config.get("capital.max_cash_utilization", 0.95))
    min_cash_buffer = float(config.get("capital.min_cash_buffer", 25.0))

    # Polymarket 的最小份数约束主要作用于买单；卖单需要允许小于最小值以便减仓/平仓。
    effective_min_order = min_order if intent.action == "buy" else 0.0
    clipped = intent.clipped(effective_min_order, max_order)
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

    if clipped.action == "buy":
        account_cash = float(clipped.metadata.get("account_cash", 0.0))
        reserved_cash = max(0.0, min_cash_buffer)
        spendable_cash = max(0.0, account_cash * max(0.0, min(max_cash_utilization, 1.0)) - reserved_cash)
        unit_price = clipped.reference_price * (1.0 + slippage_bps / 10_000.0)
        unit_cost = unit_price * (1.0 + fee_rate)
        affordable_shares = spendable_cash / unit_cost if unit_cost > 0 else 0.0

        if affordable_shares < min_order:
            return RiskDecision(False, None, "可用现金不足，无法满足最小下单量")

        if affordable_shares < clipped.shares:
            clipped = replace(clipped, shares=affordable_shares)
            clipped.metadata["cash_limited"] = True

    return RiskDecision(True, clipped, "")
