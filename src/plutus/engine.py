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

    # --- Mode B (§9B): requires the AI key; hard-locked to paper ---------------
    if mode_a is not None and settings.anthropic_api_key:
        from plutus.ai.discipline import Discipline, OpenPosition
        from plutus.ai.mode_b import (
            ModeB,
            ModeBExecutor,
            assert_mode_b_paper_locked,
        )
        from plutus.ai.mode_b_accounting import (
            p95_decision_latency_ms,
            session_pnl_r,
            sync_mode_b_trades,
        )
        from plutus.ai.mode_b_config import load_playbook as _load_playbook
        from plutus.ai.packet import build_state_packet
        from plutus.ai.scanner import Candidate, filter_candidates
        from plutus.models import ModeBTrade

        assert_mode_b_paper_locked(effective_mode=effective_trading_mode())
        playbook = _load_playbook(resolve_runtime_root(settings) / "playbook.yaml")
        risk.config.intraday_strategies.add("mode_b")
        risk.config.max_position_pct_overrides.setdefault("mode_b", 1.0)
        mode_b = ModeB(
            client=ai_client, session_factory=factory, playbook=playbook
        )
        with factory() as session:
            mb_state = session.scalars(
                select(StrategyState).where(StrategyState.strategy == "mode_b")
            ).one_or_none()
            mb_allocation = (
                float(mb_state.allocation_usd)
                if mb_state is not None and float(mb_state.allocation_usd) > 0
                else risk.config.default_allocation_usd
            )
        discipline = Discipline(playbook.mode_b, allocation_usd=mb_allocation)
        executor = ModeBExecutor(
            mode_b=mode_b,
            discipline=discipline,
            risk=risk,
            adapter=adapter,
            alert=alerter,
        )

        def _open_mode_b_positions() -> list[OpenPosition]:
            with factory() as session:
                rows = session.scalars(
                    select(ModeBTrade).where(ModeBTrade.closed_at.is_(None))
                ).all()
                return [
                    OpenPosition(
                        symbol=t.symbol,
                        qty=float(t.qty),
                        entry=float(t.entry_price),
                        stop=float(t.stop_price),
                        target=float(t.entry_price)
                        + 2 * abs(float(t.entry_price) - float(t.stop_price)),
                        setup=t.setup,
                        off_plan=t.off_plan,
                        opened_at=t.opened_at,
                    )
                    for t in rows
                ]

        def _prices_for(symbols: set[str]) -> dict[str, float]:
            out: dict[str, float] = {}
            for s in symbols:
                p = latest_minute_price(s)
                if p is not None:
                    out[s] = p
            return out

        def _planned_symbols() -> set[str]:
            plan = mode_b.load_plan()
            return {e.symbol for e in plan.watchlist} if plan is not None else set()

        def _mode_b_packet(symbol: str) -> str | None:
            now = datetime.now(UTC)
            session_open = datetime(now.year, now.month, now.day, 13, 30, tzinfo=UTC)
            try:
                bars_1m = provider.get_bars(symbol, "1m", session_open, now)
            except Exception:
                return None
            if not len(bars_1m):
                return None
            bars_5m = bars_1m.resample("5min").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last",
                 "volume": "sum"}
            ).dropna()
            typical = (bars_1m["high"] + bars_1m["low"] + bars_1m["close"]) / 3
            vwap = float(
                (typical * bars_1m["volume"]).sum() / max(bars_1m["volume"].sum(), 1)
            )
            closes = bars_1m["close"]
            ema9 = float(closes.ewm(span=9, adjust=False).mean().iloc[-1])
            ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
            positions = _open_mode_b_positions()
            mine = next((p for p in positions if p.symbol == symbol), None)
            tape, counters = mode_b.load_state()
            plan = mode_b.load_plan()
            prices = _prices_for({symbol})
            pnl = session_pnl_r(
                factory,
                r_dollars=discipline.r_dollars(),
                current_prices=prices,
                today=now.astimezone(ET).date(),
            )
            return build_state_packet(
                symbol=symbol,
                bars_1m=bars_1m,
                bars_5m=bars_5m,
                vwap=vwap,
                rvol=None,
                ema9=ema9,
                ema20=ema20,
                premarket_high=None,
                premarket_low=None,
                prior_high=None,
                prior_low=None,
                prior_close=None,
                position=(
                    {"qty": mine.qty, "entry": mine.entry, "stop": mine.stop}
                    if mine
                    else None
                ),
                open_orders=[],
                session_pnl_r=pnl,
                trades_today=counters.round_trips,
                time_et=now.astimezone(ET).strftime("%H:%M"),
                day_plan=plan.model_dump_json() if plan else "no plan",
                tape=tape,
            )

        def mode_b_plan_job() -> None:
            candidates: list[Candidate] = []
            for symbol in playbook.scanner.static_candidates:
                try:
                    now = datetime.now(UTC)
                    daily = cache.get_bars(symbol, "1d", now - timedelta(days=40), now)
                    if len(daily) < 2:
                        continue
                    prior_close = float(daily["close"].iloc[-1])
                    pm_start = datetime(now.year, now.month, now.day, 8, 0, tzinfo=UTC)
                    pm = provider.get_bars(symbol, "1m", pm_start, now)
                    if not len(pm):
                        continue
                    last = float(pm["close"].iloc[-1])
                    candidates.append(
                        Candidate(
                            symbol=symbol,
                            gap_pct=(last / prior_close - 1) * 100,
                            premarket_volume=float(pm["volume"].sum()),
                            price=last,
                            adv_iex=float(daily["volume"].tail(20).mean()),
                        )
                    )
                except Exception as exc:
                    log.warning("scan_symbol_failed", symbol=symbol, error=str(exc))
            watchlist = filter_candidates(candidates, playbook.scanner)[
                : playbook.scanner.max_watchlist
            ]
            text = "\n".join(
                f"{c.symbol}: gap {c.gap_pct:+.1f}%, pm vol {int(c.premarket_volume)} "
                f"(IEX), ${c.price:.2f}"
                for c in watchlist
            ) or "no candidates passed filters"
            regime = mode_a.todays_regime() if mode_a else "neutral"
            plan = mode_b.plan_day(
                candidates_text=text, context=f"Mode A regime: {regime}"
            )
            if plan is None:
                alerter("critical", "Mode B day plan failed — standing down today")
            else:
                alerter(
                    "info",
                    f"Mode B plan: {[e.symbol for e in plan.watchlist]}",
                )

        def mode_b_monitor_job() -> None:
            """Per-minute: Haiku monitor per watchlist name; escalate to a
            Sonnet decision only when actionable (§9B.6 tiering)."""
            now_et = datetime.now(UTC).astimezone(ET)
            if (now_et.hour, now_et.minute) < (9, 30):
                return
            tape, counters = mode_b.load_state()
            positions = _open_mode_b_positions()
            symbols = _planned_symbols() - {p.symbol for p in positions}
            for symbol in sorted(symbols):
                packet = _mode_b_packet(symbol)
                if packet is None:
                    continue
                verdict = mode_b.monitor(packet)
                if verdict is None or not verdict.actionable:
                    continue
                decision = mode_b.decide(packet)
                if decision is None:
                    continue
                prices = _prices_for({symbol})
                executor.execute(
                    decision,
                    counters=counters,
                    open_positions=positions,
                    session_pnl_r=session_pnl_r(
                        factory,
                        r_dollars=discipline.r_dollars(),
                        current_prices=prices,
                        today=now_et.date(),
                    ),
                    planned_symbols=_planned_symbols(),
                    current_prices=prices,
                )
            # persist the MUTATED counters (executor increments off_plan_used)
            # — reloading here would silently discard the off-plan budget
            mode_b.save_state(mode_b.load_state()[0], counters)

        def mode_b_position_job() -> None:
            """Every 15s: deterministic mechanics first (§9B.4 — no AI in the
            loop), then closures, daily stop, and an AI check per position."""
            positions = _open_mode_b_positions()
            _, counters = mode_b.load_state()
            if not positions:
                sync_mode_b_trades(
                    factory, discipline=discipline, counters=counters,
                    now=datetime.now(UTC),
                )
                mode_b.save_state(mode_b.load_state()[0], counters)
                return
            prices = _prices_for({p.symbol for p in positions})
            for position in positions:
                price = prices.get(position.symbol)
                if price is None:
                    continue
                for action in discipline.forced_actions(
                    position, current_price=price, counters=counters
                ):
                    if action.kind == "move_stop_breakeven":
                        parent = executor._find_parent_order_id(position.symbol)
                        if parent is not None and action.new_stop is not None:
                            executor.move_stop(
                                position.symbol,
                                parent_order_id=parent,
                                new_stop=action.new_stop,
                            )
                            counters.breakeven_done.append(position.symbol)
                    elif action.kind == "scale_out" and action.qty >= 1:
                        row = risk.submit(
                            OrderIntent(
                                symbol=position.symbol,
                                side="sell" if position.qty > 0 else "buy",
                                qty=action.qty,
                                order_type="market",
                                strategy="mode_b",
                            )
                        )
                        if row.status == "rejected":
                            # the MANDATORY +2R scale-out failed — likely the
                            # bracket legs reserve the shares; needs the
                            # reduce-legs-then-sell sequence (Monday probe)
                            alerter(
                                "critical",
                                f"mandatory scale-out REJECTED for "
                                f"{position.symbol}: {row.reject_reason} — "
                                "will retry next tick",
                            )
                        else:
                            counters.scale_out_done.append(position.symbol)
            now = datetime.now(UTC)
            sync_mode_b_trades(
                factory, discipline=discipline, counters=counters, now=now
            )
            pnl = session_pnl_r(
                factory,
                r_dollars=discipline.r_dollars(),
                current_prices=prices,
                today=now.astimezone(ET).date(),
            )
            if pnl <= playbook.mode_b.daily_stop_r:
                alerter(
                    "critical",
                    f"Mode B daily stop hit ({pnl:.2f}R) — flattening, done for the day",
                )
                risk.flatten_strategy("mode_b", reason=f"daily stop ({pnl:.2f}R)")
            mode_b.save_state(mode_b.load_state()[0], counters)

        def mode_b_journal_job() -> None:
            now = datetime.now(UTC)
            today = now.astimezone(ET).date()
            with factory() as session:
                trades = session.scalars(
                    select(ModeBTrade).where(ModeBTrade.session_date == today)
                ).all()
                lines = [
                    f"{t.symbol} {t.setup}{' (off-plan)' if t.off_plan else ''}: "
                    f"{float(t.realized_r):+.2f}R"
                    if t.realized_r is not None
                    else f"{t.symbol} {t.setup}: still open"
                    for t in trades
                ]
            tape, _ = mode_b.load_state()
            mode_b.journal(
                "Trades:\n" + ("\n".join(lines) or "none") + f"\n\nTape:\n{tape}"
            )
            p95 = p95_decision_latency_ms(factory, today=today)
            if p95 is not None and p95 > 12_000:
                alerter("warning", f"Mode B p95 decision latency {p95}ms (> 12s budget)")

        scheduler.add_job(
            session_gated(mode_b_plan_job, clock=lambda: datetime.now(UTC)),
            CronTrigger(hour=8, minute=50, timezone="America/New_York"),
            id="mode_b_plan_0850",
        )
        scheduler.add_job(
            mode_b_monitor_job,
            CronTrigger(
                hour="9-15", minute="*", timezone="America/New_York"
            ),
            id="mode_b_monitor",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            mode_b_position_job,
            IntervalTrigger(seconds=15),
            id="mode_b_positions",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            session_gated(mode_b_journal_job, clock=lambda: datetime.now(UTC)),
            CronTrigger(hour=16, minute=15, timezone="America/New_York"),
            id="mode_b_journal_1615",
        )
        log.info("mode_b_enabled", allocation=mb_allocation)

    def nightly_snapshot_job() -> None:
        # deliberately DAILY, not session-gated: net worth exists on weekends
        # and the crypto positions move then
        from plutus.aggregation import snapshot_alpaca
        from plutus.plaid_sync import plaid_sync_from_settings

        snapshot_alpaca(factory, adapter=adapter, mode=effective_trading_mode())
        try:
            plaid = plaid_sync_from_settings(settings, factory)
            if plaid is not None:
                for institution in ("m1", "vanguard"):
                    plaid.sync_holdings(institution)
        except Exception as exc:
            log.warning("nightly_plaid_failed", error=str(exc))

    scheduler.add_job(
        nightly_snapshot_job,
        CronTrigger(hour=16, minute=20, timezone="America/New_York"),
        id="nightly_snapshots",
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
