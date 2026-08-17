"""Plaid wrapper (§4): link-token, exchange+store, holdings → snapshot.
Fully mocked — Link itself is user-interactive in their browser and
production Investments access needs Plaid approval (runbook items)."""

from datetime import UTC, datetime
from types import SimpleNamespace

from pytest import approx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.db import Base, make_session_factory
from plutus.models import PlaidItem, Snapshot
from plutus.plaid_sync import PlaidSync

NOW = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)


class FakePlaidApi:
    def __init__(self) -> None:
        self.exchanged: list[str] = []

    def link_token_create(self, request: object) -> object:
        return SimpleNamespace(link_token="link-sandbox-token-1")

    def item_public_token_exchange(self, request: object) -> object:
        self.exchanged.append(getattr(request, "public_token", "?"))
        return SimpleNamespace(access_token="access-token-abc", item_id="item-1")

    def investments_holdings_get(self, request: object) -> object:
        security = SimpleNamespace(
            security_id="sec-1", ticker_symbol="VTI", name="Vanguard Total"
        )
        holding = SimpleNamespace(
            security_id="sec-1", quantity=100.0, institution_value=24_000.0
        )
        account = SimpleNamespace(
            balances=SimpleNamespace(available=500.0), type="investment"
        )
        return SimpleNamespace(
            holdings=[holding], securities=[security], accounts=[account]
        )


def make_sync() -> tuple[PlaidSync, FakePlaidApi, sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    api = FakePlaidApi()
    return PlaidSync(client=api, session_factory=factory, clock=lambda: NOW), api, factory


def test_exchange_stores_item() -> None:
    sync, api, factory = make_sync()
    sync.exchange_and_store(public_token="public-x", institution="m1")
    assert api.exchanged == ["public-x"]
    with factory() as session:
        item = session.scalars(select(PlaidItem)).one()
        assert item.institution == "m1"
        assert item.access_token == "access-token-abc"


def test_sync_holdings_writes_snapshot() -> None:
    sync, _, factory = make_sync()
    sync.exchange_and_store(public_token="p", institution="vanguard")

    sync.sync_holdings("vanguard")

    with factory() as session:
        snap = session.scalars(select(Snapshot)).one()
        assert snap.account == "vanguard"
        assert float(snap.equity) == approx(24_000.0 + 500.0)
        assert float(snap.cash) == approx(500.0)
        assert "VTI" in snap.positions_json


def test_sync_unconnected_institution_is_noop() -> None:
    sync, _, factory = make_sync()
    sync.sync_holdings("m1")  # nothing connected — no crash, no snapshot
    with factory() as session:
        assert session.scalars(select(Snapshot)).all() == []
