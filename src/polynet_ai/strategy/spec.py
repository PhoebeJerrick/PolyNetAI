from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StrategyConfig:
    raw: dict[str, Any]

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def priorities(self) -> dict[str, int]:
        return dict(self.get("priorities", {}))

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self.raw)

    def with_overrides(self, overrides: dict[str, Any]) -> "StrategyConfig":
        updated = self.to_dict()
        for path, value in overrides.items():
            parts = path.split(".")
            node = updated
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[parts[-1]] = value
        return StrategyConfig(raw=updated)


def post_window_start_delay_seconds_from_config(config: StrategyConfig) -> float:
    """读取 ``cycle.post_window_start_delay_seconds``（缺省与 ``cycle_window_timing.DEFAULT`` 一致）。"""
    from polynet_ai.adapters.cycle_window_timing import DEFAULT_POST_WINDOW_START_DELAY_SECONDS

    raw = config.get("cycle.post_window_start_delay_seconds", DEFAULT_POST_WINDOW_START_DELAY_SECONDS)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = float(DEFAULT_POST_WINDOW_START_DELAY_SECONDS)
    return max(0.0, v)


def resolve_post_window_start_delay_seconds(
    *,
    config: StrategyConfig,
    cli_seconds: float | None,
) -> float:
    """若 ``cli_seconds`` 非 None 则优先命令行，否则用配置。"""
    if cli_seconds is not None:
        return max(0.0, float(cli_seconds))
    return post_window_start_delay_seconds_from_config(config)


def load_strategy_config(path: str | Path) -> StrategyConfig:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 PyYAML，无法读取 strategy.yaml 配置。") from exc

    cfg_path = Path(path)
    with cfg_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("strategy config 必须是键值映射。")
    return StrategyConfig(raw=data)
