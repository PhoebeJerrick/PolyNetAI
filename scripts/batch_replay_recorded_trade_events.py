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
    parser.add_argument("--per-cycle-cash", type=float, default=None,
                        help="固定模式下每周期实际投注资金；不设置则与 --starting-cash 相同")
    parser.add_argument(
        "--capital-reset-mode",
        type=str,
        choices=["fixed", "cumulative"],
        default="fixed",
        help="周期资金处理模式：fixed=每周期重置，cumulative=跨周期累积",
    )
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


def run_batch_replay(
    input_dir: str | Path,
    config_path: str | Path = "configs/strategy.yaml",
    overrides_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    starting_cash: float = 100.0,
    capital_reset_mode: str = "fixed",
    per_cycle_cash: float | None = None,
    include_trade_process: bool = False,
    cycle_range: str = "",
    cycle_count: int = 0,
    report_source: str = "",
    max_cycles: int | None = None,
    report_name_prefix: str = "",
) -> Path | None:
    """核心 batch replay 逻辑，可被外部脚本调用。返回 Excel 绩效报告路径。"""
    input_resolved = _resolve_existing_path("输入目录", input_dir)
    output_resolved = Path(output_dir) if output_dir else input_resolved / "batch_replay_outputs"
    output_resolved.mkdir(parents=True, exist_ok=True)

    config = _load_config(config_path, overrides_path)
    event_files = _discover_cycle_event_files(input_resolved)
    if not event_files:
        raise RuntimeError(f"未在目录下找到任何 `<cycle_slug>/ws_trade_events.ndjson`: {input_resolved}")
    if max_cycles is not None and max_cycles > 0:
        event_files = event_files[-max_cycles:]
    
    total_files = len(event_files)
    print(f"  ℹ 共需回放 {total_files} 个周期文件")

    summary_rows: list[dict[str, object]] = []
    cycle_parts: list[pd.DataFrame] = []
    decision_parts: list[pd.DataFrame] = []

    # 引擎内部根据 capital_reset_mode 处理周期资金：
    # fixed: 每周期固定投注本金，但保留累计盈亏曲线；cumulative: 账户资金跨周期连续累积。
    engine = ReplayEngine(
        config,
        starting_cash=starting_cash,
        capital_reset_mode=capital_reset_mode,
        per_cycle_cash=per_cycle_cash,
    )
    all_finalized_cycle_rows: list[dict[str, object]] = []
    per_cycle_meta: dict[str, dict] = {}  # cycle_slug -> {event_count, decision_rows}

    for idx, event_file in enumerate(event_files, 1):
        cycle_slug = event_file.parent.name

        events = load_recorded_trade_events(event_file)
        if not events:
            print(f"[skip] {cycle_slug}: 事件文件为空")
            continue

        sorted_events = sorted(events, key=lambda e: (e.market_id, e.cycle_id, e.timestamp))
        cycle_decisions: list[dict[str, object]] = []

        for event in sorted_events:
            step = engine.process_event(event)
            if step.finalized_cycle_row is not None:
                all_finalized_cycle_rows.append(step.finalized_cycle_row)
            cycle_decisions.append(step.decision_row)

        per_cycle_meta[cycle_slug] = {
            "event_count": len(events),
            "decision_rows": cycle_decisions,
        }

        executed = sum(1 for d in cycle_decisions if d.get("executed"))
        net_profit = float(cycle_decisions[-1].get("cycle_net_profit", 0.0)) if cycle_decisions else 0.0
        print(
            f"    [{idx}/{total_files}] {cycle_slug}: "
            f"events={len(events)} | profit={net_profit:.4f} | "
            f"executed={executed}"
        )

    # 最后一个周期尚未被下一周期的事件触发结算，手动结算
    pending = engine.finalize_pending_cycle()
    if pending is not None:
        all_finalized_cycle_rows.append(pending)

    # 从结算行和决策行构建每周期汇总
    for cycle_row in all_finalized_cycle_rows:
        slug = str(cycle_row.get("cycle_id", ""))
        meta = per_cycle_meta.get(slug, {})
        decisions = meta.get("decision_rows", [])

        cdf = pd.DataFrame([cycle_row])
        cdf["cycle_slug"] = slug
        cycle_parts.append(cdf)

        if decisions:
            ddf = pd.DataFrame(decisions)
            ddf["cycle_slug"] = slug
            decision_parts.append(ddf)

        total_fees = sum(float(d.get("fill_fee", 0)) for d in decisions)
        net_profit = float(cycle_row.get("cycle_net_profit", 0.0))
        summary_rows.append(
            {
                "cycle_slug": slug,
                "event_count": meta.get("event_count", 0),
                "executed_trades": sum(1 for d in decisions if d.get("executed")),
                "accepted_signals": sum(1 for d in decisions if d.get("risk_status") == "accepted"),
                "blocked_signals": sum(1 for d in decisions if d.get("risk_status") == "blocked"),
                "total_net_profit": net_profit,
                "total_fees": total_fees,
                "win_rate": 1.0 if net_profit > 0 else 0.0,
                "winner": cycle_row.get("winner", ""),
                "account_cash": cycle_row.get("account_cash", None),
            }
        )

    summary_df = pd.DataFrame(summary_rows)
    if not summary_df.empty:
        summary_df = summary_df.sort_values("cycle_slug").reset_index(drop=True)
    cycle_df = pd.concat(cycle_parts, ignore_index=True) if cycle_parts else pd.DataFrame()
    decision_df = pd.concat(decision_parts, ignore_index=True) if decision_parts else pd.DataFrame()

    # 根据参数决定是否生成交易过程 Excel
    trade_xlsx: Path | None = None
    if include_trade_process:
        trade_xlsx = write_batch_trade_process_zh(
            input_dir=input_resolved,
            cycle_df=cycle_df,
            decision_df=decision_df,
            output_path=output_resolved,
        )

    xlsx_report = build_performance_report_zh(
        resolved_batch_dir=output_resolved,
        summary_df=summary_df,
        cycle_df=cycle_df,
        decision_df=decision_df,
        output_path=output_resolved,
        display_batch_dir=input_resolved,
        cycle_range=cycle_range,
        cycle_count=cycle_count,
        report_source=report_source,
        report_name_prefix=report_name_prefix,
        capital_reset_mode=capital_reset_mode,
        starting_cash=starting_cash,
    )
    _cleanup_batch_replay_markdown(output_resolved)

    print(f"  ✓ 回放完成，共 {len(summary_df)} 个周期")
    
    # 验证报告文件是否存在
    if xlsx_report.exists():
        print(f"  ✓ 绩效报告: {xlsx_report}")
        return xlsx_report
    else:
        print(
            f"  ✗ 警告: 未找到生成的 Excel 报告文件: {xlsx_report}\n"
            f"     请检查 {output_resolved} 目录是否包含文件。",
            file=sys.stderr,
        )
        # 列出输出目录中的文件供调试
        if output_resolved.exists():
            files = list(output_resolved.glob("*.xlsx"))
            if files:
                print(f"  ℹ 输出目录中找到的 Excel 文件:", file=sys.stderr)
                for f in files:
                    print(f"     - {f.name}", file=sys.stderr)
        return None


def main() -> int:
    args = parse_args()
    xlsx_report = run_batch_replay(
        input_dir=args.input_dir,
        config_path=args.config,
        overrides_path=args.overrides,
        output_dir=args.output_dir,
        starting_cash=args.starting_cash,
        capital_reset_mode=args.capital_reset_mode,
        per_cycle_cash=args.per_cycle_cash,
        include_trade_process=args.include_trade_process,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
