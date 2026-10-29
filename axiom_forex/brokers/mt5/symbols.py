"""Reconcile AXIOM instrument specifications with the broker's own terms.

AXIOM keeps its instrument terms in a versioned catalog and refuses to invent
them.  MT5 reports its own terms (``symbol_info``).  This module compares the
two so that:

* market data is accepted only when the price grid agrees (precision/tick),
  because a quote with the wrong grid cannot be evaluated;
* an order is accepted only when the *contract* terms agree exactly -- digits,
  tick size, contract size, minimum volume, volume step -- and the catalog's
  maximum does not exceed the broker's.

When they disagree the catalog is wrong (or stale) and trading stops with
``mt5_symbol_spec_mismatch`` and the precise diff, which is exactly the
information the operator needs to update the catalog.  Nothing is averaged,
rounded, or guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.common.validation import ValidationResult
from app.universe.specs import CurrencyPairSpec, QuantityUnit

from .terminal import MT5DataRefused, MT5SymbolInfo, symbol_trade_mode_name


@dataclass(frozen=True)
class SymbolCrossCheck:
    """The diff between the catalog spec and the broker symbol."""

    pair: str
    provider_symbol: str
    spec_version: str
    data_ok: bool
    trading_ok: bool
    errors: tuple[str, ...]
    differences: tuple[tuple[str, str, str], ...]
    broker_trade_mode: str

    @property
    def ok(self) -> bool:
        return self.data_ok and self.trading_ok

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "provider_symbol": self.provider_symbol,
            "spec_version": self.spec_version,
            "data_ok": self.data_ok,
            "trading_ok": self.trading_ok,
            "errors": list(self.errors),
            "differences": [
                {"field": field, "catalog": catalog, "broker": broker}
                for field, catalog, broker in self.differences
            ],
            "broker_trade_mode": self.broker_trade_mode,
            "source": "mt5_symbol_spec_crosscheck",
        }


def cross_check(spec: CurrencyPairSpec, info: MT5SymbolInfo) -> SymbolCrossCheck:
    errors: list[str] = []
    differences: list[tuple[str, str, str]] = []

    def compare(field: str, catalog: Any, broker: Any) -> bool:
        if str(catalog) != str(broker):
            differences.append((field, str(catalog), str(broker)))
            return False
        return True

    base, quote = _legs(str(spec.pair))
    price_grid_ok = True
    if base and info.currency_base and base != info.currency_base.upper():
        # The symbol is not the instrument the catalog means: even its quotes
        # are the wrong instrument's.
        errors.append("mt5_symbol_currency_mismatch")
        differences.append(("currency_base", base, info.currency_base))
        price_grid_ok = False
    if quote and info.currency_profit and quote != info.currency_profit.upper():
        errors.append("mt5_symbol_currency_mismatch")
        differences.append(("currency_profit", quote, info.currency_profit))
        price_grid_ok = False

    data_ok = price_grid_ok
    if not compare("quote_precision", spec.quote_precision, info.digits):
        data_ok = False
        errors.append("mt5_symbol_precision_mismatch")
    if not compare(
        "price_tick_size",
        spec.price_tick_size_decimal().normalize(),
        info.point.normalize(),
    ):
        data_ok = False
        errors.append("mt5_symbol_tick_mismatch")

    trading_ok = data_ok
    if not compare(
        "contract_size",
        spec.contract_size_decimal().normalize(),
        info.trade_contract_size.normalize(),
    ):
        trading_ok = False
        errors.append("mt5_symbol_contract_size_mismatch")
    if not compare(
        "min_quantity",
        spec.min_quantity_decimal().normalize(),
        info.volume_min.normalize(),
    ):
        trading_ok = False
        errors.append("mt5_symbol_min_volume_mismatch")
    if not compare(
        "quantity_step",
        spec.quantity_step_decimal().normalize(),
        info.volume_step.normalize(),
    ):
        trading_ok = False
        errors.append("mt5_symbol_volume_step_mismatch")
    max_quantity = spec.max_quantity_decimal()
    if max_quantity is not None and max_quantity > info.volume_max:
        trading_ok = False
        errors.append("mt5_symbol_max_volume_exceeded")
        differences.append(
            ("max_quantity", str(max_quantity), str(info.volume_max))
        )
    if not info.tradable:
        trading_ok = False
        errors.append("mt5_symbol_not_fully_tradable")
        differences.append(
            ("trade_mode", "FULL", symbol_trade_mode_name(info.trade_mode))
        )

    return SymbolCrossCheck(
        pair=str(spec.pair),
        provider_symbol=info.name,
        spec_version=str(spec.spec_version),
        data_ok=data_ok,
        trading_ok=trading_ok,
        errors=tuple(dict.fromkeys(errors)),
        differences=tuple(differences),
        broker_trade_mode=symbol_trade_mode_name(info.trade_mode),
    )


def require_data(spec: CurrencyPairSpec, info: MT5SymbolInfo) -> SymbolCrossCheck:
    check = cross_check(spec, info)
    if not check.data_ok:
        raise MT5DataRefused(
            "mt5_symbol_spec_mismatch",
            ",".join(check.errors) or "price_grid_mismatch",
        )
    return check


def require_trading(spec: CurrencyPairSpec, info: MT5SymbolInfo) -> SymbolCrossCheck:
    check = cross_check(spec, info)
    if not check.trading_ok:
        raise MT5DataRefused(
            "mt5_symbol_spec_mismatch",
            ",".join(check.errors) or "contract_terms_mismatch",
        )
    return check


def broker_terms_candidate(spec: CurrencyPairSpec, info: MT5SymbolInfo) -> dict[str, Any]:
    """The catalog entry the broker's own terms imply.

    Returned for the operator to review and commit with an explicit version; it
    is never applied automatically.  Every value comes from ``symbol_info``.
    """

    return {
        "pair": str(spec.pair),
        "pip_size": str(spec.pip_size_decimal()),
        "price_tick_size": str(info.point.normalize()),
        "quote_precision": info.digits,
        "contract_size": str(info.trade_contract_size.normalize()),
        "min_quantity": str(info.volume_min.normalize()),
        "quantity_step": str(info.volume_step.normalize()),
        "max_quantity": str(info.volume_max.normalize()),
        "quantity_unit": QuantityUnit.LOTS.value,
        "margin_multiplier": "operator_supplied",
        "correlation_group": "operator_supplied",
        "trading_calendar_id": "operator_supplied",
        "trading_calendar_version": "operator_supplied",
        "status": "ACTIVE" if info.tradable else "INACTIVE",
        "spec_version": "operator_supplied",
        "source": "mt5_symbol_info",
        "provider_aliases": {"mt5": info.name},
        "broker_symbol": info.name,
    }


def to_broker_volume(
    spec: CurrencyPairSpec, quantity: Decimal, info: MT5SymbolInfo
) -> tuple[Decimal | None, ValidationResult]:
    """AXIOM quantity -> MT5 lots, exactly, or a refusal.

    MT5 volumes are always lots.  A ``BASE_UNITS`` catalog converts through the
    contract size and must divide evenly; rounding a volume would place a
    different order than the gated one, so it is refused.
    """

    errors: list[str] = []
    quantity, quantity_result = spec.parse_quantity(quantity)
    if quantity is None:
        return None, quantity_result
    unit = QuantityUnit(spec.quantity_unit_value)
    if unit == QuantityUnit.LOTS:
        lots = quantity
    else:
        contract = spec.contract_size_decimal()
        if contract <= 0:
            return None, ValidationResult.fail("invalid_contract_size")
        # Exact division only: a base-unit size that is not a whole number of
        # lots would have to be rounded, and a rounded volume is not the gated
        # order.  The broker step check below reports it precisely.
        lots = quantity / contract
    if lots < info.volume_min:
        errors.append("quantity_below_broker_minimum")
    if lots > info.volume_max:
        errors.append("quantity_above_broker_maximum")
    if info.volume_step > 0:
        steps = lots / info.volume_step
        if steps != steps.to_integral_value():
            errors.append("quantity_not_step_aligned_with_broker")
    if errors:
        return None, ValidationResult.fail(*errors)
    return lots.normalize(), ValidationResult.ok()


def _legs(pair: str) -> tuple[str, str]:
    text = pair.replace("/", "").replace("-", "").replace("_", "").upper()
    if len(text) != 6:
        return ("", "")
    return (text[:3], text[3:])
