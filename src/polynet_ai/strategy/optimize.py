from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class OptimizationStudy:
    trials: int
    seed: int
    score_weights: dict[str, float]
    parameter_space: dict[str, Any]
    export_top_n: int = 3


def load_optimization_study(path: str | Path) -> OptimizationStudy:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 PyYAML，无法读取 optimize 配置。") from exc

    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("optimize 配置必须是键值映射。")
    parameter_space = raw.get("parameters", {})
    if not isinstance(parameter_space, dict) or not parameter_space:
        raise ValueError("optimize 配置需要非空的 parameters。")
    score_weights = raw.get("score_weights", {})
    if not isinstance(score_weights, dict) or not score_weights:
        raise ValueError("optimize 配置需要非空的 score_weights。")
    return OptimizationStudy(
        trials=int(raw.get("trials", 20)),
        seed=int(raw.get("seed", 7)),
        score_weights={str(key): float(value) for key, value in score_weights.items()},
        parameter_space=parameter_space,
        export_top_n=int(raw.get("export_top_n", 3)),
    )


def sample_overrides(study: OptimizationStudy, rng: random.Random) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for path, spec in study.parameter_space.items():
        overrides[str(path)] = _sample_value(spec, rng)
    return overrides


def _sample_value(spec: Any, rng: random.Random) -> Any:
    if isinstance(spec, list):
        if not spec:
            raise ValueError("候选列表不能为空。")
        return rng.choice(spec)
    if not isinstance(spec, dict):
        return spec

    kind = str(spec.get("type", "choice"))
    if kind == "choice":
        values = spec.get("values", [])
        if not isinstance(values, list) or not values:
            raise ValueError("choice 类型需要非空 values。")
        return rng.choice(values)
    if kind == "float":
        low = float(spec["min"])
        high = float(spec["max"])
        value = rng.uniform(low, high)
        precision = int(spec.get("precision", 4))
        step = spec.get("step")
        if step is not None:
            step = float(step)
            buckets = round((value - low) / step)
            value = low + buckets * step
        return round(value, precision)
    if kind == "int":
        low = int(spec["min"])
        high = int(spec["max"])
        step = int(spec.get("step", 1))
        values = list(range(low, high + 1, step))
        return rng.choice(values)
    raise ValueError(f"不支持的参数类型: {kind}")


def compute_score(metrics: dict[str, Any], score_weights: dict[str, float]) -> float:
    score = 0.0
    for field, weight in score_weights.items():
        value = metrics.get(field, 0.0)
        try:
            score += float(value) * float(weight)
        except (TypeError, ValueError):
            continue
    return round(score, 6)


def write_best_config(path: str | Path, merged_config: dict[str, Any]) -> Path:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 PyYAML，无法写入最佳配置。") from exc

    output = Path(path)
    with output.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(merged_config, fh, allow_unicode=True, sort_keys=False)
    return output
