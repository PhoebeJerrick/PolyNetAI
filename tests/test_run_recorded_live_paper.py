from __future__ import annotations

import pandas as pd

from scripts.run_recorded_live_paper import write_cycle_results_incremental


def test_write_cycle_results_incremental_populates_cycle_slug(tmp_path) -> None:
    out_dir = tmp_path / "streaming_out"
    cycle_row = {
        "cycle_id": "btc-updown-5m-1774147200",
        "cycle_net_profit": 1.2,
        "winner": "up",
        "account_cash": 101.2,
    }
    decision_rows = [
        {
            "cycle_id": "btc-updown-5m-1774147200",
            "selected_action": "buy",
            "selected_outcome": "up",
            "executed": True,
            "fill_fee": 0.01,
        }
    ]

    write_cycle_results_incremental(out_dir, 1, cycle_row, decision_rows, snapshot_rows=[])

    cycle_df = pd.read_csv(out_dir / "streaming_cycle_results.csv")
    decision_df = pd.read_csv(out_dir / "streaming_decision_results.csv")

    assert "cycle_slug" in cycle_df.columns
    assert "cycle_slug" in decision_df.columns
    assert cycle_df.loc[0, "cycle_slug"] == "btc-updown-5m-1774147200"
    assert decision_df.loc[0, "cycle_slug"] == "btc-updown-5m-1774147200"
