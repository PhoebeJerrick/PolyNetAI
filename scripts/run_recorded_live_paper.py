from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

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
    parser.add_argument("--starting-cash", type=float, default=1000.0)
    parser.add_argument(
        "--per-cycle-cash",
        type=float,
        default=None,
        help="兼容参数：当前准实时回放暂不使用该值，传入时仅用于保持与批量启动脚本参数一致。",
    )
    parser.add_argument("--pace-factor", type=float, default=20.0)
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


def _write_sim_batch_reports(output_dir: str | Path, result: LiveRunnerResult) -> tuple[Path, Path]:
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
    )
    _cleanup_batch_replay_markdown(out_dir)
    return perf_xlsx, trade_xlsx


def main() -> int:
    args = parse_args()

    if args.per_cycle_cash is not None:
        print(
            "[兼容提示] --per-cycle-cash 在 run_recorded_live_paper.py 中暂未启用，"
            "将继续按策略配置和账户可用资金执行。"
        )

    run_start_time = time.monotonic()
    print("\n" + "=" * 70)
    print(f"[模拟下单测试 - 流式处理] 开始于 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # ── [1/5] 加载输入数据 ─────────────────────────────────────────────
    print("\n[1/5] 加载输入数据...")
    input_dir_parts = _parse_input_dirs(args.input_dirs)
    if input_dir_parts:
        input_dirs = [_resolve_input_dir(ROOT, raw) for raw in input_dir_parts]
    else:
        input_dirs = [_resolve_input_dir(ROOT, args.input_dir)]

    missing = [p for p in input_dirs if not p.exists()]
    if missing:
        raise FileNotFoundError(f"输入目录不存在: {', '.join(str(p) for p in missing)}")

    for inp in input_dirs:
        print(f"  ✓ 输入目录已验证: {inp.name}")

    max_cycles = args.max_cycles if args.max_cycles and args.max_cycles > 0 else None
    cycle_dirs = sorted(_load_cycle_dirs_from_input_dirs(input_dirs, args.cycle_glob), key=_cycle_sort_key)
    if max_cycles is not None and max_cycles > 0:
        cycle_dirs = cycle_dirs[:max_cycles]
    print(f"  ✓ 发现 {len(cycle_dirs)} 个周期目录")

    cycle_event_counts: dict[str, int] = {}
    events: list = []
    for cycle_dir in cycle_dirs:
        event_path = cycle_dir / args.event_file_name
        cycle_events = load_recorded_trade_events(event_path)
        if cycle_events:
            cycle_event_counts[cycle_dir.name] = len(cycle_events)
            events.extend(cycle_events)
    events = sorted(events, key=lambda item: item.timestamp)
    if not events:
        raise RuntimeError(
            "未找到成交流文件，无法启动准实时回放。"
            f"\n- 输入目录: {', '.join(str(p) for p in input_dirs)}"
            f"\n- 周期匹配: {args.cycle_glob}"
            f"\n- 成交流文件: {args.event_file_name}"
        )
    if args.limit is not None:
        events = events[: args.limit]

    # ── [2/5] 初始化回放引擎 ────────────────────────────────────────────
    print("\n[2/5] 初始化回放引擎...")
    engine = ReplayEngine.from_yaml(args.config, starting_cash=args.starting_cash)
    runner = LivePaperRunner(engine)
    print(f"  ✓ 引擎已初始化，初始资金: {args.starting_cash} USDT | 资金模式: cumulative")

    # ── [3/5] 流式处理 ──────────────────────────────────────────────────
    print(f"\n[3/5] 开始逐周期流式处理（共 {len(cycle_dirs)} 个周期）...")

    _n_completed = [0]
    _last_cycle_end: list[float] = [time.monotonic()]
    _per_cycle_elapsed: list[float] = []

    def _on_cycle_complete(cycle_row: dict) -> None:
        now = time.monotonic()
        elapsed = now - _last_cycle_end[0]
        _last_cycle_end[0] = now
        _n_completed[0] += 1
        slug = str(cycle_row.get("cycle_id", "?"))
        pnl = float(cycle_row.get("cycle_net_profit", 0.0))
        n_events = cycle_event_counts.get(slug, 0)
        _per_cycle_elapsed.append(elapsed)
        events_str = f"{n_events}/{n_events} 事件, " if n_events else ""
        print(
            f"  ✓ 周期 {_n_completed[0]}/{len(cycle_dirs)} ({slug}): "
            f"{events_str}盈亏 {pnl:.2f}, 耗时 {elapsed:.1f}s"
        )

    progress_callback = None
    if args.dashboard_refresh_seconds > 0:
        print(f"  实时 dashboard 已启用：每 {args.dashboard_refresh_seconds:.1f}s 写盘一次。")

        def _flush_progress(result: LiveRunnerResult) -> None:
            export_live_result(
                result,
                args.output_dir,
                refresh_seconds=args.dashboard_refresh_seconds,
                write_excel=False,
            )

        progress_callback = _flush_progress

    _stream_start = time.monotonic()
    _last_cycle_end[0] = _stream_start
    event_stream = iter_events_with_pacing(
        events,
        pace_factor=args.pace_factor,
        max_sleep_seconds=args.max_sleep_seconds,
    )
    result = runner.run_stream(
        event_stream,
        status_every=args.status_every,
        on_progress=progress_callback,
        progress_interval_seconds=max(0.0, args.dashboard_refresh_seconds),
        on_cycle_complete=_on_cycle_complete,
    )
    stream_elapsed = time.monotonic() - _stream_start
    print(f"  ✓ 流式处理完成 (总耗时 {stream_elapsed:.1f}s)")

    export_live_result(
        result,
        args.output_dir,
        refresh_seconds=max(1.0, args.dashboard_refresh_seconds) if args.dashboard_refresh_seconds > 0 else 1.0,
    )
    print(f"  ✓ Dashboard 数据已同步至: {args.output_dir}")

    # ── [4/5] 生成报告 ──────────────────────────────────────────────────
    print("\n[4/5] 从流式输出生成最终报告...")
    if args.include_trade_process:
        performance_xlsx, trade_process_xlsx = _write_sim_batch_reports(args.output_dir, result)
        print(f"  ✓ 总绩效报告 (Excel): {performance_xlsx}")
        print(f"  ✓ 交易过程 (Excel): {trade_process_xlsx}")
    else:
        print(f"  ℹ 跳过 Excel 报告（未指定 --include-trade-process）")

    # ── [5/5] 性能统计 ──────────────────────────────────────────────────
    print("\n[5/5] 性能统计...")
    _metrics = result.replay_result.metrics_df.iloc[0].to_dict() if not result.replay_result.metrics_df.empty else {}
    n_cycles = len(result.replay_result.cycle_df)
    total_net_profit = float(_metrics.get("total_net_profit", 0.0))
    win_rate_raw = float(_metrics.get("win_rate", 0.0))
    win_rate_pct = win_rate_raw * 100 if win_rate_raw <= 1.0 else win_rate_raw
    max_drawdown = float(abs(_metrics.get("max_drawdown", 0.0)))
    avg_elapsed = sum(_per_cycle_elapsed) / len(_per_cycle_elapsed) if _per_cycle_elapsed else 0.0
    print(f"  ✓ 总周期数: {n_cycles}")
    print(f"  ✓ 总盈亏: {total_net_profit:.2f} USDT")
    print(f"  ✓ 胜率: {win_rate_pct:.1f}%")
    print(f"  ✓ 最大回撤: {max_drawdown:.2f} USDT")
    if _per_cycle_elapsed:
        print(f"  ✓ 平均每周期耗时: {avg_elapsed:.1f}s")
    print(result.replay_result.metrics_df.to_string(index=False))

    # ── 完成横幅 ────────────────────────────────────────────────────────
    total_elapsed = time.monotonic() - run_start_time
    total_min = int(total_elapsed // 60)
    total_sec = int(total_elapsed % 60)
    print(f"\n{'=' * 70}")
    print(f"[完成] 流式处理完成 | 周期数: {n_cycles}")
    print(f"      总耗时: {total_min}分 {total_sec}秒")
    print(f"{'=' * 70}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
