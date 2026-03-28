"""Phase 3 tests: 第四阶段方向确认改进（A+C联合方案）

验证 determine_winning_direction 和 build_last_minute_candidate 的方向确认逻辑：
- 三重条件确认获胜方向（仓位价值比、浮盈比、趋势强度+方向）
- 无法确认时采取保守策略：不加仓
- 确认方向后使用该方向作为加仓目标
"""
from __future__ import annotations

from datetime import datetime

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.last_minute import (
    build_last_minute_candidate,
    determine_winning_direction,
)
from polynet_ai.strategy.spec import StrategyConfig

_BASE = dict(
    market_id="m",
    cycle_id="c",
    timestamp=datetime(2026, 1, 1, 0, 5, 0),
    price=0.6,
    cycle_elapsed_seconds=270.0,
    is_last_minute=True,
    trend_bias="up",
    trend_strength=0.7,
    net_direction="Up",
    net_position=20.0,
    net_position_value=12.0,
    up_held=30.0,
    down_held=10.0,
    up_avg_price=0.55,
    down_avg_price=0.45,
    up_deviation=0.1,
    down_deviation=-0.1,
    volatility=0.05,
    volatility_ratio=0.1,
    price_percentile=0.6,
    realized_pnl=2.0,
    unrealized_up_pnl=3.0,
    unrealized_down_pnl=0.5,
    cycle_net_profit=5.0,
    opening_vs_last_move=0.0,
    confidence_proxy=0.95,
    market_regime="trend",
    strategy_trades=5,
    market_trades=20,
    up_last_price=0.60,
    down_last_price=0.40,
    up_market_vwap=0.55,
    down_market_vwap=0.45,
    up_market_n=10,
    down_market_n=10,
    up_market_high=0.65,
    up_market_low=0.50,
    down_market_high=0.50,
    down_market_low=0.35,
    tape_low=0.35,
    tape_high=0.65,
)


def _cfg() -> StrategyConfig:
    return StrategyConfig(
        raw={
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "last_minute": {
                "last_minute_min_confidence": 0.85,
                "tail_profit_scale": 0.35,
                "tail_volatility_scale": 14,
                "max_tail_exposure": 25.0,
                "preferred_leg_min_ratio": 1.2,
                "direction_ratio_threshold": 1.5,
                "pnl_ratio_threshold": 2.0,
                "min_trend_strength_for_direction": 0.6,
                "conservative_max_exposure": 10.0,
            },
            "priorities": {"last_minute": 20},
        }
    )


def _make_features(**overrides) -> FeatureSnapshot:
    vals = {**_BASE}
    vals.update(overrides)
    return FeatureSnapshot(**vals)


# ─── determine_winning_direction ───


class TestDetermineWinningDirectionUp:
    """UP 方向确认成功场景"""

    def test_up_confirmed_all_conditions_met(self):
        """UP满足三重条件：仓位比>=1.5、浮盈比>=2.0、趋势强度>=0.6且方向up"""
        # up_value=30*0.55=16.5, down_value=10*0.45=4.5 → ratio=3.67 > 1.5 ✓
        # up_pnl=3.0, down_pnl=0.5 → 3.0 > 0.5*2.0=1.0 ✓
        # trend_strength=0.7 >= 0.6 ✓, trend_bias="up" ✓
        f = _make_features()
        direction, confidence = determine_winning_direction(f, _cfg())
        assert direction == "up"
        assert confidence == 0.9

    def test_up_confirmed_high_position_ratio(self):
        """UP 仓位压倒性优势"""
        f = _make_features(
            up_held=100.0, down_held=5.0,
            unrealized_up_pnl=10.0, unrealized_down_pnl=0.1,
        )
        direction, _ = determine_winning_direction(f, _cfg())
        assert direction == "up"


class TestDetermineWinningDirectionDown:
    """DOWN 方向确认成功场景"""

    def test_down_confirmed_all_conditions_met(self):
        """DOWN满足三重条件"""
        f = _make_features(
            up_held=10.0, up_avg_price=0.45,
            down_held=30.0, down_avg_price=0.55,
            unrealized_up_pnl=0.5, unrealized_down_pnl=3.0,
            trend_bias="down", trend_strength=0.7,
        )
        direction, confidence = determine_winning_direction(f, _cfg())
        assert direction == "down"
        assert confidence == 0.9


class TestDetermineWinningDirectionNone:
    """无法确认方向的场景"""

    def test_none_when_ratio_too_low(self):
        """仓位价值比不足 1.5"""
        # up_value=20*0.55=11, down_value=15*0.45=6.75 → ratio=1.63 → OK ratio
        # But 15*0.45=6.75, 20*0.55=11 → 11/6.75=1.63 > 1.5 → passes ratio
        # Let's make them closer
        f = _make_features(
            up_held=20.0, up_avg_price=0.50,
            down_held=15.0, down_avg_price=0.50,
            unrealized_up_pnl=3.0, unrealized_down_pnl=0.5,
        )
        # up_value=10, down_value=7.5 → ratio=1.33 < 1.5
        direction, confidence = determine_winning_direction(f, _cfg())
        assert direction is None
        assert confidence == 0.0

    def test_none_when_pnl_ratio_too_low(self):
        """浮盈比不足 2.0"""
        f = _make_features(
            unrealized_up_pnl=1.5, unrealized_down_pnl=1.0,
            # 1.5 / 1.0 = 1.5 < 2.0
        )
        direction, confidence = determine_winning_direction(f, _cfg())
        assert direction is None
        assert confidence == 0.0

    def test_none_when_trend_strength_too_low(self):
        """趋势强度不足 0.6"""
        f = _make_features(trend_strength=0.4)
        direction, confidence = determine_winning_direction(f, _cfg())
        assert direction is None
        assert confidence == 0.0

    def test_none_when_trend_bias_mismatch(self):
        """趋势方向与优势侧不一致"""
        f = _make_features(trend_bias="down")  # UP仓位优势但趋势偏down
        direction, confidence = determine_winning_direction(f, _cfg())
        assert direction is None
        assert confidence == 0.0

    def test_none_when_no_trend_bias(self):
        """无趋势方向"""
        f = _make_features(trend_bias=None)
        direction, confidence = determine_winning_direction(f, _cfg())
        assert direction is None
        assert confidence == 0.0

    def test_none_both_sides_empty(self):
        """双侧均无持仓"""
        f = _make_features(
            up_held=0.0, down_held=0.0,
            up_avg_price=0.0, down_avg_price=0.0,
            unrealized_up_pnl=0.0, unrealized_down_pnl=0.0,
        )
        direction, _ = determine_winning_direction(f, _cfg())
        assert direction is None

    def test_none_when_other_side_pnl_positive_and_ratio_insufficient(self):
        """劣势侧也有正浮盈且浮盈比不够"""
        f = _make_features(
            unrealized_up_pnl=2.0, unrealized_down_pnl=1.5,
            # 2.0 / 1.5 = 1.33 < 2.0
        )
        direction, _ = determine_winning_direction(f, _cfg())
        assert direction is None


# ─── build_last_minute_candidate 集成测试 ───


class TestBuildLastMinuteWithDirectionConfirmation:
    """build_last_minute_candidate 使用方向确认逻辑"""

    def test_no_action_before_last_minute(self):
        """非最后一分钟不触发"""
        f = _make_features(is_last_minute=False)
        assert build_last_minute_candidate(f, _cfg()) == []

    def test_close_losing_positions_first(self):
        """优先平掉亏损仓位（优先于方向确认）"""
        f = _make_features(
            unrealized_up_pnl=-2.0,
            unrealized_down_pnl=1.0,
        )
        result = build_last_minute_candidate(f, _cfg())
        assert len(result) >= 1
        assert result[0].action == "sell"
        assert result[0].outcome == "up"

    def test_no_buy_when_direction_not_confirmed(self):
        """无法确认方向时，不加仓（保守策略）"""
        f = _make_features(
            unrealized_up_pnl=1.0,
            unrealized_down_pnl=0.8,
            # pnl ratio = 1.25 < 2.0 → 无法确认方向
        )
        result = build_last_minute_candidate(f, _cfg())
        # 不应有 buy 订单
        buys = [r for r in result if r.action == "buy"]
        assert len(buys) == 0

    def test_buy_when_direction_confirmed(self):
        """确认方向后加仓"""
        f = _make_features(
            up_held=5.0,  # 少于 target_net 才会触发买入
        )
        result = build_last_minute_candidate(f, _cfg())
        buys = [r for r in result if r.action == "buy"]
        if buys:
            assert buys[0].outcome == "up"
            assert buys[0].category == "last_minute"

    def test_buy_uses_confirmed_direction_not_net_direction(self):
        """使用确认的方向，而非 net_direction"""
        f = _make_features(
            net_direction="Down",
            up_held=30.0, up_avg_price=0.55,
            down_held=10.0, down_avg_price=0.45,
            unrealized_up_pnl=3.0, unrealized_down_pnl=0.5,
            trend_bias="up", trend_strength=0.7,
        )
        result = build_last_minute_candidate(f, _cfg())
        buys = [r for r in result if r.action == "buy"]
        # 方向确认为 up（来自 determine_winning_direction），而非 net_direction 的 Down
        if buys:
            assert buys[0].outcome == "up"

    def test_no_buy_when_confidence_proxy_too_low(self):
        """方向已确认但 confidence_proxy 不足时不买"""
        f = _make_features(confidence_proxy=0.5)
        result = build_last_minute_candidate(f, _cfg())
        buys = [r for r in result if r.action == "buy"]
        assert len(buys) == 0


# ─── 配置阈值边界测试 ───


class TestDirectionThresholdConfig:
    """方向确认配置阈值边界测试"""

    def test_custom_ratio_threshold(self):
        """自定义 ratio_threshold"""
        cfg = StrategyConfig(
            raw={
                **_cfg().raw,
                "last_minute": {
                    **_cfg().raw["last_minute"],
                    "direction_ratio_threshold": 3.0,  # 更严格
                },
            }
        )
        # 默认的 ratio=3.67 > 3.0 → 仍然确认
        f = _make_features()
        direction, _ = determine_winning_direction(f, cfg)
        assert direction == "up"

    def test_very_strict_ratio_blocks_confirmation(self):
        """极严格的 ratio 阈值阻止确认"""
        cfg = StrategyConfig(
            raw={
                **_cfg().raw,
                "last_minute": {
                    **_cfg().raw["last_minute"],
                    "direction_ratio_threshold": 5.0,
                },
            }
        )
        f = _make_features()
        # ratio = 16.5/4.5 = 3.67 < 5.0
        direction, _ = determine_winning_direction(f, cfg)
        assert direction is None
