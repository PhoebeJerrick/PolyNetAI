from __future__ import annotations

from datetime import datetime, timedelta

from polynet_ai.domain.models import TradeEvent
from polynet_ai.engine.live import LivePaperRunner
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


def test_live_runner_generates_snapshots_and_cycle_output() -> None:
    t0 = datetime(2026, 3, 20, 12, 0, 0)
    events = [
        TradeEvent("BTC", "cycle-a", t0, price=0.45, shares=10, outcome="up", action="buy"),
        TradeEvent("BTC", "cycle-a", t0 + timedelta(seconds=30), price=0.50, shares=8, outcome="up", action="buy"),
        TradeEvent("BTC", "cycle-a", t0 + timedelta(seconds=60), price=0.58, shares=7, outcome="up", action="buy"),
        TradeEvent("BTC", "cycle-b", t0 + timedelta(seconds=360), price=0.40, shares=6, outcome="down", action="buy"),
    ]
    sleeps: list[float] = []
    runner = LivePaperRunner(ReplayEngine(build_config()))
    result = runner.run(
        events,
        pace_factor=10.0,
        max_sleep_seconds=0.01,
        status_every=0,
        sleep_fn=sleeps.append,
    )
    assert len(result.snapshot_df) == len(events)
    assert len(result.replay_result.cycle_df) == 2
    assert "account_cash" in result.snapshot_df.columns
    assert all(delay <= 0.01 for delay in sleeps)
