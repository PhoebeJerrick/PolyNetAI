from __future__ import annotations

import argparse
import json
import random
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
from polynet_ai.strategy.optimize import (
    compute_score,
    load_optimization_study,
    sample_overrides,
    write_best_config,
)
from polynet_ai.strategy.spec import load_strategy_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket 自动参数寻优")
    parser.add_argument("--input", default="data/raw/polymarket_tracker_collection.xlsx")
    parser.add_argument("--sheet", default=None)
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--optimize", default="configs/optimize.yaml")
    parser.add_argument("--output-dir", default="artifacts/optimization/optimize_outputs")
    parser.add_argument("--starting-cash", type=float, default=100.0)
    parser.add_argument("--trials", type=int, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    events = load_excel_events(args.input, sheet_name=args.sheet)
    base_config = load_strategy_config(args.config)
    study = load_optimization_study(args.optimize)
    if args.trials is not None:
        study.trials = args.trials

    rng = random.Random(study.seed)
    rows: list[dict[str, object]] = []
    trial_payloads: list[tuple[str, dict[str, object], object]] = []

    for trial in range(1, study.trials + 1):
        overrides = sample_overrides(study, rng)
        scenario_name = f"trial_{trial:03d}"
        scenario_config = base_config.with_overrides(overrides)
        result = ReplayEngine(scenario_config, starting_cash=args.starting_cash).run(events)
        row = build_experiment_row(scenario_name, result, overrides)
        score = compute_score(row, study.score_weights)
        row["score"] = score
        rows.append(row)
        trial_payloads.append((scenario_name, overrides, result))
        print(
            f"{scenario_name}: score={score:.3f}, "
            f"profit={result.performance.total_net_profit:.3f}, "
            f"drawdown={result.performance.max_drawdown:.3f}, "
            f"win_rate={result.performance.win_rate:.3f}"
        )

    leaderboard = build_experiment_frame(rows).sort_values(
        by=["score", "total_net_profit", "win_rate"],
        ascending=[False, False, False],
    )
    leaderboard.to_csv(output_dir / "optimization_leaderboard.csv", index=False, encoding="utf-8-sig")
    _vtag = get_version_tag()
    with pd.ExcelWriter(output_dir / f"optimization_leaderboard_{_vtag}.xlsx", engine="openpyxl") as writer:
        leaderboard.to_excel(writer, sheet_name="leaderboard", index=False)

    top_scenarios = leaderboard.head(max(1, study.export_top_n))["scenario"].tolist()
    top_payload_map = {name: (overrides, result) for name, overrides, result in trial_payloads if name in top_scenarios}

    best_name = str(leaderboard.iloc[0]["scenario"])
    best_overrides, _best_result = top_payload_map[best_name]
    best_config = base_config.with_overrides(best_overrides)
    write_best_config(output_dir / "best_strategy.yaml", best_config.to_dict())
    (output_dir / "best_overrides.json").write_text(
        json.dumps(best_overrides, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    for scenario_name in top_scenarios:
        overrides, result = top_payload_map[scenario_name]
        scenario_dir = output_dir / scenario_name
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
        (scenario_dir / "overrides.json").write_text(
            json.dumps(overrides, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    print(f"自动寻优完成: {output_dir}")
    print(leaderboard.head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
