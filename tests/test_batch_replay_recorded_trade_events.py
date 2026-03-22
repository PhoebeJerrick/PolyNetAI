from __future__ import annotations

import pandas as pd

from scripts.batch_replay_recorded_trade_events import _discover_cycle_event_files
from scripts.build_batch_replay_performance_report import write_batch_trade_process_zh


def test_discover_cycle_event_files_reads_per_cycle_ndjson(tmp_path) -> None:
    cycle_a = tmp_path / "btc-updown-5m-1"
    cycle_b = tmp_path / "btc-updown-5m-2"
    cycle_a.mkdir()
    cycle_b.mkdir()
    (cycle_a / "ws_trade_events.ndjson").write_text("", encoding="utf-8")
    (cycle_b / "ws_trade_events.ndjson").write_text("", encoding="utf-8")

    files = _discover_cycle_event_files(tmp_path)

    assert [path.parent.name for path in files] == ["btc-updown-5m-1", "btc-updown-5m-2"]


def test_write_batch_trade_process_zh_writes_xlsx(tmp_path) -> None:
    cycle_df = pd.DataFrame(
        [
            {"cycle_slug": "cycle-a", "winner": "up", "account_cash": 101.0},
        ]
    )
    decision_df = pd.DataFrame(
        [
            {
                "cycle_slug": "cycle-a",
                "selected_outcome": "up",
                "selected_action": "buy",
                "executed": True,
            },
        ]
    )

    path = write_batch_trade_process_zh(
        input_dir=tmp_path,
        cycle_df=cycle_df,
        decision_df=decision_df,
        output_path=tmp_path,
    )

    assert path.suffix == ".xlsx"
    assert path.exists()
    assert not (tmp_path / "batch_replay_trade_process_zh.md").exists()
    snap = pd.read_excel(path, sheet_name="周期快照")
    assert "cycle-a" in snap["cycle_slug"].astype(str).tolist()
    assert "winner" in snap.columns
