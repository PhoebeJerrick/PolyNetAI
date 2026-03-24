from __future__ import annotations

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
        candidates: list[OrderIntent] = []

        candidates.extend(
            build_last_minute_candidate(self._snapshot_for_rule(features, ("last_minute",)), self.config)
        )
        candidates.extend(
            stop_loss_exits(self._snapshot_for_rule(features, ("exits", "stop_loss")), self.config)
        )
        candidates.extend(hedge_exits(self._snapshot_for_rule(features, ("exits", "hedge")), self.config))
        candidates.extend(
            take_profit_exits(self._snapshot_for_rule(features, ("exits", "take_profit")), self.config)
        )
        candidates.extend(grid_exits(self._snapshot_for_rule(features, ("exits", "grid")), self.config))
        candidates.extend(
            mean_reversion_exits(self._snapshot_for_rule(features, ("exits", "mean_reversion")), self.config)
        )

        candidates.extend(
            opening_entries(self._snapshot_for_rule(features, ("entries", "opening")), self.config)
        )
        candidates.extend(hedge_entries(self._snapshot_for_rule(features, ("entries", "hedge")), self.config))
        candidates.extend(grid_entries(self._snapshot_for_rule(features, ("entries", "grid")), self.config))
        candidates.extend(
            mean_reversion_entries(self._snapshot_for_rule(features, ("entries", "mean_reversion")), self.config)
        )
        candidates.extend(trend_entries(self._snapshot_for_rule(features, ("entries", "trend")), self.config))

        for candidate in candidates:
            candidate.metadata["strategy_trades"] = strategy_trades
        candidates = [candidate for candidate in candidates if candidate.shares > 0]
        candidates.sort(key=lambda item: (item.priority, -item.shares))
        return DecisionOutcome(selected=candidates[0] if candidates else None, candidates=candidates)
