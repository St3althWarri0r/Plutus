"""Plaid Investments integration (§4) for M1 and Vanguard — read-only forever.

The user connects institutions themselves through Plaid Link in their own
browser; this code never sees institution credentials, only the exchanged
access token (stored in the gitignored DB). Production Investments access
requires Plaid's approval process — the runbook tells the user to start that
application in parallel, like the spec's own Schwab advice. Until Link runs,
the CSV paste path is the workhorse.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from plutus.aggregation import snapshot_account
from plutus.config import Settings
from plutus.logging_setup import get_logger
from plutus.models import PlaidItem

log = get_logger("plutus.plaid_sync")


class PlaidSync:
    def __init__(
        self,
        *,
        client: Any,
        session_factory: sessionmaker[Session],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def create_link_token(self) -> str:
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.products import Products

        request = LinkTokenCreateRequest(
            products=[Products("investments")],
            client_name="Plutus",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id="plutus-single-user"),
        )
        response = self._client.link_token_create(request)
        return str(response.link_token)

    def exchange_and_store(self, *, public_token: str, institution: str) -> None:
        from plaid.model.item_public_token_exchange_request import (
            ItemPublicTokenExchangeRequest,
        )

        response = self._client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
        with self._session_factory() as session:
            existing = session.scalars(
                select(PlaidItem).where(PlaidItem.institution == institution)
            ).one_or_none()
            if existing is None:
                session.add(
                    PlaidItem(
                        institution=institution,
                        item_id=str(response.item_id),
                        access_token=str(response.access_token),
                    )
                )
            else:
                existing.item_id = str(response.item_id)
                existing.access_token = str(response.access_token)
            session.commit()
        log.info("plaid_item_stored", institution=institution)

    def sync_holdings(self, institution: str) -> bool:
        """Pull holdings for one connected institution into a snapshot.
        Returns False (no-op) when the institution isn't connected."""
        from plaid.model.investments_holdings_get_request import (
            InvestmentsHoldingsGetRequest,
        )

        with self._session_factory() as session:
            item = session.scalars(
                select(PlaidItem).where(PlaidItem.institution == institution)
            ).one_or_none()
            if item is None:
                log.info("plaid_not_connected", institution=institution)
                return False
            token = item.access_token

        response = self._client.investments_holdings_get(
            InvestmentsHoldingsGetRequest(access_token=token)
        )
        securities = {s.security_id: s for s in response.securities}
        positions: list[dict[str, object]] = []
        equity = 0.0
        for holding in response.holdings:
            value = float(holding.institution_value or 0)
            equity += value
            security = securities.get(holding.security_id)
            positions.append(
                {
                    "symbol": getattr(security, "ticker_symbol", None) or "?",
                    "qty": float(holding.quantity or 0),
                    "value": value,
                }
            )
        cash = sum(
            float(getattr(a.balances, "available", 0) or 0) for a in response.accounts
        )
        snapshot_account(
            self._session_factory,
            account=institution,
            equity=equity + cash,
            cash=cash,
            positions=positions,
            now=self._clock(),
        )
        return True


def plaid_sync_from_settings(
    settings: Settings, session_factory: sessionmaker[Session]
) -> PlaidSync | None:  # pragma: no cover - composition, mocked in tests
    if not settings.plaid_client_id or not settings.plaid_secret:
        return None
    import plaid
    from plaid.api import plaid_api

    environment = (
        plaid.Environment.Sandbox
        if settings.plaid_env == "sandbox"
        else plaid.Environment.Production
    )
    configuration = plaid.Configuration(
        host=environment,
        api_key={"clientId": settings.plaid_client_id, "secret": settings.plaid_secret},
    )
    client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
    return PlaidSync(client=client, session_factory=session_factory)
