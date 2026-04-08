from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from polynet_ai.domain.models import TradeEvent


ORDERBOOK_TOP_METADATA_FIELDS: tuple[str, ...] = (
    "orderbook_snapshot_source",
    "orderbook_snapshot_at",
    "orderbook_snapshot_age_ms",
    "orderbook_snapshot_stale",
    "up_bid1_price",
    "up_bid1_size",
    "up_ask1_price",
    "up_ask1_size",
    "down_bid1_price",
    "down_bid1_size",
    "down_ask1_price",
    "down_ask1_size",
)


def trade_event_to_record(event: TradeEvent) -> dict[str, Any]:
    return {
        "market_id": event.market_id,
        "cycle_id": event.cycle_id,
        "timestamp": event.timestamp.isoformat(),
        "price": float(event.price),
        "shares": float(event.shares),
        "outcome": event.outcome,
        "action": event.action,
        "source": event.source,
        "metadata": dict(event.metadata),
    }


def flatten_selected_event_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metadata.get(key)
        for key in ORDERBOOK_TOP_METADATA_FIELDS
        if key in metadata
    }


def trade_event_from_record(record: dict[str, Any]) -> TradeEvent:
    return TradeEvent(
        market_id=str(record.get("market_id") or ""),
        cycle_id=str(record.get("cycle_id") or ""),
        timestamp=datetime.fromisoformat(str(record.get("timestamp") or "")),
        price=float(record.get("price") or 0.0),
        shares=float(record.get("shares") or 0.0),
        outcome=str(record.get("outcome") or "up"),  # type: ignore[arg-type]
        action=str(record.get("action") or "buy"),  # type: ignore[arg-type]
        source=str(record.get("source") or "market"),
        metadata=dict(record.get("metadata") or {}),
    )


class TradeEventRecorder:
    def __init__(self, path: str | Path, *, flush_interval_seconds: float = 1.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._flush_interval = max(0.1, flush_interval_seconds)
        self._last_flush = 0.0  # monotonic timestamp
        self._pending_writes = 0

    def record(self, event: TradeEvent) -> None:
        self._fh.write(json.dumps(trade_event_to_record(event), ensure_ascii=False) + "\n")
        self._pending_writes += 1
        import time as _time_mod
        now = _time_mod.monotonic()
        if now - self._last_flush >= self._flush_interval or self._pending_writes >= 50:
            self._fh.flush()
            self._last_flush = now
            self._pending_writes = 0

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "TradeEventRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class CycleTradeEventRecorder:
    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._cycle_recorders: dict[str, TradeEventRecorder] = {}
        self._finalized = False

    def _cycle_path(self, cycle_id: str) -> Path:
        return self.output_dir / cycle_id / "ws_trade_events.ndjson"

    def record(self, event: TradeEvent) -> None:
        recorder = self._cycle_recorders.get(event.cycle_id)
        if recorder is None:
            recorder = TradeEventRecorder(self._cycle_path(event.cycle_id))
            self._cycle_recorders[event.cycle_id] = recorder
        recorder.record(event)

    def close(self) -> None:
        if self._finalized:
            return
        for recorder in self._cycle_recorders.values():
            recorder.close()
        self._finalized = True

    def __enter__(self) -> "CycleTradeEventRecorder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def load_recorded_trade_events(path: str | Path) -> list[TradeEvent]:
    records_path = Path(path)
    events: list[TradeEvent] = []
    if not records_path.exists():
        return events
    with records_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            payload = json.loads(text)
            if not isinstance(payload, dict):
                continue
            events.append(trade_event_from_record(payload))
    return events


def export_recorded_trade_events_csv(
    input_path: str | Path,
    output_path: str | Path,
) -> Path:
    rows = [trade_event_to_record(event) for event in load_recorded_trade_events(input_path)]
    normalized_rows = []
    for row in rows:
        item = dict(row)
        item.update(flatten_selected_event_metadata(item.get("metadata") or {}))
        item["metadata"] = json.dumps(item.get("metadata") or {}, ensure_ascii=False, sort_keys=True)
        normalized_rows.append(item)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(normalized_rows).to_csv(destination, index=False, encoding="utf-8-sig")
    return destination


def iter_recorded_trade_events(path: str | Path) -> Iterable[TradeEvent]:
    for event in load_recorded_trade_events(path):
        yield event
