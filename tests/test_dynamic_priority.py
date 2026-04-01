"""Phase 2 tests: 动态优先级调整（A+C联合方案 — 方案C）

验证 calculate_position_percentage 和 adjust_priority_by_phase：
- Phase 1: 仓位 < 65% → opening/mean_reversion buy 优先级 -15
- Phase 2: 仓位 > 50% → grid/take_profit sell 优先级 -15
- Phase 3: 仓位 < 85% → trend buy -25, grid buy -15
- Phase 4: 不调整
"""
from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.cycle_windows import calculate_position_percentage
from polynet_ai.strategy.dynamic_priority import adjust_priority_by_phase
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
    up_held=0.0,
    down_held=0.0,
    up_avg_price=0.0,
    down_avg_price=0.0,
    up_deviation=0.0,
    down_deviation=0.0,
    volatility=0.05,
    volatility_ratio=0.1,
    price_percentile=0.5,
    realized_pnl=0.0,
    unrealized_up_pnl=0.0,
    unrealized_down_pnl=0.0,
    cycle_net_profit=0.0,
    opening_vs_last_move=0.0,
    confidence_proxy=0.5,
    market_regime="range",
    strategy_trades=0,
    market_trades=5,
    up_last_price=0.50,
    down_last_price=0.50,
    up_market_vwap=0.5,
    down_market_vwap=0.5,
    up_market_n=3,
    down_market_n=3,
    up_market_high=0.55,
    up_market_low=0.45,
    down_market_high=0.55,
    down_market_low=0.45,
    tape_low=0.45,
    tape_high=0.55,
    cycle_elapsed_seconds=0.0,
)


def _cfg() -> StrategyConfig:
    return StrategyConfig(
        raw={
            "position": {"max_position_value": 85.0},
            "dynamic_priority": {
                "phase_1_position_threshold": 0.65,
                "phase_1_boost": 15,
                "phase_2_position_threshold": 0.50,
                "phase_2_boost": 15,
                "phase_3_position_threshold": 0.85,
                "phase_3_trend_boost": 25,
                "phase_3_grid_boost": 15,
            },
            "priorities": {
                "opening": 52,
                "mean_reversion": 70,
                "grid": 60,
                "take_profit": 50,
                "trend": 80,
                "stop_loss": 30,
            },
        }
    )


def _make_features(**overrides) -> FeatureSnapshot:
    vals = {**_BASE}
    vals.update(overrides)
    return FeatureSnapshot(**vals)


def _make_intent(category: str, action: str, priority: int) -> OrderIntent:
    return OrderIntent(
        market_id="m",
        cycle_id="c",
        outcome="up",
        action=action,
        shares=10.0,
        reference_price=0.5,
        category=category,
        reason="test",
        priority=priority,
    )


# ─── calculate_position_percentage ───


class TestCalculatePositionPercentage:
    def test_empty_position(self):
        f = _make_features(up_held=0.0, down_held=0.0)
        assert calculate_position_percentage(f, _cfg()) == 0.0

    def test_full_position(self):
        # 42.5 + 42.5 = 85 → 100%
        f = _make_features(up_held=85.0, up_avg_price=0.50, down_held=85.0, down_avg_price=0.50)
        assert abs(calculate_position_percentage(f, _cfg()) - 1.0) < 1e-9

    def test_partial_position(self):
        # up: 50 * 0.5 = 25, down: 30 * 0.5 = 15 → total 40 / 85 ≈ 0.4706
        f = _make_features(up_held=50.0, up_avg_price=0.50, down_held=30.0, down_avg_price=0.50)
        result = calculate_position_percentage(f, _cfg())
        assert abs(result - 40.0 / 85.0) < 1e-9

    def test_over_target(self):
        # 100 * 0.5 + 100 * 0.5 = 100 / 85 > 1.0
        f = _make_features(up_held=100.0, up_avg_price=0.50, down_held=100.0, down_avg_price=0.50)
        assert calculate_position_percentage(f, _cfg()) > 1.0

    def test_zero_max_value_returns_zero(self):
        cfg = StrategyConfig(raw={"position": {"max_position_value": 0}})
        f = _make_features(up_held=10.0, up_avg_price=0.50)
        assert calculate_position_percentage(f, cfg) == 0.0

    def test_different_prices(self):
        # up: 20 * 0.6 = 12, down: 30 * 0.4 = 12 → total 24 / 85
        f = _make_features(up_held=20.0, up_avg_price=0.60, down_held=30.0, down_avg_price=0.40)
        expected = 24.0 / 85.0
        assert abs(calculate_position_percentage(f, _cfg()) - expected) < 1e-9


# ─── Phase 1 (0-70s): opening/mean_reversion buy 优先级提升 ───


class TestPhase1PriorityBoost:
    """Phase 1: 仓位 < 65% 时，opening/mean_reversion buy 优先级 -15"""

    def test_opening_buy_boosted_when_low_position(self):
        # 仓位 < 65%，Phase 1
        f = _make_features(cycle_elapsed_seconds=30.0, up_held=10.0, up_avg_price=0.5)
        intent = _make_intent("opening", "buy", 52)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 52 - 15  # boosted

    def test_mean_reversion_buy_boosted_when_low_position(self):
        f = _make_features(cycle_elapsed_seconds=30.0, up_held=10.0, up_avg_price=0.5)
        intent = _make_intent("mean_reversion", "buy", 70)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 70 - 15

    def test_no_boost_when_position_above_threshold(self):
        # 仓位 >= 65%: 60 * 0.5 + 60 * 0.5 = 60 / 85 ≈ 70.6%
        f = _make_features(
            cycle_elapsed_seconds=30.0,
            up_held=60.0, up_avg_price=0.50, down_held=60.0, down_avg_price=0.50,
        )
        intent = _make_intent("opening", "buy", 52)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 52  # unchanged

    def test_sell_not_boosted_in_phase1(self):
        """Phase 1 不提升卖出规则"""
        f = _make_features(cycle_elapsed_seconds=30.0, up_held=10.0, up_avg_price=0.5)
        intent = _make_intent("opening", "sell", 52)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 52  # unchanged

    def test_trend_not_boosted_in_phase1(self):
        """Phase 1 不提升趋势加仓"""
        f = _make_features(cycle_elapsed_seconds=30.0, up_held=10.0, up_avg_price=0.5)
        intent = _make_intent("trend", "buy", 80)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 80  # unchanged


# ─── Phase 2 (70-160s): grid/take_profit sell 优先级提升 ───


class TestPhase2PriorityBoost:
    """Phase 2: 仓位 > 50% 时，grid/take_profit sell 优先级 -15"""

    def test_grid_sell_boosted_when_high_position(self):
        # 仓位 > 50%: 50 * 0.5 + 50 * 0.5 = 50 / 85 ≈ 58.8%
        f = _make_features(
            cycle_elapsed_seconds=100.0,
            up_held=50.0, up_avg_price=0.50, down_held=50.0, down_avg_price=0.50,
        )
        intent = _make_intent("grid", "sell", 60)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 60 - 15

    def test_take_profit_sell_boosted(self):
        f = _make_features(
            cycle_elapsed_seconds=100.0,
            up_held=50.0, up_avg_price=0.50, down_held=50.0, down_avg_price=0.50,
        )
        intent = _make_intent("take_profit", "sell", 50)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 50 - 15

    def test_no_boost_when_position_below_threshold(self):
        # 仓位 < 50%: 20 * 0.5 = 10 / 85 ≈ 11.8%
        f = _make_features(cycle_elapsed_seconds=100.0, up_held=20.0, up_avg_price=0.50)
        intent = _make_intent("grid", "sell", 60)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 60  # unchanged

    def test_buy_not_boosted_in_phase2(self):
        """Phase 2 不提升买入规则"""
        f = _make_features(
            cycle_elapsed_seconds=100.0,
            up_held=50.0, up_avg_price=0.50, down_held=50.0, down_avg_price=0.50,
        )
        intent = _make_intent("grid", "buy", 60)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 60  # unchanged


# ─── Phase 3 (160-240s): trend/grid buy 优先级提升 ───


class TestPhase3PriorityBoost:
    """Phase 3: 仓位 < 85% 时，trend buy -25, grid buy -15"""

    def test_trend_buy_boosted(self):
        # 仓位 < 85%
        f = _make_features(
            cycle_elapsed_seconds=200.0,
            up_held=30.0, up_avg_price=0.50, down_held=30.0, down_avg_price=0.50,
        )
        intent = _make_intent("trend", "buy", 80)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 80 - 25

    def test_grid_buy_boosted(self):
        f = _make_features(
            cycle_elapsed_seconds=200.0,
            up_held=30.0, up_avg_price=0.50, down_held=30.0, down_avg_price=0.50,
        )
        intent = _make_intent("grid", "buy", 60)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 60 - 15

    def test_no_boost_when_position_above_threshold(self):
        # 仓位 >= 85%: 90 * 0.5 + 90 * 0.5 = 90 / 85 > 1.0
        f = _make_features(
            cycle_elapsed_seconds=200.0,
            up_held=90.0, up_avg_price=0.50, down_held=90.0, down_avg_price=0.50,
        )
        intent = _make_intent("trend", "buy", 80)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 80  # unchanged

    def test_grid_sell_not_boosted_in_phase3(self):
        """Phase 3 不提升 grid 卖出"""
        f = _make_features(
            cycle_elapsed_seconds=200.0,
            up_held=30.0, up_avg_price=0.50, down_held=30.0, down_avg_price=0.50,
        )
        intent = _make_intent("grid", "sell", 60)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 60  # unchanged


# ─── Phase 4 (240-290s): 不调整 ───


class TestPhase4NoAdjustment:
    """Phase 4: 使用基础优先级，不做动态调整"""

    def test_opening_buy_not_adjusted(self):
        f = _make_features(cycle_elapsed_seconds=260.0, up_held=10.0, up_avg_price=0.5)
        intent = _make_intent("opening", "buy", 52)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 52

    def test_trend_buy_not_adjusted(self):
        f = _make_features(cycle_elapsed_seconds=260.0, up_held=10.0, up_avg_price=0.5)
        intent = _make_intent("trend", "buy", 80)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 80

    def test_grid_sell_not_adjusted(self):
        f = _make_features(
            cycle_elapsed_seconds=260.0,
            up_held=50.0, up_avg_price=0.50, down_held=50.0, down_avg_price=0.50,
        )
        intent = _make_intent("grid", "sell", 60)
        adjust_priority_by_phase(intent, f, _cfg())
        assert intent.priority == 60

    def test_stop_loss_never_adjusted(self):
        """止损规则在任何阶段都不调整"""
        for elapsed in [30.0, 100.0, 200.0, 260.0]:
            f = _make_features(cycle_elapsed_seconds=elapsed, up_held=10.0, up_avg_price=0.5)
            intent = _make_intent("stop_loss", "sell", 30)
            adjust_priority_by_phase(intent, f, _cfg())
            assert intent.priority == 30, f"stop_loss should never be adjusted at {elapsed}s"


# ─── 跨阶段排序效果验证 ───


class TestPriorityOrderingEffect:
    """验证动态优先级调整后的排序产生预期效果"""

    def test_phase1_opening_beats_take_profit(self):
        """Phase 1 低仓位：opening(52→37) 优先于 take_profit(50)"""
        f = _make_features(cycle_elapsed_seconds=30.0, up_held=10.0, up_avg_price=0.5)
        opening = _make_intent("opening", "buy", 52)
        tp = _make_intent("take_profit", "sell", 50)
        adjust_priority_by_phase(opening, f, _cfg())
        adjust_priority_by_phase(tp, f, _cfg())
        assert opening.priority < tp.priority

    def test_phase2_take_profit_beats_opening(self):
        """Phase 2 高仓位：take_profit(50→35) 优先于 opening(52)"""
        f = _make_features(
            cycle_elapsed_seconds=100.0,
            up_held=50.0, up_avg_price=0.50, down_held=50.0, down_avg_price=0.50,
        )
        opening = _make_intent("opening", "buy", 52)
        tp = _make_intent("take_profit", "sell", 50)
        adjust_priority_by_phase(opening, f, _cfg())
        adjust_priority_by_phase(tp, f, _cfg())
        assert tp.priority < opening.priority

    def test_phase3_trend_beats_grid_exit(self):
        """Phase 3 低仓位：trend(80→55) 优先于 grid sell(60)"""
        f = _make_features(
            cycle_elapsed_seconds=200.0,
            up_held=30.0, up_avg_price=0.50, down_held=30.0, down_avg_price=0.50,
        )
        trend = _make_intent("trend", "buy", 80)
        grid_sell = _make_intent("grid", "sell", 60)
        adjust_priority_by_phase(trend, f, _cfg())
        adjust_priority_by_phase(grid_sell, f, _cfg())
        assert trend.priority < grid_sell.priority
