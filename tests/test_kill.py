"""Kill switch (§8): KILL file or dashboard button → cancel all open orders,
flatten all bot positions, disable all strategies. Kill-initiated exits are
the only orders allowed once the switch is thrown."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fakes import FakeAdapter
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from plutus.app import create_app
from plutus.brokers.base import OrderIntent
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.models import StrategyState
from plutus.risk import RiskConfig, RiskManager

ET = ZoneInfo("America/New_York")
RTH = datetime(2024, 6, 5, 14, 0, tzinfo=ET)


def make_rm(tmp_path: Path) -> tuple[RiskManager, FakeAdapter, sessionmaker[Session]]:
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    adapter = FakeAdapter()
    rm = RiskManager(
        adapter=adapter,
        session_factory=factory,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        config=RiskConfig(default_allocation_usd=10_000.0),
        clock=lambda: RTH,
        runtime_root=tmp_path,
        price_lookup=lambda _s: 100.0,
    )
    return rm, adapter, factory


def test_kill_cancels_flattens_disables(tmp_path: Path) -> None:
    rm, adapter, factory = make_rm(tmp_path)
    open_order = rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market", strategy="s1")
    )
    rm.record_fill("s1", "SPY", 5)
    rm.record_fill("s2", "TQQQ", 2)

    report = rm.kill(source="test")

    assert (tmp_path / "KILL").exists()
    assert open_order.broker_order_id in adapter.canceled
    flattened = {(o.symbol, o.side) for o in adapter.submitted if o.strategy != "manual"}
    assert ("SPY", "sell") in flattened and ("TQQQ", "sell") in flattened
    with factory() as session:
        states = session.scalars(select(StrategyState)).all()
        assert states and all(not s.enabled for s in states)
    assert report.canceled == 1
    assert sorted(report.flattened) == ["SPY", "TQQQ"]


def test_after_kill_new_orders_rejected(tmp_path: Path) -> None:
    rm, adapter, _ = make_rm(tmp_path)
    rm.kill(source="test")
    row = rm.submit(OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market"))
    assert row.status == "rejected"
    assert "KILL" in (row.reject_reason or "")


def test_kill_cancel_failure_alerts_and_continues(tmp_path: Path) -> None:
    alerts: list[tuple[str, str]] = []
    rm, adapter, _ = make_rm(tmp_path)
    rm._alert = lambda sev, msg: alerts.append((sev, msg))
    rm.submit(OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market", strategy="s1"))
    rm.record_fill("s1", "TQQQ", 2)
    adapter.cancel_fail_with = RuntimeError("api down")

    rm.kill(source="test")  # must not raise

    assert any("cancel" in msg.lower() for _, msg in alerts)
    # flatten still attempted despite cancel failure
    assert any(o.symbol == "TQQQ" and o.side == "sell" for o in adapter.submitted)


def test_kill_endpoint_requires_confirmation(tmp_path: Path) -> None:
    rm, adapter, factory = make_rm(tmp_path)
    app = create_app(adapter=adapter, session_factory=factory, risk_manager=rm)
    client = TestClient(app)
    rm.record_fill("s1", "SPY", 5)

    resp = client.post("/kill", data={"confirm": "nope"})
    assert resp.status_code == 400
    assert not (tmp_path / "KILL").exists()

    resp = client.post("/kill", data={"confirm": "KILL"})
    assert resp.status_code == 200
    assert (tmp_path / "KILL").exists()
    assert any(o.symbol == "SPY" and o.side == "sell" for o in adapter.submitted)
