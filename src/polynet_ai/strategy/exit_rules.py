from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.cycle_windows import rule_disabled_in_cycle_tail
from polynet_ai.strategy.spec import StrategyConfig
from polynet_ai.strategy.price_reference import outcome_reference_price


def _held_up(features: FeatureSnapshot) -> float:
    return max(0.0, features.up_held)


def _held_down(features: FeatureSnapshot) -> float:
    return max(0.0, features.down_held)


def take_profit_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    fraction = float(config.get("profit_taking.take_profit_fraction", 0.35))
    intents: list[OrderIntent] = []
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    up_ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
    down_ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
    if features.unrealized_up_pnl > 0 and features.up_deviation >= float(
        config.get("profit_taking.take_profit_up_deviation", 0.20)
    ):
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="sell",
                shares=max(0.0, _held_up(features) * fraction),
                reference_price=up_ref,
                category="take_profit",
                reason="Up 达到止盈区间，分批卖出兑现利润",
                priority=int(config.priorities.get("take_profit", 50)),
            )
        )
    if features.unrealized_down_pnl > 0 and features.down_deviation >= float(
        config.get("profit_taking.take_profit_down_deviation", 0.20)
    ):
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="sell",
                shares=max(0.0, _held_down(features) * fraction),
                reference_price=down_ref,
                category="take_profit",
                reason="Down 达到止盈区间，分批卖出兑现利润",
                priority=int(config.priorities.get("take_profit", 50)),
            )
        )
    return [intent for intent in intents if intent.shares > 0]


def stop_loss_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """
    改进的止损机制：
    1. 基于周期累计亏损额 (原有机制)
    2. 基于单个持仓的亏损百分比 (新增 - 更激进)
    3. 基于市场波动率的动态阈值 (新增)
    """
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    up_ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
    down_ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
    fraction = float(config.get("stop_loss.stop_loss_fraction", 0.50))
    
    intents: list[OrderIntent] = []
    
    # 机制1: 周期累计亏损额止损 (保留原有逻辑，但降低阈值)
    cycle_loss_threshold = float(config.get("stop_loss.stop_loss_cycle_loss", 8.0))
    if features.cycle_net_profit < -cycle_loss_threshold:
        if features.unrealized_up_pnl < 0 and _held_up(features) > 0:
            intents.append(
                OrderIntent(
                    market_id=features.market_id,
                    cycle_id=features.cycle_id,
                    outcome="up",
                    action="sell",
                    shares=_held_up(features) * fraction,
                    reference_price=up_ref,
                    category="stop_loss",
                    reason=f"周期亏损${-features.cycle_net_profit:.2f}超阈值${cycle_loss_threshold:.1f}，止损 Up 持仓",
                    priority=int(config.priorities.get("stop_loss", 30)),
                )
            )
        if features.unrealized_down_pnl < 0 and _held_down(features) > 0:
            intents.append(
                OrderIntent(
                    market_id=features.market_id,
                    cycle_id=features.cycle_id,
                    outcome="down",
                    action="sell",
                    shares=_held_down(features) * fraction,
                    reference_price=down_ref,
                    category="stop_loss",
                    reason=f"周期亏损${-features.cycle_net_profit:.2f}超阈值${cycle_loss_threshold:.1f}，止损 Down 持仓",
                    priority=int(config.priorities.get("stop_loss", 30)),
                )
            )
        return [intent for intent in intents if intent.shares > 0]
    
    # 机制2: 基于百分比的激进止损 (新增)
    stop_loss_pct = float(config.get("stop_loss.stop_loss_pct", 0.02))
    high_vol_stop_loss_pct = float(config.get("stop_loss.high_vol_stop_loss_pct", 0.01))
    
    # 根据波动率调整止损百分比
    volatility_threshold = 1.5
    effective_stop_loss_pct = high_vol_stop_loss_pct if features.volatility_ratio > volatility_threshold else stop_loss_pct
    
    # 检查 UP 持仓的止损
    if _held_up(features) > 0 and features.up_avg_price > 0:
        up_loss_pct = -features.unrealized_up_pnl / (features.up_avg_price * _held_up(features))
        if up_loss_pct >= effective_stop_loss_pct:
            intents.append(
                OrderIntent(
                    market_id=features.market_id,
                    cycle_id=features.cycle_id,
                    outcome="up",
                    action="sell",
                    shares=_held_up(features) * fraction,
                    reference_price=up_ref,
                    category="stop_loss",
                    reason=f"Up 持仓亏损{up_loss_pct*100:.1f}%超止损点{effective_stop_loss_pct*100:.1f}%",
                    priority=int(config.priorities.get("stop_loss", 30)),
                )
            )
    
    # 检查 DOWN 持仓的止损
    if _held_down(features) > 0 and features.down_avg_price > 0:
        down_loss_pct = -features.unrealized_down_pnl / (features.down_avg_price * _held_down(features))
        if down_loss_pct >= effective_stop_loss_pct:
            intents.append(
                OrderIntent(
                    market_id=features.market_id,
                    cycle_id=features.cycle_id,
                    outcome="down",
                    action="sell",
                    shares=_held_down(features) * fraction,
                    reference_price=down_ref,
                    category="stop_loss",
                    reason=f"Down 持仓亏损{down_loss_pct*100:.1f}%超止损点{effective_stop_loss_pct*100:.1f}%",
                    priority=int(config.priorities.get("stop_loss", 30)),
                )
            )
    
    return [intent for intent in intents if intent.shares > 0]


def hedge_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if features.is_last_minute:
        return []
    exposure = abs(features.net_position_value)
    trigger = float(config.get("exposure.hedge_trigger_value", 50.0))
    if exposure < trigger or features.cycle_net_profit <= 0:
        return []
    
    intents: list[OrderIntent] = []
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    up_ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
    down_ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
    
    if features.unrealized_up_pnl > 0 and _held_up(features) > 0:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="sell",
                shares=min(_held_up(features), exposure * 0.1),
                reference_price=up_ref,
                category="hedge",
                reason="净敞口过大，卖出盈利 Up 仓位对冲",
                priority=int(config.priorities.get("hedge", 40)),
            )
        )
    
    if features.unrealized_down_pnl > 0 and _held_down(features) > 0:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="sell",
                shares=min(_held_down(features), exposure * 0.1),
                reference_price=down_ref,
                category="hedge",
                reason="净敞口过大，卖出盈利 Down 仓位对冲",
                priority=int(config.priorities.get("hedge", 41)),
            )
        )
    
    return intents


def grid_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if rule_disabled_in_cycle_tail(features, config, "grid"):
        return []
    if features.market_regime != "range":
        return []
    low = float(config.get("grid.grid_low_percentile", 0.25))
    high = float(config.get("grid.grid_high_percentile", 0.75))
    grid_exit_fraction = float(config.get("grid.grid_exit_fraction", 0.25))
    intents: list[OrderIntent] = []
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    up_ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
    down_ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
    if features.price_percentile >= high and _held_up(features) > 0:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="sell",
                shares=max(0.0, _held_up(features) * grid_exit_fraction),
                reference_price=up_ref,
                category="grid",
                reason="震荡区间高位卖出 Up，完成网格循环",
                priority=int(config.priorities.get("grid", 60)),
            )
        )
    if features.price_percentile <= low and _held_down(features) > 0:
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="sell",
                shares=max(0.0, _held_down(features) * grid_exit_fraction),
                reference_price=down_ref,
                category="grid",
                reason="震荡区间低位卖出 Down，完成网格循环",
                priority=int(config.priorities.get("grid", 60)),
            )
        )
    return [intent for intent in intents if intent.shares > 0]


def mean_reversion_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if not bool(config.get("mean_reversion.enabled", True)):
        return []
    if rule_disabled_in_cycle_tail(features, config, "mean_reversion"):
        return []
    mr_sell_fraction = float(config.get("mean_reversion.mean_reversion_sell_fraction", 0.40))
    intents: list[OrderIntent] = []
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    up_ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
    down_ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
    if _held_up(features) > 0 and features.up_deviation >= float(
        config.get("mean_reversion.mean_reversion_sell_up_deviation", 0.20)
    ):
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="sell",
                shares=max(0.0, _held_up(features) * mr_sell_fraction),
                reference_price=up_ref,
                category="mean_reversion",
                reason="Up 显著偏离均价，执行均值回归卖出",
                priority=int(config.priorities.get("mean_reversion", 70)),
            )
        )
    if _held_down(features) > 0 and features.down_deviation >= float(
        config.get("mean_reversion.mean_reversion_sell_down_deviation", 0.20)
    ):
        intents.append(
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="sell",
                shares=max(0.0, _held_down(features) * mr_sell_fraction),
                reference_price=down_ref,
                category="mean_reversion",
                reason="Down 显著偏离均价，执行均值回归卖出",
                priority=int(config.priorities.get("mean_reversion", 70)),
            )
        )
    return [intent for intent in intents if intent.shares > 0]


def build_exit_candidates(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    candidates: list[OrderIntent] = []
    candidates.extend(stop_loss_exits(features, config))
    candidates.extend(hedge_exits(features, config))
    candidates.extend(take_profit_exits(features, config))
    candidates.extend(grid_exits(features, config))
    candidates.extend(mean_reversion_exits(features, config))
    return candidates
