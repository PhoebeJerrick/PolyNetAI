from __future__ import annotations

from datetime import datetime, timezone

from polynet_ai.adapters.trade_event_store import (
    CycleTradeEventRecorder,
    TradeEventRecorder,
    export_recorded_trade_events_csv,
    load_recorded_trade_events,
)
from polynet_ai.domain.models import TradeEvent


def test_trade_event_store_roundtrip_preserves_fields(tmp_path) -> None:
    path = tmp_path / "events.ndjson"
    source_event = TradeEvent(
        market_id="btc-up-or-down-5m",
        cycle_id="btc-updown-5m-1774138800",
        timestamp=datetime(2026, 3, 22, 0, 20, 1, 811000, tzinfo=timezone.utc),
        price=0.51,
        shares=7.1,
        outcome="up",
        action="buy",
        source="polymarket_ws",
        metadata={"asset_id": "yes-token", "row_ordinal": 1},
    )

    with TradeEventRecorder(path) as recorder:
        recorder.record(source_event)

    loaded = load_recorded_trade_events(path)

    assert len(loaded) == 1
    restored = loaded[0]
    assert restored.market_id == source_event.market_id
    assert restored.cycle_id == source_event.cycle_id
    assert restored.timestamp == source_event.timestamp
    assert restored.price == source_event.price
    assert restored.shares == source_event.shares
    assert restored.outcome == source_event.outcome
    assert restored.action == source_event.action
    assert restored.source == source_event.source
    assert restored.metadata == source_event.metadata


def test_export_recorded_trade_events_csv_writes_flat_file(tmp_path) -> None:
    ndjson_path = tmp_path / "events.ndjson"
    csv_path = tmp_path / "events.csv"
    with TradeEventRecorder(ndjson_path) as recorder:
        recorder.record(
            TradeEvent(
                market_id="BTC",
                cycle_id="cycle-a",
                timestamp=datetime(2026, 3, 20, 12, 0, 0),
                price=0.45,
                shares=10.0,
                outcome="up",
                action="buy",
                metadata={"source_id": "abc"},
            )
        )

    output = export_recorded_trade_events_csv(ndjson_path, csv_path)

    text = output.read_text(encoding="utf-8-sig")
    assert "market_id,cycle_id,timestamp,price,shares,outcome,action,source,metadata" in text
    assert '{""source_id"": ""abc""}' in text


def test_cycle_trade_event_recorder_splits_by_cycle(tmp_path) -> None:
    with CycleTradeEventRecorder(tmp_path) as recorder:
        recorder.record(
            TradeEvent(
                market_id="BTC",
                cycle_id="btc-updown-5m-1",
                timestamp=datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc),
                price=0.45,
                shares=10.0,
                outcome="up",
                action="buy",
            )
        )
        recorder.record(
            TradeEvent(
                market_id="BTC",
                cycle_id="btc-updown-5m-2",
                timestamp=datetime(2026, 3, 20, 12, 5, 0, tzinfo=timezone.utc),
                price=0.55,
                shares=8.0,
                outcome="down",
                action="sell",
            )
        )

    first = tmp_path / "btc-updown-5m-1" / "ws_trade_events.ndjson"
    second = tmp_path / "btc-updown-5m-2" / "ws_trade_events.ndjson"
    assert first.exists()
    assert second.exists()
    assert len(load_recorded_trade_events(first)) == 1
    assert len(load_recorded_trade_events(second)) == 1
