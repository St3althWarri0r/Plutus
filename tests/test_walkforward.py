"""Walk-forward (§7): fit on rolling in-sample windows, validate out-of-sample,
report OOS-only metrics separately.

Windows step by the OOS size; partial trailing OOS windows are dropped.
Weights at t depend only on data ≤ t, so per-param weights are computed once
over the full series and IS/OOS metrics are slices of that single backtest.
"""

import numpy as np
import pandas as pd
from pytest import approx

from plutus.backtest.costs import CostModel
from plutus.backtest.engine import run_backtest
from plutus.backtest.walkforward import WalkForwardResult, walk_forward

ZERO = CostModel(slippage_bps=0.0)


class ConstantWeight:
    """Test double: hold weight w in the single symbol, always."""

    def __init__(self, w: float) -> None:
        self.w = w

    def weights_history(self, closes: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(self.w, index=closes.index, columns=closes.columns)


def rising_closes(n: int = 100) -> pd.DataFrame:
    idx = pd.date_range("2022-01-01", periods=n, freq="B")
    return pd.DataFrame({"AAA": 100 * 1.01 ** np.arange(n)}, index=idx)


def run_wf() -> "WalkForwardResult":
    return walk_forward(
        closes=rising_closes(),
        strategy_factory=lambda w: ConstantWeight(w),
        param_grid=[{"w": 0.0}, {"w": 1.0}],
        is_bars=30,
        oos_bars=20,
        fill="next_close",
        cost_model=ZERO,
        metric="sharpe",
    )


def test_window_boundaries_step_by_oos_and_drop_partial() -> None:
    result = run_wf()
    spans = [(w.is_start, w.is_end, w.oos_start, w.oos_end) for w in result.windows]
    assert spans == [(0, 30, 30, 50), (20, 50, 50, 70), (40, 70, 70, 90)]


def test_picks_param_with_best_in_sample_metric() -> None:
    result = run_wf()
    # fully invested beats flat on a steady riser in every window
    assert [w.chosen_params for w in result.windows] == [{"w": 1.0}] * 3


def test_oos_returns_are_stitched_slices_of_chosen_param_backtest() -> None:
    result = run_wf()
    closes = rising_closes()
    full = run_backtest(
        closes=closes,
        weights=ConstantWeight(1.0).weights_history(closes),
        fill="next_close",
        cost_model=ZERO,
    )
    expected = pd.concat([full.returns.iloc[30:50], full.returns.iloc[50:70],
                          full.returns.iloc[70:90]])
    assert len(result.oos_returns) == 60
    assert list(result.oos_returns) == approx(list(expected))


def test_oos_report_matches_stitched_returns() -> None:
    result = run_wf()
    growth = float(np.prod(1.0 + result.oos_returns.to_numpy()))
    assert result.oos_metrics["total_return"] == approx(growth - 1.0)
    assert result.oos_metrics["n_periods"] == 60


def test_too_short_series_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="window"):
        walk_forward(
            closes=rising_closes(40),
            strategy_factory=lambda w: ConstantWeight(w),
            param_grid=[{"w": 1.0}],
            is_bars=30,
            oos_bars=20,
            fill="next_close",
            cost_model=ZERO,
        )
