"""AlpacaAdapter: mapping, key-selection safety, retry-after-timeout chaos.

Mocked at the alpaca-py client boundary per CLAUDE.md rule 7 — never against
live credentials. Assertions target adapter behavior (what reaches the client,
what comes back), not mock call bookkeeping.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from plutus.brokers.alpaca import (
    AlpacaAdapter,
    MissingCredentialsError,
    OrderSubmitError,
    alpaca_adapter_from_settings,
)
from plutus.brokers.base import OrderIntent, OrderStatus
from plutus.config import Settings


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "trading_mode": "paper",
        "alpaca_api_key": "LIVE_KEY",
        "alpaca_secret_key": "LIVE_SECRET",
        "alpaca_paper_key": "PAPER_KEY",
        "alpaca_paper_secret": "PAPER_SECRET",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class CapturingClientFactory:
    def __init__(self, client: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.client = client or FakeTradingClient()

    def __call__(self, api_key: str, secret_key: str, paper: bool) -> Any:
        self.calls.append({"api_key": api_key, "secret_key": secret_key, "paper": paper})
        return self.client


class FakeTradingClient:
    """Stands in for alpaca.trading.client.TradingClient."""

    def __init__(self) -> None:
        self.submitted: list[Any] = []
        self.submit_error: Exception | None = None
        self.orders_by_client_id: dict[str, Any] = {}

    def get_account(self) -> Any:
        return SimpleNamespace(
            equity="10000.50", cash="2500.25", buying_power="20001.00", maintenance_margin="0"
        )

    def get_all_positions(self) -> list[Any]:
        return [
            SimpleNamespace(
                symbol="SPY",
                qty="10",
                avg_entry_price="500.10",
                market_value="5100.00",
                unrealized_pl="99.00",
            )
        ]

    def submit_order(self, order_data: Any) -> Any:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(order_data)
        order = SimpleNamespace(
            id="broker-id-1",
            client_order_id=order_data.client_order_id,
            status="accepted",
            submitted_at=datetime(2026, 8, 14, 14, 0, tzinfo=UTC),
        )
        self.orders_by_client_id[order_data.client_order_id] = order
        return order

    def get_order_by_client_id(self, client_id: str) -> Any:
        try:
            return self.orders_by_client_id[client_id]
        except KeyError:
            raise requests.HTTPError("order not found") from None

    def get_order_by_id(self, order_id: str) -> Any:
        return SimpleNamespace(id=order_id, status="filled")

    def cancel_order_by_id(self, order_id: str) -> None:
        pass

    def get_orders(self, filter: Any = None) -> list[Any]:
        return []


# --- key selection is a safety property ---------------------------------------


def test_paper_mode_constructs_client_with_paper_keys_only() -> None:
    factory = CapturingClientFactory()
    alpaca_adapter_from_settings(make_settings(), mode="paper", client_factory=factory)

    assert factory.calls == [
        {"api_key": "PAPER_KEY", "secret_key": "PAPER_SECRET", "paper": True}
    ]


def test_missing_paper_keys_raises_not_silently_falls_back_to_live() -> None:
    settings = make_settings(alpaca_paper_key=None, alpaca_paper_secret=None)
    factory = CapturingClientFactory()
    with pytest.raises(MissingCredentialsError):
        alpaca_adapter_from_settings(settings, mode="paper", client_factory=factory)


# --- order mapping ------------------------------------------------------------


def test_submit_market_order_maps_fields_and_idempotency_key() -> None:
    client = FakeTradingClient()
    adapter = AlpacaAdapter(client)
    intent = OrderIntent(symbol="SPY", side="buy", qty=2, order_type="market")

    receipt = adapter.submit_order(intent)

    (req,) = client.submitted
    assert req.symbol == "SPY"
    assert req.qty == 2
    assert req.client_order_id == intent.idempotency_key
    assert receipt.broker_order_id == "broker-id-1"
    assert receipt.status == OrderStatus.ACCEPTED
    assert receipt.idempotency_key == intent.idempotency_key


def test_submit_limit_order_carries_limit_price() -> None:
    client = FakeTradingClient()
    adapter = AlpacaAdapter(client)
    intent = OrderIntent(symbol="QQQ", side="sell", qty=1, order_type="limit", limit_price=400.5)

    adapter.submit_order(intent)

    (req,) = client.submitted
    assert req.limit_price == 400.5


def test_bracket_order_maps_stop_and_take_profit_legs() -> None:
    from alpaca.trading.enums import OrderClass

    client = FakeTradingClient()
    adapter = AlpacaAdapter(client)
    intent = OrderIntent(
        symbol="SPY",
        side="buy",
        qty=40,
        order_type="market",
        stop_price=99.8,
        take_profit_price=107.8,
        strategy="orb",
    )

    adapter.submit_order(intent)

    (req,) = client.submitted
    assert req.order_class == OrderClass.BRACKET
    assert req.stop_loss.stop_price == 99.8
    assert req.take_profit.limit_price == 107.8


def test_replace_stop_finds_stop_leg_and_replaces() -> None:
    client = FakeTradingClient()
    replaced: list[tuple[str, Any]] = []

    class StopLeg:
        id = "leg-stop-1"
        order_type = "stop"

    class TpLeg:
        id = "leg-tp-1"
        order_type = "limit"

    client.get_order_by_id = lambda order_id, options=None: SimpleNamespace(  # type: ignore[method-assign, misc]
        id=order_id, legs=[TpLeg(), StopLeg()]
    )
    client.replace_order_by_id = lambda order_id, order_data: replaced.append(  # type: ignore[attr-defined]
        (order_id, order_data)
    )
    adapter = AlpacaAdapter(client)

    adapter.replace_stop("parent-1", 101.5)

    ((leg_id, request),) = replaced
    assert leg_id == "leg-stop-1"
    assert request.stop_price == 101.5  # ReplaceOrderRequest carries the new stop


def test_replace_stop_raises_when_no_stop_leg() -> None:
    client = FakeTradingClient()
    client.get_order_by_id = lambda order_id, options=None: SimpleNamespace(  # type: ignore[method-assign, misc]
        id=order_id, legs=None
    )
    adapter = AlpacaAdapter(client)
    with pytest.raises(RuntimeError, match="stop leg"):
        adapter.replace_stop("parent-1", 101.5)


# --- chaos: API timeout mid-order (CLAUDE.md rule 7) --------------------------


def test_timeout_then_order_found_at_broker_returns_receipt_without_resubmit() -> None:
    client = FakeTradingClient()
    adapter = AlpacaAdapter(client)
    intent = OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market")

    # the request timed out but the order actually reached Alpaca
    client.submit_error = requests.Timeout("read timed out")
    client.orders_by_client_id[intent.idempotency_key] = SimpleNamespace(
        id="broker-id-77",
        client_order_id=intent.idempotency_key,
        status="accepted",
        submitted_at=None,
    )

    receipt = adapter.submit_order(intent)

    assert receipt.broker_order_id == "broker-id-77"
    assert client.submitted == []  # never re-submitted


def test_timeout_and_order_absent_at_broker_raises_submit_error() -> None:
    client = FakeTradingClient()
    adapter = AlpacaAdapter(client)
    client.submit_error = requests.Timeout("read timed out")

    with pytest.raises(OrderSubmitError):
        adapter.submit_order(OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market"))

    assert client.submitted == []


# --- account / positions / status mapping -------------------------------------


def test_account_state_parsed_from_strings() -> None:
    adapter = AlpacaAdapter(FakeTradingClient())
    account = adapter.get_account()
    assert account.equity == 10000.50
    assert account.cash == 2500.25
    assert account.buying_power == 20001.00


def test_positions_parsed() -> None:
    adapter = AlpacaAdapter(FakeTradingClient())
    (pos,) = adapter.get_positions()
    assert pos.symbol == "SPY"
    assert pos.qty == 10
    assert pos.unrealized_pl == 99.00


def test_order_status_mapping_unknown_fallback() -> None:
    adapter = AlpacaAdapter(FakeTradingClient())
    assert adapter.get_order_status("any") == OrderStatus.FILLED

    client = FakeTradingClient()
    client.get_order_by_id = lambda order_id: SimpleNamespace(  # type: ignore[method-assign]
        id=order_id, status="accepted_for_bidding"
    )
    adapter = AlpacaAdapter(client)
    assert adapter.get_order_status("any") == OrderStatus.UNKNOWN
