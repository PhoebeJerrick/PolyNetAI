from __future__ import annotations

from dataclasses import dataclass

from .models import CycleState


@dataclass(slots=True)
class SettlementSummary:
    winner: str | None
    unrealized_up_pnl: float
    unrealized_down_pnl: float
    realized_up_pnl: float
    realized_down_pnl: float
    cycle_net_profit: float


def _round3(value: float) -> float:
    rounded = round(value, 3)
    return 0.0 if abs(rounded) < 1e-10 else rounded


def winner_from_state(state: CycleState) -> str | None:
    # Prefer the latest market tick for binary winner inference.
    if state.market_last_outcome and state.market_last_price > 0:
        if state.market_last_outcome == "up":
            return "up" if state.market_last_price >= 0.5 else "down"
        return "down" if state.market_last_price >= 0.5 else "up"

    up_last = state.up_last_price
    down_last = state.down_last_price
    if up_last > 0 and down_last > 0:
        return "up" if up_last >= down_last else "down"
    if up_last > 0:
        return "up" if up_last > 0.5 else "down"
    if down_last > 0:
        return "down" if down_last > 0.5 else "up"
    return None


def settlement_summary(state: CycleState) -> SettlementSummary:
    winner = winner_from_state(state)
    up_stl = 0.0
    down_stl = 0.0
    if winner == "up":
        if state.up_position.held > 1e-10:
            up_stl = state.up_position.held * (1.0 - state.up_position.avg_price)
        if state.down_position.held > 1e-10:
            down_stl = -(state.down_position.held * state.down_position.avg_price)
    elif winner == "down":
        if state.up_position.held > 1e-10:
            up_stl = -(state.up_position.held * state.up_position.avg_price)
        if state.down_position.held > 1e-10:
            down_stl = state.down_position.held * (1.0 - state.down_position.avg_price)

    total = (
        state.up_position.realized_pnl
        + state.down_position.realized_pnl
        + up_stl
        + down_stl
    )
    return SettlementSummary(
        winner=winner,
        unrealized_up_pnl=_round3(up_stl),
        unrealized_down_pnl=_round3(down_stl),
        realized_up_pnl=_round3(state.up_position.realized_pnl),
        realized_down_pnl=_round3(state.down_position.realized_pnl),
        cycle_net_profit=_round3(total),
    )
