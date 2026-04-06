from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 与 configs/strategy.yaml 默认行保持一致；仅当配置缺省时使用
DEFAULT_RULE_PRIORITIES: dict[str, int] = {
    "risk": 10,
    "last_minute": 20,
    "stop_loss": 30,
    "hedge": 40,
    "take_profit": 50,
    "opening": 52,
    "grid": 60,
    "mean_reversion": 70,
    "trend": 80,
}


def normalize_strategy_raw(data: dict[str, Any]) -> dict[str, Any]:
    """
    归一化内存中的策略配置：
    - 若仅有扁平 ``priorities.<rule>`` 数值，则补充 ``priorities.by_phase.phase_{1..4}``（各阶段同值），
      便于分阶段覆盖优先级且保持与旧文件兼容。
    """
    raw = deepcopy(data)
    pr = raw.get("priorities")
    if not isinstance(pr, dict):
        return raw
    bp = pr.get("by_phase")
    if isinstance(bp, dict) and bp:
        return raw
    flat = {
        k: int(v)
        for k, v in pr.items()
        if k != "by_phase" and isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    if not flat:
        return raw
    new_pr = dict(pr)
    new_pr["by_phase"] = {f"phase_{i}": dict(flat) for i in range(1, 5)}
    raw["priorities"] = new_pr
    return raw


@dataclass(slots=True)
class StrategyConfig:
    raw: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", normalize_strategy_raw(deepcopy(self.raw)))

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def priority_for(self, rule: str, phase: int) -> int:
        """当前阶段下某规则的基础优先级（数值越小越优先）；再经 router 内 dynamic 调整。"""
        default = int(DEFAULT_RULE_PRIORITIES.get(rule, 99))
        pr = self.get("priorities")
        if not isinstance(pr, dict):
            return default
        ph = max(1, min(4, int(phase)))
        bp = pr.get("by_phase")
        if isinstance(bp, dict):
            bucket = bp.get(f"phase_{ph}")
            if isinstance(bucket, dict) and rule in bucket:
                try:
                    return int(bucket[rule])
                except (TypeError, ValueError):
                    pass
        v = pr.get(rule)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
        return default

    @property
    def priorities(self) -> dict[str, int]:
        """扁平优先级视图：优先返回文件中仍是标量的键；否则取 phase_1 桶。"""
        pr = self.get("priorities", {})
        if not isinstance(pr, dict):
            return {}
        out: dict[str, int] = {}
        for k, v in pr.items():
            if k == "by_phase":
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = int(v)
        if out:
            return out
        bp = pr.get("by_phase")
        if isinstance(bp, dict):
            b1 = bp.get("phase_1")
            if isinstance(b1, dict):
                for k, v in b1.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        out[k] = int(v)
        return out

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
