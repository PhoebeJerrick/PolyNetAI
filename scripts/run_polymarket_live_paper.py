from __future__ import annotations

import argparse
import atexit
import shutil
import sys
from collections.abc import Callable, Iterable
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
SRC = ROOT / "src"
# 必须把仓库根目录放进 sys.path，否则 `import scripts.*` 可能命中 site-packages 里
# pywin32 提供的同名命名空间包（…/win32/scripts），从而找不到本仓库的 scripts 包。
for _p in (SRC, ROOT, SCRIPTS_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from polynet_ai.adapters.cycle_window_timing import (  # noqa: E402
    cycle_seconds_from_market_slug,
    cycle_seconds_from_slug_prefix,
    next_bucket_start_utc,
    poll_until_success,
    sleep_until_utc_instant,
    trade_event_passes_post_window_delay,
)
from polynet_ai.domain.models import TradeEvent  # noqa: E402
from polynet_ai.adapters.polymarket_live import (  # noqa: E402
    OrderBookTopSnapshotEnricher,
    apply_proxy_env_from_dict,
    build_market_specs,
    fetch_market_spec,
    get_account_env_value,
    iter_polymarket_trade_events_robot,
    iter_polymarket_trade_events,
    load_api_env,
    resolve_default_api_config_env,
    select_account_env,
)
from polynet_ai.adapters.trade_event_store import (  # noqa: E402
    CycleTradeEventRecorder,
    TradeEventRecorder,
    export_recorded_trade_events_csv,
)
from polynet_ai.engine.live import LivePaperRunner, LiveRunnerResult, export_live_result  # noqa: E402
from polynet_ai.engine.replay import ReplayEngine  # noqa: E402
from polynet_ai.execution.paper_broker import paper_broker_for_config  # noqa: E402
from polynet_ai.execution.polymarket_auto_redeem import (  # noqa: E402
    load_auto_redeem_settings,
    redeem_report_to_audit_rows,
    run_auto_redeem_scan,
)
from polynet_ai.strategy.router import StrategyRouter  # noqa: E402
from polynet_ai.strategy.spec import load_strategy_config, resolve_post_window_start_delay_seconds  # noqa: E402

try:
    from scripts.batch_replay_recorded_trade_events import run_batch_replay  # noqa: E402
    from scripts.build_batch_replay_performance_report import (  # noqa: E402
        build_comparison_report_zh,
        build_performance_report_zh,
    )
except ImportError:
    from batch_replay_recorded_trade_events import run_batch_replay  # type: ignore
    from build_batch_replay_performance_report import (  # type: ignore
        build_comparison_report_zh,
        build_performance_report_zh,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket 实时行情 paper trading runner")
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--output-dir", default="artifacts/live/polymarket_live_outputs")
    parser.add_argument("--starting-cash", type=float, default=200.0)
    parser.add_argument("--per-cycle-cash", type=float, default=None,
                        help="固定模式下每周期实际投注资金；不设置则与 --starting-cash 相同")
    parser.add_argument(
        "--capital-reset-mode",
        type=str,
        choices=["fixed", "cumulative"],
        default="fixed",
        help="周期资金处理模式：fixed=每周期重置，cumulative=跨周期累积",
    )
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
        default=resolve_default_api_config_env(ROOT),
        help="API 配置文件路径。策略侧为 paper，不下 CLOB 真实单；默认会尝试 Relayer 自动赎回（需凭证），可用 --no-auto-redeem 关闭。",
    )
    parser.add_argument("--account-index", type=int, default=2, help="账号编号（默认 2）；会优先读取 PURSE_ADDRESS_2 等后缀键。")
    parser.add_argument(
        "--paper-execution",
        type=str,
        choices=["auto", "orderbook", "legacy"],
        default="auto",
        help="Paper 成交：auto=有 CLOB 凭证则与实盘同逻辑（订单簿 FOK）；orderbook=必须有凭证；legacy=参考价±滑点。",
    )
    parser.add_argument(
        "--record-events-dir",
        default=None,
        help="可选：按周期目录额外落盘实时事件流，输出结构兼容 record_job/<cycle_slug>/ws_trade_events.ndjson。",
    )
    parser.add_argument(
        "--record-orderbook-top",
        action="store_true",
        help="将 UP/Down 的买一卖一价格与挂单量附加写入 ws_trade_events.ndjson metadata（需可用 CLOB client）。",
    )
    parser.add_argument(
        "--orderbook-refresh-seconds",
        type=float,
        default=0.5,
        help="写盘口快照时的最小刷新间隔；0 表示每条事件都重新拉取 order book。",
    )
    parser.add_argument(
        "--no-wait-cycle-boundary",
        action="store_true",
        help="不在消费事件前等待下一个周期边界（按周期落盘时首个文件可能不完整）。",
    )
    parser.add_argument(
        "--post-window-start-delay-seconds",
        type=float,
        default=None,
        help="若指定则覆盖 strategy.yaml 的 cycle.post_window_start_delay_seconds。",
    )
    parser.add_argument(
        "--auto-redeem",
        action="store_true",
        dest="auto_redeem",
        help="自动赎回（默认已开启，可省略）。需 PURSE_PRIVATE_KEY_<N>、POLY_BUILDER_*_<N>（默认 N=2）与 pip install -e \".[redeem]\"。",
    )
    parser.add_argument(
        "--no-auto-redeem",
        action="store_false",
        dest="auto_redeem",
        help="关闭流式期间的 Data API + Relayer 自动赎回。",
    )
    parser.add_argument(
        "--redeem-poll-interval-seconds",
        type=float,
        default=180.0,
        help="redeem 定时扫描间隔（秒）；0 表示仅周期结束时触发。",
    )
    parser.set_defaults(auto_redeem=True)
    return parser.parse_args()


def _resolve_cycle_seconds_for_wait(args: argparse.Namespace, market_slugs: list[str] | None) -> int | None:
    if args.slug_prefix:
        sec = cycle_seconds_from_slug_prefix(str(args.slug_prefix))
        if sec is not None:
            return sec
    if market_slugs:
        return cycle_seconds_from_market_slug(str(market_slugs[0]))
    return None


def _maybe_wait_next_cycle_boundary(
    *,
    args: argparse.Namespace,
    market_slugs: list[str] | None,
    cycle_record_dir: Path | None,
) -> None:
    if args.no_wait_cycle_boundary:
        return
    if cycle_record_dir is None:
        return
    cycle_seconds = _resolve_cycle_seconds_for_wait(args, market_slugs)
    if cycle_seconds is None:
        print(
            "  ⚠ 无法从 --slug-prefix 或 market slug 解析周期长度，跳过「等到下一周期边界」。"
        )
        return
    boundary = next_bucket_start_utc(datetime.now(timezone.utc), cycle_seconds)
    print(
        f"  [对齐周期] 按周期落盘已启用：精确等待 UTC 桶切换 "
        f"{boundary.strftime('%Y-%m-%d %H:%M:%S')}Z ..."
    )
    sleep_until_utc_instant(boundary)
    sp = (args.slug_prefix or "").strip()
    if sp and cycle_seconds_from_slug_prefix(sp) is not None:
        expected_slug = f"{sp}{int(boundary.timestamp())}"
        print(f"  [对齐周期] 轮询 Gamma 直至新窗就绪: {expected_slug}")
        poll_until_success(
            lambda: fetch_market_spec(expected_slug),
            timeout_seconds=120.0,
            interval_seconds=0.35,
            log_fn=print,
            describe=f"Gamma 市场 {expected_slug}",
        )
        print(f"  ✓ 新窗已就绪: {expected_slug}")
    else:
        print(
            "  ℹ 未配置标准 btc-updown-Nm- 前缀，跳过 Gamma 新窗探测（仍依赖 slug 时间戳推迟策略生效）。"
        )


def _iter_record_full_socket_strategy_gated(
    events: Iterable[TradeEvent],
    *,
    delay_seconds: float,
    record_fn: Callable[[TradeEvent], None],
) -> Iterable[TradeEvent]:
    """每条 Socket 成交先落盘，再按窗起点+delay 决定是否交给策略（实盘验证按周期落盘时用）。"""
    for event in events:
        record_fn(event)
        if trade_event_passes_post_window_delay(event, delay_seconds):
            yield event


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
    env_values: dict[str, str] | None,
    account_index: int,
    paper_execution: str,
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
                engine.broker = paper_broker_for_config(
                    config,
                    env_values=env_values,
                    account_index=account_index,
                    force_legacy_slippage=paper_execution == "legacy",
                    require_orderbook_client=paper_execution == "orderbook",
                )
                last_mtime = current_mtime
                print(f"[config] 检测到 strategy 配置变更，已热加载: {cfg_path}")
        yield event


def _build_summary_from_live_result(
    replay_result,
    new_cycle_slugs: list[str],
):
    """从 LivePaperRunner 的流式处理结果中构建与批量回放兼容的 summary_df / cycle_df / decision_df。"""
    cycle_df = replay_result.cycle_df.copy()
    decision_df = replay_result.decision_df.copy()

    # 补齐 cycle_slug 列（与批量回放输出格式对齐）
    if "cycle_slug" not in cycle_df.columns and "cycle_id" in cycle_df.columns:
        cycle_df["cycle_slug"] = cycle_df["cycle_id"]
    if "cycle_slug" not in decision_df.columns and "cycle_id" in decision_df.columns:
        decision_df["cycle_slug"] = decision_df["cycle_id"]

    # 只保留本次新采集的周期
    if new_cycle_slugs:
        cycle_df = cycle_df[cycle_df["cycle_slug"].isin(new_cycle_slugs)].copy()
        decision_df = decision_df[decision_df["cycle_slug"].isin(new_cycle_slugs)].copy()

    summary_rows: list[dict] = []
    for _, crow in cycle_df.iterrows():
        slug = crow["cycle_slug"]
        cdec = decision_df[decision_df["cycle_slug"] == slug]

        executed = int(cdec["executed"].sum()) if "executed" in cdec.columns else 0
        risk_col = cdec["risk_status"] if "risk_status" in cdec.columns else pd.Series(dtype=str)
        accepted = int((risk_col == "accepted").sum())
        blocked = int((risk_col == "blocked").sum())
        fees = float(cdec["fill_fee"].sum()) if "fill_fee" in cdec.columns else 0.0
        net_profit = float(crow.get("cycle_net_profit", 0.0))

        summary_rows.append({
            "cycle_slug": slug,
            "event_count": len(cdec),
            "executed_trades": executed,
            "accepted_signals": accepted,
            "blocked_signals": blocked,
            "total_net_profit": net_profit,
            "total_fees": fees,
            "win_rate": 1.0 if net_profit > 0 else 0.0,
            "winner": crow.get("winner", ""),
            "account_cash": crow.get("account_cash", None),
        })

    summary_df = pd.DataFrame(summary_rows)
    if not summary_df.empty:
        summary_df = summary_df.sort_values("cycle_slug").reset_index(drop=True)

    return summary_df, cycle_df, decision_df


def main() -> int:
    args = parse_args()
    start_time = datetime.now()
    print("\n" + "="*70)
    print(f"[实盘行情验证] 开始于 {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)
    
    market_slugs = _load_market_slugs(args)
    dashboard_title = "Polynet AI Live Monitoring Dashboard"
    
    # 用于追踪各阶段的耗时
    stage_times = {}

    stage_1_start = datetime.now()
    print(f"\n[1/7] 初始化环境配置...")
    env_values: dict[str, str] = {}
    if Path(args.env_file).exists():
        env_values = load_api_env(args.env_file)
        selected_env = select_account_env(env_values, account_index=args.account_index)
        applied = apply_proxy_env_from_dict(selected_env)
        _purse = get_account_env_value(env_values, "PURSE_ADDRESS", account_index=args.account_index)
        print(f"  ✓ API 配置已加载，账号: {args.account_index}")
        if applied:
            print(f"  ✓ 代理配置已应用: {', '.join(applied)}")
    stage_times["1_init_env"] = (datetime.now() - stage_1_start).total_seconds()

    stage_2_start = datetime.now()
    print(f"\n[2/7] 初始化回放引擎...")
    _live_cfg = load_strategy_config(args.config)
    _broker_env = (
        select_account_env(env_values, account_index=args.account_index) if env_values else None
    )
    _paper_broker = paper_broker_for_config(
        _live_cfg,
        env_values=_broker_env,
        account_index=args.account_index,
        force_legacy_slippage=args.paper_execution == "legacy",
        require_orderbook_client=args.paper_execution == "orderbook",
    )
    engine = ReplayEngine(
        _live_cfg,
        starting_cash=args.starting_cash,
        capital_reset_mode=args.capital_reset_mode,
        per_cycle_cash=args.per_cycle_cash,
        broker=_paper_broker,
    )
    runner = LivePaperRunner(engine)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _pmode = (
        "legacy_slippage"
        if _paper_broker.clob_client is None
        else "orderbook_fok"
    )
    print(
        f"  ✓ 引擎已初始化，初始资金: {args.starting_cash} USDT | "
        f"资金模式: {args.capital_reset_mode} | paper 执行: {_pmode} (--paper-execution {args.paper_execution})"
    )
    print(f"  ✓ 输出目录: {args.output_dir}")
    stage_times["2_init_engine"] = (datetime.now() - stage_2_start).total_seconds()

    orderbook_metadata_enricher = None
    if args.record_orderbook_top:
        if _paper_broker.clob_client is None:
            print("  ⚠ 已启用盘口快照落盘，但当前无可用 CLOB client，将跳过买一卖一补充")
        else:
            refresh_seconds = max(0.0, float(args.orderbook_refresh_seconds))
            orderbook_metadata_enricher = OrderBookTopSnapshotEnricher(
                _paper_broker.clob_client,
                refresh_interval_seconds=refresh_seconds,
                log_fn=print,
            ).enrich
            print(f"  ✓ 原始事件将附带 UP/Down 买一卖一快照（最小刷新间隔 {refresh_seconds:g}s）")

    redeem_settings = None
    if args.auto_redeem:
        redeem_settings = load_auto_redeem_settings(env_values, account_index=args.account_index)
        if redeem_settings is None:
            print(
                "  ⚠ 默认已尝试启用自动赎回，但缺少带账号后缀的 PURSE_PRIVATE_KEY_<N>、PURSE_ADDRESS_<N> 或 POLY_BUILDER_*_<N>（默认 N=2），将跳过赎回；"
                " 不需要时可加 --no-auto-redeem。",
                flush=True,
            )
        else:
            print(
                f"  ✓ 自动赎回已启用（轮询 {max(0.0, float(args.redeem_poll_interval_seconds)):g}s；"
                " 需 pip install -e \".[redeem]\"）；明细见 redeem_audit 表/CSV",
                flush=True,
            )
    redeem_audit_rows: list[dict[str, object]] = []
    on_cycle_redeem = None
    redeem_poll_handler = None
    redeem_poll_sec = 0.0
    if redeem_settings is not None:

        def redeem_poll_handler() -> None:
            t0 = datetime.now(timezone.utc)
            report = run_auto_redeem_scan(redeem_settings, priority_condition_ids=None)
            t1 = datetime.now(timezone.utc)
            redeem_audit_rows.extend(
                redeem_report_to_audit_rows(
                    report, utc_start=t0, utc_end=t1, trigger="poll"
                )
            )

        def on_cycle_redeem(row: dict[str, object]) -> None:
            t0 = datetime.now(timezone.utc)
            cid = str(row.get("condition_id") or "").strip()
            report = run_auto_redeem_scan(
                redeem_settings,
                priority_condition_ids=[cid] if cid else None,
            )
            t1 = datetime.now(timezone.utc)
            redeem_audit_rows.extend(
                redeem_report_to_audit_rows(
                    report,
                    utc_start=t0,
                    utc_end=t1,
                    trigger="cycle_complete",
                    finalized_cycle_slug=str(row.get("cycle_id") or ""),
                    priority_condition_id=cid,
                )
            )

        redeem_poll_sec = max(0.0, float(args.redeem_poll_interval_seconds))

    cycle_record_dir = Path(args.record_events_dir) if args.record_events_dir else None
    run_start_timestamp = datetime.now()
    old_cycle_dirs = set()
    if cycle_record_dir is not None:
        print(f"  ℹ 实时事件落盘目录: {cycle_record_dir}")
        cycle_record_dir.mkdir(parents=True, exist_ok=True)
        # 记录现有周期，以便后续识别新周期
        old_cycle_dirs = set(d.name for d in cycle_record_dir.glob("btc-updown-5m-*") if d.is_dir())
        if old_cycle_dirs:
            print(f"  ℹ 发现 {len(old_cycle_dirs)} 个历史周期目录（将保留）")

        _crd = cycle_record_dir.resolve()

        def _cleanup_polymarket_record_job_on_exit() -> None:
            try:
                from scripts.build_batch_replay_performance_report import (
                    cleanup_polymarket_live_record_job_artifacts,
                )
            except ImportError:
                from build_batch_replay_performance_report import (  # type: ignore
                    cleanup_polymarket_live_record_job_artifacts,
                )
            cleanup_polymarket_live_record_job_artifacts(_crd)

        atexit.register(_cleanup_polymarket_record_job_on_exit)

    stage_3_start = datetime.now()
    print(f"\n[3/7] 建立 Polymarket 实时行情订阅...")
    _strategy_cfg = load_strategy_config(args.config)
    _pwd = resolve_post_window_start_delay_seconds(
        config=_strategy_cfg,
        cli_seconds=args.post_window_start_delay_seconds,
    )
    _pwd_src = (
        "命令行覆盖"
        if args.post_window_start_delay_seconds is not None
        else "strategy.yaml cycle.post_window_start_delay_seconds"
    )
    _record_full_ws = cycle_record_dir is not None
    _iter_ws_delay = 0.0 if _record_full_ws else _pwd
    if _record_full_ws:
        print(f"  ℹ 策略推迟: {_pwd:.1f}s（{_pwd_src}）；Socket 订阅不推迟，落盘为全量原始流")
    else:
        print(f"  ℹ 窗绝对起点后策略/落盘推迟: {_pwd:.1f}s（{_pwd_src}）")
    _maybe_wait_next_cycle_boundary(
        args=args,
        market_slugs=market_slugs,
        cycle_record_dir=cycle_record_dir,
    )
    event_stream = None
    if args.robot_mode:
        if market_slugs:
            print("  ℹ 机器人模式：将按显式列表执行，不做自动发现")
            specs = build_market_specs(
                market_slugs=market_slugs,
                slug_prefix=args.slug_prefix,
                max_cycles=args.max_cycles,
            )
            if not specs:
                raise RuntimeError("没有可用的实时市场可供订阅。")
            print(f"  ✓ 将跟踪 {len(specs)} 个周期")
            event_stream = iter_polymarket_trade_events(
                specs,
                metadata_enricher=orderbook_metadata_enricher,
                post_window_start_delay_seconds=_iter_ws_delay,
                log_fn=print,
            )
        elif not args.slug_prefix:
            raise ValueError("机器人模式需要提供 --slug-prefix（例如 btc-updown-5m-）。")
        else:
            print(
                f"  ℹ 机器人模式：按前缀 `{args.slug_prefix}` 自动发现并切换窗口"
            )
            print(f"  ℹ 目标周期数: {args.max_cycles}")
            event_stream = iter_polymarket_trade_events_robot(
                slug_prefix=args.slug_prefix,
                max_cycles=args.max_cycles,
                metadata_enricher=orderbook_metadata_enricher,
                poll_interval_seconds=args.poll_interval_seconds,
                post_window_start_delay_seconds=_iter_ws_delay,
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
        print(f"  ✓ 发现 {len(specs)} 个市场")
        event_stream = iter_polymarket_trade_events(
            specs,
            metadata_enricher=orderbook_metadata_enricher,
            post_window_start_delay_seconds=_iter_ws_delay,
            log_fn=print,
        )

    event_stream = _wrap_event_stream_with_config_reload(
        event_stream,
        config_path=args.config,
        engine=engine,
        env_values=_broker_env,
        account_index=args.account_index,
        paper_execution=args.paper_execution,
    )
    stage_times["3_subscribe"] = (datetime.now() - stage_3_start).total_seconds()

    progress_callback = None
    if args.dashboard_refresh_seconds > 0:
        print(f"  ✓ 实时 dashboard：每 {args.dashboard_refresh_seconds:.1f}s 刷新")

        def _flush_progress(result: LiveRunnerResult) -> None:
            export_live_result(
                result,
                args.output_dir,
                title=dashboard_title,
                refresh_seconds=args.dashboard_refresh_seconds,
                write_excel=False,
            )

        progress_callback = _flush_progress

    recorded_events_path = output_dir / "ws_trade_events.ndjson"
    recorded_events_csv_path = output_dir / "ws_trade_events.csv"
    if cycle_record_dir is not None:
        print(f"  ✓ 实时事件落盘目录: {cycle_record_dir}")

    def _record_event(event: TradeEvent) -> None:
        event_recorder.record(event)
        if cycle_event_recorder is not None:
            cycle_event_recorder.record(event)

    stage_4_start = datetime.now()
    print(f"\n[4/7] 开始实时行情回放...")
    print(f"  ℹ 请稍候，连接中...")
    run_start_time = datetime.now()
    
    with TradeEventRecorder(recorded_events_path) as event_recorder, (
        CycleTradeEventRecorder(cycle_record_dir) if cycle_record_dir is not None else nullcontext()
    ) as cycle_event_recorder:
        stream_in = (
            _iter_record_full_socket_strategy_gated(
                event_stream,
                delay_seconds=_pwd,
                record_fn=_record_event,
            )
            if cycle_record_dir is not None
            else event_stream
        )
        result = runner.run_stream(
            stream_in,
            status_every=args.status_every,
            on_event=None if cycle_record_dir is not None else _record_event,
            on_progress=progress_callback,
            progress_interval_seconds=max(0.0, args.dashboard_refresh_seconds),
            on_cycle_complete=on_cycle_redeem,
            redeem_poll_interval_seconds=redeem_poll_sec,
            on_redeem_poll=redeem_poll_handler if redeem_settings is not None and redeem_poll_sec > 0 else None,
        )
    run_elapsed = (datetime.now() - run_start_time).total_seconds()
    stage_times["4_replay"] = run_elapsed
    
    cycle_count = len(result.replay_result.cycle_df)
    print(f"  ✓ 行情回放完成 (耗时 {run_elapsed:.1f}s)")
    print(f"  ✓ 已处理周期: {cycle_count} 个（回放引擎 metrics/cycles 口径）")
    if args.robot_mode and int(args.max_cycles) > 0:
        print(
            "  ℹ --max-cycles 控制机器人串联的市场窗数量；上式「已处理周期」只统计"
            "「至少有一条通过策略门控（窗起点+post_window_start_delay）后成交」的窗。"
            "若下一窗在门控生效前几乎没有 WS 成交、或连接过早结束，入账周期数会少于 max_cycles。",
            flush=True,
        )

    stage_5_start = datetime.now()
    print(f"\n[5/7] 导出结果数据...")
    export_recorded_trade_events_csv(recorded_events_path, recorded_events_csv_path)
    redeem_audit_df = pd.DataFrame(redeem_audit_rows) if redeem_audit_rows else None
    if redeem_audit_df is not None and redeem_audit_df.empty:
        redeem_audit_df = None
    export_live_result(
        result,
        args.output_dir,
        title=dashboard_title,
        refresh_seconds=max(1.0, args.dashboard_refresh_seconds) if args.dashboard_refresh_seconds > 0 else 1.0,
        write_excel=True,
        redeem_audit_df=redeem_audit_df,
    )
    print(f"  ✓ 数据已导出")
    stage_times["5_export"] = (datetime.now() - stage_5_start).total_seconds()

    stage_6_start = datetime.now()
    live_report_path: Path | None = None
    replay_temp_dir: Path | None = None
    new_cycle_slugs: list[str] = []
    if cycle_record_dir is not None and cycle_record_dir.exists():
        all_cycle_dirs = list(cycle_record_dir.glob("btc-updown-5m-*"))
        new_cycle_dirs = [d for d in all_cycle_dirs if d.name not in old_cycle_dirs]
        new_cycle_files = [d / "ws_trade_events.ndjson" for d in new_cycle_dirs if (d / "ws_trade_events.ndjson").exists()]
        
        if new_cycle_files:
            print(f"\n[6/7] 生成实盘验证绩效报告...")
            print(f"  ℹ 发现 {len(new_cycle_files)} 个新周期的实时事件流，正在回放分析...")
            if len(new_cycle_files) != cycle_count:
                print(
                    f"  ℹ 提示: 新落盘子目录数 ({len(new_cycle_files)}) 与引擎入账周期数 ({cycle_count}) 不一致时，"
                    "多为某一窗无「门控后」引擎事件但仍写入了原始 WS 落盘。",
                    flush=True,
                )
            report_start = datetime.now()
            try:
                # 创建临时目录用于仅放新周期数据
                temp_replay_dir = cycle_record_dir / f"replay_new_cycles_{run_start_timestamp.strftime('%Y%m%d_%H%M%S')}"
                temp_replay_dir.mkdir(exist_ok=True)
                replay_temp_dir = temp_replay_dir
                
                # 复制新周期数据到临时目录
                for cycle_dir in new_cycle_dirs:
                    temp_cycle_dir = temp_replay_dir / cycle_dir.name
                    if not temp_cycle_dir.exists():
                        shutil.copytree(cycle_dir, temp_cycle_dir)
                
                new_cycle_slugs = sorted([d.name for d in new_cycle_dirs])
                
                live_report_path = run_batch_replay(
                    input_dir=temp_replay_dir,
                    config_path=args.config,
                    output_dir=cycle_record_dir / "batch_replay_outputs",
                    starting_cash=args.starting_cash,
                    capital_reset_mode=args.capital_reset_mode,
                    include_trade_process=False,
                    cycle_count=len(new_cycle_files),
                    report_source="实盘行情验证",
                    report_name_prefix="real",
                    post_window_start_delay_seconds=_pwd,
                )
                report_elapsed = (datetime.now() - report_start).total_seconds()
                stage_times["6_report"] = report_elapsed
                print(f"  ✓ 实盘验证绩效报告已生成 (耗时 {report_elapsed:.1f}s)")
                if live_report_path and live_report_path.exists():
                    print(f"  ✓ 报告位置: {live_report_path}")
                    print(f"  ℹ 新周期数: {len(new_cycle_files)}")
                else:
                    print(f"  ⚠ 警告: 实盘验证报告生成可能失败，请检查输出目录")
            except Exception as e:
                print(f"  ✗ 实盘验证报告生成失败: {e}", file=sys.stderr)
        else:
            stage_times["6_report"] = (datetime.now() - stage_6_start).total_seconds()
            if all_cycle_dirs:
                print(f"\n[6/7] 无新周期数据（共 {len(all_cycle_dirs)} 个历史周期保留）")
            else:
                print(f"\n[6/7] 无周期数据（未启用周期事件落盘或无新数据）")
    else:
        stage_times["6_report"] = (datetime.now() - stage_6_start).total_seconds()

    # --- [7/7] per-cycle 模拟下单 vs per-cycle 实盘验证 对比报告 ---
    stage_7_start = datetime.now()
    if live_report_path and live_report_path.exists() and replay_temp_dir is not None:
        comparison_output_dir = cycle_record_dir / "batch_replay_outputs" if cycle_record_dir else Path("artifacts/live/record_job_market/batch_replay_outputs")
        n_live_cycles = len(new_cycle_slugs) if new_cycle_slugs else 0
        if n_live_cycles > 0:
            print(f"\n[7/7] 生成「per-cycle 模拟下单 vs per-cycle 实盘验证」对比报告...")
            print(f"  ℹ 两侧均使用 per-cycle（每周期独立引擎）口径进行对比")
            print(f"  ℹ 周期数: {n_live_cycles}")
            try:
                sim_report_path = run_batch_replay(
                    input_dir=replay_temp_dir,
                    config_path=args.config,
                    output_dir=comparison_output_dir,
                    starting_cash=args.starting_cash,
                    capital_reset_mode=args.capital_reset_mode,
                    per_cycle_cash=args.per_cycle_cash,
                    include_trade_process=False,
                    cycle_count=n_live_cycles,
                    report_source="实盘行情（模拟下单 per-cycle）",
                    report_name_prefix="simulation",
                    post_window_start_delay_seconds=_pwd,
                    processing_mode="per-cycle",
                )
                if sim_report_path and sim_report_path.exists():
                    print(f"  ✓ 模拟下单(per-cycle)绩效报告: {sim_report_path.name}")
                    comparison_path = build_comparison_report_zh(
                        sim_report_path=sim_report_path,
                        live_report_path=live_report_path,
                        output_path=comparison_output_dir,
                    )
                    compare_elapsed = (datetime.now() - stage_7_start).total_seconds()
                    stage_times["7_comparison"] = compare_elapsed
                    print(f"  ✓ 对比报告已生成 (耗时 {compare_elapsed:.1f}s)")
                    print(f"  ✓ 对比报告位置: {comparison_path}")
                else:
                    print(f"  ⚠ 流式处理绩效报告生成失败，跳过对比")
                    stage_times["7_comparison"] = (datetime.now() - stage_7_start).total_seconds()
            except Exception as e:
                print(f"  ✗ 对比报告生成失败: {e}", file=sys.stderr)
                import traceback
                traceback.print_exc()
                stage_times["7_comparison"] = (datetime.now() - stage_7_start).total_seconds()
        else:
            print(f"\n[7/7] 无新周期数据，跳过对比")
            stage_times["7_comparison"] = (datetime.now() - stage_7_start).total_seconds()
    else:
        stage_times["7_comparison"] = (datetime.now() - stage_7_start).total_seconds()
    
    total_elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n{'='*70}")
    print("[进度汇总]")
    print(f"  [1/7] 初始化环境:     {stage_times.get('1_init_env', 0):.2f}s")
    print(f"  [2/7] 初始化引擎:     {stage_times.get('2_init_engine', 0):.2f}s")
    print(f"  [3/7] 建立订阅:       {stage_times.get('3_subscribe', 0):.2f}s")
    print(f"  [4/7] 实时回放:       {stage_times.get('4_replay', 0):.2f}s  【处理 {cycle_count} 个周期】")
    print(f"  [5/7] 导出数据:       {stage_times.get('5_export', 0):.2f}s")
    if stage_times.get('6_report', 0) > 0:
        print(f"  [6/7] 实盘报告:       {stage_times.get('6_report', 0):.2f}s")
    if stage_times.get('7_comparison', 0) > 0:
        print(f"  [7/7] 对比报告:       {stage_times.get('7_comparison', 0):.2f}s")
    print(f"  {'-'*66}")
    print(f"  总耗时:              {total_elapsed:.2f}s  (共 {int(total_elapsed//60)}分 {total_elapsed%60:.0f}秒)")
    print(f"{'='*70}")
    print(f"\n关键指标汇总:")
    print(result.replay_result.metrics_df.to_string(index=False))
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
