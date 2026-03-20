from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd


@dataclass(slots=True)
class DashboardArtifacts:
    html_path: Path
    markdown_path: Path
    summary_csv_path: Path


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


def build_dashboard_html(
    title: str,
    metrics_df: pd.DataFrame,
    cycles_df: pd.DataFrame,
    decisions_df: pd.DataFrame,
    snapshots_df: pd.DataFrame,
) -> str:
    summary_df = _build_summary_frame(metrics_df, cycles_df, decisions_df, snapshots_df)
    summary = summary_df.iloc[0].to_dict()
    alerts = _build_alerts(summary, cycles_df, decisions_df)
    equity_curve = _line_svg(_to_float_series(snapshots_df, "account_cash"), color="#3b82f6")
    cycle_curve = _line_svg(_to_float_series(cycles_df, "cycle_net_profit"), color="#8b5cf6")
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
    safe_title = html.escape(title)
    latest_market = html.escape(str(summary.get("latest_market", "")))
    latest_cycle = html.escape(str(summary.get("latest_cycle", "")))
    alert_html = "".join(_alert_badge(alert) for alert in alerts) if alerts else '<div class="alert-card ok"><div class="alert-title">无主动告警</div><div class="alert-message">当前指标未触发高风险阈值。</div></div>'
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
  </style>
</head>
<body>
  <h1>{safe_title}</h1>
  <div class="meta muted">
    <div>最新市场: {latest_market}</div>
    <div>最新周期: {latest_cycle}</div>
    <div>周期数: {summary.get("total_cycles", 0)}</div>
    <div>信号数: {summary.get("total_signals", 0)}</div>
  </div>
  <div class="grid">{card_html}</div>
  <div class="panel">
    <h2>告警视图</h2>
    <div class="alert-grid">{alert_html}</div>
  </div>
  <div class="panel-grid">
    <div class="panel">
      <h2>资金曲线</h2>
      {equity_curve}
    </div>
    <div class="panel">
      <h2>周期盈亏曲线</h2>
      {cycle_curve}
    </div>
    <div class="panel">
      <h2>规则触发次数</h2>
      {signal_counts}
    </div>
    <div class="panel">
      <h2>规则实际执行次数</h2>
      {executed_counts}
    </div>
  </div>
  <div class="panel">
    <h2>最近决策</h2>
    {decision_html}
  </div>
  <div class="panel">
    <h2>最近周期</h2>
    {cycle_html}
  </div>
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
) -> DashboardArtifacts:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    html_path = directory / "dashboard.html"
    markdown_path = directory / "daily_report.md"
    summary_csv_path = directory / "dashboard_summary.csv"
    html_path.write_text(
        build_dashboard_html(title, metrics_df, cycles_df, decisions_df, snapshots_df),
        encoding="utf-8",
    )
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
