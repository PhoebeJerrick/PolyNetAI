# PolyNetAI

面向 Polymarket 5 分钟周期市场的研究型交易工程，覆盖：

- 离线回放（Excel 历史数据、实盘事件流）
- 实时行情 paper trading
- 单周期复盘（可选真实下单）
- 参数扫描与自动寻优
- Dashboard 与日报

## 快速开始

### 1) 查看统一入口

```bash
python scripts/run_polynet.py help
python scripts/run_polynet.py profiles
```

### 2) 直接启动内置任务

```bash
python scripts/run_polynet.py start sim-paper
python scripts/run_polynet.py start market-paper
```

## Dashboard 正确加载（重点）

当你改了代码或想确保页面不是旧缓存时，按下面做：

1. 重新生成 dashboard 文件：

```bash
python scripts/run_dashboard_report.py --html-only --output-dir artifacts/live/record_job/batch_replay_outputs
```

2. 用本地控制台启动并打开页面：

```bash
python scripts/run_dashboard_console.py --dashboard-dir artifacts/live/record_job/batch_replay_outputs
```

浏览器访问：`http://127.0.0.1:8765/dashboard.html`

3. 浏览器强制刷新：`Ctrl+F5`（或 `Ctrl+Shift+R`）。

> 如果你的目录不是 `artifacts/live/record_job/batch_replay_outputs`，把上面两个命令里的目录换成你自己的输出目录。

## 常用命令（按场景）

### 离线回放与调参

```bash
python scripts/run_paper_replay.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --output artifacts/replays/paper_replay_report.xlsx
python scripts/run_parameter_sweep.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --sweep configs/sweep.yaml --output-dir artifacts/sweeps/sweep_outputs
python scripts/run_auto_optimize.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --optimize configs/optimize.yaml --output-dir artifacts/optimization/optimize_outputs
```

### Tracker 持仓对比折线图（HTML）

```bash
python scripts/build_tracker_position_compare_chart.py --input data/processed/polymarket_tracker_collection_with_accumulated_shares_v5.xlsx --output artifacts/charts/tracker_position_compare.html
```

可选指定工作表：

```bash
python scripts/build_tracker_position_compare_chart.py --input data/processed/polymarket_tracker_collection_with_accumulated_shares_v5.xlsx --sheet BTC --output artifacts/charts/tracker_position_compare.html
```

输出文件为交互式 HTML，上图包含持仓相关折线（含 `Up方向投注总价值`、`Down方向投注总价值`、`当前持仓投注总价值`、`净持仓价值`），并将 `投注份数` 的 Up/Down 方向点用不同颜色标记，便于对比。
图中会提供 `时间周期` 下拉框（默认第一个周期），并在下方提供每条曲线对应的 checkbox。勾选状态在切换周期后保持一致，可持续按指标过滤。页面下方会同步显示同周期的 `Up价格`/`Down价格` 折线图，两张图在切换周期时同时刷新，横轴均为单周期内秒数。

### 本地事件流准实时回放（对应“模拟下单测试”）

```bash
python scripts/run_recorded_live_paper.py --input-dir artifacts/live/record_job --cycle-glob "btc-updown-5m-*" --max-cycles 10 --config configs/strategy.yaml --output-dir artifacts/live/record_job/batch_replay_outputs --pace-factor 20 --status-every 100 --dashboard-refresh-seconds 1 --starting-cash 1000
```

### 实时行情 paper（对应“实盘行情验证”）

```bash
python -u scripts/run_polymarket_live_paper.py --robot-mode --slug-prefix btc-updown-5m- --max-cycles 10 --config configs/strategy.yaml --output-dir artifacts/live/polymarket_btc_10cycles --dashboard-refresh-seconds 1 --starting-cash 1000
```

### 单独抓取 websocket 事件流

```bash
python scripts/capture_polymarket_ws_events.py --slug-prefix btc-updown-5m- --max-cycles 10 --output-dir artifacts/live/ws_capture_btc_10cycles
```

后台抓取（长时间任务）：

```bash
python scripts/capture_polymarket_ws_events.py --daemonize --slug-prefix btc-updown-5m- --max-cycles 300 --start-buffer-seconds 2 --output-dir artifacts/live/ws_capture_btc_300cycles
```

### 抓取流水线一键管理

```bash
python scripts/manage_capture_pipeline.py start --output-dir artifacts/live/ws_capture_btc_300cycles --slug-prefix btc-updown-5m- --max-cycles 300 --start-buffer-seconds 2
python scripts/manage_capture_pipeline.py status --output-dir artifacts/live/ws_capture_btc_300cycles
python scripts/manage_capture_pipeline.py run-full --output-dir artifacts/live/ws_capture_btc_10cycles --slug-prefix btc-updown-5m- --max-cycles 10 --config configs/strategy.yaml --starting-cash 100
python scripts/manage_capture_pipeline.py stop --output-dir artifacts/live/ws_capture_btc_300cycles
```

### 回放已记录事件流

单周期：

```bash
python scripts/replay_recorded_trade_events.py --input artifacts/live/ws_capture_btc_10cycles/<cycle_slug>/ws_trade_events.ndjson --config configs/strategy.yaml --starting-cash 100 --output artifacts/replays/<cycle_slug>_recorded_event_replay.xlsx
```

批量：

```bash
python scripts/batch_replay_recorded_trade_events.py --input-dir artifacts/live/ws_capture_btc_10cycles --config configs/strategy.yaml --starting-cash 100
```

仅重建中文绩效报告：

```bash
python scripts/build_batch_replay_performance_report.py --input-dir artifacts/live/ws_capture_btc_10cycles
```

### 单周期复盘（可选真实下单）

```bash
python scripts/run_polymarket_cycle_review.py --config configs/strategy.yaml --output-dir artifacts/live/polymarket_cycle_review --slug-prefix btc-updown-5m- --account-index 2 --dashboard-refresh-seconds 1 --status-every 25 --start-buffer-seconds 2
```

需要真实下单时再加 `--real-trading`，并先确认 `env-file`、账户索引与风控参数。

## 主要脚本说明

- `scripts/run_polynet.py`：统一帮助、预设 profile 启动入口
- `scripts/run_dashboard_console.py`：Dashboard 本地控制台（可编辑配置/启动任务）
- `scripts/run_dashboard_report.py`：补生成 dashboard、日报与状态脚本
- `scripts/run_recorded_live_paper.py`：回放本地事件流做准实时 paper
- `scripts/run_polymarket_live_paper.py`：连接 Polymarket 实时 websocket 做 paper
- `scripts/capture_polymarket_ws_events.py`：仅抓取 websocket 事件流，不跑策略
- `scripts/replay_recorded_trade_events.py`：回放单个 `ws_trade_events.ndjson`
- `scripts/batch_replay_recorded_trade_events.py`：批量回放多个周期事件流
- `scripts/build_batch_replay_performance_report.py`：按目录重建中文绩效报告
- `scripts/manage_capture_pipeline.py`：抓取到报告的一体化管理
- `scripts/run_paper_replay.py` / `run_parameter_sweep.py` / `run_auto_optimize.py`：离线回放、扫描、自动寻优

## 目录结构

```text
PolyNetAI/
├─ src/          # 核心源码
├─ scripts/      # 可执行脚本
├─ configs/      # strategy/sweep/optimize 配置
├─ docs/         # 流程与规格文档
├─ tests/        # 测试
└─ artifacts/    # 运行产物（replays/live/sweeps/optimization）
```

## 关键产物

常见运行目录会包含：

- `cycles.csv`
- `decisions.csv`
- `metrics.csv`
- `snapshots.csv`
- `dashboard.html`
- `dashboard_state.js`
- `daily_report.md`

事件流抓取目录会包含：

- `capture_manifest.json`
- `<cycle_slug>/ws_trade_events.ndjson`

## 为什么优先回放 websocket 事件流

直接用事后汇总成交表，容易和机器人当时真实看到的流式输入不一致。  
回放 `ws_trade_events.ndjson` 更接近真实决策输入序列，复盘结论更可靠。

## 相关文档

- `docs/experiment_workflow.md`
- `docs/strategy_spec.md`
- `docs/data_and_artifacts.md`
- `notes/strategy/`

## 测试

```bash
python -m pytest -q
```
