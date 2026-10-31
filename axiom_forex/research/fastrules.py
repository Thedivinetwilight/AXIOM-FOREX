"""The library's own rules, evaluated a year at a time.

The rules in :mod:`axiom_forex.strategies.library.rules` are the contract, and
they are written for clarity: they take a sequence of bars, build Decimal tuples,
and answer one question.  Called once per bar over 370,000 bars for each of 42
variants, that is roughly an hour of arithmetic -- far outside a ten-minute
research budget.

This module answers the *same* questions incrementally: rolling sums, monotonic
deques and prefix sums over the compact series, in floats.  Two properties keep it
honest rather than merely fast:

1. **No near-threshold guesses.**  Every comparison the rule makes is checked
   against ``TIE_EPSILON``.  If the two sides are closer than that, the result is
   not "probably the same": that bar is recomputed with the real rule on real
   ``ForexQuoteBar`` objects, so the answer is the rule's own.
2. **It is tested against the rule.**  ``tests/test_research_fastrules.py`` walks
   randomized series and asserts that the fast path and the rules produce exactly
   the same signal for every bar and every predeclared variant.

The output is a signal per bar, which the sweep turns into trades.  Nothing here
sizes, prices, or books anything.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Sequence

from axiom_forex.strategies.base import Signal
from axiom_forex.strategies.library.variants import Variant

from .series import Series, TIE_EPSILON

BUY = Signal.BUY
SELL = Signal.SELL
EXIT = Signal.EXIT
HOLD = Signal.HOLD

BPS = 10_000.0

#: How many signals the sweep should expect to recompute exactly.  Reported, so a
#: pathological series (many flat bars) is visible instead of silently slow.
EXACT_FALLBACK_BUDGET = 0.05


def _near(left: float, right: float) -> bool:
    """Two **computed** values are too close for the float comparison to be trusted.

    Exact equality counts here.  Two different decimals can round to the same
    float (a price and a computed midpoint, say), so "equal in float" is not
    evidence of "equal in the rule".
    """

    scale = max(1.0, abs(left), abs(right))
    return abs(left - right) <= TIE_EPSILON * scale


def _raw_near(left: float, right: float) -> bool:
    """Two **recorded prices** are close but not identical.

    Distinct broker prices cannot collide in a double (they differ by at least a
    point), so exact equality is trustworthy; only a near-but-not-equal pair is
    ambiguous.
    """

    difference = abs(left - right)
    if difference == 0.0:
        return False
    scale = max(1.0, abs(left), abs(right))
    return difference <= TIE_EPSILON * scale


@dataclass(frozen=True)
class FastOutcome:
    signals: tuple[Signal, ...]
    fallbacks: int
    bars: int

    @property
    def fallback_fraction(self) -> float:
        return self.fallbacks / self.bars if self.bars else 0.0

    def audit_view(self) -> dict[str, Any]:
        return {
            "bars": self.bars,
            "fallbacks": self.fallbacks,
            "fallback_fraction": round(self.fallback_fraction, 6),
        }


def _exact(series: Series, index: int, variant: Variant, window: int) -> Signal:
    """Recompute one bar with the real rule, on real bars."""

    rule = variant.strategy()
    return rule.generate_signal(series.exact_window(index, window))


def _sma_prefix(values: Sequence[float]) -> list[float]:
    total = 0.0
    out = [0.0]
    for value in values:
        total += value
        out.append(total)
    return out


def _window_sum(prefix: Sequence[float], start: int, end: int) -> float:
    """Sum of ``values[start:end]`` (end exclusive)."""

    if start < 0:
        start = 0
    return prefix[end] - prefix[start]


def _monotonic_max(values: Sequence[float], window: int) -> list[float | None]:
    """max of the ``window`` values ending at each index (inclusive).

    ``None`` only while fewer than ``window`` values exist.  The deque may hold
    fewer entries than the window (that is what makes it monotonic), so its length
    says nothing about availability.
    """

    out: list[float | None] = []
    queue: deque[int] = deque()
    for index, value in enumerate(values):
        while queue and values[queue[-1]] <= value:
            queue.pop()
        queue.append(index)
        while queue[0] <= index - window:
            queue.popleft()
        out.append(values[queue[0]] if index + 1 >= window else None)
    return out


def _monotonic_min(values: Sequence[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    queue: deque[int] = deque()
    for index, value in enumerate(values):
        while queue and values[queue[-1]] >= value:
            queue.pop()
        queue.append(index)
        while queue[0] <= index - window:
            queue.popleft()
        out.append(values[queue[0]] if index + 1 >= window else None)
    return out


def _sma_cross(series: Series, variant: Variant, window: int) -> FastOutcome:
    fast = int(variant.parameters["fast_period"])
    slow = int(variant.parameters["slow_period"])
    closes = series.mid
    prefix = _sma_prefix(closes)
    total = len(closes)
    signals: list[Signal] = []
    fallbacks = 0

    def direction(index: int) -> str | None:
        """'UP'/'DOWN'/'FLAT', or None when the comparison is too close to trust."""

        if index + 1 < slow or index < 0:
            return "FLAT"
        fast_value = _window_sum(prefix, index - fast + 1, index + 1) / fast
        slow_value = _window_sum(prefix, index - slow + 1, index + 1) / slow
        if _near(fast_value, slow_value):
            return None
        if fast_value > slow_value:
            return "UP"
        return "DOWN"

    for index in range(total):
        if index + 1 < slow + 1:
            signals.append(HOLD)
            continue
        now = direction(index)
        before = direction(index - 1)
        if now is None or before is None:
            fallbacks += 1
            signals.append(_exact(series, index, variant, window))
            continue
        if before != "UP" and now == "UP":
            signals.append(BUY)
        elif before != "DOWN" and now == "DOWN":
            signals.append(SELL)
        elif before == "UP" and now != "UP":
            signals.append(EXIT)
        else:
            signals.append(HOLD)
    return FastOutcome(tuple(signals), fallbacks, total)


def _momentum(series: Series, variant: Variant, window: int) -> FastOutcome:
    lookback = int(variant.parameters["lookback"])
    threshold = float(variant.parameters["threshold_bps"])
    closes = series.mid
    signals: list[Signal] = []
    fallbacks = 0
    for index in range(len(closes)):
        # The rule needs ``len(closes) > lookback`` and then reads
        # ``closes[-lookback - 1]``: one bar further back than the window.
        if index < lookback:
            signals.append(HOLD)
            continue
        previous = closes[index - lookback]
        if previous == 0:
            signals.append(HOLD)
            continue
        momentum = (closes[index] / previous - 1.0) * BPS
        if _near(momentum, threshold) or _near(momentum, -threshold):
            fallbacks += 1
            signals.append(_exact(series, index, variant, window))
            continue
        if momentum > threshold:
            signals.append(BUY)
        elif momentum < -threshold:
            signals.append(SELL)
        else:
            signals.append(EXIT)
    return FastOutcome(tuple(signals), fallbacks, len(closes))


def _donchian(series: Series, variant: Variant, window: int) -> FastOutcome:
    lookback = int(variant.parameters["lookback"])
    exit_lookback = int(variant.parameters["exit_lookback"])
    closes = series.mid
    highs = series.mid_high
    lows = series.mid_low
    upper = _monotonic_max(highs, lookback)
    lower = _monotonic_min(lows, lookback)
    exit_upper = _monotonic_max(highs, exit_lookback)
    exit_lower = _monotonic_min(lows, exit_lookback)
    total = len(closes)
    signals: list[Signal] = []
    fallbacks = 0
    for index in range(total):
        if index < lookback:
            signals.append(HOLD)
            continue
        # The bound excludes the current bar: the level was known before it closed.
        top = upper[index - 1] if index - 1 >= 0 else None
        bottom = lower[index - 1] if index - 1 >= 0 else None
        if top is None or bottom is None:
            signals.append(HOLD)
            continue
        close = closes[index]
        if _raw_near(close, top) or _raw_near(close, bottom):
            fallbacks += 1
            signals.append(_exact(series, index, variant, window))
            continue
        if close > top:
            signals.append(BUY)
            continue
        if close < bottom:
            signals.append(SELL)
            continue
        if index >= exit_lookback:
            inner_top = exit_upper[index - 1] if index - 1 >= 0 else None
            inner_bottom = exit_lower[index - 1] if index - 1 >= 0 else None
            if inner_top is None or inner_bottom is None:
                signals.append(HOLD)
                continue
            if _raw_near(close, inner_top) or _raw_near(close, inner_bottom):
                fallbacks += 1
                signals.append(_exact(series, index, variant, window))
                continue
            if inner_bottom < close < inner_top:
                signals.append(EXIT)
                continue
        signals.append(HOLD)
    return FastOutcome(tuple(signals), fallbacks, total)


def _bollinger(series: Series, variant: Variant, window: int) -> FastOutcome:
    period = int(variant.parameters["period"])
    deviations = float(variant.parameters["deviations"])
    closes = series.mid
    prefix = _sma_prefix(closes)
    squares = _sma_prefix([value * value for value in closes])
    total = len(closes)
    signals: list[Signal] = []
    fallbacks = 0
    for index in range(total):
        if index + 1 < period:
            signals.append(HOLD)
            continue
        start = index - period + 1
        mean = _window_sum(prefix, start, index + 1) / period
        variance = max(
            0.0, _window_sum(squares, start, index + 1) / period - mean * mean
        )
        band = math.sqrt(variance) * deviations
        close = closes[index]
        previous = closes[index - 1] if index > 0 else close
        if (
            _near(close, mean - band)
            or _near(close, mean + band)
            or _near(close, mean)
            or _near(previous, mean)
        ):
            fallbacks += 1
            signals.append(_exact(series, index, variant, window))
            continue
        if close < mean - band:
            signals.append(BUY)
        elif close > mean + band:
            signals.append(SELL)
        elif (previous < mean <= close) or (previous > mean >= close):
            signals.append(EXIT)
        else:
            signals.append(HOLD)
    return FastOutcome(tuple(signals), fallbacks, total)


def _atr_breakout(series: Series, variant: Variant, window: int) -> FastOutcome:
    period = int(variant.parameters["atr_period"])
    lookback = int(variant.parameters["lookback"])
    multiple = float(variant.parameters["multiple"])
    closes = series.mid
    highs = series.mid_high
    lows = series.mid_low
    ranges: list[float] = []
    for index in range(1, len(closes)):
        high, low = highs[index], lows[index]
        previous = closes[index - 1]
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    range_prefix = _sma_prefix(ranges)
    upper = _monotonic_max(closes, lookback)
    lower = _monotonic_min(closes, lookback)
    total = len(closes)
    signals: list[Signal] = []
    fallbacks = 0
    for index in range(total):
        if index < lookback or len(ranges) < period or index < period:
            signals.append(HOLD)
            continue
        # ranges[j] describes the step from bar j to j+1, so the period's ranges
        # ending at ``index`` are ranges[index-period:index].
        atr = _window_sum(range_prefix, index - period, index) / period
        top = upper[index - 1] if index - 1 >= 0 else None
        bottom = lower[index - 1] if index - 1 >= 0 else None
        if top is None or bottom is None:
            signals.append(HOLD)
            continue
        close = closes[index]
        gate = atr * multiple
        if (
            _near(close, top + gate)
            or _near(close, bottom - gate)
            or _near(close, (top + bottom) / 2.0)
        ):
            fallbacks += 1
            signals.append(_exact(series, index, variant, window))
            continue
        if close > top + gate:
            signals.append(BUY)
        elif close < bottom - gate:
            signals.append(SELL)
        else:
            midpoint = (top + bottom) / 2.0
            previous = closes[index - 1]
            if _near(previous, midpoint):
                fallbacks += 1
                signals.append(_exact(series, index, variant, window))
            elif (close - midpoint) * (previous - midpoint) < 0:
                signals.append(EXIT)
            else:
                signals.append(HOLD)
    return FastOutcome(tuple(signals), fallbacks, total)


def _session(series: Series, variant: Variant, window: int) -> FastOutcome:
    hour = int(variant.parameters["session_hour_utc"])
    count = int(variant.parameters["range_bars"])
    buffer_bps = float(variant.parameters["buffer_bps"])
    closes = series.mid
    highs = series.mid_high
    lows = series.mid_low
    stamps = series.timestamps
    total = len(closes)
    signals: list[Signal] = []
    fallbacks = 0
    for index in range(total):
        moment = stamps[index].astimezone(timezone.utc)
        if moment.hour != hour:
            signals.append(HOLD)
            continue
        start = max(0, index - window + 1)
        session = [position for position in range(start, index + 1) if stamps[position].hour == hour]
        if len(session) < count + 1:
            signals.append(HOLD)
            continue
        opening = session[:count]
        top = max(highs[position] for position in opening)
        bottom = min(lows[position] for position in opening)
        close = closes[index]
        buffer = (top - bottom) * buffer_bps / BPS
        previous = closes[session[-2]]
        if _near(close, top + buffer) or _near(close, bottom - buffer):
            fallbacks += 1
            signals.append(_exact(series, index, variant, window))
            continue
        if close > top + buffer:
            signals.append(BUY)
        elif close < bottom - buffer:
            signals.append(SELL)
        elif bottom < close < top and not (bottom < previous < top):
            signals.append(EXIT)
        else:
            signals.append(HOLD)
    return FastOutcome(tuple(signals), fallbacks, total)


_EVALUATORS = {
    "sma_cross": _sma_cross,
    "time_series_momentum": _momentum,
    "donchian_breakout": _donchian,
    "bollinger_reversion": _bollinger,
    "atr_breakout": _atr_breakout,
    "session_range_breakout": _session,
}


def signals_for(series: Series, variant: Variant) -> FastOutcome:
    """Every bar's signal for one variant, with the exact fallbacks counted."""

    rule_name = variant.card.rule
    evaluator = _EVALUATORS.get(rule_name)
    if evaluator is None:  # pragma: no cover - every card is mapped above
        raise ValueError(f"no fast evaluator for {rule_name}")
    window = int(variant.strategy().history_limit)
    return evaluator(series, variant, window)


__all__ = [
    "EXACT_FALLBACK_BUDGET",
    "FastOutcome",
    "signals_for",
]
