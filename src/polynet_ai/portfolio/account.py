from __future__ import annotations

from dataclasses import dataclass, field

from polynet_ai.domain.models import FillEvent


@dataclass(slots=True)
class Account:
    starting_cash: float = 100.0
    cash: float = 100.0
    reserved_cash: float = 0.0
    fees_paid: float = 0.0
    fills: list[FillEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = self.starting_cash
        self.reserved_cash = 0.0

    @property
    def available_cash(self) -> float:
        return max(0.0, self.cash - self.reserved_cash)

    def reserve_cash(self, amount: float) -> None:
        if amount <= 0:
            return
        self.reserved_cash += amount

    def release_cash(self, amount: float) -> None:
        if amount <= 0:
            return
        self.reserved_cash = max(0.0, self.reserved_cash - amount)

    def apply_fill(self, fill: FillEvent) -> None:
        self.release_cash(fill.reserved_cash)
        gross = fill.price * fill.shares
        if fill.action == "buy":
            self.cash -= gross + fill.fee
        else:
            self.cash += gross - fill.fee
        self.fees_paid += fill.fee
        self.fills.append(fill)
