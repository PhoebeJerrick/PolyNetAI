# Polymarket 5分钟策略可执行规格

## 目标

该规格将 `notes/strategy/` 下的以下 4 份文档统一为一个可编码版本：

- `notes/strategy/Polymarket 5分钟周期多维度动态对冲交易策略.txt`
- `notes/strategy/最后一分钟得操作逻辑.txt`
- `notes/strategy/买入规则-策略模型.txt`
- `notes/strategy/卖出规则-策略模型.txt`

本规格的输出目标不是直接接入实盘，而是驱动 `paper trading` 仿真闭环。

## 状态字段

仿真引擎在每个周期内维护这些字段：

- `up_balance` / `down_balance`
- `up_avg_price` / `down_avg_price`
- `total_position`
- `net_position`
- `net_direction`
- `net_position_value`
- `up_realized_pnl` / `down_realized_pnl`
- `unrealized_up_pnl` / `unrealized_down_pnl`
- `cycle_net_profit`
- `opening_price` / `last_price` / `high_price` / `low_price`
- `trend_strength`
- `volatility`
- `price_percentile`
- `confidence_proxy`

## 特征定义

### 趋势确认

- 使用最近连续同方向成交次数与当前净持仓方向组合判断。
- 当 `trend_strength >= min_trend_strength` 且 `trend_bias` 非空时，认为市场进入趋势模式。

### 震荡确认

- 当 `trend_strength < min_trend_strength` 且 `abs(net_position) <= max_grid_net_position` 时，认为市场进入网格/震荡模式。

### 均价偏离

- `up_deviation = (price - up_avg_price) / up_avg_price`
- `down_deviation = (price - down_avg_price) / down_avg_price`

### 波动率

- 第一版定义为 `high_price - low_price`
- `volatility_ratio = volatility / opening_price`

### 最后一分钟

- 周期默认 300 秒。
- 当 `elapsed_seconds >= cycle_seconds - last_minute_seconds` 时进入尾盘状态。

## 规则优先级

统一使用以下优先级，从高到低执行：

1. `risk`
2. `last_minute`
3. `stop_loss`
4. `hedge`
5. `take_profit`
6. `grid`
7. `mean_reversion`
8. `trend`

同一时刻如果多个规则同时触发，只执行优先级最高的一个订单意图。

## 买入规则

### Trend

- 触发条件：
  - 非最后一分钟
  - `trend_bias` 非空
  - `trend_strength >= min_trend_strength`
  - 当前价格相对对应方向均价的偏离 `>= trend_price_edge`
- 输出动作：
  - 顺着 `trend_bias` 买入
  - 下单量 = `base_order_size + abs(net_position) * trend_scale + volatility_ratio * volatility_order_scale`

### Hedge

- 触发条件：
  - `abs(net_position_value) >= hedge_trigger_value`
  - 净方向非平衡
- 输出动作：
  - 沿净方向的反方向买入
  - 下单量 = `base_order_size + exposure_excess * hedge_scale`

### Grid

- 触发条件：
  - 市场处于 `range`
  - `abs(net_position) <= max_grid_net_position`
- 输出动作：
  - `price_percentile <= grid_low_percentile` 时买入 `up`
  - `price_percentile >= grid_high_percentile` 时买入 `down`

### Mean Reversion

- 触发条件：
  - `down_deviation <= -down_buy_deviation` 时买入 `down`
  - `up_deviation >= up_buy_deviation` 时买入 `up`
- 输出动作：
  - 下单量按偏离比例线性放大，最大不超过 `max_order_size`

## 卖出规则

### Take Profit

- 触发条件：
  - 对应方向未实现收益为正
  - `up_deviation >= take_profit_up_deviation` 时卖出 `up`
  - `down_deviation <= -take_profit_down_deviation` 时卖出 `down`
- 输出动作：
  - 分批减仓，默认卖出 `take_profit_fraction * 当前持仓`

### Stop Loss

- 触发条件：
  - `cycle_net_profit <= -stop_loss_cycle_loss`
  - 或净敞口超过限制且继续恶化
- 输出动作：
  - 优先卖出当前净方向持仓
  - 若净方向平衡，则卖出浮亏方向持仓

### Hedge Sell

- 触发条件：
  - 净持仓价值绝对值过大
  - 周期净利润为正，允许锁定部分利润
- 输出动作：
  - 卖出盈利方向部分仓位，缩小单边暴露

### Grid Sell

- 触发条件：
  - 市场处于 `range`
  - `price_percentile >= grid_high_percentile` 且持有 `up`
  - 或 `price_percentile <= grid_low_percentile` 且持有 `down`
- 输出动作：
  - 完成网格低买高卖循环

### Mean Reversion Sell

- 触发条件：
  - `up_deviation >= mean_reversion_sell_up_deviation` 且持有 `up`
  - `down_deviation <= -mean_reversion_sell_down_deviation` 且持有 `down`
- 输出动作：
  - 按偏离程度卖出对应方向仓位

## 最后一分钟规则

统一采用四步闭环：

1. 强制平掉亏损方向持仓。
2. 计算 `cycle_net_profit`、`opening_vs_last_move`、`volatility`。
3. 用 `confidence_proxy` 估计“主要盈利方向确认度”。
4. 计算尾盘目标净仓：
   - 方向：
     - `cycle_net_profit >= 0` 时优先沿当前净方向
     - `cycle_net_profit < 0` 时优先沿亏损方向的相反方向
     - 若 `confidence_proxy < last_minute_min_confidence`，则不保留方向性仓位
   - 规模：
     - `target_net = min(max_tail_exposure, tail_profit_scale * abs(cycle_net_profit) + tail_volatility_scale * volatility)`

## 风控硬约束

- `abs(net_position_value) <= max_abs_exposure`
- 单笔下单量位于 `[min_order_size, max_order_size]`
- 单周期策略下单次数不超过 `max_strategy_trades_per_cycle`
- 若下单后风险仍超限，则该订单直接拦截

## 第一版默认假设

- 以成交价作为仿真成交价基准。
- 仿真 broker 采用立即成交模型，可叠加固定滑点和手续费。
- 配置文件是单一真源，补充 txt 文档只作为参数与案例来源。
