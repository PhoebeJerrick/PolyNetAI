"""Dashboard 参数控制台：与 configs/strategy.yaml 叶子路径对齐的表单 schema。"""

from __future__ import annotations

from typing import Any


def _field(
    path: str,
    label: str,
    hint: str,
    *,
    typ: str = "number",
    risk: str = "medium",
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {"path": path, "label": label, "hint": hint, "type": typ, "risk": risk}
    row.update(extra)
    return row


def _rule_enablement_fields() -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    layout = [
        ("entries", ["opening", "hedge", "grid", "mean_reversion", "trend"]),
        ("exits", ["stop_loss", "hedge", "take_profit", "grid", "mean_reversion"]),
        ("last_minute", ["last_minute"]),
    ]
    for section, rules in layout:
        for rule in rules:
            base = f"rule_enablement.{section}.{rule}"
            fields.append(
                _field(
                    f"{base}.enabled",
                    f"{section}/{rule} 总开关",
                    "关闭则该规则不参与评估（各阶段均无效）。",
                    typ="boolean",
                    risk="high",
                )
            )
            for i in range(1, 5):
                fields.append(
                    _field(
                        f"{base}.phase_{i}",
                        f"{section}/{rule} 阶段{i}",
                        f"周期阶段 {i} 内是否使能该规则。",
                        typ="boolean",
                        risk="medium",
                    )
                )
    return fields


def _priorities_phase_section(phase: int) -> dict[str, Any]:
    rules = [
        ("risk", "风险"),
        ("last_minute", "尾盘"),
        ("stop_loss", "止损"),
        ("hedge", "对冲"),
        ("take_profit", "止盈"),
        ("opening", "开盘试探"),
        ("grid", "网格"),
        ("mean_reversion", "均值回归"),
        ("trend", "趋势"),
    ]
    fields = [
        _field(
            f"priorities.by_phase.phase_{phase}.{key}",
            f"阶段{phase} · {lab}",
            "数值越小越优先；可与扁平 priorities.* 并存，按阶段桶优先。",
            typ="number",
            risk="low",
            min=1,
            max=99,
            step=1,
        )
        for key, lab in rules
    ]
    return {
        "title": f"priorities · 阶段 {phase}",
        "description": f"与 strategy.yaml 的 priorities.by_phase.phase_{phase} 一致。",
        "fields": fields,
    }


def build_strategy_dashboard_sections() -> list[dict[str, Any]]:
    return [
        {
            "title": "cycle / batch_replay（生命周期与回放）",
            "description": "周期边界、延迟与批量回放模式。",
            "fields": [
                _field("cycle.cycle_seconds", "周期长度（秒）", "如 BTC 5 分钟盘为 300。", ui="select", options=[60, 300, 900], risk="low"),
                _field("cycle.last_minute_seconds", "尾盘窗口（秒）", "进入 last_minute 逻辑前的秒数。", ui="select", options=[30, 45, 60, 75, 90], risk="medium"),
                _field("cycle.phase_end_seconds_1", "阶段1 结束（秒）", "已过周期时间 ≤ 此值属阶段1；须严格递增。", ui="range", min=1, max=600, step=1, risk="medium"),
                _field("cycle.phase_end_seconds_2", "阶段2 结束（秒）", "", ui="range", min=1, max=600, step=1, risk="medium"),
                _field("cycle.phase_end_seconds_3", "阶段3 结束（秒）", "", ui="range", min=1, max=600, step=1, risk="medium"),
                _field("cycle.post_window_start_delay_seconds", "成交流延迟（秒）", "时间桶开始后推迟多少秒再喂给策略。", min=0, max=120, step=0.5, risk="medium"),
                _field(
                    "batch_replay.processing_mode",
                    "批量回放模式",
                    "per-cycle：逐周期；merged：合并事件流。",
                    typ="string",
                    ui="select",
                    options=["per-cycle", "merged"],
                    risk="low",
                ),
            ],
        },
        {
            "title": "position / capital / exposure（仓位与敞口）",
            "description": "目标仓位、现金与净敞口约束。",
            "fields": [
                _field("position.max_position_value", "目标总仓位价值", "与账户币种一致。", ui="range", min=1, max=500, step=1, risk="high"),
                _field("capital.max_cash_utilization", "最大资金使用率", "", ui="range", min=0.5, max=1.0, step=0.01, risk="high"),
                _field("capital.min_cash_buffer", "最小现金缓冲", "", ui="range", min=0, max=500, step=5, risk="medium"),
                _field("exposure.max_abs_exposure_value", "最大绝对敞口", "", ui="range", min=5, max=2000, step=5, risk="high"),
                _field("exposure.phase_4_max_abs_exposure_value", "阶段4 最大绝对敞口", "尾盘方向明确时放宽上限。", ui="range", min=5, max=2000, step=5, risk="high"),
                _field("exposure.hedge_trigger_value", "对冲触发阈值", "", ui="range", min=1, max=500, step=1, risk="medium"),
                _field("exposure.hedge_scale", "对冲强度系数", "", ui="range", min=0.01, max=1.0, step=0.01, risk="medium"),
                _field("exposure.max_grid_net_position", "网格最大净仓位", "", ui="range", min=1, max=200, step=1, risk="high"),
                _field("exposure.max_strategy_trades_per_cycle", "每周期最大策略成交次数", "", ui="range", min=1, max=200, step=1, risk="medium"),
            ],
        },
        {
            "title": "daily_limits（单日风控）",
            "description": "与 strategy.yaml 的 daily_limits 一致。",
            "fields": [
                _field("daily_limits.daily_loss_limit", "单日亏损限额", "", ui="range", min=1, max=500, step=1, risk="high"),
                _field("daily_limits.daily_loss_pause_trading", "触达限额暂停交易", "", typ="boolean", risk="high"),
                _field("daily_limits.warning_threshold_ratio", "警告比例（相对限额）", "", ui="range", min=0.1, max=1.0, step=0.05, risk="medium"),
                _field("daily_limits.market_limits.use_orderbook_min_order_size", "使用盘口最小下单量", "", typ="boolean", risk="low"),
                _field("daily_limits.market_limits.fallback_min_order_size", "盘口缺失时最小下单量兜底", "", min=0, max=50, step=0.5, risk="low"),
                _field("daily_limits.market_limits.enforce_sell_min_order_size", "卖单强制最小下单量", "", typ="boolean", risk="medium"),
            ],
        },
        {
            "title": "dynamic_priority（动态优先级微调）",
            "description": "在分阶段基础优先级上按仓位再调整。",
            "fields": [
                _field("dynamic_priority.phase_1_position_threshold", "阶段1 仓位阈值（比例）", "低于则提升部分买单优先级。", ui="range", min=0.1, max=1.0, step=0.01, risk="medium"),
                _field("dynamic_priority.phase_1_boost", "阶段1 提升幅度", "从优先级数值中减去。", min=0, max=50, step=1, risk="medium"),
                _field("dynamic_priority.phase_2_position_threshold", "阶段2 减仓仓位阈值", "高于则提升部分卖单。", ui="range", min=0.1, max=1.0, step=0.01, risk="medium"),
                _field("dynamic_priority.phase_2_low_position_threshold", "阶段2 加仓仓位阈值", "低于则提升所有买单（幅度同阶段2 提升）。", ui="range", min=0.05, max=0.9, step=0.01, risk="medium"),
                _field("dynamic_priority.phase_2_boost", "阶段2 提升幅度", "减仓侧与低仓位加仓侧共用。", min=0, max=50, step=1, risk="medium"),
                _field("dynamic_priority.phase_3_position_threshold", "阶段3 仓位阈值", "低于则提升 trend/grid 买单。", ui="range", min=0.1, max=1.0, step=0.01, risk="medium"),
                _field("dynamic_priority.phase_3_trend_boost", "阶段3 趋势买单幅度", "", min=0, max=50, step=1, risk="medium"),
                _field("dynamic_priority.phase_3_grid_boost", "阶段3 网格买单幅度", "", min=0, max=50, step=1, risk="medium"),
            ],
        },
        _priorities_phase_section(1),
        _priorities_phase_section(2),
        _priorities_phase_section(3),
        _priorities_phase_section(4),
        {
            "title": "priorities（扁平回退，可选）",
            "description": "若仅配置此处标量，加载时会自动复制到四阶段 by_phase。",
            "fields": [
                _field("priorities.risk", "risk", "回退键。", typ="number", min=1, max=99, step=1, risk="low"),
                _field("priorities.last_minute", "last_minute", "", typ="number", min=1, max=99, step=1, risk="low"),
                _field("priorities.stop_loss", "stop_loss", "", typ="number", min=1, max=99, step=1, risk="low"),
                _field("priorities.hedge", "hedge", "", typ="number", min=1, max=99, step=1, risk="low"),
                _field("priorities.take_profit", "take_profit", "", typ="number", min=1, max=99, step=1, risk="low"),
                _field("priorities.opening", "opening", "", typ="number", min=1, max=99, step=1, risk="low"),
                _field("priorities.grid", "grid", "", typ="number", min=1, max=99, step=1, risk="low"),
                _field("priorities.mean_reversion", "mean_reversion", "", typ="number", min=1, max=99, step=1, risk="low"),
                _field("priorities.trend", "trend", "", typ="number", min=1, max=99, step=1, risk="low"),
            ],
        },
        {
            "title": "rule_enablement（规则阶段使能）",
            "description": "与 strategy.yaml 的 rule_enablement 一致。",
            "fields": _rule_enablement_fields(),
        },
        {
            "title": "opening_entry（开盘试探）",
            "description": "",
            "fields": [
                _field("opening_entry.enabled", "使能", "", typ="boolean", risk="medium"),
                _field("opening_entry.window_seconds", "窗口（秒）", "", ui="range", min=5, max=120, step=1, risk="low"),
                _field("opening_entry.min_market_trades", "最小市场成交笔数", "", min=0, max=50, step=1, risk="medium"),
                _field("opening_entry.vwap_epsilon", "VWAP 容差", "", ui="range", min=0.0, max=0.05, step=0.001, risk="medium"),
                _field("opening_entry.range_low_fraction", "低位分段比例", "", ui="range", min=0.05, max=0.95, step=0.01, risk="medium"),
                _field("opening_entry.min_range_width", "最小区间宽度", "", ui="range", min=0.0, max=0.2, step=0.001, risk="low"),
                _field("opening_entry.infer_missing_with_binary_complement", "缺失盘口互补推断", "", typ="boolean", risk="low"),
            ],
        },
        {
            "title": "order_sizing（下单规模）",
            "description": "",
            "fields": [
                _field("order_sizing.buy.base_order_size", "买入基础量", "", ui="range", min=1, max=100, step=0.5, risk="medium"),
                _field("order_sizing.buy.min_order_size", "买入最小量", "", min=0.5, max=50, step=0.5, risk="low"),
                _field("order_sizing.buy.max_order_size", "买入最大量", "", ui="range", min=5, max=500, step=1, risk="high"),
                _field("order_sizing.buy.volatility_order_scale", "买入波动放大系数", "", ui="range", min=0, max=80, step=1, risk="medium"),
                _field("order_sizing.sell.min_order_size", "卖出最小量", "", min=0.5, max=50, step=0.5, risk="low"),
                _field("order_sizing.sell.max_order_size", "卖出最大量", "", ui="range", min=5, max=500, step=1, risk="high"),
                _field("order_sizing.sell.allow_close_below_min_order_size", "碎仓可低于最小量平仓", "", typ="boolean", risk="medium"),
            ],
        },
        {
            "title": "trend / grid / mean_reversion",
            "description": "",
            "fields": [
                _field("trend.min_trend_strength", "最小趋势强度", "", ui="range", min=0.05, max=0.95, step=0.01, risk="medium"),
                _field("trend.trend_price_edge", "趋势价格边际", "", ui="range", min=0.0, max=0.3, step=0.01, risk="medium"),
                _field("trend.trend_scale", "趋势加仓系数", "", ui="range", min=0.0, max=1.0, step=0.01, risk="high"),
                _field("trend.max_trend_order_size", "趋势单量上限", "", ui="range", min=5, max=500, step=1, risk="high"),
                _field("trend.trend_window", "趋势窗口（秒）", "", min=1, max=120, step=1, risk="medium"),
                _field("grid.grid_low_percentile", "网格低位分位", "", ui="range", min=0.05, max=0.5, step=0.01, risk="low"),
                _field("grid.grid_high_percentile", "网格高位分位", "", ui="range", min=0.5, max=0.95, step=0.01, risk="low"),
                _field("grid.grid_exit_fraction", "网格卖出比例", "", ui="range", min=0.05, max=1.0, step=0.05, risk="medium"),
                _field("grid.disable_within_seconds_before_end", "网格尾盘禁用（剩余秒）", "", ui="range", min=0, max=300, step=1, risk="medium"),
                _field("mean_reversion.enabled", "均值回归使能", "", typ="boolean", risk="medium"),
                _field("mean_reversion.up_buy_deviation", "Up 买入偏离", "", ui="range", min=0.01, max=0.5, step=0.01, risk="medium"),
                _field("mean_reversion.down_buy_deviation", "Down 买入偏离", "", ui="range", min=0.01, max=0.5, step=0.01, risk="medium"),
                _field("mean_reversion.mean_reversion_sell_up_deviation", "Up 卖出偏离", "", ui="range", min=0.01, max=0.6, step=0.01, risk="medium"),
                _field("mean_reversion.mean_reversion_sell_down_deviation", "Down 卖出偏离", "", ui="range", min=0.01, max=0.6, step=0.01, risk="medium"),
                _field("mean_reversion.mean_reversion_sell_fraction", "均值回归卖出比例", "", ui="range", min=0.05, max=1.0, step=0.05, risk="medium"),
                _field("mean_reversion.deviation_scale", "偏离放大系数", "", ui="range", min=1, max=150, step=1, risk="high"),
                _field("mean_reversion.disable_within_seconds_before_end", "均值回归尾盘禁用（剩余秒）", "", ui="range", min=0, max=300, step=1, risk="medium"),
            ],
        },
        {
            "title": "profit_taking（止盈）",
            "description": "",
            "fields": [
                _field("profit_taking.take_profit_up_deviation", "Up 止盈偏离", "", ui="range", min=0.01, max=0.6, step=0.01, risk="low"),
                _field("profit_taking.take_profit_down_deviation", "Down 止盈偏离", "", ui="range", min=0.01, max=0.6, step=0.01, risk="low"),
                _field("profit_taking.take_profit_fraction", "止盈比例", "", ui="range", min=0.05, max=1.0, step=0.05, risk="medium"),
            ],
        },
        {
            "title": "stop_loss（止损，全量）",
            "description": "周期熔断、分阶段单仓止损与高波动止损。",
            "fields": [
                _field("stop_loss.stop_loss_cycle_loss", "周期亏损熔断阈值", "", ui="range", min=1, max=200, step=1, risk="high"),
                _field("stop_loss.stop_loss_fraction", "熔断平仓比例", "", ui="range", min=0.05, max=1.0, step=0.05, risk="high"),
                _field("stop_loss.pre_phase_4_max_exit_fraction", "阶段4前最大减仓比例", "", ui="range", min=0.5, max=1.0, step=0.01, risk="high"),
                _field("stop_loss.pre_phase_4_min_remaining_shares", "阶段4前最少保留份额", "", min=0.0, max=10.0, step=0.05, risk="medium"),
            ]
            + [
                item
                for n in range(1, 5)
                for item in (
                    _field(f"stop_loss.phase_{n}_near_zero_price", f"阶段{n} 近零价阈值", "触发2 条件A。", ui="range", min=0.01, max=0.5, step=0.01, risk="high"),
                    _field(f"stop_loss.phase_{n}_stop_loss_pct", f"阶段{n} 浮亏%阈值", "", ui="range", min=0.01, max=0.5, step=0.01, risk="high"),
                    _field(
                        f"stop_loss.phase_{n}_min_hold_seconds",
                        f"阶段{n} 条件B 本阶段最短秒数",
                        "进入该阶段后起算，与周期总时长无关。",
                        min=0,
                        max=300,
                        step=1,
                        risk="medium",
                    ),
                    _field(f"stop_loss.phase_{n}_stop_loss_action_fraction", f"阶段{n} 止损动作比例", "", ui="range", min=0.05, max=1.0, step=0.05, risk="high"),
                    _field(f"stop_loss.phase_{n}_high_vol_trigger_ratio", f"阶段{n} 高波动 ratio 阈值", "", ui="range", min=0.5, max=5.0, step=0.1, risk="medium"),
                    _field(f"stop_loss.phase_{n}_high_vol_price_threshold", f"阶段{n} 高波动价格阈值", "", ui="range", min=0.01, max=0.5, step=0.01, risk="medium"),
                    _field(f"stop_loss.phase_{n}_high_vol_action_fraction", f"阶段{n} 高波动减仓比例", "", ui="range", min=0.05, max=1.0, step=0.05, risk="high"),
                )
            ]
            + [
                _field("stop_loss.stop_loss_pct", "全局止损%兜底", "", ui="range", min=0.01, max=0.5, step=0.01, risk="medium"),
                _field("stop_loss.high_vol_stop_loss_pct", "旧版高波动%（兼容）", "", ui="range", min=0.0, max=0.2, step=0.001, risk="low"),
            ],
        },
        {
            "title": "last_minute（尾盘）",
            "description": "",
            "fields": [
                _field("last_minute.last_minute_min_confidence", "最小信心", "", ui="range", min=0.5, max=0.99, step=0.01, risk="high"),
                _field("last_minute.tail_profit_scale", "盈利放大系数", "", ui="range", min=0.0, max=1.0, step=0.01, risk="medium"),
                _field("last_minute.tail_volatility_scale", "波动放大系数", "", min=0, max=100, step=1, risk="medium"),
                _field("last_minute.max_tail_exposure", "尾盘最大敞口", "", ui="range", min=0, max=500, step=1, risk="high"),
                _field("last_minute.preferred_leg_min_ratio", "优势侧最小份额倍率", "", ui="range", min=1.0, max=3.0, step=0.05, risk="high"),
                _field("last_minute.direction_ratio_threshold", "方向确认·仓位倍率", "", ui="range", min=1.0, max=5.0, step=0.1, risk="high"),
                _field("last_minute.pnl_ratio_threshold", "方向确认·浮盈倍率", "", ui="range", min=1.0, max=5.0, step=0.1, risk="high"),
                _field("last_minute.min_trend_strength_for_direction", "方向确认·最小趋势强度", "", ui="range", min=0.0, max=1.0, step=0.01, risk="high"),
                _field("last_minute.conservative_max_exposure", "无法确认方向时最大敞口", "", ui="range", min=0, max=200, step=1, risk="high"),
            ],
        },
        {
            "title": "execution（执行与滑点）",
            "description": "不含 market_limits（见 daily_limits）。",
            "fields": [
                _field("execution.fee_rate", "手续费率", "", ui="range", min=0.0, max=0.02, step=0.0005, risk="medium"),
                _field("execution.slippage_bps", "滑点 bps", "", ui="select", options=[0, 5, 10, 15, 20, 30], risk="medium"),
                _field("execution.min_seconds_between_orders", "最小下单间隔（秒）", "", ui="range", min=0, max=60, step=0.5, risk="medium"),
                _field("execution.max_same_direction_buy_fills_per_second", "同秒同向买成交上限", "0 表示不限制。", min=0, max=50, step=1, risk="medium"),
                _field("execution.min_same_outcome_price_move_ratio", "同向最小价格波动比例", "", ui="range", min=0.0, max=0.2, step=0.001, risk="medium"),
            ],
        },
        {
            "title": "rule_price_feed（规则喂价间隔）",
            "description": "",
            "fields": [
                _field("rule_price_feed.last_minute", "尾盘", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.entries.opening", "opening", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.entries.hedge", "hedge 入场", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.entries.grid", "grid 入场", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.entries.mean_reversion", "mean_reversion 入场", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.entries.trend", "trend", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.exits.stop_loss", "stop_loss", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.exits.hedge", "hedge 离场", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.exits.take_profit", "take_profit", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.exits.grid", "grid 离场", "", ui="range", min=0, max=30, step=0.5, risk="low"),
                _field("rule_price_feed.exits.mean_reversion", "mean_reversion 离场", "", ui="range", min=0, max=30, step=0.5, risk="low"),
            ],
        },
    ]


def collect_strategy_schema_paths() -> set[str]:
    paths: set[str] = set()
    for sec in build_strategy_dashboard_sections():
        for f in sec.get("fields", []):
            p = f.get("path")
            if isinstance(p, str):
                paths.add(p)
    return paths
