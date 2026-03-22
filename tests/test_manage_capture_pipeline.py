from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pandas as pd

from scripts.manage_capture_pipeline import (
    _build_capture_command,
    _build_pipeline_child_argv,
    _load_manifest_rows,
    _print_status,
)


class _Args:
    output_dir = "artifacts/live/ws_capture_btc_10cycles"
    slug_prefix = "btc-updown-5m-"
    max_cycles = 10
    status_every = 100
    poll_interval_seconds = 3.0
    ping_interval_seconds = 10.0
    receive_timeout_seconds = 1.0
    cycle_grace_seconds = 20.0
    start_buffer_seconds = 2.0
    market_slugs = None
    market_slugs_file = None
    env_file = "APIs/ApiConfig.env"
    account_index = 2


def test_build_capture_command_contains_expected_core_args() -> None:
    command = _build_capture_command(_Args())

    assert "--output-dir" in command
    assert "artifacts/live/ws_capture_btc_10cycles" in command
    assert "--max-cycles" in command
    assert "10" in command
    assert "--start-buffer-seconds" in command


def test_build_pipeline_child_argv_strips_daemon_and_log_flags() -> None:
    raw_argv = [
        "run-full",
        "--daemonize",
        "--output-dir",
        "artifacts/live/ws_capture_btc_10cycles",
        "--log-file",
        "logs/pipeline.log",
        "--pid-file=logs/pipeline.pid",
        "--max-cycles",
        "10",
    ]

    command = _build_pipeline_child_argv(raw_argv)

    assert "--daemonize" not in command
    assert "--log-file" not in command
    assert not any(str(item).startswith("--pid-file") for item in command)
    assert "run-full" in command


def test_load_manifest_rows_reads_completed_cycles(tmp_path) -> None:
    path = tmp_path / "capture_manifest.json"
    path.write_text('[{"cycle_id":"cycle-a","event_count":123}]', encoding="utf-8")

    rows = _load_manifest_rows(tmp_path)

    assert len(rows) == 1
    assert rows[0]["cycle_id"] == "cycle-a"


def test_print_status_shows_progress_and_report(tmp_path, capsys) -> None:
    output_dir = tmp_path / "capture_job"
    output_dir.mkdir()
    (output_dir / "capture_manifest.json").write_text(
        '[{"cycle_id":"cycle-a","event_count":100,"first_timestamp":"t1","last_timestamp":"t2"}]',
        encoding="utf-8",
    )
    batch_dir = output_dir / "batch_replay_outputs"
    batch_dir.mkdir()
    pd.DataFrame([{"cycle_slug": "cycle-a", "total_net_profit": 1.25}]).to_csv(
        batch_dir / "batch_replay_summary.csv",
        index=False,
        encoding="utf-8-sig",
    )
    (batch_dir / "batch_replay_performance_report_zh.md").write_text("# report", encoding="utf-8")
    (output_dir / "capture.log").write_text("line1\nline2\n", encoding="utf-8")

    args = Namespace(tail_lines=2, output_dir=str(output_dir))

    result = _print_status(args)

    captured = capsys.readouterr().out
    assert result == 0
    assert "已完成周期数: 1" in captured
    assert "批量回放总净利润: 1.250000" in captured
    assert "总报告: 已生成" in captured
