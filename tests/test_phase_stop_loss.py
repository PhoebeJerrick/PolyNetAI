"""test_phase_stop_loss.py — 四阶段动态止损机制测试（方案三）

验证 stop_loss_exits 的三层机制：

机制1 — 周期累计亏损熔断：cycle_net_profit < -18 USDT → 亏损仓位按 50% 平仓

机制2 — 单仓止损（双条件，按阶段独立配置）：
  条件A：last_price ≤ near_zero_price → 立即全平（无时间限制）
  条件B：浮亏% ≥ stop_loss_pct AND phase_elapsed ≥ min_hold_seconds → 全平
       （phase_elapsed = 周期已过时间 − 当前阶段起点）

机制3 — 高波动绝对价格止损（按阶段独立配置）：
  vol_ratio > high_vol_trigger_ratio AND last_price ≤ high_vol_price_threshold
  → 全平（仅在机制2未触发时执行）
"""
from __future__ import annotations

from datetime import datetime

import pytest

from polynet_ai.domain.models import FeatureSnapshot
from polynet_ai.strategy.exit_rules import stop_loss_exits
from polynet_ai.strategy.spec import StrategyConfig

# ─────────────────────────────────────────────────────────────────────────────
# 公共测试基础数据
# ─────────────────────────────────────────────────────────────────────────────

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
    volatility_ratio=0.1,      # 默认低波动，不触发触发3
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
)


def _cfg() -> StrategyConfig:
    """包含所有四阶段止损参数的完整配置"""
    return StrategyConfig(
        raw={
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "order_sizing": {
                "buy": {"min_order_size": 2.0},
                "sell": {"min_order_size": 2.0},
            },
            "cycle": {
                "cycle_seconds": 300,
                "phase_end_seconds_1": 70,
                "phase_end_seconds_2": 160,
                "phase_end_seconds_3": 240,
            },
            "stop_loss": {
                # 触发1
                "stop_loss_cycle_loss": 18.0,
                "stop_loss_fraction": 0.5,
                "pre_phase_4_max_exit_fraction": 0.95,
                "pre_phase_4_min_remaining_shares": 0.25,
                # 触发2 — 阶段1
                "phase_1_near_zero_price": 0.06,
                "phase_1_stop_loss_pct": 0.15,
                "phase_1_min_hold_seconds": 20,
                "phase_1_stop_loss_action_fraction": 0.5,
                # 触发2 — 阶段2
                "phase_2_near_zero_price": 0.06,
                "phase_2_stop_loss_pct": 0.12,
                "phase_2_min_hold_seconds": 40,
                "phase_2_stop_loss_action_fraction": 0.65,
                # 触发2 — 阶段3
                "phase_3_near_zero_price": 0.06,
                "phase_3_stop_loss_pct": 0.12,
                "phase_3_min_hold_seconds": 45,
                "phase_3_stop_loss_action_fraction": 0.8,
                # 触发2 — 阶段4
                "phase_4_near_zero_price": 0.08,
                "phase_4_stop_loss_pct": 0.08,
                "phase_4_min_hold_seconds": 20,
                "phase_4_stop_loss_action_fraction": 1.0,
                # 触发3 — 阶段1
                "phase_1_high_vol_trigger_ratio": 1.5,
                "phase_1_high_vol_price_threshold": 0.15,
                "phase_1_high_vol_action_fraction": 0.5,
                # 触发3 — 阶段2
                "phase_2_high_vol_trigger_ratio": 1.5,
                "phase_2_high_vol_price_threshold": 0.12,
                "phase_2_high_vol_action_fraction": 0.65,
                # 触发3 — 阶段3
                "phase_3_high_vol_trigger_ratio": 1.5,
                "phase_3_high_vol_price_threshold": 0.12,
                "phase_3_high_vol_action_fraction": 0.8,
                # 触发3 — 阶段4
                "phase_4_high_vol_trigger_ratio": 1.3,
                "phase_4_high_vol_price_threshold": 0.15,
                "phase_4_high_vol_action_fraction": 1.0,
                # fallback
                "stop_loss_pct": 0.12,
                "high_vol_stop_loss_pct": 0.01,
            },
            "priorities": {"stop_loss": 30},
        }
    )


def _cfg_no_phase() -> StrategyConfig:
    """没有阶段性配置的策略（测试 fallback 路径）"""
    return StrategyConfig(
        raw={
            "opening_entry": {"infer_missing_with_binary_complement": True},
            "stop_loss": {
                "stop_loss_cycle_loss": 18.0,
                "stop_loss_fraction": 0.5,
                "stop_loss_pct": 0.12,
                "high_vol_stop_loss_pct": 0.01,
            },
            "priorities": {"stop_loss": 30},
        }
    )


def _f(cycle_elapsed: float, up_last_price: float, up_avg_price: float = 0.50, **kw) -> FeatureSnapshot:
    """构造 UP 侧为主的 FeatureSnapshot，指定周期时间和 UP 侧价格"""
    return FeatureSnapshot(**{**_BASE, "cycle_elapsed_seconds": cycle_elapsed,
                              "up_avg_price": up_avg_price, "up_last_price": up_last_price, **kw})


def _f_down(cycle_elapsed: float, dn_last_price: float, dn_avg_price: float = 0.50, **kw) -> FeatureSnapshot:
    """构造 DOWN 侧为主的 FeatureSnapshot"""
    return FeatureSnapshot(**{**_BASE, "cycle_elapsed_seconds": cycle_elapsed,
                              "up_held": 0.0, "up_avg_price": 0.0, "up_last_price": 0.0,
                              "down_held": 10.0, "down_avg_price": dn_avg_price,
                              "down_last_price": dn_last_price, **kw})


# ─────────────────────────────────────────────────────────────────────────────
# 机制1：周期累计亏损熔断
# ─────────────────────────────────────────────────────────────────────────────


class TestTrigger1CycleLoss:
    """周期累计亏损 > 18 USDT → 熔断，清仓50%"""

    def test_triggers_when_cycle_loss_exceeded(self):
        """亏损 $20 > $18，触发熔断"""
        f = _f(30.0, up_last_price=0.49, unrealized_up_pnl=-2.0, cycle_net_profit=-20.0)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].outcome == "up"
        assert result[0].category == "stop_loss"

    def test_no_trigger_below_cycle_loss_threshold(self):
        """亏损 $15 < $18，不触发"""
        f = _f(30.0, up_last_price=0.49, cycle_net_profit=-15.0)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_sells_stop_loss_fraction_50pct(self):
        """触发1 卖出 50%（stop_loss_fraction=0.5）"""
        f = _f(30.0, up_last_price=0.49, up_held=20.0,
               unrealized_up_pnl=-2.0, cycle_net_profit=-20.0)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].shares == pytest.approx(10.0)  # 20 × 0.5

    def test_only_stops_losing_side_on_cycle_loss(self):
        """触发1 时只平亏损方向（UP 盈利不平，DOWN 亏损才平）"""
        f = _f(30.0, up_last_price=0.55,                 # UP 盈利，不平
               unrealized_up_pnl=0.5,
               down_held=10.0, down_avg_price=0.50, down_last_price=0.45,
               unrealized_down_pnl=-3.0,
               cycle_net_profit=-20.0)
        result = stop_loss_exits(f, _cfg())
        outcomes = [r.outcome for r in result]
        assert "up" not in outcomes
        assert "down" in outcomes

    def test_trigger1_returns_before_trigger2_check(self):
        """机制1 提前 return，不再执行机制2/3"""
        # UP 价格接近零（既满足触发1的周期亏损，又满足触发2条件A）
        # 但机制1应提前返回，卖出 50% 而非 100%
        f = _f(30.0, up_last_price=0.03, up_held=20.0,
               unrealized_up_pnl=-2.0, cycle_net_profit=-20.0)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].shares == pytest.approx(10.0)   # 机制1：50%，非机制2：100%

    def test_triggers_across_all_phases(self):
        """所有阶段均可触发机制1"""
        for elapsed in [30.0, 100.0, 200.0, 260.0]:
            f = _f(elapsed, up_last_price=0.49,
                   unrealized_up_pnl=-2.0, cycle_net_profit=-20.0)
            result = stop_loss_exits(f, _cfg())
            assert len(result) >= 1, f"phase at {elapsed}s should trigger cycle loss"


# ─────────────────────────────────────────────────────────────────────────────
# 机制2 — 触发2 条件A：近零价格全平（仅 last_minute 阶段）
# ─────────────────────────────────────────────────────────────────────────────


class TestTrigger2ConditionA:
    """条件A：last_price ≤ near_zero_price → 仅在 last_minute 立即全平"""

    def test_near_zero_triggers_in_last_minute(self):
        """last_minute=True：价格 0.05 ≤ 0.06，elapsed=5s 条件A触发"""
        f = _f(5.0, up_last_price=0.05, is_last_minute=True)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].outcome == "up"
        assert "条件A" in result[0].reason

    def test_near_zero_does_NOT_trigger_outside_last_minute(self):
        """last_minute=False：价格 0.05 ≤ 0.06，但非尾盘，条件A不触发"""
        f = _f(5.0, up_last_price=0.05, is_last_minute=False)
        result = stop_loss_exits(f, _cfg())
        # elapsed=5 < min_hold=20，条件B也不触发 → 结果为空
        assert all("条件A" not in r.reason for r in result)

    def test_near_zero_at_exact_threshold_last_minute(self):
        """last_minute=True：价格 = near_zero_price（0.06），恰好触发"""
        f = _f(30.0, up_last_price=0.06, is_last_minute=True)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert "条件A" in result[0].reason

    def test_above_near_zero_no_condition_a(self):
        """价格 0.07 > 0.06，条件A不触发（无论是否 last_minute）"""
        for lm in (True, False):
            f = _f(30.0, up_last_price=0.07, is_last_minute=lm)
            for r in stop_loss_exits(f, _cfg()):
                assert "条件A" not in r.reason

    def test_phase4_near_zero_triggers_in_last_minute(self):
        """阶段4 near_zero=0.08，last_minute=True，价格 0.07 触发"""
        f = _f(260.0, up_last_price=0.07, is_last_minute=True)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert "条件A" in result[0].reason

    def test_phase4_near_zero_no_trigger_outside_last_minute(self):
        """阶段4 near_zero=0.08，last_minute=False，价格 0.07 不触发条件A"""
        f = _f(260.0, up_last_price=0.07, is_last_minute=False)
        for r in stop_loss_exits(f, _cfg()):
            assert "条件A" not in r.reason

    def test_condition_a_sells_partial_position_before_phase4(self):
        """第四阶段前条件A 只允许部分止损"""
        f = _f(30.0, up_last_price=0.04, up_held=15.0, is_last_minute=True)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].shares == pytest.approx(7.5)


# ─────────────────────────────────────────────────────────────────────────────
# 机制2 — 触发2 条件B：浮亏% + 时间门槛
# ─────────────────────────────────────────────────────────────────────────────


class TestTrigger2ConditionB:
    """条件B：浮亏% ≥ stop_loss_pct AND phase_elapsed ≥ min_hold_seconds"""

    # ── 阶段1（min_hold=20s，sl_pct=15%）──────────────────────────────────

    def test_phase1_no_trigger_when_elapsed_below_min_hold(self):
        """阶段1：elapsed=10s < min_hold=20s，即使亏损 20% 也不触发条件B"""
        # 0.40 → 亏损 20%（> 15% 阈值），但时间不足
        f = _f(10.0, up_last_price=0.40)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_phase1_triggers_when_elapsed_at_min_hold(self):
        """阶段1：elapsed=20s = min_hold=20s，亏损 20% 触发条件B"""
        f = _f(20.0, up_last_price=0.40)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert "条件B" in result[0].reason

    def test_phase1_no_trigger_when_loss_below_threshold(self):
        """阶段1：elapsed=30s ≥ 20s，但亏损 10% < 15% 不触发"""
        # 0.45 → 亏损 10%（< 15%）
        f = _f(30.0, up_last_price=0.45)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_phase1_triggers_at_exact_pct_threshold(self):
        """阶段1：elapsed=25s，亏损恰好 15% = 阈值触发"""
        # 0.50 × (1-0.15) = 0.425
        f = _f(25.0, up_last_price=0.425)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    # ── 阶段2（min_hold=40s，sl_pct=12%）──────────────────────────────────

    def test_phase2_no_trigger_when_phase_elapsed_below_min_hold(self):
        """阶段2：本阶段仅过 30s < min_hold=40s，大亏也不触发条件 B"""
        f = FeatureSnapshot(**{**_BASE, "cycle_elapsed_seconds": 100.0,
                               "up_avg_price": 0.50, "up_last_price": 0.40})
        assert stop_loss_exits(f, _cfg()) == []

    def test_phase2_elapsed_meets_min_hold_triggers(self):
        """阶段2：本阶段已过 40s（周期 110s），亏损 12% 触发"""
        f = FeatureSnapshot(**{**_BASE, "cycle_elapsed_seconds": 110.0,
                               "up_avg_price": 0.50, "up_last_price": 0.44})  # 12% 亏损
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    # ── 阶段4（min_hold=20s，sl_pct=8%，最激进） ───────────────────────────

    def test_phase4_triggers_with_8pct_loss(self):
        """阶段4：本阶段已过 20s（周期 260s），亏损 10% > 8% 触发"""
        f = _f(260.0, up_last_price=0.45)   # 0.45/0.50-1 = -10% > -8%
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    def test_phase4_no_trigger_below_8pct_loss(self):
        """阶段4：亏损 5% < 8% 不触发条件B"""
        f = _f(260.0, up_last_price=0.475)   # -5%
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_phase4_more_aggressive_than_phase1(self):
        """相同亏损率：阶段4（8%）比阶段1（15%）更早触发"""
        loss_price = 0.44                    # -12% from 0.50
        # 阶段1（elapsed=30s）：min_hold=20s（满足），sl_pct=15%（12% < 15%）→ 不触发
        f1 = _f(30.0, up_last_price=loss_price)
        assert len(stop_loss_exits(f1, _cfg())) == 0
        # 阶段4（elapsed=260s）：min_hold=20s（满足），sl_pct=8%（12% > 8%）→ 触发
        f4 = _f(260.0, up_last_price=loss_price)
        assert len(stop_loss_exits(f4, _cfg())) == 1

    def test_condition_b_sells_full_position(self):
        """条件B 卖出 100%（phase_N_stop_loss_action_fraction=1.0）"""
        f = _f(260.0, up_last_price=0.45, up_held=12.0)   # 阶段4，亏10%>8%
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].shares == pytest.approx(12.0)     # 12 × 1.0

    def test_profitable_position_not_stopped(self):
        """盈利仓位不触发任何条件B止损"""
        f = _f(260.0, up_last_price=0.55)    # +10% 盈利
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 机制3 — 触发3：高波动 + 绝对价格
# ─────────────────────────────────────────────────────────────────────────────


class TestTrigger3HighVol:
    """vol_ratio > trigger_ratio AND last_price ≤ price_threshold → 全平"""

    def test_triggers_in_phase1_with_high_vol_and_low_price(self):
        """阶段1：vol_ratio=2.0 > 1.5，价格 0.12 ≤ 0.15 → 触发3
        elapsed=5s < min_hold=20s（条件B不触发），price > 0.06（条件A不触发）"""
        # 使用小亏损（7.7%<15%）+ elapsed=5s<min_hold=20s 确保条件B不触发
        f = _f(5.0, up_last_price=0.12, up_avg_price=0.13, volatility_ratio=2.0)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert "触发3" in result[0].reason

    def test_no_trigger_when_price_above_threshold_phase1(self):
        """阶段1：价格 0.20 > 0.15 → 触发3 不触发"""
        f = _f(30.0, up_last_price=0.20, volatility_ratio=2.0)
        result = stop_loss_exits(f, _cfg())
        # 价格超近零阈值（0.06），也超触发3阈值（0.15），不触发
        # pnl = (0.20-0.50)/0.50 = -60%，elapsed=30 >= 20s，sl_pct=15%（60%>15%）→ 条件B触发
        # 所以会有结果，但不是触发3
        for r in result:
            assert "触发3" not in r.reason

    def test_no_trigger_when_vol_ratio_below_threshold_phase1(self):
        """阶段1：vol_ratio=1.2 < 1.5，即使价格低也不触发3"""
        # up_last_price=0.10 ≤ 0.15，但 vol_ratio 不满足
        f = _f(5.0, up_last_price=0.10, volatility_ratio=1.2)
        # elapsed=5s < min_hold=20s，条件B不触发；条件A：0.10 > 0.06 不触发
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_phase4_lower_trigger_ratio(self):
        """阶段4 trigger_ratio=1.3，vol_ratio=1.4 即可触发"""
        f = _f(260.0, up_last_price=0.10, volatility_ratio=1.4)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1

    def test_phase4_vol_ratio_14_does_not_trigger_phase1(self):
        """vol_ratio=1.4 在阶段1（trigger_ratio=1.5）不触发触发3"""
        # elapsed=5s < min_hold=20s（排除条件B），price=0.10 > 0.06（排除条件A）
        f = _f(5.0, up_last_price=0.10, volatility_ratio=1.4)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_trigger3_sells_partial_position_before_phase4(self):
        """第四阶段前触发3 只允许部分止损"""
        f = _f(30.0, up_last_price=0.10, up_held=8.0, volatility_ratio=2.0)
        # elapsed=30 >= min_hold=20，price=0.10 > near_zero=0.06（排除条件A）
        # pnl = (0.10-0.50)/0.50 = -80%，条件B：-80% >= -15% 且 30 >= 20 → 条件B先触发
        # 因此需要让条件B不触发才能到触发3
        # 将 avg_price 设低让 pnl 不亏损：avg=0.10，last=0.10 → pnl=0（不亏损，条件B不触发）
        # 但价格 0.10 ≤ near_zero=0.06？ 0.10 > 0.06，条件A不触发
        f = FeatureSnapshot(**{**_BASE, "cycle_elapsed_seconds": 5.0,  # elapsed < min_hold → 条件B不触发
                               "up_avg_price": 0.50, "up_last_price": 0.10,
                               "up_held": 8.0, "volatility_ratio": 2.0})
        # elapsed=5 < min_hold=20 → 条件B不触; 0.10 > 0.06 → 条件A不触; vol=2>1.5 + price=0.10≤0.15 → 触发3
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].shares == pytest.approx(4.0)
        assert "触发3" in result[0].reason

    def test_trigger3_skipped_when_trigger2_fires(self):
        """条件A（尾盘近零）已触发时，同一仓位不再重复执行触发3（避免双重止损）"""
        # is_last_minute=True + price 0.04 ≤ near_zero=0.06 → 条件A；触发3也满足（vol高+price低）
        f = _f(30.0, up_last_price=0.04, volatility_ratio=2.0, is_last_minute=True)
        result = stop_loss_exits(f, _cfg())
        # 只应产生一条 intent（条件A），不是两条
        up_intents = [r for r in result if r.outcome == "up"]
        assert len(up_intents) == 1
        assert "条件A" in up_intents[0].reason


# ─────────────────────────────────────────────────────────────────────────────
# DOWN 方向止损
# ─────────────────────────────────────────────────────────────────────────────


class TestDownSideStopLoss:
    """DOWN 方向适用相同的阶段性逻辑"""

    def test_down_condition_a_triggers(self):
        """DOWN 侧价格接近零 + last_minute → 条件A触发"""
        f = _f_down(30.0, dn_last_price=0.04, is_last_minute=True)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].outcome == "down"
        assert "条件A" in result[0].reason

    def test_down_condition_b_triggers_phase4(self):
        """DOWN 侧阶段4：亏损 10% > 8%，elapsed=260s ≥ 20s → 条件B触发"""
        f = _f_down(260.0, dn_last_price=0.45)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 1
        assert result[0].outcome == "down"

    def test_down_no_trigger_when_profitable(self):
        """DOWN 侧盈利不触发止损"""
        f = _f_down(260.0, dn_last_price=0.60)
        result = stop_loss_exits(f, _cfg())
        assert len(result) == 0

    def test_both_sides_can_trigger_simultaneously(self):
        """UP 和 DOWN 同时满足止损条件时，各自产生独立 intent"""
        f = FeatureSnapshot(**{**_BASE,
                               "cycle_elapsed_seconds": 260.0,
                               "up_held": 10.0, "up_avg_price": 0.50, "up_last_price": 0.44,
                               "down_held": 10.0, "down_avg_price": 0.50, "down_last_price": 0.44})
        result = stop_loss_exits(f, _cfg())
        outcomes = {r.outcome for r in result}
        assert "up" in outcomes
        assert "down" in outcomes


# ─────────────────────────────────────────────────────────────────────────────
# Fallback：无阶段配置时的行为
# ─────────────────────────────────────────────────────────────────────────────


class TestFallbackBehavior:
    """无阶段性配置时，使用代码内置默认值"""

    def test_condition_a_near_zero_default_triggers(self):
        """无阶段配置，条件A 默认阈值 0.06：last_minute=True + 价格 0.05 触发"""
        f = _f(5.0, up_last_price=0.05, is_last_minute=True)
        result = stop_loss_exits(f, _cfg_no_phase())
        assert len(result) == 1

    def test_condition_b_fallback_pct_and_default_min_hold(self):
        """无阶段配置：stop_loss_pct=0.12，默认 min_hold=45s
           elapsed=50s ≥ 45s，亏损 15% > 12% → 条件B触发"""
        f = _f(50.0, up_last_price=0.425)    # -15%
        result = stop_loss_exits(f, _cfg_no_phase())
        assert len(result) == 1


def test_phase3_condition_b_keeps_residual_position() -> None:
    # 阶段3 需本阶段≥45s → 周期至少 160+45=205
    f = _f(205.0, up_last_price=0.40, up_held=10.0)
    result = stop_loss_exits(f, _cfg())
    assert len(result) == 1
    assert result[0].shares == pytest.approx(8.0)


def test_phase4_condition_b_can_still_sell_entire_position() -> None:
    f = _f(260.0, up_last_price=0.40, up_held=10.0)
    result = stop_loss_exits(f, _cfg())
    assert len(result) == 1
    assert result[0].shares == pytest.approx(10.0)


def test_condition_b_no_trigger_when_elapsed_below_default_min_hold() -> None:
    """无阶段配置：阶段1 内仅 30s < 默认 min_hold=45s，止损不触发"""
    f = _f(30.0, up_last_price=0.40)  # -20%（超过 fallback 12%），但本阶段时间不足
    result = stop_loss_exits(f, _cfg_no_phase())
    assert len(result) == 0

