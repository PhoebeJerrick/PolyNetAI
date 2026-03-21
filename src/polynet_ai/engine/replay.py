from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from polynet_ai.domain.models import TradeEvent
from polynet_ai.domain.state_engine import StateSnapshot
from polynet_ai.domain.settlement import settlement_summary
from polynet_ai.domain.state_engine import StateEngine
from polynet_ai.execution.paper_broker import PaperBroker
from polynet_ai.portfolio.account import Account
from polynet_ai.reporting.performance import (
    PerformanceSummary,
    rule_breakdown,
    summarize_cycles,
    summarize_decisions,
)
from polynet_ai.risk.limits import apply_risk_limits
from polynet_ai.strategy.features import build_feature_snapshot
from polynet_ai.strategy.router import StrategyRouter
from polynet_ai.strategy.spec import StrategyConfig, load_strategy_config


@dataclass(slots=True)
class ReplayResult:
    cycle_df: pd.DataFrame
    decision_df: pd.DataFrame
    metrics_df: pd.DataFrame
    performance: PerformanceSummary


@dataclass(slots=True)
class ReplayStepResult:
    decision_row: dict[str, object]
    finalized_cycle_row: dict[str, object] | None
    snapshot: StateSnapshot


class ReplayEngine:
    def __init__(
        self,
        config: StrategyConfig,
        starting_cash: float = 1000.0,
    ) -> None:
        self.config = config
        self.state_engine = StateEngine()
        self.router = StrategyRouter(config)
        self.account = Account(starting_cash=starting_cash)
        self.broker = PaperBroker(
            fee_rate=float(config.get("execution.fee_rate", 0.002)),
            slippage_bps=float(config.get("execution.slippage_bps", 10)),
        )

    def reset(self) -> None:
        self.state_engine = StateEngine()
        self.account = Account(starting_cash=self.account.starting_cash)

    @classmethod
    def from_yaml(cls, path: str | Path, starting_cash: float = 1000.0) -> "ReplayEngine":
        return cls(load_strategy_config(path), starting_cash=starting_cash)

    def run(self, events: list[TradeEvent]) -> ReplayResult:
        cycle_rows: list[dict[str, object]] = []
        decision_rows: list[dict[str, object]] = []
        sorted_events = sorted(events, key=lambda item: (item.market_id, item.cycle_id, item.timestamp))

        for event in sorted_events:
            step = self.process_event(event)
            if step.finalized_cycle_row is not None:
                cycle_rows.append(step.finalized_cycle_row)
            decision_rows.append(step.decision_row)

        pending = self.finalize_pending_cycle()
        if pending is not None:
            cycle_rows.append(pending)

        return self._build_result(cycle_rows, decision_rows)

    def process_event(self, event: TradeEvent) -> ReplayStepResult:
        finalized_cycle_row: dict[str, object] | None = None
        current_state = self.state_engine.state
        if current_state is not None:
            current_cycle_key = (current_state.market_id, current_state.cycle_id)
            incoming_cycle_key = (event.market_id, event.cycle_id)
            if incoming_cycle_key != current_cycle_key:
                finalized_cycle_row = self._finalize_cycle()

        self.state_engine.apply_market_trade(event)
        features = build_feature_snapshot(
            self.state_engine,
            cycle_seconds=int(self.config.get("cycle.cycle_seconds", 300)),
            last_minute_seconds=int(self.config.get("cycle.last_minute_seconds", 60)),
        )
        decision = self.router.route(features, strategy_trades=self.state_engine.state.strategy_trades)
        row: dict[str, object] = {
            "market_id": event.market_id,
            "cycle_id": event.cycle_id,
            "timestamp": event.timestamp,
            "market_price": event.price,
            "selected_rule": "",
            "selected_action": "",
            "selected_outcome": "",
            "selected_shares": 0.0,
            "risk_status": "no_signal",
            "risk_reason": "",
            "executed": False,
            "fill_price": 0.0,
            "fill_fee": 0.0,
            "cycle_net_profit": features.cycle_net_profit,
            "account_cash": self.account.cash,
        }
        if decision.selected is not None:
            decision.selected.metadata["account_cash"] = self.account.cash
            row["selected_rule"] = decision.selected.category
            row["selected_action"] = decision.selected.action
            row["selected_outcome"] = decision.selected.outcome
            row["selected_shares"] = decision.selected.shares
            risk_decision = apply_risk_limits(features, decision.selected, self.config)
            row["risk_status"] = "accepted" if risk_decision.accepted else "blocked"
            row["risk_reason"] = risk_decision.reason
            if risk_decision.accepted and risk_decision.intent is not None:
                row["selected_shares"] = risk_decision.intent.shares
                fill = self.broker.execute(risk_decision.intent, event.timestamp)
                self.account.apply_fill(fill)
                self.state_engine.apply_strategy_fill(fill)
                row["executed"] = True
                row["fill_price"] = fill.price
                row["fill_fee"] = fill.fee
                row["account_cash"] = self.account.cash
        snapshot = self.state_engine.snapshot()
        return ReplayStepResult(
            decision_row=row,
            finalized_cycle_row=finalized_cycle_row,
            snapshot=snapshot,
        )

    def finalize_pending_cycle(self) -> dict[str, object] | None:
        if self.state_engine.state is None:
            return None
        return self._finalize_cycle()

    def _build_result(
        self,
        cycle_rows: list[dict[str, object]],
        decision_rows: list[dict[str, object]],
    ) -> ReplayResult:
        cycle_df = pd.DataFrame(cycle_rows)
        decision_df = pd.DataFrame(decision_rows)
        performance = summarize_cycles(cycle_df, total_fees=self.account.fees_paid)
        decision_summary = summarize_decisions(decision_df)
        metrics_row = asdict(performance) | asdict(decision_summary)
        for key, value in rule_breakdown(decision_df, executed_only=False).items():
            metrics_row[f"selected_rule_{key}"] = value
        for key, value in rule_breakdown(decision_df, executed_only=True).items():
            metrics_row[f"executed_rule_{key}"] = value
        metrics_df = pd.DataFrame([metrics_row])
        return ReplayResult(
            cycle_df=cycle_df,
            decision_df=decision_df,
            metrics_df=metrics_df,
            performance=performance,
        )

    def _finalize_cycle(self) -> dict[str, object]:
        if self.state_engine.state is None:
            raise RuntimeError("cannot finalize empty cycle")
        state = self.state_engine.state
        summary = settlement_summary(state)
        settlement_cash = 0.0
        if summary.winner == "up":
            settlement_cash = state.up_position.held
        elif summary.winner == "down":
            settlement_cash = state.down_position.held
        self.account.cash += settlement_cash
        row = {
            "market_id": state.market_id,
            "cycle_id": state.cycle_id,
            "opening_price": state.opening_price,
            "last_price": state.last_price,
            "high_price": state.high_price,
            "low_price": state.low_price,
            "up_balance": state.up_balance,
            "down_balance": state.down_balance,
            "net_position": state.net_position(),
            "net_direction": state.net_direction(),
            "net_position_value": state.net_position_value(),
            "up_avg_price": state.up_position.avg_price,
            "down_avg_price": state.down_position.avg_price,
            "up_realized_pnl": state.up_position.realized_pnl,
            "down_realized_pnl": state.down_position.realized_pnl,
            "unrealized_up_pnl": summary.unrealized_up_pnl,
            "unrealized_down_pnl": summary.unrealized_down_pnl,
            "cycle_net_profit": summary.cycle_net_profit,
            "winner": summary.winner,
            "market_trades": state.market_trades,
            "strategy_trades": state.strategy_trades,
            "max_abs_net_exposure": state.max_abs_net_exposure,
            "account_cash": self.account.cash,
        }
        self.state_engine.state = None
        self.state_engine.market_tape.clear()
        self.state_engine.strategy_fills.clear()
        return row
