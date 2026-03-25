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
        },
    )

    assert "eth-updown-5m-" in updated.command
    assert "25" in updated.command
    assert "2500" in updated.command


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
    assert resolved["starting_cash"] == 1000
