"""External broker boundary: now backed by the official MetaTrader 5 adapter.

The boundary keeps its original meaning: nothing external may be reached from
the paper path, and a caller that only has an :class:`OrderRequest` can never
place a live order.  What changed is what sits behind it:

* without an attached adapter the boundary is disabled and raises
  ``external_broker_boundary_disabled`` exactly as before (paper-only runtime
  checks and health checks stay green);
* with an attached :class:`MT5TradingAdapter` the boundary can report live
  status, but a live order still has to go through
  :meth:`MT5TradingAdapter.submit` with an allowed gate decision and an approved,
  active strategy.  ``simulate_order_intent`` never becomes a live submission.

``CONFORMANCE_REQUIREMENTS`` keeps the original ten broker-agnostic items and
now also lists the MT5-specific ones, because both must hold for this broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.execution.broker import (
    BrokerCapabilities,
    PaperOrderReceipt,
    TradingBlocked,
)
from app.risk.models import GateContext, OrderRequest

from .mt5.conformance import MT5_CONFORMANCE_REQUIREMENTS

GENERAL_CONFORMANCE_REQUIREMENTS = (
    "explicit broker identity and versioned capabilities",
    "provider symbol/canonical pair alias conformance",
    "DST-aware session, holiday, break, and weekend-gap replay",
    "bid/ask quote semantics, freshness, corrections, and historical completeness",
    "commission, slippage, financing, and triple-rollover verification",
    "margin, leverage, account-currency, and pip-value verification",
    "order lifecycle, partial fills, rejections, and recovery replay",
    "passing offline provider/broker conformance suite before integration",
    "completed provider profile onboarding with provenance and effective dates",
    "credential handling outside this repository and independent audit",
)

CONFORMANCE_REQUIREMENTS = tuple(
    dict.fromkeys(GENERAL_CONFORMANCE_REQUIREMENTS + MT5_CONFORMANCE_REQUIREMENTS)
)


def conformance_checklist() -> tuple[str, ...]:
    return CONFORMANCE_REQUIREMENTS


def mt5_conformance_checklist() -> tuple[str, ...]:
    return MT5_CONFORMANCE_REQUIREMENTS


@dataclass(frozen=True)
class ExternalBrokerBoundary:
    """Disabled-by-default boundary in front of the MT5 adapter."""

    capabilities: BrokerCapabilities
    enabled: bool = False
    mt5: Any | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def simulate_order_intent(
        self, request: OrderRequest, context: GateContext
    ) -> PaperOrderReceipt:
        """Never a live path.

        A live order needs an evaluated decision *and* the approved intent the
        gate produced, so a bare request cannot become one here.  Keeping this
        refusal unconditional is what makes the boundary meaningful.
        """

        if not self.enabled or self.mt5 is None:
            raise TradingBlocked("external_broker_boundary_disabled")
        raise TradingBlocked("external_broker_boundary_requires_gate_decision")

    # -- MT5 read model -------------------------------------------------

    @property
    def mt5_attached(self) -> bool:
        return self.mt5 is not None

    def mt5_status(self) -> dict[str, Any]:
        if self.mt5 is None:
            return {
                "attached": False,
                "broker_id": "mt5_terminal",
                "provider": "official_metatrader5_python",
                "reason": "no_mt5_adapter_attached",
            }
        return self.mt5.status()

    def audit_view(self) -> dict[str, Any]:
        view: dict[str, Any] = {
            "enabled": self.enabled,
            "capabilities": self.capabilities.audit_view(),
            "conformance": list(CONFORMANCE_REQUIREMENTS),
            "mt5_attached": self.mt5_attached,
            "mt5_status": (
                {
                    "attached": False,
                    "connected": False,
                    "live_orders_possible": False,
                    "reason": "no_mt5_adapter_attached",
                }
                if self.mt5 is None
                else {
                    "attached": True,
                    "connected": bool(self.mt5.status().get("connected")),
                    "live_orders_possible": bool(
                        self.mt5.status().get("execution_authorised")
                    ),
                    "reason": "mt5_adapter_attached",
                }
            ),
            "notes": list(self.notes),
        }
        if self.mt5 is not None:
            status = self.mt5.status()
            view["mt5"] = {
                "connected": status.get("connected"),
                "armed": status.get("armed"),
                "execution_authorised": status.get("execution_authorised"),
                "account_login": (status.get("account") or {}).get("login"),
                "account_server": (status.get("account") or {}).get("server"),
                "account_currency": (status.get("account") or {}).get("currency"),
                "position_accounting": (status.get("account") or {}).get(
                    "position_accounting"
                ),
                "mode": (status.get("settings") or {}).get("mode"),
                "unresolved_orders": status.get("unresolved_orders"),
                "no_order_on_connect": True,
                "token_required_by_broker": False,
            }
        return view
