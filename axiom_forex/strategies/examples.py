from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.data.models import ForexQuoteBar
from axiom_forex.indicators import mid_closes, sma
from axiom_forex.strategies.base import Signal, Strategy


@dataclass(frozen=True)
class SmaCrossStrategy(Strategy):
    fast_period: int
    slow_period: int
    name: str = "example_sma_cross"

    def __post_init__(self) -> None:
        if self.fast_period <= 0 or self.slow_period <= 0 or self.fast_period >= self.slow_period:
            raise ValueError("Require 0 < fast_period < slow_period")
        object.__setattr__(self, "history_limit", self.slow_period + 1)

    def generate_signal(self, bars: Sequence[ForexQuoteBar]) -> Signal:
        closes = mid_closes(bars)
        if len(closes) < self.history_limit:
            return Signal.HOLD
        fast = sma(closes, self.fast_period)
        slow = sma(closes, self.slow_period)
        previous_fast = sma(closes[:-1], self.fast_period)
        previous_slow = sma(closes[:-1], self.slow_period)
        if None in (fast, slow, previous_fast, previous_slow):
            return Signal.HOLD
        if previous_fast <= previous_slow and fast > slow:
            return Signal.BUY
        if previous_fast >= previous_slow and fast < slow:
            return Signal.EXIT
        return Signal.HOLD
