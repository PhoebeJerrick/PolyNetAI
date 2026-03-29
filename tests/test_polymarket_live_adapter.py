from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from polynet_ai.adapters import polymarket_live
from polynet_ai.adapters.polymarket_live import (
    PolymarketMarketSpec,
    _discover_time_bucket_markets,
    apply_proxy_env_from_dict,
    discover_active_markets,
    get_account_env_value,
    iter_polymarket_trade_events,
    iter_polymarket_trade_events_robot,
    load_api_env,
    select_account_env,
    ws_message_to_trade_event,
)
from polynet_ai.domain.models import TradeEvent


def test_load_api_env_ignores_titles_and_blank_lines(tmp_path) -> None:
    env_file = tmp_path / "ApiConfig.env"
    env_file.write_text(
        "\n".join(
            [
                "Poly Market- APIs",
                "APP_ENV_1=prod",
                "",
                "POLY_DERIVE_API_KEY_1=test-key",
                "# comment",
                "POLY_BUILDER_API_SECRET_1=test-secret",
            ]
        ),
        encoding="utf-8",
    )

    values = load_api_env(env_file)

    assert values["APP_ENV_1"] == "prod"
    assert values["POLY_DERIVE_API_KEY_1"] == "test-key"
    assert values["POLY_BUILDER_API_SECRET_1"] == "test-secret"
    assert "Poly Market- APIs" not in values


def test_ws_message_to_trade_event_maps_yes_no_into_up_down() -> None:
    spec = PolymarketMarketSpec(
        slug="btc-updown-5m-1773826800",
        series_slug="btc-up-or-down-5m",
        condition_id="0xabc",
        yes_token_id="yes-token",
        no_token_id="no-token",
        start_time=datetime(2026, 3, 20, 12, 0, 0),
        end_time=datetime(2026, 3, 20, 12, 5, 0),
        raw={},
    )

    up_event = ws_message_to_trade_event(
        {
            "event_type": "last_trade_price",
            "asset_id": "yes-token",
            "price": "0.57",
            "size": "11.5",
            "side": "BUY",
            "timestamp": "1766789469958",
        },
        spec,
    )
    down_event = ws_message_to_trade_event(
        {
            "event_type": "last_trade_price",
            "asset_id": "no-token",
            "price": "0.43",
            "size": "8",
            "side": "SELL",
            "timestamp": "1766789469959",
        },
        spec,
    )

    assert up_event is not None
    assert up_event.market_id == "btc-up-or-down-5m"
    assert up_event.cycle_id == "btc-updown-5m-1773826800"
    assert up_event.outcome == "up"
    assert up_event.action == "buy"

    assert down_event is not None
    assert down_event.outcome == "down"
    assert down_event.action == "sell"


def test_apply_proxy_env_from_dict_strips_quotes_and_sets_lowercase(monkeypatch) -> None:
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("WS_PROXY", raising=False)

    applied = apply_proxy_env_from_dict(
        {
            "HTTPS_PROXY": '"http://127.0.0.1:7890"',
            "WS_PROXY": "http://127.0.0.1:7890",
        }
    )

    assert "HTTPS_PROXY" in applied
    assert "WS_PROXY" in applied
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["https_proxy"] == "http://127.0.0.1:7890"
    assert os.environ["WS_PROXY"] == "http://127.0.0.1:7890"


def test_select_account_env_promotes_selected_suffix_to_base_keys() -> None:
    values = {
        "PURSE_ADDRESS_1": "0x111",
        "PURSE_ADDRESS_2": "0x222",
        "POLY_DERIVE_API_KEY_2": "key-2",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
    }

    selected = select_account_env(values, account_index=2)

    assert selected["PURSE_ADDRESS"] == "0x222"
    assert selected["POLY_DERIVE_API_KEY"] == "key-2"
    assert selected["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert selected["PURSE_ADDRESS_1"] == "0x111"


def test_get_account_env_value_prefers_selected_suffix_then_base() -> None:
    values = {
        "PURSE_ADDRESS_1": "0x111",
        "PURSE_ADDRESS_2": "0x222",
        "CHAIN_ID": "137",
    }

    assert get_account_env_value(values, "PURSE_ADDRESS", account_index=2) == "0x222"
    assert get_account_env_value(values, "CHAIN_ID", account_index=2) == "137"
    assert get_account_env_value(values, "MISSING", account_index=2, default="fallback") == "fallback"


def test_discover_active_markets_filters_by_slug_prefix(monkeypatch) -> None:
    payload = [
        {
            "slug": "btc-updown-5m-1773826800",
            "conditionId": "0x1",
            "outcomes": "[\"Up\", \"Down\"]",
            "clobTokenIds": "[\"yes-1\", \"no-1\"]",
            "eventStartTime": "2099-01-01T12:00:00Z",
            "endDate": "2099-01-01T12:05:00Z",
            "acceptingOrders": True,
            "events": [{"seriesSlug": "btc-up-or-down-5m"}],
        },
        {
            "slug": "other-market",
            "conditionId": "0x2",
            "outcomes": "[\"Yes\", \"No\"]",
            "clobTokenIds": "[\"yes-2\", \"no-2\"]",
            "eventStartTime": "2099-01-01T12:10:00Z",
            "endDate": "2099-01-01T12:15:00Z",
            "acceptingOrders": True,
            "events": [{"seriesSlug": "other-series"}],
        },
    ]

    monkeypatch.setattr(polymarket_live, "_fetch_json", lambda url: payload if "offset=0" in url else [])
    specs = discover_active_markets("btc-updown-5m-", limit=10, page_size=100)

    assert len(specs) == 1
    assert specs[0].slug == "btc-updown-5m-1773826800"


def test_discover_time_bucket_markets_derives_nearby_5m_slugs(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 3, 20, 13, 50, 30, tzinfo=tz)

    monkeypatch.setattr(polymarket_live, "datetime", FixedDateTime)

    def _fake_fetch(slug: str) -> PolymarketMarketSpec:
        start_ts = int(slug.rsplit("-", 1)[-1])
        start_dt = FixedDateTime.fromtimestamp(start_ts, tz=timezone.utc).replace(tzinfo=None)
        return PolymarketMarketSpec(
            slug=slug,
            series_slug="btc-up-or-down-5m",
            condition_id=slug,
            yes_token_id=f"{slug}-yes",
            no_token_id=f"{slug}-no",
            start_time=start_dt,
            end_time=start_dt + timedelta(minutes=5),
            raw={"acceptingOrders": True},
        )

    monkeypatch.setattr(polymarket_live, "fetch_market_spec", _fake_fetch)

    specs = _discover_time_bucket_markets("btc-updown-5m-", limit=2)
    base_timestamp = int(FixedDateTime.now(timezone.utc).timestamp()) // 300 * 300

    assert [spec.slug for spec in specs] == [
        f"btc-updown-5m-{base_timestamp}",
        f"btc-updown-5m-{base_timestamp + 300}",
    ]


def test_iter_polymarket_trade_events_robot_auto_switches_new_windows(monkeypatch) -> None:
    spec_1 = PolymarketMarketSpec(
        slug="btc-updown-5m-1774012500",
        series_slug="btc-up-or-down-5m",
        condition_id="0x1",
        yes_token_id="yes-1",
        no_token_id="no-1",
        start_time=datetime(2099, 1, 1, 12, 0, 0),
        end_time=datetime(2099, 1, 1, 12, 5, 0),
        raw={},
    )
    spec_2 = PolymarketMarketSpec(
        slug="btc-updown-5m-1774012800",
        series_slug="btc-up-or-down-5m",
        condition_id="0x2",
        yes_token_id="yes-2",
        no_token_id="no-2",
        start_time=datetime(2099, 1, 1, 12, 5, 0),
        end_time=datetime(2099, 1, 1, 12, 10, 0),
        raw={},
    )

    monkeypatch.setattr(polymarket_live, "discover_active_markets", lambda slug_prefix, limit: [spec_1, spec_2])

    def _fake_iter(market_specs, **kwargs):
        for spec in market_specs:
            yield TradeEvent(
                market_id=spec.series_slug,
                cycle_id=spec.slug,
                timestamp=datetime(2099, 1, 1, 12, 0, 1),
                price=0.5,
                shares=1.0,
                outcome="up",
                action="buy",
            )

    monkeypatch.setattr(polymarket_live, "iter_polymarket_trade_events", _fake_iter)

    events = list(
        iter_polymarket_trade_events_robot(
            slug_prefix="btc-updown-5m-",
            max_cycles=2,
            poll_interval_seconds=0.01,
        )
    )

    assert len(events) == 2
    assert events[0].cycle_id == "btc-updown-5m-1774012500"
    assert events[1].cycle_id == "btc-updown-5m-1774012800"


def test_iter_polymarket_trade_events_reconnects_after_socket_closed(monkeypatch) -> None:
    spec = PolymarketMarketSpec(
        slug="btc-updown-5m-1774012500",
        series_slug="btc-up-or-down-5m",
        condition_id="0x1",
        yes_token_id="yes-1",
        no_token_id="no-1",
        start_time=datetime(2099, 1, 1, 12, 0, 0),
        end_time=datetime(2099, 1, 1, 12, 5, 0),
        raw={},
    )

    class ClosedExc(Exception):
        pass

    class TimeoutExc(Exception):
        pass

    class FakeConnection:
        def __init__(self, replies):
            self.replies = list(replies)

        def settimeout(self, timeout):
            return None

        def send(self, payload):
            return None

        def recv(self):
            item = self.replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def close(self):
            return None

    class FakeWebSocketModule:
        WebSocketTimeoutException = TimeoutExc
        WebSocketConnectionClosedException = ClosedExc

        def __init__(self):
            self.connections = [
                FakeConnection(
                    [
                        '[{"event_type":"last_trade_price","asset_id":"yes-1","price":"0.51","size":"3","side":"BUY","timestamp":"4070952001000"}]',
                        ClosedExc("lost"),
                    ]
                ),
                FakeConnection(
                    [
                        '[{"event_type":"last_trade_price","asset_id":"no-1","price":"0.49","size":"2","side":"SELL","timestamp":"4070952002000"}]',
                        '{"event_type":"market_resolved","assets_ids":["yes-1","no-1"]}',
                        TimeoutExc(),
                    ]
                ),
            ]

        def create_connection(self, url, **kwargs):
            return self.connections.pop(0)

    monkeypatch.setattr(polymarket_live, "_import_websocket_module", lambda: FakeWebSocketModule())
    monkeypatch.setattr(polymarket_live, "time", type("FakeTime", (), {"monotonic": staticmethod(lambda: 100.0), "sleep": staticmethod(lambda _: None)})())

    events = list(
        iter_polymarket_trade_events(
            [spec],
            receive_timeout_seconds=0.01,
            cycle_grace_seconds=0.0,
            post_window_start_delay_seconds=0.0,
        )
    )

    assert len(events) == 2
    assert events[0].outcome == "up"
    assert events[1].outcome == "down"
