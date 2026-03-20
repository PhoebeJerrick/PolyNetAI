from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.reporting.dashboard import generate_dashboard_from_directory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polynet AI 监控面板与日报表生成器")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--title", default="Polynet AI Monitoring Dashboard")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
