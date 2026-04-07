from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from polynet_ai.reporting.universal_stats import load_stats_config, run_universal_stats


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_load_stats_config_supports_batch_conf_style_and_ignores_job_lines(tmp_path) -> None:
    config_path = tmp_path / "batch.conf"
    config_path.write_text(
        "\n".join(
            [
                "input_dir = ../Input/5m-RawData",
                "time_window_mode = relative_start",
                "window_start_sec = 0",
                "window_end_sec = 120",
                "conditions = [",
                '  {"filter": "up_volume > 10", "group": "outcome == \'up\'"}',
                "]",
                "replay 30 configs/strategy.yaml artifacts/live/batch_jobs/demo 200 100 ../Input/5m-RawData",
            ]
        ),
        encoding="utf-8",
    )

    config = load_stats_config(config_path)

    assert config.input_dir == Path("../Input/5m-RawData")
    assert config.time_window_mode == "relative_start"
    assert config.window_end_sec == 120.0
    assert len(config.conditions) == 1
    assert config.conditions[0].filter_expr == "up_volume > 10"


def test_run_universal_stats_relative_start_computes_window_metrics(tmp_path) -> None:
    input_dir = tmp_path / "input"
    base = datetime.fromtimestamp(1_700_000_000, UTC).replace(tzinfo=None)
    _write_jsonl(
        input_dir / "raw.jsonl",
        [
            {
                "market_id": "btc-up-or-down-5m",
                "cycle_id": "btc-updown-5m-1700000000",
                "timestamp": (base + timedelta(seconds=10)).isoformat(),
                "price": 0.55,
                "shares": 10.0,
                "outcome": "up",
                "action": "buy",
            },
            {
                "market_id": "btc-up-or-down-5m",
                "cycle_id": "btc-updown-5m-1700000000",
                "timestamp": (base + timedelta(seconds=20)).isoformat(),
                "price": 0.45,
                "shares": 5.0,
                "outcome": "down",
                "action": "buy",
            },
            {
                "market_id": "btc-up-or-down-5m",
                "cycle_id": "btc-updown-5m-1700000000",
                "timestamp": (base + timedelta(seconds=250)).isoformat(),
                "price": 0.40,
                "shares": 1.0,
                "outcome": "down",
                "action": "buy",
            },
        ],
    )
    config_path = tmp_path / "batch.conf"
    output_path = tmp_path / "stats.csv"
    config_path.write_text(
        "\n".join(
            [
                f"input_dir = {input_dir}",
                "time_window_mode = relative_start",
                "window_start_sec = 0",
                "window_end_sec = 120",
                "win_rule = last_price",
                "correlation_var = delta_avg_price",
                "output_format = csv",
                f"output_path = {output_path}",
                "custom_metrics = {example_up_turnover: 'sum(price*shares) filter outcome=\"up\"'}",
            ]
        ),
        encoding="utf-8",
    )

    result = run_universal_stats(load_stats_config(config_path))

    assert len(result.cycle_df) == 1
    row = result.cycle_df.iloc[0]
    assert row["window_trade_count"] == 2
    assert row["up_volume"] == 10.0
    assert row["down_volume"] == 5.0
    assert round(float(row["delta_avg_price"]), 6) == 0.10
    assert row["winner"] == "up"
    assert row["example_up_turnover"] == 5.5
    written = pd.read_csv(output_path)
    assert written.iloc[0]["cycle_id"] == "btc-updown-5m-1700000000"


def test_run_universal_stats_relative_end_and_external_winner_file(tmp_path) -> None:
    input_dir = tmp_path / "input"
    cycle_dir = input_dir / "btc-updown-5m-1700000300"
    base = datetime.fromtimestamp(1_700_000_300, UTC).replace(tzinfo=None)
    _write_jsonl(
        cycle_dir / "ws_trade_events.ndjson",
        [
            {
                "market_id": "btc-up-or-down-5m",
                "cycle_id": "btc-updown-5m-1700000300",
                "timestamp": (base + timedelta(seconds=10)).isoformat(),
                "price": 0.51,
                "shares": 2.0,
                "outcome": "up",
                "action": "buy",
            },
            {
                "market_id": "btc-up-or-down-5m",
                "cycle_id": "btc-updown-5m-1700000300",
                "timestamp": (base + timedelta(seconds=260)).isoformat(),
                "price": 0.61,
                "shares": 8.0,
                "outcome": "down",
                "action": "buy",
            },
            {
                "market_id": "btc-up-or-down-5m",
                "cycle_id": "btc-updown-5m-1700000300",
                "timestamp": (base + timedelta(seconds=290)).isoformat(),
                "price": 0.64,
                "shares": 4.0,
                "outcome": "down",
                "action": "buy",
            },
        ],
    )
    external_path = tmp_path / "winners.csv"
    pd.DataFrame([
        {"cycle_id": "btc-updown-5m-1700000300", "winner": "down"},
    ]).to_csv(external_path, index=False)
    config_path = tmp_path / "batch.conf"
    config_path.write_text(
        "\n".join(
            [
                f"input_dir = {input_dir}",
                "time_window_mode = relative_end",
                "window_start_sec = -60",
                "window_end_sec = 0",
                "win_rule = external_file",
                f"external_result_path = {external_path}",
                "open_price_in_window = true",
                "open_price_condition = down_open > 0.6",
                "conditions = [",
                '  {"filter": "down_volume >= 10", "group": "outcome == \'down\'", "label": "tail_down"}',
                "]",
            ]
        ),
        encoding="utf-8",
    )

    result = run_universal_stats(load_stats_config(config_path))

    assert len(result.cycle_df) == 1
    row = result.cycle_df.iloc[0]
    assert row["window_trade_count"] == 2
    assert row["down_volume"] == 12.0
    assert row["winner"] == "down"
    assert row["down_open"] == 0.61
    assert result.condition_df.iloc[0]["label"] == "tail_down"
    assert result.condition_df.iloc[0]["ratio"] == 1.0
    assert result.summary["open_price_condition"]["up_win_ratio"] == 0.0