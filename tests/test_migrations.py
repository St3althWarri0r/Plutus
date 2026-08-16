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
    assert "alembic_version" in tables
