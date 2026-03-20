from __future__ import annotations

from pathlib import Path

from polynet_ai.strategy.spec import StrategyConfig
from polynet_ai.strategy.tuning import load_scenarios


def test_strategy_config_with_overrides_updates_nested_values() -> None:
    config = StrategyConfig(
        raw={
            "trend": {"min_trend_strength": 0.35},
            "stop_loss": {"stop_loss_cycle_loss": 20.0},
        }
    )
    updated = config.with_overrides(
        {
            "trend.min_trend_strength": 0.25,
            "stop_loss.stop_loss_cycle_loss": 12.0,
            "new_section.flag": True,
        }
    )
    assert config.get("trend.min_trend_strength") == 0.35
    assert updated.get("trend.min_trend_strength") == 0.25
    assert updated.get("stop_loss.stop_loss_cycle_loss") == 12.0
    assert updated.get("new_section.flag") is True


def test_load_scenarios_supports_named_and_grid(tmp_path: Path) -> None:
    sweep_path = tmp_path / "sweep.yaml"
    sweep_path.write_text(
        "\n".join(
            [
                "scenarios:",
                "  - name: baseline",
                "    overrides:",
                "      trend.min_trend_strength: 0.35",
                "grid:",
                "  execution.slippage_bps: [5, 10]",
                "  profit_taking.take_profit_fraction: [0.25, 0.35]",
            ]
        ),
        encoding="utf-8",
    )
    scenarios = load_scenarios(sweep_path)
    assert len(scenarios) == 5
    assert scenarios[0].name == "baseline"
    assert scenarios[0].overrides["trend.min_trend_strength"] == 0.35
