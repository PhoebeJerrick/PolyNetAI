# PolyNetAI

围绕 Polymarket 5 分钟周期策略的研究型工程项目，当前覆盖：

- 离线回放
- 准实时 paper trading
- 参数扫描
- 自动参数寻优
- 监控面板与日报表

## 目录结构

```text
PolyNetAI/
├─ src/                 # 核心源码
├─ scripts/             # 项目入口脚本
├─ configs/             # 策略、sweep、optimize 配置
├─ docs/                # 使用说明与策略规格
├─ tests/               # 单元测试与回放测试
├─ data/
│  ├─ raw/              # 原始 Excel（本地放置，默认不提交 Git）
│  └─ processed/        # 处理后 Excel（同上）
├─ notes/
│  └─ strategy/         # 中文策略提示词与买卖规则文档
├─ artifacts/
│  ├─ replays/          # 单次离线回放结果
│  ├─ live/             # 准实时模拟与 dashboard
│  ├─ sweeps/           # 批量参数扫描结果
│  └─ optimization/     # 自动寻优结果
├─ analyze_polymarket_tracker.py
├─ pyproject.toml
└─ .gitignore
```

## 入口脚本

- `scripts/run_paper_replay.py`
  - 单次离线回放，输出 Excel 报表
- `scripts/run_parameter_sweep.py`
  - 批量参数扫描
- `scripts/run_auto_optimize.py`
  - 自动参数寻优与排行榜
- `scripts/run_live_paper.py`
  - 准实时 paper trading runner
- `scripts/run_polymarket_live_paper.py`
  - 直接接入 Polymarket 公开实时行情的 paper trading runner
- `scripts/run_dashboard_report.py`
  - 从已有输出目录补生成 dashboard 和日报
- `scripts/run_dashboard_console.py`
  - 启动本地 dashboard 控制台，可视化编辑 `strategy / sweep / optimize` 配置
- `scripts/run_polynet.py`
  - 项目级命令入口，统一输出 help 并按预设档案启动任务
- `analyze_polymarket_tracker.py`
  - 旧版 Excel 持仓/盈亏分析工具，仍可单独使用

## 常用命令

### 0. 项目级 Help

推荐先看统一帮助：

```bash
python scripts/run_polynet.py help
```

查看项目内置启动档案：

```bash
python scripts/run_polynet.py profiles
```

直接启动“模拟下单测试”：

```bash
python scripts/run_polynet.py start sim-paper
```

直接启动“实盘行情验证（paper）”：

```bash
python scripts/run_polynet.py start market-paper
```

### 1. 离线回放

```bash
python scripts/run_paper_replay.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --output artifacts/replays/paper_replay_report.xlsx
```

### 2. 批量参数扫描

```bash
python scripts/run_parameter_sweep.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --sweep configs/sweep.yaml --output-dir artifacts/sweeps/sweep_outputs
```

### 3. 自动参数寻优

```bash
python scripts/run_auto_optimize.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --optimize configs/optimize.yaml --output-dir artifacts/optimization/optimize_outputs
```

### 4. 准实时模拟

```bash
python scripts/run_live_paper.py --input data/raw/polymarket_tracker_collection.xlsx --sheet BTC --config configs/strategy.yaml --output-dir artifacts/live/live_outputs_btc --pace-factor 20 --status-every 100
```

### 5. 接 Polymarket 实时行情跑 10 个周期

```bash
python scripts/run_polymarket_live_paper.py --slug-prefix btc-updown-5m- --max-cycles 10 --config configs/strategy.yaml --output-dir artifacts/live/polymarket_btc_10cycles
```

如需实时 dashboard 与参数热更新控制台，建议使用：

```bash
python -u scripts/run_polymarket_live_paper.py --robot-mode --slug-prefix btc-updown-5m- --max-cycles 10 --config configs/strategy.yaml --output-dir artifacts/live/polymarket_btc_10cycles --dashboard-refresh-seconds 1
```

如果你已经手头有明确的 10 个 market slug，也可以直接传：

```bash
python scripts/run_polymarket_live_paper.py --market-slugs btc-updown-5m-1773826800 btc-updown-5m-1773827100 --max-cycles 2 --config configs/strategy.yaml --output-dir artifacts/live/polymarket_selected_cycles
```

说明：

- 该脚本只读取 Polymarket 真实行情 websocket，不会真实下单
- 输出同样会生成 `cycles.csv`、`metrics.csv`、`snapshots.csv`、`live_report.xlsx`、`dashboard.html`
- 若设置 `--dashboard-refresh-seconds 1`，运行中会每秒更新 `dashboard_state.js` 与 `dashboard.html`
- 运行中的 live runner 会监控 `strategy.yaml` 修改时间，参数保存后会在后续事件处理前自动热加载
- 默认会尝试读取上一级目录的 `APIs/ApiConfig.env`，但当前 paper trading 不会使用私钥发单
- 若 `ApiConfig.env` 里配置了 `HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY`、`WS_PROXY` 等，脚本会自动写入环境变量，HTTPS 与 WebSocket 会走同一代理
- 买单现在会同时受下列约束：
  - `order_sizing.min_order_size / max_order_size`
  - `exposure.max_abs_exposure`
  - `capital.max_cash_utilization / min_cash_buffer`
  - 若可用现金不足，订单会被缩量或直接拒绝

### 6. Dashboard 控制台

如果你打算一边看 dashboard 一边改参数或一键启动/停止任务，建议先启动控制台：

```bash
python scripts/run_dashboard_console.py --dashboard-dir artifacts/live/polymarket_btc_10cycles
```

然后在浏览器打开：

```text
http://127.0.0.1:8765/dashboard.html
```

说明：

- 页面会每秒刷新实时状态
- 可在页面中直接编辑并保存 `strategy.yaml`、`sweep.yaml`、`optimize.yaml`
- 页面内置“运行控制台”，可直接一键启动：
  - `模拟下单测试`：本地 Excel 数据流准实时 paper trading
  - `实盘行情验证`：Polymarket 实时公开行情 robot-mode paper 验证
- 页面也可直接停止当前正在运行的任务，并显示当前底层命令与日志路径
- `strategy.yaml` 保存后会在 live runner 中自动热加载
- `sweep.yaml` / `optimize.yaml` 会影响后续扫描和自动寻优任务

### 7. 补生成 dashboard

```bash
python scripts/run_dashboard_report.py --input-dir artifacts/live/live_outputs_btc --title "BTC Live Dashboard"
```

### 8. 原始 Excel 分析

```bash
python analyze_polymarket_tracker.py --input data/raw/polymarket_tracker_collection.xlsx
```

如果输入位于 `data/raw/`，默认处理结果会优先输出到 `data/processed/`。

## 产物位置

- 离线回放：`artifacts/replays/`
- 准实时模拟：`artifacts/live/`
- 参数扫描：`artifacts/sweeps/`
- 自动寻优：`artifacts/optimization/`
- 仪表盘与日报：
  - `dashboard.html`
  - `daily_report.md`
  - `dashboard_summary.csv`

## 相关文档

- 使用流程：`docs/experiment_workflow.md`
- 策略规格：`docs/strategy_spec.md`
- **数据与产物是否进 Git**：`docs/data_and_artifacts.md`
- 中文策略原文：`notes/strategy/`

## 测试

```bash
python -m pytest -q
```

当前项目已经包含状态引擎、策略路由、参数扫描、自动寻优、准实时 runner、dashboard 等测试。
