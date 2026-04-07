from __future__ import annotations

import re
from datetime import datetime, timezone

_SLUG_EPOCH_SUFFIX = re.compile(r"-updown-\d+m-(\d+)$")


def cycle_seconds_from_market_slug(slug: str) -> int | None:
    match = re.search(r"-updown-(\d+)m-", slug.strip())
    if match is None:
        return None
    minutes = int(match.group(1))
    return minutes * 60 if minutes > 0 else None


def parse_window_start_epoch_from_slug(slug: str) -> int | None:
    match = _SLUG_EPOCH_SUFFIX.search(slug.strip())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def window_start_naive_utc_from_slug(slug: str) -> datetime | None:
    epoch = parse_window_start_epoch_from_slug(slug)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)