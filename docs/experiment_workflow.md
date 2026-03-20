# 参数扫描与实验流程

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
      exposure.max_abs_exposure: 150.0
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
  - `data/raw/`：原始 Excel 数据
  - `data/processed/`：分析脚本输出和处理后的 Excel
- `notes/`
  - `notes/strategy/`：策略提示词、尾盘逻辑、买卖规则文档
- `artifacts/`
  - 所有生成产物
  - `artifacts/replays/`：单次离线回放
  - `artifacts/live/`：准实时模拟与 dashboard
  - `artifacts/sweeps/`：批量参数扫描
  - `artifacts/optimization/`：自动参数寻优

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
