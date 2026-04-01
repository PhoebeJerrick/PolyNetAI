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

from polynet_ai.adapters.cycle_window_timing import filter_trade_events_after_post_window_delay
from polynet_ai.strategy.spec import load_strategy_config, resolve_post_window_start_delay_seconds
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


def resolve_position_value_denominator_from_config(config) -> float:
    value = float(config.get("position.max_position_value", 85.0))
    if value <= 1e-12:
        value = 85.0
    return value


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
    parser.add_argument("--starting-cash", type=float, default=200.0)
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
    parser.add_argument(
        "--dashboard-refresh-every-cycles",
        type=int,
        default=5,
        help="per-cycle 模式下每处理多少个周期才同步一次 dashboard；<=0 表示仅在结束时同步。",
    )
    parser.add_argument(
        "--include-performance-report",
        action="store_true",
        default=False,
        help="是否生成绩效汇总 Excel（sim_batch_replay_performance_report_*.xlsx）。",
    )
    parser.add_argument(
        "--include-trade-process",
        action="store_true",
        default=False,
        help="是否生成交易过程详细 Excel（batch_replay_trade_process_zh_*.xlsx）；依赖 --include-performance-report。",
    )
    parser.add_argument(
        "--post-window-start-delay-seconds",
        type=float,
        default=None,
        help="若指定则覆盖 strategy.yaml 的 cycle.post_window_start_delay_seconds。",
    )
    parser.add_argument(
        "--processing-mode",
        type=str,
        choices=["per-cycle", "merged"],
        default=None,
        help="处理模式：per-cycle=逐周期独立回放（默认，亦可由 strategy.yaml 的 batch_replay.processing_mode 控制）；merged=合并事件流回放。",
    )
    parser.add_argument(
        "--progress-file",
        default="",
        help="可选：每完成一个周期写入一次轻量进度文件（用于 record.sh ms 快速读取进度）。",
    )
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


def _resolve_processing_mode(config, cli_mode: str | None) -> str:
    raw = cli_mode if cli_mode is not None else config.get("batch_replay.processing_mode", "per-cycle")
    mode = str(raw).strip().lower()
    aliases = {
        "per-cycle": "per-cycle",
        "per_cycle": "per-cycle",
        "streaming": "per-cycle",
        "merged": "merged",
        "merge": "merged",
    }
    return aliases.get(mode, "per-cycle")


def recording_slug_for_path(cycle_dir: Path, *, root: Path | None = None) -> str:
    """周期录制目录相对项目根的路径（posix），用作流式 CSV / 绩效表中的 cycle_slug。

    当 ``--input-dirs`` 同时包含 ``record_job`` 与 ``record_job/More_RawData`` 等根目录时，
    不同根下可能出现**同名** ``btc-updown-5m-*`` 文件夹；仅用 ``Path.name`` 会在「分周期累计盈亏」里撞名。
    """
    base = (root or ROOT).resolve()
    resolved = cycle_dir.resolve()
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return str(resolved)


def clear_streaming_csv_cache(output_dir: str | Path) -> int:
    """删除输出目录下 ``streaming_*.csv``（流式回放中间结果）。每次新跑流式回放前应调用，避免与上次运行追加/错位混用。"""
    d = Path(output_dir)
    if not d.is_dir():
        return 0
    removed = 0
    for path in sorted(d.glob("streaming_*.csv")):
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


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


def _append_dataframe_to_csv(path: Path, df: pd.DataFrame) -> None:
    """追加 DataFrame 到 CSV，列顺序与已有表头严格一致。

    若直接用 ``DataFrame([row_dict]).to_csv(append)`` 而各 row 的 dict 键顺序不同，后续物理行的列会
    与首行表头错位，read_csv 后 ``cycle_slug`` 等字段会读错（常见症状：多行 slug 完全相同）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        df.to_csv(path, mode="w", header=True, index=False)
        return
    header = pd.read_csv(path, nrows=0)
    cols = list(header.columns)
    extra = [c for c in df.columns if c not in cols]
    if extra:
        old = pd.read_csv(path)
        all_cols = list(dict.fromkeys(list(old.columns) + list(df.columns)))
        old = old.reindex(columns=all_cols)
        df = df.reindex(columns=all_cols)
        pd.concat([old, df], ignore_index=True).to_csv(path, index=False)
        return
    aligned = df.reindex(columns=cols)
    aligned.to_csv(path, mode="a", header=False, index=False)


def _repair_duplicate_streaming_slugs(
    cycle_df: pd.DataFrame, decision_df: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """若周期表中同一 cycle_slug 出现多行（多为历史错位 CSV），用 cycle_index 后缀区分并同步决策表。"""
    if cycle_df.empty or "cycle_slug" not in cycle_df.columns or "cycle_index" not in cycle_df.columns:
        return cycle_df, decision_df
    dup_slugs = cycle_df.groupby("cycle_slug").size()
    bad = set(dup_slugs[dup_slugs > 1].index.astype(str))
    if not bad:
        return cycle_df, decision_df

    cycle_df = cycle_df.copy()
    decision_df = decision_df.copy() if not decision_df.empty else decision_df

    def _suffix_idx(val: object) -> str:
        if pd.isna(val):
            return "na"
        try:
            return str(int(float(val)))
        except (TypeError, ValueError):
            return str(val)

    def _fix_cycle_row(r: pd.Series) -> str:
        s = str(r["cycle_slug"])
        if s in bad:
            return f"{s}__i{_suffix_idx(r['cycle_index'])}"
        return s

    cycle_df["cycle_slug"] = cycle_df.apply(_fix_cycle_row, axis=1)

    if not decision_df.empty and "cycle_slug" in decision_df.columns and "cycle_index" in decision_df.columns:
        def _fix_dec_row(r: pd.Series) -> str:
            s = str(r["cycle_slug"])
            if s in bad:
                return f"{s}__i{_suffix_idx(r['cycle_index'])}"
            return s

        decision_df["cycle_slug"] = decision_df.apply(_fix_dec_row, axis=1)

    return cycle_df, decision_df


def write_cycle_results_incremental(
    output_dir: Path,
    cycle_idx: int,
    cycle_row: dict,
    decision_rows: list[dict],
    snapshot_rows: list[dict],
    *,
    recording_slug: str | None = None,
) -> None:
    """将单个周期的结果增量写入CSV文件。

    recording_slug: 唯一标识本段回放的数据源路径（推荐 ``recording_slug_for_path(cycle_dir)``）。
    多 input 根目录下同名 ``btc-updown-5m-*`` 时，仅用目录 basename 会撞名，导致「分周期累计盈亏」重复 slug。
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    row_out = dict(cycle_row)
    slug = (recording_slug or "").strip() or str(row_out.get("cycle_slug") or row_out.get("cycle_id") or "")
    row_out["cycle_slug"] = slug

    # 写入周期结果
    cycle_csv = output_dir / "streaming_cycle_results.csv"
    cycle_df = pd.DataFrame([row_out])
    cycle_df["cycle_index"] = cycle_idx
    _append_dataframe_to_csv(cycle_csv, cycle_df)

    # 写入决策结果
    if decision_rows:
        decision_csv = output_dir / "streaming_decision_results.csv"
        decision_df = pd.DataFrame(decision_rows)
        decision_df["cycle_index"] = cycle_idx
        decision_df["cycle_slug"] = slug
        _append_dataframe_to_csv(decision_csv, decision_df)

    # 写入快照结果（可选，数据量大）
    # if snapshot_rows:
    #     snapshot_csv = output_dir / "streaming_snapshot_results.csv"
    #     snapshot_df = pd.DataFrame(snapshot_rows)
    #     snapshot_df['cycle_index'] = cycle_idx
    #     if snapshot_csv.exists():
    #         snapshot_df.to_csv(snapshot_csv, mode='a', header=False, index=False)
    #     else:
    #         snapshot_df.to_csv(snapshot_csv, mode='w', header=True, index=False)


def write_progress_file(progress_file: Path, *, completed: int, total: int) -> None:
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(f"{int(completed)},{int(total)}\n", encoding="utf-8")


def _build_summary_df(cycle_df: pd.DataFrame, decision_df: pd.DataFrame) -> pd.DataFrame:
    if cycle_df.empty:
        return pd.DataFrame(columns=["cycle_slug", "executed_trades", "accepted_signals", "blocked_signals", "total_net_profit", "total_fees", "winner", "account_cash"])

    decisions = decision_df.copy()
    if decisions.empty:
        decisions = pd.DataFrame(columns=["cycle_id", "risk_status", "executed", "fill_fee", "cycle_index"])
    if "cycle_id" not in decisions.columns and "cycle_slug" in decisions.columns:
        decisions["cycle_id"] = decisions["cycle_slug"]
    if "cycle_id" not in decisions.columns:
        decisions["cycle_id"] = ""

    decisions["risk_status"] = decisions.get("risk_status", "").fillna("").astype(str)
    decisions["executed"] = decisions.get("executed", False).fillna(False).astype(bool)
    decisions["fill_fee"] = pd.to_numeric(decisions.get("fill_fee", 0.0), errors="coerce").fillna(0.0)

    use_cycle_index = (
        "cycle_index" in cycle_df.columns
        and not decisions.empty
        and "cycle_index" in decisions.columns
    )
    grouped = decisions.groupby("cycle_id", dropna=False)
    accepted_counts = grouped.apply(lambda frame: int((frame["risk_status"] == "accepted").sum()) if not frame.empty else 0)
    blocked_counts = grouped.apply(lambda frame: int((frame["risk_status"] == "blocked").sum()) if not frame.empty else 0)
    executed_counts = grouped["executed"].sum().astype(int) if not decisions.empty else pd.Series(dtype=int)
    total_fees = grouped["fill_fee"].sum() if not decisions.empty else pd.Series(dtype=float)

    rows: list[dict[str, object]] = []
    for _, row in cycle_df.iterrows():
        slug = str(row.get("cycle_slug") or row.get("cycle_id") or "")
        cycle_id = str(row.get("cycle_id", ""))

        if use_cycle_index:
            cidx = row.get("cycle_index")
            if pd.notna(cidx):
                ci = int(float(cidx))
                idx_match = pd.to_numeric(decisions["cycle_index"], errors="coerce") == float(ci)
                sub = decisions.loc[idx_match]
            else:
                sub = decisions.iloc[0:0]
            executed_trades = int(sub["executed"].sum()) if not sub.empty else 0
            accepted_signals = int((sub["risk_status"] == "accepted").sum()) if not sub.empty else 0
            blocked_signals = int((sub["risk_status"] == "blocked").sum()) if not sub.empty else 0
            fees_sum = float(sub["fill_fee"].sum()) if not sub.empty else 0.0
        else:
            executed_trades = int(executed_counts.get(cycle_id, 0))
            accepted_signals = int(accepted_counts.get(cycle_id, 0))
            blocked_signals = int(blocked_counts.get(cycle_id, 0))
            fees_sum = float(total_fees.get(cycle_id, 0.0))

        rows.append(
            {
                "cycle_slug": slug,
                "executed_trades": executed_trades,
                "accepted_signals": accepted_signals,
                "blocked_signals": blocked_signals,
                "total_net_profit": float(pd.to_numeric(row.get("cycle_net_profit", 0.0), errors="coerce")),
                "total_fees": fees_sum,
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
    position_value_denominator: float = 1000.0,
    include_performance_report: bool = True,
    include_trade_process: bool = True,
) -> tuple[Path | None, Path | None]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cycle_df = result.replay_result.cycle_df.copy()
    decision_df = result.replay_result.decision_df.copy()
    if not cycle_df.empty and "cycle_slug" not in cycle_df.columns and "cycle_id" in cycle_df.columns:
        cycle_df["cycle_slug"] = cycle_df["cycle_id"].astype(str)
    if not decision_df.empty and "cycle_slug" not in decision_df.columns and "cycle_id" in decision_df.columns:
        decision_df["cycle_slug"] = decision_df["cycle_id"].astype(str)

    summary_df = _build_summary_df(cycle_df, decision_df)
    perf_xlsx: Path | None = None
    trade_xlsx: Path | None = None
    if include_trade_process:
        trade_xlsx = write_batch_trade_process_zh(
            input_dir=out_dir,
            cycle_df=cycle_df,
            decision_df=decision_df,
            output_path=out_dir,
        )
    if include_performance_report:
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
            position_value_denominator=position_value_denominator,
        )
        _cleanup_batch_replay_markdown(out_dir)
    return perf_xlsx, trade_xlsx


def _df_rows_for_engine(df: pd.DataFrame) -> list[dict[str, object]]:
    if df.empty:
        return []
    filled = df.replace({np.nan: None})
    return [dict(row) for row in filled.to_dict(orient="records")]


def _export_dashboard_from_streaming(
    *,
    engine: ReplayEngine,
    output_dir: Path,
    snapshot_rows: list[dict[str, object]],
    refresh_seconds: float,
    write_excel: bool,
    position_value_denominator: float | None,
) -> bool:
    """从 streaming_*.csv 与当前 engine 状态重建指标并写 dashboard 产物（cycles.csv 等）。"""
    cycle_csv = output_dir / "streaming_cycle_results.csv"
    decision_csv = output_dir / "streaming_decision_results.csv"
    if not cycle_csv.exists():
        return False
    cycle_df = pd.read_csv(cycle_csv)
    if cycle_df.empty:
        return False
    decision_df = pd.read_csv(decision_csv) if decision_csv.exists() else pd.DataFrame()

    if not cycle_df.empty and "cycle_slug" not in cycle_df.columns and "cycle_id" in cycle_df.columns:
        cycle_df = cycle_df.copy()
        cycle_df["cycle_slug"] = cycle_df["cycle_id"].astype(str)
    if not decision_df.empty and "cycle_slug" not in decision_df.columns and "cycle_id" in decision_df.columns:
        decision_df = decision_df.copy()
        decision_df["cycle_slug"] = decision_df["cycle_id"].astype(str)

    cycle_df, decision_df = _repair_duplicate_streaming_slugs(cycle_df, decision_df)

    cycle_rows = _df_rows_for_engine(cycle_df)
    decision_rows = _df_rows_for_engine(decision_df)
    replay_result = engine._build_result(cycle_rows, decision_rows)
    snapshot_df = pd.DataFrame(snapshot_rows) if snapshot_rows else pd.DataFrame()
    live_result = LiveRunnerResult(replay_result=replay_result, snapshot_df=snapshot_df)
    rs = refresh_seconds if refresh_seconds > 0 else 1.0
    export_live_result(
        live_result,
        output_dir,
        title="Polynet AI Monitoring Dashboard",
        refresh_seconds=max(1.0, rs),
        write_excel=write_excel,
        position_value_denominator=position_value_denominator,
    )
    return True


def main_streaming(args: argparse.Namespace | None = None) -> int:
    """流式处理版本：逐周期加载和处理，避免内存累积"""
    args = args or parse_args()
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
    seen_resolved: set[Path] = set()
    deduped_cycle_dirs: list[Path] = []
    for _cd in cycle_dirs:
        _key = _cd.resolve()
        if _key in seen_resolved:
            continue
        seen_resolved.add(_key)
        deduped_cycle_dirs.append(_cd)
    cycle_dirs = deduped_cycle_dirs
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
    progress_file: Path | None = None
    if str(args.progress_file).strip():
        progress_file = Path(args.progress_file)
        if not progress_file.is_absolute():
            progress_file = (ROOT / progress_file).resolve()

    _rm_stream = clear_streaming_csv_cache(output_dir)
    if _rm_stream:
        print(f"  ✓ 已清理流式缓存: {_rm_stream} 个 streaming_*.csv")
    if progress_file is not None:
        write_progress_file(progress_file, completed=0, total=len(cycle_dirs))

    print(f"\n[3/5] 开始逐周期流式处理（共 {len(cycle_dirs)} 个周期）...")
    _cfg = load_strategy_config(args.config)
    position_value_denominator = resolve_position_value_denominator_from_config(_cfg)
    _pwd = resolve_post_window_start_delay_seconds(
        config=_cfg,
        cli_seconds=args.post_window_start_delay_seconds,
    )
    _pwd_src = (
        "命令行"
        if args.post_window_start_delay_seconds is not None
        else "strategy.yaml"
    )
    print(f"  ℹ 窗起点后策略推迟: {_pwd:g}s（来源: {_pwd_src}）")
    run_start_time = datetime.now()
    accumulated_snapshot_rows: list[dict[str, object]] = []

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
        raw_n = len(sorted_events)
        sorted_events = filter_trade_events_after_post_window_delay(
            sorted_events,
            post_window_start_delay_seconds=_pwd,
        )
        if _pwd > 0 and len(sorted_events) != raw_n:
            print(
                f"  ℹ 周期 {cycle_idx}: 推迟 {_pwd:g}s 后参与策略的事件 {len(sorted_events)}/{raw_n} 条"
            )

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
                snapshot_rows,
                recording_slug=recording_slug_for_path(cycle_dir),
            )

            # 更新聚合指标
            aggregator.update(cycle_row)

            cycle_elapsed = (datetime.now() - cycle_start).total_seconds()
            profit = cycle_row.get('cycle_net_profit', 0.0)
            print(f"  ✓ 周期 {cycle_idx}/{len(cycle_dirs)} ({cycle_dir.name}): "
                  f"{len(sorted_events)}/{len(cycle_events)} 事件, 盈亏 {profit:.2f}, 耗时 {cycle_elapsed:.1f}s")
            if progress_file is not None:
                write_progress_file(progress_file, completed=cycle_idx, total=len(cycle_dirs))

            accumulated_snapshot_rows.extend(result.snapshot_df.to_dict(orient="records"))
            should_refresh_dashboard = False
            if args.dashboard_refresh_seconds > 0:
                if args.dashboard_refresh_every_cycles <= 0:
                    should_refresh_dashboard = False
                else:
                    should_refresh_dashboard = (cycle_idx % args.dashboard_refresh_every_cycles == 0)
            if should_refresh_dashboard:
                _export_dashboard_from_streaming(
                    engine=engine,
                    output_dir=output_dir,
                    snapshot_rows=accumulated_snapshot_rows,
                    refresh_seconds=args.dashboard_refresh_seconds,
                    write_excel=False,
                    position_value_denominator=position_value_denominator,
                )

        # 释放内存
        del cycle_events, sorted_events, result

    run_elapsed = (datetime.now() - run_start_time).total_seconds()
    if progress_file is not None:
        write_progress_file(progress_file, completed=aggregator.total_cycles, total=len(cycle_dirs))
    print(f"  ✓ 流式处理完成 (总耗时 {run_elapsed:.1f}s)")

    cycle_csv = output_dir / "streaming_cycle_results.csv"
    if cycle_csv.exists():
        did_export = _export_dashboard_from_streaming(
            engine=engine,
            output_dir=output_dir,
            snapshot_rows=accumulated_snapshot_rows,
            refresh_seconds=args.dashboard_refresh_seconds,
            write_excel=False,
            position_value_denominator=position_value_denominator,
        )
        if did_export:
            print(f"  ✓ Dashboard 数据已同步至: {output_dir}")

    include_trade_process = bool(args.include_trade_process)
    include_performance_report = bool(args.include_performance_report or include_trade_process)
    if include_trade_process and not args.include_performance_report:
        print("  ℹ 已自动启用绩效报告：--include-trade-process 依赖 --include-performance-report")
    print(f"\n[4/5] 从流式输出生成最终报告...")
    # 读取流式输出文件
    decision_csv = output_dir / "streaming_decision_results.csv"

    if cycle_csv.exists():
        cycle_df = pd.read_csv(cycle_csv)
        decision_df = pd.read_csv(decision_csv) if decision_csv.exists() else pd.DataFrame()

        # 兼容旧版流式输出：若缺失 cycle_slug，则从 cycle_id 补齐。
        if not cycle_df.empty and "cycle_slug" not in cycle_df.columns and "cycle_id" in cycle_df.columns:
            cycle_df["cycle_slug"] = cycle_df["cycle_id"].astype(str)
        if not decision_df.empty and "cycle_slug" not in decision_df.columns and "cycle_id" in decision_df.columns:
            decision_df["cycle_slug"] = decision_df["cycle_id"].astype(str)

        cycle_df, decision_df = _repair_duplicate_streaming_slugs(cycle_df, decision_df)

        # 构建汇总数据
        summary_df = _build_summary_df(cycle_df, decision_df)

        # 生成报告
        if include_trade_process:
            trade_xlsx = write_batch_trade_process_zh(
                input_dir=output_dir,
                cycle_df=cycle_df,
                decision_df=decision_df,
                output_path=output_dir,
            )
            print(f"  ✓ 交易过程 (Excel): {trade_xlsx}")
        if include_performance_report:
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
                position_value_denominator=position_value_denominator,
            )
            _cleanup_batch_replay_markdown(output_dir)
            print(f"  ✓ 总绩效报告 (Excel): {perf_xlsx}")
        if not include_trade_process and not include_performance_report:
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


def main(args: argparse.Namespace | None = None) -> int:
    args = args or parse_args()
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
    _cfg = load_strategy_config(args.config)
    _pwd = resolve_post_window_start_delay_seconds(
        config=_cfg,
        cli_seconds=args.post_window_start_delay_seconds,
    )
    if _pwd > 0:
        raw_total = len(events)
        events = filter_trade_events_after_post_window_delay(
            events,
            post_window_start_delay_seconds=_pwd,
        )
        print(f"  ℹ 窗起点后策略推迟 {_pwd:g}s：合并流使用 {len(events)}/{raw_total} 条事件")
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
    _cfg = load_strategy_config(args.config)
    position_value_denominator = resolve_position_value_denominator_from_config(_cfg)
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
                position_value_denominator=position_value_denominator,
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
        position_value_denominator=position_value_denominator,
    )
    print(f"  ✓ 数据已导出到: {args.output_dir}")
    include_trade_process = bool(args.include_trade_process)
    include_performance_report = bool(args.include_performance_report or include_trade_process)
    if include_trade_process and not args.include_performance_report:
        print("  ℹ 已自动启用绩效报告：--include-trade-process 依赖 --include-performance-report")
    print(f"\n[5/5] 生成总绩效报告...")
    if include_trade_process or include_performance_report:
        performance_xlsx, trade_process_xlsx = _write_sim_batch_reports(
            args.output_dir,
            result,
            capital_reset_mode=args.capital_reset_mode,
            starting_cash=args.starting_cash,
            position_value_denominator=position_value_denominator,
            include_performance_report=include_performance_report,
            include_trade_process=include_trade_process,
        )
        if performance_xlsx is not None:
            print(f"  ✓ 总绩效报告 (Excel): {performance_xlsx}")
        if trade_process_xlsx is not None:
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
    _args = parse_args()
    _cfg = load_strategy_config(_args.config)
    _mode = _resolve_processing_mode(_cfg, _args.processing_mode)
    if _mode == "merged":
        print("  ℹ 处理模式: merged（合并事件流）")
        raise SystemExit(main(_args))
    print("  ℹ 处理模式: per-cycle（逐周期独立回放）")
    raise SystemExit(main_streaming(_args))
