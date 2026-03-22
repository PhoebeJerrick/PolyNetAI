from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from polynet_ai.strategy.spec import StrategyConfig
from scripts.compare_baseline_vs_overrides import compare_streaming_csv


def build_config() -> StrategyConfig:
    return StrategyConfig(
        raw={
            "cycle": {"cycle_seconds": 300, "last_minute_seconds": 60},
            "order_sizing": {"base_order_size": 8.0, "min_order_size": 2.0, "max_order_size": 60.0, "volatility_order_scale": 20.0},
            "exposure": {"max_abs_exposure": 200.0, "hedge_trigger_value": 50.0, "hedge_scale": 0.15, "max_grid_net_position": 20.0, "max_strategy_trades_per_cycle": 12},
            "trend": {"min_trend_strength": 0.35, "trend_price_edge": 0.03, "trend_scale": 0.15},
            "grid": {"grid_low_percentile": 0.25, "grid_high_percentile": 0.75},
            "mean_reversion": {
                "up_buy_deviation": 0.10,
                "down_buy_deviation": 0.10,
                "mean_reversion_sell_up_deviation": 0.20,
                "mean_reversion_sell_down_deviation": 0.20,
                "deviation_scale": 45.0,
            },
            "profit_taking": {"take_profit_up_deviation": 0.20, "take_profit_down_deviation": 0.20, "take_profit_fraction": 0.35},
            "stop_loss": {"stop_loss_cycle_loss": 20.0, "stop_loss_fraction": 0.50},
            "last_minute": {"last_minute_min_confidence": 0.60, "tail_profit_scale": 0.35, "tail_volatility_scale": 25.0, "max_tail_exposure": 40.0},
            "execution": {"fee_rate": 0.002, "slippage_bps": 10},
            "priorities": {"risk": 10, "last_minute": 20, "stop_loss": 30, "hedge": 40, "take_profit": 50, "grid": 60, "mean_reversion": 70, "trend": 80},
        }
    )


def _slug(ts: datetime) -> str:
    unix_ts = int(ts.replace(tzinfo=timezone.utc).timestamp())
    return f"btc-updown-5m-{unix_ts}"


def test_compare_streaming_csv_counts_empty_cycles_from_progress(tmp_path: Path) -> None:
    t0 = datetime(2026, 3, 20, 12, 0, 0)
    t1 = datetime(2026, 3, 20, 12, 5, 0)
    t2 = datetime(2026, 3, 20, 12, 10, 0)

    csv_path = tmp_path / "sample.csv"
    pd.DataFrame(
        [
            {
                "price": 0.45,
                "shares": 10,
                "outcome": "up",
                "action": "buy",
                "symbol": "BTC",
                "event_start_time": t0.isoformat(),
                "timestamp": (t0).isoformat(),
                "market_slug": _slug(t0),
                "condition_id": "cond-a",
                "transaction_hash": "tx-a",
                "asset": "asset-up",
            },
            {
                "price": 0.62,
                "shares": 5,
                "outcome": "up",
                "action": "buy",
                "symbol": "BTC",
                "event_start_time": t2.isoformat(),
                "timestamp": (t2).isoformat(),
                "market_slug": _slug(t2),
                "condition_id": "cond-c",
                "transaction_hash": "tx-c",
                "asset": "asset-up",
            },
        ]
    ).to_csv(csv_path, index=False, encoding="utf-8-sig")

    progress_path = csv_path.with_suffix(".progress.json")
    progress_path.write_text(
        json.dumps({"done_slugs": [_slug(t0), _slug(t1), _slug(t2)]}, ensure_ascii=False),
        encoding="utf-8",
    )

    out_df = compare_streaming_csv(
        csv_path,
        csv_chunksize=100,
        starting_cash=100.0,
        base_cfg=build_config(),
        trial_cfg=build_config(),
        trial_label="trial_022",
    )

    assert set(out_df["label"].tolist()) == {"baseline", "trial_022"}
    assert set(out_df["total_cycles"].tolist()) == {3}
    assert set(out_df["observed_cycles_with_trades"].tolist()) == {2}
    assert set(out_df["empty_cycles"].tolist()) == {1}
