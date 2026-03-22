from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from polynet_ai.domain.models import TradeEvent

PRICE_ALIASES = ("成交价格", "价格", "price", "avgprice")
QTY_ALIASES = ("投注份数", "份数", "成交份数", "成交数量", "数量", "shares", "size", "qty", "amount")
OUTCOME_ALIASES = ("结果代币类型", "方向", "结果", "币种方向", "Outcome", "outcome", "token", "token_side")
ACTION_ALIASES = ("操作方向", "操作", "交易类型", "买卖", "买卖方向", "action", "side", "trade_side")
COIN_ALIASES = ("币种", "标的", "资产", "代码", "symbol", "ticker", "coin")
CYCLE_ALIASES = ("时间周期", "市场周期", "周期", "结算周期", "轮次", "场次", "market_cycle", "cycle", "event_start_time", "开始时间")
TIME_ALIASES = ("时间", "下注时间", "timestamp", "time", "成交时间", "下注时间距开盘差(分，秒)")

BUY_KEYWORDS = ("buy", "买", "买入", "开仓", "加仓", "bought", "open")
SELL_KEYWORDS = ("sell", "卖", "卖出", "平仓", "减仓", "close", "closed")
UP_KEYWORDS = ("up", "yes", "涨", "看涨", "做多", "多")
DOWN_KEYWORDS = ("down", "no", "跌", "看跌", "做空", "空")


def normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    for ch in (" ", "_", "-", "/", "\\", "（", "）", "(", ")", "[", "]", ":"):
        text = text.replace(ch, "")
    return text


def find_column(columns: Sequence[str], aliases: Iterable[str], required: bool = True) -> str | None:
    normalized = {normalize_text(col): col for col in columns}
    for alias in aliases:
        key = normalize_text(alias)
        if key in normalized:
            return normalized[key]
    for alias in aliases:
        key = normalize_text(alias)
        for candidate, raw in normalized.items():
            if key and key in candidate:
                return raw
    if required:
        raise ValueError(f"未找到列，候选: {', '.join(aliases)}")
    return None


def parse_outcome(value: object) -> str:
    text = normalize_text(value)
    if any(keyword in text for keyword in UP_KEYWORDS):
        return "up"
    if any(keyword in text for keyword in DOWN_KEYWORDS):
        return "down"
    raise ValueError(f"无法识别方向: {value!r}")


def parse_action(value: object) -> str:
    text = normalize_text(value)
    if any(keyword in text for keyword in BUY_KEYWORDS):
        return "buy"
    if any(keyword in text for keyword in SELL_KEYWORDS):
        return "sell"
    raise ValueError(f"无法识别买卖方向: {value!r}")


def parse_cycle_anchor(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime(2026, 1, 1)


def parse_timestamp(value: object, base: datetime, ordinal: int) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and ":" in value:
        parts = value.replace("（", "(").replace("）", ")").strip()
        segments = [piece for piece in parts.replace("分", ":").replace("秒", "").split(":") if piece]
        if len(segments) >= 2 and all(seg.strip().isdigit() for seg in segments[-2:]):
            minutes = int(segments[-2])
            seconds = int(segments[-1])
            return base + timedelta(minutes=minutes, seconds=seconds)
    return base + timedelta(seconds=ordinal)


def dataframe_to_trade_events(
    df: pd.DataFrame,
    *,
    sheet: str = "BTC",
    ordinal_start: int = 0,
) -> tuple[list[TradeEvent], int]:
    """
    将已对齐列名的 DataFrame 转为 TradeEvent（与 load_excel_events 规则一致）。
    返回 (events, next_ordinal)。
    """
    if df.empty:
        return [], ordinal_start
    price_col = find_column(df.columns, PRICE_ALIASES)
    qty_col = find_column(df.columns, QTY_ALIASES)
    outcome_col = find_column(df.columns, OUTCOME_ALIASES)
    action_col = find_column(df.columns, ACTION_ALIASES, required=False)
    coin_col = find_column(df.columns, COIN_ALIASES, required=False)
    cycle_col = find_column(df.columns, CYCLE_ALIASES)
    time_col = find_column(df.columns, TIME_ALIASES, required=False)

    df = df.copy()
    if time_col and time_col in df.columns:
        df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
    if cycle_col and cycle_col in df.columns:
        df[cycle_col] = pd.to_datetime(df[cycle_col], errors="coerce")

    events: list[TradeEvent] = []
    ordinal = ordinal_start
    for _, row in df.iterrows():
        if cycle_col and pd.isna(row[cycle_col]):
            continue
        cycle_id = str(row[cycle_col])
        base_time = parse_cycle_anchor(row[cycle_col])
        timestamp = parse_timestamp(row[time_col], base_time, ordinal) if time_col else base_time + timedelta(seconds=ordinal)
        events.append(
            TradeEvent(
                market_id=str(row[coin_col]) if coin_col else sheet,
                cycle_id=cycle_id,
                timestamp=timestamp,
                price=float(row[price_col]),
                shares=float(abs(row[qty_col])),
                outcome=parse_outcome(row[outcome_col]),
                action=parse_action(row[action_col]) if action_col else ("buy" if float(row[qty_col]) >= 0 else "sell"),
                metadata={"sheet": sheet, "row_ordinal": ordinal},
            )
        )
        ordinal += 1
    return events, ordinal


def load_csv_events(
    path: str | Path,
    *,
    chunksize: int = 400_000,
    encoding: str = "utf-8-sig",
) -> list[TradeEvent]:
    """
    从 CSV 加载成交事件（支持超大文件分块读，避免 Excel 行数上限与一次性占满内存）。
    """
    path = Path(path)
    events: list[TradeEvent] = []
    ordinal = 0
    reader = pd.read_csv(path, chunksize=chunksize, encoding=encoding, low_memory=False)
    for chunk in reader:
        part, ordinal = dataframe_to_trade_events(chunk, sheet="BTC", ordinal_start=ordinal)
        events.extend(part)
    events.sort(key=lambda item: (item.cycle_id, item.timestamp, item.metadata.get("row_ordinal", 0)))
    return events


def load_excel_events(path: str | Path, sheet_name: str | None = None) -> list[TradeEvent]:
    workbook = pd.ExcelFile(path)
    sheets = [sheet_name] if sheet_name else list(workbook.sheet_names)
    events: list[TradeEvent] = []
    ordinal = 0
    for sheet in sheets:
        df = pd.read_excel(workbook, sheet_name=sheet)
        if df.empty:
            continue
        part, ordinal = dataframe_to_trade_events(df, sheet=sheet, ordinal_start=ordinal)
        events.extend(part)
    events.sort(key=lambda item: (item.cycle_id, item.timestamp, item.metadata.get("row_ordinal", 0)))
    return events
