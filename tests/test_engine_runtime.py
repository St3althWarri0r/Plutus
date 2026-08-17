"""Engine runtime: startup sequence, session ledger, strategy job functions.

Startup order (§11 crash-safe): session row → crash alert if the previous
session never stopped clean → manual baseline → fill sync/ingest → reconcile
→ day-start mark if missed. The daily-loss check must not fire before the
09:30 mark exists (it would measure against yesterday).
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from fakes import FakeAdapter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.engine import EngineRuntime, run_daily_rotation, run_orb_tick
from plutus.models import EngineSession, ManualBaseline
from plutus.risk import RiskConfig, RiskManager
from plutus.strategies.orb import OpeningRangeBreakout, OrbConfig

ET = ZoneInfo("America/New_York")
RTH = datetime(2024, 6, 5, 14, 0, tzinfo=ET)


def make_env(tmp_path: Path) -> tuple[RiskManager, FakeAdapter, sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    adapter = FakeAdapter()
    rm = RiskManager(
        adapter=adapter,
        session_factory=factory,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        config=RiskConfig(
            default_allocation_usd=10_000.0,
            max_position_pct_overrides={"tqqq_rotation": 1.0, "orb": 1.0},
        ),
        clock=lambda: RTH,
        runtime_root=tmp_path,
        price_lookup=lambda s: {"SPY": 100.0, "TQQQ": 50.0, "BSV": 80.0, "UVXY": 80.0}.get(s),
    )
    return rm, adapter, factory


# --- strategy job functions ---------------------------------------------------


def test_run_daily_rotation_submits_rebalance_orders(tmp_path: Path) -> None:
    rm, adapter, _ = make_env(tmp_path)
    idx = pd.date_range("2024-01-01", periods=30, freq="B", tz="UTC")
    up = [50.0 + i for i in range(30)]
    flat = [80.0] * 30
    closes = pd.DataFrame(
        {"TQQQ": up, "UVXY": flat, "BSV": flat, "TECL": flat, "SQQQ": flat}, index=idx
    )

    from plutus.strategies.tqqq_rotation import TQQQRotation

    run_daily_rotation(
        rm,
        strategy=TQQQRotation(sma_long=5, sma_short=3, rsi_period=3),
        closes=closes,
        latest_prices={"TQQQ": 80.0, "UVXY": 80.0, "BSV": 80.0, "TECL": 80.0, "SQQQ": 80.0},
        allocation=10_000.0,
    )

    # relentless rally → overbought hedge branch: 50% UVXY / 50% BSV
    symbols = {(o.symbol, o.side) for o in adapter.submitted}
    assert symbols == {("UVXY", "buy"), ("BSV", "buy")}


def test_run_orb_tick_submits_bracket(tmp_path: Path) -> None:
    rm, adapter, _ = make_env(tmp_path)
    orb = OpeningRangeBreakout(OrbConfig(symbols=["SPY"], range_minutes=3, risk_usd=100.0))
    idx = pd.date_range("2024-06-05 13:30", periods=4, freq="min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.5, 100.2, 101.5],
            "high": [100.7, 100.9, 100.6, 101.6],
            "low": [99.9, 100.1, 100.0, 101.0],
            "close": [100.5, 100.4, 100.3, 101.5],
            "volume": 1e5,
        },
        index=idx,
    )

    run_orb_tick(rm, orb, {"SPY": bars})

    (intent,) = adapter.submitted
    assert intent.strategy == "orb"
    assert intent.stop_price is not None and intent.take_profit_price is not None


# --- runtime lifecycle --------------------------------------------------------


def make_runtime(tmp_path: Path) -> tuple[EngineRuntime, FakeAdapter, sessionmaker[Session]]:
    rm, adapter, factory = make_env(tmp_path)
    runtime = EngineRuntime(
        risk=rm,
        session_factory=factory,
        adapter=adapter,
        clock=lambda: RTH,
        alert=lambda sev, msg: alerts.append((sev, msg)),
    )
    return runtime, adapter, factory


alerts: list[tuple[str, str]] = []


def test_startup_writes_session_marks_baseline_and_reconciles(tmp_path: Path) -> None:
    alerts.clear()
    runtime, adapter, factory = make_runtime(tmp_path)
    from plutus.brokers.base import Position

    adapter.positions.append(
        Position(symbol="LINKUSD", qty=100.0, avg_entry_price=1, market_value=100, unrealized_pl=0)
    )

    runtime.startup()

    with factory() as session:
        row = session.scalars(select(EngineSession)).one()
        assert row.stopped_at is None and not row.clean
        assert session.scalars(select(ManualBaseline)).one().symbol == "LINKUSD"

    runtime.shutdown(clean=True)
    with factory() as session:
        row = session.scalars(select(EngineSession)).one()
        assert row.clean and row.stopped_at is not None


def test_unclean_prior_session_alerts_on_next_startup(tmp_path: Path) -> None:
    alerts.clear()
    runtime, _, factory = make_runtime(tmp_path)
    runtime.startup()  # never shut down → simulated crash

    runtime2, _, _ = (
        EngineRuntime(
            risk=runtime._risk,
            session_factory=factory,
            adapter=runtime._adapter,
            clock=lambda: RTH,
            alert=lambda sev, msg: alerts.append((sev, msg)),
        ),
        None,
        None,
    )
    runtime2.startup()

    assert any("previous session" in msg.lower() for _, msg in alerts)
