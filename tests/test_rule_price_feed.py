from __future__ import annotations

from datetime import datetime, timedelta

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.features import snapshot_with_effective_price
from polynet_ai.strategy.router import StrategyRouter
from polynet_ai.strategy.spec import StrategyConfig


def _feature_snapshot(**overrides: object) -> FeatureSnapshot:
    data: dict[str, object] = {
        "market_id": "m",
        "cycle_id": "c1",
        "timestamp": datetime(2026, 1, 1, 12, 0, 0),
        "price": 0.50,
        "cycle_elapsed_seconds": 120.0,
        "is_last_minute": False,
        "trend_bias": "up",
        "trend_strength": 1.0,
        "net_direction": "空仓",
        "net_position": 0.0,
        "net_position_value": 0.0,
        "up_held": 0.0,
        "down_held": 0.0,
        "up_avg_price": 0.50,
        "down_avg_price": 0.50,
        "up_deviation": 0.0,
        "down_deviation": 0.0,
        "volatility": 0.2,
        "volatility_ratio": 0.01,
        "price_percentile": 0.5,
        "realized_pnl": 0.0,
        "unrealized_up_pnl": 0.0,
        "unrealized_down_pnl": 0.0,
        "cycle_net_profit": 0.0,
        "opening_vs_last_move": 0.0,
        "confidence_proxy": 0.5,
        "market_regime": "trend",
        "strategy_trades": 1,
        "market_trades": 10,
        "up_last_price": 0.50,
        "down_last_price": 0.50,
        "up_market_vwap": 0.50,
        "down_market_vwap": 0.50,
        "up_market_n": 1,
        "down_market_n": 1,
        "up_market_high": 0.50,
        "up_market_low": 0.50,
        "down_market_high": 0.50,
        "down_market_low": 0.50,
        "tape_low": 0.0,
        "tape_high": 1.0,
    }
    data.update(overrides)
    return FeatureSnapshot(**data)


def test_snapshot_with_effective_price_recomputes_deviation() -> None:
    base = _feature_snapshot(price=0.55, opening_vs_last_move=0.05, up_deviation=0.1, down_deviation=0.1)
    adj = snapshot_with_effective_price(base, 0.60)
    assert adj.price == 0.60
    assert abs(adj.up_deviation - 0.2) < 1e-9
    # snapshot_with_effective_price 会把 effective_price 映射到对应方向的 last_price。
    assert abs(adj.up_last_price - 0.60) < 1e-9


def test_trend_rule_keeps_prior_feed_until_interval() -> None:
    cfg = StrategyConfig(
        raw={
            "priorities": {
                "last_minute": 100,
                "stop_loss": 100,
                "hedge": 100,
                "take_profit": 100,
                "opening": 100,
                "grid": 100,
                "mean_reversion": 100,
                "trend": 1,
            },
            "rule_price_feed": {
                "last_minute": 0.0,
                "entries": {
                    "opening": 0.0,
                    "hedge": 0.0,
                    "grid": 0.0,
                    "mean_reversion": 0.0,
                    "trend": 60.0,
                },
                "exits": {
                    "stop_loss": 0.0,
                    "hedge": 0.0,
                    "take_profit": 0.0,
                    "grid": 0.0,
                    "mean_reversion": 0.0,
                },
            },
            "trend": {"min_trend_strength": 0.0, "trend_price_edge": 0.02, "trend_scale": 0.0},
            "order_sizing": {"base_order_size": 5.0, "volatility_order_scale": 0.0},
        }
    )
    router = StrategyRouter(cfg)
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    f1 = _feature_snapshot(
        timestamp=t0,
        price=0.503,
        up_deviation=0.006,
        down_deviation=0.006,
        opening_vs_last_move=0.003,
    )
    f2 = _feature_snapshot(
        timestamp=t0 + timedelta(seconds=5),
        price=0.60,
        up_deviation=0.20,
        down_deviation=0.20,
        opening_vs_last_move=0.10,
    )
    d1 = router.route(f1)
    d2 = router.route(f2)
    assert not any(c.category == "trend" for c in d1.candidates)
    assert not any(c.category == "trend" for c in d2.candidates)

    f3 = _feature_snapshot(
        timestamp=t0 + timedelta(seconds=65),
        price=0.60,
        up_deviation=0.20,
        down_deviation=0.20,
        opening_vs_last_move=0.10,
    )
    d3 = router.route(f3)
    assert any(c.category == "trend" for c in d3.candidates)


def test_opening_rule_uses_live_quotes_inside_window() -> None:
    cfg = StrategyConfig(
        raw={
            "cycle": {"cycle_seconds": 300, "last_minute_seconds": 30},
            "priorities": {
                "last_minute": 100,
                "stop_loss": 100,
                "hedge": 100,
                "take_profit": 100,
                "opening": 1,
                "grid": 100,
                "mean_reversion": 100,
                "trend": 100,
            },
            "rule_price_feed": {
                "last_minute": 0.0,
                "entries": {
                    "opening": 0.0,
                    "hedge": 0.0,
                    "grid": 0.0,
                    "mean_reversion": 0.0,
                    "trend": 0.0,
                },
                "exits": {
                    "stop_loss": 0.0,
                    "hedge": 0.0,
                    "take_profit": 0.0,
                    "grid": 0.0,
                    "mean_reversion": 0.0,
                },
            },
            "opening_entry": {
                "enabled": True,
                "window_seconds": 30.0,
                "vwap_epsilon": 0.01,
                "range_low_fraction": 0.35,
                "min_range_width": 0.02,
                "min_market_trades": 3,
                "infer_missing_with_binary_complement": True,
            },
            "trend": {"min_trend_strength": 1.0, "trend_price_edge": 1.0, "trend_scale": 0.0},
            "mean_reversion": {"enabled": False},
            "order_sizing": {"base_order_size": 5.0, "volatility_order_scale": 0.0},
        }
    )
    router = StrategyRouter(cfg)
    t0 = datetime(2026, 1, 1, 12, 0, 25, 368000)

    first = _feature_snapshot(
        timestamp=t0,
        price=0.63,
        cycle_elapsed_seconds=25.368,
        strategy_trades=0,
        trend_bias=None,
        trend_strength=0.0,
        market_regime="range",
        opening_vs_last_move=0.0,
        up_last_price=0.63,
        down_last_price=0.37,
        up_market_vwap=0.63,
        down_market_vwap=0.0,
        up_market_n=1,
        down_market_n=0,
        up_market_high=0.63,
        up_market_low=0.63,
    )
    second = _feature_snapshot(
        timestamp=t0 + timedelta(milliseconds=63),
        price=0.34,
        cycle_elapsed_seconds=25.431,
        strategy_trades=0,
        trend_bias=None,
        trend_strength=0.0,
        market_regime="range",
        opening_vs_last_move=-0.29,
        up_last_price=0.66,
        down_last_price=0.34,
        up_market_vwap=(0.63 + 0.65 + 0.66) / 3,
        down_market_vwap=(0.35 + 0.37 + 0.34) / 3,
        up_market_n=3,
        down_market_n=3,
        up_market_high=0.66,
        up_market_low=0.63,
        down_market_high=0.37,
        down_market_low=0.34,
    )

    assert router.route(first).selected is None

    decision = router.route(second)

    assert decision.selected is not None
    assert decision.selected.category == "opening"
    assert decision.selected.outcome == "down"
