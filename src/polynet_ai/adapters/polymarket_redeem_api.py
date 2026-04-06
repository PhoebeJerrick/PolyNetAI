"""
Polymarket Data API：可赎回持仓（redeemable positions）。

文档与端点与 Polymarket 前端 / 官方 Data API 一致：`GET /positions?user=...&redeemable=true`。
"""
from __future__ import annotations

import json
import time
from typing import Any

import requests

DATA_API_BASE = "https://data-api.polymarket.com"

# 与 Gamma `_fetch_json` 类似：应对偶发 TCP 重置 / 中间设备断连（如 Windows 10054）。
_REDEEM_POSITIONS_RETRY_DELAYS_SEC = (0.0, 0.6, 1.2, 2.4)


def fetch_redeemable_positions_aggregated(
    user_address: str,
    *,
    limit: int = 500,
    session: requests.Session | None = None,
    timeout: tuple[float, float] = (15.0, 60.0),
) -> list[dict[str, Any]]:
    """
    拉取用户当前可 redeem 的持仓，并按 `condition_id` 聚合。

    每项含：condition_id, slug, expected_payout, size, positions（原始行列表）。
    """
    addr = (user_address or "").strip()
    if not addr.startswith("0x"):
        return []
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", "PolyNetAI/1.0 (+redeem)")

    last_exc: BaseException | None = None
    data: Any | None = None
    for attempt, delay_sec in enumerate(_REDEEM_POSITIONS_RETRY_DELAYS_SEC):
        if delay_sec:
            time.sleep(delay_sec)
        try:
            resp = sess.get(
                f"{DATA_API_BASE}/positions",
                params={"user": addr, "redeemable": "true", "limit": int(limit)},
                timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json()
        except (
            requests.exceptions.RequestException,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            last_exc = exc
            if attempt == len(_REDEEM_POSITIONS_RETRY_DELAYS_SEC) - 1:
                raise
            continue
        if not isinstance(raw, list):
            return []
        data = raw
        break

    if data is None:
        if last_exc is not None:
            raise last_exc
        return []

    by_cond: dict[str, dict[str, Any]] = {}
    for p in data:
        if not isinstance(p, dict):
            continue
        try:
            raw_sz = p.get("size")
            size = float(raw_sz) if raw_sz is not None else 0.0
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        cid_raw = p.get("conditionId") or p.get("condition_id") or ""
        if not cid_raw or not isinstance(cid_raw, str):
            continue
        cid = cid_raw if cid_raw.startswith("0x") else "0x" + cid_raw
        if cid not in by_cond:
            slug_val = p.get("slug") or p.get("market_slug") or ""
            if not slug_val and isinstance(p.get("market"), dict):
                slug_val = str((p["market"] or {}).get("slug") or "")
            by_cond[cid] = {
                "condition_id": cid,
                "slug": slug_val,
                "expected_payout": 0.0,
                "size": 0.0,
                "positions": [],
            }
        by_cond[cid]["expected_payout"] += size
        by_cond[cid]["size"] += size
        by_cond[cid]["positions"].append(p)
    return list(by_cond.values())
