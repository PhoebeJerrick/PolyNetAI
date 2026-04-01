"""
在同一批成交事件上回放 baseline（strategy.yaml）与 overrides（如 trial_022），并排输出指标。

对大 CSV 默认走“流式双引擎”：
- 不把全部事件一次性读入内存；
- 同一条事件同时喂给 baseline 与 trial；
- 若同名 `*.progress.json` 中带有 `done_slugs`，会补齐无成交 5 分钟窗口为 0 收益周期。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pandas as pd

from polynet_ai.adapters.excel_loader import dataframe_to_trade_events, load_excel_events
from polynet_ai.engine.replay import ReplayEngine
from polynet_ai.reporting.performance import compute_max_drawdown
from polynet_ai.strategy.spec import load_strategy_config


@dataclass(slots=True)
class ReplayAggregate:
    cycle_profit_by_id: dict[str, float] = field(default_factory=dict)
    cycle_order: list[str] = field(default_factory=list)
    total_fees: float = 0.0
    total_signals: int = 0
    executed_trades: int = 0
    blocked_signals: int = 0
    accepted_signals: int = 0
    selected_rule_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    executed_rule_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Baseline vs overrides 回放对比")
    p.add_argument("--input", required=True, help="xlsx 或 csv（列与 excel_loader 一致；大文件请用 csv）")
    p.add_argument(
        "--csv-chunksize",
        type=int,
        default=400_000,
        help="CSV 分块行数（仅 .csv 生效，默认 400000）",
    )
    p.add_argument("--sheet", default=None)
    p.add_argument("--config", default="configs/strategy.yaml", help="baseline YAML")
    p.add_argument("--overrides", required=True, help="trial 的 overrides.json 路径")
    p.add_argument("--trial-label", default="trial_022", help="对比表中第二列名称")
    p.add_argument("--starting-cash", type=float, default=200.0)
    p.add_argument(
        "--output",
        default=None,
        help="输出对比 CSV（默认 artifacts/replays/compare_<stem>.csv）",
    )
    return p.parse_args()


def cycle_id_from_slug(slug: str) -> str:
    ts = int(str(slug).rsplit("-", 1)[-1])
    return str(datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None))


def load_expected_cycle_ids(input_path: Path) -> list[str] | None:
    progress_path = input_path.with_suffix(".progress.json")
    if not progress_path.exists():
        return None
    try:
        data = json.loads(progress_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    slugs = data.get("done_slugs")
    if not isinstance(slugs, list) or not slugs:
        return None
    out: list[str] = []
    for slug in slugs:
        try:
            out.append(cycle_id_from_slug(str(slug)))
        except Exception:
            continue
    return out or None


def update_decision_aggregate(agg: ReplayAggregate, decision_row: dict[str, object]) -> None:
    selected_rule = str(decision_row.get("selected_rule") or "")
    risk_status = str(decision_row.get("risk_status") or "")
    executed = bool(decision_row.get("executed"))

    if selected_rule:
        agg.total_signals += 1
        agg.selected_rule_counts[selected_rule] += 1
    if risk_status == "blocked":
        agg.blocked_signals += 1
    elif risk_status == "accepted":
        agg.accepted_signals += 1
    if executed:
        agg.executed_trades += 1
        if selected_rule:
            agg.executed_rule_counts[selected_rule] += 1


def update_cycle_aggregate(agg: ReplayAggregate, cycle_row: dict[str, object]) -> None:
    cycle_id = str(cycle_row.get("cycle_id") or "")
    if not cycle_id:
        return
    if cycle_id not in agg.cycle_profit_by_id:
        agg.cycle_order.append(cycle_id)
    agg.cycle_profit_by_id[cycle_id] = float(cycle_row.get("cycle_net_profit") or 0.0)


def iter_csv_events(path: Path, *, chunksize: int) -> Iterable:
    ordinal = 0
    reader = pd.read_csv(path, chunksize=chunksize, encoding="utf-8-sig", low_memory=False)
    for chunk_idx, chunk in enumerate(reader, start=1):
        part, ordinal = dataframe_to_trade_events(chunk, sheet="BTC", ordinal_start=ordinal)
        if chunk_idx % 5 == 0:
            print(f"[CSV] 已处理 chunk {chunk_idx}，累计事件约 {ordinal}", flush=True)
        for event in part:
            yield event


def build_metrics_row(label: str, agg: ReplayAggregate, expected_cycle_ids: list[str] | None) -> dict[str, object]:
    if expected_cycle_ids:
        cycle_ids = expected_cycle_ids
    else:
        cycle_ids = agg.cycle_order

    profits = [float(agg.cycle_profit_by_id.get(cycle_id, 0.0)) for cycle_id in cycle_ids]
    total_cycles = len(cycle_ids)
    wins = sum(1 for value in profits if value > 0)
    total_net_profit = float(sum(profits))
    observed_cycles = len(agg.cycle_profit_by_id)
    empty_cycles = max(total_cycles - observed_cycles, 0)

    row: dict[str, object] = {
        "label": label,
        "total_cycles": total_cycles,
        "observed_cycles_with_trades": observed_cycles,
        "empty_cycles": empty_cycles,
        "total_net_profit": total_net_profit,
        "average_cycle_profit": float(total_net_profit / total_cycles) if total_cycles else 0.0,
        "win_rate": float(wins / total_cycles) if total_cycles else 0.0,
        "max_drawdown": compute_max_drawdown(profits),
        "total_fees": float(agg.total_fees),
        "total_signals": agg.total_signals,
        "executed_trades": agg.executed_trades,
        "blocked_signals": agg.blocked_signals,
        "accepted_signals": agg.accepted_signals,
        "signal_execution_rate": float(agg.executed_trades / agg.total_signals) if agg.total_signals else 0.0,
    }
    for key, value in sorted(agg.selected_rule_counts.items()):
        row[f"selected_rule_{key}"] = value
    for key, value in sorted(agg.executed_rule_counts.items()):
        row[f"executed_rule_{key}"] = value
    return row


def compare_streaming_csv(
    input_path: Path,
    *,
    csv_chunksize: int,
    starting_cash: float,
    base_cfg,
    trial_cfg,
    trial_label: str,
) -> pd.DataFrame:
    expected_cycle_ids = load_expected_cycle_ids(input_path)
    if expected_cycle_ids is not None:
        print(f"已从 progress 读取完整周期数: {len(expected_cycle_ids)}", flush=True)

    engines = {
        "baseline": ReplayEngine(base_cfg, starting_cash=starting_cash),
        trial_label: ReplayEngine(trial_cfg, starting_cash=starting_cash),
    }
    aggs = {label: ReplayAggregate() for label in engines}

    print(f"从 CSV 流式加载事件: {input_path} (chunksize={csv_chunksize}) …", flush=True)
    processed = 0
    for event in iter_csv_events(input_path, chunksize=csv_chunksize):
        processed += 1
        for label, engine in engines.items():
            step = engine.process_event(event)
            update_decision_aggregate(aggs[label], step.decision_row)
            if step.finalized_cycle_row is not None:
                update_cycle_aggregate(aggs[label], step.finalized_cycle_row)
        if processed % 1_000_000 == 0:
            print(f"[回放] 已处理市场成交事件 {processed}", flush=True)

    rows: list[dict[str, object]] = []
    for label, engine in engines.items():
        pending = engine.finalize_pending_cycle()
        if pending is not None:
            update_cycle_aggregate(aggs[label], pending)
        aggs[label].total_fees = engine.account.fees_paid
        rows.append(build_metrics_row(label, aggs[label], expected_cycle_ids))

    return pd.DataFrame(rows)


def compare_in_memory_events(
    events: list,
    *,
    starting_cash: float,
    base_cfg,
    trial_cfg,
    trial_label: str,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for label, cfg in (("baseline", base_cfg), (trial_label, trial_cfg)):
        print(f"\n[回放] {label}（{len(events)} 条事件）…", flush=True)
        engine = ReplayEngine(cfg, starting_cash=starting_cash)
        result = engine.run(events)
        row = result.metrics_df.iloc[0].to_dict()
        row["label"] = label
        row["observed_cycles_with_trades"] = row.get("total_cycles", 0)
        row["empty_cycles"] = 0
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"找不到输入: {input_path}")

    base = load_strategy_config(args.config)
    with Path(args.overrides).open(encoding="utf-8") as fh:
        overrides = json.load(fh)
    if not isinstance(overrides, dict):
        raise SystemExit("overrides 必须是 JSON 对象")
    trial_cfg = base.with_overrides(overrides)

    if input_path.suffix.lower() == ".csv":
        out_df = compare_streaming_csv(
            input_path,
            csv_chunksize=args.csv_chunksize,
            starting_cash=args.starting_cash,
            base_cfg=base,
            trial_cfg=trial_cfg,
            trial_label=args.trial_label,
        )
    else:
        events = load_excel_events(str(input_path), sheet_name=args.sheet)
        out_df = compare_in_memory_events(
            events,
            starting_cash=args.starting_cash,
            base_cfg=base,
            trial_cfg=trial_cfg,
            trial_label=args.trial_label,
        )

    cols = ["label"] + [c for c in out_df.columns if c != "label"]
    out_df = out_df[cols]

    out_path = Path(args.output) if args.output else ROOT / "artifacts" / "replays" / f"compare_{input_path.stem}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(out_df.to_string(index=False))
    print(f"\n已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
