"""Explicit correlation matrix; missing correlations fail closed."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from app.common.validation import ValidationResult, canonical_pair, parse_decimal


@dataclass(frozen=True)
class CorrelationMatrix:
    pairs: frozenset[str]
    values: Mapping[tuple[str, str], Decimal]
    source: str
    version: str

    def _canonical(self, pair: str) -> str:
        canonical, error = canonical_pair(pair)
        if error or canonical is None:
            raise ValueError(error or "invalid_pair")
        return canonical

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not self.pairs:
            errors.append("empty_correlation_universe")
        for name in ("source", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"missing_{name}")
        for first in self.pairs:
            for second in self.pairs:
                key = (first, second)
                value = self.values.get(key)
                parsed, parse_error = parse_decimal(value, "correlation")
                if parse_error or parsed is None:
                    errors.append(f"missing_correlation:{first}:{second}")
                    continue
                if parsed < Decimal(-1) or parsed > Decimal(1):
                    errors.append(f"correlation_out_of_range:{first}:{second}")
                if first == second and parsed != Decimal(1):
                    errors.append(f"non_unit_diagonal:{first}")
                reverse = self.values.get((second, first))
                reverse_parsed, reverse_error = parse_decimal(reverse, "correlation")
                if reverse_error or reverse_parsed is None or reverse_parsed != parsed:
                    errors.append(f"asymmetric_correlation:{first}:{second}")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def get(self, first: str, second: str) -> Decimal:
        a = self._canonical(first)
        b = self._canonical(second)
        if not self.validate().valid:
            raise ValueError("Invalid correlation matrix")
        value = self.values.get((a, b))
        if value is None:
            raise ValueError("missing_correlation")
        return value

    def audit_view(self) -> dict[str, Any]:
        return {
            "pairs": sorted(self.pairs),
            "source": self.source,
            "version": self.version,
            "values": {f"{a}/{b}": value for (a, b), value in self.values.items()},
        }


@dataclass(frozen=True)
class AggregateRiskLimits:
    max_gross_exposure_account: Decimal
    max_adjusted_exposure_account: Decimal
    max_abs_currency_exposure_account: Decimal
    version: str

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        for name in (
            "max_gross_exposure_account",
            "max_adjusted_exposure_account",
            "max_abs_currency_exposure_account",
        ):
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None or value <= 0:
                errors.append(error or f"invalid_{name}")
        if not isinstance(self.version, str) or not self.version.strip():
            errors.append("missing_version")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()


@dataclass(frozen=True)
class AggregateRiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    gross_exposure_account: Decimal
    adjusted_exposure_account: Decimal | None
    max_abs_currency_exposure_account: Decimal

    def audit_view(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "gross_exposure_account": self.gross_exposure_account,
            "adjusted_exposure_account": self.adjusted_exposure_account,
            "max_abs_currency_exposure_account": self.max_abs_currency_exposure_account,
        }


def check_aggregate_exposure(
    *,
    matrix: CorrelationMatrix,
    limits: AggregateRiskLimits,
    pair_exposures_account: Mapping[str, Decimal],
    currency_exposures_account: Mapping[str, Decimal],
) -> AggregateRiskDecision:
    reasons: list[str] = []
    matrix_validation = matrix.validate()
    limits_validation = limits.validate()
    if not matrix_validation.valid:
        reasons.append("invalid_correlation_matrix")
    if not limits_validation.valid:
        reasons.append("invalid_aggregate_limits")
    parsed_exposures: dict[str, Decimal] = {}
    for pair, value in pair_exposures_account.items():
        canonical, error = canonical_pair(pair)
        value_decimal, value_error = parse_decimal(value, "pair_exposure")
        if error or canonical is None or value_error or value_decimal is None:
            reasons.append("invalid_pair_exposure")
            continue
        if canonical not in matrix.pairs:
            reasons.append(f"missing_correlation_for:{canonical}")
        parsed_exposures[canonical] = value_decimal
    gross = sum((abs(value) for value in parsed_exposures.values()), Decimal(0))
    adjusted: Decimal | None = None
    if not reasons:
        variance = Decimal(0)
        for first, first_value in parsed_exposures.items():
            for second, second_value in parsed_exposures.items():
                variance += first_value * second_value * matrix.get(first, second)
        if variance < 0:
            reasons.append("non_positive_semidefinite_correlation")
        else:
            adjusted = variance.sqrt()
    max_currency = Decimal(0)
    for value in currency_exposures_account.values():
        parsed, error = parse_decimal(value, "currency_exposure")
        if error or parsed is None:
            reasons.append("invalid_currency_exposure")
            continue
        max_currency = max(max_currency, abs(parsed))
    if limits_validation.valid:
        if gross > limits.max_gross_exposure_account:
            reasons.append("max_gross_exposure_exceeded")
        if adjusted is not None and adjusted > limits.max_adjusted_exposure_account:
            reasons.append("max_adjusted_exposure_exceeded")
        if max_currency > limits.max_abs_currency_exposure_account:
            reasons.append("max_currency_exposure_exceeded")
    return AggregateRiskDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        gross_exposure_account=gross,
        adjusted_exposure_account=adjusted,
        max_abs_currency_exposure_account=max_currency,
    )
