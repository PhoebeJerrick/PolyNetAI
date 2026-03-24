from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.reporting.dashboard import generate_dashboard_from_directory, refresh_dashboard_html_shell


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polynet AI 监控面板与日报表生成器")
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="不读 CSV，仅用空数据重写 dashboard.html 等（适合 batch_replay_outputs 等无 metrics.csv 的目录）；须指定 --output-dir",
    )
    parser.add_argument(
        "--input-dir",
        default=None,
        help="含 metrics.csv / cycles.csv / decisions.csv 的 live 输出根目录（与 --html-only 二选一）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="生成结果目录；默认与 --input-dir 相同。--html-only 时必须指定",
    )
    parser.add_argument("--title", default="Polynet AI Monitoring Dashboard")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.html_only:
        if not args.output_dir:
            raise SystemExit("错误: --html-only 必须配合 --output-dir（要写入 dashboard.html 的目录）。")
        artifacts = refresh_dashboard_html_shell(args.output_dir, title=args.title)
    else:
        if not args.input_dir:
            raise SystemExit("错误: 请指定 --input-dir，或使用 --html-only --output-dir <目录>。")
        artifacts = generate_dashboard_from_directory(
            input_dir=args.input_dir,
            output_dir=args.output_dir,
            title=args.title,
        )
    print(f"Dashboard: {artifacts.html_path}")
    print(f"Daily report: {artifacts.markdown_path}")
    print(f"Summary csv: {artifacts.summary_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
