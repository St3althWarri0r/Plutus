"""Backtest runs persist to DB so results are comparable over time (§7)."""

import pandas as pd
from pytest import approx
from sqlalchemy import create_engine

from plutus.backtest.costs import CostModel
from plutus.backtest.engine import run_backtest
from plutus.backtest.metrics import compute_report
from plutus.backtest.persist import load_run, save_run
from plutus.db import Base, make_session_factory

DATES = pd.date_range("2024-01-01", periods=5, freq="D")
CLOSES = pd.DataFrame({"AAA": [100.0, 110.0, 121.0, 108.9, 119.79]}, index=DATES)
WEIGHTS = pd.DataFrame({"AAA": [1.0, 1.0, 0.0, 1.0, 1.0]}, index=DATES)


def test_save_and_load_run_round_trip() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    result = run_backtest(
        closes=CLOSES, weights=WEIGHTS, fill="same_close", cost_model=CostModel(slippage_bps=0.0)
    )
    report = compute_report(result)
    config = {"universe": ["AAA"], "fill": "same_close", "slippage_bps": 0.0}

    run_id = save_run(
        factory, strategy="sma_cross_test", config=config, report=report, equity=result.equity
    )
    loaded = load_run(factory, run_id)

    assert loaded.strategy == "sma_cross_test"
    assert loaded.config["fill"] == "same_close"
    assert loaded.metrics["trade_count"] == 2
    assert loaded.equity_curve[0] == ["2024-01-01", 1.0]
    assert loaded.equity_curve[-1][1] == approx(report.final_equity)
