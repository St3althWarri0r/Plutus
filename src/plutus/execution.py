"""Target-weight → order conversion for the daily strategy (§6 engine half).

Whole shares only, floored (brackets don't combine with fractional and the
fractionable-ETF matrix isn't worth carrying). A dead-band suppresses the
1-share churn daily diffing would otherwise emit as prices drift. Sells run
before buys so the cash from exits funds the entries.

append_provisional_close exists because the bar cache deliberately clamps
daily requests to the last COMPLETED day — at 15:50 the engine must append
today's latest trade price as a provisional close or the strategy would
silently trade yesterday's signal.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from math import floor

import pandas as pd

from plutus.brokers.base import OrderIntent
from plutus.logging_setup import get_logger

log = get_logger("plutus.execution")

DEFAULT_DEAD_BAND_USD = 50.0


def compute_rebalance_orders(
    *,
    weights: dict[str, float],
    allocation: float,
    positions: dict[str, float],
    price_lookup: Callable[[str], float | None],
    strategy: str,
    dead_band_usd: float = DEFAULT_DEAD_BAND_USD,
) -> list[OrderIntent]:
    symbols = sorted(set(weights) | set(positions))
    prices: dict[str, float] = {}
    for symbol in symbols:
        price = price_lookup(symbol)
        if price is None or price <= 0:
            raise ValueError(f"cannot price {symbol} for rebalance")
        prices[symbol] = price

    sells: list[OrderIntent] = []
    buys: list[OrderIntent] = []
    for symbol in symbols:
        target_qty = float(floor(weights.get(symbol, 0.0) * allocation / prices[symbol]))
        held = positions.get(symbol, 0.0)
        diff = target_qty - held
        if abs(diff) * prices[symbol] <= dead_band_usd:
            continue
        intent = OrderIntent(
            symbol=symbol,
            side="buy" if diff > 0 else "sell",
            qty=abs(diff),
            order_type="market",
            strategy=strategy,
        )
        (buys if diff > 0 else sells).append(intent)
    orders = sells + buys
    log.info(
        "rebalance_computed",
        strategy=strategy,
        orders=[(o.symbol, o.side, o.qty) for o in orders],
    )
    return orders


def append_provisional_close(
    closes: pd.DataFrame,
    *,
    latest_prices: dict[str, float],
    now: datetime,
) -> pd.DataFrame:
    """Return a copy with today's provisional close appended (or refreshed)."""
    today = pd.Timestamp(
        datetime(now.year, now.month, now.day, tzinfo=UTC).astimezone(UTC)
    )
    out = closes.copy()
    row = {symbol: latest_prices.get(symbol) for symbol in closes.columns}
    if len(out) and out.index[-1] == today:
        out.iloc[-1] = pd.Series(row)
    else:
        out.loc[today] = pd.Series(row)
    return out
