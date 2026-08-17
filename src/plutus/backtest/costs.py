"""Execution cost model (§7).

Cost of one side of a trade = price · (slippage_bps + half_spread_bps)/10⁴
+ commission_per_share. Defaults per spec: 2 bps slippage, zero commission.
"""

from pydantic import BaseModel, Field


class CostModel(BaseModel):
    slippage_bps: float = Field(default=2.0, ge=0)
    half_spread_bps: float = Field(default=0.0, ge=0)
    commission_per_share: float = Field(default=0.0, ge=0)

    def cost_per_share(self, price: float) -> float:
        return price * (self.slippage_bps + self.half_spread_bps) / 1e4 + self.commission_per_share

    def cost_for_trade(self, price: float, shares: float) -> float:
        return shares * self.cost_per_share(price)

    def cost_fraction(self, price: float) -> float:
        """Frictional drag as a fraction of traded notional."""
        return self.cost_per_share(price) / price
