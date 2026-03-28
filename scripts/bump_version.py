#!/usr/bin/env python3
"""
bump_version.py — 自动递增补丁版本号。

在 pre-commit 钩子中调用，负责：
1. 读取 VERSION 文件，补丁号 +1
2. 将新版本写回 VERSION
3. 同步更新 pyproject.toml 和 src/polynet_ai/__init__.py
4. 将以上三个文件 git add 到当前暂存区
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bump_patch(version: str) -> str:
    parts = version.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"版本格式不合法（期望 X.Y.Z）: {version!r}")
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def update_file_version(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8-sig")
    new_text, n = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if n == 0:
        print(f"[bump_version] 警告：在 {path} 中未找到版本占位符，跳过。", file=sys.stderr)
        return
    path.write_text(new_text, encoding="utf-8")


def main() -> None:
    version_file = ROOT / "VERSION"
    old_version = version_file.read_text(encoding="utf-8-sig").strip()
    new_version = bump_patch(old_version)

    # 1. 更新 VERSION
    version_file.write_text(new_version + "\n", encoding="utf-8")

    # 2. 更新 pyproject.toml
    toml_path = ROOT / "pyproject.toml"
    update_file_version(
        toml_path,
        r'^(version\s*=\s*")[^"]*(")',
        rf'\g<1>{new_version}\g<2>',
    )

    # 3. 更新 src/polynet_ai/__init__.py
    init_path = ROOT / "src" / "polynet_ai" / "__init__.py"
    update_file_version(
        init_path,
        r'^(__version__\s*=\s*")[^"]*(")',
        rf'\g<1>{new_version}\g<2>',
    )

    # 4. 暂存修改
    files = [str(version_file), str(toml_path), str(init_path)]
    subprocess.run(["git", "add", *files], cwd=ROOT, check=True)

    print(f"[bump_version] {old_version} → {new_version}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[bump_version] 错误：{exc}", file=sys.stderr)
        sys.exit(1)
