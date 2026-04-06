from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.excel_loader import load_excel_events
from polynet_ai.engine.live import LivePaperRunner, LiveRunnerResult, export_live_result
from polynet_ai.engine.replay import ReplayEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket 准实时 paper trading runner")
    parser.add_argument("--input", default="data/raw/polymarket_tracker_collection.xlsx")
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--output-dir", default="artifacts/live/live_outputs")
    parser.add_argument("--starting-cash", type=float, default=200.0)
    parser.add_argument("--pace-factor", type=float, default=1000000.0)
    parser.add_argument("--max-sleep-seconds", type=float, default=0.25)
    parser.add_argument("--status-every", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dashboard-refresh-seconds", type=float, default=1.0)
    return parser.parse_args()


def iter_events_with_pacing(events, *, pace_factor: float, max_sleep_seconds: float):
    previous = None
    for event in events:
        if previous is not None and pace_factor > 0:
            raw_delay = (event.timestamp - previous.timestamp).total_seconds()
            if raw_delay > 0:
                time.sleep(min(max_sleep_seconds, raw_delay / pace_factor))
        previous = event
        yield event


def main() -> int:
    args = parse_args()
    events = load_excel_events(args.input, sheet_name=args.sheet)
    if args.limit is not None:
        events = events[: args.limit]

    engine = ReplayEngine.from_yaml(args.config, starting_cash=args.starting_cash)
    runner = LivePaperRunner(engine)
    event_stream = iter_events_with_pacing(
        events,
        pace_factor=args.pace_factor,
        max_sleep_seconds=args.max_sleep_seconds,
    )
    progress_callback = None
    if args.dashboard_refresh_seconds > 0:
        print(f"实时 dashboard 已启用：每 {args.dashboard_refresh_seconds:.1f}s 写盘一次。")

        def _flush_progress(result: LiveRunnerResult) -> None:
            export_live_result(
                result,
                args.output_dir,
                refresh_seconds=args.dashboard_refresh_seconds,
                write_excel=False,
            )

        progress_callback = _flush_progress

    result = runner.run_stream(
        event_stream,
        status_every=args.status_every,
        on_progress=progress_callback,
        progress_interval_seconds=max(0.0, args.dashboard_refresh_seconds),
    )
    export_live_result(
        result,
        args.output_dir,
        refresh_seconds=max(1.0, args.dashboard_refresh_seconds) if args.dashboard_refresh_seconds > 0 else 1.0,
    )
    print(f"准实时回放完成: {args.output_dir}")
    print(result.replay_result.metrics_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
