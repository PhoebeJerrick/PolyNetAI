from __future__ import annotations

from pathlib import Path

import pandas as pd

from polynet_ai.reporting.dashboard import (
    _sanitize_json_for_html_script,
    generate_dashboard_bundle,
    generate_dashboard_from_directory,
    refresh_dashboard_html_shell,
)


def test_sanitize_json_for_html_script_avoids_closing_tag() -> None:
    raw = '{"note":"x</script>y"}'
    out = _sanitize_json_for_html_script(raw)
    assert "</script>" not in out
    assert r"<\/script" in out


def build_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    metrics_df = pd.DataFrame(
        [
            {
                "total_cycles": 2,
                "total_net_profit": 12.5,
                "average_cycle_profit": 6.25,
                "sharpe_ratio": 1.23,
                "win_rate": 0.5,
                "max_drawdown": 3.2,
                "total_fees": 0.4,
                "total_signals": 10,
                "accepted_signals": 4,
                "blocked_signals": 6,
                "executed_trades": 4,
                "signal_execution_rate": 0.4,
                "selected_rule_grid": 3,
                "selected_rule_stop_loss": 2,
                "executed_rule_grid": 2,
                "executed_rule_stop_loss": 1,
            }
        ]
    )
    cycles_df = pd.DataFrame(
        [
            {"market_id": "BTC", "cycle_id": r"D:\PassiveIncome\Quantification\Projects\PolyMkt\Input\04071459\btc-updown-5m-1775345100", "cycle_net_profit": 10.0},
            {"market_id": "BTC", "cycle_id": "c2", "cycle_net_profit": 2.5},
        ]
    )
    decisions_df = pd.DataFrame(
        [
            {"timestamp": "2026-01-01 00:00:01", "selected_rule": "grid", "selected_action": "buy", "selected_outcome": "up", "risk_status": "accepted", "executed": True, "fill_price": 0.5, "cycle_net_profit": 1.0},
            {"timestamp": "2026-01-01 00:00:02", "selected_rule": "stop_loss", "selected_action": "sell", "selected_outcome": "down", "risk_status": "blocked", "executed": False, "fill_price": 0.0, "cycle_net_profit": -1.0},
        ]
    )
    snapshots_df = pd.DataFrame(
        [
            {"timestamp": "2026-01-01 00:00:01", "market_id": "BTC", "cycle_id": r"D:\PassiveIncome\Quantification\Projects\PolyMkt\Input\04071459\btc-updown-5m-1775345100", "net_position": 3.0, "cycle_net_profit": 1.0, "account_cash": 1001.0, "up_last_price": 0.52, "down_last_price": 0.48},
            {"timestamp": "2026-01-01 00:00:02", "market_id": "BTC", "cycle_id": "c2", "net_position": 1.0, "cycle_net_profit": 12.5, "account_cash": 1012.5, "up_last_price": 0.55, "down_last_price": 0.45, "up_bid1_price": 0.54, "up_bid1_size": 18.0, "up_ask1_price": 0.55, "up_ask1_size": 9.0, "down_bid1_price": 0.44, "down_bid1_size": 15.0, "down_ask1_price": 0.45, "down_ask1_size": 7.0, "orderbook_snapshot_at": "2026-01-01T00:00:02", "orderbook_snapshot_age_ms": 0.0},
        ]
    )
    return metrics_df, cycles_df, decisions_df, snapshots_df


def test_generate_dashboard_bundle_writes_expected_files(tmp_path: Path) -> None:
    metrics_df, cycles_df, decisions_df, snapshots_df = build_frames()
    artifacts = generate_dashboard_bundle(
        metrics_df=metrics_df,
        cycles_df=cycles_df,
        decisions_df=decisions_df,
        snapshots_df=snapshots_df,
        output_dir=tmp_path,
        title="Demo Dashboard",
    )
    assert artifacts.html_path.exists()
    assert artifacts.markdown_path.exists()
    assert artifacts.summary_csv_path.exists()
    assert artifacts.state_script_path.exists()
    html_text = artifacts.html_path.read_text(encoding="utf-8")
    assert "Demo Dashboard" in html_text
    assert "dashboard_state.js" in html_text
    assert "config-file-select" in html_text
    assert "/api/configs" in html_text
    assert "cycle / batch_replay（生命周期与回放）" in html_text
    assert "position / capital / exposure（仓位与敞口）" in html_text
    assert "priorities（扁平回退，可选）" in html_text
    assert "readConfigFormWithValidation" in html_text
    assert "config-field-error" in html_text
    assert "config-invalid" in html_text
    assert "Up / Down 实时价格曲线" in html_text
    assert "夏普率" in html_text
    assert "最近盘口快照" in html_text
    assert "运行控制台" in html_text
    assert "launcher-profiles" in html_text
    assert "initializeRunConsole()" in html_text
    assert "collectLauncherOverrides" in html_text
    assert "launcherState.draftOverrides" in html_text
    assert "replaceLauncherDraftWithCatalog" in html_text
    assert "保存为默认值" in html_text
    assert "参数偏好文件" in html_text
    assert "data-launch-field" in html_text
    assert "高风险" in html_text
    assert "建议范围" in html_text
    assert "type=\"range\"" in html_text
    assert "<select class=\"config-select\"" in html_text
    assert "window.__POLYNET_DASHBOARD_STATE__" in artifacts.state_script_path.read_text(encoding="utf-8")
    assert "btc-updown-5m-1775345100" in html_text
    assert r"D:\PassiveIncome\Quantification\Projects\PolyMkt\Input\04071459\btc-updown-5m-1775345100" not in html_text
    assert "核心指标" in artifacts.markdown_path.read_text(encoding="utf-8")
    assert "夏普率" in artifacts.markdown_path.read_text(encoding="utf-8")
    assert "最近盘口快照" in artifacts.markdown_path.read_text(encoding="utf-8")
    summary_text = artifacts.summary_csv_path.read_text(encoding="utf-8-sig")
    assert "alert_count" in summary_text


def test_refresh_dashboard_html_shell_writes_html(tmp_path: Path) -> None:
    out = tmp_path / "shell"
    artifacts = refresh_dashboard_html_shell(out, title="Shell Only")
    assert artifacts.html_path.exists()
    html_text = artifacts.html_path.read_text(encoding="utf-8")
    assert "Shell Only" in html_text
    assert "CONFIG_PARAM_META" in html_text or "enrichConfigSchemaField" in html_text


def test_generate_dashboard_from_directory_reads_csv_inputs(tmp_path: Path) -> None:
    metrics_df, cycles_df, decisions_df, snapshots_df = build_frames()
    metrics_df.to_csv(tmp_path / "metrics.csv", index=False, encoding="utf-8-sig")
    cycles_df.to_csv(tmp_path / "cycles.csv", index=False, encoding="utf-8-sig")
    decisions_df.to_csv(tmp_path / "decisions.csv", index=False, encoding="utf-8-sig")
    snapshots_df.to_csv(tmp_path / "snapshots.csv", index=False, encoding="utf-8-sig")
    artifacts = generate_dashboard_from_directory(tmp_path, title="Directory Dashboard")
    assert artifacts.html_path.exists()
    assert artifacts.markdown_path.exists()


def test_dashboard_highlights_alerts_in_html_and_markdown(tmp_path: Path) -> None:
    metrics_df, cycles_df, decisions_df, snapshots_df = build_frames()
    metrics_df.loc[0, "max_drawdown"] = 35.0
    metrics_df.loc[0, "blocked_signals"] = 9
    metrics_df.loc[0, "total_signals"] = 10
    snapshots_df.loc[snapshots_df.index[-1], "net_position"] = 55.0
    cycles_df["net_position_value"] = [12.0, 65.0]
    artifacts = generate_dashboard_bundle(
        metrics_df=metrics_df,
        cycles_df=cycles_df,
        decisions_df=decisions_df,
        snapshots_df=snapshots_df,
        output_dir=tmp_path / "alerts",
        title="Alert Dashboard",
    )
    html_text = artifacts.html_path.read_text(encoding="utf-8")
    markdown_text = artifacts.markdown_path.read_text(encoding="utf-8")
    summary_text = artifacts.summary_csv_path.read_text(encoding="utf-8-sig")
    assert "告警视图" in html_text
    assert "高回撤告警" in html_text or "回撤偏高" in html_text
    assert "信号阻塞率过高" in html_text or "信号阻塞率偏高" in html_text
    assert "尾盘留仓过大" in html_text or "尾盘留仓偏大" in html_text
    assert "## 告警视图" in markdown_text
    assert "alert_codes" in summary_text
