"""RiskManager: the only component allowed to call BrokerAdapter.submit_order.

Phase 1 scope (deliberate): route enforcement, idempotency-key dedupe against
the orders table, and a paper-only gate — Phase 1 has no live enablement, so
any non-paper effective mode is rejected before the adapter is touched. The
full §8 gate set (sizing, loss halts, rate limits, kill switch, reconciliation)
lands in Phase 4.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import BrokerAdapter, OrderIntent
from plutus.config import Settings, TradingMode, effective_trading_mode, get_settings
from plutus.logging_setup import get_logger
from plutus.models import Order

log = get_logger("plutus.risk")


class RiskManager:
    def __init__(
        self,
        adapter: BrokerAdapter,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
        effective_mode: TradingMode | None = None,
    ) -> None:
        self._adapter = adapter
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        # resolved once per manager unless injected (tests); submit() stamps it per order
        self._effective_mode: TradingMode = effective_mode or effective_trading_mode(self._settings)

    def submit(self, intent: OrderIntent) -> Order:
        """Persist intent, run gates, hand to the broker adapter exactly once.

        Returns the (detached) Order row reflecting the outcome. A repeated
        idempotency key returns the original row without re-submitting.
        """
        with self._session_factory() as session:
            existing = session.scalars(
                select(Order).where(Order.idempotency_key == intent.idempotency_key)
            ).one_or_none()
            if existing is not None:
                log.info("order_dedupe", idempotency_key=intent.idempotency_key)
                return existing

            row = Order(
                idempotency_key=intent.idempotency_key,
                symbol=intent.symbol,
                side=intent.side,
                qty=intent.qty,
                order_type=intent.order_type,
                limit_price=intent.limit_price,
                time_in_force=intent.time_in_force,
                strategy=intent.strategy,
                trading_mode=self._effective_mode,
                status="new",
            )
            session.add(row)
            session.commit()

            if self._effective_mode != "paper":
                row.status = "rejected"
                row.reject_reason = "live trading is not enabled in Phase 1"
                session.commit()
                log.warning("order_rejected_live_mode", symbol=intent.symbol)
                return row

            try:
                receipt = self._adapter.submit_order(intent)
            except Exception as exc:
                row.status = "rejected"
                row.reject_reason = f"{type(exc).__name__}: {exc}"
                session.commit()
                log.warning("order_rejected_broker", symbol=intent.symbol, error=str(exc))
                return row

            row.broker_order_id = receipt.broker_order_id
            row.status = str(receipt.status)
            session.commit()
            log.info(
                "order_submitted",
                symbol=intent.symbol,
                broker_order_id=receipt.broker_order_id,
                status=str(receipt.status),
            )
            return row
