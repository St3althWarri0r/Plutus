"""Rebalance executor: weights → floored whole-share orders, dead-band,
sells before buys; and the provisional-close append that lets the 15:50
signal see today (the cache clamps daily bars to completed days)."""

from datetime import UTC, datetime

import pandas as pd
from pytest import approx

from plutus.execution import append_provisional_close, compute_rebalance_orders

PRICES = {"TQQQ": 50.0, "BSV": 80.0, "UVXY": 20.0}


def lookup(symbol: str) -> float | None:
    return PRICES.get(symbol)


def test_full_swing_sells_before_buys_floored() -> None:
    orders = compute_rebalance_orders(
        weights={"TQQQ": 1.0, "BSV": 0.0},
        allocation=10_000.0,
        positions={"BSV": 125.0},
        price_lookup=lookup,
        strategy="tqqq_rotation",
    )
    assert [(o.symbol, o.side, o.qty) for o in orders] == [
        ("BSV", "sell", 125),
        ("TQQQ", "buy", 200),  # floor(10_000 / 50)
    ]
    assert all(o.strategy == "tqqq_rotation" for o in orders)


def test_split_weights_floor_each_leg() -> None:
    orders = compute_rebalance_orders(
        weights={"UVXY": 0.5, "BSV": 0.5},
        allocation=10_000.0,
        positions={},
        price_lookup=lookup,
        strategy="s",
    )
    got = {(o.symbol, o.side, o.qty) for o in orders}
    assert got == {("UVXY", "buy", 250), ("BSV", "buy", 62)}  # floor(5000/80)


def test_dead_band_suppresses_churn() -> None:
    # held 199, target 200 @ $50 → $50 diff, within the default $50 band
    orders = compute_rebalance_orders(
        weights={"TQQQ": 1.0},
        allocation=10_000.0,
        positions={"TQQQ": 199.0},
        price_lookup=lookup,
        strategy="s",
    )
    assert orders == []


def test_unpriceable_symbol_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="price"):
        compute_rebalance_orders(
            weights={"ZZZZ": 1.0},
            allocation=1_000.0,
            positions={},
            price_lookup=lookup,
            strategy="s",
        )


def test_append_provisional_close() -> None:
    idx = pd.to_datetime(["2026-08-13", "2026-08-14"]).tz_localize("UTC")
    closes = pd.DataFrame({"TQQQ": [70.0, 71.0], "BSV": [80.0, 80.1]}, index=idx)

    out = append_provisional_close(
        closes,
        latest_prices={"TQQQ": 72.5, "BSV": 80.2},
        now=datetime(2026, 8, 17, 19, 50, tzinfo=UTC),
    )

    assert len(out) == 3
    assert str(out.index[-1].date()) == "2026-08-17"
    assert out["TQQQ"].iloc[-1] == approx(72.5)
    # original frame untouched
    assert len(closes) == 2


def test_append_provisional_is_noop_when_today_already_present() -> None:
    idx = pd.to_datetime(["2026-08-17"]).tz_localize("UTC")
    closes = pd.DataFrame({"TQQQ": [70.0]}, index=idx)
    out = append_provisional_close(
        closes,
        latest_prices={"TQQQ": 71.0},
        now=datetime(2026, 8, 17, 19, 50, tzinfo=UTC),
    )
    assert len(out) == 1
    assert out["TQQQ"].iloc[-1] == approx(71.0)  # refreshed, not duplicated
