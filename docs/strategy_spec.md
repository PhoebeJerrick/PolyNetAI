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
  - `down_deviation >= take_profit_down_deviation` 时卖出 `down`
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
  - `down_deviation >= mean_reversion_sell_down_deviation` 且持有 `down`
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

- `abs(net_position_value) <= max_abs_exposure_value`
- 买单下单量位于 `[order_sizing.buy.min_order_size, order_sizing.buy.max_order_size]`
- 卖单下单量位于 `[order_sizing.sell.min_order_size, order_sizing.sell.max_order_size]`（当仅剩碎仓且允许强平时，可低于最小值）
- 若配置 `execution.market_limits.use_orderbook_min_order_size=true`，则 orderbook 的 `min_order_size` 会作为市场硬约束（可选对卖单也强制）
- 单周期策略下单次数不超过 `max_strategy_trades_per_cycle`
- 若下单后风险仍超限，则该订单直接拦截

## 第一版默认假设

- 以成交价作为仿真成交价基准。
- 仿真 broker 采用立即成交模型，可叠加固定滑点和手续费。
- 配置文件是单一真源，补充 txt 文档只作为参数与案例来源。

---

# v1.1 更新日志（代码迭代后补充）

## 新增特征定义

### 价格百分位（Price Percentile）

- 定义：`price_percentile = (price - low_price) / (high_price - low_price)`
- 范围：[0, 1]  
- 应用：网格规则判断低位(<=0.25)/高位(>=0.75)

### 信心代理（Confidence Proxy）

- 公式：`confidence_proxy = signal / (signal + volatility)`
- 其中：`signal = abs(cycle_net_profit) + abs(price_move)`
- 应用：Last Minute规则判断是否保留方向性仓位（门槛0.85）

## 新增机制

### Price Feed 缓存机制

- 为每条规则独立维护价格缓存和更新时间戳
- 配置项：`rule_price_feed.{section}.{rule}` (秒数，0=实时)
- 作用：避免规则快速切换，提高稳定性

## 新增规则

### Opening Entry（周期开盘试探建仓）

- 优先级：52
- 触发条件：周期开始30秒内、无策略成交、存在弱势方、时机OK
- 下单量：`base_order_size + volatility_ratio * volatility_order_scale`
- 改进：添加流动性检查 `min_market_trades >= 3`

## 现有规则补充与改进

### Trend Entry

- 原始：下单量按 `base_size + net_position * trend_scale` 计算
- 风险：下单量可能膨胀，需添加上限约束
- 改进：引入 `max_trend_order_size = 80.0` 配置项，实现 `size = min(size, max_trend_order_size)`

### Mean Reversion Entry  

- 原始：双向独立检测，同步时可能同时买入两侧
- 风险：双向同时触发可能放大敞口和风险
- 改进：优先平衡净方向
  - 当 `net_direction = "Up"` 且 `down_deviation <= -threshold` 时，**仅买Down**（平衡优先）
  - 当 `net_direction = "Down"` 且 `up_deviation >= threshold` 时，**仅买Up**（平衡优先）
  - 当净方向平衡或空仓时，允许双向建仓

### Stop Loss Exit

- 说明：仅卖一侧持仓（另一侧在下一时刻处理）✓ 正确

### Hedge Exit

- 原始：双侧盈利时仅返回第一个意图
- 风险：无法完整对冲，另一侧继续持仓
- 改进：返回多个意图列表，支持双侧同步卖出
  - Up盈利时添加Up卖出意图（优先级40）
  - Down盈利时添加Down卖出意图（优先级41）
  - 调用方可能执行其中一个或全部

### Grid Sell  

- 原始：硬编码卖出25%持仓 `_held_up(features) * 0.25`
- 改进：参数化为 `grid.grid_exit_fraction: 0.25`，增强灵活性

### Mean Reversion Sell

- 原始：硬编码卖出40%持仓 `_held_up(features) * 0.4`
- 改进：参数化为 `mean_reversion.mean_reversion_sell_fraction: 0.40`，增强灵活性

## Last Minute补充

### Step 3：双边比例约束

- 机制：当 `preferred_leg_min_ratio > 1.0` 时，强制优势侧最小比例
- 配置：`last_minute.preferred_leg_min_ratio`（默认1.0=禁用）
- 效果：防止end-of-cycle时持仓严重不平衡

## 新增风控约束

### 1. 相邻成交时间约束

- 配置：`execution.min_seconds_between_orders`（默认2.0秒）
- 作用：防止过度频繁交易导致流动性成本增加

### 2. 同方向价格波动约束

- 配置：`execution.min_same_outcome_price_move_ratio`（默认0.03 = 3%）
- 作用：避免价格无意义波动时重复操作同一侧

### 3. 现金利用率约束

- 配置项：
  - `capital.max_cash_utilization`（默认0.95）：最大资本投入比例
  - `capital.min_cash_buffer`（默认25.0）：最小现金缓冲
  - `execution.fee_rate`（默认0.002）：交易费率
  - `execution.slippage_bps`（默认10）：滑点基点
