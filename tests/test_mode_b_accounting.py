"""Mode B closure accounting (fills → realized R → discipline counters),
session P&L in R, §9B.7 stats, and the p95 latency check."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pytest import approx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.ai.discipline import Discipline, SessionCounters
from plutus.ai.mode_b_accounting import (
    compute_stats,
    p95_decision_latency_ms,
    session_pnl_r,
    sync_mode_b_trades,
)
from plutus.ai.mode_b_config import ModeBConfig
from plutus.db import Base, make_session_factory
from plutus.models import AiAudit, BotPosition, FillRecord, ModeBTrade

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
OPENED = NOW - timedelta(minutes=30)


def make_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def add_trade(factory: sessionmaker[Session], **kw: object) -> None:
    base: dict = dict(  # type: ignore[type-arg]
        session_date=NOW.astimezone(ET).date(),
        symbol="NVDA",
        setup="gap_and_go",
        off_plan=False,
        qty=100.0,
        entry_price=100.0,
        stop_price=99.0,
        opened_at=OPENED,
    )
    base.update(kw)
    with factory() as session:
        session.add(ModeBTrade(**base))
        session.commit()


def add_fill(
    factory: sessionmaker[Session], side: str, qty: float, price: float, minutes_ago: int
) -> None:
    at = NOW - timedelta(minutes=minutes_ago)
    with factory() as session:
        session.add(
            FillRecord(
                broker_fill_key=f"k-{side}-{qty}-{price}-{minutes_ago}",
                broker_order_id="b-1",
                strategy="mode_b",
                symbol="NVDA",
                side=side,
                qty=qty,
                price=price,
                filled_at=at,
            )
        )
        session.commit()


def test_flat_position_closes_trade_and_updates_counters() -> None:
    factory = make_factory()
    discipline = Discipline(ModeBConfig(), allocation_usd=25_000.0)  # 1R = $187.50
    add_trade(factory)
    add_fill(factory, "buy", 100, 100.0, 25)
    add_fill(factory, "sell", 100, 101.875, 5)  # +$187.50 = +1R
    counters = SessionCounters()

    sync_mode_b_trades(factory, discipline=discipline, counters=counters, now=NOW)

    with factory() as session:
        trade = session.scalars(select(ModeBTrade)).one()
        assert trade.closed_at is not None
        assert trade.realized_r is not None
        assert float(trade.realized_r) == approx(1.0, abs=0.01)
    assert counters.round_trips == 1


def test_open_position_stays_open() -> None:
    factory = make_factory()
    discipline = Discipline(ModeBConfig(), allocation_usd=25_000.0)
    add_trade(factory)
    add_fill(factory, "buy", 100, 100.0, 25)
    with factory() as session:
        session.add(BotPosition(strategy="mode_b", symbol="NVDA", qty=100))
        session.commit()

    sync_mode_b_trades(
        factory, discipline=discipline, counters=SessionCounters(), now=NOW
    )
    with factory() as session:
        assert session.scalars(select(ModeBTrade)).one().closed_at is None


def test_session_pnl_r_realized_plus_unrealized() -> None:
    factory = make_factory()
    add_trade(factory, closed_at=NOW, realized_r=-1.0)  # closed loser
    add_trade(factory, symbol="AMD", entry_price=50.0, stop_price=49.5, qty=50.0)
    with factory() as session:
        session.add(BotPosition(strategy="mode_b", symbol="AMD", qty=50))
        session.commit()

    # AMD +$0.75/share on 50 shares = +$37.5 unrealized = +0.2R at 1R=$187.5
    total = session_pnl_r(
        factory,
        r_dollars=187.5,
        current_prices={"AMD": 50.75},
        today=NOW.astimezone(ET).date(),
    )
    assert total == approx(-1.0 + 0.2, abs=0.01)


def test_stats_pf_expectancy_and_violations() -> None:
    factory = make_factory()
    for r in (1.5, -1.0, 2.0, -0.5):
        add_trade(factory, closed_at=NOW, realized_r=r)
    stats = compute_stats(factory)
    assert stats["trades"] == 4
    assert stats["profit_factor"] == approx(3.5 / 1.5)
    assert stats["expectancy_r"] == approx((1.5 - 1.0 + 2.0 - 0.5) / 4)
    assert stats["worst_day_r"] == approx(2.0)  # single day net +2.0


def test_p95_latency_over_decision_calls() -> None:
    factory = make_factory()
    with factory() as session:
        for ms in [1000] * 18 + [15000, 16000]:
            session.add(
                AiAudit(
                    mode="decision",
                    model="m",
                    prompt_hash="h",
                    prompt_text="p",
                    latency_ms=ms,
                    created_at=NOW,
                )
            )
        session.commit()
    p95 = p95_decision_latency_ms(factory, today=NOW.astimezone(ET).date())
    assert p95 is not None and p95 >= 15000
