# PolyNetAI

面向 Polymarket 5 分钟周期市场的研究型交易工程项目，当前覆盖：

- 离线回放
- 实时行情 paper trading
- 单周期实盘复盘
- 参数扫描与自动寻优
- dashboard、日报与事件流落盘回放

## 项目定位

这个项目的目标不是只做一次性回测，而是把同一套策略能力同时放进以下几种场景里：

- 历史数据回放：验证策略逻辑、风控和参数组合
- 实时公开行情 paper：观察策略在真实流式数据上的盘中行为
- 单周期实盘：跑完整 5 分钟窗口，并抓取订单、成交、市场数据做复盘
- 事件流回放：直接回放实盘时真实消费到的 websocket `TradeEvent`，避免被 Data API 事后成交表误导

## 目录结构

```text
PolyNetAI/
├─ src/                 # 核心源码
├─ scripts/             # 项目入口脚本
├─ configs/             # 策略、sweep、optimize 配置
├─ docs/                # 使用说明与实验流程
├─ tests/               # 单元测试与回放测试
├─ data/
│  ├─ raw/              # 原始 Excel / CSV（本地放置，默认不提交 Git）
│  └─ processed/        # 分析脚本产物（同上）
├─ notes/
│  └─ strategy/         # 中文策略说明与规则文档
├─ artifacts/
│  ├─ replays/          # 离线回放结果
│  ├─ live/             # 实时运行结果、dashboard、单周期复盘
│  ├─ sweeps/           # 参数扫描结果
│  └─ optimization/     # 自动寻优结果
├─ analyze_polymarket_tracker.py
├─ pyproject.toml
└─ .gitignore
```

## 推荐阅读顺序

1. 先看统一入口帮助：`scripts/run_polynet.py`
2. 再看实验流程：`docs/experiment_workflow.md`
3. 最后根据目标选择：
   - 历史回放
   - 实时 paper
   - 单周期实盘
   - 实盘事件流回放

## 主要入口脚本

### 项目级入口

- `scripts/run_polynet.py`
  - 统一输出 help、profiles，并按预设档案启动任务

### 回放与实验

- `scripts/run_paper_replay.py`
  - 单次离线回放，输出 Excel 与 CSV 报表
- `scripts/run_parameter_sweep.py`
  - 批量参数扫描
- `scripts/run_auto_optimize.py`
  - 自动参数寻优与 leaderboard

### 实时与复盘

- `scripts/run_live_paper.py`
  - 本地历史数据的准实时 paper runner
- `scripts/run_polymarket_live_paper.py`
  - 接 Polymarket 公开实时 websocket 做 paper trading
- `scripts/capture_polymarket_ws_events.py`
  - 只抓公开 websocket `TradeEvent` 事件流，按周期单独落盘，供后续离线复盘
- `scripts/run_polymarket_cycle_review.py`
  - 跑一个完整 5 分钟周期，可选真实下单，并自动抓取成交与市场数据复盘
- `scripts/replay_recorded_trade_events.py`
  - 回放实盘过程中真实消费到的 websocket 事件流

### 可视化与补报表

- `scripts/run_dashboard_console.py`
  - 本地 dashboard 控制台，可视化编辑配置并启动任务
- `scripts/run_dashboard_report.py`
  - 从已有输出目录补生成 dashboard 与日报

### 辅助分析

- `analyze_polymarket_tracker.py`
  - 旧版 Excel 持仓/盈亏分析工具，仍可单独使用

## 常用命令

### 1. 统一帮助

```bash
python scripts/run_polynet.py help
python scripts/run_polynet.py profiles
```

### 2. 快速启动内置档案

```bash
python scripts/run_polynet.py start sim-paper
python scripts/run_polynet.py start market-paper
```

### 3. 单次离线回放

```bash
python scripts/run_paper_replay.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --output artifacts/replays/paper_replay_report.xlsx
```

### 4. 参数扫描

```bash
python scripts/run_parameter_sweep.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --sweep configs/sweep.yaml --output-dir artifacts/sweeps/sweep_outputs
```

### 5. 自动参数寻优

```bash
python scripts/run_auto_optimize.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --optimize configs/optimize.yaml --output-dir artifacts/optimization/optimize_outputs
```

### 6. 本地历史数据准实时回放

```bash
python scripts/run_live_paper.py --input data/raw/polymarket_tracker_collection.xlsx --sheet BTC --config configs/strategy.yaml --output-dir artifacts/live/live_outputs_btc --pace-factor 20 --status-every 100
```

### 7. 接 Polymarket 实时行情跑 paper

跑 10 个周期：

```bash
python scripts/run_polymarket_live_paper.py --slug-prefix btc-updown-5m- --max-cycles 10 --config configs/strategy.yaml --output-dir artifacts/live/polymarket_btc_10cycles
```

带 dashboard 刷新与参数热加载：

```bash
python -u scripts/run_polymarket_live_paper.py --robot-mode --slug-prefix btc-updown-5m- --max-cycles 10 --config configs/strategy.yaml --output-dir artifacts/live/polymarket_btc_10cycles --dashboard-refresh-seconds 1
```

显式指定 market slug：

```bash
python scripts/run_polymarket_live_paper.py --market-slugs btc-updown-5m-1773826800 btc-updown-5m-1773827100 --max-cycles 2 --config configs/strategy.yaml --output-dir artifacts/live/polymarket_selected_cycles
```

### 8. 单独抓取 Polymarket websocket 事件流

如果你只想抓 BTC 5 分钟窗口的实时 websocket 价格事件，不跑策略、不下单，只做后续离线复盘输入：

抓接下来的 3 个 5 分钟周期：

```bash
python scripts/capture_polymarket_ws_events.py --slug-prefix btc-updown-5m- --max-cycles 3 --output-dir artifacts/live/ws_capture_btc_3cycles
```

显式指定要抓的 market slug：

```bash
python scripts/capture_polymarket_ws_events.py --market-slugs btc-updown-5m-1774147200 btc-updown-5m-1774147500 --max-cycles 2 --output-dir artifacts/live/ws_capture_selected
```

脚本会输出：

- `artifacts/live/ws_capture_.../ws_trade_events_all.ndjson`
- `artifacts/live/ws_capture_.../ws_trade_events_all.csv`
- `artifacts/live/ws_capture_.../capture_manifest.json`
- `artifacts/live/ws_capture_.../<cycle_slug>/ws_trade_events.ndjson`
- `artifacts/live/ws_capture_.../<cycle_slug>/ws_trade_events.csv`

这些 `ndjson` 文件可以直接拿去做离线回放。

### 9. 回放实盘 websocket 事件流

当你已经拿到某轮 live/real 运行时实际消费到的 `ws_trade_events.ndjson`，优先用它做复盘：

```bash
python scripts/replay_recorded_trade_events.py --input artifacts/live/polymarket_cycle_review/account_2/<cycle_slug>/ws_trade_events.ndjson --config configs/strategy.yaml --starting-cash 100 --output artifacts/replays/<cycle_slug>_recorded_event_replay.xlsx
```

如果要按某组覆盖参数回放当前代码，例如 `trial_022`：

```bash
python scripts/replay_recorded_trade_events.py --input artifacts/live/ws_capture_btc_3cycles/<cycle_slug>/ws_trade_events.ndjson --config configs/strategy.yaml --overrides artifacts/optimization/optimize_btc_last_6h_100u_smallcap_v2_20260321T014000Z/trial_022/overrides.json --starting-cash 100 --output artifacts/replays/<cycle_slug>_trial022_replay.xlsx
```

### 10. Dashboard 控制台

```bash
python scripts/run_dashboard_console.py --dashboard-dir artifacts/live/polymarket_btc_10cycles
```

浏览器打开：

```text
http://127.0.0.1:8765/dashboard.html
```

### 11. 补生成 dashboard

```bash
python scripts/run_dashboard_report.py --input-dir artifacts/live/live_outputs_btc --title "BTC Live Dashboard"
```

### 12. 原始 Excel 分析

```bash
python analyze_polymarket_tracker.py --input data/raw/polymarket_tracker_collection.xlsx
```

## 实时运行说明

### `run_polymarket_live_paper.py`

- 只读取 Polymarket 真实行情 websocket，不会真实下单
- 会生成 `cycles.csv`、`decisions.csv`、`metrics.csv`、`snapshots.csv`、`live_report.xlsx`
- 若设置 `--dashboard-refresh-seconds 1`，运行中会持续更新 dashboard
- `strategy.yaml` 保存后会在后续事件处理前自动热加载
- 默认尝试读取上一级目录 `APIs/ApiConfig.env`
- 若配置了 `HTTPS_PROXY`、`HTTP_PROXY`、`ALL_PROXY`、`WS_PROXY` 等，会自动写入环境变量
- 现在也会额外落盘：
  - `ws_trade_events.ndjson`
  - `ws_trade_events.csv`

### `capture_polymarket_ws_events.py`

- 只订阅并保存公开 websocket `last_trade_price` 事件，不跑策略、不做 paper、不真实下单
- 默认按 `btc-updown-5m-` 自动发现并连续抓取指定数量的 5 分钟窗口
- 每个周期会单独生成一份 `ws_trade_events.ndjson` / `ws_trade_events.csv`
- 根目录还会额外生成一份跨周期合并文件 `ws_trade_events_all.ndjson`
- `capture_manifest.json` / `capture_manifest.csv` 会列出每个周期抓到了多少事件、起止时间、首尾价格
- 这些文件可直接交给 `scripts/replay_recorded_trade_events.py` 做离线复盘

### `run_polymarket_cycle_review.py`

- 用于锁定并跑一个完整 5 分钟周期
- 可选 `--real-trading` 启用真实下单
- 会自动输出：
  - `real_order_attempts.json`
  - `account_trades_raw.csv`
  - `market_trades_raw.csv`
  - `review_summary.md`
  - `ws_trade_events.ndjson`
  - `ws_trade_events.csv`

## 为什么要记录 websocket 事件流

过去如果直接拿 Data API 的 `market_trades_raw.csv` 来回放，容易出现两个问题：

- 数据只覆盖了事后可查询到的一段成交，而不一定是机器人真实消费顺序
- `Up / Down` 两边 token 的成交混在一起时，会把单价格状态机喂偏

现在推荐的复盘顺序是：

1. 先看 `review_summary.md` 和 `real_order_attempts.json`
2. 再看 `account_trades_raw.csv`
3. 如果要离线复现“机器人当时看到的输入”，优先回放 `ws_trade_events.ndjson`

## 产物位置

- 离线回放：`artifacts/replays/`
- 实时与单周期复盘：`artifacts/live/`
- 参数扫描：`artifacts/sweeps/`
- 自动寻优：`artifacts/optimization/`
- 仪表盘与日报常见文件：
  - `dashboard.html`
  - `daily_report.md`
  - `dashboard_summary.csv`

## 相关文档

- 使用流程：`docs/experiment_workflow.md`
- 策略规格：`docs/strategy_spec.md`
- 数据与产物是否进 Git：`docs/data_and_artifacts.md`
- 中文策略原文：`notes/strategy/`

## 测试

```bash
python -m pytest -q
```

当前项目已经包含状态引擎、策略路由、参数扫描、自动寻优、live runner、dashboard、实盘事件落盘与回放等测试。

## 跑一个完整周期

下面这条命令会等待到下一个完整 5 分钟窗口开始，再执行一整轮单周期复盘；若加上 `--real-trading`，会启用真实下单：

```bash
python scripts/run_polymarket_cycle_review.py --config configs/strategy.yaml --overrides artifacts/optimization/optimize_btc_last_6h_100u_smallcap_v2_20260321T014000Z/trial_022/overrides.json --output-dir artifacts/live/polymarket_cycle_review --slug-prefix btc-updown-5m- --account-index 2 --env-file ../APIs/ApiConfig.env --dashboard-refresh-seconds 1 --status-every 25 --cycle-grace-seconds 20 --start-buffer-seconds 2 --real-trading
```
