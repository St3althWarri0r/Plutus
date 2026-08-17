"""NYSE regular-trading-hours checks (exchange_calendars XNYS) + crypto carve-out."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from plutus.market_calendar import MarketCalendar, is_crypto

ET = ZoneInfo("America/New_York")
CAL = MarketCalendar()


def et(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_regular_weekday_midday_is_rth() -> None:
    assert CAL.is_rth(et(2024, 6, 5, 14, 0))


def test_weekend_is_not_rth() -> None:
    assert not CAL.is_rth(et(2024, 6, 8, 14, 0))


def test_premarket_is_not_rth() -> None:
    assert not CAL.is_rth(et(2024, 6, 5, 9, 29))


def test_after_close_is_not_rth() -> None:
    assert not CAL.is_rth(et(2024, 6, 5, 16, 0))  # close boundary exclusive


def test_half_day_early_close_respected() -> None:
    # 2024-07-03 closed at 13:00 ET
    assert CAL.is_rth(et(2024, 7, 3, 12, 0))
    assert not CAL.is_rth(et(2024, 7, 3, 13, 30))


def test_utc_input_accepted() -> None:
    # 2024-06-05 18:00 UTC == 14:00 ET
    assert CAL.is_rth(datetime(2024, 6, 5, 18, 0, tzinfo=UTC))


def test_session_day() -> None:
    assert CAL.is_session_day(et(2024, 6, 5, 7, 0))  # Wednesday, pre-market ok
    assert not CAL.is_session_day(et(2024, 6, 8, 12, 0))  # Saturday
    assert not CAL.is_session_day(et(2024, 7, 4, 12, 0))  # Independence Day


def test_crypto_detection_by_pair_slash() -> None:
    assert is_crypto("BTC/USD")
    assert is_crypto("ETH/USD")
    assert not is_crypto("SPY")
    assert not is_crypto("TQQQ")
