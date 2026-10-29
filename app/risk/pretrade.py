"""One strict, fail-closed pre-trade gate for M1.

Every evaluation writes a structured audit decision.  Any missing input,
invalid calculation, closed calendar, stale quote, unresolved order, or live
request produces a rejection with no approved order intent.  The gate never
calls a provider or broker and never fabricates a market value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from app.accounting.costs import CostModel
from app.accounting.fx import RateReason, RateResult
from app.accounting.rollover import RolloverPolicy
from app.audit.log import AuditLog, make_decision_id, sanitize_audit_value
from app.calendar.trading import CalendarState, TradingCalendar
from app.common.types import OrderSide, TradingMode
from app.config.settings import AccountProfile, RiskLimits, SafetySettings, SystemState
from app.common.validation import parse_decimal
from app.data.models import ForexQuote, FreshnessDecision, QuoteFreshness
from app.universe.specs import CurrencyPairSpec, InstrumentCatalog, InstrumentStatus

from .calculations import RiskCalculation, calculate_risk
from .models import (
    ApprovedOrderIntent,
    CloseIntent,
    GateContext,
    OrderRequest,
    PositionSnapshot,
    RiskContext,
    UnresolvedOrder,
)


class RejectionReason(str, Enum):
    ALLOWED = "ALLOWED"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_GATE_CONTEXT = "INVALID_GATE_CONTEXT"
    INVALID_SAFETY_SETTINGS = "INVALID_SAFETY_SETTINGS"
    LIVE_TRADING_FORBIDDEN = "LIVE_TRADING_FORBIDDEN"
    LIVE_MODE_REQUEST = "LIVE_MODE_REQUEST"
    INVALID_SYSTEM_STATE = "INVALID_SYSTEM_STATE"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    EXECUTION_PAUSED = "EXECUTION_PAUSED"
    INVALID_UNRESOLVED_ORDER = "INVALID_UNRESOLVED_ORDER"
    UNRESOLVED_ORDER_PRESENT = "UNRESOLVED_ORDER_PRESENT"
    MISSING_ACCOUNT_TERMS = "MISSING_ACCOUNT_TERMS"
    INVALID_ACCOUNT_TERMS = "INVALID_ACCOUNT_TERMS"
    MISSING_RISK_LIMITS = "MISSING_RISK_LIMITS"
    INVALID_RISK_LIMITS = "INVALID_RISK_LIMITS"
    MISSING_COST_MODEL = "MISSING_COST_MODEL"
    INVALID_COST_MODEL = "INVALID_COST_MODEL"
    MISSING_ROLLOVER_SCHEDULE = "MISSING_ROLLOVER_SCHEDULE"
    INVALID_ROLLOVER_SCHEDULE = "INVALID_ROLLOVER_SCHEDULE"
    MISSING_CONVERSION_RATES = "MISSING_CONVERSION_RATES"
    INVALID_CONVERSION_RATES = "INVALID_CONVERSION_RATES"
    MISSING_QUOTE_FRESHNESS = "MISSING_QUOTE_FRESHNESS"
    INVALID_QUOTE_FRESHNESS = "INVALID_QUOTE_FRESHNESS"
    MISSING_RISK_CONTEXT = "MISSING_RISK_CONTEXT"
    INVALID_RISK_CONTEXT = "INVALID_RISK_CONTEXT"
    INVALID_INSTRUMENT_CATALOG = "INVALID_INSTRUMENT_CATALOG"
    INVALID_CALENDAR = "INVALID_CALENDAR"
    CONVERSION_SOURCE_MISMATCH = "CONVERSION_SOURCE_MISMATCH"
    COST_MODEL_MISMATCH = "COST_MODEL_MISMATCH"
    ROLLOVER_SCHEDULE_MISMATCH = "ROLLOVER_SCHEDULE_MISMATCH"
    ROLLOVER_CALENDAR_MISMATCH = "ROLLOVER_CALENDAR_MISMATCH"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    INVALID_SPEC = "INVALID_SPEC"
    MISSING_SPEC = "MISSING_SPEC"
    INSTRUMENT_INACTIVE = "INSTRUMENT_INACTIVE"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    CALENDAR_MISMATCH = "CALENDAR_MISMATCH"
    MARKET_CLOSED = "MARKET_CLOSED"
    INCOMPLETE_QUOTE = "INCOMPLETE_QUOTE"
    INVALID_QUOTE = "INVALID_QUOTE"
    CROSSED_QUOTE = "CROSSED_QUOTE"
    STALE_QUOTE = "STALE_QUOTE"
    FUTURE_QUOTE = "FUTURE_QUOTE"
    FEED_LATENCY_EXCEEDED = "FEED_LATENCY_EXCEEDED"
    QUOTE_CLOCK_ERROR = "QUOTE_CLOCK_ERROR"
    MISSING_RECEIVED_AT = "MISSING_RECEIVED_AT"
    INVALID_SPREAD = "INVALID_SPREAD"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    MISSING_CONVERSION_RATE = "MISSING_CONVERSION_RATE"
    STALE_CONVERSION_RATE = "STALE_CONVERSION_RATE"
    FUTURE_CONVERSION_RATE = "FUTURE_CONVERSION_RATE"
    INVALID_COST_CALCULATION = "INVALID_COST_CALCULATION"
    INVALID_RISK_CALCULATION = "INVALID_RISK_CALCULATION"
    MAX_ORDER_NOTIONAL_EXCEEDED = "MAX_ORDER_NOTIONAL_EXCEEDED"
    MAX_GROSS_EXPOSURE_EXCEEDED = "MAX_GROSS_EXPOSURE_EXCEEDED"
    MAX_MARGIN_EXCEEDED = "MAX_MARGIN_EXCEEDED"
    MAX_LEVERAGE_EXCEEDED = "MAX_LEVERAGE_EXCEEDED"
    MAX_CURRENCY_EXPOSURE_EXCEEDED = "MAX_CURRENCY_EXPOSURE_EXCEEDED"
    MAX_CORRELATION_GROUP_EXPOSURE_EXCEEDED = "MAX_CORRELATION_GROUP_EXPOSURE_EXCEEDED"
    MAX_OPEN_POSITIONS_EXCEEDED = "MAX_OPEN_POSITIONS_EXCEEDED"
    MAX_DAILY_LOSS_EXCEEDED = "MAX_DAILY_LOSS_EXCEEDED"
    MAX_TRADES_PER_DAY_EXCEEDED = "MAX_TRADES_PER_DAY_EXCEEDED"
    INVALID_CLOSE_INTENT = "INVALID_CLOSE_INTENT"
    CLOSE_POSITION_NOT_FOUND = "CLOSE_POSITION_NOT_FOUND"
    CLOSE_ACCOUNT_MISMATCH = "CLOSE_ACCOUNT_MISMATCH"
    CLOSE_PAIR_MISMATCH = "CLOSE_PAIR_MISMATCH"
    CLOSE_SIDE_MISMATCH = "CLOSE_SIDE_MISMATCH"
    CLOSE_QUANTITY_UNIT_MISMATCH = "CLOSE_QUANTITY_UNIT_MISMATCH"
    CLOSE_QUANTITY_EXCEEDS_POSITION = "CLOSE_QUANTITY_EXCEEDS_POSITION"
    CLOSE_INCREASES_EXPOSURE = "CLOSE_INCREASES_EXPOSURE"
    GATE_INTERNAL_ERROR = "GATE_INTERNAL_ERROR"


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    code: RejectionReason
    reason: str
    details: Mapping[str, Any]
    audit_event: Mapping[str, Any]
    order_intent: ApprovedOrderIntent | None = None
    risk_calculation: RiskCalculation | None = None
    calendar_state: CalendarState | None = None
    freshness: FreshnessDecision | None = None
    quote_rate: RateResult | None = None
    base_rate: RateResult | None = None


@dataclass(frozen=True)
class PreTradeGate:
    catalog: InstrumentCatalog
    calendar: TradingCalendar
    audit_log: AuditLog

    def evaluate(self, request: OrderRequest, context: GateContext) -> GateDecision:
        try:
            return self._evaluate(request, context)
        except Exception as exc:  # fail closed; only the exception class is logged
            return self._decide(
                request,
                context,
                RejectionReason.GATE_INTERNAL_ERROR,
                reason="gate_internal_error",
                details={"error_type": type(exc).__name__},
            )

    @staticmethod
    def _safe_view(obj: Any, method: str) -> Any:
        try:
            return getattr(obj, method)()
        except Exception as exc:
            return {"error_type": type(exc).__name__}

    def _decide(
        self,
        request: OrderRequest | None,
        context: GateContext | None,
        code: RejectionReason,
        *,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        allowed: bool = False,
        order_intent: ApprovedOrderIntent | None = None,
        spec: CurrencyPairSpec | None = None,
        calendar_state: CalendarState | None = None,
        quote: ForexQuote | None = None,
        freshness: FreshnessDecision | None = None,
        quote_rate: RateResult | None = None,
        base_rate: RateResult | None = None,
        risk_calculation: RiskCalculation | None = None,
    ) -> GateDecision:
        detail_dict = dict(details or {})
        message = reason or code.value
        request_view = (
            self._safe_view(request, "audit_view") if request is not None else None
        )
        context_view = (
            self._safe_view(context, "audit_view") if context is not None else None
        )
        evaluated_at = None
        if request is not None:
            try:
                evaluated_at = request.evaluated_at_utc()
            except Exception:
                evaluated_at = None
        decision_id = make_decision_id(
            getattr(request, "request_id", "unknown"),
            evaluated_at.isoformat() if evaluated_at else "unknown",
            code.value,
            message,
            str(sanitize_audit_value(detail_dict)),
        )
        event: dict[str, Any] = {
            "event_type": "PRE_TRADE_DECISION",
            "decision_id": decision_id,
            "allowed": allowed,
            "code": code.value,
            "reason": message,
            "evaluated_at": evaluated_at,
            "details": detail_dict,
            "request": request_view,
            "context": context_view,
            "catalog": self._safe_view(self.catalog, "audit_view")
            if self.catalog is not None
            else None,
            "calendar": {
                "view": self._safe_view(self.calendar, "audit_view")
                if hasattr(self.calendar, "audit_view")
                else {
                    "calendar_id": getattr(self.calendar, "calendar_id", None),
                    "version": getattr(self.calendar, "version", None),
                    "source": getattr(self.calendar, "source", None),
                },
                "signature": self._safe_view(self.calendar, "signature"),
            }
            if self.calendar is not None
            else None,
            "spec": self._safe_view(spec, "audit_view") if spec is not None else None,
            "calendar_state": self._safe_view(calendar_state, "audit_view")
            if calendar_state is not None
            else None,
            "quote": self._safe_view(quote, "audit_view") if quote is not None else None,
            "freshness": self._safe_view(freshness, "audit_view")
            if freshness is not None
            else None,
            "quote_rate": self._safe_view(quote_rate, "audit_view")
            if quote_rate is not None
            else None,
            "base_rate": self._safe_view(base_rate, "audit_view")
            if base_rate is not None
            else None,
            "risk_calculation": self._safe_view(risk_calculation, "audit_view")
            if risk_calculation is not None
            else None,
            "order_intent": self._safe_view(order_intent, "audit_view")
            if order_intent is not None
            else None,
        }
        try:
            audit_event = self.audit_log.record(event)
        except Exception as exc:
            audit_event = sanitize_audit_value(event)
            if isinstance(audit_event, dict):
                audit_event["audit_error"] = type(exc).__name__
            else:  # pragma: no cover - defensive
                audit_event = {"audit_error": type(exc).__name__}
        return GateDecision(
            allowed=allowed,
            code=code,
            reason=message,
            details=detail_dict,
            audit_event=audit_event,
            order_intent=order_intent,
            risk_calculation=risk_calculation,
            calendar_state=calendar_state,
            freshness=freshness,
            quote_rate=quote_rate,
            base_rate=base_rate,
        )

    def _reject(
        self,
        request: OrderRequest | None,
        context: GateContext | None,
        code: RejectionReason,
        *,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> GateDecision:
        return self._decide(
            request,
            context,
            code,
            reason=reason,
            details=details,
            allowed=False,
            **kwargs,
        )

    @staticmethod
    def _rate_code(result: RateResult, label: str) -> tuple[RejectionReason, dict[str, Any]]:
        details = {"rate": label, "rate_reason": result.reason.value}
        if result.reason == RateReason.MISSING_RATE:
            return RejectionReason.MISSING_CONVERSION_RATE, details
        if result.reason == RateReason.STALE_RATE:
            return RejectionReason.STALE_CONVERSION_RATE, details
        if result.reason == RateReason.FUTURE_RATE:
            return RejectionReason.FUTURE_CONVERSION_RATE, details
        return RejectionReason.INVALID_CONVERSION_RATES, details

    def _resolve_close_target(
        self,
        request: OrderRequest,
        context: GateContext,
        *,
        spec: CurrencyPairSpec,
        account: AccountProfile,
        quantity: Decimal,
    ) -> tuple[PositionSnapshot | None, RejectionReason | None, dict[str, Any]]:
        """Match a declared close intent against the ledger, or refuse it.

        A close is never inferred from side.  When an intent is present it must
        name an open position that this account owns, on the same pair, in the
        opposite direction, in the same quantity unit, and no larger than what
        is left after other in-flight closes.  Each failure has its own code so
        a forged or stale close can never be mistaken for a genuine reduction.
        """

        intent = request.close_intent
        if intent is None:
            return None, None, {}
        if not isinstance(intent, CloseIntent):
            return None, RejectionReason.INVALID_CLOSE_INTENT, {}
        intent_validation = intent.validate()
        if not intent_validation.valid:
            return (
                None,
                RejectionReason.INVALID_CLOSE_INTENT,
                {"errors": intent_validation.errors},
            )

        open_positions = context.open_positions
        if open_positions is None:
            return None, RejectionReason.INVALID_GATE_CONTEXT, {}
        for position in open_positions:
            if not isinstance(position, PositionSnapshot) or not position.validate().valid:
                return (
                    None,
                    RejectionReason.INVALID_GATE_CONTEXT,
                    {"position_id": getattr(position, "position_id", None)},
                )
        target = next(
            (
                position
                for position in open_positions
                if position.position_id == intent.position_id
            ),
            None,
        )
        if target is None:
            return (
                None,
                RejectionReason.CLOSE_POSITION_NOT_FOUND,
                {"position_id": intent.position_id},
            )
        if (
            intent.account_id != account.account_id
            or target.account_id != account.account_id
        ):
            # Either the caller names a position owned by another account, or
            # the snapshot itself belongs to one.
            return (
                None,
                RejectionReason.CLOSE_ACCOUNT_MISMATCH,
                {
                    "intent_account_id": intent.account_id,
                    "position_account_id": target.account_id,
                },
            )
        canonical = spec.canonical_pair
        if target.pair != canonical or (
            intent.canonical_pair is not None and intent.canonical_pair != canonical
        ):
            return (
                None,
                RejectionReason.CLOSE_PAIR_MISMATCH,
                {
                    "position_pair": target.pair,
                    "intent_pair": intent.canonical_pair,
                    "request_pair": canonical,
                },
            )
        position_side = target.side_value
        if request.side_value == position_side:
            # Same direction as the position it claims to close: this adds
            # exposure (or reverses it) instead of reducing it.
            return (
                None,
                RejectionReason.CLOSE_SIDE_MISMATCH,
                {"position_side": position_side, "request_side": request.side_value},
            )
        if (
            intent.quantity_unit is not None
            and intent.quantity_unit != target.quantity_unit
        ) or target.quantity_unit != spec.quantity_unit_value:
            return (
                None,
                RejectionReason.CLOSE_QUANTITY_UNIT_MISMATCH,
                {
                    "position_quantity_unit": target.quantity_unit,
                    "intent_quantity_unit": intent.quantity_unit,
                    "spec_quantity_unit": spec.quantity_unit_value,
                },
            )
        declared, declared_error = parse_decimal(intent.quantity, "close_quantity")
        if declared_error or declared is None or declared != quantity:
            # The intent and the order must describe the same close; a mismatch
            # means one of them is not what the gate validated.
            return (
                None,
                RejectionReason.INVALID_CLOSE_INTENT,
                {"intent_quantity": intent.quantity, "request_quantity": quantity},
            )
        available = target.available_quantity
        if declared > available:
            return (
                None,
                RejectionReason.CLOSE_QUANTITY_EXCEEDS_POSITION,
                {
                    "requested_quantity": declared,
                    "available_quantity": available,
                    "position_quantity": target.quantity,
                    "pending_close_quantity": target.pending_close_quantity,
                },
            )
        return target, None, {}

    def _evaluate(self, request: OrderRequest, context: GateContext) -> GateDecision:
        if not isinstance(request, OrderRequest):
            return self._reject(
                None, context, RejectionReason.INVALID_REQUEST,
                reason="invalid_request_object",
            )
        if not isinstance(context, GateContext):
            return self._reject(
                request, None, RejectionReason.INVALID_GATE_CONTEXT,
                reason="invalid_gate_context",
            )
        request_validation = request.validate()
        if not request_validation.valid:
            # A malformed close intent is reported as such rather than as a
            # generic invalid request, so the operator can see that the *close*
            # is what cannot be trusted.
            close_errors = tuple(
                error
                for error in request_validation.errors
                if error.startswith("close_intent:") or error == "invalid_close_intent"
            )
            code = (
                RejectionReason.INVALID_CLOSE_INTENT
                if close_errors and len(close_errors) == len(request_validation.errors)
                else RejectionReason.INVALID_REQUEST
            )
            return self._reject(
                request, context, code,
                details={"errors": request_validation.errors},
            )

        settings: SafetySettings | None = context.settings
        if settings is None:
            return self._reject(request, context, RejectionReason.INVALID_SAFETY_SETTINGS)
        settings_validation = settings.validate()
        if not settings_validation.valid:
            code = (
                RejectionReason.LIVE_TRADING_FORBIDDEN
                if any("live" in error for error in settings_validation.errors)
                else RejectionReason.INVALID_SAFETY_SETTINGS
            )
            return self._reject(
                request, context, code,
                details={"errors": settings_validation.errors},
            )
        if request.target_mode_value == TradingMode.LIVE.value:
            return self._reject(request, context, RejectionReason.LIVE_MODE_REQUEST)

        system_state: SystemState | None = context.system_state
        if system_state is None or not system_state.validate().valid:
            return self._reject(request, context, RejectionReason.INVALID_SYSTEM_STATE)
        if system_state.emergency_stop:
            return self._reject(request, context, RejectionReason.EMERGENCY_STOP)
        if system_state.kill_switch_active:
            return self._reject(request, context, RejectionReason.KILL_SWITCH_ACTIVE)
        if system_state.execution_paused:
            return self._reject(request, context, RejectionReason.EXECUTION_PAUSED)

        unresolved_orders = context.unresolved_orders
        if unresolved_orders is None:
            return self._reject(request, context, RejectionReason.INVALID_GATE_CONTEXT)
        for order in unresolved_orders:
            if not isinstance(order, UnresolvedOrder) or not order.validate().valid:
                return self._reject(
                    request, context, RejectionReason.INVALID_UNRESOLVED_ORDER
                )
            if order.is_unresolved:
                return self._reject(
                    request, context, RejectionReason.UNRESOLVED_ORDER_PRESENT,
                    details={"order_id": order.order_id, "state": order.state},
                )

        required = (
            (context.account, RejectionReason.MISSING_ACCOUNT_TERMS),
            (context.risk_limits, RejectionReason.MISSING_RISK_LIMITS),
            (context.cost_model, RejectionReason.MISSING_COST_MODEL),
            (context.rollover_policy, RejectionReason.MISSING_ROLLOVER_SCHEDULE),
            (context.conversion_rates, RejectionReason.MISSING_CONVERSION_RATES),
            (context.quote_freshness, RejectionReason.MISSING_QUOTE_FRESHNESS),
            (context.risk_context, RejectionReason.MISSING_RISK_CONTEXT),
        )
        for value, code in required:
            if value is None:
                return self._reject(request, context, code)

        account: AccountProfile = context.account  # type: ignore[assignment]
        risk_limits: RiskLimits = context.risk_limits  # type: ignore[assignment]
        cost_model: CostModel = context.cost_model  # type: ignore[assignment]
        rollover_policy: RolloverPolicy = context.rollover_policy  # type: ignore[assignment]
        quote_freshness: QuoteFreshness = context.quote_freshness  # type: ignore[assignment]
        risk_context: RiskContext = context.risk_context  # type: ignore[assignment]

        validations = (
            (account.validate(), RejectionReason.INVALID_ACCOUNT_TERMS),
            (risk_limits.validate(), RejectionReason.INVALID_RISK_LIMITS),
            (cost_model.validate(), RejectionReason.INVALID_COST_MODEL),
            (rollover_policy.validate(), RejectionReason.INVALID_ROLLOVER_SCHEDULE),
            (quote_freshness.validate(), RejectionReason.INVALID_QUOTE_FRESHNESS),
            (risk_context.validate(), RejectionReason.INVALID_RISK_CONTEXT),
            (
                self.catalog.validate() if self.catalog is not None else None,
                RejectionReason.INVALID_INSTRUMENT_CATALOG,
            ),
            (
                self.calendar.validate() if self.calendar is not None else None,
                RejectionReason.INVALID_CALENDAR,
            ),
        )
        for result, code in validations:
            if result is None or not result.valid:
                return self._reject(
                    request, context, code,
                    details={"errors": result.errors if result else ()},
                )

        conversion_rates = context.conversion_rates
        assert conversion_rates is not None
        terms = getattr(cost_model, "terms", None)
        if terms is None:
            return self._reject(request, context, RejectionReason.MISSING_COST_MODEL)
        if (
            account.cost_model_id != terms.model_id
            or account.cost_model_version != terms.version
        ):
            return self._reject(request, context, RejectionReason.COST_MODEL_MISMATCH)
        if (
            account.rollover_schedule_id != rollover_policy.schedule_id
            or account.rollover_schedule_version != rollover_policy.version
        ):
            return self._reject(
                request, context, RejectionReason.ROLLOVER_SCHEDULE_MISMATCH
            )
        if account.conversion_source != conversion_rates.source:
            return self._reject(
                request, context, RejectionReason.CONVERSION_SOURCE_MISMATCH
            )
        calendar_id = getattr(self.calendar, "calendar_id", "")
        calendar_version = getattr(self.calendar, "version", "")
        if rollover_policy.calendar_id != calendar_id:
            return self._reject(
                request, context, RejectionReason.ROLLOVER_CALENDAR_MISMATCH
            )

        spec = self.catalog.resolve(request.provider_id, request.provider_symbol)
        if spec is None:
            return self._reject(
                request, context, RejectionReason.UNKNOWN_INSTRUMENT,
                details={
                    "provider_id": request.provider_id,
                    "provider_symbol": request.provider_symbol,
                },
            )
        if not spec.spec_version or not spec.source:
            return self._reject(
                request, context, RejectionReason.MISSING_SPEC, spec=spec
            )
        spec_validation = spec.validate()
        if not spec_validation.valid:
            return self._reject(
                request, context, RejectionReason.INVALID_SPEC,
                spec=spec, details={"errors": spec_validation.errors},
            )
        if InstrumentStatus(spec.status_value) != InstrumentStatus.ACTIVE:
            return self._reject(
                request, context, RejectionReason.INSTRUMENT_INACTIVE, spec=spec
            )
        quantity, quantity_validation = spec.parse_quantity(request.quantity)
        if quantity is None or not quantity_validation.valid:
            return self._reject(
                request, context, RejectionReason.INVALID_QUANTITY,
                spec=spec, details={"errors": quantity_validation.errors},
            )

        if (
            spec.trading_calendar_id != calendar_id
            or spec.trading_calendar_version != calendar_version
        ):
            return self._reject(
                request, context, RejectionReason.CALENDAR_MISMATCH, spec=spec
            )
        evaluated_at = request.evaluated_at_utc()
        calendar_state = self.calendar.status(spec.canonical_pair or "", evaluated_at)
        if not isinstance(calendar_state, CalendarState) or not calendar_state.is_open:
            return self._reject(
                request, context, RejectionReason.MARKET_CLOSED,
                spec=spec, calendar_state=calendar_state,
                details={
                    "market_status": calendar_state.status.value
                    if isinstance(calendar_state, CalendarState)
                    else "UNKNOWN",
                    "calendar_reason": calendar_state.reason
                    if isinstance(calendar_state, CalendarState)
                    else "invalid_calendar_state",
                },
            )

        quote = request.quote
        if quote is None:
            return self._reject(
                request, context, RejectionReason.INCOMPLETE_QUOTE, spec=spec,
                calendar_state=calendar_state,
            )
        quote_validation = quote.validate(spec)
        if not quote_validation.valid:
            if any(
                error in {"missing_bid", "missing_ask"}
                for error in quote_validation.errors
            ):
                code = RejectionReason.INCOMPLETE_QUOTE
            elif "crossed_quote" in quote_validation.errors:
                code = RejectionReason.CROSSED_QUOTE
            else:
                code = RejectionReason.INVALID_QUOTE
            return self._reject(
                request, context, code,
                spec=spec, calendar_state=calendar_state, quote=quote,
                details={"errors": quote_validation.errors},
            )

        freshness = quote_freshness.check(quote, evaluated_at)
        if not freshness.ok:
            freshness_codes = {
                "STALE_QUOTE": RejectionReason.STALE_QUOTE,
                "FUTURE_QUOTE": RejectionReason.FUTURE_QUOTE,
                "FEED_LATENCY_EXCEEDED": RejectionReason.FEED_LATENCY_EXCEEDED,
                "QUOTE_CLOCK_ERROR": RejectionReason.QUOTE_CLOCK_ERROR,
                "MISSING_RECEIVED_AT": RejectionReason.MISSING_RECEIVED_AT,
            }
            code = freshness_codes.get(
                freshness.reason.value, RejectionReason.INVALID_QUOTE_FRESHNESS
            )
            return self._reject(
                request, context, code,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness,
                details={"freshness_reason": freshness.reason.value},
            )

        try:
            spread_pips = quote.spread_pips(spec)
        except Exception as exc:
            return self._reject(
                request, context, RejectionReason.INVALID_SPREAD,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness,
                details={"error_type": type(exc).__name__},
            )
        if not spread_pips.is_finite() or spread_pips < 0:
            return self._reject(
                request, context, RejectionReason.INVALID_SPREAD,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness,
            )
        if spread_pips > risk_limits.decimal("max_spread_pips"):
            return self._reject(
                request, context, RejectionReason.SPREAD_TOO_WIDE,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness,
                details={"spread_pips": spread_pips},
            )

        quote_rate = conversion_rates.get(
            spec.quote_currency, account.account_currency_code, evaluated_at
        )
        base_rate = conversion_rates.get(
            spec.base_currency, account.account_currency_code, evaluated_at
        )
        for label, rate_result in (("quote_to_account", quote_rate), ("base_to_account", base_rate)):
            if not rate_result.ok:
                code, details = self._rate_code(rate_result, label)
                return self._reject(
                    request, context, code,
                    spec=spec, calendar_state=calendar_state, quote=quote,
                    freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                    details=details,
                )
            if (
                rate_result.reason != RateReason.IDENTITY
                and rate_result.source != account.conversion_source
            ):
                return self._reject(
                    request, context, RejectionReason.CONVERSION_SOURCE_MISMATCH,
                    spec=spec, calendar_state=calendar_state, quote=quote,
                    freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                    details={"rate": label},
                )
        if quote_rate.rate is None or base_rate.rate is None:
            return self._reject(
                request, context, RejectionReason.INVALID_CONVERSION_RATES,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
            )

        close_target, close_code, close_details = self._resolve_close_target(
            request,
            context,
            spec=spec,
            account=account,
            quantity=quantity,
        )
        if close_code is not None:
            # A close that cannot be matched to a live ledger position is not a
            # close.  Refusing here -- after every instrument, calendar, quote,
            # freshness, spread, and conversion check above has already run --
            # keeps the whole unrelated safety chain in force for closes.
            return self._reject(
                request, context, close_code,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                details=close_details,
            )

        try:
            executable_price = quote.executable_price(request.side_value)
            calculation = calculate_risk(
                side=request.side_value,
                quantity=quantity,
                spec=spec,
                executable_price=executable_price,
                quote_to_account_rate=quote_rate.rate,
                base_to_account_rate=base_rate.rate,
                account=account,
                risk_context=risk_context,
                cost_model=cost_model,
                close_position=close_target,
            )
        except Exception as exc:
            # A close that cannot be projected as a reduction is refused as an
            # exposure increase, not folded into a generic calculation error.
            code = RejectionReason.INVALID_RISK_CALCULATION
            if close_target is not None and str(exc) in {
                "close_exceeds_gross_exposure",
                "close_exceeds_position",
                "close_side_does_not_oppose_position",
            }:
                code = RejectionReason.CLOSE_INCREASES_EXPOSURE
            return self._reject(
                request, context, code,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                details={"error_type": type(exc).__name__, "reason": str(exc)},
            )
        cost_validation = calculation.estimated_costs.validate()
        if not cost_validation.valid:
            return self._reject(
                request, context, RejectionReason.INVALID_COST_CALCULATION,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                risk_calculation=calculation,
                details={"errors": cost_validation.errors},
            )
        calculation_validation = calculation.validate()
        if not calculation_validation.valid:
            return self._reject(
                request, context, RejectionReason.INVALID_RISK_CALCULATION,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                risk_calculation=calculation,
                details={"errors": calculation_validation.errors},
            )

        leverage = account.leverage_decimal
        if calculation.exposure_reducing != (close_target is not None):
            # The projection must describe the same thing the gate approved.
            return self._reject(
                request, context, RejectionReason.INVALID_RISK_CALCULATION,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                risk_calculation=calculation,
            )
        if leverage > risk_limits.decimal("max_leverage"):
            return self._reject(
                request, context, RejectionReason.MAX_LEVERAGE_EXCEEDED,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                risk_calculation=calculation,
            )
        limit_checks = (
            (
                calculation.required_margin_account,
                "max_margin_account",
                RejectionReason.MAX_MARGIN_EXCEEDED,
            ),
            (
                calculation.notional_account,
                "max_order_notional_account",
                RejectionReason.MAX_ORDER_NOTIONAL_EXCEEDED,
            ),
            (
                calculation.projected_gross_exposure_account,
                "max_gross_exposure_account",
                RejectionReason.MAX_GROSS_EXPOSURE_EXCEEDED,
            ),
            (
                calculation.max_abs_currency_exposure_account,
                "max_abs_currency_exposure_account",
                RejectionReason.MAX_CURRENCY_EXPOSURE_EXCEEDED,
            ),
            (
                abs(calculation.projected_correlation_group_exposure_account),
                "max_correlation_group_exposure_account",
                RejectionReason.MAX_CORRELATION_GROUP_EXPOSURE_EXCEEDED,
            ),
        )
        if close_target is not None:
            # A close is judged on what it does to the book, not on absolute
            # entry caps: it must strictly reduce gross exposure, add no margin,
            # and never exceed the position it names.  An entry cap could
            # otherwise block the very order that brings an oversized book back
            # in line.  The projection itself is built from the ledger position
            # and the quantity actually being closed, so a close that would add
            # exposure (an oversized or reversed close) cannot reach this point.
            current_gross = risk_context.decimal("gross_exposure_account")
            if (
                calculation.projected_gross_exposure_account >= current_gross
                or calculation.required_margin_account != Decimal(0)
                or calculation.closed_base_units <= 0
                or calculation.close_position_id != close_target.position_id
            ):
                return self._reject(
                    request, context, RejectionReason.CLOSE_INCREASES_EXPOSURE,
                    spec=spec, calendar_state=calendar_state, quote=quote,
                    freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                    risk_calculation=calculation,
                    details={
                        "current_gross_exposure_account": current_gross,
                        "projected_gross_exposure_account": (
                            calculation.projected_gross_exposure_account
                        ),
                        "required_margin_account": calculation.required_margin_account,
                        "close_position_id": calculation.close_position_id,
                        "closed_base_units": calculation.closed_base_units,
                    },
                )
        if close_target is None:
            for value, limit_name, code in limit_checks:
                if value > risk_limits.decimal(limit_name):
                    return self._reject(
                        request, context, code,
                        spec=spec, calendar_state=calendar_state, quote=quote,
                        freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                        risk_calculation=calculation,
                        details={"value": value, "limit": risk_limits.decimal(limit_name)},
                    )
        if close_target is None and (
            calculation.effective_leverage > leverage
            or calculation.effective_leverage > risk_limits.decimal("max_leverage")
        ):
            return self._reject(
                request, context, RejectionReason.MAX_LEVERAGE_EXCEEDED,
                spec=spec, calendar_state=calendar_state, quote=quote,
                freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                risk_calculation=calculation,
                details={"effective_leverage": calculation.effective_leverage},
            )
        if close_target is None:
            # Entry limits (open positions, daily loss, trades per day) apply to
            # every order that adds exposure, whichever side it is on: a SELL
            # with no intent opens a short and is an entry just as a BUY is.
            # A verified close stays exempt so risk can always be cut.
            if risk_context.open_positions >= risk_limits.max_open_positions:
                return self._reject(
                    request, context, RejectionReason.MAX_OPEN_POSITIONS_EXCEEDED,
                    spec=spec, calendar_state=calendar_state, quote=quote,
                    freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                    risk_calculation=calculation,
                )
            if risk_context.decimal("realized_pnl_today_account") <= -risk_limits.decimal(
                "max_daily_loss_account"
            ):
                return self._reject(
                    request, context, RejectionReason.MAX_DAILY_LOSS_EXCEEDED,
                    spec=spec, calendar_state=calendar_state, quote=quote,
                    freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                    risk_calculation=calculation,
                )
            if risk_context.trades_today >= risk_limits.max_trades_per_day:
                return self._reject(
                    request, context, RejectionReason.MAX_TRADES_PER_DAY_EXCEEDED,
                    spec=spec, calendar_state=calendar_state, quote=quote,
                    freshness=freshness, quote_rate=quote_rate, base_rate=base_rate,
                    risk_calculation=calculation,
                )

        order_intent = ApprovedOrderIntent(
            request_id=request.request_id,
            pair=spec.canonical_pair or "",
            side=OrderSide(request.side_value),
            quantity=quantity,
            quantity_unit=spec.quantity_unit_value,
            base_units=calculation.base_units,
            executable_price=executable_price,
            quote_currency=spec.quote_currency,
            account_currency=account.account_currency_code,
            evaluated_at=evaluated_at,
            strategy_id=request.strategy_id,
            spec_version=spec.spec_version,
            calendar_signature=self.calendar.signature(),
            cost_model_version=terms.version,
            rollover_schedule_version=rollover_policy.version,
            risk_limits_version=risk_limits.limits_version,
            risk_calculation=calculation,
            close_position_id=(
                close_target.position_id if close_target is not None else None
            ),
            closed_base_units=(
                calculation.closed_base_units if close_target is not None else Decimal(0)
            ),
            exposure_reducing=close_target is not None,
        )
        return self._decide(
            request,
            context,
            RejectionReason.ALLOWED,
            reason="pre_trade_checks_passed",
            allowed=True,
            order_intent=order_intent,
            spec=spec,
            calendar_state=calendar_state,
            quote=quote,
            freshness=freshness,
            quote_rate=quote_rate,
            base_rate=base_rate,
            risk_calculation=calculation,
            details={
                "pair": spec.canonical_pair,
                "spread_pips": spread_pips,
                "executable_price": executable_price,
                "market_status": calendar_state.status.value,
                "exposure_reducing": close_target is not None,
                "close_position_id": (
                    close_target.position_id if close_target is not None else None
                ),
                "closed_base_units": calculation.closed_base_units,
                "margin_released_account": calculation.margin_released_account,
                "projected_gross_exposure_account": (
                    calculation.projected_gross_exposure_account
                ),
                "side": request.side_value,
            },
        )
