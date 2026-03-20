from __future__ import annotations

import json
from dataclasses import asdict

import pandas as pd

from polynet_ai.engine.replay import ReplayResult
from polynet_ai.reporting.performance import rule_breakdown, summarize_decisions


def build_experiment_row(
    scenario_name: str,
    replay_result: ReplayResult,
    overrides: dict[str, object],
) -> dict[str, object]:
    performance = asdict(replay_result.performance)
    decision_summary = asdict(summarize_decisions(replay_result.decision_df))
    selected_rules = rule_breakdown(replay_result.decision_df, executed_only=False)
    executed_rules = rule_breakdown(replay_result.decision_df, executed_only=True)
    row: dict[str, object] = {
        "scenario": scenario_name,
        "overrides_json": json.dumps(overrides, ensure_ascii=False, sort_keys=True),
    }
    row.update(performance)
    row.update(decision_summary)
    for key, value in selected_rules.items():
        row[f"selected_rule_{key}"] = value
    for key, value in executed_rules.items():
        row[f"executed_rule_{key}"] = value
    return row


def build_experiment_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    preferred = [
        "scenario",
        "total_net_profit",
        "average_cycle_profit",
        "win_rate",
        "max_drawdown",
        "total_fees",
        "total_signals",
        "accepted_signals",
        "blocked_signals",
        "executed_trades",
        "signal_execution_rate",
        "overrides_json",
    ]
    ordered = [column for column in preferred if column in frame.columns]
    remainder = [column for column in frame.columns if column not in ordered]
    return frame[ordered + remainder]
