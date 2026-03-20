from __future__ import annotations

import random
from pathlib import Path

from polynet_ai.strategy.optimize import compute_score, load_optimization_study, sample_overrides


def test_compute_score_combines_reward_and_penalty_terms() -> None:
    metrics = {
        "total_net_profit": 100.0,
        "max_drawdown": 30.0,
        "win_rate": 0.6,
    }
    score = compute_score(
        metrics,
        {
            "total_net_profit": 1.0,
            "max_drawdown": -0.5,
            "win_rate": 100.0,
        },
    )
    assert score == 145.0


def test_load_optimization_study_and_sampling(tmp_path: Path) -> None:
    path = tmp_path / "optimize.yaml"
    path.write_text(
        "\n".join(
            [
                "trials: 5",
                "seed: 9",
                "export_top_n: 2",
                "score_weights:",
                "  total_net_profit: 1.0",
                "  max_drawdown: -0.5",
                "parameters:",
                "  trend.min_trend_strength:",
                "    type: float",
                "    min: 0.2",
                "    max: 0.4",
                "    step: 0.1",
                "    precision: 1",
                "  execution.slippage_bps:",
                "    type: choice",
                "    values: [5, 10]",
            ]
        ),
        encoding="utf-8",
    )
    study = load_optimization_study(path)
    rng = random.Random(study.seed)
    overrides = sample_overrides(study, rng)
    assert study.trials == 5
    assert study.export_top_n == 2
    assert overrides["trend.min_trend_strength"] in {0.2, 0.3, 0.4}
    assert overrides["execution.slippage_bps"] in {5, 10}
