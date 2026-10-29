"""Explicit forex instrument specifications.

No instrument is built in.  Every pair, alias, pip size, quantity step,
contract size, margin multiplier, calendar identity, and version comes from a
versioned catalog supplied by configuration or a local fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from app.common.validation import (
    ValidationResult,
    canonical_pair,
    decimal_places,
    is_multiple_of_step,
    parse_decimal,
    validate_currency_code,
)


class QuantityUnit(str, Enum):
    BASE_UNITS = "BASE_UNITS"
    LOTS = "LOTS"


class InstrumentStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


@dataclass(frozen=True)
class CurrencyPairSpec:
    pair: str
    pip_size: Decimal
    price_tick_size: Decimal
    quote_precision: int
    contract_size: Decimal
    min_quantity: Decimal
    quantity_step: Decimal
    max_quantity: Decimal | None
    quantity_unit: QuantityUnit | str
    margin_multiplier: Decimal
    correlation_group: str
    trading_calendar_id: str
    trading_calendar_version: str
    status: InstrumentStatus | str
    spec_version: str
    source: str
    provider_aliases: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_aliases", MappingProxyType(dict(self.provider_aliases)))

    @property
    def canonical_pair(self) -> str | None:
        pair, _ = canonical_pair(self.pair)
        return pair

    @property
    def base_currency(self) -> str:
        pair = self.canonical_pair
        return pair.split("/")[0] if pair else ""

    @property
    def quote_currency(self) -> str:
        pair = self.canonical_pair
        return pair.split("/")[1] if pair else ""

    def _decimal(self, field_name: str) -> Decimal:
        value, error = parse_decimal(getattr(self, field_name), field_name)
        if error or value is None:
            raise ValueError(error or f"invalid_{field_name}")
        return value

    def pip_size_decimal(self) -> Decimal:
        return self._decimal("pip_size")

    def price_tick_size_decimal(self) -> Decimal:
        return self._decimal("price_tick_size")

    def contract_size_decimal(self) -> Decimal:
        return self._decimal("contract_size")

    def min_quantity_decimal(self) -> Decimal:
        return self._decimal("min_quantity")

    def quantity_step_decimal(self) -> Decimal:
        return self._decimal("quantity_step")

    def margin_multiplier_decimal(self) -> Decimal:
        return self._decimal("margin_multiplier")

    def max_quantity_decimal(self) -> Decimal | None:
        if self.max_quantity is None:
            return None
        return self._decimal("max_quantity")

    @property
    def quantity_unit_value(self) -> str:
        return _enum_value(self.quantity_unit)

    @property
    def status_value(self) -> str:
        return _enum_value(self.status)

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        pair, pair_error = canonical_pair(self.pair)
        if pair_error or pair is None:
            errors.append(pair_error or "invalid_pair")
        positive_fields = (
            "pip_size",
            "price_tick_size",
            "contract_size",
            "min_quantity",
            "quantity_step",
            "margin_multiplier",
        )
        parsed: dict[str, Decimal] = {}
        for name in positive_fields:
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None or value <= 0:
                errors.append(error or f"invalid_{name}")
            else:
                parsed[name] = value
        if not isinstance(self.quote_precision, int) or isinstance(self.quote_precision, bool):
            errors.append("invalid_quote_precision")
        elif self.quote_precision < 0:
            errors.append("negative_quote_precision")
        max_quantity, max_error = parse_decimal(self.max_quantity, "max_quantity")
        if self.max_quantity is not None:
            if max_error or max_quantity is None or max_quantity <= 0:
                errors.append(max_error or "invalid_max_quantity")
            elif "min_quantity" in parsed and max_quantity < parsed["min_quantity"]:
                errors.append("max_quantity_below_minimum")
        if "pip_size" in parsed and "price_tick_size" in parsed:
            if parsed["pip_size"] < parsed["price_tick_size"]:
                errors.append("pip_size_below_tick")
        if "price_tick_size" in parsed and isinstance(self.quote_precision, int):
            if decimal_places(parsed["price_tick_size"]) > self.quote_precision:
                errors.append("tick_precision_exceeds_quote_precision")
        if "min_quantity" in parsed and "quantity_step" in parsed:
            if not is_multiple_of_step(parsed["min_quantity"], parsed["quantity_step"]):
                errors.append("minimum_not_step_aligned")
        try:
            if QuantityUnit(self.quantity_unit_value) not in set(QuantityUnit):
                errors.append("invalid_quantity_unit")
        except ValueError:
            errors.append("invalid_quantity_unit")
        try:
            InstrumentStatus(self.status_value)
        except ValueError:
            errors.append("invalid_status")
        for name in (
            "correlation_group",
            "trading_calendar_id",
            "trading_calendar_version",
            "spec_version",
            "source",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"missing_{name}")
        for provider_id, alias in self.provider_aliases.items():
            if not isinstance(provider_id, str) or not provider_id.strip():
                errors.append("invalid_provider_id")
            if not isinstance(alias, str) or not alias.strip():
                errors.append("invalid_provider_alias")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def parse_quantity(self, value: Any) -> tuple[Decimal | None, ValidationResult]:
        quantity, error = parse_decimal(value, "quantity")
        if error or quantity is None:
            return None, ValidationResult.fail(error or "invalid_quantity")
        errors: list[str] = []
        if quantity <= 0:
            errors.append("non_positive_quantity")
        min_quantity = self.min_quantity_decimal()
        step = self.quantity_step_decimal()
        max_quantity = self.max_quantity_decimal()
        if quantity < min_quantity:
            errors.append("quantity_below_minimum")
        if max_quantity is not None and quantity > max_quantity:
            errors.append("quantity_above_maximum")
        if not is_multiple_of_step(quantity, step):
            errors.append("quantity_not_step_aligned")
        if errors:
            return None, ValidationResult.fail(*errors)
        return quantity, ValidationResult.ok()

    def to_base_units(self, quantity: Decimal) -> Decimal:
        unit = QuantityUnit(self.quantity_unit_value)
        if unit == QuantityUnit.LOTS:
            return quantity * self.contract_size_decimal()
        if unit == QuantityUnit.BASE_UNITS:
            return quantity
        raise ValueError("Unsupported quantity unit")

    def spread_pips(self, spread: Decimal) -> Decimal:
        if not spread.is_finite() or spread < 0:
            raise ValueError("Invalid spread")
        return spread / self.pip_size_decimal()

    def validate_price(self, value: Any) -> ValidationResult:
        price, error = parse_decimal(value, "price")
        if error or price is None:
            return ValidationResult.fail(error or "invalid_price")
        errors: list[str] = []
        if price <= 0:
            errors.append("non_positive_price")
        if decimal_places(price) > self.quote_precision:
            errors.append("price_precision_exceeded")
        if not is_multiple_of_step(price, self.price_tick_size_decimal()):
            errors.append("price_not_tick_aligned")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def alias_for(self, provider_id: str) -> str | None:
        return self.provider_aliases.get(provider_id)

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.canonical_pair,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "pip_size": self.pip_size,
            "price_tick_size": self.price_tick_size,
            "quote_precision": self.quote_precision,
            "contract_size": self.contract_size,
            "min_quantity": self.min_quantity,
            "quantity_step": self.quantity_step,
            "max_quantity": self.max_quantity,
            "quantity_unit": self.quantity_unit_value,
            "margin_multiplier": self.margin_multiplier,
            "correlation_group": self.correlation_group,
            "trading_calendar_id": self.trading_calendar_id,
            "trading_calendar_version": self.trading_calendar_version,
            "status": self.status_value,
            "spec_version": self.spec_version,
            "source": self.source,
            "provider_aliases": dict(self.provider_aliases),
        }


@dataclass(frozen=True)
class InstrumentCatalog:
    """Strict canonical-pair and provider-alias catalog; no fallback mapping."""

    specs: tuple[CurrencyPairSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "specs", tuple(self.specs))
        by_pair: dict[str, CurrencyPairSpec] = {}
        by_alias: dict[tuple[str, str], CurrencyPairSpec] = {}
        for spec in self.specs:
            pair = spec.canonical_pair
            if pair is None:
                raise ValueError("Catalog contains an invalid pair")
            if pair in by_pair:
                raise ValueError(f"Duplicate pair in catalog: {pair}")
            by_pair[pair] = spec
            for provider_id, alias in spec.provider_aliases.items():
                key = (provider_id, alias)
                if key in by_alias:
                    raise ValueError(f"Duplicate provider alias in catalog: {key}")
                by_alias[key] = spec
        object.__setattr__(self, "_by_pair", MappingProxyType(by_pair))
        object.__setattr__(self, "_by_alias", MappingProxyType(by_alias))

    def validate(self) -> ValidationResult:
        """Validate catalog identity only; individual specs are gated later.

        This ordering lets the pre-trade gate report an invalid resolved spec
        as ``INVALID_SPEC`` rather than hiding every spec problem behind a
        generic catalog error.
        """
        errors: list[str] = []
        seen_pairs: set[str] = set()
        seen_aliases: set[tuple[str, str]] = set()
        for spec in self.specs:
            pair = spec.canonical_pair
            if pair is None:
                errors.append(f"invalid_catalog_pair:{spec.pair}")
                continue
            if pair in seen_pairs:
                errors.append(f"duplicate_catalog_pair:{pair}")
            seen_pairs.add(pair)
            for provider_id, alias in spec.provider_aliases.items():
                if not isinstance(provider_id, str) or not provider_id.strip():
                    errors.append(f"invalid_provider_id:{pair}")
                    continue
                if not isinstance(alias, str) or not alias.strip():
                    errors.append(f"invalid_provider_alias:{pair}")
                    continue
                key = (provider_id, alias)
                if key in seen_aliases:
                    errors.append(f"duplicate_catalog_alias:{provider_id}:{alias}")
                seen_aliases.add(key)
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def get(self, pair: str) -> CurrencyPairSpec | None:
        canonical, error = canonical_pair(pair)
        if error or canonical is None:
            return None
        return self._by_pair.get(canonical)

    def resolve(self, provider_id: str, provider_symbol: str) -> CurrencyPairSpec | None:
        if not isinstance(provider_id, str) or not provider_id:
            return None
        if not isinstance(provider_symbol, str) or not provider_symbol.strip():
            return None
        return self._by_alias.get((provider_id, provider_symbol.strip()))

    @property
    def pairs(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_pair))

    def audit_view(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs,
            "spec_versions": {pair: spec.spec_version for pair, spec in self._by_pair.items()},
        }
