from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.polymarket_live import (  # noqa: E402
    apply_proxy_env_from_dict,
    build_market_specs,
    iter_polymarket_trade_events_robot,
    iter_polymarket_trade_events,
    load_api_env,
)
from polynet_ai.execution.paper_broker import PaperBroker  # noqa: E402
from polynet_ai.engine.live import LivePaperRunner, LiveRunnerResult, export_live_result  # noqa: E402
from polynet_ai.engine.replay import ReplayEngine  # noqa: E402
from polynet_ai.strategy.router import StrategyRouter  # noqa: E402
from polynet_ai.strategy.spec import load_strategy_config  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket 实时行情 paper trading runner")
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--output-dir", default="artifacts/live/polymarket_live_outputs")
    parser.add_argument("--starting-cash", type=float, default=1000.0)
    parser.add_argument("--status-every", type=int, default=25)
    parser.add_argument("--max-cycles", type=int, default=10)
    parser.add_argument("--slug-prefix", default=None, help="例如 btc-updown-5m-")
    parser.add_argument("--robot-mode", action="store_true", help="自动识别新窗口并自动切换订阅")
    parser.add_argument("--poll-interval-seconds", type=float, default=3.0, help="机器人模式下的窗口发现轮询间隔")
    parser.add_argument("--dashboard-refresh-seconds", type=float, default=1.0, help="实时 dashboard 写盘间隔，<=0 表示仅结束时导出")
    parser.add_argument("--market-slugs", nargs="*", default=None, help="显式指定要跟踪的 market slug 列表")
    parser.add_argument("--market-slugs-file", default=None, help="每行一个 market slug")
    parser.add_argument(
        "--env-file",
        default=str(ROOT.parent / "APIs" / "ApiConfig.env"),
        help="API 配置文件路径。当前实时仿真只做读取校验，不使用私钥下真实单。",
    )
    return parser.parse_args()


def _load_market_slugs(args: argparse.Namespace) -> list[str] | None:
    slugs: list[str] = []
    if args.market_slugs:
        slugs.extend(str(item).strip() for item in args.market_slugs if str(item).strip())
    if args.market_slugs_file:
        file_path = Path(args.market_slugs_file)
        if not file_path.exists():
            raise FileNotFoundError(f"未找到 slug 文件: {file_path}")
        slugs.extend(line.strip() for line in file_path.read_text(encoding="utf-8").splitlines() if line.strip())
    return slugs or None


def _wrap_event_stream_with_config_reload(
    events,
    *,
    config_path: str | Path,
    engine: ReplayEngine,
) :
    cfg_path = Path(config_path)
    last_mtime = cfg_path.stat().st_mtime if cfg_path.exists() else None
    for event in events:
        if cfg_path.exists():
            current_mtime = cfg_path.stat().st_mtime
            if last_mtime is None:
                last_mtime = current_mtime
            elif current_mtime > last_mtime:
                config = load_strategy_config(cfg_path)
                engine.config = config
                engine.router = StrategyRouter(config)
                engine.broker = PaperBroker(
                    fee_rate=float(config.get("execution.fee_rate", 0.002)),
                    slippage_bps=float(config.get("execution.slippage_bps", 10)),
                )
                last_mtime = current_mtime
                print(f"[config] 检测到 strategy 配置变更，已热加载: {cfg_path}")
        yield event


def main() -> int:
    args = parse_args()
    market_slugs = _load_market_slugs(args)
    dashboard_title = "Polynet AI Live Monitoring Dashboard"

    if Path(args.env_file).exists():
        env_values = load_api_env(args.env_file)
        applied = apply_proxy_env_from_dict(env_values)
        print(f"检测到 API 配置文件: {Path(args.env_file)}")
        print(f"已读取 {len(env_values)} 个键；当前仿真仅使用公开行情，不会真实下单。")
        if applied:
            print(f"已从配置文件应用代理环境变量: {', '.join(applied)}")

    engine = ReplayEngine.from_yaml(args.config, starting_cash=args.starting_cash)
    runner = LivePaperRunner(engine)
    event_stream = None
    if args.robot_mode:
        if market_slugs:
            print("已提供 --market-slugs，机器人模式将按显式列表执行，不做自动发现。")
            specs = build_market_specs(
                market_slugs=market_slugs,
                slug_prefix=args.slug_prefix,
                max_cycles=args.max_cycles,
            )
            if not specs:
                raise RuntimeError("没有可用的实时市场可供订阅。")
            print("本次将跟踪以下周期:")
            for spec in specs:
                print(f"  - {spec.slug}")
            event_stream = iter_polymarket_trade_events(specs, log_fn=print)
        elif not args.slug_prefix:
            raise ValueError("机器人模式需要提供 --slug-prefix（例如 btc-updown-5m-）。")
        else:
            print(
                f"机器人模式已启用：将按前缀 `{args.slug_prefix}` 自动发现并切换窗口，"
                f"目标窗口数: {args.max_cycles}。"
            )
            event_stream = iter_polymarket_trade_events_robot(
                slug_prefix=args.slug_prefix,
                max_cycles=args.max_cycles,
                poll_interval_seconds=args.poll_interval_seconds,
                log_fn=print,
            )
    else:
        specs = build_market_specs(
            market_slugs=market_slugs,
            slug_prefix=args.slug_prefix,
            max_cycles=args.max_cycles,
        )
        if not specs:
            raise RuntimeError("没有可用的实时市场可供订阅。")
        print("本次将跟踪以下周期:")
        for spec in specs:
            print(f"  - {spec.slug}")
        event_stream = iter_polymarket_trade_events(specs, log_fn=print)

    event_stream = _wrap_event_stream_with_config_reload(
        event_stream,
        config_path=args.config,
        engine=engine,
    )

    progress_callback = None
    if args.dashboard_refresh_seconds > 0:
        print(f"实时 dashboard 已启用：每 {args.dashboard_refresh_seconds:.1f}s 写盘一次。")

        def _flush_progress(result: LiveRunnerResult) -> None:
            export_live_result(
                result,
                args.output_dir,
                title=dashboard_title,
                refresh_seconds=args.dashboard_refresh_seconds,
                write_excel=False,
            )

        progress_callback = _flush_progress

    result = runner.run_stream(
        event_stream,
        status_every=args.status_every,
        on_progress=progress_callback,
        progress_interval_seconds=max(0.0, args.dashboard_refresh_seconds),
    )
    export_live_result(
        result,
        args.output_dir,
        title=dashboard_title,
        refresh_seconds=max(1.0, args.dashboard_refresh_seconds) if args.dashboard_refresh_seconds > 0 else 1.0,
        write_excel=True,
    )

    cycle_count = len(result.replay_result.cycle_df)
    print(f"实时仿真完成，已落盘到: {args.output_dir}")
    print(f"完成周期数: {cycle_count}")
    print(result.replay_result.metrics_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
