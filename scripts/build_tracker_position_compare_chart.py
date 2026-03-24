#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


UP_KEYWORDS = ("up", "yes", "涨", "看涨", "做多", "多")
DOWN_KEYWORDS = ("down", "no", "跌", "看跌", "做空", "空")

REQUIRED_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "成交价格": ("成交价格", "价格", "price", "avgprice"),
    "投注份数": ("投注份数", "份数", "成交份数", "成交数量", "数量", "shares", "size", "qty", "amount"),
    "结果代币类型": ("结果代币类型", "方向", "结果", "币种方向", "Outcome", "outcome", "token", "token_side"),
    "Up积累份数": ("Up积累份数",),
    "Down积累份数": ("Down积累份数",),
    "当前总持仓份数": ("当前总持仓份数", "当前总持有份数"),
    "净持仓份数": ("净持仓份数",),
    "净持仓价值": ("净持仓价值",),
    "时间周期": ("时间周期", "市场周期", "周期", "结算周期", "轮次", "场次", "market_cycle", "cycle", "event_start_time", "开始时间"),
}


def normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    for ch in (" ", "_", "-", "/", "\\", "（", "）", "(", ")", "[", "]", ":"):
        text = text.replace(ch, "")
    return text


def find_column(columns: list[str], aliases: tuple[str, ...], *, required: bool = True) -> str | None:
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


def parse_outcome_side(value: object) -> str:
    text = normalize_text(value)
    if any(token in text for token in UP_KEYWORDS):
        return "up"
    if any(token in text for token in DOWN_KEYWORDS):
        return "down"
    return "unknown"


def resolve_columns(df: pd.DataFrame) -> dict[str, str]:
    resolved: dict[str, str] = {}
    cols = [str(c) for c in df.columns]
    for key, aliases in REQUIRED_COLUMN_ALIASES.items():
        resolved[key] = str(find_column(cols, aliases, required=True))
    return resolved


def select_sheet(path: Path, sheet_name: str | None) -> str:
    xls = pd.ExcelFile(path)
    if sheet_name:
        if sheet_name not in xls.sheet_names:
            raise ValueError(f"工作表不存在: {sheet_name}；可选: {', '.join(xls.sheet_names)}")
        return sheet_name
    if not xls.sheet_names:
        raise ValueError("Excel 中没有工作表。")
    return xls.sheet_names[0]


def sort_by_cycle(df: pd.DataFrame, cycle_col: str) -> pd.DataFrame:
    out = df.copy()
    dt_series = pd.to_datetime(out[cycle_col], errors="coerce", format="mixed")
    if dt_series.notna().all():
        out["_cycle_sort_key"] = dt_series
    else:
        out["_cycle_sort_key"] = out[cycle_col].astype(str)
    out = out.sort_values("_cycle_sort_key", kind="stable").drop(columns=["_cycle_sort_key"])
    return out


def remove_subtotal_rows(df: pd.DataFrame) -> pd.DataFrame:
    marker_cols = (
        "下注时间距开盘差(分，秒)",
        "下注时间距开盘差（分，秒）",
        "下注时间距开盘差",
    )
    for col in marker_cols:
        if col in df.columns:
            mask = df[col].astype(str).str.contains("【周期小计】", na=False)
            return df.loc[~mask].copy()
    return df


def to_numeric_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def parse_seconds_value(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("（", "(").replace("）", ")").replace("分", ":").replace("秒", "")
    parts = [p.strip() for p in normalized.split(":") if p.strip()]
    if len(parts) >= 2 and all(part.replace(".", "", 1).isdigit() for part in parts[-2:]):
        return float(parts[-2]) * 60 + float(parts[-1])
    try:
        ts = pd.to_datetime(text, errors="raise")
    except Exception:
        return None
    return float(ts.minute * 60 + ts.second + ts.microsecond / 1_000_000)


def choose_cycle_seconds_x(work: pd.DataFrame) -> list[float]:
    for candidate in ("下注时间距开盘差(分，秒)", "下注时间距开盘差（分，秒）", "下注时间距开盘差", "时间", "下注时间"):
        if candidate not in work.columns:
            continue
        parsed = work[candidate].apply(parse_seconds_value)
        if parsed.notna().all():
            return parsed.astype(float).tolist()
    return [float(i) for i in range(1, len(work) + 1)]


def _build_cycle_payload(cycle_df: pd.DataFrame, cols: dict[str, str]) -> dict[str, object]:
    qty_col = cols["投注份数"]
    outcome_col = cols["结果代币类型"]
    price_col = cols["成交价格"]

    metric_pairs = [
        ("投注份数", qty_col),
        ("Up积累份数", cols["Up积累份数"]),
        ("Down积累份数", cols["Down积累份数"]),
        ("当前总持仓份数", cols["当前总持仓份数"]),
        ("净持仓份数", cols["净持仓份数"]),
        ("净持仓价值", cols["净持仓价值"]),
    ]
    metric_cols = [src_col for _, src_col in metric_pairs]

    work = remove_subtotal_rows(cycle_df)
    work = to_numeric_columns(work, metric_cols)
    work[price_col] = pd.to_numeric(work[price_col], errors="coerce")
    work["_direction"] = work[outcome_col].apply(parse_outcome_side)
    x_values = choose_cycle_seconds_x(work)

    up_points = work[work["_direction"] == "up"]
    down_points = work[work["_direction"] == "down"]
    up_x_values = choose_cycle_seconds_x(up_points) if not up_points.empty else []
    down_x_values = choose_cycle_seconds_x(down_points) if not down_points.empty else []
    metric_hover = [cols["Up积累份数"], cols["Down积累份数"], cols["当前总持仓份数"], cols["净持仓份数"], cols["净持仓价值"]]
    up_custom = up_points[metric_hover].where(pd.notna(up_points[metric_hover]), None).values.tolist()
    down_custom = down_points[metric_hover].where(pd.notna(down_points[metric_hover]), None).values.tolist()

    position_traces: list[dict[str, object]] = []
    for display_name, source_col in metric_pairs:
        position_traces.append(
            {
                "type": "scatter",
                "x": x_values,
                "y": work[source_col].where(pd.notna(work[source_col]), None).tolist(),
                "mode": "lines",
                "name": display_name,
                "line": {"width": 2},
            }
        )

    position_traces.append(
        {
            "type": "scatter",
            "x": up_x_values,
            "y": up_points[qty_col].where(pd.notna(up_points[qty_col]), None).tolist(),
            "mode": "markers",
            "name": "投注份数(Up点)",
            "marker": {"color": "#2ca02c", "size": 8, "symbol": "circle"},
            "customdata": up_custom,
            "hovertemplate": (
                "单周期内秒数: %{x}<br>"
                "方向: Up<br>"
                "投注份数: %{y}<br>"
                "Up积累份数: %{customdata[0]}<br>"
                "Down积累份数: %{customdata[1]}<br>"
                "当前总持仓份数: %{customdata[2]}<br>"
                "净持仓份数: %{customdata[3]}<br>"
                "净持仓价值: %{customdata[4]}<extra></extra>"
            ),
        }
    )
    position_traces.append(
        {
            "type": "scatter",
            "x": down_x_values,
            "y": down_points[qty_col].where(pd.notna(down_points[qty_col]), None).tolist(),
            "mode": "markers",
            "name": "投注份数(Down点)",
            "marker": {"color": "#d62728", "size": 8, "symbol": "diamond"},
            "customdata": down_custom,
            "hovertemplate": (
                "单周期内秒数: %{x}<br>"
                "方向: Down<br>"
                "投注份数: %{y}<br>"
                "Up积累份数: %{customdata[0]}<br>"
                "Down积累份数: %{customdata[1]}<br>"
                "当前总持仓份数: %{customdata[2]}<br>"
                "净持仓份数: %{customdata[3]}<br>"
                "净持仓价值: %{customdata[4]}<extra></extra>"
            ),
        }
    )

    up_price: list[object] = []
    down_price: list[object] = []
    for _, row in work.iterrows():
        px = row[price_col]
        side = row["_direction"]
        if pd.isna(px):
            up_price.append(None)
            down_price.append(None)
            continue
        pxf = float(px)
        if side == "up":
            up_price.append(pxf)
            down_price.append(1.0 - pxf)
        elif side == "down":
            down_price.append(pxf)
            up_price.append(1.0 - pxf)
        else:
            up_price.append(None)
            down_price.append(None)

    price_traces = [
        {
            "type": "scatter",
            "x": x_values,
            "y": up_price,
            "mode": "lines",
            "name": "Up价格",
            "line": {"width": 2, "color": "#2ca02c"},
        },
        {
            "type": "scatter",
            "x": x_values,
            "y": down_price,
            "mode": "lines",
            "name": "Down价格",
            "line": {"width": 2, "color": "#d62728"},
        },
    ]
    return {"position_traces": position_traces, "price_traces": price_traces}


def build_dropdown_chart(df: pd.DataFrame, cols: dict[str, str], output_file: Path, title: str) -> int:
    cycle_col = cols["时间周期"]
    clean_df = sort_by_cycle(remove_subtotal_rows(df), cycle_col)

    cycles: list[dict[str, object]] = []
    for cycle_val, cycle_df in clean_df.groupby(cycle_col, sort=False):
        if cycle_df.empty:
            continue
        cycles.append(
            {
                "label": str(cycle_val),
                "payload": _build_cycle_payload(cycle_df, cols),
            }
        )

    if not cycles:
        raise ValueError("没有可用周期数据，未生成图表。")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    series_names = [str(trace["name"]) for trace in cycles[0]["payload"]["position_traces"]]
    html = (
        "<!doctype html>\n"
        "<html lang='zh-CN'>\n"
        "<head>\n"
        "  <meta charset='utf-8' />\n"
        "  <meta name='viewport' content='width=device-width, initial-scale=1' />\n"
        "  <title>持仓对比折线图</title>\n"
        "  <script src='https://cdn.plot.ly/plotly-2.35.2.min.js'></script>\n"
        "  <style>\n"
        "    body{font-family:Arial,Helvetica,sans-serif;margin:0;padding:16px;}\n"
        "    .controls{display:flex;flex-direction:column;gap:8px;margin-bottom:8px;}\n"
        "    .filterRow{display:flex;flex-wrap:wrap;gap:10px;align-items:center;}\n"
        "    .filterItem{display:flex;align-items:center;gap:6px;}\n"
        "    .swatch{width:10px;height:10px;border-radius:2px;display:inline-block;}\n"
        "    #chart{width:100%;height:62vh;}\n"
        "    #priceChart{width:100%;height:28vh;margin-top:14px;}\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <div class='controls'>\n"
        "    <div>\n"
        "      <label for='cycleSelect'><strong>选择周期:</strong></label>\n"
        "      <select id='cycleSelect'></select>\n"
        "    </div>\n"
        "    <div class='filterRow' id='filterRow'></div>\n"
        "  </div>\n"
        "  <div id='chart'></div>\n"
        "  <div id='priceChart'></div>\n"
        "  <script>\n"
        f"    const cycles = {json.dumps(cycles, ensure_ascii=False)};\n"
        f"    const seriesNames = {json.dumps(series_names, ensure_ascii=False)};\n"
        f"    const chartTitle = {json.dumps(title, ensure_ascii=False)};\n"
        "    const cycleSelect = document.getElementById('cycleSelect');\n"
        "    const filterRow = document.getElementById('filterRow');\n"
        "    let selectedCycleIdx = 0;\n"
        "    const visibleSeries = new Set(seriesNames);\n"
        "    function colorForName(name){\n"
        "      if(name === '投注份数(Up点)') return '#2ca02c';\n"
        "      if(name === '投注份数(Down点)') return '#d62728';\n"
        "      const palette = ['#1f77b4','#ff7f0e','#9467bd','#8c564b','#e377c2','#17becf'];\n"
        "      const idx = Math.max(seriesNames.indexOf(name), 0);\n"
        "      return palette[idx % palette.length];\n"
        "    }\n"
        "    function renderCheckboxes(){\n"
        "      filterRow.innerHTML = '';\n"
        "      seriesNames.forEach((name)=>{\n"
        "        const label = document.createElement('label');\n"
        "        label.className = 'filterItem';\n"
        "        const cb = document.createElement('input');\n"
        "        cb.type = 'checkbox';\n"
        "        cb.checked = visibleSeries.has(name);\n"
        "        cb.addEventListener('change', ()=>{\n"
        "          if(cb.checked) visibleSeries.add(name); else visibleSeries.delete(name);\n"
        "          renderChart();\n"
        "        });\n"
        "        const sw = document.createElement('span');\n"
        "        sw.className = 'swatch';\n"
        "        sw.style.backgroundColor = colorForName(name);\n"
        "        const txt = document.createElement('span');\n"
        "        txt.textContent = name;\n"
        "        label.appendChild(cb);\n"
        "        label.appendChild(sw);\n"
        "        label.appendChild(txt);\n"
        "        filterRow.appendChild(label);\n"
        "      });\n"
        "    }\n"
        "    function renderChart(){\n"
        "      const cycle = cycles[selectedCycleIdx];\n"
        "      const data = cycle.payload.position_traces.map((trace)=>({ ...trace, visible: visibleSeries.has(trace.name) }));\n"
        "      const layout = {\n"
        "        title: { text: `${chartTitle} - ${cycle.label}` },\n"
        "        xaxis: { title: { text: '单周期内秒数' } },\n"
        "        yaxis: { title: { text: '数值' } },\n"
        "        hovermode: 'x unified',\n"
        "        template: 'plotly_white',\n"
        "        legend: { orientation: 'h', yanchor: 'top', y: 1.02, xanchor: 'left', x: 0 }\n"
        "      };\n"
        "      Plotly.react('chart', data, layout, { responsive: true });\n"
        "      const priceLayout = {\n"
        "        title: { text: `同周期价格折线图 - ${cycle.label}` },\n"
        "        xaxis: { title: { text: '单周期内秒数' } },\n"
        "        yaxis: { title: { text: '价格' } },\n"
        "        hovermode: 'x unified',\n"
        "        template: 'plotly_white',\n"
        "        legend: { orientation: 'h', yanchor: 'top', y: 1.02, xanchor: 'left', x: 0 }\n"
        "      };\n"
        "      Plotly.react('priceChart', cycle.payload.price_traces, priceLayout, { responsive: true });\n"
        "    }\n"
        "    cycles.forEach((cycle, idx)=>{\n"
        "      const option = document.createElement('option');\n"
        "      option.value = String(idx);\n"
        "      option.textContent = cycle.label;\n"
        "      cycleSelect.appendChild(option);\n"
        "    });\n"
        "    cycleSelect.value = '0';\n"
        "    cycleSelect.addEventListener('change', ()=>{\n"
        "      selectedCycleIdx = Number(cycleSelect.value || '0');\n"
        "      renderChart();\n"
        "    });\n"
        "    renderCheckboxes();\n"
        "    renderChart();\n"
        "  </script>\n"
        "</body>\n"
        "</html>\n"
    )
    output_file.write_text(html, encoding="utf-8")
    return len(cycles)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基于 tracker v5 Excel 生成持仓对比折线图（HTML）"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/processed/polymarket_tracker_collection_with_accumulated_shares_v5.xlsx"),
        help="输入 Excel 路径",
    )
    parser.add_argument("--sheet", type=str, default=None, help="工作表名称（默认第一个工作表）")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/charts/tracker_position_compare.html"),
        help="输出单个 HTML 路径",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.exists():
        print(f"未找到输入文件: {args.input}")
        return 1

    try:
        sheet_name = select_sheet(args.input, args.sheet)
        df = pd.read_excel(args.input, sheet_name=sheet_name)
        if df.empty:
            raise ValueError(f"工作表为空: {sheet_name}")
        cols = resolve_columns(df)
        cycle_count = build_dropdown_chart(
            df=df,
            cols=cols,
            output_file=args.output,
            title=f"持仓对比折线图 - {sheet_name}",
        )
    except Exception as exc:
        print(f"生成失败: {exc}")
        return 1

    print(f"图表已生成: {args.output.resolve()}")
    print(f"周期图数量: {cycle_count}")
    print(f"使用工作表: {sheet_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
