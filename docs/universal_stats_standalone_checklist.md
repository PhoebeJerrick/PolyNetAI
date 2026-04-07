# Universal Stats 独立工程落地清单

目标：把当前仓库里的万能统计机器人单独拧成一个最小可维护工程，并保留与现有 PolyNetAI 的兼容关系。

## 推荐目录结构

```text
polymarket-stats-bot/
├─ pyproject.toml
├─ README.md
├─ configs/
│  └─ example.conf
├─ src/
│  └─ universal_stats_bot/
│     ├─ __init__.py
│     ├─ cli.py
│     ├─ core.py
│     ├─ io.py
│     ├─ models.py
│     └─ time_window.py
└─ tests/
   └─ test_universal_stats.py
```

## 每个文件放什么

### pyproject.toml

- 定义项目名，例如 polymarket-stats-bot
- Python 版本建议 >=3.11
- 依赖最小化为：
  - pandas>=2.2
  - PyYAML>=6.0

### README.md

- 说明输入数据格式：json/jsonl/ndjson，必须包含 cycle_id、timestamp、price、shares、outcome、action
- 给出最短启动命令：
  - python -m universal_stats_bot.cli --config configs/example.conf
- 给出一个完整配置样例
- 说明输出文件：主结果 CSV/JSON + summary JSON

### configs/example.conf

- 单独的统计配置，不再和任何交易/批量回放配置混写
- 建议保留一份默认前 120 秒窗口的样例

### src/universal_stats_bot/models.py

- 只放 TradeEvent 数据类
- 保持纯数据模型，不引入 pandas 或 CLI 逻辑

### src/universal_stats_bot/io.py

- 只放事件流读写逻辑：
  - trade_event_from_record
  - trade_event_to_record
  - load_recorded_trade_events
  - CycleTradeEventRecorder
- 不要混进任何统计规则

### src/universal_stats_bot/time_window.py

- 只放 Polymarket 周期时间解析逻辑：
  - cycle_seconds_from_market_slug
  - parse_window_start_epoch_from_slug
  - window_start_naive_utc_from_slug

### src/universal_stats_bot/core.py

- 放统计主逻辑：
  - 配置解析
  - 递归扫描输入目录
  - 按 cycle_id 分组
  - 子区间裁剪
  - 基础指标与自定义指标
  - winner 判定
  - 相关性分析
  - 条件占比汇总
  - 输出写文件

### src/universal_stats_bot/cli.py

- 保持为很薄的一层
- 只负责：
  - 解析 --config
  - 调用 load_stats_config
  - 调用 run_universal_stats
  - 打印 render_console_report

### src/universal_stats_bot/__init__.py

- 导出对外稳定 API：
  - load_stats_config
  - run_universal_stats
  - render_console_report
  - TradeEvent

### tests/test_universal_stats.py

- 至少保留这 3 类测试：
  - 配置解析：batch.conf 风格、多行 conditions、自定义指标
  - 时间窗口：relative_start、relative_end
  - winner 判定：last_price、external_file

## 推荐拆分步骤

1. 先复制当前 src/universal_stats_bot/ 目录到新工程。
2. 再复制 tests/test_universal_stats.py 作为基础回归测试。
3. 新建 configs/example.conf，不要复用 PolyNetAI 里的 batch.conf。
4. 跑 pytest -q，确认迁移后测试仍通过。
5. 最后再接入你新工程自己的命令入口或调度方式。

## 当前仓库里的兼容策略

- 当前 PolyNetAI 已经把统计核心迁到 src/universal_stats_bot/
- 原路径 src/polynet_ai/reporting/universal_stats.py 现在只是兼容包装层
- 这意味着你后续要独立拆分时，优先复制 src/universal_stats_bot/，不要再从 polynet_ai/reporting/ 抽代码

## 不建议带走的文件

- record.sh：它是当前仓库的统一调度入口，不适合独立工程复用
- configs/batch.conf：它和现有 mstart 语义混在一起，只适合本仓库
- polynet_ai/strategy、polynet_ai/engine、polynet_ai/execution：统计机器人不依赖这些目录