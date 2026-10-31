"""The rule implementations behind the cards.

Every function has the same shape: it receives the *completed* bars that ended at
or before the decision instant -- never the future -- and returns one
:class:`~axiom_forex.strategies.base.Signal`.  They hold no state, so a restart
reaches the same decision from the same bars, and they invent no price: the mid
close comes from the bar, and the spread is the bar's own bid/ask.

Parameter values arrive from a card's predeclared grid.  A value outside its
declared bounds is a programming error here, not something to clamp silently.
"""

from __future__ import annotations

from decimal import Decimal
from statistics import pstdev
from typing import Any, Callable, Sequence

from app.data.models import ForexQuoteBar

from axiom_forex.strategies.base import Signal

BPS = Decimal(10_000)


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _closes(bars: Sequence[ForexQuoteBar]) -> tuple[Decimal, ...]:
    return tuple(bar.mid_close for bar in bars)


def _highs(bars: Sequence[ForexQuoteBar]) -> tuple[Decimal, ...]:
    return tuple((bar.bid.high + bar.ask.high) / Decimal(2) for bar in bars)


def _lows(bars: Sequence[ForexQuoteBar]) -> tuple[Decimal, ...]:
    return tuple((bar.bid.low + bar.ask.low) / Decimal(2) for bar in bars)


def _sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:], Decimal(0)) / Decimal(period)


def _direction(closes: Sequence[Decimal], fast_period: int, slow_period: int) -> str:
    """'UP', 'DOWN' or 'FLAT' from completed closes only."""

    fast = _sma(closes, fast_period)
    slow = _sma(closes, slow_period)
    if fast is None or slow is None:
        return "FLAT"
    if fast > slow:
        return "UP"
    if fast < slow:
        return "DOWN"
    return "FLAT"


# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------


def sma_cross(bars: Sequence[ForexQuoteBar], *, fast_period: int, slow_period: int) -> Signal:
    if int(fast_period) <= 0 or int(fast_period) >= int(slow_period):
        raise ValueError("require 0 < fast_period < slow_period")
    closes = _closes(bars)
    if len(closes) < int(slow_period) + 1:
        return Signal.HOLD
    now = _direction(closes, int(fast_period), int(slow_period))
    before = _direction(closes[:-1], int(fast_period), int(slow_period))
    if before != "UP" and now == "UP":
        return Signal.BUY
    if before != "DOWN" and now == "DOWN":
        # The strategy is long/short symmetric: a fresh down-cross is a short entry,
        # and the runner's exit of a long is its own decision.
        return Signal.SELL
    if before == "UP" and now != "UP":
        return Signal.EXIT
    return Signal.HOLD


def time_series_momentum(
    bars: Sequence[ForexQuoteBar], *, lookback: int, threshold_bps: Decimal
) -> Signal:
    closes = _closes(bars)
    window = int(lookback)
    if window <= 0 or len(closes) <= window:
        return Signal.HOLD
    previous = closes[-window - 1]
    if previous == 0:
        return Signal.HOLD
    momentum_bps = (closes[-1] / previous - Decimal(1)) * BPS
    threshold = _dec(threshold_bps)
    if momentum_bps > threshold:
        return Signal.BUY
    if momentum_bps < -threshold:
        return Signal.SELL
    if abs(momentum_bps) <= threshold:
        # No conviction: a held position is closed rather than carried on noise.
        return Signal.EXIT
    return Signal.HOLD


# ---------------------------------------------------------------------------
# breakout
# ---------------------------------------------------------------------------


def donchian_breakout(
    bars: Sequence[ForexQuoteBar], *, lookback: int, exit_lookback: int
) -> Signal:
    window = int(lookback)
    exit_window = int(exit_lookback)
    if window < 2 or exit_window < 1:
        raise ValueError("require lookback >= 2 and exit_lookback >= 1")
    closes = _closes(bars)
    highs = _highs(bars)
    lows = _lows(bars)
    if len(closes) < window + 1:
        return Signal.HOLD
    # The bound is built from the bars *before* this one, so a close can only be
    # compared with a level that was already known.
    upper = max(highs[-window - 1 : -1])
    lower = min(lows[-window - 1 : -1])
    close = closes[-1]
    if close > upper:
        return Signal.BUY
    if close < lower:
        return Signal.SELL
    if len(closes) >= exit_window + 1:
        exit_upper = max(highs[-exit_window - 1 : -1])
        exit_lower = min(lows[-exit_window - 1 : -1])
        if exit_lower < close < exit_upper:
            return Signal.EXIT
    return Signal.HOLD


# ---------------------------------------------------------------------------
# mean reversion
# ---------------------------------------------------------------------------


def bollinger_reversion(
    bars: Sequence[ForexQuoteBar], *, period: int, deviations: Decimal
) -> Signal:
    window = int(period)
    if window < 5:
        raise ValueError("require period >= 5")
    closes = _closes(bars)
    if len(closes) < window:
        return Signal.HOLD
    sample = closes[-window:]
    mid = sum(sample, Decimal(0)) / Decimal(window)
    spread = Decimal(str(pstdev([float(value) for value in sample])))
    band = spread * _dec(deviations)
    close = closes[-1]
    previous = closes[-2] if len(closes) > 1 else close
    if close < mid - band:
        return Signal.BUY
    if close > mid + band:
        return Signal.SELL
    # Reversion back through the middle closes the trade.
    if (previous < mid <= close) or (previous > mid >= close):
        return Signal.EXIT
    return Signal.HOLD


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------


def _true_ranges(bars: Sequence[ForexQuoteBar]) -> tuple[Decimal, ...]:
    highs = _highs(bars)
    lows = _lows(bars)
    closes = _closes(bars)
    values: list[Decimal] = []
    for index in range(1, len(bars)):
        high, low = highs[index], lows[index]
        previous = closes[index - 1]
        values.append(max(high - low, abs(high - previous), abs(low - previous)))
    return tuple(values)


def atr_breakout(
    bars: Sequence[ForexQuoteBar],
    *,
    atr_period: int,
    lookback: int,
    multiple: Decimal,
) -> Signal:
    period = int(atr_period)
    window = int(lookback)
    if period < 2 or window < 2:
        raise ValueError("require atr_period >= 2 and lookback >= 2")
    closes = _closes(bars)
    ranges = _true_ranges(bars)
    if len(ranges) < period or len(closes) < window + 1:
        return Signal.HOLD
    atr = sum(ranges[-period:], Decimal(0)) / Decimal(period)
    upper = max(closes[-window - 1 : -1])
    lower = min(closes[-window - 1 : -1])
    close = closes[-1]
    gate = atr * _dec(multiple)
    if close > upper + gate:
        return Signal.BUY
    if close < lower - gate:
        return Signal.SELL
    midpoint = (upper + lower) / Decimal(2)
    if (close - midpoint) * (closes[-2] - midpoint) < 0:
        return Signal.EXIT
    return Signal.HOLD


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


def session_range_breakout(
    bars: Sequence[ForexQuoteBar],
    *,
    session_hour_utc: int,
    range_bars: int,
    buffer_bps: Decimal,
) -> Signal:
    """Range from the first ``range_bars`` bars of a UTC hour, then trade its break.

    The window is derived from the bar timestamps, so the rule is decidable from
    the data alone.  The card's assumptions say plainly that a UTC hour is not a
    broker calendar session.
    """

    hour = int(session_hour_utc)
    count = int(range_bars)
    if not 0 <= hour <= 23:
        raise ValueError("require 0 <= session_hour_utc <= 23")
    if count < 1:
        raise ValueError("require range_bars >= 1")
    if len(bars) < count + 1:
        return Signal.HOLD
    current = bars[-1]
    if current.timestamp.hour != hour:
        return Signal.HOLD
    # Bars in this session so far, in order.
    session = [bar for bar in bars if bar.timestamp.hour == hour]
    if len(session) < count + 1:
        return Signal.HOLD
    opening = session[:count]
    high = max(_highs(opening))
    low = min(_lows(opening))
    close = current.mid_close
    buffer = (high - low) * _dec(buffer_bps) / BPS
    if close > high + buffer:
        return Signal.BUY
    if close < low - buffer:
        return Signal.SELL
    previous = session[-2].mid_close if len(session) > 1 else close
    if low < close < high and not (low < previous < high):
        return Signal.EXIT
    return Signal.HOLD


RULES: dict[str, Callable[..., Signal]] = {
    "sma_cross": sma_cross,
    "time_series_momentum": time_series_momentum,
    "donchian_breakout": donchian_breakout,
    "bollinger_reversion": bollinger_reversion,
    "atr_breakout": atr_breakout,
    "session_range_breakout": session_range_breakout,
}


def rule(name: str) -> Callable[..., Signal] | None:
    return RULES.get(str(name or ""))


__all__ = [
    "RULES",
    "atr_breakout",
    "bollinger_reversion",
    "donchian_breakout",
    "rule",
    "session_range_breakout",
    "sma_cross",
    "time_series_momentum",
]
