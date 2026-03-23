from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 与 polymarket_tracker_collection_with_accumulated_shares_v5.xlsx 等工作簿一致：固定工作表名 BTC
TRACKER_STYLE_SHEET = "BTC"

# 与 write_batch_trade_process_zh 决策表列顺序一致
TRADE_PROCESS_PREF_COLS = (
    "timestamp",
    "market_price",
    "selected_rule",
    "selected_outcome",
    "selected_action",
    "selected_shares",
    "risk_status",
    "risk_reason",
    "executed",
    "submitted",
    "confirmed",
    "fill_price",
    "fill_fee",
    "cycle_net_profit",
    "account_cash",
)

from polynet_ai.reporting.excel_export import (
    SAME_OUTCOME_PRICE_MOVE_COL,
    _format_elapsed,
    compute_same_outcome_price_move_pct,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据批量回放结果生成中文总绩效报告")
    parser.add_argument(
        "--input-dir",
        required=True,
        help="可传抓取目录或 batch_replay_outputs 目录，例如 artifacts/live/ws_capture_btc_10cycles",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="默认输出到解析后的 batch_replay_outputs 目录",
    )
    return parser.parse_args()


def _resolve_batch_replay_dir(input_dir: str | Path) -> Path:
    path = Path(input_dir)
    if not path.exists():
        raise FileNotFoundError(f"未找到输入目录: {path}")
    nested = path / "batch_replay_outputs"
    report_markers = (
        "batch_replay_summary.csv",
        "batch_replay_performance_report_zh.xlsx",
    )
    if any((path / name).exists() for name in report_markers):
        return path
    if any((nested / name).exists() for name in report_markers):
        return nested
    if nested.exists():
        return nested
    return path


def _discover_cycle_result_dirs(batch_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in batch_dir.iterdir()
        if path.is_dir()
        and (path / "cycles.csv").exists()
        and (path / "decisions.csv").exists()
        and (path / "metrics.csv").exists()
    )


def _compute_max_drawdown(profits: list[float]) -> float:
    if not profits:
        return 0.0
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for profit in profits:
        cumulative += float(profit)
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return max_drawdown


def _load_summary(batch_dir: Path, cycle_dirs: list[Path]) -> pd.DataFrame:
    summary_csv = batch_dir / "batch_replay_summary.csv"
    if summary_csv.exists():
        summary_df = pd.read_csv(summary_csv)
        if not summary_df.empty:
            return summary_df.sort_values("cycle_slug").reset_index(drop=True)

    rows: list[dict[str, object]] = []
    for cycle_dir in cycle_dirs:
        cycle_slug = cycle_dir.name
        metrics = pd.read_csv(cycle_dir / "metrics.csv").iloc[0].to_dict()
        cycles = pd.read_csv(cycle_dir / "cycles.csv").iloc[0].to_dict()
        rows.append(
            {
                "cycle_slug": cycle_slug,
                "executed_trades": metrics.get("executed_trades", 0),
                "accepted_signals": metrics.get("accepted_signals", 0),
                "blocked_signals": metrics.get("blocked_signals", 0),
                "total_net_profit": metrics.get("total_net_profit", 0.0),
                "total_fees": metrics.get("total_fees", 0.0),
                "winner": cycles.get("winner", ""),
                "account_cash": cycles.get("account_cash", None),
            }
        )
    summary_df = pd.DataFrame(rows)
    if not summary_df.empty:
        summary_df = summary_df.sort_values("cycle_slug").reset_index(drop=True)
    return summary_df


def _load_cycle_frames(cycle_dirs: list[Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    cycle_rows: list[pd.DataFrame] = []
    decision_rows: list[pd.DataFrame] = []
    for cycle_dir in cycle_dirs:
        cycle_slug = cycle_dir.name
        cycles_df = pd.read_csv(cycle_dir / "cycles.csv").copy()
        decisions_df = pd.read_csv(cycle_dir / "decisions.csv").copy()
        cycles_df["cycle_slug"] = cycle_slug
        decisions_df["cycle_slug"] = cycle_slug
        cycle_rows.append(cycles_df)
        decision_rows.append(decisions_df)
    cycle_df = pd.concat(cycle_rows, ignore_index=True) if cycle_rows else pd.DataFrame()
    decision_df = pd.concat(decision_rows, ignore_index=True) if decision_rows else pd.DataFrame()
    return cycle_df, decision_df


def _summarize_direction_distribution(decision_df: pd.DataFrame) -> pd.DataFrame:
    if decision_df.empty or "executed" not in decision_df.columns:
        return pd.DataFrame(columns=["selected_outcome", "selected_action", "trades", "shares"])
    executed_df = decision_df[decision_df["executed"].fillna(False).astype(bool)].copy()
    if executed_df.empty:
        return pd.DataFrame(columns=["selected_outcome", "selected_action", "trades", "shares"])
    executed_df["selected_outcome"] = executed_df["selected_outcome"].fillna("").astype(str)
    executed_df["selected_action"] = executed_df["selected_action"].fillna("").astype(str)
    executed_df["selected_shares"] = pd.to_numeric(executed_df["selected_shares"], errors="coerce").fillna(0.0)
    summary = (
        executed_df.groupby(["selected_outcome", "selected_action"], dropna=False)
        .agg(trades=("executed", "size"), shares=("selected_shares", "sum"))
        .reset_index()
        .sort_values(["selected_outcome", "selected_action"])
        .reset_index(drop=True)
    )
    return summary


def _value_counts_frame(series: pd.Series, name: str) -> pd.DataFrame:
    if series.empty:
        return pd.DataFrame(columns=[name, "count"])
    counts = (
        series.fillna("unknown")
        .astype(str)
        .value_counts(dropna=False)
        .rename_axis(name)
        .reset_index(name="count")
    )
    return counts


def _tracker_compute_args() -> argparse.Namespace:
    """与 analyze_polymarket_tracker.compute 默认列名一致。"""
    return argparse.Namespace(
        group_cols=None,
        coin_col="市场标题",
        cycle_col="时间周期",
        qty_col="投注份数",
        outcome_col="结果代币类型",
        action_col="操作方向",
        price_col="成交价格",
    )


def _batch_replay_decisions_to_tracker_input(
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    将批量回放的 cycle / decision 合并为 analyze_polymarket_tracker.compute 所需的宽表。
    与 polymarket_tracker_collection_with_accumulated_shares_v5.xlsx 使用同一套 compute/format_ws 逻辑。
    """
    if decision_df.empty:
        return pd.DataFrame()
    dec = decision_df.copy()
    if "executed" in dec.columns:
        dec = dec[dec["executed"].fillna(False).astype(bool)]
    if dec.empty:
        return pd.DataFrame()

    dec["_sort"] = range(len(dec))

    full = decision_df.copy()
    full["timestamp"] = pd.to_datetime(full.get("timestamp"), errors="coerce", utc=True)
    if "market_id" not in full.columns or full["market_id"].isna().all():
        if not cycle_df.empty and "cycle_slug" in full.columns and "market_id" in cycle_df.columns:
            full = full.merge(
                cycle_df.drop_duplicates(subset=["cycle_slug"], keep="first")[["cycle_slug", "market_id"]],
                on="cycle_slug",
                how="left",
            )
    full["market_id"] = full.get("market_id", pd.Series(dtype=object)).fillna("").astype(str)
    if "cycle_id" in full.columns:
        full["_pid"] = full["cycle_id"].fillna(full.get("cycle_slug", "")).astype(str)
    else:
        full["_pid"] = full.get("cycle_slug", pd.Series(dtype=object)).fillna("").astype(str)

    starts = full.groupby(["market_id", "_pid"], dropna=False)["timestamp"].min().reset_index(name="_cycle_start")

    if "market_id" not in dec.columns or dec["market_id"].isna().all():
        if not cycle_df.empty and "cycle_slug" in dec.columns and "market_id" in cycle_df.columns:
            dec = dec.merge(
                cycle_df.drop_duplicates(subset=["cycle_slug"], keep="first")[["cycle_slug", "market_id"]],
                on="cycle_slug",
                how="left",
            )
    dec["market_id"] = dec.get("market_id", pd.Series(dtype=object)).fillna("").astype(str)
    if "cycle_id" in dec.columns:
        dec["_pid"] = dec["cycle_id"].fillna(dec.get("cycle_slug", "")).astype(str)
    else:
        dec["_pid"] = dec.get("cycle_slug", pd.Series(dtype=object)).fillna("").astype(str)

    dec["timestamp"] = pd.to_datetime(dec.get("timestamp"), errors="coerce", utc=True)
    dec = dec.merge(starts, on=["market_id", "_pid"], how="left")

    if "cycle_id" in dec.columns:
        period = dec["cycle_id"].fillna(dec.get("cycle_slug", "")).astype(str)
    else:
        period = dec["_pid"].astype(str)

    shares = pd.to_numeric(dec.get("selected_shares", pd.Series(0.0, index=dec.index)), errors="coerce").fillna(0.0)
    fill_px = pd.to_numeric(dec.get("fill_price", pd.Series(0.0, index=dec.index)), errors="coerce").fillna(0.0)
    same_move = compute_same_outcome_price_move_pct(
        dec,
        outcome_col="selected_outcome",
        price_col="fill_price",
        group_cols=["market_id", "_pid"],
    )

    raw = pd.DataFrame(
        {
            "下注时间距开盘差(分，秒)": [
                _format_elapsed(ts, t0) for ts, t0 in zip(dec["timestamp"], dec["_cycle_start"])
            ],
            "市场标题": dec["market_id"],
            "时间周期": period,
            "结果代币类型": dec.get("selected_outcome", pd.Series(dtype=object)).fillna("").astype(str),
            "操作方向": dec.get("selected_action", pd.Series(dtype=object)).fillna("").astype(str),
            "投注份数": shares,
            "USDT价值": shares * fill_px,
            "成交价格": fill_px,
            SAME_OUTCOME_PRICE_MOVE_COL: same_move,
        }
    )
    raw["_sort"] = dec["_sort"]
    raw = raw.sort_values("_sort").drop(columns=["_sort"])
    return raw.reset_index(drop=True)


def _append_tracker_style_sheet(writer: pd.ExcelWriter, raw_input: pd.DataFrame) -> None:
    """调用 analyze_polymarket_tracker.compute + format_ws，生成与 v5 一致的累计持仓表。"""
    import analyze_polymarket_tracker as apt

    name = TRACKER_STYLE_SHEET
    if raw_input.empty:
        pd.DataFrame({"说明": ["无已执行成交，无法生成累计持仓明细（与 analyze_polymarket_tracker 一致）。"]}).to_excel(
            writer, sheet_name=name, index=False
        )
        return
    proc, meta = apt.compute(raw_input.copy(), _tracker_compute_args())
    proc.to_excel(writer, sheet_name=name, index=False)
    apt.format_ws(writer.sheets[name], meta.get("marker_col"))


def _finalize_xlsx_workbook(path: Path, *, skip_sheet_titles: frozenset[str] | None = None) -> None:
    """表头加粗并冻结首行；跳过多样式工作表（已由 format_ws 处理）。"""
    from openpyxl import load_workbook
    from openpyxl.styles import Font

    skip = skip_sheet_titles or frozenset()
    wb = load_workbook(path)
    for ws in wb.worksheets:
        if ws.title in skip:
            continue
        if ws.max_row == 0:
            continue
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = Font(bold=True)
    wb.save(path)


def _write_performance_report_xlsx(
    xlsx_path: Path,
    *,
    shown_dir: Path,
    total_cycles: int,
    total_profit: float,
    avg_profit: float,
    win_rate: float,
    max_drawdown: float,
    total_fees: float,
    total_executed: int,
    best_cycle: pd.Series | None,
    worst_cycle: pd.Series | None,
    winner_df: pd.DataFrame,
    net_direction_df: pd.DataFrame,
    direction_df: pd.DataFrame,
    enriched_summary: pd.DataFrame,
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
) -> None:
    overview_rows: list[tuple[str, object]] = [
        ("回放目录", str(shown_dir.as_posix())),
        ("周期数", int(total_cycles)),
        ("总净利润", float(total_profit)),
        ("平均单周期净利润", float(avg_profit)),
        ("胜率", float(win_rate)),
        ("最大回撤", float(max_drawdown)),
        ("总手续费", float(total_fees)),
        ("总执行成交数", int(total_executed)),
    ]
    if best_cycle is not None:
        overview_rows.append(
            (
                "最佳周期",
                f"{best_cycle['cycle_slug']} | 净利润 {float(best_cycle['total_net_profit']):.6f}",
            )
        )
    if worst_cycle is not None:
        overview_rows.append(
            (
                "最差周期",
                f"{worst_cycle['cycle_slug']} | 净利润 {float(worst_cycle['total_net_profit']):.6f}",
            )
        )
    if not winner_df.empty:
        overview_rows.append(
            (
                "周期赢家分布",
                "，".join(f"{row['winner']}={int(row['count'])}" for _, row in winner_df.iterrows()),
            )
        )
    if not net_direction_df.empty:
        overview_rows.append(
            (
                "周期结束净方向分布",
                "，".join(f"{row['net_direction']}={int(row['count'])}" for _, row in net_direction_df.iterrows()),
            )
        )
    if not direction_df.empty:
        overview_rows.append(
            (
                "执行方向分布摘要",
                "，".join(
                    f"{row['selected_outcome']} {row['selected_action']}={int(row['trades'])}笔/{float(row['shares']):.6f}份"
                    for _, row in direction_df.iterrows()
                ),
            )
        )

    overview_df = pd.DataFrame(overview_rows, columns=["项目", "值"])

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    tracker_raw = _batch_replay_decisions_to_tracker_input(cycle_df, decision_df)
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        overview_df.to_excel(writer, sheet_name="概览", index=False)
        enriched_summary.to_excel(writer, sheet_name="分周期累计盈亏", index=False)
        direction_df.to_excel(writer, sheet_name="执行方向分布", index=False)
        winner_df.to_excel(writer, sheet_name="周期赢家分布", index=False)
        net_direction_df.to_excel(writer, sheet_name="周期净方向分布", index=False)
        _append_tracker_style_sheet(writer, tracker_raw)

    _finalize_xlsx_workbook(xlsx_path, skip_sheet_titles=frozenset({TRACKER_STYLE_SHEET}))


def build_performance_report_zh(
    resolved_batch_dir: Path,
    summary_df: pd.DataFrame,
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
    output_path: Path,
    *,
    display_batch_dir: Path | None = None,
) -> Path:
    direction_df = _summarize_direction_distribution(decision_df)
    winner_df = _value_counts_frame(cycle_df.get("winner", pd.Series(dtype=object)), "winner")
    net_direction_df = _value_counts_frame(cycle_df.get("net_direction", pd.Series(dtype=object)), "net_direction")

    summary_df = summary_df.sort_values("cycle_slug").reset_index(drop=True)
    profits = [float(value) for value in summary_df.get("total_net_profit", pd.Series(dtype=float)).tolist()]
    total_cycles = len(summary_df)
    total_profit = float(sum(profits))
    avg_profit = float(summary_df["total_net_profit"].mean()) if total_cycles else 0.0
    win_rate = float((pd.to_numeric(summary_df["total_net_profit"], errors="coerce").fillna(0.0) > 0).mean()) if total_cycles else 0.0
    max_drawdown = _compute_max_drawdown(profits)
    total_fees = float(pd.to_numeric(summary_df.get("total_fees", 0.0), errors="coerce").fillna(0.0).sum()) if total_cycles else 0.0
    total_executed = int(pd.to_numeric(summary_df.get("executed_trades", 0), errors="coerce").fillna(0).sum()) if total_cycles else 0

    best_cycle = summary_df.sort_values("total_net_profit", ascending=False).iloc[0] if total_cycles else None
    worst_cycle = summary_df.sort_values("total_net_profit", ascending=True).iloc[0] if total_cycles else None

    enriched_summary = summary_df.copy()
    if total_cycles:
        enriched_summary["cumulative_profit"] = pd.to_numeric(enriched_summary["total_net_profit"], errors="coerce").fillna(0.0).cumsum()

    report_xlsx = output_path / "batch_replay_performance_report_zh.xlsx"
    shown_dir = display_batch_dir or resolved_batch_dir

    _write_performance_report_xlsx(
        report_xlsx,
        shown_dir=shown_dir,
        total_cycles=total_cycles,
        total_profit=total_profit,
        avg_profit=avg_profit,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
        total_fees=total_fees,
        total_executed=total_executed,
        best_cycle=best_cycle,
        worst_cycle=worst_cycle,
        winner_df=winner_df,
        net_direction_df=net_direction_df,
        direction_df=direction_df,
        enriched_summary=enriched_summary,
        cycle_df=cycle_df,
        decision_df=decision_df,
    )

    return report_xlsx


def _write_batch_trade_process_xlsx(
    *,
    input_dir: Path,
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
    output_path: Path,
) -> Path:
    """交易过程 Excel：元数据、周期快照、决策流水。"""
    xlsx_path = output_path / "batch_replay_trade_process_zh.xlsx"
    meta_df = pd.DataFrame([("数据目录", input_dir.as_posix())], columns=["项", "值"])
    snap_df = cycle_df.copy() if not cycle_df.empty else pd.DataFrame()

    dec = decision_df.copy()
    if not dec.empty:
        if "timestamp" in dec.columns:
            dec["_ts"] = pd.to_datetime(dec["timestamp"], errors="coerce", utc=True)
            dec = dec.sort_values(["cycle_slug", "_ts"], kind="mergesort").drop(columns=["_ts"])
        elif "cycle_slug" in dec.columns:
            dec = dec.sort_values("cycle_slug", kind="mergesort")
        cols = [c for c in TRADE_PROCESS_PREF_COLS if c in dec.columns]
        if cols:
            if "cycle_slug" in dec.columns:
                order = ["cycle_slug"] + cols
                dec_out = dec[[c for c in order if c in dec.columns]]
            else:
                dec_out = dec[cols]
        else:
            dec_out = dec.copy()
    else:
        dec_out = pd.DataFrame(columns=["cycle_slug"])

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        meta_df.to_excel(writer, sheet_name="元数据", index=False)
        snap_df.to_excel(writer, sheet_name="周期快照", index=False)
        dec_out.to_excel(writer, sheet_name="决策流水", index=False)
    _finalize_xlsx_workbook(xlsx_path)
    return xlsx_path


def write_batch_trade_process_zh(
    *,
    input_dir: Path,
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
    output_path: Path,
) -> Path:
    """按周期汇总决策流水，生成 Excel（与绩效报告配套）。"""
    if "cycle_slug" not in cycle_df.columns and not cycle_df.empty:
        raise ValueError("cycle_df 需要包含 cycle_slug 列")
    if "cycle_slug" not in decision_df.columns and not decision_df.empty:
        raise ValueError("decision_df 需要包含 cycle_slug 列")

    return _write_batch_trade_process_xlsx(
        input_dir=input_dir,
        cycle_df=cycle_df,
        decision_df=decision_df,
        output_path=output_path,
    )


def _cleanup_batch_replay_markdown(output_path: Path) -> None:
    """移除历史或并发生成的 Markdown 产物，目录中只保留 Excel。"""
    for name in ("batch_replay_performance_report_zh.md", "batch_replay_trade_process_zh.md"):
        p = output_path / name
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def build_report(batch_dir: str | Path, output_dir: str | Path | None = None) -> Path:
    resolved_batch_dir = _resolve_batch_replay_dir(batch_dir)
    cycle_dirs = _discover_cycle_result_dirs(resolved_batch_dir)
    if not cycle_dirs:
        raise RuntimeError(f"未在目录下找到任何周期回放结果: {resolved_batch_dir}")

    output_path = Path(output_dir) if output_dir else resolved_batch_dir
    output_path.mkdir(parents=True, exist_ok=True)

    summary_df = _load_summary(resolved_batch_dir, cycle_dirs)
    cycle_df, decision_df = _load_cycle_frames(cycle_dirs)
    write_batch_trade_process_zh(input_dir=resolved_batch_dir, cycle_df=cycle_df, decision_df=decision_df, output_path=output_path)
    build_performance_report_zh(resolved_batch_dir, summary_df, cycle_df, decision_df, output_path, display_batch_dir=resolved_batch_dir)
    _cleanup_batch_replay_markdown(output_path)
    return output_path / "batch_replay_performance_report_zh.xlsx"


def main() -> int:
    args = parse_args()
    report_path = build_report(args.input_dir, args.output_dir)
    print(f"已生成中文总绩效报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
