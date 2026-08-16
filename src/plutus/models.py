"""Core ORM models.

Phase 0 seeds the migration pipeline with the snapshots table (one row per
account per day — powers the net-worth-over-time chart). Later phases add
their own tables via new Alembic revisions.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from plutus.db import Base


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("account", "snapshot_date", name="uq_snapshot_account_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(32))  # e.g. m1, vanguard, schwab, alpaca
    snapshot_date: Mapped[date] = mapped_column(Date)
    equity: Mapped[float] = mapped_column(Numeric(18, 4))
    cash: Mapped[float] = mapped_column(Numeric(18, 4))
    positions_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
