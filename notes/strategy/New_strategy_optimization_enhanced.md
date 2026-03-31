# Polymarket 5分钟周期多维度动态对冲交易策略

> **版本**：v4.0（最终实施版）| **更新时间**：2026-03-29  
> **代码配置**：`configs/strategy.yaml`  
> **对应代码审计**：`notes/strategy/strategy_audit_improvement_plan.md`

---

## 一、策略概述

### 背景

本策略针对 Polymarket 上单个预测市场的 5 分钟交易周期，对 UP/DOWN 两个方向同时建仓，通过动态对冲、分阶段仓位管理与多维度规则协同，在一个周期内实现稳定盈利或控制亏损，并于周期结束前完成仓位收敛。

### 核心机制

1. **双边建仓**：同时持有 UP 和 DOWN 两个方向的仓位，通过价差与价格回归获利
2. **四阶段框架**：将 5 分钟周期细分为 4 个时间窗口，每阶段有明确的仓位目标
3. **规则优先级系统**：多个交易规则并存，通过优先级仲裁，确保风险控制最优先
4. **最后一分钟收仓**：周期末强制清理亏损方向，保留优势方向敞口，并允许尾部加仓

### 设计原则

- **先劣势后优势**：开盘优先建仓价格较低（弱势）一侧，获取价差保护
- **减仓不亏损**：网格减仓仅允许在浮盈 ≥ 0 时执行，不接受亏损减仓
- **风险控制优先**：任何规则均不得突破每日亏损限额、仓位上限、敞口上限

---

## 二、基础约束

### 仓位管理

| 约束项 | 数值 |
|-------|-----|
| 总仓位上限（资金利用率） | 85 USDT（85%，账户 100 USDT） |
| 最小订单量 | 5 份 |
| 单笔最大订单量 | 45 份 |
| 最大净敞口 | 40 USDT |

### 资金约束

| 约束项 | 数值 |
|-------|-----|
| 最小现金缓冲 | 12 USDT（始终保留） |
| 手续费率 | 0.2% |
| 滑点 | 5 bps |

### 特殊规则

- 开盘 post_window_start_delay_seconds 秒后才允许开仓（避免开盘剧烈波动）
- 若剩余仓位本身低于 5 份（即最小订单量），允许全量平仓

---

## 三、风险控制参数

### 每日风险管理

| 参数 | 值 |
|-----|---|
| 每日亏损限额 | 50 USDT |
| 警告阈值 | 限额 70%（亏损 35 USDT）时预警 |

**仓位缩减机制**：

| 累计亏损区间 | 仓位比例 |
|------------|---------|
| 0% – 70% 限额 | 正常（100%） |
| 70% – 100% 限额 | 半仓（50%） |
| > 100% 限额 | 暂停（0%） |

### 周期止损机制（四阶段动态系统）

**机制1 — 周期累计亏损熔断（全阶段统一）**

```
触发：cycle_net_profit < -18 USDT
操作：对亏损仓位强制平仓 50%（stop_loss_fraction）
```

**机制2 — 单仓止损，按周期阶段动态配置（双条件互斥）**

```
条件A（价格接近0）：当前价格 ≤ phase_N_near_zero_price
  → 立即全平，无时间门槛（方向已判定失败）

条件B（浮亏阈值）：浮亏% ≥ 阶段阈值 AND 已持仓时间 ≥ 最小持仓秒数
  → 全平（时间门槛防止入场瞬间噪声误触）
```

| 阶段 | 时间窗口 | 浮亏阈值（条件B） |
|-----|---------|----------------|
| 第一阶段 | 0 – 70s | ≥ 15% |
| 第二阶段 | 70 – 160s | ≥ 12% |
| 第三阶段 | 160 – 240s | ≥ 12% |
| 第四阶段 | 240 – 290s | ≥ 8%（最激进） |

**机制3 — 高波动绝对价格止损（按阶段配置）**

```
触发：volatility_ratio > phase_N_high_vol_trigger_ratio
      AND 当前价格 ≤ phase_N_high_vol_price_threshold
操作：全平（仅在机制2未触发时执行，避免重复止损）
```

### 敞口控制

| 参数 | 值 |
|-----|---|
| 对冲触发阈值 | 净敞口 > 12 USDT |
| 对冲规模 | base_size(5) + (净敞口 - 12) × hedge_scale(0.05) |
| 最大净敞口 | 40 USDT（硬限制） |

### 交易频率限制

| 参数 | 值 |
|-----|---|
| 最大交易次数 | 80 次/周期（`max_strategy_trades_per_cycle`，Last Minute 豁免） |
| 同方向订单最小间隔 | ≥ 2 秒（按 UP/DOWN 方向独立计时；Hedge 类订单豁免） |
| 同方向再入场价格波动要求（仅 buy 单）| ≥ 0.5% |

---

## 四、四阶段交易框架

| 阶段 | 时间窗口 | 核心目标 | 主要操作 |
|-----|---------|---------|---------|
| 第一阶段 | 0 – 70s | 双边低吸建仓到 65% | 加仓为主 |
| 第二阶段 | 70 – 160s | 网格减仓到 50% | 减仓为主 |
| 第三阶段 | 160 – 240s | 顺势加仓 + 对冲 | 加仓 + 对冲 |
| 第四阶段 | 240 – 290s | 确认方向 + 收仓 | 定向加仓 + 清仓 |

---

### 第一阶段：0–70s — 双边低吸建仓

**阶段目标**：使 UP+DOWN 两个方向的总持仓达到目标总仓位的 65%，且两个方向的仓位差值不超过 30%（可配置）。

**如何实现加仓到 65%**

本阶段由 Opening Entry 与 Mean Reversion Entry 协同完成：

**① Opening Entry（优先级 52，开盘试探）**

- 时间窗口：开盘 10–30 秒内触发
- 识别弱势方向（价格 < 0.5 的一侧），从弱势方向买入
- **2 道确认门**，全部通过方执行：
  - ① 流动性：弱势方向成交笔数 ≥ `min_market_trades`（默认 3）
  - ② 价格时机：价格 ≤ VWAP+ε 或处于价格区间低 35% 分位
- 头寸 = `_base_size()` = `base_order_size(5)` + `volatility_ratio × volatility_order_scale(5)`

**② Mean Reversion Entry（优先级 70，均值回归补仓）**

- 价格偏离均价 ≥ 0.1 时触发
- 当 |净持仓| > 30 份时，优先买入反向以平衡仓位
- 头寸 = `base_size(5)` + `偏离度 × deviation_scale(45)`

**③ 仓位平衡约束（全阶段生效）**

两方向仓位差不超过 30%，超出时不允许进一步扩大单边。净敞口 > 12 USDT 会触发 Hedge Entry 自动对冲。

---

### 第二阶段：70–160s — 网格减仓

**阶段目标**：UP/DOWN 两方向各自以减仓为主，最多将持仓减至第一阶段总持仓的 50%。**减仓原则：仅在浮盈 ≥ 0 时执行，不接受亏损减仓**。

**如何实现减仓为主**

**① Grid Exit（优先级 60，网格出场）**

- 高位卖 UP：价格分位数 ≥ 0.75
- 低位卖 DOWN：价格分位数 ≤ 0.25
- 卖出规模：25% 持仓
- **仅在单仓浮盈 ≥ 0 时执行**（不接受亏损减仓）

**② Take Profit Exit（优先级 50，止盈出场）**

- 触发条件：实际价格盈利 > 0 且 价格偏离 ≥ 0.2
- 卖出规模：30% 持仓（`take_profit_fraction`）

**③ Stop Loss Exit（优先级 30，止损保护）**

- 周期累计亏损 > 18 USDT 触发熔断：强制平仓亏损仓位的 50%
- 单仓浮亏达到阶段阈值：全平（详见第三节）
- 此为例外，允许亏损减仓（止损目的）

**④ 加仓辅助**

- Mean Reversion Entry 在此阶段仍可触发（需周期剩余 > 80 秒）
- Grid Entry 在震荡市场仍可触发（需周期剩余 > 50 秒）

---

### 第三阶段：160–240s — 顺势加仓 + 对冲建仓

**阶段目标**：

1. 趋势强度 ≥ 0.5 时顺势加仓获胜方向
2. 劣势方向网格低位加仓，摊平成本
3. 整体加仓量 > 减仓量；总仓位上限 85 USDT

**如何实现加仓为主**

**① Trend Entry（优先级 80，趋势跟随加仓）**

- 触发：趋势强度 ≥ 0.5 且 价格偏离 ≥ 0.03
- 加仓方向：当前趋势的获胜方向
- 头寸 = `base_size(5)` + `|净持仓| × trend_scale(0.1)`，上限 80 份

**② Grid Entry（优先级 60，网格摊平劣势方向）**

- 仅在震荡市场（`market_regime = "range"`）触发
- 低位买 UP（分位数 ≤ 0.25）、高位买 DOWN（分位数 ≥ 0.75）
- 头寸 = `_base_size()` = `base_order_size(5)` + `volatility_ratio × volatility_order_scale(5)`
- 周期剩余 ≤ 50 秒时禁用

**③ Hedge Entry（优先级 40，净敞口对冲）**

- 触发：净敞口 > 12 USDT
- 对冲头寸 = `base_size(5)` + `(净敞口 - 12) × hedge_scale(0.05)`

**④ 仓位上限约束**

- UP+DOWN 合计投注成本 ≤ 85 USDT
- 最大净敞口：40 USDT

---

### 第四阶段：240–290s — 确认方向 + 收仓

**阶段目标**：确认获胜方向后坚定加仓；周期最后 30 秒强制执行 Last Minute Strategy 完成收仓。

**获胜方向判断**：

优势侧（获胜方向）判断条件（满足其一）：

- 该方向份额 ≥ 另一侧 × `preferred_leg_min_ratio(1.2)`
- 或该方向市价较高

**确认后操作**

**① Trend Entry（优先级 80，坚定加仓）**：趋势方向持续加仓，上限 80 份

**② Grid Entry/Exit 禁用**：周期剩余 ≤ 50 秒时禁用，避免末期产生不必要的双边仓位

**③ Last Minute Strategy（优先级 20，最后 30 秒强制执行）**：详见第六节

---

## 五、规则优先级系统

当多个规则同时触发时，按优先级数字从小到大依次执行（数字越小越优先）：

| 优先级 | 规则名称 | 说明 |
|:------:|---------|-----|
| 10 | Risk Control | 每日限额 / 资金 / 敞口硬约束 |
| 20 | Last Minute Strategy | 周期末强制收仓，含尾部加仓 |
| 30 | Stop Loss Exit | 止损保护，优先于盈利操作 |
| 40 | Hedge Entry / Hedge Exit | 净敞口对冲，控制单边风险 |
| 50 | Take Profit Exit | 止盈兑现，锁定浮盈 |
| 52 | Opening Entry | 开盘试探建仓 |
| 60 | Grid Entry / Grid Exit | 网格交易，震荡市获利 |
| 70 | Mean Reversion Entry / Exit | 均值回归，价格偏离时建/平 |
| 80 | Trend Entry | 趋势跟随，加仓明确趋势方向 |

**关键逻辑说明**：

- Risk Control（10）始终优先，任何规则不得突破每日限额、资金、敞口限制
- Last Minute（20）在最后 30 秒接管所有常规交易决策，并可执行尾部加仓
- Stop Loss（30）优先于 Take Profit（50），确保先止损再止盈
- Hedge（40）优先于常规入场，净敞口超标时立即对冲
- Opening Entry（52）在开盘窗口内优先于 Grid 和 Mean Reversion

---

## 六、各规则详解

### 入场规则（Entry Rules）

#### 1. Opening Entry（开盘试探建仓，优先级 52）

**文件**：`src/polynet_ai/strategy/entry_rules.py`

**触发条件**：

- 周期前 30 秒内
- 识别弱势一侧（价格 < 0.5 的方向）
- **2 道确认门**（全部通过方执行）：
  - 流动性：弱势方向成交笔数 ≥ `min_market_trades`（默认 3）
  - 价格时机：价格 ≤ VWAP + `vwap_epsilon(0.01)` 或处于低 35% 分位（`range_low_fraction`）

**头寸计算**：

```
size = base_order_size(5) + volatility_ratio × volatility_order_scale(5)
```

> **设计说明**：开盘试探以价格+流动性为核心依据，头寸随波动率动态调整；去掉了波动率上限过滤和置信度评分，减少误拒率，让开盘期的建仓机会充分利用。

---

#### 2. Hedge Entry（对冲建仓，优先级 40）

**文件**：`src/polynet_ai/strategy/entry_rules.py`

**触发条件**：净敞口 > `hedge_trigger_value(12 USDT)`

**头寸计算**：

```
excess = |净持仓价值| - 12
对冲头寸 = base_size(5) + excess × hedge_scale(0.05)
对冲方向：买入反向头寸，平衡敞口
```

---

#### 3. Grid Entry（网格建仓，优先级 60）

**文件**：`src/polynet_ai/strategy/entry_rules.py`

**触发条件**：

- 市场状态 = `"range"`（震荡，trend_strength < 0.50）
- 低位买 UP：价格分位数 ≤ 0.25
- 高位买 DOWN：价格分位数 ≥ 0.75
- 周期剩余时间 > 50 秒

**头寸计算**：

```
size = base_order_size(5) + volatility_ratio × volatility_order_scale(5)（动态）
```

---

#### 4. Mean Reversion Entry（均值回归建仓，优先级 70）

**文件**：`src/polynet_ai/strategy/entry_rules.py`

**触发条件**：

- 价格偏离 ≥ 0.1（`up_buy_deviation` / `down_buy_deviation`）
- 周期剩余时间 > 80 秒

**改进逻辑**：

- 当 |净持仓| > 30 份时，优先买反向以平衡仓位
- 持仓平衡后，才允许双向继续建仓

**头寸计算**：

```
size = base_size(5) + 偏离度 × deviation_scale(45)
```

---

#### 5. Trend Entry（趋势跟随建仓，优先级 80）

**文件**：`src/polynet_ai/strategy/entry_rules.py`

**触发条件**：

- 趋势强度 ≥ `min_trend_strength(0.5)`
- 价格偏离 ≥ `trend_price_edge(0.03)`

**头寸计算**：

```
size = base_size(5) + |net_position| × trend_scale(0.1)
上限：max_trend_order_size(80 份)
```

其中 `net_position = up_balance - down_balance`（正为净多 UP，负为净多 DOWN，绝对值越大追加越多）。

---

### 出场规则（Exit Rules）

#### 1. Stop Loss Exit（止损出场，优先级 30）

**文件**：`src/polynet_ai/strategy/exit_rules.py`

**三层止损机制**：

**机制1 — 周期累计亏损熔断**：

```
触发：cycle_net_profit < -18 USDT
操作：亏损仓位强制平仓 50%（stop_loss_fraction）
```

**机制2 — 单仓止损（四阶段双条件，优先条件A）**：

```
条件A：价格 ≤ near_zero_price → 立即全平（方向已失败）
条件B：浮亏% ≥ 阶段阈值 AND 已持仓 ≥ 最小持仓秒数 → 全平
```

| 阶段 | 浮亏阈值 |
|-----|---------|
| 第一阶段（0–70s） | 15% |
| 第二阶段（70–160s） | 12% |
| 第三阶段（160–240s） | 12% |
| 第四阶段（240–290s） | 8% |

**机制3 — 高波动绝对价格止损（仅在机制2未触发时执行）**：

```
触发：volatility_ratio > 阶段触发比率 AND 价格 ≤ 阶段价格下限
操作：全平
```

---

#### 2. Take Profit Exit（止盈出场，优先级 50）

**文件**：`src/polynet_ai/strategy/exit_rules.py`

**触发条件**：实际价格盈利 > 0 且 价格偏离 ≥ `take_profit_deviation(0.20)`

**止盈规模**：`take_profit_fraction(30%)` 持仓兑现利润

---

#### 3. Hedge Exit（对冲出场，优先级 40）

**文件**：`src/polynet_ai/strategy/exit_rules.py`

**触发条件**：净敞口 > 12 USDT 且 周期盈利 > 0

**卖出规模**：盈利方向的 10% 敞口

---

#### 4. Grid Exit（网格出场，优先级 60）

**文件**：`src/polynet_ai/strategy/exit_rules.py`

**触发条件**：

- 高位卖 UP：价格分位数 ≥ 0.75，**且** UP 浮盈 ≥ 0
- 低位卖 DOWN：价格分位数 ≤ 0.25，**且** DOWN 浮盈 ≥ 0

**卖出规模**：`grid_exit_fraction(25%)` 持仓

> **约束说明**：仅在单仓浮盈 ≥ 0 时执行，严格执行"减仓不亏损"原则。

---

#### 5. Mean Reversion Exit（均值回归出场，优先级 70）

**文件**：`src/polynet_ai/strategy/exit_rules.py`

**触发条件**：价格偏离 ≥ `mean_reversion_sell_deviation(0.20)`

**卖出规模**：`mean_reversion_sell_fraction(40%)` 持仓

---

### 最后一分钟策略（Last Minute Strategy）

**文件**：`src/polynet_ai/strategy/last_minute.py`  
**优先级**：20（仅次于风险控制）  
**触发时间**：周期最后 30 秒（`last_minute_seconds`）  
**执行前提**：置信度 ≥ `last_minute_min_confidence(0.9)`

**执行逻辑（按优先级依次判断）**：

**步骤1 — 强制平掉亏损方向**

```
UP 方向亏损 → 卖出所有 UP 持仓
DOWN 方向亏损 → 卖出所有 DOWN 持仓
```

**步骤2 — 按优势侧调整留仓（含尾部加仓）**

计算目标净仓：

```python
target_net = min(
    max_tail_exposure(25 USDT),
    tail_profit_scale(0.35) × cycle_profit + tail_volatility_scale(14) × volatility
)
```

优势侧判断（满足其一）：

- 优势侧份额 ≥ 另一侧 × `preferred_leg_min_ratio(1.2)`
- 平局时：UP/DOWN 市价较高者为优势侧

留仓调整：

| 当前状态 | 操作 |
|---------|-----|
| 当前净仓 > 目标净仓 | 卖出超额部分 |
| 当前净仓 < 目标净仓 | 买入优势方向（**尾部加仓**） |
| 当前净仓 = 目标净仓 | 保持当前仓位 |

> **设计说明**：Last Minute 不仅做减仓清理，当优势方向净仓低于目标时，还会主动加仓锁定尾部方向性收益（尾部加仓）。

---

### 市场状态识别（Market Regime）

**文件**：`src/polynet_ai/strategy/features.py`

| 状态 | 触发条件 |
|-----|---------|
| `"trend"`（趋势） | `trend_strength ≥ 0.50` 或 `abs(price_move) > volatility × 0.5` |
| `"range"`（震荡） | 以上条件均不满足 |

**对各规则的影响**：

- `"range"` 状态：Grid Entry / Exit 允许触发
- `"trend"` 状态：Grid 被禁用；Trend Entry 需 `trend_strength ≥ 0.50`（阈值对齐，消除规则死区）

---

### 波动率调整机制（Volatility Adjustment）

```
volatility_ratio = 当前波动率 / 历史平均波动率
```

| 影响规则 | 方式 |
|---------|-----|
| Opening / Grid Entry 头寸 | `size += volatility_ratio × volatility_order_scale(5)` |
| Stop Loss（机制3） | 高波动时触发更严格的绝对价格止损 |
| Last Minute 目标净仓 | `target_net += tail_volatility_scale(14) × volatility` |

---

## 七、执行约束（订单验证）

每笔订单在发出前须通过以下全部检查：

1. **订单规模检查**：5 份 ≤ 订单量 ≤ 45 份
2. **下单间隔检查**：同方向订单间隔 ≥ min_seconds_between_orders 秒
3. **价格波动检查（仅 buy 单）**：同方向上次买入成交后价格变化 ≥ 0.5%（卖出单豁免）
4. **敞口检查**：预计下单后净敞口 ≤ 40 USDT
5. **资金检查**：可用资金 ≥ 最小订单所需资金（5 份 × 当前价格）
6. **仓位检查（卖出）**：可卖份数 ≥ 订单份数

**例外**：允许低于最小订单量的强制全平（剩余仓位 < 5 份时）

---

## 八、配置参数参考

**配置文件**：`configs/strategy.yaml`

| 参数名称 | 当前值 | 说明 |
|---------|:------:|-----|
| `max_cash_utilization` | 0.85 | 最大资金利用率 |
| `base_order_size` | 5 | 基础订单份数 |
| `max_single_order_size` | 45 | 单笔最大订单份数 |
| `min_cash_buffer` | 12 USDT | 最小现金缓冲 |
| `daily_loss_limit` | 50 USDT | 每日亏损限额 |
| `stop_loss_cycle_loss` | 18 USDT | 周期止损触发额 |
| `stop_loss_fraction` | 0.50 | 熔断后平仓比例 |
| `phase_1_stop_loss_pct` | 0.15 | 第一阶段单仓止损阈值 |
| `phase_2_stop_loss_pct` | 0.12 | 第二阶段单仓止损阈值 |
| `phase_3_stop_loss_pct` | 0.12 | 第三阶段单仓止损阈值 |
| `phase_4_stop_loss_pct` | 0.08 | 第四阶段单仓止损阈值 |
| `hedge_trigger_value` | 12 USDT | 对冲触发净敞口 |
| `hedge_scale` | 0.05 | 对冲头寸比例系数 |
| `max_net_exposure` | 40 USDT | 最大净敞口 |
| `deviation_scale` | 45 | 均值回归头寸系数 |
| `trend_scale` | **0.1** | 趋势头寸比例系数 |
| `max_trend_order_size` | 80 | 趋势加仓上限份数 |
| `take_profit_fraction` | **0.30** | 单次止盈比例 |
| `grid_exit_fraction` | 0.25 | 网格减仓比例 |
| `mean_reversion_sell_fraction` | 0.40 | 均值回归减仓比例 |
| `min_seconds_between_orders` | 2.0 | 同方向订单间隔（秒，UP/DOWN 独立计时，Hedge 豁免） |
| `last_minute_seconds` | 30 | 最后收仓触发时长（秒） |
| `last_minute_min_confidence` | 0.90 | 最后收仓置信度门槛 |
| `max_tail_exposure` | 25 USDT | 尾部最大留仓敞口 |
| `preferred_leg_min_ratio` | 1.2 | 优势侧确认比例阈值 |
| `max_strategy_trades_per_cycle` | 80 | 每周期最大交易次数（Last Minute 豁免） |

---

## 九、监控指标建议

1. **每日盈亏追踪**：监控是否接近 50 USDT 限额，及时做半仓准备
2. **周期止损频率**：频率过高（>20% 周期）说明入场时机或仓位设置需调整
3. **对冲触发频率**：频率过高说明仓位管理不平衡，需优化开仓策略
4. **最后一分钟平仓/加仓比例**：评估尾部风险控制与尾部加仓的有效性
5. **各阶段规则触发分布**：识别哪些规则对盈利/亏损贡献最大
6. **平均周期盈亏**：作为策略效果基准指标
7. **最大回撤**：监控单日策略风险水平

---

## 十、潜在优化方向

### 第一阶段（0–70s）

- Opening Entry 可根据弱势价格（< 0.35 vs 0.35–0.5）进一步分档控制建仓规模
- 优化劣势方向识别算法，引入历史频率或 IV 数据提高首笔准确率

### 第二阶段（70–160s）

- 网格减仓比例（当前固定 25%）可根据浮盈幅度动态调整
- 止盈阈值（当前固定 0.2）可根据波动率动态调整

### 第三阶段（160–240s）

- Trend Entry 可引入动量指标（如 RSI）辅助判断方向
- 对冲机制可根据周期累计盈利动态缩放对冲规模

### 第四阶段（240–290s）

- Last Minute 策略可引入订单簿深度判断，避免在流动性不足时大量平仓
- 尾部加仓规模可进一步与周期盈利正相关动态调整

### 风险控制

- 接入 `DailyLimitManager`（已实现，待集成至 `ReplayEngine`）：达到每日亏损限额时自动半仓/暂停
- 接入后，所有 4 种执行模式（批量/模拟/实盘验证/真实下单）均受每日风控约束

---

> **文档版本**：v4.0（最终实施版）  
> **上一版本**：v3.0 综合整合版（`New_strategy_optimization_enhanced.txt`）  
> **修订内容**：Opening Entry 简化至 2 道确认门；trend_scale 0.05→0.1；take_profit_fraction 35%→30%；Stop Loss 更新为四阶段动态系统；Grid Exit 增加浮盈 ≥ 0 约束；market_regime 阈值 0.35→0.50；Last Minute 明确包含尾部加仓逻辑；格式由 .txt 升级为 .md
