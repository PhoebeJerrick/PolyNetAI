#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


COMPARE_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Up积累份数", ("Up积累份数",)),
    ("Down积累份数", ("Down积累份数",)),
    ("当前总持仓份数", ("当前总持仓份数",)),
    ("净持仓份数", ("净持仓份数",)),
    ("净持仓成本差额", ("净持仓成本差额", "净持仓价值")),
    ("持仓异常", ("持仓异常",)),
)

CONTEXT_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("下注时间距开盘差(分，秒)", ("下注时间距开盘差(分，秒)", "下注时间距开盘差（分，秒）", "下注时间距开盘差")),
    ("时间周期", ("时间周期", "市场周期", "周期", "cycle")),
    ("结果代币类型", ("结果代币类型", "方向", "结果")),
    ("操作方向", ("操作方向", "操作", "买卖方向")),
    ("投注份数", ("投注份数", "份数", "shares")),
    ("成交价格", ("成交价格", "价格", "price")),
)


def normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    for ch in (" ", "_", "-", "/", "\\", "（", "）", "(", ")", "[", "]", ":"):
        text = text.replace(ch, "")
    return text


def find_column(columns: list[str], aliases: tuple[str, ...]) -> str | None:
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
    return None


def remove_subtotal_rows(df: pd.DataFrame) -> pd.DataFrame:
    marker_col = find_column(list(df.columns), ("下注时间距开盘差(分，秒)", "下注时间距开盘差（分，秒）", "下注时间距开盘差"))
    if marker_col is None:
        return df.copy()
    mask = df[marker_col].astype(str).str.contains("【周期小计】", na=False)
    return df.loc[~mask].copy()


def aligned_series(df: pd.DataFrame, aliases: tuple[str, ...], row_count: int) -> pd.Series:
    col = find_column([str(c) for c in df.columns], aliases)
    if col is None:
        return pd.Series([""] * row_count, dtype=object)
    series = df[col].reset_index(drop=True)
    if len(series) < row_count:
        series = series.reindex(range(row_count), fill_value="")
    return series.iloc[:row_count]


def format_value(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(round(float(value), 3))
    return str(value)


def comparable_value(value: object) -> object:
    if pd.isna(value):
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 3)
    return str(value)


def negative_position_count(df: pd.DataFrame, aliases: tuple[str, ...]) -> int:
    col = find_column([str(c) for c in df.columns], aliases)
    if col is None:
        return 0
    series = pd.to_numeric(df[col], errors="coerce")
    return int((series < -1e-9).sum())


def build_sheet_report(sheet: str, old_df: pd.DataFrame, new_df: pd.DataFrame) -> tuple[str, dict[str, int]]:
    old_work = remove_subtotal_rows(old_df)
    new_work = remove_subtotal_rows(new_df)
    row_count = min(len(old_work), len(new_work))

    summary = {
        "old_rows": len(old_work),
        "new_rows": len(new_work),
        "old_negative_up": negative_position_count(old_work, ("Up积累份数",)),
        "new_negative_up": negative_position_count(new_work, ("Up积累份数",)),
        "old_negative_down": negative_position_count(old_work, ("Down积累份数",)),
        "new_negative_down": negative_position_count(new_work, ("Down积累份数",)),
        "old_negative_total": negative_position_count(old_work, ("当前总持仓份数",)),
        "new_negative_total": negative_position_count(new_work, ("当前总持仓份数",)),
        "changed_rows": 0,
    }

    context_series = [(label, aligned_series(old_work, aliases, row_count), aligned_series(new_work, aliases, row_count)) for label, aliases in CONTEXT_COLUMNS]
    compare_series = [(label, aligned_series(old_work, aliases, row_count), aligned_series(new_work, aliases, row_count)) for label, aliases in COMPARE_COLUMNS]

    example_lines: list[str] = []
    for idx in range(row_count):
        row_changes: list[str] = []
        for label, old_series, new_series in compare_series:
            old_value = old_series.iloc[idx]
            new_value = new_series.iloc[idx]
            if comparable_value(old_value) != comparable_value(new_value):
                row_changes.append(f"{label}: {format_value(old_value)} -> {format_value(new_value)}")
        if row_changes:
            summary["changed_rows"] += 1
            if len(example_lines) < 12:
                context_parts: list[str] = []
                for label, old_series, new_series in context_series:
                    context_value = new_series.iloc[idx]
                    if comparable_value(context_value) == "":
                        context_value = old_series.iloc[idx]
                    if comparable_value(context_value) != "":
                        context_parts.append(f"{label}={format_value(context_value)}")
                example_lines.append(
                    f"- 行 {idx + 2}: {'; '.join(context_parts)} | {'; '.join(row_changes)}"
                )

    lines = [
        f"## {sheet}",
        f"- 数据行数: 旧={summary['old_rows']}，新={summary['new_rows']}",
        f"- 负持仓行数: Up 旧={summary['old_negative_up']} / 新={summary['new_negative_up']}；Down 旧={summary['old_negative_down']} / 新={summary['new_negative_down']}；总持仓 旧={summary['old_negative_total']} / 新={summary['new_negative_total']}",
        f"- 关键指标发生变化的行数: {summary['changed_rows']}",
    ]
    if example_lines:
        lines.append("- 代表性差异:")
        lines.extend(example_lines)
    else:
        lines.append("- 代表性差异: 无")
    lines.append("")
    return "\n".join(lines), summary


def build_report(old_path: Path, new_path: Path) -> str:
    old_xls = pd.ExcelFile(old_path)
    new_xls = pd.ExcelFile(new_path)
    common_sheets = [sheet for sheet in old_xls.sheet_names if sheet in new_xls.sheet_names]
    if not common_sheets:
        raise ValueError("旧文件与新文件没有可比较的共同工作表。")

    sections = [
        "# Tracker Excel 差异清单",
        f"- 旧文件: {old_path}",
        f"- 新文件: {new_path}",
        "",
    ]

    for sheet in common_sheets:
        old_df = pd.read_excel(old_path, sheet_name=sheet)
        new_df = pd.read_excel(new_path, sheet_name=sheet)
        section, _summary = build_sheet_report(sheet, old_df, new_df)
        sections.append(section)

    return "\n".join(sections).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比 tracker Excel 旧版本与新版本的关键差异")
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=False)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.old, args.new)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"差异清单已写入: {args.output}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())