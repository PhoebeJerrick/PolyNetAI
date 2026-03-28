from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.trade_event_store import load_recorded_trade_events
from polynet_ai.engine.live import LivePaperRunner, LiveRunnerResult, export_live_result
from polynet_ai.engine.replay import ReplayEngine
try:
    # Preferred when running as module from project root.
    from scripts.build_batch_replay_performance_report import (
        _cleanup_batch_replay_markdown,
        build_performance_report_zh,
        write_batch_trade_process_zh,
    )
except ModuleNotFoundError:
    # Fallback when running as a script file (sys.path[0] == scripts/).
    from build_batch_replay_performance_report import (
        _cleanup_batch_replay_markdown,
        build_performance_report_zh,
        write_batch_trade_process_zh,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基于 ws_trade_events.ndjson 的准实时 paper trading runner")
    parser.add_argument("--input-dir", default="artifacts/live/record_job;artifacts/live/record_job/More_RawData", help="单一输入根目录（兼容旧参数）")
    parser.add_argument(
        "--input-dirs",
        default="",
        help="多个输入根目录（用逗号或分号分隔）。会在每个目录下扫描周期子目录并合并回放事件。",
    )
    parser.add_argument("--cycle-glob", default="btc-updown-5m-*")
    parser.add_argument("--event-file-name", default="ws_trade_events.ndjson")
    parser.add_argument("--max-cycles", type=int, default=10)
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--output-dir", default="artifacts/live/record_job/batch_replay_outputs")
    parser.add_argument("--starting-cash", type=float, default=100.0)
    parser.add_argument("--per-cycle-cash", type=float, default=None,
                        help="固定模式下每周期实际投注资金；不设置则与 --starting-cash 相同")
    parser.add_argument(
        "--capital-reset-mode",
        type=str,
        choices=["fixed", "cumulative"],
        default="fixed",
        help="周期资金处理模式：fixed=每周期固定本金并累计盈亏曲线，cumulative=跨周期累积资金",
    )
    parser.add_argument("--pace-factor", type=float, default=1000000.0)
    parser.add_argument("--max-sleep-seconds", type=float, default=0.25)
    parser.add_argument("--status-every", type=int, default=100)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dashboard-refresh-seconds", type=float, default=1.0)
    parser.add_argument("--include-trade-process", action="store_true", default=False, help="是否生成交易过程详细 Excel")
    return parser.parse_args()


def _cycle_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.name.rsplit("-", 1)[-1]
    try:
        return (int(suffix), path.name)
    except ValueError:
        return (0, path.name)


def _parse_input_dirs(raw: str | None) -> list[str]:
    if raw is None:
        return []
    raw = str(raw).strip()
    if not raw:
        return []
    # 允许同时用逗号或分号分隔；避免在路径中包含逗号/分号导致无法解析的问题。
    return [p.strip() for p in re.split(r"[;,]", raw) if p and p.strip()]


def _resolve_input_dir(root: Path, raw_dir: str) -> Path:
    p = Path(raw_dir).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _load_cycle_dirs_from_input_dirs(
    input_dirs: list[Path],
    cycle_glob: str,
) -> list[Path]:
    # 返回所有匹配的“周期子目录”，后续由调用方做全局排序与截断。
    cycle_dirs: list[Path] = []
    for input_dir in input_dirs:
        cycle_dirs.extend((p for p in input_dir.glob(cycle_glob) if p.is_dir()))
    return cycle_dirs


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


class StreamingAggregator:
    """流式聚合器，维护必要的跨周期指标"""

    def __init__(self):
        self.cycle_profits: list[float] = []
        self.total_cycles = 0
        self.total_profit = 0.0
        self.win_count = 0
        self.total_fees = 0.0

    def update(self, cycle_row: dict) -> None:
        """更新聚合指标"""
        profit = float(cycle_row.get('cycle_net_profit', 0.0))
        self.cycle_profits.append(profit)
        self.total_cycles += 1
        self.total_profit += profit
        if profit > 0:
            self.win_count += 1

    def compute_max_drawdown(self) -> float:
        """计算最大回撤"""
        if not self.cycle_profits:
            return 0.0
        cumsum = np.cumsum(self.cycle_profits)
        running_max = np.maximum.accumulate(cumsum)
        drawdown = running_max - cumsum
        return float(np.max(drawdown))

    def get_win_rate(self) -> float:
        """计算胜率"""
        if self.total_cycles == 0:
            return 0.0
        return self.win_count / self.total_cycles


def write_cycle_results_incremental(
    output_dir: Path,
    cycle_idx: int,
    cycle_row: dict,
    decision_rows: list[dict],
    snapshot_rows: list[dict],
) -> None:
    """将单个周期的结果增量写入CSV文件"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 写入周期结果
    cycle_csv = output_dir / "streaming_cycle_results.csv"
    cycle_df = pd.DataFrame([cycle_row])
    cycle_df['cycle_index'] = cycle_idx
    if cycle_csv.exists():
        cycle_df.to_csv(cycle_csv, mode='a', header=False, index=False)
    else:
        cycle_df.to_csv(cycle_csv, mode='w', header=True, index=False)

    # 写入决策结果
    if decision_rows:
        decision_csv = output_dir / "streaming_decision_results.csv"
        decision_df = pd.DataFrame(decision_rows)
        decision_df['cycle_index'] = cycle_idx
        if decision_csv.exists():
            decision_df.to_csv(decision_csv, mode='a', header=False, index=False)
        else:
            decision_df.to_csv(decision_csv, mode='w', header=True, index=False)

    # 写入快照结果（可选，数据量大）
    # if snapshot_rows:
    #     snapshot_csv = output_dir / "streaming_snapshot_results.csv"
    #     snapshot_df = pd.DataFrame(snapshot_rows)
    #     snapshot_df['cycle_index'] = cycle_idx
    #     if snapshot_csv.exists():
    #         snapshot_df.to_csv(snapshot_csv, mode='a', header=False, index=False)
    #     else:
    #         snapshot_df.to_csv(snapshot_csv, mode='w', header=True, index=False)


def _build_summary_df(cycle_df: pd.DataFrame, decision_df: pd.DataFrame) -> pd.DataFrame:
    if cycle_df.empty:
        return pd.DataFrame(columns=["cycle_slug", "executed_trades", "accepted_signals", "blocked_signals", "total_net_profit", "total_fees", "winner", "account_cash"])

    decisions = decision_df.copy()
    if decisions.empty:
        decisions = pd.DataFrame(columns=["cycle_id", "risk_status", "executed", "fill_fee"])
    if "cycle_id" not in decisions.columns and "cycle_slug" in decisions.columns:
        decisions["cycle_id"] = decisions["cycle_slug"]
    if "cycle_id" not in decisions.columns:
        decisions["cycle_id"] = ""

    decisions["risk_status"] = decisions.get("risk_status", "").fillna("").astype(str)
    decisions["executed"] = decisions.get("executed", False).fillna(False).astype(bool)
    decisions["fill_fee"] = pd.to_numeric(decisions.get("fill_fee", 0.0), errors="coerce").fillna(0.0)

    grouped = decisions.groupby("cycle_id", dropna=False)
    accepted_counts = grouped.apply(lambda frame: int((frame["risk_status"] == "accepted").sum()) if not frame.empty else 0)
    blocked_counts = grouped.apply(lambda frame: int((frame["risk_status"] == "blocked").sum()) if not frame.empty else 0)
    executed_counts = grouped["executed"].sum().astype(int) if not decisions.empty else pd.Series(dtype=int)
    total_fees = grouped["fill_fee"].sum() if not decisions.empty else pd.Series(dtype=float)

    rows: list[dict[str, object]] = []
    for _, row in cycle_df.iterrows():
        cycle_id = str(row.get("cycle_id", ""))
        rows.append(
            {
                "cycle_slug": cycle_id,
                "executed_trades": int(executed_counts.get(cycle_id, 0)),
                "accepted_signals": int(accepted_counts.get(cycle_id, 0)),
                "blocked_signals": int(blocked_counts.get(cycle_id, 0)),
                "total_net_profit": float(pd.to_numeric(row.get("cycle_net_profit", 0.0), errors="coerce")),
                "total_fees": float(total_fees.get(cycle_id, 0.0)),
                "winner": row.get("winner", ""),
                "account_cash": row.get("account_cash", None),
            }
        )
    return pd.DataFrame(rows)


def _write_sim_batch_reports(
    output_dir: str | Path,
    result: LiveRunnerResult,
    *,
    capital_reset_mode: str,
    starting_cash: float,
) -> tuple[Path, Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cycle_df = result.replay_result.cycle_df.copy()
    decision_df = result.replay_result.decision_df.copy()
    if not cycle_df.empty and "cycle_slug" not in cycle_df.columns and "cycle_id" in cycle_df.columns:
        cycle_df["cycle_slug"] = cycle_df["cycle_id"].astype(str)
    if not decision_df.empty and "cycle_slug" not in decision_df.columns and "cycle_id" in decision_df.columns:
        decision_df["cycle_slug"] = decision_df["cycle_id"].astype(str)

    summary_df = _build_summary_df(cycle_df, decision_df)
    trade_xlsx = write_batch_trade_process_zh(
        input_dir=out_dir,
        cycle_df=cycle_df,
        decision_df=decision_df,
        output_path=out_dir,
    )
    perf_xlsx = build_performance_report_zh(
        resolved_batch_dir=out_dir,
        summary_df=summary_df,
        cycle_df=cycle_df,
        decision_df=decision_df,
        output_path=out_dir,
        display_batch_dir=out_dir,
        report_source="模拟下单测试",
        report_name_prefix="simulation",
        capital_reset_mode=capital_reset_mode,
        starting_cash=starting_cash,
    )
    _cleanup_batch_replay_markdown(out_dir)
    return perf_xlsx, trade_xlsx


def main_streaming() -> int:
    """流式处理版本：逐周期加载和处理，避免内存累积"""
    args = parse_args()
    start_time = datetime.now()
    print("\n" + "="*70)
    print(f"[模拟下单测试 - 流式处理] 开始于 {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)

    print(f"\n[1/5] 加载输入数据...")
    input_dir_parts = _parse_input_dirs(args.input_dirs)
    if input_dir_parts:
        input_dirs = [_resolve_input_dir(ROOT, raw) for raw in input_dir_parts]
    else:
        input_dirs = [_resolve_input_dir(ROOT, args.input_dir)]

    missing = [p for p in input_dirs if not p.exists()]
    if missing:
        raise FileNotFoundError(f"输入目录不存在: {', '.join(str(p) for p in missing)}")
    print(f"  ✓ 输入目录已验证: {', '.join(str(p.name) for p in input_dirs)}")

    max_cycles = args.max_cycles if args.max_cycles and args.max_cycles > 0 else None

    cycle_dirs = sorted(_load_cycle_dirs_from_input_dirs(input_dirs, args.cycle_glob), key=_cycle_sort_key)
    if max_cycles is not None and max_cycles > 0:
        cycle_dirs = cycle_dirs[:max_cycles]
    print(f"  ✓ 发现 {len(cycle_dirs)} 个周期目录")

    print(f"\n[2/5] 初始化回放引擎...")
    engine = ReplayEngine.from_yaml(
        args.config,
        starting_cash=args.starting_cash,
        capital_reset_mode=args.capital_reset_mode,
        per_cycle_cash=args.per_cycle_cash,
    )
    runner = LivePaperRunner(engine)
    print(
        f"  ✓ 引擎已初始化，初始资金: {args.starting_cash} USDT | "
        f"资金模式: {args.capital_reset_mode}"
    )

    # 初始化流式聚合器
    aggregator = StreamingAggregator()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[3/5] 开始逐周期流式处理（共 {len(cycle_dirs)} 个周期）...")
    run_start_time = datetime.now()

    for cycle_idx, cycle_dir in enumerate(cycle_dirs, 1):
        cycle_start = datetime.now()
        event_path = cycle_dir / args.event_file_name

        # 加载单个周期的事件
        cycle_events = load_recorded_trade_events(event_path)
        if not cycle_events:
            print(f"  ⚠ 周期 {cycle_idx}/{len(cycle_dirs)} ({cycle_dir.name}): 无事件，跳过")
            continue

        sorted_events = sorted(cycle_events, key=lambda e: e.timestamp)
        if args.limit is not None:
            sorted_events = sorted_events[:args.limit]

        # 处理单个周期
        event_stream = iter_events_with_pacing(
            sorted_events,
            pace_factor=args.pace_factor,
            max_sleep_seconds=args.max_sleep_seconds,
        )

        result = runner.run_stream(
            event_stream,
            status_every=args.status_every,
            on_progress=None,
            progress_interval_seconds=0.0,
        )

        # 提取周期结果
        if not result.replay_result.cycle_df.empty:
            cycle_row = result.replay_result.cycle_df.iloc[-1].to_dict()
            decision_rows = result.replay_result.decision_df.to_dict('records')
            snapshot_rows = result.snapshot_df.to_dict('records')

            # 增量写入文件
            write_cycle_results_incremental(
                output_dir,
                cycle_idx,
                cycle_row,
                decision_rows,
                snapshot_rows
            )

            # 更新聚合指标
            aggregator.update(cycle_row)

            cycle_elapsed = (datetime.now() - cycle_start).total_seconds()
            profit = cycle_row.get('cycle_net_profit', 0.0)
            print(f"  ✓ 周期 {cycle_idx}/{len(cycle_dirs)} ({cycle_dir.name}): "
                  f"{len(cycle_events)} 事件, 盈亏 {profit:.2f}, 耗时 {cycle_elapsed:.1f}s")

        # 释放内存
        del cycle_events, sorted_events, result

    run_elapsed = (datetime.now() - run_start_time).total_seconds()
    print(f"  ✓ 流式处理完成 (总耗时 {run_elapsed:.1f}s)")

    print(f"\n[4/5] 从流式输出生成最终报告...")
    # 读取流式输出文件
    cycle_csv = output_dir / "streaming_cycle_results.csv"
    decision_csv = output_dir / "streaming_decision_results.csv"

    if cycle_csv.exists():
        cycle_df = pd.read_csv(cycle_csv)
        decision_df = pd.read_csv(decision_csv) if decision_csv.exists() else pd.DataFrame()

        # 构建汇总数据
        summary_df = _build_summary_df(cycle_df, decision_df)

        # 生成报告
        if args.include_trade_process:
            trade_xlsx = write_batch_trade_process_zh(
                input_dir=output_dir,
                cycle_df=cycle_df,
                decision_df=decision_df,
                output_path=output_dir,
            )
            perf_xlsx = build_performance_report_zh(
                resolved_batch_dir=output_dir,
                summary_df=summary_df,
                cycle_df=cycle_df,
                decision_df=decision_df,
                output_path=output_dir,
                display_batch_dir=output_dir,
                report_source="模拟下单测试（流式）",
                report_name_prefix="simulation_streaming",
                capital_reset_mode=args.capital_reset_mode,
                starting_cash=args.starting_cash,
            )
            _cleanup_batch_replay_markdown(output_dir)
            print(f"  ✓ 总绩效报告 (Excel): {perf_xlsx}")
            print(f"  ✓ 交易过程 (Excel): {trade_xlsx}")
        else:
            print(f"  ✓ 流式输出文件已生成在: {output_dir}")

    print(f"\n[5/5] 性能统计...")
    total_elapsed = (datetime.now() - start_time).total_seconds()
    print(f"  ✓ 总周期数: {aggregator.total_cycles}")
    print(f"  ✓ 总盈亏: {aggregator.total_profit:.2f} USDT")
    print(f"  ✓ 胜率: {aggregator.get_win_rate():.1%}")
    print(f"  ✓ 最大回撤: {aggregator.compute_max_drawdown():.2f} USDT")
    print(f"  ✓ 平均每周期耗时: {run_elapsed/max(1, aggregator.total_cycles):.1f}s")

    print(f"\n{'='*70}")
    print(f"[完成] 流式处理完成 | 周期数: {aggregator.total_cycles}")
    print(f"      总耗时: {int(total_elapsed//60)}分 {total_elapsed%60:.0f}秒")
    print(f"{'='*70}\n")
    return 0


def main() -> int:
    args = parse_args()
    start_time = datetime.now()
    print("\n" + "="*70)
    print(f"[模拟下单测试] 开始于 {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)

    print(f"\n[1/5] 加载输入数据...")
    input_dir_parts = _parse_input_dirs(args.input_dirs)
    if input_dir_parts:
        input_dirs = [_resolve_input_dir(ROOT, raw) for raw in input_dir_parts]
    else:
        input_dirs = [_resolve_input_dir(ROOT, args.input_dir)]

    missing = [p for p in input_dirs if not p.exists()]
    if missing:
        raise FileNotFoundError(f"输入目录不存在: {', '.join(str(p) for p in missing)}")
    print(f"  ✓ 输入目录已验证: {', '.join(str(p.name) for p in input_dirs)}")

    max_cycles = args.max_cycles if args.max_cycles and args.max_cycles > 0 else None

    cycle_dirs = sorted(_load_cycle_dirs_from_input_dirs(input_dirs, args.cycle_glob), key=_cycle_sort_key)
    if max_cycles is not None and max_cycles > 0:
        cycle_dirs = cycle_dirs[:max_cycles]
    print(f"  ✓ 发现 {len(cycle_dirs)} 个周期目录，正在加载事件流...")

    events = []
    for cycle_dir in cycle_dirs:
        event_path = cycle_dir / args.event_file_name
        cycle_events = load_recorded_trade_events(event_path)
        if cycle_events:
            events.extend(cycle_events)
    events = sorted(events, key=lambda item: item.timestamp)
    if not events:
        raise RuntimeError(
            "未找到成交流文件，无法启动准实时回放。"
            f"\n- 输入目录: {', '.join(str(p) for p in input_dirs)}"
            f"\n- 周期匹配: {args.cycle_glob}"
            f"\n- 成交流文件: {args.event_file_name}"
        )
    print(f"  ✓ 已加载 {len(events)} 条成交事件")
    if args.limit is not None:
        events = events[: args.limit]
        print(f"  ✓ 事件限制: 使用前 {len(events)} 条")

    print(f"\n[2/5] 初始化回放引擎...")
    engine = ReplayEngine.from_yaml(
        args.config,
        starting_cash=args.starting_cash,
        capital_reset_mode=args.capital_reset_mode,
        per_cycle_cash=args.per_cycle_cash,
    )
    runner = LivePaperRunner(engine)
    print(
        f"  ✓ 引擎已初始化，初始资金: {args.starting_cash} USDT | "
        f"资金模式: {args.capital_reset_mode}"
    )
    
    event_stream = iter_events_with_pacing(
        events,
        pace_factor=args.pace_factor,
        max_sleep_seconds=args.max_sleep_seconds,
    )
    progress_callback = None
    if args.dashboard_refresh_seconds > 0:
        print(f"  ✓ 实时 dashboard 已启用：每 {args.dashboard_refresh_seconds:.1f}s 写盘一次")

        def _flush_progress(result: LiveRunnerResult) -> None:
            export_live_result(
                result,
                args.output_dir,
                refresh_seconds=args.dashboard_refresh_seconds,
                write_excel=False,
            )

        progress_callback = _flush_progress

    print(f"\n[3/5] 开始回放事件流（{len(events)} 条事件，速度x{args.pace_factor}）...")
    run_start_time = datetime.now()
    result = runner.run_stream(
        event_stream,
        status_every=args.status_every,
        on_progress=progress_callback,
        progress_interval_seconds=max(0.0, args.dashboard_refresh_seconds),
    )
    run_elapsed = (datetime.now() - run_start_time).total_seconds()
    print(f"  ✓ 回放完成 (耗时 {run_elapsed:.1f}s)")
    
    print(f"\n[4/5] 导出结果数据...")
    export_live_result(
        result,
        args.output_dir,
        refresh_seconds=max(1.0, args.dashboard_refresh_seconds) if args.dashboard_refresh_seconds > 0 else 1.0,
    )
    print(f"  ✓ 数据已导出到: {args.output_dir}")
    print(f"\n[5/5] 生成总绩效报告...")
    if args.include_trade_process:
        performance_xlsx, trade_process_xlsx = _write_sim_batch_reports(
            args.output_dir,
            result,
            capital_reset_mode=args.capital_reset_mode,
            starting_cash=args.starting_cash,
        )
        print(f"  ✓ 总绩效报告 (Excel): {performance_xlsx}")
        print(f"  ✓ 交易过程 (Excel): {trade_process_xlsx}")
    else:
        print(f"  ✓ 报告文件已生成在: {args.output_dir}")
    
    total_elapsed = (datetime.now() - start_time).total_seconds()
    cycle_count = len(result.replay_result.cycle_df)
    print(f"\n{'='*70}")
    print(f"[完成] 模拟下单测试完成 | 周期数: {cycle_count}")
    print(f"      总耗时: {int(total_elapsed//60)}分 {total_elapsed%60:.0f}秒")
    print(f"{'='*70}")
    print(f"\n关键指标汇总:")
    print(result.replay_result.metrics_df.to_string(index=False))
    print()
    return 0


if __name__ == "__main__":
    # 默认使用流式处理版本以优化内存占用和性能
    raise SystemExit(main_streaming())
