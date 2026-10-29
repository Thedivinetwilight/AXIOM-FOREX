"""Timestamped account-currency conversion rates.

A rate of ``r`` for ``(FROM, TO)`` means ``1 FROM = r TO``.  Identity rates
are generated only when the two currency codes are equal and are explicitly
labeled as identity in the result; they never substitute for a missing market
rate between different currencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from app.common.validation import (
    ValidationResult,
    as_utc,
    parse_decimal,
    validate_currency_code,
)
from app.data.models import QuoteFreshness


class RateReason(str, Enum):
    OK = "OK"
    IDENTITY = "IDENTITY"
    MISSING_RATE = "MISSING_RATE"
    STALE_RATE = "STALE_RATE"
    FUTURE_RATE = "FUTURE_RATE"
    INVALID_RATE = "INVALID_RATE"
    INVALID_POLICY = "INVALID_POLICY"
    INVALID_BOOK = "INVALID_BOOK"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"


@dataclass(frozen=True)
class ConversionRate:
    from_currency: str
    to_currency: str
    rate: Decimal
    timestamp: datetime
    source: str
    rate_id: str = ""

    @property
    def from_currency_code(self) -> str:
        code, _ = validate_currency_code(self.from_currency, "from_currency")
        return code or ""

    @property
    def to_currency_code(self) -> str:
        code, _ = validate_currency_code(self.to_currency, "to_currency")
        return code or ""

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        _, from_error = validate_currency_code(self.from_currency, "from_currency")
        _, to_error = validate_currency_code(self.to_currency, "to_currency")
        if from_error:
            errors.append(from_error)
        if to_error:
            errors.append(to_error)
        if self.from_currency_code == self.to_currency_code:
            errors.append("identity_rate_must_not_be_stored")
        rate, rate_error = parse_decimal(self.rate, "conversion_rate")
        if rate_error or rate is None or rate <= 0:
            errors.append(rate_error or "invalid_conversion_rate")
        _, timestamp_error = as_utc(self.timestamp, "rate_timestamp")
        if timestamp_error:
            errors.append(timestamp_error)
        if not isinstance(self.source, str) or not self.source.strip():
            errors.append("missing_rate_source")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def rate_decimal(self) -> Decimal:
        value, error = parse_decimal(self.rate, "conversion_rate")
        if error or value is None:
            raise ValueError(error or "invalid conversion rate")
        return value

    def timestamp_utc(self) -> datetime:
        value, error = as_utc(self.timestamp, "rate_timestamp")
        if error or value is None:
            raise ValueError(error or "invalid rate timestamp")
        return value

    def audit_view(self) -> dict[str, Any]:
        return {
            "from_currency": self.from_currency_code,
            "to_currency": self.to_currency_code,
            "rate": self.rate,
            "timestamp": self.timestamp,
            "source": self.source,
            "rate_id": self.rate_id,
        }


@dataclass(frozen=True)
class RateResult:
    ok: bool
    reason: RateReason
    rate: Decimal | None = None
    source: str | None = None
    timestamp: datetime | None = None
    freshness: Any = None

    def audit_view(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason.value,
            "rate": self.rate,
            "source": self.source,
            "timestamp": self.timestamp,
            "freshness": self.freshness.audit_view()
            if hasattr(self.freshness, "audit_view")
            else None,
        }


@dataclass(frozen=True)
class ConversionRateBook:
    source: str
    freshness: QuoteFreshness
    rates: Mapping[tuple[str, str], ConversionRate]

    def __post_init__(self) -> None:
        normalized: dict[tuple[str, str], ConversionRate] = {}
        for key, rate in self.rates.items():
            try:
                from_currency, to_currency = key
            except (TypeError, ValueError):
                normalized[key] = rate  # validated and rejected below
                continue
            from_code, from_error = validate_currency_code(from_currency, "from_currency")
            to_code, to_error = validate_currency_code(to_currency, "to_currency")
            normalized[
                (
                    from_code if not from_error and from_code else str(from_currency),
                    to_code if not to_error and to_code else str(to_currency),
                )
            ] = rate
        object.__setattr__(self, "rates", MappingProxyType(normalized))

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not isinstance(self.source, str) or not self.source.strip():
            errors.append("missing_rate_book_source")
        freshness_result = self.freshness.validate()
        errors.extend(freshness_result.errors)
        for key, rate in self.rates.items():
            if not isinstance(rate, ConversionRate):
                errors.append(f"invalid_rate_object:{key}")
                continue
            errors.extend(rate.validate().errors)
            expected_key = (rate.from_currency_code, rate.to_currency_code)
            if key != expected_key:
                errors.append(f"rate_key_mismatch:{key}")
            if rate.source != self.source:
                errors.append(f"rate_source_mismatch:{key}")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def get(
        self,
        from_currency: str,
        to_currency: str,
        evaluated_at: datetime,
    ) -> RateResult:
        validation = self.validate()
        if not validation.valid:
            return RateResult(False, RateReason.INVALID_BOOK)
        from_code, from_error = validate_currency_code(from_currency, "from_currency")
        to_code, to_error = validate_currency_code(to_currency, "to_currency")
        if from_error or to_error or from_code is None or to_code is None:
            return RateResult(False, RateReason.INVALID_RATE)
        if from_code == to_code:
            return RateResult(
                True,
                RateReason.IDENTITY,
                rate=Decimal(1),
                source="IDENTITY",
                timestamp=as_utc(evaluated_at, "evaluated_at")[0],
            )
        rate = self.rates.get((from_code, to_code))
        if rate is None:
            return RateResult(False, RateReason.MISSING_RATE)
        freshness = self.freshness.check_instant(rate.timestamp, evaluated_at)
        if not freshness.ok:
            if freshness.reason.value == "STALE_QUOTE":
                return RateResult(False, RateReason.STALE_RATE, freshness=freshness)
            if freshness.reason.value == "FUTURE_QUOTE":
                return RateResult(False, RateReason.FUTURE_RATE, freshness=freshness)
            return RateResult(False, RateReason.INVALID_POLICY, freshness=freshness)
        return RateResult(
            True,
            RateReason.OK,
            rate=rate.rate_decimal(),
            source=rate.source,
            timestamp=rate.timestamp_utc(),
            freshness=freshness,
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "freshness": self.freshness.audit_view(),
            "rate_keys": sorted(f"{a}/{b}" for a, b in self.rates),
        }
