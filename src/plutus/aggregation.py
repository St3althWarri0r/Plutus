"""Account aggregation (§4): snapshots, CSV paste parsing, net-worth math.

Permanent decisions:
- Snapshot dates are ET session dates (a 21:00 ET refresh must not write
  tomorrow's UTC date); same-day snapshots upsert.
- The Alpaca account keys by mode (alpaca_paper / alpaca): paper equity is
  fake money and NEVER counts toward net worth — the card shows, the total
  excludes. Blending would poison the chart today and contaminate it
  permanently once live trading starts.
- Net worth forward-fills each account across the union of snapshot dates:
  holdings persist between sparse CSV uploads; gaps would sawtooth the total.
- CSV import is textarea PASTE, not file upload (multipart needs
  python-multipart, which Phase 1 deliberately excluded). A parse failure
  lists the headers actually found — the user's first real paste calibrates
  the header maps.
"""

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import BrokerAdapter
from plutus.logging_setup import get_logger
from plutus.models import Snapshot

log = get_logger("plutus.aggregation")

ET = ZoneInfo("America/New_York")

PAPER_ACCOUNTS = {"alpaca_paper"}

# institution → {canonical field → acceptable header names (lowercased)}
_HEADER_MAPS: dict[str, dict[str, list[str]]] = {
    "m1": {
        "symbol": ["symbol", "ticker"],
        "qty": ["quantity", "shares"],
        "value": ["value", "market value", "total value"],
    },
    "vanguard": {
        "symbol": ["symbol", "ticker"],
        "qty": ["shares", "quantity"],
        "value": ["total value", "value", "market value"],
        "name": ["investment name", "name", "fund name"],
    },
}


class CsvParseError(Exception):
    pass


@dataclass
class ParsedHoldings:
    institution: str
    equity: float
    cash: float
    holdings: list[dict[str, object]] = field(default_factory=list)


def _resolve_headers(
    institution: str, headers: list[str]
) -> dict[str, str]:
    mapping = _HEADER_MAPS[institution]
    lowered = {h.lower().strip(): h for h in headers}
    resolved: dict[str, str] = {}
    for canonical, options in mapping.items():
        for option in options:
            if option in lowered:
                resolved[canonical] = lowered[option]
                break
    missing = {"symbol", "qty", "value"} - set(resolved)
    if missing:
        raise CsvParseError(
            f"{institution} CSV missing expected columns {sorted(missing)}; "
            f"headers found: {headers}. Send this error back so the header "
            "map can be extended."
        )
    return resolved


def parse_holdings_csv(text: str, *, institution: str) -> ParsedHoldings:
    if institution not in _HEADER_MAPS:
        raise CsvParseError(f"unknown institution {institution!r} (m1 | vanguard)")
    reader = csv.DictReader(io.StringIO(text.strip()))
    if reader.fieldnames is None:
        raise CsvParseError("empty CSV")
    resolved = _resolve_headers(institution, list(reader.fieldnames))

    holdings: list[dict[str, object]] = []
    equity = 0.0
    cash = 0.0
    for row in reader:
        raw_value = (row.get(resolved["value"]) or "").replace("$", "").replace(",", "")
        if not raw_value.strip():
            continue
        value = float(raw_value)
        symbol = (row.get(resolved["symbol"]) or "").strip()
        name = (row.get(resolved.get("name", "")) or "").strip().lower()
        if not symbol:
            # symbol-less rows (Vanguard settlement fund etc.) count as cash
            if "cash" in name or "settlement" in name or name:
                cash += value
                equity += value
                continue
            continue
        qty_raw = (row.get(resolved["qty"]) or "0").replace(",", "")
        holdings.append(
            {"symbol": symbol, "qty": float(qty_raw or 0), "value": value}
        )
        equity += value
    if not holdings and cash == 0.0:
        raise CsvParseError("no holdings rows parsed — is this the right export?")
    return ParsedHoldings(
        institution=institution, equity=equity, cash=cash, holdings=holdings
    )


def snapshot_account(
    session_factory: sessionmaker[Session],
    *,
    account: str,
    equity: float,
    cash: float,
    positions: list[dict[str, object]],
    now: datetime | None = None,
) -> None:
    moment = (now or datetime.now(UTC)).astimezone(ET)
    day = moment.date()
    with session_factory() as session:
        row = session.scalars(
            select(Snapshot).where(
                Snapshot.account == account, Snapshot.snapshot_date == day
            )
        ).one_or_none()
        if row is None:
            row = Snapshot(account=account, snapshot_date=day)
            session.add(row)
        row.equity = equity
        row.cash = cash
        row.positions_json = json.dumps(positions, default=str)
        session.commit()
    log.info("snapshot_written", account=account, date=str(day), equity=equity)


def snapshot_alpaca(
    session_factory: sessionmaker[Session],
    *,
    adapter: BrokerAdapter,
    mode: str,
    now: datetime | None = None,
) -> None:
    account = adapter.get_account()
    positions = [
        {"symbol": p.symbol, "qty": p.qty, "value": p.market_value}
        for p in adapter.get_positions()
    ]
    snapshot_account(
        session_factory,
        account=f"alpaca_{mode}" if mode == "paper" else "alpaca",
        equity=account.equity,
        cash=account.cash,
        positions=positions,
        now=now,
    )


@dataclass
class NetWorthSeries:
    total: list[tuple[date, float]]
    by_account: dict[str, list[tuple[date, float]]]
    latest_by_account: dict[str, float]


def net_worth_series(
    session_factory: sessionmaker[Session], *, days: int, today: date | None = None
) -> NetWorthSeries:
    today = today or datetime.now(UTC).astimezone(ET).date()
    cutoff = today - timedelta(days=days)
    with session_factory() as session:
        rows = session.scalars(select(Snapshot).order_by(Snapshot.snapshot_date)).all()

    by_account_raw: dict[str, dict[date, float]] = {}
    for row in rows:
        by_account_raw.setdefault(row.account, {})[row.snapshot_date] = float(row.equity)

    latest = {
        account: values[max(values)] for account, values in by_account_raw.items()
    }

    all_dates = sorted({d for values in by_account_raw.values() for d in values})
    total: list[tuple[date, float]] = []
    by_account: dict[str, list[tuple[date, float]]] = {a: [] for a in by_account_raw}
    running: dict[str, float] = {}
    for day in all_dates:
        for account, values in by_account_raw.items():
            if day in values:
                running[account] = values[day]  # forward-fill
            if account in running and day >= cutoff:
                by_account[account].append((day, running[account]))
        if day >= cutoff:
            total.append(
                (
                    day,
                    sum(v for a, v in running.items() if a not in PAPER_ACCOUNTS),
                )
            )
    return NetWorthSeries(total=total, by_account=by_account, latest_by_account=latest)
