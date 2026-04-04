"""CLOB 订单簿上 FOK 吃单路径的共享逻辑（实盘 PolymarketBroker 与订单簿对齐的 PaperBroker 共用）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from py_clob_client.clob_types import OrderBookSummary, OrderSummary

from polynet_ai.domain.models import ExecutionResult, FillEvent, OrderIntent


@dataclass(slots=True)
class ExecutionPlan:
    shares: float
    estimated_vwap: float
    limit_price: float
    deepest_price: float
    tick_size: float
    min_order_size: float


def level_price(level: OrderSummary) -> float:
    return float(level.price or 0.0)


def level_size(level: OrderSummary) -> float:
    return float(level.size or 0.0)


def round_to_tick(price: float, tick_size: float) -> float:
    decimals = max(0, len(str(tick_size).split(".", 1)[1]) if "." in str(tick_size) else 0)
    return round(price, decimals)


def normalize_shares_for_order(action: str, shares: float, price: float) -> float:
    if action == "buy":
        if price <= 0:
            return 0.0
        return round(shares, 4)
    return round(shares, 2)


def normalize_market_amount(action: str, shares: float, price: float) -> float:
    if action == "buy":
        return round(shares * price, 2)
    return round(shares, 2)


def estimate_fok_plan(
    book: OrderBookSummary,
    *,
    action: str,
    shares: float,
    price_buffer_ticks: int = 1,
) -> ExecutionPlan | None:
    if shares <= 0:
        return None

    tick_size = float(book.tick_size or "0.01")
    if action == "buy":
        levels = sorted(book.asks or [], key=level_price)
    else:
        levels = sorted(book.bids or [], key=level_price, reverse=True)
    if not levels:
        return None

    remaining = shares
    filled = 0.0
    notional = 0.0
    deepest = 0.0
    for level in levels:
        price = level_price(level)
        size = level_size(level)
        if price <= 0 or size <= 0:
            continue
        take = min(remaining, size)
        if take <= 0:
            continue
        filled += take
        remaining -= take
        notional += take * price
        deepest = price
        if remaining <= 1e-9:
            break

    if filled + 1e-9 < shares:
        return None

    estimated_vwap = notional / filled
    if action == "buy":
        limit_price = min(0.99, deepest + tick_size * max(0, price_buffer_ticks))
    else:
        limit_price = max(0.001, deepest - tick_size * max(0, price_buffer_ticks))
    limit_price = round_to_tick(limit_price, tick_size)
    normalized_shares = normalize_shares_for_order(action, shares, limit_price)
    if normalized_shares <= 0:
        return None
    return ExecutionPlan(
        shares=normalized_shares,
        estimated_vwap=estimated_vwap,
        limit_price=limit_price,
        deepest_price=deepest,
        tick_size=tick_size,
        min_order_size=float(book.min_order_size or 0.0),
    )


def resolve_outcome_token_id(intent: OrderIntent) -> str:
    if intent.outcome == "up":
        token_id = intent.metadata.get("up_token_id") or intent.metadata.get("yes_token_id")
    else:
        token_id = intent.metadata.get("down_token_id") or intent.metadata.get("no_token_id")
    if not token_id:
        raise ValueError(f"无法解析 {intent.outcome} 对应 token_id，cycle={intent.cycle_id}")
    return str(token_id)


def synthetic_paper_fill_from_plan(
    intent: OrderIntent,
    plan: ExecutionPlan,
    *,
    timestamp: datetime,
    fee_rate: float,
) -> FillEvent:
    gross = plan.estimated_vwap * plan.shares
    fee = gross * fee_rate
    slippage = abs(plan.estimated_vwap - float(intent.reference_price))
    return FillEvent(
        market_id=intent.market_id,
        cycle_id=intent.cycle_id,
        timestamp=timestamp,
        price=plan.estimated_vwap,
        shares=plan.shares,
        outcome=intent.outcome,
        action=intent.action,
        fee=fee,
        slippage=slippage,
        reason=intent.reason,
        fill_source="paper_orderbook_fok",
    )


def fok_precheck_execution_result(
    intent: OrderIntent,
    *,
    book: OrderBookSummary,
    price_buffer_ticks: int,
    use_orderbook_min_order_size: bool,
    market_min_order_size_fallback: float,
    enforce_sell_min_order_size: bool,
) -> tuple[ExecutionResult | None, ExecutionPlan | None]:
    """与 PolymarketBroker.execute 前半段一致：失败时返回 (ExecutionResult, None)，成功返回 (None, plan)。"""
    tick_size = float(book.tick_size or "0.01")
    raw_market_min = float(book.min_order_size or market_min_order_size_fallback)
    market_min_order_size = max(0.0, raw_market_min if use_orderbook_min_order_size else 0.0)
    enforce_min_order_size = intent.action == "buy" or enforce_sell_min_order_size
    required_min_order_size = market_min_order_size if enforce_min_order_size else 0.0

    if required_min_order_size > 0 and float(intent.shares) + 1e-9 < required_min_order_size:
        return (
            ExecutionResult(
                status="invalid_amount",
                reason=f"下单份额低于市场最小下单量 {required_min_order_size:g}",
            ),
            None,
        )

    plan = estimate_fok_plan(
        book,
        action=intent.action,
        shares=float(intent.shares),
        price_buffer_ticks=price_buffer_ticks,
    )
    if plan is None:
        return ExecutionResult(status="no_liquidity", reason="订单簿深度不足"), None

    if required_min_order_size > 0 and plan.shares + 1e-9 < required_min_order_size:
        return (
            ExecutionResult(
                status="invalid_amount",
                reason=f"归一化后份额低于市场最小下单量 {required_min_order_size:g}",
            ),
            None,
        )

    market_amount = normalize_market_amount(intent.action, plan.shares, plan.limit_price)
    if market_amount <= 0:
        return ExecutionResult(status="invalid_amount", reason="下单金额无效"), None

    return None, plan
