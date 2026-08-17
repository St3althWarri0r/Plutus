"""RiskManager: the only component allowed to call BrokerAdapter.submit_order.

Phase 4: the full §8 gate chain runs before EVERY order, in this sequence —
kill switch → trading mode → idempotency dedupe → strategy enabled → stale
data → market hours → rate limits → priced gates (position size, stop-based
risk, leveraged-ETF cap, concurrent positions). Risk-reducing exits bypass the
entry gates and the rate limiter: a daily-loss flatten must never be throttled.
A flip (closing past flat) counts as an entry.

Everything time- or price-dependent is injected (clock, price_lookup,
stale_check, calendar, runtime_root) so the chain is fully testable with a
mocked broker. The kill check fails CLOSED: an invalid runtime root rejects
orders rather than assuming no KILL file exists.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import BrokerAdapter, OrderIntent
from plutus.config import (
    Settings,
    TradingMode,
    effective_trading_mode,
    get_settings,
    resolve_runtime_root,
)
from plutus.logging_setup import get_logger
from plutus.market_calendar import MarketCalendar, is_crypto
from plutus.models import BotPosition, Order, StrategyState

log = get_logger("plutus.risk")

Alert = Callable[[str, str], None]  # (severity, message)


def log_alert(severity: str, message: str) -> None:
    log.warning("alert", severity=severity, message=message)


class RiskConfig(BaseModel):
    """§8 hard gates — all configurable, spec defaults."""

    max_position_pct: float = Field(default=0.20, gt=0)
    # full-allocation rotation strategies legitimately hold 100% in one symbol;
    # the override keys by strategy name (§8 gates are configurable by design)
    max_position_pct_overrides: dict[str, float] = Field(
        default_factory=lambda: {"tqqq_rotation": 1.0}
    )
    max_risk_per_trade_pct: float = Field(default=0.01, gt=0)
    max_concurrent_positions: int = Field(default=3, gt=0)
    daily_loss_halt_pct: float = Field(default=0.03, gt=0)
    leveraged_cap_pct: float = Field(default=0.25, gt=0)
    leveraged_symbols: set[str] = Field(
        default_factory=lambda: {
            "TQQQ", "SQQQ", "TECL", "TECS", "UVXY", "SVXY", "SOXL", "SOXS",
            "UPRO", "SPXU", "SPXL", "SSO", "SDS", "QLD", "QID", "TMF", "TMV",
            "LABU", "LABD", "FNGU", "FNGD",
        }
    )
    orders_per_minute: int = Field(default=10, gt=0)
    orders_per_day: int = Field(default=100, gt=0)
    intraday_strategies: set[str] = Field(default_factory=set)
    # dollar allocations — NOT fractions of account equity, which is dominated
    # by non-bot holdings in this account
    default_allocation_usd: float = Field(default=25_000.0, gt=0)
    total_bot_equity_usd: float = Field(default=100_000.0, gt=0)


class GateRejection(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize_symbol(symbol: str) -> str:
    """Alpaca reports crypto positions without the pair slash (BTC/USD → BTCUSD)."""
    return symbol.replace("/", "")


@dataclass
class ReconcileReport:
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    in_flight: list[str] = field(default_factory=list)


@dataclass
class KillReport:
    canceled: int = 0
    flattened: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(
        self,
        adapter: BrokerAdapter,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
        effective_mode: TradingMode | None = None,
        config: RiskConfig | None = None,
        calendar: MarketCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
        runtime_root: Path | None = None,
        price_lookup: Callable[[str], float | None] | None = None,
        stale_check: Callable[[str], bool] | None = None,
        alert: Alert = log_alert,
    ) -> None:
        self._adapter = adapter
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._effective_mode: TradingMode = effective_mode or effective_trading_mode(self._settings)
        self.config = config or RiskConfig()
        self._calendar = calendar or MarketCalendar()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runtime_root = runtime_root or resolve_runtime_root(self._settings)
        self._price_lookup = price_lookup or (lambda _s: None)
        self._stale_check = stale_check or (lambda _s: False)
        self._alert = alert

    # --- public API -----------------------------------------------------------

    def submit(self, intent: OrderIntent) -> Order:
        """Full §8 gate chain, then the broker adapter — exactly once."""
        return self._submit(intent, exit_privileged=False)

    def kill_active(self) -> bool:
        """True when trading must stop. Fails closed on an invalid root."""
        return not self._runtime_root.is_dir() or (self._runtime_root / "KILL").exists()

    def kill(self, source: str) -> KillReport:
        """§8 kill switch: persist KILL, cancel all open orders, flatten every
        bot position, disable every strategy. Never raises — each step alerts
        on failure and moves on; un-kill is a manual KILL-file removal."""
        self._alert("critical", f"KILL switch engaged (source: {source})")
        report = KillReport()
        try:
            (self._runtime_root / "KILL").touch()
        except OSError as exc:
            self._alert("critical", f"could not persist KILL file: {exc}")

        with self._session_factory() as session:
            open_orders = session.scalars(
                select(Order).where(
                    Order.broker_order_id.is_not(None),
                    Order.status.not_in(["filled", "canceled", "rejected", "expired"]),
                )
            ).all()
            for row in open_orders:
                assert row.broker_order_id is not None
                try:
                    self._adapter.cancel_order(row.broker_order_id)
                    row.status = "canceled"
                    report.canceled += 1
                except Exception as exc:
                    self._alert(
                        "critical",
                        f"kill: cancel failed for {row.symbol} "
                        f"({row.broker_order_id}): {exc}",
                    )
            session.commit()
            strategies = {
                r.strategy
                for r in session.scalars(
                    select(BotPosition).where(BotPosition.qty != 0)
                ).all()
            }
            all_known = strategies | {
                s.strategy for s in session.scalars(select(StrategyState)).all()
            }

        for strategy in sorted(strategies):
            with self._session_factory() as session:
                rows = session.scalars(
                    select(BotPosition).where(
                        BotPosition.strategy == strategy, BotPosition.qty != 0
                    )
                ).all()
                symbols = [r.symbol for r in rows]
            self.flatten_strategy(strategy, reason=f"kill switch ({source})")
            report.flattened.extend(symbols)

        with self._session_factory() as session:
            for strategy in sorted(all_known):
                self._disable(session, strategy, f"kill switch ({source})")
                report.disabled.append(strategy)
            session.commit()
        log.warning(
            "kill_executed",
            source=source,
            canceled=report.canceled,
            flattened=report.flattened,
            disabled=report.disabled,
        )
        return report

    # --- position book (fills only — acceptance never books a position) ------

    def record_fill(self, strategy: str, symbol: str, signed_qty: float) -> None:
        with self._session_factory() as session:
            row = session.scalars(
                select(BotPosition).where(
                    BotPosition.strategy == strategy, BotPosition.symbol == symbol
                )
            ).one_or_none()
            if row is None:
                session.add(BotPosition(strategy=strategy, symbol=symbol, qty=signed_qty))
            else:
                row.qty = float(row.qty) + signed_qty
            session.commit()
            log.info("fill_recorded", strategy=strategy, symbol=symbol, signed_qty=signed_qty)

    def sync_fills(self) -> None:
        """Resolve non-terminal orders against the broker; book confirmed fills."""
        from plutus.brokers.base import OrderStatus

        with self._session_factory() as session:
            open_orders = session.scalars(
                select(Order).where(
                    Order.broker_order_id.is_not(None),
                    Order.status.not_in(["filled", "canceled", "rejected", "expired"]),
                )
            ).all()
            for row in open_orders:
                assert row.broker_order_id is not None
                status = self._adapter.get_order_status(row.broker_order_id)
                if status == OrderStatus.FILLED:
                    signed = float(row.qty) if row.side == "buy" else -float(row.qty)
                    row.status = str(status)
                    session.commit()
                    self.record_fill(row.strategy, row.symbol, signed)
                elif str(status) != row.status and status != OrderStatus.UNKNOWN:
                    prior = row.status
                    row.status = str(status)
                    # rule 6: partial fill then cancel/expiry leaves shares the
                    # bot book doesn't know about — halt + alert, never guess
                    if (
                        status in (OrderStatus.CANCELED, OrderStatus.EXPIRED)
                        and prior == "partially_filled"
                    ):
                        self._disable(
                            session,
                            row.strategy,
                            f"partial fill then {status} on {row.symbol} — "
                            "manual reconcile required",
                        )
                        self._alert(
                            "critical",
                            f"partial fill then {status}: {row.strategy}/{row.symbol} "
                            f"order {row.broker_order_id} — strategy halted, "
                            "reconcile manually",
                        )
                    session.commit()

    # --- reconciliation (§8: DB is intent, broker is truth) -------------------

    def mark_manual_baseline(self) -> None:
        """Snapshot broker holdings the bot book doesn't own — the human's
        positions. Called at engine start and before the 09:15 reconcile.
        Baseline symbols stop generating unknown-position warnings; mid-day
        manual trades still mismatch until the next mark (§8-correct)."""
        from plutus.models import ManualBaseline

        with self._session_factory() as session:
            bot: dict[str, float] = {}
            for r in session.scalars(select(BotPosition).where(BotPosition.qty != 0)).all():
                key = _normalize_symbol(r.symbol)
                bot[key] = bot.get(key, 0.0) + float(r.qty)

            session.query(ManualBaseline).delete()
            now = self._clock().astimezone(UTC)
            for p in self._adapter.get_positions():
                key = _normalize_symbol(p.symbol)
                residual = p.qty - bot.get(key, 0.0)
                if abs(residual) > 1e-9:
                    session.add(ManualBaseline(symbol=key, qty=residual, marked_at=now))
            session.commit()
            log.info("manual_baseline_marked")

    def _baseline(self, session: Session) -> dict[str, float]:
        from plutus.models import ManualBaseline

        return {
            r.symbol: float(r.qty)
            for r in session.scalars(select(ManualBaseline)).all()
        }

    def reconcile(self) -> ReconcileReport:
        self.sync_fills()
        report = ReconcileReport()
        with self._session_factory() as session:
            still_open = session.scalars(
                select(Order).where(
                    Order.broker_order_id.is_not(None),
                    Order.status.not_in(["filled", "canceled", "rejected", "expired"]),
                )
            ).all()
            in_flight = {_normalize_symbol(o.symbol) for o in still_open}

            bot_rows = session.scalars(select(BotPosition).where(BotPosition.qty != 0)).all()
            expected: dict[str, float] = {}
            owners: dict[str, set[str]] = {}
            for r in bot_rows:
                key = _normalize_symbol(r.symbol)
                expected[key] = expected.get(key, 0.0) + float(r.qty)
                owners.setdefault(key, set()).add(r.strategy)

            # the human's marked holdings shift expectations and are never unknown
            baseline = self._baseline(session)
            for key, qty in baseline.items():
                expected[key] = expected.get(key, 0.0) + qty
                owners.setdefault(key, set())

            broker = {
                _normalize_symbol(p.symbol): p.qty for p in self._adapter.get_positions()
            }

            for symbol, exp_qty in expected.items():
                if symbol in in_flight:
                    report.in_flight.append(symbol)
                    continue
                actual = broker.get(symbol, 0.0)
                # crypto quantities carry more precision than our Numeric
                # columns; tolerate storage rounding, scaled to position size
                tolerance = max(1e-6, 1e-8 * abs(exp_qty))
                if abs(actual - exp_qty) > tolerance:
                    report.mismatches.append(
                        {"symbol": symbol, "expected": exp_qty, "broker": actual}
                    )
                    for strategy in owners[symbol]:
                        self._disable(session, strategy, f"reconcile mismatch on {symbol}")
                    self._alert(
                        "critical",
                        f"reconcile mismatch {symbol}: expected {exp_qty}, broker {actual} "
                        f"— halted {sorted(owners[symbol])}",
                    )

            report.in_flight.extend(
                s for s in in_flight if s not in report.in_flight and s not in expected
            )
            for symbol in broker:
                if symbol not in expected and symbol not in in_flight:
                    report.unknown.append(symbol)
            if report.unknown:
                self._alert(
                    "warning",
                    f"broker positions outside bot book (not halting): {sorted(report.unknown)}",
                )
            session.commit()
        log.info(
            "reconcile_done",
            mismatches=len(report.mismatches),
            unknown=report.unknown,
            in_flight=report.in_flight,
        )
        return report

    # --- daily-loss halt (§8: −3% → flatten + disable until manual re-enable) --

    def mark_day_start(self, strategy: str, equity: float) -> None:
        with self._session_factory() as session:
            state = self._state(session, strategy)
            state.day_start_equity_usd = equity
            session.commit()

    def check_daily_loss(self, strategy: str, current_equity: float) -> None:
        with self._session_factory() as session:
            state = self._state(session, strategy)
            day_start = (
                float(state.day_start_equity_usd)
                if state.day_start_equity_usd is not None
                else None
            )
        if day_start is None or day_start <= 0:
            return
        loss = (day_start - current_equity) / day_start
        if loss >= self.config.daily_loss_halt_pct:
            self._alert(
                "critical",
                f"daily loss halt: {strategy} down {loss:.2%} "
                f"(≥ {self.config.daily_loss_halt_pct:.0%}) — flattening and disabling",
            )
            self.flatten_strategy(strategy, reason=f"daily loss halt ({loss:.2%})")

    def flatten_strategy(self, strategy: str, reason: str, *, disable: bool = True) -> None:
        """Close every bot position for the strategy.

        disable=True (halts, kill) leaves the strategy off until manual
        re-enable; disable=False is for the routine 15:55 auto-flatten, which
        must never touch enable state — a daily-loss halt earlier in the day
        stays a halt. Exit orders carry full exit privileges — no entry gates,
        no rate limit. A broker failure mid-flatten alerts and never raises.
        """
        with self._session_factory() as session:
            if disable:
                self._disable(session, strategy, reason)
            session.commit()
            rows = session.scalars(
                select(BotPosition).where(
                    BotPosition.strategy == strategy, BotPosition.qty != 0
                )
            ).all()
            positions = [(r.symbol, float(r.qty)) for r in rows]

        for symbol, qty in positions:
            intent = OrderIntent(
                symbol=symbol,
                side="sell" if qty > 0 else "buy",
                qty=abs(qty),
                order_type="market",
                time_in_force="gtc" if "/" in symbol else "day",
                strategy=strategy,
            )
            row = self._submit(intent, exit_privileged=True)
            if row.status == "rejected":
                self._alert(
                    "critical",
                    f"flatten order failed for {strategy}/{symbol}: {row.reject_reason}",
                )

    def enable_strategy(self, strategy: str) -> None:
        with self._session_factory() as session:
            state = self._state(session, strategy)
            state.enabled = True
            state.halt_reason = None
            session.commit()

    def _disable(self, session: Session, strategy: str, reason: str) -> None:
        state = self._state(session, strategy)
        state.enabled = False
        state.halt_reason = reason
        log.warning("strategy_disabled", strategy=strategy, reason=reason)

    def _state(self, session: Session, strategy: str) -> StrategyState:
        state = session.scalars(
            select(StrategyState).where(StrategyState.strategy == strategy)
        ).one_or_none()
        if state is None:
            state = StrategyState(strategy=strategy, enabled=True)
            session.add(state)
            session.flush()
        return state

    # --- submission core ------------------------------------------------------

    def _submit(self, intent: OrderIntent, *, exit_privileged: bool) -> Order:
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
                created_at=self._clock().astimezone(UTC),
            )
            session.add(row)
            session.commit()

            try:
                self._run_gates(session, intent, exit_privileged=exit_privileged)
            except GateRejection as rejection:
                row.status = "rejected"
                row.reject_reason = rejection.reason
                session.commit()
                log.warning("order_rejected_gate", symbol=intent.symbol, reason=rejection.reason)
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
                strategy=intent.strategy,
                broker_order_id=receipt.broker_order_id,
                status=str(receipt.status),
            )
            return row

    # --- gate chain -----------------------------------------------------------

    def _run_gates(
        self, session: Session, intent: OrderIntent, *, exit_privileged: bool
    ) -> None:
        self._gate_kill(exit_privileged=exit_privileged)
        self._gate_mode()
        self._gate_strategy_enabled(session, intent, exit_privileged=exit_privileged)

        is_exit = exit_privileged or self._is_exit(session, intent)
        if is_exit:
            return  # risk-reducing: no entry gates, no rate limit

        self._gate_stale(intent)
        self._gate_market_hours(intent)
        self._gate_rate_limit(session, intent)
        self._gate_priced(session, intent)

    def _gate_kill(self, *, exit_privileged: bool) -> None:
        if not self._runtime_root.is_dir():
            raise GateRejection(
                f"runtime root {self._runtime_root} is not a directory — "
                "cannot verify KILL absence, failing closed"
            )
        if (self._runtime_root / "KILL").exists() and not exit_privileged:
            raise GateRejection("KILL switch present — all trading halted")

    def _gate_mode(self) -> None:
        if self._effective_mode != "paper":
            raise GateRejection("live trading is not enabled before Phase 9")

    def _gate_strategy_enabled(
        self, session: Session, intent: OrderIntent, *, exit_privileged: bool
    ) -> None:
        if exit_privileged:
            return
        state = session.scalars(
            select(StrategyState).where(StrategyState.strategy == intent.strategy)
        ).one_or_none()
        if state is not None and not state.enabled:
            raise GateRejection(
                f"strategy {intent.strategy!r} is disabled"
                + (f" ({state.halt_reason})" if state.halt_reason else "")
            )

    def _gate_stale(self, intent: OrderIntent) -> None:
        if self._stale_check(intent.symbol):
            raise GateRejection(f"market data for {intent.symbol} is stale — entries blocked")

    def _gate_market_hours(self, intent: OrderIntent) -> None:
        if is_crypto(intent.symbol):
            return
        if not self._calendar.is_rth(self._clock()):
            raise GateRejection("market closed — equity entries only during regular hours")

    def _gate_rate_limit(self, session: Session, intent: OrderIntent) -> None:
        now = self._clock().astimezone(UTC)
        minute_count = self._order_count(
            session,
            intent.strategy,
            now - timedelta(seconds=60),
            exclude_key=intent.idempotency_key,
        )
        if minute_count >= self.config.orders_per_minute:
            raise GateRejection(
                f"rate limit: {minute_count} orders in the last minute "
                f"(max {self.config.orders_per_minute})"
            )
        day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        day_count = self._order_count(
            session, intent.strategy, day_start, exclude_key=intent.idempotency_key
        )
        if day_count >= self.config.orders_per_day:
            raise GateRejection(
                f"rate limit: {day_count} orders today (max {self.config.orders_per_day})"
            )

    def _gate_priced(self, session: Session, intent: OrderIntent) -> None:
        price = intent.limit_price or self._price_lookup(intent.symbol)
        if price is None or price <= 0:
            raise GateRejection(
                f"cannot price {intent.symbol} for risk sizing — entry rejected"
            )
        allocation = self._allocation(session, intent.strategy)

        # position size: resulting notional ≤ 20% of strategy allocation
        pos = self._position_qty(session, intent.strategy, intent.symbol)
        signed = intent.qty if intent.side == "buy" else -intent.qty
        resulting_notional = abs(pos + signed) * price
        max_pct = self.config.max_position_pct_overrides.get(
            intent.strategy, self.config.max_position_pct
        )
        cap = max_pct * allocation
        if resulting_notional > cap:
            raise GateRejection(
                f"position size {resulting_notional:.2f} exceeds "
                f"{max_pct:.0%} of allocation ({cap:.2f})"
            )

        # stop-based risk per trade
        if intent.stop_price is not None:
            risk = abs(price - intent.stop_price) * intent.qty
            budget = self.config.max_risk_per_trade_pct * allocation
            if risk > budget:
                raise GateRejection(
                    f"stop-based risk {risk:.2f} exceeds "
                    f"{self.config.max_risk_per_trade_pct:.0%} of allocation ({budget:.2f})"
                )

        # leveraged-ETF notional cap across the whole bot book
        if intent.symbol in self.config.leveraged_symbols:
            total = resulting_notional
            rows = session.scalars(
                select(BotPosition).where(BotPosition.qty != 0)
            ).all()
            for r in rows:
                if r.symbol in self.config.leveraged_symbols and not (
                    r.strategy == intent.strategy and r.symbol == intent.symbol
                ):
                    other_price = self._price_lookup(r.symbol)
                    if other_price is None:
                        raise GateRejection(
                            f"cannot price held leveraged position {r.symbol} — failing closed"
                        )
                    total += abs(float(r.qty)) * other_price
            lev_cap = self.config.leveraged_cap_pct * self.config.total_bot_equity_usd
            if total > lev_cap:
                raise GateRejection(
                    f"leveraged notional {total:.2f} exceeds "
                    f"{self.config.leveraged_cap_pct:.0%} of bot equity ({lev_cap:.2f})"
                )

        # concurrent-position cap for intraday strategies
        if intent.strategy in self.config.intraday_strategies:
            held = {
                r.symbol
                for r in session.scalars(
                    select(BotPosition).where(
                        BotPosition.strategy == intent.strategy, BotPosition.qty != 0
                    )
                ).all()
            }
            if intent.symbol not in held and len(held) >= self.config.max_concurrent_positions:
                raise GateRejection(
                    f"concurrent-position cap: {len(held)} open "
                    f"(max {self.config.max_concurrent_positions})"
                )

    # --- helpers --------------------------------------------------------------

    def _order_count(
        self, session: Session, strategy: str, since: datetime, *, exclude_key: str
    ) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.strategy == strategy,
                    Order.status != "rejected",
                    Order.created_at >= since,
                    Order.idempotency_key != exclude_key,  # the row under review
                )
            )
            or 0
        )

    def _allocation(self, session: Session, strategy: str) -> float:
        state = session.scalars(
            select(StrategyState).where(StrategyState.strategy == strategy)
        ).one_or_none()
        if state is not None and float(state.allocation_usd) > 0:
            return float(state.allocation_usd)
        return self.config.default_allocation_usd

    def _position_qty(self, session: Session, strategy: str, symbol: str) -> float:
        if strategy == "manual":
            # the human's book is the broker account itself
            normalized = symbol.replace("/", "")
            for p in self._adapter.get_positions():
                if p.symbol.replace("/", "") == normalized:
                    return p.qty
            return 0.0
        row = session.scalars(
            select(BotPosition).where(
                BotPosition.strategy == strategy, BotPosition.symbol == symbol
            )
        ).one_or_none()
        return float(row.qty) if row is not None else 0.0

    def _is_exit(self, session: Session, intent: OrderIntent) -> bool:
        """Risk-reducing = strictly shrinks |position| without crossing flat."""
        pos = self._position_qty(session, intent.strategy, intent.symbol)
        if pos > 0 and intent.side == "sell":
            return intent.qty <= pos
        if pos < 0 and intent.side == "buy":
            return intent.qty <= -pos
        return False
