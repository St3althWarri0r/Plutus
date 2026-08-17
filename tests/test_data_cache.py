"""Bar cache (§5): backtests never re-fetch; coverage ranges merge.

Timestamps are normalized at the cache boundary: daily bars are stored at
UTC midnight regardless of the vendor's session-open stamps.
"""

from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import create_engine

from plutus.data.cache import CachedDataProvider
from plutus.data.provider import Interval, is_stale
from plutus.db import Base, make_session_factory


class CountingFakeProvider:
    """Deterministic daily bars; counts vendor fetches."""

    def __init__(self) -> None:
        self.fetch_calls: list[tuple[str, str, datetime, datetime]] = []

    def get_bars(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> pd.DataFrame:
        self.fetch_calls.append((symbol, interval, start, end))
        # vendor stamps daily bars at 14:30 UTC (09:30 ET session open) and,
        # like the real one, returns nothing stamped after the request end
        days = pd.date_range(start.date(), end.date(), freq="D", tz="UTC")
        idx = days + pd.Timedelta(hours=14, minutes=30)
        idx = idx[idx <= pd.Timestamp(end)]
        base = pd.Series(range(len(idx)), index=idx, dtype=float)
        price = 100.0 + base + (start.toordinal() % 7)  # varies with range start
        return pd.DataFrame(
            {
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
                "volume": 1_000_000.0,
            },
            index=idx,
        )


def make_cache() -> tuple[CachedDataProvider, CountingFakeProvider]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    provider = CountingFakeProvider()
    return CachedDataProvider(provider, make_session_factory(engine)), provider


def _day(d: int) -> datetime:
    return datetime(2024, 1, d, tzinfo=UTC)


def test_first_fetch_then_cache_hit() -> None:
    cache, provider = make_cache()

    first = cache.get_bars("TQQQ", "1d", _day(1), _day(10))
    second = cache.get_bars("TQQQ", "1d", _day(1), _day(10))

    assert len(provider.fetch_calls) == 1
    assert len(first) == 10
    pd.testing.assert_frame_equal(first, second)


def test_subrange_served_from_cache() -> None:
    cache, provider = make_cache()

    cache.get_bars("TQQQ", "1d", _day(1), _day(10))
    sub = cache.get_bars("TQQQ", "1d", _day(3), _day(6))

    assert len(provider.fetch_calls) == 1
    assert len(sub) == 4


def test_partial_overlap_fetches_once_and_merges_coverage() -> None:
    cache, provider = make_cache()

    cache.get_bars("TQQQ", "1d", _day(1), _day(10))
    cache.get_bars("TQQQ", "1d", _day(5), _day(20))
    assert len(provider.fetch_calls) == 2

    # merged coverage: third request inside [1, 20] must not fetch
    full = cache.get_bars("TQQQ", "1d", _day(1), _day(20))
    assert len(provider.fetch_calls) == 2
    assert len(full) == 20


def test_daily_timestamps_normalized_to_utc_midnight() -> None:
    cache, _ = make_cache()

    bars = cache.get_bars("TQQQ", "1d", _day(1), _day(3))

    assert list(bars.index) == list(pd.date_range("2024-01-01", "2024-01-03", tz="UTC"))


def test_symbols_and_intervals_are_isolated() -> None:
    cache, provider = make_cache()

    cache.get_bars("TQQQ", "1d", _day(1), _day(5))
    cache.get_bars("SPY", "1d", _day(1), _day(5))

    assert len(provider.fetch_calls) == 2


def test_final_day_bar_included_despite_vendor_session_stamps() -> None:
    """A midnight end bound must still pull the end day's ET-stamped bar.

    Regression: the vendor stamps daily bars after midnight UTC, so passing
    the normalized bound through to the vendor silently drops the last day —
    and coverage then poisons the cache against ever fetching it.
    """
    cache, _ = make_cache()

    bars = cache.get_bars("TQQQ", "1d", _day(1), _day(10))

    assert bars.index[-1] == pd.Timestamp("2024-01-10", tz="UTC")
    assert len(bars) == 10


def test_end_bound_clamped_to_last_completed_utc_day() -> None:
    """Requesting through 'today' must not claim coverage for today's
    still-forming bar; a later request must fetch it."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    provider = CountingFakeProvider()
    now = {"t": datetime(2024, 1, 10, 15, 0, tzinfo=UTC)}  # mid-session Jan 10
    cache = CachedDataProvider(
        provider, make_session_factory(engine), clock=lambda: now["t"]
    )

    bars = cache.get_bars("TQQQ", "1d", _day(1), _day(10))
    assert bars.index[-1] == pd.Timestamp("2024-01-09", tz="UTC")
    assert len(provider.fetch_calls) == 1

    # next day, the same request must re-fetch to pick up Jan 10
    now["t"] = datetime(2024, 1, 11, 15, 0, tzinfo=UTC)
    bars = cache.get_bars("TQQQ", "1d", _day(1), _day(10))
    assert bars.index[-1] == pd.Timestamp("2024-01-10", tz="UTC")
    assert len(provider.fetch_calls) == 2


def test_is_stale_2x_interval() -> None:
    now = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)
    assert is_stale(now - timedelta(minutes=3), "1m", now)
    assert not is_stale(now - timedelta(minutes=1), "1m", now)
    assert is_stale(now - timedelta(days=3), "1d", now)
    assert not is_stale(now - timedelta(days=1), "1d", now)
