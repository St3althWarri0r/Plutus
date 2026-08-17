"""Backtest engine semantics, pinned by hand-computed micro-cases.

The fill convention is the load-bearing parameter (§7: the same strategy
swung ~1.77→1.13 Sharpe between conventions in prior analysis), so every
mode's equity path is asserted against explicit arithmetic, not against
other library output.

Timeline model, single symbol, daily bars:
- weights[t] is the TARGET weight decided at close t.
- same_close:  filled at close t   → close-to-close return r[t+1] earns w[t].
- next_close:  filled at close t+1 → r[t+2] earns w[t].
- next_open:   filled at open t+1  → overnight leg of day t+1 earns w[t-1],
               intraday leg (open→close) earns w[t].
- Costs: turnover Σ|Δw| charged on the fill day at the fill price.
"""

import pandas as pd
import pytest
from pytest import approx

from plutus.backtest.costs import CostModel
from plutus.backtest.engine import run_backtest

DATES = pd.date_range("2024-01-01", periods=5, freq="D")
CLOSES = pd.DataFrame({"AAA": [100.0, 110.0, 121.0, 108.9, 119.79]}, index=DATES)
OPENS = pd.DataFrame({"AAA": [100.0, 105.0, 115.0, 115.0, 110.0]}, index=DATES)
# in AAA fully, exit signal at t2's close, re-enter at t3's close
WEIGHTS = pd.DataFrame({"AAA": [1.0, 1.0, 0.0, 1.0, 1.0]}, index=DATES)

ZERO_COST = CostModel(slippage_bps=0.0)


def test_same_close_equity_path() -> None:
    result = run_backtest(closes=CLOSES, weights=WEIGHTS, fill="same_close", cost_model=ZERO_COST)
    # held during: r1 (+10%), r2 (+10%), flat r3 (-10%), r4 (+10%)
    expected = [1.0, 1.1, 1.1 * 1.1, 1.1 * 1.1, 1.1 * 1.1 * 1.1]
    assert list(result.equity) == approx(expected)


def test_next_close_equity_path() -> None:
    result = run_backtest(closes=CLOSES, weights=WEIGHTS, fill="next_close", cost_model=ZERO_COST)
    # weight decided at t fills at close t+1, so it earns r[t+2]:
    # r2 earns w0=1 (+10%), r3 earns w1=1 (-10%), r4 earns w2=0
    expected = [1.0, 1.0, 1.1, 1.1 * 0.9, 1.1 * 0.9]
    assert list(result.equity) == approx(expected)


def test_next_open_equity_path() -> None:
    result = run_backtest(
        closes=CLOSES, weights=WEIGHTS, fill="next_open", cost_model=ZERO_COST, opens=OPENS
    )
    # day t factor = (1 + w[t-2]·ovn[t]) · (1 + w[t-1]·intra[t])
    f1 = 1.0 * (110 / 105)                      # ovn w=0, intra w0=1
    f2 = (115 / 110) * (121 / 115)              # both legs held
    f3 = (115 / 121) * 1.0                      # ovn w1=1, intra w2=0
    f4 = 1.0 * (119.79 / 110)                   # ovn w2=0, intra w3=1
    expected = [1.0, f1, f1 * f2, f1 * f2 * f3, f1 * f2 * f3 * f4]
    assert list(result.equity) == approx(expected)


def test_next_close_equals_same_close_with_shifted_weights() -> None:
    shifted = WEIGHTS.shift(1).fillna(0.0)
    a = run_backtest(closes=CLOSES, weights=shifted, fill="same_close", cost_model=ZERO_COST)
    b = run_backtest(closes=CLOSES, weights=WEIGHTS, fill="next_close", cost_model=ZERO_COST)
    assert list(a.equity) == approx(list(b.equity))


def test_costs_charged_on_fill_day_against_turnover() -> None:
    # 10 bps slippage, zero commission → drag = turnover × 0.001, price-free
    cm = CostModel(slippage_bps=10.0)
    result = run_backtest(closes=CLOSES, weights=WEIGHTS, fill="same_close", cost_model=cm)
    c = 1 - 0.001
    # fills: t0 enter (|Δw|=1), t2 exit (1), t3 re-enter (1)
    expected = [c, c * 1.1, c * 1.1 * 1.1 * c, c * 1.1 * 1.1 * c * c, c * 1.1 * 1.1 * c * c * 1.1]
    assert list(result.equity) == approx(expected)


def test_commission_costs_use_fill_price() -> None:
    # commission only: cf = commission/price → cheaper at higher fill price
    cm = CostModel(slippage_bps=0.0, commission_per_share=0.01)
    result = run_backtest(closes=CLOSES, weights=WEIGHTS, fill="same_close", cost_model=cm)
    # entry at close 100 → cf 1e-4; exit at close 121 → 0.01/121; re-enter at 108.9
    e0 = 1 - 0.01 / 100
    e2 = 1 - 0.01 / 121
    e3 = 1 - 0.01 / 108.9
    expected_final = e0 * 1.1 * 1.1 * e2 * e3 * 1.1
    assert result.equity.iloc[-1] == approx(expected_final)


def test_turnover_and_effective_weights_exposed() -> None:
    result = run_backtest(closes=CLOSES, weights=WEIGHTS, fill="same_close", cost_model=ZERO_COST)
    assert list(result.turnover) == approx([1.0, 0.0, 1.0, 1.0, 0.0])
    assert list(result.effective_weights["AAA"]) == approx([0.0, 1.0, 1.0, 0.0, 1.0])


def test_next_open_requires_opens() -> None:
    with pytest.raises(ValueError, match="opens"):
        run_backtest(closes=CLOSES, weights=WEIGHTS, fill="next_open", cost_model=ZERO_COST)


def test_fill_mode_is_required_and_validated() -> None:
    with pytest.raises(ValueError, match="fill"):
        run_backtest(
            closes=CLOSES,
            weights=WEIGHTS,
            fill="whenever",  # type: ignore[arg-type]
            cost_model=ZERO_COST,
        )


def test_multi_symbol_weights() -> None:
    closes = pd.DataFrame(
        {"AAA": [100.0, 110.0, 121.0], "BBB": [50.0, 45.0, 54.0]}, index=DATES[:3]
    )
    weights = pd.DataFrame({"AAA": [0.5, 0.5, 0.5], "BBB": [0.5, 0.5, 0.5]}, index=DATES[:3])
    result = run_backtest(closes=closes, weights=weights, fill="same_close", cost_model=ZERO_COST)
    # r1 = 0.5·10% + 0.5·(−10%) = 0 ; r2 = 0.5·10% + 0.5·20% = 15%
    assert list(result.equity) == approx([1.0, 1.0, 1.15])
