"""The Alembic pipeline must bring a fresh SQLite DB to head."""

import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_upgrade_head_on_fresh_db(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    db_url = f"sqlite:///{db_path}"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={db_url}", "upgrade", "head"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert "snapshots" in tables
    assert "orders" in tables
    assert "bars" in tables
    assert "bar_coverage" in tables
    assert "backtest_runs" in tables
    assert "strategy_state" in tables
    assert "bot_positions" in tables
    assert "fills" in tables
    assert "manual_baseline" in tables
    assert "engine_sessions" in tables
    assert "alembic_version" in tables

    inspector = inspect(create_engine(db_url))
    fill_uniques = {
        tuple(uc["column_names"]) for uc in inspector.get_unique_constraints("fills")
    }
    assert ("broker_fill_key",) in fill_uniques  # idempotent re-ingestion
    order_uniques = {
        tuple(uc["column_names"]) for uc in inspector.get_unique_constraints("orders")
    }
    assert ("idempotency_key",) in order_uniques
    bar_uniques = {
        tuple(uc["column_names"]) for uc in inspector.get_unique_constraints("bars")
    }
    assert ("symbol", "interval", "ts") in bar_uniques
