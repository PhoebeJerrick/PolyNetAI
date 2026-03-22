from __future__ import annotations

from datetime import datetime, timezone

from polynet_ai.adapters.trade_event_store import load_recorded_trade_events
from scripts.capture_polymarket_ws_events import CycleCaptureWriter
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
