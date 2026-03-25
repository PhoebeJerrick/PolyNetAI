# Polymarket 5分钟策略可执行规格 - 完整版

## 目标

该规格将 `notes/strategy/` 下的以下 4 份文档统一为一个可编码版本：

- `notes/strategy/Polymarket 5分钟周期多维度动态对冲交易策略.txt`
- `notes/strategy/最后一分钟得操作逻辑.txt`
- `notes/strategy/买入规则-策略模型.txt`
- `notes/strategy/卖出规则-策略模型.txt`

本规格的输出目标不是直接接入实盘，而是驱动 `paper trading` 仿真闭环。

**版本迭代说明**：本文档基于约 20+ 次代码迭代更新，补充了原初规格未涵盖的新机制与规则细节。

---

## 状态字段

仿真引擎在每个周期内维护这些字段：

### 持仓与价值
- `up_balance` / `down_balance` - 每个方向的成交累计金额
- `up_avg_price` / `down_avg_price` - 每个方向的成本均价
- `up_held` / `down_held` - 每个方向当前持仓量
- `total_position` - 总持仓（up_held + down_held）
- `net_position` - 净持仓（up_held - down_held）
- `net_direction` - 净方向（"Up"/"Down"/"平衡"/"空仓"）
- `net_position_value` - 净敞口美元价值

### 盈亏
- `up_realized_pnl` / `down_realized_pnl` - 每个方向的已实现盈亏
- `unrealized_up_pnl` / `unrealized_down_pnl` - 每个方向的未实现盈亏
- `cycle_net_profit` - 当前周期累计净利润

### 价格与高低
- `opening_price` - 周期开盘价
- `last_price` / `price` - 最后成交价
- `high_price` / `low_price` - 周期内最高/最低价
- `up_last_price` / `down_last_price` - Up/Down 方向的最后成交价
- `up_market_high` / `up_market_low` - Up 方向市场成交范围
- `down_market_high` / `down_market_low` - Down 方向市场成交范围

### 特征指标
- `trend_bias` - 趋势方向（"up"/"down"/None）
- `trend_strength` - 趋势强度（0-1）
- `volatility` - 波动率（high - low）
- `volatility_ratio` - 归一化波动率（volatility / opening_price）
- `price_percentile` - 当前价格在周期范围内的百分位（0-1）
- `confidence_proxy` - 盈利方向确认度（0-1）
- `market_regime` - 市场体制（"trend"/"range"）

### 市场深度信息
- `up_market_n` / `down_market_n` - 每个方向的市场成交次数
- `up_market_vwap` / `down_market_vwap` - 加权平均价

### 周期与执行
- `cycle_id` - 周期标识
- `timestamp` - 当前事件时间戳
- `cycle_elapsed_seconds` - 周期已耗时（秒）
- `is_last_minute` - 是否进入最后一分钟窗口
- `strategy_trades` - 当前周期已执行策略成交次数

---

## 特征定义

### 趋势确认

**概念**：通过连续同方向市场成交判定并确认趋势方向。

**计算方式**：
```
trend_bias = consecutive_outcome (if consecutive_count >= 2 else None)
trend_strength = consecutive_outcome_count / max(1, market_trades + strategy_trades)
market_regime = "trend" if (trend_strength >= 0.35 OR abs(price_move) > 0.5 * volatility) else "range"
```

其中：
- `consecutive_outcome` - 最近连续同方向成交的方向
- `consecutive_outcome_count` - 最近连续同方向成交的次数

**触发阈值**：
- 当 `trend_strength >= min_trend_strength`（默认 0.35）且 `trend_bias` 非空时，认为市场进入趋势模式
- 此时可触发趋势追踪类规则

### 震荡确认

**概念**：市场缺乏明显趋势方向，价格在一定范围内反复波动。

**触发条件**：
```
is_range_mode = (trend_strength < min_trend_strength(0.35)) AND 
                (abs(net_position) <= max_grid_net_position)
```

**含义**：
- 趋势强度不足
- 净持仓不过大，仍有对冲与加仓空间

### 均价偏离

**计算方式**：
```
up_deviation = (price - up_avg_price) / up_avg_price
down_deviation = (price - down_avg_price) / down_avg_price
```

**应用**：
- 价格相对均价的偏离程度越大，表明趋势方向性越强
- 均值回归规则利用偏离过大时的反向操作

### 波动率

**定义**：
```
volatility = high_price - low_price
volatility_ratio = volatility / opening_price
```

**应用**：
- 用于调整下单量（波动率越大，单次下单量越大）
- 最后一分钟窗口中用于估计尾盘目标仓位

### 价格百分位

**定义**：
```
price_percentile = (last_price - low_price) / (high_price - low_price)
```

**取值范围**：0（最低价）到 1（最高价）

**应用**：网格规则使用，判断是否处于低位（<0.25）或高位（>0.75）

### 信心代理（Confidence Proxy）

**定义**：估计当前盈利方向的确认度
```
signal = abs(cycle_net_profit) + abs(price_move)
confidence_proxy = signal / (signal + volatility)
取值范围：[0, 1]
```

**解释**：
- 分子：周期已收益 + 价格移动量（确定性信号）
- 分母：信号 + 波动率（不确定性）
- 比值高 → 趋势清晰，方向确认度高
- 比值低 → 市场嘈杂，方向不确定

**应用**：最后一分钟规则用于判断是否保留持仓

### 最后一分钟

**定义**：
```
周期默认 300 秒
last_minute_window = cycle_seconds - last_minute_seconds（默认 60 秒）
is_last_minute = (elapsed_seconds >= last_minute_window)
```

**特点**：
- 进入尾盘后，大部分常规规则被禁用
- 优先考虑风险出场与盈利锁定

---

## 规则路由架构

### 规则优先级（从高到低）

统一使用以下优先级，同一时刻如果多个规则同时触发，**只执行优先级最高的一个订单意图**：

| 优先级 | 规则类别 | 优先级值 |
|--------|---------|---------|
| 1 | **风控拦截** (Risk Filter) | - |
| 2 | **最后一分钟** (Last Minute) | 20 |
| 3 | **止损** (Stop Loss) | 30 |
| 4 | **套期保值买** (Hedge Entry) | 40 |
| 5 | **套期保值卖** (Hedge Exit) | 40 |
| 6 | **止盈** (Take Profit) | 50 |
| 7 | **开盘试探** (Opening Entry) | 52 |
| 8 | **网格** (Grid) | 60 |
| 9 | **均值回归** (Mean Reversion) | 70 |
| 10 | **趋势** (Trend) | 80 |

### 规则执行流程

```
FeatureSnapshot 
    ↓
[Price Feed 缓存]（可选延迟价格更新）
    ↓
[三个决策阶段，按顺序生成候选订单]
    ├─ 最后一分钟处理（如适用）
    ├─ 出场规则（止损 → 套保卖 → 止盈 → 网格卖 → 均值卖）
    └─ 入场规则（开盘 → 套保买 → 网格买 → 均值买 → 趋势买）
    ↓
[按优先级排序候选订单]
    ↓
[依次执行风控检查，拦截超限订单]
    ↓
[执行策略成交（限制数量）]
```

---

## Price Feed 机制（新增）

### 设计意图

避免不同规则因价格频繁变动产生不同决策快照，同时支持有意延迟某些规则的价格更新。

### 工作方式

对于每一条规则，维护一个独立的价格缓存与最后更新时间戳：

```
if rule_enabled:
    rule_cache_interval = config.get(f"rule_price_feed.{section}.{rule}", 0.0)
    
    if interval > 0:
        # 延迟更新模式：间隔秒数内使用缓存价格
        if (now - last_update_time) >= interval:
            cache_price = latest_price
            last_update_time = now
        else:
            effective_price = cache_price
    else:
        # 实时模式：直接使用最新价格
        effective_price = latest_price
```

### 配置示例

```yaml
rule_price_feed:
  last_minute: 0.0        # 最后一分钟总是用实时价格
  exits:
    stop_loss: 0.0        # 止损总是用实时价格
    hedge: 2.0            # 套保卖出延迟2秒更新
  entries:
    opening: 0.0          # 开盘总是实时
    trend: 5.0            # 趋势买入延迟5秒更新
```

### 作用

- **趋势规则**延迟可避免频繁追跟单
- **网格规则**延迟可稳定低买高卖
- **止损/最后一分钟**不延迟确保快速反应

---

## 入场规则详解

### Opening Entry（新增 - 周期开盘试探建仓）

**概念**：尽快在周期开始时探明市场偏好方向，抢占均价优势。

**触发条件**：
- `not features.is_last_minute`
- `features.strategy_trades == 0`（本周期无策略成交）
- `features.cycle_elapsed_seconds <= opening_entry.window_seconds`（默认30秒）
- 存在相对弱势方向（基于双边价格或二元补价推断）
- 弱势方的价格满足时机约束

**时机约束检查**：
```
if n == 1: return True  # 仅单次成交，直接介入
if price <= vwap + epsilon: return True  # 价格不高于加权价
if span >= min_range and price <= lo + range_frac * span: return True
    # 区间内成交，且价格处于相对低位
```

参数：
- `vwap_epsilon`（默认0.01）
- `range_low_fraction`（默认0.35）
- `min_range_width`（默认0.02）

**弱势方推断**：
```
if up_price exists and down_price exists:
    weak = "up" if up_price < down_price else "down"
elif only_up exists:
    infer down = 1.0 - up_price
    weak = "up" if up_price < infer_down else "down"
elif only_down exists:
    infer up = 1.0 - down_price
    weak = "down" if infer_up < down_price else "up"
```

**下单量**：
```
size = base_order_size + volatility_ratio * volatility_order_scale
```

**优先级**：52 - 高于网格和趋势，低于风控和尾盘

---

### Trend Entry（趋势跟踪建仓）

**触发条件**：
- `not features.is_last_minute`
- `features.trend_bias is not None`
- `features.trend_strength >= min_trend_strength`（默认0.35）
- `deviation >= trend_price_edge`（默认0.03）

其中 `deviation` 为趋势方向对应的均价偏离。

**下单量**：
```
size = base_order_size + abs(net_position) * trend_scale + 
       volatility_ratio * volatility_order_scale

其中：
- base_order_size（默认5.0）
- trend_scale（默认0.15）- 持仓越多，当前单次越大（追踪加仓）
- volatility_order_scale（默认10.0）
```

**优先级**：80 - 最低，只在其他规则都未触发时执行

---

### Hedge Entry（对冲建仓 - 控制单边敞口）

**触发条件**：
- `not features.is_last_minute`
- `abs(net_position_value) >= hedge_trigger_value`（默认50.0）
- `features.net_direction in {"Up", "Down"}`（非平衡/空仓）

**动作**：沿净方向的反方向建仓，缩小敞口

**下单量**：
```
opposite = "down" if net_direction == "Up" else "up"
excess = max(0, abs(net_position_value) - hedge_trigger_value)
size = base_order_size + excess * hedge_scale

其中：
- hedge_scale（默认0.15）
```

**优先级**：40

---

### Grid Entry（网格低位建仓）

**触发条件**：
- `not rule_disabled_in_cycle_tail(config)`（可通过配置禁用尾盘）
- `not features.is_last_minute`
- `features.market_regime == "range"`
- `abs(net_position) <= max_grid_net_position`（默认20.0）
- 价格在低位或高位：
  - `price_percentile <= grid_low_percentile`（默认0.25）→ 买入Up
  - `price_percentile >= grid_high_percentile`（默认0.75）→ 买入Down

**下单量**：
```
size = base_order_size + volatility_ratio * volatility_order_scale
```

**优先级**：60

---

### Mean Reversion Entry（均值回归建仓）

**启用条件**：`mean_reversion.enabled`（默认true）

**触发条件**：
- `not rule_disabled_in_cycle_tail(config)`
- `not features.is_last_minute`
- 价格偏离均价过大：
  - `up_deviation >= up_buy_deviation`（默认0.10）→ 买入Up
  - `down_deviation <= -down_buy_deviation`（默认0.10）→ 买入Down

**下单量**：
```
size = base_order_size + abs(deviation) * deviation_scale

其中：
- deviation_scale（默认45.0）- 偏离越大，单次越大，追踪回归
```

**优先级**：70

---

## 出场规则详解

### Stop Loss Exit（周期止损）

**触发条件**：
```
if features.cycle_net_profit > -stop_loss_cycle_loss:
    return []  # 未触发止损

stop_loss_cycle_loss = 默认20.0
```

**动作**：
1. 若Up有浮亏（`unrealized_up_pnl < 0`）且有持仓，卖出Up一部分
2. 若Down有浮亏且有持仓，卖出Down一部分

**卖出量**：
```
shares = held * stop_loss_fraction
stop_loss_fraction = 默认0.50（卖出50%）
```

**优先级**：30 - 高于大部分规则，确保快速止损

**特点**：
- 只触发一例（先检查Up，若满足则返回；否则检查Down）
- 在周期累计亏损超过阈值时激活

---

### Take Profit Exit（部分止盈）

**触发条件**（独立判断Up和Down）：

**Up方向**：
- `unrealized_up_pnl > 0`（浮盈为正）
- `up_deviation >= take_profit_up_deviation`（默认0.20）

**Down方向**：
- `unrealized_down_pnl > 0`
- `down_deviation >= take_profit_down_deviation`（默认0.20）

**卖出量**：
```
shares = held * take_profit_fraction
take_profit_fraction = 默认0.35（卖出35%）
```

**优先级**：50

---

### Hedge Exit（对冲减仓）

**触发条件**：
- `not features.is_last_minute`
- `abs(net_position_value) >= hedge_trigger_value`（默认50.0）
- `features.cycle_net_profit > 0`（本周期有浮盈）

**动作**：卖出浮盈方向部分仓位，锁定利润

**卖出量**：
```
shares = min(held, exposure * 0.1)
即：卖出持仓与敞口10%的较小值
```

**优先级**：40

**特点**：
- 仅在有浮盈时才卖出（避免止损卖出）
- 只卖出一侧（遵循优先级，先检查Up）

---

### Grid Exit（网格高位减仓）

**启用条件**：`not rule_disabled_in_cycle_tail(config)`

**触发条件**：
```
market_regime == "range" AND
(
  (price_percentile >= grid_high_percentile AND held_up > 0) OR
  (price_percentile <= grid_low_percentile AND held_down > 0)
)

grid_low_percentile = 默认0.25
grid_high_percentile = 默认0.75
```

**卖出量**：
```
shares = held * 0.25（卖出25% - 硬编码）
```

**优先级**：60

---

### Mean Reversion Exit（均值回归减仓）

**启用条件**：`mean_reversion.enabled`（默认true）

**触发条件**：
- `not rule_disabled_in_cycle_tail(config)`
- Up方向：`held_up > 0` AND `up_deviation >= mean_reversion_sell_up_deviation`（默认0.20）
- Down方向：`held_down > 0` AND `down_deviation >= mean_reversion_sell_down_deviation`（默认0.20）

**卖出量**：
```
shares = held * 0.4（卖出40% - 硬编码）
```

**优先级**：70

---

## 最后一分钟规则详解

### 四步执行流程

**Step 1：强制平亏损持仓**

```python
if unrealized_up_pnl < 0 and up_held > 0:
    return OrderIntent(action="sell", outcome="up", shares=up_held, ...)
if unrealized_down_pnl < 0 and down_held > 0:
    return OrderIntent(action="sell", outcome="down", shares=down_held, ...)
```

若此步返回，尾盘处理结束，所有亏损方向已平掉。

---

**Step 2：方向决策**

当两侧都无亏损时，决定要保留哪个方向的仓位：

```python
min_confidence = config.get("last_minute_min_confidence", 0.85)
if confidence_proxy < min_confidence:
    return []  # 信心不足，不保留方向性仓位，平掉所有

if cycle_net_profit >= 0 and net_direction in {"Up", "Down"}:
    # 有浮盈，优先保留当前净方向
    target_outcome = "up" if net_direction == "Up" else "down"
elif unrealized_up_pnl < unrealized_down_pnl:
    # Down浮盈更高，保留Down
    target_outcome = "down"
else:
    # Up浮盈更高，保留Up
    target_outcome = "up"
```

---

**Step 3：双边比例约束（新增机制）**

可选的优势侧份额比例维持：

```python
ratio = config.get("last_minute.preferred_leg_min_ratio", 1.0)
if ratio > 1.0 + 1e-12:
    # 计算double方（浮盈更高的一侧）
    favored = up/down (whichever has higher unrealized_pnl)
    fav_held = held[favored]
    oth_held = held[other]
    
    if oth_held > 1e-12 and fav_held < ratio * oth_held:
        # 优势侧份额不足，则强制选择优势侧并增加目标仓位
        target_outcome = favored
        target_net = min(max_tail_exposure, max(ratio * oth_held, tail_formula))
```

示例：若 `preferred_leg_min_ratio = 1.5` 且 Down 持仓10，Up持仓5，则强制保留Down并扩大到至少15。

---

**Step 4：尾盘仓位调整**

```python
max_tail_exposure = config.get("max_tail_exposure", 40.0)
tail_profit_scale = config.get("tail_profit_scale", 0.35)
tail_volatility_scale = config.get("tail_volatility_scale", 25.0)

target_net = min(
    max_tail_exposure,
    tail_profit_scale * abs(cycle_net_profit) + 
    tail_volatility_scale * volatility
)

current_held = held[target_outcome]
if current_held < target_net:
    # 不足目标，买入补足
    return OrderIntent(
        action="buy",
        outcome=target_outcome,
        shares=target_net - current_held,
        ...
    )
elif current_held > target_net:
    # 隐含逻辑：由之前的止盈规则或平亏规则处理减仓
    pass
```

### 参数说明

| 参数 | 默认值 | 含义 |
|------|-------|------|
| `last_minute_min_confidence` | 0.85 | 信心阈值，低于此值不保留方向性仓位 |
| `max_tail_exposure` | 40.0 | 尾盘最大敞口，防止过度追踪 |
| `tail_profit_scale` | 0.35 | 盈利对尾盘目标仓位的权重 |
| `tail_volatility_scale` | 25.0 | 波动率对尾盘目标仓位的权重 |
| `preferred_leg_min_ratio` | 1.0 | > 1.0 时激活双边比例约束 |

### 优先级与禁用规则

- **优先级**：20（最高）
- **禁用其他大部分规则**：进入最后一分钟后，趋势、均值、网格、对冲等规则不再触发
- **例外**：风控拦截始终有效

---

## 风控硬约束

### 订单规模约束

```
买单：[order_sizing.buy.min_order_size, order_sizing.buy.max_order_size]
      默认：[2.0, 60.0]

卖单：[order_sizing.sell.min_order_size, order_sizing.sell.max_order_size]
      默认：[2.0, 60.0]

特例：卖出碎仓（closing_out_remaining）时可低于最小值
      条件：allow_close_below_min_order_size = true
```

### 市场最小下单量约束（新增）

```
if execution.market_limits.use_orderbook_min_order_size:
    market_min = orderbook metadata 或 fallback_min_order_size
    effective_min = max(config_min_order_size, market_min)
```

**场景**：Polymarket 上某些市场可能有更严格的最小下单要求。

### 敞口约束

```
abs(net_position_value) <= max_abs_exposure_value
默认值：200.0

约束应用：
- 既存敞口已超限时，仅允许对冲(hedge)和尾盘(last_minute)规则的买入
- 下单后敞口将超限时，同样的异常处理
```

### 成交次数约束

```
strategy_trades_per_cycle <= max_strategy_trades_per_cycle
默认值：12

检查时机：每次决策前均检查
```

### 新增约束（代码中实现，文档未提及）

**相邻成交时间约束**：
```
if min_seconds_between_orders > 0:
    last_fill_time = metadata["last_strategy_fill_at"]
    if (now - last_fill_time) <= min_seconds_between_orders:
        return RiskDecision(False, "下单间隔不足")
        
默认值：2.0秒
```

**相同方向价格波动约束**：
```
if min_same_outcome_price_move_ratio > 0:
    last_fill_price = metadata[f"last_strategy_fill_price_{outcome}"]
    current_price = reference_price
    if abs(current_price - last_fill_price) / last_fill_price <= min_same_outcome_price_move_ratio:
        return RiskDecision(False, "同方向价格波动不足")
        
默认值：0.03（3%）
```

**现金约束**（买单专用）：
```
account_cash = metadata["account_available_cash"]
max_cash_utilization = config.get("capital.max_cash_utilization", 0.95)
min_cash_buffer = config.get("capital.min_cash_buffer", 25.0)

spendable_cash = account_cash * max_cash_utilization - min_cash_buffer
unit_cost = reference_price * (1 + slippage / 10000) * (1 + fee_rate)
affordable_shares = spendable_cash / unit_cost

if affordable_shares < effective_min_order:
    return RiskDecision(False, "现金不足")
```

---

## 订单意图结构

每个规则生成的订单意图包含：

```python
@dataclass
class OrderIntent:
    market_id: str
    cycle_id: str
    outcome: "up" | "down"
    action: "buy" | "sell"
    shares: float
    reference_price: float  # 用于价格参考，不保证成交价
    category: str  # 规则分类：opening, trend, hedge, grid, mean_reversion, 
                   # take_profit, stop_loss, last_minute
    reason: str  # 人工可读的触发说明
    priority: int  # 规则优先级
    metadata: dict  # 额外元数据
```

### 优先级选择机制

当多个规则同时触发时：
1. 按优先级从高到低排序候选订单
2. 逐个执行风控检查
3. **执行第一个通过风控的订单，其他同时触发的规则本轮次被跳过**
4. 下一时刻重新评估所有规则

---

## 已知问题与改进空间

### Issue 1：Hedge Exit 逻辑的对称性缺陷

**问题描述**：
```python
# 当前实现只卖出一侧
if unrealized_up_pnl > 0 and held_up > 0:
    return OrderIntent(...)  # 卖出Up
# 永远不会到达下面这个分支
if unrealized_down_pnl > 0 and held_down > 0:
    return OrderIntent(...)
```

**影响**：
- 双方都有浮盈时，只能卖出一侧
- 某个方向反复浮亏时无法缩小其敞口

**建议修复**：
```python
# 应改为可返回多条意图，或按优先级卖出多个
intents = []
if unrealized_up_pnl > 0 and held_up > 0:
    intents.append(...)
if unrealized_down_pnl > 0 and held_down > 0:
    intents.append(...)
return intents
```

---

### Issue 2：Grid Exit 和 Mean Reversion Exit 的卖出比例硬编码

**问题描述**：
- Grid Exit：固定卖出 25% (`held * 0.25`)
- Mean Reversion Exit：固定卖出 40% (`held * 0.4`)

**影响**：
- 无法灵活适应不同市场条件
- 无法通过配置调整减仓策略

**建议修复**：
```yaml
grid:
  grid_exit_fraction: 0.25

mean_reversion:
  mean_reversion_sell_fraction: 0.40
```

---

### Issue 3：开盘试探建仓未验证流动性

**问题描述**：
Opening Entry 规则仅基于价格关系判断，未考虑市场深度 (`up_market_n`, `down_market_n`)。

**风险**：
- 市场流动性不足时强行建仓，可能无法快速对冲

**建议修复**：
```python
min_market_trades_for_opening = 3  # 至少有3笔市场成交
if weak == "up" and features.up_market_n < min_market_trades_for_opening:
    return []
```

---

### Issue 4：Trend Entry 下单量与已有持仓的相互放大

**问题描述**：
```python
size = base_order_size + abs(net_position) * trend_scale
# 当持仓已很大时，下单量快速膨胀
```

**影响**：
- 单笔下单量可能超过预期，尤其在多次连续趋势确认时
- 可能触发敞口约束被拦截

**建议修复**：
```python
# 使用对数衰减或上限约束
max_trend_order_size = 80.0
size = min(max_trend_order_size, base_order_size + abs(net_position) * trend_scale)
```

---

### Issue 5：Mean Reversion 的双向同时触发

**问题描述**：
```python
# 当 up 和 down 同时偏离时，两个意图都被生成
if up_deviation >= up_buy_deviation:
    intents.append(OrderIntent(..., outcome="up", ...))
if down_deviation <= -down_buy_deviation:
    intents.append(OrderIntent(..., outcome="down", ...))
return intents
```

**影响**：
- 两个订单可能都通过风控并执行
- 意图是缩小敞口，反而可能放大敞口

**建议修复**：
```python
# 优先平衡净方向
if net_direction == "Up" and down_deviation <= -down_buy_deviation:
    intents.append(OrderIntent(..., outcome="down", ...))
elif net_direction == "Down" and up_deviation >= up_buy_deviation:
    intents.append(OrderIntent(..., outcome="up", ...))
else:
    # 仅在平衡时才双向建仓
    if up_deviation >= up_buy_deviation:
        intents.append(...)
    if down_deviation <= -down_buy_deviation:
        intents.append(...)
```

---

### Issue 6：Price Feed 缓存的过期处理

**问题描述**：
当某个规则的缓存价格因长期未更新而显著滞后时，无主动失效机制。

**建议修复**：
```python
max_cache_age = config.get("rule_price_feed.max_cache_age_seconds", 30.0)
if (now - last_update_time).total_seconds() > max_cache_age:
    # 强制刷新缓存
    cache_price = latest_price
    last_update_time = now
```

---

### Issue 7：Last Minute 的 confidence_proxy 计算可能过于乐观

**问题描述**：
```python
confidence = signal / (signal + volatility)
# 当信号很小但波动率也很小时（比如0.01信号+0.02波动），
# confidence = 0.01 / 0.03 ≈ 0.33，远低于0.85阈值
# 反之，在高噪声市场，signal 和 volatility 都很大时，
# 比值反而趋近于原始信噪比，可能高估确定性
```

**建议改进**：
```python
# 添加最小阈值或加权调整
confidence = signal / (signal + volatility)
confidence = max(confidence, base_confidence_floor)
# 或者考虑夏普比：
confidence = price_move / volatility  # 与波动率的正相关关系
```

---

## 配置示例

### 最小可配置集合

```yaml
# 策略启用
strategy:
  enabled: true

# 时间配置
cycle:
  cycle_seconds: 300
  last_minute_seconds: 60

# 订单规模
order_sizing:
  base_order_size: 5.0
  volatility_order_scale: 10.0
  
  buy:
    min_order_size: 2.0
    max_order_size: 60.0
  
  sell:
    min_order_size: 2.0
    max_order_size: 60.0

# 规则优先级
priorities:
  opening: 52
  trend: 80
  hedge: 40
  grid: 60
  mean_reversion: 70
  
  take_profit: 50
  stop_loss: 30
  last_minute: 20

# 趋势规则
trend:
  min_trend_strength: 0.35
  trend_price_edge: 0.03
  trend_scale: 0.15

# 对冲规则
exposure:
  hedge_trigger_value: 50.0
  hedge_scale: 0.15
  max_grid_net_position: 20.0
  max_abs_exposure_value: 200.0

# 网格规则
grid:
  grid_low_percentile: 0.25
  grid_high_percentile: 0.75

# 均值回归
mean_reversion:
  enabled: true
  up_buy_deviation: 0.10
  down_buy_deviation: 0.10
  deviation_scale: 45.0
  mean_reversion_sell_up_deviation: 0.20
  mean_reversion_sell_down_deviation: 0.20

# 止盈止损
profit_taking:
  take_profit_fraction: 0.35
  take_profit_up_deviation: 0.20
  take_profit_down_deviation: 0.20

stop_loss:
  stop_loss_cycle_loss: 20.0
  stop_loss_fraction: 0.50

# 开盘建仓
opening_entry:
  enabled: true
  window_seconds: 30.0
  infer_missing_with_binary_complement: true
  vwap_epsilon: 0.01
  range_low_fraction: 0.35
  min_range_width: 0.02

# 最后一分钟
last_minute:
  last_minute_min_confidence: 0.85
  max_tail_exposure: 40.0
  tail_profit_scale: 0.35
  tail_volatility_scale: 25.0
  preferred_leg_min_ratio: 1.0

# 风控
execution:
  fee_rate: 0.002
  slippage_bps: 10.0
  min_seconds_between_orders: 2.0
  min_same_outcome_price_move_ratio: 0.03
  max_strategy_trades_per_cycle: 12
  
  market_limits:
    use_orderbook_min_order_size: true
    fallback_min_order_size: 0.0
    enforce_sell_min_order_size: true

capital:
  max_cash_utilization: 0.95
  min_cash_buffer: 25.0

# Price Feed 缓存（可选）
rule_price_feed:
  last_minute: 0.0
  entries:
    opening: 0.0
    trend: 0.0
    hedge: 0.0
    grid: 0.0
    mean_reversion: 0.0
  exits:
    stop_loss: 0.0
    hedge: 0.0
    take_profit: 0.0
    grid: 0.0
    mean_reversion: 0.0
```

---

## 第一版默认假设

- 以成交价作为仿真成交价基准
- 仿真 broker 采用立即成交模型，可叠加固定滑点和手续费
- 配置文件是单一真源，补充 txt 文档只作为参数与案例来源

---

## 版本演进记录

### v0.1（原始规格）
- 定义了 8 大规则和优先级
- 基础特征与风控约束

### v1.0（代码迭代后）
- 新增 Opening Entry 规则
- 完整的 Price Feed 缓存机制
- 详细解析所有规则的代码实现
- 发现并记录 7 项已知问题
- 新增多项风控约束
- 补充信心代理与其他特征的精确计算公式

