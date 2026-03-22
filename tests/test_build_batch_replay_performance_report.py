from __future__ import annotations

import pandas as pd

from scripts.build_batch_replay_performance_report import (
    TRACKER_STYLE_SHEET,
    build_report,
    _compute_max_drawdown,
)


def test_compute_max_drawdown_from_cycle_profit_series() -> None:
    profits = [3.0, -5.0, 2.0, -1.0]

    value = _compute_max_drawdown(profits)

    assert value == 5.0


def test_build_report_writes_xlsx_markdown_and_trade_process(tmp_path) -> None:
    batch_dir = tmp_path / "batch_replay_outputs"
    cycle_dir = batch_dir / "btc-updown-5m-1774147200"
    cycle_dir.mkdir(parents=True)

    pd.DataFrame(
        [
            {
                "cycle_slug": "btc-updown-5m-1774147200",
                "executed_trades": 2,
                "accepted_signals": 2,
                "blocked_signals": 0,
                "total_net_profit": 1.5,
                "total_fees": 0.1,
                "winner": "up",
                "account_cash": 101.5,
            }
        ]
    ).to_csv(batch_dir / "batch_replay_summary.csv", index=False, encoding="utf-8-sig")

    pd.DataFrame(
        [
            {
                "market_id": "btc-up-or-down-5m",
                "cycle_id": "btc-updown-5m-1774147200",
                "net_direction": "Up",
                "winner": "up",
                "account_cash": 101.5,
            }
        ]
    ).to_csv(cycle_dir / "cycles.csv", index=False, encoding="utf-8-sig")

    pd.DataFrame(
        [
            {
                "selected_outcome": "up",
                "selected_action": "buy",
                "selected_shares": 10.0,
                "executed": True,
            },
            {
                "selected_outcome": "up",
                "selected_action": "sell",
                "selected_shares": 5.0,
                "executed": True,
            },
        ]
    ).to_csv(cycle_dir / "decisions.csv", index=False, encoding="utf-8-sig")

    pd.DataFrame(
        [
            {
                "total_net_profit": 1.5,
                "total_fees": 0.1,
                "executed_trades": 2,
                "accepted_signals": 2,
                "blocked_signals": 0,
            }
        ]
    ).to_csv(cycle_dir / "metrics.csv", index=False, encoding="utf-8-sig")

    report_path = build_report(batch_dir)

    assert report_path.suffix == ".xlsx"
    assert report_path.exists()
    xl = pd.ExcelFile(report_path)
    assert "概览" in xl.sheet_names
    overview = pd.read_excel(report_path, sheet_name="概览")
    lookup = {str(k): v for k, v in zip(overview["项目"], overview["值"])}
    assert int(float(lookup["周期数"])) == 1
    assert abs(float(lookup["总净利润"]) - 1.5) < 1e-9
    assert abs(float(lookup["胜率"]) - 1.0) < 1e-9
    assert abs(float(lookup["最大回撤"]) - 0.0) < 1e-9

    assert TRACKER_STYLE_SHEET in xl.sheet_names
    tracker = pd.read_excel(report_path, sheet_name=TRACKER_STYLE_SHEET)
    assert "Up积累份数" in tracker.columns
    assert "周期净利润" in tracker.columns

    md_path = batch_dir / "batch_replay_performance_report_zh.md"
    assert md_path.exists()
    text = md_path.read_text(encoding="utf-8")
    assert "批量离线回放总绩效报告" in text
    assert "总净利润: 1.500000" in text
    assert "胜率: 100.00%" in text
    assert "最大回撤: 0.000000" in text
    assert (batch_dir / "batch_replay_trade_process_zh.md").exists()
    assert not (batch_dir / "batch_replay_direction_distribution.csv").exists()
    assert not (batch_dir / "batch_replay_summary_enriched.csv").exists()
