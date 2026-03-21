from __future__ import annotations

from datetime import datetime, timedelta

from polynet_ai.domain.models import FillEvent, TradeEvent
from polynet_ai.domain.settlement import settlement_summary
from polynet_ai.domain.state_engine import StateEngine


def test_state_engine_tracks_balances_and_realized_pnl() -> None:
    engine = StateEngine()
    t0 = datetime(2026, 3, 20, 12, 0, 0)
    engine.apply_market_trade(
        TradeEvent("BTC", "cycle-1", t0, price=0.4, shares=10, outcome="up", action="buy")
    )
    engine.apply_strategy_fill(
        FillEvent("BTC", "cycle-1", t0 + timedelta(seconds=5), price=0.4, shares=10, outcome="up", action="buy")
    )
    engine.apply_strategy_fill(
        FillEvent("BTC", "cycle-1", t0 + timedelta(seconds=10), price=0.6, shares=4, outcome="up", action="sell")
    )

    snapshot = engine.snapshot()
    assert snapshot.up_balance == 6
    assert snapshot.net_position == 6
    assert round(snapshot.up_realized_pnl, 3) == 0.8
    assert snapshot.up_last_price == 0.6
    assert snapshot.down_last_price == 0.0


def test_settlement_summary_matches_winner_logic() -> None:
    engine = StateEngine()
    t0 = datetime(2026, 3, 20, 12, 0, 0)
    engine.apply_market_trade(
        TradeEvent("BTC", "cycle-2", t0, price=0.45, shares=10, outcome="up", action="buy")
    )
    engine.apply_market_trade(
        TradeEvent("BTC", "cycle-2", t0 + timedelta(seconds=20), price=0.7, shares=3, outcome="down", action="buy")
    )
    engine.apply_strategy_fill(
        FillEvent("BTC", "cycle-2", t0 + timedelta(seconds=25), price=0.4, shares=10, outcome="up", action="buy")
    )
    engine.apply_strategy_fill(
        FillEvent("BTC", "cycle-2", t0 + timedelta(seconds=30), price=0.45, shares=2, outcome="up", action="sell")
    )

    summary = settlement_summary(engine.state)
    assert summary.winner == "down"
    assert summary.realized_up_pnl == 0.1
    assert round(summary.unrealized_up_pnl, 3) == -3.2
