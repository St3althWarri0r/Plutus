"""Fill-based position tracking, scoped reconciliation, daily-loss halt.

DB is the source of intent, broker is the source of truth (§11). Positions
update on confirmed FILLS, never on acceptance (an accepted-but-unfilled
order must not create a phantom position). Reconciliation only halts on
mismatches in symbols the bot owns; unknown broker positions (the human's
own holdings) alert without halting; symbols with in-flight orders are
resolved, not halted on.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fakes import FakeAdapter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import OrderIntent, OrderStatus, Position
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.models import BotPosition, Order, StrategyState
from plutus.risk import RiskConfig, RiskManager

ET = ZoneInfo("America/New_York")
RTH = datetime(2024, 6, 5, 14, 0, tzinfo=ET)


class Harness:
    def __init__(self, tmp_path: Path, **config_overrides: object) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.factory: sessionmaker[Session] = make_session_factory(engine)
        self.adapter = FakeAdapter()
        self.alerts: list[tuple[str, str]] = []
        kwargs: dict[str, object] = {"default_allocation_usd": 10_000.0}
        kwargs.update(config_overrides)
        self.rm = RiskManager(
            adapter=self.adapter,
            session_factory=self.factory,
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            config=RiskConfig(**kwargs),  # type: ignore[arg-type]
            clock=lambda: RTH,
            runtime_root=tmp_path,
            price_lookup=lambda s: 100.0,
            alert=lambda severity, message: self.alerts.append((severity, message)),
        )

    def bot_qty(self, strategy: str, symbol: str) -> float:
        with self.factory() as session:
            row = session.scalars(
                select(BotPosition).where(
                    BotPosition.strategy == strategy, BotPosition.symbol == symbol
                )
            ).one_or_none()
            return float(row.qty) if row else 0.0

    def strategy_enabled(self, strategy: str) -> bool:
        with self.factory() as session:
            row = session.scalars(
                select(StrategyState).where(StrategyState.strategy == strategy)
            ).one_or_none()
            return row.enabled if row else True

    def broker_holds(self, symbol: str, qty: float) -> None:
        self.adapter.positions.append(
            Position(
                symbol=symbol, qty=qty, avg_entry_price=1.0, market_value=qty, unrealized_pl=0.0
            )
        )


# --- fills --------------------------------------------------------------------


def test_record_fill_upserts_and_accumulates(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.record_fill("s1", "SPY", 5)
    h.rm.record_fill("s1", "SPY", -2)
    assert h.bot_qty("s1", "SPY") == 3


def test_sync_fills_updates_positions_on_fill_not_acceptance(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    row = h.rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=4, order_type="market", strategy="s1")
    )
    assert row.status == "accepted"
    assert h.bot_qty("s1", "SPY") == 0.0  # accepted ≠ filled

    assert row.broker_order_id is not None
    h.adapter.status_by_broker_id[row.broker_order_id] = OrderStatus.FILLED
    h.rm.sync_fills()

    assert h.bot_qty("s1", "SPY") == 4.0
    with h.factory() as session:
        assert session.scalars(select(Order)).one().status == "filled"


# --- reconciliation -----------------------------------------------------------


def test_reconcile_mismatch_halts_owner_and_alerts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.record_fill("s1", "SPY", 5)
    h.broker_holds("SPY", 3)  # broker disagrees

    report = h.rm.reconcile()

    assert report.mismatches and report.mismatches[0]["symbol"] == "SPY"
    assert not h.strategy_enabled("s1")
    assert any(sev == "critical" for sev, _ in h.alerts)


def test_reconcile_clean_book_no_halt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.record_fill("s1", "SPY", 5)
    h.broker_holds("SPY", 5)
    report = h.rm.reconcile()
    assert report.mismatches == []
    assert h.strategy_enabled("s1")


def test_unknown_broker_position_alerts_without_halt(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.broker_holds("LINKUSD", 20_788.02)  # the human's own holding
    report = h.rm.reconcile()
    assert report.mismatches == []
    assert "LINKUSD" in report.unknown
    assert any(sev == "warning" for sev, _ in h.alerts)


def test_pending_order_symbol_is_inflight_not_mismatch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    row = h.rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=4, order_type="market", strategy="s1")
    )
    assert row.status == "accepted"  # still pending at the broker, no fill yet
    report = h.rm.reconcile()
    assert report.mismatches == []
    assert "SPY" in report.in_flight
    assert h.strategy_enabled("s1")


def test_reconcile_normalizes_crypto_pair_symbols(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.record_fill("s1", "BTC/USD", 0.001)
    h.broker_holds("BTCUSD", 0.001)  # broker reports without the slash
    report = h.rm.reconcile()
    assert report.mismatches == []


# --- manual baseline ----------------------------------------------------------


def test_baseline_absorbs_human_holdings_no_more_warnings(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.broker_holds("LINKUSD", 20_788.02)
    h.broker_holds("BTCUSD", 0.001)

    h.rm.mark_manual_baseline()
    report = h.rm.reconcile()

    assert report.mismatches == []
    assert report.unknown == []
    assert h.alerts == []  # the fatigue fix: known human holdings stay silent


def test_baseline_is_broker_minus_bot_book(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.record_fill("s1", "SPY", 5)
    h.broker_holds("SPY", 7)  # 5 bot + 2 human

    h.rm.mark_manual_baseline()
    assert h.rm.reconcile().mismatches == []

    # bot buys 1 more (filled): broker 8, bot 6, baseline 2 → still clean
    h.rm.record_fill("s1", "SPY", 1)
    h.adapter.positions[0].qty = 8
    assert h.rm.reconcile().mismatches == []

    # broker loses a share the bot thinks it owns → mismatch
    h.adapter.positions[0].qty = 7
    assert h.rm.reconcile().mismatches != []


def test_monday_spy_trace_pending_then_fill_stays_clean(tmp_path: Path) -> None:
    """The Phase 4 flagged collision: manual SPY order pending over the
    weekend, baseline marked at engine start, order fills Monday."""
    h = Harness(tmp_path)
    h.broker_holds("LINKUSD", 100.0)
    row = h.rm.submit(
        OrderIntent(symbol="SPY", side="buy", qty=1, order_type="market", strategy="manual")
    )
    assert row.broker_order_id is not None

    h.rm.mark_manual_baseline()  # engine start: SPY pending, not yet held
    first = h.rm.reconcile()
    assert first.mismatches == [] and "SPY" in first.in_flight

    # Monday: the order fills; broker now holds it
    h.adapter.status_by_broker_id[row.broker_order_id] = OrderStatus.FILLED
    h.broker_holds("SPY", 1)
    second = h.rm.reconcile()  # sync_fills books it under 'manual' in bot book
    assert second.mismatches == []
    assert not any(sev == "critical" for sev, _ in h.alerts)


def test_crypto_precision_survives_baseline_round_trip(tmp_path: Path) -> None:
    """Regression (found by live smoke): broker reports LINKUSD to 9+ decimal
    places; column rounding must not manufacture a reconcile mismatch."""
    h = Harness(tmp_path)
    h.broker_holds("LINKUSD", 20_788.024399992)
    h.rm.mark_manual_baseline()

    report = h.rm.reconcile()

    assert report.mismatches == []
    assert not any(sev == "critical" for sev, _ in h.alerts)


def test_new_unknown_position_after_baseline_still_alerts(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.mark_manual_baseline()
    h.broker_holds("GME", 42.0)  # appeared after the mark
    report = h.rm.reconcile()
    assert "GME" in report.unknown
    assert any(sev == "warning" for sev, _ in h.alerts)


# --- daily-loss halt ----------------------------------------------------------


def test_daily_loss_breach_flattens_and_disables_unthrottled(tmp_path: Path) -> None:
    # rate limit 1/min: flatten of 3 positions must not be throttled
    h = Harness(tmp_path, orders_per_minute=1)
    h.rm.record_fill("s1", "SPY", 5)
    h.rm.record_fill("s1", "TQQQ", 2)
    h.rm.record_fill("s1", "BTC/USD", 0.01)
    h.rm.mark_day_start("s1", equity=10_000.0)

    h.rm.check_daily_loss("s1", current_equity=9_600.0)  # −4% > 3% halt

    assert not h.strategy_enabled("s1")
    flattened = {(o.symbol, o.side) for o in h.adapter.submitted}
    assert flattened == {("SPY", "sell"), ("TQQQ", "sell"), ("BTC/USD", "sell")}
    assert any(sev == "critical" for sev, _ in h.alerts)


def test_daily_loss_within_band_no_action(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.record_fill("s1", "SPY", 5)
    h.rm.mark_day_start("s1", equity=10_000.0)
    h.rm.check_daily_loss("s1", current_equity=9_800.0)  # −2%
    assert h.strategy_enabled("s1")
    assert h.adapter.submitted == []


def test_flatten_adapter_failure_alerts_and_disables_without_crash(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.record_fill("s1", "SPY", 5)
    h.rm.mark_day_start("s1", equity=10_000.0)
    h.adapter.fail_with = RuntimeError("alpaca 500")

    h.rm.check_daily_loss("s1", current_equity=9_000.0)  # breach; flatten will fail

    assert not h.strategy_enabled("s1")
    assert any("flatten" in msg.lower() for _, msg in h.alerts)
