from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.trade_event_store import load_recorded_trade_events  # noqa: E402
from polynet_ai.engine.replay import ReplayEngine  # noqa: E402
from polynet_ai.reporting.excel_export import export_replay_to_excel  # noqa: E402
from polynet_ai.strategy.spec import load_strategy_config  # noqa: E402
from scripts.build_batch_replay_performance_report import build_report  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量回放抓取目录下的 websocket 事件流")
    parser.add_argument("--input-dir", required=True, help="抓取目录，例如 artifacts/live/ws_capture_btc_10cycles")
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--overrides", default=None, help="可选 JSON 覆盖参数，例如 trial_022 的 overrides.json")
    parser.add_argument("--starting-cash", type=float, default=100.0)
    parser.add_argument("--output-dir", default=None, help="默认输出到 <input-dir>/batch_replay_outputs")
    return parser.parse_args()


def _load_config(config_path: str | Path, overrides_path: str | Path | None):
    config = load_strategy_config(config_path)
    if overrides_path:
        path = Path(overrides_path)
        with path.open("r", encoding="utf-8") as fh:
            overrides = json.load(fh)
        if not isinstance(overrides, dict):
            raise ValueError(f"overrides 文件必须是 JSON 对象: {path}")
        config = config.with_overrides(overrides)
    return config


def _discover_cycle_event_files(input_dir: Path) -> list[Path]:
    files = sorted(
        path
        for path in input_dir.glob("*/ws_trade_events.ndjson")
        if path.is_file()
    )
    return files


def _write_batch_summary(input_dir: Path, output_dir: Path, summary_df: pd.DataFrame) -> tuple[Path, Path]:
    summary_csv = output_dir / "batch_replay_summary.csv"
    summary_md = output_dir / "batch_replay_summary.md"
    summary_df.to_csv(summary_csv, index=False, encoding="utf-8-sig")

    total_cycles = len(summary_df)
    total_profit = float(summary_df["total_net_profit"].sum()) if total_cycles else 0.0
    avg_profit = float(summary_df["total_net_profit"].mean()) if total_cycles else 0.0
    win_rate = float((summary_df["total_net_profit"] > 0).mean()) if total_cycles else 0.0
    lines = [
        "# 批量回放摘要",
        "",
        f"- 输入目录: `{input_dir.as_posix()}`",
        f"- 输出目录: `{output_dir.as_posix()}`",
        f"- 回放周期数: {total_cycles}",
        f"- 总净利润: {total_profit:.6f}",
        f"- 平均单周期净利润: {avg_profit:.6f}",
        f"- 盈利周期占比: {win_rate:.2%}",
        "",
        "## 分周期结果",
        "",
        summary_df.to_string(index=False) if total_cycles else "无可用周期",
    ]
    summary_md.write_text("\n".join(lines), encoding="utf-8")
    return summary_csv, summary_md


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        raise FileNotFoundError(f"未找到输入目录: {input_dir}")

    output_dir = Path(args.output_dir) if args.output_dir else input_dir / "batch_replay_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    config = _load_config(args.config, args.overrides)
    event_files = _discover_cycle_event_files(input_dir)
    if not event_files:
        raise RuntimeError(f"未在目录下找到任何 `<cycle_slug>/ws_trade_events.ndjson`: {input_dir}")

    summary_rows: list[dict[str, object]] = []
    for event_file in event_files:
        cycle_slug = event_file.parent.name
        events = load_recorded_trade_events(event_file)
        if not events:
            print(f"[skip] {cycle_slug}: 事件文件为空")
            continue

        engine = ReplayEngine(config, starting_cash=args.starting_cash)
        result = engine.run(events)

        cycle_output_dir = output_dir / cycle_slug
        cycle_output_dir.mkdir(parents=True, exist_ok=True)
        excel_path = cycle_output_dir / f"{cycle_slug}_replay.xlsx"
        export_replay_to_excel(result.cycle_df, result.decision_df, result.metrics_df, excel_path)
        result.cycle_df.to_csv(cycle_output_dir / "cycles.csv", index=False, encoding="utf-8-sig")
        result.decision_df.to_csv(cycle_output_dir / "decisions.csv", index=False, encoding="utf-8-sig")
        result.metrics_df.to_csv(cycle_output_dir / "metrics.csv", index=False, encoding="utf-8-sig")

        metrics_row = result.metrics_df.iloc[0].to_dict()
        cycle_row = result.cycle_df.iloc[0].to_dict() if not result.cycle_df.empty else {}
        summary_rows.append(
            {
                "cycle_slug": cycle_slug,
                "event_count": len(events),
                "executed_trades": metrics_row.get("executed_trades", 0),
                "accepted_signals": metrics_row.get("accepted_signals", 0),
                "blocked_signals": metrics_row.get("blocked_signals", 0),
                "total_net_profit": metrics_row.get("total_net_profit", 0.0),
                "total_fees": metrics_row.get("total_fees", 0.0),
                "win_rate": metrics_row.get("win_rate", 0.0),
                "winner": cycle_row.get("winner", ""),
                "account_cash": cycle_row.get("account_cash", None),
                "output_excel": str(excel_path),
            }
        )
        print(
            f"[done] {cycle_slug}: events={len(events)} "
            f"net_profit={float(metrics_row.get('total_net_profit', 0.0)):.6f} "
            f"executed={metrics_row.get('executed_trades', 0)}"
        )

    summary_df = pd.DataFrame(summary_rows)
    if not summary_df.empty:
        summary_df = summary_df.sort_values("cycle_slug").reset_index(drop=True)
    summary_csv, summary_md = _write_batch_summary(input_dir, output_dir, summary_df)
    report_path = build_report(output_dir)

    print(f"批量回放完成，共 {len(summary_df)} 个周期")
    print(f"摘要 CSV: {summary_csv}")
    print(f"摘要 MD: {summary_md}")
    print(f"总绩效报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
