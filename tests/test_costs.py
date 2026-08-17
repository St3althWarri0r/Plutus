"""Cost model (§7): slippage bps + half-spread bps + per-share commission."""

from pytest import approx

from plutus.backtest.costs import CostModel


def test_default_costs_are_2bps_slippage_no_commission() -> None:
    cm = CostModel()
    assert cm.slippage_bps == 2.0
    assert cm.half_spread_bps == 0.0
    assert cm.commission_per_share == 0.0


def test_cost_per_side_in_dollars() -> None:
    cm = CostModel(slippage_bps=2.0, half_spread_bps=1.0, commission_per_share=0.005)
    # price 100, 10 shares: (2+1) bps of $100 = $0.03/share + $0.005 commission
    assert cm.cost_per_share(price=100.0) == approx(0.035)
    assert cm.cost_for_trade(price=100.0, shares=10) == approx(0.35)


def test_cost_as_return_fraction() -> None:
    cm = CostModel(slippage_bps=2.0)
    # frictional drag as a fraction of traded notional: 2 bps
    assert cm.cost_fraction(price=100.0) == approx(0.0002)

    cm2 = CostModel(slippage_bps=0.0, half_spread_bps=0.0, commission_per_share=0.01)
    # $0.01 commission on a $50 share = 2 bps of notional
    assert cm2.cost_fraction(price=50.0) == approx(0.0002)


def test_zero_cost_model() -> None:
    cm = CostModel(slippage_bps=0.0)
    assert cm.cost_fraction(price=123.45) == 0.0
