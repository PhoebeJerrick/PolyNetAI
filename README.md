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

> 运行 `record.sh` 的小结：
> - Linux：可直接 `./record.sh ds10`
> - Windows（PowerShell）：建议用 `bash -lc "./record.sh ds10"`（否则可能出现“没反应/没输出”，因为 `record.sh` 是 `bash` 脚本，PowerShell 不会自动按 bash 解释）。

> 云服务器做 dashboard 实盘验证：
> - Linux：可直接 `RECORD_DASHBOARD_HOST=0.0.0.0 ./record.sh dmb10`
> - 然后浏览器访问 `http://43.167.171.148:8765/dashboard.html`

## record.sh 一键入口（推荐）
```bash
./record.sh help
./record.sh d                  # 启动 dashboard 控制台
./record.sh ds<N>             # dashboard + 模拟下单回放 N 个 5m 周期
./record.sh dm<N>             # dashboard + 实盘行情验证 N 个 5m 周期（paper）
./record.sh dmb<N>            # dashboard + 实盘行情验证 N 个 5m 周期（paper，后台）
./record.sh pm                # 查看后台实盘验证状态与最近日志（默认 tail 20）
./record.sh pm100             # 查看后台实盘验证状态与最近日志（tail 100）
./record.sh chart              # 打开 artifacts/charts/tracker_position_compare.html
./record.sh excel-v5          # 生成/更新 data/processed/..._with_accumulated_shares_v5.xlsx

./record.sh s<N>              # 后台开始抓未来 N 个完整 5m 周期
./record.sh r<N>              # 前台抓取并回放 N 个周期（直到生成业绩报告）
./record.sh rb<N>             # 后台抓取并回放 N 个周期（直到生成业绩报告）
./record.sh p                 # 查看后台状态和进度
./record.sh x                 # 停止后台任务（含后台实盘验证）
```

## 常用命令（按场景）

### Tracker 持仓对比折线图（HTML）
直接用 `./record.sh chart` 打开对比图；若需要先生成 v5 累积份数 Excel，可先跑 `./record.sh excel-v5`。

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
