from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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
from polynet_ai.strategy.cycle_windows import (
    calculate_position_percentage,
    determine_phase,
    is_rule_enabled_for_phase,
)
from polynet_ai.strategy.dynamic_priority import apply_dynamic_priorities_to_candidates
from polynet_ai.strategy.features import snapshot_with_effective_price, snapshot_with_effective_quotes
from polynet_ai.strategy.last_minute import build_last_minute_candidate
from polynet_ai.strategy.spec import StrategyConfig


class StrategyRouter:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self._feed_market_cycle: tuple[str, str] | None = None
        self._feed_last_at: dict[str, datetime] = {}
        self._feed_prices: dict[str, float] = {}
        self._feed_effective_outcomes: dict[str, Outcome | None] = {}
        self._feed_quotes: dict[str, tuple[float, float]] = {}

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
        phase = determine_phase(features.cycle_elapsed_seconds, self.config)
        
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
        
        # 使用线程池并行执行所有规则（受GIL影响较小，主要是I/O和数据处理）
        with ThreadPoolExecutor(max_workers=4) as executor:
            filtered_specs = [
                (rule_func, path)
                for rule_func, path in rule_specs
                if self._rule_spec_enabled(path, phase)
            ]
            futures = {
                executor.submit(
                    rule_func,
                    self._snapshot_for_rule(features, path),
                    self.config
                ): path
                for rule_func, path in filtered_specs
            }
            
            # 按完成顺序收集结果（不一定是提交顺序）
            for future in as_completed(futures):
                try:
                    path = futures[future]
                    result = future.result()
                    if result:
                        rule_scope = ":".join(path)
                        for item in result:
                            item.metadata["_rule_scope"] = rule_scope
                        candidates.extend(result)
                except Exception as e:
                    # 如果规则执行错误，记录但继续处理其他规则
                    import sys
                    print(f"警告: 规则执行失败: {e}", file=sys.stderr)
                    continue

        for candidate in candidates:
            candidate.metadata["strategy_trades"] = strategy_trades
        candidates = [candidate for candidate in candidates if candidate.shares > 0]
        candidates = apply_dynamic_priorities_to_candidates(candidates, features, self.config)
        candidates.sort(key=lambda item: (item.priority, -item.shares))
        if not candidates:
            return DecisionOutcome(selected=None, candidates=[])

        phase3_preferred_buy = self._phase3_prefer_buy_candidate(candidates, features, phase)
        if phase3_preferred_buy is not None:
            preferred_candidates = [
                item
                for item in candidates
                if item.action == "buy" and item.category in {"trend", "hedge"}
            ]
            preferred_candidates.sort(key=lambda item: (item.priority, -item.shares))
            return DecisionOutcome(
                selected=phase3_preferred_buy,
                candidates=preferred_candidates,
            )

        guard_fallback = self._fallback_buy_for_low_balance(candidates, features, phase)
        if guard_fallback is not None:
            fallback_scope = str(guard_fallback.metadata.get("_rule_scope", ""))
            fallback_scoped_candidates = [
                item
                for item in candidates
                if str(item.metadata.get("_rule_scope", "")) == fallback_scope
            ]
            fallback_scoped_candidates.sort(key=lambda item: (item.priority, -item.shares))
            return DecisionOutcome(
                selected=guard_fallback,
                candidates=fallback_scoped_candidates,
            )

        # 仅允许“规则内候选”：锁定最优先候选所属规则作用域，
        # 回退仅在该规则作用域内进行，不跨规则尝试。
        top_scope = str(candidates[0].metadata.get("_rule_scope", ""))
        scoped_candidates = [
            item
            for item in candidates
            if str(item.metadata.get("_rule_scope", "")) == top_scope
        ]
        scoped_candidates.sort(key=lambda item: (item.priority, -item.shares))
        return DecisionOutcome(
            selected=scoped_candidates[0] if scoped_candidates else None,
            candidates=scoped_candidates,
        )

    def _phase3_prefer_buy_candidate(
        self,
        candidates: list[OrderIntent],
        features: FeatureSnapshot,
        phase: int,
    ) -> OrderIntent | None:
        """Phase3 在未达目标仓位时，卖出候选可让位给 trend/hedge 买入。"""
        if phase != 3 or not candidates:
            return None
        top = candidates[0]
        if top.action != "sell":
            return None
        # 仅在网格减仓占顶时允许“买入优先”回退，避免压制止盈/止损类风险动作。
        if top.category != "grid":
            return None
        target_thr = float(self.config.get("dynamic_priority.phase_3_position_threshold", 0.75))
        pos_pct = calculate_position_percentage(features, self.config)
        if pos_pct >= target_thr:
            return None
        preferred_buys = [
            item
            for item in candidates
            if item.action == "buy" and item.category in {"trend", "hedge"}
        ]
        if not preferred_buys:
            return None
        preferred_buys.sort(key=lambda item: (item.priority, -item.shares))
        return preferred_buys[0]

    def _fallback_buy_for_low_balance(
        self,
        candidates: list[OrderIntent],
        features: FeatureSnapshot,
        phase: int,
    ) -> OrderIntent | None:
        # 阶段1~3防止方向持仓被卖到 0：低仓位时若首选为卖出，回退到同方向买入规则。
        if phase < 1 or phase > 3 or not candidates:
            return None
        top = candidates[0]
        if top.action != "sell":
            return None

        held = features.up_held if top.outcome == "up" else features.down_held
        if held > 5.0:
            return None

        for item in candidates[1:]:
            if item.action == "buy" and item.outcome == top.outcome:
                return item
        return None

    def _rule_spec_enabled(self, path: tuple[str, ...], phase: int) -> bool:
        if path == ("last_minute",):
            return is_rule_enabled_for_phase(
                self.config,
                section="last_minute",
                rule="last_minute",
                phase=phase,
            )
        section, rule = path[0], path[1]
        return is_rule_enabled_for_phase(
            self.config,
            section=section,
            rule=rule,
            phase=phase,
        )
