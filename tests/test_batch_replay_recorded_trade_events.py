from __future__ import annotations

import pandas as pd

from scripts.batch_replay_recorded_trade_events import (
    _discover_cycle_event_files,
    _effective_use_streaming,
)
from scripts.build_batch_replay_performance_report import write_batch_trade_process_zh


def test_effective_use_streaming_processing_mode_overrides() -> None:
    assert _effective_use_streaming(use_streaming=True, processing_mode="merged") is False
    assert _effective_use_streaming(use_streaming=False, processing_mode="per-cycle") is True
    assert _effective_use_streaming(use_streaming=False, processing_mode=None) is False


def test_discover_cycle_event_files_reads_per_cycle_ndjson(tmp_path) -> None:
    cycle_a = tmp_path / "btc-updown-5m-1"
    cycle_b = tmp_path / "btc-updown-5m-2"
    cycle_a.mkdir()
    cycle_b.mkdir()
    (cycle_a / "ws_trade_events.ndjson").write_text("", encoding="utf-8")
    (cycle_b / "ws_trade_events.ndjson").write_text("", encoding="utf-8")

    files = _discover_cycle_event_files(tmp_path)

    assert [path.parent.name for path in files] == ["btc-updown-5m-1", "btc-updown-5m-2"]


def test_discover_cycle_event_files_ignores_non_cycle_output_dirs(tmp_path) -> None:
    cycle_dir = tmp_path / "btc-updown-5m-1774240500"
    output_dir = tmp_path / "batch_replay_outputs"
    cycle_dir.mkdir()
    output_dir.mkdir()
    (cycle_dir / "ws_trade_events.ndjson").write_text("", encoding="utf-8")
    (output_dir / "ws_trade_events.ndjson").write_text("", encoding="utf-8")

    files = _discover_cycle_event_files(tmp_path)

    assert [path.parent.name for path in files] == ["btc-updown-5m-1774240500"]


def test_discover_cycle_event_files_ignores_cycle_id_mismatch(tmp_path) -> None:
    valid_dir = tmp_path / "btc-updown-5m-1774240200"
    mismatched_dir = tmp_path / "btc-updown-5m-1774240500"
    valid_dir.mkdir()
    mismatched_dir.mkdir()
    (valid_dir / "ws_trade_events.ndjson").write_text(
        '{"cycle_id":"btc-updown-5m-1774240200","price":0.5}\n',
        encoding="utf-8",
    )
    (mismatched_dir / "ws_trade_events.ndjson").write_text(
        '{"cycle_id":"btc-updown-5m-1774240200","price":0.5}\n',
        encoding="utf-8",
    )

    files = _discover_cycle_event_files(tmp_path)

    assert [path.parent.name for path in files] == ["btc-updown-5m-1774240200"]


def test_discover_cycle_event_files_skips_files_without_valid_cycle_id(tmp_path) -> None:
    cycle_dir = tmp_path / "btc-updown-5m-1774240200"
    cycle_dir.mkdir()
    (cycle_dir / "ws_trade_events.ndjson").write_text(
        '\nnot-json\n{"price":0.5}\n',
        encoding="utf-8",
    )

    files = _discover_cycle_event_files(tmp_path)

    assert files == []


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
                "market_outcome": "down",
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
    dec = pd.read_excel(path, sheet_name="决策流水")
    assert "cycle-a" in snap["cycle_slug"].astype(str).tolist()
    assert "winner" in snap.columns
    assert "市场方向" in dec.columns
    assert "策略方向" in dec.columns
