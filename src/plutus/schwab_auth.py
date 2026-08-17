"""Interactive Schwab OAuth (§3): `python -m plutus.schwab_auth`.

Runs schwab-py's login flow in the USER'S browser — their Schwab login never
touches this code. On success, records the refresh-token birth time in the
sidecar so the 7-day health clock is ours. Re-run this every time the health
alert fires (the 7-day re-auth is a recurring manual chore; §3 documents it
as Schwab's known limitation).
"""

from pathlib import Path

from plutus.brokers.schwab import TokenHealth
from plutus.config import get_settings
from plutus.logging_setup import configure_logging, get_logger

log = get_logger("plutus.schwab_auth")

CALLBACK_URL = "https://127.0.0.1:8182"


def main() -> None:  # pragma: no cover - interactive OAuth, user-driven
    configure_logging()
    settings = get_settings()
    if not settings.schwab_app_key or not settings.schwab_app_secret:
        raise SystemExit(
            "SCHWAB_APP_KEY / SCHWAB_APP_SECRET missing from .env — has the "
            "developer.schwab.com app been approved?"
        )
    if not settings.schwab_token_path:
        raise SystemExit("SCHWAB_TOKEN_PATH missing from .env (e.g. schwab_token.json)")

    from schwab.auth import client_from_login_flow

    token_path = Path(settings.schwab_token_path)
    print("A browser window will open — log in to Schwab and approve access.")
    client_from_login_flow(
        settings.schwab_app_key,
        settings.schwab_app_secret,
        CALLBACK_URL,
        str(token_path),
    )
    TokenHealth(token_path).record_refresh()
    print(
        f"Token stored at {token_path}. The refresh token dies in 7 days; "
        "the engine will warn at day 6."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
