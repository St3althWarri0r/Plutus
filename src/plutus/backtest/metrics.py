"""Standard backtest report (§7).

Stated conventions — comparability with prior analyses depends on them:
- daily periods, annualization by √252 (Sharpe/Sortino) and 252 (CAGR), rf = 0
- Sortino: downside deviation over ALL periods against a 0 target; +inf when
  there is no downside and mean return is positive
- trade = contiguous nonzero effective-weight run per symbol; its P&L is the
  compounded weighted contribution of that symbol, gross of costs
"""

import math
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel

from plutus.backtest.engine import BacktestResult, FillMode

PERIODS_PER_YEAR = 252


class TradeRecord(BaseModel):
    symbol: str
    start: str
    end: str
    pnl: float


class BacktestReport(BaseModel):
    fill: FillMode
    start: str
    end: str
    n_periods: int
    total_return: float
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    exposure: float
    total_turnover: float
    trade_count: int
    worst_trades: list[dict[str, Any]]
    yearly_returns: dict[str, float]
    final_equity: float

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


def _extract_trades(result: BacktestResult) -> list[TradeRecord]:
    trades: list[TradeRecord] = []
    for symbol in result.effective_weights.columns:
        held = result.effective_weights[symbol] != 0.0
        run_ids = (held != held.shift()).cumsum()
        for _, run in result.weighted_returns[symbol].groupby(run_ids):
            if not held.loc[run.index[0]]:
                continue
            pnl = float(np.prod(1.0 + run.to_numpy(dtype=float)) - 1.0)
            run_idx = pd.DatetimeIndex(run.index)
            trades.append(
                TradeRecord(
                    symbol=str(symbol),
                    start=str(run_idx[0].date()),
                    end=str(run_idx[-1].date()),
                    pnl=pnl,
                )
            )
    return trades


def compute_report(result: BacktestResult) -> BacktestReport:
    rets = result.returns.to_numpy(dtype=float)
    idx = pd.DatetimeIndex(result.returns.index)
    n = len(rets)
    final_equity = float(result.equity.iloc[-1])
    growth = float(np.prod(1.0 + rets))
    total_return = growth - 1.0

    cagr = growth ** (PERIODS_PER_YEAR / n) - 1.0 if growth > 0 else -1.0

    mean = float(rets.mean())
    std = float(rets.std(ddof=1))
    sharpe = mean / std * math.sqrt(PERIODS_PER_YEAR) if std > 0 else 0.0

    downside = np.clip(rets, None, 0.0)
    downside_dev = float(np.sqrt((downside**2).mean()))
    if downside_dev > 0:
        sortino = mean / downside_dev * math.sqrt(PERIODS_PER_YEAR)
    else:
        sortino = math.inf if mean > 0 else 0.0

    eq = result.equity.to_numpy(dtype=float)
    max_drawdown = float((eq / np.maximum.accumulate(eq) - 1.0).min())

    exposure = float((result.effective_weights.abs().sum(axis=1) > 0).mean())

    trades = _extract_trades(result)
    worst = sorted(trades, key=lambda t: t.pnl)[:5]

    years = idx.year
    yearly = {
        str(year): float(np.prod(1.0 + rets[years == year]) - 1.0)
        for year in sorted(set(years))
    }

    return BacktestReport(
        fill=result.fill,
        start=str(idx[0].date()),
        end=str(idx[-1].date()),
        n_periods=n,
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
        exposure=exposure,
        total_turnover=float(result.turnover.sum()),
        trade_count=len(trades),
        worst_trades=[t.model_dump() for t in worst],
        yearly_returns=yearly,
        final_equity=final_equity,
    )
