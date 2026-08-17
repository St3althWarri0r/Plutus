"""Account aggregation (§4): snapshots (ET-dated, upserting), CSV paste
parsing with honest failures, and net-worth math (forward-fill, paper
exclusion, range slicing)."""

import json
from datetime import UTC, date, datetime

from fakes import FakeAdapter
from pytest import approx, raises
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.aggregation import (
    CsvParseError,
    net_worth_series,
    parse_holdings_csv,
    snapshot_account,
    snapshot_alpaca,
)
from plutus.brokers.base import Position
from plutus.db import Base, make_session_factory
from plutus.models import Snapshot


def make_factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


# --- snapshots ----------------------------------------------------------------


def test_snapshot_uses_et_date_not_utc() -> None:
    factory = make_factory()
    # 2026-08-17 21:00 ET == 2026-08-18 01:00 UTC — must record the ET date
    late_evening = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)
    snapshot_account(
        factory,
        account="m1",
        equity=50_000.0,
        cash=1_000.0,
        positions=[{"symbol": "VTI", "qty": 100}],
        now=late_evening,
    )
    with factory() as session:
        row = session.scalars(select(Snapshot)).one()
        assert row.snapshot_date == date(2026, 8, 17)


def test_same_day_snapshot_upserts() -> None:
    factory = make_factory()
    now = datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
    snapshot_account(factory, account="m1", equity=50_000.0, cash=0.0, positions=[], now=now)
    snapshot_account(factory, account="m1", equity=51_000.0, cash=0.0, positions=[], now=now)
    with factory() as session:
        rows = session.scalars(select(Snapshot)).all()
        assert len(rows) == 1
        assert float(rows[0].equity) == 51_000.0


def test_snapshot_alpaca_keys_by_mode() -> None:
    factory = make_factory()
    adapter = FakeAdapter()
    adapter.positions.append(
        Position(symbol="SPY", qty=1, avg_entry_price=776.0, market_value=774.0, unrealized_pl=-2)
    )
    snapshot_alpaca(
        factory, adapter=adapter, mode="paper", now=datetime(2026, 8, 17, 15, 0, tzinfo=UTC)
    )
    with factory() as session:
        row = session.scalars(select(Snapshot)).one()
        assert row.account == "alpaca_paper"  # paper never pollutes real net worth
        positions = json.loads(row.positions_json)
        assert positions[0]["symbol"] == "SPY"


# --- CSV paste ----------------------------------------------------------------

M1_SAMPLE = """Symbol,Name,Quantity,Avg. Price,Value
VTI,Vanguard Total Stock Market,100.5,220.10,24875.55
AAPL,Apple Inc,10,150.00,2312.40
"""

VANGUARD_SAMPLE = """Investment Name,Symbol,Shares,Share Price,Total Value
Vanguard 500 Index,VFIAX,25.25,510.00,12877.50
Cash,,,1.00,532.10
"""


def test_m1_csv_parses_holdings_and_total() -> None:
    result = parse_holdings_csv(M1_SAMPLE, institution="m1")
    assert result.equity == approx(24875.55 + 2312.40)
    assert {h["symbol"] for h in result.holdings} == {"VTI", "AAPL"}


def test_vanguard_csv_parses_with_cash_row() -> None:
    result = parse_holdings_csv(VANGUARD_SAMPLE, institution="vanguard")
    assert result.equity == approx(12877.50 + 532.10)
    assert result.cash == approx(532.10)


def test_unknown_headers_error_lists_what_was_found() -> None:
    bad = "Ticker,Amount\nVTI,100\n"
    with raises(CsvParseError) as excinfo:
        parse_holdings_csv(bad, institution="m1")
    message = str(excinfo.value)
    assert "Ticker" in message and "Amount" in message  # calibration probe


# --- net-worth math -----------------------------------------------------------


def seed(factory: sessionmaker[Session], account: str, day: date, equity: float) -> None:
    with factory() as session:
        session.add(
            Snapshot(
                account=account, snapshot_date=day, equity=equity, cash=0, positions_json="[]"
            )
        )
        session.commit()


def test_forward_fill_across_sparse_accounts() -> None:
    factory = make_factory()
    seed(factory, "m1", date(2026, 8, 10), 50_000.0)
    seed(factory, "vanguard", date(2026, 8, 12), 30_000.0)
    seed(factory, "m1", date(2026, 8, 14), 52_000.0)

    series = net_worth_series(factory, days=365, today=date(2026, 8, 17))

    values = dict(series.total)
    # on 8/12: m1 forward-filled at 50k + vanguard 30k
    assert values[date(2026, 8, 12)] == approx(80_000.0)
    # on 8/14+: 52k + 30k
    assert values[date(2026, 8, 14)] == approx(82_000.0)


def test_paper_accounts_excluded_from_total_but_listed() -> None:
    factory = make_factory()
    seed(factory, "m1", date(2026, 8, 14), 50_000.0)
    seed(factory, "alpaca_paper", date(2026, 8, 14), 42_000_000.0)

    series = net_worth_series(factory, days=365, today=date(2026, 8, 17))

    assert dict(series.total)[date(2026, 8, 14)] == approx(50_000.0)
    assert "alpaca_paper" in series.latest_by_account  # card still shown


def test_range_slicing() -> None:
    factory = make_factory()
    seed(factory, "m1", date(2025, 1, 1), 10_000.0)
    seed(factory, "m1", date(2026, 8, 10), 50_000.0)
    month = net_worth_series(factory, days=30, today=date(2026, 8, 17))
    assert all(d >= date(2026, 7, 18) for d, _ in month.total)
    assert dict(month.total)[date(2026, 8, 10)] == approx(50_000.0)
