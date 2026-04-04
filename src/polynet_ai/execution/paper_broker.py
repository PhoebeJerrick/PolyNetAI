from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from polynet_ai.domain.models import ExecutionResult, FillEvent, OrderIntent
from polynet_ai.execution.fok_orderbook import (
    fok_precheck_execution_result,
    resolve_outcome_token_id,
    synthetic_paper_fill_from_plan,
)


@dataclass(slots=True)
class PaperBroker:
    """Paper 成交：默认与实盘一致走 CLOB 订单簿 FOK 计划价/量；无 client 时退回参考价±滑点（仅离线/无凭证场景）。"""

    fee_rate: float = 0.002
    slippage_bps: float = 10.0
    clob_client: Any | None = None
    price_buffer_ticks: int = 1
    use_orderbook_min_order_size: bool = True
    market_min_order_size_fallback: float = 5.0
    enforce_sell_min_order_size: bool = True

    def execute(self, intent: OrderIntent, timestamp: datetime) -> ExecutionResult:
        if self.clob_client is not None:
            return self._execute_orderbook_aligned(intent, timestamp)
        return self._execute_legacy_slippage(intent, timestamp)

    def _execute_orderbook_aligned(self, intent: OrderIntent, timestamp: datetime) -> ExecutionResult:
        try:
            token_id = resolve_outcome_token_id(intent)
            book = self.clob_client.get_order_book(token_id)
        except ValueError as exc:
            return ExecutionResult(status="invalid_amount", reason=str(exc))
        except Exception as exc:
            return ExecutionResult(status="rejected", reason=f"orderbook:{exc!s}")

        err, plan = fok_precheck_execution_result(
            intent,
            book=book,
            price_buffer_ticks=self.price_buffer_ticks,
            use_orderbook_min_order_size=self.use_orderbook_min_order_size,
            market_min_order_size_fallback=self.market_min_order_size_fallback,
            enforce_sell_min_order_size=self.enforce_sell_min_order_size,
        )
        if err is not None or plan is None:
            return err or ExecutionResult(status="no_liquidity", reason="订单簿深度不足")

        fill = synthetic_paper_fill_from_plan(intent, plan, timestamp=timestamp, fee_rate=self.fee_rate)
        return ExecutionResult(status="filled", fill=fill)

    def _execute_legacy_slippage(self, intent: OrderIntent, timestamp: datetime) -> ExecutionResult:
        slippage = intent.reference_price * (self.slippage_bps / 10_000.0)
        price = (
            intent.reference_price + slippage
            if intent.action == "buy"
            else max(0.0, intent.reference_price - slippage)
        )
        fee = price * intent.shares * self.fee_rate
        return ExecutionResult(
            status="filled",
            fill=FillEvent(
                market_id=intent.market_id,
                cycle_id=intent.cycle_id,
                timestamp=timestamp,
                price=price,
                shares=intent.shares,
                outcome=intent.outcome,
                action=intent.action,
                fee=fee,
                slippage=slippage,
                reason=intent.reason,
                fill_source="paper_simulated",
            ),
        )

    def poll(self, timestamp: datetime) -> list[FillEvent]:
        return []

    def pending_context(self) -> dict[str, float | int]:
        return {
            "pending_order_count": 0,
            "pending_buy_reserved_cash": 0.0,
            "pending_up_sell_shares": 0.0,
            "pending_down_sell_shares": 0.0,
        }


def paper_broker_for_config(
    config: Mapping[str, Any],
    *,
    env_values: dict[str, str] | None,
    account_index: int = 2,
    signature_type: int = 2,
    force_legacy_slippage: bool = False,
    require_orderbook_client: bool = False,
) -> PaperBroker:
    """按 strategy 配置与环境构造 PaperBroker：有 CLOB 凭证且未强制 legacy 时与实盘共用订单簿 FOK 逻辑。"""
    fee_rate = float(config.get("execution.fee_rate", 0.002))
    slippage_bps = float(config.get("execution.slippage_bps", 10))
    raw_use_ob = config.get("execution.paper_use_orderbook", True)
    if isinstance(raw_use_ob, str):
        use_orderbook = raw_use_ob.strip().lower() in ("1", "true", "yes", "on")
    else:
        use_orderbook = bool(raw_use_ob)
    price_buffer_ticks = int(config.get("execution.price_buffer_ticks", 1))

    base_kw: dict[str, Any] = {
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
        "price_buffer_ticks": price_buffer_ticks,
        "use_orderbook_min_order_size": bool(config.get("execution.use_orderbook_min_order_size", True)),
        "market_min_order_size_fallback": float(config.get("execution.market_min_order_size_fallback", 5.0)),
        "enforce_sell_min_order_size": bool(config.get("execution.enforce_sell_min_order_size", True)),
    }

    if force_legacy_slippage or not use_orderbook:
        return PaperBroker(**base_kw)

    if require_orderbook_client:
        if not env_values:
            raise ValueError("require_orderbook_client=True 但未提供 env_values")
        from polynet_ai.execution.polymarket_broker import clob_client_from_env

        client = clob_client_from_env(
            env_values,
            account_index=account_index,
            signature_type=signature_type,
        )
        return PaperBroker(clob_client=client, **base_kw)

    if not env_values:
        return PaperBroker(**base_kw)

    try:
        from polynet_ai.execution.polymarket_broker import clob_client_from_env

        client = clob_client_from_env(
            env_values,
            account_index=account_index,
            signature_type=signature_type,
        )
        return PaperBroker(clob_client=client, **base_kw)
    except ValueError:
        return PaperBroker(**base_kw)
