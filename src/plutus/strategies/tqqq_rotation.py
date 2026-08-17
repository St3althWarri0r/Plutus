"""Strategy #1 — TQQQ rotation (§6), ported exactly.

Regime filter is TQQQ-based (not QQQ); the legacy SOXL leg is deliberately
absent; the overbought hedge splits 50% UVXY / 50% BSV per the Aug-2026
revision. Thresholds are strict inequalities (> overbought, < oversold).

Signal logic only: weights_history() maps a closes frame to target weights.
Converting targets to orders is the engine's job (Phase 5); nothing here may
ever talk to a broker.
"""

from collections.abc import Callable

import pandas as pd

from plutus.strategies.indicators import rsi_wilder, sma

UNIVERSE = ["TQQQ", "UVXY", "BSV", "TECL", "SQQQ"]


def branch_weights(
    above_long: bool,
    rsi_tqqq: float,
    above_short: bool,
    rsi_bsv: float,
    rsi_sqqq: float,
    overbought: float,
    oversold: float,
) -> dict[str, float]:
    """The §6 tree for one bar, on already-computed indicator values."""
    if above_long:
        if rsi_tqqq > overbought:
            return {"UVXY": 0.5, "BSV": 0.5}
        return {"TQQQ": 1.0}
    if rsi_tqqq < oversold:
        return {"TECL": 1.0}
    if above_short:
        return {"TQQQ": 1.0}
    # ties break to BSV (defensive: prefer the bond ETF over the inverse 3x)
    return {"SQQQ": 1.0} if rsi_sqqq > rsi_bsv else {"BSV": 1.0}


class TQQQRotation:
    name = "tqqq_rotation"
    schedule = "daily@15:50 ET"  # informational until the live engine (Phase 5)

    def __init__(
        self,
        sma_long: int = 200,
        sma_short: int = 20,
        rsi_period: int = 10,
        overbought: float = 79.0,
        oversold: float = 31.0,
        rsi_fn: Callable[[pd.Series, int], pd.Series] = rsi_wilder,
    ) -> None:
        self.sma_long = sma_long
        self.sma_short = sma_short
        self.rsi_period = rsi_period
        self.overbought = overbought
        self.oversold = oversold
        self.rsi_fn = rsi_fn

    @property
    def universe(self) -> list[str]:
        return list(UNIVERSE)

    @property
    def warmup_bars(self) -> int:
        return max(self.sma_long, self.sma_short, self.rsi_period + 1)

    def weights_history(self, closes: pd.DataFrame) -> pd.DataFrame:
        """Target weights decided at each bar's close; warmup rows are flat."""
        missing = set(UNIVERSE) - set(closes.columns)
        if missing:
            raise ValueError(f"closes frame missing universe symbols: {sorted(missing)}")

        tqqq = closes["TQQQ"]
        long_sma = sma(tqqq, self.sma_long)
        short_sma = sma(tqqq, self.sma_short)
        rsi_tqqq = self.rsi_fn(tqqq, self.rsi_period)
        rsi_bsv = self.rsi_fn(closes["BSV"], self.rsi_period)
        rsi_sqqq = self.rsi_fn(closes["SQQQ"], self.rsi_period)

        ready = (
            long_sma.notna() & short_sma.notna()
            & rsi_tqqq.notna() & rsi_bsv.notna() & rsi_sqqq.notna()
        )

        weights = pd.DataFrame(0.0, index=closes.index, columns=UNIVERSE)
        for i in range(len(closes)):
            if not bool(ready.iloc[i]):
                continue
            allocation = branch_weights(
                above_long=bool(tqqq.iloc[i] > long_sma.iloc[i]),
                rsi_tqqq=float(rsi_tqqq.iloc[i]),
                above_short=bool(tqqq.iloc[i] > short_sma.iloc[i]),
                rsi_bsv=float(rsi_bsv.iloc[i]),
                rsi_sqqq=float(rsi_sqqq.iloc[i]),
                overbought=self.overbought,
                oversold=self.oversold,
            )
            for symbol, weight in allocation.items():
                weights.loc[weights.index[i], symbol] = weight
        return weights
