"""SchwabAdapter (§3): token-age clock we own, 6d warn / 7d hard-stale gate,
mocked-client mapping, and the Phase 8 live carve-out that must NOT widen
Phase 9's door."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fakes import FakeAdapter
from pytest import approx, raises
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from plutus.brokers.base import OrderIntent, OrderStatus
from plutus.brokers.schwab import SchwabAdapter, SchwabAuthStale, TokenHealth
from plutus.config import Settings
from plutus.db import Base, make_session_factory
from plutus.risk import RiskConfig, RiskManager

NOW = datetime(2026, 8, 17, 18, 0, tzinfo=UTC)


# --- token health -------------------------------------------------------------


def make_health(tmp_path: Path, age_days: float | None) -> TokenHealth:
    token_path = tmp_path / "schwab_token.json"
    token_path.write_text("{}")
    health = TokenHealth(token_path, clock=lambda: NOW)
    if age_days is not None:
        health.record_refresh(now=NOW - timedelta(days=age_days))
    return health


def test_status_progression(tmp_path: Path) -> None:
    assert make_health(tmp_path, 5.0).status() == "ok"
    assert make_health(tmp_path, 6.1).status() == "warn"
    assert make_health(tmp_path, 7.1).status() == "stale"


def test_unknown_age_is_stale_fail_closed(tmp_path: Path) -> None:
    # token file exists but no sidecar (pre-existing token) → cannot verify
    health = make_health(tmp_path, None)
    assert health.status() == "stale"


def test_missing_token_is_unconfigured(tmp_path: Path) -> None:
    health = TokenHealth(tmp_path / "nope.json", clock=lambda: NOW)
    assert health.status() == "unconfigured"


def test_record_refresh_persists_sidecar(tmp_path: Path) -> None:
    make_health(tmp_path, 2.0)
    reloaded = TokenHealth(tmp_path / "schwab_token.json", clock=lambda: NOW)
    assert reloaded.status() == "ok"
    sidecar = json.loads((tmp_path / "schwab_token.json.meta.json").read_text())
    assert "refresh_token_created_at" in sidecar


# --- adapter mapping (mocked client; response shapes are probe-calibrated
# at the LIBRARY level only — the live round-trip is first real validation) ----


class FakeResponse:
    def __init__(self, payload: Any, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        pass


class FakeSchwabClient:
    def __init__(self) -> None:
        self.placed: list[Any] = []
        self.canceled: list[str] = []

    def get_account(self, account_hash: str, *, fields: Any = None) -> FakeResponse:
        payload: dict[str, Any] = {
            "securitiesAccount": {
                "currentBalances": {
                    "liquidationValue": 12_345.67,
                    "cashBalance": 2_000.25,
                    "buyingPower": 4_000.50,
                },
            }
        }
        if fields is not None:
            payload["securitiesAccount"]["positions"] = [
                {
                    "instrument": {"symbol": "VTI"},
                    "longQuantity": 10.0,
                    "shortQuantity": 0.0,
                    "averagePrice": 220.0,
                    "marketValue": 2_250.0,
                }
            ]
        return FakeResponse(payload)

    def place_order(self, account_hash: str, order_spec: Any) -> FakeResponse:
        self.placed.append(order_spec)
        return FakeResponse(
            {}, headers={"Location": "https://api.schwab.com/v1/accounts/h/orders/98765"}
        )

    def get_order(self, order_id: str, account_hash: str) -> FakeResponse:
        return FakeResponse({"status": "FILLED"})

    def cancel_order(self, order_id: str, account_hash: str) -> FakeResponse:
        self.canceled.append(order_id)
        return FakeResponse({})

    def get_orders_for_account(self, account_hash: str, **kw: Any) -> FakeResponse:
        return FakeResponse([])


def make_adapter(tmp_path: Path, age_days: float = 1.0) -> tuple[SchwabAdapter, FakeSchwabClient]:
    client = FakeSchwabClient()
    health = make_health(tmp_path, age_days)
    return SchwabAdapter(client, account_hash="hash-1", token_health=health), client


def test_account_mapping(tmp_path: Path) -> None:
    adapter, _ = make_adapter(tmp_path)
    account = adapter.get_account()
    assert account.equity == approx(12_345.67)
    assert account.cash == approx(2_000.25)
    assert account.buying_power == approx(4_000.50)


def test_positions_mapping(tmp_path: Path) -> None:
    adapter, _ = make_adapter(tmp_path)
    (pos,) = adapter.get_positions()
    assert pos.symbol == "VTI" and pos.qty == 10.0


def test_submit_extracts_order_id_from_location(tmp_path: Path) -> None:
    adapter, client = make_adapter(tmp_path)
    receipt = adapter.submit_order(
        OrderIntent(symbol="VTI", side="buy", qty=1, order_type="market")
    )
    assert receipt.broker_order_id == "98765"
    assert len(client.placed) == 1


def test_order_status_mapping(tmp_path: Path) -> None:
    adapter, _ = make_adapter(tmp_path)
    assert adapter.get_order_status("98765") == OrderStatus.FILLED


def test_stale_auth_refuses_every_call(tmp_path: Path) -> None:
    adapter, client = make_adapter(tmp_path, age_days=7.5)
    with raises(SchwabAuthStale):
        adapter.get_account()
    with raises(SchwabAuthStale):
        adapter.submit_order(
            OrderIntent(symbol="VTI", side="buy", qty=1, order_type="market")
        )
    assert client.placed == []  # never trade on stale auth (§3)


# --- the live carve-out: narrow, both switches physical, alpaca stays shut ----


def live_rm(tmp_path: Path, adapter: object, *, lock: bool) -> RiskManager:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker = make_session_factory(engine)  # type: ignore[type-arg]
    if lock:
        (tmp_path / "live.lock").touch()
    settings = Settings(trading_mode="live", _env_file=None)  # type: ignore[call-arg]
    return RiskManager(
        adapter=adapter,  # type: ignore[arg-type]
        session_factory=factory,
        settings=settings,
        effective_mode="live",
        config=RiskConfig(default_allocation_usd=10_000.0),
        clock=lambda: datetime(2026, 8, 17, 18, 0, tzinfo=UTC),  # Monday 14:00 ET
        runtime_root=tmp_path,
        price_lookup=lambda _s: 100.0,
    )


def test_manual_schwab_test_passes_with_both_switches(tmp_path: Path) -> None:
    adapter, client = make_adapter(tmp_path)
    rm = live_rm(tmp_path, adapter, lock=True)
    row = rm.submit(
        OrderIntent(
            symbol="VTI", side="buy", qty=1, order_type="market",
            strategy="manual_schwab_test",
        )
    )
    assert row.status == "filled" or row.status == "accepted"
    assert len(client.placed) == 1


def test_manual_schwab_test_without_lock_rejected(tmp_path: Path) -> None:
    adapter, client = make_adapter(tmp_path)
    rm = live_rm(tmp_path, adapter, lock=False)
    row = rm.submit(
        OrderIntent(
            symbol="VTI", side="buy", qty=1, order_type="market",
            strategy="manual_schwab_test",
        )
    )
    assert row.status == "rejected"
    assert client.placed == []


def test_alpaca_strategies_still_reject_live_even_with_switches(tmp_path: Path) -> None:
    """The carve-out must not widen Phase 9's door."""
    rm = live_rm(tmp_path, FakeAdapter(), lock=True)
    row = rm.submit(
        OrderIntent(
            symbol="TQQQ", side="buy", qty=1, order_type="market",
            strategy="tqqq_rotation",
        )
    )
    assert row.status == "rejected"
    assert "Phase 9" in (row.reject_reason or "")
