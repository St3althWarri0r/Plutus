"""Vectorized daily weights→returns backtest engine (§7).

The fill convention is an explicit, required parameter — the same strategy
can swing dramatically between conventions, so callers must always name one:

- ``same_close``: signal at close t, filled at close t. Weight w[t] earns the
  close-to-close return r[t+1].
- ``next_close``: signal at close t, filled at close t+1. w[t] earns r[t+2].
- ``next_open``: signal at close t, filled at open t+1. The overnight leg of
  day t+1 is still earned by w[t-1]; the intraday leg (open→close) by w[t].

Costs are charged on the fill day: turnover Σ|Δw| × cost_fraction at the
fill price (close for the close fills, open for next_open).
"""

from dataclasses import dataclass
from typing import Literal, get_args

import pandas as pd

from plutus.backtest.costs import CostModel

FillMode = Literal["same_close", "next_open", "next_close"]


@dataclass(frozen=True)
class BacktestResult:
    equity: pd.Series
    returns: pd.Series
    effective_weights: pd.DataFrame
    weighted_returns: pd.DataFrame  # per-symbol contribution: eff_weight · close return
    turnover: pd.Series
    fill: FillMode


def _validate(closes: pd.DataFrame, weights: pd.DataFrame, opens: pd.DataFrame | None) -> None:
    if not closes.index.equals(weights.index) or list(closes.columns) != list(weights.columns):
        raise ValueError("weights must share closes' index and columns")
    if opens is not None and (
        not closes.index.equals(opens.index) or list(closes.columns) != list(opens.columns)
    ):
        raise ValueError("opens must share closes' index and columns")


def run_backtest(
    *,
    closes: pd.DataFrame,
    weights: pd.DataFrame,
    fill: FillMode,
    cost_model: CostModel,
    opens: pd.DataFrame | None = None,
    initial_capital: float = 1.0,
) -> BacktestResult:
    if fill not in get_args(FillMode):
        raise ValueError(f"fill must be one of {get_args(FillMode)}, got {fill!r}")
    if fill == "next_open" and opens is None:
        raise ValueError("next_open fill requires opens")
    _validate(closes, weights, opens)

    w = weights.fillna(0.0)
    r = closes.pct_change().fillna(0.0)

    # per-symbol weight change decided at t (initial entry counts, from flat)
    dw = w.diff()
    dw.iloc[0] = w.iloc[0]
    abs_dw = dw.abs()

    if fill == "same_close":
        eff = w.shift(1).fillna(0.0)
        fill_dw = abs_dw
        fill_price = closes
        growth = 1.0 + (eff * r).sum(axis=1)
    elif fill == "next_close":
        eff = w.shift(2).fillna(0.0)
        fill_dw = abs_dw.shift(1).fillna(0.0)
        fill_price = closes
        growth = 1.0 + (eff * r).sum(axis=1)
    else:  # next_open
        assert opens is not None
        overnight = (opens / closes.shift(1) - 1.0).fillna(0.0)
        intraday = (closes / opens - 1.0).fillna(0.0)
        w_overnight = w.shift(2).fillna(0.0)
        w_intraday = w.shift(1).fillna(0.0)
        eff = w_intraday
        fill_dw = abs_dw.shift(1).fillna(0.0)
        fill_price = opens
        growth = (1.0 + (w_overnight * overnight).sum(axis=1)) * (
            1.0 + (w_intraday * intraday).sum(axis=1)
        )

    bps_frac = (cost_model.slippage_bps + cost_model.half_spread_bps) / 1e4
    cost_fraction = bps_frac + cost_model.commission_per_share / fill_price
    cost_drag = (fill_dw * cost_fraction).sum(axis=1)

    factor = growth * (1.0 - cost_drag)
    equity = initial_capital * factor.cumprod()
    return BacktestResult(
        equity=equity,
        returns=factor - 1.0,
        effective_weights=eff,
        weighted_returns=eff * r,
        turnover=fill_dw.sum(axis=1),
        fill=fill,
    )
