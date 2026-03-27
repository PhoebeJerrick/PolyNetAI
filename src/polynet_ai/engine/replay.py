from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
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
        starting_cash: float = 100.0,
        capital_reset_mode: str = "cumulative",
        broker: object | None = None,
        per_cycle_cash: float | None = None,
    ) -> None:
        self.config = config
        # 优化 #1：缓存配置参数，避免每事件都做 dict lookup
        self.cycle_seconds = int(config.get("cycle.cycle_seconds", 300))
        self.last_minute_seconds = int(config.get("cycle.last_minute_seconds", 60))
        self.state_engine = StateEngine()
        self.router = StrategyRouter(config)
        self.capital_reset_mode = str(capital_reset_mode).strip().lower() or "cumulative"
        if self.capital_reset_mode not in {"fixed", "cumulative"}:
            raise ValueError("capital_reset_mode must be 'fixed' or 'cumulative'")
        # fixed 模式下，_equity_baseline 是资金曲线的起始锚点（总权益基准）；
        # _per_cycle_cash 是每周期分配给策略的实际投注资金（账户重置目标值）。
        # cumulative 模式下两者设为相同值，account.cash 自然累积。
        self._equity_baseline = float(starting_cash)
        self._per_cycle_cash = float(per_cycle_cash) if per_cycle_cash is not None else float(starting_cash)
        self.account = Account(starting_cash=self._per_cycle_cash)
        self._closed_cycle_net_profit = 0.0
        self.broker = broker or PaperBroker(
            fee_rate=float(config.get("execution.fee_rate", 0.002)),
            slippage_bps=float(config.get("execution.slippage_bps", 10)),
        )
        self._last_strategy_fill_at: datetime | None = None
        self._last_strategy_fill_price_up: float | None = None
        self._last_strategy_fill_price_down: float | None = None

    def reset(self) -> None:
        self.state_engine = StateEngine()
        self.account = Account(starting_cash=self._per_cycle_cash)
        self._closed_cycle_net_profit = 0.0
        self._last_strategy_fill_at = None
        self._last_strategy_fill_price_up = None
        self._last_strategy_fill_price_down = None

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        starting_cash: float = 100.0,
        capital_reset_mode: str = "cumulative",
        per_cycle_cash: float | None = None,
    ) -> "ReplayEngine":
        return cls(
            load_strategy_config(path),
            starting_cash=starting_cash,
            capital_reset_mode=capital_reset_mode,
            per_cycle_cash=per_cycle_cash,
        )

    def display_cash(self, current_cycle_net_profit: float = 0.0) -> float:
        if self.capital_reset_mode == "fixed":
            return self._equity_baseline + self._closed_cycle_net_profit + float(current_cycle_net_profit)
        return self.account.cash

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
        self._apply_confirmed_fills(event.timestamp, active_cycle_key=(event.market_id, event.cycle_id))
        self._sync_account_reservations()
        current_state = self.state_engine.state
        if current_state is not None:
            current_cycle_key = (current_state.market_id, current_state.cycle_id)
            incoming_cycle_key = (event.market_id, event.cycle_id)
            if incoming_cycle_key != current_cycle_key:
                finalized_cycle_row = self._finalize_cycle()

        self.state_engine.apply_market_trade(event)
        features = build_feature_snapshot(
            self.state_engine,
            cycle_seconds=self.cycle_seconds,
            last_minute_seconds=self.last_minute_seconds,
        )
        decision = self.router.route(features, strategy_trades=self.state_engine.state.strategy_trades)
        row: dict[str, object] = {
            "market_id": event.market_id,
            "cycle_id": event.cycle_id,
            "timestamp": event.timestamp,
            "market_price": event.price,
            "market_outcome": event.outcome,
            "selected_rule": "",
            "selected_action": "",
            "selected_outcome": "",
            "selected_shares": 0.0,
            "risk_status": "no_signal",
            "risk_reason": "",
            "executed": False,
            "submitted": False,
            "confirmed": False,
            "broker_status": "",
            "broker_order_id": "",
            "fill_price": 0.0,
            "fill_fee": 0.0,
            "cycle_net_profit": features.cycle_net_profit,
            "account_cash": self.display_cash(features.cycle_net_profit),
            "available_cash": self.account.available_cash,
        }
        if decision.selected is not None:
            pending_context = self._pending_context()
            decision.selected.metadata["account_cash"] = self.account.cash
            decision.selected.metadata["account_available_cash"] = self.account.available_cash
            decision.selected.metadata["market_price"] = event.price
            decision.selected.metadata.update(event.metadata)
            decision.selected.metadata.update(pending_context)
            decision.selected.metadata["last_strategy_fill_at"] = self._last_strategy_fill_at
            decision.selected.metadata["last_strategy_fill_price_up"] = self._last_strategy_fill_price_up
            decision.selected.metadata["last_strategy_fill_price_down"] = self._last_strategy_fill_price_down
            row["selected_rule"] = decision.selected.category
            row["selected_action"] = decision.selected.action
            row["selected_outcome"] = decision.selected.outcome
            row["selected_shares"] = decision.selected.shares
            risk_decision = apply_risk_limits(features, decision.selected, self.config)
            row["risk_status"] = "accepted" if risk_decision.accepted else "blocked"
            row["risk_reason"] = risk_decision.reason
            if risk_decision.accepted and risk_decision.intent is not None:
                row["selected_shares"] = risk_decision.intent.shares
                try:
                    execution = self.broker.execute(risk_decision.intent, event.timestamp)
                except Exception as exc:
                    row["risk_status"] = "broker_error"
                    row["risk_reason"] = str(exc)
                else:
                    row["broker_status"] = execution.status
                    row["broker_order_id"] = execution.order_id
                    if execution.status == "submitted":
                        row["submitted"] = True
                        self._sync_account_reservations()
                        row["risk_status"] = "submitted"
                        row["available_cash"] = self.account.available_cash
                    elif execution.fill is not None:
                        self._apply_fill(execution.fill)
                        self._sync_account_reservations()
                        row["executed"] = True
                        row["confirmed"] = True
                        row["fill_price"] = execution.fill.price
                        row["fill_fee"] = execution.fill.fee
                        row["account_cash"] = self.display_cash(features.cycle_net_profit)
                        row["available_cash"] = self.account.available_cash
                    elif execution.status != "filled":
                        self._sync_account_reservations()
                        row["risk_status"] = execution.status
                        row["risk_reason"] = execution.reason
        snapshot = self.state_engine.snapshot()
        return ReplayStepResult(
            decision_row=row,
            finalized_cycle_row=finalized_cycle_row,
            snapshot=snapshot,
        )

    def finalize_pending_cycle(self) -> dict[str, object] | None:
        if self.state_engine.state is None:
            return None
        if self.state_engine.state.last_event_timestamp is not None:
            self._apply_confirmed_fills(
                self.state_engine.state.last_event_timestamp,
                active_cycle_key=(self.state_engine.state.market_id, self.state_engine.state.cycle_id),
            )
            self._sync_account_reservations()
        return self._finalize_cycle()

    def _apply_confirmed_fills(
        self,
        timestamp: datetime,
        *,
        active_cycle_key: tuple[str, str] | None = None,
    ) -> None:
        if not hasattr(self.broker, "poll"):
            return
        fills = self.broker.poll(timestamp)
        for fill in fills:
            state = self.state_engine.state
            fill_cycle_key = (fill.market_id, fill.cycle_id)
            if active_cycle_key is not None and fill_cycle_key != active_cycle_key:
                self.account.apply_fill(fill)
                continue
            if state is not None and fill_cycle_key != (state.market_id, state.cycle_id):
                self.account.apply_fill(fill)
                continue
            self._apply_fill(fill)

    def _apply_fill(self, fill) -> None:
        self.account.apply_fill(fill)
        self.state_engine.apply_strategy_fill(fill)
        self._last_strategy_fill_at = fill.timestamp
        if fill.outcome == "up":
            self._last_strategy_fill_price_up = fill.price
        else:
            self._last_strategy_fill_price_down = fill.price

    def _pending_context(self) -> dict[str, object]:
        if not hasattr(self.broker, "pending_context"):
            return {}
        context = dict(self.broker.pending_context())
        context["pending_buy_reserved_cash"] = float(context.get("pending_buy_reserved_cash", 0.0))
        context["pending_up_sell_shares"] = float(context.get("pending_up_sell_shares", 0.0))
        context["pending_down_sell_shares"] = float(context.get("pending_down_sell_shares", 0.0))
        return context

    def _sync_account_reservations(self) -> None:
        pending_context = self._pending_context()
        self.account.reserved_cash = float(pending_context.get("pending_buy_reserved_cash", 0.0))

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
        if self.capital_reset_mode == "fixed":
            self._closed_cycle_net_profit += float(summary.cycle_net_profit)
        cycle_account_cash = self.display_cash()
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
            "account_cash": cycle_account_cash,
        }
        self.state_engine.state = None
        self.state_engine.market_tape.clear()
        self.state_engine.strategy_fills.clear()
        if self.capital_reset_mode == "fixed":
            self.account.cash = self._per_cycle_cash
            self.account.reserved_cash = 0.0
            self._last_strategy_fill_at = None
            self._last_strategy_fill_price_up = None
            self._last_strategy_fill_price_down = None
        return row
