from __future__ import annotations

from decimal import Decimal
from statistics import pstdev
from typing import Sequence

from app.data.models import ForexQuoteBar


def mid_closes(bars: Sequence[ForexQuoteBar]) -> tuple[Decimal, ...]:
    return tuple(bar.mid_close for bar in bars)


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:], Decimal(0)) / Decimal(period)


def roc(values: Sequence[Decimal], period: int) -> Decimal | None:
    if period <= 0 or len(values) <= period:
        return None
    previous = values[-period - 1]
    if previous == 0:
        return None
    return values[-1] / previous - Decimal(1)


def realized_volatility(values: Sequence[Decimal], period: int) -> Decimal | None:
    if period <= 1 or len(values) <= period:
        return None
    window = values[-period - 1 :]
    returns = [
        current / previous - Decimal(1)
        for previous, current in zip(window, window[1:])
        if previous != 0
    ]
    if len(returns) < 2:
        return None
    return Decimal(str(pstdev([float(value) for value in returns])))
