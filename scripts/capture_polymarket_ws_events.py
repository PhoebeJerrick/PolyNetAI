from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.adapters.polymarket_live import (  # noqa: E402
    apply_proxy_env_from_dict,
    build_market_specs,
    get_account_env_value,
    iter_polymarket_trade_events,
    iter_polymarket_trade_events_robot,
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
    parser.add_argument("--slug-prefix", default="btc-updown-5m-", help="默认抓 BTC 5 分钟窗口")
    parser.add_argument("--max-cycles", type=int, default=1, help="要抓取的 5 分钟周期数")
    parser.add_argument("--status-every", type=int, default=100, help="每处理多少条事件输出一次进度")
    parser.add_argument("--poll-interval-seconds", type=float, default=3.0, help="自动发现新窗口的轮询间隔")
    parser.add_argument("--ping-interval-seconds", type=float, default=10.0)
    parser.add_argument("--receive-timeout-seconds", type=float, default=1.0)
    parser.add_argument("--cycle-grace-seconds", type=float, default=20.0)
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


def _build_event_stream(args: argparse.Namespace):
    market_slugs = _load_market_slugs(args)
    if market_slugs:
        specs = build_market_specs(
            market_slugs=market_slugs,
            slug_prefix=args.slug_prefix,
            max_cycles=args.max_cycles,
        )
        if not specs:
            raise RuntimeError("没有可用的市场可供订阅。")
        print("本次将显式抓取以下周期:")
        for spec in specs:
            print(f"  - {spec.slug}")
        return iter_polymarket_trade_events(
            specs,
            ping_interval_seconds=args.ping_interval_seconds,
            receive_timeout_seconds=args.receive_timeout_seconds,
            cycle_grace_seconds=args.cycle_grace_seconds,
            log_fn=print,
        )
    print(
        f"将按前缀 `{args.slug_prefix}` 自动发现并抓取 {args.max_cycles} 个 5 分钟周期；"
        "每个周期都会单独落盘，且会额外生成一个合并事件流文件。"
    )
    return iter_polymarket_trade_events_robot(
        slug_prefix=args.slug_prefix,
        max_cycles=args.max_cycles,
        poll_interval_seconds=args.poll_interval_seconds,
        ping_interval_seconds=args.ping_interval_seconds,
        receive_timeout_seconds=args.receive_timeout_seconds,
        cycle_grace_seconds=args.cycle_grace_seconds,
        log_fn=print,
    )


def main() -> int:
    args = parse_args()
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
