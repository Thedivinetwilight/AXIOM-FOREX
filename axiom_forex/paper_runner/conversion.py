"""Conversion rates taken from the broker's own quotes.

A EUR/USD position in a USD account needs EUR->USD to convert exposure, margin,
costs and P&L.  That rate is not something to default to 1.0, and it is not
something to invent: the broker quotes it as the EUR/USD symbol itself.  This
module reads that quote from the connected terminal, so the rate carries the
broker's timestamp and an explicit provenance label.

The rules, all of them fail-closed:

* A direct leg is the mid of the broker's bid/ask for the pair, timestamped by the
  broker's tick.
* A reciprocal leg is the inverse of that mid, labelled ``INVERSE`` so the
  derivation is visible rather than implied.
* A cross with no shared currency (EUR->GBP from EUR/USD and GBP/USD) is
  *supported* only through the quote currency: EUR/USD gives EUR->USD and GBP/USD
  gives GBP->USD, so EUR->GBP = (EUR/USD) / (GBP/USD).  That is an arithmetic
  identity on two broker quotes, not an invented rate, and it is labelled
  ``CROSS`` with both source symbols recorded.
* Anything else resolves to no rate, and the caller refuses.  Nothing is
  interpolated, nothing is carried over from an earlier tick, and a stale tick is
  reported as stale by the freshness policy rather than silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from app.accounting.fx import ConversionRate, ConversionRateBook
from app.data.models import QuoteFreshness

SOURCE_TERMINAL = "MT5_TERMINAL_TICK"
BASIS_DIRECT = "DIRECT"
BASIS_INVERSE = "INVERSE"
BASIS_CROSS = "CROSS"

DIRECT = "DIRECT"
INVERSE = "INVERSE"
CROSS = "CROSS"
MISSING = "MISSING"


@dataclass(frozen=True)
class ConversionLeg:
    """One derived rate and exactly where its numbers came from."""

    from_currency: str
    to_currency: str
    basis: str
    symbols: tuple[str, ...]
    timestamp: datetime | None = None
    rate: Decimal | None = None

    @property
    def ok(self) -> bool:
        return self.rate is not None and self.timestamp is not None

    def audit_view(self) -> dict[str, Any]:
        return {
            "from": self.from_currency,
            "to": self.to_currency,
            "basis": self.basis,
            "symbols": list(self.symbols),
            "rate": str(self.rate) if self.rate is not None else "",
            "timestamp": self.timestamp.isoformat() if self.timestamp else "",
        }


def _stamp(quote: Any) -> datetime | None:
    """The broker's own timestamp for a quote, whatever the object calls it."""

    value = getattr(quote, "timestamp", None) or getattr(quote, "time", None)
    return value if isinstance(value, datetime) else None


def _pair_symbol(first: str, second: str, symbols: Iterable[str]) -> str | None:
    wanted = f"{first}{second}".upper()
    for symbol in symbols:
        if symbol.upper() == wanted:
            return symbol
    # Broker suffixes are common (EURUSD.pro); the catalogue already recorded the
    # exact symbol per pair, so a suffix-only match is still exact.
    for symbol in symbols:
        base = symbol.upper().split(".")[0].split("_")[0]
        if base == wanted:
            return symbol
    return None


def _mid(bid: Decimal, ask: Decimal) -> Decimal:
    return (bid + ask) / Decimal(2)


def derive_rate(
    *,
    from_currency: str,
    to_currency: str,
    quotes: Mapping[str, Any],
    symbols: Iterable[str],
    symbol_of_pair: Mapping[tuple[str, str], str] | None = None,
) -> ConversionLeg:
    """Derive one rate from broker quotes, or explain why it cannot be derived.

    ``quotes`` maps a terminal symbol to an object with ``bid``/``ask``/``timestamp``.
    """

    source = from_currency.upper()
    target = to_currency.upper()
    if source == target:
        return ConversionLeg(source, target, "IDENTITY", ())

    lookup = dict(symbol_of_pair or {})

    def symbol_for(first: str, second: str) -> str | None:
        recorded = lookup.get((first, second))
        if recorded:
            return recorded if recorded in quotes else None
        found = _pair_symbol(first, second, symbols)
        return found if found is not None and found in quotes else None

    direct = symbol_for(source, target)
    if direct is not None:
        quote = quotes[direct]
        return ConversionLeg(
            source,
            target,
            BASIS_DIRECT,
            (direct,),
            timestamp=_stamp(quote),
            rate=_mid(quote.bid, quote.ask),
        )

    indirect = symbol_for(target, source)
    if indirect is not None:
        quote = quotes[indirect]
        mid = _mid(quote.bid, quote.ask)
        if mid <= 0:
            return ConversionLeg(source, target, MISSING, (indirect,))
        return ConversionLeg(
            source,
            target,
            BASIS_INVERSE,
            (indirect,),
            timestamp=_stamp(quote),
            rate=Decimal(1) / mid,
        )

    # Cross through a shared currency, using two broker quotes.
    if symbol_for(source, target) is None:
        for shared in ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"):
            if shared in {source, target}:
                continue
            first = symbol_for(source, shared)
            second = symbol_for(target, shared)
            if first is None or second is None:
                continue
            left, right = quotes[first], quotes[second]
            right_mid = _mid(right.bid, right.ask)
            if right_mid <= 0:
                continue
            stamps = [_stamp(left), _stamp(right)]
            if any(stamp is None for stamp in stamps):
                continue
            # The later of the two timestamps: the derived rate is no fresher
            # than its older input, so freshness is judged conservatively.
            return ConversionLeg(
                source,
                target,
                BASIS_CROSS,
                (first, second),
                timestamp=min(stamp for stamp in stamps if stamp is not None),
                rate=_mid(left.bid, left.ask) / right_mid,
            )
    return ConversionLeg(source, target, MISSING, ())


def build_rate_book(
    *,
    required: Iterable[tuple[str, str]],
    quotes: Mapping[str, Any],
    symbols: Iterable[str],
    freshness: QuoteFreshness,
    symbol_of_pair: Mapping[tuple[str, str], str] | None = None,
    source: str = SOURCE_TERMINAL,
) -> tuple[ConversionRateBook, tuple[ConversionLeg, ...]]:
    """Build a book from broker quotes plus the legs that could not be derived."""

    symbol_list = tuple(symbols)
    rates: dict[tuple[str, str], ConversionRate] = {}
    legs: list[ConversionLeg] = []
    missing: list[ConversionLeg] = []
    seen: set[tuple[str, str]] = set()
    for pair in required:
        key = (pair[0].upper(), pair[1].upper())
        if key in seen or key[0] == key[1]:
            continue
        seen.add(key)
        leg = derive_rate(
            from_currency=key[0],
            to_currency=key[1],
            quotes=quotes,
            symbols=symbol_list,
            symbol_of_pair=symbol_of_pair,
        )
        legs.append(leg)
        if not leg.ok:
            missing.append(leg)
            continue
        rates[key] = ConversionRate(
            from_currency=key[0],
            to_currency=key[1],
            rate=leg.rate,
            timestamp=leg.timestamp,
            source=source,
            rate_id=f"mt5:{leg.basis.lower()}:{key[0]}{key[1]}",
        )
    book = ConversionRateBook(source=source, freshness=freshness, rates=rates)
    return book, tuple(missing)


# ---------------------------------------------------------------------------
# Live rates: derived at the instant they are needed
# ---------------------------------------------------------------------------


class LiveConversionRates:
    """A rate source that asks the terminal for a *current* quote on each use.

    Consumers (the cost model, the ledger, the rollover engine) only need
    ``source`` and ``get(from, to, at)``, so this object substitutes for a
    pre-built :class:`ConversionRateBook` without changing any of them.  Unlike a
    static book it cannot go quietly stale: every lookup re-reads the broker's
    quote, applies the same freshness policy the paper lane uses, and returns
    ``STALE_RATE`` / ``MISSING_RATE`` instead of a possibly-wrong number.
    """

    def __init__(
        self,
        *,
        source: str,
        freshness: QuoteFreshness,
        market: Any,
        symbols: Iterable[str] = (),
        symbol_of_pair: Mapping[tuple[str, str], str] | None = None,
        cache_seconds: Decimal = Decimal("0"),
    ) -> None:
        self.source = source
        self.freshness = freshness
        self.market = market
        self.symbols = tuple(symbols)
        self.symbol_of_pair = dict(symbol_of_pair or {})
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[datetime, Any]] = {}
        self.lookups: list[dict[str, Any]] = []

    # -- helpers ---------------------------------------------------------

    def _tick(self, symbol: str, at: datetime) -> Any:
        cached = self._cache.get(symbol)
        if cached is not None and self.cache_seconds > 0:
            stamp, quote = cached
            if (at - stamp).total_seconds() <= float(self.cache_seconds):
                return quote
        quote = self.market.tick(symbol)
        self._cache[symbol] = (at, quote)
        return quote

    def _legs(self, source: str, target: str) -> tuple[str, tuple[str, ...], str]:
        """Return ``(basis, symbols, orientation)`` for a direct/inverse/cross leg."""

        def symbol_for(first: str, second: str) -> str | None:
            recorded = self.symbol_of_pair.get((first, second))
            if recorded:
                return recorded
            return _pair_symbol(first, second, self.symbols)

        direct = symbol_for(source, target)
        if direct:
            return BASIS_DIRECT, (direct,), ""
        inverse = symbol_for(target, source)
        if inverse:
            return BASIS_INVERSE, (inverse,), ""
        for shared in ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"):
            if shared in {source, target}:
                continue
            left, right = symbol_for(source, shared), symbol_for(target, shared)
            if left and right:
                return BASIS_CROSS, (left, right), ""
        return MISSING, (), ""

    # -- the ConversionRateBook surface ----------------------------------

    def validate(self) -> Any:
        from app.common.validation import ValidationResult

        return ValidationResult.ok()

    def get(self, from_currency: str, to_currency: str, at: datetime) -> Any:
        from app.accounting.fx import RateReason, RateResult

        source = str(from_currency).upper()
        target = str(to_currency).upper()
        if source == target:
            return RateResult(True, RateReason.IDENTITY, rate=Decimal(1), source="IDENTITY", timestamp=at)
        basis, symbols, _ = self._legs(source, target)
        if basis == MISSING:
            self.lookups.append(
                {"from": source, "to": target, "basis": MISSING, "symbols": [], "ok": False}
            )
            return RateResult(False, RateReason.MISSING_RATE)
        try:
            quotes = [self._tick(symbol, at) for symbol in symbols]
        except Exception as exc:  # a terminal refusal is "no rate", not a guess
            self.lookups.append(
                {
                    "from": source,
                    "to": target,
                    "basis": basis,
                    "symbols": list(symbols),
                    "ok": False,
                    "error": type(exc).__name__,
                }
            )
            return RateResult(False, RateReason.MISSING_RATE)
        stamps = [_stamp(quote) for quote in quotes]
        if any(stamp is None for stamp in stamps):
            self.lookups.append(
                {"from": source, "to": target, "basis": basis, "symbols": list(symbols), "ok": False}
            )
            return RateResult(False, RateReason.INVALID_RATE)
        stamp = min(stamps) if len(stamps) > 1 else stamps[0]
        freshness = self.freshness.check_instant(stamp, at)
        if not freshness.ok:
            reason = (
                RateReason.STALE_RATE
                if getattr(freshness.reason, "value", "") == "STALE_QUOTE"
                else RateReason.FUTURE_RATE
            )
            self.lookups.append(
                {
                    "from": source,
                    "to": target,
                    "basis": basis,
                    "symbols": list(symbols),
                    "ok": False,
                    "reason": getattr(freshness.reason, "value", ""),
                }
            )
            return RateResult(False, reason, freshness=freshness)
        if basis == BASIS_DIRECT:
            rate = _mid(quotes[0].bid, quotes[0].ask)
        elif basis == BASIS_INVERSE:
            mid = _mid(quotes[0].bid, quotes[0].ask)
            rate = Decimal(1) / mid if mid > 0 else Decimal(0)
        else:
            right_mid = _mid(quotes[1].bid, quotes[1].ask)
            rate = _mid(quotes[0].bid, quotes[0].ask) / right_mid if right_mid > 0 else Decimal(0)
        if rate <= 0:
            return RateResult(False, RateReason.INVALID_RATE)
        self.lookups.append(
            {
                "from": source,
                "to": target,
                "basis": basis,
                "symbols": list(symbols),
                "ok": True,
                "rate": str(rate),
                "timestamp": stamp.isoformat(),
            }
        )
        return RateResult(
            True,
            RateReason.OK,
            rate=rate,
            source=self.source,
            timestamp=stamp,
            freshness=freshness,
        )

    def recent_lookups(self, limit: int = 12) -> list[dict[str, Any]]:
        return list(self.lookups[-limit:])
