from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Outcome = Literal["up", "down"]
TradeAction = Literal["buy", "sell"]
RuleCategory = Literal[
    "trend",
    "hedge",
    "grid",
    "mean_reversion",
    "opening",
    "take_profit",
    "stop_loss",
    "last_minute",
    "risk",
    "none",
]


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


@dataclass(slots=True)
class FillEvent:
    market_id: str
    cycle_id: str
    timestamp: datetime
    price: float
    shares: float
    outcome: Outcome
    action: TradeAction
    fee: float = 0.0
    slippage: float = 0.0
    reason: str = ""
    reserved_cash: float = 0.0

    @property
    def signed_shares(self) -> float:
        return self.shares if self.action == "buy" else -self.shares


BrokerExecutionStatus = Literal[
    "filled",
    "submitted",
    "rejected",
    "no_liquidity",
    "invalid_amount",
]


@dataclass(slots=True)
class ExecutionResult:
    status: BrokerExecutionStatus
    fill: FillEvent | None = None
    order_id: str = ""
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderIntent:
    market_id: str
    cycle_id: str
    outcome: Outcome
    action: TradeAction
    shares: float
    reference_price: float
    category: RuleCategory
    reason: str
    priority: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def clipped(self, min_shares: float, max_shares: float) -> "OrderIntent | None":
        size = max(min_shares, min(max_shares, self.shares))
        if size <= 0:
            return None
        return OrderIntent(
            market_id=self.market_id,
            cycle_id=self.cycle_id,
            outcome=self.outcome,
            action=self.action,
            shares=size,
            reference_price=self.reference_price,
            category=self.category,
            reason=self.reason,
            priority=self.priority,
            metadata=dict(self.metadata),
        )


@dataclass(slots=True)
class PositionBook:
    held: float = 0.0
    cost: float = 0.0
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    last_price: float = 0.0

    def apply_fill(self, fill: FillEvent) -> float:
        signed = fill.signed_shares
        shares = abs(signed)
        realized = 0.0
        self.last_price = fill.price
        if signed > 0:
            self.held += shares
            self.cost += shares * fill.price
            self.avg_price = self.cost / self.held if self.held else 0.0
            return 0.0

        realized = shares * (fill.price - self.avg_price)
        self.realized_pnl += realized
        self.held -= shares
        if self.held <= 1e-10:
            self.held = 0.0
            self.cost = 0.0
            self.avg_price = 0.0
        else:
            self.cost = self.held * self.avg_price
        return realized


@dataclass(slots=True)
class CycleState:
    market_id: str
    cycle_id: str
    cycle_start: datetime | None = None
    cycle_end: datetime | None = None
    up_balance: float = 0.0
    down_balance: float = 0.0
    up_position: PositionBook = field(default_factory=PositionBook)
    down_position: PositionBook = field(default_factory=PositionBook)
    opening_price: float | None = None
    last_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    up_last_price: float = 0.0
    down_last_price: float = 0.0
    market_last_price: float = 0.0
    market_last_outcome: Outcome | None = None
    up_market_sum: float = 0.0
    up_market_n: int = 0
    up_market_high: float = 0.0
    up_market_low: float = 0.0
    down_market_sum: float = 0.0
    down_market_n: int = 0
    down_market_high: float = 0.0
    down_market_low: float = 0.0
    market_trades: int = 0
    strategy_trades: int = 0
    consecutive_outcome: Outcome | None = None
    consecutive_outcome_count: int = 0
    consecutive_action: TradeAction | None = None
    consecutive_action_count: int = 0
    last_event_timestamp: datetime | None = None
    max_abs_net_exposure: float = 0.0
    reentry_anchor_up_price: float = 0.0
    reentry_anchor_down_price: float = 0.0
    reentry_armed_at: datetime | None = None

    def price_range(self) -> float:
        if self.market_trades == 0:
            return 0.0
        return max(0.0, self.high_price - self.low_price)

    def total_position(self) -> float:
        return self.up_balance + self.down_balance

    def net_position(self) -> float:
        return self.up_balance - self.down_balance

    def net_direction(self) -> str:
        up = round(self.up_balance, 10)
        down = round(self.down_balance, 10)
        if abs(up + down) < 1e-10:
            return "空仓"
        if abs(up - down) < 1e-10:
            return "平衡"
        return "Up" if up > down else "Down"

    def net_position_value(self) -> float:
        direction = self.net_direction()
        net_shares = self.net_position()
        if direction in {"空仓", "平衡"}:
            return 0.0
        if direction == "Up":
            return net_shares * self.up_position.avg_price
        return net_shares * self.down_position.avg_price


@dataclass(slots=True)
class FeatureSnapshot:
    market_id: str
    cycle_id: str
    timestamp: datetime
    price: float
    cycle_elapsed_seconds: float
    is_last_minute: bool
    trend_bias: Outcome | None
    trend_strength: float
    net_direction: str
    net_position: float
    net_position_value: float
    up_held: float
    down_held: float
    up_avg_price: float
    down_avg_price: float
    up_deviation: float
    down_deviation: float
    volatility: float
    volatility_ratio: float
    price_percentile: float
    realized_pnl: float
    unrealized_up_pnl: float
    unrealized_down_pnl: float
    cycle_net_profit: float
    opening_vs_last_move: float
    confidence_proxy: float
    market_regime: Literal["trend", "range"]
    strategy_trades: int
    market_trades: int
    up_last_price: float
    down_last_price: float
    up_market_vwap: float
    down_market_vwap: float
    up_market_n: int
    down_market_n: int
    up_market_high: float
    up_market_low: float
    down_market_high: float
    down_market_low: float
    tape_low: float
    tape_high: float
    up_signal_basis_price: float = 0.0
    down_signal_basis_price: float = 0.0
    reentry_armed: bool = False


@dataclass(slots=True)
class DecisionOutcome:
    selected: OrderIntent | None
    candidates: list[OrderIntent]
    blocked_reasons: list[str] = field(default_factory=list)
