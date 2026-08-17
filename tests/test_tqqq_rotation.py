"""Strategy #1 — the §6 decision tree, ported exactly.

    IF TQQQ close > TQQQ 200d SMA:
        IF RSI(10, TQQQ) > 79:  50% UVXY / 50% BSV
        ELSE:                   100% TQQQ
    ELSE:
        IF RSI(10, TQQQ) < 31:  100% TECL
        ELIF TQQQ close > TQQQ 20d SMA:  100% TQQQ
        ELSE:                   100% of higher-RSI(10) of {BSV, SQQQ}

Thresholds are strict (> 79, < 31) per spec; the boundary tests pin that.
Tests use shortened windows (5/3/3) — identical logic, small fixtures.
"""

import numpy as np
import pandas as pd
from pytest import approx

from plutus.strategies.tqqq_rotation import TQQQRotation, branch_weights

OB, OS = 79.0, 31.0


# --- the tree itself, exhaustively -------------------------------------------


def test_bull_calm_holds_tqqq() -> None:
    w = branch_weights(True, 60.0, True, 50.0, 50.0, OB, OS)
    assert w == {"TQQQ": 1.0}


def test_bull_overbought_hedges_uvxy_bsv() -> None:
    w = branch_weights(True, 79.5, True, 50.0, 50.0, OB, OS)
    assert w == {"UVXY": 0.5, "BSV": 0.5}


def test_overbought_boundary_is_strict() -> None:
    # RSI exactly 79 → NOT overbought → hold TQQQ
    assert branch_weights(True, 79.0, True, 50.0, 50.0, OB, OS) == {"TQQQ": 1.0}


def test_bear_oversold_buys_tecl() -> None:
    w = branch_weights(False, 30.0, False, 50.0, 50.0, OB, OS)
    assert w == {"TECL": 1.0}


def test_oversold_boundary_is_strict() -> None:
    # RSI exactly 31 → NOT oversold → fall through to the 20d check
    assert branch_weights(False, 31.0, True, 50.0, 50.0, OB, OS) == {"TQQQ": 1.0}


def test_bear_recovery_above_short_sma_holds_tqqq() -> None:
    assert branch_weights(False, 50.0, True, 50.0, 50.0, OB, OS) == {"TQQQ": 1.0}


def test_bear_chop_picks_higher_rsi_of_bsv_sqqq() -> None:
    assert branch_weights(False, 50.0, False, 60.0, 40.0, OB, OS) == {"BSV": 1.0}
    assert branch_weights(False, 50.0, False, 40.0, 60.0, OB, OS) == {"SQQQ": 1.0}


def test_bear_chop_tie_breaks_to_bsv() -> None:
    assert branch_weights(False, 50.0, False, 50.0, 50.0, OB, OS) == {"BSV": 1.0}


# --- weights_history integration ---------------------------------------------


def make_strategy() -> TQQQRotation:
    return TQQQRotation(sma_long=5, sma_short=3, rsi_period=3)


def frame(tqqq: list[float], n: int | None = None) -> pd.DataFrame:
    n = n or len(tqqq)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    flat = [100.0] * n
    return pd.DataFrame(
        {"TQQQ": tqqq, "UVXY": flat, "BSV": flat, "TECL": flat, "SQQQ": flat}, index=idx
    )


def test_warmup_rows_are_flat() -> None:
    closes = frame([100 + i for i in range(10)])
    weights = make_strategy().weights_history(closes)
    # sma_long=5 needs 5 bars; rsi(3) needs 3 changes — first 4 rows must be flat
    assert weights.iloc[:4].to_numpy().sum() == 0.0


def test_relentless_rally_ends_overbought_hedged() -> None:
    closes = frame([100 + 2 * i for i in range(12)])
    weights = make_strategy().weights_history(closes)
    last = weights.iloc[-1]
    # straight up: above SMA5, RSI3=100 > 79 → hedge
    assert last["UVXY"] == approx(0.5)
    assert last["BSV"] == approx(0.5)


def test_crash_ends_in_tecl() -> None:
    closes = frame([200 - 8 * i for i in range(12)])
    weights = make_strategy().weights_history(closes)
    last = weights.iloc[-1]
    # straight down: below SMA5, RSI3=0 < 31 → TECL
    assert last["TECL"] == approx(1.0)


def test_post_warmup_weights_sum_to_one_within_universe() -> None:
    rng = np.random.default_rng(7)
    prices = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, size=120)))
    closes = frame(list(prices))
    weights = make_strategy().weights_history(closes)
    post = weights.iloc[5:]
    assert post.sum(axis=1).to_numpy() == approx(np.ones(len(post)))
    assert set(weights.columns) == {"TQQQ", "UVXY", "BSV", "TECL", "SQQQ"}
