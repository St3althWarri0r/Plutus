"""Fill ingestion (watermarked, idempotent) and per-strategy equity.

equity_now(strategy) = day-start mark + net cash flow from today's fills
+ mark-to-market of the strategy's open bot positions. The 1.5% warning
alerts on CROSSING, not on every check; the −3% halt stays in RiskManager.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fakes import FakeAdapter
from pytest import approx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import Fill, OrderIntent
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.models import FillRecord
from plutus.pnl import DailyLossMonitor, equity_now, ingest_fills
from plutus.risk import RiskConfig, RiskManager

ET = ZoneInfo("America/New_York")
RTH = datetime(2024, 6, 5, 14, 0, tzinfo=ET)
T0 = datetime(2024, 6, 5, 13, 40, tzinfo=UTC)


def make_env(tmp_path: Path) -> tuple[RiskManager, FakeAdapter, sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
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
        price_lookup=lambda s: {"SPY": 110.0, "TQQQ": 50.0}.get(s),
    )
    return rm, adapter, factory


def fill(
    order_id: str,
    symbol: str,
    side: Literal["buy", "sell"],
    qty: float,
    price: float,
    at: datetime,
) -> Fill:
    return Fill(
        broker_order_id=order_id, symbol=symbol, side=side, qty=qty, price=price, filled_at=at
    )


def test_ingest_is_watermarked_and_idempotent(tmp_path: Path) -> None:
    rm, adapter, factory = make_env(tmp_path)
    adapter.fills = [
        fill("b1", "SPY", "buy", 5, 100.0, T0),
        fill("b2", "SPY", "sell", 2, 105.0, T0 + timedelta(minutes=5)),
    ]

    n1 = ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))
    n2 = ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))

    assert n1 == 2 and n2 == 0
    with factory() as session:
        rows = session.scalars(select(FillRecord)).all()
        assert len(rows) == 2


def test_ingest_attributes_strategy_from_orders_table(tmp_path: Path) -> None:
    rm, adapter, factory = make_env(tmp_path)
    row = rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=5, order_type="market", strategy="s1")
    )
    assert row.broker_order_id is not None
    adapter.fills = [fill(row.broker_order_id, "SPY", "buy", 5, 100.0, T0)]

    ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))

    with factory() as session:
        rec = session.scalars(select(FillRecord)).one()
        assert rec.strategy == "s1"

    # fills for orders the bot never placed attribute to manual
    adapter.fills.append(fill("unknown-order", "SPY", "buy", 1, 100.0, T0 + timedelta(minutes=1)))
    ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))
    with factory() as session:
        strategies = {r.strategy for r in session.scalars(select(FillRecord)).all()}
        assert strategies == {"s1", "manual"}


def test_fill_dedupe_survives_timestamp_jitter(tmp_path: Path) -> None:
    """Live incident 2026-08-17: Alpaca returned the same fill with a 1μs
    filled_at difference across polls; a timestamp-based key ingested it
    twice, double-counting cash flow and firing a phantom −35% daily halt.
    The key must be built from stable fields only."""
    rm, adapter, factory = make_env(tmp_path)
    adapter.fills = [fill("b9", "QQQ", "buy", 6, 734.29, T0)]
    ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))

    # same fill re-observed with jittered microseconds
    adapter.fills = [fill("b9", "QQQ", "buy", 6, 734.29, T0 + timedelta(microseconds=1))]
    n = ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))

    assert n == 0
    with factory() as session:
        rows = session.scalars(select(FillRecord)).all()
        assert len(rows) == 1


def test_bracket_leg_fill_attributes_to_holder_and_reduces_book(tmp_path: Path) -> None:
    """Broker-created bracket LEGS were never in our orders table. Their fills
    must attribute to the strategy holding the reducing position and shrink
    its book — otherwise the first target hit leaves a stale book, a false
    reconcile halt, and a 15:55 flatten that would short the account."""
    from plutus.models import BotPosition

    rm, adapter, factory = make_env(tmp_path)
    row = rm.submit(
        OrderIntent(
            symbol="SPY",
            side="buy",
            qty=15,
            order_type="market",
            stop_price=109.0,
            take_profit_price=112.0,
            strategy="mode_b",
        )
    )
    assert row.broker_order_id is not None
    # parent fill: tracked order → attributed via orders table; book updated
    # through the sync path
    adapter.fills = [fill(row.broker_order_id, "SPY", "buy", 15, 100.0, T0)]
    ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))
    rm.record_fill("mode_b", "SPY", 15)

    # the target LEG fills — an order id we never submitted
    adapter.fills.append(
        fill("leg-uuid-1", "SPY", "sell", 15, 112.0, T0 + timedelta(minutes=30))
    )
    ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))

    with factory() as session:
        leg = session.scalars(
            select(FillRecord).where(FillRecord.broker_order_id == "leg-uuid-1")
        ).one()
        assert leg.strategy == "mode_b"  # not "manual"
        book = session.scalars(
            select(BotPosition).where(BotPosition.strategy == "mode_b")
        ).one()
        assert float(book.qty) == 0.0  # book reduced to flat


def test_unmatched_unknown_fill_still_attributes_manual(tmp_path: Path) -> None:
    rm, adapter, factory = make_env(tmp_path)
    adapter.fills = [fill("mystery-1", "GME", "buy", 1, 20.0, T0)]
    ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))
    with factory() as session:
        rec = session.scalars(select(FillRecord)).one()
        assert rec.strategy == "manual"


def test_equity_now_cash_flow_plus_mark_to_market(tmp_path: Path) -> None:
    rm, adapter, factory = make_env(tmp_path)
    rm.mark_day_start("s1", equity=10_000.0)
    row = rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=5, order_type="market", strategy="s1")
    )
    assert row.broker_order_id is not None
    adapter.fills = [fill(row.broker_order_id, "SPY", "buy", 5, 100.0, T0)]
    ingest_fills(adapter, factory, clock=lambda: T0 + timedelta(hours=1))
    rm.record_fill("s1", "SPY", 5)

    # bought 5 @ 100 (−500 cash), now marked at 110 (+550 MTM) → +50
    value = equity_now(
        "s1",
        session_factory=factory,
        price_lookup=lambda s: {"SPY": 110.0}.get(s),
        day_start=10_000.0,
        since=T0 - timedelta(hours=1),
    )
    assert value == approx(10_050.0)


def test_daily_loss_monitor_alerts_on_crossing_only(tmp_path: Path) -> None:
    alerts: list[tuple[str, str]] = []
    monitor = DailyLossMonitor(
        warn_pct=0.015, alert=lambda sev, msg: alerts.append((sev, msg))
    )

    monitor.observe("s1", day_start=10_000.0, equity=9_900.0)  # −1.0%: quiet
    monitor.observe("s1", day_start=10_000.0, equity=9_840.0)  # −1.6%: warn
    monitor.observe("s1", day_start=10_000.0, equity=9_830.0)  # still down: quiet
    monitor.observe("s1", day_start=10_000.0, equity=9_900.0)  # recovered
    monitor.observe("s1", day_start=10_000.0, equity=9_840.0)  # crossed again

    warn_count = sum(1 for sev, _ in alerts if sev == "warning")
    assert warn_count == 2
