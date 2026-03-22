from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.trade_event_store import load_recorded_trade_events  # noqa: E402
from polynet_ai.engine.replay import ReplayEngine  # noqa: E402
from polynet_ai.reporting.excel_export import export_replay_to_excel  # noqa: E402
from polynet_ai.strategy.spec import load_strategy_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回放实盘 websocket 落盘事件流")
    parser.add_argument("--input", required=True, help="`ws_trade_events.ndjson` 路径")
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--overrides", default=None, help="可选 JSON 覆盖参数，例如 trial_022 的 overrides.json")
    parser.add_argument("--output", default="artifacts/replays/recorded_event_replay.xlsx")
    parser.add_argument("--starting-cash", type=float, default=1000.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    events = load_recorded_trade_events(args.input)
    if not events:
        raise RuntimeError(f"未从事件文件读取到任何 TradeEvent: {args.input}")
    config = load_strategy_config(args.config)
    if args.overrides:
        overrides_path = Path(args.overrides)
        with overrides_path.open("r", encoding="utf-8") as fh:
            overrides = json.load(fh)
        if not isinstance(overrides, dict):
            raise ValueError(f"overrides 文件必须是 JSON 对象: {overrides_path}")
        config = config.with_overrides(overrides)
    engine = ReplayEngine(config, starting_cash=args.starting_cash)
    result = engine.run(events)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    export_replay_to_excel(result.cycle_df, result.decision_df, result.metrics_df, output_path)
    result.cycle_df.to_csv(output_path.with_name(output_path.stem + "_cycles.csv"), index=False, encoding="utf-8-sig")
    result.decision_df.to_csv(output_path.with_name(output_path.stem + "_decisions.csv"), index=False, encoding="utf-8-sig")
    result.metrics_df.to_csv(output_path.with_name(output_path.stem + "_metrics.csv"), index=False, encoding="utf-8-sig")
    print(f"已从实盘事件流回放完成: {args.input}")
    print(result.metrics_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
