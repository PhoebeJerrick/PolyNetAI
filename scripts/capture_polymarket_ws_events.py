from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.polymarket_live import (  # noqa: E402
    PolymarketMarketSpec,
    apply_proxy_env_from_dict,
    build_market_specs,
    discover_active_markets,
    get_account_env_value,
    iter_polymarket_trade_events,
    load_api_env,
    select_account_env,
)
from polynet_ai.adapters.trade_event_store import (  # noqa: E402
    TradeEventRecorder,
    export_recorded_trade_events_csv,
)
from polynet_ai.domain.models import TradeEvent  # noqa: E402


@dataclass(slots=True)
class CaptureStats:
    cycle_id: str
    market_id: str
    event_count: int = 0
    first_timestamp: str = ""
    last_timestamp: str = ""
    first_price: float = 0.0
    last_price: float = 0.0
    buy_events: int = 0
    sell_events: int = 0
    up_events: int = 0
    down_events: int = 0

    def apply(self, event: TradeEvent) -> None:
        timestamp = event.timestamp.isoformat()
        if not self.first_timestamp:
            self.first_timestamp = timestamp
            self.first_price = float(event.price)
        self.last_timestamp = timestamp
        self.last_price = float(event.price)
        self.event_count += 1
        if event.action == "buy":
            self.buy_events += 1
        else:
            self.sell_events += 1
        if event.outcome == "up":
            self.up_events += 1
        else:
            self.down_events += 1

    def to_row(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "market_id": self.market_id,
            "event_count": self.event_count,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "first_price": self.first_price,
            "last_price": self.last_price,
            "buy_events": self.buy_events,
            "sell_events": self.sell_events,
            "up_events": self.up_events,
            "down_events": self.down_events,
        }


class CycleCaptureWriter:
    def __init__(self, output_dir: str | Path, *, combined_filename: str = "ws_trade_events_all.ndjson") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.combined_path = self.output_dir / combined_filename
        self._combined_recorder = TradeEventRecorder(self.combined_path)
        self._cycle_recorders: dict[str, TradeEventRecorder] = {}
        self._cycle_paths: dict[str, Path] = {}
        self._stats: dict[str, CaptureStats] = {}
        self._finalized_rows: list[dict[str, object]] | None = None

    def _cycle_dir(self, cycle_id: str) -> Path:
        return self.output_dir / cycle_id

    def _cycle_path(self, cycle_id: str) -> Path:
        return self._cycle_dir(cycle_id) / "ws_trade_events.ndjson"

    def _get_cycle_recorder(self, event: TradeEvent) -> TradeEventRecorder:
        recorder = self._cycle_recorders.get(event.cycle_id)
        if recorder is not None:
            return recorder
        cycle_path = self._cycle_path(event.cycle_id)
        recorder = TradeEventRecorder(cycle_path)
        self._cycle_recorders[event.cycle_id] = recorder
        self._cycle_paths[event.cycle_id] = cycle_path
        return recorder

    def record(self, event: TradeEvent) -> None:
        self._combined_recorder.record(event)
        cycle_recorder = self._get_cycle_recorder(event)
        cycle_recorder.record(event)
        stats = self._stats.get(event.cycle_id)
        if stats is None:
            stats = CaptureStats(cycle_id=event.cycle_id, market_id=event.market_id)
            self._stats[event.cycle_id] = stats
        stats.apply(event)

    def finalize(self) -> list[dict[str, object]]:
        if self._finalized_rows is not None:
            return self._finalized_rows
        self._combined_recorder.close()
        export_recorded_trade_events_csv(self.combined_path, self.combined_path.with_suffix(".csv"))

        summary_rows: list[dict[str, object]] = []
        for cycle_id, recorder in self._cycle_recorders.items():
            recorder.close()
            cycle_path = self._cycle_paths[cycle_id]
            export_recorded_trade_events_csv(cycle_path, cycle_path.with_suffix(".csv"))
            stats = self._stats[cycle_id]
            row = stats.to_row()
            summary_rows.append(row)
            summary_path = self._cycle_dir(cycle_id) / "capture_summary.json"
            summary_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")

        summary_rows.sort(key=lambda item: str(item["cycle_id"]))
        manifest_json = self.output_dir / "capture_manifest.json"
        manifest_csv = self.output_dir / "capture_manifest.csv"
        manifest_json.write_text(json.dumps(summary_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        pd.DataFrame(summary_rows).to_csv(manifest_csv, index=False, encoding="utf-8-sig")
        self._finalized_rows = summary_rows
        return summary_rows

    def close(self) -> list[dict[str, object]]:
        return self.finalize()

    def __enter__(self) -> "CycleCaptureWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finalize()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="独立抓取 Polymarket BTC 5m websocket 事件流")
    parser.add_argument("--output-dir", default="artifacts/live/ws_capture_btc_5m")
    parser.add_argument("--daemonize", action="store_true", help="以后台方式启动抓取任务，适合 Linux 云服务器长期运行")
    parser.add_argument("--log-file", default=None, help="后台运行时日志文件路径，默认 <output-dir>/capture.log")
    parser.add_argument("--pid-file", default=None, help="后台运行时 PID 文件路径，默认 <output-dir>/capture.pid")
    parser.add_argument("--slug-prefix", default="btc-updown-5m-", help="默认抓 BTC 5 分钟窗口")
    parser.add_argument("--max-cycles", type=int, default=1, help="要抓取的 5 分钟周期数")
    parser.add_argument("--status-every", type=int, default=100, help="每处理多少条事件输出一次进度")
    parser.add_argument("--poll-interval-seconds", type=float, default=3.0, help="自动发现新窗口的轮询间隔")
    parser.add_argument("--ping-interval-seconds", type=float, default=10.0)
    parser.add_argument("--receive-timeout-seconds", type=float, default=1.0)
    parser.add_argument(
        "--cycle-grace-seconds",
        type=float,
        default=0.0,
        help="抓连续未来周期时默认建议为 0，避免错过紧接着开始的下一个 5m 窗口。",
    )
    parser.add_argument(
        "--start-buffer-seconds",
        type=float,
        default=2.0,
        help="在新周期开始前提前建立连接的秒数；真正写盘仍从周期开始时刻算起。",
    )
    parser.add_argument("--market-slugs", nargs="*", default=None, help="显式指定要抓取的 market slug 列表")
    parser.add_argument("--market-slugs-file", default=None, help="每行一个 market slug")
    parser.add_argument(
        "--env-file",
        default=str(ROOT.parent / "APIs" / "ApiConfig.env"),
        help="用于加载代理等环境变量；仅抓公开 websocket，不会真实下单。",
    )
    parser.add_argument("--account-index", type=int, default=2, help="账号编号；主要用于读取带后缀的代理/配置键")
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


def _apply_env_file(path: str | Path, account_index: int) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    env_values = load_api_env(env_path)
    selected_env = select_account_env(env_values, account_index=account_index)
    applied = apply_proxy_env_from_dict(selected_env)
    purse = get_account_env_value(env_values, "PURSE_ADDRESS", account_index=account_index)
    print(f"检测到 API 配置文件: {env_path}")
    print(f"默认账号: {account_index}" + (f" (PURSE_ADDRESS={purse})" if purse else ""))
    if applied:
        print(f"已从配置文件应用代理环境变量: {', '.join(applied)}")


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _build_daemon_child_argv(script_path: Path, argv: list[str]) -> list[str]:
    child_args: list[str] = []
    skip_next = False
    value_flags = {"--log-file", "--pid-file"}
    for index, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token == "--daemonize":
            continue
        if any(token.startswith(flag + "=") for flag in value_flags):
            continue
        if token in value_flags:
            if index + 1 < len(argv):
                skip_next = True
            continue
        child_args.append(token)
    return [sys.executable, "-u", str(script_path)] + child_args


def _launch_in_background(args: argparse.Namespace, raw_argv: list[str]) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log_file) if args.log_file else output_dir / "capture.log"
    pid_path = Path(args.pid_file) if args.pid_file else output_dir / "capture.pid"
    meta_path = output_dir / "capture_background_meta.json"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError:
            existing_pid = 0
        if _is_process_running(existing_pid):
            raise RuntimeError(
                f"检测到已有后台抓取任务仍在运行，pid={existing_pid}。"
                f"如需停止，请先在 Linux 上执行 `kill {existing_pid}`。"
            )
        pid_path.unlink(missing_ok=True)

    command = _build_daemon_child_argv(Path(__file__).resolve(), raw_argv)
    quoted_command = " ".join(shlex.quote(part) for part in command)
    with log_path.open("a", encoding="utf-8") as log_fh:
        log_fh.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] launch: {quoted_command}\n")
        log_fh.flush()
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    pid_path.write_text(str(process.pid), encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "log_file": str(log_path),
                "pid_file": str(pid_path),
                "output_dir": str(output_dir),
                "slug_prefix": args.slug_prefix,
                "max_cycles": args.max_cycles,
                "start_buffer_seconds": args.start_buffer_seconds,
                "status_every": args.status_every,
                "poll_interval_seconds": args.poll_interval_seconds,
                "env_file": args.env_file,
                "account_index": args.account_index,
                "command": command,
                "launched_at": datetime.now().isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("后台抓取任务已启动。")
    print(f"PID: {process.pid}")
    print(f"日志: {log_path}")
    print(f"PID 文件: {pid_path}")
    print(f"查看日志: tail -f {log_path}")
    print(f"停止任务: kill {process.pid}")
    return 0


def _utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sort_specs_by_start(specs: list[PolymarketMarketSpec]) -> list[PolymarketMarketSpec]:
    return sorted(specs, key=lambda item: (item.start_time or datetime.max, item.slug))


def _extend_reserved_specs(
    reserved_specs: list[PolymarketMarketSpec],
    discovered_specs: list[PolymarketMarketSpec],
    *,
    seen_slugs: set[str],
    now: datetime,
) -> list[PolymarketMarketSpec]:
    queue = list(reserved_specs)
    for spec in _sort_specs_by_start(discovered_specs):
        if spec.slug in seen_slugs:
            continue
        if spec.start_time is None:
            continue
        if spec.end_time is not None and spec.end_time <= now:
            continue
        if spec.start_time <= now:
            continue
        seen_slugs.add(spec.slug)
        queue.append(spec)
    return _sort_specs_by_start(queue)


def _resolve_future_specs(
    specs: list[PolymarketMarketSpec],
    *,
    require_future_start: bool,
) -> list[PolymarketMarketSpec]:
    if not require_future_start:
        return _sort_specs_by_start(specs)
    now = _utc_now_naive()
    future_specs = [spec for spec in specs if spec.start_time is not None and spec.start_time > now]
    return _sort_specs_by_start(future_specs)


def _wait_until_subscription_time(spec: PolymarketMarketSpec, *, start_buffer_seconds: float, log_fn=print) -> None:
    if spec.start_time is None:
        return
    subscribe_at = spec.start_time - timedelta(seconds=max(0.0, start_buffer_seconds))
    while True:
        now = _utc_now_naive()
        remaining = (subscribe_at - now).total_seconds()
        if remaining <= 0:
            break
        if log_fn is not None:
            log_fn(f"[capture] 等待新周期开始: {spec.slug}，约 {remaining:.1f}s 后建立订阅")
        time.sleep(min(max(remaining, 0.2), 10.0))


def _iter_events_for_spec_from_cycle_start(
    spec: PolymarketMarketSpec,
    *,
    ping_interval_seconds: float,
    receive_timeout_seconds: float,
    cycle_grace_seconds: float,
    start_buffer_seconds: float,
    log_fn=print,
):
    _wait_until_subscription_time(spec, start_buffer_seconds=start_buffer_seconds, log_fn=log_fn)
    cycle_start = spec.start_time
    if log_fn is not None:
        start_text = cycle_start.isoformat() if cycle_start is not None else "unknown"
        log_fn(f"[capture] 开始抓取新周期 {spec.slug} (cycle_start={start_text})")
    for event in iter_polymarket_trade_events(
        [spec],
        ping_interval_seconds=ping_interval_seconds,
        receive_timeout_seconds=receive_timeout_seconds,
        cycle_grace_seconds=cycle_grace_seconds,
        log_fn=log_fn,
    ):
        if cycle_start is not None and event.timestamp < cycle_start:
            continue
        yield event


def _iter_future_cycle_events(
    *,
    slug_prefix: str,
    max_cycles: int,
    poll_interval_seconds: float,
    ping_interval_seconds: float,
    receive_timeout_seconds: float,
    cycle_grace_seconds: float,
    start_buffer_seconds: float,
    discover_limit: int = 16,
    log_fn=print,
):
    seen_slugs: set[str] = set()
    handled_cycles = 0
    no_new_rounds = 0
    reserved_specs: list[PolymarketMarketSpec] = []

    while max_cycles <= 0 or handled_cycles < max_cycles:
        now = _utc_now_naive()
        remaining = max_cycles - handled_cycles if max_cycles > 0 else discover_limit
        fetch_limit = max(discover_limit, remaining + 2 if max_cycles > 0 else discover_limit)
        discovered_specs = discover_active_markets(slug_prefix=slug_prefix, limit=fetch_limit)
        reserved_specs = _extend_reserved_specs(
            reserved_specs,
            discovered_specs,
            seen_slugs=seen_slugs,
            now=now,
        )

        if not reserved_specs:
            no_new_rounds += 1
            if log_fn is not None and no_new_rounds % 10 == 1:
                log_fn(
                    f"[capture] 当前没有尚未开始的新窗口（prefix={slug_prefix}），"
                    f"等待 {poll_interval_seconds:.1f}s 后继续查找..."
                )
            time.sleep(max(0.2, poll_interval_seconds))
            continue

        no_new_rounds = 0
        next_spec = reserved_specs.pop(0)
        handled_cycles += 1
        if log_fn is not None:
            log_fn(
                f"[capture] 已锁定未来窗口 {next_spec.slug} "
                f"({handled_cycles}/{max_cycles if max_cycles > 0 else 'inf'})"
            )
        yield from _iter_events_for_spec_from_cycle_start(
            next_spec,
            ping_interval_seconds=ping_interval_seconds,
            receive_timeout_seconds=receive_timeout_seconds,
            cycle_grace_seconds=cycle_grace_seconds,
            start_buffer_seconds=start_buffer_seconds,
            log_fn=log_fn,
        )


def _build_event_stream(args: argparse.Namespace):
    market_slugs = _load_market_slugs(args)
    if market_slugs:
        raw_specs = build_market_specs(
            market_slugs=market_slugs,
            slug_prefix=args.slug_prefix,
            max_cycles=args.max_cycles,
        )
        specs = _resolve_future_specs(raw_specs, require_future_start=True)
        if not specs:
            raise RuntimeError("显式指定的 market slug 都已经开始或缺少 start_time，无法保证从新周期开始抓取。")
        print("本次将从周期开始抓取以下 future market slug:")
        for spec in specs:
            print(f"  - {spec.slug}")
        def _stream():
            for spec in specs:
                yield from _iter_events_for_spec_from_cycle_start(
                    spec,
                    ping_interval_seconds=args.ping_interval_seconds,
                    receive_timeout_seconds=args.receive_timeout_seconds,
                    cycle_grace_seconds=args.cycle_grace_seconds,
                    start_buffer_seconds=args.start_buffer_seconds,
                    log_fn=print,
                )
        return _stream()
    print(
        f"将按前缀 `{args.slug_prefix}` 自动发现未来的 {args.max_cycles} 个 5 分钟周期；"
        "会跳过已开始的当前窗口，并从新周期开始时刻起正式写盘。"
    )
    return _iter_future_cycle_events(
        slug_prefix=args.slug_prefix,
        max_cycles=args.max_cycles,
        poll_interval_seconds=args.poll_interval_seconds,
        ping_interval_seconds=args.ping_interval_seconds,
        receive_timeout_seconds=args.receive_timeout_seconds,
        cycle_grace_seconds=args.cycle_grace_seconds,
        start_buffer_seconds=args.start_buffer_seconds,
        log_fn=print,
    )


def main() -> int:
    args = parse_args()
    if args.daemonize:
        return _launch_in_background(args, sys.argv[1:])
    _apply_env_file(args.env_file, args.account_index)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    total_events = 0
    started_at = datetime.now().isoformat(timespec="seconds")
    manifest_rows: list[dict[str, object]] = []

    try:
        with CycleCaptureWriter(output_dir) as writer:
            for event in _build_event_stream(args):
                writer.record(event)
                total_events += 1
                if args.status_every > 0 and total_events % args.status_every == 0:
                    print(
                        f"[capture] 已写入 {total_events} 条事件，"
                        f"当前周期={event.cycle_id} 价格={event.price:.4f} outcome={event.outcome} action={event.action}"
                    )
            manifest_rows = writer.finalize()
    except KeyboardInterrupt:
        print("收到中断信号，正在保存已抓到的事件流...")
        manifest_path = output_dir / "capture_manifest.json"
        if manifest_path.exists():
            manifest_rows = json.loads(manifest_path.read_text(encoding="utf-8"))

    print(f"事件流抓取完成，开始时间: {started_at}")
    print(f"总事件数: {total_events}")
    print(f"输出目录: {output_dir}")
    if manifest_rows:
        print("已完成周期:")
        for row in manifest_rows:
            print(
                "  - "
                f"{row['cycle_id']} | events={row['event_count']} | "
                f"{row['first_timestamp']} -> {row['last_timestamp']}"
            )
        first_cycle = manifest_rows[0]["cycle_id"]
        first_cycle_path = output_dir / str(first_cycle) / "ws_trade_events.ndjson"
        print("离线回放示例:")
        print(
            "  python scripts/replay_recorded_trade_events.py "
            f"--input \"{first_cycle_path}\" "
            "--config configs/strategy.yaml "
            "--starting-cash 100 "
            f"--output \"artifacts/replays/{first_cycle}_replay.xlsx\""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
