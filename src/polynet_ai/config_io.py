from __future__ import annotations

from pathlib import Path
from typing import Any


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 PyYAML，无法读取 YAML 配置。") from exc

    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是键值映射: {file_path}")
    return data


def _deep_merge_dict_inplace(base: Any, updates: dict[str, Any]) -> None:
    if not isinstance(base, dict):
        return
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge_dict_inplace(base[key], value)
        else:
            base[key] = value


def _write_yaml_merge_preserve(path: Path, data: dict[str, Any]) -> None:
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    text = path.read_text(encoding="utf-8")
    root = y.load(text)
    if root is None:
        root = {}
    if not isinstance(root, dict):
        root = {}
    _deep_merge_dict_inplace(root, data)
    with path.open("w", encoding="utf-8") as fh:
        y.dump(root, fh)


def write_yaml_mapping(path: str | Path, data: dict[str, Any]) -> Path:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 PyYAML，无法写入 YAML 配置。") from exc

    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    if file_path.exists() and file_path.suffix.lower() in (".yaml", ".yml"):
        try:
            _write_yaml_merge_preserve(file_path, data)
            return file_path
        except ModuleNotFoundError:
            pass
        except Exception:
            # ruamel 解析失败时退回整文件覆盖（可能丢失注释）
            pass

    with file_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    return file_path
