from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.excel_loader import load_excel_events
from polynet_ai.engine.replay import ReplayEngine
from polynet_ai.reporting.excel_export import export_replay_to_excel, get_version_tag
from polynet_ai.reporting.experiments import build_experiment_frame, build_experiment_row
from polynet_ai.strategy.spec import load_strategy_config
from polynet_ai.strategy.tuning import load_scenarios


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket 参数扫描与批量回放")
    parser.add_argument("--input", default="data/raw/polymarket_tracker_collection.xlsx")
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--sweep", default="configs/sweep.yaml")
    parser.add_argument("--output-dir", default="artifacts/sweeps/sweep_outputs")
    parser.add_argument("--starting-cash", type=float, default=200.0)
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    events = load_excel_events(args.input, sheet_name=args.sheet)
    base_config = load_strategy_config(args.config)
    scenarios = load_scenarios(args.sweep)
    if args.limit is not None:
        scenarios = scenarios[: args.limit]

    experiment_rows: list[dict[str, object]] = []
    _vtag = get_version_tag()
    for scenario in scenarios:
        scenario_config = base_config.with_overrides(scenario.overrides)
        engine = ReplayEngine(scenario_config, starting_cash=args.starting_cash)
        result = engine.run(events)
        scenario_dir = output_dir / scenario.name
        scenario_dir.mkdir(parents=True, exist_ok=True)
        export_replay_to_excel(
            result.cycle_df,
            result.decision_df,
            result.metrics_df,
            scenario_dir / f"replay_report_{_vtag}.xlsx",
        )
        result.cycle_df.to_csv(scenario_dir / "cycles.csv", index=False, encoding="utf-8-sig")
        result.decision_df.to_csv(scenario_dir / "decisions.csv", index=False, encoding="utf-8-sig")
        result.metrics_df.to_csv(scenario_dir / "metrics.csv", index=False, encoding="utf-8-sig")
        experiment_rows.append(build_experiment_row(scenario.name, result, scenario.overrides))
        print(
            f"{scenario.name}: profit={result.performance.total_net_profit:.3f}, "
            f"drawdown={result.performance.max_drawdown:.3f}, "
            f"win_rate={result.performance.win_rate:.3f}"
        )

    summary_df = build_experiment_frame(experiment_rows).sort_values(
        by=["total_net_profit", "win_rate"],
        ascending=[False, False],
    )
    summary_df.to_csv(output_dir / "sweep_summary.csv", index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(output_dir / f"sweep_summary_{_vtag}.xlsx", engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="summary", index=False)
    print(f"参数扫描完成: {output_dir}")
    print(summary_df.head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
