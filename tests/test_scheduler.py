"""Market-hours jobs (§8): reconcile 09:15/16:15 ET, day-start mark 09:30,
intraday auto-flatten 15:55, periodic daily-loss check. The scheduler is
built unstarted — create_app never spawns one; tests invoke job functions
directly and only assert registry + trigger times, never wall-clock."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fakes import FakeAdapter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.risk import RiskConfig, RiskManager
from plutus.scheduler import build_scheduler

ET = ZoneInfo("America/New_York")
RTH = datetime(2024, 6, 5, 14, 0, tzinfo=ET)


def make_rm(tmp_path: Path) -> tuple[RiskManager, FakeAdapter]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker = make_session_factory(engine)  # type: ignore[type-arg]
    adapter = FakeAdapter()
    rm = RiskManager(
        adapter=adapter,
        session_factory=factory,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        config=RiskConfig(intraday_strategies={"orb"}),
        clock=lambda: RTH,
        runtime_root=tmp_path,
        price_lookup=lambda _s: 100.0,
    )
    return rm, adapter


def test_job_registry_and_times(tmp_path: Path) -> None:
    rm, _ = make_rm(tmp_path)
    sched = build_scheduler(rm, equity_lookup=lambda s: 10_000.0, strategies=["s1"])

    assert not sched.running  # never auto-started
    jobs = {j.id: str(j.trigger) for j in sched.get_jobs()}
    assert "reconcile_am" in jobs and "hour='9', minute='15'" in jobs["reconcile_am"]
    assert "reconcile_pm" in jobs and "hour='16', minute='15'" in jobs["reconcile_pm"]
    assert "day_start_mark" in jobs and "hour='9', minute='30'" in jobs["day_start_mark"]
    assert "flatten_intraday" in jobs and "hour='15', minute='55'" in jobs["flatten_intraday"]
    assert "daily_loss_check" in jobs


def test_day_start_job_marks_equity(tmp_path: Path) -> None:
    rm, _ = make_rm(tmp_path)
    sched = build_scheduler(rm, equity_lookup=lambda s: 12_345.0, strategies=["s1"])
    sched.get_job("day_start_mark").func()

    from sqlalchemy import select

    from plutus.models import StrategyState

    with rm._session_factory() as session:
        state = session.scalars(
            select(StrategyState).where(StrategyState.strategy == "s1")
        ).one()
        assert state.day_start_equity_usd is not None
        assert float(state.day_start_equity_usd) == 12_345.0


def test_flatten_intraday_job_flattens_only_intraday_strategies(tmp_path: Path) -> None:
    rm, adapter = make_rm(tmp_path)
    rm.record_fill("orb", "SPY", 3)       # intraday: must flatten
    rm.record_fill("tqqq_rotation", "TQQQ", 2)  # daily strat: untouched
    sched = build_scheduler(rm, equity_lookup=lambda s: 10_000.0, strategies=["orb"])

    sched.get_job("flatten_intraday").func()

    symbols = {o.symbol for o in adapter.submitted}
    assert symbols == {"SPY"}


def test_daily_loss_job_checks_each_strategy(tmp_path: Path) -> None:
    rm, adapter = make_rm(tmp_path)
    rm.record_fill("s1", "SPY", 3)
    rm.mark_day_start("s1", equity=10_000.0)
    sched = build_scheduler(rm, equity_lookup=lambda s: 9_500.0, strategies=["s1"])  # −5%

    sched.get_job("daily_loss_check").func()

    assert any(o.symbol == "SPY" and o.side == "sell" for o in adapter.submitted)
