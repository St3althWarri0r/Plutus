"""AlpacaDataProvider: request mapping (IEX feed, adjusted bars), frame shape.

Mocked at the alpaca-py market-data client boundary (rule 7).
"""

from datetime import UTC, datetime
from typing import Any

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.timeframe import TimeFrameUnit

from plutus.data.alpaca_data import AlpacaDataProvider


class FakeBarsClient:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def get_stock_bars(self, request: Any) -> Any:
        self.requests.append(request)
        idx = pd.MultiIndex.from_product(
            [
                [request.symbol_or_symbols],
                pd.date_range("2024-01-02 14:30", periods=3, freq="D", tz="UTC"),
            ],
            names=["symbol", "timestamp"],
        )
        frame = pd.DataFrame(
            {
                "open": [10.0, 11.0, 12.0],
                "high": [10.5, 11.5, 12.5],
                "low": [9.5, 10.5, 11.5],
                "close": [10.2, 11.2, 12.2],
                "volume": [1e6, 2e6, 3e6],
                "trade_count": [10, 20, 30],
                "vwap": [10.1, 11.1, 12.1],
            },
            index=idx,
        )

        class Result:
            df = frame

        return Result()


def test_daily_request_uses_iex_feed_and_full_adjustment() -> None:
    client = FakeBarsClient()
    provider = AlpacaDataProvider(client)

    bars = provider.get_bars(
        "TQQQ", "1d", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)
    )

    (req,) = client.requests
    assert req.symbol_or_symbols == "TQQQ"
    assert req.feed == DataFeed.IEX
    assert req.adjustment == Adjustment.ALL
    assert req.timeframe.unit == TimeFrameUnit.Day

    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert len(bars) == 3
    assert bars["close"].iloc[0] == 10.2


def test_minute_request_maps_timeframe() -> None:
    client = FakeBarsClient()
    provider = AlpacaDataProvider(client)

    provider.get_bars(
        "SPY", "1m", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 2, 16, tzinfo=UTC)
    )

    (req,) = client.requests
    assert req.timeframe.unit == TimeFrameUnit.Minute
    assert req.timeframe.amount == 1
