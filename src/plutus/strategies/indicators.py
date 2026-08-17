"""SMA and RSI — the §6 tree's entire signal vocabulary.

RSI default is Wilder's (TA-Lib/TradingView standard): the average gain/loss
seeds with the SMA of the first `period` changes and then recurses
avg = (prev·(period−1) + current)/period. Cutler's variant (plain rolling SMA
of gains/losses) is kept to quantify divergence, since the source strategy's
platform definition can't be verified.
"""

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def _gains_losses(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    change = series.diff().to_numpy(dtype=float)
    gains = np.where(change > 0, change, 0.0)
    losses = np.where(change < 0, -change, 0.0)
    return gains, losses


def _rsi_from_averages(avg_gain: np.ndarray, avg_loss: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)
    # zero average loss → RSI 100 (unless zero gain too → neutral 50)
    rsi = np.where(avg_loss == 0, np.where(avg_gain == 0, 50.0, 100.0), rsi)
    return rsi


def rsi_wilder(series: pd.Series, period: int) -> pd.Series:
    gains, losses = _gains_losses(series)
    n = len(series)
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    if n > period:
        # seed: SMA of the first `period` changes (positions 1..period)
        avg_gain[period] = gains[1 : period + 1].mean()
        avg_loss[period] = losses[1 : period + 1].mean()
        for i in range(period + 1, n):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period
    out = _rsi_from_averages(avg_gain, avg_loss)
    out[:period] = np.nan
    return pd.Series(out, index=series.index)


def rsi_cutler(series: pd.Series, period: int) -> pd.Series:
    gains, losses = _gains_losses(series)
    avg_gain = pd.Series(gains, index=series.index).rolling(period).mean().to_numpy()
    avg_loss = pd.Series(losses, index=series.index).rolling(period).mean().to_numpy()
    out = _rsi_from_averages(avg_gain, avg_loss)
    out[:period] = np.nan
    return pd.Series(out, index=series.index)
