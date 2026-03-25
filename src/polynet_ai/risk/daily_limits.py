"""每日亏损限制管理模块"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass


@dataclass(slots=True)
class DailyLimitStatus:
    """每日限制状态"""
    today_loss: float = 0.0
    daily_limit: float = 50.0
    is_paused: bool = False
    last_update: datetime | None = None
    warning_sent: bool = False
    reduction_sent: bool = False
    
    @property
    def loss_ratio(self) -> float:
        """当日亏损占限额的比例"""
        if self.daily_limit <= 0:
            return 0.0
        return abs(self.today_loss) / self.daily_limit
    
    @property
    def is_warning_level(self) -> bool:
        """是否达到警告水位 (70%)"""
        return self.loss_ratio >= 0.7
    
    @property
    def is_limit_reached(self) -> bool:
        """是否达到每日限额"""
        return self.loss_ratio >= 1.0
    
    def reset_for_new_day(self) -> None:
        """重置为新的一天"""
        self.today_loss = 0.0
        self.is_paused = False
        self.warning_sent = False
        self.reduction_sent = False
        self.last_update = None


class DailyLimitManager:
    """每日风险管理"""
    
    def __init__(self, daily_limit: float = 50.0, auto_pause: bool = True, warning_threshold: float = 0.7):
        """
        初始化每日限制管理器
        
        Args:
            daily_limit: 每日最大亏损限额 (美元)
            auto_pause: 达到限额时是否自动暂停交易
            warning_threshold: 警告阈值 (占限额比例, 0-1)
        """
        self.daily_limit = daily_limit
        self.auto_pause = auto_pause
        self.warning_threshold = warning_threshold
        self.status = DailyLimitStatus(daily_limit=daily_limit)
        self.last_reset_date: datetime | None = None
    
    def update_loss(self, cycle_profit: float, current_time: datetime | None = None) -> None:
        """
        更新当日累计亏损
        
        Args:
            cycle_profit: 本周期利润 (可以是负数表示亏损)
            current_time: 当前时间，如果为None则使用系统时间
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)
        
        # 检查是否需要重置为新的一天
        if self._should_reset_for_new_day(current_time):
            self.status.reset_for_new_day()
            self.last_reset_date = current_time.date() if hasattr(current_time, 'date') else current_time
        
        # 更新亏损记录
        self.status.today_loss += min(0, cycle_profit)  # 只累计负数(亏损)
        self.status.last_update = current_time
    
    def check_daily_limit(self) -> bool:
        """
        检查是否超过每日限额
        
        Returns:
            True 表示未超限可继续交易，False 表示已超限应停止交易
        """
        if self.status.is_limit_reached and self.auto_pause:
            self.status.is_paused = True
            return False
        return True
    
    def should_reduce_positions(self) -> bool:
        """
        检查是否需要降低头寸大小
        
        Returns:
            True 表示应该降低头寸大小
        """
        if self.status.is_warning_level and not self.status.reduction_sent:
            return True
        return False
    
    def should_send_warning(self) -> bool:
        """
        检查是否需要发送警告
        
        Returns:
            True 表示应该发送警告
        """
        if self.status.is_warning_level and not self.status.warning_sent:
            self.status.warning_sent = True
            return True
        if self.status.is_limit_reached and not self.status.reduction_sent:
            self.status.reduction_sent = True
            return True
        return False
    
    def get_position_scale(self) -> float:
        """
        获取当前头寸规模缩放因子 (0-1)
        
        根据每日亏损情况动态调整头寸大小:
        - 0-70%: 1.0 (正常)
        - 70-100%: 0.5 (降低50%)
        - >100%: 0.0 (暂停)
        
        Returns:
            头寸规模缩放因子
        """
        ratio = self.status.loss_ratio
        
        if ratio > 1.0:
            return 0.0  # 暂停交易
        elif ratio >= self.warning_threshold:
            return 0.5  # 半仓运行
        else:
            return 1.0  # 正常运行
    
    def get_status_message(self) -> str:
        """获取状态信息"""
        ratio_pct = self.status.loss_ratio * 100
        return f"Daily Loss: ${abs(self.status.today_loss):.2f}/{self.daily_limit:.0f} ({ratio_pct:.1f}%)"
    
    def _should_reset_for_new_day(self, current_time: datetime) -> bool:
        """检查是否应该重置为新的一天"""
        if self.last_reset_date is None:
            return True
        
        # 比较日期
        current_date = current_time.date() if hasattr(current_time, 'date') else current_time
        if current_date != self.last_reset_date:
            return True
        
        return False
