from __future__ import annotations

from scripts.batch_replay_recorded_trade_events import _discover_cycle_event_files


def test_discover_cycle_event_files_reads_per_cycle_ndjson(tmp_path) -> None:
    cycle_a = tmp_path / "btc-updown-5m-1"
    cycle_b = tmp_path / "btc-updown-5m-2"
    cycle_a.mkdir()
    cycle_b.mkdir()
    (cycle_a / "ws_trade_events.ndjson").write_text("", encoding="utf-8")
    (cycle_b / "ws_trade_events.ndjson").write_text("", encoding="utf-8")
    (tmp_path / "ws_trade_events_all.ndjson").write_text("", encoding="utf-8")

    files = _discover_cycle_event_files(tmp_path)

    assert [path.parent.name for path in files] == ["btc-updown-5m-1", "btc-updown-5m-2"]
