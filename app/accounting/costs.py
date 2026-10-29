"""Minimal pre-trade cost model inputs.

This is deliberately not the full M3 accounting engine.  It provides an
explicit, versioned cost estimate so the M1 gate can reject missing, negative,
or non-finite cost inputs.  Financing here is a one-day placeholder; triple
rollover accrual is owned by the later rollover/accounting milestone.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.common.validation import ValidationResult, parse_decimal


@dataclass(frozen=True)
class CostModelTerms:
    model_id: str
    version: str
    commission_rate: Decimal  # fraction of notional, per side
    slippage_pips: Decimal  # adverse pips, per side
    financing_rate_daily: Decimal  # fraction of notional, one-day placeholder
    source: str

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        for name in ("model_id", "version", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"missing_{name}")
        for name in ("commission_rate", "slippage_pips", "financing_rate_daily"):
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None or value < 0:
                errors.append(error or f"invalid_{name}")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def decimal(self, name: str) -> Decimal:
        value, error = parse_decimal(getattr(self, name), name)
        if error or value is None:
            raise ValueError(error or f"invalid_{name}")
        return value

    def audit_view(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "commission_rate": self.commission_rate,
            "slippage_pips": self.slippage_pips,
            "financing_rate_daily": self.financing_rate_daily,
            "source": self.source,
        }


@dataclass(frozen=True)
class CostEstimate:
    commission_account: Decimal
    slippage_account: Decimal
    financing_account: Decimal
    total_account: Decimal
    basis: str = "m1_round_trip_estimate"

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        values: dict[str, Decimal] = {}
        for name in (
            "commission_account",
            "slippage_account",
            "financing_account",
            "total_account",
        ):
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None or value < 0:
                errors.append(error or f"invalid_{name}")
            else:
                values[name] = value
        if len(values) == 4:
            expected = (
                values["commission_account"]
                + values["slippage_account"]
                + values["financing_account"]
            )
            if values["total_account"] != expected:
                errors.append("cost_total_mismatch")
        if not isinstance(self.basis, str) or not self.basis.strip():
            errors.append("missing_cost_basis")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "commission_account": self.commission_account,
            "slippage_account": self.slippage_account,
            "financing_account": self.financing_account,
            "total_account": self.total_account,
            "basis": self.basis,
        }


class CostModel(Protocol):
    terms: CostModelTerms

    def validate(self) -> ValidationResult: ...

    def estimate_new_order_cost(
        self,
        *,
        notional_account: Decimal,
        pip_value_account: Decimal,
    ) -> CostEstimate: ...


@dataclass(frozen=True)
class StaticCostModel:
    terms: CostModelTerms

    def validate(self) -> ValidationResult:
        return self.terms.validate()

    def estimate_new_order_cost(
        self,
        *,
        notional_account: Decimal,
        pip_value_account: Decimal,
    ) -> CostEstimate:
        if not self.validate().valid:
            raise ValueError("Invalid cost model")
        commission_rate = self.terms.decimal("commission_rate")
        slippage_pips = self.terms.decimal("slippage_pips")
        financing_rate = self.terms.decimal("financing_rate_daily")
        notional, notional_error = parse_decimal(notional_account, "notional_account")
        pip_value, pip_error = parse_decimal(pip_value_account, "pip_value_account")
        if notional_error or notional is None or notional <= 0:
            raise ValueError("Invalid notional for cost estimate")
        if pip_error or pip_value is None or pip_value <= 0:
            raise ValueError("Invalid pip value for cost estimate")
        commission = notional * commission_rate * Decimal(2)
        slippage = pip_value * slippage_pips * Decimal(2)
        financing = notional * financing_rate
        return CostEstimate(
            commission_account=commission,
            slippage_account=slippage,
            financing_account=financing,
            total_account=commission + slippage + financing,
        )

    def audit_view(self) -> dict[str, Any]:
        return self.terms.audit_view()
