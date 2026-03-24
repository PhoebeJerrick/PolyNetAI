from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.trade_event_store import load_recorded_trade_events
from polynet_ai.engine.replay import ReplayEngine
from polynet_ai.strategy.spec import load_strategy_config


@dataclass(slots=True)
class Scenario:
    name: str
    phase: str
    overrides: dict[str, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="batch_replay 调参执行脚本（baseline/放宽门槛/尾盘平衡/小网格）")
    parser.add_argument("--input-dir", default="artifacts/live/record_job")
    parser.add_argument("--cycle-glob", default="btc-updown-5m-*")
    parser.add_argument("--event-file-name", default="ws_trade_events.ndjson")
    parser.add_argument("--max-cycles", type=int, default=10)
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--starting-cash", type=float, default=1000.0)
    parser.add_argument("--output-dir", default="artifacts/live/record_job/tuning_runs")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--relax-seconds-values", default="1.5,2.0,2.5")
    parser.add_argument("--relax-move-values", default="0.005,0.01,0.015")
    parser.add_argument("--tail-ratio-values", default="1.0,1.05,1.1")
    parser.add_argument("--tail-confidence-values", default="0.82,0.84,0.86")
    parser.add_argument("--objective-balance-weight", type=float, default=2.0)
    parser.add_argument("--objective-drawdown-weight", type=float, default=1.0)
    parser.add_argument("--objective-profit-weight", type=float, default=0.25)
    parser.add_argument("--objective-exec-weight", type=float, default=1.0)
    return parser.parse_args()


def _parse_float_list(text: str) -> list[float]:
    values = [item.strip() for item in str(text).split(",")]
    out: list[float] = []
    for value in values:
        if not value:
            continue
        out.append(float(value))
    if not out:
        raise ValueError(f"参数列表不能为空: {text}")
    return out


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
    return sorted(events, key=lambda item: item.timestamp), cycle_dirs


def _winner_loser_imbalance(cycle_row: pd.Series) -> float:
    winner = str(cycle_row.get("winner", "")).strip().lower()
    up_balance = float(cycle_row.get("up_balance", 0.0) or 0.0)
    down_balance = float(cycle_row.get("down_balance", 0.0) or 0.0)
    if winner == "up":
        winner_shares = up_balance
        loser_shares = down_balance
    elif winner == "down":
        winner_shares = down_balance
        loser_shares = up_balance
    else:
        return abs(up_balance - down_balance)
    return abs(winner_shares - loser_shares)


def _compute_settlement_imbalance_stats(cycle_df: pd.DataFrame) -> tuple[float, float]:
    if cycle_df.empty:
        return 0.0, 0.0
    imbalances = cycle_df.apply(_winner_loser_imbalance, axis=1).astype(float)
    return float(imbalances.mean()), float(imbalances.max())


def _compute_take_profit_buyback(decision_df: pd.DataFrame) -> tuple[int, int, float]:
    if decision_df.empty:
        return 0, 0, 0.0

    total_tp_sell = 0
    total_buyback = 0
    sort_cols = ["cycle_id"]
    for candidate in ("event_index", "event_ordinal", "timestamp"):
        if candidate in decision_df.columns:
            sort_cols.append(candidate)
    by_cycle = decision_df.sort_values(sort_cols, kind="stable").groupby("cycle_id", dropna=False)

    for _, frame in by_cycle:
        pending_by_outcome: dict[str, int] = {}
        for _, row in frame.iterrows():
            if not bool(row.get("executed", False)):
                continue
            rule = str(row.get("selected_rule", "")).strip()
            action = str(row.get("selected_action", "")).strip()
            outcome = str(row.get("selected_outcome", "")).strip().lower()
            if not outcome:
                continue
            if rule == "take_profit" and action == "sell":
                total_tp_sell += 1
                pending_by_outcome[outcome] = pending_by_outcome.get(outcome, 0) + 1
            elif action == "buy" and pending_by_outcome.get(outcome, 0) > 0:
                total_buyback += 1
                pending_by_outcome[outcome] -= 1

    rate = float(total_buyback / total_tp_sell) if total_tp_sell else 0.0
    return total_tp_sell, total_buyback, rate


def _run_one_scenario(
    scenario: Scenario,
    *,
    base_config_path: Path,
    starting_cash: float,
    events: list,
    output_root: Path,
) -> dict[str, object]:
    base_cfg = load_strategy_config(base_config_path)
    cfg = base_cfg.with_overrides(scenario.overrides) if scenario.overrides else base_cfg
    engine = ReplayEngine(cfg, starting_cash=starting_cash)
    result = engine.run(events)

    scenario_dir = output_root / "scenarios" / scenario.name
    scenario_dir.mkdir(parents=True, exist_ok=True)
    result.cycle_df.to_csv(scenario_dir / "cycles.csv", index=False, encoding="utf-8-sig")
    result.decision_df.to_csv(scenario_dir / "decisions.csv", index=False, encoding="utf-8-sig")
    result.metrics_df.to_csv(scenario_dir / "metrics.csv", index=False, encoding="utf-8-sig")
    (scenario_dir / "overrides.json").write_text(
        json.dumps(scenario.overrides, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    metrics = result.metrics_df.iloc[0].to_dict() if not result.metrics_df.empty else {}
    imbalance_mean, imbalance_max = _compute_settlement_imbalance_stats(result.cycle_df)
    tp_sell_count, buyback_count, buyback_rate = _compute_take_profit_buyback(result.decision_df)

    row: dict[str, object] = {
        "scenario": scenario.name,
        "phase": scenario.phase,
        "overrides_json": json.dumps(scenario.overrides, ensure_ascii=False, sort_keys=True),
        "total_cycles": int(metrics.get("total_cycles", 0) or 0),
        "total_net_profit": float(metrics.get("total_net_profit", 0.0) or 0.0),
        "max_drawdown": float(metrics.get("max_drawdown", 0.0) or 0.0),
        "signal_execution_rate": float(metrics.get("signal_execution_rate", 0.0) or 0.0),
        "executed_trades": int(metrics.get("executed_trades", 0) or 0),
        "accepted_signals": int(metrics.get("accepted_signals", 0) or 0),
        "blocked_signals": int(metrics.get("blocked_signals", 0) or 0),
        "settlement_imbalance_mean": imbalance_mean,
        "settlement_imbalance_max": imbalance_max,
        "take_profit_sell_count": tp_sell_count,
        "buyback_after_take_profit_count": buyback_count,
        "buyback_after_take_profit_rate": buyback_rate,
    }
    return row


def _build_report_md(
    rows: pd.DataFrame,
    output_path: Path,
    *,
    best_scenario: str,
    best_overrides: dict[str, float],
) -> None:
    baseline = rows.loc[rows["phase"] == "baseline"].head(1)
    relax = rows.loc[rows["phase"] == "relax_gates"].head(1)
    tail = rows.loc[rows["phase"] == "tail_balance"].head(1)
    top_grid = rows.loc[rows["phase"] == "batch_compare"].head(5)

    def _line(frame: pd.DataFrame) -> str:
        if frame.empty:
            return "- (无数据)"
        r = frame.iloc[0]
        return (
            f"- `{r['scenario']}`: exec={float(r['signal_execution_rate']):.4f}, "
            f"net={float(r['total_net_profit']):.3f}, dd={float(r['max_drawdown']):.3f}, "
            f"imbalance_mean={float(r['settlement_imbalance_mean']):.3f}, "
            f"buyback_rate={float(r['buyback_after_take_profit_rate']):.3f}"
        )

    lines = [
        "# batch_replay 调参执行结果",
        "",
        "## 1) baseline 指标",
        _line(baseline),
        "",
        "## 2) 仅放宽执行门槛",
        _line(relax),
        "",
        "## 3) 尾盘平衡微调",
        _line(tail),
        "",
        "## 4) 小网格批量对比（Top 5）",
    ]
    if top_grid.empty:
        lines.append("- (无 batch_compare 结果)")
    else:
        for _, row in top_grid.iterrows():
            lines.append(
                f"- `{row['scenario']}`: score={float(row['objective_score']):.4f}, "
                f"exec={float(row['signal_execution_rate']):.4f}, net={float(row['total_net_profit']):.3f}, "
                f"dd={float(row['max_drawdown']):.3f}, imbalance={float(row['settlement_imbalance_mean']):.3f}"
            )
    lines.extend(
        [
            "",
            "## 最优参数组",
            f"- `best_scenario`: `{best_scenario}`",
            "- `best_overrides`:",
            "```json",
            json.dumps(best_overrides, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    input_dir = (ROOT / args.input_dir).resolve()
    config_path = (ROOT / args.config).resolve()
    output_dir = (ROOT / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists():
        raise FileNotFoundError(f"输入目录不存在: {input_dir}")
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    max_cycles = args.max_cycles if args.max_cycles and args.max_cycles > 0 else None
    events, cycle_dirs = _load_events_from_cycle_dirs(input_dir, args.cycle_glob, args.event_file_name, max_cycles)
    if not events:
        raise RuntimeError(
            f"未在 {input_dir} 下找到匹配 {args.cycle_glob}/{args.event_file_name} 的成交流，无法执行调参。"
        )
    print(f"已加载事件: {len(events)}，周期目录: {len(cycle_dirs)}", flush=True)

    relax_seconds_values = _parse_float_list(args.relax_seconds_values)
    relax_move_values = _parse_float_list(args.relax_move_values)
    tail_ratio_values = _parse_float_list(args.tail_ratio_values)
    tail_conf_values = _parse_float_list(args.tail_confidence_values)

    relax_seconds = sorted(relax_seconds_values)[1] if len(relax_seconds_values) > 1 else relax_seconds_values[0]
    relax_move = sorted(relax_move_values)[1] if len(relax_move_values) > 1 else relax_move_values[0]
    tail_ratio = sorted(tail_ratio_values)[1] if len(tail_ratio_values) > 1 else tail_ratio_values[0]
    tail_conf = sorted(tail_conf_values)[1] if len(tail_conf_values) > 1 else tail_conf_values[0]

    baseline = Scenario(name="baseline", phase="baseline", overrides={})
    relax = Scenario(
        name=f"relax_gates_s{relax_seconds:g}_m{relax_move:g}",
        phase="relax_gates",
        overrides={
            "execution.min_seconds_between_orders": relax_seconds,
            "execution.min_same_outcome_price_move_ratio": relax_move,
        },
    )
    tail = Scenario(
        name=f"tail_balance_s{relax_seconds:g}_m{relax_move:g}_r{tail_ratio:g}_c{tail_conf:g}",
        phase="tail_balance",
        overrides={
            "execution.min_seconds_between_orders": relax_seconds,
            "execution.min_same_outcome_price_move_ratio": relax_move,
            "last_minute.preferred_leg_min_ratio": tail_ratio,
            "last_minute.last_minute_min_confidence": tail_conf,
        },
    )

    scenarios: list[Scenario] = [baseline, relax, tail]
    for sec, move, ratio, conf in product(
        relax_seconds_values,
        relax_move_values,
        tail_ratio_values,
        tail_conf_values,
    ):
        scenario = Scenario(
            name=f"grid_s{sec:g}_m{move:g}_r{ratio:g}_c{conf:g}",
            phase="batch_compare",
            overrides={
                "execution.min_seconds_between_orders": float(sec),
                "execution.min_same_outcome_price_move_ratio": float(move),
                "last_minute.preferred_leg_min_ratio": float(ratio),
                "last_minute.last_minute_min_confidence": float(conf),
            },
        )
        scenarios.append(scenario)

    rows: list[dict[str, object]] = []
    total = len(scenarios)
    for idx, scenario in enumerate(scenarios, start=1):
        print(f"[{idx}/{total}] 回放 {scenario.name} …", flush=True)
        row = _run_one_scenario(
            scenario,
            base_config_path=config_path,
            starting_cash=args.starting_cash,
            events=events,
            output_root=output_dir,
        )
        rows.append(row)
        print(
            f"  -> exec={row['signal_execution_rate']:.4f}, net={row['total_net_profit']:.3f}, "
            f"dd={row['max_drawdown']:.3f}, imbalance={row['settlement_imbalance_mean']:.3f}",
            flush=True,
        )

    summary = pd.DataFrame(rows)
    summary["objective_score"] = (
        -args.objective_balance_weight * summary["settlement_imbalance_mean"].astype(float)
        -args.objective_drawdown_weight * summary["max_drawdown"].astype(float)
        +args.objective_profit_weight * summary["total_net_profit"].astype(float)
        +args.objective_exec_weight * summary["signal_execution_rate"].astype(float)
    )

    baseline_df = summary.loc[summary["phase"] == "baseline"].copy()
    relax_df = summary.loc[summary["phase"] == "relax_gates"].copy()
    tail_df = summary.loc[summary["phase"] == "tail_balance"].copy()
    batch_df = summary.loc[summary["phase"] == "batch_compare"].copy()

    batch_sorted = batch_df.sort_values(
        by=["objective_score", "settlement_imbalance_mean", "max_drawdown", "total_net_profit"],
        ascending=[False, True, True, False],
    )

    ordered = pd.concat(
        [
            baseline_df,
            relax_df,
            tail_df,
            batch_sorted,
        ],
        ignore_index=True,
    )
    ordered.to_csv(output_dir / "tuning_summary.csv", index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(output_dir / "tuning_summary.xlsx", engine="openpyxl") as writer:
        ordered.to_excel(writer, sheet_name="summary", index=False)

    top_k = max(args.top_k, 1)
    top_df = batch_sorted.head(top_k)
    top_df.to_csv(output_dir / "top_candidates.csv", index=False, encoding="utf-8-sig")

    if top_df.empty:
        raise RuntimeError("batch_compare 没有产出候选结果，无法筛选最优参数。")

    best_row = top_df.iloc[0]
    best_scenario = str(best_row["scenario"])
    best_overrides = json.loads(str(best_row["overrides_json"]))
    (output_dir / "best_config_overrides.json").write_text(
        json.dumps(best_overrides, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    _build_report_md(
        ordered,
        output_dir / "tuning_report.md",
        best_scenario=best_scenario,
        best_overrides=best_overrides,
    )

    print(f"调参回放完成: {output_dir}")
    print(f"最优参数组: {best_scenario}")
    print(top_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
