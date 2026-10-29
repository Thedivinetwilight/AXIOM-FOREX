"""Deterministic local fixture provider; not a broker/data-provider recommendation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from app.accounting.fx import ConversionRate, ConversionRateBook
from app.calendar.trading import TradingCalendar
from app.common.validation import as_utc
from app.data.models import ForexQuote, ForexQuoteBar, QuoteFreshness, QuoteSideBar
from app.universe.specs import CurrencyPairSpec, InstrumentCatalog


BAR_SOURCE = "SYNTHETIC_FIXTURE_BAR_SOURCE"
QUOTE_SOURCE = "SYNTHETIC_FIXTURE_QUOTE_SOURCE"


def _bar(
    *,
    spec: CurrencyPairSpec,
    timestamp: datetime,
    index: int,
) -> ForexQuoteBar:
    pip = spec.pip_size_decimal()
    if index < 15:
        mid = Decimal("1.10000") - Decimal(index) * pip
    else:
        mid = Decimal("1.09850") + Decimal(index - 15) * pip * Decimal(2)
    bid_close = mid - pip
    ask_close = mid + pip
    wiggle = pip * Decimal(index % 3) / Decimal(2)
    bid_open = bid_close - wiggle
    ask_open = ask_close + wiggle
    bid_high = max(bid_open, bid_close) + pip / Decimal(2)
    bid_low = min(bid_open, bid_close) - pip / Decimal(2)
    ask_high = max(ask_open, ask_close) + pip / Decimal(2)
    ask_low = min(ask_open, ask_close) - pip / Decimal(2)
    bar = ForexQuoteBar(
        pair=spec.canonical_pair or "",
        timeframe="1m",
        timestamp=timestamp,
        duration_seconds=60,
        bid=QuoteSideBar(
            open=bid_open, high=bid_high, low=bid_low, close=bid_close
        ),
        ask=QuoteSideBar(
            open=ask_open, high=ask_high, low=ask_low, close=ask_close
        ),
        source=BAR_SOURCE,
        quote_count=10,
        last_quote_at=timestamp + timedelta(seconds=59),
        complete=True,
    )
    validation = bar.validate(spec)
    if not validation.valid:
        raise ValueError("Invalid synthetic bar: " + validation.reason)
    return bar


def build_synthetic_bars(
    *,
    spec: CurrencyPairSpec,
    calendar: TradingCalendar,
    start_at: datetime,
    count: int,
) -> tuple[ForexQuoteBar, ...]:
    if count <= 0:
        raise ValueError("count must be positive")
    cursor, error = as_utc(start_at, "start_at")
    if error or cursor is None:
        raise ValueError(error or "invalid_start_at")
    pair = spec.canonical_pair or ""
    bars: list[ForexQuoteBar] = []
    steps = 0
    max_steps = count * 10000 + 10000
    while len(bars) < count and steps < max_steps:
        if calendar.status(pair, cursor).is_open:
            bars.append(_bar(spec=spec, timestamp=cursor, index=len(bars)))
        cursor += timedelta(minutes=1)
        steps += 1
    if len(bars) != count:
        raise ValueError("Calendar did not provide enough open minutes")
    return tuple(bars)


def make_backtest_conversion_book(
    bars: tuple[ForexQuoteBar, ...],
    *,
    spec: CurrencyPairSpec,
    account_currency: str,
    quote_rate: Decimal,
    base_rate: Decimal,
    source: str,
) -> ConversionRateBook:
    if not bars:
        raise ValueError("Empty bars")
    timestamp = bars[0].timestamp
    span = (bars[-1].end_at - timestamp).total_seconds() + 10
    freshness = QuoteFreshness(
        policy_id="synthetic-backtest-rate-freshness",
        version="fixture-v1",
        max_age_seconds=Decimal(str(span)),
        max_future_skew_seconds=Decimal(1),
        max_feed_latency_seconds=Decimal(1),
    )
    rates = {
        (spec.quote_currency, account_currency): ConversionRate(
            spec.quote_currency, account_currency, quote_rate, timestamp, source, "quote-rate"
        ),
        (spec.base_currency, account_currency): ConversionRate(
            spec.base_currency, account_currency, base_rate, timestamp, source, "base-rate"
        ),
    }
    return ConversionRateBook(source=source, freshness=freshness, rates=rates)


def _side_payload(side: QuoteSideBar) -> dict[str, str]:
    return {
        point: str(getattr(side, point))
        for point in ("open", "high", "low", "close")
    }


def synthetic_bar_payloads(
    bars: tuple[ForexQuoteBar, ...], *, provider_symbol: str
) -> tuple[dict[str, Any], ...]:
    """Serialize local bars into strict provider-shaped raw payloads."""
    payloads: list[dict[str, Any]] = []
    for bar in bars:
        payloads.append(
            {
                "symbol": provider_symbol,
                "timestamp": bar.timestamp.isoformat(),
                "last_quote_at": bar.last_quote_at.isoformat(),
                "timeframe": bar.timeframe,
                "duration_seconds": bar.duration_seconds,
                "bid": _side_payload(bar.bid),
                "ask": _side_payload(bar.ask),
                "source": bar.source,
                "quote_count": bar.quote_count,
                "complete": bar.complete,
            }
        )
    return tuple(payloads)


def synthetic_quote_payloads(
    bars: tuple[ForexQuoteBar, ...],
    *,
    provider_symbol: str,
    quote_source: str = QUOTE_SOURCE,
    quote_id_prefix: str = "fixture-quote",
    metadata: dict[str, str] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Serialize the last quote of each bar into strict raw quote payloads.

    ``quote_source`` and ``quote_id_prefix`` let an imported dataset round-trip
    through the normalizer without being relabeled as a synthetic fixture.
    """
    payloads: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        payloads.append(
            {
                "symbol": provider_symbol,
                "timestamp": bar.last_quote_at.isoformat(),
                "received_at": (
                    bar.last_quote_at + timedelta(milliseconds=100)
                ).isoformat(),
                "bid": str(bar.bid.close),
                "ask": str(bar.ask.close),
                "source": quote_source,
                "quote_id": f"{quote_id_prefix}:{index}",
                "metadata": dict(metadata or {"fixture": "synthetic"}),
            }
        )
    return tuple(payloads)


@dataclass(frozen=True)
class SyntheticForexProvider:
    bars: tuple[ForexQuoteBar, ...]
    catalog: InstrumentCatalog
    provider_id: str
    source: str = QUOTE_SOURCE
    supported_timeframes: frozenset[str] = frozenset({"1m"})
    quotes: tuple[ForexQuote, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", tuple(self.bars))
        object.__setattr__(self, "quotes", tuple(self.quotes))
        if not self.provider_id.strip():
            raise ValueError("provider_id is required")
        ordered = all(
            self.bars[index].timestamp < self.bars[index + 1].timestamp
            for index in range(len(self.bars) - 1)
        )
        if not ordered:
            raise ValueError("Bars must be strictly increasing")
        quotes_ordered = all(
            self.quotes[index].timestamp_utc() <= self.quotes[index + 1].timestamp_utc()
            for index in range(len(self.quotes) - 1)
        )
        if not quotes_ordered:
            raise ValueError("Quotes must be chronologically ordered")

    def _resolved_pair(self, provider_symbol: str) -> str | None:
        spec = self.catalog.resolve(self.provider_id, provider_symbol)
        return spec.canonical_pair if spec else None

    def latest_quote(self, provider_symbol: str) -> ForexQuote | None:
        pair = self._resolved_pair(provider_symbol)
        if pair is None or not self.bars:
            return None
        if self.quotes:
            normalized = self.quotes[-1]
            if (normalized.canonical_pair or "") == pair:
                return normalized
            return None
        bar = self.bars[-1]
        return ForexQuote(
            pair=bar.pair,
            timestamp=bar.last_quote_at,
            bid=bar.bid.close,
            ask=bar.ask.close,
            source=self.source,
            quote_id=f"{self.provider_id}:{bar.timestamp.isoformat()}",
            received_at=bar.last_quote_at + timedelta(milliseconds=100),
            metadata={"fixture": "synthetic"},
        )

    def historical_bars(
        self,
        provider_symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> tuple[ForexQuoteBar, ...]:
        pair = self._resolved_pair(provider_symbol)
        start_utc, start_error = as_utc(start, "start")
        end_utc, end_error = as_utc(end, "end")
        if pair is None or timeframe not in self.supported_timeframes or start_error or end_error:
            return ()
        return tuple(
            bar
            for bar in self.bars
            if (bar.canonical_pair or "") == pair
            and bar.timeframe == timeframe
            and start_utc <= bar.timestamp < end_utc
        )
