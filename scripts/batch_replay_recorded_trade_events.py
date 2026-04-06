from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.cycle_window_timing import filter_trade_events_after_post_window_delay  # noqa: E402
from polynet_ai.adapters.trade_event_store import load_recorded_trade_events  # noqa: E402
from polynet_ai.engine.replay import ReplayEngine  # noqa: E402
from polynet_ai.strategy.spec import (  # noqa: E402
    load_strategy_config,
    resolve_post_window_start_delay_seconds,
)
from scripts.build_batch_replay_performance_report import (  # noqa: E402
    _cleanup_batch_replay_markdown,
    build_performance_report_zh,
    resolve_phase_end_seconds,
    write_batch_trade_process_zh,
)
from scripts.run_recorded_live_paper import (  # noqa: E402
    _append_dataframe_to_csv,
    clear_streaming_csv_cache,
    recording_slug_for_path,
)


_CYCLE_DIR_RE = re.compile(r".+-updown-\d+m-\d+$")


def _event_file_matches_cycle_dir(path: Path) -> bool:
    """空文件保留给后续 skip；非空文件必须能读到匹配目录名的 cycle_id。"""
    saw_nonblank_line = False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                saw_nonblank_line = True
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                cycle_id = str(payload.get("cycle_id") or "").strip()
                if cycle_id:
                    return cycle_id == path.parent.name
    except OSError:
        return False
    return not saw_nonblank_line


def _resolve_existing_path(label: str, path: str | Path) -> Path:
    """相对路径优先相对 cwd；不存在时再尝试项目根目录。"""
    p = Path(path)
    if p.is_absolute():
        if p.exists():
            return p.resolve()
        raise FileNotFoundError(f"未找到{label}（绝对路径）: {p}")

    cwd_path = (Path.cwd() / p).resolve()
    if cwd_path.exists():
        return cwd_path
    root_path = (ROOT / p).resolve()
    if root_path.exists():
        return root_path
    raise FileNotFoundError(
        f"未找到{label}: {p}\n"
        f"  已尝试: {cwd_path}\n"
        f"  已尝试: {root_path}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量回放抓取目录下的 websocket 事件流")
    parser.add_argument("--input-dir", required=True, help="抓取目录，例如 artifacts/live/ws_capture_btc_10cycles")
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--overrides", default=None, help="可选 JSON 覆盖参数，例如 trial_022 的 overrides.json")
    parser.add_argument("--starting-cash", type=float, default=200.0)
    parser.add_argument("--per-cycle-cash", type=float, default=None,
                        help="固定模式下每周期实际投注资金；不设置则与 --starting-cash 相同")
    parser.add_argument(
        "--capital-reset-mode",
        type=str,
        choices=["fixed", "cumulative"],
        default="fixed",
        help="周期资金处理模式：fixed=每周期重置，cumulative=跨周期累积",
    )
    parser.add_argument("--output-dir", default=None, help="默认输出到 <input-dir>/batch_replay_outputs")
    parser.add_argument(
        "--include-trade-process",
        action="store_true",
        default=False,
        help="是否生成交易过程详细 Excel（batch_replay_trade_process_zh_*.xlsx）",
    )
    parser.add_argument(
        "--post-window-start-delay-seconds",
        type=float,
        default=None,
        help="若指定则覆盖 strategy.yaml 的 cycle.post_window_start_delay_seconds。",
    )
    return parser.parse_args()


def _load_config(config_path: str | Path, overrides_path: str | Path | None):
    cfg_resolved = _resolve_existing_path("配置文件", config_path)
    config = load_strategy_config(cfg_resolved)
    if overrides_path:
        path = _resolve_existing_path("overrides 文件", overrides_path)
        with path.open("r", encoding="utf-8") as fh:
            overrides = json.load(fh)
        if not isinstance(overrides, dict):
            raise ValueError(f"overrides 文件必须是 JSON 对象: {path}")
        config = config.with_overrides(overrides)
    return config


def _discover_cycle_event_files(input_dir: Path) -> list[Path]:
    files = sorted(
        path
        for path in input_dir.glob("*/ws_trade_events.ndjson")
        if path.is_file() and _CYCLE_DIR_RE.fullmatch(path.parent.name) is not None
    )
    return [path for path in files if _event_file_matches_cycle_dir(path)]


def _effective_use_streaming(*, use_streaming: bool, processing_mode: str | None) -> bool:
    """与 run_recorded_live_paper 的 processing_mode 语义对齐：per-cycle=流式逐文件；merged=单引擎合并。"""
    if processing_mode is None:
        return use_streaming
    raw = str(processing_mode).strip().lower().replace("_", "-")
    aliases = {
        "per-cycle": True,
        "streaming": True,
        "merged": False,
        "merge": False,
    }
    if raw in aliases:
        return aliases[raw]
    return use_streaming


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
    cycle_slug: str,
    cycle_row: dict,
    decision_rows: list[dict],
) -> None:
    """将单个周期的结果增量写入CSV文件（列与表头对齐，避免追加行错位）。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    cycle_csv = output_dir / "streaming_cycle_results.csv"
    cycle_df = pd.DataFrame([cycle_row])
    cycle_df["cycle_index"] = cycle_idx
    cycle_df["cycle_slug"] = cycle_slug
    _append_dataframe_to_csv(cycle_csv, cycle_df)

    if decision_rows:
        decision_csv = output_dir / "streaming_decision_results.csv"
        decision_df = pd.DataFrame(decision_rows)
        decision_df["cycle_index"] = cycle_idx
        decision_df["cycle_slug"] = cycle_slug
        _append_dataframe_to_csv(decision_csv, decision_df)


def run_batch_replay(
    input_dir: str | Path,
    config_path: str | Path = "configs/strategy.yaml",
    overrides_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    starting_cash: float = 200.0,
    capital_reset_mode: str = "fixed",
    per_cycle_cash: float | None = None,
    include_trade_process: bool = False,
    cycle_range: str = "",
    cycle_count: int = 0,
    report_source: str = "",
    max_cycles: int | None = None,
    report_name_prefix: str = "",
    use_streaming: bool = True,  # 新增参数：默认使用流式处理
    post_window_start_delay_seconds: float | None = None,
    processing_mode: str | None = None,
) -> Path | None:
    """核心 batch replay 逻辑，可被外部脚本调用。返回 Excel 绩效报告路径。

    ``processing_mode`` 与 ``use_streaming`` 同时传入时，以 ``processing_mode`` 为准
    （例如 ``per-cycle`` / ``merged``），便于与实盘脚本参数对齐。
    """
    input_resolved = _resolve_existing_path("输入目录", input_dir)
    output_resolved = Path(output_dir) if output_dir else input_resolved / "batch_replay_outputs"
    output_resolved.mkdir(parents=True, exist_ok=True)

    config = _load_config(config_path, overrides_path)
    _pwd = resolve_post_window_start_delay_seconds(
        config=config,
        cli_seconds=post_window_start_delay_seconds,
    )
    event_files = _discover_cycle_event_files(input_resolved)
    if not event_files:
        raise RuntimeError(f"未在目录下找到任何 `<cycle_slug>/ws_trade_events.ndjson`: {input_resolved}")
    if max_cycles is not None and max_cycles > 0:
        event_files = event_files[-max_cycles:]

    total_files = len(event_files)
    print(f"  ℹ 共需回放 {total_files} 个周期文件")
    print(f"  ℹ 窗起点后策略推迟: {_pwd:g}s（与实盘/WebSocket 路径一致）")

    stream = _effective_use_streaming(use_streaming=use_streaming, processing_mode=processing_mode)

    # 引擎内部根据 capital_reset_mode 处理周期资金
    engine = ReplayEngine(
        config,
        starting_cash=starting_cash,
        capital_reset_mode=capital_reset_mode,
        per_cycle_cash=per_cycle_cash,
    )

    if stream:
        # 流式处理模式：逐周期处理，增量输出
        aggregator = StreamingAggregator()

        clear_streaming_csv_cache(output_resolved)

        for idx, event_file in enumerate(event_files, 1):
            cycle_slug = recording_slug_for_path(event_file.parent)
            events = load_recorded_trade_events(event_file)
            if not events:
                print(f"[skip] {cycle_slug}: 事件文件为空")
                continue

            sorted_events = sorted(events, key=lambda e: (e.market_id, e.cycle_id, e.timestamp))
            raw_n = len(sorted_events)
            sorted_events = filter_trade_events_after_post_window_delay(
                sorted_events,
                post_window_start_delay_seconds=_pwd,
            )
            if _pwd > 0 and len(sorted_events) != raw_n:
                print(
                    f"    [{idx}] 推迟 {_pwd:g}s：使用 {len(sorted_events)}/{raw_n} 条事件"
                )
            cycle_decisions: list[dict[str, object]] = []

            for event in sorted_events:
                step = engine.process_event(event)
                cycle_decisions.append(step.decision_row)

            # 获取周期结果：须用结算后口径，与 LivePaperRunner（每段流结束 finalize_pending_cycle）一致。
            # 决策行里的 cycle_net_profit 来自特征快照，多为未结算值；逐文件回放时下一周期文件尚未读入，
            # 不会在本文件最后一条事件上触发 _finalize_cycle，若直接取 cycle_decisions[-1] 会与「分周期执行交易流水」
            # （按成交与结算口径）及 simulation 报告不一致。
            if cycle_decisions:
                finalized = engine.finalize_pending_cycle()
                if finalized is not None:
                    cycle_row = {
                        "cycle_id": cycle_slug,
                        "cycle_net_profit": float(finalized.get("cycle_net_profit", 0.0)),
                        "winner": finalized.get("winner", ""),
                        "account_cash": finalized.get("account_cash", None),
                    }
                else:
                    cycle_row = {
                        "cycle_id": cycle_slug,
                        "cycle_net_profit": float(cycle_decisions[-1].get("cycle_net_profit", 0.0)),
                        "winner": cycle_decisions[-1].get("winner", ""),
                        "account_cash": cycle_decisions[-1].get("account_cash", None),
                    }

                # 增量写入文件
                write_cycle_results_incremental(
                    output_resolved,
                    idx,
                    cycle_slug,
                    cycle_row,
                    cycle_decisions
                )

                # 更新聚合指标
                aggregator.update(cycle_row)

                executed = sum(1 for d in cycle_decisions if d.get("executed"))
                net_profit = cycle_row['cycle_net_profit']
                print(
                    f"    [{idx}/{total_files}] {cycle_slug}: "
                    f"events={len(sorted_events)}/{len(events)} | profit={net_profit:.4f} | "
                    f"executed={executed}"
                )

            # 释放内存
            del events, sorted_events, cycle_decisions

        # 各周期已在文件循环内 finalize；此处仅兜底（例如未来改为合并读入多文件单周期时）
        pending = engine.finalize_pending_cycle()
        if pending:
            print("  ℹ 流结束后仍有未结算周期（已结算）；请检查事件切分逻辑")

        # 从流式输出文件读取数据生成报告
        cycle_csv = output_resolved / "streaming_cycle_results.csv"
        decision_csv = output_resolved / "streaming_decision_results.csv"

        if cycle_csv.exists():
            cycle_df = pd.read_csv(cycle_csv)
            decision_df = pd.read_csv(decision_csv) if decision_csv.exists() else pd.DataFrame()

            # 构建汇总数据
            summary_rows = []
            for _, row in cycle_df.iterrows():
                slug = str(row.get('cycle_slug', ''))
                cycle_decisions = decision_df[decision_df['cycle_slug'] == slug].to_dict('records') if not decision_df.empty else []

                total_fees = sum(float(d.get("fill_fee", 0)) for d in cycle_decisions)
                net_profit = float(row.get("cycle_net_profit", 0.0))
                summary_rows.append({
                    "cycle_slug": slug,
                    "event_count": 0,  # 流式模式下不记录事件数
                    "executed_trades": sum(1 for d in cycle_decisions if d.get("executed")),
                    "accepted_signals": sum(1 for d in cycle_decisions if d.get("risk_status") == "accepted"),
                    "blocked_signals": sum(1 for d in cycle_decisions if d.get("risk_status") == "blocked"),
                    "total_net_profit": net_profit,
                    "total_fees": total_fees,
                    "win_rate": 1.0 if net_profit > 0 else 0.0,
                    "winner": row.get("winner", ""),
                    "account_cash": row.get("account_cash", None),
                })

            summary_df = pd.DataFrame(summary_rows)
        else:
            summary_df = pd.DataFrame()
            cycle_df = pd.DataFrame()
            decision_df = pd.DataFrame()

    else:
        # 原有的一次性加载模式（保留作为备份）
        summary_rows: list[dict[str, object]] = []
        cycle_parts: list[pd.DataFrame] = []
        decision_parts: list[pd.DataFrame] = []
        all_finalized_cycle_rows: list[dict[str, object]] = []
        per_cycle_meta: dict[str, dict] = {}

        for idx, event_file in enumerate(event_files, 1):
            cycle_slug = event_file.parent.name
            events = load_recorded_trade_events(event_file)
            if not events:
                print(f"[skip] {cycle_slug}: 事件文件为空")
                continue

            sorted_events = sorted(events, key=lambda e: (e.market_id, e.cycle_id, e.timestamp))
            sorted_events = filter_trade_events_after_post_window_delay(
                sorted_events,
                post_window_start_delay_seconds=_pwd,
            )
            cycle_decisions: list[dict[str, object]] = []

            for event in sorted_events:
                step = engine.process_event(event)
                if step.finalized_cycle_row is not None:
                    all_finalized_cycle_rows.append(step.finalized_cycle_row)
                cycle_decisions.append(step.decision_row)

            per_cycle_meta[cycle_slug] = {
                "event_count": len(events),
                "decision_rows": cycle_decisions,
            }

            executed = sum(1 for d in cycle_decisions if d.get("executed"))
            net_profit = float(cycle_decisions[-1].get("cycle_net_profit", 0.0)) if cycle_decisions else 0.0
            print(
                f"    [{idx}/{total_files}] {cycle_slug}: "
                f"events={len(sorted_events)}/{len(events)} | profit={net_profit:.4f} | "
                f"executed={executed}"
            )

        pending = engine.finalize_pending_cycle()
        if pending is not None:
            all_finalized_cycle_rows.append(pending)

        for cycle_row in all_finalized_cycle_rows:
            slug = str(cycle_row.get("cycle_id", ""))
            meta = per_cycle_meta.get(slug, {})
            decisions = meta.get("decision_rows", [])

            cdf = pd.DataFrame([cycle_row])
            cdf["cycle_slug"] = slug
            cycle_parts.append(cdf)

            if decisions:
                ddf = pd.DataFrame(decisions)
                ddf["cycle_slug"] = slug
                decision_parts.append(ddf)

            total_fees = sum(float(d.get("fill_fee", 0)) for d in decisions)
            net_profit = float(cycle_row.get("cycle_net_profit", 0.0))
            summary_rows.append({
                "cycle_slug": slug,
                "event_count": meta.get("event_count", 0),
                "executed_trades": sum(1 for d in decisions if d.get("executed")),
                "accepted_signals": sum(1 for d in decisions if d.get("risk_status") == "accepted"),
                "blocked_signals": sum(1 for d in decisions if d.get("risk_status") == "blocked"),
                "total_net_profit": net_profit,
                "total_fees": total_fees,
                "win_rate": 1.0 if net_profit > 0 else 0.0,
                "winner": cycle_row.get("winner", ""),
                "account_cash": cycle_row.get("account_cash", None),
            })

        summary_df = pd.DataFrame(summary_rows)
        if not summary_df.empty:
            summary_df = summary_df.sort_values("cycle_slug").reset_index(drop=True)
        cycle_df = pd.concat(cycle_parts, ignore_index=True) if cycle_parts else pd.DataFrame()
        decision_df = pd.concat(decision_parts, ignore_index=True) if decision_parts else pd.DataFrame()

    # 生成报告（流式和非流式共用）
    trade_xlsx: Path | None = None
    if include_trade_process:
        trade_xlsx = write_batch_trade_process_zh(
            input_dir=input_resolved,
            cycle_df=cycle_df,
            decision_df=decision_df,
            output_path=output_resolved,
        )

    xlsx_report = build_performance_report_zh(
        resolved_batch_dir=output_resolved,
        summary_df=summary_df,
        cycle_df=cycle_df,
        decision_df=decision_df,
        output_path=output_resolved,
        display_batch_dir=input_resolved,
        cycle_range=cycle_range,
        cycle_count=cycle_count,
        report_source=report_source,
        report_name_prefix=report_name_prefix,
        capital_reset_mode=capital_reset_mode,
        starting_cash=starting_cash,
        phase_end_seconds=resolve_phase_end_seconds(config_path=config_path),
    )
    _cleanup_batch_replay_markdown(output_resolved)

    print(f"  ✓ 回放完成，共 {len(summary_df)} 个周期")

    if xlsx_report.exists():
        print(f"  ✓ 绩效报告: {xlsx_report}")
        return xlsx_report
    else:
        print(
            f"  ✗ 警告: 未找到生成的 Excel 报告文件: {xlsx_report}\n"
            f"     请检查 {output_resolved} 目录是否包含文件。",
            file=sys.stderr,
        )
        if output_resolved.exists():
            files = list(output_resolved.glob("*.xlsx"))
            if files:
                print(f"  ℹ 输出目录中找到的 Excel 文件:", file=sys.stderr)
                for f in files:
                    print(f"     - {f.name}", file=sys.stderr)
        return None


def main() -> int:
    args = parse_args()
    xlsx_report = run_batch_replay(
        input_dir=args.input_dir,
        config_path=args.config,
        overrides_path=args.overrides,
        output_dir=args.output_dir,
        starting_cash=args.starting_cash,
        capital_reset_mode=args.capital_reset_mode,
        per_cycle_cash=args.per_cycle_cash,
        include_trade_process=args.include_trade_process,
        post_window_start_delay_seconds=args.post_window_start_delay_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
