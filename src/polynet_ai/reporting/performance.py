from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(slots=True)
class PerformanceSummary:
    total_cycles: int
    total_net_profit: float
    average_cycle_profit: float
    win_rate: float
    max_drawdown: float
    total_fees: float


@dataclass(slots=True)
class DecisionSummary:
    total_signals: int
    executed_trades: int
    blocked_signals: int
    accepted_signals: int
    signal_execution_rate: float


def compute_max_drawdown(pnl_series: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    max_drawdown = 0.0
    for pnl in pnl_series:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return abs(max_drawdown)


def summarize_cycles(cycle_df: pd.DataFrame, total_fees: float) -> PerformanceSummary:
    if cycle_df.empty:
        return PerformanceSummary(0, 0.0, 0.0, 0.0, 0.0, total_fees)
    profits = cycle_df["cycle_net_profit"].astype(float).tolist()
    wins = sum(1 for profit in profits if profit > 0)
    return PerformanceSummary(
        total_cycles=len(cycle_df),
        total_net_profit=float(sum(profits)),
        average_cycle_profit=float(sum(profits) / len(profits)),
        win_rate=float(wins / len(profits)),
        max_drawdown=compute_max_drawdown(profits),
        total_fees=float(total_fees),
    )


def summarize_decisions(decision_df: pd.DataFrame) -> DecisionSummary:
    if decision_df.empty:
        return DecisionSummary(0, 0, 0, 0, 0.0)
    total_signals = int((decision_df["selected_rule"].fillna("") != "").sum())
    executed = int(decision_df["executed"].fillna(False).sum())
    blocked = int((decision_df["risk_status"].fillna("") == "blocked").sum())
    accepted = int((decision_df["risk_status"].fillna("") == "accepted").sum())
    rate = float(executed / total_signals) if total_signals else 0.0
    return DecisionSummary(
        total_signals=total_signals,
        executed_trades=executed,
        blocked_signals=blocked,
        accepted_signals=accepted,
        signal_execution_rate=rate,
    )


def rule_breakdown(decision_df: pd.DataFrame, executed_only: bool = False) -> dict[str, int]:
    if decision_df.empty or "selected_rule" not in decision_df.columns:
        return {}
    df = decision_df
    if executed_only and "executed" in df.columns:
        df = df[df["executed"] == True]  # noqa: E712
    counts = (
        df["selected_rule"]
        .fillna("")
        .loc[lambda series: series != ""]
        .value_counts()
        .sort_index()
        .to_dict()
    )
    return {str(key): int(value) for key, value in counts.items()}
