from __future__ import annotations

import json
import os
import queue as _queue_mod
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from polynet_ai.adapters.cycle_window_timing import (
    DEFAULT_CYCLE_SECONDS,
    cycle_seconds_from_market_slug,
    DEFAULT_POST_WINDOW_START_DELAY_SECONDS,
    effective_strategy_start_naive_utc,
    naive_utc_to_aware_utc,
    sleep_until_utc_instant,
    window_start_naive_utc_from_slug,
)
from polynet_ai.domain.models import TradeEvent

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_MARKET_WS_HOST = "ws-subscriptions-clob.polymarket.com"

# ---------------------------------------------------------------------------
# DNS 预解析缓存 — 避免每次重连都走代理/系统 DNS，减少 60-140s 的重连空窗
# ---------------------------------------------------------------------------
_dns_cache: dict[str, tuple[str, float]] = {}  # host -> (ip, expire_monotonic)
_DNS_CACHE_TTL = 120.0  # 秒


def _resolve_host_cached(host: str, port: int = 443) -> str | None:
    """返回缓存的 IP 地址；缓存失效或解析失败时返回 None（回退到让 websocket-client 自行解析）。"""
    cached = _dns_cache.get(host)
    if cached is not None and time.monotonic() < cached[1]:
        return cached[0]
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        if infos:
            ip = infos[0][4][0]
            _dns_cache[host] = (ip, time.monotonic() + _DNS_CACHE_TTL)
            return ip
    except OSError:
        pass
    return None


def _ws_error_detail(exc: BaseException, *, max_len: int = 220) -> str:
    """将 WebSocket 相关异常压缩为一行，便于日志区分网络/代理问题与程序错误。"""
    text = f"{exc.__class__.__name__}: {exc}"
    text = " ".join(text.split())
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


_HTTP_HEADERS = {
    "User-Agent": "PolyNetAI/0.1 (+https://polymarket.com)",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
}


def _http_verify() -> bool | str:
    """是否校验 HTTPS 证书。默认 True。仅在网络/代理导致握手失败时设为 false（不安全）。"""
    v = os.environ.get("POLYNET_HTTP_VERIFY", "true").strip().lower()
    if v in {"0", "false", "no"}:
        return False
    return True


def _ws_ssl_opts() -> dict[str, Any] | None:
    """WebSocket TLS。仅在握手失败且你清楚风险时使用 POLYNET_WS_SSL_VERIFY=false。"""
    v = os.environ.get("POLYNET_WS_SSL_VERIFY", "true").strip().lower()
    if v in {"0", "false", "no"}:
        return {"cert_reqs": ssl.CERT_NONE}
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def _parse_json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    raise ValueError(f"无法解析 JSON 列表: {value!r}")


def _fetch_json_urllib(url: str, *, timeout: float = 30.0) -> Any:
    """标准库 HTTPS，部分环境下 TLS 不如 requests 稳定。"""
    ctx = ssl.create_default_context()
    request = Request(url, headers=dict(_HTTP_HEADERS))
    with urlopen(request, timeout=timeout, context=ctx) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def _fetch_json_requests(url: str) -> Any:
    import requests

    verify = _http_verify()
    response = requests.get(
        url,
        headers=_HTTP_HEADERS,
        timeout=(10, 45),
        verify=verify,
    )
    response.raise_for_status()
    return response.json()


def _fetch_json_once(url: str) -> Any:
    try:
        return _fetch_json_requests(url)
    except ImportError:
        return _fetch_json_urllib(url)


def _fetch_json(url: str) -> Any:
    """
    拉取 Gamma API JSON。优先使用 requests（TLS/连接更稳），失败时重试数次。
    仍失败则回退 urllib 再试一次（便于对比环境问题）。

    环境变量：
    - HTTPS_PROXY / HTTP_PROXY：系统代理
    - POLYNET_HTTP_VERIFY=false：跳过证书校验（仅排查用，不安全）
    """
    last: Exception | None = None
    delays = (0.0, 0.6, 1.2, 2.4)
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            return _fetch_json_once(url)
        except Exception as exc:
            last = exc
            if attempt == len(delays) - 1:
                break

    try:
        return _fetch_json_urllib(url)
    except Exception as urllib_exc:
        hint = (
            "无法访问 Polymarket Gamma API（HTTPS）。"
            "请检查：网络/VPN、系统代理（HTTPS_PROXY）、"
            "或临时设置 POLYNET_HTTP_VERIFY=false 仅作排查（不安全）。"
        )
        raise RuntimeError(hint) from (last if last is not None else urllib_exc)


def default_api_config_env_candidates(root: Path) -> tuple[Path, ...]:
    """相对仓库根目录 `root` 的 ApiConfig.env 默认搜索路径（按顺序）。

    常见布局（与 `record.sh` 一致）：
    1. ``<workspace>/APIs/ApiConfig.env`` — 与 ``PolyMkt`` 同级，例如 ``Projects/PolyMkt/PolyNetAI`` → ``Projects/APIs``；
    2. ``<PolyMkt>/APIs/ApiConfig.env`` — 与仓库同在一级目录下；
    3. ``<repo>/APIs/ApiConfig.env`` — 仅在本仓库内。
    """
    return (
        root.parent.parent / "APIs" / "ApiConfig.env",
        root.parent / "APIs" / "ApiConfig.env",
        root / "APIs" / "ApiConfig.env",
    )


def resolve_default_api_config_env(root: Path) -> str:
    """返回第一个存在的默认 ApiConfig.env 路径；均不存在时返回首选路径字符串。"""
    for candidate in default_api_config_env_candidates(root):
        if candidate.exists():
            return str(candidate.resolve())
    return str(default_api_config_env_candidates(root)[0].resolve())


def load_api_env(path: str | Path) -> dict[str, str]:
    """
    解析 `ApiConfig.env` 类文件（KEY=value，一行一项）。

    兼容常见写法：
    - UTF-8 BOM（utf-8-sig）；
    - shell 风格 `export KEY=value` / `export KEY = value`（会去掉前缀 export）。
    """
    values: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        return values
    text = env_path.read_text(encoding="utf-8-sig")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
            if not line or line.startswith("#") or "=" not in line:
                continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip()
    return values


def select_account_env(values: dict[str, str], account_index: int = 2) -> dict[str, str]:
    """
    从 `ApiConfig.env` 中挑选指定账号的配置，并将 `FOO_<N>` 映射为无后缀键 `FOO`（供仍读裸键的下游使用）。

    多账户实盘约定（默认 N=2）：
    - CLOB/钱包：`PURSE_PRIVATE_KEY_2`、`PURSE_ADDRESS_2`、`POLY_DERIVE_API_KEY_2`、
      `POLY_DERIVE_API_SECRET_2`、`POLY_DERIVE_API_PASSPHRASE_2`；
    - 账号 1 则用 `_1` 后缀；`--account-index` 与后缀 N 一致。

    规则：
    - 保留原始所有键；
    - 若存在 `<KEY>_<account_index>`，则额外写入 `<KEY>`（覆盖同名裸键，以当前账号为准）；
    - 默认账号 2，与 `get_account_env_value` 默认一致。
    """
    selected = dict(values)
    suffix = f"_{int(account_index)}"
    for key, value in values.items():
        if key.endswith(suffix) and len(key) > len(suffix):
            selected[key[: -len(suffix)]] = value
    return selected


def get_account_env_value(
    values: dict[str, str],
    key: str,
    *,
    account_index: int = 2,
    default: str | None = None,
) -> str | None:
    """
    读取某配置项：优先 `<key>_<account_index>`（如 `PURSE_ADDRESS_2`），否则回退裸 `<key>`（兼容旧单账户文件）。
    """
    suffix_key = f"{key}_{int(account_index)}"
    if suffix_key in values:
        return values[suffix_key]
    if key in values:
        return values[key]
    return default


def account_env_keys_for_index(keys: Iterable[str], account_index: int) -> list[str]:
    """将逻辑键名转为带账号后缀的 ApiConfig 键名列表（用于报错提示）。"""
    n = int(account_index)
    return [f"{k}_{n}" for k in keys]


def _strip_env_value(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {'"', "'"}:
        s = s[1:-1]
    return s.strip()


def apply_proxy_env_from_dict(values: dict[str, str], *, overwrite: bool = True) -> list[str]:
    """
    将 ApiConfig.env 中的代理宏写入 `os.environ`，供 `requests` / `urllib` 使用。

    支持的键（不区分大小写）：HTTP_PROXY、HTTPS_PROXY、ALL_PROXY、NO_PROXY、WS_PROXY。
    - HTTP(S)_PROXY / ALL_PROXY / NO_PROXY：同时写入大写与小写键，兼容不同库。
    - WS_PROXY：供 `websocket-client` 显式走 HTTP CONNECT 代理（见 `_websocket_proxy_kwargs`）。

    若某键在 `os.environ` 中已存在且 `overwrite=False`，则跳过。
    """
    upper_map: dict[str, str] = {}
    for k, v in values.items():
        ku = k.strip().upper()
        if ku not in upper_map:
            upper_map[ku] = v

    proxy_canon = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "WS_PROXY")
    applied: list[str] = []

    for canon in proxy_canon:
        if canon not in upper_map:
            continue
        val = _strip_env_value(upper_map[canon])
        if not val:
            continue
        if not overwrite and canon in os.environ:
            continue
        os.environ[canon] = val
        applied.append(canon)
        if canon != "WS_PROXY":
            os.environ[canon.lower()] = val

    return applied


def _websocket_proxy_kwargs() -> dict[str, Any]:
    """
    为 `websocket.create_connection` 构造 HTTP 代理参数。
    优先 `WS_PROXY`，否则 `HTTPS_PROXY` / `ALL_PROXY`（与常见工具一致）。
    """
    raw = (
        os.environ.get("WS_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
    )
    if not raw:
        return {}
    raw = _strip_env_value(raw)
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return {}
    host = parsed.hostname
    if not host:
        return {}
    port = int(parsed.port or 80)
    out: dict[str, Any] = {"http_proxy_host": host, "http_proxy_port": port}
    if parsed.username:
        out["http_proxy_auth"] = (parsed.username, parsed.password or "")
    return out


@dataclass(slots=True)
class PolymarketMarketSpec:
    slug: str
    series_slug: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    start_time: datetime | None
    end_time: datetime | None
    raw: dict[str, Any]

    @property
    def asset_ids(self) -> list[str]:
        return [self.yes_token_id, self.no_token_id]

    @property
    def token_to_outcome(self) -> dict[str, str]:
        return {
            self.yes_token_id: "up",
            self.no_token_id: "down",
        }

    @classmethod
    def from_market_json(cls, payload: dict[str, Any]) -> "PolymarketMarketSpec":
        outcomes = [item.lower() for item in _parse_json_list(payload.get("outcomes", []))]
        token_ids = _parse_json_list(payload.get("clobTokenIds", []))
        if len(outcomes) != 2 or len(token_ids) != 2:
            raise ValueError(f"市场 {payload.get('slug', '')} 不是标准二元市场。")

        outcome_to_token = dict(zip(outcomes, token_ids, strict=False))
        yes_token = outcome_to_token.get("yes") or outcome_to_token.get("up")
        no_token = outcome_to_token.get("no") or outcome_to_token.get("down")
        if not yes_token or not no_token:
            raise ValueError(f"市场 {payload.get('slug', '')} 缺少 Up/Down 或 Yes/No token。")

        events = payload.get("events") or []
        series_slug = ""
        if events and isinstance(events, list) and isinstance(events[0], dict):
            series_slug = str(events[0].get("seriesSlug") or "")

        slug = str(payload.get("slug") or "")
        return cls(
            slug=slug,
            series_slug=series_slug or slug,
            condition_id=str(payload.get("conditionId") or payload.get("id") or slug),
            yes_token_id=str(yes_token),
            no_token_id=str(no_token),
            start_time=_parse_datetime(payload.get("eventStartTime") or payload.get("startDate")),
            end_time=_parse_datetime(payload.get("endDate")),
            raw=payload,
        )


def fetch_market_spec(slug: str) -> PolymarketMarketSpec:
    payload = _fetch_json(f"{GAMMA_API_BASE}/markets/slug/{quote(slug)}")
    if not isinstance(payload, dict):
        raise ValueError(f"未获取到市场详情: {slug}")
    return PolymarketMarketSpec.from_market_json(payload)


def _best_price_size(levels: Iterable[Any], *, reverse: bool) -> tuple[float | None, float | None]:
    ranked = sorted(
        (item for item in levels if item is not None),
        key=lambda item: float(getattr(item, "price", 0.0) or 0.0),
        reverse=reverse,
    )
    for level in ranked:
        price = float(getattr(level, "price", 0.0) or 0.0)
        size = float(getattr(level, "size", 0.0) or 0.0)
        if price > 0 and size > 0:
            return price, size
    return None, None


def _top_of_book_metadata(prefix: str, book: Any) -> dict[str, Any]:
    bid_price, bid_size = _best_price_size(getattr(book, "bids", ()) or (), reverse=True)
    ask_price, ask_size = _best_price_size(getattr(book, "asks", ()) or (), reverse=False)
    return {
        f"{prefix}_bid1_price": bid_price,
        f"{prefix}_bid1_size": bid_size,
        f"{prefix}_ask1_price": ask_price,
        f"{prefix}_ask1_size": ask_size,
    }


def _empty_top_of_book_metadata(prefix: str) -> dict[str, Any]:
    return {
        f"{prefix}_bid1_price": None,
        f"{prefix}_bid1_size": None,
        f"{prefix}_ask1_price": None,
        f"{prefix}_ask1_size": None,
    }


def _is_missing_clob_orderbook(exc: BaseException) -> bool:
    """
    CLOB `get_order_book` 对部分 token 返回 404，文案常为「No orderbook exists for the requested token id」。
    这与 WS 上仍有 last_trade_price 并不矛盾（一侧无挂单簿、簿尚未就绪、或 Gamma/CLOB 短暂不一致时可见）。
    """
    code = getattr(exc, "status_code", None)
    if code == 404:
        return True
    parts: list[str] = []
    err = getattr(exc, "error_msg", None)
    if isinstance(err, dict):
        parts.append(json.dumps(err, ensure_ascii=False))
    elif err is not None:
        parts.append(str(err))
    parts.append(str(exc))
    blob = " ".join(parts).lower()
    if "no orderbook" in blob:
        return True
    if "orderbook" in blob and "not exist" in blob:
        return True
    return False


def _orderbook_top_metadata_safe(client: Any, prefix: str, token_id: str) -> dict[str, Any]:
    try:
        book = client.get_order_book(token_id)
        return _top_of_book_metadata(prefix, book)
    except Exception as exc:
        if _is_missing_clob_orderbook(exc):
            return _empty_top_of_book_metadata(prefix)
        raise


@dataclass(slots=True)
class OrderBookTopSnapshotEnricher:
    client: Any
    refresh_interval_seconds: float = 0.5
    log_fn: Callable[[str], None] | None = None
    _cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict, init=False)

    def enrich(self, event: TradeEvent, spec: PolymarketMarketSpec) -> dict[str, Any]:
        del event
        now = time.monotonic()
        refresh_interval = max(0.0, float(self.refresh_interval_seconds))
        cached = self._cache.get(spec.slug)
        if cached is not None and now - cached[0] < refresh_interval:
            snapshot = dict(cached[1])
            snapshot["orderbook_snapshot_age_ms"] = round((now - cached[0]) * 1000.0, 3)
            return snapshot

        try:
            payload = self._fetch_snapshot(spec)
        except Exception as exc:
            if self.log_fn is not None:
                self.log_fn(f"[polymarket] 拉取盘口快照失败 {spec.slug}: {_ws_error_detail(exc)}")
            if cached is None:
                return {}
            snapshot = dict(cached[1])
            snapshot["orderbook_snapshot_age_ms"] = round((now - cached[0]) * 1000.0, 3)
            snapshot["orderbook_snapshot_stale"] = True
            return snapshot

        self._cache[spec.slug] = (now, payload)
        snapshot = dict(payload)
        snapshot["orderbook_snapshot_age_ms"] = 0.0
        return snapshot

    def _fetch_snapshot(self, spec: PolymarketMarketSpec) -> dict[str, Any]:
        payload = {
            "orderbook_snapshot_source": "clob_order_book",
            "orderbook_snapshot_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }
        payload.update(_orderbook_top_metadata_safe(self.client, "up", spec.yes_token_id))
        payload.update(_orderbook_top_metadata_safe(self.client, "down", spec.no_token_id))
        return payload


def _discover_time_bucket_markets(slug_prefix: str, limit: int) -> list[PolymarketMarketSpec]:
    match = re.fullmatch(r"([a-z0-9-]+)-updown-(\d+)m-", slug_prefix)
    if match is None:
        return []

    cycle_seconds = int(match.group(2)) * 60
    if cycle_seconds <= 0:
        return []

    now_utc = datetime.now(timezone.utc)
    now = now_utc.replace(tzinfo=None)
    base_timestamp = int(now_utc.timestamp()) // cycle_seconds * cycle_seconds
    offsets = range(-1, max(limit + 2, 4))
    specs: list[PolymarketMarketSpec] = []

    for offset in offsets:
        slug = f"{slug_prefix}{base_timestamp + offset * cycle_seconds}"
        try:
            spec = fetch_market_spec(slug)
        except Exception:
            continue
        # 优先使用 slug 推导的时间窗判定是否已过期，避免 Gamma endDate 滞后导致旧窗被再次选中。
        slug_start = window_start_naive_utc_from_slug(spec.slug)
        if slug_start is not None and now >= slug_start + timedelta(seconds=cycle_seconds):
            continue
        if spec.end_time is not None and spec.end_time <= now:
            continue
        if spec.raw.get("acceptingOrders") is False:
            continue
        specs.append(spec)
        if len(specs) >= limit:
            break

    specs.sort(key=lambda item: (item.start_time or datetime.max, item.slug))
    return specs


def discover_active_markets(slug_prefix: str, limit: int, page_size: int = 100) -> list[PolymarketMarketSpec]:
    direct_specs = _discover_time_bucket_markets(slug_prefix=slug_prefix, limit=limit)
    if direct_specs:
        return direct_specs[:limit]

    matches: list[PolymarketMarketSpec] = []
    seen: set[str] = set()
    offset = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    while len(matches) < limit:
        query = urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
            }
        )
        payload = _fetch_json(f"{GAMMA_API_BASE}/markets?{query}")
        if not isinstance(payload, list) or not payload:
            break

        for market in payload:
            if not isinstance(market, dict):
                continue
            slug = str(market.get("slug") or "")
            if not slug.startswith(slug_prefix):
                continue
            if slug in seen:
                continue
            if market.get("acceptingOrders") is False:
                continue
            try:
                spec = PolymarketMarketSpec.from_market_json(market)
            except ValueError:
                continue
            cycle_seconds = cycle_seconds_from_market_slug(spec.slug) or DEFAULT_CYCLE_SECONDS
            slug_start = window_start_naive_utc_from_slug(spec.slug)
            # 对分页接口同样做 slug 时窗过滤，避免 API 活跃标记延迟把旧窗带进来。
            if slug_start is not None and now >= slug_start + timedelta(seconds=float(cycle_seconds)):
                continue
            # Gamma 有时仍返回「活跃」但 5 分钟窗已结束的市场；旧窗几乎不会有 last_trade_price，会导致 0 事件、0 周期。
            if spec.end_time is not None and spec.end_time <= now:
                continue
            matches.append(spec)
            seen.add(slug)
            if len(matches) >= limit:
                break

        offset += page_size

    def _window_sort_key(item: PolymarketMarketSpec) -> tuple:
        st, et = item.start_time, item.end_time
        if st is not None and et is not None and st <= now < et:
            return (0, st, item.slug)
        if et is not None and now < et:
            return (1, et, item.slug)
        if et is None:
            return (1, datetime.min, item.slug)
        return (2, item.start_time or datetime.min, item.slug)

    matches.sort(key=_window_sort_key)
    return matches[:limit]


def _import_websocket_module():
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - runtime safeguard
        raise RuntimeError(
            "缺少 `websocket-client` 依赖。请先执行 `python -m pip install websocket-client`。"
        ) from exc
    return websocket


def _iter_message_objects(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text or text in {"PING", "PONG"}:
        return []
    payload = json.loads(text)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _parse_ws_timestamp_naive_utc(value: object) -> datetime | None:
    try:
        ts_ms = int(str(value or "0"))
    except (TypeError, ValueError):
        return None
    if ts_ms <= 0:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def _parse_float_or_none(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f


def _extract_book_level_from_ws_message(
    message: dict[str, Any],
    spec: PolymarketMarketSpec,
) -> tuple[str, datetime, dict[str, float | None]] | None:
    """从 WS 消息中提取某一 outcome 的买一卖一（若消息携带盘口）。"""
    asset_id = str(message.get("asset_id") or "")
    outcome = spec.token_to_outcome.get(asset_id)
    if outcome is None:
        return None
    ts = _parse_ws_timestamp_naive_utc(message.get("timestamp"))
    if ts is None:
        return None

    # 兼容不同消息字段命名（best_bid / bid / bids[0] 等）。
    bid = (
        _parse_float_or_none(message.get("best_bid"))
        or _parse_float_or_none(message.get("bid"))
        or _parse_float_or_none(message.get("bestBid"))
    )
    ask = (
        _parse_float_or_none(message.get("best_ask"))
        or _parse_float_or_none(message.get("ask"))
        or _parse_float_or_none(message.get("bestAsk"))
    )
    bid_size = (
        _parse_float_or_none(message.get("best_bid_size"))
        or _parse_float_or_none(message.get("bid_size"))
        or _parse_float_or_none(message.get("bestBidSize"))
    )
    ask_size = (
        _parse_float_or_none(message.get("best_ask_size"))
        or _parse_float_or_none(message.get("ask_size"))
        or _parse_float_or_none(message.get("bestAskSize"))
    )
    if bid is None and ask is None:
        bids = message.get("bids")
        asks = message.get("asks")
        if isinstance(bids, list) and bids:
            topb = bids[0]
            if isinstance(topb, dict):
                bid = _parse_float_or_none(topb.get("price")) or bid
                bid_size = _parse_float_or_none(topb.get("size")) or bid_size
        if isinstance(asks, list) and asks:
            topa = asks[0]
            if isinstance(topa, dict):
                ask = _parse_float_or_none(topa.get("price")) or ask
                ask_size = _parse_float_or_none(topa.get("size")) or ask_size
    if bid is None and ask is None:
        return None
    return outcome, ts, {
        "bid1_price": bid,
        "bid1_size": bid_size,
        "ask1_price": ask,
        "ask1_size": ask_size,
    }


def _aligned_orderbook_metadata_for_trade(
    event: TradeEvent,
    *,
    book_cache: dict[str, tuple[datetime, dict[str, float | None]]],
) -> dict[str, Any]:
    """取不晚于 trade 时间的 up/down 最新盘口，写入事件 metadata。"""
    up = book_cache.get("up")
    down = book_cache.get("down")
    if up is None or down is None:
        return {}
    up_ts, up_lv = up
    down_ts, down_lv = down
    if up_ts > event.timestamp or down_ts > event.timestamp:
        return {}
    aligned_at = max(up_ts, down_ts)
    lag_ms = (event.timestamp - aligned_at).total_seconds() * 1000.0
    return {
        "orderbook_snapshot_source": "ws_orderbook_aligned",
        "orderbook_snapshot_at": aligned_at.isoformat(),
        "orderbook_snapshot_age_ms": round(max(0.0, lag_ms), 3),
        "up_bid1_price": up_lv.get("bid1_price"),
        "up_bid1_size": up_lv.get("bid1_size"),
        "up_ask1_price": up_lv.get("ask1_price"),
        "up_ask1_size": up_lv.get("ask1_size"),
        "down_bid1_price": down_lv.get("bid1_price"),
        "down_bid1_size": down_lv.get("bid1_size"),
        "down_ask1_price": down_lv.get("ask1_price"),
        "down_ask1_size": down_lv.get("ask1_size"),
    }


def ws_message_to_trade_event(message: dict[str, Any], spec: PolymarketMarketSpec) -> TradeEvent | None:
    if str(message.get("event_type") or "") != "last_trade_price":
        return None

    asset_id = str(message.get("asset_id") or "")
    outcome = spec.token_to_outcome.get(asset_id)
    if outcome is None:
        return None

    timestamp_ms = int(str(message.get("timestamp") or "0"))
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
    side = str(message.get("side") or "BUY").upper()
    action = "buy" if side == "BUY" else "sell"
    price = float(str(message.get("price") or "0"))
    size = abs(float(str(message.get("size") or "0")))

    return TradeEvent(
        market_id=spec.series_slug,
        cycle_id=spec.slug,
        timestamp=timestamp,
        price=price,
        shares=size,
        outcome=outcome,
        action=action,
        source="polymarket_ws",
        metadata={
            "market_slug": spec.slug,
            "condition_id": spec.condition_id,
            "asset_id": asset_id,
            "up_token_id": spec.yes_token_id,
            "down_token_id": spec.no_token_id,
            "yes_token_id": spec.yes_token_id,
            "no_token_id": spec.no_token_id,
            "event_type": "last_trade_price",
        },
    )


# ---------------------------------------------------------------------------
# WS reader thread — 独立线程执行 recv + ping，与消费者解耦
# ---------------------------------------------------------------------------

_WS_READER_DONE = object()  # sentinel: 读线程结束


def _ws_reader_target(  # noqa: C901, PLR0912, PLR0913, PLR0915
    *,
    spec: PolymarketMarketSpec,
    event_queue: _queue_mod.Queue,
    stop_event: threading.Event,
    websocket_module: Any,
    align_orderbook_with_trade_ws: bool,
    ping_interval_seconds: float,
    receive_timeout_seconds: float,
    cycle_grace_seconds: float,
    reconnect_delay_seconds: float,
    reconnect_max_delay_seconds: float,
    data_silence_timeout_seconds: float,
    connect_timeout_seconds: float,
    cycle_seconds: float,
    cycle_window_start: datetime | None,
    effective_cycle_end: datetime | None,
    eff_cutoff_naive: datetime | None,
    log_fn: Callable[[str], None] | None,
) -> None:
    """Background WS reader: recv + ping + parse, feeds TradeEvent to *event_queue*.

    当所有事件发送完毕（或 stop_event 被置位）后，向 queue 放入
    ``_WS_READER_DONE`` 哨兵，通知消费者退出。
    """
    timeout_exc = websocket_module.WebSocketTimeoutException
    closed_exc = websocket_module.WebSocketConnectionClosedException

    ws_book_cache: dict[str, tuple[datetime, dict[str, float | None]]] = {}
    close_after: float | None = None
    late_event_stop_logged = False
    hard_end_stop_logged = False
    reconnect_attempt = 0

    try:  # outer try — guarantee sentinel
        while not stop_event.is_set():
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            if effective_cycle_end is not None and utc_now >= effective_cycle_end:
                close_after = close_after or (time.monotonic() + cycle_grace_seconds)
                if log_fn is not None and not hard_end_stop_logged:
                    log_fn(
                        "### [窗口到期切换] "
                        f"cycle={spec.slug} reason=hard_window_end "
                        f"window_start={cycle_window_start.isoformat() if cycle_window_start is not None else '-'} "
                        f"effective_end={effective_cycle_end.isoformat()} "
                        f"now_utc={utc_now.isoformat()} "
                        "action=switch_to_next_cycle ###"
                    )
                    hard_end_stop_logged = True
            if close_after is not None and time.monotonic() >= close_after:
                break

            ws_kw: dict[str, Any] = {"timeout": connect_timeout_seconds}
            sslopt = _ws_ssl_opts()
            if sslopt is not None:
                ws_kw["sslopt"] = sslopt
            ws_kw.update(_websocket_proxy_kwargs())
            if log_fn is not None and reconnect_attempt == 0:
                resolved_ip = _resolve_host_cached(_MARKET_WS_HOST)
                if resolved_ip is not None:
                    log_fn(f"[ws-diag] DNS 预解析 {_MARKET_WS_HOST} -> {resolved_ip}")

            connection = None
            while not stop_event.is_set():
                connect_err: BaseException | None = None
                t_connect_start = time.monotonic()
                try:
                    connection = websocket_module.create_connection(MARKET_WS_URL, **ws_kw)
                    t_connect_elapsed = time.monotonic() - t_connect_start
                    connection.settimeout(receive_timeout_seconds)
                    connection.send(
                        json.dumps(
                            {
                                "assets_ids": spec.asset_ids,
                                "type": "market",
                                "custom_feature_enabled": True,
                            }
                        )
                    )
                    reconnect_attempt = 0
                    last_ping = time.monotonic()
                    if log_fn is not None:
                        log_fn(
                            f"[ws-diag] 连接建立成功 {spec.slug} "
                            f"耗时={t_connect_elapsed:.2f}s "
                            f"proxy={'yes' if ws_kw.get('http_proxy_host') else 'no'}"
                        )
                    break
                except closed_exc as e:
                    connect_err = e
                    reconnect_attempt += 1
                except Exception as e:
                    connect_err = e
                    reconnect_attempt += 1
                finally:
                    if connection is not None and reconnect_attempt > 0:
                        try:
                            connection.close()
                        except Exception:
                            pass
                        connection = None

                t_connect_elapsed = time.monotonic() - t_connect_start
                if close_after is not None and time.monotonic() >= close_after:
                    break
                backoff_delay = min(reconnect_max_delay_seconds, reconnect_delay_seconds * (2 ** min(reconnect_attempt - 1, 5)))
                if log_fn is not None:
                    why = f" {_ws_error_detail(connect_err)}" if connect_err is not None else ""
                    log_fn(
                        f"[ws-diag] 连接 {spec.slug} 失败{why}，"
                        f"连接耗时={t_connect_elapsed:.2f}s "
                        f"{backoff_delay:.1f}s 后重试（第 {reconnect_attempt} 次）"
                    )
                time.sleep(max(0.1, backoff_delay))

            if connection is None:
                break

            last_data_at = time.monotonic()
            session_start = time.monotonic()
            session_events = 0
            ping_sent_count = 0

            try:
                while not stop_event.is_set():
                    now = time.monotonic()

                    if close_after is not None and now >= close_after:
                        break

                    if effective_cycle_end is not None and close_after is None:
                        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                        if utc_now >= effective_cycle_end:
                            close_after = now + cycle_grace_seconds
                            if log_fn is not None and not hard_end_stop_logged:
                                log_fn(
                                    "### [窗口到期切换] "
                                    f"cycle={spec.slug} reason=hard_window_end "
                                    f"window_start={cycle_window_start.isoformat() if cycle_window_start is not None else '-'} "
                                    f"effective_end={effective_cycle_end.isoformat()} "
                                    f"now_utc={utc_now.isoformat()} "
                                    "action=switch_to_next_cycle ###"
                                )
                                hard_end_stop_logged = True

                    if data_silence_timeout_seconds > 0 and now - last_data_at >= data_silence_timeout_seconds:
                        if log_fn is not None:
                            log_fn(
                                f"[polymarket] {spec.slug} 数据静默超过 "
                                f"{data_silence_timeout_seconds:.0f}s（silent freeze），强制重连"
                            )
                        break

                    if now - last_ping >= ping_interval_seconds:
                        try:
                            connection.send("PING")
                            ping_sent_count += 1
                        except Exception:
                            if log_fn is not None:
                                session_dur = now - session_start
                                log_fn(
                                    f"[ws-diag] PING 发送失败 {spec.slug} "
                                    f"session={session_dur:.1f}s events={session_events} "
                                    f"pings_sent={ping_sent_count}"
                                )
                            break
                        last_ping = now

                    try:
                        raw = connection.recv()
                    except timeout_exc:
                        raw = None

                    if raw is None:
                        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                        if effective_cycle_end is not None and utc_now >= effective_cycle_end:
                            close_after = close_after or (time.monotonic() + cycle_grace_seconds)
                            if log_fn is not None and not hard_end_stop_logged:
                                log_fn(
                                    "### [窗口到期切换] "
                                    f"cycle={spec.slug} reason=hard_window_end "
                                    f"window_start={cycle_window_start.isoformat() if cycle_window_start is not None else '-'} "
                                    f"effective_end={effective_cycle_end.isoformat()} "
                                    f"now_utc={utc_now.isoformat()} "
                                    "action=switch_to_next_cycle ###"
                                )
                                hard_end_stop_logged = True
                        if close_after is not None and time.monotonic() >= close_after:
                            break
                        continue

                    stop_message_batch = False
                    for message in _iter_message_objects(raw):
                        # 单批消息内也要检查硬截止，避免一次 recv 返回大量积压数据时拖延切窗。
                        if close_after is not None and time.monotonic() >= close_after:
                            stop_message_batch = True
                            break
                        if align_orderbook_with_trade_ws:
                            parsed_book = _extract_book_level_from_ws_message(message, spec)
                            if parsed_book is not None:
                                ob_outcome, ob_ts, ob_level = parsed_book
                                prev = ws_book_cache.get(ob_outcome)
                                if prev is None or ob_ts >= prev[0]:
                                    ws_book_cache[ob_outcome] = (ob_ts, ob_level)
                        asset_ids = [str(item) for item in message.get("assets_ids", [])]
                        if (
                            str(message.get("event_type") or "") == "market_resolved"
                            and (str(message.get("winning_asset_id") or "") in spec.token_to_outcome or any(item in spec.token_to_outcome for item in asset_ids))
                        ):
                            close_after = time.monotonic()

                        event = ws_message_to_trade_event(message, spec)
                        if event is not None:
                            if align_orderbook_with_trade_ws:
                                event.metadata.update(
                                    _aligned_orderbook_metadata_for_trade(
                                        event,
                                        book_cache=ws_book_cache,
                                    )
                                )
                            if cycle_window_start is not None:
                                elapsed = (event.timestamp - cycle_window_start).total_seconds()
                                if elapsed > float(cycle_seconds):
                                    close_after = time.monotonic()
                                    if log_fn is not None and not late_event_stop_logged:
                                        log_fn(
                                            "### [超窗强制切换] "
                                            f"cycle={spec.slug} elapsed={elapsed:.3f}s "
                                            f"limit={cycle_seconds}s action=switch_to_next_cycle ###"
                                        )
                                        late_event_stop_logged = True
                                    stop_message_batch = True
                                    break
                            last_data_at = time.monotonic()
                            session_events += 1
                            if eff_cutoff_naive is not None and event.timestamp < eff_cutoff_naive:
                                continue
                            event_queue.put(event)
                    if stop_message_batch:
                        break
            except closed_exc as e:
                reconnect_attempt += 1
                session_dur = time.monotonic() - session_start
                backoff_delay = min(reconnect_max_delay_seconds, reconnect_delay_seconds * (2 ** min(reconnect_attempt - 1, 5)))
                if log_fn is not None:
                    log_fn(
                        f"[ws-diag] 连接中断 {spec.slug}（{_ws_error_detail(e)}），"
                        f"session={session_dur:.1f}s events={session_events} "
                        f"pings_sent={ping_sent_count} "
                        f"{backoff_delay:.1f}s 后重连（第 {reconnect_attempt} 次）"
                    )
                time.sleep(max(0.1, backoff_delay))
                continue
            finally:
                try:
                    connection.close()
                except Exception:
                    pass

            if close_after is not None and time.monotonic() >= close_after:
                break
            if (
                effective_cycle_end is not None
                and datetime.now(timezone.utc).replace(tzinfo=None) >= effective_cycle_end
            ):
                close_after = close_after or (time.monotonic() + cycle_grace_seconds)
                if log_fn is not None and not hard_end_stop_logged:
                    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                    log_fn(
                        "### [窗口到期切换] "
                        f"cycle={spec.slug} reason=hard_window_end "
                        f"window_start={cycle_window_start.isoformat() if cycle_window_start is not None else '-'} "
                        f"effective_end={effective_cycle_end.isoformat()} "
                        f"now_utc={now_utc.isoformat()} "
                        "action=switch_to_next_cycle ###"
                    )
                    hard_end_stop_logged = True
                if close_after is not None and time.monotonic() >= close_after:
                    break
            if close_after is None:
                reconnect_attempt += 1
                backoff_delay = min(reconnect_max_delay_seconds, reconnect_delay_seconds * (2 ** min(reconnect_attempt - 1, 5)))
                if log_fn is not None:
                    log_fn(f"[polymarket] 连接结束但窗口未完结，{backoff_delay:.1f}s 后重连 {spec.slug}")
                time.sleep(max(0.1, backoff_delay))
                continue

            if close_after is not None and time.monotonic() >= close_after:
                break
    finally:
        event_queue.put(_WS_READER_DONE)


def iter_polymarket_trade_events(
    market_specs: Iterable[PolymarketMarketSpec],
    *,
    metadata_enricher: Callable[[TradeEvent, PolymarketMarketSpec], dict[str, Any]] | None = None,
    align_orderbook_with_trade_ws: bool = True,
    ping_interval_seconds: float = 5.0,
    receive_timeout_seconds: float = 1.0,
    cycle_grace_seconds: float = 5.0,
    reconnect_delay_seconds: float = 1.0,
    reconnect_max_delay_seconds: float = 8.0,
    data_silence_timeout_seconds: float = 45.0,
    connect_timeout_seconds: float = 15.0,
    post_window_start_delay_seconds: float = DEFAULT_POST_WINDOW_START_DELAY_SECONDS,
    log_fn: Callable[[str], None] | None = None,
) -> Iterable[TradeEvent]:
    websocket = _import_websocket_module()

    for spec in market_specs:
        if log_fn is not None:
            log_fn(f"[polymarket] 订阅周期 {spec.slug}")
        eff_cutoff_naive = effective_strategy_start_naive_utc(
            spec.slug, spec.start_time, post_window_start_delay_seconds
        )
        if post_window_start_delay_seconds > 0 and eff_cutoff_naive is not None:
            eff_aware = naive_utc_to_aware_utc(eff_cutoff_naive)
            if datetime.now(timezone.utc) < eff_aware:
                if log_fn is not None:
                    log_fn(
                        f"[polymarket] {spec.slug} 策略生效时刻 "
                        f"{eff_aware.strftime('%Y-%m-%d %H:%M:%S')}Z（窗起点+{post_window_start_delay_seconds:g}s），等待中..."
                    )
                sleep_until_utc_instant(eff_aware)
        _cycle_seconds = cycle_seconds_from_market_slug(spec.slug) or DEFAULT_CYCLE_SECONDS
        slug_window_start = window_start_naive_utc_from_slug(spec.slug)
        cycle_window_start = spec.start_time or slug_window_start
        if (
            spec.start_time is not None
            and slug_window_start is not None
            and abs((spec.start_time - slug_window_start).total_seconds()) > float(_cycle_seconds)
        ):
            cycle_window_start = spec.start_time
        effective_cycle_end = (
            cycle_window_start + timedelta(seconds=float(_cycle_seconds))
            if cycle_window_start is not None
            else spec.end_time
        )
        event_queue: _queue_mod.Queue = _queue_mod.Queue()
        stop_event = threading.Event()
        reader = threading.Thread(
            target=_ws_reader_target,
            kwargs=dict(
                spec=spec,
                event_queue=event_queue,
                stop_event=stop_event,
                websocket_module=websocket,
                align_orderbook_with_trade_ws=align_orderbook_with_trade_ws,
                ping_interval_seconds=ping_interval_seconds,
                receive_timeout_seconds=receive_timeout_seconds,
                cycle_grace_seconds=cycle_grace_seconds,
                reconnect_delay_seconds=reconnect_delay_seconds,
                reconnect_max_delay_seconds=reconnect_max_delay_seconds,
                data_silence_timeout_seconds=data_silence_timeout_seconds,
                connect_timeout_seconds=connect_timeout_seconds,
                cycle_seconds=_cycle_seconds,
                cycle_window_start=cycle_window_start,
                effective_cycle_end=effective_cycle_end,
                eff_cutoff_naive=eff_cutoff_naive,
                log_fn=log_fn,
            ),
            daemon=True,
            name=f"ws-reader-{spec.slug}",
        )
        reader.start()

        try:
            while True:
                try:
                    item = event_queue.get(timeout=1.0)
                except _queue_mod.Empty:
                    if not reader.is_alive():
                        break
                    continue
                if item is _WS_READER_DONE:
                    break
                if metadata_enricher is not None:
                    item.metadata.update(metadata_enricher(item, spec) or {})
                yield item
        finally:
            stop_event.set()
            reader.join(timeout=5.0)


def iter_polymarket_trade_events_robot(
    *,
    slug_prefix: str,
    max_cycles: int,
    metadata_enricher: Callable[[TradeEvent, PolymarketMarketSpec], dict[str, Any]] | None = None,
    poll_interval_seconds: float = 1.0,
    discover_limit: int = 12,
    ping_interval_seconds: float = 5.0,
    receive_timeout_seconds: float = 1.0,
    cycle_grace_seconds: float = 5.0,
    data_silence_timeout_seconds: float = 45.0,
    connect_timeout_seconds: float = 15.0,
    post_window_start_delay_seconds: float = DEFAULT_POST_WINDOW_START_DELAY_SECONDS,
    log_fn: Callable[[str], None] | None = None,
) -> Iterable[TradeEvent]:
    """
    机器人模式：
    - 按 slug 前缀自动发现尚未结束的窗口
    - 自动跳过已处理窗口
    - 当前窗口结束后自动切到下一窗口
    """
    seen_slugs: set[str] = set()
    handled_cycles = 0
    no_new_rounds = 0

    while max_cycles <= 0 or handled_cycles < max_cycles:
        remaining = max_cycles - handled_cycles if max_cycles > 0 else discover_limit
        fetch_limit = max(1, min(discover_limit, remaining if max_cycles > 0 else discover_limit))
        specs = discover_active_markets(slug_prefix=slug_prefix, limit=fetch_limit)
        queued_specs = [spec for spec in specs if spec.slug not in seen_slugs]

        if not queued_specs:
            no_new_rounds += 1
            if log_fn is not None and no_new_rounds % 10 == 1:
                log_fn(
                    f"[polymarket] 暂无新窗口（prefix={slug_prefix}），"
                    f"等待 {poll_interval_seconds:.1f}s 后重试..."
                )
            time.sleep(max(0.2, poll_interval_seconds))
            continue

        no_new_rounds = 0
        for spec in queued_specs:
            if max_cycles > 0 and handled_cycles >= max_cycles:
                break
            seen_slugs.add(spec.slug)
            handled_cycles += 1
            if log_fn is not None:
                log_fn(f"[polymarket] 机器人模式切换到窗口 {spec.slug} ({handled_cycles}/{max_cycles if max_cycles > 0 else 'inf'})")
            yield from iter_polymarket_trade_events(
                [spec],
                metadata_enricher=metadata_enricher,
                ping_interval_seconds=ping_interval_seconds,
                receive_timeout_seconds=receive_timeout_seconds,
                cycle_grace_seconds=cycle_grace_seconds,
                data_silence_timeout_seconds=data_silence_timeout_seconds,
                connect_timeout_seconds=connect_timeout_seconds,
                post_window_start_delay_seconds=post_window_start_delay_seconds,
                log_fn=log_fn,
            )


def build_market_specs(
    *,
    market_slugs: list[str] | None = None,
    slug_prefix: str | None = None,
    max_cycles: int = 10,
) -> list[PolymarketMarketSpec]:
    if market_slugs:
        return [fetch_market_spec(slug) for slug in market_slugs[:max_cycles]]
    if slug_prefix:
        specs = discover_active_markets(slug_prefix=slug_prefix, limit=max_cycles)
        if not specs:
            raise ValueError(
                f"未找到 slug 前缀为 `{slug_prefix}` 且尚未结束（endDate > 当前 UTC）的活动市场；"
                "若近期无对应系列，请稍后再试或使用 --market-slugs 指定 slug。"
            )
        return specs
    raise ValueError("必须提供 `market_slugs` 或 `slug_prefix`。")
