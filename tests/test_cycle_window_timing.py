from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polynet_ai.adapters.cycle_window_timing import (
    filter_trade_events_after_post_window_delay,
    next_bucket_start_utc,
    parse_window_start_epoch_from_slug,
    trade_event_passes_post_window_delay,
)
from polynet_ai.domain.models import TradeEvent


def test_parse_window_start_epoch_from_slug() -> None:
    assert parse_window_start_epoch_from_slug("btc-updown-5m-1700000000") == 1700000000
    assert parse_window_start_epoch_from_slug("btc-updown-5m-1700000000-extra") is None


def test_trade_event_passes_post_window_delay() -> None:
    base = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).replace(tzinfo=None)
    early = base + timedelta(seconds=5)
    late = base + timedelta(seconds=15)
    ev_early = TradeEvent(
        market_id="m",
        cycle_id="btc-updown-5m-1700000000",
        timestamp=early,
        price=0.5,
        shares=1.0,
        outcome="up",
        action="buy",
    )
    ev_late = TradeEvent(
        market_id="m",
        cycle_id="btc-updown-5m-1700000000",
        timestamp=late,
        price=0.5,
        shares=1.0,
        outcome="up",
        action="buy",
    )
    assert not trade_event_passes_post_window_delay(ev_early, 10.0)
    assert trade_event_passes_post_window_delay(ev_late, 10.0)
    assert trade_event_passes_post_window_delay(ev_early, 0.0)

    too_late = base + timedelta(seconds=301)
    ev_too_late = TradeEvent(
        market_id="m",
        cycle_id="btc-updown-5m-1700000000",
        timestamp=too_late,
        price=0.5,
        shares=1.0,
        outcome="up",
        action="buy",
    )
    assert not trade_event_passes_post_window_delay(ev_too_late, 10.0)
    assert not trade_event_passes_post_window_delay(ev_too_late, 0.0)


def test_filter_trade_events_after_post_window_delay() -> None:
    base = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).replace(tzinfo=None)
    early = base + timedelta(seconds=5)
    late = base + timedelta(seconds=15)
    events = [
        TradeEvent(
            market_id="m",
            cycle_id="btc-updown-5m-1700000000",
            timestamp=early,
            price=0.5,
            shares=1.0,
            outcome="up",
            action="buy",
        ),
        TradeEvent(
            market_id="m",
            cycle_id="btc-updown-5m-1700000000",
            timestamp=late,
            price=0.5,
            shares=1.0,
            outcome="up",
            action="buy",
        ),
    ]
    out = filter_trade_events_after_post_window_delay(events, post_window_start_delay_seconds=10.0)
    assert len(out) == 1
    assert out[0].timestamp == late


def test_filter_trade_events_strict_cycle_upper_bound() -> None:
    base = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).replace(tzinfo=None)
    in_cycle = base + timedelta(seconds=300)
    out_cycle = base + timedelta(seconds=301)
    events = [
        TradeEvent(
            market_id="m",
            cycle_id="btc-updown-5m-1700000000",
            timestamp=in_cycle,
            price=0.5,
            shares=1.0,
            outcome="up",
            action="buy",
        ),
        TradeEvent(
            market_id="m",
            cycle_id="btc-updown-5m-1700000000",
            timestamp=out_cycle,
            price=0.5,
            shares=1.0,
            outcome="up",
            action="buy",
        ),
    ]
    out = filter_trade_events_after_post_window_delay(events, post_window_start_delay_seconds=0.0)
    assert len(out) == 1
    assert out[0].timestamp == in_cycle


def test_next_bucket_start_utc_exact_boundary() -> None:
    cycle_seconds = 300
    ts = 1_700_000_100
    assert ts % cycle_seconds == 0
    b = datetime.fromtimestamp(ts, tz=timezone.utc)
    n = next_bucket_start_utc(b, cycle_seconds)
    assert int(n.timestamp()) == ts
