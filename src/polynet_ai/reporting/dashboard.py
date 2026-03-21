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
        return f"{float(value):,.{digits}f}"
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
        cells = "".join(f"<td>{html.escape(str(value))}</td>" for value in row.tolist())
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
    <div class="config-hint">说明：`strategy.yaml` 可在 live runner 运行中热加载；`sweep.yaml` 和 `optimize.yaml` 会影响后续扫描/寻优任务，不会回溯修改已完成结果。</div>
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
    }};
    const CONFIG_SCHEMAS = {{
      strategy: {{
        sections: [
          {{
            title: "周期与节奏",
            description: "控制一个交易周期多长、最后一分钟风控何时介入。",
            fields: [
              {{ path: "cycle.cycle_seconds", label: "周期长度（秒）", hint: "BTC 5 分钟市场通常为 300 秒。", type: "number", ui: "select", options: [300], risk: "low" }},
              {{ path: "cycle.last_minute_seconds", label: "尾盘窗口（秒）", hint: "最后一分钟逻辑开始生效的时间。", type: "number", ui: "select", options: [30, 45, 60, 75, 90], risk: "medium" }},
            ],
          }},
          {{
            title: "下单规模",
            description: "基础单量、波动放大和单笔上下限。",
            fields: [
              {{ path: "order_sizing.base_order_size", label: "基础下单量", hint: "大多数规则的起始仓位大小。", type: "number", ui: "range", min: 1, max: 20, step: 1, risk: "medium" }},
              {{ path: "order_sizing.min_order_size", label: "最小下单量", hint: "低于该值的订单会被拒绝。", type: "number", min: 0.5, max: 10, step: 0.5, risk: "low" }},
              {{ path: "order_sizing.max_order_size", label: "最大下单量", hint: "单笔订单的硬上限。", type: "number", ui: "range", min: 5, max: 200, step: 1, risk: "high" }},
              {{ path: "order_sizing.volatility_order_scale", label: "波动率加仓系数", hint: "波动越大，基础下单量放大越明显。", type: "number", ui: "range", min: 0, max: 60, step: 1, risk: "medium" }},
            ],
          }},
          {{
            title: "资金与本金",
            description: "控制可用现金比例与安全缓冲，防止超过本金能力下单。",
            fields: [
              {{ path: "capital.max_cash_utilization", label: "最大资金使用率", hint: "例如 0.95 表示最多动用 95% 的现金。", type: "number", ui: "range", min: 0.50, max: 1.00, step: 0.01, risk: "high" }},
              {{ path: "capital.min_cash_buffer", label: "最小现金缓冲", hint: "始终保留的现金安全垫。", type: "number", ui: "range", min: 0, max: 200, step: 5, risk: "medium" }},
            ],
          }},
          {{
            title: "敞口与风控",
            description: "限制最大净敞口、网格净仓位和单周期交易次数。",
            fields: [
              {{ path: "exposure.max_abs_exposure", label: "最大绝对敞口", hint: "限制净敞口价值上限。", type: "number", ui: "range", min: 20, max: 1000, step: 10, risk: "high" }},
              {{ path: "exposure.hedge_trigger_value", label: "对冲触发阈值", hint: "超过该敞口后可触发对冲逻辑。", type: "number", ui: "range", min: 10, max: 300, step: 5, risk: "medium" }},
              {{ path: "exposure.hedge_scale", label: "对冲强度系数", hint: "超额敞口换算成对冲单量的比例。", type: "number", ui: "range", min: 0.01, max: 1.00, step: 0.01, risk: "medium" }},
              {{ path: "exposure.max_grid_net_position", label: "网格最大净仓位", hint: "防止区间策略滚成单边大仓。", type: "number", ui: "range", min: 2, max: 100, step: 1, risk: "high" }},
              {{ path: "exposure.max_strategy_trades_per_cycle", label: "每周期最大成交次数", hint: "限制单个周期内的策略交易频率。", type: "number", ui: "range", min: 1, max: 50, step: 1, risk: "medium" }},
            ],
          }},
          {{
            title: "趋势与网格",
            description: "趋势追随和区间网格的关键阈值。",
            fields: [
              {{ path: "trend.min_trend_strength", label: "最小趋势强度", hint: "低于该值则不触发趋势单。", type: "number", ui: "range", min: 0.10, max: 0.80, step: 0.01, risk: "medium" }},
              {{ path: "trend.trend_price_edge", label: "趋势价格边际", hint: "价格偏离需超过该值才追随趋势。", type: "number", ui: "range", min: 0.00, max: 0.30, step: 0.01, risk: "medium" }},
              {{ path: "trend.trend_scale", label: "趋势加仓系数", hint: "当前净仓越大，趋势单会按此系数放大。", type: "number", ui: "range", min: 0.00, max: 1.00, step: 0.01, risk: "high" }},
              {{ path: "grid.grid_low_percentile", label: "网格低位分位数", hint: "低于该分位视作区间低位。", type: "number", ui: "range", min: 0.05, max: 0.50, step: 0.01, risk: "low" }},
              {{ path: "grid.grid_high_percentile", label: "网格高位分位数", hint: "高于该分位视作区间高位。", type: "number", ui: "range", min: 0.50, max: 0.95, step: 0.01, risk: "low" }},
            ],
          }},
          {{
            title: "均值回归与止盈止损",
            description: "控制回归入场、均值回归退出、止盈比例与止损阈值。",
            fields: [
              {{ path: "mean_reversion.up_buy_deviation", label: "Up 买入偏离阈值", hint: "Up 相对均价的偏离度达到该值时考虑买入。", type: "number", ui: "range", min: 0.01, max: 0.50, step: 0.01, risk: "medium" }},
              {{ path: "mean_reversion.down_buy_deviation", label: "Down 买入偏离阈值", hint: "Down 相对均价的偏离度达到该值时考虑买入。", type: "number", ui: "range", min: 0.01, max: 0.50, step: 0.01, risk: "medium" }},
              {{ path: "mean_reversion.mean_reversion_sell_up_deviation", label: "Up 卖出偏离阈值", hint: "均值回归退出的 Up 阈值。", type: "number", ui: "range", min: 0.01, max: 0.60, step: 0.01, risk: "medium" }},
              {{ path: "mean_reversion.mean_reversion_sell_down_deviation", label: "Down 卖出偏离阈值", hint: "均值回归退出的 Down 阈值。", type: "number", ui: "range", min: 0.01, max: 0.60, step: 0.01, risk: "medium" }},
              {{ path: "mean_reversion.deviation_scale", label: "偏离度放大系数", hint: "偏离越大，单量放大越多。", type: "number", ui: "range", min: 1, max: 100, step: 1, risk: "high" }},
              {{ path: "profit_taking.take_profit_up_deviation", label: "Up 止盈偏离阈值", hint: "触发止盈所需偏离度。", type: "number", ui: "range", min: 0.01, max: 0.60, step: 0.01, risk: "low" }},
              {{ path: "profit_taking.take_profit_down_deviation", label: "Down 止盈偏离阈值", hint: "触发止盈所需偏离度。", type: "number", ui: "range", min: 0.01, max: 0.60, step: 0.01, risk: "low" }},
              {{ path: "profit_taking.take_profit_fraction", label: "止盈卖出比例", hint: "每次止盈卖出持仓的比例。", type: "number", ui: "range", min: 0.05, max: 1.00, step: 0.05, risk: "medium" }},
              {{ path: "stop_loss.stop_loss_cycle_loss", label: "周期止损阈值", hint: "周期亏损超过该值触发止损。", type: "number", ui: "range", min: 1, max: 100, step: 1, risk: "high" }},
              {{ path: "stop_loss.stop_loss_fraction", label: "止损卖出比例", hint: "每次止损卖出持仓的比例。", type: "number", ui: "range", min: 0.05, max: 1.00, step: 0.05, risk: "high" }},
            ],
          }},
          {{
            title: "尾盘逻辑与执行",
            description: "最后一分钟留仓、成交成本和规则优先级。",
            fields: [
              {{ path: "last_minute.last_minute_min_confidence", label: "尾盘最小信心阈值", hint: "低于该值不做尾盘主动留仓。", type: "number", ui: "range", min: 0.50, max: 0.99, step: 0.01, risk: "high" }},
              {{ path: "last_minute.tail_profit_scale", label: "尾盘盈利放大系数", hint: "周期盈利越高，允许尾盘保留更多仓位。", type: "number", ui: "range", min: 0.00, max: 1.00, step: 0.01, risk: "medium" }},
              {{ path: "last_minute.tail_volatility_scale", label: "尾盘波动放大系数", hint: "波动越高，尾盘目标仓位越大。", type: "number", ui: "range", min: 0, max: 100, step: 1, risk: "medium" }},
              {{ path: "last_minute.max_tail_exposure", label: "尾盘最大敞口", hint: "尾盘留仓的上限。", type: "number", ui: "range", min: 0, max: 200, step: 5, risk: "high" }},
              {{ path: "execution.fee_rate", label: "手续费率", hint: "paper broker 使用的模拟手续费。", type: "number", ui: "range", min: 0.000, max: 0.010, step: 0.0005, risk: "medium" }},
              {{ path: "execution.slippage_bps", label: "滑点（bps）", hint: "paper broker 使用的模拟滑点。", type: "number", ui: "select", options: [0, 5, 10, 15, 20, 30], risk: "medium" }},
              {{ path: "priorities.risk", label: "风险规则优先级", hint: "数值越小越优先。", type: "number", min: 1, max: 99, step: 1, risk: "low" }},
              {{ path: "priorities.last_minute", label: "尾盘规则优先级", hint: "数值越小越优先。", type: "number", min: 1, max: 99, step: 1, risk: "low" }},
              {{ path: "priorities.stop_loss", label: "止损规则优先级", hint: "数值越小越优先。", type: "number", min: 1, max: 99, step: 1, risk: "low" }},
              {{ path: "priorities.hedge", label: "对冲规则优先级", hint: "数值越小越优先。", type: "number", min: 1, max: 99, step: 1, risk: "low" }},
              {{ path: "priorities.take_profit", label: "止盈规则优先级", hint: "数值越小越优先。", type: "number", min: 1, max: 99, step: 1, risk: "low" }},
              {{ path: "priorities.grid", label: "网格规则优先级", hint: "数值越小越优先。", type: "number", min: 1, max: 99, step: 1, risk: "low" }},
              {{ path: "priorities.mean_reversion", label: "均值回归优先级", hint: "数值越小越优先。", type: "number", min: 1, max: 99, step: 1, risk: "low" }},
              {{ path: "priorities.trend", label: "趋势规则优先级", hint: "数值越小越优先。", type: "number", min: 1, max: 99, step: 1, risk: "low" }},
            ],
          }},
        ],
      }},
      sweep: {{
        sections: [
          {{
            title: "命名场景",
            description: "定义几组有业务含义的参数组合。",
            fields: [
              {{ path: "scenarios", label: "场景列表", hint: "使用 JSON 数组编辑，每项包含 name 和 overrides。", type: "json" }},
            ],
          }},
          {{
            title: "网格扫描",
            description: "批量枚举多个候选值组合。",
            fields: [
              {{ path: "grid.execution.slippage_bps", label: "滑点候选列表", hint: "例如 [5, 10, 15]。", type: "json" }},
              {{ path: "grid.profit_taking.take_profit_fraction", label: "止盈比例候选列表", hint: "例如 [0.25, 0.35]。", type: "json" }},
            ],
          }},
        ],
      }},
      optimize: {{
        sections: [
          {{
            title: "优化任务",
            description: "控制试验次数、随机种子和导出数量。",
            fields: [
              {{ path: "trials", label: "试验次数", hint: "一次自动寻优会采样多少组参数。", type: "number", ui: "range", min: 1, max: 200, step: 1, risk: "medium" }},
              {{ path: "seed", label: "随机种子", hint: "固定后可重复同一组采样。", type: "number", min: 0, max: 999999, step: 1, risk: "low" }},
              {{ path: "export_top_n", label: "导出前 N 名", hint: "输出得分最好的配置数量。", type: "number", ui: "range", min: 1, max: 20, step: 1, risk: "low" }},
            ],
          }},
          {{
            title: "评分权重",
            description: "定义收益、回撤、费用、胜率等指标在评分中的重要性。",
            fields: [
              {{ path: "score_weights.total_net_profit", label: "净利润权重", hint: "收益越高得分越高。", type: "number" }},
              {{ path: "score_weights.max_drawdown", label: "最大回撤权重", hint: "通常设置为负值惩罚回撤。", type: "number" }},
              {{ path: "score_weights.total_fees", label: "总费用权重", hint: "通常设置为负值惩罚交易成本。", type: "number" }},
              {{ path: "score_weights.win_rate", label: "胜率权重", hint: "提高高胜率方案的综合得分。", type: "number" }},
              {{ path: "score_weights.signal_execution_rate", label: "执行率权重", hint: "提高信号能落地方案的得分。", type: "number" }},
            ],
          }},
          {{
            title: "参数空间",
            description: "定义每个参数的搜索范围与采样规则。",
            fields: [
              {{ path: "parameters", label: "优化参数空间", hint: "使用 JSON 编辑，每个键是一条参数路径及其采样规则。", type: "json" }},
            ],
          }},
        ],
      }},
    }};
    function setConfigStatus(message, level) {{
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
      const label = configEscapeHtml(field.label || field.path);
      const path = configEscapeHtml(field.path);
      const hint = field.hint ? `<div class="config-field-hint">${{configEscapeHtml(field.hint)}}</div>` : "";
      const riskBadge = buildRiskBadge(field);
      const rangeHint = buildRangeHint(field);
      return `<div class="config-row"><div class="config-label-row"><label class="config-label" for="cfg-${{path}}">${{label}}</label>${{riskBadge}}</div><div class="config-path">${{path}}</div>${{renderSchemaInput(field, value)}}${{rangeHint}}${{hint}}</div>`;
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
          .map((row) => renderConfigField({{ path: row.path, label: row.path, hint: "未预设说明的高级参数，仍可直接按 YAML 路径编辑。" }}, row.value))
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
    function readConfigForm() {{
      const out = {{}};
      document.querySelectorAll("#config-form [data-config-path]").forEach((node) => {{
        const path = node.getAttribute("data-config-path") || "";
        const kind = node.getAttribute("data-config-kind") || "string";
        let value;
        if (kind === "boolean") {{
          value = Boolean(node.checked);
        }} else if (kind === "number") {{
          value = Number(node.value);
        }} else if (kind === "json") {{
          value = JSON.parse(node.value || "null");
        }} else {{
          value = node.value;
        }}
        configSetByPath(out, path, value);
      }});
      return out;
    }}
    async function saveConfig() {{
      if (!configConsoleState.currentName) {{
        setConfigStatus("请先选择一个配置文件。", "error");
        return;
      }}
      try {{
        const data = readConfigForm();
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
    function renderLauncherField(profile, field) {{
      const profileName = configEscapeHtml(profile.name || "");
      const fieldName = configEscapeHtml(field.name || "");
      const label = configEscapeHtml(field.label || field.name || "");
      const notes = [];
      if (field.description) notes.push(`<div class="config-field-hint">${{configEscapeHtml(field.description)}}</div>`);
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
          const selected = String(option) === String(field.value) ? "selected" : "";
          return `<option value="${{value}}" ${{selected}}>${{value}}</option>`;
        }}).join("");
        return `<div class="config-row"><label class="config-label">${{label}}</label><select class="config-select" data-launch-profile="${{profileName}}" data-launch-field="${{fieldName}}">${{options}}</select>${{desc}}</div>`;
      }}
      if (field.kind === "number") {{
        return `<div class="config-row"><label class="config-label">${{label}}</label><input class="config-input" type="number" value="${{configEscapeHtml(field.value)}}" data-launch-profile="${{profileName}}" data-launch-field="${{fieldName}}" ${{min}} ${{max}} ${{step}}>${{buildRangeHint(field)}}${{desc}}</div>`;
      }}
      return `<div class="config-row"><label class="config-label">${{label}}</label><input class="config-input" type="text" value="${{configEscapeHtml(field.value ?? "")}}" data-launch-profile="${{profileName}}" data-launch-field="${{fieldName}}">${{desc}}</div>`;
    }}
    function collectLauncherOverrides(profileName) {{
      const overrides = {{}};
      document.querySelectorAll(`[data-launch-profile="${{profileName}}"][data-launch-field]`).forEach((node) => {{
        const fieldName = node.getAttribute("data-launch-field");
        if (!fieldName) return;
        overrides[fieldName] = node.value;
      }});
      return overrides;
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
        profilesNode.addEventListener("click", async function(event) {{
          const target = event.target;
          if (!target) return;
          const profileName = target.getAttribute("data-launch-profile");
          const saveProfileName = target.getAttribute("data-launch-save-profile");
          if (profileName) {{
            const overrides = collectLauncherOverrides(profileName);
            setLauncherStatus(`正在启动 ${{profileName}} ...`, "");
            try {{
              const payload = await startLauncherProfile(profileName, overrides);
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
          if (saveProfileName) {{
            const overrides = collectLauncherOverrides(saveProfileName);
            setLauncherStatus(`正在保存 ${{saveProfileName}} 默认值...`, "");
            try {{
              await saveLauncherDefaults(saveProfileName, overrides);
              const catalog = await loadLauncherCatalog();
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
        renderLauncherProfiles(payload.profiles || [], payload.status || {{}});
        renderLauncherMeta(payload.status || {{}}, payload.help_command || "", payload.preferences_path || "");
        if (payload.status && payload.status.running) {{
          setLauncherStatus(`正在运行：${{payload.status.profile_title || payload.status.profile_name || ""}}`, "ok");
        }} else {{
          setLauncherStatus("已自动回填默认值或最近一次启动参数，可直接启动。", "");
        }}
        setInterval(async function() {{
          try {{
            const status = await refreshLauncherStatus();
            renderLauncherMeta(status || {{}}, payload.help_command || "", payload.preferences_path || "");
            const catalog = await loadLauncherCatalog();
            renderLauncherProfiles(catalog.profiles || [], status || {{}});
            if (status && status.running) {{
              setLauncherStatus(`正在运行：${{status.profile_title || status.profile_name || ""}}`, "ok");
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
          const numberId = target.getAttribute("data-sync-number");
          if (numberId) {{
            const numberNode = document.getElementById(numberId);
            if (numberNode) numberNode.value = target.value;
          }}
          const rangeId = target.getAttribute("data-sync-range");
          if (rangeId) {{
            const rangeNode = document.getElementById(rangeId);
            if (rangeNode) rangeNode.value = target.value;
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


def generate_dashboard_from_directory(
    input_dir: str | Path,
    output_dir: str | Path | None = None,
    title: str = "Polynet AI Monitoring Dashboard",
) -> DashboardArtifacts:
    directory = Path(input_dir)
    target = Path(output_dir) if output_dir else directory
    metrics_df = pd.read_csv(directory / "metrics.csv")
    cycles_df = pd.read_csv(directory / "cycles.csv")
    decisions_df = pd.read_csv(directory / "decisions.csv")
    snapshot_path = directory / "snapshots.csv"
    snapshots_df = pd.read_csv(snapshot_path) if snapshot_path.exists() else pd.DataFrame()
    return generate_dashboard_bundle(metrics_df, cycles_df, decisions_df, snapshots_df, target, title=title)
