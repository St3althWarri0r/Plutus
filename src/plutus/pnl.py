"""Fill ingestion and per-strategy equity (§11 crash-safe accounting).

Fills are the P&L record (additive to the status-based position booking in
RiskManager.sync_fills — a divergence between the two halts via the
partial-fill rule, so this module can stay simple). Ingestion is watermarked
on the newest stored filled_at with an overlap window, and idempotent via the
broker_fill_key unique constraint.

equity_now = day-start mark + net cash flow from fills since the mark
+ mark-to-market of the strategy's open bot positions. This avoids cost-basis
accounting entirely: at the 09:30 mark the two terms are zero, and every
subsequent trade/mark moves them consistently.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import BrokerAdapter
from plutus.logging_setup import get_logger
from plutus.models import BotPosition, FillRecord, Order
from plutus.risk import Alert

log = get_logger("plutus.pnl")

_WATERMARK_OVERLAP = timedelta(minutes=10)
_DEFAULT_LOOKBACK = timedelta(days=3)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def ingest_fills(
    adapter: BrokerAdapter,
    session_factory: sessionmaker[Session],
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Pull fills since the watermark into the fills table. Returns new-row count."""
    now = (clock or (lambda: datetime.now(UTC)))()
    with session_factory() as session:
        newest = session.scalar(select(func.max(FillRecord.filled_at)))
    since = (
        _aware(newest) - _WATERMARK_OVERLAP
        if newest is not None
        else now - _DEFAULT_LOOKBACK
    )

    new_rows = 0
    for fill in adapter.get_fills(since):
        key = f"{fill.broker_order_id}:{_aware(fill.filled_at).isoformat()}"
        with session_factory() as session:
            order = session.scalars(
                select(Order).where(Order.broker_order_id == fill.broker_order_id)
            ).one_or_none()
            strategy = order.strategy if order is not None else "manual"
            session.add(
                FillRecord(
                    broker_fill_key=key,
                    broker_order_id=fill.broker_order_id,
                    strategy=strategy,
                    symbol=fill.symbol,
                    side=fill.side,
                    qty=fill.qty,
                    price=fill.price,
                    filled_at=_aware(fill.filled_at),
                )
            )
            try:
                session.commit()
                new_rows += 1
            except IntegrityError:
                session.rollback()  # already ingested (overlap window)
    if new_rows:
        log.info("fills_ingested", count=new_rows)
    return new_rows


def equity_now(
    strategy: str,
    *,
    session_factory: sessionmaker[Session],
    price_lookup: Callable[[str], float | None],
    day_start: float,
    since: datetime,
) -> float | None:
    """None when an open position can't be priced — callers must not guess."""
    with session_factory() as session:
        fills = session.scalars(
            select(FillRecord).where(
                FillRecord.strategy == strategy, FillRecord.filled_at >= since
            )
        ).all()
        cash_flow = sum(
            (float(f.qty) * float(f.price)) * (-1.0 if f.side == "buy" else 1.0)
            for f in fills
        )
        positions = session.scalars(
            select(BotPosition).where(BotPosition.strategy == strategy, BotPosition.qty != 0)
        ).all()
        mtm = 0.0
        for p in positions:
            price = price_lookup(p.symbol)
            if price is None:
                log.warning("equity_unpriceable", strategy=strategy, symbol=p.symbol)
                return None
            mtm += float(p.qty) * price
    return day_start + cash_flow + mtm


class DailyLossMonitor:
    """§11: warn at −1.5% intraday, on crossing only (the −3% halt is §8's)."""

    def __init__(self, warn_pct: float, alert: Alert) -> None:
        self._warn_pct = warn_pct
        self._alert = alert
        self._breached: set[str] = set()

    def observe(self, strategy: str, *, day_start: float, equity: float) -> None:
        if day_start <= 0:
            return
        loss = (day_start - equity) / day_start
        if loss >= self._warn_pct and strategy not in self._breached:
            self._breached.add(strategy)
            self._alert(
                "warning",
                f"{strategy} down {loss:.2%} intraday (warn threshold "
                f"{self._warn_pct:.1%})",
            )
        elif loss < self._warn_pct and strategy in self._breached:
            self._breached.discard(strategy)
