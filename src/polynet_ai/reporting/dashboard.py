from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd


@dataclass(slots=True)
class DashboardArtifacts:
    html_path: Path
    markdown_path: Path
    summary_csv_path: Path
    state_script_path: Path


@dataclass(slots=True)
class DashboardAlert:
    code: str
    severity: str
    title: str
    message: str


def _to_float_series(frame: pd.DataFrame, column: str) -> list[float]:
    if frame.empty or column not in frame.columns:
        return []
    return pd.to_numeric(frame[column], errors="coerce").fillna(0.0).astype(float).tolist()


def _format_value(value: Any, digits: int = 3) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        text = f"{float(value):,.{digits}f}"
        # Display at most `digits` decimals instead of always fixed width.
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    return str(value)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _line_svg(values: list[float], width: int = 640, height: int = 180, color: str = "#4f8cff") -> str:
    if not values:
        return '<svg viewBox="0 0 640 180" class="chart"><text x="20" y="90">No data</text></svg>'
    min_v = min(values)
    max_v = max(values)
    span = max(max_v - min_v, 1e-9)
    step_x = width / max(1, len(values) - 1)
    points: list[str] = []
    area_points = [f"0,{height}"]
    for idx, value in enumerate(values):
        x = idx * step_x
        y = height - ((value - min_v) / span) * (height - 20) - 10
        points.append(f"{x:.2f},{y:.2f}")
        area_points.append(f"{x:.2f},{y:.2f}")
    area_points.append(f"{width:.2f},{height}")
    polyline = " ".join(points)
    area = " ".join(area_points)
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart">'
        f'<polygon points="{area}" fill="{color}" opacity="0.12"></polygon>'
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="3"></polyline>'
        f'<text x="10" y="16" fill="#94a3b8">min {_format_value(min_v)}</text>'
        f'<text x="{width - 140}" y="16" fill="#94a3b8">max {_format_value(max_v)}</text>'
        "</svg>"
    )


def _multi_line_svg(
    series_map: list[tuple[str, list[float], str]],
    width: int = 640,
    height: int = 180,
) -> str:
    valid = [(label, values, color) for label, values, color in series_map if values]
    if not valid:
        return '<svg viewBox="0 0 640 180" class="chart"><text x="20" y="90">No data</text></svg>'
    all_values = [value for _, values, _ in valid for value in values]
    min_v = min(all_values)
    max_v = max(all_values)
    span = max(max_v - min_v, 1e-9)
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">']
    for series_idx, (label, values, color) in enumerate(valid):
        step_x = width / max(1, len(values) - 1)
        points: list[str] = []
        for idx, value in enumerate(values):
            x = idx * step_x
            y = height - ((value - min_v) / span) * (height - 20) - 10
            points.append(f"{x:.2f},{y:.2f}")
        parts.append(
            f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3"></polyline>'
        )
        parts.append(
            f'<text x="{10 + series_idx * 120}" y="16" fill="{color}">{html.escape(label)} {html.escape(_format_value(values[-1]))}</text>'
        )
    parts.append(f'<text x="10" y="{height - 8}" fill="#94a3b8">min {_format_value(min_v)}</text>')
    parts.append(f'<text x="{width - 140}" y="{height - 8}" fill="#94a3b8">max {_format_value(max_v)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _bar_svg(items: list[tuple[str, float]], width: int = 640, height: int = 220, color: str = "#22c55e") -> str:
    if not items:
        return '<svg viewBox="0 0 640 220" class="chart"><text x="20" y="110">No data</text></svg>'
    max_v = max(value for _, value in items) or 1.0
    bar_gap = 12
    usable_width = width - 80
    bar_width = max(28, usable_width / max(1, len(items)) - bar_gap)
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart">']
    for idx, (label, value) in enumerate(items):
        x = 40 + idx * (bar_width + bar_gap)
        bar_h = (value / max_v) * (height - 70)
        y = height - 40 - bar_h
        parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{bar_h:.2f}" rx="6" fill="{color}"></rect>')
        parts.append(f'<text x="{x + bar_width / 2:.2f}" y="{y - 8:.2f}" text-anchor="middle" fill="#cbd5e1">{_format_value(value, 0)}</text>')
        safe_label = html.escape(label)
        parts.append(f'<text x="{x + bar_width / 2:.2f}" y="{height - 14}" text-anchor="middle" fill="#94a3b8">{safe_label}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _extract_rule_counts(metrics_df: pd.DataFrame, prefix: str) -> list[tuple[str, float]]:
    if metrics_df.empty:
        return []
    row = metrics_df.iloc[0].to_dict()
    items: list[tuple[str, float]] = []
    for key, value in row.items():
        if isinstance(key, str) and key.startswith(prefix):
            try:
                items.append((key.replace(prefix, ""), float(value)))
            except (TypeError, ValueError):
                continue
    items.sort(key=lambda item: item[1], reverse=True)
    return items


def _render_table(
    frame: pd.DataFrame,
    row_class_fn: Callable[[pd.Series], str] | None = None,
) -> str:
    if frame.empty:
        return "<p>No data.</p>"
    headers = "".join(f"<th>{html.escape(str(col))}</th>" for col in frame.columns)
    rows: list[str] = []
    for _, row in frame.iterrows():
        css_class = row_class_fn(row) if row_class_fn else ""
        class_attr = f' class="{css_class}"' if css_class else ""
        rendered_cells: list[str] = []
        for value in row.tolist():
            if isinstance(value, bool):
                rendered = "True" if value else "False"
            elif isinstance(value, (int, float)):
                rendered = _format_value(value, 3)
            else:
                rendered = str(value)
            rendered_cells.append(f"<td>{html.escape(rendered)}</td>")
        cells = "".join(rendered_cells)
        rows.append(f"<tr{class_attr}>{cells}</tr>")
    return f'<table class="table"><thead><tr>{headers}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def _build_summary_frame(metrics_df: pd.DataFrame, cycles_df: pd.DataFrame, decisions_df: pd.DataFrame, snapshots_df: pd.DataFrame) -> pd.DataFrame:
    metrics = metrics_df.iloc[0].to_dict() if not metrics_df.empty else {}
    latest_cash = float(pd.to_numeric(snapshots_df.get("account_cash", pd.Series(dtype=float)), errors="coerce").fillna(0).iloc[-1]) if not snapshots_df.empty and "account_cash" in snapshots_df.columns else 0.0
    latest_cycle = str(snapshots_df.iloc[-1]["cycle_id"]) if not snapshots_df.empty and "cycle_id" in snapshots_df.columns else ""
    latest_market = str(snapshots_df.iloc[-1]["market_id"]) if not snapshots_df.empty and "market_id" in snapshots_df.columns else ""
    latest_net = float(pd.to_numeric(snapshots_df.get("net_position", pd.Series(dtype=float)), errors="coerce").fillna(0).iloc[-1]) if not snapshots_df.empty and "net_position" in snapshots_df.columns else 0.0
    latest_cycle_net_profit = float(pd.to_numeric(snapshots_df.get("cycle_net_profit", pd.Series(dtype=float)), errors="coerce").fillna(0).iloc[-1]) if not snapshots_df.empty and "cycle_net_profit" in snapshots_df.columns else 0.0
    latest_exposure_value = float(pd.to_numeric(cycles_df.get("net_position_value", pd.Series(dtype=float)), errors="coerce").fillna(0).iloc[-1]) if not cycles_df.empty and "net_position_value" in cycles_df.columns else 0.0
    blocked_ratio = (
        float(metrics.get("blocked_signals", 0.0)) / float(metrics.get("total_signals", 1.0))
        if float(metrics.get("total_signals", 0.0)) > 0
        else 0.0
    )
    summary = {
        "latest_market": latest_market,
        "latest_cycle": latest_cycle,
        "latest_cash": latest_cash,
        "latest_net_position": latest_net,
        "latest_cycle_net_profit": latest_cycle_net_profit,
        "latest_exposure_value": latest_exposure_value,
        "total_cycles": metrics.get("total_cycles", 0),
        "total_net_profit": metrics.get("total_net_profit", 0.0),
        "average_cycle_profit": metrics.get("average_cycle_profit", 0.0),
        "win_rate": metrics.get("win_rate", 0.0),
        "max_drawdown": metrics.get("max_drawdown", 0.0),
        "total_fees": metrics.get("total_fees", 0.0),
        "total_signals": metrics.get("total_signals", 0),
        "accepted_signals": metrics.get("accepted_signals", 0),
        "blocked_signals": metrics.get("blocked_signals", 0),
        "executed_trades": metrics.get("executed_trades", 0),
        "signal_execution_rate": metrics.get("signal_execution_rate", 0.0),
        "blocked_ratio": blocked_ratio,
        "cycle_rows": len(cycles_df),
        "decision_rows": len(decisions_df),
        "snapshot_rows": len(snapshots_df),
    }
    return pd.DataFrame([summary])


def _build_alerts(summary: dict[str, Any], cycles_df: pd.DataFrame, decisions_df: pd.DataFrame) -> list[DashboardAlert]:
    alerts: list[DashboardAlert] = []
    max_drawdown = abs(_to_float(summary.get("max_drawdown", 0.0)))
    blocked_ratio = _to_float(summary.get("blocked_ratio", 0.0))
    latest_net_position = abs(_to_float(summary.get("latest_net_position", 0.0)))
    latest_exposure_value = abs(_to_float(summary.get("latest_exposure_value", 0.0)))
    latest_cycle_net_profit = _to_float(summary.get("latest_cycle_net_profit", 0.0))
    signal_execution_rate = _to_float(summary.get("signal_execution_rate", 0.0))

    if max_drawdown >= 100:
        alerts.append(DashboardAlert("drawdown_critical", "critical", "高回撤告警", f"最大回撤已达到 {_format_value(max_drawdown)}，风险极高。"))
    elif max_drawdown >= 20:
        alerts.append(DashboardAlert("drawdown_warn", "warning", "回撤偏高", f"最大回撤为 {_format_value(max_drawdown)}，建议检查止损和仓位控制。"))

    if blocked_ratio >= 0.8:
        alerts.append(DashboardAlert("blocked_critical", "critical", "信号阻塞率过高", f"当前阻塞率为 {_format_value(blocked_ratio * 100, 2)}%，大量信号被风控拒绝。"))
    elif blocked_ratio >= 0.6:
        alerts.append(DashboardAlert("blocked_warn", "warning", "信号阻塞率偏高", f"当前阻塞率为 {_format_value(blocked_ratio * 100, 2)}%，可能存在参数过激或风控过紧。"))

    if latest_net_position >= 50 or latest_exposure_value >= 50:
        alerts.append(DashboardAlert("tail_critical", "critical", "尾盘留仓过大", f"最新净持仓 {_format_value(latest_net_position)}，净敞口价值 {_format_value(latest_exposure_value)}。"))
    elif latest_net_position >= 20 or latest_exposure_value >= 20:
        alerts.append(DashboardAlert("tail_warn", "warning", "尾盘留仓偏大", f"最新净持仓 {_format_value(latest_net_position)}，建议检查最后一分钟减仓规则。"))

    if latest_cycle_net_profit < 0:
        alerts.append(DashboardAlert("cycle_loss", "warning", "最新周期亏损", f"最新周期盈亏为 {_format_value(latest_cycle_net_profit)}。"))

    if signal_execution_rate <= 0.15 and _to_float(summary.get("total_signals", 0.0)) >= 20:
        alerts.append(DashboardAlert("exec_low", "warning", "信号执行率偏低", f"当前信号执行率仅 {_format_value(signal_execution_rate * 100, 2)}%。"))

    if not cycles_df.empty and "cycle_net_profit" in cycles_df.columns:
        recent_losses = int((pd.to_numeric(cycles_df["cycle_net_profit"], errors="coerce").fillna(0.0) < 0).tail(3).sum())
        if recent_losses >= 2:
            alerts.append(DashboardAlert("recent_losses", "warning", "近期周期连续承压", f"最近 3 个周期中有 {recent_losses} 个周期亏损。"))

    if not decisions_df.empty and "risk_status" in decisions_df.columns:
        recent_blocked = int((decisions_df["risk_status"].fillna("") == "blocked").tail(20).sum())
        if recent_blocked >= 15:
            alerts.append(DashboardAlert("recent_blocked", "warning", "近期风控拦截密集", f"最近 20 条信号中有 {recent_blocked} 条被风控拦截。"))

    return alerts


def _alert_badge(alert: DashboardAlert) -> str:
    return (
        f'<div class="alert-card {alert.severity}">'
        f'<div class="alert-title">{html.escape(alert.title)}</div>'
        f'<div class="alert-message">{html.escape(alert.message)}</div>'
        "</div>"
    )


def build_dashboard_state(
    title: str,
    metrics_df: pd.DataFrame,
    cycles_df: pd.DataFrame,
    decisions_df: pd.DataFrame,
    snapshots_df: pd.DataFrame,
    refresh_seconds: float = 1.0,
) -> dict[str, Any]:
    summary_df = _build_summary_frame(metrics_df, cycles_df, decisions_df, snapshots_df)
    summary = summary_df.iloc[0].to_dict()
    alerts = _build_alerts(summary, cycles_df, decisions_df)
    equity_curve = _line_svg(_to_float_series(snapshots_df, "account_cash"), color="#3b82f6")
    cycle_curve = _line_svg(_to_float_series(cycles_df, "cycle_net_profit"), color="#8b5cf6")
    outcome_curve = _multi_line_svg(
        [
            ("Up", _to_float_series(snapshots_df, "up_last_price"), "#22c55e"),
            ("Down", _to_float_series(snapshots_df, "down_last_price"), "#ef4444"),
        ]
    )
    signal_counts = _bar_svg(_extract_rule_counts(metrics_df, "selected_rule_"), color="#0ea5e9")
    executed_counts = _bar_svg(_extract_rule_counts(metrics_df, "executed_rule_"), color="#22c55e")

    recent_decisions = decisions_df.tail(12).copy()
    if not recent_decisions.empty:
        for column in ("timestamp", "market_price", "selected_rule", "selected_action", "selected_outcome", "risk_status", "executed", "fill_price", "cycle_net_profit"):
            if column not in recent_decisions.columns:
                recent_decisions[column] = ""
        recent_decisions = recent_decisions[
            ["timestamp", "selected_rule", "selected_action", "selected_outcome", "risk_status", "executed", "fill_price", "cycle_net_profit"]
        ]
    decision_html = _render_table(
        recent_decisions,
        row_class_fn=lambda row: "row-danger" if str(row.get("risk_status", "")) == "blocked" else ("row-warning" if not bool(row.get("executed", False)) and str(row.get("selected_rule", "")) else ""),
    ) if not recent_decisions.empty else "<p>No recent decisions.</p>"

    recent_cycles = cycles_df.tail(8).copy()
    cycle_html = _render_table(
        recent_cycles,
        row_class_fn=lambda row: "row-danger" if _to_float(row.get("cycle_net_profit", 0.0)) < 0 else "",
    ) if not recent_cycles.empty else "<p>No cycle summary.</p>"

    cards = [
        ("净利润", _format_value(summary.get("total_net_profit", 0.0))),
        ("最大回撤", _format_value(summary.get("max_drawdown", 0.0))),
        ("胜率", _format_value(float(summary.get("win_rate", 0.0)) * 100, 2) + "%"),
        ("信号执行率", _format_value(float(summary.get("signal_execution_rate", 0.0)) * 100, 2) + "%"),
        ("最新现金", _format_value(summary.get("latest_cash", 0.0))),
        ("最新净持仓", _format_value(summary.get("latest_net_position", 0.0))),
    ]
    card_html = "".join(
        f'<div class="card"><div class="card-label">{html.escape(label)}</div><div class="card-value">{html.escape(value)}</div></div>'
        for label, value in cards
    )
    alert_html = "".join(_alert_badge(alert) for alert in alerts) if alerts else '<div class="alert-card ok"><div class="alert-title">无主动告警</div><div class="alert-message">当前指标未触发高风险阈值。</div></div>'
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {
        "title": title,
        "latest_market": str(summary.get("latest_market", "")),
        "latest_cycle": str(summary.get("latest_cycle", "")),
        "total_cycles": _format_value(summary.get("total_cycles", 0), 0),
        "total_signals": _format_value(summary.get("total_signals", 0), 0),
        "refresh_seconds": refresh_seconds,
        "generated_at": generated_at,
        "live_status_text": f"实时面板: 每 {refresh_seconds:.1f}s 更新，最近写盘 {generated_at}",
        "card_html": card_html,
        "alert_html": alert_html,
        "equity_curve_html": equity_curve,
        "cycle_curve_html": cycle_curve,
        "outcome_curve_html": outcome_curve,
        "signal_counts_html": signal_counts,
        "executed_counts_html": executed_counts,
        "decision_html": decision_html,
        "cycle_html": cycle_html,
    }


def _build_dashboard_state_script(state: dict[str, Any]) -> str:
    return "window.__POLYNET_DASHBOARD_STATE__ = " + json.dumps(state, ensure_ascii=False) + ";\n"


def _dashboard_rule_price_param_meta() -> dict[str, dict[str, str]]:
    detail = (
        "秒数为规则侧缓存盘口价的刷新间隔；0 表示尽量用当前 tick 的最新价。"
        "调大可减轻细碎抖动，但在极速行情中判断会略滞后。"
    )
    pairs = [
        ("rule_price_feed.last_minute", "尾盘：常填 0 或 0.5"),
        ("rule_price_feed.entries.opening", "opening：例 0、1"),
        ("rule_price_feed.entries.hedge", "hedge 入场：例 0、1"),
        ("rule_price_feed.entries.grid", "grid 入场：例 0、2"),
        ("rule_price_feed.entries.mean_reversion", "均值回归入场：例 0、1"),
        ("rule_price_feed.entries.trend", "trend 入场：例 0、1"),
        ("rule_price_feed.exits.stop_loss", "止损离场：建议偏小，如 0、0.5"),
        ("rule_price_feed.exits.hedge", "对冲离场：例 0、1"),
        ("rule_price_feed.exits.take_profit", "止盈离场：例 0、1"),
        ("rule_price_feed.exits.grid", "grid 离场：例 0、2"),
        ("rule_price_feed.exits.mean_reversion", "均值回归离场：例 0、1"),
    ]
    return {path: {"example": ex, "detail": detail} for path, ex in pairs}


def _dashboard_priority_param_meta() -> dict[str, dict[str, str]]:
    detail = (
        "数字越小优先级越高（越早参与本 tick 的规则排序）。"
        "通常让风险、止损等护栏排在加仓类规则之前；改完后建议回放确认拦截顺序符合预期。"
    )
    pairs: list[tuple[str, str]] = [
        ("priorities.risk", "例：1，统合风险最先裁定"),
        ("priorities.last_minute", "例：3～8，尾盘在多数入场之后"),
        ("priorities.opening", "例：5～15，开盘试探插队位置"),
        ("priorities.stop_loss", "例：2～5，紧挨风险之后"),
        ("priorities.hedge", "例：4～10"),
        ("priorities.take_profit", "例：6～12"),
        ("priorities.grid", "例：8～20"),
        ("priorities.mean_reversion", "例：8～20"),
        ("priorities.trend", "例：10～25"),
    ]
    prules = [
        "risk",
        "last_minute",
        "stop_loss",
        "hedge",
        "take_profit",
        "opening",
        "grid",
        "mean_reversion",
        "trend",
    ]
    for ph in range(1, 5):
        for r in prules:
            pairs.append((f"priorities.by_phase.phase_{ph}.{r}", f"阶段{ph} · {r}；与扁平 priorities.{r} 语义相同，可按阶段覆盖。"))
    return {path: {"example": ex, "detail": detail} for path, ex in pairs}


def _build_dashboard_config_param_meta() -> dict[str, dict[str, str]]:
    meta: dict[str, dict[str, str]] = {
        "cycle.cycle_seconds": {
            "example": "300 — 常见 BTC 5 分钟 Up/Down 一整轮秒数",
            "detail": "必须与所交易市场的实际周期一致；换 15 分钟等品种时要改成 900 等对应值，否则尾盘与节奏类逻辑会错位。",
        },
        "cycle.last_minute_seconds": {
            "example": "60 表示周期最后 60 秒内进入「最后一分钟」规则集",
            "detail": "过大会过早进入尾盘逻辑，过小则几乎没有缓冲；需与 cycle_seconds 和实盘节奏一起考虑。",
        },
        "opening_entry.enabled": {
            "example": "勾选：周期开头允许按 opening 规则试探建仓",
            "detail": "关闭后仅依赖网格、趋势等后续逻辑，适合想完全禁用开盘脉冲的场景。",
        },
        "opening_entry.window_seconds": {
            "example": "30 — 仅周期开始后 30 秒内评估 opening",
            "detail": "窗口越短越保守；过长会把中段行情仍当作「开盘」，与业务语义不符。",
        },
        "opening_entry.vwap_epsilon": {
            "example": "0.01 表示弱势侧可比 VWAP 高约 1 分（0.01）仍算可接受",
            "detail": "用于放宽「必须严格在 VWAP 下」的硬条件；过大可能买到明显偏贵的边际。",
        },
        "opening_entry.range_low_fraction": {
            "example": "0.35 表示价格落在区间下 35% 宽度内才算「低位」",
            "detail": "与 min_range_width 联用；过宽几乎总触发，过窄则很少开仓。",
        },
        "opening_entry.min_range_width": {
            "example": "0.02 表示买卖价差至少约 2 分才启用区间低位判断",
            "detail": "盘口极薄时跳过区间逻辑，避免在噪声上误判「低位」。",
        },
        "opening_entry.infer_missing_with_binary_complement": {
            "example": "开启：若只见 Up=0.6，则推断 Down≈0.4",
            "detail": "在仅单边有报价时避免规则完全失明；若数据源本身不可靠，可关闭改为要求双边价。",
        },
        "order_sizing.buy.base_order_size": {
            "example": "5 表示多数买入规则从 5 份合约量级起步（具体单位随撮合定义）",
            "detail": "会与买入波动率放大、买入 max_order_size 等共同约束最终下单量。",
        },
        "order_sizing.buy.min_order_size": {
            "example": "5 — 买单低于此值会被风控拦截",
            "detail": "建议不低于交易所 orderbook 的 min_order_size，避免实盘拒单。",
        },
        "order_sizing.buy.max_order_size": {
            "example": "40 — 单笔买入量不会超过 40",
            "detail": "硬上限，用于抑制单条买入信号过激；与敞口、资金使用率一起看。",
        },
        "order_sizing.buy.volatility_order_scale": {
            "example": "20 — 波动率指标越高，买入基础单量按模型放大越明显",
            "detail": "设为 0 则关闭波动放大；过大可能在剧烈行情中单笔过重。",
        },
        "order_sizing.sell.min_order_size": {
            "example": "5 — 卖单常规最小值（可被强平豁免逻辑覆盖）",
            "detail": "与 execution.market_limits.enforce_sell_min_order_size 一起决定卖单是否严格受交易所最小手数约束。",
        },
        "order_sizing.sell.max_order_size": {
            "example": "40 — 单笔卖出量不会超过 40",
            "detail": "防止止盈/止损规则一次性卸仓过大。",
        },
        "order_sizing.sell.allow_close_below_min_order_size": {
            "example": "开启：仅当剩余可卖仓位本身低于最小手数时允许低于最小值平仓",
            "detail": "用于处理碎仓尾单；关闭后会严格按最小下单量执行。",
        },
        "capital.max_cash_utilization": {
            "example": "0.92 表示最多动用账户现金的 92%",
            "detail": "留余量应对手续费、滑点与未结算占用；拉满到 1.0 容易因舍入或费用导致拒单。",
        },
        "capital.min_cash_buffer": {
            "example": "50 — 账户始终保留约 50 单位现金不动",
            "detail": "缓冲越大越保守；在高频小单策略中可适当降低。",
        },
        "exposure.max_abs_exposure_value": {
            "example": "200 — 净敞口价值绝对值不超过 200（与账户币种一致）",
            "detail": "超过后风控会限制新开仓；与对冲、网格净仓协同使用。",
        },
        "exposure.hedge_trigger_value": {
            "example": "80 — 净敞口超过 80 时开始考虑对冲单",
            "detail": "阈值越低越早对冲，可能增加换手；越高则单边暴露时间更长。",
        },
        "exposure.hedge_scale": {
            "example": "0.5 表示超额敞口的一半换算为对冲目标量（示意，以代码为准）",
            "detail": "调大对冲更猛，调小更温和；需与 hedge_trigger 一起看。",
        },
        "exposure.max_strategy_trades_per_cycle": {
            "example": "12 — 单个 5 分钟周期内策略成交不超过 12 次",
            "detail": "抑制过度交易与费用；实盘延迟高时可适当降低。",
        },
        "trend.min_trend_strength": {
            "example": "0.35 — 趋势强度低于 0.35 不触发趋势单",
            "detail": "越高越挑剔，越少追涨杀跌；越低越容易在弱趋势中频繁交易。",
        },
        "trend.trend_price_edge": {
            "example": "0.05 — 价格需偏离参考价约 5 分以上才认定「够偏」",
            "detail": "过滤微小波动；过大可能错过早期趋势段。",
        },
        "trend.trend_scale": {
            "example": "0.3 — 净仓越大，趋势单量按该系数额外放大",
            "detail": "高风险参数：放大后单边加速更快，需配合止损与敞口上限。",
        },
        "grid.grid_low_percentile": {
            "example": "0.2 — 价格低于近期分布的 20% 分位视为网格「低位」",
            "detail": "与 grid_high_percentile 共同定义区间；两者过近会减少网格触发。",
        },
        "grid.grid_high_percentile": {
            "example": "0.8 — 高于 80% 分位视为「高位」",
            "detail": "典型设置是低分位 < 高分位，留出中间中性带。",
        },
        "grid.disable_within_seconds_before_end": {
            "example": "45 — 周期剩余 ≤45 秒时不再挂网格买卖",
            "detail": "避免临近结算时区间策略与尾盘留仓逻辑冲突；可与均值回归尾盘窗口对齐调参。",
        },
        "mean_reversion.enabled": {
            "example": "开启：允许偏离均值时的买卖与回归平仓",
            "detail": "关闭后整条均值回归链路停用，仅保留其他规则。",
        },
        "mean_reversion.up_buy_deviation": {
            "example": "0.08 — Up 价比参考均价低约 8 分时考虑买入",
            "detail": "阈值越小信号越密；越大则只在深度折价时出手。",
        },
        "mean_reversion.down_buy_deviation": {
            "example": "0.08 — Down 侧对称逻辑",
            "detail": "可与 Up 侧设为不同值以反映流动性或偏好差异。",
        },
        "mean_reversion.mean_reversion_sell_up_deviation": {
            "example": "0.1 — Up 涨回接近均价以上约 10 分时考虑卖出",
            "detail": "均值回归「获利了结」侧阈值，与买入阈值不必对称。",
        },
        "mean_reversion.mean_reversion_sell_down_deviation": {
            "example": "0.1 — Down 侧回归卖出阈值",
            "detail": "过紧可能过早卖飞，过松则回撤吞掉利润。",
        },
        "mean_reversion.deviation_scale": {
            "example": "25 — 偏离越大，单次下单量按模型放得越多",
            "detail": "放大系数过高时，深偏离会带来超大单，务必有 max_order_size 与敞口兜底。",
        },
        "mean_reversion.disable_within_seconds_before_end": {
            "example": "60 — 最后 60 秒不做均值回归开平仓",
            "detail": "防止与尾盘竞价、结算价跳动叠加造成误触。",
        },
        "profit_taking.take_profit_up_deviation": {
            "example": "0.12 — Up 侧浮盈达到约 12 分偏离时触发止盈评估",
            "detail": "与 take_profit_fraction 配合决定「赚多少卖多少」。",
        },
        "profit_taking.take_profit_down_deviation": {
            "example": "0.12 — Down 价格高于其均价约 12 分时触发止盈评估",
            "detail": "当前策略中 Down 与 Up 一致，均以「高于各自均价」作为止盈触发方向。",
        },
        "profit_taking.take_profit_fraction": {
            "example": "0.35 — 每次止盈卖出当前该腿持仓的 35%",
            "detail": "1.0 表示一次性清仓该方向；分批卖出可平滑收益曲线。",
        },
        "stop_loss.stop_loss_cycle_loss": {
            "example": "18 — 本周期已实现亏损超过 18 单位则触发止损流程",
            "detail": "单位与账户计价一致；过小易误触，过大则单笔周期风险高。",
        },
        "stop_loss.stop_loss_fraction": {
            "example": "0.5 — 止损动作时卖掉约一半相关持仓",
            "detail": "与周期止损阈值搭配；全卖用 1.0，温和减仓用更小比例。",
        },
        "last_minute.last_minute_min_confidence": {
            "example": "0.72 — 模型或规则给出的信心低于 0.72 则不做主动尾盘留仓",
            "detail": "越高越谨慎，尾盘仓位越小；越低越激进。",
        },
        "last_minute.tail_profit_scale": {
            "example": "0.4 — 本周期盈利越多，尾盘允许保留的仓位按该系数放大",
            "detail": "盈利薄时尾盘收缩，盈利厚时略放宽；与 max_tail_exposure 上限一起约束。",
        },
        "last_minute.tail_volatility_scale": {
            "example": "15 — 波动率越高，尾盘目标仓位按模型放得越大",
            "detail": "认为波动大时「值得一搏」的权重；设为 0 可关闭该维度影响。",
        },
        "last_minute.max_tail_exposure": {
            "example": "80 — 尾盘净敞口价值不超过 80",
            "detail": "最后一道上限，防止尾盘逻辑在极端信号下堆过大裸敞口。",
        },
        "last_minute.preferred_leg_min_ratio": {
            "example": "1.15 — 优势侧份额至少为另一侧的 1.15 倍才满足「偏一边」",
            "detail": "1.0 表示关闭该约束；大于 1 会强制尾盘更「站队」一侧。",
        },
        "execution.fee_rate": {
            "example": "0.002 表示成交名义金额的约 0.2% 作为手续费",
            "detail": "仅影响 paper 成本模拟；与实盘费率不一致时回测收益会偏差。",
        },
        "execution.slippage_bps": {
            "example": "10 — 假设每边约 10 bps 的成交价劣化",
            "detail": "基点越大模拟越悲观；0 表示理想成交价。",
        },
        "execution.min_seconds_between_orders": {
            "example": "2.5 — 两次下单至少间隔 2.5 秒",
            "detail": "抑制刷单与 API 压力；实盘延迟大时可略放宽。",
        },
        "execution.min_same_outcome_price_move_ratio": {
            "example": "0.02 — 同一 outcome 再次下单前价格至少相对上次变动约 2%",
            "detail": "防止在同一价位附近反复小额加仓；设为 0 则关闭该过滤。",
        },
        "execution.market_limits.use_orderbook_min_order_size": {
            "example": "开启：按 orderbook 的 min_order_size 作为市场最小手数",
            "detail": "建议保持开启；若关闭将仅按策略配置阈值约束。",
        },
        "execution.market_limits.fallback_min_order_size": {
            "example": "5 — 当 orderbook 未返回 min_order_size 时的兜底值",
            "detail": "建议设成该系列市场的常见最小手数。",
        },
        "execution.market_limits.enforce_sell_min_order_size": {
            "example": "开启：卖单也按市场最小手数限制",
            "detail": "关闭后仅买单强制市场最小手数，卖单可更灵活但实盘更易被拒。",
        },
        "scenarios": {
            "example": "数组内一项：name 填「基线」，overrides 里写 order_sizing.buy.base_order_size: 5",
            "detail": "用于 sweep 命名分组；overrides 的键与 strategy.yaml 路径一致，值为该场景覆盖内容。",
        },
        "grid.execution.slippage_bps": {
            "example": "[0, 5, 10, 20] — 网格逐档试这些滑点假设",
            "detail": "与 grid 扫描脚本约定一致；元素须为合法 bps 数值。",
        },
        "grid.profit_taking.take_profit_fraction": {
            "example": "[0.25, 0.5, 0.75] — 扫描多档止盈比例",
            "detail": "每项应在 0～1 之间；与回测目标函数一起看性价比。",
        },
        "trials": {
            "example": "40 — 自动寻优随机/贝叶斯采样 40 组参数",
            "detail": "越大搜索越充分但耗时越久；可先小 trials 粗搜再放大。",
        },
        "seed": {
            "example": "42 — 固定种子使同样 trials 下复现同一批候选",
            "detail": "改种子会换一批采样；调参对比时建议记录 seed。",
        },
        "export_top_n": {
            "example": "5 — 把得分最高的 5 组参数导出为配置文件或报告",
            "detail": "便于人工复核前几名，而不是只看单一最优。",
        },
        "score_weights.total_net_profit": {
            "example": "1.0 — 净利润每多 1 单位约贡献 1 分（示意）",
            "detail": "权重为相对比例；需与其它指标量级匹配，否则某一项会主导排序。",
        },
        "score_weights.max_drawdown": {
            "example": "-2.0 — 回撤越大扣分越多（负权重）",
            "detail": "通常用负数惩罚回撤；绝对值越大越厌恶回撤。",
        },
        "score_weights.total_fees": {
            "example": "-0.5 — 费用越高得分越低",
            "detail": "抑制高换手策略；若回测费用模型偏低，可适当加大惩罚。",
        },
        "score_weights.win_rate": {
            "example": "0.8 — 胜率高的试验得分略上浮",
            "detail": "若只追求胜率可能牺牲盈亏比，建议与利润、回撤权重复合使用。",
        },
        "score_weights.signal_execution_rate": {
            "example": "0.3 — 信号能落地的比例越高越好",
            "detail": "反映参数是否过于激进导致大量被风控拦截；与净利润权重平衡。",
        },
        "parameters": {
            "example": "键为 strategy 路径，值为 dict：如 type: uniform, low: 3, high: 8",
            "detail": "与 optimize 脚本约定的参数空间格式一致；错误路径会导致寻优跳过或运行报错。",
        },
    }
    meta.update(_dashboard_rule_price_param_meta())
    meta.update(_dashboard_priority_param_meta())
    return meta


def _dashboard_static_config_schemas() -> dict[str, Any]:
    """sweep / optimize 控制台 schema（与内嵌 JS 历史行为一致）。"""
    return {
        "sweep": {
            "sections": [
                {
                    "title": "命名场景",
                    "description": "定义几组有业务含义的参数组合。",
                    "fields": [
                        {
                            "path": "scenarios",
                            "label": "场景列表",
                            "hint": "使用 JSON 数组编辑，每项包含 name 和 overrides。",
                            "type": "json",
                        },
                    ],
                },
                {
                    "title": "网格扫描",
                    "description": "批量枚举多个候选值组合。",
                    "fields": [
                        {
                            "path": "grid.execution.slippage_bps",
                            "label": "滑点候选列表",
                            "hint": "例如 [5, 10, 15]。",
                            "type": "json",
                        },
                        {
                            "path": "grid.profit_taking.take_profit_fraction",
                            "label": "止盈比例候选列表",
                            "hint": "例如 [0.25, 0.35]。",
                            "type": "json",
                        },
                    ],
                },
            ],
        },
        "optimize": {
            "sections": [
                {
                    "title": "优化任务",
                    "description": "控制试验次数、随机种子和导出数量。",
                    "fields": [
                        {
                            "path": "trials",
                            "label": "试验次数",
                            "hint": "一次自动寻优会采样多少组参数。",
                            "type": "number",
                            "ui": "range",
                            "min": 1,
                            "max": 200,
                            "step": 1,
                            "risk": "medium",
                        },
                        {
                            "path": "seed",
                            "label": "随机种子",
                            "hint": "固定后可重复同一组采样。",
                            "type": "number",
                            "min": 0,
                            "max": 999999,
                            "step": 1,
                            "risk": "low",
                        },
                        {
                            "path": "export_top_n",
                            "label": "导出前 N 名",
                            "hint": "输出得分最好的配置数量。",
                            "type": "number",
                            "ui": "range",
                            "min": 1,
                            "max": 20,
                            "step": 1,
                            "risk": "low",
                        },
                    ],
                },
                {
                    "title": "评分权重",
                    "description": "定义收益、回撤、费用、胜率等指标在评分中的重要性。",
                    "fields": [
                        {"path": "score_weights.total_net_profit", "label": "净利润权重", "hint": "收益越高得分越高。", "type": "number"},
                        {"path": "score_weights.max_drawdown", "label": "最大回撤权重", "hint": "通常设置为负值惩罚回撤。", "type": "number"},
                        {"path": "score_weights.total_fees", "label": "总费用权重", "hint": "通常设置为负值惩罚交易成本。", "type": "number"},
                        {"path": "score_weights.win_rate", "label": "胜率权重", "hint": "提高高胜率方案的综合得分。", "type": "number"},
                        {
                            "path": "score_weights.signal_execution_rate",
                            "label": "执行率权重",
                            "hint": "提高信号能落地方案的得分。",
                            "type": "number",
                        },
                    ],
                },
                {
                    "title": "参数空间",
                    "description": "定义每个参数的搜索范围与采样规则。",
                    "fields": [
                        {
                            "path": "parameters",
                            "label": "优化参数空间",
                            "hint": "使用 JSON 编辑，每个键是一条参数路径及其采样规则。",
                            "type": "json",
                        },
                    ],
                },
            ],
        },
    }


def _dashboard_config_schemas_json() -> str:
    from polynet_ai.reporting.strategy_console_schema import build_strategy_dashboard_sections

    merged: dict[str, Any] = {"strategy": {"sections": build_strategy_dashboard_sections()}}
    merged.update(_dashboard_static_config_schemas())
    return json.dumps(merged, ensure_ascii=False)


def _dashboard_config_param_meta_script() -> str:
    payload = json.dumps(_build_dashboard_config_param_meta(), ensure_ascii=False)
    return (
        f"\n    const CONFIG_PARAM_META = {payload};\n"
        "    function enrichConfigSchemaField(field) {\n"
        "      const meta = CONFIG_PARAM_META[field.path];\n"
        "      if (!meta) return field;\n"
        "      return Object.assign({}, field, {\n"
        "        example: field.example || meta.example,\n"
        "        detail: field.detail || meta.detail,\n"
        "      });\n"
        "    }\n"
    )


def build_dashboard_html(
    title: str,
    metrics_df: pd.DataFrame,
    cycles_df: pd.DataFrame,
    decisions_df: pd.DataFrame,
    snapshots_df: pd.DataFrame,
    refresh_seconds: float = 1.0,
) -> str:
    state = build_dashboard_state(
        title,
        metrics_df,
        cycles_df,
        decisions_df,
        snapshots_df,
        refresh_seconds=refresh_seconds,
    )
    safe_title = html.escape(str(state["title"]))
    initial_state_json = json.dumps(state, ensure_ascii=False)
    config_schemas_json = _dashboard_config_schemas_json()
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; background:#0f172a; color:#e2e8f0; margin:0; padding:24px; }}
    h1,h2,h3 {{ margin:0 0 12px; }}
    .muted {{ color:#94a3b8; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:16px; margin: 20px 0; }}
    .card {{ background:#111827; border:1px solid #1f2937; border-radius:12px; padding:16px; }}
    .card-label {{ color:#94a3b8; font-size:13px; margin-bottom:8px; }}
    .card-value {{ font-size:28px; font-weight:700; }}
    .panel {{ background:#111827; border:1px solid #1f2937; border-radius:12px; padding:16px; margin:16px 0; }}
    .panel-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap:16px; }}
    .alert-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:16px; margin:16px 0 20px; }}
    .alert-card {{ border-radius:12px; padding:16px; border:1px solid #334155; background:#111827; }}
    .alert-card.warning {{ border-color:#f59e0b; background:#1f1606; }}
    .alert-card.critical {{ border-color:#ef4444; background:#2a0d10; }}
    .alert-card.ok {{ border-color:#10b981; background:#062019; }}
    .alert-title {{ font-weight:700; margin-bottom:6px; }}
    .alert-message {{ color:#cbd5e1; line-height:1.5; }}
    .chart {{ width:100%; height:auto; background:#0b1220; border-radius:8px; }}
    .table {{ width:100%; border-collapse: collapse; font-size:13px; }}
    .table th,.table td {{ border-bottom:1px solid #1f2937; padding:8px; text-align:left; }}
    .table th {{ color:#93c5fd; }}
    .row-warning td {{ background:#2a2107; }}
    .row-danger td {{ background:#2a0d10; }}
    .meta {{ display:flex; gap:24px; flex-wrap:wrap; margin-top:8px; }}
    .live-status {{ margin-top:12px; font-size:13px; }}
    .config-toolbar {{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-top:12px; }}
    .config-toolbar select,.config-toolbar button,.config-input,.config-textarea {{
      background:#0b1220; color:#e2e8f0; border:1px solid #334155; border-radius:8px; padding:8px 10px; font:inherit;
    }}
    .config-toolbar button {{ cursor:pointer; }}
    .config-form {{ display:flex; flex-direction:column; gap:16px; margin-top:16px; }}
    .config-section {{ border:1px solid #1f2937; border-radius:12px; background:#0b1220; padding:14px; }}
    .config-section-title {{ font-size:16px; font-weight:700; color:#e2e8f0; margin-bottom:6px; }}
    .config-section-desc {{ color:#94a3b8; font-size:12px; margin-bottom:12px; line-height:1.5; }}
    .config-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:12px; }}
    .config-row {{ border:1px solid #1f2937; border-radius:10px; padding:12px; background:#111827; }}
    .config-label {{ color:#e2e8f0; font-weight:600; display:block; margin-bottom:6px; }}
    .config-label-row {{ display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:6px; }}
    .config-path {{ color:#64748b; font-size:12px; margin-bottom:6px; font-family: Consolas, monospace; }}
    .config-field-hint {{ color:#94a3b8; font-size:12px; line-height:1.5; margin-top:6px; }}
    .config-field-example {{ color:#a5b4fc; font-size:12px; line-height:1.5; margin-top:6px; }}
    .config-field-error {{ color:#fca5a5; font-size:12px; line-height:1.5; margin-top:6px; white-space:pre-wrap; }}
    .config-label-inline {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; flex:1; min-width:0; }}
    .config-label-inline-text {{ margin-bottom:0 !important; display:inline; }}
    .config-param-help {{ position:relative; display:inline-flex; align-items:center; flex-shrink:0; outline:none; }}
    .config-param-help-mark {{
      display:inline-flex; align-items:center; justify-content:center;
      width:18px; height:18px; border-radius:999px;
      background:#334155; color:#e2e8f0; font-size:11px; font-weight:700;
      cursor:help; line-height:1; user-select:none;
    }}
    .config-param-tooltip-panel {{
      display:none; position:absolute; left:0; top:calc(100% + 8px); z-index:60;
      min-width:240px; max-width:min(420px, 92vw);
      padding:10px 12px; background:#1e293b; color:#e2e8f0;
      border:1px solid #475569; border-radius:10px;
      box-shadow:0 12px 32px rgba(0,0,0,0.45);
      font-size:12px; font-weight:400; line-height:1.55; white-space:pre-wrap;
    }}
    .config-param-help:hover .config-param-tooltip-panel,
    .config-param-help:focus-within .config-param-tooltip-panel {{ display:block; }}
    .config-range-note {{ color:#93c5fd; font-size:12px; margin-top:6px; }}
    .config-risk-badge {{ font-size:11px; padding:3px 8px; border-radius:999px; white-space:nowrap; border:1px solid transparent; }}
    .config-risk-badge.low {{ color:#86efac; border-color:#166534; background:#052e16; }}
    .config-risk-badge.medium {{ color:#fde68a; border-color:#854d0e; background:#422006; }}
    .config-risk-badge.high {{ color:#fecaca; border-color:#991b1b; background:#450a0a; }}
    .config-input-stack {{ display:flex; flex-direction:column; gap:8px; }}
    .config-slider {{ width:100%; accent-color:#3b82f6; }}
    .config-select {{ width:100%; background:#0b1220; color:#e2e8f0; border:1px solid #334155; border-radius:8px; padding:8px 10px; font:inherit; box-sizing:border-box; }}
    .config-input,.config-textarea {{ width:100%; box-sizing:border-box; }}
    .config-textarea {{ min-height:88px; resize:vertical; }}
    .config-input.config-invalid,.config-textarea.config-invalid,.config-select.config-invalid {{ border-color:#ef4444; box-shadow:0 0 0 1px rgba(239,68,68,0.35); }}
    .config-hint {{ margin-top:12px; color:#94a3b8; font-size:12px; line-height:1.5; }}
    .config-status.ok {{ color:#86efac; }}
    .config-status.error {{ color:#fca5a5; }}
    .config-checkbox {{ width:18px; height:18px; accent-color:#3b82f6; }}
    .launcher-grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:16px; margin-top:16px; }}
    .launcher-card {{ background:#0b1220; border:1px solid #334155; border-radius:12px; padding:16px; display:flex; flex-direction:column; gap:10px; }}
    .launcher-title {{ font-size:16px; font-weight:700; }}
    .launcher-desc {{ color:#cbd5e1; line-height:1.6; }}
    .launcher-command {{ margin:0; white-space:pre-wrap; word-break:break-all; background:#020617; border:1px solid #1e293b; border-radius:8px; padding:10px; color:#93c5fd; font-size:12px; line-height:1.5; }}
    .launcher-actions {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:14px; }}
    .launcher-meta {{ color:#94a3b8; font-size:12px; line-height:1.6; }}
    .launcher-card-actions {{ display:flex; gap:10px; flex-wrap:wrap; }}
    .btn-danger {{ background:#991b1b; border-color:#7f1d1d; }}
    .btn-danger:hover {{ background:#b91c1c; }}
  </style>
</head>
<body>
  <h1 id="dashboard-title">{safe_title}</h1>
  <div class="meta muted">
    <div id="dashboard-latest-market">最新市场: {html.escape(str(state["latest_market"]))}</div>
    <div id="dashboard-latest-cycle">最新周期: {html.escape(str(state["latest_cycle"]))}</div>
    <div id="dashboard-total-cycles">周期数: {html.escape(str(state["total_cycles"]))}</div>
    <div id="dashboard-total-signals">信号数: {html.escape(str(state["total_signals"]))}</div>
  </div>
  <div id="dashboard-live-status" class="live-status muted">{html.escape(str(state["live_status_text"]))}</div>
  <div class="panel">
    <h2>参数控制台</h2>
    <div id="config-console-status" class="muted config-status">参数控制台：若通过本地控制服务打开，可在线修改并保存 `strategy / sweep / optimize` 配置。</div>
    <div class="config-toolbar">
      <select id="config-file-select">
        <option value="">选择配置文件</option>
      </select>
      <button id="config-reload-btn" type="button">刷新配置</button>
      <button id="config-save-btn" type="button">保存配置</button>
    </div>
    <div id="config-form" class="config-form"></div>
    <div class="config-hint">说明：`strategy.yaml` 可在 live runner 运行中热加载；`sweep.yaml` 和 `optimize.yaml` 会影响后续扫描/寻优任务，不会回溯修改已完成结果。每项下的「示例」为典型填法；标签旁「?」可悬停或按 Tab 聚焦查看扩展说明。</div>
  </div>
  <div class="panel">
    <h2>运行控制台</h2>
    <div id="launcher-status" class="config-status">正在连接运行控制台...</div>
    <div id="launcher-help" class="config-hint"></div>
    <div id="launcher-meta" class="launcher-meta"></div>
    <div id="launcher-profiles" class="launcher-grid"></div>
    <div class="launcher-actions">
      <button id="launcher-refresh-btn" type="button">刷新运行状态</button>
      <button id="launcher-stop-btn" type="button" class="btn-danger">停止当前任务</button>
    </div>
  </div>
  <div id="dashboard-cards" class="grid">{state["card_html"]}</div>
  <div class="panel">
    <h2>告警视图</h2>
    <div id="dashboard-alerts" class="alert-grid">{state["alert_html"]}</div>
  </div>
  <div class="panel-grid">
    <div class="panel">
      <h2>资金曲线</h2>
      <div id="dashboard-equity-curve">{state["equity_curve_html"]}</div>
    </div>
    <div class="panel">
      <h2>Up / Down 实时价格曲线</h2>
      <div id="dashboard-outcome-curve">{state["outcome_curve_html"]}</div>
    </div>
    <div class="panel">
      <h2>周期盈亏曲线</h2>
      <div id="dashboard-cycle-curve">{state["cycle_curve_html"]}</div>
    </div>
    <div class="panel">
      <h2>规则触发次数</h2>
      <div id="dashboard-signal-counts">{state["signal_counts_html"]}</div>
    </div>
    <div class="panel">
      <h2>规则实际执行次数</h2>
      <div id="dashboard-executed-counts">{state["executed_counts_html"]}</div>
    </div>
  </div>
  <div class="panel">
    <h2>最近决策</h2>
    <div id="dashboard-decisions">{state["decision_html"]}</div>
  </div>
  <div class="panel">
    <h2>最近周期</h2>
    <div id="dashboard-cycles">{state["cycle_html"]}</div>
  </div>
  <script>
    window.__POLYNET_DASHBOARD_STATE__ = {initial_state_json};
    function dashboardSetText(id, value) {{
      const node = document.getElementById(id);
      if (node) node.textContent = value || "";
    }}
    function dashboardSetHtml(id, value) {{
      const node = document.getElementById(id);
      if (node) node.innerHTML = value || "";
    }}
    function configEscapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}
    function applyDashboardState(state) {{
      if (!state) return;
      document.title = state.title || document.title;
      dashboardSetText("dashboard-title", state.title || "");
      dashboardSetText("dashboard-latest-market", "最新市场: " + (state.latest_market || ""));
      dashboardSetText("dashboard-latest-cycle", "最新周期: " + (state.latest_cycle || ""));
      dashboardSetText("dashboard-total-cycles", "周期数: " + (state.total_cycles || "0"));
      dashboardSetText("dashboard-total-signals", "信号数: " + (state.total_signals || "0"));
      dashboardSetText("dashboard-live-status", state.live_status_text || "");
      dashboardSetHtml("dashboard-cards", state.card_html || "");
      dashboardSetHtml("dashboard-alerts", state.alert_html || "");
      dashboardSetHtml("dashboard-equity-curve", state.equity_curve_html || "");
      dashboardSetHtml("dashboard-outcome-curve", state.outcome_curve_html || "");
      dashboardSetHtml("dashboard-cycle-curve", state.cycle_curve_html || "");
      dashboardSetHtml("dashboard-signal-counts", state.signal_counts_html || "");
      dashboardSetHtml("dashboard-executed-counts", state.executed_counts_html || "");
      dashboardSetHtml("dashboard-decisions", state.decision_html || "");
      dashboardSetHtml("dashboard-cycles", state.cycle_html || "");
    }}
    function reloadDashboardStateScript() {{
      const existing = document.getElementById("dashboard-state-script");
      if (existing) existing.remove();
      const script = document.createElement("script");
      script.id = "dashboard-state-script";
      script.src = "dashboard_state.js?ts=" + Date.now();
      script.onload = function() {{
        applyDashboardState(window.__POLYNET_DASHBOARD_STATE__);
      }};
      document.body.appendChild(script);
    }}
    const configConsoleState = {{
      available: false,
      currentName: "",
      currentData: null,
    }};
    const launcherState = {{
      profiles: [],
      draftOverrides: {{}},
      runningSnapshot: null,
    }};
    const CONFIG_SCHEMAS = {config_schemas_json};
    {_dashboard_config_param_meta_script()}    function setConfigStatus(message, level) {{
      const node = document.getElementById("config-console-status");
      if (!node) return;
      node.textContent = message || "";
      node.className = "muted config-status" + (level ? " " + level : "");
    }}
    function configFlatten(node, prefix) {{
      const rows = [];
      if (Array.isArray(node) || node === null || typeof node !== "object") {{
        rows.push({{ path: prefix, value: node }});
        return rows;
      }}
      Object.keys(node).forEach((key) => {{
        const nextPath = prefix ? prefix + "." + key : key;
        const value = node[key];
        if (value && typeof value === "object" && !Array.isArray(value)) {{
          rows.push(...configFlatten(value, nextPath));
        }} else {{
          rows.push({{ path: nextPath, value }});
        }}
      }});
      return rows;
    }}
    function configGetByPath(source, path) {{
      return path.split(".").reduce((node, key) => (node && typeof node === "object" ? node[key] : undefined), source);
    }}
    function configCollectUnknownFields(data, schema) {{
      const known = new Set();
      (schema.sections || []).forEach((section) => {{
        (section.fields || []).forEach((field) => known.add(field.path));
      }});
      return configFlatten(data, "").filter((row) => !known.has(row.path));
    }}
    function formatConfigValue(value) {{
      if (typeof value === "number") {{
        return Number.isInteger(value) ? String(value) : String(value);
      }}
      return String(value);
    }}
    function buildRangeHint(field) {{
      const parts = [];
      if (field.min !== undefined || field.max !== undefined) {{
        parts.push(`建议范围 ${{field.min ?? "-inf"}} ~ ${{field.max ?? "+inf"}}`);
      }}
      if (field.step !== undefined) {{
        parts.push(`步长 ${{field.step}}`);
      }}
      return parts.length ? `<div class="config-range-note">${{configEscapeHtml(parts.join("，"))}}</div>` : "";
    }}
    function buildRiskBadge(field) {{
      const risk = field.risk || "";
      if (!risk) return "";
      const mapping = {{
        low: "低风险",
        medium: "中风险",
        high: "高风险",
      }};
      return `<span class="config-risk-badge ${{configEscapeHtml(risk)}}">${{configEscapeHtml(mapping[risk] || risk)}}</span>`;
    }}
    function buildParamHelpExtra(field) {{
      if (!field.detail) return "";
      return `<span class="config-param-help" tabindex="0" aria-label="扩展说明"><span class="config-param-help-mark">?</span><span class="config-param-tooltip-panel" role="tooltip">${{configEscapeHtml(field.detail)}}</span></span>`;
    }}
    function configSetByPath(target, path, value) {{
      const parts = path.split(".");
      let node = target;
      for (let index = 0; index < parts.length - 1; index += 1) {{
        const key = parts[index];
        if (!node[key] || typeof node[key] !== "object" || Array.isArray(node[key])) {{
          node[key] = {{}};
        }}
        node = node[key];
      }}
      node[parts[parts.length - 1]] = value;
    }}
    function renderConfigInput(path, value, kindOverride) {{
      const safePath = configEscapeHtml(path);
      const kind = kindOverride || (typeof value === "boolean" ? "boolean" : typeof value === "number" ? "number" : Array.isArray(value) || (value && typeof value === "object") ? "json" : "string");
      if (kind === "boolean") {{
        return `<input class="config-checkbox" id="cfg-${{safePath}}" data-config-path="${{safePath}}" data-config-kind="boolean" type="checkbox" ${{value ? "checked" : ""}}>`;
      }}
      if (kind === "number") {{
        return `<input class="config-input" id="cfg-${{safePath}}" data-config-path="${{safePath}}" data-config-kind="number" type="number" step="any" value="${{Number(value ?? 0)}}">`;
      }}
      if (kind === "json") {{
        return `<textarea class="config-textarea" id="cfg-${{safePath}}" data-config-path="${{safePath}}" data-config-kind="json">${{configEscapeHtml(JSON.stringify(value ?? null, null, 2))}}</textarea>`;
      }}
      return `<input class="config-input" id="cfg-${{safePath}}" data-config-path="${{safePath}}" data-config-kind="string" type="text" value="${{configEscapeHtml(value ?? "")}}">`;
    }}
    function renderSchemaInput(field, value) {{
      const safePath = configEscapeHtml(field.path);
      if (field.ui === "select" && Array.isArray(field.options)) {{
        const optionKind = field.type === "number" ? "number" : "string";
        const options = field.options.map((option) => {{
          const optionValue = typeof option === "object" && option !== null ? option.value : option;
          const optionLabel = typeof option === "object" && option !== null ? option.label : option;
          const selected = String(optionValue) === String(value) ? "selected" : "";
          return `<option value="${{configEscapeHtml(optionValue)}}" ${{selected}}>${{configEscapeHtml(optionLabel)}}</option>`;
        }}).join("");
        return `<select class="config-select" id="cfg-${{safePath}}" data-config-path="${{safePath}}" data-config-kind="${{optionKind}}">${{options}}</select>`;
      }}
      if (field.ui === "range" && field.type === "number") {{
        const current = Number(value ?? field.min ?? 0);
        const min = field.min ?? 0;
        const max = field.max ?? 100;
        const step = field.step ?? 1;
        return `<div class="config-input-stack"><input class="config-slider" id="cfg-range-${{safePath}}" data-sync-number="cfg-${{safePath}}" type="range" min="${{min}}" max="${{max}}" step="${{step}}" value="${{current}}"><input class="config-input" id="cfg-${{safePath}}" data-config-path="${{safePath}}" data-config-kind="number" data-sync-range="cfg-range-${{safePath}}" type="number" min="${{min}}" max="${{max}}" step="${{step}}" value="${{current}}"></div>`;
      }}
      if (field.type === "number") {{
        return `<input class="config-input" id="cfg-${{safePath}}" data-config-path="${{safePath}}" data-config-kind="number" type="number" step="${{field.step ?? "any"}}" ${{field.min !== undefined ? `min="${{field.min}}"` : ""}} ${{field.max !== undefined ? `max="${{field.max}}"` : ""}} value="${{Number(value ?? 0)}}">`;
      }}
      return renderConfigInput(field.path, value, field.type);
    }}
    function renderConfigField(field, value) {{
      const resolved = enrichConfigSchemaField(field);
      const label = configEscapeHtml(resolved.label || resolved.path);
      const path = configEscapeHtml(resolved.path);
      const hint = resolved.hint ? `<div class="config-field-hint">${{configEscapeHtml(resolved.hint)}}</div>` : "";
      const example = resolved.example ? `<div class="config-field-example">示例：${{configEscapeHtml(resolved.example)}}</div>` : "";
      const errorSlot = `<div class="config-field-error" data-config-error-for="${{path}}"></div>`;
      const riskBadge = buildRiskBadge(resolved);
      const rangeHint = buildRangeHint(resolved);
      const helpExtra = buildParamHelpExtra(resolved);
      return `<div class="config-row"><div class="config-label-row"><div class="config-label-inline"><label class="config-label config-label-inline-text" for="cfg-${{path}}">${{label}}</label>${{helpExtra}}</div>${{riskBadge}}</div><div class="config-path">${{path}}</div>${{renderSchemaInput(resolved, value)}}${{errorSlot}}${{rangeHint}}${{hint}}${{example}}</div>`;
    }}
    function renderConfigSection(section, rowsHtml) {{
      return `<section class="config-section"><div class="config-section-title">${{configEscapeHtml(section.title || "")}}</div><div class="config-section-desc">${{configEscapeHtml(section.description || "")}}</div><div class="config-grid">${{rowsHtml}}</div></section>`;
    }}
    function renderConfigForm(configName, data) {{
      const form = document.getElementById("config-form");
      if (!form) return;
      const rows = configFlatten(data, "");
      if (!rows.length) {{
        form.innerHTML = "<div class=\\"muted\\">当前配置为空。</div>";
        return;
      }}
      const schema = CONFIG_SCHEMAS[configName] || {{ sections: [] }};
      const sections = [];
      (schema.sections || []).forEach((section) => {{
        const sectionRows = (section.fields || [])
          .filter((field) => configGetByPath(data, field.path) !== undefined)
          .map((field) => renderConfigField(field, configGetByPath(data, field.path)))
          .join("");
        if (sectionRows) {{
          sections.push(renderConfigSection(section, sectionRows));
        }}
      }});
      const unknownFields = configCollectUnknownFields(data, schema);
      if (unknownFields.length) {{
        const advancedRows = unknownFields
          .map((row) => renderConfigField({{
            path: row.path,
            label: row.path,
            hint: "未预设说明的高级参数，仍可直接按 YAML 路径编辑。",
            example: "保留当前 YAML 中该键已有取值，或对照策略代码/注释填写",
            detail: "该键未列入控制台标准表单。请确认路径与策略配置模型一致，类型错误可能导致加载或运行时报错。",
          }}, row.value))
          .join("");
        sections.push(renderConfigSection({{ title: "其他高级配置", description: "以下字段未预置为业务表单，但仍然可以继续编辑。" }}, advancedRows));
      }}
      form.innerHTML = sections.join("");
    }}
    async function fetchConfigJson(url, options) {{
      const response = await fetch(url, options);
      if (!response.ok) {{
        let message = response.status + " " + response.statusText;
        try {{
          const payload = await response.json();
          if (payload && payload.error) message = payload.error;
        }} catch (error) {{
        }}
        throw new Error(message);
      }}
      return response.json();
    }}
    async function loadConfigCatalog() {{
      const payload = await fetchConfigJson("/api/configs", {{ cache: "no-store" }});
      const select = document.getElementById("config-file-select");
      if (!select) return [];
      const options = (payload.configs || []).filter((item) => item && item.exists);
      select.innerHTML = options.map((item) => `<option value="${{configEscapeHtml(item.name)}}">${{configEscapeHtml(item.name)}} - ${{configEscapeHtml(item.path)}}</option>`).join("");
      if (options.length && !select.value) {{
        select.value = options[0].name;
      }}
      return options;
    }}
    async function loadConfig(name) {{
      const payload = await fetchConfigJson(`/api/config/${{name}}`, {{ cache: "no-store" }});
      configConsoleState.available = true;
      configConsoleState.currentName = payload.name || name;
      configConsoleState.currentData = payload.data || {{}};
      renderConfigForm(configConsoleState.currentName, configConsoleState.currentData);
      setConfigStatus(`已载入 ${{configConsoleState.currentName}}，保存后会直接写回 YAML。`, "ok");
    }}
    function clearConfigFieldError(path) {{
      if (!path) return;
      document.querySelectorAll("#config-form [data-config-path]").forEach((node) => {{
        if ((node.getAttribute("data-config-path") || "") !== path) return;
        node.classList.remove("config-invalid");
      }});
      document.querySelectorAll("#config-form [data-config-error-for]").forEach((node) => {{
        if ((node.getAttribute("data-config-error-for") || "") !== path) return;
        node.textContent = "";
      }});
    }}
    function clearAllConfigFieldErrors() {{
      document.querySelectorAll("#config-form .config-invalid").forEach((node) => node.classList.remove("config-invalid"));
      document.querySelectorAll("#config-form [data-config-error-for]").forEach((node) => (node.textContent = ""));
    }}
    function setConfigFieldError(path, message) {{
      if (!path) return;
      document.querySelectorAll("#config-form [data-config-path]").forEach((node) => {{
        if ((node.getAttribute("data-config-path") || "") !== path) return;
        node.classList.add("config-invalid");
      }});
      document.querySelectorAll("#config-form [data-config-error-for]").forEach((node) => {{
        if ((node.getAttribute("data-config-error-for") || "") !== path) return;
        node.textContent = message || "";
      }});
    }}
    function readConfigFormWithValidation() {{
      const out = {{}};
      const errors = [];
      document.querySelectorAll("#config-form [data-config-path]").forEach((node) => {{
        const path = node.getAttribute("data-config-path") || "";
        const kind = node.getAttribute("data-config-kind") || "string";
        let value;
        if (kind === "boolean") {{
          value = Boolean(node.checked);
        }} else if (kind === "number") {{
          const raw = String(node.value ?? "");
          if (!raw.trim()) {{
            errors.push({{ path, message: "数值不能为空" }});
            return;
          }}
          const parsed = Number(raw);
          if (!Number.isFinite(parsed)) {{
            errors.push({{ path, message: `数值非法: ${{raw}}` }});
            return;
          }}
          value = parsed;
        }} else if (kind === "json") {{
          try {{
            value = JSON.parse(node.value || "null");
          }} catch (error) {{
            const detail = error && error.message ? error.message : String(error);
            errors.push({{ path, message: `JSON 解析失败: ${{detail}}` }});
            return;
          }}
        }} else {{
          value = node.value;
        }}
        configSetByPath(out, path, value);
      }});
      return {{ data: out, errors }};
    }}
    async function saveConfig() {{
      if (!configConsoleState.currentName) {{
        setConfigStatus("请先选择一个配置文件。", "error");
        return;
      }}
      try {{
        clearAllConfigFieldErrors();
        const result = readConfigFormWithValidation();
        const data = result.data;
        const errors = result.errors || [];
        if (errors.length) {{
          errors.slice(0, 30).forEach((item) => setConfigFieldError(item.path, item.message));
          const summary = errors
            .slice(0, 8)
            .map((item) => `- ${{item.path}}：${{item.message}}`)
            .join("\\n");
          const more = errors.length > 8 ? `\\n... 另有 ${{errors.length - 8}} 项错误` : "";
          setConfigStatus(`保存失败：表单存在 ${{errors.length}} 项错误，请先修复。\\n${{summary}}${{more}}`, "error");
          return;
        }}
        await fetchConfigJson(`/api/config/${{configConsoleState.currentName}}`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ data }}),
        }});
        configConsoleState.currentData = data;
        setConfigStatus(`已保存 ${{configConsoleState.currentName}}。若当前在跑 live runner，strategy 配置会在下一批事件前自动热加载。`, "ok");
      }} catch (error) {{
        setConfigStatus(`保存失败: ${{error.message}}`, "error");
      }}
    }}
    function setLauncherStatus(message, tone) {{
      const node = document.getElementById("launcher-status");
      if (!node) return;
      node.textContent = message || "";
      node.className = "config-status" + (tone ? ` ${{tone}}` : "");
    }}
    function getLauncherDraftValue(profileName, fieldName, fallbackValue) {{
      const profileDraft = launcherState.draftOverrides[profileName];
      if (profileDraft && Object.prototype.hasOwnProperty.call(profileDraft, fieldName)) {{
        return profileDraft[fieldName];
      }}
      return fallbackValue;
    }}
    function renderLauncherField(profile, field) {{
      const profileName = configEscapeHtml(profile.name || "");
      const fieldName = configEscapeHtml(field.name || "");
      const draftValue = getLauncherDraftValue(profile.name || "", field.name || "", field.value);
      const label = configEscapeHtml(field.label || field.name || "");
      const helpExtra = buildParamHelpExtra(field);
      const labelRow = `<div class="config-label-row"><div class="config-label-inline"><span class="config-label config-label-inline-text">${{label}}</span>${{helpExtra}}</div></div>`;
      const notes = [];
      if (field.description) notes.push(`<div class="config-field-hint">${{configEscapeHtml(field.description)}}</div>`);
      if (field.example) notes.push(`<div class="config-field-example">示例：${{configEscapeHtml(field.example)}}</div>`);
      if (field.saved_default !== undefined && field.saved_default !== null) {{
        notes.push(`<div class="config-field-hint">默认值：${{configEscapeHtml(field.saved_default)}}</div>`);
      }}
      if (field.last_used !== undefined && field.last_used !== null) {{
        notes.push(`<div class="config-field-hint">最近启动：${{configEscapeHtml(field.last_used)}}</div>`);
      }}
      const desc = notes.join("");
      const min = field.min !== undefined && field.min !== null ? `min="${{field.min}}"` : "";
      const max = field.max !== undefined && field.max !== null ? `max="${{field.max}}"` : "";
      const step = field.step !== undefined && field.step !== null ? `step="${{field.step}}"` : "";
      if (field.kind === "select" && Array.isArray(field.options)) {{
        const options = field.options.map((option) => {{
          const value = configEscapeHtml(option);
          const selected = String(option) === String(draftValue) ? "selected" : "";
          return `<option value="${{value}}" ${{selected}}>${{value}}</option>`;
        }}).join("");
        return `<div class="config-row">${{labelRow}}<select class="config-select" data-launch-profile="${{profileName}}" data-launch-field="${{fieldName}}">${{options}}</select>${{desc}}</div>`;
      }}
      if (field.kind === "checkbox") {{
        const checked = draftValue === true || draftValue === "true" || draftValue === 1 ? "checked" : "";
        return `<div class="config-row">${{labelRow}}<input class="config-checkbox" type="checkbox" ${{checked}} data-launch-profile="${{profileName}}" data-launch-field="${{fieldName}}">${{desc}}</div>`;
      }}
      if (field.kind === "number") {{
        // 用 type="text" 避免浏览器对 type="number"（min/max/step）的一些强校验导致你无法稳定输入。
        return `<div class="config-row">${{labelRow}}<input class="config-input" type="text" inputmode="numeric" autocomplete="off" value="${{configEscapeHtml(draftValue)}}" data-launch-profile="${{profileName}}" data-launch-field="${{fieldName}}">${{buildRangeHint(field)}}${{desc}}</div>`;
      }}
      return `<div class="config-row">${{labelRow}}<input class="config-input" type="text" value="${{configEscapeHtml(draftValue ?? "")}}" data-launch-profile="${{profileName}}" data-launch-field="${{fieldName}}">${{desc}}</div>`;
    }}
    function collectLauncherOverrides(profileName) {{
      const overrides = {{}};
      document.querySelectorAll(`[data-launch-profile="${{profileName}}"][data-launch-field]`).forEach((node) => {{
        const fieldName = node.getAttribute("data-launch-field");
        if (!fieldName) return;
        // 对于 checkbox，使用 .checked；否则用 .value
        overrides[fieldName] = node.type === "checkbox" ? node.checked : node.value;
      }});
      return overrides;
    }}
    function updateLauncherDraft(profileName, fieldName, value) {{
      if (!profileName || !fieldName) return;
      if (!launcherState.draftOverrides[profileName]) {{
        launcherState.draftOverrides[profileName] = {{}};
      }}
      launcherState.draftOverrides[profileName][fieldName] = value;
    }}
    function replaceLauncherDraftWithCatalog(profiles) {{
      const nextDrafts = {{}};
      (profiles || []).forEach((profile) => {{
        const profileName = profile.name || "";
        if (!profileName) return;
        const currentDraft = launcherState.draftOverrides[profileName] || {{}};
        const fieldDraft = {{}};
        (profile.fields || []).forEach((field) => {{
          const fieldName = field.name || "";
          if (!fieldName) return;
          if (Object.prototype.hasOwnProperty.call(currentDraft, fieldName)) {{
            fieldDraft[fieldName] = currentDraft[fieldName];
          }} else {{
            fieldDraft[fieldName] = field.value;
          }}
        }});
        nextDrafts[profileName] = fieldDraft;
      }});
      launcherState.draftOverrides = nextDrafts;
    }}
    function renderLauncherProfiles(profiles, status) {{
      const container = document.getElementById("launcher-profiles");
      if (!container) return;
      launcherState.profiles = profiles || [];
      const running = Boolean(status && status.running);
      container.innerHTML = (profiles || []).map((profile) => {{
        const disabled = running ? "disabled" : "";
        const fieldHtml = (profile.fields || []).map((field) => renderLauncherField(profile, field)).join("");
        return `<div class="launcher-card">
          <div class="launcher-title">${{configEscapeHtml(profile.title || profile.name || "")}}</div>
          <div class="launcher-desc">${{configEscapeHtml(profile.description || "")}}</div>
          <div class="config-grid">${{fieldHtml}}</div>
          <pre class="launcher-command">${{configEscapeHtml(profile.command_text || "")}}</pre>
          <div class="launcher-card-actions">
            <button type="button" class="launcher-start-btn" data-launch-profile="${{configEscapeHtml(profile.name || "")}}" ${{disabled}}>启动这个任务</button>
            <button type="button" class="launcher-save-defaults-btn" data-launch-save-profile="${{configEscapeHtml(profile.name || "")}}" ${{disabled}}>保存为默认值</button>
          </div>
        </div>`;
      }}).join("");
    }}
    function renderLauncherMeta(status, helpCommand, preferencesPath) {{
      const helpNode = document.getElementById("launcher-help");
      const metaNode = document.getElementById("launcher-meta");
      const stopButton = document.getElementById("launcher-stop-btn");
      if (helpNode) {{
        const helpLines = [];
        if (helpCommand) {{
          helpLines.push(`项目专用命令帮助：<code>${{configEscapeHtml(helpCommand)}}</code>`);
        }}
        if (preferencesPath) {{
          helpLines.push(`参数偏好文件：<code>${{configEscapeHtml(preferencesPath)}}</code>`);
        }}
        helpNode.innerHTML = helpLines.join("<br>");
      }}
      if (metaNode) {{
        const statusLines = [];
        if (status && status.command_text) {{
          statusLines.push(`当前命令：<code>${{configEscapeHtml(status.command_text)}}</code>`);
        }}
        if (status && status.log_path) {{
          statusLines.push(`运行日志：<code>${{configEscapeHtml(status.log_path)}}</code>`);
        }}
        metaNode.innerHTML = statusLines.join("<br>");
      }}
      if (stopButton) {{
        stopButton.disabled = !(status && status.running);
      }}
    }}
    async function loadLauncherCatalog() {{
      return fetchConfigJson("/api/launcher", {{ cache: "no-store" }});
    }}
    async function refreshLauncherStatus() {{
      return fetchConfigJson("/api/launcher/status", {{ cache: "no-store" }});
    }}
    async function startLauncherProfile(profileName, overrides) {{
      return fetchConfigJson("/api/launcher/start", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ profile: profileName, overrides: overrides || {{}} }}),
      }});
    }}
    async function saveLauncherDefaults(profileName, overrides) {{
      return fetchConfigJson("/api/launcher/defaults", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ profile: profileName, overrides: overrides || {{}} }}),
      }});
    }}
    async function stopLauncherProfile() {{
      return fetchConfigJson("/api/launcher/stop", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{}}),
      }});
    }}
    async function initializeRunConsole() {{
      const profilesNode = document.getElementById("launcher-profiles");
      const refreshButton = document.getElementById("launcher-refresh-btn");
      const stopButton = document.getElementById("launcher-stop-btn");
      if (profilesNode) {{
        profilesNode.addEventListener("input", function(event) {{
          const target = event.target;
          if (!target) return;
          const profileName = target.getAttribute("data-launch-profile");
          const fieldName = target.getAttribute("data-launch-field");
          if (!profileName || !fieldName) return;
          // 处理 checkbox 的 .checked，其他类型用 .value
          const value = target.type === "checkbox" ? target.checked : target.value;
          updateLauncherDraft(profileName, fieldName, value);
        }});
        // 为 checkbox 添加 change 事件监听，确保 checkbox 状态改变时也能被捕获
        profilesNode.addEventListener("change", function(event) {{
          const target = event.target;
          if (!target || target.type !== "checkbox") return;
          const profileName = target.getAttribute("data-launch-profile");
          const fieldName = target.getAttribute("data-launch-field");
          if (!profileName || !fieldName) return;
          updateLauncherDraft(profileName, fieldName, target.checked);
        }});
        profilesNode.addEventListener("click", async function(event) {{
          const target = event.target;
          if (!target) return;
          const isStartButton = target.classList && target.classList.contains("launcher-start-btn");
          const isSaveDefaultsButton = target.classList && target.classList.contains("launcher-save-defaults-btn");
          const profileName = target.getAttribute("data-launch-profile");
          const saveProfileName = target.getAttribute("data-launch-save-profile");
          if (isStartButton && profileName) {{
            const overrides = collectLauncherOverrides(profileName);
            setLauncherStatus(`正在启动 ${{profileName}} ...`, "");
            try {{
              const payload = await startLauncherProfile(profileName, overrides);
              if (launcherState.draftOverrides[profileName]) {{
                delete launcherState.draftOverrides[profileName];
              }}
              renderLauncherMeta(payload.status || {{}}, "", "");
              setLauncherStatus(
                payload.status && payload.status.running
                  ? `任务已启动：${{payload.status.profile_title || profileName}} (PID ${{payload.status.pid || "-"}})`
                  : "启动请求已完成。",
                "ok",
              );
              const catalog = await loadLauncherCatalog();
              renderLauncherProfiles(catalog.profiles || [], catalog.status || {{}});
              renderLauncherMeta(catalog.status || {{}}, catalog.help_command || "", catalog.preferences_path || "");
            }} catch (error) {{
              setLauncherStatus(`启动失败: ${{error.message}}`, "error");
            }}
            return;
          }}
          if (isSaveDefaultsButton && saveProfileName) {{
            const overrides = collectLauncherOverrides(saveProfileName);
            setLauncherStatus(`正在保存 ${{saveProfileName}} 默认值...`, "");
            try {{
              await saveLauncherDefaults(saveProfileName, overrides);
              if (launcherState.draftOverrides[saveProfileName]) {{
                delete launcherState.draftOverrides[saveProfileName];
              }}
              const catalog = await loadLauncherCatalog();
              replaceLauncherDraftWithCatalog(catalog.profiles || []);
              renderLauncherProfiles(catalog.profiles || [], catalog.status || {{}});
              renderLauncherMeta(catalog.status || {{}}, catalog.help_command || "", catalog.preferences_path || "");
              setLauncherStatus("默认值已保存，下次打开页面会自动回填。", "ok");
            }} catch (error) {{
              setLauncherStatus(`保存默认值失败: ${{error.message}}`, "error");
            }}
          }}
        }});
      }}
      if (refreshButton) {{
        refreshButton.addEventListener("click", async function() {{
          try {{
            const payload = await loadLauncherCatalog();
            replaceLauncherDraftWithCatalog(payload.profiles || []);
            renderLauncherProfiles(payload.profiles || [], payload.status || {{}});
            renderLauncherMeta(payload.status || {{}}, payload.help_command || "", payload.preferences_path || "");
            if (payload.status && payload.status.running) {{
              setLauncherStatus(`正在运行：${{payload.status.profile_title || payload.status.profile_name || ""}}`, "ok");
            }} else if (payload.status && payload.status.last_exit_code !== null && payload.status.last_exit_code !== undefined) {{
              setLauncherStatus(`当前无任务运行，上次退出码: ${{payload.status.last_exit_code}}`, payload.status.last_exit_code === 0 ? "ok" : "error");
            }} else {{
              setLauncherStatus("当前无任务运行。", "");
            }}
          }} catch (error) {{
            setLauncherStatus(`刷新失败: ${{error.message}}`, "error");
          }}
        }});
      }}
      if (stopButton) {{
        stopButton.addEventListener("click", async function() {{
          setLauncherStatus("正在停止当前任务...", "");
          try {{
            const payload = await stopLauncherProfile();
            renderLauncherMeta(payload.status || {{}}, "", "");
            setLauncherStatus("当前任务已停止。", "ok");
            const catalog = await loadLauncherCatalog();
            renderLauncherProfiles(catalog.profiles || [], catalog.status || {{}});
            renderLauncherMeta(catalog.status || {{}}, catalog.help_command || "", catalog.preferences_path || "");
          }} catch (error) {{
            setLauncherStatus(`停止失败: ${{error.message}}`, "error");
          }}
        }});
      }}
      try {{
        const payload = await loadLauncherCatalog();
        replaceLauncherDraftWithCatalog(payload.profiles || []);
        renderLauncherProfiles(payload.profiles || [], payload.status || {{}});
        renderLauncherMeta(payload.status || {{}}, payload.help_command || "", payload.preferences_path || "");
        launcherState.runningSnapshot = Boolean(payload.status && payload.status.running);
        if (payload.status && payload.status.running) {{
          setLauncherStatus(`正在运行：${{payload.status.profile_title || payload.status.profile_name || ""}}`, "ok");
        }} else {{
          setLauncherStatus("已自动回填默认值或最近一次启动参数，可直接启动。", "");
        }}
        setInterval(async function() {{
          try {{
            const status = await refreshLauncherStatus();
            renderLauncherMeta(status || {{}}, payload.help_command || "", payload.preferences_path || "");
            const runningNow = Boolean(status && status.running);
            const shouldReloadCatalog = launcherState.runningSnapshot !== runningNow;
            if (shouldReloadCatalog) {{
              // 不在轮询期间重建 profiles 卡片，避免覆盖用户正在编辑的输入值。
              // 启动/停止按钮点击后会显式刷新需要更新的 UI。
              launcherState.runningSnapshot = runningNow;
            }}
            if (status && status.running) {{
              setLauncherStatus(`正在运行：${{status.profile_title || status.profile_name || ""}}`, "ok");
            }} else if (launcherState.runningSnapshot === false) {{
              if (status && status.last_exit_code !== null && status.last_exit_code !== undefined) {{
                setLauncherStatus(
                  `当前无任务运行，上次退出码: ${{status.last_exit_code}}`,
                  status.last_exit_code === 0 ? "ok" : "error",
                );
              }} else {{
                setLauncherStatus("当前无任务运行。", "");
              }}
            }}
          }} catch (error) {{
          }}
        }}, 3000);
      }} catch (error) {{
        setLauncherStatus("运行控制台未连接。请通过本地 dashboard 控制台打开当前页面。", "error");
      }}
    }}
    async function initializeConfigConsole() {{
      const select = document.getElementById("config-file-select");
      const reloadButton = document.getElementById("config-reload-btn");
      const saveButton = document.getElementById("config-save-btn");
      const form = document.getElementById("config-form");
      if (form) {{
        form.addEventListener("input", function(event) {{
          const target = event.target;
          if (!target) return;
          const configPath = target.getAttribute && target.getAttribute("data-config-path");
          if (configPath) {{
            clearConfigFieldError(configPath);
          }}
          const numberId = target.getAttribute("data-sync-number");
          if (numberId) {{
            const numberNode = document.getElementById(numberId);
            if (numberNode) {{
              numberNode.value = target.value;
              const linkedPath = numberNode.getAttribute("data-config-path") || "";
              if (linkedPath) clearConfigFieldError(linkedPath);
            }}
          }}
          const rangeId = target.getAttribute("data-sync-range");
          if (rangeId) {{
            const rangeNode = document.getElementById(rangeId);
            if (rangeNode) {{
              rangeNode.value = target.value;
              const linkedPath = target.getAttribute("data-config-path") || "";
              if (linkedPath) clearConfigFieldError(linkedPath);
            }}
          }}
        }});
      }}
      if (select) {{
        select.addEventListener("change", function() {{
          if (select.value) {{
            loadConfig(select.value).catch((error) => setConfigStatus(`读取配置失败: ${{error.message}}`, "error"));
          }}
        }});
      }}
      if (reloadButton) {{
        reloadButton.addEventListener("click", function() {{
          if (select && select.value) {{
            loadConfig(select.value).catch((error) => setConfigStatus(`读取配置失败: ${{error.message}}`, "error"));
          }}
        }});
      }}
      if (saveButton) {{
        saveButton.addEventListener("click", function() {{
          saveConfig();
        }});
      }}
      try {{
        const configs = await loadConfigCatalog();
        if (!configs.length) {{
          setConfigStatus("未发现可编辑配置文件。", "error");
          return;
        }}
        if (select && select.value) {{
          await loadConfig(select.value);
        }}
      }} catch (error) {{
        setConfigStatus("参数控制台未连接。请使用 `python scripts/run_dashboard_console.py --dashboard-dir <输出目录>` 通过 http://localhost 打开 dashboard。", "error");
      }}
    }}
    applyDashboardState(window.__POLYNET_DASHBOARD_STATE__);
    setInterval(reloadDashboardStateScript, Math.max(250, Number(window.__POLYNET_DASHBOARD_STATE__.refresh_seconds || 1) * 1000));
    initializeConfigConsole();
    initializeRunConsole();
  </script>
</body>
</html>
"""


def build_daily_markdown_report(
    title: str,
    metrics_df: pd.DataFrame,
    cycles_df: pd.DataFrame,
    decisions_df: pd.DataFrame,
    snapshots_df: pd.DataFrame,
) -> str:
    summary = _build_summary_frame(metrics_df, cycles_df, decisions_df, snapshots_df).iloc[0].to_dict()
    alerts = _build_alerts(summary, cycles_df, decisions_df)
    worst_cycle = ""
    best_cycle = ""
    if not cycles_df.empty and "cycle_net_profit" in cycles_df.columns:
        ordered = cycles_df.sort_values(by="cycle_net_profit", ascending=False)
        best_cycle = f"{ordered.iloc[0].get('cycle_id', '')}: {_format_value(ordered.iloc[0].get('cycle_net_profit', 0.0))}"
        worst_cycle = f"{ordered.iloc[-1].get('cycle_id', '')}: {_format_value(ordered.iloc[-1].get('cycle_net_profit', 0.0))}"
    selected_rules = _extract_rule_counts(metrics_df, "selected_rule_")[:5]
    executed_rules = _extract_rule_counts(metrics_df, "executed_rule_")[:5]
    lines = [
        f"# {title}",
        "",
        "## 核心指标",
        f"- 总净利润: {_format_value(summary.get('total_net_profit', 0.0))}",
        f"- 最大回撤: {_format_value(summary.get('max_drawdown', 0.0))}",
        f"- 胜率: {_format_value(float(summary.get('win_rate', 0.0)) * 100, 2)}%",
        f"- 总信号数: {_format_value(summary.get('total_signals', 0), 0)}",
        f"- 已执行交易: {_format_value(summary.get('executed_trades', 0), 0)}",
        f"- 信号执行率: {_format_value(float(summary.get('signal_execution_rate', 0.0)) * 100, 2)}%",
        f"- 最新现金: {_format_value(summary.get('latest_cash', 0.0))}",
        f"- 最新净持仓: {_format_value(summary.get('latest_net_position', 0.0))}",
        "",
        "## 告警视图",
    ]
    if alerts:
        for alert in alerts:
            lines.append(f"- [{alert.severity}] {alert.title}: {alert.message}")
    else:
        lines.append("- 当前未触发主动告警。")
    lines.extend([
        "",
        "## 周期观察",
        f"- 最佳周期: {best_cycle or 'N/A'}",
        f"- 最差周期: {worst_cycle or 'N/A'}",
        f"- 最新市场: {summary.get('latest_market', '')}",
        f"- 最新周期: {summary.get('latest_cycle', '')}",
        "",
        "## 规则触发 Top5",
    ])
    for name, value in selected_rules:
        lines.append(f"- {name}: {_format_value(value, 0)}")
    lines.extend(["", "## 规则执行 Top5"])
    for name, value in executed_rules:
        lines.append(f"- {name}: {_format_value(value, 0)}")
    return "\n".join(lines) + "\n"


def generate_dashboard_bundle(
    metrics_df: pd.DataFrame,
    cycles_df: pd.DataFrame,
    decisions_df: pd.DataFrame,
    snapshots_df: pd.DataFrame,
    output_dir: str | Path,
    title: str = "Polynet AI Monitoring Dashboard",
    refresh_seconds: float = 1.0,
) -> DashboardArtifacts:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    html_path = directory / "dashboard.html"
    markdown_path = directory / "daily_report.md"
    summary_csv_path = directory / "dashboard_summary.csv"
    state_script_path = directory / "dashboard_state.js"
    dashboard_state = build_dashboard_state(
        title,
        metrics_df,
        cycles_df,
        decisions_df,
        snapshots_df,
        refresh_seconds=refresh_seconds,
    )
    html_path.write_text(
        build_dashboard_html(title, metrics_df, cycles_df, decisions_df, snapshots_df, refresh_seconds=refresh_seconds),
        encoding="utf-8",
    )
    state_script_path.write_text(_build_dashboard_state_script(dashboard_state), encoding="utf-8")
    markdown_path.write_text(
        build_daily_markdown_report(title, metrics_df, cycles_df, decisions_df, snapshots_df),
        encoding="utf-8",
    )
    summary_df = _build_summary_frame(metrics_df, cycles_df, decisions_df, snapshots_df)
    alerts = _build_alerts(summary_df.iloc[0].to_dict(), cycles_df, decisions_df)
    alert_columns = {
        "alert_count": len(alerts),
        "has_critical_alert": any(alert.severity == "critical" for alert in alerts),
        "has_warning_alert": any(alert.severity == "warning" for alert in alerts),
        "alert_codes": "|".join(alert.code for alert in alerts),
    }
    for key, value in alert_columns.items():
        summary_df[key] = value
    summary_df.to_csv(
        summary_csv_path,
        index=False,
        encoding="utf-8-sig",
    )
    return DashboardArtifacts(
        html_path=html_path,
        markdown_path=markdown_path,
        summary_csv_path=summary_csv_path,
        state_script_path=state_script_path,
    )


def refresh_dashboard_html_shell(
    output_dir: str | Path,
    title: str = "Polynet AI Monitoring Dashboard",
    refresh_seconds: float = 1.0,
) -> DashboardArtifacts:
    """Write dashboard.html / dashboard_state.js / daily_report.md / summary csv with empty data.

    Use when the target folder has no live ``metrics.csv`` (e.g. ``batch_replay_outputs``) but you
    need an up-to-date HTML shell for the config console and launcher UI.
    """
    empty = pd.DataFrame()
    return generate_dashboard_bundle(
        empty,
        empty,
        empty,
        empty,
        output_dir,
        title=title,
        refresh_seconds=refresh_seconds,
    )


def generate_dashboard_from_directory(
    input_dir: str | Path,
    output_dir: str | Path | None = None,
    title: str = "Polynet AI Monitoring Dashboard",
) -> DashboardArtifacts:
    directory = Path(input_dir)
    metrics_path = directory / "metrics.csv"
    if not metrics_path.is_file():
        hint_dir = Path(output_dir).resolve() if output_dir else directory.resolve()
        raise FileNotFoundError(
            f"找不到 {metrics_path.resolve()}。该路径应是 live/单次回放输出根目录（根下有 metrics.csv、"
            "cycles.csv、decisions.csv）。\n"
            "batch_replay_outputs 等目录只有 batch_replay_summary 等文件，没有上述 CSV。\n"
            "若只需刷新 dashboard 页面外壳（参数控制台、运行控制台），请改用：\n"
            f'  python scripts/run_dashboard_report.py --html-only --output-dir "{hint_dir}"'
        )
    target = Path(output_dir) if output_dir else directory
    metrics_df = pd.read_csv(metrics_path)
    cycles_df = pd.read_csv(directory / "cycles.csv")
    decisions_df = pd.read_csv(directory / "decisions.csv")
    snapshot_path = directory / "snapshots.csv"
    snapshots_df = pd.read_csv(snapshot_path) if snapshot_path.exists() else pd.DataFrame()
    return generate_dashboard_bundle(metrics_df, cycles_df, decisions_df, snapshots_df, target, title=title)
