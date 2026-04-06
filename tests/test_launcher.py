from __future__ import annotations

from pathlib import Path

from polynet_ai.launcher import apply_profile_overrides, build_launch_profiles, build_project_help_text, resolve_profile_values


def test_build_launch_profiles_points_to_dashboard_dir(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    sim = profiles["sim-paper"]
    market = profiles["market-paper"]

    assert "--output-dir" in sim.command
    assert str(tmp_path) in sim.command
    assert "scripts/run_recorded_live_paper.py" in sim.command

    assert "--output-dir" in market.command
    assert str(tmp_path) in market.command
    assert "scripts/run_polymarket_live_paper.py" in market.command
    assert "--record-orderbook-top" in market.command
    assert "--orderbook-refresh-seconds" in market.command


def test_project_help_text_contains_unified_commands(tmp_path: Path) -> None:
    text = build_project_help_text(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    assert "python scripts/run_polynet.py help" in text
    assert "python scripts/run_polynet.py profiles" in text
    assert "python scripts/run_polynet.py start sim-paper" in text
    assert "python scripts/run_polynet.py start market-paper" in text
    assert "实盘验证，但仍是 paper 模式" in text


def test_apply_profile_overrides_updates_market_profile_command(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )
    updated = apply_profile_overrides(
        profiles["market-paper"],
        {
            "slug_prefix": "eth-updown-5m-",
            "max_cycles": 25,
            "starting_cash": 2500,
            "orderbook_refresh_seconds": 1.2,
        },
    )

    assert "eth-updown-5m-" in updated.command
    assert "25" in updated.command
    assert "2500" in updated.command
    assert "1.2" in updated.command


def test_apply_profile_overrides_updates_sim_profile_command(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )
    updated = apply_profile_overrides(
        profiles["sim-paper"],
        {
            "cycle_glob": "btc-updown-5m-*",
            "input_dirs": "artifacts/live/record_job;artifacts/live/record_job_v2",
            "starting_cash": 5000,
            "max_cycles": 25,
        },
    )

    assert "btc-updown-5m-*" in updated.command
    assert "artifacts/live/record_job;artifacts/live/record_job_v2" in updated.command
    assert "5000" in updated.command
    assert "25" in updated.command


def test_apply_profile_overrides_handles_checkbox_flags(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    enabled = apply_profile_overrides(
        profiles["sim-paper"],
        {"include_trade_process": True},
    )
    disabled = apply_profile_overrides(
        profiles["sim-paper"],
        {"include_trade_process": False},
    )

    assert "--include-trade-process" in enabled.command
    assert "--include-trade-process" not in disabled.command
    assert "False" not in enabled.command
    assert "False" not in disabled.command


def test_resolve_profile_values_uses_defaults_for_missing_fields(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    resolved = resolve_profile_values(
        profiles["market-paper"],
        {"slug_prefix": "eth-updown-5m-"},
    )

    assert resolved["slug_prefix"] == "eth-updown-5m-"
    assert resolved["max_cycles"] == 10
    assert resolved["starting_cash"] == 200


def test_resolve_profile_values_coerces_checkbox_strings(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    resolved_false = resolve_profile_values(
        profiles["sim-paper"],
        {"include_trade_process": "False"},
    )
    resolved_true = resolve_profile_values(
        profiles["sim-paper"],
        {"include_trade_process": "true"},
    )

    assert resolved_false["include_trade_process"] is False
    assert resolved_true["include_trade_process"] is True


def test_capital_reset_mode_field_exists_with_default_fixed(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    # Check field exists in both profiles
    sim_profile = profiles["sim-paper"]
    market_profile = profiles["market-paper"]
    
    sim_field = next((f for f in sim_profile.fields if f.name == "capital_reset_mode"), None)
    market_field = next((f for f in market_profile.fields if f.name == "capital_reset_mode"), None)
    market_ob_field = next((f for f in market_profile.fields if f.name == "record_orderbook_top"), None)
    
    assert sim_field is not None, "capital_reset_mode field missing from sim-paper profile"
    assert market_field is not None, "capital_reset_mode field missing from market-paper profile"
    assert market_ob_field is not None, "record_orderbook_top field missing from market-paper profile"
    
    # Check defaults and options
    assert sim_field.default == "fixed"
    assert market_field.default == "fixed"
    assert market_ob_field.default is True
    assert "fixed" in sim_field.options
    assert "cumulative" in sim_field.options


def test_market_profile_checkbox_can_disable_orderbook_recording(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )

    updated = apply_profile_overrides(
        profiles["market-paper"],
        {"record_orderbook_top": False},
    )

    assert "--record-orderbook-top" not in updated.command


def test_capital_reset_mode_override_in_commands(tmp_path: Path) -> None:
    profiles = build_launch_profiles(
        root=Path("D:/PassiveIncome/Quantification/Projects/PolyMkt/PolyNetAI"),
        dashboard_dir=tmp_path,
        python_executable="python",
    )
    
    # Test sim-paper with cumulative mode
    updated_sim = apply_profile_overrides(
        profiles["sim-paper"],
        {"capital_reset_mode": "cumulative"},
    )
    assert "--capital-reset-mode" in updated_sim.command
    assert "cumulative" in updated_sim.command
    
    # Test market-paper with fixed mode (default)
    updated_market = apply_profile_overrides(
        profiles["market-paper"],
        {"slug_prefix": "btc-updown-5m-"},
    )
    assert "--capital-reset-mode" in updated_market.command
    assert "fixed" in updated_market.command
