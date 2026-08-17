"""Persist backtest runs so results are comparable over time (§7)."""

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session, sessionmaker

from plutus.backtest.metrics import BacktestReport
from plutus.models import BacktestRun


@dataclass(frozen=True)
class LoadedRun:
    id: int
    strategy: str
    config: dict[str, Any]
    metrics: dict[str, Any]
    equity_curve: list[list[Any]]  # [iso_date, equity]


def save_run(
    session_factory: sessionmaker[Session],
    *,
    strategy: str,
    config: dict[str, Any],
    report: BacktestReport,
    equity: pd.Series,
) -> int:
    index = pd.DatetimeIndex(equity.index)
    values = equity.to_numpy(dtype=float)
    curve: list[list[Any]] = [
        [str(ts.date()), float(v)] for ts, v in zip(index, values, strict=True)
    ]
    with session_factory() as session:
        row = BacktestRun(
            strategy=strategy,
            config_json=json.dumps(config),
            metrics_json=json.dumps(report.as_dict()),
            equity_curve_json=json.dumps(curve),
        )
        session.add(row)
        session.commit()
        return row.id


def load_run(session_factory: sessionmaker[Session], run_id: int) -> LoadedRun:
    with session_factory() as session:
        row = session.get(BacktestRun, run_id)
        if row is None:
            raise KeyError(f"no backtest run with id {run_id}")
        return LoadedRun(
            id=row.id,
            strategy=row.strategy,
            config=json.loads(row.config_json),
            metrics=json.loads(row.metrics_json),
            equity_curve=json.loads(row.equity_curve_json),
        )
