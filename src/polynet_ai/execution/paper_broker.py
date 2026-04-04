from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from polynet_ai.domain.models import ExecutionResult, FillEvent, OrderIntent


@dataclass(slots=True)
class PaperBroker:
    fee_rate: float = 0.002
    slippage_bps: float = 10.0

    def execute(self, intent: OrderIntent, timestamp: datetime) -> ExecutionResult:
        slippage = intent.reference_price * (self.slippage_bps / 10_000.0)
        price = intent.reference_price + slippage if intent.action == "buy" else max(0.0, intent.reference_price - slippage)
        fee = price * intent.shares * self.fee_rate
        return ExecutionResult(
            status="filled",
            fill=FillEvent(
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
                fill_source="paper_simulated",
            ),
        )

    def poll(self, timestamp: datetime) -> list[FillEvent]:
        return []

    def pending_context(self) -> dict[str, float | int]:
        return {
            "pending_order_count": 0,
            "pending_buy_reserved_cash": 0.0,
            "pending_up_sell_shares": 0.0,
            "pending_down_sell_shares": 0.0,
        }
