"""Domain models for the broker abstraction layer (CLAUDE.md §3)."""

import uuid

import pytest
from pydantic import ValidationError

from plutus.brokers.base import OrderIntent, OrderStatus


def test_order_intent_auto_generates_unique_idempotency_key() -> None:
    a = OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market")
    b = OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market")
    # must be a valid UUID and unique per intent (retry-after-timeout protection)
    uuid.UUID(a.idempotency_key)
    assert a.idempotency_key != b.idempotency_key


def test_order_intent_rejects_non_positive_qty() -> None:
    with pytest.raises(ValidationError):
        OrderIntent(symbol="SPY", side="buy", qty=0, order_type="market")
    with pytest.raises(ValidationError):
        OrderIntent(symbol="SPY", side="buy", qty=-5, order_type="market")


def test_limit_order_requires_limit_price() -> None:
    with pytest.raises(ValidationError):
        OrderIntent(symbol="SPY", side="buy", qty=1, order_type="limit")
    ok = OrderIntent(symbol="SPY", side="buy", qty=1, order_type="limit", limit_price=500.25)
    assert ok.limit_price == 500.25


def test_market_order_rejects_limit_price() -> None:
    with pytest.raises(ValidationError):
        OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market", limit_price=500.0)


def test_symbol_normalized_to_uppercase() -> None:
    intent = OrderIntent(symbol="spy", side="buy", qty=1, order_type="market")
    assert intent.symbol == "SPY"


def test_order_status_terminal_states() -> None:
    assert OrderStatus.FILLED.is_terminal
    assert OrderStatus.CANCELED.is_terminal
    assert OrderStatus.REJECTED.is_terminal
    assert not OrderStatus.NEW.is_terminal
    assert not OrderStatus.PARTIALLY_FILLED.is_terminal
