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
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import BrokerAdapter, OrderIntent

if TYPE_CHECKING:
    from plutus.ai.mode_a import ModeA
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


def _review_entry(
    risk: RiskManager, intent: "OrderIntent", mode_a: "ModeA | None", context: str
) -> "OrderIntent | None":
    """Mode A review for one ENTRY (§9). Exits never reach this function —
    that is 9.4's 'never blocks risk-reducing exits' enforced structurally.
    A failed/timed-out review proceeds at 0.5× with a critical alert: a
    silently degraded supervisor must never pass unnoticed."""
    from plutus.ai.mode_a import apply_review

    if mode_a is None:
        return intent  # Mode A not enabled: Phase 5 behavior, full size
    review = mode_a.trade_review(intent, context=context)
    if review is None:
        risk._alert(
            "critical",
            f"AI review unavailable for {intent.symbol} — proceeding at 0.5× (§9.4)",
        )
    result = apply_review(intent, review)
    if result is None and review is not None and review.action == "veto":
        risk.record_veto(intent, review.rationale)
    return result


def run_daily_rotation(
    risk: RiskManager,
    *,
    strategy: TQQQRotation,
    closes: pd.DataFrame,
    latest_prices: dict[str, float],
    allocation: float,
    mode_a: "ModeA | None" = None,
) -> None:
    """The 15:50 ET job: provisional close → weights → rebalance orders.
    Buys are entries (reviewed); the rotation never shorts, so sells are
    always risk-reducing exits and skip review."""
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
        if intent.side == "buy":
            reviewed = _review_entry(
                risk, intent, mode_a, context=f"daily rotation rebalance to {dict(weights)}"
            )
            if reviewed is None:
                continue
            intent = reviewed
        risk.submit(intent)


def run_orb_tick(
    risk: RiskManager,
    orb: OpeningRangeBreakout,
    minute_bars_by_symbol: dict[str, pd.DataFrame],
    mode_a: "ModeA | None" = None,
) -> None:
    for symbol, bars in minute_bars_by_symbol.items():
        intent = orb.on_minute(symbol, bars)
        if intent is None:
            continue
        reviewed = _review_entry(
            risk, intent, mode_a, context=f"ORB breakout entry, stop {intent.stop_price}"
        )
        if reviewed is not None:
            risk.submit(reviewed)


# --- runtime ------------------------------------------------------------------


def make_strategy_equity(
    session_factory: sessionmaker[Session],
    risk: RiskManager,
    *,
    price_lookup: Callable[[str], float | None],
) -> Callable[[str], float | None]:
    """equity_lookup with a bootstrap base: before any 09:30 mark exists,
    measure from day_start=allocation since inception — otherwise the mark
    waits for equity and equity waits for the mark, and daily-loss protection
    never activates on a fresh DB."""
    from plutus.pnl import equity_now

    def lookup(strategy: str) -> float | None:
        with session_factory() as session:
            state = session.scalars(
                select(StrategyState).where(StrategyState.strategy == strategy)
            ).one_or_none()
            if state is None:
                return None
            if state.day_start_equity_usd is not None:
                day_start = float(state.day_start_equity_usd)
                now = risk._clock().astimezone(UTC)
                since = datetime(now.year, now.month, now.day, 13, 30, tzinfo=UTC)
            else:
                day_start = float(state.allocation_usd)
                since = datetime(1970, 1, 1, tzinfo=UTC)  # inception bootstrap
        if day_start <= 0:
            return None
        return equity_now(
            strategy,
            session_factory=session_factory,
            price_lookup=price_lookup,
            day_start=day_start,
            since=since,
        )

    return lookup


def make_error_listener(
    *, alert: Alert, errored: dict[str, bool]
) -> Callable[[object], None]:
    """APScheduler EVENT_JOB_ERROR hook: the session ledger is the 'zero
    unhandled errors' acceptance instrument — a job crash must alert and
    mark the session unclean."""

    def listener(event: object) -> None:
        errored["flag"] = True
        job_id = getattr(event, "job_id", "?")
        exception = getattr(event, "exception", None)
        alert("critical", f"scheduler job {job_id} raised: {exception!r}")

    return listener


class EngineRuntime:
    def __init__(
        self,
        *,
        risk: RiskManager,
        session_factory: sessionmaker[Session],
        adapter: BrokerAdapter,
        clock: Callable[[], datetime] | None = None,
        alert: Alert = log_alert,
        strategies: list[str] | None = None,
    ) -> None:
        self._risk = risk
        self._session_factory = session_factory
        self._adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._alert = alert
        self._strategies = strategies or []
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

        self._seed_strategy_states()
        self._risk.mark_manual_baseline()
        self._risk.sync_fills()
        ingest_fills(self._adapter, self._session_factory, clock=self._clock)
        self._risk.reconcile()
        self._mark_day_start_if_missed()
        log.info("engine_started", session_id=self._session_id)

    def _seed_strategy_states(self) -> None:
        """Fresh DBs have no strategy_state rows; seed them with the default
        allocation so the mark/equity/loss chain can bootstrap."""
        with self._session_factory() as session:
            for name in self._strategies:
                state = session.scalars(
                    select(StrategyState).where(StrategyState.strategy == name)
                ).one_or_none()
                if state is None:
                    session.add(
                        StrategyState(
                            strategy=name,
                            enabled=True,
                            allocation_usd=self._risk.config.default_allocation_usd,
                        )
                    )
                elif float(state.allocation_usd) <= 0:
                    state.allocation_usd = self._risk.config.default_allocation_usd
            session.commit()

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
    from plutus.pnl import DailyLossMonitor
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

    strategy_equity = make_strategy_equity(factory, risk, price_lookup=latest_minute_price)

    # Mode A: enabled only when the key is configured; absent key = Phase 5
    # behavior at full size (§9.4's 0.5× fallback is for outages of a
    # CONFIGURED supervisor, not for the feature being off)
    mode_a: ModeA | None = None
    if settings.anthropic_api_key:
        from plutus.ai.client import AiClient, make_anthropic_transport
        from plutus.ai.mode_a import ModeA as _ModeA

        ai_client = AiClient(
            session_factory=factory,
            transport=make_anthropic_transport(settings.anthropic_api_key),
            model=settings.ai_model,
        )
        mode_a = _ModeA(client=ai_client, session_factory=factory)
        log.info("mode_a_enabled", model=settings.ai_model)
    else:
        log.info("mode_a_disabled_no_key")

    runtime = EngineRuntime(
        risk=risk,
        session_factory=factory,
        adapter=adapter,
        alert=alerter,
        strategies=[rotation.name, orb.name],
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
        if mode_a is not None:
            from plutus.ai.mode_a import regime_allocation_multiplier

            allocation *= regime_allocation_multiplier(mode_a.todays_regime())
        run_daily_rotation(
            risk,
            strategy=rotation,
            closes=closes,
            latest_prices=priced,
            allocation=allocation,
            mode_a=mode_a,
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
        run_orb_tick(risk, orb, bars, mode_a=mode_a)

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

    from apscheduler.events import EVENT_JOB_ERROR
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from plutus.scheduler import session_gated as _gate

    scheduler.add_job(
        _gate(rotation_job, clock=lambda: datetime.now(UTC)),
        CronTrigger(hour=15, minute=50, timezone="America/New_York"),
        id="rotation_1550",
    )
    scheduler.add_job(
        orb_job,
        CronTrigger(hour="9-15", minute="*", timezone="America/New_York"),
        id="orb_tick",
    )
    scheduler.add_job(fills_job, IntervalTrigger(seconds=20), id="fills_sync")
    scheduler.add_job(loss_watch_job, IntervalTrigger(seconds=60), id="loss_watch")

    if mode_a is not None:
        active_mode_a = mode_a  # narrowed binding for the job closures

        def brief_job() -> None:
            now = datetime.now(UTC)
            symbols = sorted(set(rotation.universe) | {"SPY", "QQQ"})
            closes_frames = {
                s: cache.get_bars(s, "1d", now - timedelta(days=10), now) for s in symbols
            }
            prior_closes: dict[str, float] = {}
            day_changes: dict[str, float] = {}
            for s, f in closes_frames.items():
                if len(f) >= 2:
                    prior_closes[s] = float(f["close"].iloc[-1])
                    day_changes[s] = float(f["close"].iloc[-1] / f["close"].iloc[-2] - 1)
            premarket = {
                s: p for s in ("SPY", "QQQ") if (p := latest_minute_price(s)) is not None
            }
            with factory() as session:
                positions = [
                    (r.strategy, r.symbol, float(r.qty))
                    for r in session.scalars(
                        select(BotPosition).where(BotPosition.qty != 0)
                    ).all()
                ]
            brief = active_mode_a.morning_brief(
                prior_closes=prior_closes,
                day_changes=day_changes,
                premarket=premarket,
                uvxy_level=latest_minute_price("UVXY"),
                positions=positions,
            )
            if brief is None:
                alerter("critical", "morning brief failed — regime defaults neutral")
            else:
                alerter("info", f"brief: {brief.regime} — {brief.notes[:200]}")

        def journal_job() -> None:
            from plutus.models import FillRecord

            now = datetime.now(UTC)
            day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
            with factory() as session:
                fills = session.scalars(
                    select(FillRecord).where(FillRecord.filled_at >= day_start)
                ).all()
                fills_text = "\n".join(
                    f"{f.strategy} {f.side} {float(f.qty)} {f.symbol} @ {float(f.price)}"
                    for f in fills
                ) or "no fills today"
                # §9: P&L ATTRIBUTION needs per-strategy numbers, not raw fills
                pnl_lines = []
                for st in session.scalars(select(StrategyState)).all():
                    if st.day_start_equity_usd is None:
                        continue
                    equity = strategy_equity(st.strategy)
                    if equity is not None:
                        delta = equity - float(st.day_start_equity_usd)
                        pnl_lines.append(f"{st.strategy}: {delta:+.2f} USD on the day")
            pnl_text = "\n".join(pnl_lines) or "no day-start marks"
            active_mode_a.journal(
                session_summary=(
                    f"Per-strategy day P&L:\n{pnl_text}\n\nToday's fills:\n{fills_text}"
                )
            )

        from plutus.scheduler import session_gated

        scheduler.add_job(
            session_gated(brief_job, clock=lambda: datetime.now(UTC)),
            CronTrigger(hour=8, minute=30, timezone="America/New_York"),
            id="brief_0830",
        )
        scheduler.add_job(
            session_gated(journal_job, clock=lambda: datetime.now(UTC)),
            CronTrigger(hour=16, minute=30, timezone="America/New_York"),
            id="journal_1630",
        )

    errored = {"flag": False}
    scheduler.add_listener(
        make_error_listener(alert=alerter, errored=errored), EVENT_JOB_ERROR
    )

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
        runtime.shutdown(
            clean=not errored["flag"],
            error="one or more scheduler jobs raised" if errored["flag"] else None,
        )
    except Exception as exc:
        alerter("critical", f"engine crashed: {exc}")
        runtime.shutdown(clean=False, error=str(exc))
        raise


if __name__ == "__main__":  # pragma: no cover
    main()
