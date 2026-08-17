"""Mode B — the AI day trader (§9B). The model makes the trading decisions;
deterministic code (discipline.py, RiskManager, this executor) enforces the
discipline a good human trader imposes on themselves.

Non-negotiables enforced here in code:
- every entry is a BRACKET (no stop → rejected before any broker call)
- the setup name must exist in playbook.yaml (closed set)
- Discipline.check_entry runs before RiskManager, whose §8 chain is unchanged
- Mode B is hard-locked to paper: live mode + Mode B raises at startup
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
from plutus.ai.discipline import Discipline, OpenPosition, SessionCounters
from plutus.ai.mode_b_config import Playbook
from plutus.brokers.base import BrokerAdapter, OrderIntent
from plutus.logging_setup import get_logger
from plutus.models import DayPlan, ModeBState, ModeBTrade
from plutus.risk import Alert, RiskManager, log_alert

log = get_logger("plutus.ai.mode_b")

ET = ZoneInfo("America/New_York")

STRATEGY_NAME = "mode_b"
MONITOR_MODEL_DEFAULT = "claude-haiku-4-5"


def assert_mode_b_paper_locked(*, effective_mode: str) -> None:
    """§9B.7: Mode B starts hard-locked to paper. The lock comes off only by
    the user's hand (and not in this codebase yet)."""
    if effective_mode != "paper":
        raise RuntimeError(
            "Mode B is hard-locked to paper trading (§9B.7); refusing to start "
            f"with effective mode {effective_mode!r}"
        )


# --- schemas ------------------------------------------------------------------


class PlanEntry(BaseModel):
    symbol: str
    bias: Literal["long", "short"]
    setup: str
    trigger_level: float
    stop_level: float
    targets: list[float] = Field(default_factory=list)
    invalidation: str


class DayPlanResult(BaseModel):
    watchlist: list[PlanEntry] = Field(max_length=6)
    notes: str = ""


class MonitorResult(BaseModel):
    actionable: bool
    reason: str = ""


class ModeBDecision(BaseModel):
    action: Literal[
        "enter", "exit", "scale_out", "move_stop", "cancel", "hold", "stand_down"
    ]
    symbol: str | None = None
    side: Literal["buy", "sell"] = "buy"
    setup_name: str | None = None
    entry_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    stop_price: float | None = None
    targets: list[float] = Field(default_factory=list)
    size_r: float = 1.0
    confidence: float | None = None
    reason: str = ""
    tape_append: str = ""


# --- the agent ----------------------------------------------------------------


class ModeB:
    TAPE_CHAR_BUDGET = 6_000  # ≈1,500 tokens (§9B.3)

    def __init__(
        self,
        *,
        client: AiClient,
        session_factory: sessionmaker[Session],
        playbook: Playbook,
        clock: Callable[[], datetime] | None = None,
        monitor_model: str = MONITOR_MODEL_DEFAULT,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self.playbook = playbook
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monitor_model = monitor_model

    def system_prompt(self) -> str:
        """Deterministic (cache-controlled): persona + playbook + hard rules."""
        return "\n\n".join(
            [
                "You are a disciplined discretionary day trader (momentum/"
                "technical, prop-desk style) trading a PAPER account. Not HFT,"
                " not scalping, not swing. Plan the trade, trade the plan.",
                self.playbook.setups_prompt_block(),
                "HARD RULES (enforced in code — violations are rejected, so "
                "don't attempt them): every entry needs stop_price; setups "
                "only from the playbook; max 2 concurrent; 8 round trips/day; "
                "daily stop −2.5R; one off-plan trade/day at half size; no "
                "adding to losers; no new entries after 15:30 ET.",
            ]
        )

    # --- calls ---------------------------------------------------------------

    def plan_day(self, *, candidates_text: str, context: str) -> DayPlanResult | None:
        plan = self._client.call_structured(
            mode="day_plan",
            system=self.system_prompt(),
            user=(
                "Write today's day plan (≤6 names). For each: bias, playbook "
                "setup, entry trigger level, stop level, targets, invalidation."
                f"\n\nScanner candidates:\n{candidates_text}\n\nContext:\n{context}"
            ),
            schema=DayPlanResult,
            cache_system=True,
            max_tokens=2048,
        )
        if plan is not None:
            with self._session_factory() as session:
                today = self._clock().astimezone(ET).date()
                row = session.scalars(
                    select(DayPlan).where(DayPlan.session_date == today)
                ).one_or_none()
                if row is None:
                    session.add(
                        DayPlan(session_date=today, plan_json=plan.model_dump_json())
                    )
                else:
                    row.plan_json = plan.model_dump_json()
                session.commit()
        return plan

    def load_plan(self) -> DayPlanResult | None:
        with self._session_factory() as session:
            today = self._clock().astimezone(ET).date()
            row = session.scalars(
                select(DayPlan).where(DayPlan.session_date == today)
            ).one_or_none()
            if row is None:
                return None
            return DayPlanResult.model_validate_json(row.plan_json)

    def monitor(self, packet: str) -> MonitorResult | None:
        """The cheap per-bar gate (§9B.6): is anything actionable at all?"""
        return self._client.call_structured(
            mode="monitor",
            system=self.system_prompt(),
            user=f"Anything actionable this bar? Be strict.\n\n{packet}",
            schema=MonitorResult,
            model=self._monitor_model,
            cache_system=True,
            max_tokens=256,
        )

    def decide(self, packet: str) -> ModeBDecision | None:
        return self._client.call_structured(
            mode="decision",
            system=self.system_prompt(),
            user=f"Decide the next action.\n\n{packet}",
            schema=ModeBDecision,
            cache_system=True,
            max_tokens=1024,
        )

    def journal(self, session_summary: str) -> None:
        self._client.call_structured(
            mode="mode_b_journal",
            system=self.system_prompt(),
            user=(
                "Write the 16:15 journal: per-trade entries (setup, thesis, "
                "execution grade A–F, what went wrong/right) + session summary."
                f"\n\n{session_summary}"
            ),
            schema=MonitorResult,  # summary lands verbatim in ai_audit
            max_tokens=2048,
        )

    # --- tape + counters (§9B.3, persisted) ----------------------------------

    def load_state(self) -> tuple[str, SessionCounters]:
        with self._session_factory() as session:
            row = self._state_row(session)
            counters = SessionCounters.model_validate(json.loads(row.counters_json))
            return row.tape_text, counters

    def save_state(self, tape: str, counters: SessionCounters) -> None:
        with self._session_factory() as session:
            row = self._state_row(session)
            row.tape_text = tape[-self.TAPE_CHAR_BUDGET :]
            row.counters_json = counters.model_dump_json()
            session.commit()

    def append_tape(self, text: str) -> None:
        tape, counters = self.load_state()
        combined = (tape + "\n" + text).strip()
        # oldest details auto-compact (§9B.3): keep the newest budget's worth
        self.save_state(combined[-self.TAPE_CHAR_BUDGET :], counters)

    def _state_row(self, session: Session) -> ModeBState:
        today = self._clock().astimezone(ET).date()
        row = session.scalars(
            select(ModeBState).where(ModeBState.session_date == today)
        ).one_or_none()
        if row is None:
            row = ModeBState(session_date=today, tape_text="", counters_json="{}")
            session.add(row)
            session.flush()
        return row


# --- the executor -------------------------------------------------------------


class ModeBExecutor:
    def __init__(
        self,
        *,
        mode_b: ModeB,
        discipline: Discipline,
        risk: RiskManager,
        adapter: BrokerAdapter,
        alert: Alert = log_alert,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._mode_b = mode_b
        self._discipline = discipline
        self._risk = risk
        self._adapter = adapter
        self._alert = alert
        self._clock = clock or (lambda: datetime.now(UTC))

    def execute(
        self,
        decision: ModeBDecision,
        *,
        counters: SessionCounters,
        open_positions: list[OpenPosition],
        session_pnl_r: float,
        planned_symbols: set[str],
        current_prices: dict[str, float],
    ) -> None:
        if decision.tape_append:
            self._mode_b.append_tape(decision.tape_append)

        if decision.action in ("hold", "stand_down"):
            return
        if decision.action == "enter":
            self._enter(
                decision,
                counters=counters,
                open_positions=open_positions,
                session_pnl_r=session_pnl_r,
                planned_symbols=planned_symbols,
                current_prices=current_prices,
            )
        elif decision.action in ("exit", "scale_out"):
            self._exit(decision, open_positions, full=decision.action == "exit")
        elif decision.action == "move_stop":
            self._agent_move_stop(decision, open_positions)
        elif decision.action == "cancel":
            self._cancel(decision)

    def _enter(
        self,
        decision: ModeBDecision,
        *,
        counters: SessionCounters,
        open_positions: list[OpenPosition],
        session_pnl_r: float,
        planned_symbols: set[str],
        current_prices: dict[str, float],
    ) -> None:
        symbol = decision.symbol
        if symbol is None:
            return
        if decision.stop_price is None:
            self._alert(
                "warning", f"Mode B enter without stop_price for {symbol} — rejected"
            )
            return
        setup = decision.setup_name or ""
        if not self._mode_b.playbook.is_valid_setup(setup):
            self._alert(
                "warning",
                f"Mode B named a setup outside the playbook ({setup!r}) — rejected",
            )
            return
        price = decision.limit_price or current_prices.get(symbol)
        if price is None:
            self._alert("warning", f"Mode B enter unpriceable for {symbol} — rejected")
            return

        check = self._discipline.check_entry(
            symbol=symbol,
            counters=counters,
            open_positions=open_positions,
            session_pnl_r=session_pnl_r,
            now=self._clock(),
            planned=symbol in planned_symbols,
            current_price=price,
        )
        if not check.allowed:
            log.info("mode_b_entry_blocked", symbol=symbol, reason=check.reason)
            self._mode_b.append_tape(f"[discipline] entry blocked: {check.reason}")
            return

        qty = self._discipline.size_for_entry(
            entry=price, stop=decision.stop_price, off_plan=check.off_plan
        )
        if qty < 1:
            log.info("mode_b_entry_unsizeable", symbol=symbol)
            return
        per_share_r = abs(price - decision.stop_price)
        target = (
            decision.targets[0]
            if decision.targets
            else price + 2 * per_share_r * (1 if decision.side == "buy" else -1)
        )

        intent = OrderIntent(
            symbol=symbol,
            side=decision.side,
            qty=float(qty),
            order_type=decision.entry_type,
            limit_price=decision.limit_price,
            stop_price=decision.stop_price,
            take_profit_price=target,
            strategy=STRATEGY_NAME,
        )
        row = self._risk.submit(intent)
        if row.status == "rejected":
            self._mode_b.append_tape(f"[risk] entry rejected: {row.reject_reason}")
            return

        if check.off_plan:
            counters.off_plan_used += 1
        now = self._clock().astimezone(UTC)
        with self._risk._session_factory() as session:
            session.add(
                ModeBTrade(
                    session_date=now.astimezone(ET).date(),
                    symbol=symbol,
                    setup=setup,
                    off_plan=check.off_plan,
                    qty=float(qty),
                    entry_price=price,
                    stop_price=decision.stop_price,
                    opened_at=now,
                )
            )
            session.commit()
        log.info(
            "mode_b_entered",
            symbol=symbol,
            setup=setup,
            qty=qty,
            stop=decision.stop_price,
            target=target,
        )

    def _exit(
        self, decision: ModeBDecision, open_positions: list[OpenPosition], *, full: bool
    ) -> None:
        symbol = decision.symbol
        position = next((p for p in open_positions if p.symbol == symbol), None)
        if position is None or symbol is None:
            return
        qty = (
            abs(position.qty)
            if full
            else float(
                int(abs(position.qty) * self._mode_b.playbook.mode_b.scale_out_fraction)
            )
        )
        if qty < 1:
            return
        self._risk.submit(
            OrderIntent(
                symbol=symbol,
                side="sell" if position.qty > 0 else "buy",
                qty=qty,
                order_type="market",
                strategy=STRATEGY_NAME,
            )
        )

    def _agent_move_stop(
        self, decision: ModeBDecision, open_positions: list[OpenPosition]
    ) -> None:
        if decision.symbol is None or decision.stop_price is None:
            return
        parent = self._find_parent_order_id(decision.symbol)
        if parent is None:
            return
        self.move_stop(decision.symbol, parent_order_id=parent, new_stop=decision.stop_price)

    def move_stop(self, symbol: str, *, parent_order_id: str, new_stop: float) -> None:
        """Replace the bracket's stop leg. On failure: alert and leave the
        position under its ORIGINAL bracket — never naked."""
        try:
            self._adapter.replace_stop(parent_order_id, new_stop)
            log.info("mode_b_stop_moved", symbol=symbol, new_stop=new_stop)
        except Exception as exc:
            self._alert(
                "critical",
                f"stop move failed for {symbol} ({exc}); position remains under "
                "its original bracket — manual check advised",
            )

    def _cancel(self, decision: ModeBDecision) -> None:
        if decision.symbol is None:
            return
        from plutus.models import Order

        with self._risk._session_factory() as session:
            rows = session.scalars(
                select(Order).where(
                    Order.strategy == STRATEGY_NAME,
                    Order.symbol == decision.symbol,
                    Order.broker_order_id.is_not(None),
                    Order.status.not_in(["filled", "canceled", "rejected", "expired"]),
                )
            ).all()
            for row in rows:
                assert row.broker_order_id is not None
                try:
                    self._adapter.cancel_order(row.broker_order_id)
                    row.status = "canceled"
                except Exception as exc:
                    self._alert("warning", f"cancel failed for {decision.symbol}: {exc}")
            session.commit()

    def _find_parent_order_id(self, symbol: str) -> str | None:
        from plutus.models import Order

        with self._risk._session_factory() as session:
            row = session.scalars(
                select(Order)
                .where(
                    Order.strategy == STRATEGY_NAME,
                    Order.symbol == symbol,
                    Order.broker_order_id.is_not(None),
                )
                .order_by(Order.id.desc())
            ).first()
            return row.broker_order_id if row is not None else None
