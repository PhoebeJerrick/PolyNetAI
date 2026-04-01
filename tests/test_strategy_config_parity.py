"""策略配置归一化、Dashboard schema 覆盖与 YAML 注释保留写入。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from polynet_ai.config_io import write_yaml_mapping
from polynet_ai.reporting.strategy_console_schema import collect_strategy_schema_paths
from polynet_ai.strategy.spec import StrategyConfig, normalize_strategy_raw


def _flatten_leaf_paths(obj: object, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                paths |= _flatten_leaf_paths(v, p)
            else:
                paths.add(p)
    return paths


def test_normalize_flat_priorities_adds_by_phase() -> None:
    raw = normalize_strategy_raw(
        {
            "priorities": {
                "opening": 52,
                "trend": 80,
            },
        }
    )
    cfg = StrategyConfig(raw=raw)
    for ph in range(1, 5):
        assert cfg.priority_for("opening", ph) == 52
        assert cfg.priority_for("trend", ph) == 80


def test_priority_for_respects_per_phase_values() -> None:
    cfg = StrategyConfig(
        raw={
            "priorities": {
                "by_phase": {
                    "phase_1": {"opening": 52, "take_profit": 50},
                    "phase_4": {"opening": 35, "take_profit": 50},
                },
            },
        }
    )
    assert cfg.priority_for("opening", 1) == 52
    assert cfg.priority_for("opening", 4) == 35


def test_priority_for_uses_by_phase_from_file() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "configs" / "strategy.yaml"
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    cfg = StrategyConfig(raw=data)
    assert cfg.priority_for("opening", 1) == 52
    assert cfg.priority_for("stop_loss", 4) == 30


def test_strategy_yaml_leaves_in_dashboard_schema() -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "configs" / "strategy.yaml").open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    yaml_paths = _flatten_leaf_paths(data)
    schema_paths = collect_strategy_schema_paths()
    missing = sorted(yaml_paths - schema_paths)
    assert not missing, f"strategy.yaml 有 {len(missing)} 条路径未出现在控制台 schema: {missing[:30]}"


def test_write_yaml_mapping_preserves_comment_with_ruamel(tmp_path: Path) -> None:
    try:
        import ruamel.yaml  # noqa: F401
    except ModuleNotFoundError:
        pytest.skip("需要安装 ruamel.yaml")
    p = tmp_path / "s.yaml"
    p.write_text(
        textwrap.dedent(
            """\
            # KEEP_THIS_COMMENT
            cycle:
              cycle_seconds: 300
            batch_replay:
              processing_mode: per-cycle
            """
        ),
        encoding="utf-8",
    )
    write_yaml_mapping(p, {"cycle": {"cycle_seconds": 299}})
    text = p.read_text(encoding="utf-8")
    assert "KEEP_THIS_COMMENT" in text
    assert "299" in text
    assert "per-cycle" in text
