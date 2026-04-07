from __future__ import annotations

import ast
import json
import math
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from universal_stats_bot.io import (
    CycleTradeEventRecorder,
    load_recorded_trade_events,
    trade_event_from_record,
)
from universal_stats_bot.models import TradeEvent
from universal_stats_bot.time_window import (
    cycle_seconds_from_market_slug,
    window_start_naive_utc_from_slug,
)


DEFAULT_STATS_INPUT_DIR = "../Input/5m-RawData"
DEFAULT_STATS_OUTPUT_PATH = Path("artifacts/stats/universal_stats.csv")
SUPPORTED_WINDOW_MODES = {
    "full_cycle",
    "relative_start",
    "relative_end",
    "absolute_time",
    "percentage",
}
SUPPORTED_OUTPUT_FORMATS = {"table", "csv", "json"}
SUPPORTED_CORRELATION_METHODS = {"pearson", "spearman"}
SUPPORTED_WIN_RULES = {"last_price", "threshold", "external_file", "script"}
_DURATION_TOKEN_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)([smhd])", re.IGNORECASE)
_SAFE_SCALAR_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "int": int,
    "float": float,
    "bool": bool,
    "str": str,
    "len": len,
}


@dataclass(slots=True)
class ConditionSpec:
    filter_expr: str
    group_expr: str
    label: str = ""


@dataclass(slots=True)
class UniversalStatsConfig:
    input_dir: Path = Path(DEFAULT_STATS_INPUT_DIR)
    global_start_time: datetime | None = None
    global_end_time: datetime | None = None
    time_window_mode: str = "full_cycle"
    window_start_sec: float | None = None
    window_end_sec: float | None = None
    window_start_offset: str | None = None
    window_end_offset: str | None = None
    window_start_pct: float | None = None
    window_end_pct: float | None = None
    skip_empty_window: bool = True
    win_rule: str = "last_price"
    win_threshold: float = 0.5
    win_direction: str = "up"
    external_result_path: Path | None = None
    win_script: str | None = None
    correlation_var: str | None = None
    correlation_method: str = "pearson"
    conditions: list[ConditionSpec] = field(default_factory=list)
    open_price_rule: str = "first_trade"
    open_price_in_window: bool = False
    open_price_condition: str | None = None
    output_format: str = "csv"
    output_path: Path = field(default_factory=lambda: DEFAULT_STATS_OUTPUT_PATH)
    output_columns: list[str] = field(default_factory=list)
    custom_metrics: dict[str, str] = field(default_factory=dict)
    event_file_name: str = "ws_trade_events.ndjson"


@dataclass(slots=True)
class UniversalStatsResult:
    cycle_df: pd.DataFrame
    condition_df: pd.DataFrame
    summary: dict[str, Any]
    output_path: Path | None
    summary_path: Path | None
    warning_count: int
    warning_samples: list[str]


@dataclass(slots=True)
class WarningCollector:
    max_samples: int = 20
    count: int = 0
    samples: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        self.count += 1
        if len(self.samples) < self.max_samples:
            self.samples.append(message)


@dataclass(slots=True)
class CycleWindow:
    cycle_start: datetime
    cycle_end: datetime
    window_start: datetime
    window_end: datetime

    @property
    def cycle_duration_seconds(self) -> float:
        return max(0.0, (self.cycle_end - self.cycle_start).total_seconds())

    @property
    def window_duration_seconds(self) -> float:
        return max(0.0, (self.window_end - self.window_start).total_seconds())


def load_stats_config(path: str | Path) -> UniversalStatsConfig:
    config_path = Path(path)
    raw = config_path.read_text(encoding="utf-8")
    data = _parse_stats_config_text(raw)
    return _normalize_stats_config(data)


def run_universal_stats(config: UniversalStatsConfig) -> UniversalStatsResult:
    warnings = WarningCollector()
    input_dir = Path(config.input_dir)
    if not input_dir.exists():
        raise FileNotFoundError(f"统计输入目录不存在: {input_dir}")

    rows: list[dict[str, Any]] = []
    external_winners = _load_external_winners(config.external_result_path) if config.external_result_path else {}

    for events in _iter_cycle_events(input_dir, config, warnings):
        if not events:
            continue
        row = _build_cycle_row(events, config, external_winners, warnings)
        if row is None:
            continue
        rows.append(row)

    cycle_df = pd.DataFrame(rows)
    if not cycle_df.empty:
        cycle_df = cycle_df.sort_values(["cycle_start", "cycle_id"], kind="stable").reset_index(drop=True)

    condition_df = _build_condition_summary(cycle_df, config.conditions)
    summary = _build_summary(cycle_df, condition_df, config)
    output_path, summary_path = _write_outputs(cycle_df, condition_df, summary, config)
    return UniversalStatsResult(
        cycle_df=cycle_df,
        condition_df=condition_df,
        summary=summary,
        output_path=output_path,
        summary_path=summary_path,
        warning_count=warnings.count,
        warning_samples=list(warnings.samples),
    )


def render_console_report(result: UniversalStatsResult, config: UniversalStatsConfig) -> str:
    lines = [
        "万能统计机器人",
        f"- 输入目录: {config.input_dir}",
        f"- 周期数: {len(result.cycle_df)}",
        f"- 子区间: {_describe_window_config(config)}",
    ]
    if result.summary.get("correlation"):
        corr = result.summary["correlation"]
        p_value = corr.get("p_value")
        if p_value is None or (isinstance(p_value, float) and math.isnan(p_value)):
            p_text = "N/A"
        else:
            p_text = f"{float(p_value):.6f}"
        lines.append(
            "- 相关性: "
            f"{corr['method']}({corr['variable']}, winner)={corr['value']:.6f}, p-value={p_text}"
        )
    if result.summary.get("open_price_condition"):
        item = result.summary["open_price_condition"]
        lines.append(
            "- 开盘条件: "
            f"{item['condition']} | matched={item['matched_cycles']} | up_win_ratio={item['up_win_ratio']:.4f}"
        )
    if not result.condition_df.empty:
        lines.append("- 条件占比:")
        for row in result.condition_df.to_dict(orient="records"):
            lines.append(
                "  "
                f"{row['label']} | matched={row['matched_cycles']} | group={row['group_cycles']} | ratio={row['ratio']:.4f}"
            )
    if result.output_path is not None:
        lines.append(f"- 输出: {result.output_path}")
    if result.summary_path is not None:
        lines.append(f"- 汇总: {result.summary_path}")
    if result.warning_count:
        lines.append(f"- 警告: {result.warning_count} 条")
        for sample in result.warning_samples[:5]:
            lines.append(f"  {sample}")
    if config.output_format == "table" and not result.cycle_df.empty:
        table_df = _select_output_columns(result.cycle_df, config.output_columns)
        lines.append("")
        lines.append(table_df.to_string(index=False))
    return "\n".join(lines)


def evaluate_scalar_expression(expression: str, context: dict[str, Any]) -> Any:
    normalized = _normalize_expression(expression)
    tree = ast.parse(normalized, mode="eval")
    return _eval_ast(tree.body, context, trade_contexts=None, scalar_context=context)


def evaluate_trade_metric_expression(expression: str, trade_contexts: list[dict[str, Any]], scalar_context: dict[str, Any]) -> Any:
    base_expression, filter_expression = _split_filter_expression(expression)
    filtered_contexts = trade_contexts
    if filter_expression:
        filtered_contexts = [
            item
            for item in trade_contexts
            if _truthy(evaluate_scalar_expression(filter_expression, {**scalar_context, **item}))
        ]
    normalized = _normalize_expression(base_expression)
    tree = ast.parse(normalized, mode="eval")
    return _eval_ast(tree.body, scalar_context, trade_contexts=filtered_contexts, scalar_context=scalar_context)


def _parse_stats_config_text(text: str) -> dict[str, Any]:
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("{"):
        payload = json.loads(stripped)
        if not isinstance(payload, dict):
            raise ValueError("统计配置 JSON 必须是对象")
        return payload
    data: dict[str, Any] = {}
    current_key: str | None = None
    current_value_lines: list[str] = []
    for raw_line in text.splitlines():
        line = _strip_inline_comment(raw_line).strip()
        if not line:
            continue
        if current_key is None:
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            current_key = key.strip()
            current_value_lines = [value.strip()]
        else:
            current_value_lines.append(line)
        joined = "\n".join(current_value_lines).strip()
        if _is_balanced_literal(joined):
            data[current_key] = _parse_config_value(joined)
            current_key = None
            current_value_lines = []
    if current_key is not None:
        data[current_key] = _parse_config_value("\n".join(current_value_lines))
    return data


def _strip_inline_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    chars: list[str] = []
    for char in line:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            chars.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            chars.append(char)
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            chars.append(char)
            continue
        if char == "#" and not in_single and not in_double:
            break
        chars.append(char)
    return "".join(chars)


def _is_balanced_literal(text: str) -> bool:
    balance = 0
    in_single = False
    in_double = False
    escaped = False
    for char in text:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if char in "[{(":
            balance += 1
        elif char in "]})":
            balance -= 1
    return balance <= 0 and not in_single and not in_double


def _parse_config_value(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml
    except ModuleNotFoundError:
        yaml = None
    if yaml is not None:
        parsed = yaml.safe_load(text)
        if parsed is not None:
            return parsed
    return text


def _normalize_stats_config(data: dict[str, Any]) -> UniversalStatsConfig:
    time_window_mode = str(data.get("time_window_mode") or "full_cycle").strip().lower()
    if time_window_mode not in SUPPORTED_WINDOW_MODES:
        raise ValueError(f"不支持的 time_window_mode: {time_window_mode}")
    win_rule = str(data.get("win_rule") or "last_price").strip().lower()
    if win_rule not in SUPPORTED_WIN_RULES:
        raise ValueError(f"不支持的 win_rule: {win_rule}")
    output_format = str(data.get("output_format") or "csv").strip().lower()
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(f"不支持的 output_format: {output_format}")
    correlation_method = str(data.get("correlation_method") or "pearson").strip().lower()
    if correlation_method not in SUPPORTED_CORRELATION_METHODS:
        raise ValueError(f"不支持的 correlation_method: {correlation_method}")
    raw_conditions = data.get("conditions") or []
    if isinstance(raw_conditions, dict):
        raw_conditions = [raw_conditions]
    conditions: list[ConditionSpec] = []
    for item in raw_conditions:
        if not isinstance(item, dict):
            continue
        filter_expr = str(item.get("filter") or "").strip()
        group_expr = str(item.get("group") or "").strip()
        if not filter_expr or not group_expr:
            continue
        conditions.append(ConditionSpec(filter_expr=filter_expr, group_expr=group_expr, label=str(item.get("label") or "").strip()))
    output_columns = _to_string_list(data.get("output_columns"))
    custom_metrics = data.get("custom_metrics") or {}
    if not isinstance(custom_metrics, dict):
        raise ValueError("custom_metrics 必须是键值映射")
    output_path_value = data.get("output_path")
    if output_path_value:
        output_path = Path(str(output_path_value))
    elif output_format == "json":
        output_path = DEFAULT_STATS_OUTPUT_PATH.with_suffix(".json")
    else:
        output_path = DEFAULT_STATS_OUTPUT_PATH
    return UniversalStatsConfig(
        input_dir=Path(str(data.get("input_dir") or data.get("data_stream_dir") or DEFAULT_STATS_INPUT_DIR)),
        global_start_time=_parse_datetime(data.get("global_start_time")),
        global_end_time=_parse_datetime(data.get("global_end_time")),
        time_window_mode=time_window_mode,
        window_start_sec=_optional_float(data.get("window_start_sec")),
        window_end_sec=_optional_float(data.get("window_end_sec")),
        window_start_offset=_optional_string(data.get("window_start_offset")),
        window_end_offset=_optional_string(data.get("window_end_offset")),
        window_start_pct=_optional_float(data.get("window_start_pct")),
        window_end_pct=_optional_float(data.get("window_end_pct")),
        skip_empty_window=_to_bool(data.get("skip_empty_window"), default=True),
        win_rule=win_rule,
        win_threshold=float(data.get("win_threshold") or 0.5),
        win_direction=_normalize_outcome(str(data.get("win_direction") or "up")),
        external_result_path=Path(str(data["external_result_path"])) if data.get("external_result_path") else None,
        win_script=_optional_string(data.get("win_script") or data.get("script")),
        correlation_var=_optional_string(data.get("correlation_var")),
        correlation_method=correlation_method,
        conditions=conditions,
        open_price_rule=str(data.get("open_price_rule") or "first_trade").strip().lower(),
        open_price_in_window=_to_bool(data.get("open_price_in_window"), default=False),
        open_price_condition=_optional_string(data.get("open_price_condition")),
        output_format=output_format,
        output_path=output_path,
        output_columns=output_columns,
        custom_metrics={str(key): str(value) for key, value in custom_metrics.items()},
        event_file_name=str(data.get("event_file_name") or "ws_trade_events.ndjson").strip() or "ws_trade_events.ndjson",
    )


def _iter_cycle_events(input_dir: Path, config: UniversalStatsConfig, warnings: WarningCollector) -> Iterable[list[TradeEvent]]:
    per_cycle_files = sorted(path for path in input_dir.rglob(config.event_file_name) if path.is_file())
    if per_cycle_files:
        for cycle_file in per_cycle_files:
            events = load_recorded_trade_events(cycle_file)
            if not events:
                warnings.warn(f"空周期文件，已跳过: {cycle_file}")
                continue
            yield events
        return
    source_files = sorted(
        path
        for path in input_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl", ".ndjson"}
    )
    if not source_files:
        raise FileNotFoundError(f"未在目录下找到任何 JSON/JSONL 事件文件: {input_dir}")
    with tempfile.TemporaryDirectory(prefix="universal_stats_bot_") as temp_dir:
        spool_dir = Path(temp_dir)
        recorder = CycleTradeEventRecorder(spool_dir, event_file_name=config.event_file_name)
        try:
            for source_file in source_files:
                _spool_source_file(source_file, recorder, config, warnings)
        finally:
            recorder.close()
        for cycle_file in sorted(path for path in spool_dir.rglob(config.event_file_name) if path.is_file()):
            events = load_recorded_trade_events(cycle_file)
            if not events:
                warnings.warn(f"空周期文件，已跳过: {cycle_file}")
                continue
            yield events


def _spool_source_file(source_file: Path, recorder: CycleTradeEventRecorder, config: UniversalStatsConfig, warnings: WarningCollector) -> None:
    try:
        with source_file.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    warnings.warn(f"JSON 解析失败，已跳过: {source_file}:{line_no} ({exc})")
                    continue
                if not isinstance(payload, dict):
                    warnings.warn(f"非对象记录，已跳过: {source_file}:{line_no}")
                    continue
                try:
                    event = trade_event_from_record(payload)
                except Exception as exc:
                    warnings.warn(f"事件字段缺失或无效，已跳过: {source_file}:{line_no} ({exc})")
                    continue
                if not event.cycle_id:
                    warnings.warn(f"缺少 cycle_id，已跳过: {source_file}:{line_no}")
                    continue
                if config.global_start_time and event.timestamp < config.global_start_time:
                    continue
                if config.global_end_time and event.timestamp > config.global_end_time:
                    continue
                recorder.record(event)
    except OSError as exc:
        warnings.warn(f"读取文件失败，已跳过: {source_file} ({exc})")


def _build_cycle_row(
    events: list[TradeEvent],
    config: UniversalStatsConfig,
    external_winners: dict[str, str],
    warnings: WarningCollector,
) -> dict[str, Any] | None:
    events = sorted(events, key=lambda event: (event.timestamp, event.market_id, event.outcome, event.action))
    cycle_id = str(events[0].cycle_id)
    market_id = str(events[0].market_id)
    window = _resolve_cycle_window(cycle_id, events, config)
    if window is None:
        warnings.warn(f"周期窗口无效，已跳过: {cycle_id}")
        return None

    window_events = [event for event in events if window.window_start <= event.timestamp <= window.window_end]
    if not window_events and config.skip_empty_window:
        warnings.warn(f"子区间无数据，已跳过: {cycle_id}")
        return None

    trade_contexts = _build_trade_contexts(events, window)
    row: dict[str, Any] = {
        "cycle_id": cycle_id,
        "market_id": market_id,
        "cycle_start": pd.Timestamp(window.cycle_start),
        "cycle_end": pd.Timestamp(window.cycle_end),
        "cycle_duration_seconds": window.cycle_duration_seconds,
        "window_start": pd.Timestamp(window.window_start),
        "window_end": pd.Timestamp(window.window_end),
        "window_duration_seconds": window.window_duration_seconds,
        "window_trade_count": len(window_events),
        "cycle_trade_count": len(events),
    }

    row.update(_compute_side_metrics(window_events, outcome="up", prefix="up"))
    row.update(_compute_side_metrics(window_events, outcome="down", prefix="down"))
    row["total_volume"] = float(row.get("up_volume") or 0.0) + float(row.get("down_volume") or 0.0)
    row["window_total_volume"] = row["total_volume"]
    row["delta_avg_price"] = _delta_values(row.get("up_avg_price"), row.get("down_avg_price"))
    row["window_delta_avg_price"] = row["delta_avg_price"]
    row["window_up_volume"] = row.get("up_volume")
    row["window_down_volume"] = row.get("down_volume")
    row["window_up_trade_count"] = row.get("up_trade_count")
    row["window_down_trade_count"] = row.get("down_trade_count")
    row["window_up_avg_price"] = row.get("up_avg_price")
    row["window_down_avg_price"] = row.get("down_avg_price")
    row["window_up_vwap"] = row.get("up_vwap")
    row["window_down_vwap"] = row.get("down_vwap")

    open_source_events = window_events if config.open_price_in_window else events
    row.update(_compute_open_prices(open_source_events))

    for metric_name, expression in config.custom_metrics.items():
        try:
            row[metric_name] = evaluate_trade_metric_expression(expression, trade_contexts, row)
        except Exception as exc:
            warnings.warn(f"自定义指标 {metric_name} 计算失败，已置空: {cycle_id} ({exc})")
            row[metric_name] = math.nan

    try:
        winner = _resolve_winner(events, row, config, external_winners)
    except Exception as exc:
        warnings.warn(f"winner 解析失败，已跳过: {cycle_id} ({exc})")
        return None

    row["winner"] = winner
    row["outcome"] = winner
    row["winner_value"] = 1.0 if winner == "up" else 0.0
    row["winner_is_up"] = winner == "up"
    row["winner_is_down"] = winner == "down"
    row["outcome_win"] = winner in {"up", "down"}
    row["last_full_cycle_price"] = float(events[-1].price)
    row["last_full_cycle_outcome"] = _normalize_outcome(events[-1].outcome)
    return row


def _resolve_cycle_window(
    cycle_id: str,
    events: list[TradeEvent],
    config: UniversalStatsConfig,
) -> CycleWindow | None:
    first_timestamp = min(event.timestamp for event in events)
    last_timestamp = max(event.timestamp for event in events)
    parsed_start = window_start_naive_utc_from_slug(cycle_id)
    cycle_seconds = cycle_seconds_from_market_slug(cycle_id) or max(
        1.0,
        (last_timestamp - first_timestamp).total_seconds(),
    )
    cycle_start = parsed_start or first_timestamp
    nominal_end = cycle_start + timedelta(seconds=cycle_seconds)
    cycle_end = max(last_timestamp, nominal_end)

    if config.time_window_mode == "full_cycle":
        return CycleWindow(cycle_start=cycle_start, cycle_end=cycle_end, window_start=cycle_start, window_end=cycle_end)

    if config.time_window_mode == "relative_start":
        start_sec = float(config.window_start_sec or 0.0)
        end_sec = float(config.window_end_sec if config.window_end_sec is not None else cycle_seconds)
        window_start = cycle_start + timedelta(seconds=start_sec)
        window_end = cycle_start + timedelta(seconds=end_sec)
    elif config.time_window_mode == "relative_end":
        start_sec = float(config.window_start_sec or -cycle_seconds)
        end_sec = float(config.window_end_sec or 0.0)
        window_start = cycle_end + timedelta(seconds=start_sec)
        window_end = cycle_end + timedelta(seconds=end_sec)
    elif config.time_window_mode == "absolute_time":
        start_offset = _parse_duration(config.window_start_offset or "0s")
        end_offset = _parse_duration(config.window_end_offset or f"{int(cycle_seconds)}s")
        window_start = _offset_from_cycle_bounds(cycle_start, cycle_end, start_offset)
        window_end = _offset_from_cycle_bounds(cycle_start, cycle_end, end_offset)
    elif config.time_window_mode == "percentage":
        start_pct = float(config.window_start_pct or 0.0)
        end_pct = float(config.window_end_pct if config.window_end_pct is not None else 1.0)
        window_start = cycle_start + timedelta(seconds=cycle_seconds * start_pct)
        window_end = cycle_start + timedelta(seconds=cycle_seconds * end_pct)
    else:
        return None

    window_start = max(window_start, cycle_start)
    window_end = min(window_end, cycle_end)
    if window_start > window_end:
        return None
    return CycleWindow(cycle_start=cycle_start, cycle_end=cycle_end, window_start=window_start, window_end=window_end)


def _build_trade_contexts(events: list[TradeEvent], window: CycleWindow) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        elapsed_seconds = (event.timestamp - window.cycle_start).total_seconds()
        seconds_to_end = (window.cycle_end - event.timestamp).total_seconds()
        contexts.append(
            {
                "event": event,
                "market_id": event.market_id,
                "cycle_id": event.cycle_id,
                "timestamp": pd.Timestamp(event.timestamp),
                "price": float(event.price),
                "shares": float(event.shares),
                "abs_shares": abs(float(event.shares)),
                "signed_shares": float(event.signed_shares),
                "outcome": _normalize_outcome(event.outcome),
                "action": str(event.action).strip().lower(),
                "time_in_window": window.window_start <= event.timestamp <= window.window_end,
                "elapsed_seconds": elapsed_seconds,
                "seconds_to_end": seconds_to_end,
                "cycle_duration_seconds": window.cycle_duration_seconds,
                "event_index": index,
            }
        )
    return contexts


def _compute_side_metrics(events: Iterable[TradeEvent], *, outcome: str, prefix: str) -> dict[str, Any]:
    selected = [event for event in events if _normalize_outcome(event.outcome) == outcome]
    volume = float(sum(abs(float(event.shares)) for event in selected))
    trade_count = len(selected)
    weighted_sum = float(sum(float(event.price) * abs(float(event.shares)) for event in selected))
    avg_price = weighted_sum / volume if volume > 0 else math.nan
    return {
        f"{prefix}_volume": volume,
        f"{prefix}_trade_count": trade_count,
        f"{prefix}_avg_price": avg_price,
        f"{prefix}_vwap": avg_price,
    }


def _compute_open_prices(events: list[TradeEvent]) -> dict[str, Any]:
    up_open = next((float(event.price) for event in events if _normalize_outcome(event.outcome) == "up"), math.nan)
    down_open = next((float(event.price) for event in events if _normalize_outcome(event.outcome) == "down"), math.nan)
    open_price = float(events[0].price) if events else math.nan
    return {
        "open_price": open_price,
        "open_price_up": up_open,
        "open_price_down": down_open,
        "up_open": up_open,
        "down_open": down_open,
    }


def _resolve_winner(
    events: list[TradeEvent],
    row: dict[str, Any],
    config: UniversalStatsConfig,
    external_winners: dict[str, str],
) -> str:
    cycle_id = str(events[0].cycle_id)
    if config.win_rule == "external_file":
        winner = external_winners.get(cycle_id)
        if winner not in {"up", "down"}:
            raise ValueError(f"external_file 未找到 winner: {cycle_id}")
        return winner
    if config.win_rule == "threshold":
        target_outcome = config.win_direction
        target_events = [event for event in events if _normalize_outcome(event.outcome) == target_outcome]
        target_event = target_events[-1] if target_events else events[-1]
        return _winner_from_trade(target_event, threshold=config.win_threshold, target_outcome=target_outcome)
    if config.win_rule == "script":
        if not config.win_script:
            raise ValueError("win_rule=script 时必须提供 win_script")
        return _run_winner_script(config.win_script, events, row)
    return _winner_from_trade(events[-1], threshold=config.win_threshold)


def _winner_from_trade(event: TradeEvent, *, threshold: float, target_outcome: str | None = None) -> str:
    outcome = _normalize_outcome(target_outcome or event.outcome)
    price = float(event.price)
    if outcome == "up":
        return "up" if price > threshold else "down"
    return "down" if price > threshold else "up"


def _run_winner_script(script_value: str, events: list[TradeEvent], row: dict[str, Any]) -> str:
    script_path = Path(script_value)
    context = {
        "events": events,
        "row": row,
        "cycle_id": str(events[0].cycle_id),
        "market_id": str(events[0].market_id),
    }
    if script_path.exists() and script_path.suffix.lower() == ".py":
        namespace: dict[str, Any] = {}
        exec(script_path.read_text(encoding="utf-8"), {"__builtins__": {}}, namespace)
        decide = namespace.get("decide_winner")
        if not callable(decide):
            raise ValueError(f"脚本缺少 decide_winner(context) 函数: {script_path}")
        winner = decide(context)
    else:
        winner = evaluate_scalar_expression(script_value, context)
    normalized = _normalize_outcome(str(winner))
    if normalized not in {"up", "down"}:
        raise ValueError(f"脚本返回非法 winner: {winner}")
    return normalized


def _load_external_winners(path: Path) -> dict[str, str]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {str(key): _normalize_outcome(str(value)) for key, value in payload.items()}
        raise ValueError("external_result_path JSON 必须是 cycle_id -> winner 的对象")

    df = pd.read_csv(path)
    if "cycle_id" not in df.columns or "winner" not in df.columns:
        raise ValueError("external_result_path CSV 必须包含 cycle_id,winner 两列")
    return {
        str(row["cycle_id"]): _normalize_outcome(str(row["winner"]))
        for _, row in df[["cycle_id", "winner"]].dropna().iterrows()
    }


def _build_condition_summary(cycle_df: pd.DataFrame, conditions: list[ConditionSpec]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if cycle_df.empty:
        return pd.DataFrame(columns=["label", "filter", "group", "matched_cycles", "group_cycles", "ratio"])
    for condition in conditions:
        matched_cycles = 0
        group_cycles = 0
        for record in cycle_df.to_dict(orient="records"):
            context = _expression_context(record)
            if _truthy(evaluate_scalar_expression(condition.filter_expr, context)):
                matched_cycles += 1
                if _truthy(evaluate_scalar_expression(condition.group_expr, context)):
                    group_cycles += 1
        ratio = float(group_cycles / matched_cycles) if matched_cycles else math.nan
        rows.append(
            {
                "label": condition.label or f"{condition.filter_expr} => {condition.group_expr}",
                "filter": condition.filter_expr,
                "group": condition.group_expr,
                "matched_cycles": matched_cycles,
                "group_cycles": group_cycles,
                "ratio": ratio,
            }
        )
    return pd.DataFrame(rows)


def _build_summary(
    cycle_df: pd.DataFrame,
    condition_df: pd.DataFrame,
    config: UniversalStatsConfig,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "cycle_count": int(len(cycle_df)),
        "window": _describe_window_config(config),
        "condition_rows": condition_df.to_dict(orient="records") if not condition_df.empty else [],
    }
    correlation = _compute_correlation(cycle_df, config.correlation_var, config.correlation_method)
    if correlation is not None:
        summary["correlation"] = correlation
    if config.open_price_condition and not cycle_df.empty:
        matched = cycle_df.apply(
            lambda row: _truthy(evaluate_scalar_expression(config.open_price_condition or "", _expression_context(row.to_dict()))),
            axis=1,
        )
        matched_df = cycle_df.loc[matched]
        if matched_df.empty:
            summary["open_price_condition"] = {
                "condition": config.open_price_condition,
                "matched_cycles": 0,
                "up_win_ratio": math.nan,
            }
        else:
            summary["open_price_condition"] = {
                "condition": config.open_price_condition,
                "matched_cycles": int(len(matched_df)),
                "up_win_ratio": float((matched_df["winner"] == "up").mean()),
            }
    return summary


def _compute_correlation(
    cycle_df: pd.DataFrame,
    variable: str | None,
    method: str,
) -> dict[str, Any] | None:
    if not variable or cycle_df.empty or variable not in cycle_df.columns:
        return None
    numeric_x = pd.to_numeric(cycle_df[variable], errors="coerce")
    numeric_y = pd.to_numeric(cycle_df.get("winner_value"), errors="coerce")
    valid = numeric_x.notna() & numeric_y.notna()
    if int(valid.sum()) < 2:
        return None
    x = numeric_x.loc[valid].astype(float)
    y = numeric_y.loc[valid].astype(float)
    if method == "spearman":
        x = x.rank(method="average")
        y = y.rank(method="average")
    corr = float(x.corr(y, method="pearson"))
    p_value = _approximate_p_value(corr, len(x))
    return {
        "variable": variable,
        "method": method,
        "value": corr,
        "p_value": p_value,
        "sample_size": int(len(x)),
    }


def _approximate_p_value(correlation: float, sample_size: int) -> float | None:
    if sample_size < 4 or math.isnan(correlation):
        return None
    corr = max(min(correlation, 0.999999), -0.999999)
    z = 0.5 * math.log((1.0 + corr) / (1.0 - corr)) * math.sqrt(sample_size - 3)
    return math.erfc(abs(z) / math.sqrt(2.0))


def _write_outputs(
    cycle_df: pd.DataFrame,
    condition_df: pd.DataFrame,
    summary: dict[str, Any],
    config: UniversalStatsConfig,
) -> tuple[Path | None, Path | None]:
    if config.output_format == "table":
        return None, None

    output_path = Path(config.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_path.with_name(f"{output_path.stem}_summary.json")

    if config.output_format == "csv":
        _select_output_columns(cycle_df, config.output_columns).to_csv(output_path, index=False, encoding="utf-8-sig")
    else:
        payload = {
            "rows": _select_output_columns(cycle_df, config.output_columns).to_dict(orient="records"),
            "summary": summary,
            "conditions": condition_df.to_dict(orient="records"),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")

    summary_path.write_text(
        json.dumps(
            {
                "summary": summary,
                "conditions": condition_df.to_dict(orient="records"),
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    return output_path, summary_path


def _select_output_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    if not columns:
        return df.copy()
    out = df.copy()
    for column in columns:
        if column not in out.columns:
            out[column] = math.nan
    return out.loc[:, columns]


def _split_filter_expression(expression: str) -> tuple[str, str | None]:
    marker = " filter "
    lowered = expression.lower()
    index = lowered.find(marker)
    if index < 0:
        return expression, None
    return expression[:index].strip(), expression[index + len(marker):].strip()


def _normalize_expression(expression: str) -> str:
    return re.sub(r"(?<![=!<>])=(?!=)", "==", expression.strip())


def _eval_ast(node: ast.AST, context: dict[str, Any], *, trade_contexts: list[dict[str, Any]] | None, scalar_context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in context:
            return context[node.id]
        if node.id in scalar_context:
            return scalar_context[node.id]
        if node.id in {"True", "False", "None"}:
            return eval(node.id)
        return None
    if isinstance(node, ast.UnaryOp):
        value = _eval_ast(node.operand, context, trade_contexts=trade_contexts, scalar_context=scalar_context)
        if isinstance(node.op, ast.USub):
            return -float(value)
        if isinstance(node.op, ast.UAdd):
            return +float(value)
        if isinstance(node.op, ast.Not):
            return not _truthy(value)
    if isinstance(node, ast.BoolOp):
        values = [_eval_ast(value, context, trade_contexts=trade_contexts, scalar_context=scalar_context) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(_truthy(value) for value in values)
        if isinstance(node.op, ast.Or):
            return any(_truthy(value) for value in values)
    if isinstance(node, ast.BinOp):
        left = _eval_ast(node.left, context, trade_contexts=trade_contexts, scalar_context=scalar_context)
        right = _eval_ast(node.right, context, trade_contexts=trade_contexts, scalar_context=scalar_context)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            return left ** right
    if isinstance(node, ast.Compare):
        left = _eval_ast(node.left, context, trade_contexts=trade_contexts, scalar_context=scalar_context)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_ast(comparator, context, trade_contexts=trade_contexts, scalar_context=scalar_context)
            ok = False
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            else:
                raise ValueError(f"不支持的比较表达式: {ast.dump(node)}")
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        func_name = node.func.id
        if trade_contexts is not None and func_name in {"sum", "avg", "mean", "count", "min", "max", "first", "last"}:
            return _eval_aggregate_call(func_name, node.args, trade_contexts, scalar_context)
        if func_name in _SAFE_SCALAR_FUNCS:
            args = [_eval_ast(arg, context, trade_contexts=trade_contexts, scalar_context=scalar_context) for arg in node.args]
            return _SAFE_SCALAR_FUNCS[func_name](*args)
    raise ValueError(f"不支持的表达式: {ast.dump(node)}")


def _eval_aggregate_call(func_name: str, args: list[ast.AST], trade_contexts: list[dict[str, Any]], scalar_context: dict[str, Any]) -> Any:
    if func_name == "count":
        if not args:
            return len(trade_contexts)
        values = [_eval_ast(args[0], trade_context, trade_contexts=None, scalar_context=scalar_context) for trade_context in trade_contexts]
        return sum(1 for value in values if _truthy(value))
    if not args:
        raise ValueError(f"聚合函数 {func_name} 需要参数")
    values = [_eval_ast(args[0], trade_context, trade_contexts=None, scalar_context=scalar_context) for trade_context in trade_contexts]
    if func_name == "sum":
        return sum(float(value or 0.0) for value in values)
    if func_name in {"avg", "mean"}:
        if not values:
            return math.nan
        return sum(float(value or 0.0) for value in values) / len(values)
    if func_name == "min":
        return min(values) if values else math.nan
    if func_name == "max":
        return max(values) if values else math.nan
    if func_name == "first":
        return values[0] if values else math.nan
    if func_name == "last":
        return values[-1] if values else math.nan
    raise ValueError(f"不支持的聚合函数: {func_name}")


def _expression_context(record: dict[str, Any]) -> dict[str, Any]:
    context = dict(record)
    context.setdefault("winner_is_up", str(record.get("winner") or "") == "up")
    context.setdefault("winner_is_down", str(record.get("winner") or "") == "down")
    context.setdefault("outcome", record.get("winner"))
    context.setdefault("outcome_win", str(record.get("winner") or "") in {"up", "down"})
    return context


def _describe_window_config(config: UniversalStatsConfig) -> str:
    if config.time_window_mode == "full_cycle":
        return "full_cycle"
    if config.time_window_mode == "relative_start":
        return f"relative_start[{config.window_start_sec},{config.window_end_sec}]"
    if config.time_window_mode == "relative_end":
        return f"relative_end[{config.window_start_sec},{config.window_end_sec}]"
    if config.time_window_mode == "absolute_time":
        return f"absolute_time[{config.window_start_offset},{config.window_end_offset}]"
    return f"percentage[{config.window_start_pct},{config.window_end_pct}]"


def _delta_values(left: Any, right: Any) -> float:
    if _is_missing(left) or _is_missing(right):
        return math.nan
    return abs(float(left) - float(right))


def _offset_from_cycle_bounds(cycle_start: datetime, cycle_end: datetime, offset_seconds: float) -> datetime:
    if offset_seconds < 0:
        return cycle_end + timedelta(seconds=offset_seconds)
    return cycle_start + timedelta(seconds=offset_seconds)


def _parse_duration(value: str | float | int) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return float(text)
    total = 0.0
    matches = list(_DURATION_TOKEN_RE.finditer(text))
    if not matches:
        raise ValueError(f"无法解析时间偏移: {value}")
    unit_seconds = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    for match in matches:
        amount = float(match.group(1))
        unit = match.group(2).lower()
        total += amount * unit_seconds[unit]
    return total


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", "null"):
        return None
    timestamp = pd.to_datetime(value, utc=True)
    return timestamp.tz_localize(None).to_pydatetime()


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    return float(value)


def _optional_string(value: Any) -> str | None:
    if value in (None, "", "null"):
        return None
    return str(value).strip() or None


def _to_bool(value: Any, *, default: bool) -> bool:
    if value in (None, "", "null"):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _to_string_list(value: Any) -> list[str]:
    if value in (None, "", "null"):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _normalize_outcome(value: str) -> str:
    text = str(value).strip().lower()
    if "down" in text:
        return "down"
    return "up"


def _truthy(value: Any) -> bool:
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value)


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value