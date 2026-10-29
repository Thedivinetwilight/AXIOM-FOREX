"""Provider-neutral market-data interfaces for later milestones.

M1 defines only the contract.  Normalization, quote-to-bar aggregation,
caching, historical storage, and concrete REST/WSS adapters are M2 work.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import ForexQuote, ForexQuoteBar


@runtime_checkable
class MarketDataProvider(Protocol):
    provider_id: str
    supported_timeframes: frozenset[str]

    def latest_quote(self, provider_symbol: str) -> ForexQuote | None:
        """Return the provider's most recent normalized quote, never an order."""
        ...

    def historical_bars(
        self,
        provider_symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> tuple[ForexQuoteBar, ...]:
        """Return only completed bid/ask bars for the half-open [start, end)."""
        ...
