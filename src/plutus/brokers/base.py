"""Broker-agnostic domain models and the BrokerAdapter protocol (CLAUDE.md §3).

Every OrderIntent carries a client-generated idempotency key (UUID); adapters
must never submit the same key twice — this is the retry-after-timeout
double-order protection.
"""

import enum
import uuid
from datetime import datetime
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
TimeInForce = Literal["day", "gtc"]


class OrderStatus(enum.StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }


class OrderIntent(BaseModel):
    symbol: str
    side: Side
    qty: float = Field(gt=0)
    order_type: OrderType
    limit_price: float | None = Field(default=None, gt=0)
    time_in_force: TimeInForce = "day"
    strategy: str = "manual"
    idempotency_key: str = Field(default_factory=lambda: str(uuid.uuid4()))

    @field_validator("symbol")
    @classmethod
    def _uppercase_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must be non-empty")
        return v

    @model_validator(mode="after")
    def _limit_price_matches_type(self) -> Self:
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("market orders must not carry limit_price")
        return self


class OrderReceipt(BaseModel):
    broker_order_id: str
    idempotency_key: str
    status: OrderStatus
    submitted_at: datetime | None = None


class Position(BaseModel):
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


class AccountState(BaseModel):
    equity: float
    cash: float
    buying_power: float
    margin_used: float = 0.0


class Fill(BaseModel):
    broker_order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    filled_at: datetime


@runtime_checkable
class BrokerAdapter(Protocol):
    def get_account(self) -> AccountState: ...

    def get_positions(self) -> list[Position]: ...

    def submit_order(self, order: OrderIntent) -> OrderReceipt: ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def get_order_status(self, broker_order_id: str) -> OrderStatus: ...

    def get_fills(self, since: datetime) -> list[Fill]: ...
