"""Phase 2 acceptance: reproduce a known SMA-cross backtest within tolerance.

Two independent implementations must agree: our engine (semantics pinned by
hand-computed micro-cases) vs vectorbt's Portfolio.from_signals (§2's library,
serving as the oracle). vectorbt's default fills on the signal bar's close —
our same_close convention. Zero costs for the comparison; a with-cost case is
hand-verified in test_engine.py.
"""

import numpy as np
import pandas as pd
import vectorbt as vbt
from pytest import approx

from plutus.backtest.costs import CostModel
from plutus.backtest.engine import run_backtest


def make_prices(n: int = 300) -> pd.Series:
    rng = np.random.default_rng(42)  # fresh per call: test data independent of run order
    steps = rng.normal(loc=0.0004, scale=0.015, size=n)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    return pd.Series(100.0 * np.exp(np.cumsum(steps)), index=idx, name="close")


def test_sma_cross_matches_vectorbt_oracle() -> None:
    close = make_prices()
    fast = close.rolling(10).mean()
    slow = close.rolling(30).mean()
    in_market = (fast > slow) & fast.notna() & slow.notna()

    # our engine: target weight decided at each close
    weights = in_market.astype(float).to_frame("SYM")
    closes = close.to_frame("SYM")
    result = run_backtest(
        closes=closes,
        weights=weights,
        fill="same_close",
        cost_model=CostModel(slippage_bps=0.0),
    )

    # oracle: same signals as entry/exit events
    entries = in_market & ~in_market.shift(1, fill_value=False)
    exits = ~in_market & in_market.shift(1, fill_value=False)
    pf = vbt.Portfolio.from_signals(close, entries, exits, fees=0.0, init_cash=1.0)
    oracle_equity = pf.value() if callable(pf.value) else pf.value

    ours = result.equity.to_numpy()
    theirs = np.asarray(oracle_equity)
    assert ours == approx(theirs, rel=1e-6)
    # sanity: the strategy actually traded
    assert result.turnover.sum() > 4


def test_conventions_differ_on_same_signals() -> None:
    """The reason fill is a required parameter: same signals, different truth."""
    close = make_prices()
    fast = close.rolling(10).mean()
    slow = close.rolling(30).mean()
    weights = ((fast > slow) & slow.notna()).astype(float).to_frame("SYM")
    closes = close.to_frame("SYM")

    zero = CostModel(slippage_bps=0.0)
    a = run_backtest(closes=closes, weights=weights, fill="same_close", cost_model=zero)
    b = run_backtest(closes=closes, weights=weights, fill="next_close", cost_model=zero)

    assert a.equity.iloc[-1] != approx(b.equity.iloc[-1], rel=1e-3)
