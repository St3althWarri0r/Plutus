"""Standard report metrics (§7).

Conventions stated (comparability with prior analysis depends on them):
daily periods, annualization √252 / 252, rf = 0, Sortino uses downside
deviation over all periods with target 0, trade = contiguous nonzero-weight
run per symbol with gross-of-cost P&L from that symbol's contribution.
"""

import math

import pandas as pd
from pytest import approx

from plutus.backtest.costs import CostModel
from plutus.backtest.engine import run_backtest
from plutus.backtest.metrics import BacktestReport, compute_report

DATES = pd.date_range("2024-01-01", periods=5, freq="D")
CLOSES = pd.DataFrame({"AAA": [100.0, 110.0, 121.0, 108.9, 119.79]}, index=DATES)
WEIGHTS = pd.DataFrame({"AAA": [1.0, 1.0, 0.0, 1.0, 1.0]}, index=DATES)
ZERO_COST = CostModel(slippage_bps=0.0)


def _same_close_report() -> BacktestReport:
    result = run_backtest(closes=CLOSES, weights=WEIGHTS, fill="same_close", cost_model=ZERO_COST)
    return compute_report(result)


def test_total_return_and_cagr() -> None:
    report = _same_close_report()
    assert report.total_return == approx(1.331 - 1)
    # 5 daily periods → annualized over 5/252 years
    assert report.cagr == approx(1.331 ** (252 / 5) - 1)


def test_sharpe_daily_sqrt252_rf0() -> None:
    report = _same_close_report()
    rets = [0.0, 0.1, 0.1, 0.0, 0.1]
    mean = sum(rets) / 5
    var = sum((x - mean) ** 2 for x in rets) / 4  # ddof=1
    assert report.sharpe == approx(mean / math.sqrt(var) * math.sqrt(252))


def test_max_drawdown_and_sortino() -> None:
    result = run_backtest(closes=CLOSES, weights=WEIGHTS, fill="next_close", cost_model=ZERO_COST)
    report = compute_report(result)
    # equity [1, 1, 1.1, 0.99, 0.99]: trough 0.99 from peak 1.1
    assert report.max_drawdown == approx(0.99 / 1.1 - 1)
    # returns [0, 0, .1, -.1, 0]: mean 0 → sortino 0
    assert report.sortino == approx(0.0)


def test_sortino_inf_when_no_downside() -> None:
    report = _same_close_report()
    assert math.isinf(report.sortino)


def test_exposure_turnover_trades() -> None:
    report = _same_close_report()
    assert report.exposure == approx(3 / 5)  # eff weights [0,1,1,0,1]
    assert report.total_turnover == approx(3.0)  # enter, exit, re-enter
    assert report.trade_count == 2
    # run t1-t2: 1.1·1.1−1 = 0.21 ; run t4: 0.1
    assert report.worst_trades[0]["pnl"] == approx(0.1)
    assert report.worst_trades[1]["pnl"] == approx(0.21)
    assert report.worst_trades[0]["symbol"] == "AAA"


def test_yearly_returns_table() -> None:
    dates = pd.to_datetime(["2023-12-28", "2023-12-29", "2024-01-02", "2024-01-03"])
    closes = pd.DataFrame({"AAA": [100.0, 110.0, 121.0, 133.1]}, index=dates)
    weights = pd.DataFrame({"AAA": [1.0, 1.0, 1.0, 1.0]}, index=dates)
    result = run_backtest(closes=closes, weights=weights, fill="same_close", cost_model=ZERO_COST)
    report = compute_report(result)
    # 2023: one held return of +10% ; 2024: 1.1·1.1 within the year
    assert report.yearly_returns == {
        "2023": approx(0.10),
        "2024": approx(1.331 / 1.1 - 1),
    }


def test_report_serializes_to_json_dict() -> None:
    report = _same_close_report()
    payload = report.as_dict()
    assert payload["trade_count"] == 2
    assert isinstance(payload["yearly_returns"], dict)
    assert payload["fill"] == "same_close"
