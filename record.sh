#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-python}"
DEFAULT_OUTPUT_DIR="${RECORD_OUTPUT_DIR:-artifacts/live/record_job}"
DEFAULT_SLUG_PREFIX="${RECORD_SLUG_PREFIX:-btc-updown-5m-}"
DEFAULT_CONFIG="${RECORD_CONFIG:-configs/strategy.yaml}"
DEFAULT_OVERRIDES="${RECORD_OVERRIDES:-artifacts/optimization/optimize_btc_last_6h_100u_smallcap_v2_20260321T014000Z/trial_022/overrides.json}"
DEFAULT_STARTING_CASH="${RECORD_STARTING_CASH:-100}"
DEFAULT_ENV_FILE="${RECORD_ENV_FILE:-../APIs/ApiConfig.env}"
DEFAULT_ACCOUNT_INDEX="${RECORD_ACCOUNT_INDEX:-2}"
DEFAULT_START_BUFFER_SECONDS="${RECORD_START_BUFFER_SECONDS:-2}"

print_help() {
  cat <<'EOF'
用法:
  ./record.sh s3                后台开始抓未来 3 个完整 5m 周期
  ./record.sh s -3              与上面等价，兼容旧写法
  ./record.sh p                 查看后台状态和进度
  ./record.sh r10               前台一键抓取 10 个周期并直到生成业绩报告
  ./record.sh rb10              后台一键抓取 10 个周期并直到生成业绩报告
  ./record.sh x                 一键停止后台任务

可选参数:
  N             例如 s3 / r10 / rb300，表示周期数
  -N            例如 -3 / -10，兼容旧写法
  -o DIR        自定义输出目录
  -u PREFIX     自定义 slug 前缀，默认 btc-updown-5m-
  -c FILE       自定义 config，默认 configs/strategy.yaml
  -j FILE       自定义 overrides.json
  -k CASH       自定义 starting cash

示例:
  ./record.sh s300
  ./record.sh p
  ./record.sh r10
  ./record.sh rb300
  ./record.sh x
EOF
}

COMMAND="${1:-h}"
if [[ $# -gt 0 ]]; then
  shift
fi

CYCLES=""
OUTPUT_DIR="$DEFAULT_OUTPUT_DIR"
SLUG_PREFIX="$DEFAULT_SLUG_PREFIX"
CONFIG_PATH="$DEFAULT_CONFIG"
OVERRIDES_PATH="$DEFAULT_OVERRIDES"
STARTING_CASH="$DEFAULT_STARTING_CASH"

if [[ "$COMMAND" =~ ^(rb|runb|run-bg)([0-9]+)$ ]]; then
  COMMAND="rb"
  CYCLES="${BASH_REMATCH[2]}"
elif [[ "$COMMAND" =~ ^(s|start|r|run)([0-9]+)$ ]]; then
  COMMAND="${BASH_REMATCH[1]}"
  CYCLES="${BASH_REMATCH[2]}"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    -[0-9]*)
      CYCLES="${1#-}"
      shift
      ;;
    -o)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    -u)
      SLUG_PREFIX="$2"
      shift 2
      ;;
    -c)
      CONFIG_PATH="$2"
      shift 2
      ;;
    -j)
      OVERRIDES_PATH="$2"
      shift 2
      ;;
    -k)
      STARTING_CASH="$2"
      shift 2
      ;;
    -h|--help|h|help)
      print_help
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      echo "" >&2
      print_help
      exit 2
      ;;
  esac
done

if [[ -z "$CYCLES" ]]; then
  CYCLES="10"
fi

run_manage() {
  "$PYTHON_BIN" scripts/manage_capture_pipeline.py "$@"
}

case "$COMMAND" in
  s|start)
    run_manage start \
      --output-dir "$OUTPUT_DIR" \
      --slug-prefix "$SLUG_PREFIX" \
      --max-cycles "$CYCLES" \
      --start-buffer-seconds "$DEFAULT_START_BUFFER_SECONDS" \
      --env-file "$DEFAULT_ENV_FILE" \
      --account-index "$DEFAULT_ACCOUNT_INDEX"
    ;;
  p|ps|status)
    run_manage status \
      --output-dir "$OUTPUT_DIR"
    ;;
  r|run)
    run_manage run-full \
      --output-dir "$OUTPUT_DIR" \
      --slug-prefix "$SLUG_PREFIX" \
      --max-cycles "$CYCLES" \
      --start-buffer-seconds "$DEFAULT_START_BUFFER_SECONDS" \
      --env-file "$DEFAULT_ENV_FILE" \
      --account-index "$DEFAULT_ACCOUNT_INDEX" \
      --config "$CONFIG_PATH" \
      --overrides "$OVERRIDES_PATH" \
      --starting-cash "$STARTING_CASH"
    ;;
  rb|runb|run-bg)
    run_manage run-full \
      --daemonize \
      --output-dir "$OUTPUT_DIR" \
      --slug-prefix "$SLUG_PREFIX" \
      --max-cycles "$CYCLES" \
      --start-buffer-seconds "$DEFAULT_START_BUFFER_SECONDS" \
      --env-file "$DEFAULT_ENV_FILE" \
      --account-index "$DEFAULT_ACCOUNT_INDEX" \
      --config "$CONFIG_PATH" \
      --overrides "$OVERRIDES_PATH" \
      --starting-cash "$STARTING_CASH"
    ;;
  x|stop|kill)
    run_manage stop \
      --output-dir "$OUTPUT_DIR"
    ;;
  h|help|"")
    print_help
    ;;
  *)
    echo "未知命令: $COMMAND" >&2
    echo "" >&2
    print_help
    exit 2
    ;;
esac
