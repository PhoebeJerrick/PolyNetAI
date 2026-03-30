from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.trade_event_store import load_recorded_trade_events  # noqa: E402
from polynet_ai.engine.replay import ReplayEngine  # noqa: E402
from polynet_ai.strategy.spec import load_strategy_config  # noqa: E402
from scripts.build_batch_replay_performance_report import (  # noqa: E402
    _cleanup_batch_replay_markdown,
    build_performance_report_zh,
    write_batch_trade_process_zh,
)


def _resolve_existing_path(label: str, path: str | Path) -> Path:
    """相对路径优先相对 cwd；不存在时再尝试项目根目录。"""
    p = Path(path)
    if p.is_absolute():
        if p.exists():
            return p.resolve()
        raise FileNotFoundError(f"未找到{label}（绝对路径）: {p}")

    cwd_path = (Path.cwd() / p).resolve()
    if cwd_path.exists():
        return cwd_path
    root_path = (ROOT / p).resolve()
    if root_path.exists():
        return root_path
    raise FileNotFoundError(
        f"未找到{label}: {p}\n"
        f"  已尝试: {cwd_path}\n"
        f"  已尝试: {root_path}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量回放抓取目录下的 websocket 事件流")
    parser.add_argument("--input-dir", required=True, help="抓取目录，例如 artifacts/live/ws_capture_btc_10cycles")
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--overrides", default=None, help="可选 JSON 覆盖参数，例如 trial_022 的 overrides.json")
    parser.add_argument("--starting-cash", type=float, default=100.0)
    parser.add_argument("--output-dir", default=None, help="默认输出到 <input-dir>/batch_replay_outputs")
    parser.add_argument(
        "--include-trade-process",
        action="store_true",
        default=False,
        help="是否生成交易过程详细 Excel（batch_replay_trade_process_zh_*.xlsx）",
    )
    return parser.parse_args()


def _load_config(config_path: str | Path, overrides_path: str | Path | None):
    cfg_resolved = _resolve_existing_path("配置文件", config_path)
    config = load_strategy_config(cfg_resolved)
    if overrides_path:
        path = _resolve_existing_path("overrides 文件", overrides_path)
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


def main() -> int:
    args = parse_args()
    input_dir = _resolve_existing_path("输入目录", args.input_dir)

    output_dir = Path(args.output_dir) if args.output_dir else input_dir / "batch_replay_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    config = _load_config(args.config, args.overrides)
    event_files = _discover_cycle_event_files(input_dir)
    if not event_files:
        raise RuntimeError(f"未在目录下找到任何 `<cycle_slug>/ws_trade_events.ndjson`: {input_dir}")

    summary_rows: list[dict[str, object]] = []
    cycle_parts: list[pd.DataFrame] = []
    decision_parts: list[pd.DataFrame] = []

    for event_file in event_files:
        cycle_slug = event_file.parent.name
        events = load_recorded_trade_events(event_file)
        if not events:
            print(f"[skip] {cycle_slug}: 事件文件为空")
            continue

        engine = ReplayEngine(config, starting_cash=args.starting_cash)
        result = engine.run(events)

        cdf = result.cycle_df.copy()
        cdf["cycle_slug"] = cycle_slug
        ddf = result.decision_df.copy()
        ddf["cycle_slug"] = cycle_slug
        cycle_parts.append(cdf)
        decision_parts.append(ddf)

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
    cycle_df = pd.concat(cycle_parts, ignore_index=True) if cycle_parts else pd.DataFrame()
    decision_df = pd.concat(decision_parts, ignore_index=True) if decision_parts else pd.DataFrame()

    # 根据参数决定是否生成交易过程 Excel
    trade_xlsx: Path | None = None
    if args.include_trade_process:
        trade_xlsx = write_batch_trade_process_zh(
            input_dir=input_dir,
            cycle_df=cycle_df,
            decision_df=decision_df,
            output_path=output_dir,
        )

    xlsx_report = build_performance_report_zh(
        resolved_batch_dir=output_dir,
        summary_df=summary_df,
        cycle_df=cycle_df,
        decision_df=decision_df,
        output_path=output_dir,
        display_batch_dir=input_dir,
    )
    _cleanup_batch_replay_markdown(output_dir)

    print(f"批量回放完成，共 {len(summary_df)} 个周期")
    if trade_xlsx:
        print(f"交易过程 (Excel): {trade_xlsx}")
    print(f"总绩效报告 (Excel): {xlsx_report}")
    if not xlsx_report.exists():
        print(
            "警告: 未找到 Excel 总绩效文件。请确认已使用包含 batch_replay_performance_report.xlsx 生成的最新代码。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
