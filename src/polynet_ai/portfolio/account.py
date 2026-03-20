from __future__ import annotations

from dataclasses import dataclass, field

from polynet_ai.domain.models import FillEvent


@dataclass(slots=True)
class Account:
    starting_cash: float = 1000.0
    cash: float = 1000.0
    fees_paid: float = 0.0
    fills: list[FillEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.starting_cash

    def apply_fill(self, fill: FillEvent) -> None:
        gross = fill.price * fill.shares
        if fill.action == "buy":
            self.cash -= gross + fill.fee
        else:
            self.cash += gross - fill.fee
        self.fees_paid += fill.fee
        self.fills.append(fill)
