from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


def export_replay_to_excel(
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
    metrics_df: pd.DataFrame,
    output_path: str | Path,
) -> Path:
    output = Path(output_path)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        cycle_df.to_excel(writer, sheet_name="cycles", index=False)
        decision_df.to_excel(writer, sheet_name="decisions", index=False)
        metrics_df.to_excel(writer, sheet_name="metrics", index=False)
    return output


def _format_elapsed(label: pd.Timestamp, cycle_start: pd.Timestamp) -> str:
    if pd.isna(label) or pd.isna(cycle_start):
        return ""
    delta = label - cycle_start
    total_seconds = max(0, int(delta.total_seconds()))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}分{seconds:02d}秒"


def _build_sheet_name(values: Iterable[object]) -> str:
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if text.lower().startswith("btc"):
            return "BTC"
        return text[:31]
    return "Trades"


def export_trade_ledger_to_excel(
    decision_df: pd.DataFrame,
    snapshot_df: pd.DataFrame,
    output_path: str | Path,
) -> Path:
    output = Path(output_path)
    columns = [
        "下注时间距开盘差(分，秒)",
        "市场标题",
        "时间周期",
        "结果代币类型",
        "操作方向",
        "投注份数",
        "USDT价值",
        "成交价格",
        "Up积累份数",
        "Up加权均价",
        "Down积累份数",
        "Down加权均价",
        "当前总持仓份数",
        "净持仓份数",
        "净持仓价值",
        "净持仓方向",
        "Up已成交差价盈亏",
        "Down已成交差价盈亏",
        "已成交数量",
        "未平仓UP盈亏",
        "未平仓Down盈亏",
        "周期净利润",
    ]
    executed = decision_df.copy()
    if "executed" in executed.columns:
        executed = executed[executed["executed"] == True].copy()
    if executed.empty:
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            pd.DataFrame(columns=columns).to_excel(writer, sheet_name="Trades", index=False)
        return output

    snapshots = snapshot_df.copy()
    if "event_index" not in executed.columns:
        executed["event_index"] = range(1, len(executed) + 1)
    if "event_index" not in snapshots.columns:
        snapshots["event_index"] = range(1, len(snapshots) + 1)

    executed["timestamp"] = pd.to_datetime(executed.get("timestamp"), errors="coerce")
    snapshots["timestamp"] = pd.to_datetime(snapshots.get("timestamp"), errors="coerce")
    cycle_starts = snapshots.groupby("cycle_id", dropna=False)["timestamp"].min().rename("cycle_start").reset_index()
    merged = executed.merge(
        snapshots,
        on="event_index",
        how="left",
        suffixes=("_decision", ""),
    ).merge(cycle_starts, on="cycle_id", how="left")

    ledger = pd.DataFrame(
        {
            "下注时间距开盘差(分，秒)": [
                _format_elapsed(ts, start)
                for ts, start in zip(merged["timestamp"], merged["cycle_start"])
            ],
            "市场标题": merged.get("market_id", pd.Series(dtype=object)).fillna(""),
            "时间周期": merged.get("cycle_id", pd.Series(dtype=object)).fillna(""),
            "结果代币类型": merged.get("selected_outcome", pd.Series(dtype=object)).fillna(""),
            "操作方向": merged.get("selected_action", pd.Series(dtype=object)).fillna(""),
            "投注份数": pd.to_numeric(merged.get("selected_shares"), errors="coerce").fillna(0.0),
            "USDT价值": (
                pd.to_numeric(merged.get("selected_shares"), errors="coerce").fillna(0.0)
                * pd.to_numeric(merged.get("fill_price"), errors="coerce").fillna(0.0)
            ),
            "成交价格": pd.to_numeric(merged.get("fill_price"), errors="coerce").fillna(0.0),
            "Up积累份数": pd.to_numeric(merged.get("up_balance"), errors="coerce").fillna(0.0),
            "Up加权均价": pd.to_numeric(merged.get("up_avg_price"), errors="coerce").fillna(0.0),
            "Down积累份数": pd.to_numeric(merged.get("down_balance"), errors="coerce").fillna(0.0),
            "Down加权均价": pd.to_numeric(merged.get("down_avg_price"), errors="coerce").fillna(0.0),
            "当前总持仓份数": (
                pd.to_numeric(merged.get("up_balance"), errors="coerce").fillna(0.0)
                + pd.to_numeric(merged.get("down_balance"), errors="coerce").fillna(0.0)
            ),
            "净持仓份数": pd.to_numeric(merged.get("net_position"), errors="coerce").fillna(0.0),
            "净持仓价值": pd.to_numeric(merged.get("net_position_value"), errors="coerce").fillna(0.0),
            "净持仓方向": merged.get("net_direction", pd.Series(dtype=object)).fillna(""),
            "Up已成交差价盈亏": pd.to_numeric(merged.get("up_realized_pnl"), errors="coerce").fillna(0.0),
            "Down已成交差价盈亏": pd.to_numeric(merged.get("down_realized_pnl"), errors="coerce").fillna(0.0),
            "已成交数量": pd.to_numeric(merged.get("strategy_trades"), errors="coerce").fillna(0.0),
            "未平仓UP盈亏": pd.to_numeric(merged.get("unrealized_up_pnl"), errors="coerce").fillna(0.0),
            "未平仓Down盈亏": pd.to_numeric(merged.get("unrealized_down_pnl"), errors="coerce").fillna(0.0),
            "周期净利润": pd.to_numeric(merged.get("cycle_net_profit"), errors="coerce").fillna(0.0),
        }
    )
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        ledger.to_excel(writer, sheet_name=_build_sheet_name(merged.get("market_id", [])), index=False)
    return output
