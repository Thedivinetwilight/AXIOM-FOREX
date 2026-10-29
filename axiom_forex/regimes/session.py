from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from app.calendar.trading import TradingCalendar
from app.data.models import ForexQuoteBar
from axiom_forex.indicators import mid_closes, realized_volatility, roc


class RegimeLabel(str, Enum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGE_HIGH_VOL = "RANGE_HIGH_VOL"
    RANGE_LOW_VOL = "RANGE_LOW_VOL"


@dataclass(frozen=True)
class RegimeSnapshot:
    label: RegimeLabel
    momentum: object
    volatility: object
    samples: int
    market_open: bool

    def audit_view(self) -> dict[str, object]:
        return {
            "label": self.label.value,
            "momentum": self.momentum,
            "volatility": self.volatility,
            "samples": self.samples,
            "market_open": self.market_open,
        }


def classify_regime(
    bars: Sequence[ForexQuoteBar],
    *,
    calendar: TradingCalendar,
    pair: str,
) -> RegimeSnapshot:
    closes = mid_closes(bars)
    momentum = roc(closes, 5)
    volatility = realized_volatility(closes, 5)
    market_open = calendar.status(pair, bars[-1].timestamp).is_open if bars else False
    if momentum is None or volatility is None:
        label = RegimeLabel.INSUFFICIENT_DATA
    elif momentum > 0 and volatility < momentum:
        label = RegimeLabel.TRENDING_UP
    elif momentum < 0 and volatility < abs(momentum):
        label = RegimeLabel.TRENDING_DOWN
    elif volatility >= abs(momentum):
        label = RegimeLabel.RANGE_HIGH_VOL
    else:
        label = RegimeLabel.RANGE_LOW_VOL
    return RegimeSnapshot(label, momentum, volatility, len(closes), market_open)
