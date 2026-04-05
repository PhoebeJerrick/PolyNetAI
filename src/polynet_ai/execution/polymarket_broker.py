from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderType,
)

from polynet_ai.adapters.polymarket_live import account_env_keys_for_index, get_account_env_value
from polynet_ai.domain.models import ExecutionResult, FillEvent, OrderIntent
from polynet_ai.execution.fok_orderbook import ExecutionPlan, estimate_fok_plan, normalize_market_amount

DATA_API_BASE = "https://data-api.polymarket.com"


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
    condition_id: str = ""
    poll_attempts: int = 0
    last_polled_at: datetime | None = None


def clob_client_from_env(
    values: dict[str, str],
    *,
    account_index: int = 2,
    signature_type: int = 2,
    host: str = "https://clob.polymarket.com",
) -> ClobClient:
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
        hinted = ", ".join(account_env_keys_for_index(missing, account_index))
        raise ValueError(
            f"账号 {account_index} 缺少 CLOB 所需配置: {', '.join(missing)} "
            f"（建议在 ApiConfig.env 使用带后缀键: {hinted}）"
        )
    return ClobClient(
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


@dataclass(slots=True)
class PolymarketBroker:
    client: ClobClient
    fee_rate: float = 0.002
    signature_type: int = 2
    account_index: int = 2
    purse_address: str = ""
    price_buffer_ticks: int = 1
    confirmation_poll_interval_seconds: float = 0.5
    confirmation_timeout_seconds: float = 8.0
    confirmation_grace_seconds: float = 0.25
    use_orderbook_min_order_size: bool = True
    market_min_order_size_fallback: float = 5.0
    enforce_sell_min_order_size: bool = True
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
        funder = get_account_env_value(values, "PURSE_ADDRESS", account_index=account_index)
        client = clob_client_from_env(
            values,
            account_index=account_index,
            signature_type=signature_type,
            host=host,
        )
        return cls(
            client=client,
            fee_rate=fee_rate,
            signature_type=signature_type,
            account_index=account_index,
            purse_address=str(funder or "").strip(),
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

    def _query_conditional_token_balance(self, token_id: str) -> float | None:
        """查询链上条件代币余额（ERC1155），返回 None 表示查询失败。"""
        try:
            payload = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=self.signature_type,
                )
            )
            return float((payload or {}).get("balance") or 0.0) / 1_000_000.0
        except Exception:
            return None

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
        # P0-fix: 按方向统计 pending buy 的预估仓位价值 & 计数
        pending_buy_up_value = 0.0
        pending_buy_down_value = 0.0
        pending_buy_up_count = 0
        pending_buy_down_count = 0
        pending_sell_up_count = 0
        pending_sell_down_count = 0
        for pending in self.pending_orders.values():
            if pending.intent.action == "buy":
                pending_buy_reserved_cash += pending.reserved_cash
                est_value = pending.execution_plan.shares * pending.execution_plan.estimated_vwap
                if pending.intent.outcome == "up":
                    pending_buy_up_value += est_value
                    pending_buy_up_count += 1
                else:
                    pending_buy_down_value += est_value
                    pending_buy_down_count += 1
            elif pending.intent.outcome == "up":
                pending_up_sell_shares += pending.reserved_shares
                pending_sell_up_count += 1
            else:
                pending_down_sell_shares += pending.reserved_shares
                pending_sell_down_count += 1
        return {
            "pending_order_count": len(self.pending_orders),
            "pending_buy_reserved_cash": pending_buy_reserved_cash,
            "pending_up_sell_shares": pending_up_sell_shares,
            "pending_down_sell_shares": pending_down_sell_shares,
            "pending_buy_up_value": pending_buy_up_value,
            "pending_buy_down_value": pending_buy_down_value,
            "pending_buy_up_count": pending_buy_up_count,
            "pending_buy_down_count": pending_buy_down_count,
            "pending_sell_up_count": pending_sell_up_count,
            "pending_sell_down_count": pending_sell_down_count,
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
        tick_size = float(book.tick_size or "0.01")
        raw_market_min = float(book.min_order_size or self.market_min_order_size_fallback)
        market_min_order_size = max(0.0, raw_market_min if self.use_orderbook_min_order_size else 0.0)
        enforce_min_order_size = intent.action == "buy" or self.enforce_sell_min_order_size
        required_min_order_size = market_min_order_size if enforce_min_order_size else 0.0
        plan = estimate_fok_plan(
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
            "market_tick_size": tick_size,
            "market_min_order_size": market_min_order_size,
            "required_min_order_size": required_min_order_size,
        }
        if required_min_order_size > 0 and float(intent.shares) + 1e-9 < required_min_order_size:
            record["status"] = "invalid_amount"
            record["status_reason"] = "below_market_min_order_size"
            self.submitted_orders.append(record)
            return ExecutionResult(
                status="invalid_amount",
                reason=f"下单份额低于市场最小下单量 {required_min_order_size:g}",
            )
        if plan is None:
            record["status"] = "no_liquidity"
            self.submitted_orders.append(record)
            return ExecutionResult(status="no_liquidity", reason="订单簿深度不足")
        if required_min_order_size > 0 and plan.shares + 1e-9 < required_min_order_size:
            record["status"] = "invalid_amount"
            record["status_reason"] = "normalized_shares_below_market_min_order_size"
            record["execution_plan"] = asdict(plan)
            self.submitted_orders.append(record)
            return ExecutionResult(
                status="invalid_amount",
                reason=f"归一化后份额低于市场最小下单量 {required_min_order_size:g}",
            )

        market_amount = normalize_market_amount(intent.action, plan.shares, plan.limit_price)
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
            error_msg = str((record.get("response") or {}).get("errorMsg") or "订单被拒绝")
            is_balance_error = "balance" in error_msg.lower()
            result_metadata: dict[str, Any] = {}
            if is_balance_error:
                if intent.action == "sell":
                    # SELL: ERC1155 条件代币余额不足 — 显示引擎持仓 vs 链上余额
                    chain_balance = self._query_conditional_token_balance(token_id)
                    chain_str = f"{chain_balance:.6f}" if chain_balance is not None else "查询失败"
                    error_msg = (
                        f"卖出失败：链上代币余额不足 | "
                        f"方向={intent.outcome} "
                        f"引擎欲卖={plan.shares:.4f} "
                        f"链上余额={chain_str} "
                        f"(CLOB: {error_msg})"
                    )
                    # 将链上余额传回引擎，用于矫正 PositionBook.held
                    if chain_balance is not None:
                        result_metadata["chain_balance_correction"] = {
                            "outcome": intent.outcome,
                            "chain_balance": chain_balance,
                            "engine_wanted": plan.shares,
                        }
                else:
                    # BUY: USDC 余额不足
                    error_msg = (
                        f"买入失败：USDC 余额不足 | "
                        f"方向={intent.outcome} "
                        f"所需金额≈{market_amount * (1.0 + self.fee_rate):.2f}U "
                        f"(CLOB: {error_msg})"
                    )
                logger.warning("[broker] %s", error_msg)
            record["error_msg"] = error_msg
            self.submitted_orders.append(record)
            return ExecutionResult(status="rejected", reason=error_msg, metadata=result_metadata)

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
                condition_id=str(intent.metadata.get("condition_id") or "").strip(),
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
                    fill, src = self._resolve_fill_after_timeout(pending, timestamp)
                    record["status"] = "confirmed_timeout_fallback"
                    record["confirmation_state"] = "confirmed"
                    record["fill_source"] = src
                    confirmed_fills.append(fill)
                    self.pending_orders.pop(order_id, None)
                continue

            record["confirmation_response"] = order_payload
            payload_dict = self._coerce_order_payload_dict(order_payload)
            status = str(payload_dict.get("status") or "").lower()
            if status in {"matched", "filled", "mined", "confirmed"}:
                fill = None
                if payload_dict:
                    fill = self._build_fill_from_clob_order_payload(payload_dict, pending, timestamp)
                if fill is None:
                    fill = self._build_fill_from_pending(
                        pending,
                        timestamp,
                        fill_source="exchange_get_order_estimate",
                        fill_note="get_order 状态已成交但缺少可解析成交量，退回 post_order/计划估算",
                    )
                else:
                    fill.fill_note = fill.fill_note or "CLOB get_order 解析成交量价"
                    # BUY 订单的 size_matched 为扣费前 gross token，尝试 Data API 获取净份额
                    if pending.intent.action == "buy":
                        api_fill = self._try_build_fill_from_data_api_short(pending, timestamp)
                        if api_fill is not None:
                            fill = api_fill
                        else:
                            fill.fill_note = (
                                f"CLOB get_order 解析（gross token）| "
                                f"Data API 对账未命中，size_matched={fill.shares:.6f} 可能高于实际净份额"
                            )
                            logger.warning(
                                "[broker] BUY 对账回退: order=%s size_matched=%.6f, "
                                "Data API 未命中, 引擎将记录 gross token（可能偏高~4%%）",
                                order_id, fill.shares,
                            )
                record["status"] = "confirmed"
                record["confirmation_state"] = status or "confirmed"
                record["fill_source"] = fill.fill_source
                confirmed_fills.append(fill)
                self.pending_orders.pop(order_id, None)
                continue
            if status in {"cancelled", "canceled", "unmatched", "failed", "rejected"}:
                record["status"] = f"confirm_{status or 'failed'}"
                record["confirmation_state"] = status or "failed"
                self.pending_orders.pop(order_id, None)
                continue
            if self._should_timeout_confirm(pending, timestamp):
                fill, src = self._resolve_fill_after_timeout(pending, timestamp)
                record["status"] = "confirmed_timeout_fallback"
                record["confirmation_state"] = status or "timeout_fallback"
                record["fill_source"] = src
                confirmed_fills.append(fill)
                self.pending_orders.pop(order_id, None)
        return confirmed_fills

    @staticmethod
    def _coerce_order_payload_dict(order_payload: Any) -> dict[str, Any]:
        if isinstance(order_payload, dict):
            return order_payload
        if order_payload is None:
            return {}
        model_dump = getattr(order_payload, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump()
            except Exception:
                dumped = None
            if isinstance(dumped, dict):
                return dumped
        raw = getattr(order_payload, "__dict__", None)
        if isinstance(raw, dict):
            return raw
        return {}

    def _resolve_fill_after_timeout(
        self, pending: PendingOrder, timestamp: datetime
    ) -> tuple[FillEvent, str]:
        api_fill = self._try_build_fill_from_data_api(pending, timestamp)
        if api_fill is not None:
            return api_fill, api_fill.fill_source
        est = self._build_fill_from_pending(
            pending,
            timestamp,
            fill_source="timeout_estimate",
            fill_note="get_order 失败或超时，且 Data API 未匹配到成交；份额/价为 post_order/计划估算，请与交易所对账",
        )
        return est, est.fill_source

    def _should_timeout_confirm(self, pending: PendingOrder, timestamp: datetime) -> bool:
        return (timestamp - pending.submitted_at).total_seconds() >= self.confirmation_timeout_seconds

    def _build_fill_from_pending(
        self,
        pending: PendingOrder,
        timestamp: datetime,
        *,
        fill_source: str,
        fill_note: str = "",
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
            reason=f"{pending.intent.reason} [{fill_source}]",
            reserved_cash=pending.reserved_cash,
            fill_source=fill_source,
            fill_note=fill_note,
            broker_order_id=pending.order_id,
        )

    def _build_fill_from_clob_order_payload(
        self,
        order_payload: dict[str, Any],
        pending: PendingOrder,
        timestamp: datetime,
    ) -> FillEvent | None:
        def _f(name: str) -> float:
            try:
                return float(order_payload.get(name) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        size_matched = _f("size_matched") or _f("sizeMatched") or _f("matched_size") or _f("matchedSize")
        if size_matched <= 0:
            return None

        # ── 实际成交价：优先从 post_order 的 makingAmount/takingAmount 推导 ──
        # get_order 的 price 字段仅为限价，不是成交均价。
        _resp = pending.response or {}
        _making = float(_resp.get("makingAmount") or 0.0)
        _taking = float(_resp.get("takingAmount") or 0.0)
        if _making > 0 and _taking > 0:
            if pending.intent.action == "buy":
                raw_price = _making / _taking        # USDC 支出 / 收到 token
            else:
                raw_price = _taking / _making         # USDC 收入 / 卖出 token
        else:
            raw_price = (
                _f("avg_price")
                or _f("averagePrice")
                or _f("avgPrice")
                or _f("price")
                or pending.execution_plan.estimated_vwap
            )
        if raw_price <= 0:
            return None
        gross = size_matched * raw_price
        fee = gross * self.fee_rate
        return FillEvent(
            market_id=pending.intent.market_id,
            cycle_id=pending.intent.cycle_id,
            timestamp=timestamp,
            price=raw_price,
            shares=size_matched,
            outcome=pending.intent.outcome,
            action=pending.intent.action,
            fee=fee,
            slippage=abs(raw_price - pending.intent.reference_price),
            reason=f"{pending.intent.reason} [exchange_get_order]",
            reserved_cash=pending.reserved_cash,
            fill_source="exchange_get_order",
            fill_note="",
            broker_order_id=pending.order_id,
        )

    @staticmethod
    def _normalize_trade_ts(raw_ts: int) -> int:
        ts = int(raw_ts or 0)
        if ts > 10_000_000_000:
            return ts // 1000
        return ts

    def _try_build_fill_from_data_api(
        self,
        pending: PendingOrder,
        poll_timestamp: datetime,
    ) -> FillEvent | None:
        user = (self.purse_address or "").strip()
        cid = (pending.condition_id or "").strip()
        if not user.startswith("0x") or not cid.startswith("0x"):
            return None
        want_buy = pending.intent.action == "buy"
        try:
            resp = requests.get(
                f"{DATA_API_BASE}/trades",
                params={"user": user, "market": cid, "limit": 120},
                headers={"User-Agent": "PolyNetAI/1.0 (+broker-reconcile)"},
                timeout=(12.0, 45.0),
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return None
        if not isinstance(payload, list) or not payload:
            return None
        since = pending.submitted_at.timestamp()
        best: dict[str, Any] | None = None
        best_dt = 1e18
        for item in payload:
            if not isinstance(item, dict):
                continue
            asset = str(item.get("asset") or "")
            if asset and asset != pending.token_id:
                continue
            side = str(item.get("side") or "").upper()
            if want_buy and side != "BUY":
                continue
            if not want_buy and side != "SELL":
                continue
            try:
                sz = float(item.get("size") or 0.0)
                px = float(item.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if sz <= 0 or px <= 0:
                continue
            ts = self._normalize_trade_ts(int(item.get("timestamp") or 0))
            if ts < since - 3:
                continue
            dt = abs(ts - since)
            if dt < best_dt:
                best_dt = dt
                best = item
        if best is None:
            return None
        sz = float(best.get("size") or 0.0)
        px = float(best.get("price") or 0.0)
        gross = sz * px
        fee = gross * self.fee_rate
        return FillEvent(
            market_id=pending.intent.market_id,
            cycle_id=pending.intent.cycle_id,
            timestamp=poll_timestamp,
            price=px,
            shares=sz,
            outcome=pending.intent.outcome,
            action=pending.intent.action,
            fee=fee,
            slippage=abs(px - pending.intent.reference_price),
            reason=f"{pending.intent.reason} [data_api_trades]",
            reserved_cash=pending.reserved_cash,
            fill_source="data_api_trades",
            fill_note="Data API /trades 按用户+condition+token 侧匹配，可能与链上最终结算仍有细微差异",
            broker_order_id=pending.order_id,
        )

    def _try_build_fill_from_data_api_short(
        self,
        pending: PendingOrder,
        poll_timestamp: datetime,
    ) -> FillEvent | None:
        """用 post_order 的 transactionHash 快速查 Data API 获取扣费后净 shares。

        超时 3 秒；不命中则返回 None，调用方退回 CLOB 估算。
        """
        user = (self.purse_address or "").strip()
        cid = (pending.condition_id or "").strip()
        if not user.startswith("0x") or not cid.startswith("0x"):
            return None
        resp_body = pending.response or {}
        known_tx: set[str] = set()
        for tx in resp_body.get("transactionsHashes") or []:
            if tx:
                known_tx.add(str(tx).strip().lower())
        if not known_tx:
            return None
        try:
            resp = requests.get(
                f"{DATA_API_BASE}/trades",
                params={"user": user, "market": cid, "limit": 60},
                headers={"User-Agent": "PolyNetAI/1.0 (+broker-reconcile-tx)"},
                timeout=(3.0, 5.0),
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return None
        if not isinstance(payload, list):
            return None
        for item in payload:
            if not isinstance(item, dict):
                continue
            tx_hash = str(item.get("transactionHash") or "").strip().lower()
            if tx_hash not in known_tx:
                continue
            try:
                sz = float(item.get("size") or 0.0)
                px = float(item.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if sz <= 0 or px <= 0:
                continue
            gross = sz * px
            fee = gross * self.fee_rate
            return FillEvent(
                market_id=pending.intent.market_id,
                cycle_id=pending.intent.cycle_id,
                timestamp=poll_timestamp,
                price=px,
                shares=sz,
                outcome=pending.intent.outcome,
                action=pending.intent.action,
                fee=fee,
                slippage=abs(px - pending.intent.reference_price),
                reason=f"{pending.intent.reason} [data_api_trades]",
                reserved_cash=pending.reserved_cash,
                fill_source="data_api_trades",
                fill_note="Data API 按 transactionHash 精确匹配（BUY 扣费后净份额）",
                broker_order_id=pending.order_id,
            )
        return None
