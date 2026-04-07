from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polynet_ai.reporting.universal_stats import (  # noqa: E402
    load_stats_config,
    render_console_report,
    run_universal_stats,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket BTC 5m 万能统计机器人",
        epilog=(
            "示例:\n"
            "  python scripts/run_universal_stats.py --config configs/batch.conf\n"
            "  python scripts/run_universal_stats.py --config configs/batch.conf --help"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="configs/batch.conf",
        help="统计配置文件，支持 key=value 或 JSON；默认 configs/batch.conf",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_stats_config(args.config)
    result = run_universal_stats(config)
    print(render_console_report(result, config), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())