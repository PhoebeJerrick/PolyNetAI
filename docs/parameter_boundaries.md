# 参数作用边界说明（目标仓位 vs 风险硬约束）

本文用于统一调参口径，避免把“目标仓位参数”和“风险硬约束参数”混用。

## 1) 目标仓位参数（策略意图）

这类参数用于表达策略希望达到的仓位或节奏，不代表系统一定会成交到该水平。

- `position.max_position_value`
  - 含义：策略层“总持仓成本价值”的目标基准。
  - 主要用途：
    - 持仓价值占比：`总持仓成本 / position.max_position_value`
    - 动态阶段阈值：`phase_1_target = position.max_position_value * dynamic_priority.phase_1_position_threshold`
- `dynamic_priority.phase_1_position_threshold`
  - 含义：阶段 1 的仓位比例阈值（相对 `position.max_position_value`）。
- `last_minute.max_tail_exposure`
  - 含义：尾盘希望保留的目标净仓上限（目标值，会继续受风险约束拦截）。
- 各策略规则下单量参数（如 `order_sizing.base_order_size`、`trend.trend_scale`、`mean_reversion.deviation_scale`）
  - 含义：生成“候选订单意图”的规模。

## 2) 风险硬约束参数（执行边界）

这类参数是硬限制，命中后会直接截断或缩小订单，即使目标仓位未达成也不会放行。

- 现金约束
  - `capital.max_cash_utilization`
  - `capital.min_cash_buffer`
  - 作用：限制买单可用资金上限。
- 净敞口约束
  - `exposure.max_abs_exposure_value`
  - `exposure.phase_4_max_abs_exposure_value`
  - 作用：限制下单后绝对净敞口。
- 单笔成交量约束
  - `order_sizing.buy.max_order_size`
  - `order_sizing.sell.max_order_size`
  - 作用：限制单笔下单份数。
- 每周期执行次数约束
  - `exposure.max_strategy_trades_per_cycle`
  - 作用：限制每周期可执行策略成交次数。

## 3) 口径建议

- 调“仓位目标”优先看：`position.max_position_value` 与 `dynamic_priority.phase_1_position_threshold`。
- 调“风险边界”优先看：`capital.*`、`exposure.*`、`order_sizing.*.max_order_size`、`exposure.max_strategy_trades_per_cycle`。
- 当出现“目标仓位上不去”时，先排查是否被风险硬约束拦截，而不是继续放大目标仓位参数。
