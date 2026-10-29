"""Multi-pair, multi-currency research ledger.

Unlike the single-spec local paper portfolio, this book holds positions in
several pairs at once against one shared multi-currency cash account.  Exposure
is therefore aggregated across *shared currencies*: two long positions in pairs
that both hold the same base currency contribute to the same currency exposure
and are evaluated together, not as independent bets.

The book reuses the existing versioned cost/FX/rollover primitives
(``ForexCostModel`` and ``account_rate``) so evaluation costs and conversion are
the same explicit terms used elsewhere.  It creates no orders and knows nothing
about brokers, paper execution, or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from app.common.types import OrderSide
from app.common.validation import as_utc, parse_decimal
from app.data.models import ForexQuoteBar
from app.universe.specs import CurrencyPairSpec

from axiom_forex.portfolio.costs import ForexCostModel
from axiom_forex.portfolio.fx import account_rate
from axiom_forex.risk.aggregate import (
    AggregateRiskDecision,
    check_aggregate_exposure,
)

from .contracts import EvaluationError, TradeRecord

ZERO = Decimal(0)


class MissingMarkError(EvaluationError):
    """Raised when a position cannot be valued from a completed bar."""


@dataclass(frozen=True)
class PairTerms:
    """Everything the book needs to trade one pair under explicit terms."""

    pair: str
    spec: CurrencyPairSpec
    cost_model: ForexCostModel
    rollover_cutoffs: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class BookPosition:
    position_id: str
    pair: str
    side: OrderSide
    quantity: Decimal
    base_units: Decimal
    entry_price: Decimal
    entry_at: datetime
    entry_notional_account: Decimal
    entry_costs_account: Decimal
    session: str
    regime: str
    decision_index: int
    data_class: str

    @property
    def side_sign(self) -> int:
        return 1 if self.side == OrderSide.BUY else -1

    @property
    def entry_timestamp(self) -> datetime:
        """Adapter for the rollover engine, which names the field this way."""

        return self.entry_at

    def audit_view(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "pair": self.pair,
            "side": self.side.value,
            "quantity": self.quantity,
            "base_units": self.base_units,
            "entry_price": self.entry_price,
            "entry_at": self.entry_at,
            "entry_notional_account": self.entry_notional_account,
            "entry_costs_account": self.entry_costs_account,
            "session": self.session,
            "regime": self.regime,
            "decision_index": self.decision_index,
            "data_class": self.data_class,
        }


class MultiPairBook:
    """Shared multi-currency ledger for research evaluation."""

    def __init__(
        self,
        *,
        account: Any,
        terms: Mapping[str, PairTerms],
        conversion_rates: Any,
        correlation_matrix: Any,
        aggregate_limits: Any,
    ) -> None:
        if not terms:
            raise EvaluationError("no_pair_terms")
        self.account = account
        self.terms = dict(terms)
        self.conversion_rates = conversion_rates
        self.correlation_matrix = correlation_matrix
        self.aggregate_limits = aggregate_limits
        self.cash_by_currency: dict[str, Decimal] = {
            account.account_currency_code: account.initial_capital_decimal
        }
        self.positions: dict[str, BookPosition] = {}
        self.closed: list[TradeRecord] = []
        self.cost_total_account = ZERO
        self.turnover_account = ZERO
        self.realized_by_pair: dict[str, Decimal] = {}
        #: Two-sided traded notional per pair (entry plus exit).
        self.turnover_by_pair: dict[str, Decimal] = {}

    # -- conversion and valuation -----------------------------------------
    def _rate(self, currency: str, at: datetime) -> Decimal:
        return account_rate(
            conversion_rates=self.conversion_rates,
            account=self.account,
            currency=currency,
            at=at,
        )

    def _cash(self, currency: str) -> Decimal:
        return self.cash_by_currency.get(currency, ZERO)

    def _terms(self, pair: str) -> PairTerms:
        terms = self.terms.get(pair)
        if terms is None:
            raise EvaluationError(f"missing_pair_terms:{pair}")
        return terms

    def _mark_price(self, position: BookPosition, bar: ForexQuoteBar) -> Decimal:
        closing_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        return bar.executable_price(closing_side, point="close")

    def _require_marks(
        self, marks: Mapping[str, ForexQuoteBar]
    ) -> dict[str, ForexQuoteBar]:
        missing = sorted(
            {position.pair for position in self.positions.values()} - set(marks)
        )
        if missing:
            raise MissingMarkError("missing_mark_for:" + ",".join(missing))
        return dict(marks)

    def unrealized_by_pair(
        self, marks: Mapping[str, ForexQuoteBar], at: datetime
    ) -> dict[str, Decimal]:
        marks = self._require_marks(marks)
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise EvaluationError("invalid_valuation_time")
        totals: dict[str, Decimal] = {}
        for position in self.positions.values():
            terms = self._terms(position.pair)
            mark = self._mark_price(position, marks[position.pair])
            quote_rate = self._rate(terms.spec.quote_currency, at_utc)
            pnl_quote = (
                position.side_sign * (mark - position.entry_price) * position.base_units
            )
            totals[position.pair] = totals.get(position.pair, ZERO) + pnl_quote * quote_rate
        return totals

    def _position_revaluation(
        self, marks: Mapping[str, ForexQuoteBar], at: datetime
    ) -> Decimal:
        """Value each open position at its own liquidation mark.

        The currency ledger holds the base leg and the entry notional of a
        position until it closes.  Those legs are carried at the *static*
        conversion cross, while the pair's own price moves independently.  Adding
        the mark-to-market P&L on top of the currency legs would count the entry
        notional twice and make equity depend on how far the pair price sits from
        the conversion cross at entry -- a jump that vanishes when the position
        closes.  Replace the static valuation of the base leg with the pair's
        own mark instead, so equity always equals cash plus realized and
        unrealized P&L.
        """

        marks = self._require_marks(marks)
        total = ZERO
        for position in self.positions.values():
            terms = self._terms(position.pair)
            mark = self._mark_price(position, marks[position.pair])
            quote_rate = self._rate(terms.spec.quote_currency, at)
            base_rate = self._rate(terms.spec.base_currency, at)
            total += (
                position.side_sign
                * position.base_units
                * (mark * quote_rate - base_rate)
            )
        return total

    def equity(self, marks: Mapping[str, ForexQuoteBar], at: datetime) -> Decimal:
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise EvaluationError("invalid_valuation_time")
        cash = sum(
            (
                amount * self._rate(currency, at_utc)
                for currency, amount in self.cash_by_currency.items()
            ),
            ZERO,
        )
        return cash + self._position_revaluation(marks, at_utc)

    # -- exposure ----------------------------------------------------------
    def pair_notionals(
        self, marks: Mapping[str, ForexQuoteBar], at: datetime
    ) -> dict[str, Decimal]:
        """Signed position notional per pair, normalized to account currency."""

        marks = self._require_marks(marks)
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise EvaluationError("invalid_valuation_time")
        totals: dict[str, Decimal] = {}
        for position in self.positions.values():
            terms = self._terms(position.pair)
            mark = self._mark_price(position, marks[position.pair])
            quote_rate = self._rate(terms.spec.quote_currency, at_utc)
            value = position.side_sign * position.base_units * mark * quote_rate
            totals[position.pair] = totals.get(position.pair, ZERO) + value
        return totals

    def currency_exposures(
        self, marks: Mapping[str, ForexQuoteBar], at: datetime
    ) -> dict[str, Decimal]:
        """Net exposure per *shared* currency, normalized to account currency."""

        marks = self._require_marks(marks)
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise EvaluationError("invalid_valuation_time")
        totals: dict[str, Decimal] = {}
        for position in self.positions.values():
            terms = self._terms(position.pair)
            mark = self._mark_price(position, marks[position.pair])
            signed_units = position.side_sign * position.base_units
            base_rate = self._rate(terms.spec.base_currency, at_utc)
            quote_rate = self._rate(terms.spec.quote_currency, at_utc)
            base_exposure = signed_units * base_rate
            quote_exposure = -signed_units * mark * quote_rate
            totals[terms.spec.base_currency] = (
                totals.get(terms.spec.base_currency, ZERO) + base_exposure
            )
            totals[terms.spec.quote_currency] = (
                totals.get(terms.spec.quote_currency, ZERO) + quote_exposure
            )
        return {
            currency: value
            for currency, value in totals.items()
            if value != ZERO
        }

    def gross_exposure(
        self, marks: Mapping[str, ForexQuoteBar], at: datetime
    ) -> Decimal:
        return sum(
            (abs(value) for value in self.pair_notionals(marks, at).values()), ZERO
        )

    def margin(self, marks: Mapping[str, ForexQuoteBar], at: datetime) -> Decimal:
        marks = self._require_marks(marks)
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise EvaluationError("invalid_valuation_time")
        total = ZERO
        for position in self.positions.values():
            terms = self._terms(position.pair)
            mark = self._mark_price(position, marks[position.pair])
            quote_rate = self._rate(terms.spec.quote_currency, at_utc)
            notional = position.base_units * mark * quote_rate
            total += (
                notional
                / self.account.leverage_decimal
                * terms.spec.margin_multiplier_decimal()
            )
        return total

    def aggregate_decision(
        self, marks: Mapping[str, ForexQuoteBar], at: datetime
    ) -> AggregateRiskDecision:
        return check_aggregate_exposure(
            matrix=self.correlation_matrix,
            limits=self.aggregate_limits,
            pair_exposures_account=self.pair_notionals(marks, at),
            currency_exposures_account=self.currency_exposures(marks, at),
        )

    def entry_decision(
        self,
        *,
        pair: str,
        side: OrderSide,
        base_units: Decimal,
        price: Decimal,
        marks: Mapping[str, ForexQuoteBar],
        at: datetime,
    ) -> AggregateRiskDecision:
        """Aggregate risk decision *including* the candidate exposure."""

        terms = self._terms(pair)
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise EvaluationError("invalid_valuation_time")
        signed_units = (1 if side == OrderSide.BUY else -1) * base_units
        quote_rate = self._rate(terms.spec.quote_currency, at_utc)
        base_rate = self._rate(terms.spec.base_currency, at_utc)
        pair_exposures = self.pair_notionals(marks, at_utc)
        pair_exposures[pair] = (
            pair_exposures.get(pair, ZERO) + signed_units * price * quote_rate
        )
        currency = self.currency_exposures(marks, at_utc)
        currency[terms.spec.base_currency] = (
            currency.get(terms.spec.base_currency, ZERO) + signed_units * base_rate
        )
        currency[terms.spec.quote_currency] = (
            currency.get(terms.spec.quote_currency, ZERO)
            - signed_units * price * quote_rate
        )
        return check_aggregate_exposure(
            matrix=self.correlation_matrix,
            limits=self.aggregate_limits,
            pair_exposures_account=pair_exposures,
            currency_exposures_account=currency,
        )

    # -- trading -----------------------------------------------------------
    def open_position(
        self,
        *,
        position_id: str,
        pair: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        at: datetime,
        session: str,
        regime: str,
        decision_index: int,
        data_class: str,
    ) -> BookPosition:
        terms = self._terms(pair)
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise EvaluationError("invalid_open_time")
        parsed_price, price_error = parse_decimal(price, "price")
        if price_error or parsed_price is None or parsed_price <= 0:
            raise EvaluationError("invalid_fill_price")
        if position_id in self.positions:
            raise EvaluationError("duplicate_position_id")
        quantity_decimal, quantity_validation = terms.spec.parse_quantity(quantity)
        if not quantity_validation.valid or quantity_decimal is None:
            raise EvaluationError("invalid_quantity")
        base_units = terms.spec.to_base_units(quantity_decimal)
        quote_rate = self._rate(terms.spec.quote_currency, at_utc)
        notional_quote = base_units * parsed_price
        notional_account = notional_quote * quote_rate
        costs = terms.cost_model.entry_costs(
            base_units=base_units, price=parsed_price, at=at_utc
        )
        sign = 1 if side == OrderSide.BUY else -1
        account_currency = self.account.account_currency_code
        self.cash_by_currency[terms.spec.base_currency] = (
            self._cash(terms.spec.base_currency) + sign * base_units
        )
        self.cash_by_currency[terms.spec.quote_currency] = (
            self._cash(terms.spec.quote_currency) - sign * notional_quote
        )
        self.cash_by_currency[account_currency] = (
            self._cash(account_currency) - costs.total_account
        )
        self.cost_total_account += costs.total_account
        self.turnover_account += notional_account
        self.turnover_by_pair[pair] = (
            self.turnover_by_pair.get(pair, ZERO) + notional_account
        )
        position = BookPosition(
            position_id=position_id,
            pair=pair,
            side=side,
            quantity=quantity_decimal,
            base_units=base_units,
            entry_price=parsed_price,
            entry_at=at_utc,
            entry_notional_account=notional_account,
            entry_costs_account=costs.total_account,
            session=session,
            regime=regime,
            decision_index=decision_index,
            data_class=data_class,
        )
        self.positions[position_id] = position
        return position

    def close_position(
        self,
        *,
        position_id: str,
        price: Decimal,
        at: datetime,
    ) -> TradeRecord:
        position = self.positions.get(position_id)
        if position is None:
            raise EvaluationError("unknown_position")
        terms = self._terms(position.pair)
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise EvaluationError("invalid_close_time")
        parsed_price, price_error = parse_decimal(price, "price")
        if price_error or parsed_price is None or parsed_price <= 0:
            raise EvaluationError("invalid_fill_price")
        # Only cutoffs already elapsed at the exit instant may accrue; a future
        # cutoff is a scheduling error, not financing.
        elapsed = tuple(
            cutoff
            for cutoff in terms.rollover_cutoffs
            if as_utc(cutoff, "cutoff")[0] is not None
            and as_utc(cutoff, "cutoff")[0] <= at_utc
        )
        costs = terms.cost_model.exit_costs(
            position=position,
            price=parsed_price,
            at=at_utc,
            cutoffs=elapsed,
        )
        quote_rate = self._rate(terms.spec.quote_currency, at_utc)
        gross_pnl_quote = (
            position.side_sign * (parsed_price - position.entry_price) * position.base_units
        )
        gross_pnl_account = gross_pnl_quote * quote_rate
        # Both sides of the trade cost money.  Entry costs were already taken
        # out of the account when the position opened, so net P&L must subtract
        # them here too; otherwise the trade table, the grouped metrics, and the
        # equity curve report three different numbers for the same trade.
        total_costs_account = position.entry_costs_account + costs.total_account
        net_pnl_account = gross_pnl_account - total_costs_account
        sign = position.side_sign
        account_currency = self.account.account_currency_code
        self.cash_by_currency[terms.spec.base_currency] = (
            self._cash(terms.spec.base_currency) - sign * position.base_units
        )
        self.cash_by_currency[terms.spec.quote_currency] = (
            self._cash(terms.spec.quote_currency)
            + sign * position.base_units * parsed_price
        )
        self.cash_by_currency[account_currency] = (
            self._cash(account_currency) - costs.total_account
        )
        exit_notional = position.base_units * parsed_price * quote_rate
        self.cost_total_account += costs.total_account
        self.turnover_account += exit_notional
        self.turnover_by_pair[position.pair] = (
            self.turnover_by_pair.get(position.pair, ZERO) + exit_notional
        )
        self.realized_by_pair[position.pair] = (
            self.realized_by_pair.get(position.pair, ZERO) + net_pnl_account
        )
        trade = TradeRecord(
            trade_id=position_id,
            pair=position.pair,
            side=position.side.value,
            quantity=position.quantity,
            base_units=position.base_units,
            entry_price=position.entry_price,
            exit_price=parsed_price,
            entry_at=position.entry_at,
            exit_at=at_utc,
            decision_index=position.decision_index,
            entry_session=position.session,
            entry_regime=position.regime,
            notional_account=position.entry_notional_account,
            exit_notional_account=exit_notional,
            gross_pnl_account=gross_pnl_account,
            costs_account=total_costs_account,
            net_pnl_account=net_pnl_account,
            holding_seconds=Decimal(str((at_utc - position.entry_at).total_seconds())),
            data_class=position.data_class,
        )
        self.closed.append(trade)
        del self.positions[position_id]
        return trade

    # -- reporting ---------------------------------------------------------
    def state_view(self) -> dict[str, Any]:
        return {
            "account_currency": self.account.account_currency_code,
            "cash_by_currency": {
                str(currency): value for currency, value in self.cash_by_currency.items()
            },
            "open_positions": [
                position.audit_view() for position in self.positions.values()
            ],
            "closed_trades": len(self.closed),
            "cost_total_account": self.cost_total_account,
            "turnover_account": self.turnover_account,
            "realized_by_pair": {
                str(pair): value for pair, value in self.realized_by_pair.items()
            },
        }
