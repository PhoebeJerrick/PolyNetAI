from __future__ import annotations

from py_clob_client.clob_types import OrderBookSummary, OrderSummary

from polynet_ai.execution.fok_orderbook import estimate_fok_plan, normalize_market_amount


def test_estimate_fok_plan_for_buy_uses_ask_depth_and_buffer() -> None:
    book = OrderBookSummary(
        asks=[
            OrderSummary(price="0.51", size="2"),
            OrderSummary(price="0.52", size="4"),
        ],
        bids=[],
        tick_size="0.01",
        min_order_size="5",
    )

    plan = estimate_fok_plan(book, action="buy", shares=5)

    assert plan is not None
    assert round(plan.estimated_vwap, 3) == 0.516
    assert round(plan.deepest_price, 2) == 0.52
    assert round(plan.limit_price, 2) == 0.53
    assert plan.min_order_size == 5.0


def test_estimate_fok_plan_for_sell_uses_bid_depth_and_buffer() -> None:
    book = OrderBookSummary(
        bids=[
            OrderSummary(price="0.49", size="3"),
            OrderSummary(price="0.48", size="5"),
        ],
        asks=[],
        tick_size="0.01",
    )

    plan = estimate_fok_plan(book, action="sell", shares=4)

    assert plan is not None
    assert round(plan.estimated_vwap, 4) == 0.4875
    assert round(plan.deepest_price, 2) == 0.48
    assert round(plan.limit_price, 2) == 0.47


def test_estimate_fok_plan_returns_none_when_depth_is_insufficient() -> None:
    book = OrderBookSummary(
        asks=[OrderSummary(price="0.51", size="2")],
        bids=[],
        tick_size="0.01",
    )

    plan = estimate_fok_plan(book, action="buy", shares=3)

    assert plan is None


def test_normalize_market_amount_rounds_buy_to_two_decimal_notional() -> None:
    amount = normalize_market_amount("buy", shares=8.3829787234, price=0.53)

    assert amount == 4.44


def test_normalize_market_amount_rounds_sell_to_two_decimal_shares() -> None:
    amount = normalize_market_amount("sell", shares=8.3829787234, price=0.53)

    assert amount == 8.38
