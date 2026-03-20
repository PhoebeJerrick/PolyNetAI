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
│  ├─ raw/              # 原始 Excel 数据
│  └─ processed/        # 处理后 Excel 数据
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
- `scripts/run_dashboard_report.py`
  - 从已有输出目录补生成 dashboard 和日报
- `analyze_polymarket_tracker.py`
  - 旧版 Excel 持仓/盈亏分析工具，仍可单独使用

## 常用命令

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

### 5. 补生成 dashboard

```bash
python scripts/run_dashboard_report.py --input-dir artifacts/live/live_outputs_btc --title "BTC Live Dashboard"
```

### 6. 原始 Excel 分析

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
- 中文策略原文：`notes/strategy/`

## 测试

```bash
python -m pytest -q
```

当前项目已经包含状态引擎、策略路由、参数扫描、自动寻优、准实时 runner、dashboard 等测试。
