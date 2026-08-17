"""Walk-forward validation (§7): rolling in-sample fit, out-of-sample stitch.

Windows step by the OOS size; a trailing OOS window shorter than oos_bars is
dropped. Because strategy weights at t depend only on data ≤ t, each param
combo is backtested once over the full series and IS/OOS metrics are computed
on slices of that single run — no per-window recomputation, no lookahead.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

from plutus.backtest.costs import CostModel
from plutus.backtest.engine import FillMode, run_backtest
from plutus.backtest.metrics import PERIODS_PER_YEAR

Metric = Literal["sharpe", "total_return"]


class WeightStrategy(Protocol):
    def weights_history(self, closes: pd.DataFrame) -> pd.DataFrame: ...


@dataclass(frozen=True)
class Window:
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int
    chosen_params: dict[str, Any]
    is_metric: float


@dataclass(frozen=True)
class WalkForwardResult:
    windows: list[Window]
    oos_returns: pd.Series
    oos_metrics: dict[str, float]


def _score(returns: np.ndarray, metric: Metric) -> float:
    if metric == "total_return":
        return float(np.prod(1.0 + returns) - 1.0)
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    return float(returns.mean()) / std * math.sqrt(PERIODS_PER_YEAR) if std > 0 else 0.0


def _summarize(returns: np.ndarray) -> dict[str, float]:
    equity = np.cumprod(1.0 + returns)
    drawdown = equity / np.maximum.accumulate(equity) - 1.0
    return {
        "n_periods": len(returns),
        "total_return": float(equity[-1] - 1.0),
        "cagr": float(equity[-1] ** (PERIODS_PER_YEAR / len(returns)) - 1.0),
        "sharpe": _score(returns, "sharpe"),
        "max_drawdown": float(drawdown.min()),
    }


def walk_forward(
    *,
    closes: pd.DataFrame,
    strategy_factory: Callable[..., WeightStrategy],
    param_grid: list[dict[str, Any]],
    is_bars: int,
    oos_bars: int,
    fill: FillMode,
    cost_model: CostModel,
    metric: Metric = "sharpe",
    opens: pd.DataFrame | None = None,
) -> WalkForwardResult:
    n = len(closes)
    if n < is_bars + oos_bars:
        raise ValueError(
            f"series has {n} rows; needs at least one full window "
            f"({is_bars} in-sample + {oos_bars} out-of-sample)"
        )

    # one full-series backtest per param combo
    full_returns: list[pd.Series] = []
    for params in param_grid:
        strategy = strategy_factory(**params)
        result = run_backtest(
            closes=closes,
            weights=strategy.weights_history(closes),
            fill=fill,
            cost_model=cost_model,
            opens=opens,
        )
        full_returns.append(result.returns)

    windows: list[Window] = []
    oos_parts: list[pd.Series] = []
    start = 0
    while start + is_bars + oos_bars <= n:
        is_lo, is_hi = start, start + is_bars
        oos_lo, oos_hi = is_hi, is_hi + oos_bars

        scores = [
            _score(r.iloc[is_lo:is_hi].to_numpy(dtype=float), metric) for r in full_returns
        ]
        best = int(np.argmax(scores))
        windows.append(
            Window(
                is_start=is_lo,
                is_end=is_hi,
                oos_start=oos_lo,
                oos_end=oos_hi,
                chosen_params=dict(param_grid[best]),
                is_metric=scores[best],
            )
        )
        oos_parts.append(full_returns[best].iloc[oos_lo:oos_hi])
        start += oos_bars

    oos_returns = pd.concat(oos_parts)
    return WalkForwardResult(
        windows=windows,
        oos_returns=oos_returns,
        oos_metrics=_summarize(oos_returns.to_numpy(dtype=float)),
    )
