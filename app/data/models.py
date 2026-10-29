"""Timestamped executable bid/ask quotes and completed quote bars.

A derived mid price is available for research only.  Orders must use the ask
for BUY and the bid for SELL.  Missing values are preserved as ``None`` long
enough for validation to reject them; no side, timestamp, or bar is invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from app.common.types import OrderSide
from app.common.validation import (
    ValidationResult,
    as_utc,
    canonical_pair,
    decimal_places,
    is_multiple_of_step,
    parse_decimal,
)

NumericLike = Decimal | int | float | str | None


class FreshnessReason(str, Enum):
    OK = "OK"
    INVALID_POLICY = "INVALID_POLICY"
    INVALID_QUOTE_TIMESTAMP = "INVALID_QUOTE_TIMESTAMP"
    MISSING_RECEIVED_AT = "MISSING_RECEIVED_AT"
    INVALID_EVALUATION_TIME = "INVALID_EVALUATION_TIME"
    FUTURE_QUOTE = "FUTURE_QUOTE"
    STALE_QUOTE = "STALE_QUOTE"
    FEED_LATENCY_EXCEEDED = "FEED_LATENCY_EXCEEDED"
    QUOTE_CLOCK_ERROR = "QUOTE_CLOCK_ERROR"


@dataclass(frozen=True)
class FreshnessDecision:
    ok: bool
    reason: FreshnessReason
    age_seconds: Decimal | None = None
    feed_latency_seconds: Decimal | None = None
    received_age_seconds: Decimal | None = None

    def audit_view(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason.value,
            "age_seconds": self.age_seconds,
            "feed_latency_seconds": self.feed_latency_seconds,
            "received_age_seconds": self.received_age_seconds,
        }


@dataclass(frozen=True)
class QuoteFreshness:
    """Explicit quote/rate freshness policy; there is no implicit tolerance."""

    policy_id: str
    version: str
    max_age_seconds: Decimal
    max_future_skew_seconds: Decimal
    max_feed_latency_seconds: Decimal

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not self.policy_id.strip() or not self.version.strip():
            errors.append("missing_freshness_policy_identity")
        max_age, age_error = parse_decimal(self.max_age_seconds, "max_age_seconds")
        future_skew, skew_error = parse_decimal(
            self.max_future_skew_seconds, "max_future_skew_seconds"
        )
        feed_latency, latency_error = parse_decimal(
            self.max_feed_latency_seconds, "max_feed_latency_seconds"
        )
        if age_error or max_age is None or max_age <= 0:
            errors.append(age_error or "invalid_max_age_seconds")
        if skew_error or future_skew is None or future_skew < 0:
            errors.append(skew_error or "invalid_max_future_skew_seconds")
        if latency_error or feed_latency is None or feed_latency < 0:
            errors.append(latency_error or "invalid_max_feed_latency_seconds")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def _values(self) -> tuple[Decimal, Decimal, Decimal]:
        max_age, _ = parse_decimal(self.max_age_seconds, "max_age_seconds")
        future_skew, _ = parse_decimal(self.max_future_skew_seconds, "max_future_skew_seconds")
        feed_latency, _ = parse_decimal(
            self.max_feed_latency_seconds, "max_feed_latency_seconds"
        )
        assert max_age is not None and future_skew is not None and feed_latency is not None
        return max_age, future_skew, feed_latency

    @staticmethod
    def _seconds(delta: timedelta) -> Decimal:
        return Decimal(str(delta.total_seconds()))

    def check_instant(
        self, timestamp: Any, evaluated_at: Any
    ) -> FreshnessDecision:
        """Freshness for a timestamp with no separate receive time (e.g. FX rates)."""
        if not self.validate().valid:
            return FreshnessDecision(False, FreshnessReason.INVALID_POLICY)
        ts, ts_error = as_utc(timestamp, "timestamp")
        now, now_error = as_utc(evaluated_at, "evaluated_at")
        if ts_error or ts is None:
            return FreshnessDecision(False, FreshnessReason.INVALID_QUOTE_TIMESTAMP)
        if now_error or now is None:
            return FreshnessDecision(False, FreshnessReason.INVALID_EVALUATION_TIME)
        max_age, future_skew, _ = self._values()
        age = self._seconds(now - ts)
        if age < -future_skew:
            return FreshnessDecision(False, FreshnessReason.FUTURE_QUOTE, age_seconds=age)
        if age > max_age:
            return FreshnessDecision(False, FreshnessReason.STALE_QUOTE, age_seconds=age)
        return FreshnessDecision(True, FreshnessReason.OK, age_seconds=age)

    def check(self, quote: "ForexQuote", evaluated_at: Any) -> FreshnessDecision:
        if not self.validate().valid:
            return FreshnessDecision(False, FreshnessReason.INVALID_POLICY)
        ts, ts_error = as_utc(quote.timestamp, "quote_timestamp")
        received, received_error = as_utc(quote.received_at, "received_at")
        now, now_error = as_utc(evaluated_at, "evaluated_at")
        if ts_error or ts is None:
            return FreshnessDecision(False, FreshnessReason.INVALID_QUOTE_TIMESTAMP)
        if quote.received_at is None or received_error or received is None:
            return FreshnessDecision(False, FreshnessReason.MISSING_RECEIVED_AT)
        if now_error or now is None:
            return FreshnessDecision(False, FreshnessReason.INVALID_EVALUATION_TIME)
        max_age, future_skew, max_latency = self._values()
        age = self._seconds(now - ts)
        latency = self._seconds(received - ts)
        received_age = self._seconds(now - received)
        if latency < 0:
            return FreshnessDecision(
                False, FreshnessReason.QUOTE_CLOCK_ERROR, age, latency, received_age
            )
        if age < -future_skew or received_age < -future_skew:
            return FreshnessDecision(
                False, FreshnessReason.FUTURE_QUOTE, age, latency, received_age
            )
        if age > max_age or received_age > max_age:
            return FreshnessDecision(
                False, FreshnessReason.STALE_QUOTE, age, latency, received_age
            )
        if latency > max_latency:
            return FreshnessDecision(
                False, FreshnessReason.FEED_LATENCY_EXCEEDED, age, latency, received_age
            )
        return FreshnessDecision(
            True, FreshnessReason.OK, age, latency, received_age
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "max_age_seconds": self.max_age_seconds,
            "max_future_skew_seconds": self.max_future_skew_seconds,
            "max_feed_latency_seconds": self.max_feed_latency_seconds,
        }


def _validate_price_for_spec(value: Decimal, spec: Any, name: str) -> list[str]:
    errors: list[str] = []
    if value <= 0 or not value.is_finite():
        errors.append(f"invalid_{name}")
    precision = getattr(spec, "quote_precision", None)
    tick = getattr(spec, "price_tick_size_decimal", None)
    if isinstance(precision, int) and decimal_places(value) > precision:
        errors.append(f"{name}_precision_exceeded")
    if tick is not None:
        try:
            tick_value = tick() if callable(tick) else tick
            if not is_multiple_of_step(value, tick_value):
                errors.append(f"{name}_not_tick_aligned")
        except (TypeError, ValueError):
            errors.append(f"{name}_tick_validation_failed")
    return errors


@dataclass(frozen=True)
class ForexQuote:
    pair: str
    timestamp: datetime
    bid: NumericLike
    ask: NumericLike
    source: str
    quote_id: str = ""
    received_at: datetime | None = None
    bid_size: NumericLike = None
    ask_size: NumericLike = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def canonical_pair(self) -> str | None:
        pair, _ = canonical_pair(self.pair)
        return pair

    @property
    def is_complete(self) -> bool:
        return self.bid is not None and self.ask is not None

    def timestamp_utc(self) -> datetime:
        value, error = as_utc(self.timestamp, "quote_timestamp")
        if error or value is None:
            raise ValueError(error or "invalid quote timestamp")
        return value

    def received_at_utc(self) -> datetime:
        value, error = as_utc(self.received_at, "received_at")
        if error or value is None:
            raise ValueError(error or "invalid received_at")
        return value

    def prices(self) -> tuple[Decimal, Decimal]:
        if not self.is_complete:
            raise ValueError("Incomplete quote")
        bid, bid_error = parse_decimal(self.bid, "bid")
        ask, ask_error = parse_decimal(self.ask, "ask")
        if bid_error or ask_error or bid is None or ask is None:
            raise ValueError(bid_error or ask_error or "invalid quote prices")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError("Crossed or non-positive quote")
        return bid, ask

    @property
    def spread(self) -> Decimal:
        bid, ask = self.prices()
        return ask - bid

    @property
    def mid(self) -> Decimal:
        """Research-only midpoint; never an executable price."""
        bid, ask = self.prices()
        return (bid + ask) / Decimal(2)

    def executable_price(self, side: OrderSide | str) -> Decimal:
        bid, ask = self.prices()
        side_value = side.value if isinstance(side, OrderSide) else str(side).upper()
        if side_value == OrderSide.BUY.value:
            return ask
        if side_value == OrderSide.SELL.value:
            return bid
        raise ValueError("Unsupported order side")

    def spread_pips(self, spec: Any) -> Decimal:
        return self.spread / spec.pip_size_decimal()

    def validate(self, spec: Any | None = None) -> ValidationResult:
        errors: list[str] = []
        pair, pair_error = canonical_pair(self.pair)
        if pair_error or pair is None:
            errors.append(pair_error or "invalid_pair")
        if spec is not None and pair != getattr(spec, "canonical_pair", None):
            errors.append("pair_mismatch")
        _, timestamp_error = as_utc(self.timestamp, "quote_timestamp")
        if timestamp_error:
            errors.append(timestamp_error)
        if self.received_at is not None:
            _, received_error = as_utc(self.received_at, "received_at")
            if received_error:
                errors.append(received_error)
        if not isinstance(self.source, str) or not self.source.strip():
            errors.append("missing_quote_source")
        if not self.is_complete:
            if self.bid is None:
                errors.append("missing_bid")
            if self.ask is None:
                errors.append("missing_ask")
        bid, bid_error = parse_decimal(self.bid, "bid")
        ask, ask_error = parse_decimal(self.ask, "ask")
        if bid_error:
            errors.append(bid_error)
        if ask_error:
            errors.append(ask_error)
        if bid is not None and ask is not None:
            if bid <= 0:
                errors.append("non_positive_bid")
            if ask <= 0:
                errors.append("non_positive_ask")
            if ask < bid:
                errors.append("crossed_quote")
            if spec is not None:
                errors.extend(_validate_price_for_spec(bid, spec, "bid"))
                errors.extend(_validate_price_for_spec(ask, spec, "ask"))
        for name in ("bid_size", "ask_size"):
            value = getattr(self, name)
            if value is not None:
                parsed, error = parse_decimal(value, name)
                if error or parsed is None or parsed < 0:
                    errors.append(error or f"invalid_{name}")
        for key, value in self.metadata.items():
            if not isinstance(key, str) or not isinstance(value, str):
                errors.append("invalid_metadata")
                break
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.canonical_pair,
            "timestamp": self.timestamp,
            "bid": self.bid,
            "ask": self.ask,
            "source": self.source,
            "quote_id": self.quote_id,
            "received_at": self.received_at,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
        }


@dataclass(frozen=True)
class QuoteSideBar:
    open: NumericLike
    high: NumericLike
    low: NumericLike
    close: NumericLike

    def parsed(self) -> tuple[dict[str, Decimal] | None, ValidationResult]:
        values: dict[str, Decimal] = {}
        errors: list[str] = []
        for name in ("open", "high", "low", "close"):
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None:
                errors.append(error or f"missing_{name}")
            else:
                values[name] = value
        if errors or len(values) != 4:
            return None, ValidationResult.fail(*errors)
        if any(value <= 0 for value in values.values()):
            errors.append("non_positive_price")
        if values["high"] < max(values["open"], values["close"]):
            errors.append("high_below_open_or_close")
        if values["low"] > min(values["open"], values["close"]):
            errors.append("low_above_open_or_close")
        if values["high"] < values["low"]:
            errors.append("high_below_low")
        if errors:
            return None, ValidationResult.fail(*errors)
        return values, ValidationResult.ok()


@dataclass(frozen=True)
class ForexQuoteBar:
    """A complete, provider-built bar with separate bid and ask OHLC."""

    pair: str
    timeframe: str
    timestamp: datetime
    duration_seconds: int
    bid: QuoteSideBar
    ask: QuoteSideBar
    source: str
    quote_count: int
    last_quote_at: datetime
    complete: bool
    volume: NumericLike = None
    volume_kind: str | None = None

    @property
    def canonical_pair(self) -> str | None:
        pair, _ = canonical_pair(self.pair)
        return pair

    @property
    def end_at(self) -> datetime:
        ts, error = as_utc(self.timestamp, "bar_timestamp")
        if error or ts is None:
            raise ValueError(error or "invalid bar timestamp")
        return ts + timedelta(seconds=self.duration_seconds)

    def executable_price(self, side: OrderSide | str, *, point: str = "close") -> Decimal:
        if point not in {"open", "high", "low", "close"}:
            raise ValueError("Unsupported bar point")
        side_value = side.value if isinstance(side, OrderSide) else str(side).upper()
        if side_value == OrderSide.BUY.value:
            values, result = self.ask.parsed()
        elif side_value == OrderSide.SELL.value:
            values, result = self.bid.parsed()
        else:
            raise ValueError("Unsupported order side")
        if not result.valid or values is None:
            raise ValueError(result.reason)
        return values[point]

    @property
    def mid_close(self) -> Decimal:
        bid, bid_result = self.bid.parsed()
        ask, ask_result = self.ask.parsed()
        if not bid_result.valid or not ask_result.valid or bid is None or ask is None:
            raise ValueError("Incomplete bid/ask bar")
        return (bid["close"] + ask["close"]) / Decimal(2)

    def validate(self, spec: Any | None = None) -> ValidationResult:
        errors: list[str] = []
        pair, pair_error = canonical_pair(self.pair)
        if pair_error or pair is None:
            errors.append(pair_error or "invalid_pair")
        if spec is not None and pair != getattr(spec, "canonical_pair", None):
            errors.append("pair_mismatch")
        ts, ts_error = as_utc(self.timestamp, "bar_timestamp")
        if ts_error or ts is None:
            errors.append(ts_error or "invalid_bar_timestamp")
        last_quote, last_error = as_utc(self.last_quote_at, "last_quote_at")
        if last_error or last_quote is None:
            errors.append(last_error or "invalid_last_quote_at")
        if not isinstance(self.timeframe, str) or not self.timeframe.strip():
            errors.append("missing_timeframe")
        if not isinstance(self.duration_seconds, int) or isinstance(self.duration_seconds, bool):
            errors.append("invalid_duration_type")
        elif self.duration_seconds <= 0:
            errors.append("invalid_duration")
        if not isinstance(self.complete, bool) or not self.complete:
            errors.append("incomplete_bar")
        if not isinstance(self.quote_count, int) or isinstance(self.quote_count, bool) or self.quote_count <= 0:
            errors.append("invalid_quote_count")
        if not isinstance(self.source, str) or not self.source.strip():
            errors.append("missing_bar_source")
        if ts is not None and last_quote is not None and isinstance(self.duration_seconds, int):
            end = ts + timedelta(seconds=self.duration_seconds)
            if last_quote < ts or last_quote >= end:
                errors.append("last_quote_outside_bar")
        bid_values, bid_result = self.bid.parsed()
        ask_values, ask_result = self.ask.parsed()
        errors.extend(bid_result.errors)
        errors.extend(ask_result.errors)
        if bid_values is not None and ask_values is not None:
            for point in ("open", "high", "low", "close"):
                if bid_values[point] > ask_values[point]:
                    errors.append(f"crossed_{point}")
        if spec is not None:
            for side_name, side in (("bid", self.bid), ("ask", self.ask)):
                for point in ("open", "high", "low", "close"):
                    value, error = parse_decimal(getattr(side, point), point)
                    if value is not None and not error:
                        errors.extend(
                            _validate_price_for_spec(value, spec, f"{side_name}_{point}")
                        )
        if self.volume is not None:
            volume, volume_error = parse_decimal(self.volume, "volume")
            if volume_error or volume is None or volume < 0:
                errors.append(volume_error or "invalid_volume")
            if not isinstance(self.volume_kind, str) or not self.volume_kind.strip():
                errors.append("missing_volume_kind")
        elif self.volume_kind is not None:
            errors.append("volume_kind_without_volume")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.canonical_pair,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "duration_seconds": self.duration_seconds,
            "source": self.source,
            "quote_count": self.quote_count,
            "last_quote_at": self.last_quote_at,
            "complete": self.complete,
            "volume": self.volume,
            "volume_kind": self.volume_kind,
        }
