from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from polynet_ai.domain.models import FillEvent, OrderIntent


@dataclass(slots=True)
class PaperBroker:
    fee_rate: float = 0.002
    slippage_bps: float = 10.0

    def execute(self, intent: OrderIntent, timestamp: datetime) -> FillEvent:
        slippage = intent.reference_price * (self.slippage_bps / 10_000.0)
        price = intent.reference_price + slippage if intent.action == "buy" else max(0.0, intent.reference_price - slippage)
        fee = price * intent.shares * self.fee_rate
        return FillEvent(
            market_id=intent.market_id,
            cycle_id=intent.cycle_id,
            timestamp=timestamp,
            price=price,
            shares=intent.shares,
            outcome=intent.outcome,
            action=intent.action,
            fee=fee,
            slippage=slippage,
            reason=intent.reason,
        )
