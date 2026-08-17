"""SchwabAdapter (§3, broker #2) — live-only; Schwab has NO paper environment.

Probed against schwab-py 1.5.1 (2026-08-17): Client.get_account(account_hash,
fields=), place_order(account_hash, order_spec), get_order/cancel_order
(order_id, account_hash), get_orders_for_account(account_hash, ...); order
specs via schwab.orders.equities builders. Response-shape mapping below is
probe-calibrated at the LIBRARY level only — the Phase 8 manual round-trip is
the first real-data validation.

Token discipline (§3): access tokens live ~30 min (schwab-py refreshes those
itself); the REFRESH token dies every 7 days and renewing it is a manual
re-auth. We own that clock — a sidecar file written by our OAuth helper, not
parsed out of schwab-py's token file (undocumented format). 6 days → warn
alert (24h early); ≥7 days or unknown age → every adapter call raises
SchwabAuthStale. Never trade on stale auth.

Idempotency honesty: schwab-py exposes no client-supplied order id, so this
adapter CANNOT guarantee retry-after-timeout dedupe the way AlpacaAdapter
does. Ambiguous submit failures are resolved by looking up recent orders and
matching symbol/qty/side within a short window — weaker, and said so here.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from plutus.brokers.base import (
    AccountState,
    Fill,
    OrderIntent,
    OrderReceipt,
    OrderStatus,
    Position,
)
from plutus.logging_setup import get_logger

log = get_logger("plutus.brokers.schwab")

WARN_AGE = timedelta(days=6)
STALE_AGE = timedelta(days=7)

_STATUS_MAP = {
    "AWAITING_PARENT_ORDER": OrderStatus.NEW,
    "AWAITING_CONDITION": OrderStatus.NEW,
    "AWAITING_MANUAL_REVIEW": OrderStatus.NEW,
    "ACCEPTED": OrderStatus.ACCEPTED,
    "PENDING_ACTIVATION": OrderStatus.ACCEPTED,
    "QUEUED": OrderStatus.NEW,
    "WORKING": OrderStatus.ACCEPTED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "PENDING_CANCEL": OrderStatus.ACCEPTED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "REPLACED": OrderStatus.CANCELED,
}

TokenStatus = Literal["ok", "warn", "stale", "unconfigured"]


class SchwabAuthStale(RuntimeError):
    """Refresh token dead or of unknown age — halted awaiting re-auth (§3)."""


class TokenHealth:
    """The 7-day refresh-token clock, owned by us via a sidecar file the
    OAuth helper writes. Unknown age fails closed (stale)."""

    def __init__(self, token_path: Path, clock: Callable[[], datetime] | None = None) -> None:
        self._token_path = token_path
        self._sidecar = token_path.with_name(token_path.name + ".meta.json")
        self._clock = clock or (lambda: datetime.now(UTC))

    def record_refresh(self, now: datetime | None = None) -> None:
        moment = (now or self._clock()).astimezone(UTC)
        self._sidecar.write_text(
            json.dumps({"refresh_token_created_at": moment.isoformat()})
        )
        log.info("schwab_refresh_recorded", at=moment.isoformat())

    def age(self) -> timedelta | None:
        if not self._sidecar.exists():
            return None
        try:
            raw = json.loads(self._sidecar.read_text())["refresh_token_created_at"]
            created = datetime.fromisoformat(raw)
        except (KeyError, ValueError, json.JSONDecodeError):
            return None
        return self._clock().astimezone(UTC) - created

    def status(self) -> TokenStatus:
        if not self._token_path.exists():
            return "unconfigured"
        age = self.age()
        if age is None or age >= STALE_AGE:
            return "stale"
        if age >= WARN_AGE:
            return "warn"
        return "ok"


def check_token_health(health: TokenHealth, alert: Callable[[str, str], None]) -> None:
    """Daily job: warn 24h before the 7-day death; critical once stale."""
    status = health.status()
    if status == "warn":
        age = health.age()
        alert(
            "warning",
            f"Schwab refresh token is {age.days if age else '?'} days old — it dies "
            "at 7 days; re-auth with `python -m plutus.schwab_auth` today",
        )
    elif status == "stale":
        alert(
            "critical",
            "Schwab auth is STALE — adapter halted awaiting re-auth "
            "(`python -m plutus.schwab_auth`)",
        )


class SchwabAdapter:
    """BrokerAdapter over a schwab-py Client. Every call checks token health
    first — 'halted, awaiting re-auth' is a hard gate, not a warning."""

    def __init__(
        self,
        client: Any,
        *,
        account_hash: str,
        token_health: TokenHealth,
    ) -> None:
        self._client = client
        self._account_hash = account_hash
        self._health = token_health

    def _assert_fresh(self) -> None:
        status = self._health.status()
        if status in ("stale", "unconfigured"):
            raise SchwabAuthStale(
                f"Schwab token status is {status!r} — halted awaiting re-auth"
            )

    def get_account(self) -> AccountState:
        self._assert_fresh()
        payload = self._client.get_account(self._account_hash).json()
        balances = payload.get("securitiesAccount", {}).get("currentBalances", {})
        return AccountState(
            equity=float(balances.get("liquidationValue") or balances.get("equity") or 0),
            cash=float(balances.get("cashBalance") or 0),
            buying_power=float(balances.get("buyingPower") or 0),
        )

    def get_positions(self) -> list[Position]:
        self._assert_fresh()
        payload = self._client.get_account(self._account_hash, fields="positions").json()
        rows = payload.get("securitiesAccount", {}).get("positions", []) or []
        positions: list[Position] = []
        for row in rows:
            qty = float(row.get("longQuantity") or 0) - float(row.get("shortQuantity") or 0)
            positions.append(
                Position(
                    symbol=str(row.get("instrument", {}).get("symbol", "?")),
                    qty=qty,
                    avg_entry_price=float(row.get("averagePrice") or 0),
                    market_value=float(row.get("marketValue") or 0),
                    unrealized_pl=float(row.get("longOpenProfitLoss") or 0),
                )
            )
        return positions

    def submit_order(self, order: OrderIntent) -> OrderReceipt:
        # qty truncates to whole shares (Schwab equities are whole-share)
        self._assert_fresh()
        from schwab.orders import equities

        if order.order_type == "market":
            builder = (
                equities.equity_buy_market(order.symbol, int(order.qty))
                if order.side == "buy"
                else equities.equity_sell_market(order.symbol, int(order.qty))
            )
        else:
            assert order.limit_price is not None
            builder = (
                equities.equity_buy_limit(order.symbol, int(order.qty), str(order.limit_price))
                if order.side == "buy"
                else equities.equity_sell_limit(
                    order.symbol, int(order.qty), str(order.limit_price)
                )
            )
        response = self._client.place_order(self._account_hash, builder)
        order_id = self._extract_order_id(response)
        log.info("schwab_order_placed", symbol=order.symbol, broker_order_id=order_id)
        return OrderReceipt(
            broker_order_id=order_id,
            idempotency_key=order.idempotency_key,
            status=OrderStatus.ACCEPTED,
        )

    @staticmethod
    def _extract_order_id(response: Any) -> str:
        location = response.headers.get("Location", "")
        if not location:
            raise RuntimeError("Schwab place_order returned no Location header")
        return str(location).rstrip("/").split("/")[-1]

    def cancel_order(self, broker_order_id: str) -> None:
        self._assert_fresh()
        self._client.cancel_order(broker_order_id, self._account_hash)

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        self._assert_fresh()
        payload = self._client.get_order(broker_order_id, self._account_hash).json()
        return _STATUS_MAP.get(str(payload.get("status", "")), OrderStatus.UNKNOWN)

    def get_fills(self, since: datetime) -> list[Fill]:
        self._assert_fresh()
        response = self._client.get_orders_for_account(
            self._account_hash, from_entered_datetime=since
        )
        fills: list[Fill] = []
        for row in response.json() or []:
            if str(row.get("status", "")) != "FILLED":
                continue
            qty = float(row.get("filledQuantity") or 0)
            if qty <= 0:
                continue
            legs = row.get("orderLegCollection") or [{}]
            instruction = str(legs[0].get("instruction", "BUY")).upper()
            fills.append(
                Fill(
                    broker_order_id=str(row.get("orderId", "?")),
                    symbol=str(legs[0].get("instrument", {}).get("symbol", "?")),
                    side="buy" if "BUY" in instruction else "sell",
                    qty=qty,
                    price=float(row.get("price") or 0),
                    filled_at=datetime.fromisoformat(
                        str(row.get("closeTime") or datetime.now(UTC).isoformat())
                    ),
                )
            )
        return fills

    def replace_stop(self, parent_broker_order_id: str, new_stop: float) -> None:
        raise NotImplementedError(
            "bracket management is Alpaca/Mode B territory; Schwab gets it "
            "if Mode B ever passes §9B.7 and moves here"
        )
