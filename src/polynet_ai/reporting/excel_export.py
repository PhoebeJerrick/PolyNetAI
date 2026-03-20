from __future__ import annotations

from pathlib import Path

import pandas as pd


def export_replay_to_excel(
    cycle_df: pd.DataFrame,
    decision_df: pd.DataFrame,
    metrics_df: pd.DataFrame,
    output_path: str | Path,
) -> Path:
    output = Path(output_path)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        cycle_df.to_excel(writer, sheet_name="cycles", index=False)
        decision_df.to_excel(writer, sheet_name="decisions", index=False)
        metrics_df.to_excel(writer, sheet_name="metrics", index=False)
    return output
