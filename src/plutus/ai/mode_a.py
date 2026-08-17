"""Mode A — the AI supervisor for systematic strategies (§9).

Data honesty: the prompt names exactly what the model sees — IEX-only
pre-market prints, UVXY level as a volatility proxy, NO futures and NO real
VIX (a §9 deviation; we have no free source for either). A model told it has
futures when it has a proxy will confabulate around the gap.

Discipline lives in code, not prompts: the size multiplier is clamped to
[0.5, 1.5] before any arithmetic; vetoes drop the order (recorded upstream);
a failed/timed-out review means the systematic strategy proceeds at 0.5×
(§9.4) — and exits never enter this module at all.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plutus.ai.client import AiClient
from plutus.brokers.base import OrderIntent
from plutus.logging_setup import get_logger
from plutus.models import AiAudit

log = get_logger("plutus.ai.mode_a")

ET = ZoneInfo("America/New_York")

MULTIPLIER_MIN, MULTIPLIER_MAX = 0.5, 1.5
OUTAGE_MULTIPLIER = 0.5

Regime = Literal["risk_on", "neutral", "risk_off"]


class BriefResult(BaseModel):
    regime: Regime
    notes: str
    watch_items: list[str] = Field(default_factory=list)


class ReviewResult(BaseModel):
    action: Literal["approve", "veto", "resize"]
    size_multiplier: float
    rationale: str


class JournalResult(BaseModel):
    summary: str
    anomalies: list[str] = Field(default_factory=list)


def regime_allocation_multiplier(regime: str) -> float:
    """§9: risk_off halves sizing; anything else is full size."""
    return 0.5 if regime == "risk_off" else 1.0


def apply_review(intent: OrderIntent, review: ReviewResult | None) -> OrderIntent | None:
    """Turn a review into an order (or None for veto). review=None is the
    §9.4 outage path: proceed deterministically at 0.5×, floored."""
    if review is None:
        qty = float(int(intent.qty * OUTAGE_MULTIPLIER))
        return None if qty < 1 else intent.model_copy(update={"qty": qty})
    if review.action == "veto":
        return None
    if review.action == "resize":
        clamped = min(MULTIPLIER_MAX, max(MULTIPLIER_MIN, review.size_multiplier))
        qty = float(int(intent.qty * clamped))
        return None if qty < 1 else intent.model_copy(update={"qty": qty})
    return intent


def build_brief_prompt(
    *,
    prior_closes: dict[str, float],
    day_changes: dict[str, float],
    premarket: dict[str, float],
    uvxy_level: float | None,
    positions: list[tuple[str, str, float]],
) -> str:
    lines = [
        "Assess the market regime for today's US equity session.",
        "",
        "DATA NOTES (be precise about what you do NOT have):",
        "- Pre-market prints are IEX-only and may be sparse.",
        "- UVXY level is supplied as a volatility proxy. You do NOT have the",
        "  real VIX index.",
        "- You have NO futures data and NO news headlines.",
        "",
        f"Prior-day closes: {json.dumps(prior_closes)}",
        f"Prior-day changes: {json.dumps(day_changes)}",
        f"Pre-market prints (IEX): {json.dumps(premarket)}",
        f"UVXY level (volatility proxy): {uvxy_level}",
        f"Open bot positions (strategy, symbol, qty): {positions}",
        "",
        "Return regime risk_on|neutral|risk_off, brief notes, and watch items.",
    ]
    return "\n".join(lines)


def build_review_prompt(intent: OrderIntent, context: str) -> str:
    return (
        "Review this systematic strategy entry signal. Approvals still pass "
        "all deterministic risk gates; your multiplier is clamped to "
        "[0.5, 1.5] in code regardless of what you return.\n\n"
        f"Order: {intent.side} {intent.qty} {intent.symbol} "
        f"({intent.strategy}, {intent.order_type})\n"
        f"Context: {context}"
    )


class ModeA:
    def __init__(
        self,
        *,
        client: AiClient,
        session_factory: sessionmaker[Session],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def morning_brief(
        self,
        *,
        prior_closes: dict[str, float],
        day_changes: dict[str, float],
        premarket: dict[str, float],
        uvxy_level: float | None,
        positions: list[tuple[str, str, float]],
    ) -> BriefResult | None:
        prompt = build_brief_prompt(
            prior_closes=prior_closes,
            day_changes=day_changes,
            premarket=premarket,
            uvxy_level=uvxy_level,
            positions=positions,
        )
        return self._client.call_structured(
            mode="brief",
            system="You are the pre-market risk supervisor for a systematic "
            "trading bot. Be conservative; risk_off halves all sizing.",
            user=prompt,
            schema=BriefResult,
        )

    def todays_regime(self) -> Regime:
        """Regime from TODAY's brief (ET session date) only; else neutral.
        Yesterday's regime applied to today is worse than no regime."""
        today_et = self._clock().astimezone(ET).date()
        with self._session_factory() as session:
            rows = session.scalars(
                select(AiAudit)
                .where(AiAudit.mode == "brief", AiAudit.decision_json.is_not(None))
                .order_by(AiAudit.id.desc())
            ).all()
            for row in rows:
                created = row.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if created.astimezone(ET).date() != today_et:
                    continue
                regime = json.loads(row.decision_json or "{}").get("regime")
                if regime in ("risk_on", "neutral", "risk_off"):
                    return regime  # type: ignore[no-any-return]
        return "neutral"

    def trade_review(self, intent: OrderIntent, *, context: str) -> ReviewResult | None:
        return self._client.call_structured(
            mode="review",
            system="You review systematic entry signals. veto only with a "
            "concrete reason; resize within [0.5, 1.5].",
            user=build_review_prompt(intent, context),
            schema=ReviewResult,
        )

    def journal(self, *, session_summary: str) -> JournalResult | None:
        return self._client.call_structured(
            mode="journal",
            system="Write the post-market journal: P&L attribution and "
            "anomalies, terse and specific.",
            user=session_summary,
            schema=JournalResult,
        )
