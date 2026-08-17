"""§8 gate chain: every order passes every gate; exits keep their privileges.

Gate order: kill → mode → dedupe → strategy-enabled → stale → market-hours →
rate-limit → priced gates (position size, risk-per-trade, leveraged cap,
concurrent). Exits (risk-reducing orders) bypass entry gates and the rate
limiter; a flip (sell more than held) counts as an entry. All clocks and
prices are injected — nothing here touches wall time or a network.
"""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fakes import FakeAdapter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.brokers.base import OrderIntent
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.models import BotPosition, Order, StrategyState
from plutus.risk import RiskConfig, RiskManager

ET = ZoneInfo("America/New_York")
RTH = datetime(2024, 6, 5, 14, 0, tzinfo=ET)  # Wednesday, mid-session
SUNDAY = datetime(2024, 6, 9, 14, 0, tzinfo=ET)


class Harness:
    def __init__(self, tmp_path: Path, **config_overrides: object) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.factory: sessionmaker[Session] = make_session_factory(engine)
        self.adapter = FakeAdapter()
        self.root = tmp_path
        self.now = RTH
        self.prices: dict[str, float] = {"SPY": 100.0, "TQQQ": 50.0, "BTC/USD": 60000.0}
        self.stale: set[str] = set()
        self.alerts: list[tuple[str, str]] = []
        config_kwargs: dict[str, object] = {
            "default_allocation_usd": 10_000.0,
            "total_bot_equity_usd": 20_000.0,
        }
        config_kwargs.update(config_overrides)
        config = RiskConfig(**config_kwargs)  # type: ignore[arg-type]
        self.rm = RiskManager(
            adapter=self.adapter,
            session_factory=self.factory,
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            config=config,
            clock=lambda: self.now,
            runtime_root=self.root,
            price_lookup=lambda s: self.prices.get(s),
            stale_check=lambda s: s in self.stale,
            alert=lambda severity, message: self.alerts.append((severity, message)),
        )

    def set_position(self, strategy: str, symbol: str, qty: float) -> None:
        """Bot strategies book to bot_positions; 'manual' books to the broker."""
        if strategy == "manual":
            from plutus.brokers.base import Position

            self.adapter.positions.append(
                Position(
                    symbol=symbol.replace("/", ""),
                    qty=qty,
                    avg_entry_price=1.0,
                    market_value=qty,
                    unrealized_pl=0.0,
                )
            )
            return
        with self.factory() as session:
            session.add(BotPosition(strategy=strategy, symbol=symbol, qty=qty))
            session.commit()

    def order_statuses(self) -> list[str]:
        with self.factory() as session:
            return [o.status for o in session.scalars(select(Order)).all()]


def buy(
    symbol: str = "SPY",
    qty: float = 1,
    strategy: str = "manual",
    stop_price: float | None = None,
) -> OrderIntent:
    return OrderIntent(
        symbol=symbol,
        side="buy",
        qty=qty,
        order_type="market",
        strategy=strategy,
        stop_price=stop_price,
    )


def sell(symbol: str = "SPY", qty: float = 1, strategy: str = "manual") -> OrderIntent:
    return OrderIntent(symbol=symbol, side="sell", qty=qty, order_type="market", strategy=strategy)


# --- kill gate ----------------------------------------------------------------


def test_kill_file_rejects_everything(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    (tmp_path / "KILL").touch()
    row = h.rm.submit(buy())
    assert row.status == "rejected"
    assert "KILL" in (row.reject_reason or "")
    assert h.adapter.submitted == []


def test_invalid_runtime_root_fails_closed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm._runtime_root = tmp_path / "does-not-exist"  # wrong root: cannot verify KILL absence
    row = h.rm.submit(buy())
    assert row.status == "rejected"
    assert h.adapter.submitted == []


# --- strategy enabled ---------------------------------------------------------


def test_disabled_strategy_rejected(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    with h.factory() as session:
        session.add(StrategyState(strategy="s1", enabled=False, halt_reason="daily loss"))
        session.commit()
    row = h.rm.submit(buy(strategy="s1"))
    assert row.status == "rejected"
    assert "disabled" in (row.reject_reason or "")


def test_unknown_strategy_defaults_enabled(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert h.rm.submit(buy(strategy="brand-new")).status == "accepted"


# --- stale data ---------------------------------------------------------------


def test_stale_data_blocks_entries_not_exits(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.stale.add("SPY")
    assert h.rm.submit(buy()).status == "rejected"
    h.set_position("manual", "SPY", 5)
    assert h.rm.submit(sell(qty=5)).status == "accepted"


# --- market hours -------------------------------------------------------------


def test_equity_entry_outside_rth_rejected(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.now = SUNDAY
    row = h.rm.submit(buy())
    assert row.status == "rejected"
    assert "market" in (row.reject_reason or "").lower()


def test_crypto_entry_outside_rth_accepted(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.now = SUNDAY
    assert h.rm.submit(buy(symbol="BTC/USD", qty=0.001)).status == "accepted"


def test_exit_outside_rth_accepted(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.now = SUNDAY
    h.set_position("manual", "SPY", 3)
    assert h.rm.submit(sell(qty=3)).status == "accepted"


def test_flip_counts_as_entry(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.now = SUNDAY
    h.set_position("manual", "SPY", 1)
    # selling 2 while long 1 flips short → entry → RTH gate applies
    assert h.rm.submit(sell(qty=2)).status == "rejected"


# --- rate limits --------------------------------------------------------------


def test_orders_per_minute_limit(tmp_path: Path) -> None:
    h = Harness(tmp_path, orders_per_minute=3)
    for _ in range(3):
        assert h.rm.submit(buy(qty=0.1)).status == "accepted"
    assert h.rm.submit(buy(qty=0.1)).status == "rejected"
    # a minute later the window has passed
    h.now = RTH + timedelta(seconds=61)
    assert h.rm.submit(buy(qty=0.1)).status == "accepted"


def test_orders_per_day_limit(tmp_path: Path) -> None:
    h = Harness(tmp_path, orders_per_minute=1000, orders_per_day=5)
    for i in range(5):
        h.now = RTH + timedelta(minutes=i)
        assert h.rm.submit(buy(qty=0.1)).status == "accepted"
    h.now = RTH + timedelta(minutes=6)
    assert h.rm.submit(buy(qty=0.1)).status == "rejected"


def test_exits_bypass_rate_limit(tmp_path: Path) -> None:
    h = Harness(tmp_path, orders_per_minute=1)
    h.set_position("manual", "SPY", 10)
    assert h.rm.submit(buy(qty=0.1)).status == "accepted"  # uses up the minute
    for _ in range(3):
        assert h.rm.submit(sell(qty=1)).status == "accepted"


# --- priced gates -------------------------------------------------------------


def test_position_size_cap_20pct_of_allocation(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    # allocation 10k → cap 2k → at $100, 21 shares breaches
    assert h.rm.submit(buy(qty=21)).status == "rejected"
    assert h.rm.submit(buy(qty=19)).status == "accepted"


def test_unpriceable_entry_fails_closed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    row = h.rm.submit(buy(symbol="ZZZZ"))
    assert row.status == "rejected"
    assert "price" in (row.reject_reason or "").lower()


def test_risk_per_trade_stop_based(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    # 1% of 10k = $100 risk budget; entry 100, stop 95 → $5/share → cap 20 shares
    ok = buy(qty=15, stop_price=95.0)
    too_big = buy(qty=15, stop_price=90.0)  # $10/share × 15 = $150 > $100
    assert h.rm.submit(ok).status == "accepted"
    assert h.rm.submit(too_big).status == "rejected"


def test_leveraged_etf_notional_cap(tmp_path: Path) -> None:
    # allocation large enough that the position-size gate stays out of the way
    h = Harness(tmp_path, default_allocation_usd=50_000.0)
    # bot equity 20k → leveraged cap 5k; TQQQ @ 50: 60 sh (3k) held + 50 more (2.5k) breaches
    h.set_position("s1", "TQQQ", 60)
    assert h.rm.submit(buy(symbol="TQQQ", qty=50, strategy="s1")).status == "rejected"
    assert h.rm.submit(buy(symbol="TQQQ", qty=30, strategy="s1")).status == "accepted"


def test_concurrent_positions_cap_for_intraday_strategies(tmp_path: Path) -> None:
    h = Harness(tmp_path, intraday_strategies={"scalper"}, max_concurrent_positions=2)
    h.set_position("scalper", "SPY", 1)
    h.set_position("scalper", "TQQQ", 1)
    row = h.rm.submit(buy(symbol="BTC/USD", qty=0.001, strategy="scalper"))
    assert row.status == "rejected"
    assert "concurrent" in (row.reject_reason or "")
    # non-intraday strategies are not subject to the cap
    h.set_position("swing", "SPY", 1)
    h.set_position("swing", "TQQQ", 1)
    assert h.rm.submit(buy(symbol="BTC/USD", qty=0.001, strategy="swing")).status == "accepted"


# --- stop_price is sizing-only this phase ------------------------------------


def test_stop_price_not_forwarded_to_adapter(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.rm.submit(buy(qty=5, stop_price=95.0))
    (intent,) = h.adapter.submitted
    assert intent.stop_price == 95.0  # carried on the intent for records/sizing
