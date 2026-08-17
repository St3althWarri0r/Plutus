"""Market-hours jobs (§8) on APScheduler: reconcile at 09:15/16:15 ET,
day-start equity mark at 09:30, intraday auto-flatten at 15:55, and a
periodic daily-loss check through the trading day.

build_scheduler returns an UNSTARTED BackgroundScheduler — starting it is an
explicit deployment decision (never done by create_app), so tests and one-off
scripts can never spawn a live scheduler by accident. Jobs skip non-session
days via the exchange calendar.
"""

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from plutus.logging_setup import get_logger
from plutus.market_calendar import MarketCalendar
from plutus.risk import RiskManager

log = get_logger("plutus.scheduler")

ET = "America/New_York"


def session_gated(
    fn: Callable[[], None],
    *,
    clock: Callable[[], "datetime"],
    calendar: MarketCalendar | None = None,
) -> Callable[[], None]:
    """Wrap a job so it only runs on NYSE session days — for the engine's own
    crons (rotation/brief/journal). Weekend firings would false-alarm
    (Saturday 15:50 'unpriceable' criticals) and spend AI dollars on nothing.
    Deliberately NOT used for fills_sync (crypto fills 24/7) or loss_watch
    (self-quiets when unpriceable)."""
    cal = calendar or MarketCalendar()

    def wrapped() -> None:
        if not cal.is_session_day(clock()):
            log.info("job_skipped_non_session")
            return
        fn()

    return wrapped


def build_scheduler(
    risk: RiskManager,
    equity_lookup: Callable[[str], float | None],
    strategies: list[str],
    calendar: MarketCalendar | None = None,
) -> BackgroundScheduler:
    cal = calendar or MarketCalendar()
    scheduler = BackgroundScheduler(timezone=ET)

    def on_session(fn: Callable[[], None]) -> Callable[[], None]:
        def wrapped() -> None:
            if not cal.is_session_day(risk._clock()):
                log.info("job_skipped_non_session")
                return
            fn()

        return wrapped

    def reconcile() -> None:
        risk.reconcile()

    def day_start_mark() -> None:
        for strategy in strategies:
            equity = equity_lookup(strategy)
            if equity is not None:
                risk.mark_day_start(strategy, equity=equity)

    def flatten_intraday() -> None:
        # routine end-of-day flatten: closes positions but NEVER touches
        # enable state — a daily-loss halt from earlier today must survive
        for strategy in sorted(risk.config.intraday_strategies):
            risk.flatten_strategy(strategy, reason="15:55 ET auto-flatten", disable=False)

    def daily_loss_check() -> None:
        # before the 09:30 mark the reference is yesterday's — never compare
        now_et = risk._clock().astimezone(ZoneInfo(ET))
        if (now_et.hour, now_et.minute) < (9, 30) or now_et.hour >= 16:
            return
        for strategy in strategies:
            equity = equity_lookup(strategy)
            if equity is not None:
                risk.check_daily_loss(strategy, current_equity=equity)

    scheduler.add_job(
        on_session(reconcile), CronTrigger(hour=9, minute=15, timezone=ET), id="reconcile_am"
    )
    scheduler.add_job(
        on_session(day_start_mark), CronTrigger(hour=9, minute=30, timezone=ET),
        id="day_start_mark",
    )
    scheduler.add_job(
        on_session(daily_loss_check),
        CronTrigger(hour="9-16", minute="*/5", timezone=ET),
        id="daily_loss_check",
    )
    scheduler.add_job(
        on_session(flatten_intraday), CronTrigger(hour=15, minute=55, timezone=ET),
        id="flatten_intraday",
    )
    scheduler.add_job(
        on_session(reconcile), CronTrigger(hour=16, minute=15, timezone=ET), id="reconcile_pm"
    )
    return scheduler
