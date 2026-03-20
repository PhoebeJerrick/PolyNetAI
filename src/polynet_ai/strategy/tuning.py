from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SweepScenario:
    name: str
    overrides: dict[str, Any]


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 PyYAML，无法读取 sweep 配置。") from exc

    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("sweep 配置必须是键值映射。")
    return data


def load_scenarios(path: str | Path) -> list[SweepScenario]:
    raw = load_yaml_file(path)
    scenarios: list[SweepScenario] = []

    named = raw.get("scenarios", [])
    if named:
        for item in named:
            if not isinstance(item, dict):
                raise ValueError("scenarios 条目必须为字典。")
            name = str(item.get("name") or f"scenario_{len(scenarios) + 1}")
            overrides = item.get("overrides", {})
            if not isinstance(overrides, dict):
                raise ValueError(f"{name} 的 overrides 必须为字典。")
            scenarios.append(SweepScenario(name=name, overrides=overrides))

    grid = raw.get("grid", {})
    if grid:
        if not isinstance(grid, dict):
            raise ValueError("grid 必须为键值映射。")
        keys = list(grid.keys())
        value_lists: list[list[Any]] = []
        for key in keys:
            values = grid[key]
            if not isinstance(values, list) or not values:
                raise ValueError(f"{key} 的 grid 值必须是非空列表。")
            value_lists.append(values)
        for values in product(*value_lists):
            overrides = dict(zip(keys, values, strict=False))
            name = "__".join(f"{key.split('.')[-1]}={value}" for key, value in overrides.items())
            scenarios.append(SweepScenario(name=name, overrides=overrides))

    if not scenarios:
        raise ValueError("未在 sweep 配置中找到 scenarios 或 grid。")
    return scenarios
