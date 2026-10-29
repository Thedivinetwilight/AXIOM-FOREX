"""Explicit inputs and outputs for the M1 pre-trade gate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from app.accounting.costs import CostModel
from app.accounting.fx import ConversionRateBook
from app.accounting.rollover import RolloverPolicy
from app.common.types import OrderSide, TradingMode
from app.common.validation import (
    ValidationResult,
    as_utc,
    canonical_pair,
    parse_decimal,
    validate_currency_code,
)
from app.config.settings import AccountProfile, RiskLimits, SafetySettings, SystemState
from app.data.models import ForexQuote, QuoteFreshness

if TYPE_CHECKING:  # pragma: no cover - import cycle avoided at runtime
    from .calculations import RiskCalculation


FINAL_ORDER_STATES = frozenset({"FILLED", "CANCELLED", "REJECTED"})


@dataclass(frozen=True)
class UnresolvedOrder:
    order_id: str
    state: str
    pair: str | None = None
    updated_at: datetime | None = None

    @property
    def is_unresolved(self) -> bool:
        return str(self.state).upper() not in FINAL_ORDER_STATES

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            errors.append("missing_order_id")
        if not isinstance(self.state, str) or not self.state.strip():
            errors.append("missing_order_state")
        if self.pair is not None:
            _, pair_error = canonical_pair(self.pair)
            if pair_error:
                errors.append(pair_error)
        if self.updated_at is not None:
            _, timestamp_error = as_utc(self.updated_at, "order_updated_at")
            if timestamp_error:
                errors.append(timestamp_error)
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "state": self.state,
            "pair": self.pair,
            "updated_at": self.updated_at,
            "is_unresolved": self.is_unresolved,
        }


@dataclass(frozen=True)
class RiskContext:
    """Current account exposures, already normalized to account currency."""

    equity_account: Decimal
    gross_exposure_account: Decimal
    currency_exposures_account: Mapping[str, Decimal]
    correlation_group_exposures_account: Mapping[str, Decimal]
    open_positions: int
    realized_pnl_today_account: Decimal
    trades_today: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "currency_exposures_account",
            MappingProxyType(dict(self.currency_exposures_account)),
        )
        object.__setattr__(
            self,
            "correlation_group_exposures_account",
            MappingProxyType(dict(self.correlation_group_exposures_account)),
        )

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        equity, equity_error = parse_decimal(self.equity_account, "equity_account")
        if equity_error or equity is None or equity <= 0:
            errors.append(equity_error or "invalid_equity_account")
        gross, gross_error = parse_decimal(
            self.gross_exposure_account, "gross_exposure_account"
        )
        if gross_error or gross is None or gross < 0:
            errors.append(gross_error or "invalid_gross_exposure_account")
        realized, realized_error = parse_decimal(
            self.realized_pnl_today_account, "realized_pnl_today_account"
        )
        if realized_error or realized is None:
            errors.append(realized_error or "invalid_realized_pnl_today_account")
        if not isinstance(self.open_positions, int) or isinstance(self.open_positions, bool):
            errors.append("invalid_open_positions")
        elif self.open_positions < 0:
            errors.append("negative_open_positions")
        if not isinstance(self.trades_today, int) or isinstance(self.trades_today, bool):
            errors.append("invalid_trades_today")
        elif self.trades_today < 0:
            errors.append("negative_trades_today")
        for currency, exposure in self.currency_exposures_account.items():
            _, currency_error = validate_currency_code(currency, "currency")
            value, value_error = parse_decimal(exposure, "currency_exposure")
            if currency_error:
                errors.append(currency_error)
            if value_error or value is None:
                errors.append(value_error or "invalid_currency_exposure")
        for group, exposure in self.correlation_group_exposures_account.items():
            if not isinstance(group, str) or not group.strip():
                errors.append("invalid_correlation_group")
            value, value_error = parse_decimal(exposure, "correlation_group_exposure")
            if value_error or value is None:
                errors.append(value_error or "invalid_correlation_group_exposure")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def decimal(self, field_name: str) -> Decimal:
        value, error = parse_decimal(getattr(self, field_name), field_name)
        if error or value is None:
            raise ValueError(error or f"invalid_{field_name}")
        return value

    def currency_exposure(self, currency: str) -> Decimal:
        value, _ = parse_decimal(self.currency_exposures_account.get(currency, 0), "exposure")
        return value or Decimal(0)

    def correlation_group_exposure(self, group: str) -> Decimal:
        value, _ = parse_decimal(
            self.correlation_group_exposures_account.get(group, 0), "group_exposure"
        )
        return value or Decimal(0)

    def audit_view(self) -> dict[str, Any]:
        return {
            "equity_account": self.equity_account,
            "gross_exposure_account": self.gross_exposure_account,
            "currency_exposures_account": dict(self.currency_exposures_account),
            "correlation_group_exposures_account": dict(
                self.correlation_group_exposures_account
            ),
            "open_positions": self.open_positions,
            "realized_pnl_today_account": self.realized_pnl_today_account,
            "trades_today": self.trades_today,
        }


@dataclass(frozen=True)
class PositionSnapshot:
    """One open position, as reported by the ledger that owns it.

    The gate never trusts a caller's claim that an order "reduces risk"; it
    matches the request against this snapshot, which is built from the actual
    ledger.  ``pending_close_quantity`` is the part already promised to an
    in-flight close, so two concurrent closes cannot both consume the same
    quantity.
    """

    position_id: str
    account_id: str
    pair: str
    side: OrderSide | str
    quantity: Decimal | int | float | str
    quantity_unit: str
    base_units: Decimal | int | float | str
    opened_at: datetime
    pending_close_quantity: Decimal | int | float | str = 0

    @property
    def side_value(self) -> str:
        return (
            self.side.value
            if isinstance(self.side, OrderSide)
            else str(self.side).upper()
        )

    @property
    def available_quantity(self) -> Decimal:
        """Remaining quantity that is not already promised to another close."""

        remaining, remaining_error = parse_decimal(self.quantity, "position_quantity")
        pending, pending_error = parse_decimal(
            self.pending_close_quantity, "pending_close_quantity"
        )
        if remaining_error or remaining is None:
            return Decimal(0)
        if pending_error or pending is None:
            pending = Decimal(0)
        available = remaining - pending
        return available if available > 0 else Decimal(0)

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not isinstance(self.position_id, str) or not self.position_id.strip():
            errors.append("missing_position_id")
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            errors.append("missing_position_account_id")
        canonical, pair_error = canonical_pair(self.pair)
        if pair_error or canonical is None:
            errors.append(pair_error or "invalid_position_pair")
        try:
            OrderSide(self.side_value)
        except ValueError:
            errors.append("invalid_position_side")
        if not isinstance(self.quantity_unit, str) or not self.quantity_unit.strip():
            errors.append("missing_position_quantity_unit")
        for name in ("quantity", "base_units"):
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None or value <= 0:
                errors.append(error or f"invalid_{name}")
        pending, pending_error = parse_decimal(
            self.pending_close_quantity, "pending_close_quantity"
        )
        if pending_error or pending is None or pending < 0:
            errors.append(pending_error or "invalid_pending_close_quantity")
        elif pending > (parse_decimal(self.quantity, "quantity")[0] or Decimal(0)):
            errors.append("pending_close_exceeds_position")
        _, timestamp_error = as_utc(self.opened_at, "opened_at")
        if timestamp_error:
            errors.append(timestamp_error)
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "account_id": self.account_id,
            "pair": self.pair,
            "side": self.side_value,
            "quantity": self.quantity,
            "quantity_unit": self.quantity_unit,
            "base_units": self.base_units,
            "opened_at": self.opened_at,
            "pending_close_quantity": self.pending_close_quantity,
            "available_quantity": self.available_quantity,
        }


@dataclass(frozen=True)
class CloseIntent:
    """The exact open position, and how much of it, an order intends to close.

    This is the *only* way an order can be treated as risk-reducing.  It names a
    position by id and account, carries the quantity to close, and is matched
    against the ledger snapshot by the gate.  Order side is never used to infer
    a close: a SELL with no intent is a new short entry.
    """

    position_id: str
    account_id: str
    quantity: Decimal | int | float | str
    pair: str | None = None
    quantity_unit: str | None = None

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not isinstance(self.position_id, str) or not self.position_id.strip():
            errors.append("missing_close_position_id")
        if not isinstance(self.account_id, str) or not self.account_id.strip():
            errors.append("missing_close_account_id")
        quantity, quantity_error = parse_decimal(self.quantity, "close_quantity")
        if quantity_error or quantity is None or quantity <= 0:
            errors.append(quantity_error or "invalid_close_quantity")
        if self.pair is not None:
            _, pair_error = canonical_pair(self.pair)
            if pair_error:
                errors.append(pair_error)
        if self.quantity_unit is not None and (
            not isinstance(self.quantity_unit, str) or not self.quantity_unit.strip()
        ):
            errors.append("invalid_close_quantity_unit")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def quantity_decimal(self) -> Decimal:
        value, error = parse_decimal(self.quantity, "close_quantity")
        if error or value is None:
            raise ValueError(error or "invalid_close_quantity")
        return value

    @property
    def canonical_pair(self) -> str | None:
        pair, error = canonical_pair(self.pair) if self.pair is not None else (None, None)
        return None if error else pair

    def audit_view(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "account_id": self.account_id,
            "quantity": self.quantity,
            "pair": self.pair,
            "quantity_unit": self.quantity_unit,
        }


@dataclass(frozen=True)
class OrderRequest:
    request_id: str
    provider_id: str
    provider_symbol: str
    side: OrderSide | str
    quantity: Decimal | int | float | str
    target_mode: TradingMode | str
    evaluated_at: datetime
    quote: ForexQuote | None
    strategy_id: str
    #: Present only when the order closes (part of) a named open position.  The
    #: gate verifies it against the ledger; it can never be inferred from side.
    close_intent: CloseIntent | None = None

    @property
    def reduces_exposure(self) -> bool:
        """Derived, never settable: an order is a close only if it names one."""

        return self.close_intent is not None

    @property
    def side_value(self) -> str:
        return self.side.value if isinstance(self.side, OrderSide) else str(self.side).upper()

    @property
    def target_mode_value(self) -> str:
        return (
            self.target_mode.value
            if isinstance(self.target_mode, TradingMode)
            else str(self.target_mode).upper()
        )

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            errors.append("missing_request_id")
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            errors.append("missing_provider_id")
        if not isinstance(self.provider_symbol, str) or not self.provider_symbol.strip():
            errors.append("missing_provider_symbol")
        try:
            OrderSide(self.side_value)
        except ValueError:
            errors.append("invalid_order_side")
        quantity, quantity_error = parse_decimal(self.quantity, "quantity")
        if quantity_error or quantity is None or quantity <= 0:
            errors.append(quantity_error or "invalid_quantity")
        try:
            TradingMode(self.target_mode_value)
        except ValueError:
            errors.append("invalid_target_mode")
        _, timestamp_error = as_utc(self.evaluated_at, "evaluated_at")
        if timestamp_error:
            errors.append(timestamp_error)
        if self.quote is not None and not isinstance(self.quote, ForexQuote):
            errors.append("invalid_quote_type")
        if not isinstance(self.strategy_id, str) or not self.strategy_id.strip():
            errors.append("missing_strategy_id")
        if self.close_intent is not None:
            if not isinstance(self.close_intent, CloseIntent):
                errors.append("invalid_close_intent")
            else:
                errors.extend(
                    f"close_intent:{error}"
                    for error in self.close_intent.validate().errors
                )
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def quantity_decimal(self) -> Decimal:
        value, error = parse_decimal(self.quantity, "quantity")
        if error or value is None:
            raise ValueError(error or "invalid_quantity")
        return value

    def evaluated_at_utc(self) -> datetime:
        value, error = as_utc(self.evaluated_at, "evaluated_at")
        if error or value is None:
            raise ValueError(error or "invalid evaluated_at")
        return value

    def audit_view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "provider_id": self.provider_id,
            "provider_symbol": self.provider_symbol,
            "side": self.side_value,
            "quantity": self.quantity,
            "target_mode": self.target_mode_value,
            "evaluated_at": self.evaluated_at,
            "strategy_id": self.strategy_id,
            "reduces_exposure": self.reduces_exposure,
            "close_intent": (
                self.close_intent.audit_view()
                if isinstance(self.close_intent, CloseIntent)
                else None
            ),
        }


@dataclass(frozen=True)
class GateContext:
    settings: SafetySettings | None
    system_state: SystemState | None
    account: AccountProfile | None
    risk_limits: RiskLimits | None
    cost_model: CostModel | None
    rollover_policy: RolloverPolicy | None
    conversion_rates: ConversionRateBook | None
    quote_freshness: QuoteFreshness | None
    risk_context: RiskContext | None
    unresolved_orders: tuple[UnresolvedOrder, ...]
    #: Open positions exactly as the ledger reports them.  Close validation is
    #: performed against this snapshot, never against the request's own claims.
    open_positions: tuple[PositionSnapshot, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "settings": self.settings.signature() if self.settings else None,
            "system_state": self.system_state.audit_view() if self.system_state else None,
            "account": self.account.audit_view() if self.account else None,
            "risk_limits": self.risk_limits.audit_view() if self.risk_limits else None,
            "cost_model": self.cost_model.terms.audit_view()
            if self.cost_model is not None
            else None,
            "rollover_policy": self.rollover_policy.audit_view()
            if self.rollover_policy
            else None,
            "conversion_rates": self.conversion_rates.audit_view()
            if self.conversion_rates
            else None,
            "quote_freshness": self.quote_freshness.audit_view()
            if self.quote_freshness
            else None,
            "risk_context": self.risk_context.audit_view() if self.risk_context else None,
            "unresolved_orders": [order.audit_view() for order in self.unresolved_orders],
            "open_positions": [
                position.audit_view() for position in self.open_positions
            ],
        }


@dataclass(frozen=True)
class ApprovedOrderIntent:
    """A local, simulated intent. It is not a broker order or a fill."""

    request_id: str
    pair: str
    side: OrderSide
    quantity: Decimal
    quantity_unit: str
    base_units: Decimal
    executable_price: Decimal
    quote_currency: str
    account_currency: str
    evaluated_at: datetime
    strategy_id: str
    spec_version: str
    calendar_signature: str
    cost_model_version: str
    rollover_schedule_version: str
    risk_limits_version: str
    risk_calculation: "RiskCalculation"
    #: Set only for an approved close, and carried into the persisted receipt so
    #: settlement can prove it is closing the position the gate approved.
    close_position_id: str | None = None
    closed_base_units: Decimal = Decimal(0)
    exposure_reducing: bool = False

    def audit_view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "pair": self.pair,
            "side": self.side.value,
            "quantity": self.quantity,
            "quantity_unit": self.quantity_unit,
            "base_units": self.base_units,
            "executable_price": self.executable_price,
            "quote_currency": self.quote_currency,
            "account_currency": self.account_currency,
            "evaluated_at": self.evaluated_at,
            "strategy_id": self.strategy_id,
            "spec_version": self.spec_version,
            "calendar_signature": self.calendar_signature,
            "cost_model_version": self.cost_model_version,
            "rollover_schedule_version": self.rollover_schedule_version,
            "risk_limits_version": self.risk_limits_version,
            "close_position_id": self.close_position_id,
            "closed_base_units": self.closed_base_units,
            "exposure_reducing": self.exposure_reducing,
        }
