"""Phase 8 acceptance: one clean LIVE Schwab round-trip, manually triggered.

`python -m plutus.schwab_roundtrip SYMBOL`

This is real money. It runs ONLY when the user has personally armed both
platform switches — TRADING_MODE=live in .env and a live.lock file THEY
created (§13: code never creates live.lock) — and then types the exact
confirmation naming the order. The order routes through the RiskManager like
every other order (this script is the documented §12 exception to rule 5's
'no test scripts' — it exists to exercise that path, not bypass it). After
submit it polls for the fill; unfilled after the window, it cancels.
"""

import sys
import time
from pathlib import Path

from plutus.brokers.base import OrderIntent, OrderStatus
from plutus.brokers.schwab import SchwabAdapter, TokenHealth
from plutus.config import get_settings, resolve_runtime_root
from plutus.db import make_engine, make_session_factory
from plutus.logging_setup import configure_logging, get_logger
from plutus.risk import RiskManager

log = get_logger("plutus.schwab_roundtrip")

POLL_SECONDS = 60


def main() -> None:  # pragma: no cover - live-money manual script
    configure_logging()
    settings = get_settings()
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "").upper().strip()
    if not symbol:
        raise SystemExit("usage: python -m plutus.schwab_roundtrip SYMBOL")

    root = resolve_runtime_root(settings)
    if settings.trading_mode != "live":
        raise SystemExit("TRADING_MODE=live is not set — set it in .env yourself.")
    if not (root / "live.lock").is_file():
        raise SystemExit(
            "live.lock is absent. Create it YOURSELF (touch live.lock) — "
            "code never will (§13)."
        )
    if not settings.schwab_token_path:
        raise SystemExit("SCHWAB_TOKEN_PATH missing — run python -m plutus.schwab_auth")

    from schwab.auth import client_from_token_file

    token_path = Path(settings.schwab_token_path)
    health = TokenHealth(token_path)
    if health.status() != "ok":
        raise SystemExit(f"Schwab token status is {health.status()!r} — re-auth first.")

    client = client_from_token_file(
        str(token_path), settings.schwab_app_key, settings.schwab_app_secret
    )
    hashes = client.get_account_numbers().json()
    account_hash = hashes[0]["hashValue"]
    adapter = SchwabAdapter(client, account_hash=account_hash, token_health=health)

    account = adapter.get_account()
    print(f"Schwab account equity ${account.equity:,.2f}, cash ${account.cash:,.2f}")
    phrase = f"BUY 1 {symbol} LIVE"
    typed = input(f"This places a REAL order. Type '{phrase}' to proceed: ").strip()
    if typed != phrase:
        raise SystemExit("confirmation mismatch — nothing sent.")

    def quote_price(sym: str) -> float | None:
        # a real quote so the FULL §8 gate chain (incl. priced gates) runs on
        # this live order — better acceptance evidence than a keyhole bypass
        try:
            payload = client.get_quote(sym).json()
            entry = payload.get(sym, {})
            quote = entry.get("quote", entry)
            price = quote.get("lastPrice") or quote.get("mark") or quote.get("askPrice")
            return float(price) if price else None
        except Exception:
            return None

    risk = RiskManager(
        adapter=adapter,
        session_factory=make_session_factory(make_engine()),
        settings=settings,
        effective_mode="live",
        price_lookup=quote_price,
    )
    intent = OrderIntent(
        symbol=symbol, side="buy", qty=1, order_type="market",
        strategy="manual_schwab_test",
    )
    row = risk.submit(intent)
    if row.status == "rejected":
        raise SystemExit(f"REJECTED: {row.reject_reason}")
    print(f"submitted — broker order {row.broker_order_id}; polling {POLL_SECONDS}s…")

    assert row.broker_order_id is not None
    deadline = time.monotonic() + POLL_SECONDS
    while time.monotonic() < deadline:
        status = adapter.get_order_status(row.broker_order_id)
        print(f"  status: {status}")
        if status == OrderStatus.FILLED:
            print("ROUND TRIP CLEAN — filled. Remove live.lock and reset "
                  "TRADING_MODE=paper now.")
            return
        if status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
            raise SystemExit(f"order ended {status} — round trip NOT clean.")
        time.sleep(5)
    print("unfilled after window — canceling…")
    adapter.cancel_order(row.broker_order_id)
    print("canceled. Round trip incomplete; try during RTH with a liquid symbol.")


if __name__ == "__main__":  # pragma: no cover
    main()
