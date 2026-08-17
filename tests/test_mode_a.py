"""Mode A (§9): brief → regime, per-signal review with in-code clamps,
regime staleness (today's brief only), veto recording semantics."""

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from plutus.ai.client import AiClient, CostTable
from plutus.ai.mode_a import (
    BriefResult,
    ModeA,
    ReviewResult,
    apply_review,
    build_brief_prompt,
    regime_allocation_multiplier,
)
from plutus.brokers.base import OrderIntent
from plutus.db import Base, make_session_factory
from plutus.models import AiAudit


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
            name = "decide"

            def __init__(self, payload: dict) -> None:  # type: ignore[type-arg]
                self.input = payload

        class Response:
            def __init__(self, payload: dict) -> None:  # type: ignore[type-arg]
                self.content = [Block(payload)]
                self.usage = Usage()

        return Response(item)


def make_mode_a(script: list) -> tuple[ModeA, sessionmaker]:  # type: ignore[type-arg]
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    now = datetime(2026, 8, 17, 12, 30, tzinfo=UTC)  # 08:30 ET Monday
    client = AiClient(
        session_factory=factory,
        transport=ScriptedTransport(script),
        cost_table=CostTable({"claude-sonnet-4-6": (3.0, 15.0)}),
        clock=lambda: now,
    )
    return ModeA(client=client, session_factory=factory, clock=lambda: now), factory


# --- prompts ------------------------------------------------------------------


def test_brief_prompt_labels_proxies_honestly() -> None:
    prompt = build_brief_prompt(
        prior_closes={"SPY": 776.06, "TQQQ": 77.15},
        day_changes={"SPY": 0.004, "TQQQ": 0.012},
        premarket={"SPY": 777.2},
        uvxy_level=20.4,
        positions=[("tqqq_rotation", "TQQQ", 120.0)],
    )
    assert "UVXY" in prompt and "volatility proxy" in prompt
    assert "IEX" in prompt  # data-source honesty
    assert "NO futures" in prompt  # explicit absence statement
    assert "NOT have" in prompt and "real VIX" in prompt  # proxy, not the index


# --- brief / regime -----------------------------------------------------------


def test_brief_persists_and_regime_reads_today_only() -> None:
    mode_a, factory = make_mode_a(
        [{"regime": "risk_off", "notes": "gap down", "watch_items": ["UVXY spike"]}]
    )
    result = mode_a.morning_brief(
        prior_closes={}, day_changes={}, premarket={}, uvxy_level=30.0, positions=[]
    )
    assert isinstance(result, BriefResult) and result.regime == "risk_off"

    assert mode_a.todays_regime() == "risk_off"
    assert regime_allocation_multiplier("risk_off") == 0.5
    assert regime_allocation_multiplier("neutral") == 1.0
    assert regime_allocation_multiplier("risk_on") == 1.0


def test_stale_brief_is_ignored_neutral_default() -> None:
    mode_a, factory = make_mode_a(
        [{"regime": "risk_off", "notes": "x", "watch_items": []}]
    )
    mode_a.morning_brief(
        prior_closes={}, day_changes={}, premarket={}, uvxy_level=10.0, positions=[]
    )
    # backdate the audit row to Friday: yesterday's regime must not apply
    with factory() as session:
        row = session.query(AiAudit).one()
        row.created_at = datetime(2026, 8, 14, 12, 30, tzinfo=UTC)
        session.commit()

    assert mode_a.todays_regime() == "neutral"


def test_no_brief_at_all_defaults_neutral() -> None:
    mode_a, _ = make_mode_a([])
    assert mode_a.todays_regime() == "neutral"


# --- review + clamp -----------------------------------------------------------


def entry(qty: float = 100) -> OrderIntent:
    return OrderIntent(
        symbol="TQQQ", side="buy", qty=qty, order_type="market", strategy="tqqq_rotation"
    )


def test_review_approve_passes_through() -> None:
    mode_a, _ = make_mode_a(
        [{"action": "approve", "size_multiplier": 1.0, "rationale": "fine"}]
    )
    review = mode_a.trade_review(entry(), context="ctx")
    assert review is not None and review.action == "approve"


def test_apply_review_resize_clamps_and_floors() -> None:
    # a hallucinated 3.0 multiplier must clamp to 1.5 BEFORE arithmetic
    resized = apply_review(
        entry(qty=101), ReviewResult(action="resize", size_multiplier=3.0, rationale="r")
    )
    assert resized is not None
    assert resized.qty == 151.0  # floor(101 × 1.5)

    shrunk = apply_review(
        entry(qty=101), ReviewResult(action="resize", size_multiplier=0.1, rationale="r")
    )
    assert shrunk is not None
    assert shrunk.qty == 50.0  # floor(101 × 0.5) — lower clamp


def test_apply_review_veto_returns_none() -> None:
    assert apply_review(
        entry(), ReviewResult(action="veto", size_multiplier=1.0, rationale="no")
    ) is None


def test_review_failure_falls_back_to_half_size() -> None:
    """§9.4 outage semantics live in apply_review(None): 0.5×, floored."""
    result = apply_review(entry(qty=101), None)
    assert result is not None
    assert result.qty == 50.0
