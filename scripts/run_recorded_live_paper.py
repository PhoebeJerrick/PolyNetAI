from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.trade_event_store import load_recorded_trade_events
from polynet_ai.engine.live import LivePaperRunner, LiveRunnerResult, export_live_result
from polynet_ai.engine.replay import ReplayEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基于 ws_trade_events.ndjson 的准实时 paper trading runner")
    parser.add_argument("--input-dir", default="artifacts/live/record_job")
    parser.add_argument("--cycle-glob", default="btc-updown-5m-*")
    parser.add_argument("--event-file-name", default="ws_trade_events.ndjson")
    parser.add_argument("--max-cycles", type=int, default=10)
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--output-dir", default="artifacts/live/record_job/batch_replay_outputs")
    parser.add_argument("--starting-cash", type=float, default=1000.0)
    parser.add_argument("--pace-factor", type=float, default=20.0)
    parser.add_argument("--max-sleep-seconds", type=float, default=0.25)
    parser.add_argument("--status-every", type=int, default=100)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dashboard-refresh-seconds", type=float, default=1.0)
    return parser.parse_args()


def _cycle_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.name.rsplit("-", 1)[-1]
    try:
        return (int(suffix), path.name)
    except ValueError:
        return (0, path.name)


def _load_events_from_cycle_dirs(input_dir: Path, cycle_glob: str, event_file_name: str, max_cycles: int | None):
    events = []
    cycle_dirs = sorted((p for p in input_dir.glob(cycle_glob) if p.is_dir()), key=_cycle_sort_key)
    if max_cycles is not None and max_cycles > 0:
        cycle_dirs = cycle_dirs[:max_cycles]
    for cycle_dir in cycle_dirs:
        event_path = cycle_dir / event_file_name
        cycle_events = load_recorded_trade_events(event_path)
        if cycle_events:
            events.extend(cycle_events)
    return sorted(events, key=lambda item: item.timestamp)


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
    input_dir = (ROOT / args.input_dir).resolve()
    if not input_dir.exists():
        raise FileNotFoundError(f"输入目录不存在: {input_dir}")
    max_cycles = args.max_cycles if args.max_cycles and args.max_cycles > 0 else None
    events = _load_events_from_cycle_dirs(input_dir, args.cycle_glob, args.event_file_name, max_cycles)
    if not events:
        raise RuntimeError(
            f"未在 {input_dir} 下找到匹配 {args.cycle_glob}/{args.event_file_name} 的成交流文件，无法启动准实时回放。"
        )
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
