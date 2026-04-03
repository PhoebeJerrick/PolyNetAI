from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.cycle_windows import grid_align_net_direction_for_phase
from polynet_ai.strategy.spec import StrategyConfig


def _feat(
    *,
    net_direction: str = "Down",
    up_last: float,
    down_last: float,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        market_id="m",
        cycle_id="c",
        timestamp=datetime(2026, 1, 1, 0, 0, 0),
        price=up_last,
        cycle_elapsed_seconds=200.0,
        is_last_minute=False,
        trend_bias=None,
        trend_strength=0.0,
        net_direction=net_direction,
        net_position=5.0,
        net_position_value=1.0,
        up_held=20.0,
        down_held=5.0,
        up_avg_price=0.5,
        down_avg_price=0.5,
        up_deviation=0.0,
        down_deviation=0.0,
        volatility=0.1,
        volatility_ratio=0.1,
        price_percentile=0.5,
        realized_pnl=0.0,
        unrealized_up_pnl=0.0,
        unrealized_down_pnl=0.0,
        cycle_net_profit=0.0,
        opening_vs_last_move=0.0,
        confidence_proxy=0.5,
        market_regime="trend",
        strategy_trades=2,
        market_trades=10,
        up_last_price=up_last,
        down_last_price=down_last,
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


def test_phase3_up_above_threshold_despite_net_direction_down() -> None:
    cfg = StrategyConfig(raw={"grid": {"phase_3_advantage_price_threshold": 0.55}})
    f = _feat(net_direction="Down", up_last=0.58, down_last=0.42)
    assert grid_align_net_direction_for_phase(f, cfg, 3) == "Up"


def test_phase4_neither_above_threshold_falls_back_to_higher_price() -> None:
    cfg = StrategyConfig(raw={"grid": {"phase_4_advantage_price_threshold": 0.65}})
    f = _feat(up_last=0.52, down_last=0.48)
    assert grid_align_net_direction_for_phase(f, cfg, 4) == "Up"


def test_phase1_uses_net_direction_not_price_rule() -> None:
    cfg = StrategyConfig(raw={"grid": {}})
    f = _feat(net_direction="Down", up_last=0.9, down_last=0.1)
    assert grid_align_net_direction_for_phase(f, cfg, 1) == "Down"
