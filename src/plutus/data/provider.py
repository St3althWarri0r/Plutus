"""DataProvider interface and data-quality helpers (§5).

Bar frames are indexed by a UTC DatetimeIndex with columns
open/high/low/close/volume. Providers (Alpaca today, Polygon/Databento later)
slot in behind this protocol without touching strategies.
"""

from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

import pandas as pd

Interval = Literal["1d", "1m"]

_INTERVAL_LENGTH: dict[Interval, timedelta] = {
    "1d": timedelta(days=1),
    "1m": timedelta(minutes=1),
}


@runtime_checkable
class DataProvider(Protocol):
    def get_bars(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> pd.DataFrame: ...


def is_stale(latest_bar_ts: datetime, interval: Interval, now: datetime) -> bool:
    """Data-quality gate (§5): latest bar older than 2× its interval.

    Pure arithmetic — the 'during market hours' condition and the block on new
    entries are wired where the engine runs (Phase 4/5).
    """
    return now - latest_bar_ts > 2 * _INTERVAL_LENGTH[interval]
