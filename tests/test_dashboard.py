"""Phase 1 dashboard: account card, positions, manual paper order form.

The app factory accepts injected adapter/session_factory so these tests run
against the FakeAdapter — never live credentials (CLAUDE.md rule 7).
"""

from fakes import FakeAdapter
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from plutus.app import create_app
from plutus.brokers.base import OrderStatus, Position
from plutus.db import Base, make_session_factory
from plutus.models import Order


def make_client(adapter: FakeAdapter | None) -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared connection across TestClient threads
    )
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    app = create_app(adapter=adapter, session_factory=factory)
    return TestClient(app), factory


def test_dashboard_shows_paper_banner_account_and_order_form() -> None:
    adapter = FakeAdapter()
    adapter.positions = [
        Position(
            symbol="SPY", qty=10, avg_entry_price=500.0, market_value=5100.0, unrealized_pl=100.0
        )
    ]
    client, _ = make_client(adapter)

    page = client.get("/")

    assert page.status_code == 200
    assert "PAPER" in page.text
    assert "1000.00" in page.text  # equity from FakeAdapter
    assert "SPY" in page.text
    assert '<form' in page.text and 'action="/orders"' in page.text


def test_submit_paper_order_routes_through_risk_manager_and_persists() -> None:
    adapter = FakeAdapter()
    client, factory = make_client(adapter)

    resp = client.post(
        "/orders",
        data={"symbol": "spy", "side": "buy", "qty": "2", "order_type": "market"},
    )

    assert resp.status_code == 200
    assert "accepted" in resp.text
    (intent,) = adapter.submitted
    assert intent.symbol == "SPY"
    assert intent.qty == 2
    with factory() as session:
        row = session.scalars(select(Order)).one()
        assert row.broker_order_id == "brk-1"
        assert row.trading_mode == "paper"


def test_invalid_order_form_returns_error_not_500() -> None:
    client, _ = make_client(FakeAdapter())

    resp = client.post(
        "/orders",
        data={"symbol": "SPY", "side": "buy", "qty": "0", "order_type": "market"},
    )

    assert resp.status_code == 422
    assert "qty" in resp.text


def test_orders_partial_polls_status_to_fill_confirmation() -> None:
    adapter = FakeAdapter()
    client, factory = make_client(adapter)
    client.post(
        "/orders",
        data={"symbol": "SPY", "side": "buy", "qty": "1", "order_type": "market"},
    )
    # broker reports the order filled since submission
    adapter.status_by_broker_id["brk-1"] = OrderStatus.FILLED

    partial = client.get("/partials/orders")

    assert partial.status_code == 200
    assert "filled" in partial.text
    with factory() as session:
        row = session.scalars(select(Order)).one()
        assert row.status == "filled"


def test_missing_credentials_renders_notice_instead_of_crashing() -> None:
    client, _ = make_client(None)

    page = client.get("/")

    assert page.status_code == 200
    assert "ALPACA_PAPER_KEY" in page.text

    resp = client.post(
        "/orders",
        data={"symbol": "SPY", "side": "buy", "qty": "1", "order_type": "market"},
    )
    assert resp.status_code == 503
