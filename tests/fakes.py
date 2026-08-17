"""Shared in-memory BrokerAdapter double for tests (CLAUDE.md rule 7)."""

from datetime import datetime

from plutus.brokers.base import (
    AccountState,
    Fill,
    OrderIntent,
    OrderReceipt,
    OrderStatus,
    Position,
)


class FakeAdapter:
    """In-memory BrokerAdapter double; counts submissions per idempotency key."""

    def __init__(self) -> None:
        self.submitted: list[OrderIntent] = []
        self.fail_with: Exception | None = None
        self.cancel_fail_with: Exception | None = None
        self.status_by_broker_id: dict[str, OrderStatus] = {}
        self.positions: list[Position] = []
        self.canceled: list[str] = []

    def get_account(self) -> AccountState:
        return AccountState(equity=1000.0, cash=1000.0, buying_power=2000.0)

    def get_positions(self) -> list[Position]:
        return self.positions

    def submit_order(self, order: OrderIntent) -> OrderReceipt:
        if self.fail_with is not None:
            raise self.fail_with
        self.submitted.append(order)
        broker_id = f"brk-{len(self.submitted)}"
        self.status_by_broker_id.setdefault(broker_id, OrderStatus.ACCEPTED)
        return OrderReceipt(
            broker_order_id=broker_id,
            idempotency_key=order.idempotency_key,
            status=OrderStatus.ACCEPTED,
        )

    def cancel_order(self, broker_order_id: str) -> None:
        if self.cancel_fail_with is not None:
            raise self.cancel_fail_with
        self.canceled.append(broker_order_id)
        self.status_by_broker_id[broker_order_id] = OrderStatus.CANCELED

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        return self.status_by_broker_id.get(broker_order_id, OrderStatus.ACCEPTED)

    def get_fills(self, since: datetime) -> list[Fill]:
        return []
