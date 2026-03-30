from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.cycle_windows import determine_phase
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


def _cfg_float(config: StrategyConfig, *paths: str, default: float) -> float:
    for path in paths:
        value = config.get(path, None)
        if value is not None:
            return float(value)
    return default


def _resolve_market_min_order_size(intent: OrderIntent, config: StrategyConfig) -> float:
    use_orderbook_min = bool(config.get("execution.market_limits.use_orderbook_min_order_size", True))
    if not use_orderbook_min:
        return 0.0
    raw_market_min = intent.metadata.get("market_min_order_size")
    if raw_market_min is not None:
        try:
            return max(0.0, float(raw_market_min))
        except (TypeError, ValueError):
            pass
    return max(0.0, float(config.get("execution.market_limits.fallback_min_order_size", 0.0)))


def apply_risk_limits(features: FeatureSnapshot, intent: OrderIntent, config: StrategyConfig) -> RiskDecision:
    buy_min_order = _cfg_float(
        config,
        "order_sizing.buy.min_order_size",
        "order_sizing.min_order_size",
        default=2.0,
    )
    buy_max_order = _cfg_float(
        config,
        "order_sizing.buy.max_order_size",
        "order_sizing.max_order_size",
        default=60.0,
    )
    sell_min_order = _cfg_float(
        config,
        "order_sizing.sell.min_order_size",
        default=buy_min_order,
    )
    sell_max_order = _cfg_float(
        config,
        "order_sizing.sell.max_order_size",
        default=buy_max_order,
    )
    allow_close_below_sell_min = bool(
        config.get("order_sizing.sell.allow_close_below_min_order_size", True)
    )
    market_min_order = _resolve_market_min_order_size(intent, config)
    enforce_sell_market_min = bool(config.get("execution.market_limits.enforce_sell_min_order_size", True))
    effective_buy_min_order = max(buy_min_order, market_min_order)
    effective_sell_min_order = max(sell_min_order, market_min_order if enforce_sell_market_min else 0.0)
    max_exposure = float(
        config.get(
            "exposure.max_abs_exposure_value",
            config.get("exposure.max_abs_exposure", 200.0),
        )
    )
    # Phase 4（最后阶段）：方向已基本确定，允许扩大净敞口上限以充分获利
    if determine_phase(features.cycle_elapsed_seconds, config) == 4:
        max_exposure = float(
            config.get("exposure.phase_4_max_abs_exposure_value", max_exposure)
        )
    max_trades = int(config.get("exposure.max_strategy_trades_per_cycle", 12))
    fee_rate = float(config.get("execution.fee_rate", 0.002))
    slippage_bps = float(config.get("execution.slippage_bps", 10.0))
    max_cash_utilization = float(config.get("capital.max_cash_utilization", 0.95))
    min_cash_buffer = float(config.get("capital.min_cash_buffer", 0.0))
    if intent.action == "buy":
        clipped = intent.clipped(effective_buy_min_order, buy_max_order)
    else:
        clipped = intent.clipped(effective_sell_min_order, sell_max_order)
    if clipped is None:
        return RiskDecision(False, None, "订单规模低于最小阈值")

    min_gap = float(config.get("execution.min_seconds_between_orders", 2.0))
    if min_gap > 0:
        # 按方向独立追踪下单间隔：买 UP 检查 up 最后成交时间，买/卖 DOWN 检查 down
        direction_key = "last_strategy_fill_at_up" if clipped.outcome == "up" else "last_strategy_fill_at_down"
        last_at = _coerce_datetime(clipped.metadata.get(direction_key))
        if last_at is not None:
            delta = (features.timestamp - last_at).total_seconds()
            if delta <= min_gap:
                return RiskDecision(False, None, f"策略下单间隔不足{min_gap:g}秒（{clipped.outcome}方向）")

    # 价格波动检查：仅对买入单生效；卖出单（止盈/止损/减仓）不受此约束
    min_move = float(config.get("execution.min_same_outcome_price_move_ratio", 0.005))
    if clipped.action == "buy" and min_move > 0 and clipped.reference_price > 1e-12:
        key = "last_strategy_fill_price_up" if clipped.outcome == "up" else "last_strategy_fill_price_down"
        raw_last = clipped.metadata.get(key)
        if raw_last is not None:
            last_px = float(raw_last)
            if last_px > 1e-12:
                move = abs(clipped.reference_price - last_px) / last_px
                if move <= min_move:
                    pct = min_move * 100.0
                    return RiskDecision(False, None, f"同方向买入价格相对上次成交价波动不足{pct:g}%")

    if features.net_position_value and abs(features.net_position_value) > max_exposure and clipped.action == "buy":
        if clipped.category not in {"hedge", "last_minute"}:
            return RiskDecision(False, None, "净敞口已超限，仅允许对冲或尾盘调整")

    if clipped.metadata.get("strategy_trades", 0) >= max_trades and clipped.category != "last_minute":
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

        if affordable_shares < effective_buy_min_order:
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
        if (
            clipped.shares + 1e-12 < effective_sell_min_order
            and available_shares > 0
            and allow_close_below_sell_min
            and available_shares + 1e-12 <= effective_sell_min_order
        ):
            clipped = replace(clipped, shares=available_shares)
            clipped.metadata["sell_below_min_forced_close"] = True
        elif clipped.shares + 1e-12 < effective_sell_min_order:
            return RiskDecision(False, None, "卖单规模低于最小下单量")

    return RiskDecision(True, clipped, "")
