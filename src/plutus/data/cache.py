"""DB-backed bar cache (§5): backtests never re-fetch.

Coverage-range design: bar_coverage stores contiguous [start, end] ranges
already fetched per (symbol, interval). A request inside stored coverage is
served from the bars table with zero vendor calls; anything else fetches the
full requested range, upserts, and merges coverage. Deliberately avoids
exchange-calendar hole accounting.

Daily timestamps are normalized to UTC midnight at this boundary — vendors
stamp daily bars at session open in ET, which would make range comparisons
timezone-brittle.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.data.provider import DataProvider, Interval
from plutus.logging_setup import get_logger
from plutus.models import Bar, BarCoverage

log = get_logger("plutus.data.cache")


def _normalize_ts(ts: pd.Timestamp, interval: Interval) -> datetime:
    dt = ts.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    if interval == "1d":
        return datetime(dt.year, dt.month, dt.day, tzinfo=UTC)
    return dt


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything in this module is UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _normalize_bound(dt: datetime, interval: Interval) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    if interval == "1d":
        return datetime(dt.year, dt.month, dt.day, tzinfo=UTC)
    return dt


class CachedDataProvider:
    """Wraps any DataProvider with the bars/bar_coverage cache."""

    def __init__(
        self,
        provider: DataProvider,
        session_factory: sessionmaker[Session],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_bars(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> pd.DataFrame:
        start = _normalize_bound(start, interval)
        end = _normalize_bound(end, interval)
        if interval == "1d":
            # never claim coverage for a day whose bar is still forming —
            # clamp to the last completed UTC day
            now = self._clock().astimezone(UTC)
            last_complete = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=1)
            end = min(end, last_complete)
        with self._session_factory() as session:
            if not self._covered(session, symbol, interval, start, end):
                self._fetch_and_store(session, symbol, interval, start, end)
            return self._read(session, symbol, interval, start, end)

    def _covered(
        self, session: Session, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> bool:
        row = session.scalars(
            select(BarCoverage).where(
                BarCoverage.symbol == symbol,
                BarCoverage.interval == interval,
                BarCoverage.start_ts <= start,
                BarCoverage.end_ts >= end,
            )
        ).first()
        return row is not None

    def _fetch_and_store(
        self, session: Session, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> None:
        log.info("bars_fetch", symbol=symbol, interval=interval, start=str(start), end=str(end))
        # vendors stamp daily bars inside the session (after UTC midnight), so
        # the vendor request must extend past the normalized end or the final
        # day's bar is silently excluded
        vendor_end = end + timedelta(days=1) if interval == "1d" else end
        frame = self._provider.get_bars(symbol, interval, start, vendor_end)

        # replace any overlapping rows, then insert fresh ones (portable upsert)
        session.execute(
            delete(Bar).where(
                Bar.symbol == symbol,
                Bar.interval == interval,
                Bar.ts >= start,
                Bar.ts <= end,
            )
        )
        frame_idx = pd.DatetimeIndex(frame.index)
        for i, ts in enumerate(frame_idx):
            normalized = _normalize_ts(ts, interval)
            if not (start <= normalized <= end):
                continue  # padding may pull a bar past the coverage window
            row = frame.iloc[i]
            session.add(
                Bar(
                    symbol=symbol,
                    interval=interval,
                    ts=normalized,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
        self._merge_coverage(session, symbol, interval, start, end)
        session.commit()

    def _merge_coverage(
        self, session: Session, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> None:
        overlapping = list(
            session.scalars(
                select(BarCoverage).where(
                    BarCoverage.symbol == symbol,
                    BarCoverage.interval == interval,
                    BarCoverage.start_ts <= end,
                    BarCoverage.end_ts >= start,
                )
            ).all()
        )
        merged_start = min([start, *(_aware(r.start_ts) for r in overlapping)])
        merged_end = max([end, *(_aware(r.end_ts) for r in overlapping)])
        for r in overlapping:
            session.delete(r)
        session.add(
            BarCoverage(symbol=symbol, interval=interval, start_ts=merged_start, end_ts=merged_end)
        )

    def _read(
        self, session: Session, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> pd.DataFrame:
        rows = session.scalars(
            select(Bar)
            .where(
                Bar.symbol == symbol,
                Bar.interval == interval,
                Bar.ts >= start,
                Bar.ts <= end,
            )
            .order_by(Bar.ts)
        ).all()
        index = pd.DatetimeIndex([pd.Timestamp(_aware(r.ts)) for r in rows])
        return pd.DataFrame(
            {
                "open": [float(r.open) for r in rows],
                "high": [float(r.high) for r in rows],
                "low": [float(r.low) for r in rows],
                "close": [float(r.close) for r in rows],
                "volume": [float(r.volume) for r in rows],
            },
            index=index,
        )
