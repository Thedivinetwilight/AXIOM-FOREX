"""Provider-neutral broker capabilities and a local simulated adapter.

M1 creates a simulated order *intent receipt* after the pre-trade gate allows
it.  It does not place, route, fill, cancel, or reconcile an order.  There is
no network client, credential, account endpoint, or live-mode capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.audit.log import AuditLog, make_decision_id
from app.common.types import ExecutionMode, TradingMode
from app.common.validation import ValidationResult
from app.risk.models import GateContext, OrderRequest
from app.risk.pretrade import GateDecision, PreTradeGate


class TradingBlocked(Exception):
    """Raised when a simulated intent cannot be created safely."""

    def __init__(self, reason: str, decision: GateDecision | None = None):
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


@dataclass(frozen=True)
class BrokerCapabilities:
    broker_id: str
    capability_version: str
    execution_mode: ExecutionMode | str
    supports_live_orders: bool
    supports_partial_fills: bool
    order_types: frozenset[str]
    calendar_id: str

    @property
    def execution_mode_value(self) -> str:
        return (
            self.execution_mode.value
            if isinstance(self.execution_mode, ExecutionMode)
            else str(self.execution_mode).upper()
        )

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not isinstance(self.broker_id, str) or not self.broker_id.strip():
            errors.append("missing_broker_id")
        if not isinstance(self.capability_version, str) or not self.capability_version.strip():
            errors.append("missing_capability_version")
        try:
            if ExecutionMode(self.execution_mode_value) != ExecutionMode.LOCAL_PAPER:
                errors.append("unsupported_execution_mode")
        except ValueError:
            errors.append("unsupported_execution_mode")
        if not isinstance(self.supports_live_orders, bool) or self.supports_live_orders:
            errors.append("live_orders_not_supported")
        if not isinstance(self.supports_partial_fills, bool):
            errors.append("invalid_partial_fill_capability")
        if not isinstance(self.order_types, frozenset) or not self.order_types:
            errors.append("missing_order_types")
        elif "SIMULATED_INTENT" not in self.order_types:
            errors.append("simulated_intent_capability_required")
        if not isinstance(self.calendar_id, str) or not self.calendar_id.strip():
            errors.append("missing_calendar_id")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "broker_id": self.broker_id,
            "capability_version": self.capability_version,
            "execution_mode": self.execution_mode_value,
            "supports_live_orders": self.supports_live_orders,
            "supports_partial_fills": self.supports_partial_fills,
            "order_types": sorted(self.order_types),
            "calendar_id": self.calendar_id,
        }


@dataclass(frozen=True)
class PaperOrderReceipt:
    simulation_id: str
    status: str
    live: bool
    mode: str
    created_at: datetime
    intent: Any
    decision_id: str
    capabilities: BrokerCapabilities

    def audit_view(self) -> dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "status": self.status,
            "live": self.live,
            "mode": self.mode,
            "created_at": self.created_at,
            "decision_id": self.decision_id,
            "intent": self.intent.audit_view()
            if hasattr(self.intent, "audit_view")
            else None,
            "capabilities": self.capabilities.audit_view(),
        }


@runtime_checkable
class BrokerAdapter(Protocol):
    capabilities: BrokerCapabilities

    def simulate_order_intent(
        self, request: OrderRequest, context: GateContext
    ) -> PaperOrderReceipt: ...


@dataclass(frozen=True)
class LocalPaperBroker:
    gate: PreTradeGate
    capabilities: BrokerCapabilities
    audit_log: AuditLog

    def __post_init__(self) -> None:
        result = self.capabilities.validate()
        if not result.valid:
            raise ValueError("Invalid local paper capabilities: " + result.reason)

    def simulate_order_intent(
        self, request: OrderRequest, context: GateContext
    ) -> PaperOrderReceipt:
        if (
            getattr(request, "target_mode_value", "") == TradingMode.LIVE.value
            or self.capabilities.supports_live_orders
        ):
            self.audit_log.record(
                {
                    "event_type": "LOCAL_PAPER_LIVE_REQUEST_BLOCKED",
                    "allowed": False,
                    "live": False,
                    "reason": "live_trading_forbidden",
                    "request": request.audit_view()
                    if hasattr(request, "audit_view")
                    else None,
                }
            )
            raise TradingBlocked("live_trading_forbidden")
        decision = self.gate.evaluate(request, context)
        if not decision.allowed or decision.order_intent is None:
            raise TradingBlocked(decision.code.value, decision)
        intent = decision.order_intent
        simulation_id = make_decision_id(
            "local-paper-intent",
            request.request_id,
            intent.evaluated_at.isoformat(),
            intent.pair,
            intent.side.value,
            str(intent.quantity),
        )
        receipt = PaperOrderReceipt(
            simulation_id=simulation_id,
            status="SIMULATED_INTENT",
            live=False,
            mode=TradingMode.PAPER.value,
            created_at=intent.evaluated_at,
            intent=intent,
            decision_id=str(decision.audit_event.get("decision_id", "")),
            capabilities=self.capabilities,
        )
        self.audit_log.record(
            {
                "event_type": "LOCAL_PAPER_INTENT_SIMULATED",
                "decision_id": receipt.decision_id,
                "simulation_id": simulation_id,
                "allowed": True,
                "live": False,
                "receipt": receipt.audit_view(),
            }
        )
        return receipt
