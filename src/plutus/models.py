"""Core ORM models.

Phase 0 seeded the snapshots table (one row per account per day — powers the
net-worth-over-time chart). Phase 1 adds orders: one row per OrderIntent that
reaches the RiskManager, keyed by the client-generated idempotency key so a
retry after a timeout can never double-submit. Phase 2 adds the bar cache
(bars + bar_coverage, so backtests never re-fetch) and backtest_runs (persisted
reports, comparable over time).
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


class Bar(Base):
    __tablename__ = "bars"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", name="uq_bar_symbol_interval_ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))
    interval: Mapped[str] = mapped_column(String(8))  # '1d', '1m'
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # UTC, normalized by cache
    open: Mapped[float] = mapped_column(Numeric(18, 6))
    high: Mapped[float] = mapped_column(Numeric(18, 6))
    low: Mapped[float] = mapped_column(Numeric(18, 6))
    close: Mapped[float] = mapped_column(Numeric(18, 6))
    volume: Mapped[float] = mapped_column(Numeric(20, 2))
    # a later split invalidates cached history for that symbol (flush + re-fetch)
    adjustment: Mapped[str] = mapped_column(String(8), default="all")


class BarCoverage(Base):
    """Contiguous [start, end] ranges already fetched per (symbol, interval)."""

    __tablename__ = "bar_coverage"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))
    interval: Mapped[str] = mapped_column(String(8))
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyState(Base):
    """Per-strategy enable/halt state and the daily-loss reference mark.

    allocation_usd is the strategy's risk budget in dollars — NOT a fraction
    of account equity, which is dominated by non-bot holdings. day_start_
    equity_usd is stamped by the 09:30 scheduler job and survives restarts.
    """

    __tablename__ = "strategy_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    halt_reason: Mapped[str | None] = mapped_column(Text, default=None)
    allocation_usd: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    day_start_equity_usd: Mapped[float | None] = mapped_column(Numeric(18, 2), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BotPosition(Base):
    """Positions the bot believes it holds (source of intent; broker is truth).

    Updated on confirmed fills only — never on order acceptance — and
    corrected by reconciliation.
    """

    __tablename__ = "bot_positions"
    __table_args__ = (
        UniqueConstraint("strategy", "symbol", name="uq_bot_position_strategy_symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(16))
    qty: Mapped[float] = mapped_column(Numeric(18, 6))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[str] = mapped_column(Text)  # universe, dates, fill, costs, params
    metrics_json: Mapped[str] = mapped_column(Text)
    equity_curve_json: Mapped[str] = mapped_column(Text)  # [[iso_date, equity], ...]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
