"""Opening-range breakout (§6 Strategy #2): intraday template exercising
bracket orders. Parameters live in strategies.toml, not code. Long-only.
Entry: first close above the range high after the range completes.
Stop: range low. Target: entry + target_r × (entry − stop). Size: floor of
risk_usd / (entry − stop)."""

from pathlib import Path

import pandas as pd
from pytest import approx

from plutus.strategies.orb import OpeningRangeBreakout, OrbConfig, load_orb_config


def bars(closes: list[float], highs: list[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2026-08-17 13:30", periods=len(closes), freq="min", tz="UTC")
    highs = highs or [c + 0.2 for c in closes]
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": 1e5,
        },
        index=idx,
    )


def config() -> OrbConfig:
    return OrbConfig(
        symbols=["SPY", "QQQ"], range_minutes=15, risk_usd=100.0, target_r=2.0
    )


def test_no_signal_while_range_forming() -> None:
    orb = OpeningRangeBreakout(config())
    assert orb.on_minute("SPY", bars([100.0] * 10)) is None


def test_breakout_emits_bracket_intent() -> None:
    orb = OpeningRangeBreakout(config())
    # 15-min range: highs peak 102.2, lows floor 99.8; bar 16 closes above range high
    closes = [100.0, 101.0, 102.0, 101.5, 100.5] * 3 + [102.8]
    frame = bars(closes)

    intent = orb.on_minute("SPY", frame)

    assert intent is not None
    assert intent.side == "buy" and intent.symbol == "SPY"
    assert intent.stop_price == approx(99.8)  # range low
    r = 102.8 - 99.8
    assert intent.take_profit_price == approx(102.8 + 2.0 * r)
    assert intent.qty == float(int(100.0 / r))  # floor(risk / per-share risk)
    assert intent.strategy == "orb"


def test_no_entry_below_range_high_and_one_entry_per_day() -> None:
    orb = OpeningRangeBreakout(config())
    closes = [100.0, 101.0, 102.0, 101.5, 100.5] * 3
    quiet = bars(closes + [101.9])
    assert orb.on_minute("SPY", quiet) is None

    breakout = bars(closes + [102.8])
    assert orb.on_minute("SPY", breakout) is not None
    # second breakout the same day: already entered
    again = bars(closes + [102.8, 103.5])
    assert orb.on_minute("SPY", again) is None
    # a different symbol is independent
    assert orb.on_minute("QQQ", breakout) is not None


def test_config_loads_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "strategies.toml"
    path.write_text(
        """
[orb]
symbols = ["SPY"]
range_minutes = 30
risk_usd = 250.0
target_r = 1.5
"""
    )
    cfg = load_orb_config(path)
    assert cfg.symbols == ["SPY"]
    assert cfg.range_minutes == 30
    assert cfg.risk_usd == 250.0
    assert cfg.target_r == 1.5
