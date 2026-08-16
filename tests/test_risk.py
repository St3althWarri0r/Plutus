"""RiskManager shim: sole submit path, idempotency dedupe, paper-only in Phase 1.

Full §8 gates arrive in Phase 4; what is pinned here is the routing contract:
nothing reaches BrokerAdapter.submit_order except through RiskManager.submit.
"""

from fakes import FakeAdapter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import OrderIntent
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.models import Order
from plutus.risk import RiskManager


def make_rm() -> tuple[RiskManager, FakeAdapter, sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    adapter = FakeAdapter()
    settings = Settings(trading_mode="paper", _env_file=None)  # type: ignore[call-arg]
    rm = RiskManager(adapter=adapter, session_factory=factory, settings=settings)
    return rm, adapter, factory


def test_submit_routes_to_adapter_and_persists() -> None:
    rm, adapter, factory = make_rm()
    intent = OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market")

    result = rm.submit(intent)

    assert len(adapter.submitted) == 1
    assert result.broker_order_id == "brk-1"
    assert result.status == "accepted"
    with factory() as session:
        row = session.scalars(select(Order)).one()
        assert row.idempotency_key == intent.idempotency_key
        assert row.broker_order_id == "brk-1"
        assert row.trading_mode == "paper"


def test_duplicate_idempotency_key_submits_once() -> None:
    rm, adapter, factory = make_rm()
    intent = OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market")

    first = rm.submit(intent)
    second = rm.submit(intent)

    assert len(adapter.submitted) == 1
    assert first.broker_order_id == second.broker_order_id
    with factory() as session:
        assert len(session.scalars(select(Order)).all()) == 1


def test_adapter_failure_recorded_as_rejected() -> None:
    rm, adapter, factory = make_rm()
    adapter.fail_with = RuntimeError("insufficient buying power")
    intent = OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market")

    result = rm.submit(intent)

    assert result.status == "rejected"
    assert result.reject_reason is not None
    assert "insufficient buying power" in result.reject_reason
    with factory() as session:
        row = session.scalars(select(Order)).one()
        assert row.status == "rejected"


def test_live_mode_rejected_in_phase_1_without_touching_adapter() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    adapter = FakeAdapter()
    settings = Settings(trading_mode="live", _env_file=None)  # type: ignore[call-arg]
    # even if live.lock existed, Phase 1's RiskManager only ever submits paper
    rm = RiskManager(
        adapter=adapter, session_factory=factory, settings=settings, effective_mode="live"
    )

    result = rm.submit(OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market"))

    assert result.status == "rejected"
    assert len(adapter.submitted) == 0
