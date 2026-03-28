# 四阶段交易策略优化方案（A+C联合方案）

## 一、背景与问题

### 当前策略的核心矛盾

**止损机制（优先级30）：**
- 单仓亏损 ≥ 2%：立即止损
- 周期累计亏损 > $18：止损50%持仓
- 高波动止损：波动率>1.5时，单仓亏损 ≥ 1%立即止损

**加仓规则优先级（数字越小优先级越高）：**
- Opening Entry (52)
- Grid Entry (60)
- Mean Reversion Entry (70)
- Trend Entry (80)

**矛盾**：止损优先级(30)远高于所有加仓规则(52-80)，一旦触发止损，会立即执行，破坏分阶段的仓位目标。

### 四个阶段目标无法实现的原因

1. **第一阶段（0-70s）目标65%仓位**
   - 问题：开盘建仓时，单仓亏损≥2%会立即止损，无法达到65%目标
   - 问题：没有主动追求65%仓位的机制，只是被动等待规则触发

2. **第二阶段（70-160s）减仓为主**
   - 问题：没有"减仓为主"的强制机制，依赖市场条件

3. **第三阶段（160-240s）加仓为主**
   - 问题：Trend Entry优先级最低(80)，容易被其他规则抢占
   - 问题：止损会破坏"加仓为主"的策略

4. **第四阶段（240-290s）确认方向加仓**
   - 问题：方向确认条件太宽松（优势侧≥另一侧×1.2）
   - 问题：无法确认方向时，只是被动选择市价较高者

---

## 二、优化方案：阶段性止损阈值 + 动态优先级调整

### 方案A：阶段性止损阈值

**核心思想**：在不同阶段使用不同的止损阈值，在关键建仓/加仓阶段放宽止损，给策略更多空间。

**具体实现**：

| 阶段 | 时间范围 | 单仓止损阈值 | 理由 |
|------|---------|------------|------|
| 第一阶段 | 0-70s | 3.0% | 建仓阶段需要容忍更大波动 |
| 第二阶段 | 70-160s | 2.0% | 已有仓位，需要保护利润 |
| 第三阶段 | 160-240s | 2.5% | 加仓阶段需要一定容忍度 |
| 第四阶段 | 240-290s | 1.5% | 临近结束，快速止损 |

**配置文件修改**（configs/strategy.yaml）：
```yaml
stop_loss:
  # 阶段性止损阈值
  phase_1_stop_loss_pct: 0.03      # 第一阶段：3%
  phase_2_stop_loss_pct: 0.02      # 第二阶段：2%
  phase_3_stop_loss_pct: 0.025     # 第三阶段：2.5%
  phase_4_stop_loss_pct: 0.015     # 第四阶段：1.5%

  # 高波动止损（全阶段统一）
  high_vol_stop_loss_pct: 0.01

  # 周期累计止损（全阶段统一）
  stop_loss_cycle_loss: 18.0
  stop_loss_fraction: 0.5
```

### 方案C：动态优先级调整

**核心思想**：在关键阶段，根据当前仓位状态，动态提高加仓/减仓规则的优先级，确保阶段目标实现。

**具体实现**：

#### 第一阶段（0-70s）：确保达到65%仓位

当仓位 < 65%时，提高加仓规则优先级：
- Opening Entry: 52 → 37（提高15）
- Mean Reversion Entry: 70 → 55（提高15）

#### 第二阶段（70-160s）：确保减仓为主

当仓位 > 50%时，提高减仓规则优先级：
- Grid Exit: 60 → 45（提高15）
- Take Profit Exit: 50 → 35（提高15）

#### 第三阶段（160-240s）：确保加仓为主

当仓位 < 85%时，大幅提高趋势加仓优先级：
- Trend Entry: 80 → 55（提高25）
- Grid Entry: 60 → 45（提高15）

#### 第四阶段（240-290s）：确认方向

**第四阶段使用基础优先级，不做动态调整**：
- Last Minute Strategy保持优先级20
- 其他规则使用配置文件中定义的基础优先级
- 不继承第三阶段的优先级调整

---

## 三、实施细节

### 3.1 添加阶段判断函数

**文件**：`src/polynet_ai/strategy/cycle_windows.py`

```python
def determine_phase(cycle_elapsed_seconds: float) -> int:
    """
    根据周期已过时间判断当前阶段

    Args:
        cycle_elapsed_seconds: 周期已过时间（秒）

    Returns:
        阶段编号：1, 2, 3, 4
    """
    if cycle_elapsed_seconds <= 70:
        return 1
    elif cycle_elapsed_seconds <= 160:
        return 2
    elif cycle_elapsed_seconds <= 240:
        return 3
    else:
        return 4
```

### 3.2 修改止损规则

**文件**：`src/polynet_ai/strategy/exit_rules.py`

在 `stop_loss_exits` 函数中添加阶段性止损阈值逻辑：

```python
def stop_loss_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """止损出场规则 - 使用阶段性止损阈值"""

    # 根据阶段获取止损阈值
    phase = determine_phase(features.cycle_elapsed_seconds)
    stop_loss_pct = config.get(f"stop_loss.phase_{phase}_stop_loss_pct", 0.02)

    # 高波动止损阈值（全阶段统一）
    high_vol_stop_loss_pct = config.get("stop_loss.high_vol_stop_loss_pct", 0.01)

    # 使用动态阈值进行止损判断
    # ... 原有逻辑，但使用 stop_loss_pct 而不是固定的 0.02
```

### 3.3 添加仓位计算函数

**文件**：`src/polynet_ai/domain/state_engine.py`

```python
def calculate_position_percentage(features: FeatureSnapshot) -> float:
    """
    计算当前仓位占目标仓位的百分比

    Returns:
        仓位百分比（0.0-1.0）
    """
    # 计算UP和Down两个方向的总持仓成本价值
    up_position_value = features.up_shares * features.up_avg_cost
    down_position_value = features.down_shares * features.down_avg_cost
    total_position_value = up_position_value + down_position_value

    # 目标总仓位：85 USDT
    max_position_value = 85.0

    return total_position_value / max_position_value
```

### 3.4 添加动态优先级调整函数

**文件**：`src/polynet_ai/strategy/router.py`

```python
def adjust_priority_by_phase(
    intent: OrderIntent,
    features: FeatureSnapshot,
    config: StrategyConfig
) -> int:
    """
    根据阶段和仓位状态动态调整规则优先级

    Args:
        intent: 订单意图
        features: 特征快照
        config: 策略配置

    Returns:
        调整后的优先级
    """
    base_priority = intent.priority
    phase = determine_phase(features.cycle_elapsed_seconds)
    current_position_pct = calculate_position_percentage(features)

    # 第一阶段（0-70s）：如果仓位不足65%，提高加仓优先级
    if phase == 1 and current_position_pct < 0.65:
        if intent.rule_name in ["opening_entry", "mean_reversion"]:
            return base_priority - 15

    # 第二阶段（70-160s）：如果仓位超过50%，提高减仓优先级
    if phase == 2 and current_position_pct > 0.50:
        if intent.rule_name in ["grid_exit", "take_profit"]:
            return base_priority - 15

    # 第三阶段（160-240s）：如果仓位不足85%，大幅提高加仓优先级
    if phase == 3 and current_position_pct < 0.85:
        if intent.rule_name == "trend":
            return base_priority - 25
        elif intent.rule_name == "grid_entry":
            return base_priority - 15

    return base_priority
```

在 `router.py` 的 `route` 函数中应用优先级调整：

```python
def route(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    # ... 原有逻辑收集所有候选订单 ...

    # 应用动态优先级调整
    for intent in candidates:
        adjusted_priority = adjust_priority_by_phase(intent, features, config)
        intent.priority = adjusted_priority

    # 按调整后的优先级排序
    candidates.sort(key=lambda item: (item.priority, -item.shares))

    # ... 原有逻辑 ...
```

### 3.5 改进第四阶段方向确认逻辑

**文件**：`src/polynet_ai/strategy/last_minute.py`

在 `last_minute_strategy` 函数中添加更严格的方向确认条件：

```python
def determine_winning_direction(features: FeatureSnapshot, config: StrategyConfig):
    """
    确认获胜方向 - 使用更严格的条件

    Returns:
        ("up", confidence) 或 ("down", confidence) 或 (None, 0.0)
    """
    up_value = features.up_shares * features.up_avg_cost
    down_value = features.down_shares * features.down_avg_cost

    up_pnl = calculate_pnl(features, "up")
    down_pnl = calculate_pnl(features, "down")

    # 条件1：优势侧份额 ≥ 另一侧 × 1.5（而不是1.2）
    ratio_threshold = 1.5

    # 条件2：优势侧浮盈 > 劣势侧浮盈 × 2
    pnl_ratio_threshold = 2.0

    # 条件3：趋势强度 ≥ 0.6
    min_trend_strength = 0.6

    # 判断UP是否为获胜方向
    if (up_value >= down_value * ratio_threshold and
        up_pnl > down_pnl * pnl_ratio_threshold and
        features.trend_strength >= min_trend_strength and
        features.trend_bias == "up"):
        return ("up", 0.9)

    # 判断DOWN是否为获胜方向
    if (down_value >= up_value * ratio_threshold and
        down_pnl > up_pnl * pnl_ratio_threshold and
        features.trend_strength >= min_trend_strength and
        features.trend_bias == "down"):
        return ("down", 0.9)

    # 无法确认获胜方向
    return (None, 0.0)


def last_minute_strategy(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """最后一分钟策略 - 改进方向确认逻辑"""

    if not features.is_last_minute:
        return []

    winning_direction, confidence = determine_winning_direction(features, config)

    # 如果无法确认获胜方向
    if winning_direction is None:
        # 策略：保守处理
        # 1. 平掉所有亏损仓位
        # 2. 保留小额对冲仓位（目标净仓=$10而不是$25）
        # 3. 不加仓
        target_net_exposure = 10.0  # 降低风险暴露

        # ... 生成保守的平仓订单 ...
    else:
        # 确认了获胜方向，按原有逻辑处理
        # ... 原有逻辑 ...
```

---

## 四、回答用户的四个问题

### 1. 第一阶段如何实现加仓为主(达到仓位的65%)？

**优化后的实现**：
1. **放宽止损**：单仓止损阈值从2%提高到3%，减少止损干扰
2. **提高优先级**：当仓位<65%时，Opening Entry(52→37)、Mean Reversion Entry(70→55)
3. **主动监控**：router持续监控仓位百分比，主动触发加仓规则

**效果**：在开盘70秒内，策略会主动追求65%仓位目标，不再被动等待。

### 2. 第二阶段如何实现减仓为主？

**优化后的实现**：
1. **保持标准止损**：单仓止损阈值2%
2. **提高减仓优先级**：当仓位>50%时，Grid Exit(60→45)、Take Profit Exit(50→35)
3. **优先级高于加仓**：减仓规则优先级提升后，高于所有加仓规则

**效果**：第二阶段会优先执行减仓操作，确保"减仓为主"。

### 3. 第三阶段如何实现加仓为主？

**优化后的实现**：
1. **放宽止损**：单仓止损阈值从2%提高到2.5%
2. **大幅提高趋势加仓优先级**：当仓位<85%时，Trend Entry(80→55)
3. **确保不被对冲干扰**：Trend优先级(55)高于Hedge Entry(40)后的调整

**效果**：第三阶段趋势加仓成为核心策略，不再被其他规则抢占。

### 4. 第四阶段如何确认获胜方向并加仓？如果无法确认怎么办？

**优化后的实现**：

**方向确认条件（更严格）**：
1. 优势侧份额 ≥ 另一侧 × 1.5（而不是1.2）
2. 优势侧浮盈 > 劣势侧浮盈 × 2
3. 趋势强度 ≥ 0.6（说明：趋势强度是0-1之间的数值，表示价格朝一个方向移动的强度。0.6表示中等以上趋势，例如UP价格从0.45持续上涨到0.65，或DOWN价格从0.55持续下跌到0.35。如果趋势强度<0.6，说明市场处于震荡或弱趋势状态，不适合确认方向）

**如果无法确认获胜方向**：
1. 不加仓，保持当前仓位
2. 平掉所有亏损仓位
3. 保留小额对冲仓位（目标净仓=$10而不是$25）
4. 降低风险暴露

**效果**：只在高置信度时加仓，无法确认时采取保守策略，避免盲目加仓。

---

## 五、需要修改的文件清单

| 文件 | 修改内容 |
|------|---------|
| `configs/strategy.yaml` | 添加阶段性止损配置 |
| `src/polynet_ai/strategy/cycle_windows.py` | 添加 `determine_phase` 函数 |
| `src/polynet_ai/strategy/exit_rules.py` | 修改 `stop_loss_exits` 使用阶段性阈值 |
| `src/polynet_ai/domain/state_engine.py` | 添加 `calculate_position_percentage` 函数 |
| `src/polynet_ai/strategy/router.py` | 添加 `adjust_priority_by_phase` 函数并应用 |
| `src/polynet_ai/strategy/last_minute.py` | 改进 `determine_winning_direction` 逻辑 |

---

## 六、验证方法

### 单元测试
1. 测试 `determine_phase` 函数：验证阶段判断正确性
2. 测试 `calculate_position_percentage` 函数：验证仓位计算准确性
3. 测试 `adjust_priority_by_phase` 函数：验证优先级调整逻辑
4. 测试 `determine_winning_direction` 函数：验证方向确认条件

### 回测验证
使用历史数据回测，监控以下指标：
1. **第一阶段仓位达成率**：目标≥90%的周期达到65%仓位
2. **第二阶段减仓比例**：目标≥50%的周期执行减仓
3. **第三阶段加仓比例**：目标≥80%的周期执行加仓
4. **第四阶段方向确认成功率**：目标≥70%
5. **止损触发频率**：目标<20%周期
6. **整体盈亏表现**：目标正期望值

### 监控指标
- 各阶段实际仓位分布
- 止损触发时机和频率
- 规则优先级冲突情况
- 动态优先级调整生效次数

---

## 七、方案优势

1. ✅ **平衡风险与目标**：在关键阶段放宽止损，给策略更多空间，同时保持风险控制
2. ✅ **主动目标驱动**：不再被动等待规则触发，而是主动追求阶段性仓位目标
3. ✅ **实现难度适中**：不需要大规模重构，只需在关键位置添加逻辑
4. ✅ **可逐步实施**：可以先实施方案A，验证后再实施方案C
5. ✅ **易于调优**：阶段性阈值和优先级调整幅度都可以通过配置文件调整

---

## 八、风险提示

1. ⚠️ **第一阶段放宽止损到3%**：可能增加单周期最大亏损，需要密切监控
2. ⚠️ **动态优先级调整**：可能产生意外的规则冲突，需要充分测试
3. ⚠️ **第四阶段方向确认条件更严格**：可能导致更多周期无法确认方向，需要评估保守策略的表现

---

## 九、实施建议

### 阶段1：实现阶段性止损阈值（低风险，优先实施）
1. 修改配置文件和止损规则
2. 单元测试验证
3. 回测验证效果
4. 如果效果良好，进入阶段2

### 阶段2：实现动态优先级调整（中风险，谨慎实施）
1. 添加仓位计算和优先级调整函数
2. 单元测试和集成测试
3. 回测验证效果
4. 监控实盘表现

### 阶段3：改进第四阶段方向确认（低风险，可选）
1. 修改方向确认逻辑
2. 回测验证保守策略表现
3. 根据实际情况调整阈值

---

## 十、总结

本方案通过**阶段性止损阈值**和**动态优先级调整**两个核心机制，解决了当前策略止损机制与分阶段目标的矛盾。

**核心改进**：
1. 在建仓/加仓阶段放宽止损（3%、2.5%），给策略更多空间
2. 根据仓位状态动态提高加仓/减仓规则优先级，主动追求阶段目标
3. 改进第四阶段方向确认逻辑，只在高置信度时加仓

**预期效果**：
- 第一阶段能够稳定达到65%仓位
- 第二阶段能够有效执行减仓
- 第三阶段能够顺利加仓到85%
- 第四阶段能够准确确认方向或采取保守策略
- 整体策略表现更加稳定，盈利能力提升
