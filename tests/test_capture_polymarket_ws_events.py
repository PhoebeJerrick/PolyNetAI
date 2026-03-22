from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from polynet_ai.adapters.trade_event_store import load_recorded_trade_events
from scripts.capture_polymarket_ws_events import (
    CycleCaptureWriter,
    _build_daemon_child_argv,
    _resolve_future_specs,
)
from polynet_ai.adapters.polymarket_live import PolymarketMarketSpec
from polynet_ai.domain.models import TradeEvent


def test_cycle_capture_writer_splits_cycles_and_exports_manifest(tmp_path) -> None:
    writer = CycleCaptureWriter(tmp_path)
    writer.record(
        TradeEvent(
            market_id="btc-up-or-down-5m",
            cycle_id="btc-updown-5m-1",
            timestamp=datetime(2026, 3, 22, 0, 0, 1, tzinfo=timezone.utc),
            price=0.51,
            shares=7.0,
            outcome="up",
            action="buy",
            source="polymarket_ws",
        )
    )
    writer.record(
        TradeEvent(
            market_id="btc-up-or-down-5m",
            cycle_id="btc-updown-5m-2",
            timestamp=datetime(2026, 3, 22, 0, 5, 1, tzinfo=timezone.utc),
            price=0.49,
            shares=9.0,
            outcome="down",
            action="sell",
            source="polymarket_ws",
        )
    )

    rows = writer.finalize()

    assert len(rows) == 2
    assert (tmp_path / "ws_trade_events_all.ndjson").exists()
    assert (tmp_path / "ws_trade_events_all.csv").exists()
    assert (tmp_path / "capture_manifest.json").exists()
    assert (tmp_path / "capture_manifest.csv").exists()

    first_cycle_path = tmp_path / "btc-updown-5m-1" / "ws_trade_events.ndjson"
    second_cycle_path = tmp_path / "btc-updown-5m-2" / "ws_trade_events.ndjson"
    assert first_cycle_path.exists()
    assert second_cycle_path.exists()
    assert (first_cycle_path.with_suffix(".csv")).exists()
    assert (second_cycle_path.with_suffix(".csv")).exists()

    first_events = load_recorded_trade_events(first_cycle_path)
    second_events = load_recorded_trade_events(second_cycle_path)
    assert len(first_events) == 1
    assert len(second_events) == 1
    assert first_events[0].cycle_id == "btc-updown-5m-1"
    assert second_events[0].cycle_id == "btc-updown-5m-2"


def test_resolve_future_specs_filters_started_cycles() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    started = PolymarketMarketSpec(
        slug="btc-updown-5m-started",
        series_slug="btc-up-or-down-5m",
        condition_id="started",
        yes_token_id="yes-a",
        no_token_id="no-a",
        start_time=now - timedelta(seconds=30),
        end_time=now + timedelta(minutes=4),
        raw={},
    )
    future = PolymarketMarketSpec(
        slug="btc-updown-5m-future",
        series_slug="btc-up-or-down-5m",
        condition_id="future",
        yes_token_id="yes-b",
        no_token_id="no-b",
        start_time=now + timedelta(minutes=5),
        end_time=now + timedelta(minutes=10),
        raw={},
    )

    resolved = _resolve_future_specs([started, future], require_future_start=True)

    assert [spec.slug for spec in resolved] == ["btc-updown-5m-future"]


def test_build_daemon_child_argv_strips_background_only_flags() -> None:
    argv = [
        "--daemonize",
        "--output-dir",
        "artifacts/live/ws_capture_btc_10cycles",
        "--log-file",
        "logs/capture.log",
        "--pid-file=logs/capture.pid",
        "--max-cycles",
        "10",
    ]

    command = _build_daemon_child_argv(Path("scripts/capture_polymarket_ws_events.py"), argv)

    assert "--daemonize" not in command
    assert "--log-file" not in command
    assert not any(str(item).startswith("--pid-file") for item in command)
    assert "--output-dir" in command
    assert "--max-cycles" in command
