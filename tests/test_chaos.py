"""Remaining chaos drills (rule 7). The rest of the §8 chaos coverage lives
with its subject: timeout-mid-order (test_alpaca_adapter), duplicate submit
(test_risk), kill drills (test_kill), mismatch/pending/unknown reconcile and
unthrottled daily-loss flatten (test_reconcile), gate storms (test_risk_gates).
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fakes import FakeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from plutus.brokers.base import OrderIntent, OrderStatus, Position
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.risk import RiskConfig, RiskManager
from plutus.scheduler import build_scheduler

ET = ZoneInfo("America/New_York")
RTH = datetime(2024, 6, 5, 14, 0, tzinfo=ET)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_rm(tmp_path: Path, clock: Clock) -> tuple[RiskManager, FakeAdapter]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker = make_session_factory(engine)  # type: ignore[type-arg]
    adapter = FakeAdapter()
    rm = RiskManager(
        adapter=adapter,
        session_factory=factory,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        config=RiskConfig(),
        clock=clock,
        runtime_root=tmp_path,
        price_lookup=lambda _s: 100.0,
    )
    return rm, adapter


def test_duplicate_fill_sync_books_position_once(tmp_path: Path) -> None:
    """The duplicate-webhook analog: a fill observed twice must book once."""
    rm, adapter = make_rm(tmp_path, Clock(RTH))
    row = rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=4, order_type="market", strategy="s1")
    )
    assert row.broker_order_id is not None
    adapter.status_by_broker_id[row.broker_order_id] = OrderStatus.FILLED

    rm.sync_fills()
    rm.sync_fills()  # duplicate delivery

    from sqlalchemy import select

    from plutus.models import BotPosition

    with rm._session_factory() as session:
        pos = session.scalars(select(BotPosition)).one()
        assert float(pos.qty) == 4.0


def test_inflight_order_resolves_to_clean_reconcile(tmp_path: Path) -> None:
    """Pending → no halt; after the fill lands both books agree → still clean."""
    rm, adapter = make_rm(tmp_path, Clock(RTH))
    row = rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=4, order_type="market", strategy="s1")
    )

    first = rm.reconcile()
    assert first.mismatches == [] and "SPY" in first.in_flight

    assert row.broker_order_id is not None
    adapter.status_by_broker_id[row.broker_order_id] = OrderStatus.FILLED
    adapter.positions.append(
        Position(symbol="SPY", qty=4, avg_entry_price=100.0, market_value=400.0, unrealized_pl=0)
    )
    second = rm.reconcile()
    assert second.mismatches == [] and second.in_flight == []


def test_1555_flatten_never_reenables_a_halted_strategy(tmp_path: Path) -> None:
    """§8: daily-loss halt is 'until manual re-enable' — the routine 15:55
    auto-flatten must not clear it."""
    from sqlalchemy import select

    from plutus.models import StrategyState

    rm, adapter = make_rm(tmp_path, Clock(RTH))
    rm.config.intraday_strategies.add("orb")
    rm.record_fill("orb", "SPY", 3)
    rm.mark_day_start("orb", equity=10_000.0)
    rm.check_daily_loss("orb", current_equity=9_000.0)  # −10% → halted

    sched = build_scheduler(rm, equity_lookup=lambda s: 9_000.0, strategies=["orb"])
    sched.get_job("flatten_intraday").func()

    with rm._session_factory() as session:
        state = session.scalars(
            select(StrategyState).where(StrategyState.strategy == "orb")
        ).one()
        assert not state.enabled
        assert "daily loss" in (state.halt_reason or "")


def test_partial_fill_then_cancel_halts_and_alerts(tmp_path: Path) -> None:
    """Rule 6: partial fill → halt + alert + reconcile, never guess. A cancel
    after a partial fill leaves shares the bot book doesn't know about."""
    from sqlalchemy import select

    from plutus.models import StrategyState

    alerts: list[tuple[str, str]] = []
    rm, adapter = make_rm(tmp_path, Clock(RTH))
    rm._alert = lambda sev, msg: alerts.append((sev, msg))
    row = rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=10, order_type="market", strategy="s1")
    )
    assert row.broker_order_id is not None

    adapter.status_by_broker_id[row.broker_order_id] = OrderStatus.PARTIALLY_FILLED
    rm.sync_fills()
    adapter.status_by_broker_id[row.broker_order_id] = OrderStatus.CANCELED
    rm.sync_fills()

    assert any(sev == "critical" and "partial" in msg.lower() for sev, msg in alerts)
    with rm._session_factory() as session:
        state = session.scalars(
            select(StrategyState).where(StrategyState.strategy == "s1")
        ).one()
        assert not state.enabled


def test_scheduler_jobs_skip_non_session_days(tmp_path: Path) -> None:
    clock = Clock(datetime(2024, 7, 4, 9, 15, tzinfo=ET))  # holiday
    rm, adapter = make_rm(tmp_path, clock)
    rm.record_fill("s1", "SPY", 5)  # a mismatch that WOULD halt on a session day
    sched = build_scheduler(rm, equity_lookup=lambda s: 10_000.0, strategies=["s1"])

    sched.get_job("reconcile_am").func()

    from sqlalchemy import select

    from plutus.models import StrategyState

    with rm._session_factory() as session:
        states = session.scalars(select(StrategyState)).all()
        assert all(s.enabled for s in states)  # nothing halted on a holiday
