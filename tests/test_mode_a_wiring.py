"""Mode A wired into the engine paths (§9): buys reviewed, sells exempt,
vetoes recorded as order rows, risk_off halves allocation, outage → 0.5×
with a critical alert. This file is the Phase 6 acceptance drill."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from fakes import FakeAdapter
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from plutus.ai.client import AiClient
from plutus.ai.mode_a import ModeA
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.engine import run_daily_rotation
from plutus.models import Order
from plutus.risk import RiskConfig, RiskManager
from plutus.strategies.tqqq_rotation import TQQQRotation

ET = ZoneInfo("America/New_York")
RTH = datetime(2024, 6, 5, 14, 0, tzinfo=ET)


class ScriptedTransport:
    def __init__(self, script: list) -> None:  # type: ignore[type-arg]
        self.script = list(script)
        self.calls = 0

    def __call__(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item

        class Usage:
            input_tokens = 100
            output_tokens = 50

        class Block:
            type = "tool_use"

            def __init__(self, payload: dict) -> None:  # type: ignore[type-arg]
                self.input = payload

        class Response:
            def __init__(self, payload: dict) -> None:  # type: ignore[type-arg]
                self.content = [Block(payload)]
                self.usage = Usage()

        return Response(item)


def make_env(
    tmp_path: Path, script: list  # type: ignore[type-arg]
) -> tuple[RiskManager, FakeAdapter, sessionmaker[Session], ModeA, list[tuple[str, str]]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    adapter = FakeAdapter()
    alerts: list[tuple[str, str]] = []
    rm = RiskManager(
        adapter=adapter,
        session_factory=factory,
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        config=RiskConfig(
            default_allocation_usd=10_000.0,
            max_position_pct_overrides={"tqqq_rotation": 1.0},
        ),
        clock=lambda: RTH,
        runtime_root=tmp_path,
        price_lookup=lambda s: 80.0,
        alert=lambda sev, msg: alerts.append((sev, msg)),
    )
    clock = lambda: RTH.astimezone(UTC)  # noqa: E731
    client = AiClient(
        session_factory=factory, transport=ScriptedTransport(script), clock=clock
    )
    mode_a = ModeA(client=client, session_factory=factory, clock=clock)
    return rm, adapter, factory, mode_a, alerts


IDX = pd.date_range("2024-01-01", periods=30, freq="B", tz="UTC")
RALLY = pd.DataFrame(
    {
        "TQQQ": [50.0 + i for i in range(30)],
        "UVXY": [80.0] * 30,
        "BSV": [80.0] * 30,
        "TECL": [80.0] * 30,
        "SQQQ": [80.0] * 30,
    },
    index=IDX,
)
PRICES = {"TQQQ": 80.0, "UVXY": 80.0, "BSV": 80.0, "TECL": 80.0, "SQQQ": 80.0}


def rotation(rm: RiskManager, mode_a: ModeA | None, allocation: float = 10_000.0) -> None:
    run_daily_rotation(
        rm,
        strategy=TQQQRotation(sma_long=5, sma_short=3, rsi_period=3),
        closes=RALLY,
        latest_prices=PRICES,
        allocation=allocation,
        mode_a=mode_a,
    )


def test_approved_entries_submit_at_full_size(tmp_path: Path) -> None:
    # rally → hedge branch: two buy entries, each reviewed
    script = [
        {"action": "approve", "size_multiplier": 1.0, "rationale": "ok"},
        {"action": "approve", "size_multiplier": 1.0, "rationale": "ok"},
    ]
    rm, adapter, _, mode_a, _ = make_env(tmp_path, script)
    rotation(rm, mode_a)
    assert {(o.symbol, o.qty) for o in adapter.submitted} == {("UVXY", 62.0), ("BSV", 62.0)}


def test_veto_drops_order_and_records_row(tmp_path: Path) -> None:
    script = [
        {"action": "veto", "size_multiplier": 1.0, "rationale": "UVXY term decay"},
        {"action": "approve", "size_multiplier": 1.0, "rationale": "ok"},
    ]
    rm, adapter, factory, mode_a, _ = make_env(tmp_path, script)
    rotation(rm, mode_a)

    assert len(adapter.submitted) == 1  # one vetoed, one through
    with factory() as session:
        vetoed = session.scalars(select(Order).where(Order.status == "vetoed")).all()
        assert len(vetoed) == 1
        assert "UVXY term decay" in (vetoed[0].reject_reason or "")


def test_resize_applies_clamped_multiplier(tmp_path: Path) -> None:
    script = [
        {"action": "resize", "size_multiplier": 0.6, "rationale": "light"},
        {"action": "approve", "size_multiplier": 1.0, "rationale": "ok"},
    ]
    rm, adapter, _, mode_a, _ = make_env(tmp_path, script)
    rotation(rm, mode_a)
    quantities = {o.symbol: o.qty for o in adapter.submitted}
    assert 37.0 in quantities.values()  # floor(62 × 0.6)


def test_outage_drill_half_size_and_critical_alert(tmp_path: Path) -> None:
    """Phase 6 acceptance: configured supervisor times out on every attempt →
    entries proceed deterministically at 0.5× (floored), critical alert."""
    script = [TimeoutError("deadline"), TimeoutError("deadline")] * 2  # 2 entries × 2 attempts
    rm, adapter, _, mode_a, alerts = make_env(tmp_path, script)
    rotation(rm, mode_a)

    assert {o.qty for o in adapter.submitted} == {31.0}  # floor(62 × 0.5)
    assert len(adapter.submitted) == 2
    assert any(sev == "critical" and "review" in msg.lower() for sev, msg in alerts)


def test_no_mode_a_runs_full_size_unreviewed(tmp_path: Path) -> None:
    """Key absent = Mode A off = Phase 5 behavior at 1.0×."""
    rm, adapter, _, _, alerts = make_env(tmp_path, [])
    rotation(rm, None)
    assert {o.qty for o in adapter.submitted} == {62.0}
    assert alerts == []


def test_risk_off_regime_halves_allocation(tmp_path: Path) -> None:
    # brief says risk_off; then two approvals
    script = [
        {"regime": "risk_off", "notes": "n", "watch_items": []},
        {"action": "approve", "size_multiplier": 1.0, "rationale": "ok"},
        {"action": "approve", "size_multiplier": 1.0, "rationale": "ok"},
    ]
    rm, adapter, _, mode_a, _ = make_env(tmp_path, script)
    mode_a.morning_brief(
        prior_closes={}, day_changes={}, premarket={}, uvxy_level=None, positions=[]
    )
    from plutus.ai.mode_a import regime_allocation_multiplier

    allocation = 10_000.0 * regime_allocation_multiplier(mode_a.todays_regime())
    rotation(rm, mode_a, allocation=allocation)

    # 5_000 × 0.5 weight / 80 → floor(31.25) = 31 shares per leg
    assert {o.qty for o in adapter.submitted} == {31.0}
