"""§9B.4 day-trader discipline — every rule in code, none in prompts."""

from datetime import UTC, datetime, timedelta

from pytest import approx

from plutus.ai.discipline import Discipline, EntryCheck, OpenPosition, SessionCounters
from plutus.ai.mode_b_config import ModeBConfig

NOW = datetime(2026, 8, 17, 14, 30, tzinfo=UTC)  # 10:30 ET Monday


def make_discipline(allocation: float = 25_000.0) -> Discipline:
    return Discipline(ModeBConfig(), allocation_usd=allocation)


def pos(
    symbol: str = "NVDA", entry: float = 100.0, stop: float = 99.0, qty: float = 100
) -> OpenPosition:
    return OpenPosition(
        symbol=symbol, qty=qty, entry=entry, stop=stop, target=104.0,
        setup="gap_and_go", off_plan=False, opened_at=NOW,
    )


def check(
    d: Discipline,
    counters: SessionCounters | None = None,
    positions: list[OpenPosition] | None = None,
    session_pnl_r: float = 0.0,
    now: datetime = NOW,
    symbol: str = "NVDA",
    planned: bool = True,
    current_price: float | None = None,
) -> EntryCheck:
    return d.check_entry(
        symbol=symbol,
        counters=counters or SessionCounters(),
        open_positions=positions or [],
        session_pnl_r=session_pnl_r,
        now=now,
        planned=planned,
        current_price=current_price,
    )


def test_r_dollars_is_075_pct_of_allocation() -> None:
    assert make_discipline(25_000.0).r_dollars() == approx(187.50)


def test_position_size_r_over_risk() -> None:
    d = make_discipline(25_000.0)
    # entry 100, stop 98 → $2/share → floor(187.5/2) = 93
    assert d.size_for_entry(entry=100.0, stop=98.0, off_plan=False) == 93
    # off-plan at half size
    assert d.size_for_entry(entry=100.0, stop=98.0, off_plan=True) == 46


def test_entry_allowed_baseline() -> None:
    assert check(make_discipline()).allowed


def test_max_concurrent_two() -> None:
    result = check(
        make_discipline(),
        positions=[pos("NVDA"), pos("TSLA")],
        symbol="AMD",
    )
    assert not result.allowed and "concurrent" in result.reason


def test_round_trip_cap_eight() -> None:
    counters = SessionCounters(round_trips=8)
    result = check(make_discipline(), counters=counters)
    assert not result.allowed and "round trip" in result.reason


def test_daily_stop_blocks_entries() -> None:
    result = check(make_discipline(), session_pnl_r=-2.6)
    assert not result.allowed and "daily stop" in result.reason


def test_cooldown_blocks_until_expiry() -> None:
    counters = SessionCounters(cooldown_until=NOW + timedelta(minutes=10))
    assert not check(make_discipline(), counters=counters).allowed
    counters = SessionCounters(cooldown_until=NOW - timedelta(minutes=1))
    assert check(make_discipline(), counters=counters).allowed


def test_no_entries_after_1530_et() -> None:
    late = datetime(2026, 8, 17, 19, 31, tzinfo=UTC)  # 15:31 ET
    assert not check(make_discipline(), now=late).allowed


def test_adding_to_loser_rejected_always() -> None:
    losing = pos("NVDA", entry=100.0)
    result = check(
        make_discipline(), positions=[losing], symbol="NVDA", current_price=99.2
    )
    assert not result.allowed and "loser" in result.reason
    # adding to a winner is not this rule's business
    result = check(
        make_discipline(), positions=[losing], symbol="NVDA", current_price=101.5
    )
    assert result.allowed


def test_off_plan_budget_one_per_day() -> None:
    d = make_discipline()
    first = check(d, planned=False)
    assert first.allowed and first.off_plan
    counters = SessionCounters(off_plan_used=1)
    second = check(d, counters=counters, planned=False)
    assert not second.allowed and "off-plan" in second.reason


def test_consecutive_full_r_losses_trigger_cooldown() -> None:
    d = make_discipline()
    counters = SessionCounters()
    d.on_trade_closed(counters, realized_r=-1.0, now=NOW)
    assert counters.cooldown_until is None
    d.on_trade_closed(counters, realized_r=-1.02, now=NOW)
    assert counters.cooldown_until == NOW + timedelta(minutes=30)
    assert counters.round_trips == 2


def test_winner_resets_consecutive_losses() -> None:
    d = make_discipline()
    counters = SessionCounters()
    d.on_trade_closed(counters, realized_r=-1.0, now=NOW)
    d.on_trade_closed(counters, realized_r=0.5, now=NOW)
    d.on_trade_closed(counters, realized_r=-1.0, now=NOW)
    assert counters.cooldown_until is None  # never two in a row


def test_partial_loss_does_not_count_as_full_r() -> None:
    d = make_discipline()
    counters = SessionCounters()
    d.on_trade_closed(counters, realized_r=-0.4, now=NOW)
    d.on_trade_closed(counters, realized_r=-0.5, now=NOW)
    assert counters.cooldown_until is None


def test_forced_actions_breakeven_then_scale_out() -> None:
    d = make_discipline()
    position = pos("NVDA", entry=100.0, stop=99.0)  # 1R = $1
    counters = SessionCounters()

    none_yet = d.forced_actions(position, current_price=100.5, counters=counters)
    assert none_yet == []

    at_1r = d.forced_actions(position, current_price=101.0, counters=counters)
    assert [a.kind for a in at_1r] == ["move_stop_breakeven"]

    # once done, not repeated
    counters.breakeven_done.append("NVDA")
    at_2r = d.forced_actions(position, current_price=102.0, counters=counters)
    assert [a.kind for a in at_2r] == ["scale_out"]
    assert at_2r[0].qty == approx(34.0)  # ≥1/3 of 100, floored to whole shares

    counters.scale_out_done.append("NVDA")
    assert d.forced_actions(position, current_price=103.0, counters=counters) == []
