"""One canonical factory for M1 pre-trade gate contexts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app.accounting.fx import ConversionRateBook
from app.accounting.rollover import RolloverPolicy
from app.config.settings import AccountProfile, RiskLimits, SafetySettings, SystemState
from app.data.models import ForexQuoteBar, QuoteFreshness
from app.risk.models import GateContext

if TYPE_CHECKING:  # pragma: no cover - import-cycle avoidance
    from axiom_forex.execution.paper_engine import PaperExecutionEngine
    from axiom_forex.portfolio.costs import ForexCostModel
    from axiom_forex.portfolio.ledger import ForexPortfolio


@dataclass(frozen=True)
class ForexExecutionContextFactory:
    """Build every GateContext from the same config, risk, and recovery state."""

    safety_settings: SafetySettings
    system_state: SystemState
    account: AccountProfile
    risk_limits: RiskLimits
    cost_model: "ForexCostModel"
    rollover_policy: RolloverPolicy
    conversion_rates: ConversionRateBook
    quote_freshness: QuoteFreshness
    portfolio: "ForexPortfolio"
    paper_execution: "PaperExecutionEngine"

    def build(self, *, at: datetime, bar: ForexQuoteBar) -> GateContext:
        unresolved = self.paper_execution.unresolved_orders()
        # The position snapshot comes from the ledger, with the quantity already
        # promised to in-flight closes subtracted, so the gate validates a close
        # against the real book rather than against the request's claims.
        open_positions = self.portfolio.position_snapshots(
            self.paper_execution.pending_close_quantities()
        )
        return GateContext(
            settings=self.safety_settings,
            system_state=self.system_state,
            account=self.account,
            risk_limits=self.risk_limits,
            cost_model=self.cost_model,
            rollover_policy=self.rollover_policy,
            conversion_rates=self.conversion_rates,
            quote_freshness=self.quote_freshness,
            risk_context=self.portfolio.risk_context(at, bar),
            unresolved_orders=unresolved,
            open_positions=open_positions,
        )

    def audit_view(self) -> dict[str, object]:
        return {
            "settings": self.safety_settings.signature(),
            "account": self.account.audit_view(),
            "risk_limits": self.risk_limits.audit_view(),
            "cost_model": self.cost_model.terms.audit_view(),
            "rollover_policy": self.rollover_policy.audit_view(),
            "quote_freshness": self.quote_freshness.audit_view(),
            "conversion_rates": self.conversion_rates.audit_view(),
        }
