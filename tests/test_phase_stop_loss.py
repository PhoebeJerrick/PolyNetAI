"""Phase 1 tests: 阶段性止损阈值（A+C联合方案）

验证 stop_loss_exits 在不同周期阶段使用不同的止损百分比阈值：
- Phase 1 (0-70s):   3.0%  建仓阶段，容忍更大波动
- Phase 2 (70-160s): 2.0%  保护利润
- Phase 3 (160-240s):2.5%  加仓阶段，适度容忍
- Phase 4 (240-290s):1.5%  临近结束，快速止损
"""
from __future__ import annotations

from datetime import datetime

import pytest

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.exit_rules import stop_loss_exits
from polynet_ai.strategy.spec import StrategyConfig

_BASE = dict(
    market_id="m",
    cycle_id="c",
    timestamp=datetime(2026, 1, 1, 0, 0, 0),
    price=0.5,
    is_last_minute=False,
    trend_bias=None,
    trend_strength=0.0,
    net_direction="Up",
    net_position=10.0,
    net_position_value=5.0,
    up_held=10.0,
    down_held=0.0,
    up_avg_price=0.50,
    down_avg_price=0.0,
    up_deviation=0.0,
    down_deviation=0.0,
    volatility=0.05,
    volatility_ratio=0.1,
    price_percentile=0.5,
    realized_pnl=0.0,
    unrealized_up_pnl=-0.5,
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
)


def _cfg() -> StrategyConfig:
    """策略配置：包含阶段性止损阈值"""
    return StrategyConfig(
        raw={
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "stop_loss": {
                "phase_1_stop_loss_pct": 0.03,
                "phase_2_stop_loss_pct": 0.02,
                "phase_3_stop_loss_pct": 0.025,
                "phase_4_stop_loss_pct": 0.015,
                "high_vol_stop_loss_pct": 0.01,
                "stop_loss_cycle_loss": 18.0,
                "stop_loss_fraction": 0.5,
                "stop_loss_pct": 0.02,
            },
            "priorities": {"stop_loss": 30},
        }
    )


def _cfg_no_phase() -> StrategyConfig:
    """不含阶段性配置的策略（测试 fallback 到 stop_loss_pct）"""
    return StrategyConfig(
        raw={
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "stop_loss": {
                "high_vol_stop_loss_pct": 0.01,
                "stop_loss_cycle_loss": 18.0,
                "stop_loss_fraction": 0.5,
                "stop_loss_pct": 0.02,
            },
            "priorities": {"stop_loss": 30},
        }
    )


def _make_features(cycle_elapsed: float, up_avg_price: float, up_last_price: float, **overrides) -> FeatureSnapshot:
    """构造 FeatureSnapshot，指定周期时间和 UP 侧的均价/市价以控制亏损百分比"""
    vals = {
        **_BASE,
        "cycle_elapsed_seconds": cycle_elapsed,
        "up_avg_price": up_avg_price,
        "up_last_price": up_last_price,
    }
    vals.update(overrides)
    return FeatureSnapshot(**vals)


# ─── Phase 1 (0-70s): 止损阈值 3.0% ───


class TestPhase1StopLoss:
    """第一阶段（0-70s）：止损阈值 3.0%"""

    def test_loss_below_phase1_threshold_no_trigger(self):
        """亏损 2.5% < 3.0%，Phase 1 不触发止损"""
        # up_avg_price=0.50, up_last_price=0.4875 → 亏损 2.5%
        f = _make_features(30.0, up_avg_price=0.50, up_last_price=0.4875)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_loss_at_phase1_threshold_triggers(self):
        """亏损 3.0% = 3.0%，Phase 1 触发止损"""
        # up_avg_price=0.50, up_last_price=0.485 → 亏损 3.0%
        f = _make_features(30.0, up_avg_price=0.50, up_last_price=0.485)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].outcome == "up"
        assert result[0].action == "sell"

    def test_loss_above_phase1_threshold_triggers(self):
        """亏损 4.0% > 3.0%，Phase 1 触发止损"""
        f = _make_features(30.0, up_avg_price=0.50, up_last_price=0.48)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    def test_phase1_boundary_70s(self):
        """边界：70s 仍属于 Phase 1"""
        f = _make_features(70.0, up_avg_price=0.50, up_last_price=0.4875)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0  # 2.5% < 3.0%


# ─── Phase 2 (70-160s): 止损阈值 2.0% ───


class TestPhase2StopLoss:
    """第二阶段（70-160s）：止损阈值 2.0%"""

    def test_loss_below_phase2_threshold_no_trigger(self):
        """亏损 1.5% < 2.0%，Phase 2 不触发"""
        f = _make_features(100.0, up_avg_price=0.50, up_last_price=0.4925)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_loss_at_phase2_threshold_triggers(self):
        """亏损 2.0% = 2.0%，Phase 2 触发"""
        f = _make_features(100.0, up_avg_price=0.50, up_last_price=0.49)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    def test_same_loss_no_trigger_in_phase1_but_triggers_in_phase2(self):
        """同样 2.5% 亏损：Phase 1(3%) 不触发，Phase 2(2%) 触发"""
        f1 = _make_features(30.0, up_avg_price=0.50, up_last_price=0.4875)
        assert len(stop_loss_exits(f1, _cfg())) == 0

        f2 = _make_features(100.0, up_avg_price=0.50, up_last_price=0.4875)
        assert len(stop_loss_exits(f2, _cfg())) == 1


# ─── Phase 3 (160-240s): 止损阈值 2.5% ───


class TestPhase3StopLoss:
    """第三阶段（160-240s）：止损阈值 2.5%"""

    def test_loss_below_phase3_threshold_no_trigger(self):
        """亏损 2.0% < 2.5%，Phase 3 不触发"""
        f = _make_features(200.0, up_avg_price=0.50, up_last_price=0.49)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_loss_at_phase3_threshold_triggers(self):
        """亏损 2.5% = 2.5%，Phase 3 触发"""
        f = _make_features(200.0, up_avg_price=0.50, up_last_price=0.4875)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1


# ─── Phase 4 (240-290s): 止损阈值 1.5% ───


class TestPhase4StopLoss:
    """第四阶段（240-290s）：止损阈值 1.5%"""

    def test_loss_below_phase4_threshold_no_trigger(self):
        """亏损 1.0% < 1.5%，Phase 4 不触发"""
        f = _make_features(260.0, up_avg_price=0.50, up_last_price=0.495)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_loss_at_phase4_threshold_triggers(self):
        """亏损 1.5% = 1.5%，Phase 4 触发"""
        # up_avg_price=0.50, up_last_price=0.4925 → 亏损 1.5%
        f = _make_features(260.0, up_avg_price=0.50, up_last_price=0.4925)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    def test_phase4_tightest_stop(self):
        """Phase 4 是最紧的止损：同样 2% 亏损在 Phase 2 刚好触发，在 Phase 4 也触发"""
        f = _make_features(260.0, up_avg_price=0.50, up_last_price=0.49)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1


# ─── 高波动止损覆盖（全阶段统一） ───


class TestHighVolatilityOverride:
    """高波动率时使用统一的 1% 止损阈值，覆盖阶段性阈值"""

    def test_high_vol_overrides_phase1(self):
        """Phase 1 正常阈值 3%，但高波动时用 1%"""
        # 1.5% 亏损在 Phase 1 (3%) 正常不触发，但高波动 (1%) 触发
        f = _make_features(
            30.0,
            up_avg_price=0.50,
            up_last_price=0.4925,
            volatility_ratio=2.0,
        )
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    def test_high_vol_overrides_phase3(self):
        """Phase 3 正常阈值 2.5%，但高波动时用 1%"""
        f = _make_features(
            200.0,
            up_avg_price=0.50,
            up_last_price=0.4925,
            volatility_ratio=2.0,
        )
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    def test_normal_vol_uses_phase_threshold(self):
        """正常波动率使用阶段性阈值"""
        # 1.5% 亏损 + Phase 1 (3%) + 正常波动 → 不触发
        f = _make_features(
            30.0,
            up_avg_price=0.50,
            up_last_price=0.4925,
            volatility_ratio=0.5,
        )
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0


# ─── 周期累计亏损止损（全阶段统一） ───


class TestCycleLossStopLoss:
    """周期累计亏损止损不受阶段影响"""

    def test_cycle_loss_triggers_regardless_of_phase(self):
        """周期亏损超阈值时，无论哪个阶段都触发"""
        for elapsed in [30.0, 100.0, 200.0, 260.0]:
            f = _make_features(
                elapsed,
                up_avg_price=0.50,
                up_last_price=0.49,
                cycle_net_profit=-20.0,
                unrealized_up_pnl=-2.0,
            )
            result = stop_loss_exits(f, _cfg())
            assert len(result) >= 1, f"Phase at {elapsed}s should trigger cycle loss stop"
            assert result[0].category == "stop_loss"

    def test_cycle_loss_early_returns_before_pct_check(self):
        """周期累计亏损触发后，不再执行百分比止损检查（提前返回）"""
        # 周期亏损严重，但 UP 方向浮盈（不亏损）→ 不触发 UP 止损
        f = _make_features(
            30.0,
            up_avg_price=0.50,
            up_last_price=0.55,  # UP 盈利
            cycle_net_profit=-20.0,
            unrealized_up_pnl=0.5,
            unrealized_down_pnl=-3.0,
            down_held=10.0,
            down_avg_price=0.50,
            down_last_price=0.45,
        )
        result = stop_loss_exits(f, _cfg())
        # 只有 DOWN 侧亏损会被止损，UP 侧不会
        outcomes = [r.outcome for r in result]
        assert "up" not in outcomes
        assert "down" in outcomes


# ─── Fallback: 无阶段配置时回退到 stop_loss_pct ───


class TestFallbackStopLoss:
    """未配置阶段性阈值时，回退到 stop_loss_pct (2%)"""

    def test_fallback_uses_default_pct(self):
        """无阶段配置时，2% 亏损触发止损（使用 fallback 的 stop_loss_pct=0.02）"""
        f = _make_features(30.0, up_avg_price=0.50, up_last_price=0.49)
        result = stop_loss_exits(f, _cfg_no_phase())
        assert len(result) == 1

    def test_fallback_below_threshold_no_trigger(self):
        """无阶段配置时，1.5% 亏损不触发"""
        f = _make_features(30.0, up_avg_price=0.50, up_last_price=0.4925)
        result = stop_loss_exits(f, _cfg_no_phase())
        assert len(result) == 0


# ─── DOWN 方向止损 ───


class TestDownSideStopLoss:
    """DOWN 方向同样使用阶段性止损阈值"""

    def test_down_phase1_no_trigger_below_threshold(self):
        """DOWN 侧 2.5% 亏损 < Phase 1 (3%) 不触发"""
        f = _make_features(
            30.0,
            up_avg_price=0.0,
            up_last_price=0.0,
            up_held=0.0,
            down_held=10.0,
            down_avg_price=0.50,
            down_last_price=0.4875,
        )
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_down_phase4_triggers(self):
        """DOWN 侧 2% 亏损 > Phase 4 (1.5%) 触发"""
        f = _make_features(
            260.0,
            up_avg_price=0.0,
            up_last_price=0.0,
            up_held=0.0,
            down_held=10.0,
            down_avg_price=0.50,
            down_last_price=0.49,
        )
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].outcome == "down"


# ─── 止损份额验证 ───


class TestStopLossShares:
    """止损时卖出 50% 持仓"""

    def test_sells_half_position(self):
        """止损卖出 50% 持仓（stop_loss_fraction=0.5）"""
        f = _make_features(100.0, up_avg_price=0.50, up_last_price=0.49, up_held=20.0)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].shares == pytest.approx(10.0)
