"""Strategy #2 — opening-range breakout on SPY/QQQ (§6 intraday template).

Parameters come from strategies.toml (config, not code). Long-only: after the
first `range_minutes` of the session define [range_low, range_high]; the first
minute close above range_high enters with a bracket — stop at the range low,
target at entry + target_r × (entry − stop), sized floor(risk_usd / R).
One entry per symbol per day; state resets when the session date changes.
"""

import tomllib
from datetime import date
from math import floor
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field

from plutus.brokers.base import OrderIntent
from plutus.logging_setup import get_logger

log = get_logger("plutus.strategies.orb")

STRATEGY_NAME = "orb"


class OrbConfig(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ"])
    range_minutes: int = Field(default=15, gt=0)
    risk_usd: float = Field(default=100.0, gt=0)
    target_r: float = Field(default=2.0, gt=0)
    # at SPY ≈ $776 risk-based sizing alone yields ~$25k notional and dies at
    # the §8 position-size gate — the notional cap binds first
    max_notional_usd: float = Field(default=5_000.0, gt=0)


def load_orb_config(path: Path) -> OrbConfig:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return OrbConfig.model_validate(data.get("orb", {}))


class OpeningRangeBreakout:
    name = STRATEGY_NAME

    def __init__(self, config: OrbConfig) -> None:
        self.config = config
        self._entered: dict[str, date] = {}  # symbol → session date of entry

    def on_minute(self, symbol: str, bars_today: pd.DataFrame) -> OrderIntent | None:
        """bars_today: today's 1-minute bars from the open through now."""
        n = self.config.range_minutes
        if len(bars_today) <= n:
            return None  # range still forming

        session = pd.Timestamp(bars_today.index[-1]).date()
        if self._entered.get(symbol) == session:
            return None

        range_bars = bars_today.iloc[:n]
        range_high = float(range_bars["high"].max())
        range_low = float(range_bars["low"].min())
        last_close = float(bars_today["close"].iloc[-1])
        if last_close <= range_high:
            return None

        per_share_risk = last_close - range_low
        if per_share_risk <= 0:
            return None
        qty = min(
            floor(self.config.risk_usd / per_share_risk),
            floor(self.config.max_notional_usd / last_close),
        )
        if qty < 1:
            log.info("orb_skip_unsizeable", symbol=symbol, r=per_share_risk, price=last_close)
            return None

        self._entered[symbol] = session
        target = last_close + self.config.target_r * per_share_risk
        log.info(
            "orb_breakout",
            symbol=symbol,
            entry=last_close,
            stop=range_low,
            target=target,
            qty=qty,
        )
        return OrderIntent(
            symbol=symbol,
            side="buy",
            qty=qty,
            order_type="market",
            stop_price=range_low,
            take_profit_price=target,
            strategy=STRATEGY_NAME,
        )
