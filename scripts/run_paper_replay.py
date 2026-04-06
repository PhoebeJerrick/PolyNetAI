from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.excel_loader import load_excel_events
from polynet_ai.engine.replay import ReplayEngine
from polynet_ai.reporting.excel_export import export_replay_to_excel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket 5分钟仿真交易回放")
    parser.add_argument("--input", default="data/raw/polymarket_tracker_collection.xlsx")
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--output", default="artifacts/replays/paper_replay_report.xlsx")
    parser.add_argument("--starting-cash", type=float, default=200.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events = load_excel_events(args.input, sheet_name=args.sheet)
    engine = ReplayEngine.from_yaml(args.config, starting_cash=args.starting_cash)
    result = engine.run(events)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_replay_to_excel(result.cycle_df, result.decision_df, result.metrics_df, output_path)
    print(f"回放完成: {args.output}")
    print(result.metrics_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
