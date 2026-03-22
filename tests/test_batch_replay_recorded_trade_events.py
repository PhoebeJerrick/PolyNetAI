from __future__ import annotations

import pandas as pd

from scripts.batch_replay_recorded_trade_events import _discover_cycle_event_files, _write_batch_summary


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


def test_write_batch_summary_creates_summary_files(tmp_path) -> None:
    summary_df = pd.DataFrame(
        [
            {"cycle_slug": "cycle-a", "total_net_profit": 2.5},
            {"cycle_slug": "cycle-b", "total_net_profit": -1.0},
        ]
    )

    summary_csv, summary_md = _write_batch_summary(tmp_path, tmp_path, summary_df)

    assert summary_csv.exists()
    assert summary_md.exists()
    text = summary_md.read_text(encoding="utf-8")
    assert "批量回放摘要" in text
    assert "总净利润: 1.500000" in text
