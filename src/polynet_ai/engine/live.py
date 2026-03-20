from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from polynet_ai.domain.models import TradeEvent
from polynet_ai.engine.replay import ReplayEngine, ReplayResult
from polynet_ai.reporting.dashboard import generate_dashboard_bundle


@dataclass(slots=True)
class LiveRunnerResult:
    replay_result: ReplayResult
    snapshot_df: pd.DataFrame


class LivePaperRunner:
    def __init__(self, engine: ReplayEngine) -> None:
        self.engine = engine

    def run(
        self,
        events: Iterable[TradeEvent],
        pace_factor: float = 0.0,
        max_sleep_seconds: float = 0.5,
        status_every: int = 25,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> LiveRunnerResult:
        sleep_impl = sleep_fn or time.sleep
        decision_rows: list[dict[str, object]] = []
        cycle_rows: list[dict[str, object]] = []
        snapshot_rows: list[dict[str, object]] = []
        previous_event: TradeEvent | None = None

        sorted_events = sorted(events, key=lambda item: (item.market_id, item.cycle_id, item.timestamp))
        for index, event in enumerate(sorted_events, start=1):
            if pace_factor > 0 and previous_event is not None:
                raw_delay = (event.timestamp - previous_event.timestamp).total_seconds()
                if raw_delay > 0:
                    sleep_impl(min(max_sleep_seconds, raw_delay / pace_factor))
            previous_event = event

            step = self.engine.process_event(event)
            if step.finalized_cycle_row is not None:
                cycle_rows.append(step.finalized_cycle_row)
            decision_rows.append(step.decision_row)

            snapshot_row = asdict(step.snapshot)
            snapshot_row["event_index"] = index
            snapshot_row["account_cash"] = self.engine.account.cash
            snapshot_rows.append(snapshot_row)

            if status_every > 0 and index % status_every == 0:
                print(
                    f"[live] events={index} cycle={step.snapshot.cycle_id} "
                    f"net={step.snapshot.net_position:.3f} pnl={step.snapshot.cycle_net_profit:.3f} "
                    f"cash={self.engine.account.cash:.3f}"
                )

        pending = self.engine.finalize_pending_cycle()
        if pending is not None:
            cycle_rows.append(pending)

        replay_result = self.engine._build_result(cycle_rows, decision_rows)
        return LiveRunnerResult(
            replay_result=replay_result,
            snapshot_df=pd.DataFrame(snapshot_rows),
        )


def export_live_result(result: LiveRunnerResult, output_dir: str | Path) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result.replay_result.cycle_df.to_csv(directory / "cycles.csv", index=False, encoding="utf-8-sig")
    result.replay_result.decision_df.to_csv(directory / "decisions.csv", index=False, encoding="utf-8-sig")
    result.replay_result.metrics_df.to_csv(directory / "metrics.csv", index=False, encoding="utf-8-sig")
    result.snapshot_df.to_csv(directory / "snapshots.csv", index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(directory / "live_report.xlsx", engine="openpyxl") as writer:
        result.replay_result.cycle_df.to_excel(writer, sheet_name="cycles", index=False)
        result.replay_result.decision_df.to_excel(writer, sheet_name="decisions", index=False)
        result.replay_result.metrics_df.to_excel(writer, sheet_name="metrics", index=False)
        result.snapshot_df.to_excel(writer, sheet_name="snapshots", index=False)
    generate_dashboard_bundle(
        metrics_df=result.replay_result.metrics_df,
        cycles_df=result.replay_result.cycle_df,
        decisions_df=result.replay_result.decision_df,
        snapshots_df=result.snapshot_df,
        output_dir=directory,
        title="Polynet AI Live Monitoring Dashboard",
    )
    return directory
