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
            "exposure": {"max_abs_exposure_value": 200.0, "hedge_trigger_value": 50.0, "hedge_scale": 0.15, "max_strategy_trades_per_cycle": 12},
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
            intent = OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="buy",
                shares=5.0,
                reference_price=features.price,
                category="grid",
                reason="test immediate fill",
                priority=10,
            )
            return DecisionOutcome(selected=intent, candidates=[intent])

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
            intent = OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="buy",
                shares=5.0,
                reference_price=features.price,
                category="grid",
                reason="test pending confirm",
                priority=10,
            )
            return DecisionOutcome(selected=intent, candidates=[intent])

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


def test_fixed_mode_keeps_curve_but_resets_betting_cash_per_cycle() -> None:
    engine = ReplayEngine(build_config(), starting_cash=100.0, capital_reset_mode="fixed")
    t0 = datetime(2026, 3, 20, 12, 0, 0)

    # cycle-a: 买入 up@0.4，winner=up，预期正收益。
    engine.state_engine.apply_market_trade(
        TradeEvent("BTC", "cycle-a", t0, price=0.8, shares=10, outcome="up", action="buy")
    )
    fill_a = FillEvent("BTC", "cycle-a", t0 + timedelta(seconds=5), price=0.4, shares=10, outcome="up", action="buy")
    engine.state_engine.apply_strategy_fill(fill_a)
    engine.account.apply_fill(fill_a)
    row_a = engine.finalize_pending_cycle()
    assert row_a is not None
    pnl_a = float(row_a["cycle_net_profit"])
    assert round(float(row_a["account_cash"]), 3) == round(100.0 + pnl_a, 3)
    # fixed 模式下，下一周期下注本金应回到 starting_cash。
    assert round(engine.account.cash, 3) == 100.0

    # cycle-b: 买入 up@0.4，winner=down，预期负收益；资金曲线应在上个周期基础上继续累计。
    engine.state_engine.apply_market_trade(
        TradeEvent("BTC", "cycle-b", t0 + timedelta(minutes=5), price=0.2, shares=10, outcome="up", action="buy")
    )
    fill_b = FillEvent(
        "BTC",
        "cycle-b",
        t0 + timedelta(minutes=5, seconds=5),
        price=0.4,
        shares=10,
        outcome="up",
        action="buy",
    )
    engine.state_engine.apply_strategy_fill(fill_b)
    engine.account.apply_fill(fill_b)
    row_b = engine.finalize_pending_cycle()
    assert row_b is not None
    pnl_b = float(row_b["cycle_net_profit"])
    expected_curve_cash = 100.0 + pnl_a + pnl_b
    assert round(float(row_b["account_cash"]), 3) == round(expected_curve_cash, 3)
    # 每周期投注本金仍保持固定值。
    assert round(engine.account.cash, 3) == 100.0


def test_buy_fill_frequency_limit_blocks_same_direction_within_one_second() -> None:
    base = build_config().to_dict()
    base["execution"]["min_seconds_between_orders"] = 0.0
    base["execution"]["max_same_direction_buy_fills_per_second"] = 1
    engine = ReplayEngine(StrategyConfig(raw=base))

    class StubRouter:
        def route(self, features, strategy_trades):
            intent = OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="buy",
                shares=5.0,
                reference_price=features.price,
                category="grid",
                reason="test buy fill rate limit",
                priority=10,
            )
            return DecisionOutcome(selected=intent, candidates=[intent])

    engine.router = StubRouter()
    t0 = datetime(2026, 3, 20, 12, 0, 0)
    first = TradeEvent("BTC", "cycle-a", t0, price=0.45, shares=10, outcome="up", action="buy")
    second = TradeEvent("BTC", "cycle-a", t0 + timedelta(milliseconds=500), price=0.451, shares=9, outcome="up", action="buy")

    first_step = engine.process_event(first)
    second_step = engine.process_event(second)

    assert first_step.decision_row["executed"] is True
    assert second_step.decision_row["executed"] is False
    assert second_step.decision_row["risk_status"] == "blocked"
    assert "同方向买入成交限频" in str(second_step.decision_row["risk_reason"])
