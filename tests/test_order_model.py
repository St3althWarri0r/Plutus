"""Order rows persist intent + broker state; idempotency_key is unique."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from plutus.db import Base
from plutus.models import Order


def _order(key: str) -> Order:
    return Order(
        idempotency_key=key,
        symbol="SPY",
        side="buy",
        qty=1,
        order_type="market",
        strategy="manual",
        trading_mode="paper",
        status="new",
    )


def test_duplicate_idempotency_key_rejected() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_order("k-1"))
        session.commit()
        session.add(_order("k-1"))
        with pytest.raises(IntegrityError):
            session.commit()
