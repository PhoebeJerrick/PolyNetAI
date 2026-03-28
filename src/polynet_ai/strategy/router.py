from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os

from polynet_ai.domain.models import DecisionOutcome, FeatureSnapshot, OrderIntent, Outcome
from polynet_ai.strategy.entry_rules import (
    grid_entries,
    hedge_entries,
    mean_reversion_entries,
    opening_entries,
    trend_entries,
)
from polynet_ai.strategy.exit_rules import (
    grid_exits,
    hedge_exits,
    mean_reversion_exits,
    stop_loss_exits,
    take_profit_exits,
)
from polynet_ai.strategy.cycle_windows import calculate_position_percentage, determine_phase
from polynet_ai.strategy.features import snapshot_with_effective_price, snapshot_with_effective_quotes
from polynet_ai.strategy.last_minute import build_last_minute_candidate
from polynet_ai.strategy.spec import StrategyConfig


def adjust_priority_by_phase(
    intent: OrderIntent,
    features: FeatureSnapshot,
    config: StrategyConfig,
) -> None:
    """
    根据阶段和仓位状态动态调整规则优先级（A+C联合方案 — 方案C）

    直接修改 intent.priority（原地修改，无返回值）。
    Phase 4 不做调整，使用基础优先级。
    """
    phase = determine_phase(features.cycle_elapsed_seconds)
    if phase == 4:
        return

    pos_pct = calculate_position_percentage(features, config)

    if phase == 1:
        threshold = float(config.get("dynamic_priority.phase_1_position_threshold", 0.65))
        boost = int(config.get("dynamic_priority.phase_1_boost", 15))
        if pos_pct < threshold and intent.action == "buy" and intent.category in ("opening", "mean_reversion"):
            intent.priority -= boost

    elif phase == 2:
        threshold = float(config.get("dynamic_priority.phase_2_position_threshold", 0.50))
        boost = int(config.get("dynamic_priority.phase_2_boost", 15))
        if pos_pct > threshold and intent.action == "sell" and intent.category in ("grid", "take_profit"):
            intent.priority -= boost

    elif phase == 3:
        threshold = float(config.get("dynamic_priority.phase_3_position_threshold", 0.85))
        if pos_pct < threshold and intent.action == "buy":
            if intent.category == "trend":
                intent.priority -= int(config.get("dynamic_priority.phase_3_trend_boost", 25))
            elif intent.category == "grid":
                intent.priority -= int(config.get("dynamic_priority.phase_3_grid_boost", 15))


# 优化 #4：辅助函数用于规则执行（可被序列化）
def _execute_rule(rule_func, snapshot, config):
    """执行单个规则并返回结果"""
    try:
        return rule_func(snapshot, config)
    except Exception as e:
        import sys
        print(f"警告: 规则 {rule_func.__name__} 执行失败: {e}", file=sys.stderr)
        return []


class StrategyRouter:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self._feed_market_cycle: tuple[str, str] | None = None
        self._feed_last_at: dict[str, datetime] = {}
        self._feed_prices: dict[str, float] = {}
        self._feed_effective_outcomes: dict[str, Outcome | None] = {}
        self._feed_quotes: dict[str, tuple[float, float]] = {}
        # 优化 #4：从环境变量读取并行配置，默认启用
        self._enable_parallel = os.environ.get("POLYNET_PARALLEL_RULES", "1") == "1"
        self._max_workers = int(os.environ.get("POLYNET_MAX_WORKERS", "4"))

    def _reset_feed_context(self, features: FeatureSnapshot) -> None:
        ctx = (features.market_id, features.cycle_id)
        if self._feed_market_cycle != ctx:
            self._feed_market_cycle = ctx
            self._feed_last_at.clear()
            self._feed_prices.clear()
            self._feed_effective_outcomes.clear()
            self._feed_quotes.clear()

    @staticmethod
    def _feed_key(path: tuple[str, ...]) -> str:
        return ":".join(path)

    def _feed_interval_seconds(self, path: tuple[str, ...]) -> float:
        if path == ("last_minute",):
            return float(self.config.get("rule_price_feed.last_minute", 0.0))
        section, rule = path[0], path[1]
        return float(self.config.get(f"rule_price_feed.{section}.{rule}", 0.0))

    def _snapshot_for_rule(self, base: FeatureSnapshot, path: tuple[str, ...]) -> FeatureSnapshot:
        interval = self._feed_interval_seconds(path)
        if interval <= 0:
            return base
        key = self._feed_key(path)
        now = base.timestamp
        latest = base.price
        last_at = self._feed_last_at.get(key)
        if last_at is None or (now - last_at).total_seconds() >= interval:
            self._feed_prices[key] = latest
            self._feed_last_at[key] = now
            self._feed_quotes[key] = (float(base.up_last_price), float(base.down_last_price))
            # 推断本次缓存的价格来自哪个 outcome（由 base.price 对应到 up/down last_price）
            effective_outcome: Outcome | None = None
            if base.up_last_price > 1e-12 and abs(base.price - base.up_last_price) <= 1e-12:
                effective_outcome = "up"
            elif base.down_last_price > 1e-12 and abs(base.price - base.down_last_price) <= 1e-12:
                effective_outcome = "down"
            self._feed_effective_outcomes[key] = effective_outcome
        effective = self._feed_prices.get(key, latest)
        cached_quote = self._feed_quotes.get(key, (float(base.up_last_price), float(base.down_last_price)))
        cached_up, cached_down = cached_quote
        if (
            abs(effective - base.price) <= 1e-15
            and abs(cached_up - float(base.up_last_price)) <= 1e-15
            and abs(cached_down - float(base.down_last_price)) <= 1e-15
        ):
            return base
        effective_outcome = self._feed_effective_outcomes.get(key)
        # 用双边价格快照重建规则特征，避免 0.5 附近单价映射歧义。
        return snapshot_with_effective_quotes(
            base,
            up_price=cached_up,
            down_price=cached_down,
            active_outcome=effective_outcome,
        )

    def route(self, features: FeatureSnapshot, strategy_trades: int = 0) -> DecisionOutcome:
        self._reset_feed_context(features)

        # 优化 #4：并行化规则评估，11个规则可独立并行执行
        # 定义所有规则及其对应的路径配置
        rule_specs = [
            (build_last_minute_candidate, ("last_minute",)),
            (stop_loss_exits, ("exits", "stop_loss")),
            (hedge_exits, ("exits", "hedge")),
            (take_profit_exits, ("exits", "take_profit")),
            (grid_exits, ("exits", "grid")),
            (mean_reversion_exits, ("exits", "mean_reversion")),
            (opening_entries, ("entries", "opening")),
            (hedge_entries, ("entries", "hedge")),
            (grid_entries, ("entries", "grid")),
            (mean_reversion_entries, ("entries", "mean_reversion")),
            (trend_entries, ("entries", "trend")),
        ]

        candidates: list[OrderIntent] = []

        # 优化 #4：可配置的并行执行（通过环境变量控制）
        if self._enable_parallel:
            # 使用线程池并行执行所有规则
            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                futures = {
                    executor.submit(
                        _execute_rule,
                        rule_func,
                        self._snapshot_for_rule(features, path),
                        self.config
                    ): rule_func
                    for rule_func, path in rule_specs
                }

                # 按完成顺序收集结果
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        candidates.extend(result)
        else:
            # 顺序执行（用于调试或对比）
            for rule_func, path in rule_specs:
                result = _execute_rule(rule_func, self._snapshot_for_rule(features, path), self.config)
                if result:
                    candidates.extend(result)

        for candidate in candidates:
            candidate.metadata["strategy_trades"] = strategy_trades
        candidates = [candidate for candidate in candidates if candidate.shares > 0]

        # A+C联合方案：根据阶段和仓位动态调整优先级
        for candidate in candidates:
            adjust_priority_by_phase(candidate, features, self.config)

        candidates.sort(key=lambda item: (item.priority, -item.shares))
        return DecisionOutcome(selected=candidates[0] if candidates else None, candidates=candidates)
