"""尾盘方向判定与 build_last_minute_candidate：与文档第四阶段/第六节一致（份额比例或市价）。"""

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
            },
            "priorities": {"last_minute": 20},
        }
    )


def _make_features(**overrides) -> FeatureSnapshot:
    vals = {**_BASE}
    vals.update(overrides)
    return FeatureSnapshot(**vals)


class TestDetermineWinningDirection:
    def test_up_when_share_ratio_favors_up(self):
        """UP 份额 ≥ DOWN × ratio"""
        f = _make_features()
        direction, conf = determine_winning_direction(f, _cfg())
        assert direction == "up"
        assert conf == 0.85

    def test_down_when_share_ratio_favors_down(self):
        f = _make_features(
            up_held=10.0,
            up_avg_price=0.45,
            down_held=30.0,
            down_avg_price=0.55,
            unrealized_up_pnl=0.5,
            unrealized_down_pnl=3.0,
            trend_bias="down",
        )
        direction, conf = determine_winning_direction(f, _cfg())
        assert direction == "down"
        assert conf == 0.85

    def test_price_tiebreak_when_balanced_shares(self):
        """双侧均有仓但都不满足份额倍率 → 市价较高侧"""
        f = _make_features(
            up_held=10.0,
            down_held=10.0,
            up_last_price=0.35,
            down_last_price=0.65,
        )
        assert determine_winning_direction(f, _cfg())[0] == "down"

    def test_only_up_held(self):
        f = _make_features(down_held=0.0, down_avg_price=0.0)
        assert determine_winning_direction(f, _cfg())[0] == "up"

    def test_only_down_held(self):
        f = _make_features(up_held=0.0, up_avg_price=0.0, down_held=15.0, down_avg_price=0.5)
        assert determine_winning_direction(f, _cfg())[0] == "down"

    def test_both_flat_uses_price(self):
        f = _make_features(
            up_held=0.0,
            down_held=0.0,
            up_avg_price=0.0,
            down_avg_price=0.0,
            unrealized_up_pnl=0.0,
            unrealized_down_pnl=0.0,
            up_last_price=0.72,
            down_last_price=0.28,
        )
        assert determine_winning_direction(f, _cfg())[0] == "up"


class TestBuildLastMinuteWithDirectionConfirmation:
    def test_no_action_before_last_minute(self):
        f = _make_features(is_last_minute=False)
        assert build_last_minute_candidate(f, _cfg()) == []

    def test_close_losing_positions_first(self):
        f = _make_features(
            unrealized_up_pnl=-2.0,
            unrealized_down_pnl=1.0,
        )
        result = build_last_minute_candidate(f, _cfg())
        assert len(result) >= 1
        assert result[0].action == "sell"
        assert result[0].outcome == "up"

    def test_buy_after_direction_even_if_old_pnl_ratio_would_fail(self):
        """旧版三重门会因浮盈比拒绝；现按份额/市价仍可加仓（若目标仓 > 当前）"""
        f = _make_features(
            unrealized_up_pnl=1.0,
            unrealized_down_pnl=0.8,
            up_held=5.0,
        )
        result = build_last_minute_candidate(f, _cfg())
        buys = [r for r in result if r.action == "buy"]
        assert len(buys) == 1
        assert buys[0].outcome == "up"

    def test_buy_when_direction_confirmed(self):
        f = _make_features(up_held=5.0)
        result = build_last_minute_candidate(f, _cfg())
        buys = [r for r in result if r.action == "buy"]
        assert len(buys) == 1
        assert buys[0].outcome == "up"
        assert buys[0].category == "last_minute"

    def test_buy_follows_share_or_price_not_net_direction(self):
        f = _make_features(
            net_direction="Down",
            up_held=5.0,
            up_avg_price=0.55,
            down_held=10.0,
            down_avg_price=0.45,
            unrealized_up_pnl=3.0,
            unrealized_down_pnl=0.5,
            trend_bias="up",
            trend_strength=0.7,
        )
        result = build_last_minute_candidate(f, _cfg())
        buys = [r for r in result if r.action == "buy"]
        assert len(buys) == 1
        assert buys[0].outcome == "up"

    def test_no_buy_when_confidence_proxy_too_low(self):
        f = _make_features(confidence_proxy=0.5, up_held=5.0)
        result = build_last_minute_candidate(f, _cfg())
        buys = [r for r in result if r.action == "buy"]
        assert len(buys) == 0


class TestPreferredLegMinRatioConfig:
    def test_higher_ratio_requires_stronger_share_skew(self):
        cfg = StrategyConfig(
            raw={
                **_cfg().raw,
                "last_minute": {
                    **_cfg().raw["last_minute"],
                    "preferred_leg_min_ratio": 2.0,
                },
            }
        )
        f = _make_features(up_held=18.0, down_held=10.0, up_last_price=0.2, down_last_price=0.8)
        # 18 < 10*2 → 无份额优势 → 市价高者为 down
        assert determine_winning_direction(f, cfg)[0] == "down"
