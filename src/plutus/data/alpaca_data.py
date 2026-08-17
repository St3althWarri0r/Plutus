"""Alpaca Market Data provider (§5): free IEX feed, split/dividend-adjusted bars.

adjustment='all' matters: TQQQ has split repeatedly and raw bars would poison
every SMA/RSI signal across split dates. A future split invalidates cached
history — flush the bars table for that symbol and re-fetch.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from plutus.brokers.alpaca import MissingCredentialsError
from plutus.config import Settings
from plutus.data.provider import Interval

_TIMEFRAMES: dict[Interval, TimeFrame] = {
    "1d": TimeFrame(1, TimeFrameUnit.Day),
    "1m": TimeFrame(1, TimeFrameUnit.Minute),
}

_COLUMNS = ["open", "high", "low", "close", "volume"]


class AlpacaDataProvider:
    """DataProvider over alpaca-py's StockHistoricalDataClient."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get_bars(
        self, symbol: str, interval: Interval, start: datetime, end: datetime
    ) -> pd.DataFrame:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_TIMEFRAMES[interval],
            start=start,
            end=end,
            adjustment=Adjustment.ALL,
            feed=DataFeed.IEX,
        )
        frame = self._client.get_stock_bars(request).df
        if frame.empty:
            return pd.DataFrame(columns=_COLUMNS)
        return cast(pd.DataFrame, frame.xs(symbol, level="symbol")[_COLUMNS])


def alpaca_data_provider_from_settings(
    settings: Settings,
    client_factory: Callable[..., Any] | None = None,
) -> AlpacaDataProvider:
    """Market data works with either key pair; prefer paper keys when present."""
    factory = client_factory or cast(Callable[..., Any], StockHistoricalDataClient)
    key = settings.alpaca_paper_key or settings.alpaca_api_key
    secret = settings.alpaca_paper_secret or settings.alpaca_secret_key
    if not key or not secret:
        raise MissingCredentialsError("market data requires an Alpaca key pair in .env")
    return AlpacaDataProvider(factory(api_key=key, secret_key=secret))
