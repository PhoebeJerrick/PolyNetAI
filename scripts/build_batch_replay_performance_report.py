from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


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
    if (path / "batch_replay_summary.csv").exists():
        return path
    nested = path / "batch_replay_outputs"
    if (nested / "batch_replay_summary.csv").exists():
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


def build_report(batch_dir: str | Path, output_dir: str | Path | None = None) -> Path:
    resolved_batch_dir = _resolve_batch_replay_dir(batch_dir)
    cycle_dirs = _discover_cycle_result_dirs(resolved_batch_dir)
    if not cycle_dirs:
        raise RuntimeError(f"未在目录下找到任何周期回放结果: {resolved_batch_dir}")

    output_path = Path(output_dir) if output_dir else resolved_batch_dir
    output_path.mkdir(parents=True, exist_ok=True)

    summary_df = _load_summary(resolved_batch_dir, cycle_dirs)
    cycle_df, decision_df = _load_cycle_frames(cycle_dirs)
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

    direction_csv = output_path / "batch_replay_direction_distribution.csv"
    winner_csv = output_path / "batch_replay_winner_distribution.csv"
    net_direction_csv = output_path / "batch_replay_net_direction_distribution.csv"
    enriched_summary_csv = output_path / "batch_replay_summary_enriched.csv"
    report_md = output_path / "batch_replay_performance_report_zh.md"

    direction_df.to_csv(direction_csv, index=False, encoding="utf-8-sig")
    winner_df.to_csv(winner_csv, index=False, encoding="utf-8-sig")
    net_direction_df.to_csv(net_direction_csv, index=False, encoding="utf-8-sig")
    enriched_summary.to_csv(enriched_summary_csv, index=False, encoding="utf-8-sig")

    lines = [
        "# 批量离线回放总绩效报告",
        "",
        f"- 回放目录: `{resolved_batch_dir.as_posix()}`",
        f"- 周期数: {total_cycles}",
        f"- 总净利润: {total_profit:.6f}",
        f"- 平均单周期净利润: {avg_profit:.6f}",
        f"- 胜率: {win_rate:.2%}",
        f"- 最大回撤: {max_drawdown:.6f}",
        f"- 总手续费: {total_fees:.6f}",
        f"- 总执行成交数: {total_executed}",
        "",
        "## 关键结论",
        "",
    ]
    if best_cycle is not None:
        lines.append(
            f"- 最佳周期: `{best_cycle['cycle_slug']}`，净利润 `{float(best_cycle['total_net_profit']):.6f}`"
        )
    if worst_cycle is not None:
        lines.append(
            f"- 最差周期: `{worst_cycle['cycle_slug']}`，净利润 `{float(worst_cycle['total_net_profit']):.6f}`"
        )
    if not winner_df.empty:
        lines.append(
            "- 周期赢家分布: " + "，".join(f"{row['winner']}={int(row['count'])}" for _, row in winner_df.iterrows())
        )
    if not net_direction_df.empty:
        lines.append(
            "- 周期结束净方向分布: "
            + "，".join(f"{row['net_direction']}={int(row['count'])}" for _, row in net_direction_df.iterrows())
        )
    if not direction_df.empty:
        direction_text = "，".join(
            f"{row['selected_outcome']} {row['selected_action']}={int(row['trades'])}笔/{float(row['shares']):.6f}份"
            for _, row in direction_df.iterrows()
        )
        lines.append(f"- 执行方向分布: {direction_text}")

    lines.extend(
        [
            "",
            "## 分周期累计盈亏",
            "",
            enriched_summary.to_string(index=False) if not enriched_summary.empty else "无可用数据",
            "",
            "## 执行方向分布",
            "",
            direction_df.to_string(index=False) if not direction_df.empty else "无已执行成交",
            "",
            "## 周期赢家分布",
            "",
            winner_df.to_string(index=False) if not winner_df.empty else "无可用数据",
            "",
            "## 周期结束净方向分布",
            "",
            net_direction_df.to_string(index=False) if not net_direction_df.empty else "无可用数据",
            "",
            "## 产物文件",
            "",
            f"- `{report_md.name}`",
            f"- `{enriched_summary_csv.name}`",
            f"- `{direction_csv.name}`",
            f"- `{winner_csv.name}`",
            f"- `{net_direction_csv.name}`",
        ]
    )

    report_md.write_text("\n".join(lines), encoding="utf-8")
    return report_md


def main() -> int:
    args = parse_args()
    report_path = build_report(args.input_dir, args.output_dir)
    print(f"已生成中文总绩效报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
