"""Conservative, finite pre-trade risk calculations.

These calculations support the M1 gate only.  They do not implement position
accounting, realized P&L, margin calls, or financing accrual; those remain M3.
Every input must be explicit and finite, and any failed calculation causes the
gate to reject rather than substitute a default.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from app.accounting.costs import CostEstimate, CostModel
from app.common.types import OrderSide
from app.common.validation import ValidationResult, parse_decimal
from app.config.settings import AccountProfile
from app.universe.specs import CurrencyPairSpec

from .models import PositionSnapshot, RiskContext


@dataclass(frozen=True)
class RiskCalculation:
    side_sign: int
    base_units: Decimal
    executable_price: Decimal
    notional_quote: Decimal
    quote_to_account_rate: Decimal
    base_to_account_rate: Decimal
    notional_account: Decimal
    pip_value_account: Decimal
    required_margin_account: Decimal
    projected_gross_exposure_account: Decimal
    projected_equity_account: Decimal
    effective_leverage: Decimal
    projected_currency_exposures_account: Mapping[str, Decimal]
    max_abs_currency_exposure_account: Decimal
    correlation_group: str
    projected_correlation_group_exposure_account: Decimal
    estimated_costs: CostEstimate
    #: True only for a close that the gate matched to an open ledger position.
    exposure_reducing: bool = False
    close_position_id: str | None = None
    closed_base_units: Decimal = Decimal(0)
    margin_released_account: Decimal = Decimal(0)

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if self.side_sign not in (-1, 1):
            errors.append("invalid_side_sign")
        positive_fields = (
            "base_units",
            "executable_price",
            "notional_quote",
            "quote_to_account_rate",
            "base_to_account_rate",
            "notional_account",
            "pip_value_account",
            "required_margin_account",
            "projected_equity_account",
            "effective_leverage",
        )
        # A close adds no margin and can take gross exposure (and therefore
        # effective leverage) all the way to zero, so those two fields must be
        # allowed to be exactly zero -- but never negative.
        zero_allowed = {"required_margin_account", "effective_leverage"}
        for name in positive_fields:
            value, error = parse_decimal(getattr(self, name), name)
            if self.exposure_reducing and name in zero_allowed:
                if error or value is None or value < 0:
                    errors.append(error or f"invalid_{name}")
                continue
            if error or value is None or value <= 0:
                errors.append(error or f"invalid_{name}")
        released, released_error = parse_decimal(
            self.margin_released_account, "margin_released_account"
        )
        if released_error or released is None or released < 0:
            errors.append(released_error or "invalid_margin_released_account")
        closed_units, closed_error = parse_decimal(
            self.closed_base_units, "closed_base_units"
        )
        if closed_error or closed_units is None or closed_units < 0:
            errors.append(closed_error or "invalid_closed_base_units")
        if self.exposure_reducing:
            if not isinstance(self.close_position_id, str) or not self.close_position_id.strip():
                errors.append("missing_close_position_id")
            if closed_units is None or closed_units <= 0:
                errors.append("close_without_closed_units")
        elif self.close_position_id is not None or (closed_units or 0) > 0:
            errors.append("close_fields_without_close_intent")
        for name in (
            "projected_gross_exposure_account",
            "max_abs_currency_exposure_account",
        ):
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None or value < 0:
                errors.append(error or f"invalid_{name}")
        for currency, exposure in self.projected_currency_exposures_account.items():
            if not isinstance(currency, str) or not currency.strip():
                errors.append("invalid_exposure_currency")
            value, error = parse_decimal(exposure, "currency_exposure")
            if error or value is None:
                errors.append(error or "invalid_currency_exposure")
        group_exposure, group_error = parse_decimal(
            self.projected_correlation_group_exposure_account, "correlation_group_exposure"
        )
        if group_error or group_exposure is None:
            errors.append(group_error or "invalid_correlation_group_exposure")
        if not isinstance(self.correlation_group, str) or not self.correlation_group.strip():
            errors.append("invalid_correlation_group")
        cost_result = self.estimated_costs.validate()
        errors.extend(f"cost:{error}" for error in cost_result.errors)
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "side_sign": self.side_sign,
            "base_units": self.base_units,
            "executable_price": self.executable_price,
            "notional_quote": self.notional_quote,
            "quote_to_account_rate": self.quote_to_account_rate,
            "base_to_account_rate": self.base_to_account_rate,
            "notional_account": self.notional_account,
            "pip_value_account": self.pip_value_account,
            "required_margin_account": self.required_margin_account,
            "projected_gross_exposure_account": self.projected_gross_exposure_account,
            "projected_equity_account": self.projected_equity_account,
            "effective_leverage": self.effective_leverage,
            "projected_currency_exposures_account": dict(
                self.projected_currency_exposures_account
            ),
            "max_abs_currency_exposure_account": self.max_abs_currency_exposure_account,
            "correlation_group": self.correlation_group,
            "projected_correlation_group_exposure_account": self.projected_correlation_group_exposure_account,
            "estimated_costs": self.estimated_costs.audit_view(),
            "exposure_reducing": self.exposure_reducing,
            "close_position_id": self.close_position_id,
            "closed_base_units": self.closed_base_units,
            "margin_released_account": self.margin_released_account,
        }


def calculate_risk(
    *,
    side: OrderSide | str,
    quantity: Decimal,
    spec: CurrencyPairSpec,
    executable_price: Decimal,
    quote_to_account_rate: Decimal,
    base_to_account_rate: Decimal,
    account: AccountProfile,
    risk_context: RiskContext,
    cost_model: CostModel,
    close_position: PositionSnapshot | None = None,
) -> RiskCalculation:
    side_value = side.value if isinstance(side, OrderSide) else str(side).upper()
    if side_value == OrderSide.BUY.value:
        side_sign = 1
    elif side_value == OrderSide.SELL.value:
        side_sign = -1
    else:
        raise ValueError("Unsupported order side")
    if close_position is not None:
        # The projection below must describe the *change* this order makes to the
        # book, so a close is modelled with the sign of the position it unwinds,
        # not the sign of its own side.  Anything that does not oppose the
        # position would add exposure and is refused here as well as in the gate.
        position_validation = close_position.validate()
        if not position_validation.valid:
            raise ValueError("invalid_close_position: " + position_validation.reason)
        position_side = close_position.side_value
        if position_side not in {OrderSide.BUY.value, OrderSide.SELL.value}:
            raise ValueError("invalid_close_position_side")
        if position_side == side_value:
            raise ValueError("close_side_does_not_oppose_position")
    inputs = {
        "quantity": quantity,
        "executable_price": executable_price,
        "quote_to_account_rate": quote_to_account_rate,
        "base_to_account_rate": base_to_account_rate,
    }
    parsed: dict[str, Decimal] = {}
    for name, value in inputs.items():
        decimal_value, error = parse_decimal(value, name)
        if error or decimal_value is None or decimal_value <= 0:
            raise ValueError(error or f"invalid_{name}")
        parsed[name] = decimal_value
    base_units = spec.to_base_units(parsed["quantity"])
    if not base_units.is_finite() or base_units <= 0:
        raise ValueError("invalid_base_units")
    leverage = account.leverage_decimal
    margin_multiplier = spec.margin_multiplier_decimal()
    if leverage <= 0 or margin_multiplier <= 0:
        raise ValueError("invalid_leverage_or_margin_multiplier")
    notional_quote = base_units * parsed["executable_price"]
    notional_account = notional_quote * parsed["quote_to_account_rate"]
    pip_value_account = (
        base_units * spec.pip_size_decimal() * parsed["quote_to_account_rate"]
    )
    margin_for_notional = notional_account / leverage * margin_multiplier
    estimated_costs = cost_model.estimate_new_order_cost(
        notional_account=notional_account,
        pip_value_account=pip_value_account,
    )
    # Cost-estimate validation is deliberately left to the gate so that an
    # invalid estimate can be reported as a cost-calculation rejection rather
    # than silently folded into a generic risk-calculation error.
    equity = risk_context.decimal("equity_account")
    gross_exposure = risk_context.decimal("gross_exposure_account")
    if equity <= 0 or gross_exposure < 0:
        raise ValueError("invalid_account_exposure_state")
    projected_equity = equity - estimated_costs.total_account
    if projected_equity <= 0:
        raise ValueError("non_positive_projected_equity")
    if close_position is not None:
        position_units, position_error = parse_decimal(
            close_position.base_units, "position_base_units"
        )
        if position_error or position_units is None or position_units <= 0:
            raise ValueError("invalid_close_position_units")
        if base_units > position_units:
            raise ValueError("close_exceeds_position")
        position_sign = 1 if position_side == OrderSide.BUY.value else -1
        required_margin = Decimal(0)
        margin_released = margin_for_notional
        # The closed notional is already part of gross exposure; a valid close
        # can only take it away.  Anything else means the request is not the
        # reduction it claims to be.
        projected_gross_exposure = gross_exposure - notional_account
        if projected_gross_exposure < 0:
            raise ValueError("close_exceeds_gross_exposure")
    else:
        position_sign = side_sign
        required_margin = margin_for_notional
        margin_released = Decimal(0)
        projected_gross_exposure = gross_exposure + notional_account
    effective_leverage = projected_gross_exposure / projected_equity
    projected_currency: dict[str, Decimal] = {}
    for currency, value in risk_context.currency_exposures_account.items():
        parsed_exposure, error = parse_decimal(value, "currency_exposure")
        if error or parsed_exposure is None:
            raise ValueError("invalid_currency_exposure")
        projected_currency[currency] = parsed_exposure
    exposure_delta_sign = -position_sign if close_position is not None else side_sign
    base_delta = base_units * parsed["base_to_account_rate"] * exposure_delta_sign
    quote_delta = -notional_account * exposure_delta_sign
    projected_currency[spec.base_currency] = (
        projected_currency.get(spec.base_currency, Decimal(0)) + base_delta
    )
    projected_currency[spec.quote_currency] = (
        projected_currency.get(spec.quote_currency, Decimal(0)) + quote_delta
    )
    max_abs_currency_exposure = max(
        (abs(value) for value in projected_currency.values()), default=Decimal(0)
    )
    group = spec.correlation_group
    current_group_exposure = risk_context.correlation_group_exposure(group)
    projected_group_exposure = (
        current_group_exposure
        + notional_account * (-position_sign if close_position is not None else side_sign)
    )
    return RiskCalculation(
        side_sign=side_sign,
        base_units=base_units,
        executable_price=parsed["executable_price"],
        notional_quote=notional_quote,
        quote_to_account_rate=parsed["quote_to_account_rate"],
        base_to_account_rate=parsed["base_to_account_rate"],
        notional_account=notional_account,
        pip_value_account=pip_value_account,
        required_margin_account=required_margin,
        projected_gross_exposure_account=projected_gross_exposure,
        projected_equity_account=projected_equity,
        effective_leverage=effective_leverage,
        projected_currency_exposures_account=MappingProxyType(projected_currency),
        max_abs_currency_exposure_account=max_abs_currency_exposure,
        correlation_group=group,
        projected_correlation_group_exposure_account=projected_group_exposure,
        estimated_costs=estimated_costs,
        exposure_reducing=close_position is not None,
        close_position_id=(
            close_position.position_id if close_position is not None else None
        ),
        closed_base_units=base_units if close_position is not None else Decimal(0),
        margin_released_account=margin_released,
    )
