"""NYSE session/RTH checks over exchange_calendars (§2), plus the crypto carve-out.

Crypto pairs trade 24/7 and are exempt from the market-hours gate. Detection
is by the pair slash ("BTC/USD") — the only form our order path produces.
"""

from datetime import datetime
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


@lru_cache
def _xnys() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS")


class MarketCalendar:
    """Regular trading hours for NYSE, half-days included."""

    def is_rth(self, dt: datetime) -> bool:
        ts = pd.Timestamp(dt).tz_convert("UTC")
        cal = _xnys()
        if not cal.is_trading_minute(ts):
            return False
        # is_trading_minute treats the closing minute as tradable; RTH entry
        # checks want [open, close) so the 16:00 boundary rejects
        session = cal.minute_to_session(ts)
        return bool(ts < cal.session_close(session))

    def is_session_day(self, dt: datetime) -> bool:
        """Is dt's date (in ET) a trading session at all (any time of day)?"""
        date = pd.Timestamp(dt).tz_convert("America/New_York").date()
        return bool(_xnys().is_session(str(date)))
