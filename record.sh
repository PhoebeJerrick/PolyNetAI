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
DEFAULT_PER_CYCLE_CASH="${RECORD_PER_CYCLE_CASH:-}"
DEFAULT_ENV_FILE="${RECORD_ENV_FILE:-../APIs/ApiConfig.env}"
DEFAULT_ACCOUNT_INDEX="${RECORD_ACCOUNT_INDEX:-2}"
DEFAULT_START_BUFFER_SECONDS="${RECORD_START_BUFFER_SECONDS:-2}"
OPEN_EDGE_ON_DASHBOARD="${RECORD_OPEN_DASHBOARD_EDGE:-1}"

print_help() {
  printf "%s\n" \
    "用法（子命令）:" \
    "  ./record.sh d                 启动 dashboard 控制台（前台）" \
    "  ./record.sh ds<N>            dashboard + 模拟下单回放 N 个 5m 周期" \
    "  ./record.sh dm<N>            dashboard + 实盘行情验证 N 个 5m 周期（paper）" \
    "  ./record.sh dmb<N>           dashboard + 实盘行情验证 N 个 5m 周期（paper，后台）" \
    "  ./record.sh pm[LINES]        查看后台实盘验证状态与最近日志（默认 20 行）" \
    "  ./record.sh chart             打开 artifacts/charts/tracker_position_compare.html" \
    "  ./record.sh excel-v5          生成 data/processed/..._with_accumulated_shares_v5.xlsx" \
    "" \
    "  ./record.sh s<N>              后台开始抓未来 N 个完整 5m 周期" \
    "  ./record.sh r<N>              前台抓取并回放 N 个周期（直到生成业绩报告）" \
    "  ./record.sh rb<N>             后台抓取并回放 N 个周期（直到生成业绩报告）" \
    "  ./record.sh p                查看后台状态和进度" \
    "  ./record.sh x                一键停止后台任务（含后台实盘验证）" \
    "" \
    "可选参数（通用）:" \
    "  N              例如 ds10 / r10 / rb300" \
    "  -N             例如 s -3 / r -10（兼容旧写法）" \
    "  -o DIR         自定义输出目录（用于 dashboard 与抓取管道）" \
    "  -u PREFIX      自定义 slug 前缀，默认 btc-updown-5m-" \
    "  -c FILE        自定义 config，默认 configs/strategy.yaml" \
    "  -k CASH        自定义 starting cash（用于模拟下单）" \
    "  -K CASH        自定义每周期固定投注资金（仅 dm/dmb；透传 --per-cycle-cash）" \
    "" \
    "环境变量（可选）:" \
    "  RECORD_OPEN_DASHBOARD_EDGE=0   关闭 record.sh d 后自动用 Edge 打开网页" \
    "  RECORD_DASHBOARD_HOST=0.0.0.0  dashboard 监听地址（云服务器可设 0.0.0.0）" \
    "  RECORD_DASHBOARD_PORT=8765     dashboard 监听端口" \
    "  RECORD_PER_CYCLE_CASH=100      dm/dmb 默认每周期固定投注资金" \
    "" \
    "Linux 云服务器推荐：" \
    "  RECORD_DASHBOARD_HOST=0.0.0.0 ./record.sh dmb10" \
    "  浏览器访问: http://43.167.171.148:8765/dashboard.html"
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
PER_CYCLE_CASH="$DEFAULT_PER_CYCLE_CASH"
PER_CYCLE_CASH_SET="false"
if [[ -n "$DEFAULT_PER_CYCLE_CASH" ]]; then
  PER_CYCLE_CASH_SET="true"
fi
PM_TAIL_LINES="20"

if [[ "$COMMAND" =~ ^(rb|runb|run-bg)([0-9]+)$ ]]; then
  COMMAND="rb"
  CYCLES="${BASH_REMATCH[2]}"
elif [[ "$COMMAND" =~ ^(s|start|r|run)([0-9]+)$ ]]; then
  COMMAND="${BASH_REMATCH[1]}"
  CYCLES="${BASH_REMATCH[2]}"
elif [[ "$COMMAND" =~ ^ds([0-9]+)$ ]]; then
  COMMAND="ds"
  CYCLES="${BASH_REMATCH[1]}"
elif [[ "$COMMAND" =~ ^dmb([0-9]+)$ ]]; then
  COMMAND="dmb"
  CYCLES="${BASH_REMATCH[1]}"
elif [[ "$COMMAND" =~ ^(dm|dashboard-market)([0-9]+)$ ]]; then
  COMMAND="dm"
  CYCLES="${BASH_REMATCH[2]}"
elif [[ "$COMMAND" =~ ^pm([0-9]+)$ ]]; then
  COMMAND="pm"
  PM_TAIL_LINES="${BASH_REMATCH[1]}"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    -[0-9]*)
      if [[ "$COMMAND" == "pm" ]]; then
        PM_TAIL_LINES="${1#-}"
      else
        CYCLES="${1#-}"
      fi
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
    -K|--per-cycle-cash)
      PER_CYCLE_CASH="$2"
      PER_CYCLE_CASH_SET="true"
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

stop_pid_file() {
  local pid_file="$1"
  local label="$2"
  if [[ ! -f "$pid_file" ]]; then
    return 0
  fi
  local pid
  pid=$(cat "$pid_file" 2>/dev/null | tr -d '[:space:]')
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "$label 已停止 (pid=$pid)"
  fi
  rm -f "$pid_file"
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
    DASHBOARD_PID_FILE="$OUTPUT_DIR/dashboard_console.pid"

    # 检查旧的 dashboard 控制台进程
    OLD_DASHBOARD_PID=""
    if [[ -f "$DASHBOARD_PID_FILE" ]]; then
      OLD_DASHBOARD_PID=$(cat "$DASHBOARD_PID_FILE" 2>/dev/null | tr -d '[:space:]')
    fi
    OLD_DASHBOARD_ALIVE="0"
    if [[ -n "$OLD_DASHBOARD_PID" ]] && kill -0 "$OLD_DASHBOARD_PID" 2>/dev/null; then
      OLD_DASHBOARD_ALIVE="1"
    fi

    # 如果端口在监听 或 PID 文件记录的旧进程仍在运行，则提示并杀掉旧进程后重启
    DASHBOARD_ALIVE="0"
    if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('$DASHBOARD_URL', timeout=1).read(1)" >/dev/null 2>&1; then
      DASHBOARD_ALIVE="1"
    fi

    if [[ "$DASHBOARD_ALIVE" == "1" ]] || [[ "$OLD_DASHBOARD_ALIVE" == "1" ]]; then
      echo "检测到旧的 Dashboard 控制台仍在运行 (pid=${OLD_DASHBOARD_PID:-未知})，正在停止并重启..."
      # 优先用 PID 文件记录的进程号杀，兜底用端口查找
      if [[ "$OLD_DASHBOARD_ALIVE" == "1" ]]; then
        kill "$OLD_DASHBOARD_PID" 2>/dev/null || true
        sleep 1
      fi
      # 如果端口仍然在监听（PID 文件可能已失效），通过端口查找并杀掉
      if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('$DASHBOARD_URL', timeout=0.5).read(1)" >/dev/null 2>&1; then
        "$PYTHON_BIN" -c "
import socket, os, signal, sys
try:
    import subprocess
    result = subprocess.run(['netstat', '-ano'], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if ':$DASHBOARD_PORT' in line and 'LISTENING' in line:
            pid = line.strip().split()[-1]
            if pid.isdigit():
                os.kill(int(pid), signal.SIGTERM)
except Exception:
    pass
" 2>/dev/null || true
        sleep 1
      fi
      rm -f "$DASHBOARD_PID_FILE"
    fi

    # 启动新的 dashboard 控制台（由 Python 进程自己写入操作系统原生 PID）
    nohup "$PYTHON_BIN" scripts/run_dashboard_console.py --dashboard-dir "$DASHBOARD_DIR" --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" --pid-file "$DASHBOARD_PID_FILE" > "$DASHBOARD_LOG" 2>&1 &
    disown 2>/dev/null || true
    # 等待 PID 文件写入
    for _ in {1..20}; do
      if [[ -f "$DASHBOARD_PID_FILE" ]]; then
        break
      fi
      sleep 0.2
    done
    DASHBOARD_PID=""
    if [[ -f "$DASHBOARD_PID_FILE" ]]; then
      DASHBOARD_PID=$(cat "$DASHBOARD_PID_FILE" 2>/dev/null | tr -d '[:space:]')
    fi
    echo "Dashboard 控制台已启动: $DASHBOARD_URL (pid=${DASHBOARD_PID:-未知})"
    echo "运行日志: $DASHBOARD_LOG"
    echo "PID 文件: $DASHBOARD_PID_FILE"

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
    DASHBOARD_PID_FILE="$OUTPUT_DIR/dashboard_console.pid"
    if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('$DASHBOARD_URL', timeout=1).read(1)" >/dev/null 2>&1; then
      DASHBOARD_ALIVE="1"
    fi
    if [[ "$DASHBOARD_ALIVE" != "1" ]]; then
      "$PYTHON_BIN" scripts/run_dashboard_console.py --dashboard-dir "$DASHBOARD_DIR" --pid-file "$DASHBOARD_PID_FILE" > "$DASHBOARD_LOG" 2>&1 &
    fi

    DS_STARTING_CASH="$STARTING_CASH"
    # 如果用户没显式指定 -k，则让模拟下单默认更贴近 launcher.py 里的配置（通常 1000）。
    if [[ "$STARTING_CASH_SET" != "true" ]]; then
      DS_STARTING_CASH="100"
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
  dm)
    DASHBOARD_DIR="$OUTPUT_DIR/polymarket_live_outputs"
    mkdir -p "$DASHBOARD_DIR"
    "$PYTHON_BIN" scripts/run_dashboard_report.py --html-only --output-dir "$DASHBOARD_DIR" --title "Polynet AI Live Monitoring Dashboard"

    DASHBOARD_LOG="$DASHBOARD_DIR/dashboard_console.log"
    DASHBOARD_HOST="${RECORD_DASHBOARD_HOST:-127.0.0.1}"
    DASHBOARD_PORT="${RECORD_DASHBOARD_PORT:-8765}"
    DASHBOARD_URL="http://$DASHBOARD_HOST:$DASHBOARD_PORT/dashboard.html"
    DASHBOARD_PID_FILE="$OUTPUT_DIR/dashboard_console.pid"
    DASHBOARD_ALIVE="0"
    if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('$DASHBOARD_URL', timeout=1).read(1)" >/dev/null 2>&1; then
      DASHBOARD_ALIVE="1"
    fi
    if [[ "$DASHBOARD_ALIVE" != "1" ]]; then
      nohup "$PYTHON_BIN" scripts/run_dashboard_console.py --dashboard-dir "$DASHBOARD_DIR" --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" --pid-file "$DASHBOARD_PID_FILE" > "$DASHBOARD_LOG" 2>&1 &
      disown 2>/dev/null || true
    fi

    DM_STARTING_CASH="$STARTING_CASH"
    if [[ "$STARTING_CASH_SET" != "true" ]]; then
      DM_STARTING_CASH="100"
    fi
    DM_PER_CYCLE_CASH="$PER_CYCLE_CASH"

    DM_ARGS=(
      --robot-mode
      --slug-prefix "$SLUG_PREFIX"
      --max-cycles "$CYCLES"
      --config "$CONFIG_PATH"
      --output-dir "$DASHBOARD_DIR"
      --record-events-dir "$OUTPUT_DIR/record_job_market"
      --dashboard-refresh-seconds 1
      --starting-cash "$DM_STARTING_CASH"
      --env-file "$DEFAULT_ENV_FILE"
      --account-index "$DEFAULT_ACCOUNT_INDEX"
    )
    if [[ "$PER_CYCLE_CASH_SET" == "true" && -n "$DM_PER_CYCLE_CASH" ]]; then
      DM_ARGS+=(--per-cycle-cash "$DM_PER_CYCLE_CASH")
    fi

    "$PYTHON_BIN" scripts/run_polymarket_live_paper.py "${DM_ARGS[@]}"
    ;;
  dmb)
    DASHBOARD_DIR="$OUTPUT_DIR/polymarket_live_outputs"
    mkdir -p "$DASHBOARD_DIR"
    "$PYTHON_BIN" scripts/run_dashboard_report.py --html-only --output-dir "$DASHBOARD_DIR" --title "Polynet AI Live Monitoring Dashboard"

    DASHBOARD_LOG="$DASHBOARD_DIR/dashboard_console.log"
    DASHBOARD_HOST="${RECORD_DASHBOARD_HOST:-127.0.0.1}"
    DASHBOARD_PORT="${RECORD_DASHBOARD_PORT:-8765}"
    DASHBOARD_URL="http://$DASHBOARD_HOST:$DASHBOARD_PORT/dashboard.html"
    DASHBOARD_PID_FILE="$OUTPUT_DIR/dashboard_console.pid"
    MARKET_PID_FILE="$OUTPUT_DIR/market_paper.pid"
    MARKET_LOG="$DASHBOARD_DIR/market_paper.log"
    DASHBOARD_ALIVE="0"
    if "$PYTHON_BIN" -c "import urllib.request; urllib.request.urlopen('$DASHBOARD_URL', timeout=1).read(1)" >/dev/null 2>&1; then
      DASHBOARD_ALIVE="1"
    fi
    if [[ "$DASHBOARD_ALIVE" != "1" ]]; then
      nohup "$PYTHON_BIN" scripts/run_dashboard_console.py --dashboard-dir "$DASHBOARD_DIR" --host "$DASHBOARD_HOST" --port "$DASHBOARD_PORT" --pid-file "$DASHBOARD_PID_FILE" > "$DASHBOARD_LOG" 2>&1 &
      disown 2>/dev/null || true
    fi

    DM_STARTING_CASH="$STARTING_CASH"
    if [[ "$STARTING_CASH_SET" != "true" ]]; then
      DM_STARTING_CASH="100"
    fi
    DM_PER_CYCLE_CASH="$PER_CYCLE_CASH"

    if [[ -f "$MARKET_PID_FILE" ]]; then
      OLD_MARKET_PID=$(cat "$MARKET_PID_FILE" 2>/dev/null | tr -d '[:space:]')
      if [[ -n "$OLD_MARKET_PID" ]] && kill -0 "$OLD_MARKET_PID" 2>/dev/null; then
        echo "检测到已有后台实盘验证任务在运行 (pid=$OLD_MARKET_PID)，请先执行 ./record.sh x 或手动停止。"
        exit 1
      fi
      rm -f "$MARKET_PID_FILE"
    fi

    nohup bash -lc 'echo $$ > "$0"; cmd=("$1" scripts/run_polymarket_live_paper.py --robot-mode --slug-prefix "$2" --max-cycles "$3" --config "$4" --output-dir "$5" --record-events-dir "$6" --dashboard-refresh-seconds 1 --starting-cash "$7" --env-file "$8" --account-index "$9"); if [[ -n "${10}" ]]; then cmd+=(--per-cycle-cash "${10}"); fi; exec "${cmd[@]}"' \
      "$MARKET_PID_FILE" \
      "$PYTHON_BIN" \
      "$SLUG_PREFIX" \
      "$CYCLES" \
      "$CONFIG_PATH" \
      "$DASHBOARD_DIR" \
      "$OUTPUT_DIR/record_job_market" \
      "$DM_STARTING_CASH" \
      "$DEFAULT_ENV_FILE" \
      "$DEFAULT_ACCOUNT_INDEX" \
      "$DM_PER_CYCLE_CASH" \
      > "$MARKET_LOG" 2>&1 &
    disown 2>/dev/null || true

    MARKET_PID=""
    for _ in {1..20}; do
      if [[ -f "$MARKET_PID_FILE" ]]; then
        MARKET_PID=$(cat "$MARKET_PID_FILE" 2>/dev/null | tr -d '[:space:]')
        if [[ -n "$MARKET_PID" ]]; then
          break
        fi
      fi
      sleep 0.2
    done

    echo "Dashboard 控制台已启动: $DASHBOARD_URL"
    echo "后台实盘验证已启动 (pid=${MARKET_PID:-未知})"
    echo "Dashboard 日志: $DASHBOARD_LOG"
    echo "实盘验证日志: $MARKET_LOG"
    echo "Dashboard PID 文件: $DASHBOARD_PID_FILE"
    echo "实盘验证 PID 文件: $MARKET_PID_FILE"
    echo "浏览器访问: http://43.167.171.148:${DASHBOARD_PORT}/dashboard.html"
    ;;
  pm|paper-market|paper-market-status)
    DASHBOARD_DIR="$OUTPUT_DIR/polymarket_live_outputs"
    DASHBOARD_PID_FILE="$OUTPUT_DIR/dashboard_console.pid"
    MARKET_PID_FILE="$OUTPUT_DIR/market_paper.pid"
    DASHBOARD_LOG="$DASHBOARD_DIR/dashboard_console.log"
    MARKET_LOG="$DASHBOARD_DIR/market_paper.log"

    echo "## 后台实盘验证状态"
    echo "- 输出目录: $OUTPUT_DIR"
    echo "- Dashboard 目录: $DASHBOARD_DIR"

    if [[ -f "$DASHBOARD_PID_FILE" ]]; then
      DASHBOARD_PID=$(cat "$DASHBOARD_PID_FILE" 2>/dev/null | tr -d '[:space:]')
      if [[ -n "$DASHBOARD_PID" ]] && kill -0 "$DASHBOARD_PID" 2>/dev/null; then
        echo "- Dashboard 控制台: 运行中 (pid=$DASHBOARD_PID)"
      else
        echo "- Dashboard 控制台: 未运行"
      fi
    else
      echo "- Dashboard 控制台: 未运行"
    fi

    if [[ -f "$MARKET_PID_FILE" ]]; then
      MARKET_PID=$(cat "$MARKET_PID_FILE" 2>/dev/null | tr -d '[:space:]')
      if [[ -n "$MARKET_PID" ]] && kill -0 "$MARKET_PID" 2>/dev/null; then
        echo "- 实盘验证任务: 运行中 (pid=$MARKET_PID)"
      else
        echo "- 实盘验证任务: 未运行"
      fi
    else
      echo "- 实盘验证任务: 未运行"
    fi

    if [[ -f "$DASHBOARD_LOG" ]]; then
      echo "- Dashboard 日志: $DASHBOARD_LOG"
    fi
    if [[ -f "$MARKET_LOG" ]]; then
      echo "- 实盘验证日志: $MARKET_LOG"
    fi

    if [[ -f "$MARKET_LOG" ]]; then
      echo ""
      echo "## 实盘验证最近日志 (tail ${PM_TAIL_LINES})"
      tail -n "$PM_TAIL_LINES" "$MARKET_LOG" || true
    elif [[ -f "$DASHBOARD_LOG" ]]; then
      echo ""
      echo "## Dashboard 最近日志 (tail ${PM_TAIL_LINES})"
      tail -n "$PM_TAIL_LINES" "$DASHBOARD_LOG" || true
    fi
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
      --output-dir "$OUTPUT_DIR" \
      --dashboard-pid-file "$OUTPUT_DIR/dashboard_console.pid"
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
    stop_pid_file "$OUTPUT_DIR/market_paper.pid" "后台实盘验证任务"
    run_manage stop \
      --output-dir "$OUTPUT_DIR" \
      --dashboard-pid-file "$OUTPUT_DIR/dashboard_console.pid"
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
