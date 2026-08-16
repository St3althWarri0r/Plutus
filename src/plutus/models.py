"""Core ORM models.

Phase 0 seeded the snapshots table (one row per account per day — powers the
net-worth-over-time chart). Phase 1 adds orders: one row per OrderIntent that
reaches the RiskManager, keyed by the client-generated idempotency key so a
retry after a timeout can never double-submit.
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


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_order_idempotency_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(36))
    broker: Mapped[str] = mapped_column(String(16), default="alpaca")
    broker_order_id: Mapped[str | None] = mapped_column(String(64), default=None)
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Numeric(18, 6))
    order_type: Mapped[str] = mapped_column(String(8))
    limit_price: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    time_in_force: Mapped[str] = mapped_column(String(8), default="day")
    strategy: Mapped[str] = mapped_column(String(32), default="manual")
    trading_mode: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(20), default="new")
    reject_reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
