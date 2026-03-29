from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent
from polynet_ai.strategy.cycle_windows import determine_phase, rule_disabled_in_cycle_tail
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

    # 检查 UP 持仓的止盈 - 使用当前市场价格计算实际盈亏
    if _held_up(features) > 0 and features.up_avg_price > 0:
        current_price = features.up_last_price
        if current_price > 0:
            actual_pnl_pct = (current_price - features.up_avg_price) / features.up_avg_price
            # 只有当实际盈利且达到偏离度阈值时才触发止盈
            if actual_pnl_pct > 0 and features.up_deviation >= float(
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

    # 检查 DOWN 持仓的止盈 - 使用当前市场价格计算实际盈亏
    if _held_down(features) > 0 and features.down_avg_price > 0:
        current_price = features.down_last_price
        if current_price > 0:
            actual_pnl_pct = (current_price - features.down_avg_price) / features.down_avg_price
            # 只有当实际盈利且达到偏离度阈值时才触发止盈
            if actual_pnl_pct > 0 and features.down_deviation >= float(
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
    三层止损机制（方案三，四阶段动态止损）：

    机制1 — 周期累计亏损熔断（全阶段统一）：
      cycle_net_profit < -stop_loss_cycle_loss
      → 对亏损仓位按 stop_loss_fraction（50%）强制平仓，提前返回。

    机制2 — 单仓止损（按周期阶段独立配置，双条件互斥）：
      条件A：当前价格 ≤ phase_N_near_zero_price
             → 立即全平，无时间门槛（价格接近0表示方向已判定失败）
      条件B：浮亏% ≥ phase_N_stop_loss_pct
             AND cycle_elapsed_seconds ≥ phase_N_min_hold_seconds
             → 全平（时间门槛防止入场瞬间噪声误触，30s内≥2%逆向概率82%）

    机制3 — 高波动绝对价格止损（按周期阶段独立配置）：
      volatility_ratio > phase_N_high_vol_trigger_ratio
      AND 当前价格 ≤ phase_N_high_vol_price_threshold
      → 全平（仅在机制2未触发时执行，避免对同一仓位重复止损）
    """
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    up_ref   = outcome_reference_price(features, "up",   infer_missing_with_binary_complement=infer_missing)
    down_ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
    priority = int(config.priorities.get("stop_loss", 30))

    intents: list[OrderIntent] = []

    # ── 机制1：周期累计亏损熔断 ─────────────────────────────────────────────
    cycle_loss_threshold = float(config.get("stop_loss.stop_loss_cycle_loss", 18.0))
    stop_loss_fraction   = float(config.get("stop_loss.stop_loss_fraction", 0.50))

    if features.cycle_net_profit < -cycle_loss_threshold:
        if features.unrealized_up_pnl < 0 and _held_up(features) > 0:
            intents.append(OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="sell",
                shares=_held_up(features) * stop_loss_fraction,
                reference_price=up_ref,
                category="stop_loss",
                reason=(
                    f"周期亏损${-features.cycle_net_profit:.2f}超阈值${cycle_loss_threshold:.1f}，"
                    f"熔断止损 Up（{stop_loss_fraction*100:.0f}%）"
                ),
                priority=priority,
            ))
        if features.unrealized_down_pnl < 0 and _held_down(features) > 0:
            intents.append(OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="sell",
                shares=_held_down(features) * stop_loss_fraction,
                reference_price=down_ref,
                category="stop_loss",
                reason=(
                    f"周期亏损${-features.cycle_net_profit:.2f}超阈值${cycle_loss_threshold:.1f}，"
                    f"熔断止损 Down（{stop_loss_fraction*100:.0f}%）"
                ),
                priority=priority,
            ))
        return [intent for intent in intents if intent.shares > 0]

    # ── 读取当前阶段参数 ──────────────────────────────────────────────────────
    phase   = determine_phase(features.cycle_elapsed_seconds, config)
    elapsed = features.cycle_elapsed_seconds
    sl_pct_fallback = float(config.get("stop_loss.stop_loss_pct", 0.12))

    # 触发2 参数
    near_zero_price = float(config.get(f"stop_loss.phase_{phase}_near_zero_price", 0.06))
    stop_loss_pct   = float(config.get(f"stop_loss.phase_{phase}_stop_loss_pct", sl_pct_fallback))
    min_hold_secs   = float(config.get(f"stop_loss.phase_{phase}_min_hold_seconds", 45.0))
    sl_action_frac  = float(config.get(f"stop_loss.phase_{phase}_stop_loss_action_fraction", 1.0))

    # 触发3 参数
    hv_trigger_ratio = float(config.get(f"stop_loss.phase_{phase}_high_vol_trigger_ratio", 1.5))
    hv_price_thr     = float(config.get(f"stop_loss.phase_{phase}_high_vol_price_threshold", 0.12))
    hv_action_frac   = float(config.get(f"stop_loss.phase_{phase}_high_vol_action_fraction", 1.0))
    is_high_vol      = features.volatility_ratio > hv_trigger_ratio

    # ── 机制2 & 3：逐方向检查 ────────────────────────────────────────────────
    sides = [
        ("up",   _held_up(features),   features.up_avg_price,   features.up_last_price,   up_ref),
        ("down", _held_down(features), features.down_avg_price, features.down_last_price, down_ref),
    ]

    for outcome, held, avg_price, last_price, ref_price in sides:
        if held <= 0 or avg_price <= 0 or last_price <= 0:
            continue

        pnl_pct  = (last_price - avg_price) / avg_price
        triggered = False

        # 机制2 — 触发2：双条件（条件A / 条件B 互斥）
        if last_price <= near_zero_price:
            # 条件A：近零价格，立即全平（无时间限制）
            intents.append(OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome=outcome,
                action="sell",
                shares=held * sl_action_frac,
                reference_price=ref_price,
                category="stop_loss",
                reason=(
                    f"{outcome.upper()} 价格{last_price:.4f}≤近零阈值{near_zero_price:.4f}"
                    f"（阶段{phase}），条件A全平"
                ),
                priority=priority,
            ))
            triggered = True

        elif pnl_pct < 0 and abs(pnl_pct) >= stop_loss_pct and elapsed >= min_hold_secs:
            # 条件B：浮亏% ≥ 阈值 AND 已过 ≥ min_hold_seconds
            intents.append(OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome=outcome,
                action="sell",
                shares=held * sl_action_frac,
                reference_price=ref_price,
                category="stop_loss",
                reason=(
                    f"{outcome.upper()} 浮亏{abs(pnl_pct)*100:.1f}%≥{stop_loss_pct*100:.1f}%"
                    f"，已过{elapsed:.0f}s≥{min_hold_secs:.0f}s（阶段{phase}），条件B全平"
                ),
                priority=priority,
            ))
            triggered = True

        # 机制3 — 触发3：高波动 + 绝对价格（触发2未触发时才检查）
        if not triggered and is_high_vol and last_price <= hv_price_thr:
            intents.append(OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome=outcome,
                action="sell",
                shares=held * hv_action_frac,
                reference_price=ref_price,
                category="stop_loss",
                reason=(
                    f"{outcome.upper()} 高波动(ratio={features.volatility_ratio:.2f}>{hv_trigger_ratio:.1f})"
                    f" 价格{last_price:.4f}≤{hv_price_thr:.4f}（阶段{phase}），触发3全平"
                ),
                priority=priority,
            ))

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

    # 检查 UP 持仓的对冲 - 使用当前市场价格计算实际盈亏
    if _held_up(features) > 0 and features.up_avg_price > 0:
        current_price = features.up_last_price
        if current_price > 0:
            actual_pnl_pct = (current_price - features.up_avg_price) / features.up_avg_price
            # 只有当实际盈利时才触发对冲
            if actual_pnl_pct > 0:
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

    # 检查 DOWN 持仓的对冲 - 使用当前市场价格计算实际盈亏
    if _held_down(features) > 0 and features.down_avg_price > 0:
        current_price = features.down_last_price
        if current_price > 0:
            actual_pnl_pct = (current_price - features.down_avg_price) / features.down_avg_price
            # 只有当实际盈利时才触发对冲
            if actual_pnl_pct > 0:
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
