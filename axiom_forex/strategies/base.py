from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Sequence

from app.data.models import ForexQuoteBar


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"
    HOLD = "HOLD"


class Strategy(ABC):
    name: str
    history_limit: int

    @abstractmethod
    def generate_signal(self, bars: Sequence[ForexQuoteBar]) -> Signal:
        """Only completed bars available at decision time may be supplied."""
