"""Strict, exception-light validation helpers for untrusted market inputs.

All monetary values are represented as :class:`decimal.Decimal`.  Provider
payloads may still arrive as strings, integers, or finite floats; those are
converted with ``Decimal(str(value))`` so binary float noise is not silently
accepted as a price.  Missing and non-finite values are reported, never
replaced with defaults.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
_PAIR_SEPARATORS = ("/", "-", "_", ".")


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()

    @classmethod
    def ok(cls) -> "ValidationResult":
        return cls(True, ())

    @classmethod
    def fail(cls, *errors: str) -> "ValidationResult":
        return cls(False, tuple(errors))

    @property
    def is_valid(self) -> bool:
        return self.valid

    @property
    def reason(self) -> str:
        return "; ".join(self.errors) if self.errors else "ok"


def parse_decimal(value: Any, name: str) -> tuple[Decimal | None, str | None]:
    """Return ``(decimal, error)``; never raises for ordinary bad input."""
    if value is None:
        return None, f"missing_{name}"
    if isinstance(value, bool):
        return None, f"invalid_{name}_bool"
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None, f"non_finite_{name}"
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None, f"invalid_{name}"
    else:
        return None, f"invalid_{name}_type"
    if not result.is_finite():
        return None, f"non_finite_{name}"
    return result, None


def as_utc(value: Any, name: str) -> tuple[datetime | None, str | None]:
    """Return a timezone-aware UTC datetime or a named validation error."""
    if not isinstance(value, datetime):
        return None, f"invalid_{name}_type"
    if value.tzinfo is None or value.utcoffset() is None:
        return None, f"naive_{name}"
    try:
        return value.astimezone(timezone.utc), None
    except (OverflowError, ValueError):
        return None, f"invalid_{name}"


def validate_currency_code(value: Any, name: str) -> tuple[str | None, str | None]:
    """Validate a three-letter synthetic or real currency code."""
    if not isinstance(value, str):
        return None, f"invalid_{name}_type"
    code = value.strip().upper()
    if not _CURRENCY.fullmatch(code):
        return None, f"invalid_{name}"
    return code, None


def canonical_pair(value: Any) -> tuple[str | None, str | None]:
    """Canonicalize an explicitly separated pair to ``BASE/QUOTE``.

    Six-letter strings are intentionally rejected: splitting them would
    require an ambiguous instrument-master assumption.  Codes need not be
    real ISO currencies so local fixtures can remain provider-neutral.
    """
    if not isinstance(value, str):
        return None, "invalid_pair_type"
    text = value.strip().upper()
    parts: list[str] | None = None
    for separator in _PAIR_SEPARATORS:
        if separator in text:
            candidate = text.split(separator)
            if len(candidate) == 2:
                parts = candidate
                break
            return None, "invalid_pair_separator"
    if parts is None:
        return None, "ambiguous_pair"
    base, base_error = validate_currency_code(parts[0], "base_currency")
    quote, quote_error = validate_currency_code(parts[1], "quote_currency")
    if base_error or quote_error or base is None or quote is None:
        return None, base_error or quote_error or "invalid_pair"
    if base == quote:
        return None, "identical_currencies"
    return f"{base}/{quote}", None


def decimal_places(value: Decimal) -> int:
    """Significant trailing decimal places, ignoring trailing zeros."""
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def is_multiple_of_step(value: Decimal, step: Decimal) -> bool:
    if not value.is_finite() or not step.is_finite() or step <= 0:
        return False
    quotient = value / step
    return quotient.is_finite() and quotient == quotient.to_integral_value()
