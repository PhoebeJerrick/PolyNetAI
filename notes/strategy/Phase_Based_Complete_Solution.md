# 四阶段策略完善方案 - 混合方案（时间窗口 + 阶段目标控制）

## 问题分析

原方案（纯时间窗口）的不足：
1. ❌ 无法控制"达到65%仓位"的目标
2. ❌ 无法实现"加仓为主"或"减仓为主"
3. ❌ 无法记录阶段结束时的状态
4. ❌ 无法根据阶段目标动态调整规则行为

## 完善方案：时间窗口 + 阶段目标控制

### 核心思路

**方案1（时间窗口）+ 阶段状态跟踪 + 规则行为调整**

1. **时间窗口**：控制规则在哪个阶段可以触发
2. **阶段状态跟踪**：记录每个阶段的目标和当前进度
3. **规则行为调整**：根据阶段目标动态调整规则的行为（加仓量、减仓量、触发条件）

---

## 一、阶段状态跟踪机制

### 1.1 在 CycleState 中添加阶段跟踪字段

```python
# src/polynet_ai/domain/models.py

@dataclass(slots=True)
class CycleState:
    market_id: str
    cycle_id: str
    cycle_start: datetime | None = None
    cycle_end: datetime | None = None

    # 原有字段...
    up_position: PositionBook = field(default_factory=PositionBook)
    down_position: PositionBook = field(default_factory=PositionBook)

    # 【新增】阶段跟踪字段
    phase1_end_total_held: float = 0.0          # 第一阶段结束时的总持仓
    phase1_end_cash_used: float = 0.0           # 第一阶段结束时的已用资金
    phase2_min_total_held: float = 0.0          # 第二阶段允许的最小持仓（phase1的50%）
    phase3_start_total_held: float = 0.0        # 第三阶段开始时的总持仓
    current_phase: int = 1                       # 当前阶段（1-4）
```

### 1.2 阶段切换逻辑

```python
# src/polynet_ai/domain/state_engine.py

def update_phase_tracking(state: CycleState, features: FeatureSnapshot, config: StrategyConfig):
    """更新阶段跟踪状态"""
    elapsed = features.cycle_elapsed_seconds

    # 阶段1 -> 阶段2 切换（70s）
    if state.current_phase == 1 and elapsed >= 70:
        state.current_phase = 2
        state.phase1_end_total_held = features.up_held + features.down_held
        state.phase1_end_cash_used = (features.up_held * features.up_avg_price +
                                      features.down_held * features.down_avg_price)
        state.phase2_min_total_held = state.phase1_end_total_held * 0.5

    # 阶段2 -> 阶段3 切换（160s）
    elif state.current_phase == 2 and elapsed >= 160:
        state.current_phase = 3
        state.phase3_start_total_held = features.up_held + features.down_held

    # 阶段3 -> 阶段4 切换（240s）
    elif state.current_phase == 3 and elapsed >= 240:
        state.current_phase = 4
```

---

## 二、四个阶段的具体实现

### 第一阶段（0-70s）：趋势低吸加仓，达到65%仓位

#### 目标控制
```python
def opening_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """第一阶段：加仓为主，目标达到65%仓位"""

    # 时间窗口检查
    if not (10 <= features.cycle_elapsed_seconds <= 70):
        return []

    # 【关键】阶段目标检查：已达到65%仓位，停止加仓
    phase1_target_utilization = float(config.get("phase1.target_utilization", 0.65))
    current_cash_used = (features.up_held * features.up_avg_price +
                        features.down_held * features.down_avg_price)
    current_utilization = current_cash_used / features.starting_cash

    if current_utilization >= phase1_target_utilization:
        return []  # 已达目标，停止加仓

    # 【关键】调整加仓量：距离目标越远，加仓越激进
    remaining_target = phase1_target_utilization - current_utilization
    size_multiplier = 1.0 + remaining_target * 2.0  # 距离目标远时放大加仓

    # 原有逻辑...
    size = _base_size(config, features) * size_multiplier
    # ...
```

#### Mean Reversion 在第一阶段的行为
```python
def mean_reversion_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """第一阶段：配合 Opening 达到65%仓位"""

    # 时间窗口检查
    if features.cycle_elapsed_seconds > 70:
        # 第二、三阶段的逻辑...
        pass

    # 第一阶段：加仓为主
    if features.cycle_elapsed_seconds <= 70:
        # 检查是否已达65%目标
        phase1_target_utilization = float(config.get("phase1.target_utilization", 0.65))
        current_cash_used = (features.up_held * features.up_avg_price +
                            features.down_held * features.down_avg_price)
        current_utilization = current_cash_used / features.starting_cash

        if current_utilization >= phase1_target_utilization:
            return []  # 已达目标

        # 加仓逻辑...
```

---

### 第二阶段（70-160s）：网格减仓为主，最多减到第一阶段持仓的50%

#### 减仓控制
```python
def grid_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """第二阶段：减仓为主，但不能减到第一阶段持仓的50%以下"""

    # 时间窗口检查
    if not (70 <= features.cycle_elapsed_seconds <= 160):
        return []

    # 【关键】阶段目标检查：不能减到第一阶段持仓的50%以下
    phase1_end_total = features.metadata.get("phase1_end_total_held", 0)
    phase2_min_held = phase1_end_total * 0.5
    current_total_held = features.up_held + features.down_held

    if current_total_held <= phase2_min_held:
        return []  # 已达最小持仓，停止减仓

    # 【关键】调整减仓量：确保不会减到50%以下
    max_sellable = current_total_held - phase2_min_held

    # 原有逻辑...
    intents = []
    if features.price_percentile >= high and _held_up(features) > 0:
        sell_size = min(
            _held_up(features) * grid_exit_fraction,
            max_sellable * 0.5  # 最多卖掉可卖量的一半（保留给Down）
        )
        intents.append(...)
    # ...
```

#### Take Profit 在第二阶段的行为
```python
def take_profit_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """第二阶段：止盈减仓，但受最小持仓限制"""

    # 时间窗口检查
    if features.cycle_elapsed_seconds < 70:
        return []  # 第一阶段不止盈

    # 第二阶段：减仓受限
    if 70 <= features.cycle_elapsed_seconds <= 160:
        phase1_end_total = features.metadata.get("phase1_end_total_held", 0)
        phase2_min_held = phase1_end_total * 0.5
        current_total_held = features.up_held + features.down_held

        if current_total_held <= phase2_min_held:
            return []  # 已达最小持仓

        max_sellable = current_total_held - phase2_min_held
        # 调整止盈卖出量...

    # 原有逻辑...
```

---

### 第三阶段（160-240s）：顺势加仓为主，减仓为辅

#### 加仓放大
```python
def trend_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """第三阶段：趋势加仓，加仓量放大"""

    # 时间窗口检查
    if features.cycle_elapsed_seconds < 160:
        return []

    # 【关键】第三阶段：加仓量放大
    if 160 <= features.cycle_elapsed_seconds <= 240:
        phase3_aggression = float(config.get("phase3.aggression_multiplier", 1.5))
        size = _base_size(config, features) * phase3_aggression
    else:
        # 第四阶段：正常加仓
        size = _base_size(config, features)

    # 原有逻辑...
```

#### 减仓缩小
```python
def grid_exits(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """第三阶段：减仓为辅，减仓量缩小"""

    # 时间窗口检查
    if not (160 <= features.cycle_elapsed_seconds <= 240):
        return []

    # 【关键】第三阶段：减仓量缩小
    phase3_exit_fraction = float(config.get("phase3.exit_fraction_scale", 0.5))
    grid_exit_fraction = float(config.get("grid.grid_exit_fraction", 0.25))
    actual_exit_fraction = grid_exit_fraction * phase3_exit_fraction  # 减仓量减半

    # 原有逻辑...
    intents.append(
        OrderIntent(
            shares=_held_up(features) * actual_exit_fraction,  # 减仓量缩小
            ...
        )
    )
```

---

### 第四阶段（240-290s）：确认获胜方向，坚定加仓

#### 获胜方向确认
```python
def last_minute_candidate(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """第四阶段：确认获胜方向并加仓"""

    if not features.is_last_minute:
        return []

    # 【关键】确认获胜方向的逻辑
    confidence_threshold = float(config.get("phase4.win_direction_confidence", 0.9))

    # 方法1：基于置信度
    if features.confidence_proxy >= confidence_threshold:
        # 高置信度，确认获胜方向
        if features.cycle_net_profit >= 0 and features.net_direction in {"Up", "Down"}:
            target_outcome = "up" if features.net_direction == "Up" else "down"
            confirmed = True
        else:
            # 盈利但方向不明确，用浮盈判断
            target_outcome = "up" if features.unrealized_up_pnl > features.unrealized_down_pnl else "down"
            confirmed = True
    else:
        # 【关键】低置信度，无法确认获胜方向
        confirmed = False

    # 【关键】根据是否确认，采取不同策略
    if confirmed:
        # 确认了获胜方向：坚定加仓
        target_net = min(
            max_tail_exposure,
            tail_profit_scale * abs(features.cycle_net_profit) +
            tail_volatility_scale * features.volatility
        )
        # 加仓逻辑...
    else:
        # 【关键】无法确认获胜方向：保守处理
        # 策略1：平掉亏损方向，保留盈利方向
        intents = []
        if features.unrealized_up_pnl < 0 and features.up_held > 0:
            intents.append(
                OrderIntent(
                    outcome="up",
                    action="sell",
                    shares=features.up_held,
                    reason="第四阶段无法确认获胜方向，平掉亏损Up方向",
                    ...
                )
            )
        if features.unrealized_down_pnl < 0 and features.down_held > 0:
            intents.append(
                OrderIntent(
                    outcome="down",
                    action="sell",
                    shares=features.down_held,
                    reason="第四阶段无法确认获胜方向，平掉亏损Down方向",
                    ...
                )
            )
        return intents
```

#### Trend 在第四阶段的行为
```python
def trend_entries(features: FeatureSnapshot, config: StrategyConfig) -> list[OrderIntent]:
    """第四阶段：根据确认的获胜方向坚定加仓"""

    if features.cycle_elapsed_seconds < 240:
        return []

    # 【关键】第四阶段：只在确认了获胜方向时才加仓
    confidence_threshold = float(config.get("phase4.win_direction_confidence", 0.9))

    if features.confidence_proxy < confidence_threshold:
        return []  # 无法确认，不加仓

    # 确认了获胜方向，坚定加仓
    if features.trend_bias:
        # 【关键】第四阶段加仓量放大
        phase4_aggression = float(config.get("phase4.aggression_multiplier", 2.0))
        size = _base_size(config, features) * phase4_aggression
        # ...
```

---

## 三、配置文件完整示例

```yaml
# ============================================================================
# 四阶段策略配置
# ============================================================================

# 第一阶段：0-70s - 趋势低吸加仓，达到65%仓位
phase1:
  target_utilization: 0.65          # 目标资金利用率65%
  aggression_multiplier: 1.2        # 加仓激进度

opening_entry:
  enabled: true
  window_start_seconds: 10
  window_end_seconds: 70
  # 原有参数...

# 第二阶段：70-160s - 网格减仓为主，最多减到第一阶段持仓的50%
phase2:
  min_position_ratio: 0.5           # 最小持仓比例（相对第一阶段）
  exit_fraction_scale: 1.0          # 减仓比例（正常）

grid:
  window_start_seconds: 70
  window_end_seconds: 240
  grid_exit_fraction: 0.25
  # 原有参数...

profit_taking:
  window_start_seconds: 70
  take_profit_fraction: 0.35
  # 原有参数...

# 第三阶段：160-240s - 顺势加仓为主，减仓为辅
phase3:
  aggression_multiplier: 1.5        # 加仓量放大50%
  exit_fraction_scale: 0.5          # 减仓量缩小50%

trend:
  window_start_seconds: 160
  min_trend_strength: 0.5
  # 原有参数...

# 第四阶段：240-290s - 确认获胜方向，坚定加仓
phase4:
  win_direction_confidence: 0.9     # 确认获胜方向的置信度阈值
  aggression_multiplier: 2.0        # 确认后加仓量放大100%
  unconfirmed_strategy: "close_losing"  # 无法确认时的策略：平掉亏损方向

last_minute:
  last_minute_seconds: 30
  last_minute_min_confidence: 0.9
  # 原有参数...
```

---

## 四、实现步骤

### 步骤1：修改 CycleState 添加阶段跟踪字段

```python
# src/polynet_ai/domain/models.py
@dataclass(slots=True)
class CycleState:
    # 原有字段...

    # 新增阶段跟踪字段
    phase1_end_total_held: float = 0.0
    phase1_end_cash_used: float = 0.0
    phase2_min_total_held: float = 0.0
    phase3_start_total_held: float = 0.0
    current_phase: int = 1
```

### 步骤2：在 state_engine 中添加阶段切换逻辑

```python
# src/polynet_ai/domain/state_engine.py
def update_phase_tracking(state: CycleState, features: FeatureSnapshot, config: StrategyConfig):
    # 阶段切换逻辑...
```

### 步骤3：修改规则函数，添加阶段目标控制

- opening_entries: 添加65%目标检查
- mean_reversion_entries: 第一阶段配合达到65%
- grid_exits: 第二阶段不能减到50%以下
- take_profit_exits: 第二阶段受最小持仓限制
- trend_entries: 第三阶段加仓放大，第四阶段确认后加仓
- last_minute_candidate: 第四阶段确认获胜方向逻辑

### 步骤4：在 FeatureSnapshot 中添加阶段信息

```python
# src/polynet_ai/domain/models.py
@dataclass(slots=True)
class FeatureSnapshot:
    # 原有字段...

    # 新增：从 CycleState 传递过来的阶段信息
    current_phase: int = 1
    phase1_end_total_held: float = 0.0
    phase2_min_total_held: float = 0.0
```

### 步骤5：在 router 中调用阶段切换逻辑

```python
# src/polynet_ai/strategy/router.py
def route(self, features: FeatureSnapshot, strategy_trades: int = 0) -> DecisionOutcome:
    # 在规则评估前，更新阶段跟踪
    # update_phase_tracking(state, features, self.config)

    # 原有逻辑...
```

---

## 五、回答你的四个问题

### Q1: 如何实现第一阶段"加仓为主，达到65%仓位"？

**A**:
1. 在 opening_entries 和 mean_reversion_entries 中添加目标检查
2. 计算当前资金利用率 = 已用资金 / 总资金
3. 如果 >= 65%，停止加仓
4. 距离目标越远，加仓越激进（size_multiplier）

### Q2: 如何实现第二阶段"减仓为主"？

**A**:
1. 在阶段1->2切换时，记录 phase1_end_total_held
2. 计算 phase2_min_total_held = phase1_end_total_held * 0.5
3. 在所有出场规则中检查：current_total_held <= phase2_min_total_held 时停止减仓
4. 调整减仓量：max_sellable = current_total_held - phase2_min_total_held

### Q3: 如何实现第三阶段"加仓为主"？

**A**:
1. 在 trend_entries 中，第三阶段加仓量放大（aggression_multiplier = 1.5）
2. 在 grid_exits 中，第三阶段减仓量缩小（exit_fraction_scale = 0.5）
3. "加仓的量多，减仓的量少"通过调整 multiplier 实现

### Q4: 如何实现第四阶段"确认获胜方向并加仓"？

**A**:
1. **确认获胜方向**：
   - 置信度 >= 0.9 且 周期盈利 > 0 且 净方向明确
   - 或：置信度 >= 0.9 且 浮盈差异明显

2. **确认后**：
   - 坚定加仓获胜方向（aggression_multiplier = 2.0）
   - Trend 规则只在确认后才触发

3. **无法确认时**：
   - 策略1：平掉亏损方向，保留盈利方向
   - 策略2：不再加仓，保持当前持仓
   - 策略3：双向平仓，锁定利润

---

## 六、总结

### 完善方案 = 时间窗口 + 阶段目标控制

| 机制 | 作用 | 实现方式 |
|------|------|---------|
| 时间窗口 | 控制规则在哪个阶段触发 | window_start/end_seconds |
| 阶段目标 | 控制"达到65%"、"减到50%"等目标 | phase1_target_utilization, phase2_min_held |
| 行为调整 | 控制"加仓为主"、"减仓为主" | aggression_multiplier, exit_fraction_scale |
| 状态跟踪 | 记录阶段切换时的状态 | phase1_end_total_held, current_phase |

### 关键改进

✅ 第一阶段：目标控制（65%）+ 动态加仓量
✅ 第二阶段：最小持仓限制（50%）+ 减仓量控制
✅ 第三阶段：加仓放大 + 减仓缩小
✅ 第四阶段：获胜方向确认 + 无法确认时的保守策略

这个完善方案才能真正实现你的四阶段策略！
