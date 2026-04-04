from __future__ import annotations

from unittest.mock import MagicMock

from polynet_ai.adapters.polymarket_redeem_api import fetch_redeemable_positions_aggregated


def test_fetch_redeemable_empty_address() -> None:
    assert fetch_redeemable_positions_aggregated("") == []
    assert fetch_redeemable_positions_aggregated("not_hex") == []


def test_fetch_redeemable_aggregates_by_condition() -> None:
    payload = [
        {
            "conditionId": "aa" * 32,
            "slug": "m1",
            "size": 5.0,
        },
        {
            "conditionId": "0x" + "aa" * 32,
            "slug": "m1",
            "size": 3.0,
        },
    ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = mock_resp
    out = fetch_redeemable_positions_aggregated("0xUser", session=session)
    assert len(out) == 1
    assert out[0]["condition_id"] == "0x" + "aa" * 32
    assert out[0]["expected_payout"] == 8.0
    assert out[0]["size"] == 8.0
