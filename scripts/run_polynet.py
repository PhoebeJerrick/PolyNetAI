from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.launcher import build_launch_profiles, build_project_help_text  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PolyNetAI 项目级命令入口")
    parser.add_argument(
        "--dashboard-dir",
        default="artifacts/live/polymarket_btc_10cycles",
        help="dashboard 输出目录；启动档案会默认写入这里",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("help", help="查看项目级启动帮助")

    profiles_parser = subparsers.add_parser("profiles", help="列出可用启动档案")
    profiles_parser.add_argument("--verbose", action="store_true", help="显示完整底层命令")

    start_parser = subparsers.add_parser("start", help="按启动档案运行项目")
    start_parser.add_argument("profile", choices=["sim-paper", "market-paper"])

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dashboard_dir = (ROOT / args.dashboard_dir).resolve()

    if args.command == "help":
        print(
            build_project_help_text(
                root=ROOT,
                dashboard_dir=dashboard_dir,
                python_executable=sys.executable,
            ),
            end="",
        )
        return 0

    profiles = build_launch_profiles(
        root=ROOT,
        dashboard_dir=dashboard_dir,
        python_executable=sys.executable,
    )

    if args.command == "profiles":
        for profile in profiles.values():
            print(f"{profile.name}: {profile.title}")
            print(f"  {profile.description}")
            if args.verbose:
                print(f"  {subprocess.list2cmdline(profile.command)}")
        return 0

    profile = profiles[args.profile]
    print(f"即将启动: {profile.title}")
    print(subprocess.list2cmdline(profile.command))
    completed = subprocess.run(profile.command, cwd=ROOT)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
