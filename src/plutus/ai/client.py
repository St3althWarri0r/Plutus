"""Structured AI calls with the §9 shared constraints baked in.

- Strict JSON via a forced tool call; the tool input is validated against the
  caller's pydantic schema (tool_choice forces A call, not a VALID one).
- Malformed → exactly one retry → None (no-op). We own the retry: the SDK
  client must be constructed with max_retries=0 or the 20s budget silently
  multiplies.
- Every ATTEMPT is audited verbatim — timeouts and malformed responses write
  rows with null response/decision. The audit table is the outage-drill
  debugging surface and the (6b) cost display's source.
"""

import hashlib
import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from plutus.logging_setup import get_logger
from plutus.models import AiAudit

log = get_logger("plutus.ai.client")

DEFAULT_MODEL = "claude-sonnet-4-6"
CALL_TIMEOUT_SECONDS = 20.0

S = TypeVar("S", bound=BaseModel)

Transport = Callable[..., Any]  # kwargs of anthropic Messages.create → response


class CostTable:
    """$/Mtok (input, output) per model — config, never inline."""

    def __init__(self, prices: dict[str, tuple[float, float]]) -> None:
        self._prices = prices

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        if model not in self._prices:
            return None
        in_price, out_price = self._prices[model]
        return input_tokens / 1e6 * in_price + output_tokens / 1e6 * out_price


DEFAULT_COSTS = CostTable({"claude-sonnet-4-6": (3.0, 15.0)})


def make_anthropic_transport(api_key: str) -> Transport:  # pragma: no cover - smoke
    import anthropic

    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=CALL_TIMEOUT_SECONDS,
        max_retries=0,  # we own the retry policy
    )
    return client.messages.create


class AiClient:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        transport: Transport,
        model: str = DEFAULT_MODEL,
        cost_table: CostTable = DEFAULT_COSTS,
        clock: Callable[[], "datetime"] | None = None,
    ) -> None:
        from datetime import UTC, datetime

        self._session_factory = session_factory
        self._transport = transport
        self.model = model
        self._costs = cost_table
        self._clock = clock or (lambda: datetime.now(UTC))

    def call_structured(
        self,
        *,
        mode: str,
        system: str,
        user: str,
        schema: type[S],
        max_tokens: int = 1024,
    ) -> S | None:
        """Two attempts max; None means no-op (§9: malformed → retry → no-op)."""
        for attempt in (1, 2):
            result = self._attempt(
                mode=mode, system=system, user=user, schema=schema, max_tokens=max_tokens
            )
            if result is not None:
                return result
            if attempt == 1:
                log.warning("ai_retry", mode=mode)
        log.warning("ai_noop", mode=mode)
        return None

    def _attempt(
        self, *, mode: str, system: str, user: str, schema: type[S], max_tokens: int
    ) -> S | None:
        tool = {
            "name": "decide",
            "description": f"Return the {mode} decision.",
            "input_schema": schema.model_json_schema(),
        }
        prompt_text = f"[system]\n{system}\n[user]\n{user}"
        prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()

        started = time.perf_counter()
        response: Any = None
        error: str | None = None
        decision: S | None = None
        try:
            response = self._transport(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "decide"},
            )
            payload = self._extract_tool_input(response)
            decision = schema.model_validate(payload)
        except ValidationError as exc:
            error = f"schema validation failed: {exc.errors()[:3]}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        cost = (
            self._costs.cost(self.model, input_tokens, output_tokens)
            if input_tokens is not None and output_tokens is not None
            else None
        )

        with self._session_factory() as session:
            session.add(
                AiAudit(
                    mode=mode,
                    model=self.model,
                    prompt_hash=prompt_hash,
                    prompt_text=prompt_text,
                    response_text=(
                        json.dumps(self._extract_tool_input(response), default=str)
                        if response is not None and error is None
                        else None
                    ),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    cost_usd=cost,
                    decision_json=decision.model_dump_json() if decision else None,
                    error=error,
                    created_at=self._clock(),
                )
            )
            session.commit()

        if error is not None:
            log.warning("ai_attempt_failed", mode=mode, error=error, latency_ms=latency_ms)
        return decision

    @staticmethod
    def _extract_tool_input(response: Any) -> dict[str, Any]:
        for block in getattr(response, "content", []):
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        raise ValueError("no tool_use block in response")
