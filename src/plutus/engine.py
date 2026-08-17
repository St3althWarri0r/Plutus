"""The paper-autotrading engine process (§12 Phase 5): `python -m plutus.engine`.

Separate process from the dashboard. Startup is crash-safe (§11): write the
session row, alert if the previous session never stopped clean, snapshot the
manual baseline, sync/ingest fills, reconcile against the broker, mark day
start if 09:30 already passed — only then does the scheduler start. SIGTERM
stops the scheduler and marks the session clean. Fill confirmation is a
20-second sync/ingest poll — a documented deviation from §3's websocket
stream (see ARCHITECTURE.md); broker-side brackets protect ORB positions
regardless of process health.
"""

import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import BrokerAdapter
from plutus.execution import append_provisional_close, compute_rebalance_orders
from plutus.logging_setup import configure_logging, get_logger
from plutus.models import BotPosition, EngineSession, StrategyState
from plutus.pnl import ingest_fills
from plutus.risk import Alert, RiskManager, log_alert
from plutus.strategies.orb import OpeningRangeBreakout
from plutus.strategies.tqqq_rotation import TQQQRotation

log = get_logger("plutus.engine")

ET = ZoneInfo("America/New_York")


# --- strategy job functions (pure-ish, composed by the runtime) ---------------


def run_daily_rotation(
    risk: RiskManager,
    *,
    strategy: TQQQRotation,
    closes: pd.DataFrame,
    latest_prices: dict[str, float],
    allocation: float,
) -> None:
    """The 15:50 ET job: provisional close → weights → rebalance orders."""
    frame = append_provisional_close(
        closes, latest_prices=latest_prices, now=risk._clock().astimezone(UTC)
    )
    weights = strategy.weights_history(frame).iloc[-1]
    with risk._session_factory() as session:
        rows = session.scalars(
            select(BotPosition).where(BotPosition.strategy == strategy.name)
        ).all()
        positions = {r.symbol: float(r.qty) for r in rows}
    orders = compute_rebalance_orders(
        weights={s: float(weights[s]) for s in weights.index},
        allocation=allocation,
        positions=positions,
        price_lookup=lambda s: latest_prices.get(s),
        strategy=strategy.name,
    )
    for intent in orders:
        risk.submit(intent)


def run_orb_tick(
    risk: RiskManager,
    orb: OpeningRangeBreakout,
    minute_bars_by_symbol: dict[str, pd.DataFrame],
) -> None:
    for symbol, bars in minute_bars_by_symbol.items():
        intent = orb.on_minute(symbol, bars)
        if intent is not None:
            risk.submit(intent)


# --- runtime ------------------------------------------------------------------


class EngineRuntime:
    def __init__(
        self,
        *,
        risk: RiskManager,
        session_factory: sessionmaker[Session],
        adapter: BrokerAdapter,
        clock: Callable[[], datetime] | None = None,
        alert: Alert = log_alert,
    ) -> None:
        self._risk = risk
        self._session_factory = session_factory
        self._adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._alert = alert
        self._session_id: int | None = None

    def startup(self) -> None:
        now = self._clock().astimezone(UTC)
        with self._session_factory() as session:
            unclean = session.scalars(
                select(EngineSession).where(
                    EngineSession.stopped_at.is_(None) | (EngineSession.clean.is_(False))
                )
            ).all()
            if unclean:
                self._alert(
                    "critical",
                    f"previous session did not stop clean "
                    f"({len(unclean)} unclean session rows) — was the engine killed?",
                )
            row = EngineSession(session_date=now.date(), started_at=now)
            session.add(row)
            session.commit()
            self._session_id = row.id

        self._risk.mark_manual_baseline()
        self._risk.sync_fills()
        ingest_fills(self._adapter, self._session_factory, clock=self._clock)
        self._risk.reconcile()
        self._mark_day_start_if_missed()
        log.info("engine_started", session_id=self._session_id)

    def shutdown(self, *, clean: bool, error: str | None = None) -> None:
        if self._session_id is None:
            return
        with self._session_factory() as session:
            row = session.get(EngineSession, self._session_id)
            if row is not None:
                row.stopped_at = self._clock().astimezone(UTC)
                row.clean = clean
                row.error = error
                session.commit()
        log.info("engine_stopped", clean=clean, error=error)

    def _mark_day_start_if_missed(self) -> None:
        """If the engine starts mid-session after 09:30 ET, mark day-start now."""
        now_et = self._clock().astimezone(ET)
        if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30):
            return
        with self._session_factory() as session:
            states = session.scalars(select(StrategyState)).all()
            for state in states:
                marked = state.updated_at is not None and state.day_start_equity_usd is not None
                if not marked and float(state.allocation_usd) > 0:
                    state.day_start_equity_usd = state.allocation_usd
            session.commit()


def main() -> None:  # pragma: no cover - composition root, exercised by smoke
    from plutus.alerts import TelegramAlerter
    from plutus.app import _default_price_lookup
    from plutus.brokers.alpaca import alpaca_adapter_from_settings
    from plutus.config import effective_trading_mode, get_settings, resolve_runtime_root
    from plutus.data.alpaca_data import alpaca_data_provider_from_settings
    from plutus.data.cache import CachedDataProvider
    from plutus.db import make_engine, make_session_factory
    from plutus.pnl import DailyLossMonitor, equity_now
    from plutus.scheduler import build_scheduler
    from plutus.strategies.orb import load_orb_config

    configure_logging()
    settings = get_settings()
    factory = make_session_factory(make_engine())
    adapter = alpaca_adapter_from_settings(settings, effective_trading_mode())
    provider = alpaca_data_provider_from_settings(settings)
    cache = CachedDataProvider(provider, factory)
    alerter = TelegramAlerter(settings)
    price_lookup = _default_price_lookup(factory)

    risk = RiskManager(
        adapter=adapter,
        session_factory=factory,
        settings=settings,
        alert=alerter,
        price_lookup=price_lookup,
    )
    rotation = TQQQRotation()
    orb_config_path = resolve_runtime_root(settings) / "strategies.toml"
    orb = OpeningRangeBreakout(load_orb_config(orb_config_path))
    risk.config.intraday_strategies.add(orb.name)

    def latest_minute_price(symbol: str) -> float | None:
        try:
            now = datetime.now(UTC)
            bars = provider.get_bars(symbol, "1m", now - timedelta(minutes=20), now)
            return float(bars["close"].iloc[-1]) if len(bars) else None
        except Exception:
            return None

    def strategy_equity(strategy: str) -> float | None:
        with factory() as session:
            state = session.scalars(
                select(StrategyState).where(StrategyState.strategy == strategy)
            ).one_or_none()
            if state is None or state.day_start_equity_usd is None:
                return None
            day_start = float(state.day_start_equity_usd)
        now = datetime.now(UTC)
        since = datetime(now.year, now.month, now.day, 13, 30, tzinfo=UTC)
        return equity_now(
            strategy,
            session_factory=factory,
            price_lookup=latest_minute_price,
            day_start=day_start,
            since=since,
        )

    runtime = EngineRuntime(
        risk=risk, session_factory=factory, adapter=adapter, alert=alerter
    )
    runtime.startup()
    if alerter.configured:
        alerter("info", "engine started (paper) — Telegram alerting live")

    scheduler = build_scheduler(
        risk, equity_lookup=strategy_equity, strategies=[rotation.name, orb.name]
    )
    monitor = DailyLossMonitor(warn_pct=0.015, alert=alerter)

    def rotation_job() -> None:
        now = datetime.now(UTC)
        closes_frames = {
            s: cache.get_bars(s, "1d", now - timedelta(days=450), now)
            for s in rotation.universe
        }
        closes = pd.DataFrame({s: f["close"] for s, f in closes_frames.items()}).dropna()
        latest = {s: latest_minute_price(s) for s in rotation.universe}
        priced = {s: p for s, p in latest.items() if p is not None}
        if len(priced) < len(rotation.universe):
            alerter("critical", f"rotation skipped — unpriceable: {set(latest) - set(priced)}")
            return
        with factory() as session:
            state = session.scalars(
                select(StrategyState).where(StrategyState.strategy == rotation.name)
            ).one_or_none()
            allocation = (
                float(state.allocation_usd)
                if state is not None and float(state.allocation_usd) > 0
                else risk.config.default_allocation_usd
            )
        run_daily_rotation(
            risk, strategy=rotation, closes=closes, latest_prices=priced, allocation=allocation
        )

    def orb_job() -> None:
        now = datetime.now(UTC)
        session_open = datetime(now.year, now.month, now.day, 13, 30, tzinfo=UTC)
        bars = {}
        for symbol in orb.config.symbols:
            try:
                frame = provider.get_bars(symbol, "1m", session_open, now)
                if len(frame):
                    bars[symbol] = frame
            except Exception as exc:
                log.warning("orb_bars_unavailable", symbol=symbol, error=str(exc))
        run_orb_tick(risk, orb, bars)

    def fills_job() -> None:
        risk.sync_fills()
        n = ingest_fills(adapter, factory)
        if n:
            alerter("info", f"{n} new fill(s) recorded")

    def loss_watch_job() -> None:
        for strategy in (rotation.name, orb.name):
            with factory() as session:
                state = session.scalars(
                    select(StrategyState).where(StrategyState.strategy == strategy)
                ).one_or_none()
            if state is None or state.day_start_equity_usd is None:
                continue
            equity = strategy_equity(strategy)
            if equity is not None:
                monitor.observe(
                    strategy, day_start=float(state.day_start_equity_usd), equity=equity
                )

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        rotation_job, CronTrigger(hour=15, minute=50, timezone="America/New_York"),
        id="rotation_1550",
    )
    scheduler.add_job(
        orb_job,
        CronTrigger(hour="9-15", minute="*", timezone="America/New_York"),
        id="orb_tick",
    )
    scheduler.add_job(fills_job, IntervalTrigger(seconds=20), id="fills_sync")
    scheduler.add_job(loss_watch_job, IntervalTrigger(seconds=60), id="loss_watch")

    stopping = {"flag": False}

    def on_sigterm(_signum: int, _frame: FrameType | None) -> None:
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)

    scheduler.start()
    log.info("scheduler_running")
    try:
        while not stopping["flag"]:
            time.sleep(1)
        scheduler.shutdown(wait=True)
        runtime.shutdown(clean=True)
    except Exception as exc:
        alerter("critical", f"engine crashed: {exc}")
        runtime.shutdown(clean=False, error=str(exc))
        raise


if __name__ == "__main__":  # pragma: no cover
    main()
