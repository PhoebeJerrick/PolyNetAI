from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderBookSummary,
    OrderSummary,
    OrderType,
)

from polynet_ai.adapters.polymarket_live import get_account_env_value
from polynet_ai.domain.models import ExecutionResult, FillEvent, OrderIntent


@dataclass(slots=True)
class ExecutionPlan:
    shares: float
    estimated_vwap: float
    limit_price: float
    deepest_price: float
    tick_size: float


@dataclass(slots=True)
class PendingOrder:
    order_id: str
    token_id: str
    intent: OrderIntent
    submitted_at: datetime
    reserved_cash: float
    reserved_shares: float
    response: dict[str, Any]
    execution_plan: ExecutionPlan
    record_index: int
    poll_attempts: int = 0
    last_polled_at: datetime | None = None


def _level_price(level: OrderSummary) -> float:
    return float(level.price or 0.0)


def _level_size(level: OrderSummary) -> float:
    return float(level.size or 0.0)


def _round_to_tick(price: float, tick_size: float) -> float:
    decimals = max(0, len(str(tick_size).split(".", 1)[1]) if "." in str(tick_size) else 0)
    return round(price, decimals)


def _normalize_shares_for_order(action: str, shares: float, price: float) -> float:
    if action == "buy":
        if price <= 0:
            return 0.0
        return round(shares, 4)
    return round(shares, 2)


def _normalize_market_amount(action: str, shares: float, price: float) -> float:
    if action == "buy":
        return round(shares * price, 2)
    return round(shares, 2)


def _estimate_fok_plan(
    book: OrderBookSummary,
    *,
    action: str,
    shares: float,
    price_buffer_ticks: int = 1,
) -> ExecutionPlan | None:
    if shares <= 0:
        return None

    tick_size = float(book.tick_size or "0.01")
    if action == "buy":
        levels = sorted(book.asks or [], key=_level_price)
    else:
        levels = sorted(book.bids or [], key=_level_price, reverse=True)
    if not levels:
        return None

    remaining = shares
    filled = 0.0
    notional = 0.0
    deepest = 0.0
    for level in levels:
        price = _level_price(level)
        size = _level_size(level)
        if price <= 0 or size <= 0:
            continue
        take = min(remaining, size)
        if take <= 0:
            continue
        filled += take
        remaining -= take
        notional += take * price
        deepest = price
        if remaining <= 1e-9:
            break

    if filled + 1e-9 < shares:
        return None

    estimated_vwap = notional / filled
    if action == "buy":
        limit_price = min(0.99, deepest + tick_size * max(0, price_buffer_ticks))
    else:
        limit_price = max(0.001, deepest - tick_size * max(0, price_buffer_ticks))
    limit_price = _round_to_tick(limit_price, tick_size)
    normalized_shares = _normalize_shares_for_order(action, shares, limit_price)
    if normalized_shares <= 0:
        return None
    return ExecutionPlan(
        shares=normalized_shares,
        estimated_vwap=estimated_vwap,
        limit_price=limit_price,
        deepest_price=deepest,
        tick_size=tick_size,
    )


@dataclass(slots=True)
class PolymarketBroker:
    client: ClobClient
    fee_rate: float = 0.002
    signature_type: int = 2
    account_index: int = 2
    price_buffer_ticks: int = 1
    confirmation_poll_interval_seconds: float = 0.5
    confirmation_timeout_seconds: float = 8.0
    confirmation_grace_seconds: float = 0.25
    submitted_orders: list[dict[str, Any]] = field(default_factory=list)
    pending_orders: dict[str, PendingOrder] = field(default_factory=dict)
    last_confirmation_poll_at: datetime | None = None

    @classmethod
    def from_env(
        cls,
        values: dict[str, str],
        *,
        account_index: int = 2,
        fee_rate: float = 0.002,
        signature_type: int = 2,
        host: str = "https://clob.polymarket.com",
    ) -> "PolymarketBroker":
        private_key = get_account_env_value(values, "PURSE_PRIVATE_KEY", account_index=account_index)
        funder = get_account_env_value(values, "PURSE_ADDRESS", account_index=account_index)
        api_key = get_account_env_value(values, "POLY_DERIVE_API_KEY", account_index=account_index)
        api_secret = get_account_env_value(values, "POLY_DERIVE_API_SECRET", account_index=account_index)
        api_passphrase = get_account_env_value(values, "POLY_DERIVE_API_PASSPHRASE", account_index=account_index)
        missing = [
            name
            for name, value in {
                "PURSE_PRIVATE_KEY": private_key,
                "PURSE_ADDRESS": funder,
                "POLY_DERIVE_API_KEY": api_key,
                "POLY_DERIVE_API_SECRET": api_secret,
                "POLY_DERIVE_API_PASSPHRASE": api_passphrase,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"账号 {account_index} 缺少真实下单所需配置: {', '.join(missing)}")

        client = ClobClient(
            host=host,
            chain_id=137,
            key=private_key,
            creds=ApiCreds(
                api_key=str(api_key),
                api_secret=str(api_secret),
                api_passphrase=str(api_passphrase),
            ),
            signature_type=signature_type,
            funder=str(funder),
        )
        return cls(
            client=client,
            fee_rate=fee_rate,
            signature_type=signature_type,
            account_index=account_index,
        )

    def get_collateral_balance_usdc(self) -> float:
        payload = self.client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self.signature_type,
            )
        )
        raw_balance = float((payload or {}).get("balance") or 0.0)
        return raw_balance / 1_000_000.0

    def get_open_orders(
        self,
        *,
        market: str | None = None,
        asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return list(
            self.client.get_orders(
                OpenOrderParams(
                    market=market,
                    asset_id=asset_id,
                )
            )
        )

    def cancel_market_orders(self, *, market: str = "", asset_id: str = "") -> dict[str, Any]:
        return dict(self.client.cancel_market_orders(market=market, asset_id=asset_id))

    def export_orders(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(self.submitted_orders, ensure_ascii=False, indent=2), encoding="utf-8")
        return output

    def pending_context(self) -> dict[str, float | int]:
        pending_buy_reserved_cash = 0.0
        pending_up_sell_shares = 0.0
        pending_down_sell_shares = 0.0
        for pending in self.pending_orders.values():
            if pending.intent.action == "buy":
                pending_buy_reserved_cash += pending.reserved_cash
            elif pending.intent.outcome == "up":
                pending_up_sell_shares += pending.reserved_shares
            else:
                pending_down_sell_shares += pending.reserved_shares
        return {
            "pending_order_count": len(self.pending_orders),
            "pending_buy_reserved_cash": pending_buy_reserved_cash,
            "pending_up_sell_shares": pending_up_sell_shares,
            "pending_down_sell_shares": pending_down_sell_shares,
        }

    def _resolve_token_id(self, intent: OrderIntent) -> str:
        if intent.outcome == "up":
            token_id = intent.metadata.get("up_token_id") or intent.metadata.get("yes_token_id")
        else:
            token_id = intent.metadata.get("down_token_id") or intent.metadata.get("no_token_id")
        if not token_id:
            raise ValueError(f"无法解析 {intent.outcome} 对应 token_id，cycle={intent.cycle_id}")
        return str(token_id)

    def execute(self, intent: OrderIntent, timestamp: datetime) -> ExecutionResult:
        token_id = self._resolve_token_id(intent)
        book = self.client.get_order_book(token_id)
        plan = _estimate_fok_plan(
            book,
            action=intent.action,
            shares=float(intent.shares),
            price_buffer_ticks=self.price_buffer_ticks,
        )
        record: dict[str, Any] = {
            "timestamp": timestamp.isoformat(),
            "market_id": intent.market_id,
            "cycle_id": intent.cycle_id,
            "action": intent.action,
            "outcome": intent.outcome,
            "requested_shares": float(intent.shares),
            "reference_price": float(intent.reference_price),
            "token_id": token_id,
        }
        if plan is None:
            record["status"] = "no_liquidity"
            self.submitted_orders.append(record)
            return ExecutionResult(status="no_liquidity", reason="订单簿深度不足")

        market_amount = _normalize_market_amount(intent.action, plan.shares, plan.limit_price)
        if market_amount <= 0:
            record["status"] = "invalid_amount"
            self.submitted_orders.append(record)
            return ExecutionResult(status="invalid_amount", reason="下单金额无效")
        record["execution_plan"] = asdict(plan)
        record["market_amount"] = market_amount
        try:
            signed_order = self.client.create_market_order(
                MarketOrderArgs(
                    token_id=token_id,
                    amount=market_amount,
                    side="BUY" if intent.action == "buy" else "SELL",
                    price=plan.limit_price,
                    order_type=OrderType.FOK,
                )
            )
            response = self.client.post_order(signed_order, OrderType.FOK)
        except Exception as exc:
            record["status"] = "error"
            record["error"] = str(exc)
            self.submitted_orders.append(record)
            raise

        if isinstance(response, dict):
            record["response"] = response
            success = bool(response.get("success"))
        else:
            record["response"] = {"raw": response}
            success = False
        if not success:
            record["status"] = "rejected"
            self.submitted_orders.append(record)
            return ExecutionResult(status="rejected", reason=str((record.get("response") or {}).get("errorMsg") or "订单被拒绝"))

        order_id = str((response or {}).get("orderID") or "")
        reserved_cash = market_amount * (1.0 + self.fee_rate) if intent.action == "buy" else 0.0
        reserved_shares = plan.shares if intent.action == "sell" else 0.0
        record["status"] = "submitted"
        record["order_id"] = order_id
        record["confirmation_state"] = "pending"
        self.submitted_orders.append(record)
        if order_id:
            self.pending_orders[order_id] = PendingOrder(
                order_id=order_id,
                token_id=token_id,
                intent=intent,
                submitted_at=timestamp,
                reserved_cash=reserved_cash,
                reserved_shares=reserved_shares,
                response=dict(response),
                execution_plan=plan,
                record_index=len(self.submitted_orders) - 1,
            )
        return ExecutionResult(
            status="submitted",
            order_id=order_id,
            metadata={
                "reserved_cash": reserved_cash,
                "reserved_shares": reserved_shares,
            },
        )

    def poll(self, timestamp: datetime) -> list[FillEvent]:
        if not self.pending_orders:
            return []
        if (
            self.last_confirmation_poll_at is not None
            and (timestamp - self.last_confirmation_poll_at).total_seconds() < self.confirmation_poll_interval_seconds
        ):
            return []
        self.last_confirmation_poll_at = timestamp

        confirmed_fills: list[FillEvent] = []
        for order_id, pending in list(self.pending_orders.items()):
            if (timestamp - pending.submitted_at).total_seconds() < self.confirmation_grace_seconds:
                continue
            record = self.submitted_orders[pending.record_index]
            pending.poll_attempts += 1
            pending.last_polled_at = timestamp
            record["confirmation_poll_attempts"] = pending.poll_attempts
            record["last_confirmation_poll_at"] = timestamp.isoformat()
            try:
                order_payload = self.client.get_order(order_id)
            except Exception as exc:
                record["last_confirmation_error"] = str(exc)
                if self._should_timeout_confirm(pending, timestamp):
                    fill = self._build_fill_from_pending(pending, timestamp, source="post_order_timeout_fallback")
                    record["status"] = "confirmed_timeout_fallback"
                    record["confirmation_state"] = "confirmed"
                    confirmed_fills.append(fill)
                    self.pending_orders.pop(order_id, None)
                continue

            record["confirmation_response"] = order_payload
            status = str((order_payload or {}).get("status") or "").lower()
            if status in {"matched", "filled", "mined", "confirmed"}:
                fill = self._build_fill_from_pending(pending, timestamp, source="get_order")
                record["status"] = "confirmed"
                record["confirmation_state"] = status or "confirmed"
                confirmed_fills.append(fill)
                self.pending_orders.pop(order_id, None)
                continue
            if status in {"cancelled", "canceled", "unmatched", "failed", "rejected"}:
                record["status"] = f"confirm_{status or 'failed'}"
                record["confirmation_state"] = status or "failed"
                self.pending_orders.pop(order_id, None)
                continue
            if self._should_timeout_confirm(pending, timestamp):
                fill = self._build_fill_from_pending(pending, timestamp, source="post_order_timeout_fallback")
                record["status"] = "confirmed_timeout_fallback"
                record["confirmation_state"] = status or "timeout_fallback"
                confirmed_fills.append(fill)
                self.pending_orders.pop(order_id, None)
        return confirmed_fills

    def _should_timeout_confirm(self, pending: PendingOrder, timestamp: datetime) -> bool:
        return (timestamp - pending.submitted_at).total_seconds() >= self.confirmation_timeout_seconds

    def _build_fill_from_pending(
        self,
        pending: PendingOrder,
        timestamp: datetime,
        *,
        source: str,
    ) -> FillEvent:
        response = pending.response or {}
        taking_amount = float(response.get("takingAmount") or 0.0)
        making_amount = float(response.get("makingAmount") or 0.0)
        if pending.intent.action == "buy":
            actual_shares = taking_amount if taking_amount > 0 else pending.execution_plan.shares
            gross = making_amount if making_amount > 0 else actual_shares * pending.execution_plan.estimated_vwap
        else:
            actual_shares = making_amount if making_amount > 0 else pending.execution_plan.shares
            gross = taking_amount if taking_amount > 0 else actual_shares * pending.execution_plan.estimated_vwap
        actual_price = gross / actual_shares if actual_shares > 0 else pending.execution_plan.estimated_vwap
        fee = gross * self.fee_rate
        return FillEvent(
            market_id=pending.intent.market_id,
            cycle_id=pending.intent.cycle_id,
            timestamp=timestamp,
            price=actual_price,
            shares=actual_shares,
            outcome=pending.intent.outcome,
            action=pending.intent.action,
            fee=fee,
            slippage=abs(actual_price - pending.intent.reference_price),
            reason=f"{pending.intent.reason} [{source}]",
            reserved_cash=pending.reserved_cash,
        )
