"""Mode B core: plan/monitor/decision schemas, tape persistence + compaction,
executor (bracket-enforced entries through discipline + RiskManager), forced
mechanics via the adapter, closure accounting, paper lock."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fakes import FakeAdapter
from pytest import raises
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.ai.client import AiClient
from plutus.ai.discipline import Discipline, SessionCounters
from plutus.ai.mode_b import (
    DayPlanResult,
    ModeB,
    ModeBDecision,
    ModeBExecutor,
)
from plutus.ai.mode_b_config import load_playbook
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.models import DayPlan, ModeBTrade
from plutus.risk import RiskConfig, RiskManager

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 17, 14, 30, tzinfo=UTC)  # 10:30 ET Monday
PLAYBOOK = load_playbook(Path(__file__).resolve().parent.parent / "playbook.yaml")


class ScriptedTransport:
    def __init__(self, script: list) -> None:  # type: ignore[type-arg]
        self.script = list(script)
        self.kwargs_seen: list[dict] = []  # type: ignore[type-arg]

    def __call__(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.kwargs_seen.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item

        class Usage:
            input_tokens = 500
            output_tokens = 100

        class Block:
            type = "tool_use"

            def __init__(self, payload: dict) -> None:  # type: ignore[type-arg]
                self.input = payload

        class Response:
            def __init__(self, payload: dict) -> None:  # type: ignore[type-arg]
                self.content = [Block(payload)]
                self.usage = Usage()

        return Response(item)


class Env:
    def __init__(self, tmp_path: Path, script: list) -> None:  # type: ignore[type-arg]
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.factory: sessionmaker[Session] = make_session_factory(engine)
        self.adapter = FakeAdapter()
        self.alerts: list[tuple[str, str]] = []
        self.transport = ScriptedTransport(script)
        self.rm = RiskManager(
            adapter=self.adapter,
            session_factory=self.factory,
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            config=RiskConfig(
                default_allocation_usd=25_000.0,
                max_position_pct_overrides={"mode_b": 1.0},
                intraday_strategies={"mode_b"},
            ),
            clock=lambda: NOW,
            runtime_root=tmp_path,
            price_lookup=lambda s: 100.0,
            alert=lambda sev, msg: self.alerts.append((sev, msg)),
        )
        client = AiClient(
            session_factory=self.factory, transport=self.transport, clock=lambda: NOW
        )
        self.mode_b = ModeB(
            client=client,
            session_factory=self.factory,
            playbook=PLAYBOOK,
            clock=lambda: NOW,
        )
        self.discipline = Discipline(PLAYBOOK.mode_b, allocation_usd=25_000.0)
        self.executor = ModeBExecutor(
            mode_b=self.mode_b,
            discipline=self.discipline,
            risk=self.rm,
            adapter=self.adapter,
            alert=lambda sev, msg: self.alerts.append((sev, msg)),
            clock=lambda: NOW,
        )


def plan_dict() -> dict:  # type: ignore[type-arg]
    return {
        "watchlist": [
            {
                "symbol": "NVDA",
                "bias": "long",
                "setup": "gap_and_go",
                "trigger_level": 101.0,
                "stop_level": 99.0,
                "targets": [104.0],
                "invalidation": "loses PM low",
            }
        ],
        "notes": "risk-on tape",
    }


def enter_decision(**kw: object) -> dict:  # type: ignore[type-arg]
    base: dict = {  # type: ignore[type-arg]
        "action": "enter",
        "symbol": "NVDA",
        "side": "buy",
        "setup_name": "gap_and_go",
        "entry_type": "market",
        "stop_price": 99.0,
        "targets": [104.0],
        "size_r": 1.0,
        "confidence": 0.7,
        "reason": "PM high break with volume",
        "tape_append": "entered NVDA gap-and-go",
    }
    base.update(kw)
    return base


# --- plan + tape --------------------------------------------------------------


def test_plan_persists_and_reloads(tmp_path: Path) -> None:
    env = Env(tmp_path, [plan_dict()])
    plan = env.mode_b.plan_day(candidates_text="NVDA gap +3.1%", context="quiet tape")
    assert isinstance(plan, DayPlanResult)
    with env.factory() as session:
        row = session.scalars(select(DayPlan)).one()
        assert row.session_date == NOW.date()

    reloaded = env.mode_b.load_plan()
    assert reloaded is not None and reloaded.watchlist[0].symbol == "NVDA"


def test_tape_appends_and_compacts(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    env.mode_b.append_tape("A" * 5_000)
    env.mode_b.append_tape("B" * 5_000)
    tape, _ = env.mode_b.load_state()
    assert len(tape) <= env.mode_b.TAPE_CHAR_BUDGET
    assert tape.endswith("B" * 100)  # newest survives; oldest compacted away


def test_counters_persist_round_trip(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    counters = SessionCounters(round_trips=3, off_plan_used=1)
    env.mode_b.save_state("tape text", counters)
    tape, loaded = env.mode_b.load_state()
    assert tape == "tape text"
    assert loaded.round_trips == 3 and loaded.off_plan_used == 1


# --- executor: enter ----------------------------------------------------------


def test_enter_builds_bracket_and_records_trade(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    decision = ModeBDecision.model_validate(enter_decision())
    counters = SessionCounters()

    env.executor.execute(
        decision,
        counters=counters,
        open_positions=[],
        session_pnl_r=0.0,
        planned_symbols={"NVDA"},
        current_prices={"NVDA": 100.0},
    )

    (intent,) = env.adapter.submitted
    assert intent.strategy == "mode_b"
    assert intent.stop_price == 99.0 and intent.take_profit_price == 104.0
    assert intent.qty == 187  # floor(187.5 / (100-99))
    with env.factory() as session:
        trade = session.scalars(select(ModeBTrade)).one()
        assert trade.setup == "gap_and_go" and not trade.off_plan


def test_enter_without_stop_rejected_in_code(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    decision = ModeBDecision.model_validate(enter_decision(stop_price=None))
    env.executor.execute(
        decision,
        counters=SessionCounters(),
        open_positions=[],
        session_pnl_r=0.0,
        planned_symbols={"NVDA"},
        current_prices={"NVDA": 100.0},
    )
    assert env.adapter.submitted == []
    assert any("stop" in msg.lower() for _, msg in env.alerts)


def test_enter_with_invented_setup_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    decision = ModeBDecision.model_validate(
        enter_decision(setup_name="quantum_reversal_deluxe")
    )
    env.executor.execute(
        decision,
        counters=SessionCounters(),
        open_positions=[],
        session_pnl_r=0.0,
        planned_symbols={"NVDA"},
        current_prices={"NVDA": 100.0},
    )
    assert env.adapter.submitted == []


def test_unplanned_symbol_enters_off_plan_half_size(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    decision = ModeBDecision.model_validate(enter_decision(symbol="AMD"))
    counters = SessionCounters()
    env.executor.execute(
        decision,
        counters=counters,
        open_positions=[],
        session_pnl_r=0.0,
        planned_symbols={"NVDA"},
        current_prices={"AMD": 100.0},
    )
    (intent,) = env.adapter.submitted
    assert intent.qty == 93  # floor(187 × 0.5)
    assert counters.off_plan_used == 1
    with env.factory() as session:
        assert session.scalars(select(ModeBTrade)).one().off_plan


def test_discipline_block_means_no_order(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    decision = ModeBDecision.model_validate(enter_decision())
    env.executor.execute(
        decision,
        counters=SessionCounters(round_trips=8),
        open_positions=[],
        session_pnl_r=0.0,
        planned_symbols={"NVDA"},
        current_prices={"NVDA": 100.0},
    )
    assert env.adapter.submitted == []


def test_short_entries_rejected_long_only_v1(tmp_path: Path) -> None:
    """All five playbook setups are long patterns; short plumbing is
    half-built (unsigned qty), so v1 gates shorts out with an alert."""
    env = Env(tmp_path, [])
    decision = ModeBDecision.model_validate(enter_decision(side="sell"))
    env.executor.execute(
        decision,
        counters=SessionCounters(),
        open_positions=[],
        session_pnl_r=0.0,
        planned_symbols={"NVDA"},
        current_prices={"NVDA": 100.0},
    )
    assert env.adapter.submitted == []
    assert any("long-only" in msg.lower() for _, msg in env.alerts)


# --- forced mechanics ---------------------------------------------------------


def test_move_stop_replaces_leg_and_failure_alerts(tmp_path: Path) -> None:
    env = Env(tmp_path, [])
    env.executor.move_stop("NVDA", parent_order_id="brk-1", new_stop=100.0)
    assert env.adapter.replaced == [("brk-1", 100.0)]

    env.adapter.replace_fail_with = RuntimeError("api down")
    env.executor.move_stop("NVDA", parent_order_id="brk-2", new_stop=101.0)
    assert any(
        sev == "critical" and "bracket" in msg.lower() for sev, msg in env.alerts
    )


# --- paper lock ---------------------------------------------------------------


def test_mode_b_live_raises_at_startup(tmp_path: Path) -> None:
    from plutus.ai.mode_b import assert_mode_b_paper_locked

    with raises(RuntimeError, match="paper"):
        assert_mode_b_paper_locked(effective_mode="live")
    assert_mode_b_paper_locked(effective_mode="paper")  # no raise
