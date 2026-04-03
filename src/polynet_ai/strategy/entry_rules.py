from __future__ import annotations

from polynet_ai.domain.models import FeatureSnapshot, OrderIntent, Outcome
from polynet_ai.strategy.cycle_windows import determine_phase, rule_disabled_in_cycle_tail
from polynet_ai.strategy.spec import StrategyConfig
from polynet_ai.strategy.price_reference import outcome_reference_price


def _base_size(config: StrategyConfig, features: FeatureSnapshot) -> float:
    base_order_size = float(
        config.get(
            "order_sizing.buy.base_order_size",
            config.get("order_sizing.base_order_size", 5.0),
        )
    )
    volatility_order_scale = float(
        config.get(
            "order_sizing.buy.volatility_order_scale",
            config.get("order_sizing.volatility_order_scale", 10.0),
        )
    )
    return base_order_size + (
        features.volatility_ratio * volatility_order_scale
    )


def _clamp_binary_price(p: float) -> float:
    return max(0.01, min(0.99, p))


def _grid_phase_percentiles(config: StrategyConfig, phase: int) -> tuple[float, float]:
    low = float(
        config.get(
            f"grid.phase_{phase}_low_percentile",
            config.get("grid.grid_low_percentile", 0.25),
        )
    )
    high = float(
        config.get(
            f"grid.phase_{phase}_high_percentile",
            config.get("grid.grid_high_percentile", 0.75),
        )
    )
    return low, high


def _opening_weak_outcome(features: FeatureSnapshot, use_comp: bool) -> Outcome | None:
    eps = 1e-9
    pu, pd = features.up_last_price, features.down_last_price
    nu, nd = features.up_market_n, features.down_market_n
    if pu > eps and pd > eps:
        if abs(pu - pd) < 1e-12:
            return None
        return "up" if pu < pd else "down"
    if not use_comp:
        return None
    if nu >= 1 and nd == 0:
        pim = _clamp_binary_price(1.0 - pu)
        if abs(pu - pim) < 1e-12:
            return None
        return "down" if pim < pu else "up"
    if nd >= 1 and nu == 0:
        pim = _clamp_binary_price(1.0 - pd)
        if abs(pd - pim) < 1e-12:
            return None
        return "up" if pim < pd else "down"
    return None


def _opening_price_timing_ok(
    weak: Outcome,
    features: FeatureSnapshot,
    vwap_eps: float,
    range_frac: float,
    min_range: float,
) -> bool:
    if weak == "up":
        n = features.up_market_n
        p = features.up_last_price
        vwap = features.up_market_vwap
        lo, hi = features.up_market_low, features.up_market_high
    else:
        n = features.down_market_n
        p = features.down_last_price
        vwap = features.down_market_vwap
        lo, hi = features.down_market_low, features.down_market_high
    if n < 1 or p <= 1e-9:
        return False
    if n == 1:
        return True
    if p <= vwap + vwap_eps:
        return True
    span = hi - lo
    if span >= min_range and p <= lo + range_frac * span + vwap_eps:
        return True
    return False


def opening_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """
    改进的开盘试探规则：添加多指标确认机制
    - 原有：单一信号入场 (弱势一侧)
    - 改进：需要2个指标确认才入场
      1. 弱势一侧价格检查
      2. 流动性确认
      3. 市场状态确认 (可选)
    """
    if not config.get("opening_entry.enabled", True):
        return []
    if features.is_last_minute or features.strategy_trades != 0:
        return []
    window = float(config.get("opening_entry.window_seconds", 30.0))
    if features.cycle_elapsed_seconds > window:
        return []
    use_comp = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    weak = _opening_weak_outcome(features, use_comp)
    if weak is None:
        return []
    
    # 【改进1】流动性检查 - 确保有足够的市场成交
    min_market_trades = int(config.get("opening_entry.min_market_trades", 2))
    if weak == "up" and features.up_market_n < min_market_trades:
        return []
    if weak == "down" and features.down_market_n < min_market_trades:
        return []
    
    # 【改进2】价格时机确认 - 原有逻辑
    if not _opening_price_timing_ok(
        weak,
        features,
        float(config.get("opening_entry.vwap_epsilon", 0.01)),
        float(config.get("opening_entry.range_low_fraction", 0.35)),
        float(config.get("opening_entry.min_range_width", 0.02)),
    ):
        return []
    
    size = _base_size(config, features)
    ref = features.up_last_price if weak == "up" else features.down_last_price
    phase = determine_phase(features.cycle_elapsed_seconds, config)
    return [
        OrderIntent(
            market_id=features.market_id,
            cycle_id=features.cycle_id,
            outcome=weak,
            action="buy",
            shares=size,
            reference_price=ref,
            category="opening",
            reason=f"开盘试探建仓：买入相对低价（{weak}方），流动性+价格时机确认",
            priority=int(config.priority_for("opening", phase)),
        )
    ]


def trend_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if features.is_last_minute or not features.trend_bias:
        return []
    if features.trend_strength < float(config.get("trend.min_trend_strength", 0.35)):
        return []

    price_edge = float(config.get("trend.trend_price_edge", 0.03))
    deviation = features.up_deviation if features.trend_bias == "up" else features.down_deviation
    if deviation < price_edge:
        return []

    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    
    # 计算基础下单量
    base_size = _base_size(config, features)
    trend_scale = float(config.get("trend.trend_scale", 0.1))
    position_add = abs(features.net_position) * trend_scale
    size = base_size + position_add
    
    # 添加上限约束 (新增)
    max_trend_order_size = float(config.get("trend.max_trend_order_size", 80.0))
    size = min(size, max_trend_order_size)
    
    ref = outcome_reference_price(features, features.trend_bias, infer_missing_with_binary_complement=infer_missing)
    phase = determine_phase(features.cycle_elapsed_seconds, config)
    return [
        OrderIntent(
            market_id=features.market_id,
            cycle_id=features.cycle_id,
            outcome=features.trend_bias,
            action="buy",
            shares=size,
            reference_price=ref,
            category="trend",
            reason="趋势确认后顺势加仓",
            priority=int(config.priority_for("trend", phase)),
        )
    ]


def hedge_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if features.is_last_minute:
        return []
    trigger = float(config.get("exposure.hedge_trigger_value", 50.0))
    exposure = abs(features.net_position_value)
    if exposure < trigger or features.net_direction in {"空仓", "平衡"}:
        return []

    opposite = "down" if features.net_direction == "Up" else "up"
    excess = max(0.0, exposure - trigger)
    size = _base_size(config, features) + excess * float(config.get("exposure.hedge_scale", 0.15))
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    ref = outcome_reference_price(features, opposite, infer_missing_with_binary_complement=infer_missing)
    phase = determine_phase(features.cycle_elapsed_seconds, config)
    # Hotfix: 避免在亏损周期里继续对冲“亏损腿”，导致在 P3/P4 出现反复做T式回补。
    disable_raw = config.get("exposure.hedge_disable_when_cycle_net_profit_negative_in_phases", None)
    disable_set: set[int] = set()
    if isinstance(disable_raw, list):
        for x in disable_raw:
            try:
                disable_set.add(int(x))
            except (TypeError, ValueError):
                continue
    elif isinstance(disable_raw, str):
        for part in disable_raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                disable_set.add(int(part))
            except ValueError:
                continue
    if phase in disable_set and features.cycle_net_profit < 0:
        return []
    return [
        OrderIntent(
            market_id=features.market_id,
            cycle_id=features.cycle_id,
            outcome=opposite,
            action="buy",
            shares=size,
            reference_price=ref,
            category="hedge",
            reason="净敞口过大，执行反向对冲买入",
            priority=int(config.priority_for("hedge", phase)),
        )
    ]


def grid_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    if rule_disabled_in_cycle_tail(features, config, "grid"):
        return []
    phase = determine_phase(features.cycle_elapsed_seconds, config)
    if features.is_last_minute:
        return []
    if features.market_regime != "range":
        # V5 语义：在“潜在优势侧”阶段允许用 grid 做价差/做 T。
        # 仅在配置声明的阶段启用（默认仍严格 range）。
        allow_raw = config.get("grid.allow_in_trend_phases", None)
        allow_set: set[int] = set()
        if isinstance(allow_raw, list):
            for x in allow_raw:
                try:
                    allow_set.add(int(x))
                except (TypeError, ValueError):
                    continue
        elif isinstance(allow_raw, str):
            for part in allow_raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    allow_set.add(int(part))
                except ValueError:
                    continue
        if phase not in allow_set:
            return []

    # Hotfix: 只允许网格在“优势腿”（当前 net_direction 对应的 outcome）上做T。
    align_raw = config.get("grid.enforce_net_direction_alignment_in_phases", None)
    align_set: set[int] = set()
    if isinstance(align_raw, list):
        for x in align_raw:
            try:
                align_set.add(int(x))
            except (TypeError, ValueError):
                continue
    elif isinstance(align_raw, str):
        for part in align_raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                align_set.add(int(part))
            except ValueError:
                continue
    if phase in align_set and features.net_direction not in {"Up", "Down"}:
        return []
    # 首单由 opening_entries 负责；grid 仅在已有至少一笔策略成交后介入
    if features.strategy_trades == 0:
        return []

    size = _base_size(config, features)
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    pri = int(config.priority_for("grid", phase))
    low, high = _grid_phase_percentiles(config, phase)
    if features.price_percentile <= low:
        if phase in align_set and features.net_direction != "Up":
            return []
        ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
        return [
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="up",
                action="buy",
                shares=size,
                reference_price=ref,
                category="grid",
                reason=f"震荡区间低位买入 Up（阶段{phase}）",
                priority=pri,
            )
        ]
    if features.price_percentile >= high:
        if phase in align_set and features.net_direction != "Down":
            return []
        ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
        return [
            OrderIntent(
                market_id=features.market_id,
                cycle_id=features.cycle_id,
                outcome="down",
                action="buy",
                shares=size,
                reference_price=ref,
                category="grid",
                reason=f"震荡区间高位买入 Down（阶段{phase}）",
                priority=pri,
            )
        ]
    return []


def mean_reversion_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """
    改进的均值回归规则：优先平衡净方向
    
    逻辑改进：
    1. 当有大额单向持仓时，优先平衡反向
    2. 仅在净方向平衡后，才允许双向建仓
    3. 加入头寸大小限制，防止过度集中
    """
    if not bool(config.get("mean_reversion.enabled", True)):
        return []
    if rule_disabled_in_cycle_tail(features, config, "mean_reversion"):
        return []
    if features.is_last_minute:
        return []
    
    up_threshold = float(config.get("mean_reversion.up_buy_deviation", 0.10))
    down_threshold = float(config.get("mean_reversion.down_buy_deviation", 0.10))
    deviation_scale = float(config.get("mean_reversion.deviation_scale", 45.0))
    
    # 【新增】头寸限制
    max_net_position = float(config.get("mean_reversion.max_net_position", 60.0))
    
    intents: list[OrderIntent] = []
    infer_missing = bool(config.get("opening_entry.infer_missing_with_binary_complement", True))
    phase = determine_phase(features.cycle_elapsed_seconds, config)
    mr_pri = int(config.priority_for("mean_reversion", phase))

    # 【改进】优先平衡大额单向持仓逻辑
    # 如果持仓过于倾斜，优先平衡反向
    abs_net = abs(features.net_position)
    imbalance_threshold = float(config.get("mean_reversion.position_imbalance_threshold", 30.0))
    
    if abs_net > imbalance_threshold:
        # 持仓严重不平衡，需要立即平衡
        if features.net_direction == "Up" and features.down_held < abs_net * 0.5:
            # Up方向过多，优先买Down平衡
            if features.down_deviation <= -down_threshold:
                ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
                balance_size = min(
                    _base_size(config, features) + abs(features.down_deviation) * deviation_scale,
                    abs_net * 0.8  # 平衡目标：至少缩小到80%
                )
                intents.append(
                    OrderIntent(
                        market_id=features.market_id,
                        cycle_id=features.cycle_id,
                        outcome="down",
                        action="buy",
                        shares=balance_size,
                        reference_price=ref,
                        category="mean_reversion",
                        reason=f"优先平衡持仓不均：Up {features.up_held:.1f}占优，买入 Down 平衡",
                        priority=mr_pri + 5,
                    )
                )
                return intents  # 优先执行平衡，暂不进行其他操作
        
        elif features.net_direction == "Down" and features.up_held < abs_net * 0.5:
            # Down方向过多，优先买Up平衡
            if features.up_deviation >= up_threshold:
                ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
                balance_size = min(
                    _base_size(config, features) + features.up_deviation * deviation_scale,
                    abs_net * 0.8  # 平衡目标
                )
                intents.append(
                    OrderIntent(
                        market_id=features.market_id,
                        cycle_id=features.cycle_id,
                        outcome="up",
                        action="buy",
                        shares=balance_size,
                        reference_price=ref,
                        category="mean_reversion",
                        reason=f"优先平衡持仓不均：Down {features.down_held:.1f}占优，买入 Up 平衡",
                        priority=mr_pri + 5,
                    )
                )
                return intents  # 优先执行平衡，暂不进行其他操作
    
    # 【保留】原有双向建仓逻辑（仅在持仓平衡时执行）
    if abs_net <= imbalance_threshold:
        if features.up_deviation >= up_threshold and features.net_position < max_net_position:
            ref = outcome_reference_price(features, "up", infer_missing_with_binary_complement=infer_missing)
            intents.append(
                OrderIntent(
                    market_id=features.market_id,
                    cycle_id=features.cycle_id,
                    outcome="up",
                    action="buy",
                    shares=_base_size(config, features) + features.up_deviation * deviation_scale,
                    reference_price=ref,
                    category="mean_reversion",
                    reason="Up 价格显著高于均价，执行均值回归买入",
                    priority=mr_pri,
                )
            )
        if features.down_deviation <= -down_threshold and features.net_position > -max_net_position:
            ref = outcome_reference_price(features, "down", infer_missing_with_binary_complement=infer_missing)
            intents.append(
                OrderIntent(
                    market_id=features.market_id,
                    cycle_id=features.cycle_id,
                    outcome="down",
                    action="buy",
                    shares=_base_size(config, features) + abs(features.down_deviation) * deviation_scale,
                    reference_price=ref,
                    category="mean_reversion",
                    reason="Down 价格显著低于均价，执行均值回归买入",
                    priority=mr_pri,
                )
            )
    
    return intents


def build_entry_candidates(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    candidates: list[OrderIntent] = []
    candidates.extend(opening_entries(features, config))
    candidates.extend(hedge_entries(features, config))
    candidates.extend(grid_entries(features, config))
    candidates.extend(mean_reversion_entries(features, config))
    candidates.extend(trend_entries(features, config))
    return candidates
