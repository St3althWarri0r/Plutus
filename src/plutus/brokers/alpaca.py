"""Alpaca adapter (paper + live from one code path, selected by trading mode).

The idempotency key rides Alpaca's client_order_id. On an ambiguous submit
failure (timeout / connection drop) the adapter looks the order up by that key
before concluding anything: if Alpaca has it, the original receipt is returned;
if Alpaca does not, OrderSubmitError is raised. It never blind-retries — that
is exactly the retry-after-timeout double-order the key exists to prevent.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.enums import TimeInForce as AlpacaTIF
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

from plutus.brokers.base import (
    AccountState,
    Fill,
    OrderIntent,
    OrderReceipt,
    OrderStatus,
    Position,
)
from plutus.config import Settings, TradingMode
from plutus.logging_setup import get_logger

log = get_logger("plutus.brokers.alpaca")

_AMBIGUOUS_ERRORS = (requests.Timeout, requests.ConnectionError)

_STATUS_MAP = {
    "new": OrderStatus.NEW,
    "pending_new": OrderStatus.NEW,
    "accepted": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "pending_cancel": OrderStatus.ACCEPTED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
    "done_for_day": OrderStatus.EXPIRED,
}


class MissingCredentialsError(RuntimeError):
    """The keys required for the requested trading mode are not configured."""


class OrderSubmitError(RuntimeError):
    """Submit failed and the order is confirmed absent at the broker."""


def _to_status(raw: object) -> OrderStatus:
    value = getattr(raw, "value", raw)  # alpaca enum or plain string
    return _STATUS_MAP.get(str(value), OrderStatus.UNKNOWN)


class AlpacaAdapter:
    """BrokerAdapter implementation over an alpaca-py TradingClient."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get_account(self) -> AccountState:
        acct = self._client.get_account()
        return AccountState(
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power),
            margin_used=float(acct.maintenance_margin or 0),
        )

    def get_positions(self) -> list[Position]:
        return [
            Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
            )
            for p in self._client.get_all_positions()
        ]

    def submit_order(self, order: OrderIntent) -> OrderReceipt:
        request = self._build_request(order)
        try:
            placed = self._client.submit_order(order_data=request)
        except _AMBIGUOUS_ERRORS as exc:
            log.warning(
                "submit_ambiguous_failure",
                symbol=order.symbol,
                idempotency_key=order.idempotency_key,
                error=str(exc),
            )
            placed = self._lookup_after_ambiguous_failure(order, exc)
        return OrderReceipt(
            broker_order_id=str(placed.id),
            idempotency_key=order.idempotency_key,
            status=_to_status(placed.status),
            submitted_at=placed.submitted_at,
        )

    def _lookup_after_ambiguous_failure(self, order: OrderIntent, cause: Exception) -> Any:
        """The submit call died mid-flight; ask Alpaca whether the order exists."""
        try:
            found = self._client.get_order_by_client_id(order.idempotency_key)
        except Exception:
            raise OrderSubmitError(
                f"submit of {order.symbol} ({order.idempotency_key}) failed with "
                f"{type(cause).__name__} and the order is not present at Alpaca; "
                "not retrying automatically"
            ) from cause
        log.info(
            "submit_recovered_after_timeout",
            symbol=order.symbol,
            idempotency_key=order.idempotency_key,
            broker_order_id=str(found.id),
        )
        return found

    def cancel_order(self, broker_order_id: str) -> None:
        self._client.cancel_order_by_id(broker_order_id)

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        return _to_status(self._client.get_order_by_id(broker_order_id).status)

    def get_fills(self, since: datetime) -> list[Fill]:
        request = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=since)
        fills: list[Fill] = []
        for o in self._client.get_orders(filter=request):
            if not o.filled_at or not float(o.filled_qty or 0):
                continue
            fills.append(
                Fill(
                    broker_order_id=str(o.id),
                    symbol=o.symbol,
                    side="buy" if str(getattr(o.side, "value", o.side)) == "buy" else "sell",
                    qty=float(o.filled_qty),
                    price=float(o.filled_avg_price),
                    filled_at=o.filled_at,
                )
            )
        return fills

    def _build_request(self, order: OrderIntent) -> MarketOrderRequest | LimitOrderRequest:
        common: dict[str, Any] = {
            "symbol": order.symbol,
            "qty": order.qty,
            "side": OrderSide.BUY if order.side == "buy" else OrderSide.SELL,
            "time_in_force": AlpacaTIF.DAY if order.time_in_force == "day" else AlpacaTIF.GTC,
            "client_order_id": order.idempotency_key,
        }
        if order.order_type == "limit":
            return LimitOrderRequest(limit_price=order.limit_price, **common)
        return MarketOrderRequest(**common)


def alpaca_adapter_from_settings(
    settings: Settings,
    mode: TradingMode,
    client_factory: Callable[..., Any] | None = None,
) -> AlpacaAdapter:
    """Build an adapter for the given *effective* trading mode.

    Paper mode constructs the client from the paper key pair only; live keys
    are never read on the paper path (and vice versa).
    """
    factory = client_factory or cast(Callable[..., Any], TradingClient)
    if mode == "paper":
        if not settings.alpaca_paper_key or not settings.alpaca_paper_secret:
            raise MissingCredentialsError(
                "paper mode requires ALPACA_PAPER_KEY and ALPACA_PAPER_SECRET in .env"
            )
        client = factory(
            api_key=settings.alpaca_paper_key,
            secret_key=settings.alpaca_paper_secret,
            paper=True,
        )
    else:
        if not settings.alpaca_api_key or not settings.alpaca_secret_key:
            raise MissingCredentialsError(
                "live mode requires ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
            )
        client = factory(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=False,
        )
    return AlpacaAdapter(client)
