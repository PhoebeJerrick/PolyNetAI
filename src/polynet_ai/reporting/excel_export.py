from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from polynet_ai.adapters.cycle_window_timing import window_start_naive_utc_from_slug

_REPO_ROOT = Path(__file__).resolve().parents[3]


def absolute_cycle_open_timestamp_utc_for_cycle_id(cycle_id: object) -> pd.Timestamp:
    """Polymarket 时间桶的 cycle_id/slug 对应绝对开盘（UTC）；无法从 slug 解析则 NaT。

    报表「下注时间距开盘差」、尾盘窗口按秒切分等应与 ``window_start_naive_utc_from_slug`` 一致。
    """
    naive = window_start_naive_utc_from_slug(str(cycle_id).strip())
    if naive is None:
        return pd.NaT
    return pd.Timestamp(naive).tz_localize("UTC")

# 与 analyze_polymarket_tracker / 批量绩效报告交易流水表一致，置于「成交价格」右侧
SAME_OUTCOME_PRICE_MOVE_COL = "同向成交价波动幅度(%)"
EXECUTION_LEDGER_SHEET_NAME = "分周期执行交易流水"


def get_version_tag() -> str:
    """返回当前包版本标签，格式如 'V0.1.0'，用于在输出文件名中标注版本。"""
    version_file = _REPO_ROOT / "VERSION"
    if version_file.exists():
        return f"V{version_file.read_text(encoding='utf-8-sig').strip()}"
    return "V0"


def compute_same_outcome_price_move_pct(
    df: pd.DataFrame,
    *,
    outcome_col: str,
    price_col: str,
    group_cols: list[str],
    timestamp_col: str | None = "timestamp",
) -> pd.Series:
    """
    按 group_cols 分组、组内按时间排序，计算每笔相对「上一次同结果代币成交价」的涨跌幅百分比。
    组内某方向第一笔为 NaN。
    """
    if df.empty:
        return pd.Series(dtype=float, index=df.index)
    work = df.reset_index(drop=True)
    orig_index = df.index
    for c in group_cols:
        if c not in work.columns:
            work[c] = ""
    ts_col = timestamp_col if timestamp_col and timestamp_col in work.columns else None
    if ts_col:
        work["_ts_sort"] = pd.to_datetime(work[ts_col], errors="coerce", utc=True)
        sort_keys = [*group_cols, "_ts_sort"]
    elif "event_index" in work.columns:
        work["_ts_sort"] = pd.to_numeric(work["event_index"], errors="coerce")
        sort_keys = [*group_cols, "_ts_sort"]
    else:
        sort_keys = list(group_cols)
    ordered = work.sort_values(sort_keys, kind="mergesort")
    last: dict[tuple[tuple[object, ...], str], float] = {}
    result: list[float] = [float("nan")] * len(work)
    for pos, row in ordered.iterrows():
        gk = tuple(row[c] for c in group_cols)
        raw_o = str(row[outcome_col]).strip().lower()
        ok: str = "down" if "down" in raw_o else "up"
        px = float(pd.to_numeric(row[price_col], errors="coerce") or 0.0)
        key = (gk, ok)
        prev = last.get(key)
        if prev is not None and prev > 1e-12:
            result[int(pos)] = (px - prev) / prev * 100.0
        last[key] = px
    return pd.Series(result, index=orig_index)


def infer_decision_reason(
    df: pd.DataFrame,
    *,
    rule_col: str = "selected_rule",
    action_col: str = "selected_action",
) -> pd.Series:
    """
    根据 `selected_rule` + `selected_action` 推断更可读的中文“决策原因”分类。

    说明：
    - 买入侧：网格/顺势/首单/对冲（以及均值回归的兜底）
    - 卖出侧：止盈/止损/对冲/网格（以及均值回归的兜底）
    """
    if df.empty:
        return pd.Series(dtype=object, index=df.index)

    rule = df.get(rule_col, pd.Series("", index=df.index)).fillna("").astype(str).str.lower()
    action = df.get(action_col, pd.Series("", index=df.index)).fillna("").astype(str).str.lower()

    # action 来自 OrderIntent.action：通常为 "buy"/"sell"（这里容错做中文关键词匹配）
    is_buy = action.str.contains("buy", na=False) | action.str.contains("买", na=False)
    is_sell = action.str.contains("sell", na=False) | action.str.contains("卖", na=False)

    buy_map: dict[str, str] = {
        "opening": "首单买入",
        "trend": "顺势买入",
        "grid": "网格买入",
        "hedge": "对冲买入",
        "mean_reversion": "均值回归买入",
    }
    sell_map: dict[str, str] = {
        "take_profit": "止盈卖出",
        "stop_loss": "止损卖出",
        "grid": "网格卖出",
        "hedge": "对冲卖出",
        "mean_reversion": "均值回归卖出",
    }

    rule_nonempty = rule.ne("")  # only classify when we have a rule name
    reason = pd.Series("", index=df.index, dtype=object)

    buy_mask = is_buy & rule_nonempty
    buy_mapped = rule.loc[buy_mask].map(buy_map)
    reason.loc[buy_mapped.index] = buy_mapped.fillna("")
    buy_unknown_idx = buy_mapped.index[buy_mapped.isna()]
    reason.loc[buy_unknown_idx] = "其他(" + rule.loc[buy_unknown_idx] + ")"

    sell_mask = is_sell & rule_nonempty
    sell_mapped = rule.loc[sell_mask].map(sell_map)
    reason.loc[sell_mapped.index] = sell_mapped.fillna("")
    sell_unknown_idx = sell_mapped.index[sell_mapped.isna()]
    reason.loc[sell_unknown_idx] = "其他(" + rule.loc[sell_unknown_idx] + ")"

    # 兜底：action 未识别但 rule 存在
    other_mask = (~buy_mask) & (~sell_mask) & rule_nonempty
    other_idx = other_mask.index[other_mask]
    if len(other_idx):
        reason.loc[other_idx] = "其他(" + rule.loc[other_idx] + ")"

    return reason


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
    """
    trade_ledger.xlsx 的 sheet 命名：
    - 优先使用 market_id（更贴近测试期望，如 "BTC"）
    - 若无法推断，则退回到中文表名兜底
    """
    try:
        for v in values:
            s = str(v or "").strip()
            if s:
                # Excel sheet 名长度上限 31；这里做简单截断避免异常
                return s[:31]
    except TypeError:
        # values 可能不是可迭代对象
        pass
    return EXECUTION_LEDGER_SHEET_NAME


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
        "决策原因",
        "投注份数",
        "USDT价值",
        "成交价格",
        SAME_OUTCOME_PRICE_MOVE_COL,
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
            pd.DataFrame(columns=columns).to_excel(writer, sheet_name=EXECUTION_LEDGER_SHEET_NAME, index=False)
        return output

    snapshots = snapshot_df.copy()
    if "event_index" not in executed.columns:
        executed["event_index"] = range(1, len(executed) + 1)
    if "event_index" not in snapshots.columns:
        snapshots["event_index"] = range(1, len(snapshots) + 1)

    executed["timestamp"] = pd.to_datetime(executed.get("timestamp"), errors="coerce", utc=True)
    snapshots["timestamp"] = pd.to_datetime(snapshots.get("timestamp"), errors="coerce", utc=True)
    cycle_starts_fb = snapshots.groupby("cycle_id", dropna=False)["timestamp"].min().rename("cycle_start_fb").reset_index()
    cycle_starts_fb["cycle_start"] = cycle_starts_fb["cycle_id"].map(absolute_cycle_open_timestamp_utc_for_cycle_id)
    miss = cycle_starts_fb["cycle_start"].isna() & cycle_starts_fb["cycle_start_fb"].notna()
    cycle_starts_fb.loc[miss, "cycle_start"] = cycle_starts_fb.loc[miss, "cycle_start_fb"]
    cycle_starts = cycle_starts_fb.drop(columns=["cycle_start_fb"])
    merged = executed.merge(
        snapshots,
        on="event_index",
        how="left",
        suffixes=("_decision", ""),
    ).merge(cycle_starts, on="cycle_id", how="left")

    outcome_src = (
        "selected_outcome_decision"
        if "selected_outcome_decision" in merged.columns
        else "selected_outcome"
    )
    ts_for_move = next((c for c in ("timestamp_decision", "timestamp") if c in merged.columns), None)
    move_pct = compute_same_outcome_price_move_pct(
        merged,
        outcome_col=outcome_src,
        price_col="fill_price",
        group_cols=["market_id", "cycle_id"],
        timestamp_col=ts_for_move,
    )

    ts_ledger = "timestamp_decision" if "timestamp_decision" in merged.columns else "timestamp"
    ledger = pd.DataFrame(
        {
            "下注时间距开盘差(分，秒)": [
                _format_elapsed(ts, start)
                for ts, start in zip(merged[ts_ledger], merged["cycle_start"])
            ],
            "市场标题": merged.get("market_id", pd.Series(dtype=object)).fillna(""),
            "时间周期": merged.get("cycle_id", pd.Series(dtype=object)).fillna(""),
            "结果代币类型": merged.get("selected_outcome", pd.Series(dtype=object)).fillna(""),
            "操作方向": merged.get("selected_action", pd.Series(dtype=object)).fillna(""),
            "决策原因": infer_decision_reason(merged),
            "投注份数": pd.to_numeric(merged.get("selected_shares"), errors="coerce").fillna(0.0),
            "USDT价值": (
                pd.to_numeric(merged.get("selected_shares"), errors="coerce").fillna(0.0)
                * pd.to_numeric(merged.get("fill_price"), errors="coerce").fillna(0.0)
            ),
            "成交价格": pd.to_numeric(merged.get("fill_price"), errors="coerce").fillna(0.0),
            SAME_OUTCOME_PRICE_MOVE_COL: move_pct,
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
