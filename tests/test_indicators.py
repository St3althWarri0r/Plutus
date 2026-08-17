"""SMA and RSI(10) — the strategy's signal semantics.

Wilder's RSI (the TA-Lib/TradingView standard) is the default: seed the
average gain/loss with the SMA of the first `period` changes, then recurse
avg = (prev·(period−1) + current)/period. Cutler's variant (plain SMA of
gains/losses) exists to measure divergence against the Composer-era numbers.
"""

import numpy as np
import pandas as pd
from pytest import approx

from plutus.strategies.indicators import rsi_cutler, rsi_wilder, sma


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="B"))


def test_sma_basic_and_warmup_nan() -> None:
    s = series([1, 2, 3, 4, 5])
    result = sma(s, 3)
    assert np.isnan(result.iloc[0]) and np.isnan(result.iloc[1])
    assert result.iloc[2] == approx(2.0)
    assert result.iloc[4] == approx(4.0)


def test_wilder_rsi_hand_computed_recursion() -> None:
    # period 3; changes: +1, +2, -1, +0.5, -2
    s = series([10.0, 11.0, 13.0, 12.0, 12.5, 10.5])
    result = rsi_wilder(s, 3)

    # seed at t3: avg_gain = (1+2+0)/3 = 1.0 ; avg_loss = (0+0+1)/3 = 1/3
    rs3 = 1.0 / (1 / 3)
    assert result.iloc[3] == approx(100 - 100 / (1 + rs3))  # 75.0

    # t4: avg_gain = (1.0·2 + 0.5)/3 = 5/6 ; avg_loss = ((1/3)·2 + 0)/3 = 2/9
    rs4 = (5 / 6) / (2 / 9)
    assert result.iloc[4] == approx(100 - 100 / (1 + rs4))

    # t5: avg_gain = ((5/6)·2 + 0)/3 = 5/9 ; avg_loss = ((2/9)·2 + 2)/3 = 22/27
    rs5 = (5 / 9) / (22 / 27)
    assert result.iloc[5] == approx(100 - 100 / (1 + rs5))

    # warmup is NaN through the seed-1 position
    assert result.iloc[:3].isna().all()


def test_rsi_all_up_is_100_all_down_is_0() -> None:
    up = rsi_wilder(series([1, 2, 3, 4, 5, 6, 7]), 3)
    down = rsi_wilder(series([7, 6, 5, 4, 3, 2, 1]), 3)
    assert up.iloc[-1] == approx(100.0)
    assert down.iloc[-1] == approx(0.0)


def test_cutler_rsi_is_sma_based() -> None:
    # same series as the Wilder hand case; Cutler at t4 uses plain 3-bar SMA
    # of gains/losses over changes (+2, −1, +0.5): avg_gain=(2+0+0.5)/3,
    # avg_loss=(0+1+0)/3
    s = series([10.0, 11.0, 13.0, 12.0, 12.5, 10.5])
    result = rsi_cutler(s, 3)
    rs = (2.5 / 3) / (1 / 3)
    assert result.iloc[4] == approx(100 - 100 / (1 + rs))


def test_wilder_and_cutler_agree_on_seed_bar() -> None:
    s = series([10.0, 11.0, 13.0, 12.0, 12.5, 10.5])
    w = rsi_wilder(s, 3)
    c = rsi_cutler(s, 3)
    assert w.iloc[3] == approx(c.iloc[3])  # identical until recursion starts
