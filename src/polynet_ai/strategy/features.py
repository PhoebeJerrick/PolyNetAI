from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from polynet_ai.domain.models import FeatureSnapshot, Outcome
from polynet_ai.domain.settlement import settlement_summary
from polynet_ai.domain.state_engine import StateEngine


def _clamp_binary_price(price: float) -> float:
    return max(0.01, min(0.99, float(price)))


def _deviation(price: float, avg_price: float) -> float:
    if avg_price <= 1e-10:
        return 0.0
    return (price - avg_price) / avg_price


def _signal_basis_price(avg_price: float, reentry_anchor_price: float) -> float:
    if avg_price > 1e-10:
        return avg_price
    if reentry_anchor_price > 1e-10:
        return reentry_anchor_price
    return 0.0


def _price_percentile(price: float, low: float, high: float) -> float:
    if high - low <= 1e-10:
        return 0.5
    return max(0.0, min(1.0, (price - low) / (high - low)))


def _confidence_proxy(net_profit: float, price_move: float, volatility: float) -> float:
    signal = abs(net_profit) + abs(price_move)
    if volatility <= 1e-10:
        return 0.5
    return max(0.0, min(1.0, signal / (signal + volatility)))


def _resolve_outcome_prices(
    up_last_price: float,
    down_last_price: float,
    fallback_price: float,
    *,
    infer_missing_with_binary_complement: bool = True,
    eps: float = 1e-9,
) -> tuple[float, float]:
    up_px = float(up_last_price or 0.0)
    down_px = float(down_last_price or 0.0)
    fallback = float(fallback_price or 0.0)

    if up_px <= eps and infer_missing_with_binary_complement and down_px > eps:
        up_px = _clamp_binary_price(1.0 - down_px)
    if down_px <= eps and infer_missing_with_binary_complement and up_px > eps:
        down_px = _clamp_binary_price(1.0 - up_px)

    if up_px <= eps and fallback > eps:
        up_px = _clamp_binary_price(fallback)
    if down_px <= eps and fallback > eps:
        down_px = _clamp_binary_price(fallback)
    return up_px, down_px


def _implied_up_price_bounds(
    *,
    up_low: float,
    up_high: float,
    up_n: int,
    down_low: float,
    down_high: float,
    down_n: int,
    eps: float = 1e-9,
) -> tuple[float, float]:
    lows: list[float] = []
    highs: list[float] = []

    if up_n > 0 and up_low > eps and up_high > eps:
        lows.append(float(up_low))
        highs.append(float(up_high))
    if down_n > 0 and down_low > eps and down_high > eps:
        lows.append(_clamp_binary_price(1.0 - float(down_high)))
        highs.append(_clamp_binary_price(1.0 - float(down_low)))

    if not lows or not highs:
        return 0.0, 1.0
    lo = min(lows)
    hi = max(highs)
    if hi - lo <= eps:
        return lo, lo
    return lo, hi


def snapshot_with_effective_price(
    features: FeatureSnapshot,
    effective_price: float,
    *,
    effective_outcome: Outcome | None = None,
) -> FeatureSnapshot:
    """
    用 effective_price 替换「最后一笔价」及其派生字段，供各规则在独立喂价间隔下评估；
    盘口 outcome 价、VWAP、持仓盈亏等仍保留 base 的实时值。
    """
    opening_level = features.price - features.opening_vs_last_move
    price_move = effective_price - opening_level

    # `effective_price` 来自 rule_price_feed 的缓存，未必属于当前 tick 的 up/down。
    # 若能提供 effective_outcome，则把 effective_price 直接映射到对应方向，
    # 另一方向使用二元互补 (1 - p) 以避免“串方向”。
    if effective_outcome is None:
        up_last_price = float(features.up_last_price or 0.0)
        down_last_price = float(features.down_last_price or 0.0)
        if up_last_price > 1e-12 and abs(features.price - up_last_price) <= 1e-12:
            effective_outcome = "up"
        elif down_last_price > 1e-12 and abs(features.price - down_last_price) <= 1e-12:
            effective_outcome = "down"
        else:
            # 无法精确判断 effective_price 属于哪个方向时：
            # 退化为把 effective_price 映射到与 features.price 更接近的方向，
            # 这可以避免 rule_price_feed 缓存快照在边界场景下“串方向”。
            if up_last_price > 1e-12 and down_last_price > 1e-12:
                if abs(features.price - up_last_price) <= abs(features.price - down_last_price):
                    effective_outcome = "up"
                else:
                    effective_outcome = "down"
            elif up_last_price > 1e-12:
                effective_outcome = "up"
            elif down_last_price > 1e-12:
                effective_outcome = "down"

    if effective_outcome == "up":
        up_px = float(effective_price)
        down_px = _clamp_binary_price(1.0 - float(effective_price))
    elif effective_outcome == "down":
        down_px = float(effective_price)
        up_px = _clamp_binary_price(1.0 - float(effective_price))
    else:
        # 兜底：沿用旧的方向解析逻辑（主要用于无法推断 effective_outcome 的情况）。
        up_last_price = float(features.up_last_price or 0.0)
        down_last_price = float(features.down_last_price or 0.0)
        if abs(features.price - up_last_price) <= 1e-12:
            up_last_price = effective_price
        elif abs(features.price - down_last_price) <= 1e-12:
            down_last_price = effective_price
        up_px, down_px = _resolve_outcome_prices(
            up_last_price,
            down_last_price,
            effective_price,
            infer_missing_with_binary_complement=True,
        )
    up_basis = _signal_basis_price(features.up_avg_price, features.up_signal_basis_price)
    down_basis = _signal_basis_price(features.down_avg_price, features.down_signal_basis_price)
    up_deviation = _deviation(up_px, up_basis)
    down_deviation = _deviation(down_px, down_basis)
    up_low, up_high = _implied_up_price_bounds(
        up_low=features.up_market_low,
        up_high=features.up_market_high,
        up_n=features.up_market_n,
        down_low=features.down_market_low,
        down_high=features.down_market_high,
        down_n=features.down_market_n,
    )
    price_percentile = _price_percentile(up_px, up_low, up_high)
    market_regime = (
        "trend"
        if features.trend_strength >= 0.50 or abs(price_move) > features.volatility * 0.5
        else "range"
    )
    confidence_proxy = _confidence_proxy(features.cycle_net_profit, price_move, features.volatility)
    return replace(
        features,
        price=effective_price,
        up_last_price=up_px,
        down_last_price=down_px,
        up_deviation=up_deviation,
        down_deviation=down_deviation,
        price_percentile=price_percentile,
        opening_vs_last_move=price_move,
        confidence_proxy=confidence_proxy,
        market_regime=market_regime,
    )


def snapshot_with_effective_quotes(
    features: FeatureSnapshot,
    *,
    up_price: float,
    down_price: float,
    active_outcome: Outcome | None = None,
) -> FeatureSnapshot:
    """
    使用「双边盘口快照」重建规则评估特征。

    这比 snapshot_with_effective_price(单值) 更稳健，尤其在 0.5 附近可避免
    up/down 方向歧义导致的错映射。
    """
    up_px, down_px = _resolve_outcome_prices(
        up_price,
        down_price,
        features.price,
        infer_missing_with_binary_complement=True,
    )
    if active_outcome == "up":
        effective_price = up_px
    elif active_outcome == "down":
        effective_price = down_px
    else:
        effective_price = features.price

    opening_level = features.price - features.opening_vs_last_move
    price_move = effective_price - opening_level
    up_basis = _signal_basis_price(features.up_avg_price, features.up_signal_basis_price)
    down_basis = _signal_basis_price(features.down_avg_price, features.down_signal_basis_price)
    up_deviation = _deviation(up_px, up_basis)
    down_deviation = _deviation(down_px, down_basis)
    up_low, up_high = _implied_up_price_bounds(
        up_low=features.up_market_low,
        up_high=features.up_market_high,
        up_n=features.up_market_n,
        down_low=features.down_market_low,
        down_high=features.down_market_high,
        down_n=features.down_market_n,
    )
    price_percentile = _price_percentile(up_px, up_low, up_high)
    market_regime = (
        "trend"
        if features.trend_strength >= 0.50 or abs(price_move) > features.volatility * 0.5
        else "range"
    )
    confidence_proxy = _confidence_proxy(features.cycle_net_profit, price_move, features.volatility)
    return replace(
        features,
        price=effective_price,
        up_last_price=up_px,
        down_last_price=down_px,
        up_deviation=up_deviation,
        down_deviation=down_deviation,
        price_percentile=price_percentile,
        opening_vs_last_move=price_move,
        confidence_proxy=confidence_proxy,
        market_regime=market_regime,
    )


def build_feature_snapshot(
    engine: StateEngine,
    cycle_seconds: int,
    last_minute_seconds: int,
    trend_window_secs: float = 6.0,
    phase_3_end_seconds: float = 240.0,
) -> FeatureSnapshot:
    if engine.state is None or engine.state.last_event_timestamp is None:
        raise RuntimeError("state engine has no market context")

    state = engine.state
    summary = settlement_summary(state)
    timestamp = state.last_event_timestamp
    cycle_start = state.cycle_start or timestamp
    elapsed = max(0.0, (timestamp - cycle_start).total_seconds())
    volatility = state.price_range()
    opening = state.opening_price or state.last_price
    price_move = state.last_price - opening
    volatility_ratio = volatility / opening if opening > 1e-10 else 0.0

    # ── 趋势强度重设计：基于最近 N 秒内市场成交的同向比例 ──────────────────────────────────
    # 用时间窗口而非枚举数量，确保在不同成交频率下覆盖相同时段
    _trend_window_secs = trend_window_secs
    _tape_all = list(engine.market_tape)
    _cutoff = timestamp - timedelta(seconds=_trend_window_secs)
    _recent_tape = [t for t in _tape_all if t.timestamp >= _cutoff]
    if len(_recent_tape) >= 3:
        # 趋势强度：以“同向成交的失衡程度”度量，取值范围应在 [0, 1]。
        # 旧实现使用了 dominant_count/total，导致 trend_strength 永远 >= 0.5，
        # 从而几乎总将市场误判为 trend，禁用依赖 range 的网格进出逻辑。
        _up_count = sum(1 for t in _recent_tape if t.outcome == "up")
        _total = len(_recent_tape)
        _down_count = _total - _up_count
        if _up_count == _down_count:
            trend_strength = 0.0
            trend_bias = None
        else:
            trend_bias = "up" if _up_count > _down_count else "down"
            # imbalance = (dominant - minor)/total, where dominant+minor=total
            trend_strength = abs(_up_count - _down_count) / _total
    else:
        trend_strength = 0.0
        trend_bias = None

    net_position_value = state.net_position_value()
    market_regime = "trend" if trend_strength >= 0.50 or abs(price_move) > volatility * 0.5 else "range"
    up_vwap = state.up_market_sum / state.up_market_n if state.up_market_n else 0.0
    down_vwap = state.down_market_sum / state.down_market_n if state.down_market_n else 0.0
    up_px, down_px = _resolve_outcome_prices(
        state.up_last_price,
        state.down_last_price,
        state.last_price,
        infer_missing_with_binary_complement=True,
    )
    # ── 反向价格联动推断：基于最新市场成交方向实时更新另一方向的隐含价格 ────────────────
    # 确保 UP 价格上涨时 DOWN 持仓的止损/止盈 能基于最新推断价格而非陈旧的历史成交价
    if state.market_last_outcome == "up" and state.up_last_price > 1e-9:
        down_px = _clamp_binary_price(1.0 - state.up_last_price)
    elif state.market_last_outcome == "down" and state.down_last_price > 1e-9:
        up_px = _clamp_binary_price(1.0 - state.down_last_price)
    up_low, up_high = _implied_up_price_bounds(
        up_low=state.up_market_low,
        up_high=state.up_market_high,
        up_n=state.up_market_n,
        down_low=state.down_market_low,
        down_high=state.down_market_high,
        down_n=state.down_market_n,
    )
    # ── 浮动盈亏：尾盘用结算估值，非尾盘用市价估值 ──────────────────────────────────────
    # 非尾盘阶段无法确认最终获胜方向（当前赔率不足以判断结算结果），
    # 强制结算估值会把另一方向持仓计为全损，误触发熔断机制（机制1）。
    # 进入 last_minute 后时间不够反转，才切换为结算价值做最终风控。
    _is_last_minute = elapsed >= max(0, cycle_seconds - last_minute_seconds)
    if _is_last_minute:
        _unrealized_up   = summary.unrealized_up_pnl
        _unrealized_down = summary.unrealized_down_pnl
        _cycle_net       = summary.cycle_net_profit
    else:
        _u = (state.up_position.held * (up_px - state.up_position.avg_price)
              if state.up_position.avg_price > 0 else 0.0)
        _d = (state.down_position.held * (down_px - state.down_position.avg_price)
              if state.down_position.avg_price > 0 else 0.0)
        _unrealized_up   = round(_u, 3)
        _unrealized_down = round(_d, 3)
        _cycle_net       = round(
            state.up_position.realized_pnl + state.down_position.realized_pnl + _u + _d, 3
        )
    # Re-entry 仅在 phase1~3 生效；phase4 不再使用清仓锚点恢复信号基准。
    phase_4_started = elapsed > float(phase_3_end_seconds)
    up_reentry_armed = (state.up_reentry_armed_at is not None) and (not phase_4_started)
    down_reentry_armed = (state.down_reentry_armed_at is not None) and (not phase_4_started)
    up_signal_basis = _signal_basis_price(
        state.up_position.avg_price,
        state.reentry_anchor_up_price if up_reentry_armed else 0.0,
    )
    down_signal_basis = _signal_basis_price(
        state.down_position.avg_price,
        state.reentry_anchor_down_price if down_reentry_armed else 0.0,
    )
    return FeatureSnapshot(
        market_id=state.market_id,
        cycle_id=state.cycle_id,
        timestamp=timestamp,
        price=state.last_price,
        cycle_elapsed_seconds=elapsed,
        is_last_minute=_is_last_minute,
        trend_bias=trend_bias,
        trend_strength=trend_strength,
        net_direction=state.net_direction(),
        net_position=state.net_position(),
        net_position_value=net_position_value,
        up_held=state.up_position.held,
        down_held=state.down_position.held,
        up_avg_price=state.up_position.avg_price,
        down_avg_price=state.down_position.avg_price,
        up_deviation=_deviation(up_px, up_signal_basis),
        down_deviation=_deviation(down_px, down_signal_basis),
        volatility=volatility,
        volatility_ratio=volatility_ratio,
        price_percentile=_price_percentile(up_px, up_low, up_high),
        realized_pnl=state.up_position.realized_pnl + state.down_position.realized_pnl,
        unrealized_up_pnl=_unrealized_up,
        unrealized_down_pnl=_unrealized_down,
        cycle_net_profit=_cycle_net,
        opening_vs_last_move=price_move,
        confidence_proxy=_confidence_proxy(_cycle_net, price_move, volatility),
        market_regime=market_regime,
        strategy_trades=state.strategy_trades,
        market_trades=state.market_trades,
        up_last_price=up_px,
        down_last_price=down_px,
        up_market_vwap=up_vwap,
        down_market_vwap=down_vwap,
        up_market_n=state.up_market_n,
        down_market_n=state.down_market_n,
        up_market_high=state.up_market_high,
        up_market_low=state.up_market_low,
        down_market_high=state.down_market_high,
        down_market_low=state.down_market_low,
        tape_low=state.low_price,
        tape_high=state.high_price,
        up_signal_basis_price=up_signal_basis,
        down_signal_basis_price=down_signal_basis,
        reentry_armed=up_reentry_armed or down_reentry_armed,
        up_reentry_armed=up_reentry_armed,
        down_reentry_armed=down_reentry_armed,
    )
