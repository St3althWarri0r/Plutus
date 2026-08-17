"""/net-worth: four account cards, SVG chart, CSV paste import, refresh."""

from datetime import date

from fakes import FakeAdapter
from sqlalchemy import select
from test_dashboard import make_client

from plutus.models import Snapshot


def seed_snapshot(factory, account: str, day: date, equity: float) -> None:  # type: ignore[no-untyped-def]
    with factory() as session:
        session.add(
            Snapshot(
                account=account, snapshot_date=day, equity=equity, cash=0, positions_json="[]"
            )
        )
        session.commit()


def test_page_shows_four_cards_chart_and_paper_exclusion() -> None:
    client, factory = make_client(FakeAdapter())
    seed_snapshot(factory, "m1", date(2026, 8, 14), 50_000.0)
    seed_snapshot(factory, "vanguard", date(2026, 8, 14), 30_000.0)
    seed_snapshot(factory, "alpaca_paper", date(2026, 8, 14), 42_000_000.0)

    page = client.get("/net-worth")

    assert page.status_code == 200
    for marker in ["M1", "Vanguard", "Schwab", "Alpaca"]:
        assert marker in page.text
    assert "<svg" in page.text and "polyline" in page.text
    assert "80,000" in page.text or "80000" in page.text  # total excludes paper
    assert "Phase 8" in page.text  # Schwab honest-empty card


def test_csv_paste_import_writes_snapshot() -> None:
    client, factory = make_client(FakeAdapter())
    csv_text = "Symbol,Name,Quantity,Avg. Price,Value\nVTI,V,100,220,22000.00\n"

    resp = client.post(
        "/import-csv", data={"institution": "m1", "csv_text": csv_text}
    )

    assert resp.status_code == 200
    with factory() as session:
        snap = session.scalars(select(Snapshot).where(Snapshot.account == "m1")).one()
        assert float(snap.equity) == 22_000.0


def test_bad_csv_lists_headers_in_422() -> None:
    client, _ = make_client(FakeAdapter())
    resp = client.post(
        "/import-csv", data={"institution": "m1", "csv_text": "Ticker,Amount\nVTI,5\n"}
    )
    assert resp.status_code == 422
    assert "Ticker" in resp.text


def test_refresh_writes_alpaca_paper_snapshot() -> None:
    client, factory = make_client(FakeAdapter())
    resp = client.post("/refresh-snapshots")
    assert resp.status_code == 200
    with factory() as session:
        snap = session.scalars(
            select(Snapshot).where(Snapshot.account == "alpaca_paper")
        ).one()
        assert float(snap.equity) == 1000.0  # FakeAdapter equity
