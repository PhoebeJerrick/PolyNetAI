from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.spec import StrategyConfig


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass(slots=True)
class RiskDecision:
    accepted: bool
    intent: OrderIntent | None
    reason: str = ""


def apply_risk_limits(features: FeatureSnapshot, intent: OrderIntent, config: StrategyConfig) -> RiskDecision:
    min_order = float(config.get("order_sizing.min_order_size", 2.0))
    max_order = float(config.get("order_sizing.max_order_size", 60.0))
    max_exposure = float(
        config.get(
            "exposure.max_abs_exposure_value",
            config.get("exposure.max_abs_exposure", 200.0),
        )
    )
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

    min_gap = float(config.get("execution.min_seconds_between_orders", 2.0))
    if min_gap > 0:
        last_at = _coerce_datetime(clipped.metadata.get("last_strategy_fill_at"))
        if last_at is not None:
            delta = (features.timestamp - last_at).total_seconds()
            if delta <= min_gap:
                return RiskDecision(False, None, f"策略下单间隔不足{min_gap:g}秒")

    min_move = float(config.get("execution.min_same_outcome_price_move_ratio", 0.03))
    if min_move > 0 and clipped.reference_price > 1e-12:
        key = "last_strategy_fill_price_up" if clipped.outcome == "up" else "last_strategy_fill_price_down"
        raw_last = clipped.metadata.get(key)
        if raw_last is not None:
            last_px = float(raw_last)
            if last_px > 1e-12:
                move = abs(clipped.reference_price - last_px) / last_px
                if move <= min_move:
                    pct = min_move * 100.0
                    return RiskDecision(False, None, f"同方向价格相对上次成交价波动不足{pct:g}%")

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
        account_cash = float(clipped.metadata.get("account_available_cash", clipped.metadata.get("account_cash", 0.0)))
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
    else:
        held_shares = features.up_held if clipped.outcome == "up" else features.down_held
        pending_sell_shares = float(
            clipped.metadata.get(
                f"pending_{clipped.outcome}_sell_shares",
                clipped.metadata.get("pending_sell_shares", 0.0),
            )
        )
        available_shares = max(0.0, held_shares - pending_sell_shares)

        if available_shares <= 0:
            return RiskDecision(False, None, "可卖仓位不足，存在待确认卖单")

        if available_shares < clipped.shares:
            clipped = replace(clipped, shares=available_shares)
            clipped.metadata["pending_sell_limited"] = True

    return RiskDecision(True, clipped, "")
