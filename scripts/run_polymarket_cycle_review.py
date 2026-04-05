from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.cycle_window_timing import (  # noqa: E402
    current_or_next_bucket_start_utc,
    cycle_seconds_from_slug_prefix,
    next_bucket_start_utc,
    poll_until_success,
    sleep_until_utc_instant,
    window_start_naive_utc_from_slug,
)
from polynet_ai.adapters.polymarket_live import (  # noqa: E402
    apply_proxy_env_from_dict,
    default_api_config_env_candidates,
    fetch_market_spec,
    get_account_env_value,
    iter_polymarket_trade_events,
    load_api_env,
    resolve_default_api_config_env,
    select_account_env,
)
from polynet_ai.adapters.trade_event_store import (  # noqa: E402
    TradeEventRecorder,
    export_recorded_trade_events_csv,
)
from polynet_ai.engine.live import LivePaperRunner, export_live_result  # noqa: E402
from polynet_ai.engine.replay import ReplayEngine  # noqa: E402
from polynet_ai.execution.polymarket_auto_redeem import (  # noqa: E402
    load_auto_redeem_settings,
    redeem_report_to_audit_rows,
    run_auto_redeem_scan,
)
from polynet_ai.execution.paper_broker import paper_broker_for_config  # noqa: E402
from polynet_ai.execution.polymarket_broker import PolymarketBroker  # noqa: E402
from polynet_ai.reporting.excel_export import get_version_tag  # noqa: E402
from polynet_ai.strategy.spec import (  # noqa: E402
    load_strategy_config,
    resolve_post_window_start_delay_seconds,
)

DATA_API_BASE = "https://data-api.polymarket.com"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行完整 5m 周期并复盘；支持单进程连续多窗（--max-cycles，对齐 robot 式连续跟窗）。"
    )
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument(
        "--overrides",
        default=None,
        help="可选：trial 的 overrides.json；不传则仅使用 --config（避免依赖本机未必存在的 optimization 产物）。",
    )
    parser.add_argument("--output-dir", default="artifacts/live/polymarket_cycle_review")
    parser.add_argument(
        "--run-subdir",
        default="",
        help="输出子目录名；默认空表示使用当前市场 slug（每窗独立目录）。"
        " 设为固定名（如 current）便于外部 dashboard 始终指向同一目录。",
    )
    parser.add_argument("--slug-prefix", default="btc-updown-5m-")
    parser.add_argument("--starting-cash", type=float, default=200.0)
    parser.add_argument(
        "--capital-reset-mode",
        type=str,
        choices=["fixed", "cumulative"],
        default="cumulative",
        help="周期资金模式：fixed=每周期重置名义本金（与批量 paper 一致）；cumulative=跨周期累积。",
    )
    parser.add_argument(
        "--per-cycle-cash",
        type=float,
        default=None,
        help="fixed 模式下每周期名义本金；默认与 --starting-cash 相同。",
    )
    parser.add_argument(
        "--min-collateral-usdc",
        type=float,
        default=None,
        help="仅 --real-trading 且 fixed：抵押 USDC（余额减挂单预留）低于该值则跳过本窗；默认等于 per-cycle-cash；传 0 关闭。",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=1,
        help="在同一进程内连续执行多少个完整窗（每窗仍会等 UTC 桶切换）；默认 1。",
    )
    parser.add_argument("--real-trading", action="store_true", help="启用真实下单；否则仅做 paper。")
    parser.add_argument("--signature-type", type=int, default=2, help="Polymarket signature type，默认 2（Gnosis Safe / proxy wallet）。")
    parser.add_argument("--status-every", type=int, default=25)
    parser.add_argument("--dashboard-refresh-seconds", type=float, default=1.0)
    parser.add_argument(
        "--start-buffer-seconds",
        type=float,
        default=None,
        help="若指定则覆盖 strategy.yaml 的 cycle.post_window_start_delay_seconds。",
    )
    parser.add_argument("--cycle-grace-seconds", type=float, default=20.0)
    parser.add_argument(
        "--account-index",
        type=int,
        default=2,
        help="账号编号（默认 2）；实盘读取 PURSE_PRIVATE_KEY_2、PURSE_ADDRESS_2、POLY_DERIVE_API_*_2 等后缀键（优先于无后缀键）。",
    )
    parser.add_argument(
        "--auto-redeem",
        action="store_true",
        dest="auto_redeem",
        help="自动赎回（默认已开启，可省略）。依赖带账号后缀的 PURSE_*_<N>、POLY_BUILDER_*_<N>（默认 N=2）与 pip install -e \".[redeem]\"。",
    )
    parser.add_argument(
        "--no-auto-redeem",
        action="store_false",
        dest="auto_redeem",
        help="关闭自动赎回（Data API + Relayer gasless redeem）。",
    )
    parser.add_argument(
        "--redeem-poll-interval-seconds",
        type=float,
        default=180.0,
        help="流式运行期间 redeem 轮询间隔（秒）；0 表示仅周期结束触发。",
    )
    parser.add_argument(
        "--env-file",
        default=resolve_default_api_config_env(ROOT),
        help="API 配置文件路径。未加 --real-trading 时仅拉取成交/代理；加 --real-trading 时会读取私钥与 CLOB 凭证并真实下单。",
    )
    parser.set_defaults(auto_redeem=True)
    return parser.parse_args()


def _format_elapsed(ts: datetime, cycle_start: datetime) -> str:
    total_seconds = max(0, int((ts - cycle_start).total_seconds()))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}分{seconds:02d}秒"


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_overrides(path: str | Path) -> dict[str, Any]:
    override_path = Path(path)
    with override_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"overrides 必须是 JSON 对象: {override_path}")
    return data


def _build_engine(
    config_path: str | Path,
    overrides_path: str | Path | None,
    starting_cash: float,
) -> ReplayEngine:
    config = load_strategy_config(config_path)
    if overrides_path:
        overrides = _load_overrides(overrides_path)
        config = config.with_overrides(overrides)
    return ReplayEngine(config, starting_cash=starting_cash)


def _build_strategy_config(config_path: str | Path, overrides_path: str | Path | None):
    config = load_strategy_config(config_path)
    if not overrides_path:
        return config
    overrides = _load_overrides(overrides_path)
    return config.with_overrides(overrides)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "PolyNetAI/1.0"})
    return session


def _fetch_trades_pages(
    *,
    market: str,
    user: str | None = None,
    taker_only: bool = False,
    side: str | None = None,
    limit: int = 10000,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    session = _session()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for page in range(max_pages):
        params: dict[str, Any] = {
            "market": market,
            "limit": limit,
            "offset": page * limit,
            "takerOnly": str(bool(taker_only)).lower(),
        }
        if user:
            params["user"] = user
        if side:
            params["side"] = side
        response = session.get(f"{DATA_API_BASE}/trades", params=params, timeout=(15, 90))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if not isinstance(item, dict):
                continue
            dedupe_key = (
                item.get("transactionHash"),
                item.get("timestamp"),
                item.get("asset"),
                item.get("side"),
                item.get("price"),
                item.get("size"),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rows.append(item)
        if len(payload) < limit:
            break
    return rows


def _filter_cycle_window(rows: list[dict[str, Any]], *, cycle_start: datetime, cycle_end: datetime) -> list[dict[str, Any]]:
    start_s = int(cycle_start.timestamp())
    end_s = int(cycle_end.timestamp())
    filtered: list[dict[str, Any]] = []
    for item in rows:
        ts = int(item.get("timestamp") or 0)
        if ts > 10_000_000_000:
            ts = ts // 1000
        if start_s <= ts <= end_s:
            filtered.append(item)
    filtered.sort(key=lambda row: int(row.get("timestamp") or 0))
    return filtered


def _rows_to_trade_excel(rows: list[dict[str, Any]], *, cycle_slug: str, cycle_start: datetime) -> pd.DataFrame:
    out_rows: list[dict[str, Any]] = []
    for item in rows:
        raw_ts = int(item.get("timestamp") or 0)
        ts = datetime.fromtimestamp((raw_ts / 1000) if raw_ts > 10_000_000_000 else raw_ts, tz=timezone.utc)
        outcome_raw = str(item.get("outcome") or "").strip().lower()
        outcome = "Up" if outcome_raw in {"yes", "up"} else "Down"
        side = str(item.get("side") or "").strip().upper() or "BUY"
        out_rows.append(
            {
                "下注时间距开盘差(分，秒)": _format_elapsed(ts, cycle_start),
                "币种": "BTC",
                "市场标题": str(item.get("title") or item.get("eventSlug") or ""),
                "时间周期": cycle_slug,
                "结果代币类型": outcome,
                "操作方向": side,
                "投注份数": float(item.get("size") or 0.0),
                "成交价格": float(item.get("price") or 0.0),
                "时间": ts.isoformat(),
                "交易哈希": str(item.get("transactionHash") or ""),
                "资产ID": str(item.get("asset") or ""),
            }
        )
    return pd.DataFrame(out_rows)


def _summarize_trade_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["结果代币类型", "操作方向", "成交笔数", "总份数", "均价"])
    summary = (
        df.groupby(["结果代币类型", "操作方向"], dropna=False)
        .agg(
            成交笔数=("投注份数", "size"),
            总份数=("投注份数", "sum"),
            均价=("成交价格", "mean"),
        )
        .reset_index()
    )
    return summary


def _write_review_summary(
    output_path: Path,
    *,
    account_index: int,
    purse_address: str | None,
    cycle_slug: str,
    cycle_start: datetime,
    cycle_end: datetime,
    user_trade_df: pd.DataFrame,
    market_trade_df: pd.DataFrame,
    paper_decision_df: pd.DataFrame,
    mode_label: str,
) -> None:
    executed = paper_decision_df.copy()
    if "executed" in executed.columns:
        executed = executed[executed["executed"] == True].copy()
    paper_summary = pd.DataFrame(columns=["selected_outcome", "selected_action", "成交笔数", "总份数", "均价"])
    if not executed.empty:
        paper_summary = (
            executed.groupby(["selected_outcome", "selected_action"], dropna=False)
            .agg(
                成交笔数=("selected_shares", "size"),
                总份数=("selected_shares", "sum"),
                均价=("fill_price", "mean"),
            )
            .reset_index()
        )

    lines = [
        "# 单周期实盘复盘摘要",
        "",
        "## 成交与对账文件",
        "",
        "- `strategy_fills_audit.csv`：引擎入账的每笔成交及 `fill_source`（exchange_get_order / exchange_get_order_estimate / data_api_trades / timeout_estimate / paper_simulated）。",
        "- `real_order_attempts.json`：CLOB 下单与确认过程；确认后含 `fill_source`。",
        "- `account_trades_*.xlsx` / Data API：交易所侧成交，可与上两者核对。",
        "- `ws_trade_events.ndjson` / `ws_trade_events.csv`：本脚本订阅到的**原始 WS 成交事件流**（逐条落盘）。",
        "- `redeem_audit.csv` 及 Excel 工作表 `redeem_audit`：每次赎回扫描的 UTC 时间窗、`trigger`（poll / cycle_complete / collateral_guard）、各 condition 的 `outcome`。",
        "",
        f"- 账号: {account_index}",
        f"- PURSE_ADDRESS: {purse_address or '未配置'}",
        f"- 周期: {cycle_slug}",
        f"- 开始(UTC): {cycle_start.isoformat()}",
        f"- 结束(UTC): {cycle_end.isoformat()}",
        f"- {mode_label} 已执行笔数: {len(executed)}",
        f"- 用户实际成交笔数: {len(user_trade_df)}",
        f"- 市场原始成交条数: {len(market_trade_df)}",
        "",
        f"## {mode_label} 执行汇总",
        "",
    ]
    if paper_summary.empty:
        lines.append("无已执行 paper 成交。")
    else:
        lines.extend(paper_summary.to_string(index=False).splitlines())
    lines.extend(["", "## 用户实际成交汇总", ""])
    user_summary = _summarize_trade_frame(user_trade_df)
    if user_summary.empty:
        lines.append("该周期未抓到账号成交。")
    else:
        lines.extend(user_summary.to_string(index=False).splitlines())
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _execute_one_polymarket_cycle(
    args: argparse.Namespace,
    *,
    strategy_config: Any,
    post_delay: float,
    cycle_seconds: int,
    per_cycle: float,
    min_collateral: float,
    redeem_settings: Any,
    real_broker: PolymarketBroker | None,
    purse_address: str,
    cycle_ix: int,
    max_cycles: int,
    env_values: dict[str, str],
) -> None:
    redeem_audit_rows: list[dict[str, object]] = []

    spec = None
    target_start: datetime | None = None
    # 连续多周期时允许迟到加入：只要在 opening_entry.window_seconds 内即可
    max_late_join = float(strategy_config.get("opening_entry.window_seconds", 50.0))
    while True:
        if cycle_ix == 0:
            # 首个周期：等待下一个桶边界（传统行为）
            target_start = next_bucket_start_utc(datetime.now(timezone.utc), cycle_seconds)
        else:
            # 后续周期：优先加入当前正在进行的桶（如果在 opening window 内）
            target_start = current_or_next_bucket_start_utc(
                datetime.now(timezone.utc), cycle_seconds, max_late_join,
            )
        target_slug = f"{args.slug_prefix}{int(target_start.timestamp())}"

        now_utc = datetime.now(timezone.utc)
        elapsed_in_target = max(0.0, (now_utc - target_start).total_seconds())
        if elapsed_in_target > 0:
            print(
                f"[cycle] 目标完整周期: {target_slug}（已进行{elapsed_in_target:.0f}s，迟到加入）",
                flush=True,
            )
        else:
            print(f"[cycle] 目标完整周期: {target_slug}", flush=True)
            print(f"[cycle] 精确等待 UTC 桶切换 {target_start.isoformat()} ...", flush=True)
        sleep_until_utc_instant(target_start)
        spec = poll_until_success(
            lambda: fetch_market_spec(target_slug),
            timeout_seconds=120.0,
            interval_seconds=0.35,
            log_fn=lambda msg: print(msg, flush=True),
            describe=f"Gamma 市场 {target_slug}",
        )
        print(f"[cycle] 新窗已在 Gamma 就绪: {spec.slug}", flush=True)

        skip_collateral_guard = (
            not args.real_trading
            or args.capital_reset_mode != "fixed"
            or min_collateral <= 0
            or real_broker is None
        )
        if skip_collateral_guard:
            break

        if redeem_settings is not None:
            t_r0 = datetime.now(timezone.utc)
            report = run_auto_redeem_scan(
                redeem_settings, log_fn=lambda m: print(m, flush=True)
            )
            t_r1 = datetime.now(timezone.utc)
            redeem_audit_rows.extend(
                redeem_report_to_audit_rows(
                    report,
                    utc_start=t_r0,
                    utc_end=t_r1,
                    trigger="collateral_guard",
                )
            )
        live_balance = real_broker.get_collateral_balance_usdc()
        pending_ctx = real_broker.pending_context()
        reserved = float(pending_ctx.get("pending_buy_reserved_cash", 0.0))
        available = live_balance - reserved
        if available + 1e-9 >= min_collateral:
            print(
                f"[cycle] 抵押检查通过: balance={live_balance:.4f} 预留≈{reserved:.4f} "
                f"可支配≈{available:.4f} >= {min_collateral:g} USDC",
                flush=True,
            )
            break
        print(
            f"[cycle] 跳过本窗 {spec.slug}：可支配抵押≈{available:.4f} < {min_collateral:g} USDC",
            flush=True,
        )
        next_start = target_start + timedelta(seconds=cycle_seconds)
        sleep_until_utc_instant(next_start)

    assert spec is not None and target_start is not None

    launch_at = target_start + timedelta(seconds=post_delay)
    if datetime.now(timezone.utc) < launch_at:
        print(
            f"[cycle] 等待策略生效时刻 {launch_at.isoformat()}（窗起点+{post_delay:g}s）...",
            flush=True,
        )
        sleep_until_utc_instant(launch_at)

    effective_starting_cash = float(args.starting_cash)
    mode_label = "Paper"
    if args.real_trading:
        assert real_broker is not None
        mode_label = "Real"
        if args.capital_reset_mode == "fixed":
            effective_starting_cash = float(per_cycle)
        else:
            effective_starting_cash = float(real_broker.get_collateral_balance_usdc())
        print(f"[real] 引擎名义本金={effective_starting_cash:.6f} USDC", flush=True)
        live_balance = real_broker.get_collateral_balance_usdc()
        print(f"[real] collateral_balance={live_balance:.6f} USDC", flush=True)
        open_orders = real_broker.get_open_orders(market=spec.condition_id)
        print(f"[real] 当前目标市场未完成订单数={len(open_orders)}", flush=True)
    elif args.capital_reset_mode == "fixed":
        effective_starting_cash = float(per_cycle)
        print(f"[paper] fixed：引擎名义本金={effective_starting_cash:g} USDC", flush=True)

    if args.real_trading:
        broker_for_engine: Any = real_broker
    else:
        broker_for_engine = paper_broker_for_config(
            strategy_config,
            env_values=env_values if env_values else None,
            account_index=args.account_index,
            signature_type=args.signature_type,
        )

    engine = ReplayEngine(
        strategy_config,
        starting_cash=effective_starting_cash,
        capital_reset_mode=args.capital_reset_mode,
        per_cycle_cash=float(per_cycle) if args.capital_reset_mode == "fixed" else None,
        broker=broker_for_engine,
    )
    runner = LivePaperRunner(engine)

    run_leaf = (args.run_subdir.strip() or spec.slug)
    run_dir = Path(args.output_dir) / f"account_{args.account_index}" / run_leaf
    run_dir.mkdir(parents=True, exist_ok=True)

    on_cycle_redeem = None
    redeem_poll_cb = None
    redeem_poll_sec = 0.0
    if redeem_settings is not None:
        def redeem_poll_cb() -> None:
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

    progress_callback = None
    if args.dashboard_refresh_seconds > 0:
        def _flush_progress(result) -> None:
            export_live_result(
                result,
                run_dir,
                title=f"PolyNet AI {spec.slug}",
                refresh_seconds=args.dashboard_refresh_seconds,
                write_excel=False,
            )

        progress_callback = _flush_progress

    recorded_events_path = run_dir / "ws_trade_events.ndjson"
    recorded_events_csv_path = run_dir / "ws_trade_events.csv"

    with TradeEventRecorder(recorded_events_path) as event_recorder:
        result = runner.run_stream(
            iter_polymarket_trade_events(
                [spec],
                cycle_grace_seconds=args.cycle_grace_seconds,
                post_window_start_delay_seconds=post_delay,
                log_fn=print,
            ),
            status_every=args.status_every,
            on_event=event_recorder.record,
            on_progress=progress_callback,
            progress_interval_seconds=max(0.0, args.dashboard_refresh_seconds),
            on_cycle_complete=on_cycle_redeem,
            redeem_poll_interval_seconds=redeem_poll_sec,
            on_redeem_poll=redeem_poll_cb if redeem_settings is not None and redeem_poll_sec > 0 else None,
        )
    export_recorded_trade_events_csv(recorded_events_path, recorded_events_csv_path)
    redeem_audit_df = pd.DataFrame(redeem_audit_rows) if redeem_audit_rows else None
    if redeem_audit_df is not None and redeem_audit_df.empty:
        redeem_audit_df = None
    export_live_result(
        result,
        run_dir,
        title=f"PolyNet AI {spec.slug}",
        refresh_seconds=max(1.0, args.dashboard_refresh_seconds) if args.dashboard_refresh_seconds > 0 else 1.0,
        write_excel=True,
        redeem_audit_df=redeem_audit_df,
    )
    if real_broker is not None:
        real_broker.export_orders(run_dir / "real_order_attempts.json")

    if engine.account.fills:
        pd.DataFrame([asdict(f) for f in engine.account.fills]).to_csv(
            run_dir / "strategy_fills_audit.csv",
            index=False,
            encoding="utf-8-sig",
        )
        print(
            "[review] strategy_fills_audit.csv 已写入（含 fill_source / fill_note，可与 account_trades 对账）",
            flush=True,
        )

    cycle_start_api = _ensure_utc(spec.start_time or target_start)
    cycle_end = _ensure_utc(spec.end_time or (target_start + timedelta(seconds=cycle_seconds)))
    slug_open_naive = window_start_naive_utc_from_slug(spec.slug)
    review_display_start = (
        _ensure_utc(slug_open_naive) if slug_open_naive is not None else cycle_start_api
    )

    user_rows: list[dict[str, Any]] = []
    if purse_address:
        user_rows = _filter_cycle_window(
            _fetch_trades_pages(
                market=spec.condition_id,
                user=purse_address,
                taker_only=False,
            ),
            cycle_start=cycle_start_api,
            cycle_end=cycle_end + timedelta(seconds=args.cycle_grace_seconds),
        )
    market_rows = _filter_cycle_window(
        _fetch_trades_pages(
            market=spec.condition_id,
            taker_only=False,
        ),
        cycle_start=cycle_start_api,
        cycle_end=cycle_end + timedelta(seconds=args.cycle_grace_seconds),
    )

    user_raw_csv = run_dir / "account_trades_raw.csv"
    market_raw_csv = run_dir / "market_trades_raw.csv"
    pd.DataFrame(user_rows).to_csv(user_raw_csv, index=False, encoding="utf-8-sig")
    pd.DataFrame(market_rows).to_csv(market_raw_csv, index=False, encoding="utf-8-sig")

    user_trade_df = _rows_to_trade_excel(user_rows, cycle_slug=spec.slug, cycle_start=review_display_start)
    _vtag = get_version_tag()
    user_trade_xlsx = run_dir / f"account_trades_{_vtag}.xlsx"
    with pd.ExcelWriter(user_trade_xlsx, engine="openpyxl") as writer:
        user_trade_df.to_excel(writer, sheet_name="BTC", index=False)

    market_trade_df = pd.DataFrame(market_rows)
    market_trade_xlsx = run_dir / f"market_trades_raw_{_vtag}.xlsx"
    with pd.ExcelWriter(market_trade_xlsx, engine="openpyxl") as writer:
        market_trade_df.to_excel(writer, sheet_name="BTC", index=False)

    analyzed_xlsx = run_dir / f"account_trades_analyzed_{_vtag}.xlsx"
    if user_trade_df.empty:
        print("[review] 该周期未抓到账号成交，跳过 analyze_polymarket_tracker.py。", flush=True)
    else:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "analyze_polymarket_tracker.py"),
                "--input",
                str(user_trade_xlsx),
                "--output",
                str(analyzed_xlsx),
                "--sheet",
                "BTC",
            ],
            check=True,
        )

    _write_review_summary(
        run_dir / "review_summary.md",
        account_index=args.account_index,
        purse_address=purse_address,
        cycle_slug=spec.slug,
        cycle_start=review_display_start,
        cycle_end=cycle_end,
        user_trade_df=user_trade_df,
        market_trade_df=market_trade_df,
        paper_decision_df=result.replay_result.decision_df,
        mode_label=mode_label,
    )

    suffix = f" ({cycle_ix + 1}/{max_cycles})" if max_cycles > 1 else ""
    print(f"[done] 单周期复盘完成{suffix}: {run_dir}", flush=True)


def main() -> int:
    args = parse_args()

    env_path = Path(args.env_file)
    if args.real_trading and not env_path.exists():
        tried = "\n  ".join(str(p.resolve()) for p in default_api_config_env_candidates(ROOT))
        raise FileNotFoundError(
            "实盘需要 ApiConfig：以下路径均不存在或未找到配置文件：\n"
            f"  {env_path.resolve()}\n"
            "未传 --env-file 时默认按顺序尝试：\n"
            f"  {tried}\n"
            "请将 ApiConfig.env 放到上述任一路径，或设置环境变量 RECORD_ENV_FILE / 使用 --env-file 指向实际文件。"
        )
    env_values = load_api_env(args.env_file) if env_path.exists() else {}
    selected_env = select_account_env(env_values, account_index=args.account_index)
    applied = apply_proxy_env_from_dict(selected_env) if selected_env else []
    # 与代理一致：凭证在 select_account_env 后读取（裸键已由 *_<N> 提升，且与 get_account_env_value 后缀规则一致）
    cred_env = selected_env
    purse_address = get_account_env_value(cred_env, "PURSE_ADDRESS", account_index=args.account_index)
    print(f"[env] 默认账号={args.account_index}", flush=True)
    if purse_address:
        print(f"[env] PURSE_ADDRESS={purse_address}", flush=True)
    if applied:
        print(f"[env] 已应用代理: {', '.join(applied)}", flush=True)

    strategy_config = _build_strategy_config(args.config, args.overrides)
    post_delay = resolve_post_window_start_delay_seconds(
        config=strategy_config,
        cli_seconds=args.start_buffer_seconds,
    )
    print(
        f"[cycle] 窗起点后策略推迟: {post_delay:g}s（"
        f"{'命令行 --start-buffer-seconds' if args.start_buffer_seconds is not None else 'strategy.yaml cycle.post_window_start_delay_seconds'}）",
        flush=True,
    )

    cycle_seconds = cycle_seconds_from_slug_prefix(args.slug_prefix)
    if cycle_seconds is None:
        raise ValueError(f"无法从 slug 前缀解析周期长度: {args.slug_prefix}")

    per_cycle = args.per_cycle_cash if args.per_cycle_cash is not None else args.starting_cash
    if args.capital_reset_mode == "fixed":
        print(
            f"[cycle] fixed 模式：每周期名义本金 per_cycle_cash={per_cycle:g} USDC（与 paper 批量口径对齐）",
            flush=True,
        )

    min_collateral = 0.0
    if args.real_trading and args.capital_reset_mode == "fixed":
        if args.min_collateral_usdc is None:
            min_collateral = float(per_cycle)
        else:
            min_collateral = float(args.min_collateral_usdc)
        if min_collateral > 0:
            print(
                f"[cycle] 真实+f fixed：抵押低于 {min_collateral:g} USDC 时将跳过该窗"
                "（默认会在检查前尝试自动赎回，可用 --no-auto-redeem 关闭）",
                flush=True,
            )

    redeem_settings = None
    if args.auto_redeem:
        redeem_settings = load_auto_redeem_settings(cred_env, account_index=args.account_index)
        if redeem_settings is None:
            print(
                "[redeem] 默认已尝试启用自动赎回，但缺少带账号后缀的 PURSE_PRIVATE_KEY_<N>、PURSE_ADDRESS_<N> 或 "
                "POLY_BUILDER_API_*_<N>（默认 N=2，与 --account-index 一致；无后缀裸键仅作兼容）；"
                " 将跳过赎回。若不需要赎回请使用 --no-auto-redeem。",
                flush=True,
            )
        else:
            print(
                f"[redeem] 已启用自动赎回；轮询间隔={max(0.0, float(args.redeem_poll_interval_seconds)):g}s",
                flush=True,
            )

    real_broker: PolymarketBroker | None = None
    if args.real_trading:
        real_broker = PolymarketBroker.from_env(
            cred_env,
            account_index=args.account_index,
            fee_rate=float(strategy_config.get("execution.fee_rate", 0.002)),
            signature_type=args.signature_type,
        )

    if args.max_cycles < 1:
        raise ValueError("--max-cycles 须 >= 1")

    for cycle_ix in range(args.max_cycles):
        if args.max_cycles > 1:
            print(
                f"[cycle] 多窗进度 {cycle_ix + 1}/{args.max_cycles}（单进程连续执行）",
                flush=True,
            )
        _execute_one_polymarket_cycle(
            args,
            strategy_config=strategy_config,
            post_delay=post_delay,
            cycle_seconds=cycle_seconds,
            per_cycle=per_cycle,
            min_collateral=min_collateral,
            redeem_settings=redeem_settings,
            real_broker=real_broker,
            purse_address=purse_address,
            cycle_ix=cycle_ix,
            max_cycles=args.max_cycles,
            env_values=cred_env,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
