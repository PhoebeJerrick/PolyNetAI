from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 分周期执行交易流水（原 BTC 工作表）
TRACKER_STYLE_SHEET = "分周期执行交易流水"

# 与 write_batch_trade_process_zh 决策表列顺序一致
TRADE_PROCESS_PREF_COLS = (
    "timestamp",
    "market_price",
    "market_outcome",
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
    infer_decision_reason,
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
    report_markers = ("batch_replay_summary.csv",)
    report_globs = ("batch_replay_performance_report_zh*.xlsx",)
    has_report_in_path = any((path / name).exists() for name in report_markers) or any(
        next(path.glob(pattern), None) is not None for pattern in report_globs
    )
    has_report_in_nested = any((nested / name).exists() for name in report_markers) or any(
        next(nested.glob(pattern), None) is not None for pattern in report_globs
    )
    if has_report_in_path:
        return path
    if has_report_in_nested:
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


def _infer_cycle_length_seconds(cycle_slug: object) -> int:
    text = str(cycle_slug or "").lower()
    m = re.search(r"-(\d+)m-", text)
    if m:
        return int(m.group(1)) * 60
    return 300


def summarize_tail_window_executions(
    decision_df: pd.DataFrame,
    cycle_df: pd.DataFrame,
    *,
    last_minute_seconds: int = 30,
) -> tuple[pd.DataFrame, list[tuple[str, object]]]:
    """
    按周期汇总「周期末 last_minute_seconds 内」的已执行成交：笔数、现金流净额（不含结算）、手续费。
    周期长度从 cycle_slug 中的 ``-Xm-`` 推断（如 5m -> 300s），否则 300s。
    """
    empty_overview: list[tuple[str, object]] = [
        (
            "尾盘窗口说明",
            f"周期末最后 {last_minute_seconds}s（周期长度从 slug 中 -Xm- 推断，否则 300s）",
        ),
        ("尾盘已执行成交笔数(合计)", 0),
        ("尾盘成交现金流净额(不含结算)", 0.0),
        ("尾盘手续费合计", 0.0),
    ]
    if decision_df.empty or "cycle_slug" not in decision_df.columns:
        return pd.DataFrame(), empty_overview

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

    dec = decision_df.copy()
    if "executed" in dec.columns:
        dec = dec[dec["executed"].fillna(False).astype(bool)]
    if dec.empty:
        return pd.DataFrame(), empty_overview

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
    dec = dec[dec["_cycle_start"].notna() & dec["timestamp"].notna()]
    if dec.empty:
        return pd.DataFrame(), empty_overview

    dec["_cycle_len"] = dec["cycle_slug"].map(_infer_cycle_length_seconds)
    dec["_elapsed_sec"] = (dec["timestamp"] - dec["_cycle_start"]).dt.total_seconds()
    tail_cut = dec["_cycle_len"] - int(last_minute_seconds)
    tail = dec[dec["_elapsed_sec"] >= tail_cut].copy()
    if tail.empty:
        return pd.DataFrame(), empty_overview

    sh = pd.to_numeric(tail["selected_shares"], errors="coerce").fillna(0.0)
    px = pd.to_numeric(tail["fill_price"], errors="coerce").fillna(0.0)
    fee = pd.to_numeric(tail.get("fill_fee", 0.0), errors="coerce").fillna(0.0)
    act = tail["selected_action"].fillna("").astype(str).str.lower()
    cash = pd.Series(0.0, index=tail.index, dtype=float)
    m_buy = act == "buy"
    m_sell = act == "sell"
    cash.loc[m_buy] = -(sh[m_buy] * px[m_buy] + fee[m_buy])
    cash.loc[m_sell] = sh[m_sell] * px[m_sell] - fee[m_sell]
    tail["_cash_flow"] = cash

    per_cycle = (
        tail.groupby("cycle_slug", dropna=False)
        .agg(
            tail_executed_trades=("selected_shares", "size"),
            tail_cash_flow_net=("_cash_flow", "sum"),
            tail_fees=("fill_fee", lambda s: pd.to_numeric(s, errors="coerce").fillna(0.0).sum()),
        )
        .reset_index()
        .sort_values("cycle_slug", kind="mergesort")
        .reset_index(drop=True)
    )

    overview_rows: list[tuple[str, object]] = [
        (
            "尾盘窗口说明",
            f"周期末最后 {last_minute_seconds}s（周期长度从 slug 中 -Xm- 推断，否则 300s）",
        ),
        ("尾盘已执行成交笔数(合计)", int(len(tail))),
        ("尾盘成交现金流净额(不含结算)", float(tail["_cash_flow"].sum())),
        ("尾盘手续费合计", float(pd.to_numeric(tail["fill_fee"], errors="coerce").fillna(0.0).sum())),
    ]
    return per_cycle, overview_rows


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
    decision_reason = infer_decision_reason(dec)

    raw = pd.DataFrame(
        {
            "下注时间距开盘差(分，秒)": [
                _format_elapsed(ts, t0) for ts, t0 in zip(dec["timestamp"], dec["_cycle_start"])
            ],
            "市场标题": dec["market_id"],
            "时间周期": period,
            "结果代币类型": dec.get("selected_outcome", pd.Series(dtype=object)).fillna("").astype(str),
            "操作方向": dec.get("selected_action", pd.Series(dtype=object)).fillna("").astype(str),
            "决策原因": decision_reason,
            "投注份数": shares,
            "USDT价值": shares * fill_px,
            "成交价格": fill_px,
            SAME_OUTCOME_PRICE_MOVE_COL: same_move,
        }
    )
    raw["_sort"] = dec["_sort"]
    raw = raw.sort_values("_sort").drop(columns=["_sort"])
    return raw.reset_index(drop=True)


def _winner_label(value: object) -> str:
    text = str(value or "").strip().lower()
    if text == "up":
        return "Up"
    if text == "down":
        return "Down"
    return ""


def _align_tracker_subtotals_with_cycle_snapshot(
    tracker_df: pd.DataFrame,
    cycle_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    用 cycle_df 的周期结算结果回填 BTC 表的【周期小计】行，避免与周期快照口径不一致。
    """
    if tracker_df.empty or cycle_df.empty:
        return tracker_df
    marker_col = "下注时间距开盘差(分，秒)"
    cycle_col = "时间周期"
    if marker_col not in tracker_df.columns or cycle_col not in tracker_df.columns:
        return tracker_df
    if "cycle_id" not in cycle_df.columns:
        return tracker_df

    out = tracker_df.copy()
    subtotal_mask = out[marker_col].astype(str) == "【周期小计】"
    if not subtotal_mask.any():
        return out

    cycle_lookup = cycle_df.drop_duplicates(subset=["cycle_id"], keep="last").set_index("cycle_id")

    def _get(cycle_id: str, col: str) -> object:
        if col not in cycle_lookup.columns:
            return None
        try:
            return cycle_lookup.at[cycle_id, col]
        except KeyError:
            return None

    for idx in out.index[subtotal_mask]:
        cycle_id = str(out.at[idx, cycle_col])
        if not cycle_id:
            continue
        if "最终Winner方向" in out.columns:
            out.at[idx, "最终Winner方向"] = _winner_label(_get(cycle_id, "winner"))
        if "未平仓UP盈亏" in out.columns:
            value = _get(cycle_id, "unrealized_up_pnl")
            if value is not None:
                out.at[idx, "未平仓UP盈亏"] = value
        if "未平仓Down盈亏" in out.columns:
            value = _get(cycle_id, "unrealized_down_pnl")
            if value is not None:
                out.at[idx, "未平仓Down盈亏"] = value
        if "周期净利润" in out.columns:
            value = _get(cycle_id, "cycle_net_profit")
            if value is not None:
                out.at[idx, "周期净利润"] = value
        if "Up已成交差价盈亏" in out.columns:
            value = _get(cycle_id, "up_realized_pnl")
            if value is not None:
                out.at[idx, "Up已成交差价盈亏"] = value
        if "Down已成交差价盈亏" in out.columns:
            value = _get(cycle_id, "down_realized_pnl")
            if value is not None:
                out.at[idx, "Down已成交差价盈亏"] = value
    return out


def _append_tracker_style_sheet(
    writer: pd.ExcelWriter,
    raw_input: pd.DataFrame,
    cycle_df: pd.DataFrame,
) -> None:
    """调用 analyze_polymarket_tracker.compute + format_ws，生成与 v5 一致的累计持仓表。"""
    import analyze_polymarket_tracker as apt

    name = TRACKER_STYLE_SHEET
    if raw_input.empty:
        pd.DataFrame({"说明": ["无已执行成交，无法生成累计持仓明细（与 analyze_polymarket_tracker 一致）。"]}).to_excel(
            writer, sheet_name=name, index=False
        )
        return
    proc, meta = apt.compute(raw_input.copy(), _tracker_compute_args())
    proc = _align_tracker_subtotals_with_cycle_snapshot(proc, cycle_df)
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
    tail_per_cycle_df: pd.DataFrame | None = None,
    tail_overview_rows: list[tuple[str, object]] | None = None,
    report_source: str = "",
) -> None:
    overview_rows: list[tuple[str, object]] = []
    if report_source:
        overview_rows.append(("数据来源", report_source))
    overview_rows += [
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
    if tail_overview_rows:
        overview_df = pd.concat(
            [overview_df, pd.DataFrame(tail_overview_rows, columns=["项目", "值"])],
            ignore_index=True,
        )

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    tracker_raw = _batch_replay_decisions_to_tracker_input(cycle_df, decision_df)
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        overview_df.to_excel(writer, sheet_name="概览", index=False)
        enriched_summary.to_excel(writer, sheet_name="分周期累计盈亏", index=False)
        direction_df.to_excel(writer, sheet_name="执行方向分布", index=False)
        winner_df.to_excel(writer, sheet_name="周期赢家分布", index=False)
        net_direction_df.to_excel(writer, sheet_name="周期净方向分布", index=False)
        if tail_per_cycle_df is not None and not tail_per_cycle_df.empty:
            tail_per_cycle_df.to_excel(writer, sheet_name="尾盘窗口成交汇总", index=False)
        _append_tracker_style_sheet(writer, tracker_raw, cycle_df)

    _finalize_xlsx_workbook(xlsx_path, skip_sheet_titles=frozenset({TRACKER_STYLE_SHEET}))


def _today_suffix() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def build_performance_report_zh(
    resolved_batch_dir: Path,
    summary_df: pd.DataFrame,
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
    output_path: Path,
    *,
    display_batch_dir: Path | None = None,
    cycle_range: str = "",
    cycle_count: int = 0,
    report_source: str = "",
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
        if {"account_cash", "total_net_profit"}.issubset(enriched_summary.columns):
            account_cash = pd.to_numeric(enriched_summary["account_cash"], errors="coerce")
            total_net_profit_col = pd.to_numeric(enriched_summary["total_net_profit"], errors="coerce").fillna(0.0)
            implied_start_cash = account_cash - total_net_profit_col
            enriched_summary["implied_start_cash"] = implied_start_cash
            first_start_cash = implied_start_cash.dropna().iloc[0] if implied_start_cash.notna().any() else 0.0
            enriched_summary["estimated_cash_from_cum"] = first_start_cash + enriched_summary["cumulative_profit"]

    suffix = _today_suffix()
    if cycle_count > 0:
        report_xlsx = output_path / f"batch_replay_performance_report_zh_{cycle_count}_{suffix}.xlsx"
    else:
        report_xlsx = output_path / f"batch_replay_performance_report_zh_{suffix}.xlsx"
    shown_dir = display_batch_dir or resolved_batch_dir

    tail_per_cycle, tail_overview = summarize_tail_window_executions(
        decision_df, cycle_df, last_minute_seconds=30
    )

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
        tail_per_cycle_df=tail_per_cycle,
        tail_overview_rows=tail_overview,
        report_source=report_source,
    )

    return report_xlsx


def _find_latest_report(report_dir: Path) -> Path | None:
    """在目录中查找最新的绩效报告 xlsx 文件。"""
    candidates = sorted(
        report_dir.glob("batch_replay_performance_report_zh_*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def build_comparison_report_zh(
    sim_report_path: Path,
    live_report_path: Path,
    output_path: Path,
) -> Path:
    """生成「模拟下单测试 vs 实盘行情验证」对比报告。

    从两份绩效报告中提取概览数据和分周期数据，生成一份对比 Excel。
    """
    output_path.mkdir(parents=True, exist_ok=True)

    # --- 读取两份报告的概览 sheet ---
    sim_overview = pd.read_excel(sim_report_path, sheet_name="概览")
    live_overview = pd.read_excel(live_report_path, sheet_name="概览")

    # 构建并排对比行
    comparison_rows: list[dict[str, object]] = []
    sim_map: dict[str, object] = dict(zip(sim_overview["项目"], sim_overview["值"]))
    live_map: dict[str, object] = dict(zip(live_overview["项目"], live_overview["值"]))
    all_keys = list(dict.fromkeys(list(sim_map.keys()) + list(live_map.keys())))
    # 过滤掉纯路径 / 来源标识行
    skip_keys = {"回放目录", "数据来源"}
    for key in all_keys:
        if key in skip_keys:
            continue
        sim_val = sim_map.get(key, "—")
        live_val = live_map.get(key, "—")
        diff = ""
        try:
            sv = float(sim_val)  # type: ignore[arg-type]
            lv = float(live_val)  # type: ignore[arg-type]
            diff_val = lv - sv
            diff = f"{diff_val:+.6f}"
        except (ValueError, TypeError):
            pass
        comparison_rows.append({
            "指标": key,
            "模拟下单": sim_val,
            "实盘验证": live_val,
            "差异 (实盘-模拟)": diff,
        })

    comparison_df = pd.DataFrame(comparison_rows)

    # --- 元信息 ---
    meta_rows = [
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("模拟报告", str(sim_report_path.name)),
        ("实盘报告", str(live_report_path.name)),
    ]
    meta_df = pd.DataFrame(meta_rows, columns=["项目", "值"])

    # --- 读取分周期数据 ---
    try:
        sim_cycles = pd.read_excel(sim_report_path, sheet_name="分周期累计盈亏")
    except Exception:
        sim_cycles = pd.DataFrame()
    try:
        live_cycles = pd.read_excel(live_report_path, sheet_name="分周期累计盈亏")
    except Exception:
        live_cycles = pd.DataFrame()

    # --- 写出 ---
    report_name = f"sim_vs_live_comparison_zh_{_today_suffix()}.xlsx"
    report_xlsx = output_path / report_name
    with pd.ExcelWriter(report_xlsx, engine="openpyxl") as writer:
        meta_df.to_excel(writer, sheet_name="报告信息", index=False)
        comparison_df.to_excel(writer, sheet_name="对比概览", index=False)
        if not sim_cycles.empty:
            sim_cycles.to_excel(writer, sheet_name="模拟下单_分周期", index=False)
        if not live_cycles.empty:
            live_cycles.to_excel(writer, sheet_name="实盘验证_分周期", index=False)
    _finalize_xlsx_workbook(report_xlsx)
    return report_xlsx


def _write_batch_trade_process_xlsx(
    *,
    input_dir: Path,
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
    output_path: Path,
) -> Path:
    """交易过程 Excel：元数据、周期快照、决策流水。"""
    xlsx_path = output_path / f"batch_replay_trade_process_zh_{_today_suffix()}.xlsx"
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

    # Clarify "market direction" (event outcome) vs "strategy direction" (selected outcome).
    dec_out = dec_out.rename(
        columns={
            "market_outcome": "市场方向",
            "selected_outcome": "策略方向",
            "selected_action": "策略动作",
        }
    )

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
    report_path = build_performance_report_zh(
        resolved_batch_dir,
        summary_df,
        cycle_df,
        decision_df,
        output_path,
        display_batch_dir=resolved_batch_dir,
    )
    _cleanup_batch_replay_markdown(output_path)
    return report_path


def main() -> int:
    args = parse_args()
    report_path = build_report(args.input_dir, args.output_dir)
    print(f"已生成中文总绩效报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
