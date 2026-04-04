#!/usr/bin/env python3
"""对比同一批录制事件下 legacy 滑点 paper 与 CLOB 订单簿 FOK paper 的回放结果（默认 10 周期）。

用法（需有效 ApiConfig.env 才能跑 orderbook 臂）::

    python scripts/compare_paper_execution_10_cycles.py \\
        --input-dir artifacts/live/record_job_market \\
        --env-file ../APIs/ApiConfig.env \\
        --config configs/strategy.yaml \\
        --max-cycles 10

无 env 时仅跑 legacy 臂并提示。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.cycle_window_timing import filter_trade_events_after_post_window_delay
from polynet_ai.adapters.trade_event_store import load_recorded_trade_events
from polynet_ai.engine.replay import ReplayEngine
from polynet_ai.execution.paper_broker import PaperBroker, paper_broker_for_config
from polynet_ai.strategy.spec import load_strategy_config, resolve_post_window_start_delay_seconds


def _cycle_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.name.rsplit("-", 1)[-1]
    try:
        return (int(suffix), path.name)
    except ValueError:
        return (0, path.name)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", required=True, help="含 btc-updown-5m-* 子目录的根路径")
    p.add_argument("--cycle-glob", default="btc-updown-5m-*")
    p.add_argument("--event-file-name", default="ws_trade_events.ndjson")
    p.add_argument("--max-cycles", type=int, default=10)
    p.add_argument("--config", default="configs/strategy.yaml")
    p.add_argument("--starting-cash", type=float, default=200.0)
    p.add_argument("--capital-reset-mode", choices=["fixed", "cumulative"], default="fixed")
    p.add_argument("--per-cycle-cash", type=float, default=None)
    p.add_argument("--env-file", default="", help="CLOB 凭证；空则跳过 orderbook 臂")
    p.add_argument("--account-index", type=int, default=2)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir).expanduser()
    if not input_dir.is_absolute():
        input_dir = (ROOT / input_dir).resolve()
    cycle_dirs = sorted(
        (p for p in input_dir.glob(args.cycle_glob) if p.is_dir()),
        key=_cycle_sort_key,
    )[: max(1, args.max_cycles)]
    if not cycle_dirs:
        print(f"未找到周期目录: {input_dir} / {args.cycle_glob}")
        return 2

    events: list = []
    for d in cycle_dirs:
        ev = load_recorded_trade_events(d / args.event_file_name)
        events.extend(ev)
    events.sort(key=lambda e: e.timestamp)
    cfg = load_strategy_config(args.config)
    pwd = resolve_post_window_start_delay_seconds(config=cfg, cli_seconds=None)
    if pwd > 0:
        events = filter_trade_events_after_post_window_delay(events, post_window_start_delay_seconds=pwd)
    if not events:
        print("过滤后无事件")
        return 2

    per_cycle = args.per_cycle_cash if args.per_cycle_cash is not None else args.starting_cash
    base_kw = dict(
        starting_cash=args.starting_cash,
        capital_reset_mode=args.capital_reset_mode,
        per_cycle_cash=float(per_cycle) if args.capital_reset_mode == "fixed" else None,
    )

    legacy_eng = ReplayEngine(
        cfg,
        broker=PaperBroker(
            fee_rate=float(cfg.get("execution.fee_rate", 0.002)),
            slippage_bps=float(cfg.get("execution.slippage_bps", 10)),
        ),
        **base_kw,
    )
    legacy_res = legacy_eng.run(list(events))
    legacy_perf = legacy_res.performance
    legacy_exec = int(legacy_res.decision_df["executed"].sum()) if "executed" in legacy_res.decision_df.columns else 0

    print("=== legacy（参考价±滑点）===")
    print(f"  cycles={len(legacy_res.cycle_df)} executed_signals={legacy_exec}")
    print(f"  total_net_profit={legacy_perf.total_net_profit:.6f} total_fees={legacy_perf.total_fees:.6f}")

    env_path = Path(args.env_file).expanduser() if str(args.env_file).strip() else None
    if env_path and not env_path.is_absolute():
        env_path = ROOT / env_path
    if not env_path or not env_path.is_file():
        print("\n未提供有效 --env-file，跳过 orderbook 臂对比。")
        return 0

    from polynet_ai.adapters.polymarket_live import load_api_env, select_account_env

    raw_env = load_api_env(str(env_path))
    sel = select_account_env(raw_env, account_index=args.account_index)
    try:
        ob_broker = paper_broker_for_config(
            cfg,
            env_values=sel,
            account_index=args.account_index,
            require_orderbook_client=True,
        )
    except ValueError as exc:
        print(f"\n无法构造 CLOB client: {exc}")
        return 1

    ob_eng = ReplayEngine(cfg, broker=ob_broker, **base_kw)
    ob_res = ob_eng.run(list(events))
    ob_perf = ob_res.performance
    ob_exec = int(ob_res.decision_df["executed"].sum()) if "executed" in ob_res.decision_df.columns else 0

    print("\n=== orderbook FOK（与实盘同前置校验+VWAP 成交）===")
    print(f"  cycles={len(ob_res.cycle_df)} executed_signals={ob_exec}")
    print(f"  total_net_profit={ob_perf.total_net_profit:.6f} total_fees={ob_perf.total_fees:.6f}")

    print("\n=== 差分（orderbook - legacy）===")
    print(f"  Δtotal_net_profit={ob_perf.total_net_profit - legacy_perf.total_net_profit:.6f}")
    print(f"  Δtotal_fees={ob_perf.total_fees - legacy_perf.total_fees:.6f}")
    print(f"  Δexecuted={ob_exec - legacy_exec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
