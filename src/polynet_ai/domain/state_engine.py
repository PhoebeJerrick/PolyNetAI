from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import CycleState, FillEvent, Outcome, TradeAction, TradeEvent
from .settlement import settlement_summary

# 从 cycle slug（如 btc-updown-5m-1774240200）解析名义开盘 epoch
_SLUG_EPOCH_RE = re.compile(r"-updown-\d+m-(\d+)$")


def _clamp_binary_price(price: float) -> float:
    return max(0.01, min(0.99, float(price)))


@dataclass(slots=True)
class StateSnapshot:
    timestamp: datetime
    market_id: str
    cycle_id: str
    price: float
    up_balance: float
    down_balance: float
    total_position: float
    net_position: float
    net_direction: str
    net_position_value: float
    up_avg_price: float
    down_avg_price: float
    up_realized_pnl: float
    down_realized_pnl: float
    unrealized_up_pnl: float
    unrealized_down_pnl: float
    cycle_net_profit: float
    high_price: float
    low_price: float
    opening_price: float | None
    up_last_price: float
    down_last_price: float
    market_trades: int
    strategy_trades: int


@dataclass(slots=True)
class StateEngine:
    history_limit: int = 200
    market_tape: deque[TradeEvent] = field(default_factory=lambda: deque(maxlen=200))
    strategy_fills: deque[FillEvent] = field(default_factory=lambda: deque(maxlen=200))
    state: CycleState | None = None
    _snapshot_cache: StateSnapshot | None = field(default=None, init=False, repr=False)

    def start_cycle(self, market_id: str, cycle_id: str, timestamp: datetime) -> CycleState:
        self.market_tape.clear()
        self.strategy_fills.clear()
        # 优先从 cycle_id slug 解析名义开盘时间，使 cycle_elapsed_seconds 与
        # 报表「下注时间距开盘差」对齐；解析失败则回退到首条事件时间。
        m = _SLUG_EPOCH_RE.search(str(cycle_id))
        if m is not None:
            try:
                cycle_start: datetime = datetime.fromtimestamp(
                    int(m.group(1)), tz=timezone.utc
                ).replace(tzinfo=None)
            except (ValueError, OSError):
                cycle_start = timestamp
        else:
            cycle_start = timestamp
        self.state = CycleState(market_id=market_id, cycle_id=cycle_id, cycle_start=cycle_start)
        self._snapshot_cache = None  # Invalidate cache on new cycle
        return self.state

    def ensure_cycle(self, event: TradeEvent | FillEvent) -> CycleState:
        if (
            self.state is None
            or self.state.market_id != event.market_id
            or self.state.cycle_id != event.cycle_id
        ):
            return self.start_cycle(event.market_id, event.cycle_id, event.timestamp)
        return self.state

    def apply_market_trade(self, trade: TradeEvent) -> CycleState:
        state = self.ensure_cycle(trade)
        cid = trade.metadata.get("condition_id")
        if cid:
            state.condition_id = str(cid)
        self._update_price_stats(state, trade.price, trade.timestamp)
        self._update_market_price(state, trade.outcome, trade.price)
        state.market_last_price = trade.price
        state.market_last_outcome = trade.outcome
        self._bump_outcome_market_stats(state, trade.outcome, trade.price)
        state.market_trades += 1
        state.last_event_timestamp = trade.timestamp
        self.market_tape.append(trade)
        self._update_consecutive(state, trade.outcome, trade.action)
        self._update_exposure(state)
        self._snapshot_cache = None  # Invalidate cache after state change
        return state

    def apply_strategy_fill(self, fill: FillEvent) -> CycleState:
        state = self.ensure_cycle(fill)
        prev_up_held = state.up_position.held
        prev_down_held = state.down_position.held
        self._update_price_stats(state, fill.price, fill.timestamp)
        self._update_market_price(state, fill.outcome, fill.price)
        self._update_balances(state, fill.outcome, fill.action, fill.shares)
        self._update_position_book(state, fill.outcome, fill.action, fill.price, fill.shares)
        if fill.action == "buy":
            if fill.outcome == "up" and state.up_position.held > 1e-10:
                self._clear_reentry_anchor(state, "up")
            elif fill.outcome == "down" and state.down_position.held > 1e-10:
                self._clear_reentry_anchor(state, "down")
        elif fill.action == "sell":
            if fill.outcome == "up" and prev_up_held > 1e-10 and state.up_position.held <= 1e-10:
                self._arm_reentry_anchor(state, "up", fill.timestamp)
            elif fill.outcome == "down" and prev_down_held > 1e-10 and state.down_position.held <= 1e-10:
                self._arm_reentry_anchor(state, "down", fill.timestamp)
        state.strategy_trades += 1
        state.last_event_timestamp = fill.timestamp
        self.strategy_fills.append(fill)
        self._update_consecutive(state, fill.outcome, fill.action)
        self._update_exposure(state)
        self._snapshot_cache = None  # Invalidate cache after state change
        return state

    def snapshot(self) -> StateSnapshot:
        if self.state is None:
            raise RuntimeError("cycle state is not initialized")
        
        # Return cached snapshot if available to avoid redundant calculations
        if self._snapshot_cache is not None:
            return self._snapshot_cache
        
        summary = settlement_summary(self.state)
        self._snapshot_cache = StateSnapshot(
            timestamp=self.state.last_event_timestamp or self.state.cycle_start or datetime.utcnow(),
            market_id=self.state.market_id,
            cycle_id=self.state.cycle_id,
            price=self.state.last_price,
            up_balance=self.state.up_balance,
            down_balance=self.state.down_balance,
            total_position=self.state.total_position(),
            net_position=self.state.net_position(),
            net_direction=self.state.net_direction(),
            net_position_value=self.state.net_position_value(),
            up_avg_price=self.state.up_position.avg_price,
            down_avg_price=self.state.down_position.avg_price,
            up_realized_pnl=self.state.up_position.realized_pnl,
            down_realized_pnl=self.state.down_position.realized_pnl,
            unrealized_up_pnl=summary.unrealized_up_pnl,
            unrealized_down_pnl=summary.unrealized_down_pnl,
            cycle_net_profit=summary.cycle_net_profit,
            high_price=self.state.high_price,
            low_price=self.state.low_price,
            opening_price=self.state.opening_price,
            up_last_price=self.state.up_last_price,
            down_last_price=self.state.down_last_price,
            market_trades=self.state.market_trades,
            strategy_trades=self.state.strategy_trades,
        )
        return self._snapshot_cache

    @staticmethod
    def _update_balances(
        state: CycleState,
        outcome: Outcome,
        action: TradeAction,
        shares: float,
    ) -> None:
        signed = shares if action == "buy" else -shares
        if outcome == "up":
            state.up_balance += signed
        else:
            state.down_balance += signed

    @staticmethod
    def _update_position_book(
        state: CycleState,
        outcome: Outcome,
        action: TradeAction,
        price: float,
        shares: float,
    ) -> None:
        fill = FillEvent(
            market_id=state.market_id,
            cycle_id=state.cycle_id,
            timestamp=state.last_event_timestamp or state.cycle_start or datetime.utcnow(),
            price=price,
            shares=shares,
            outcome=outcome,
            action=action,
        )
        if outcome == "up":
            state.up_position.apply_fill(fill)
            state.up_position.last_price = price
        else:
            state.down_position.apply_fill(fill)
            state.down_position.last_price = price

    @staticmethod
    def _update_price_stats(state: CycleState, price: float, timestamp: datetime) -> None:
        state.last_price = price
        state.last_event_timestamp = timestamp
        state.cycle_end = timestamp
        if state.opening_price is None:
            state.opening_price = price
            state.high_price = price
            state.low_price = price
        else:
            state.high_price = max(state.high_price, price)
            state.low_price = min(state.low_price, price)

    @staticmethod
    def _update_market_price(state: CycleState, outcome: Outcome, price: float) -> None:
        if outcome == "up":
            state.up_last_price = price
        else:
            state.down_last_price = price

    @staticmethod
    def _bump_outcome_market_stats(state: CycleState, outcome: Outcome, price: float) -> None:
        if outcome == "up":
            state.up_market_sum += price
            state.up_market_n += 1
            if state.up_market_n == 1:
                state.up_market_high = price
                state.up_market_low = price
            else:
                state.up_market_high = max(state.up_market_high, price)
                state.up_market_low = min(state.up_market_low, price)
        else:
            state.down_market_sum += price
            state.down_market_n += 1
            if state.down_market_n == 1:
                state.down_market_high = price
                state.down_market_low = price
            else:
                state.down_market_high = max(state.down_market_high, price)
                state.down_market_low = min(state.down_market_low, price)

    @staticmethod
    def _update_consecutive(state: CycleState, outcome: Outcome, action: TradeAction) -> None:
        if state.consecutive_outcome == outcome:
            state.consecutive_outcome_count += 1
        else:
            state.consecutive_outcome = outcome
            state.consecutive_outcome_count = 1

        if state.consecutive_action == action:
            state.consecutive_action_count += 1
        else:
            state.consecutive_action = action
            state.consecutive_action_count = 1

    @staticmethod
    def _update_exposure(state: CycleState) -> None:
        state.max_abs_net_exposure = max(
            state.max_abs_net_exposure,
            abs(state.net_position_value()),
        )

    @staticmethod
    def _clear_reentry_anchor(state: CycleState, outcome: Outcome) -> None:
        if outcome == "up":
            state.reentry_anchor_up_price = 0.0
            state.up_reentry_armed_at = None
            return
        state.reentry_anchor_down_price = 0.0
        state.down_reentry_armed_at = None

    @staticmethod
    def _arm_reentry_anchor(state: CycleState, outcome: Outcome, timestamp: datetime) -> None:
        up_price = float(state.up_last_price or 0.0)
        down_price = float(state.down_last_price or 0.0)
        if up_price <= 1e-10 and down_price > 1e-10:
            up_price = _clamp_binary_price(1.0 - down_price)
        if down_price <= 1e-10 and up_price > 1e-10:
            down_price = _clamp_binary_price(1.0 - up_price)
        if outcome == "up":
            state.reentry_anchor_up_price = up_price
            state.up_reentry_armed_at = timestamp
            return
        state.reentry_anchor_down_price = down_price
        state.down_reentry_armed_at = timestamp
