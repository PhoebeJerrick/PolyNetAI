from __future__ import annotations

import json
import os
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from polynet_ai.adapters.cycle_window_timing import (
    DEFAULT_POST_WINDOW_START_DELAY_SECONDS,
    effective_strategy_start_naive_utc,
    naive_utc_to_aware_utc,
    sleep_until_utc_instant,
)
from polynet_ai.domain.models import TradeEvent

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

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
    """相对仓库根目录 `root` 的 ApiConfig.env 默认搜索路径（按顺序）。"""
    return (
        root.parent / "APIs" / "ApiConfig.env",
        root.parent.parent / "APIs" / "ApiConfig.env",
    )


def resolve_default_api_config_env(root: Path) -> str:
    """返回第一个存在的默认 ApiConfig.env 路径；均不存在时返回首选路径字符串。"""
    for candidate in default_api_config_env_candidates(root):
        if candidate.exists():
            return str(candidate.resolve())
    return str(default_api_config_env_candidates(root)[0].resolve())


def load_api_env(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def select_account_env(values: dict[str, str], account_index: int = 2) -> dict[str, str]:
    """
    从 `ApiConfig.env` 中挑选指定账号的配置，并将 `FOO_2` 形式映射为无后缀键 `FOO`。

    规则：
    - 保留原始所有键，兼容旧逻辑；
    - 若存在 `<KEY>_<account_index>`，则额外写入 `<KEY>`；
    - 默认账号 2，便于双账号场景直接切换。
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
    suffix_key = f"{key}_{int(account_index)}"
    if suffix_key in values:
        return values[suffix_key]
    if key in values:
        return values[key]
    return default


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


def iter_polymarket_trade_events(
    market_specs: Iterable[PolymarketMarketSpec],
    *,
    ping_interval_seconds: float = 10.0,
    receive_timeout_seconds: float = 1.0,
    cycle_grace_seconds: float = 20.0,
    reconnect_delay_seconds: float = 1.0,
    post_window_start_delay_seconds: float = DEFAULT_POST_WINDOW_START_DELAY_SECONDS,
    log_fn: Callable[[str], None] | None = None,
) -> Iterable[TradeEvent]:
    websocket = _import_websocket_module()
    timeout_exc = websocket.WebSocketTimeoutException
    closed_exc = websocket.WebSocketConnectionClosedException

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
        close_after = None
        reconnect_attempt = 0

        while True:
            utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
            if spec.end_time is not None and utc_now >= spec.end_time:
                close_after = close_after or (time.monotonic() + cycle_grace_seconds)
            if close_after is not None and time.monotonic() >= close_after:
                break

            ws_kw: dict[str, Any] = {"timeout": 10}
            sslopt = _ws_ssl_opts()
            if sslopt is not None:
                ws_kw["sslopt"] = sslopt
            ws_kw.update(_websocket_proxy_kwargs())
            connection = None
            while True:
                try:
                    connection = websocket.create_connection(MARKET_WS_URL, **ws_kw)
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
                    break
                except closed_exc:
                    reconnect_attempt += 1
                except Exception:
                    reconnect_attempt += 1
                finally:
                    if connection is not None and reconnect_attempt > 0:
                        try:
                            connection.close()
                        except Exception:
                            pass
                        connection = None

                if close_after is not None and time.monotonic() >= close_after:
                    break
                if log_fn is not None:
                    log_fn(f"[polymarket] 连接 {spec.slug} 失败，{reconnect_delay_seconds:.1f}s 后重试（第 {reconnect_attempt} 次）")
                time.sleep(max(0.1, reconnect_delay_seconds))

            if connection is None:
                break

            try:
                while True:
                    now = time.monotonic()
                    if now - last_ping >= ping_interval_seconds:
                        connection.send("PING")
                        last_ping = now

                    try:
                        raw = connection.recv()
                    except timeout_exc:
                        raw = None

                    if raw is None:
                        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                        if spec.end_time is not None and utc_now >= spec.end_time:
                            close_after = close_after or (time.monotonic() + cycle_grace_seconds)
                        if close_after is not None and time.monotonic() >= close_after:
                            break
                        continue

                    for message in _iter_message_objects(raw):
                        asset_ids = [str(item) for item in message.get("assets_ids", [])]
                        if (
                            str(message.get("event_type") or "") == "market_resolved"
                            and (str(message.get("winning_asset_id") or "") in spec.token_to_outcome or any(item in spec.token_to_outcome for item in asset_ids))
                        ):
                            close_after = time.monotonic()

                        event = ws_message_to_trade_event(message, spec)
                        if event is not None:
                            if eff_cutoff_naive is not None and event.timestamp < eff_cutoff_naive:
                                continue
                            yield event
            except closed_exc:
                reconnect_attempt += 1
                if log_fn is not None:
                    log_fn(f"[polymarket] 连接中断 {spec.slug}，{reconnect_delay_seconds:.1f}s 后重连（第 {reconnect_attempt} 次）")
                time.sleep(max(0.1, reconnect_delay_seconds))
                continue
            finally:
                try:
                    connection.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass

            if close_after is not None and time.monotonic() >= close_after:
                break
            if spec.end_time is not None and datetime.now(timezone.utc).replace(tzinfo=None) >= spec.end_time:
                close_after = close_after or (time.monotonic() + cycle_grace_seconds)
                if close_after is not None and time.monotonic() >= close_after:
                    break
            if close_after is None:
                if log_fn is not None:
                    log_fn(f"[polymarket] 连接结束但窗口未完结，{reconnect_delay_seconds:.1f}s 后重连 {spec.slug}")
                time.sleep(max(0.1, reconnect_delay_seconds))
                continue

            if close_after is not None and time.monotonic() >= close_after:
                break


def iter_polymarket_trade_events_robot(
    *,
    slug_prefix: str,
    max_cycles: int,
    poll_interval_seconds: float = 3.0,
    discover_limit: int = 12,
    ping_interval_seconds: float = 10.0,
    receive_timeout_seconds: float = 1.0,
    cycle_grace_seconds: float = 20.0,
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
                ping_interval_seconds=ping_interval_seconds,
                receive_timeout_seconds=receive_timeout_seconds,
                cycle_grace_seconds=cycle_grace_seconds,
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
