"""Per-side spread/slippage/commission costs plus explicit rollover financing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from app.accounting.costs import CostEstimate, CostModelTerms, StaticCostModel
from app.accounting.fx import ConversionRateBook
from app.common.validation import ValidationResult, parse_decimal
from app.config.settings import AccountProfile
from app.universe.specs import CurrencyPairSpec

from .fx import account_rate
from .rollover import RolloverEngine


@dataclass(frozen=True)
class ForexExecutionCosts:
    commission_account: Decimal
    slippage_account: Decimal
    financing_account: Decimal
    total_account: Decimal
    basis: str
    rollover_events: tuple[dict[str, Any], ...] = ()

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        for name in (
            "commission_account",
            "slippage_account",
            "financing_account",
            "total_account",
        ):
            value, error = parse_decimal(getattr(self, name), name)
            if error or value is None or not value.is_finite():
                errors.append(error or f"non_finite_{name}")
        if not isinstance(self.basis, str) or not self.basis.strip():
            errors.append("missing_basis")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def allocated(self, fraction: Decimal) -> "ForexExecutionCosts":
        """Return the part of these costs belonging to ``fraction`` of a position.

        Partial closes must split the entry costs they already paid.  Allocating
        the share exactly keeps the sum of every closed trade's entry costs equal
        to the entry costs actually charged, so ``gross - costs == net`` holds
        for each partial close and for the position as a whole.
        """

        share, error = parse_decimal(fraction, "fraction")
        if error or share is None or share < 0 or share > 1:
            raise ValueError(error or "invalid_allocation_fraction")
        return ForexExecutionCosts(
            commission_account=self.commission_account * share,
            slippage_account=self.slippage_account * share,
            financing_account=self.financing_account * share,
            total_account=self.total_account * share,
            basis=f"{self.basis}:partial_allocation",
            rollover_events=tuple(self.rollover_events) if share == 1 else (),
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "commission_account": self.commission_account,
            "slippage_account": self.slippage_account,
            "financing_account": self.financing_account,
            "total_account": self.total_account,
            "basis": self.basis,
            "rollover_events": list(self.rollover_events),
        }


@dataclass(frozen=True)
class ForexCostModel:
    terms: CostModelTerms
    spec: CurrencyPairSpec
    account: AccountProfile
    conversion_rates: ConversionRateBook
    rollover_engine: RolloverEngine

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        errors.extend(self.terms.validate().errors)
        errors.extend(self.spec.validate().errors)
        errors.extend(self.account.validate().errors)
        errors.extend(self.rollover_engine.validate().errors)
        if self.conversion_rates.source != self.account.conversion_source:
            errors.append("conversion_source_mismatch")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def _common_costs(
        self,
        *,
        base_units: Decimal,
        price: Decimal,
        at: datetime,
    ) -> tuple[Decimal, Decimal, Decimal]:
        quote_rate = account_rate(
            conversion_rates=self.conversion_rates,
            account=self.account,
            currency=self.spec.quote_currency,
            at=at,
        )
        notional_account = base_units * price * quote_rate
        pip_value_account = base_units * self.spec.pip_size_decimal() * quote_rate
        commission = notional_account * self.terms.commission_rate
        slippage = pip_value_account * self.terms.slippage_pips
        return commission, slippage, notional_account

    def estimate_new_order_cost(
        self,
        *,
        notional_account: Decimal,
        pip_value_account: Decimal,
    ) -> CostEstimate:
        """Implement the M1 ``CostModel`` protocol from the same terms.

        The pre-trade gate and the portfolio ledger therefore share one
        versioned cost configuration instead of two competing objects.
        """
        if not self.validate().valid:
            raise ValueError("Invalid forex cost model")
        return StaticCostModel(self.terms).estimate_new_order_cost(
            notional_account=notional_account,
            pip_value_account=pip_value_account,
        )

    def entry_costs(
        self,
        *,
        base_units: Decimal,
        price: Decimal,
        at: datetime,
    ) -> ForexExecutionCosts:
        if not self.validate().valid:
            raise ValueError("Invalid forex cost model")
        commission, slippage, _ = self._common_costs(
            base_units=base_units, price=price, at=at
        )
        return ForexExecutionCosts(
            commission_account=commission,
            slippage_account=slippage,
            financing_account=Decimal(0),
            total_account=commission + slippage,
            basis="entry_side_explicit_terms",
        )

    def exit_costs(
        self,
        *,
        position: Any,
        price: Decimal,
        at: datetime,
        cutoffs: Sequence[datetime],
    ) -> ForexExecutionCosts:
        if not self.validate().valid:
            raise ValueError("Invalid forex cost model")
        commission, slippage, _ = self._common_costs(
            base_units=position.base_units, price=price, at=at
        )
        accrual = self.rollover_engine.accrue(position, cutoffs=cutoffs, as_of=at)
        financing = accrual.total_account
        return ForexExecutionCosts(
            commission_account=commission,
            slippage_account=slippage,
            financing_account=financing,
            total_account=commission + slippage + financing,
            basis="exit_side_with_explicit_rollover",
            rollover_events=tuple(event.audit_view() for event in accrual.events),
        )
