from __future__ import annotations

import argparse

import pandas as pd

from analyze_polymarket_tracker import compute
from scripts.build_tracker_position_compare_chart import resolve_columns


def _build_args() -> argparse.Namespace:
    return argparse.Namespace(
        input=None,
        output=None,
        sheet=None,
        price_col=None,
        qty_col=None,
        outcome_col=None,
        action_col=None,
        coin_col=None,
        cycle_col=None,
        group_cols=None,
        config=None,
        position_value_denominator=1000.0,
        winner=None,
    )


def test_compute_clamps_negative_position_and_marks_anomaly() -> None:
    df = pd.DataFrame(
        {
            "下注时间距开盘差(分，秒)": ["00:01", "00:02", "00:03"],
            "时间周期": ["cycle-1", "cycle-1", "cycle-1"],
            "结果代币类型": ["Up", "Up", "Up"],
            "操作方向": ["SELL", "BUY", "SELL"],
            "投注份数": [5.0, 3.0, 1.0],
            "成交价格": [0.6, 0.4, 0.7],
        }
    )

    proc, _meta = compute(df, _build_args())
    rows = proc[proc["下注时间距开盘差(分，秒)"].astype(str) != "【周期小计】"].reset_index(drop=True)

    assert rows.loc[0, "Up积累份数"] == 0
    assert rows.loc[0, "Up持仓成本"] == 0
    assert rows.loc[0, "当前总持仓份数"] == 0
    assert rows.loc[0, "净持仓份数"] == 0
    assert rows.loc[0, "净持仓成本差额"] == 0
    assert "Up卖出超持仓 5份" in str(rows.loc[0, "持仓异常"])
    assert rows.loc[0, "同向成交价波动幅度(%)"] == ""
    assert rows.loc[0, "相对于加权均价的价格波动百分比"] == ""
    assert rows.loc[0, "持仓价值加/减仓百分比"] == ""

    assert rows.loc[1, "Up积累份数"] == 3
    assert rows.loc[1, "Up持仓成本"] == 1.2
    assert rows.loc[1, "净持仓成本差额"] == 1.2
    assert rows.loc[1, "同向成交价波动幅度(%)"] == -0.333
    assert rows.loc[1, "相对于加权均价的价格波动百分比"] == 0
    assert rows.loc[1, "持仓价值加/减仓百分比"] == ""
    assert rows.loc[2, "Up积累份数"] == 2
    assert rows.loc[2, "Up持仓成本"] == 0.8
    assert rows.loc[2, "净持仓成本差额"] == 0.8
    assert rows.loc[2, "同向成交价波动幅度(%)"] == 0.75
    assert rows.loc[2, "相对于加权均价的价格波动百分比"] == 0.75
    assert rows.loc[2, "持仓价值加/减仓百分比"] == -0.333

    subtotal = proc[proc["下注时间距开盘差(分，秒)"].astype(str) == "【周期小计】"].iloc[0]
    assert subtotal["持仓异常"] == "本周期持仓异常 1 笔"


def test_compute_adds_cost_and_position_value_change_columns_in_expected_order() -> None:
    df = pd.DataFrame(
        {
            "下注时间距开盘差(分，秒)": ["00:01", "00:02", "00:03", "00:04", "00:05"],
            "时间周期": ["cycle-1", "cycle-1", "cycle-1", "cycle-1", "cycle-1"],
            "结果代币类型": ["Up", "Up", "Up", "Down", "Down"],
            "操作方向": ["BUY", "BUY", "SELL", "BUY", "BUY"],
            "投注份数": [2.0, 1.0, 1.0, 4.0, 2.0],
            "成交价格": [0.5, 0.6, 0.7, 0.25, 0.2],
        }
    )

    proc, _meta = compute(df, _build_args())
    rows = proc[proc["下注时间距开盘差(分，秒)"].astype(str) != "【周期小计】"].reset_index(drop=True)
    cols = list(proc.columns)

    price_idx = cols.index("成交价格")
    assert cols[price_idx + 1 : price_idx + 10] == [
        "同向成交价波动幅度(%)",
        "相对于加权均价的价格波动百分比",
        "Up积累份数",
        "Up持仓成本",
        "Up的加权均价",
        "Down积累份数",
        "Down持仓成本",
        "Down加权均价",
        "当前总持仓份数",
    ]

    assert rows.loc[0, "Up持仓成本"] == 1
    assert rows.loc[0, "相对于加权均价的价格波动百分比"] == 0
    assert rows.loc[1, "Up持仓成本"] == 1.6
    assert rows.loc[1, "同向成交价波动幅度(%)"] == 0.2
    assert rows.loc[1, "相对于加权均价的价格波动百分比"] == 0.125
    assert rows.loc[1, "持仓价值加/减仓百分比"] == 0.6
    assert rows.loc[2, "Up持仓成本"] == 1.067
    assert rows.loc[2, "相对于加权均价的价格波动百分比"] == 0.312
    assert rows.loc[2, "持仓价值加/减仓百分比"] == -0.333
    assert rows.loc[3, "Down持仓成本"] == 1
    assert rows.loc[3, "相对于加权均价的价格波动百分比"] == 0
    assert rows.loc[3, "持仓价值加/减仓百分比"] == ""
    assert rows.loc[4, "Down持仓成本"] == 1.4
    assert rows.loc[4, "同向成交价波动幅度(%)"] == -0.2
    assert rows.loc[4, "相对于加权均价的价格波动百分比"] == -0.143
    assert rows.loc[4, "持仓价值加/减仓百分比"] == 0.4


def test_chart_builder_accepts_new_net_cost_column_name() -> None:
    df = pd.DataFrame(
        {
            "成交价格": [0.4],
            "投注份数": [3.0],
            "结果代币类型": ["Up"],
            "Up积累份数": [3.0],
            "Up的加权均价": [0.4],
            "Down积累份数": [0.0],
            "Down加权均价": [0.0],
            "当前总持仓份数": [3.0],
            "净持仓份数": [3.0],
            "净持仓成本差额": [1.2],
            "时间周期": ["cycle-1"],
        }
    )

    cols = resolve_columns(df)
    assert cols["净持仓成本差额"] == "净持仓成本差额"