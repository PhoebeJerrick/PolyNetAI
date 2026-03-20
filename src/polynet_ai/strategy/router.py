from __future__ import annotations

from polynet_ai.domain.models import DecisionOutcome, FeatureSnapshot, OrderIntent
from polynet_ai.strategy.entry_rules import build_entry_candidates
from polynet_ai.strategy.exit_rules import build_exit_candidates
from polynet_ai.strategy.last_minute import build_last_minute_candidate
from polynet_ai.strategy.spec import StrategyConfig


class StrategyRouter:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def route(self, features: FeatureSnapshot, strategy_trades: int = 0) -> DecisionOutcome:
        candidates: list[OrderIntent] = []
        candidates.extend(build_last_minute_candidate(features, self.config))
        candidates.extend(build_exit_candidates(features, self.config))
        candidates.extend(build_entry_candidates(features, self.config))
        for candidate in candidates:
            candidate.metadata["strategy_trades"] = strategy_trades
        candidates = [candidate for candidate in candidates if candidate.shares > 0]
        candidates.sort(key=lambda item: (item.priority, -item.shares))
        return DecisionOutcome(selected=candidates[0] if candidates else None, candidates=candidates)
