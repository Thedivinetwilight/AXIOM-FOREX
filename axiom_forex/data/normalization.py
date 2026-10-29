"""Strict provider payload normalization; no interpolation or vendor assumptions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from app.common.validation import ValidationResult, as_utc, parse_decimal
from app.data.models import ForexQuote, ForexQuoteBar, QuoteSideBar
from app.universe.specs import CurrencyPairSpec, InstrumentCatalog


def parse_timestamp(value: Any, name: str) -> tuple[datetime | None, str | None]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None, f"invalid_{name}"
    else:
        return None, f"invalid_{name}_type"
    return as_utc(parsed, name)


@dataclass(frozen=True)
class NormalizedResult:
    value: ForexQuote | ForexQuoteBar | None
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.errors


def _alias_spec(
    catalog: InstrumentCatalog,
    provider_id: str,
    symbol: Any,
) -> tuple[CurrencyPairSpec | None, str | None]:
    if not isinstance(symbol, str) or not symbol.strip():
        return None, "missing_provider_symbol"
    spec = catalog.resolve(provider_id, symbol)
    if spec is None:
        return None, "unknown_provider_symbol"
    return spec, None


def _parse_side_bar(raw: Any, name: str, errors: list[str]) -> QuoteSideBar | None:
    if not isinstance(raw, Mapping):
        errors.append(f"missing_{name}_bar")
        return None
    values: dict[str, Decimal] = {}
    for point in ("open", "high", "low", "close"):
        value = raw.get(point)
        if value is None:
            errors.append(f"missing_{name}_{point}")
            continue
        try:
            values[point] = Decimal(str(value))
        except Exception:
            errors.append(f"invalid_{name}_{point}")
    if len(values) != 4:
        return None
    return QuoteSideBar(
        open=values["open"],
        high=values["high"],
        low=values["low"],
        close=values["close"],
    )


@dataclass(frozen=True)
class DataNormalizer:
    catalog: InstrumentCatalog
    provider_id: str

    def normalize_quote(self, payload: Mapping[str, Any]) -> NormalizedResult:
        errors: list[str] = []
        spec, alias_error = _alias_spec(self.catalog, self.provider_id, payload.get("symbol"))
        if alias_error or spec is None:
            return NormalizedResult(None, (alias_error or "unknown_provider_symbol",))
        timestamp, timestamp_error = parse_timestamp(payload.get("timestamp"), "quote_timestamp")
        received_at, received_error = parse_timestamp(payload.get("received_at"), "received_at")
        if timestamp_error or timestamp is None:
            errors.append(timestamp_error or "invalid_quote_timestamp")
        if received_error or received_at is None:
            errors.append(received_error or "invalid_received_at")
        bid_raw = payload.get("bid")
        ask_raw = payload.get("ask")
        bid: Decimal | None = None
        ask: Decimal | None = None
        if bid_raw is None:
            errors.append("missing_bid")
        else:
            bid, bid_error = parse_decimal(bid_raw, "bid")
            if bid_error:
                errors.append(bid_error)
        if ask_raw is None:
            errors.append("missing_ask")
        else:
            ask, ask_error = parse_decimal(ask_raw, "ask")
            if ask_error:
                errors.append(ask_error)
        source = payload.get("source")
        if not isinstance(source, str) or not source.strip():
            errors.append("missing_quote_source")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            errors.append("invalid_metadata")
            metadata = {}
        if errors or timestamp is None or received_at is None or spec.canonical_pair is None:
            return NormalizedResult(None, tuple(errors))
        quote = ForexQuote(
            pair=spec.canonical_pair,
            timestamp=timestamp,
            bid=bid,
            ask=ask,
            source=str(source),
            quote_id=str(payload.get("quote_id", "")),
            received_at=received_at,
            metadata={str(key): str(value) for key, value in metadata.items()},
        )
        validation = quote.validate(spec)
        if not validation.valid:
            return NormalizedResult(None, tuple(validation.errors))
        return NormalizedResult(quote, ())

    def normalize_bar(self, payload: Mapping[str, Any]) -> NormalizedResult:
        errors: list[str] = []
        spec, alias_error = _alias_spec(self.catalog, self.provider_id, payload.get("symbol"))
        if alias_error or spec is None:
            return NormalizedResult(None, (alias_error or "unknown_provider_symbol",))
        timestamp, timestamp_error = parse_timestamp(payload.get("timestamp"), "bar_timestamp")
        last_quote_at, last_error = parse_timestamp(payload.get("last_quote_at"), "last_quote_at")
        if timestamp_error or timestamp is None:
            errors.append(timestamp_error or "invalid_bar_timestamp")
        if last_error or last_quote_at is None:
            errors.append(last_error or "invalid_last_quote_at")
        bid = _parse_side_bar(payload.get("bid"), "bid", errors)
        ask = _parse_side_bar(payload.get("ask"), "ask", errors)
        timeframe = payload.get("timeframe")
        duration = payload.get("duration_seconds")
        quote_count = payload.get("quote_count")
        source = payload.get("source")
        complete = payload.get("complete")
        if not isinstance(timeframe, str) or not timeframe.strip():
            errors.append("missing_timeframe")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
            errors.append("invalid_duration_seconds")
        if not isinstance(quote_count, int) or isinstance(quote_count, bool) or quote_count <= 0:
            errors.append("invalid_quote_count")
        if not isinstance(source, str) or not source.strip():
            errors.append("missing_bar_source")
        if not isinstance(complete, bool) or not complete:
            errors.append("incomplete_bar")
        if errors or timestamp is None or last_quote_at is None or bid is None or ask is None:
            return NormalizedResult(None, tuple(errors))
        bar = ForexQuoteBar(
            pair=spec.canonical_pair or "",
            timeframe=str(timeframe),
            timestamp=timestamp,
            duration_seconds=int(duration),
            bid=bid,
            ask=ask,
            source=str(source),
            quote_count=int(quote_count),
            last_quote_at=last_quote_at,
            complete=True,
        )
        validation = bar.validate(spec)
        if not validation.valid:
            return NormalizedResult(None, tuple(validation.errors))
        return NormalizedResult(bar, ())

    def normalize_quotes(self, payloads: Iterable[Mapping[str, Any]]) -> tuple[NormalizedResult, ...]:
        return tuple(self.normalize_quote(payload) for payload in payloads)

    def normalize_bars(self, payloads: Iterable[Mapping[str, Any]]) -> tuple[NormalizedResult, ...]:
        return tuple(self.normalize_bar(payload) for payload in payloads)
