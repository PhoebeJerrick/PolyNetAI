from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.cycle_windows import cycle_seconds_remaining, rule_disabled_in_cycle_tail
from polynet_ai.strategy.entry_rules import grid_entries, mean_reversion_entries
from polynet_ai.strategy.exit_rules import grid_exits, mean_reversion_exits, take_profit_exits
from polynet_ai.strategy.spec import StrategyConfig

_BASE = dict(
    market_id="m",
    cycle_id="c",
    timestamp=datetime(2026, 1, 1, 0, 0, 0),
    price=0.5,
    is_last_minute=False,
    trend_bias=None,
    trend_strength=0.0,
    net_direction="平衡",
    net_position=0.0,
    net_position_value=0.0,
    up_held=10.0,
    down_held=10.0,
    up_avg_price=0.5,
    down_avg_price=0.5,
    up_deviation=0.3,
    down_deviation=-0.3,
    volatility=0.1,
    volatility_ratio=0.1,
    price_percentile=0.95,
    realized_pnl=0.0,
    unrealized_up_pnl=1.0,
    unrealized_down_pnl=1.0,
    cycle_net_profit=0.0,
    opening_vs_last_move=0.0,
    confidence_proxy=0.5,
    market_regime="range",
    strategy_trades=0,
    market_trades=0,
    up_last_price=0.6,
    down_last_price=0.4,
    up_market_vwap=0.5,
    down_market_vwap=0.5,
    up_market_n=2,
    down_market_n=2,
    up_market_high=0.7,
    up_market_low=0.3,
    down_market_high=0.7,
    down_market_low=0.3,
    tape_low=0.3,
    tape_high=0.7,
)


def _cfg() -> StrategyConfig:
    return StrategyConfig(
        raw={
            "cycle": {"cycle_seconds": 300},
            "order_sizing": {"base_order_size": 5.0, "volatility_order_scale": 0.0},
            "exposure": {"max_grid_net_position": 20.0},
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "grid": {
                "grid_low_percentile": 0.25,
                "grid_high_percentile": 0.75,
                "disable_within_seconds_before_end": 30,
            },
            "mean_reversion": {
                "up_buy_deviation": 0.1,
                "down_buy_deviation": 0.1,
                "mean_reversion_sell_up_deviation": 0.2,
                "mean_reversion_sell_down_deviation": 0.2,
                "deviation_scale": 45.0,
                "disable_within_seconds_before_end": 80,
            },
            "priorities": {"grid": 60, "mean_reversion": 70},
        }
    )


def test_cycle_seconds_remaining() -> None:
    cfg = _cfg()
    f = FeatureSnapshot(**_BASE, cycle_elapsed_seconds=250.0)
    assert cycle_seconds_remaining(f, cfg) == 50.0


def test_rule_disabled_in_cycle_tail_respects_threshold() -> None:
    cfg = _cfg()
    f_ok = FeatureSnapshot(**_BASE, cycle_elapsed_seconds=250.0)
    assert not rule_disabled_in_cycle_tail(f_ok, cfg, "grid")
    f_tail = FeatureSnapshot(**_BASE, cycle_elapsed_seconds=275.0)
    assert rule_disabled_in_cycle_tail(f_tail, cfg, "grid")


def test_missing_disable_key_never_tail_disables() -> None:
    cfg = StrategyConfig(raw={"cycle": {"cycle_seconds": 300}, "grid": {}})
    f = FeatureSnapshot(**_BASE, cycle_elapsed_seconds=299.0)
    assert not rule_disabled_in_cycle_tail(f, cfg, "grid")


def test_grid_entries_exits_suppressed_in_tail() -> None:
    cfg = _cfg()
    f = FeatureSnapshot(**{**_BASE, "cycle_elapsed_seconds": 275.0, "price_percentile": 0.96})
    assert grid_entries(f, cfg) == []
    assert grid_exits(f, cfg) == []


def test_mean_reversion_suppressed_when_more_than_last_minute_left() -> None:
    cfg = _cfg()
    # 220s elapsed -> 80s left: mean reversion tail matches, grid still active
    f = FeatureSnapshot(**{**_BASE, "cycle_elapsed_seconds": 220.0})
    assert mean_reversion_entries(f, cfg) == []
    assert mean_reversion_exits(f, cfg) == []


def test_mean_reversion_down_sell_triggers_on_positive_deviation() -> None:
    cfg = _cfg()
    f = FeatureSnapshot(
        **{
            **_BASE,
            "cycle_elapsed_seconds": 100.0,
            "up_held": 0.0,
            "down_held": 10.0,
            "down_deviation": 0.25,
        }
    )
    intents = mean_reversion_exits(f, cfg)
    assert any(intent.outcome == "down" and intent.action == "sell" for intent in intents)


def test_mean_reversion_down_sell_not_triggered_on_negative_deviation() -> None:
    cfg = _cfg()
    f = FeatureSnapshot(
        **{
            **_BASE,
            "cycle_elapsed_seconds": 100.0,
            "up_held": 0.0,
            "down_held": 10.0,
            "down_deviation": -0.25,
        }
    )
    intents = mean_reversion_exits(f, cfg)
    assert not any(intent.outcome == "down" and intent.action == "sell" for intent in intents)


def test_take_profit_down_sell_triggers_on_positive_deviation() -> None:
    cfg = _cfg()
    f = FeatureSnapshot(
        **{
            **_BASE,
            "cycle_elapsed_seconds": 100.0,
            "up_held": 0.0,
            "down_held": 10.0,
            "down_deviation": 0.25,
            "unrealized_down_pnl": 1.0,
        }
    )
    intents = take_profit_exits(f, cfg)
    assert any(intent.outcome == "down" and intent.action == "sell" for intent in intents)


def test_take_profit_down_sell_not_triggered_on_negative_deviation() -> None:
    cfg = _cfg()
    f = FeatureSnapshot(
        **{
            **_BASE,
            "cycle_elapsed_seconds": 100.0,
            "up_held": 0.0,
            "down_held": 10.0,
            "down_deviation": -0.25,
            "unrealized_down_pnl": 1.0,
        }
    )
    intents = take_profit_exits(f, cfg)
    assert not any(intent.outcome == "down" and intent.action == "sell" for intent in intents)
