#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-python}"
DEFAULT_OUTPUT_DIR="${RECORD_OUTPUT_DIR:-artifacts/live/record_job}"
DEFAULT_SLUG_PREFIX="${RECORD_SLUG_PREFIX:-btc-updown-5m-}"
DEFAULT_CONFIG="${RECORD_CONFIG:-configs/strategy.yaml}"
DEFAULT_OVERRIDES="${RECORD_OVERRIDES:-artifacts/trial_022/overrides.json}"
DEFAULT_STARTING_CASH="${RECORD_STARTING_CASH:-100}"
DEFAULT_ENV_FILE="${RECORD_ENV_FILE:-../APIs/ApiConfig.env}"
DEFAULT_ACCOUNT_INDEX="${RECORD_ACCOUNT_INDEX:-2}"
DEFAULT_START_BUFFER_SECONDS="${RECORD_START_BUFFER_SECONDS:-2}"
OPEN_EDGE_ON_DASHBOARD="${RECORD_OPEN_DASHBOARD_EDGE:-1}"

print_help() {
  printf "%s\n" \
    "用法（子命令）:" \
    "  ./record.sh d                 启动 dashboard 控制台（前台）" \
    "  ./record.sh ds<N>            dashboard + 模拟下单回放 N 个 5m 周期" \
    "  ./record.sh chart             打开 artifacts/charts/tracker_position_compare.html" \
    "  ./record.sh excel-v5          生成 data/processed/..._with_accumulated_shares_v5.xlsx" \
    "" \
    "  ./record.sh s<N>              后台开始抓未来 N 个完整 5m 周期" \
    "  ./record.sh r<N>              前台抓取并回放 N 个周期（直到生成业绩报告）" \
    "  ./record.sh rb<N>             后台抓取并回放 N 个周期（直到生成业绩报告）" \
    "  ./record.sh p                查看后台状态和进度" \
    "  ./record.sh x                一键停止后台任务" \
    "" \
    "可选参数（通用）:" \
    "  N              例如 ds10 / r10 / rb300" \
    "  -N             例如 s -3 / r -10（兼容旧写法）" \
    "  -o DIR         自定义输出目录（用于 dashboard 与抓取管道）" \
    "  -u PREFIX      自定义 slug 前缀，默认 btc-updown-5m-" \
    "  -c FILE        自定义 config，默认 configs/strategy.yaml" \
    "  -k CASH        自定义 starting cash（用于模拟下单）" \
    "" \
    "环境变量（可选）:" \
    "  RECORD_OPEN_DASHBOARD_EDGE=0   关闭 record.sh d 后自动用 Edge 打开网页"
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
STARTING_CASH_SET="false"

if [[ "$COMMAND" =~ ^(rb|runb|run-bg)([0-9]+)$ ]]; then
  COMMAND="rb"
  CYCLES="${BASH_REMATCH[2]}"
elif [[ "$COMMAND" =~ ^(s|start|r|run)([0-9]+)$ ]]; then
  COMMAND="${BASH_REMATCH[1]}"
  CYCLES="${BASH_REMATCH[2]}"
elif [[ "$COMMAND" =~ ^ds([0-9]+)$ ]]; then
  COMMAND="ds"
  CYCLES="${BASH_REMATCH[1]}"
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
      STARTING_CASH_SET="true"
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
  d)
    DASHBOARD_DIR="$OUTPUT_DIR/batch_replay_outputs"
    mkdir -p "$DASHBOARD_DIR"
    # 每次都刷新 dashboard.html，避免你本地旧页面继续使用旧 UI（例如 number 输入控件）。
    "$PYTHON_BIN" scripts/run_dashboard_report.py --html-only --output-dir "$DASHBOARD_DIR" --title "Polynet AI Monitoring Dashboard"

    DASHBOARD_LOG="$DASHBOARD_DIR/dashboard_console.log"
    DASHBOARD_HOST="${RECORD_DASHBOARD_HOST:-127.0.0.1}"
    DASHBOARD_PORT="${RECORD_DASHBOARD_PORT:-8765}"
    DASHBOARD_URL="http://$DASHBOARD_HOST:$DASHBOARD_PORT/dashboard.html"

    # 用 nohup/disown 让控制台脱离当前 bash 生命周期，避免 bash/pty 结束后导致控制台被杀。
    # 如果端口已在监听，避免重复启动导致状态混乱。
    DASHBOARD_ALIVE="0"
    if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('$DASHBOARD_URL', timeout=1).read(1)" >/dev/null 2>&1; then
      DASHBOARD_ALIVE="1"
    fi

    if [[ "$DASHBOARD_ALIVE" != "1" ]]; then
      nohup "$PYTHON_BIN" scripts/run_dashboard_console.py --dashboard-dir "$DASHBOARD_DIR" --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" > "$DASHBOARD_LOG" 2>&1 &
      DASHBOARD_PID=$!
      disown "$DASHBOARD_PID" 2>/dev/null || true
      echo "Dashboard 控制台已启动: $DASHBOARD_URL"
      echo "运行日志: $DASHBOARD_LOG"
    else
      echo "Dashboard 控制台已在运行: $DASHBOARD_URL"
      echo "运行日志: $DASHBOARD_LOG"
    fi

    if [[ "${OPEN_EDGE_ON_DASHBOARD}" == "1" ]]; then
      # 等待服务就绪，避免浏览器打开后发现页面还没开始监听
      for _ in {1..30}; do
        if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('${DASHBOARD_URL}', timeout=0.2).read(1)" >/dev/null 2>&1; then
          break
        fi
        sleep 0.2
      done

      # 通过 PowerShell 打开 Edge（优先 msedge），失败不影响控制台运行。
      # 使用最小化启动，尽量避免抢焦点/“跳转出当前终端”。
      powershell -NoProfile -Command "\$url='${DASHBOARD_URL}'; \$edge=Get-Command msedge -ErrorAction SilentlyContinue; if(\$edge){Start-Process -FilePath 'msedge' -ArgumentList \$url -WindowStyle Minimized}else{Start-Process -FilePath \$url}" || true
    fi

    # 不 wait：让 `bash -lc \"./record.sh d\"` 结束后你的命令行可继续使用。
    ;;
  ds)
    DASHBOARD_DIR="$OUTPUT_DIR/batch_replay_outputs"
    mkdir -p "$DASHBOARD_DIR"
    "$PYTHON_BIN" scripts/run_dashboard_report.py --html-only --output-dir "$DASHBOARD_DIR" --title "Polynet AI Monitoring Dashboard"

    # 后台启动本地控制台；如果端口已被占用则跳过启动（避免重复控制台）。
    DASHBOARD_LOG="$DASHBOARD_DIR/dashboard_console.log"
    DASHBOARD_ALIVE="0"
    DASHBOARD_URL="http://127.0.0.1:8765/dashboard.html"
    if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('$DASHBOARD_URL', timeout=1).read(1)" >/dev/null 2>&1; then
      DASHBOARD_ALIVE="1"
    fi
    if [[ "$DASHBOARD_ALIVE" != "1" ]]; then
      "$PYTHON_BIN" scripts/run_dashboard_console.py --dashboard-dir "$DASHBOARD_DIR" > "$DASHBOARD_LOG" 2>&1 &
    fi

    DS_STARTING_CASH="$STARTING_CASH"
    # 如果用户没显式指定 -k，则让模拟下单默认更贴近 launcher.py 里的配置（通常 1000）。
    if [[ "$STARTING_CASH_SET" != "true" ]]; then
      DS_STARTING_CASH="1000"
    fi

    "$PYTHON_BIN" scripts/run_recorded_live_paper.py \
      --input-dir "$OUTPUT_DIR" \
      --cycle-glob "$SLUG_PREFIX"* \
      --max-cycles "$CYCLES" \
      --config "$CONFIG_PATH" \
      --output-dir "$DASHBOARD_DIR" \
      --pace-factor 20 \
      --status-every 100 \
      --dashboard-refresh-seconds 1 \
      --starting-cash "$DS_STARTING_CASH"
    ;;
  chart)
    DASHBOARD_CHART_PATH="artifacts/charts/tracker_position_compare.html"
    if [[ ! -f "$DASHBOARD_CHART_PATH" ]]; then
      "$PYTHON_BIN" scripts/build_tracker_position_compare_chart.py --input data/processed/polymarket_tracker_collection_with_accumulated_shares_v5.xlsx --output "$DASHBOARD_CHART_PATH"
    fi
    powershell -NoProfile -Command "Start-Process -FilePath \"$DASHBOARD_CHART_PATH\""
    ;;
  excel-v5)
    OUT_XLSX="data/processed/polymarket_tracker_collection_with_accumulated_shares_v5.xlsx"
    mkdir -p "data/processed"
    "$PYTHON_BIN" analyze_polymarket_tracker.py \
      --input data/raw/polymarket_tracker_collection.xlsx \
      --output "$OUT_XLSX"
    "$PYTHON_BIN" scripts/_update_floating_pnl_columns_in_xlsx.py --processed-v5 "$OUT_XLSX"
    ;;
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
