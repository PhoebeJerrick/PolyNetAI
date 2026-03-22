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
  - 只抓公开 websocket `TradeEvent` 事件流，从新周期开始写盘，按周期单独落盘
- `scripts/run_polymarket_cycle_review.py`
  - 跑一个完整 5 分钟周期，可选真实下单，并自动抓取成交与市场数据复盘
- `scripts/replay_recorded_trade_events.py`
  - 回放实盘过程中真实消费到的 websocket 事件流
- `scripts/batch_replay_recorded_trade_events.py`
  - 批量回放整个抓取目录下的多个周期事件流
- `scripts/build_batch_replay_performance_report.py`
  - 基于批量回放结果生成更适合阅读的中文总绩效报告
- `scripts/manage_capture_pipeline.py`
  - 统一管理抓取任务、后台状态、一键抓取到报告、一键停止

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

如果你嫌下面这些 Python 指令太长，Linux 云服务器上直接优先用仓库根目录的 `record.sh`。

最短常用命令：

```bash
chmod +x record.sh
./record.sh s3
./record.sh p
./record.sh r10
./record.sh rb300
./record.sh x
```

含义分别是：

- `./record.sh s3`
  - 后台开始抓未来 3 个完整的 5 分钟周期数据流
- `./record.sh p`
  - 查看后台运行状态和当前进度
- `./record.sh r10`
  - 前台一键抓 10 个周期，并在结束后自动批量回放和生成总绩效报告
- `./record.sh rb300`
  - 后台一键抓 300 个周期，并在结束后自动批量回放和生成总绩效报告
- `./record.sh x`
  - 一键停止后台任务

默认行为：

- 默认输出目录是 `artifacts/live/record_job`
- 默认抓 `btc-updown-5m-`
- 默认回放参数是当前 README 里这套 `trial_022` overrides
- 默认按“连续完整未来窗口”抓取，避免 `s3` 只抓到不连续周期
- 如果你反复执行 `./record.sh s ...` 或 `./record.sh rb ...`，会继续使用同一个默认输出目录

如果你想改输出目录：

```bash
./record.sh s300 -o artifacts/live/record_job_300
./record.sh p -o artifacts/live/record_job_300
./record.sh x -o artifacts/live/record_job_300
```

抓接下来的 3 个 5 分钟周期：

```bash
python scripts/capture_polymarket_ws_events.py --slug-prefix btc-updown-5m- --max-cycles 3 --output-dir artifacts/live/ws_capture_btc_3cycles
```

如果你要在 Linux 云服务器后台长时间抓取几百个周期，可以直接在同一条命令上加 `--daemonize`：

```bash
python scripts/capture_polymarket_ws_events.py --daemonize --slug-prefix btc-updown-5m- --max-cycles 300 --start-buffer-seconds 2 --output-dir artifacts/live/ws_capture_btc_300cycles
```

后台模式默认会额外生成：

- `artifacts/live/ws_capture_btc_300cycles/capture.log`
- `artifacts/live/ws_capture_btc_300cycles/capture.pid`
- `artifacts/live/ws_capture_btc_300cycles/capture_background_meta.json`

常用运维命令：

```bash
tail -f artifacts/live/ws_capture_btc_300cycles/capture.log
cat artifacts/live/ws_capture_btc_300cycles/capture.pid
kill $(cat artifacts/live/ws_capture_btc_300cycles/capture.pid)
```

如果你想自定义日志和 PID 文件位置：

```bash
python scripts/capture_polymarket_ws_events.py --daemonize --slug-prefix btc-updown-5m- --max-cycles 300 --output-dir artifacts/live/ws_capture_btc_300cycles --log-file logs/btc_300cycles.log --pid-file logs/btc_300cycles.pid
```

默认行为：

- 会跳过已经开始的当前窗口
- 只锁定未来的新窗口
- 会在窗口开始前提前连上 websocket，但只从周期开始时刻起正式写盘
- 默认 `cycle_grace_seconds=0`，优先保证连续多个 5 分钟窗口不丢开头

如果你想调节“提前连线但不提前写盘”的缓冲时间：

```bash
python scripts/capture_polymarket_ws_events.py --slug-prefix btc-updown-5m- --max-cycles 3 --start-buffer-seconds 2 --output-dir artifacts/live/ws_capture_btc_3cycles
```

显式指定要抓的 market slug：

```bash
python scripts/capture_polymarket_ws_events.py --market-slugs btc-updown-5m-1774147500 btc-updown-5m-1774147800 --max-cycles 2 --output-dir artifacts/live/ws_capture_selected
```

脚本会输出：

- `artifacts/live/ws_capture_.../ws_trade_events_all.ndjson`
- `artifacts/live/ws_capture_.../ws_trade_events_all.csv`
- `artifacts/live/ws_capture_.../capture_manifest.json`
- `artifacts/live/ws_capture_.../<cycle_slug>/ws_trade_events.ndjson`
- `artifacts/live/ws_capture_.../<cycle_slug>/ws_trade_events.csv`

这些 `ndjson` 文件可以直接拿去做离线回放。

如果你更希望用一个统一的小工具脚本来管理整个流程，可以直接用下面这几个子命令。

后台开始抓数据：

```bash
python scripts/manage_capture_pipeline.py start --output-dir artifacts/live/ws_capture_btc_300cycles --slug-prefix btc-updown-5m- --max-cycles 300 --start-buffer-seconds 2
```

查看后台运行状态和进度：

```bash
python scripts/manage_capture_pipeline.py status --output-dir artifacts/live/ws_capture_btc_300cycles
```

一键抓取并直到生成业绩报告：

```bash
python scripts/manage_capture_pipeline.py run-full --output-dir artifacts/live/ws_capture_btc_10cycles --slug-prefix btc-updown-5m- --max-cycles 10 --config configs/strategy.yaml --overrides artifacts/optimization/optimize_btc_last_6h_100u_smallcap_v2_20260321T014000Z/trial_022/overrides.json --starting-cash 100
```

如果想把整条流水线也放到后台：

```bash
python scripts/manage_capture_pipeline.py run-full --daemonize --output-dir artifacts/live/ws_capture_btc_300cycles --slug-prefix btc-updown-5m- --max-cycles 300 --config configs/strategy.yaml --overrides artifacts/optimization/optimize_btc_last_6h_100u_smallcap_v2_20260321T014000Z/trial_022/overrides.json --starting-cash 100
```

一键停止进程：

```bash
python scripts/manage_capture_pipeline.py stop --output-dir artifacts/live/ws_capture_btc_300cycles
```

### 9. 单周期离线回放 websocket 事件流

当你已经拿到某轮 live/real 运行时实际消费到的 `ws_trade_events.ndjson`，优先用它做复盘：

```bash
python scripts/replay_recorded_trade_events.py --input artifacts/live/polymarket_cycle_review/account_2/<cycle_slug>/ws_trade_events.ndjson --config configs/strategy.yaml --starting-cash 100 --output artifacts/replays/<cycle_slug>_recorded_event_replay.xlsx
```

如果要按某组覆盖参数回放当前代码，例如 `trial_022`：

```bash
python scripts/replay_recorded_trade_events.py --input artifacts/live/ws_capture_btc_3cycles/<cycle_slug>/ws_trade_events.ndjson --config configs/strategy.yaml --overrides artifacts/optimization/optimize_btc_last_6h_100u_smallcap_v2_20260321T014000Z/trial_022/overrides.json --starting-cash 100 --output artifacts/replays/<cycle_slug>_trial022_replay.xlsx
```

### 10. 批量回放整个抓取目录

如果你已经连续抓了 10 个周期，可以一条命令把整个目录全部回放出来；现在这条命令在批量回放结束后，会自动顺手生成总绩效中文报告：

```bash
python scripts/batch_replay_recorded_trade_events.py --input-dir artifacts/live/ws_capture_btc_10cycles --config configs/strategy.yaml --overrides artifacts/optimization/optimize_btc_last_6h_100u_smallcap_v2_20260321T014000Z/trial_022/overrides.json --starting-cash 100
```

默认会输出到：

- `artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs/<cycle_slug>/...`
- `artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs/batch_replay_summary.csv`
- `artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs/batch_replay_summary.md`
- `artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs/batch_replay_performance_report_zh.md`
- `artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs/batch_replay_summary_enriched.csv`
- `artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs/batch_replay_direction_distribution.csv`
- `artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs/batch_replay_winner_distribution.csv`
- `artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs/batch_replay_net_direction_distribution.csv`

### 11. 生成批量回放总绩效报告

在批量回放完成后，再运行下面这条命令，可以自动生成一份更适合阅读的中文总报告：

```bash
python scripts/build_batch_replay_performance_report.py --input-dir artifacts/live/ws_capture_btc_10cycles
```

也可以直接把输入目录指向 `batch_replay_outputs`：

```bash
python scripts/build_batch_replay_performance_report.py --input-dir artifacts/live/ws_capture_btc_10cycles/batch_replay_outputs
```

默认会输出：

- `batch_replay_performance_report_zh.md`
- `batch_replay_summary_enriched.csv`
- `batch_replay_direction_distribution.csv`
- `batch_replay_winner_distribution.csv`
- `batch_replay_net_direction_distribution.csv`

报告内容会集中汇总：

- 总净利润
- 平均单周期净利润
- 胜率
- 最大回撤
- 总手续费
- 总执行成交数
- 执行方向分布
- 周期赢家分布
- 周期结束净方向分布

### 12. Dashboard 控制台

```bash
python scripts/run_dashboard_console.py --dashboard-dir artifacts/live/polymarket_btc_10cycles
```

浏览器打开：

```text
http://127.0.0.1:8765/dashboard.html
```

### 13. 补生成 dashboard

```bash
python scripts/run_dashboard_report.py --input-dir artifacts/live/live_outputs_btc --title "BTC Live Dashboard"
```

### 14. 原始 Excel 分析

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
- 默认按 `btc-updown-5m-` 自动发现未来的新 5 分钟窗口，并跳过已经开始的当前窗口
- 会在窗口开始前按 `--start-buffer-seconds` 提前建立连接，但真正写盘从周期开始时刻算起
- 支持 `--daemonize` 后台启动，适合 Linux 云服务器长时间抓几百个周期
- 后台模式会自动写 `capture.log`、`capture.pid`、`capture_background_meta.json`
- 每个周期会单独生成一份 `ws_trade_events.ndjson` / `ws_trade_events.csv`
- 根目录还会额外生成一份跨周期合并文件 `ws_trade_events_all.ndjson`
- `capture_manifest.json` / `capture_manifest.csv` 会列出每个周期抓到了多少事件、起止时间、首尾价格
- 这些文件可直接交给 `scripts/replay_recorded_trade_events.py` 做离线复盘

### `batch_replay_recorded_trade_events.py`

- 会扫描抓取目录下所有 `<cycle_slug>/ws_trade_events.ndjson`
- 每个周期单独输出 Excel、`cycles.csv`、`decisions.csv`、`metrics.csv`
- 最后会自动同时输出：
  - `batch_replay_summary.csv` / `batch_replay_summary.md`
  - `batch_replay_performance_report_zh.md`
  - 方向分布、赢家分布、净方向分布等聚合 CSV
- 适合抓完 5 个、10 个或更多周期后，直接批量评估当前代码的离线盈亏能力

### `build_batch_replay_performance_report.py`

- 可直接读取抓取目录或 `batch_replay_outputs` 目录
- 自动汇总总收益、胜率、最大回撤、方向分布等关键指标
- 会额外输出几份聚合 CSV，方便你继续做二次分析或画图
- 适合在 10 个周期批量回放结束后，一次性做总体验收

### `manage_capture_pipeline.py`

- `start`: 后台开始抓数据
- `status`: 查看后台运行状态、已完成周期数、已记录事件数、最近完成周期、是否已生成总报告
- `run-full`: 一键抓取，结束后自动批量回放并生成总绩效报告
- `run-full --daemonize`: 将整条抓取到报告的流水线放到后台
- `stop`: 一键停止抓取进程或整条后台流水线

### `record.sh`

- Linux 下的超短包装命令，优先给日常操作用
- `./record.sh s -3`: 后台抓未来 3 个完整周期
- `./record.sh p`: 查看状态和进度
- `./record.sh r -10`: 前台抓取并直到生成报告
- `./record.sh rb -300`: 后台抓取并直到生成报告
- `./record.sh x`: 一键停止
- `-o` 可切换输出目录，适合并行开不同任务

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
