"""State packet (§9B.2): server-computed, compact, char-budgeted with
priority truncation — position/orders survive, tape gets cut first."""

import pandas as pd

from plutus.ai.packet import build_state_packet


def bars(n: int, start_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2026-08-17 13:30", periods=n, freq="min", tz="UTC")
    prices = [start_price + 0.1 * i for i in range(n)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 0.2 for p in prices],
            "low": [p - 0.2 for p in prices],
            "close": prices,
            "volume": [10_000] * n,
        },
        index=idx,
    )


def build(tape: str = "quiet open", budget: int = 16_000) -> str:
    return build_state_packet(
        symbol="NVDA",
        bars_1m=bars(45),
        bars_5m=bars(12),
        vwap=101.5,
        rvol=2.3,
        ema9=103.0,
        ema20=102.4,
        premarket_high=104.0,
        premarket_low=99.5,
        prior_high=103.5,
        prior_low=98.0,
        prior_close=100.2,
        position={"qty": 10, "entry": 101.0, "stop": 100.0, "target": 105.0},
        open_orders=["bracket stop 100.0 / target 105.0"],
        session_pnl_r=-0.4,
        trades_today=2,
        time_et="10:42",
        day_plan="NVDA: long bias, ORB above 104",
        tape=tape,
        char_budget=budget,
    )


def test_packet_contains_all_sections() -> None:
    packet = build()
    for token in ["NVDA", "VWAP", "RVOL", "EMA9", "position", "PLAN", "TAPE",
                  "P&L", "-0.4R", "10:42"]:
        assert token in packet or token.lower() in packet.lower()


def test_packet_respects_char_budget() -> None:
    packet = build(tape="x" * 50_000, budget=8_000)
    assert len(packet) <= 8_000


def test_truncation_cuts_tape_before_position() -> None:
    packet = build(tape="TAPEMARKER " * 5_000, budget=6_000)
    assert len(packet) <= 6_000
    assert "'qty': 10" in packet  # position survives untrimmed
    # tape absorbed the entire cut: 5000 markers in, only what fits remains
    assert packet.count("TAPEMARKER") < 500


def test_only_last_30_1m_and_10_5m_bars_summarized() -> None:
    packet = build()
    # first of 45 1m bars (100.0) must have been dropped; last (104.4) kept
    assert "104.4" in packet
    assert "100.0," not in packet.split("TAPE")[0].split("1m bars")[-1].split("5m bars")[0]
