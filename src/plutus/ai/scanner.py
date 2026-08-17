"""Deterministic pre-market scanner (§9B.1) — candidates in, watchlist out.

Candidates come from Alpaca's screener (most actives ∪ movers) plus the
static momentum list in playbook.yaml; the filters are IEX-scaled. The spread
filter is omitted: IEX quotes are not the NBBO, so a 10 bps check against
them would be noise (documented deviation).
"""

from pydantic import BaseModel

from plutus.ai.mode_b_config import ScannerConfig
from plutus.logging_setup import get_logger

log = get_logger("plutus.ai.scanner")


class Candidate(BaseModel):
    symbol: str
    gap_pct: float  # signed; the filter uses |gap|
    premarket_volume: float  # IEX shares
    price: float
    adv_iex: float  # 20-day average daily volume, IEX shares


def filter_candidates(
    candidates: list[Candidate], config: ScannerConfig
) -> list[Candidate]:
    kept: list[Candidate] = []
    for c in candidates:
        if abs(c.gap_pct) < config.min_gap_pct:
            continue
        if c.premarket_volume < config.premarket_volume_min_iex:
            continue
        if not (config.price_min <= c.price <= config.price_max):
            continue
        if c.adv_iex < config.adv_min_iex:
            continue
        kept.append(c)
    kept.sort(key=lambda c: abs(c.gap_pct), reverse=True)
    log.info("scanner_filtered", kept=[c.symbol for c in kept])
    return kept
