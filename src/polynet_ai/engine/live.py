from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from polynet_ai.domain.models import TradeEvent
from polynet_ai.engine.replay import ReplayEngine, ReplayResult
from polynet_ai.reporting.dashboard import generate_dashboard_bundle
from polynet_ai.reporting.excel_export import export_trade_ledger_to_excel, get_version_tag


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
        on_event: Callable[[TradeEvent], None] | None = None,
    ) -> LiveRunnerResult:
        sleep_impl = sleep_fn or time.sleep
        previous_event_ref: list[TradeEvent | None] = [None]

        sorted_events = sorted(events, key=lambda item: (item.market_id, item.cycle_id, item.timestamp))
        return self._consume_events(
            sorted_events,
            status_every=status_every,
            on_event=on_event,
            before_step=(
                lambda event: self._apply_replay_delay(
                    event=event,
                    previous_event=previous_event_ref[0],
                    pace_factor=pace_factor,
                    max_sleep_seconds=max_sleep_seconds,
                    sleep_impl=sleep_impl,
                )
            ),
            previous_event_ref=previous_event_ref,
        )

    def run_stream(
        self,
        events: Iterable[TradeEvent],
        *,
        status_every: int = 25,
        on_event: Callable[[TradeEvent], None] | None = None,
        on_progress: Callable[[LiveRunnerResult], None] | None = None,
        progress_interval_seconds: float = 0.0,
        on_cycle_complete: Callable[[dict[str, object]], None] | None = None,
    ) -> LiveRunnerResult:
        return self._consume_events(
            events,
            status_every=status_every,
            on_event=on_event,
            on_progress=on_progress,
            progress_interval_seconds=progress_interval_seconds,
            on_cycle_complete=on_cycle_complete,
        )

    def _apply_replay_delay(
        self,
        *,
        event: TradeEvent,
        previous_event: TradeEvent | None,
        pace_factor: float,
        max_sleep_seconds: float,
        sleep_impl: Callable[[float], None],
    ) -> None:
        if pace_factor <= 0 or previous_event is None:
            return
        raw_delay = (event.timestamp - previous_event.timestamp).total_seconds()
        if raw_delay > 0:
            sleep_impl(min(max_sleep_seconds, raw_delay / pace_factor))

    def _consume_events(
        self,
        events: Iterable[TradeEvent],
        *,
        status_every: int,
        on_event: Callable[[TradeEvent], None] | None = None,
        before_step: Callable[[TradeEvent], None] | None = None,
        previous_event_ref: list[TradeEvent | None] | None = None,
        on_progress: Callable[[LiveRunnerResult], None] | None = None,
        progress_interval_seconds: float = 0.0,
        on_cycle_complete: Callable[[dict[str, object]], None] | None = None,
    ) -> LiveRunnerResult:
        decision_rows: list[dict[str, object]] = []
        cycle_rows: list[dict[str, object]] = []
        snapshot_rows: list[dict[str, object]] = []
        previous_ref = previous_event_ref or [None]
        last_progress_flush = time.monotonic()

        for index, event in enumerate(events, start=1):
            if before_step is not None:
                before_step(event)
            if on_event is not None:
                on_event(event)
            previous_ref[0] = event

            step = self.engine.process_event(event)
            if step.finalized_cycle_row is not None:
                cycle_rows.append(step.finalized_cycle_row)
                if on_cycle_complete is not None:
                    on_cycle_complete(step.finalized_cycle_row)
            decision_row = dict(step.decision_row)
            decision_row["event_index"] = index
            decision_rows.append(decision_row)

            snapshot_row = asdict(step.snapshot)
            snapshot_row["event_index"] = index
            snapshot_row["account_cash"] = self.engine.display_cash(step.snapshot.cycle_net_profit)
            snapshot_row["available_cash"] = self.engine.account.available_cash
            snapshot_rows.append(snapshot_row)

            if status_every > 0 and index % status_every == 0:
                print(
                    f"[live] events={index} cycle={step.snapshot.cycle_id} "
                    f"net={step.snapshot.net_position:.3f} pnl={step.snapshot.cycle_net_profit:.3f} "
                    f"cash={self.engine.account.cash:.3f}"
                )

            if (
                on_progress is not None
                and progress_interval_seconds > 0
                and time.monotonic() - last_progress_flush >= progress_interval_seconds
            ):
                on_progress(self._build_live_result(cycle_rows, decision_rows, snapshot_rows))
                last_progress_flush = time.monotonic()

        pending = self.engine.finalize_pending_cycle()
        if pending is not None:
            cycle_rows.append(pending)
            if on_cycle_complete is not None:
                on_cycle_complete(pending)

        return self._build_live_result(cycle_rows, decision_rows, snapshot_rows)

    def _build_live_result(
        self,
        cycle_rows: list[dict[str, object]],
        decision_rows: list[dict[str, object]],
        snapshot_rows: list[dict[str, object]],
    ) -> LiveRunnerResult:
        replay_result = self.engine._build_result(list(cycle_rows), list(decision_rows))
        return LiveRunnerResult(
            replay_result=replay_result,
            snapshot_df=pd.DataFrame(list(snapshot_rows)),
        )


def export_live_result(
    result: LiveRunnerResult,
    output_dir: str | Path,
    *,
    title: str = "Polynet AI Live Monitoring Dashboard",
    refresh_seconds: float = 1.0,
    write_excel: bool = True,
    position_value_denominator: float | None = None,
) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result.replay_result.cycle_df.to_csv(directory / "cycles.csv", index=False, encoding="utf-8-sig")
    result.replay_result.decision_df.to_csv(directory / "decisions.csv", index=False, encoding="utf-8-sig")
    result.replay_result.metrics_df.to_csv(directory / "metrics.csv", index=False, encoding="utf-8-sig")
    result.snapshot_df.to_csv(directory / "snapshots.csv", index=False, encoding="utf-8-sig")
    _vtag = get_version_tag()
    export_trade_ledger_to_excel(
        decision_df=result.replay_result.decision_df,
        snapshot_df=result.snapshot_df,
        output_path=directory / f"trade_ledger_{_vtag}.xlsx",
        position_value_denominator=position_value_denominator,
    )
    if write_excel:
        with pd.ExcelWriter(directory / f"live_report_{_vtag}.xlsx", engine="openpyxl") as writer:
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
        title=title,
        refresh_seconds=refresh_seconds,
    )
    return directory
