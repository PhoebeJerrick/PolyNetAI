from __future__ import annotations

import argparse
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
    cycle_seconds_from_slug_prefix,
    next_bucket_start_utc,
    poll_until_success,
    sleep_until_utc_instant,
    window_start_naive_utc_from_slug,
)
from polynet_ai.adapters.polymarket_live import (  # noqa: E402
    apply_proxy_env_from_dict,
    fetch_market_spec,
    get_account_env_value,
    iter_polymarket_trade_events,
    load_api_env,
    select_account_env,
)
from polynet_ai.adapters.trade_event_store import (  # noqa: E402
    TradeEventRecorder,
    export_recorded_trade_events_csv,
)
from polynet_ai.engine.live import LivePaperRunner, export_live_result  # noqa: E402
from polynet_ai.engine.replay import ReplayEngine  # noqa: E402
from polynet_ai.execution.polymarket_broker import PolymarketBroker  # noqa: E402
from polynet_ai.reporting.excel_export import get_version_tag  # noqa: E402
from polynet_ai.strategy.spec import (  # noqa: E402
    load_strategy_config,
    resolve_post_window_start_delay_seconds,
)

DATA_API_BASE = "https://data-api.polymarket.com"
DEFAULT_OVERRIDES = (
    ROOT
    / "artifacts"
    / "optimization"
    / "optimize_btc_last_6h_100u_smallcap_v2_20260321T014000Z"
    / "trial_022"
    / "overrides.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行一个完整 5m 周期并自动抓取用户成交/市场原始数据做复盘比对。")
    parser.add_argument("--config", default="configs/strategy.yaml")
    parser.add_argument("--overrides", default=str(DEFAULT_OVERRIDES))
    parser.add_argument("--output-dir", default="artifacts/live/polymarket_cycle_review")
    parser.add_argument("--slug-prefix", default="btc-updown-5m-")
    parser.add_argument("--starting-cash", type=float, default=200.0)
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
    parser.add_argument("--account-index", type=int, default=2)
    parser.add_argument(
        "--env-file",
        default=str(ROOT.parent / "APIs" / "ApiConfig.env"),
        help="API 配置文件路径；本脚本只会用账号地址抓成交与应用代理，不会真实下单。",
    )
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


def _build_engine(config_path: str | Path, overrides_path: str | Path, starting_cash: float) -> ReplayEngine:
    config = load_strategy_config(config_path)
    overrides = _load_overrides(overrides_path)
    return ReplayEngine(config.with_overrides(overrides), starting_cash=starting_cash)


def _build_strategy_config(config_path: str | Path, overrides_path: str | Path):
    config = load_strategy_config(config_path)
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


def main() -> int:
    args = parse_args()

    env_values = load_api_env(args.env_file) if Path(args.env_file).exists() else {}
    selected_env = select_account_env(env_values, account_index=args.account_index)
    applied = apply_proxy_env_from_dict(selected_env) if selected_env else []
    purse_address = get_account_env_value(env_values, "PURSE_ADDRESS", account_index=args.account_index)
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
    target_start = next_bucket_start_utc(datetime.now(timezone.utc), cycle_seconds)
    target_slug = f"{args.slug_prefix}{int(target_start.timestamp())}"

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
    launch_at = target_start + timedelta(seconds=post_delay)
    if datetime.now(timezone.utc) < launch_at:
        print(
            f"[cycle] 等待策略生效时刻 {launch_at.isoformat()}（窗起点+{post_delay:g}s）...",
            flush=True,
        )
        sleep_until_utc_instant(launch_at)
    real_broker = None
    effective_starting_cash = args.starting_cash
    mode_label = "Paper"
    if args.real_trading:
        real_broker = PolymarketBroker.from_env(
            env_values,
            account_index=args.account_index,
            fee_rate=float(strategy_config.get("execution.fee_rate", 0.002)),
            signature_type=args.signature_type,
        )
        live_balance = real_broker.get_collateral_balance_usdc()
        effective_starting_cash = live_balance
        mode_label = "Real"
        print(f"[real] collateral_balance={live_balance:.6f} USDC", flush=True)
        open_orders = real_broker.get_open_orders(market=spec.condition_id)
        print(f"[real] 当前目标市场未完成订单数={len(open_orders)}", flush=True)

    engine = ReplayEngine(
        strategy_config,
        starting_cash=effective_starting_cash,
        broker=real_broker,
    )
    runner = LivePaperRunner(engine)

    run_dir = Path(args.output_dir) / f"account_{args.account_index}" / spec.slug
    run_dir.mkdir(parents=True, exist_ok=True)

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
        )
    export_recorded_trade_events_csv(recorded_events_path, recorded_events_csv_path)
    export_live_result(
        result,
        run_dir,
        title=f"PolyNet AI {spec.slug}",
        refresh_seconds=max(1.0, args.dashboard_refresh_seconds) if args.dashboard_refresh_seconds > 0 else 1.0,
        write_excel=True,
    )
    if real_broker is not None:
        real_broker.export_orders(run_dir / "real_order_attempts.json")

    # API 拉取区间仍以 Gamma 的 start/end 为准；展示「距开盘差」等与批量报告一致时用 slug epoch。
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

    print(f"[done] 单周期复盘完成: {run_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
