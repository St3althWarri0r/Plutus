"""AI client (§9 shared constraints): strict JSON via forced tool use,
malformed → one retry → None, every ATTEMPT audited (timeouts with null
response), cost from a config table — never hardcoded."""

from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from plutus.ai.client import AiClient, CostTable
from plutus.db import Base, make_session_factory
from plutus.models import AiAudit


class Verdict(BaseModel):
    action: str
    size_multiplier: float


class FakeUsage:
    input_tokens = 1000
    output_tokens = 200


class FakeToolBlock:
    type = "tool_use"

    def __init__(self, payload: dict) -> None:  # type: ignore[type-arg]
        self.input = payload
        self.name = "decide"


class FakeResponse:
    def __init__(self, payload: dict) -> None:  # type: ignore[type-arg]
        self.content = [FakeToolBlock(payload)]
        self.usage = FakeUsage()


class ScriptedTransport:
    """Yields queued responses/exceptions per call."""

    def __init__(self, script: list) -> None:  # type: ignore[type-arg]
        self.script = list(script)
        self.calls = 0

    def __call__(self, **kwargs: object) -> FakeResponse:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


def make_client(tmp_path: Path, transport: ScriptedTransport) -> tuple[AiClient, sessionmaker]:  # type: ignore[type-arg]
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    client = AiClient(
        session_factory=factory,
        transport=transport,
        model="claude-sonnet-4-6",
        cost_table=CostTable({"claude-sonnet-4-6": (3.0, 15.0)}),  # $/Mtok
    )
    return client, factory


def audit_rows(factory: sessionmaker) -> list[AiAudit]:  # type: ignore[type-arg]
    with factory() as session:
        return list(session.scalars(select(AiAudit).order_by(AiAudit.id)).all())


def test_valid_response_parses_and_audits(tmp_path: Path) -> None:
    transport = ScriptedTransport([{"action": "approve", "size_multiplier": 1.0}])
    client, factory = make_client(tmp_path, transport)

    result = client.call_structured(
        mode="review", system="sys", user="usr", schema=Verdict
    )

    assert result is not None and result.action == "approve"
    (row,) = audit_rows(factory)
    assert row.mode == "review"
    assert row.input_tokens == 1000 and row.output_tokens == 200
    # cost: 1000/1M × $3 + 200/1M × $15 = 0.003 + 0.003
    assert row.cost_usd is not None
    assert float(row.cost_usd) == 0.006
    assert row.decision_json is not None and "approve" in row.decision_json
    assert row.error is None


def test_malformed_then_valid_retries_once(tmp_path: Path) -> None:
    transport = ScriptedTransport(
        [{"wrong": "shape"}, {"action": "veto", "size_multiplier": 1.0}]
    )
    client, factory = make_client(tmp_path, transport)

    result = client.call_structured(mode="review", system="s", user="u", schema=Verdict)

    assert result is not None and result.action == "veto"
    assert transport.calls == 2
    rows = audit_rows(factory)
    assert len(rows) == 2  # both attempts audited
    assert rows[0].error is not None and rows[1].error is None


def test_malformed_twice_returns_none(tmp_path: Path) -> None:
    transport = ScriptedTransport([{"bad": 1}, {"bad": 2}])
    client, factory = make_client(tmp_path, transport)

    result = client.call_structured(mode="review", system="s", user="u", schema=Verdict)

    assert result is None
    assert transport.calls == 2  # one retry, then no-op — never a third


def test_timeout_returns_none_and_audits(tmp_path: Path) -> None:
    transport = ScriptedTransport([TimeoutError("deadline")])
    client, factory = make_client(tmp_path, transport)

    result = client.call_structured(mode="brief", system="s", user="u", schema=Verdict)

    assert result is None
    (row, *_) = audit_rows(factory)
    assert row.response_text is None
    assert row.error is not None and "deadline" in row.error
