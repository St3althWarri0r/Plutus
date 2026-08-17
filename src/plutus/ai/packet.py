"""Server-computed state packet for Mode B decisions (§9B.2).

Compact by construction (bars as terse OHLCV lines, floats rounded) and
char-budgeted with priority truncation: position/orders and session numbers
always survive; the plan trims before them; bars before the plan; the tape
is cut first. ~4 chars/token makes the default 16k chars ≈ 4k tokens.
"""

import pandas as pd

DEFAULT_CHAR_BUDGET = 16_000


def _bars_block(frame: pd.DataFrame, keep: int) -> str:
    tail = frame.tail(keep)
    lines = []
    for ts, row in zip(pd.DatetimeIndex(tail.index), tail.to_numpy(), strict=True):
        o, h, lo, c, v = row[:5]
        lines.append(f"{ts.strftime('%H:%M')} {o:.2f}/{h:.2f}/{lo:.2f}/{c:.2f} v{int(v)}")
    return "\n".join(lines)


def build_state_packet(
    *,
    symbol: str,
    bars_1m: pd.DataFrame,
    bars_5m: pd.DataFrame,
    vwap: float | None,
    rvol: float | None,
    ema9: float | None,
    ema20: float | None,
    premarket_high: float | None,
    premarket_low: float | None,
    prior_high: float | None,
    prior_low: float | None,
    prior_close: float | None,
    position: dict[str, float] | None,
    open_orders: list[str],
    session_pnl_r: float,
    trades_today: int,
    time_et: str,
    day_plan: str,
    tape: str,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> str:
    def fmt(x: float | None) -> str:
        return f"{x:.2f}" if x is not None else "n/a"

    head = "\n".join(
        [
            f"SYMBOL {symbol} | time ET {time_et} | session P&L {session_pnl_r:+.1f}R "
            f"| trades today {trades_today}",
            f"VWAP {fmt(vwap)} | RVOL {fmt(rvol)} | EMA9 {fmt(ema9)} | EMA20 {fmt(ema20)}",
            f"PM high/low {fmt(premarket_high)}/{fmt(premarket_low)} | "
            f"prior H/L/C {fmt(prior_high)}/{fmt(prior_low)}/{fmt(prior_close)}",
            f"position: {position if position is not None else 'flat'}",
            f"open orders: {open_orders or 'none'}",
        ]
    )
    plan_block = f"PLAN:\n{day_plan}"
    bars_block = (
        f"1m bars (last 30):\n{_bars_block(bars_1m, 30)}\n"
        f"5m bars (last 10):\n{_bars_block(bars_5m, 10)}"
    )
    tape_block = f"TAPE:\n{tape}"

    # priority truncation: head is untouchable; tape gives way first, then
    # bars, then the plan
    plan_b, bars_b, tape_b = plan_block, bars_block, tape_block

    def total() -> int:
        return len(head) + len(plan_b) + len(bars_b) + len(tape_b) + 4

    if total() > char_budget:
        overshoot = total() - char_budget
        cut = min(overshoot, max(0, len(tape_b) - len("TAPE:\n")))
        tape_b = tape_b[: len(tape_b) - cut]
    if total() > char_budget:
        overshoot = total() - char_budget
        cut = min(overshoot, max(0, len(bars_b) - 600))
        bars_b = bars_b[: len(bars_b) - cut]
    if total() > char_budget:
        overshoot = total() - char_budget
        cut = min(overshoot, max(0, len(plan_b) - 200))
        plan_b = plan_b[: len(plan_b) - cut]

    packet = "\n".join([head, plan_b, bars_b, tape_b])
    return packet[:char_budget]
