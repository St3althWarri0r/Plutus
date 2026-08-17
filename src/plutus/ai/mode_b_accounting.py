"""Mode B accounting: fills → realized R, session P&L in R, §9B.7 stats,
and the §9B.6 p95 decision-latency check. All deterministic, all from DB."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plutus.ai.discipline import Discipline, SessionCounters
from plutus.logging_setup import get_logger
from plutus.models import AiAudit, BotPosition, FillRecord, ModeBTrade

log = get_logger("plutus.ai.mode_b_accounting")

ET = ZoneInfo("America/New_York")

STRATEGY = "mode_b"


def sync_mode_b_trades(
    session_factory: sessionmaker[Session],
    *,
    discipline: Discipline,
    counters: SessionCounters,
    now: datetime,
) -> list[float]:
    """Close trade rows whose position went flat; realized P&L is the net
    cash flow of the symbol's mode_b fills since the trade opened (exact when
    flat). Returns the realized_r values closed this pass."""
    closed: list[float] = []
    r_dollars = discipline.r_dollars()
    with session_factory() as session:
        open_trades = session.scalars(
            select(ModeBTrade).where(ModeBTrade.closed_at.is_(None))
        ).all()
        for trade in open_trades:
            position = session.scalars(
                select(BotPosition).where(
                    BotPosition.strategy == STRATEGY, BotPosition.symbol == trade.symbol
                )
            ).one_or_none()
            if position is not None and abs(float(position.qty)) > 1e-9:
                continue  # still open
            fills = session.scalars(
                select(FillRecord).where(
                    FillRecord.strategy == STRATEGY,
                    FillRecord.symbol == trade.symbol,
                    FillRecord.filled_at >= trade.opened_at,
                )
            ).all()
            if not fills:
                continue  # nothing filled yet (pending entry) — leave open
            cash = sum(
                float(f.qty) * float(f.price) * (1.0 if f.side == "sell" else -1.0)
                for f in fills
            )
            realized_r = cash / r_dollars if r_dollars > 0 else 0.0
            sells = [f for f in fills if f.side == "sell"]
            trade.closed_at = now
            trade.exit_price = float(sells[-1].price) if sells else None
            trade.realized_r = realized_r
            session.commit()
            discipline.on_trade_closed(counters, realized_r=realized_r, now=now)
            closed.append(realized_r)
            log.info(
                "mode_b_trade_closed",
                symbol=trade.symbol,
                setup=trade.setup,
                realized_r=round(realized_r, 3),
            )
    return closed


def session_pnl_r(
    session_factory: sessionmaker[Session],
    *,
    r_dollars: float,
    current_prices: dict[str, float],
    today: date,
) -> float:
    """Realized (closed trades today) + unrealized (open trades marked to
    current price), in R (§9B.4 daily stop counts both)."""
    if r_dollars <= 0:
        return 0.0
    total = 0.0
    with session_factory() as session:
        rows = session.scalars(
            select(ModeBTrade).where(ModeBTrade.session_date == today)
        ).all()
        for trade in rows:
            if trade.realized_r is not None:
                total += float(trade.realized_r)
            elif trade.closed_at is None:
                price = current_prices.get(trade.symbol)
                if price is not None:
                    total += (
                        (price - float(trade.entry_price)) * float(trade.qty)
                    ) / r_dollars
    return total


def compute_stats(session_factory: sessionmaker[Session]) -> dict[str, object]:
    """§9B.7 promotion-bar numbers. 'daily stop never breached' is shown as
    trigger data (worst day in R) — the user judges."""
    with session_factory() as session:
        trades: Sequence[ModeBTrade] = session.scalars(
            select(ModeBTrade).where(ModeBTrade.realized_r.is_not(None))
        ).all()
        rs = [float(t.realized_r) for t in trades if t.realized_r is not None]
        sessions = {t.session_date for t in trades}
        by_day: dict[date, float] = {}
        for t in trades:
            if t.realized_r is not None:
                by_day[t.session_date] = by_day.get(t.session_date, 0.0) + float(
                    t.realized_r
                )
    wins = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    return {
        "sessions": len(sessions),
        "trades": len(rs),
        "profit_factor": (wins / losses) if losses > 0 else None,
        "expectancy_r": (sum(rs) / len(rs)) if rs else None,
        "worst_day_r": min(by_day.values()) if by_day else None,
        "best_day_r": max(by_day.values()) if by_day else None,
    }


def p95_decision_latency_ms(
    session_factory: sessionmaker[Session], *, today: date
) -> int | None:
    day_start = datetime(today.year, today.month, today.day, tzinfo=ET).astimezone(UTC)
    with session_factory() as session:
        rows = session.scalars(
            select(AiAudit.latency_ms).where(
                AiAudit.mode == "decision",
                AiAudit.latency_ms.is_not(None),
                AiAudit.created_at >= day_start,
            )
        ).all()
    values = sorted(int(v) for v in rows if v is not None)
    if not values:
        return None
    index = max(0, int(round(0.95 * len(values))) - 1)
    return values[index]
