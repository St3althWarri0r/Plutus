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
    qty: Mapped[float] = mapped_column(Numeric(24, 10))
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
    qty: Mapped[float] = mapped_column(Numeric(24, 10))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FillRecord(Base):
    """P&L record of confirmed fills (additive to status-based booking).

    broker_fill_key dedupes re-ingestion: broker_order_id + filled_at is
    unique per fill event on Alpaca's closed-order feed.
    """

    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("broker_fill_key", name="uq_fill_broker_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_fill_key: Mapped[str] = mapped_column(String(128))
    broker_order_id: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Numeric(24, 10))
    price: Mapped[float] = mapped_column(Numeric(18, 4))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ManualBaseline(Base):
    """Broker holdings not owned by the bot book, marked at engine start and
    pre-reconcile — the human's positions, subtracted before mismatch checks."""

    __tablename__ = "manual_baseline"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True)
    qty: Mapped[float] = mapped_column(Numeric(24, 10))
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EngineSession(Base):
    """Session ledger for the §12/§13 acceptance clock (≥30 clean sessions)."""

    __tablename__ = "engine_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    clean: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class AiAudit(Base):
    """Verbatim record of every AI call and response (§9 shared constraint).

    One row per ATTEMPT — timeouts and malformed responses audit with a null
    response/decision. cost_usd is summable for the (6b) per-day display.
    """

    __tablename__ = "ai_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))  # brief | review | journal
    model: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    prompt_text: Mapped[str] = mapped_column(Text)
    response_text: Mapped[str | None] = mapped_column(Text, default=None)
    input_tokens: Mapped[int | None] = mapped_column(default=None)
    output_tokens: Mapped[int | None] = mapped_column(default=None)
    latency_ms: Mapped[int | None] = mapped_column(default=None)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6), default=None)
    decision_json: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DayPlan(Base):
    """Mode B's morning plan (§9B.1) — persisted, anchoring the session."""

    __tablename__ = "day_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, unique=True)
    plan_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModeBState(Base):
    """Session tape + discipline counters (§9B.3/9B.4) — survives restarts."""

    __tablename__ = "mode_b_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, unique=True)
    tape_text: Mapped[str] = mapped_column(Text, default="")
    counters_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModeBTrade(Base):
    """One Mode B round trip (§9B.7 stats source — not derived from fills)."""

    __tablename__ = "mode_b_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_date: Mapped[date] = mapped_column(Date)
    symbol: Mapped[str] = mapped_column(String(16))
    setup: Mapped[str] = mapped_column(String(48))
    off_plan: Mapped[bool] = mapped_column(default=False)
    qty: Mapped[float] = mapped_column(Numeric(24, 10))
    entry_price: Mapped[float] = mapped_column(Numeric(18, 4))
    stop_price: Mapped[float] = mapped_column(Numeric(18, 4))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 4), default=None)
    realized_r: Mapped[float | None] = mapped_column(Numeric(10, 4), default=None)


class PlaidItem(Base):
    """One connected Plaid institution (M1, Vanguard). The access token is
    runtime state living only in the gitignored DB — never in code or env."""

    __tablename__ = "plaid_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution: Mapped[str] = mapped_column(String(32), unique=True)  # m1 | vanguard
    item_id: Mapped[str] = mapped_column(String(64))
    access_token: Mapped[str] = mapped_column(String(128))
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[str] = mapped_column(Text)  # universe, dates, fill, costs, params
    metrics_json: Mapped[str] = mapped_column(Text)
    equity_curve_json: Mapped[str] = mapped_column(Text)  # [[iso_date, equity], ...]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
