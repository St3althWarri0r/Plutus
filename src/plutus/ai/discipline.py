"""§9B.4 — the discipline a good human day trader imposes on themselves,
enforced in code, not prompt. The model decides WHAT to trade; this module
decides whether it MAY, how BIG, and forces breakeven/scale-out mechanics.

Numbers live in playbook.yaml (ModeBConfig). Counters persist in mode_b_state
so a restart cannot reset the round-trip count or an active cooldown.
Layering note: at $25k allocation 1R = $187.50, so the −2.5R daily stop
(≈$469) triggers well inside §8's −3% allocation halt ($750) — Mode B's own
stop is the tighter, inner gate by design.
"""

from datetime import UTC, datetime, timedelta
from math import floor
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from plutus.ai.mode_b_config import ModeBConfig
from plutus.logging_setup import get_logger

log = get_logger("plutus.ai.discipline")

ET = ZoneInfo("America/New_York")

FULL_R_LOSS_THRESHOLD = -0.95  # a loss ≥ ~1R counts as a full-R loss


class SessionCounters(BaseModel):
    round_trips: int = 0
    consecutive_full_losses: int = 0
    cooldown_until: datetime | None = None
    off_plan_used: int = 0
    breakeven_done: list[str] = Field(default_factory=list)
    scale_out_done: list[str] = Field(default_factory=list)


class OpenPosition(BaseModel):
    symbol: str
    qty: float
    entry: float
    stop: float
    target: float
    setup: str
    off_plan: bool
    opened_at: datetime


class EntryCheck(BaseModel):
    allowed: bool
    reason: str = ""
    off_plan: bool = False


class ForcedAction(BaseModel):
    kind: str  # move_stop_breakeven | scale_out
    symbol: str
    qty: float = 0.0
    new_stop: float | None = None


class Discipline:
    def __init__(self, config: ModeBConfig, *, allocation_usd: float) -> None:
        self.config = config
        self.allocation_usd = allocation_usd

    def r_dollars(self) -> float:
        return self.allocation_usd * self.config.allocation_fraction_r

    def size_for_entry(self, *, entry: float, stop: float, off_plan: bool) -> int:
        per_share = abs(entry - stop)
        if per_share <= 0:
            return 0
        qty = floor(self.r_dollars() / per_share)
        if off_plan:
            qty = floor(qty * self.config.off_plan_size_factor)
        return qty

    def check_entry(
        self,
        *,
        symbol: str,
        counters: SessionCounters,
        open_positions: list[OpenPosition],
        session_pnl_r: float,
        now: datetime,
        planned: bool,
        current_price: float | None = None,
    ) -> EntryCheck:
        now_et = now.astimezone(ET)
        cutoff_h, cutoff_m = (
            int(p) for p in self.config.no_new_entries_after.split(":")
        )
        if (now_et.hour, now_et.minute) >= (cutoff_h, cutoff_m):
            return EntryCheck(allowed=False, reason="no new entries after 15:30 ET")

        if session_pnl_r <= self.config.daily_stop_r:
            return EntryCheck(
                allowed=False,
                reason=f"daily stop breached ({session_pnl_r:.2f}R) — done for the day",
            )

        if counters.cooldown_until is not None:
            cooldown = counters.cooldown_until
            if cooldown.tzinfo is None:
                cooldown = cooldown.replace(tzinfo=UTC)
            if now < cooldown:
                return EntryCheck(allowed=False, reason="anti-tilt cooldown active")

        if counters.round_trips >= self.config.max_round_trips_per_day:
            return EntryCheck(allowed=False, reason="max round trips reached")

        existing = next((p for p in open_positions if p.symbol == symbol), None)
        if existing is not None:
            if current_price is not None:
                losing = (
                    current_price < existing.entry
                    if existing.qty > 0
                    else current_price > existing.entry
                )
                if losing:
                    return EntryCheck(
                        allowed=False, reason="adding to losers is rejected, always"
                    )
        elif len(open_positions) >= self.config.max_concurrent:
            return EntryCheck(
                allowed=False,
                reason=f"max concurrent positions ({self.config.max_concurrent})",
            )

        if not planned:
            if counters.off_plan_used >= self.config.off_plan_trades_per_day:
                return EntryCheck(
                    allowed=False, reason="off-plan trade budget spent (1/day)"
                )
            return EntryCheck(allowed=True, off_plan=True)

        return EntryCheck(allowed=True)

    def forced_actions(
        self,
        position: OpenPosition,
        *,
        current_price: float,
        counters: SessionCounters,
    ) -> list[ForcedAction]:
        """Deterministic position mechanics (no AI in the loop): stop to
        breakeven at +1R, mandatory ≥1/3 off at +2R."""
        per_share_r = abs(position.entry - position.stop)
        if per_share_r <= 0:
            return []
        direction = 1.0 if position.qty > 0 else -1.0
        r_now = (current_price - position.entry) * direction / per_share_r

        actions: list[ForcedAction] = []
        if (
            r_now >= self.config.breakeven_at_r
            and position.symbol not in counters.breakeven_done
        ):
            actions.append(
                ForcedAction(
                    kind="move_stop_breakeven",
                    symbol=position.symbol,
                    new_stop=position.entry,
                )
            )
        elif (
            r_now >= self.config.scale_out_at_r
            and position.symbol not in counters.scale_out_done
        ):
            actions.append(
                ForcedAction(
                    kind="scale_out",
                    symbol=position.symbol,
                    qty=float(floor(abs(position.qty) * self.config.scale_out_fraction)),
                )
            )
        return actions

    def on_trade_closed(
        self, counters: SessionCounters, *, realized_r: float, now: datetime
    ) -> None:
        counters.round_trips += 1
        if realized_r <= FULL_R_LOSS_THRESHOLD:
            counters.consecutive_full_losses += 1
            if counters.consecutive_full_losses >= self.config.anti_tilt_consecutive_losses:
                counters.cooldown_until = now + timedelta(
                    minutes=self.config.anti_tilt_cooldown_minutes
                )
                counters.consecutive_full_losses = 0
                log.warning("anti_tilt_cooldown", until=str(counters.cooldown_until))
        else:
            counters.consecutive_full_losses = 0
