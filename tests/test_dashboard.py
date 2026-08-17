"""Phase 1 dashboard: account card, positions, manual paper order form.

The app factory accepts injected adapter/session_factory so these tests run
against the FakeAdapter — never live credentials (CLAUDE.md rule 7).
"""

from datetime import UTC, datetime

from fakes import FakeAdapter
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from plutus.app import create_app
from plutus.brokers.base import OrderStatus, Position
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.models import Order
from plutus.risk import RiskManager


def make_client(adapter: FakeAdapter | None) -> tuple[TestClient, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared connection across TestClient threads
    )
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    risk = None
    if adapter is not None:
        risk = RiskManager(
            adapter=adapter,
            session_factory=factory,
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            clock=lambda: datetime(2024, 6, 5, 18, 0, tzinfo=UTC),  # Wed 14:00 ET
            price_lookup=lambda _s: 100.0,
        )
    app = create_app(adapter=adapter, session_factory=factory, risk_manager=risk)
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
    # ack snippet only — re-rendering the polling orders div here would
    # duplicate it on the page
    assert 'id="orders"' not in resp.text
    (intent,) = adapter.submitted
    assert intent.symbol == "SPY"
    assert intent.qty == 2
    with factory() as session:
        row = session.scalars(select(Order)).one()
        assert row.broker_order_id == "brk-1"
        assert row.trading_mode == "paper"


def test_order_form_passes_time_in_force_through() -> None:
    adapter = FakeAdapter()
    client, _ = make_client(adapter)

    # crypto pairs trade 24/7 but Alpaca rejects day TIF on them — the form
    # must be able to send gtc
    resp = client.post(
        "/orders",
        data={
            "symbol": "BTC/USD",
            "side": "buy",
            "qty": "0.001",
            "order_type": "market",
            "time_in_force": "gtc",
        },
    )

    assert resp.status_code == 200
    (intent,) = adapter.submitted
    assert intent.time_in_force == "gtc"
    assert intent.symbol == "BTC/USD"


def test_order_form_defaults_time_in_force_to_day() -> None:
    adapter = FakeAdapter()
    client, _ = make_client(adapter)

    client.post(
        "/orders",
        data={"symbol": "SPY", "side": "buy", "qty": "1", "order_type": "market"},
    )

    (intent,) = adapter.submitted
    assert intent.time_in_force == "day"


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


def test_strategy_panel_lists_state_and_enable_endpoint_reenables() -> None:
    from plutus.models import StrategyState

    adapter = FakeAdapter()
    client, factory = make_client(adapter)
    with factory() as session:
        session.add(
            StrategyState(
                strategy="tqqq_rotation",
                enabled=False,
                halt_reason="daily loss halt (3.20%)",
                allocation_usd=25_000,
            )
        )
        session.commit()

    page = client.get("/")
    assert "tqqq_rotation" in page.text
    assert "daily loss halt" in page.text

    resp = client.post("/strategies/tqqq_rotation/enable", data={"confirm": "ENABLE"})
    assert resp.status_code == 200
    with factory() as session:
        state = session.scalars(select(StrategyState)).one()
        assert state.enabled and state.halt_reason is None

    # confirmation text required — §8 halts need a deliberate manual re-enable
    with factory() as session:
        state = session.scalars(select(StrategyState)).one()
        state.enabled = False
        session.commit()
    resp = client.post("/strategies/tqqq_rotation/enable", data={"confirm": "nope"})
    assert resp.status_code == 400


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
