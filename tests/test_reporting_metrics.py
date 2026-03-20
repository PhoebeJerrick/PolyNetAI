from __future__ import annotations

import pandas as pd

from polynet_ai.reporting.performance import rule_breakdown, summarize_decisions


def test_summarize_decisions_and_rule_breakdown() -> None:
    decision_df = pd.DataFrame(
        [
            {"selected_rule": "trend", "risk_status": "accepted", "executed": True},
            {"selected_rule": "trend", "risk_status": "blocked", "executed": False},
            {"selected_rule": "grid", "risk_status": "accepted", "executed": True},
            {"selected_rule": "", "risk_status": "no_signal", "executed": False},
        ]
    )
    summary = summarize_decisions(decision_df)
    assert summary.total_signals == 3
    assert summary.executed_trades == 2
    assert summary.blocked_signals == 1
    assert rule_breakdown(decision_df)["trend"] == 2
    assert rule_breakdown(decision_df, executed_only=True)["trend"] == 1
