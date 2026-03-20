from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.excel_loader import load_excel_events
from polynet_ai.engine.live import LivePaperRunner, export_live_result
from polynet_ai.engine.replay import ReplayEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket 准实时 paper trading runner")
    parser.add_argument("--input", default="data/raw/polymarket_tracker_collection.xlsx")
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--output-dir", default="artifacts/live/live_outputs")
    parser.add_argument("--starting-cash", type=float, default=1000.0)
    parser.add_argument("--pace-factor", type=float, default=0.0)
    parser.add_argument("--max-sleep-seconds", type=float, default=0.25)
    parser.add_argument("--status-every", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events = load_excel_events(args.input, sheet_name=args.sheet)
    if args.limit is not None:
        events = events[: args.limit]

    engine = ReplayEngine.from_yaml(args.config, starting_cash=args.starting_cash)
    runner = LivePaperRunner(engine)
    result = runner.run(
        events,
        pace_factor=args.pace_factor,
        max_sleep_seconds=args.max_sleep_seconds,
        status_every=args.status_every,
    )
    export_live_result(result, args.output_dir)
    print(f"准实时回放完成: {args.output_dir}")
    print(result.replay_result.metrics_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
