from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from polynet_ai.domain.models import TradeEvent


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
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")

    def record(self, event: TradeEvent) -> None:
        self._fh.write(json.dumps(trade_event_to_record(event), ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "TradeEventRecorder":
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
        item["metadata"] = json.dumps(item.get("metadata") or {}, ensure_ascii=False, sort_keys=True)
        normalized_rows.append(item)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(normalized_rows).to_csv(destination, index=False, encoding="utf-8-sig")
    return destination


def iter_recorded_trade_events(path: str | Path) -> Iterable[TradeEvent]:
    for event in load_recorded_trade_events(path):
        yield event
