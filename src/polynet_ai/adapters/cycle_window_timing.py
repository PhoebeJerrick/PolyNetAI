"""Polymarket 等「时间桶」市场的周期对齐：UTC 边界精确等待、窗起点后策略生效推迟。

实盘 / 回放 / 批量脚本共用，保证「窗绝对起点 + N 秒」后再向策略投递成交的逻辑一致。
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

from polynet_ai.domain.models import TradeEvent

DEFAULT_POST_WINDOW_START_DELAY_SECONDS = 10.0
DEFAULT_CYCLE_SECONDS = 300

_SLUG_EPOCH_SUFFIX = re.compile(r"-updown-\d+m-(\d+)$")

T = TypeVar("T")


def cycle_seconds_from_market_slug(slug: str) -> int | None:
    match = re.search(r"-updown-(\d+)m-", slug.strip())
    if match is None:
        return None
    minutes = int(match.group(1))
    return minutes * 60 if minutes > 0 else None


def cycle_seconds_from_slug_prefix(slug_prefix: str) -> int | None:
    match = re.fullmatch(r"([a-z0-9-]+)-updown-(\d+)m-", slug_prefix.strip())
    if match is None:
        return None
    minutes = int(match.group(2))
    return minutes * 60 if minutes > 0 else None


def parse_window_start_epoch_from_slug(slug: str) -> int | None:
    m = _SLUG_EPOCH_SUFFIX.search(slug.strip())
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def window_start_naive_utc_from_slug(slug: str) -> datetime | None:
    ep = parse_window_start_epoch_from_slug(slug)
    if ep is None:
        return None
    return datetime.fromtimestamp(ep, tz=timezone.utc).replace(tzinfo=None)


def naive_utc_to_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def resolve_polymarket_window_start_naive_utc(slug: str, api_start: datetime | None) -> datetime | None:
    from_slug = window_start_naive_utc_from_slug(slug)
    if from_slug is not None:
        return from_slug
    return api_start


def effective_strategy_start_naive_utc(
    slug: str,
    api_start: datetime | None,
    post_window_start_delay_seconds: float,
) -> datetime | None:
    if post_window_start_delay_seconds <= 0:
        return None
    base = resolve_polymarket_window_start_naive_utc(slug, api_start)
    if base is None:
        return None
    return base + timedelta(seconds=post_window_start_delay_seconds)


def next_bucket_start_utc(now_utc: datetime, cycle_seconds: int) -> datetime:
    """下一个时间桶的 UTC 起点；若当前恰在桶边界上则返回当前桶起点。"""
    epoch_seconds = now_utc.timestamp()
    current_bucket = math.floor(epoch_seconds / cycle_seconds) * cycle_seconds
    is_exact_boundary = abs(epoch_seconds - current_bucket) < 1e-6
    target_ts = current_bucket if is_exact_boundary else current_bucket + cycle_seconds
    return datetime.fromtimestamp(target_ts, tz=timezone.utc)


def current_or_next_bucket_start_utc(
    now_utc: datetime,
    cycle_seconds: int,
    max_late_join_seconds: float,
) -> datetime:
    """优先返回当前正在进行中的 bucket（如果已过时间 <= max_late_join_seconds），
    否则返回下一个 bucket。用于连续多周期执行时避免跳过刚开始的 bucket。"""
    epoch_seconds = now_utc.timestamp()
    current_bucket_ts = math.floor(epoch_seconds / cycle_seconds) * cycle_seconds
    elapsed_in_bucket = epoch_seconds - current_bucket_ts
    if elapsed_in_bucket <= max_late_join_seconds:
        return datetime.fromtimestamp(current_bucket_ts, tz=timezone.utc)
    return datetime.fromtimestamp(current_bucket_ts + cycle_seconds, tz=timezone.utc)


def sleep_until_utc_instant(deadline_utc: datetime) -> None:
    """睡到 wall clock 到达 ``deadline_utc``（timezone-aware UTC），末段细粒度 sleep。"""
    deadline_ts = deadline_utc.timestamp()
    while True:
        now = time.time()
        remaining = deadline_ts - now
        if remaining <= 0:
            return
        if remaining > 2.0:
            chunk = min(remaining - 0.2, 30.0)
            time.sleep(chunk)
            continue
        if remaining > 0.2:
            time.sleep(min(remaining * 0.5, 0.05))
            continue
        if remaining > 0.02:
            time.sleep(min(remaining, 0.005))
            continue
        time.sleep(min(remaining, 0.0005))


def poll_until_success(
    fn: Callable[[], T],
    *,
    timeout_seconds: float = 120.0,
    interval_seconds: float = 0.35,
    log_fn: Callable[[str], None] | None = None,
    describe: str = "操作",
) -> T:
    deadline = time.monotonic() + max(0.01, timeout_seconds)
    attempt = 0
    last_exc: BaseException | None = None
    while time.monotonic() < deadline:
        attempt += 1
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            if log_fn is not None and (attempt == 1 or attempt % 8 == 0):
                log_fn(f"[cycle] {describe} 尚未就绪（第 {attempt} 次）: {exc}")
            time.sleep(min(interval_seconds, max(0.05, deadline - time.monotonic())))
    raise RuntimeError(f"{describe} 超时（{timeout_seconds:.0f}s）") from last_exc


def trade_event_passes_post_window_delay(event: TradeEvent, post_window_start_delay_seconds: float) -> bool:
    """事件是否在有效窗口内：``[window_start + delay, window_start + cycle_seconds]``。"""
    ws = window_start_naive_utc_from_slug(event.cycle_id)
    if ws is None:
        return True
    cycle_seconds = cycle_seconds_from_market_slug(event.cycle_id) or DEFAULT_CYCLE_SECONDS
    elapsed = (event.timestamp - ws).total_seconds()
    if elapsed > float(cycle_seconds):
        return False
    if post_window_start_delay_seconds <= 0:
        return True
    return elapsed >= float(post_window_start_delay_seconds)


def filter_trade_events_after_post_window_delay(
    events: list[TradeEvent],
    *,
    post_window_start_delay_seconds: float,
) -> list[TradeEvent]:
    """仅保留有效窗口内事件：``[window_start + delay, window_start + cycle_seconds]``。"""
    if not events:
        return events
    return [e for e in events if trade_event_passes_post_window_delay(e, post_window_start_delay_seconds)]
