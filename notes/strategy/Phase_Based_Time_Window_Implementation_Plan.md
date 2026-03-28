# 四阶段时间窗口实现方案

## 一、方案概述

**目标**：为每个规则添加时间窗口限制，使其符合四阶段交易策略

**方法**：在配置文件中为每个规则添加 `window_start_seconds` 和 `window_end_seconds` 参数

**优势**：
1. 符合现有代码风格（已有 `disable_within_seconds_before_end` 参数）
2. 规则保持独立，易于测试和调试
3. 不需要重构 router 的并行执行逻辑
4. 灵活性高 - 规则可以在多个阶段生效
5. 不需要 reset 缓存

---

## 二、四阶段时间划分

基于你的优化文档，四个阶段的时间划分：

| 阶段 | 时间范围 | 主要目标 | 核心规则 |
|------|---------|---------|---------|
| 第一阶段 | 0-70s | 趋势低吸加仓，达到65%仓位 | Opening, Mean Reversion |
| 第二阶段 | 70-160s | 网格减仓为主 | Grid Exit, Take Profit, Stop Loss |
| 第三阶段 | 160-240s | 顺势加仓+对冲 | Trend, Hedge, Grid Entry |
| 第四阶段 | 240-290s | 确认方向加仓 | Last Minute, Trend, Mean Reversion |

---

## 三、规则时间窗口配置

### 3.1 入场规则时间窗口

```yaml
# Opening Entry - 仅第一阶段
opening_entry:
  enabled: true
  window_start_seconds: 10      # 开盘10s后开始
  window_end_seconds: 70        # 70s后停止
  window_seconds: 30            # 保留原有参数（向后兼容）

# Mean Reversion Entry - 第一、二、三阶段
mean_reversion:
  enabled: true
  window_start_seconds: 0       # 从开始就可以
  window_end_seconds: 240       # 240s后停止（第四阶段不用）
  disable_within_seconds_before_end: 80  # 保留原有参数

# Grid Entry - 第二、三阶段
grid:
  window_start_seconds: 70      # 70s后开始
  window_end_seconds: 240       # 240s后停止
  disable_within_seconds_before_end: 50  # 保留原有参数

# Trend Entry - 第三、四阶段
trend:
  window_start_seconds: 160     # 160s后开始
  # 不设 window_end_seconds 表示一直到周期结束

# Hedge Entry - 全周期（除最后一分钟）
hedge:
  # 不设时间窗口，全周期生效
  # 已有 is_last_minute 检查
```

### 3.2 出场规则时间窗口

```yaml
# Stop Loss Exit - 全周期
stop_loss:
  # 不设时间窗口，全周期生效（风险控制优先）

# Take Profit Exit - 第二、三、四阶段
profit_taking:
  window_start_seconds: 70      # 70s后开始止盈
  # 不设 window_end_seconds，一直到周期结束

# Hedge Exit - 全周期（除最后一分钟）
hedge:
  # 不设时间窗口，全周期生效

# Grid Exit - 第二、三阶段
grid:
  window_start_seconds: 70      # 70s后开始
  window_end_seconds: 240       # 240s后停止
  disable_within_seconds_before_end: 50  # 保留原有参数

# Mean Reversion Exit - 第二、三阶段
mean_reversion:
  window_start_seconds: 70      # 70s后开始
  window_end_seconds: 240       # 240s后停止
  disable_within_seconds_before_end: 80  # 保留原有参数
```

### 3.3 最后一分钟策略

```yaml
# Last Minute - 第四阶段（最后30秒）
last_minute:
  last_minute_seconds: 30       # 保留原有参数
  # 已有 is_last_minute 检查，不需要额外时间窗口
```

---

## 四、代码实现

### 4.1 创建通用时间窗口检查函数

在 `src/polynet_ai/strategy/cycle_windows.py` 中添加：

```python
def rule_in_time_window(
    features: FeatureSnapshot,
    config: StrategyConfig,
    rule_name: str
) -> bool:
    """
    检查规则是否在配置的时间窗口内

    Args:
        features: 特征快照
        config: 策略配置
        rule_name: 规则名称（如 "opening_entry", "mean_reversion" 等）

    Returns:
        True 如果在时间窗口内，False 否则
    """
    # 获取时间窗口配置
    window_start = config.get(f"{rule_name}.window_start_seconds", None)
    window_end = config.get(f"{rule_name}.window_end_seconds", None)

    elapsed = features.cycle_elapsed_seconds

    # 检查开始时间
    if window_start is not None and elapsed < float(window_start):
        return False

    # 检查结束时间
    if window_end is not None and elapsed > float(window_end):
        return False

    return True
```

### 4.2 修改入场规则

#### opening_entries (entry_rules.py)

```python
def opening_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """开盘试探规则 - 第一阶段（10-70s）"""
    if not config.get("opening_entry.enabled", True):
        return []

    # 【新增】时间窗口检查
    if not rule_in_time_window(features, config, "opening_entry"):
        return []

    if features.is_last_minute or features.strategy_trades != 0:
        return []

    # 原有逻辑...
```

#### mean_reversion_entries (entry_rules.py)

```python
def mean_reversion_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """均值回归规则 - 第一、二、三阶段（0-240s）"""
    if not bool(config.get("mean_reversion.enabled", True)):
        return []

    # 【新增】时间窗口检查
    if not rule_in_time_window(features, config, "mean_reversion"):
        return []

    # 保留原有的 cycle_tail 检查（向后兼容）
    if rule_disabled_in_cycle_tail(features, config, "mean_reversion"):
        return []

    if features.is_last_minute:
        return []

    # 原有逻辑...
```

#### grid_entries (entry_rules.py)

```python
def grid_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """网格入场规则 - 第二、三阶段（70-240s）"""
    # 【新增】时间窗口检查
    if not rule_in_time_window(features, config, "grid"):
        return []

    # 保留原有检查
    if rule_disabled_in_cycle_tail(features, config, "grid"):
        return []

    if features.is_last_minute or features.market_regime != "range":
        return []

    # 原有逻辑...
```

#### trend_entries (entry_rules.py)

```python
def trend_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """趋势跟随规则 - 第三、四阶段（160s-结束）"""
    # 【新增】时间窗口检查
    if not rule_in_time_window(features, config, "trend"):
        return []

    if features.is_last_minute or not features.trend_bias:
        return []

    # 原有逻辑...
```

#### hedge_entries (entry_rules.py)

```python
def hedge_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """对冲入场规则 - 全周期（除最后一分钟）"""
    # 【可选】时间窗口检查（如果需要限制对冲时间）
    # if not rule_in_time_window(features, config, "hedge"):
    #     return []

    if features.is_last_minute:
        return []

    # 原有逻辑...
```

### 4.3 修改出场规则

#### take_profit_exits (exit_rules.py)

```python
def take_profit_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """止盈出场规则 - 第二、三、四阶段（70s-结束）"""
    # 【新增】时间窗口检查
    if not rule_in_time_window(features, config, "profit_taking"):
        return []

    # 原有逻辑...
```

#### grid_exits (exit_rules.py)

```python
def grid_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """网格出场规则 - 第二、三阶段（70-240s）"""
    # 【新增】时间窗口检查
    if not rule_in_time_window(features, config, "grid"):
        return []

    # 保留原有检查
    if rule_disabled_in_cycle_tail(features, config, "grid"):
        return []

    # 原有逻辑...
```

#### mean_reversion_exits (exit_rules.py)

```python
def mean_reversion_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """均值回归出场规则 - 第二、三阶段（70-240s）"""
    if not bool(config.get("mean_reversion.enabled", True)):
        return []

    # 【新增】时间窗口检查
    if not rule_in_time_window(features, config, "mean_reversion"):
        return []

    # 保留原有检查
    if rule_disabled_in_cycle_tail(features, config, "mean_reversion"):
        return []

    # 原有逻辑...
```

---

## 五、配置文件完整示例

### strategy.yaml 新增配置

```yaml
# ============================================================================
# 四阶段时间窗口配置
# ============================================================================

# 第一阶段：0-70s - 趋势低吸加仓
opening_entry:
  enabled: true
  window_start_seconds: 10
  window_end_seconds: 70
  window_seconds: 30  # 保留向后兼容
  min_market_trades: 1
  vwap_epsilon: 0.01
  range_low_fraction: 0.35
  min_range_width: 0.02
  infer_missing_with_binary_complement: true

# 第一、二、三阶段：0-240s
mean_reversion:
  enabled: true
  window_start_seconds: 0
  window_end_seconds: 240
  up_buy_deviation: 0.1
  down_buy_deviation: 0.1
  mean_reversion_sell_up_deviation: 0.2
  mean_reversion_sell_down_deviation: 0.2
  mean_reversion_sell_fraction: 0.40
  deviation_scale: 45
  disable_within_seconds_before_end: 80  # 保留向后兼容

# 第二、三阶段：70-240s
grid:
  window_start_seconds: 70
  window_end_seconds: 240
  grid_low_percentile: 0.25
  grid_high_percentile: 0.75
  grid_exit_fraction: 0.25
  disable_within_seconds_before_end: 50  # 保留向后兼容

# 第三、四阶段：160s-结束
trend:
  window_start_seconds: 160
  # 不设 window_end_seconds，一直到周期结束
  min_trend_strength: 0.5
  trend_price_edge: 0.03
  trend_scale: 0.05
  max_trend_order_size: 80.0

# 第二、三、四阶段：70s-结束
profit_taking:
  window_start_seconds: 70
  # 不设 window_end_seconds，一直到周期结束
  take_profit_up_deviation: 0.2
  take_profit_down_deviation: 0.2
  take_profit_fraction: 0.3

# 全周期规则（不设时间窗口）
hedge:
  # 对冲规则全周期生效

stop_loss:
  # 止损规则全周期生效（风险控制优先）
  stop_loss_cycle_loss: 18.0
  stop_loss_fraction: 0.5
  stop_loss_pct: 0.02
  high_vol_stop_loss_pct: 0.01

last_minute:
  # 最后一分钟策略（已有 is_last_minute 检查）
  last_minute_min_confidence: 0.9
  tail_profit_scale: 0.35
  tail_volatility_scale: 14
  max_tail_exposure: 25.0
  preferred_leg_min_ratio: 1.2
```

---

## 六、测试计划

### 6.1 单元测试

为 `rule_in_time_window` 函数添加测试：

```python
def test_rule_in_time_window():
    # 测试在窗口内
    # 测试在窗口外（开始前）
    # 测试在窗口外（结束后）
    # 测试无窗口限制
    # 测试只有开始时间
    # 测试只有结束时间
```

### 6.2 集成测试

使用现有的回测数据测试四阶段策略：

```bash
# 测试第一阶段（0-70s）
# 验证：只有 Opening 和 Mean Reversion 规则触发

# 测试第二阶段（70-160s）
# 验证：Grid Exit, Take Profit, Mean Reversion 规则触发

# 测试第三阶段（160-240s）
# 验证：Trend, Hedge, Grid Entry 规则触发

# 测试第四阶段（240-290s）
# 验证：Last Minute, Trend 规则触发
```

### 6.3 回测验证

```bash
# 使用历史数据回测
./record.sh ds10  # 模拟下单回放10个周期

# 检查日志，验证规则触发时间是否符合预期
```

---

## 七、向后兼容性

### 7.1 保留原有参数

- `window_seconds` (opening_entry) - 保留
- `disable_within_seconds_before_end` (grid, mean_reversion) - 保留

### 7.2 默认行为

如果不配置 `window_start_seconds` 和 `window_end_seconds`：
- 规则按原有逻辑执行
- 不受时间窗口限制

### 7.3 优先级

如果同时配置了新旧参数：
1. 先检查 `window_start_seconds` / `window_end_seconds`
2. 再检查 `disable_within_seconds_before_end`
3. 两者都满足才执行规则

---

## 八、优势总结

### 8.1 相比方案2（阶段式规则使能）的优势

| 维度 | 方案1（时间窗口） | 方案2（阶段使能） |
|------|-----------------|-----------------|
| 代码复杂度 | 低 - 只需添加检查函数 | 高 - 需要重构 router |
| 规则独立性 | 高 - 规则仍然独立 | 低 - 规则依赖阶段状态 |
| 并行执行 | 不受影响 | 需要处理阶段切换同步 |
| 缓存管理 | 不需要 reset | 需要在阶段切换时 reset |
| 测试难度 | 低 - 规则独立测试 | 高 - 需要模拟阶段切换 |
| 灵活性 | 高 - 规则可跨阶段 | 低 - 规则绑定阶段 |
| 向后兼容 | 完全兼容 | 需要大量修改 |

### 8.2 符合现有代码风格

- 已有 `rule_disabled_in_cycle_tail` 函数
- 已有 `window_seconds` 参数
- 已有 `is_last_minute` 检查
- 新增的 `rule_in_time_window` 函数风格一致

---

## 九、实施步骤

1. **创建 `rule_in_time_window` 函数** (cycle_windows.py)
2. **修改入场规则** (entry_rules.py)
   - opening_entries
   - mean_reversion_entries
   - grid_entries
   - trend_entries
3. **修改出场规则** (exit_rules.py)
   - take_profit_exits
   - grid_exits
   - mean_reversion_exits
4. **更新配置文件** (strategy.yaml)
5. **添加单元测试**
6. **运行回测验证**
7. **更新文档**

---

## 十、注意事项

### 10.1 不需要 reset 缓存

因为规则自己决定是否执行，router 的缓存（`_feed_last_at`, `_feed_prices` 等）不需要 reset。

### 10.2 优先级不变

时间窗口只是过滤规则，不改变优先级排序。

### 10.3 最后一分钟策略

Last Minute 策略已经有 `is_last_minute` 检查，不需要额外的时间窗口配置。

### 10.4 风险控制优先

Stop Loss 规则不设时间窗口，全周期生效，确保风险控制优先。

---

## 十一、问题解答

### Q1: 为什么不用方案2（阶段式规则使能）？

**A**: 方案2需要：
1. 在 router 中添加阶段状态管理
2. 在阶段切换时 reset 所有缓存
3. 处理并行执行时的阶段切换同步问题
4. 破坏规则的独立性

这些都会增加代码复杂度，而方案1只需要添加一个简单的检查函数。

### Q2: 时间窗口会影响性能吗？

**A**: 不会。时间窗口检查只是一个简单的数值比较，开销可以忽略不计。

### Q3: 如何调试时间窗口？

**A**: 可以在日志中输出规则触发时间：

```python
if not rule_in_time_window(features, config, "opening_entry"):
    logger.debug(f"Opening entry skipped: elapsed={features.cycle_elapsed_seconds}s")
    return []
```

### Q4: 优先级是值越小越高吗？

**A**: 是的！在 router.py:156 中：

```python
candidates.sort(key=lambda item: (item.priority, -item.shares))
```

按 `priority` 升序排序，所以：
- Risk (10) 优先级最高
- Trend (80) 优先级最低

---

## 十二、总结

方案1（时间窗口限制）是最适合你当前代码结构和策略风格的方案：

✅ 简单 - 只需添加一个检查函数
✅ 灵活 - 规则可以跨阶段生效
✅ 兼容 - 不破坏现有代码
✅ 高效 - 不影响并行执行
✅ 易测 - 规则独立测试

准备好后，我可以帮你实现这个方案。
