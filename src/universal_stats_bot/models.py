from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Outcome = Literal["up", "down"]
TradeAction = Literal["buy", "sell"]


@dataclass(slots=True)
class TradeEvent:
    market_id: str
    cycle_id: str
    timestamp: datetime
    price: float
    shares: float
    outcome: Outcome
    action: TradeAction = "buy"
    source: str = "market"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def signed_shares(self) -> float:
        return self.shares if self.action == "buy" else -self.shares