from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import OrderIntent
from polynet_ai.execution.fok_orderbook import (
    estimate_fok_plan,
    fok_precheck_execution_result,
    synthetic_paper_fill_from_plan,
)
from polynet_ai.execution.paper_broker import PaperBroker


class _Lvl:
    def __init__(self, price: str, size: str) -> None:
        self.price = price
        self.size = size


class _Book:
    def __init__(self) -> None:
        self.tick_size = "0.01"
        self.min_order_size = "1"
        self.asks = [_Lvl("0.50", "100"), _Lvl("0.52", "100")]
        self.bids = [_Lvl("0.48", "100")]


def test_estimate_fok_plan_buy_walks_asks() -> None:
    book = _Book()
    plan = estimate_fok_plan(book, action="buy", shares=10.0, price_buffer_ticks=1)
    assert plan is not None
    assert plan.shares > 0
    assert 0.5 <= plan.estimated_vwap <= 0.52


def test_fok_precheck_and_synthetic_fill() -> None:
    book = _Book()
    intent = OrderIntent(
        market_id="m",
        cycle_id="c",
        category="trend",
        action="buy",
        outcome="up",
        shares=5.0,
        reference_price=0.49,
        reason="t",
        priority=1,
        metadata={"up_token_id": "1"},
    )
    err, plan = fok_precheck_execution_result(
        intent,
        book=book,
        price_buffer_ticks=1,
        use_orderbook_min_order_size=True,
        market_min_order_size_fallback=5.0,
        enforce_sell_min_order_size=True,
    )
    assert err is None and plan is not None
    fill = synthetic_paper_fill_from_plan(intent, plan, timestamp=datetime(2026, 1, 1), fee_rate=0.002)
    assert fill.fill_source == "paper_orderbook_fok"
    assert fill.shares == plan.shares


def test_paper_broker_legacy_without_client() -> None:
    b = PaperBroker()
    intent = OrderIntent(
        market_id="m",
        cycle_id="c",
        category="trend",
        action="buy",
        outcome="up",
        shares=1.0,
        reference_price=0.5,
        reason="t",
        priority=1,
        metadata={},
    )
    r = b.execute(intent, datetime(2026, 1, 1))
    assert r.status == "filled" and r.fill is not None
    assert r.fill.fill_source == "paper_simulated"
