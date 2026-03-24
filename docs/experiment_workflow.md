# 参数扫描与实验流程

## 批量下载 BTC 5 分钟市场历史成交（Gamma + data-api）

将过去约 N 天、每个 `btc-updown-5m-<unix_ts>` 窗口的公开成交拉到 `data/raw/`，生成与 `excel_loader` 兼容的 `csv/xlsx`：

```bash
python scripts/download_polymarket_btc_trades_range.py --days 30 --output-prefix polymarket_btc_5m_last30d --resume
```

说明：

- 默认会尝试读取上一级目录 `APIs/ApiConfig.env` 中的代理（与 `run_polymarket_live_paper.py` 一致）。
- **加速（默认）**：先枚举全部 `btc-updown-5m-*` slug，再用 Gamma `/markets` **多 `slug` 批量**拉元数据（`--slug-batch-size`，默认 40）；再用 data-api `/trades` **`--page-limit` 默认 10000**（官方上限）；最后用 **`--workers` 默认 12** 并发拉各市场成交。
- 单月约 `30 * 24 * 12 = 8640` 个槽位；中断后加 `--resume` 续跑（进度里为 `done_slugs`，也可从已生成 CSV 的 `market_slug` 推断）。
- 若行数超过 Excel 单表上限（`1048576`），脚本会**只保留 CSV 作为正式产物**，并在 summary/progress 中标记 `xlsx_written=false`，不再在最后一步因 `to_excel()` 失败而把成功下载误报成失败。
- `--max-pages`（默认 12）限制单市场分页；极端流动性下可能截断尾部成交。
- 串行拉成交时用 `--workers 1`；元数据最慢模式：`--discover slug`（逐条 `markets/slug/...`）。
- 并发时加 `--quiet` 可少刷屏。若出现 **连接被重置**（Windows 常见 `WinError 10054` / `ConnectionResetError`），多为并发过高或远端限流：请 **`--workers` 降到 6～10**、加 **`--resume`** 续跑未完成的 slug；脚本已内置 **urllib3 重试 + 单页/单市场退避重试**（`--market-retries`）。仍不稳定时可设环境变量 `POLYNET_DOWNLOAD_CLOSE=1` 禁用 keep-alive（略慢但更稳）。
- **`WinError 10053`（本机软件中止连接）** 常见于 **代理/防火墙** 在 TLS 阶段断开；发现阶段已带 **批量/单条退避重试**。仍频繁时可 **`--slug-batch-size 15`**、暂时去掉代理或缩小 `HTTPS_PROXY` 使用范围。

## Baseline 与 trial overrides 并排对比

在同一批事件上对比 `configs/strategy.yaml` 与某次寻优的 `overrides.json`（如 `trial_022`）：

```bash
python scripts/compare_baseline_vs_overrides.py ^
  --input data/raw/<你的数据>.xlsx ^
  --overrides artifacts/optimization/<run>/trial_022/overrides.json ^
  --starting-cash 100 ^
  --output artifacts/replays/compare_baseline_vs_trial022.csv
```

`--input` 支持 **csv**；当输入为大 CSV 时，脚本会：

- **流式分块读取**（不再一次性把全部事件装进内存）
- 用同一条事件流同时驱动 `baseline` 与 `trial overrides`
- 若同名 `*.progress.json` 含 `done_slugs`，会把**无成交 5 分钟窗口补成 0 收益周期**

因此输出里的 `total_cycles` 表示完整周期数；新增：

- `observed_cycles_with_trades`：实际有成交的周期数
- `empty_cycles`：无成交但被补齐的周期数

## 单次回放

使用基础策略配置执行一次回放：

```bash
python scripts/run_paper_replay.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --output artifacts/replays/paper_replay_report.xlsx
```

输出包括：

- `cycles`：周期级收益与持仓结果
- `decisions`：逐笔信号、风控状态与模拟成交
- `metrics`：收益、回撤、手续费、信号数、成交数、规则触发统计

## 批量参数扫描

使用 `configs/sweep.yaml` 进行多组参数实验：

```bash
python scripts/run_parameter_sweep.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --sweep configs/sweep.yaml --output-dir artifacts/sweeps/sweep_outputs
```

输出目录结构：

- `artifacts/sweeps/sweep_outputs/sweep_summary.csv`
- `artifacts/sweeps/sweep_outputs/sweep_summary.xlsx`
- `artifacts/sweeps/sweep_outputs/<scenario>/replay_report.xlsx`
- `artifacts/sweeps/sweep_outputs/<scenario>/cycles.csv`
- `artifacts/sweeps/sweep_outputs/<scenario>/decisions.csv`
- `artifacts/sweeps/sweep_outputs/<scenario>/metrics.csv`

## Sweep 配置格式

支持两种方式：

### 1. 具名场景

```yaml
scenarios:
  - name: baseline
    overrides: {}
  - name: tighter_risk
    overrides:
      exposure.max_abs_exposure_value: 150.0
      stop_loss.stop_loss_cycle_loss: 12.0
```

### 2. 参数网格

```yaml
grid:
  execution.slippage_bps: [5, 10]
  profit_taking.take_profit_fraction: [0.25, 0.35]
```

程序会自动做笛卡尔积组合，并把每组参数写入汇总表的 `overrides_json`。

## 推荐实验顺序

1. 先固定 `execution.slippage_bps` 和 `fee_rate`，避免执行成本噪声掩盖策略差异。
2. 再扫描 `trend.min_trend_strength`、`stop_loss.stop_loss_cycle_loss`、`last_minute.max_tail_exposure`。
3. 观察 `total_net_profit`、`max_drawdown`、`signal_execution_rate` 与各规则触发次数是否稳定。
4. 选出收益和回撤平衡更好的组合，再做更细粒度扫描。

## 自动参数寻优

当参数维度变多时，可以直接使用 `configs/optimize.yaml` 做随机化搜索：

```bash
python scripts/run_auto_optimize.py --input data/raw/polymarket_tracker_collection.xlsx --config configs/strategy.yaml --optimize configs/optimize.yaml --output-dir artifacts/optimization/optimize_outputs
```

输出内容：

- `artifacts/optimization/optimize_outputs/optimization_leaderboard.csv`
- `artifacts/optimization/optimize_outputs/optimization_leaderboard.xlsx`
- `artifacts/optimization/optimize_outputs/best_strategy.yaml`
- `artifacts/optimization/optimize_outputs/best_overrides.json`
- `artifacts/optimization/optimize_outputs/<top_scenario>/replay_report.xlsx`

### Optimize 配置格式

```yaml
trials: 16
seed: 42
export_top_n: 3

score_weights:
  total_net_profit: 1.0
  max_drawdown: -0.65
  total_fees: -0.25
  win_rate: 120.0
  signal_execution_rate: 15.0

parameters:
  trend.min_trend_strength:
    type: float
    min: 0.20
    max: 0.50
    step: 0.05
  execution.slippage_bps:
    type: choice
    values: [5, 10, 15]
```

### 评分逻辑

- `score = Σ(metric * weight)`
- 权重大于 `0` 的指标会被奖励
- 权重小于 `0` 的指标会被惩罚
- 默认会综合考虑 `收益`、`回撤`、`手续费`、`胜率`、`信号执行率`

### 推荐用法

1. 先用较小 `trials` 快速摸底，确认哪些参数最敏感。
2. 再缩小参数范围，提高搜索密度。
3. 最后把 `best_strategy.yaml` 作为新的基线配置，再继续做 sweep 或更细粒度优化。

## 准实时 Paper Trading Runner

当你想观察策略在“持续到来的事件流”上的行为时，可以使用 `scripts/run_live_paper.py`。它会逐条消费事件、按可选节奏回放、打印运行状态，并输出滚动快照。

```bash
python scripts/run_live_paper.py --input data/raw/polymarket_tracker_collection.xlsx --sheet BTC --config configs/strategy.yaml --output-dir artifacts/live/live_outputs_btc --pace-factor 20 --status-every 100
```

关键参数：

- `--pace-factor`
  - `0` 表示不等待，尽快跑完
  - `20` 表示把原始事件间隔压缩为 `1/20`
- `--max-sleep-seconds`
  - 单次等待的上限，避免历史间隔过大时阻塞太久
- `--status-every`
  - 每处理多少条事件打印一次当前 `cycle/net/pnl/cash`
- `--limit`
  - 只消费前 N 条事件，适合快速调试

输出内容：

- `artifacts/live/live_outputs/cycles.csv`
- `artifacts/live/live_outputs/decisions.csv`
- `artifacts/live/live_outputs/metrics.csv`
- `artifacts/live/live_outputs/snapshots.csv`
- `artifacts/live/live_outputs/live_report.xlsx`

其中 `snapshots.csv` 会记录每条事件后的账户现金、净持仓、周期盈亏等滚动状态，适合观察盘中行为。

## 直接接入 Polymarket 实时行情

如果你不想再先整理 Excel，而是直接吃 Polymarket 的真实市场成交流，可以使用 `scripts/run_polymarket_live_paper.py`。

```bash
python scripts/run_polymarket_live_paper.py --slug-prefix btc-updown-5m- --max-cycles 10 --config configs/strategy.yaml --output-dir artifacts/live/polymarket_btc_10cycles
```

关键点：

- `--slug-prefix`
  - 用于自动发现一批活动市场，例如 `btc-updown-5m-`
- `--market-slugs`
  - 如果你已经知道要跟踪哪些 market，可以直接显式传 slug
- `--market-slugs-file`
  - 也可以从文件中读取，每行一个 slug
- `--max-cycles`
  - 最多跟踪多少个周期；适合你这种“先跑 10 个周期看真实盈利和回撤”的需求
- `--env-file`
  - 默认读取项目上一级目录 `APIs/ApiConfig.env`
  - 当前脚本只做读取校验，不会使用私钥去真实下单
  - 若文件中包含 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY` / `WS_PROXY`，会在启动时写入 `os.environ`（值可带引号），供 HTTPS 请求与 WebSocket 走代理

当前实现说明：

- 实时数据来自 Polymarket 公共 `market websocket`
- 使用 `last_trade_price` 事件映射为引擎里的 `TradeEvent`
- 订单执行仍然走本地 `paper broker`，因此手续费、滑点、持仓、盈亏、回撤统计逻辑与离线回放保持一致

### HTTPS / SSL 握手失败（如 `SSLEOFError`）

访问 `gamma-api.polymarket.com` 拉市场列表时若 TLS 被中断，多为网络、代理或防火墙导致。可依次尝试：

1. 配置系统代理后重试（PowerShell 示例）：
   - `$env:HTTPS_PROXY="http://127.0.0.1:7890"`（按你的代理端口改）
2. 重新安装/更新依赖后执行：`pip install -r requirements.txt`（确保含 `requests`）
3. **仅排查用**：跳过证书校验（不安全，勿长期使用）
   - `$env:POLYNET_HTTP_VERIFY="false"`
   - 若 WebSocket 也握手失败：`$env:POLYNET_WS_SSL_VERIFY="false"`
4. 仍不行时换网络或 VPN 后再跑同一命令

输出内容与现有 live runner 基本一致：

- `cycles.csv`
- `decisions.csv`
- `metrics.csv`
- `snapshots.csv`
- `live_report.xlsx`
- `dashboard.html`
- `daily_report.md`

## 监控面板与日报表

现在可以把 `snapshots + decisions + metrics + cycles` 自动生成更适合盯盘的可视化输出。

### 1. 对准实时 runner 自动生成

`scripts/run_live_paper.py` 现在除了导出 `csv/xlsx`，还会自动额外生成：

- `dashboard.html`
- `daily_report.md`
- `dashboard_summary.csv`

适合直接打开 `dashboard.html` 看资金曲线、周期盈亏曲线、规则触发/执行次数、最近决策和最近周期。

### 2. 对已有输出目录补生成

如果目录里已经有这些文件：

- `metrics.csv`
- `cycles.csv`
- `decisions.csv`
- `snapshots.csv` 可选

就可以单独运行：

```bash
python scripts/run_dashboard_report.py --input-dir artifacts/live/live_outputs_btc --title "BTC Live Dashboard"
```

如果你想输出到别的目录：

```bash
python scripts/run_dashboard_report.py --input-dir artifacts/live/live_outputs_btc --output-dir artifacts/live/dashboard_btc --title "BTC Daily Monitoring"
```

## 推荐项目结构

建议长期保持：

- `src/`
  - 代码主目录
- `configs/`
  - 策略、sweep、optimize 配置
- `docs/`
  - 使用说明与策略规格
- `tests/`
  - 单元测试与回放测试
- `data/`
  - `data/raw/`：原始 Excel（本地放置，默认不提交 Git）
  - `data/processed/`：分析脚本输出和处理后的 Excel（同上）
  - 详见：`docs/data_and_artifacts.md`
- `notes/`
  - `notes/strategy/`：策略提示词、尾盘逻辑、买卖规则文档
- `artifacts/`
  - 所有生成产物
  - `artifacts/replays/`：单次离线回放
  - `artifacts/live/`：准实时模拟与 dashboard
  - `artifacts/sweeps/`：批量参数扫描
  - `artifacts/optimization/`：自动参数寻优

### 实盘事件落盘与回放

为避免使用 Data API 事后成交表重建输入流时产生偏差，实时脚本现在会把**实际消费到的 websocket `TradeEvent`** 原样落盘：

- 单周期实盘脚本 `scripts/run_polymarket_cycle_review.py`
  - 输出 `ws_trade_events.ndjson`
  - 输出 `ws_trade_events.csv`
- 实时仿真脚本 `scripts/run_polymarket_live_paper.py`
  - 输出 `ws_trade_events.ndjson`
  - 输出 `ws_trade_events.csv`

推荐优先使用 `ws_trade_events.ndjson` 做离线复盘，因为它保存了引擎真实消费顺序、时间戳、方向与 metadata。

回放命令示例：

```bash
python scripts/replay_recorded_trade_events.py \
  --input artifacts/live/polymarket_cycle_review/account_2/<cycle_slug>/ws_trade_events.ndjson \
  --config configs/strategy.yaml \
  --starting-cash 100 \
  --output artifacts/replays/<cycle_slug>_recorded_event_replay.xlsx
```

这样得到的回放输入会和当时实盘更接近，不再依赖 `market_trades_raw.csv` 这类事后查询结果去反推事件流。

### 面板内容

- 核心指标卡片：净利润、最大回撤、胜率、信号执行率、最新现金、最新净持仓
- 告警视图：高回撤、信号阻塞率过高、尾盘留仓过大、最新周期亏损、近期连续亏损、近期风控拦截密集
- 资金曲线：基于 `snapshots.account_cash`
- 周期盈亏曲线：基于 `cycles.cycle_net_profit`
- 规则触发次数柱状图
- 规则实际执行次数柱状图
- 最近决策表
- 最近周期表

### 告警高亮逻辑

- 高回撤：
  - `max_drawdown >= 20` 触发预警
  - `max_drawdown >= 100` 触发严重告警
- 信号阻塞率：
  - `blocked_signals / total_signals >= 60%` 触发预警
  - `>= 80%` 触发严重告警
- 尾盘留仓：
  - `abs(latest_net_position) >= 20` 或 `abs(latest_exposure_value) >= 20` 触发预警
  - `>= 50` 触发严重告警
- 最新周期亏损会单独提示
- 最近 3 个周期中若至少 2 个亏损，会提示“近期周期连续承压”
- 最近 20 条信号中若至少 15 条被风控拦截，会提示“近期风控拦截密集”

HTML 中会对：

- `risk_status = blocked` 的最近决策行做红色高亮
- `selected_rule` 已触发但未执行的决策做黄色高亮
- `cycle_net_profit < 0` 的周期行做红色高亮

### 日报内容

`daily_report.md` 会给出：

- 核心指标摘要
- 告警视图摘要
- 最佳/最差周期
- 最新市场与最新周期
- 规则触发 Top5
- 规则执行 Top5
