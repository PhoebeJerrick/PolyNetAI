from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.build_batch_replay_performance_report import resolve_position_value_denominator
from scripts.run_recorded_live_paper import resolve_position_value_denominator_from_config
from polynet_ai.strategy.spec import StrategyConfig

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze_polymarket_tracker import _resolve_position_value_denominator


def test_offline_analyzer_uses_config_position_max_value(tmp_path) -> None:
    cfg = tmp_path / "strategy.yaml"
    cfg.write_text("position:\n  max_position_value: 123.0\n", encoding="utf-8")
    args = argparse.Namespace(position_value_denominator=None, config=str(cfg))
    assert _resolve_position_value_denominator(args) == 123.0


def test_batch_report_denominator_prefers_config_and_explicit(tmp_path) -> None:
    cfg = tmp_path / "strategy.yaml"
    cfg.write_text("position:\n  max_position_value: 91.0\n", encoding="utf-8")
    assert resolve_position_value_denominator(config_path=cfg) == 91.0
    assert resolve_position_value_denominator(config_path=cfg, explicit=77.0) == 77.0


def test_live_runner_denominator_uses_position_max_value() -> None:
    config = StrategyConfig(raw={"position": {"max_position_value": 66.0}})
    assert resolve_position_value_denominator_from_config(config) == 66.0
