from __future__ import annotations

from datetime import datetime, timedelta

from polynet_ai.domain.models import DecisionOutcome, ExecutionResult, FillEvent, OrderIntent, TradeEvent
from polynet_ai.engine.replay import ReplayEngine
from polynet_ai.strategy.spec import StrategyConfig


def build_config() -> StrategyConfig:
    return StrategyConfig(
        raw={
            "cycle": {"cycle_seconds": 300, "last_minute_seconds": 60},
            "order_sizing": {"base_order_size": 8.0, "min_order_size": 2.0, "max_order_size": 60.0, "volatility_order_scale": 20.0},
            "exposure": {"max_abs_exposure": 200.0, "hedge_trigger_value": 50.0, "hedge_scale": 0.15, "max_grid_net_position": 20.0, "max_strategy_trades_per_cycle": 12},
            "trend": {"min_trend_strength": 0.35, "trend_price_edge": 0.03, "trend_scale": 0.15},
            "grid": {"grid_low_percentile": 0.25, "grid_high_percentile": 0.75},
            "mean_reversion": {"up_buy_deviation": 0.10, "down_buy_deviation": 0.10, "mean_reversion_sell_up_deviation": 0.20, "mean_reversion_sell_down_deviation": 0.20, "deviation_scale": 45.0},
            "profit_taking": {"take_profit_up_deviation": 0.20, "take_profit_down_deviation": 0.20, "take_profit_fraction": 0.35},
            "stop_loss": {"stop_loss_cycle_loss": 20.0, "stop_loss_fraction": 0.50},
            "last_minute": {"last_minute_min_confidence": 0.60, "tail_profit_scale": 0.35, "tail_volatility_scale": 25.0, "max_tail_exposure": 40.0},
            "execution": {"fee_rate": 0.002, "slippage_bps": 10},
            "priorities": {"risk": 10, "last_minute": 20, "stop_loss": 30, "hedge": 40, "take_profit": 50, "grid": 60, "mean_reversion": 70, "trend": 80},
        }
    )


def test_replay_engine_runs_end_to_end() -> None:
    t0 = datetime(2026, 3, 20, 12, 0, 0)
    events = [
        TradeEvent("BTC", "cycle-a", t0, price=0.45, shares=10, outcome="up", action="buy"),
        TradeEvent("BTC", "cycle-a", t0 + timedelta(seconds=30), price=0.50, shares=8, outcome="up", action="buy"),
        TradeEvent("BTC", "cycle-a", t0 + timedelta(seconds=60), price=0.58, shares=7, outcome="up", action="buy"),
        TradeEvent("BTC", "cycle-a", t0 + timedelta(seconds=260), price=0.62, shares=5, outcome="up", action="buy"),
    ]

    engine = ReplayEngine(build_config())

    class StubRouter:
        def route(self, features, strategy_trades):
            return DecisionOutcome(
                selected=OrderIntent(
                    market_id=features.market_id,
                    cycle_id=features.cycle_id,
                    outcome="up",
                    action="buy",
                    shares=5.0,
                    reference_price=features.price,
                    category="grid",
                    reason="test immediate fill",
                    priority=10,
                ),
                candidates=[],
            )

    engine.router = StubRouter()
    result = engine.run(events)
    assert len(result.cycle_df) == 1
    assert len(result.decision_df) == 4
    assert result.decision_df["executed"].any()
    assert "total_net_profit" in result.metrics_df.columns


def test_finalize_cycle_settles_remaining_winner_shares_into_cash() -> None:
    engine = ReplayEngine(build_config(), starting_cash=100.0)
    t0 = datetime(2026, 3, 20, 12, 0, 0)

    engine.state_engine.apply_market_trade(
        TradeEvent("BTC", "cycle-a", t0, price=0.8, shares=10, outcome="up", action="buy")
    )
    engine.state_engine.apply_strategy_fill(
        FillEvent("BTC", "cycle-a", t0 + timedelta(seconds=5), price=0.4, shares=10, outcome="up", action="buy")
    )
    engine.account.apply_fill(
        FillEvent("BTC", "cycle-a", t0 + timedelta(seconds=5), price=0.4, shares=10, outcome="up", action="buy")
    )
    engine.state_engine.apply_market_trade(
        TradeEvent("BTC", "cycle-a", t0 + timedelta(seconds=10), price=0.8, shares=6, outcome="up", action="buy")
    )

    cycle_row = engine.finalize_pending_cycle()

    assert cycle_row is not None
    assert cycle_row["winner"] == "up"
    assert cycle_row["account_cash"] > 100.0
    assert round(float(cycle_row["account_cash"]), 3) == round(100.0 + float(cycle_row["cycle_net_profit"]), 3)


class PendingConfirmBroker:
    def __init__(self) -> None:
        self.poll_calls = 0
        self.pending_fill: FillEvent | None = None

    def execute(self, intent: OrderIntent, timestamp: datetime) -> ExecutionResult:
        self.pending_fill = FillEvent(
            market_id=intent.market_id,
            cycle_id=intent.cycle_id,
            timestamp=timestamp + timedelta(seconds=1),
            price=intent.reference_price,
            shares=intent.shares,
            outcome=intent.outcome,
            action=intent.action,
            fee=intent.reference_price * intent.shares * 0.002,
            reason=intent.reason,
            reserved_cash=intent.reference_price * intent.shares * 1.002,
        )
        return ExecutionResult(
            status="submitted",
            order_id="order-1",
            metadata={"reserved_cash": self.pending_fill.reserved_cash},
        )

    def poll(self, timestamp: datetime) -> list[FillEvent]:
        self.poll_calls += 1
        if self.pending_fill is None or self.poll_calls < 2:
            return []
        fill = self.pending_fill
        self.pending_fill = None
        return [fill]

    def pending_context(self) -> dict[str, float | int]:
        return {
            "pending_order_count": 1 if self.pending_fill is not None else 0,
            "pending_buy_reserved_cash": self.pending_fill.reserved_cash if self.pending_fill is not None else 0.0,
            "pending_up_sell_shares": 0.0,
            "pending_down_sell_shares": 0.0,
        }


def test_replay_engine_confirms_submitted_order_without_blocking_loop() -> None:
    broker = PendingConfirmBroker()
    engine = ReplayEngine(build_config(), starting_cash=100.0, broker=broker)

    class StubRouter:
        def route(self, features, strategy_trades):
            return DecisionOutcome(
                selected=OrderIntent(
                    market_id=features.market_id,
                    cycle_id=features.cycle_id,
                    outcome="up",
                    action="buy",
                    shares=5.0,
                    reference_price=features.price,
                    category="grid",
                    reason="test pending confirm",
                    priority=10,
                ),
                candidates=[],
            )

    engine.router = StubRouter()
    t0 = datetime(2026, 3, 20, 12, 0, 0)
    events = [
        TradeEvent("BTC", "cycle-a", t0, price=0.45, shares=10, outcome="up", action="buy"),
        TradeEvent("BTC", "cycle-a", t0 + timedelta(seconds=30), price=0.46, shares=8, outcome="up", action="buy"),
        TradeEvent("BTC", "cycle-a", t0 + timedelta(seconds=60), price=0.47, shares=8, outcome="up", action="buy"),
    ]

    first = engine.process_event(events[0])
    assert first.decision_row["submitted"] is True
    assert first.decision_row["executed"] is False
    assert engine.account.available_cash < engine.account.cash

    second = engine.process_event(events[1])
    assert engine.state_engine.state is not None
    assert engine.state_engine.state.strategy_trades == 1
    assert engine.account.available_cash <= engine.account.cash

    third = engine.process_event(events[2])
    assert third.decision_row["executed"] is False
    assert engine.state_engine.state is not None
    assert engine.state_engine.state.strategy_trades >= 1
