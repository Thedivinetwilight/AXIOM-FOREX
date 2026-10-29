"""Multi-currency forex position ledger and account-currency risk snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.accounting.fx import ConversionRateBook
from app.common.types import OrderSide
from app.common.validation import as_utc, parse_decimal
from app.config.settings import AccountProfile
from app.data.models import ForexQuoteBar
from app.risk.models import PositionSnapshot, RiskContext
from app.universe.specs import CurrencyPairSpec

from .costs import ForexCostModel, ForexExecutionCosts
from .fx import account_rate


@dataclass(frozen=True)
class Position:
    position_id: str
    pair: str
    side: OrderSide
    quantity: Decimal
    base_units: Decimal
    entry_price: Decimal
    entry_timestamp: datetime
    entry_quote_rate: Decimal
    entry_base_rate: Decimal
    notional_account: Decimal
    margin_account: Decimal
    #: Entry costs still unallocated to a close.  A partial close allocates the
    #: closed part of these costs to the closed trade and leaves the rest here.
    costs: ForexExecutionCosts
    #: Identity of the owning account.  A close must match it, so an intent can
    #: never name a position that belongs to a different account.
    account_id: str = ""
    #: Size at entry.  ``quantity``/``base_units`` are the *remaining* size, so
    #: valuation code keeps working unchanged while partial closes reduce it.
    original_quantity: Decimal = Decimal(0)
    original_base_units: Decimal = Decimal(0)
    #: Full entry costs charged when the position opened (audit trail only).
    entry_costs_total_account: Decimal = Decimal(0)

    @property
    def side_sign(self) -> int:
        return 1 if self.side == OrderSide.BUY else -1

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity

    @property
    def closed_quantity(self) -> Decimal:
        original = self.original_quantity if self.original_quantity > 0 else self.quantity
        closed = original - self.quantity
        return closed if closed > 0 else Decimal(0)

    @property
    def is_partially_closed(self) -> bool:
        return self.closed_quantity > 0

    def audit_view(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "account_id": self.account_id,
            "pair": self.pair,
            "side": self.side.value,
            "quantity": self.quantity,
            "base_units": self.base_units,
            "original_quantity": self.original_quantity,
            "original_base_units": self.original_base_units,
            "closed_quantity": self.closed_quantity,
            "entry_price": self.entry_price,
            "entry_timestamp": self.entry_timestamp,
            "entry_quote_rate": self.entry_quote_rate,
            "entry_base_rate": self.entry_base_rate,
            "notional_account": self.notional_account,
            "margin_account": self.margin_account,
            "entry_costs_total_account": self.entry_costs_total_account,
            "costs": self.costs.audit_view(),
        }


@dataclass(frozen=True)
class ClosedTrade:
    position_id: str
    pair: str
    side: OrderSide
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    realized_pnl_account: Decimal
    costs: ForexExecutionCosts
    #: Entry-side costs already taken out of the account when the position
    #: opened.  ``realized_pnl_account`` is net of both sides.
    entry_costs_account: Decimal = Decimal(0)
    #: Size closed by this trade, in base units.  A partial close leaves the
    #: position open with the remainder; several trades may share a position id.
    closed_base_units: Decimal = Decimal(0)
    partial: bool = False

    @property
    def total_costs_account(self) -> Decimal:
        return self.entry_costs_account + self.costs.total_account

    def audit_view(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "pair": self.pair,
            "side": self.side.value,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "realized_pnl_account": self.realized_pnl_account,
            "entry_costs_account": self.entry_costs_account,
            "total_costs_account": self.total_costs_account,
            "closed_base_units": self.closed_base_units,
            "partial": self.partial,
            "costs": self.costs.audit_view(),
        }


DAILY_COUNTER_BASIS_UTC = "UTC_CALENDAR_DATE"


def trading_day(
    at: datetime, calendar: Any | None = None
) -> tuple[date | None, str]:
    """Return the calendar day that ``at`` belongs to, and the basis used.

    Daily risk counters (realized P&L today, trades today) must roll over at the
    broker's trading-day boundary, not never and not at an arbitrary local
    midnight.  The boundary is taken from the supplied trading calendar's own
    timezone, because that is the day the broker's calendar describes; without a
    calendar the UTC calendar date is used and the basis says so, so a report
    never claims a broker trading day it does not have.
    """

    at_utc, error = as_utc(at, "at")
    if error or at_utc is None:
        return None, DAILY_COUNTER_BASIS_UTC
    timezone_name = getattr(calendar, "timezone_name", None)
    if isinstance(timezone_name, str) and timezone_name.strip():
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            zone = None
        if zone is not None:
            return at_utc.astimezone(zone).date(), f"CALENDAR_LOCAL_DATE:{timezone_name}"
    return at_utc.date(), DAILY_COUNTER_BASIS_UTC


class ForexPortfolio:
    def __init__(
        self,
        *,
        account: AccountProfile,
        spec: CurrencyPairSpec,
        conversion_rates: ConversionRateBook,
        cost_model: ForexCostModel,
        calendar: Any | None = None,
    ):
        validation = account.validate()
        if not validation.valid:
            raise ValueError("Invalid account: " + validation.reason)
        spec_validation = spec.validate()
        if not spec_validation.valid:
            raise ValueError("Invalid spec: " + spec_validation.reason)
        self.account = account
        self.spec = spec
        self.conversion_rates = conversion_rates
        self.cost_model = cost_model
        self.cash_by_currency: dict[str, Decimal] = {
            account.account_currency_code: account.initial_capital_decimal
        }
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.realized_pnl_today_account = Decimal(0)
        self.trades_today = 0
        self.cost_total_account = Decimal(0)
        self.rollover_total_account = Decimal(0)
        # Daily counters describe one trading day only.  ``trading_day`` and the
        # basis string are persisted so a restart resumes the same day instead of
        # either carrying yesterday's losses forever or silently resetting them.
        self.calendar = calendar
        self.daily_counter_basis = trading_day(datetime.now(timezone.utc), calendar)[1]
        self.daily_counter_day: date | None = None

    def _trading_day(self, at: datetime) -> tuple[date | None, str]:
        return trading_day(at, self.calendar)

    def _roll_daily_counters(self, at: datetime) -> None:
        """Reset the daily counters when ``at`` starts a new trading day."""

        day, basis = self._trading_day(at)
        self.daily_counter_basis = basis
        if day is None or day == self.daily_counter_day:
            return
        self.daily_counter_day = day
        self.realized_pnl_today_account = Decimal(0)
        self.trades_today = 0

    def _daily_counters_at(self, at: datetime) -> tuple[Decimal, int]:
        """Counters as the gate must see them at ``at`` (pure: no mutation)."""

        day, _ = self._trading_day(at)
        if day is None or self.daily_counter_day is None or day == self.daily_counter_day:
            return self.realized_pnl_today_account, self.trades_today
        return Decimal(0), 0

    def empty_clone(self) -> "ForexPortfolio":
        """Create a clean ledger with the same explicit, versioned dependencies."""
        return ForexPortfolio(
            account=self.account,
            spec=self.spec,
            conversion_rates=self.conversion_rates,
            cost_model=self.cost_model,
            calendar=self.calendar,
        )

    def _rate(self, currency: str, at: datetime) -> Decimal:
        return account_rate(
            conversion_rates=self.conversion_rates,
            account=self.account,
            currency=currency,
            at=at,
        )

    def _cash(self, currency: str) -> Decimal:
        return self.cash_by_currency.get(currency, Decimal(0))

    def _mark_price(self, position: Position, bar: ForexQuoteBar) -> Decimal:
        closing_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        return bar.executable_price(closing_side, point="close")

    def _unrealized_pnl_account(
        self, position: Position, at: datetime, bar: ForexQuoteBar
    ) -> Decimal:
        mark = self._mark_price(position, bar)
        quote_rate = self._rate(self.spec.quote_currency, at)
        pnl_quote = position.side_sign * (mark - position.entry_price) * position.base_units
        return pnl_quote * quote_rate

    def _position_revaluation(
        self, at: datetime, bar: ForexQuoteBar | None
    ) -> Decimal:
        """Value open positions at their own liquidation mark, not at the cross.

        The currency ledger carries a position's base leg and entry notional
        until it closes, and those legs are valued at the *static* conversion
        book.  The pair's own price is a separate declared input, so adding
        mark-to-market P&L on top of the currency legs counts the entry
        notional twice and makes equity jump by however far the entry price
        sits from the implied conversion cross.  Replace the static valuation
        of the base leg with the position's own mark so equity always equals
        cash plus realized and unrealized P&L.  Without a bar there is no
        mark: the entry price is used, i.e. no unrealized P&L.
        """

        quote_rate = self._rate(self.spec.quote_currency, at)
        base_rate = self._rate(self.spec.base_currency, at)
        total = Decimal(0)
        for position in self.positions.values():
            mark = (
                self._mark_price(position, bar)
                if bar is not None
                else position.entry_price
            )
            total += (
                position.side_sign
                * position.base_units
                * (mark * quote_rate - base_rate)
            )
        return total

    def entry_notional_account(
        self, *, quantity: Any, price: Any, at: datetime
    ) -> Decimal:
        """Account-currency notional an entry of ``quantity`` would carry.

        This is the same arithmetic the ledger applies when it opens a
        position: base units from the spec, times the executable price, times
        the timestamped quote-to-account rate.  It fails closed (``ValueError``)
        when the quantity is off the tradable grid or when there is no
        conversion rate at that instant -- a limit check must never be run
        against a number that had to be guessed.
        """

        quantity_decimal, validation = self.spec.parse_quantity(quantity)
        if not validation.valid or quantity_decimal is None:
            raise ValueError("invalid_entry_quantity")
        price_decimal, price_error = parse_decimal(price, "price")
        if price_error or price_decimal is None or price_decimal <= 0:
            raise ValueError("invalid_entry_price")
        at_utc, at_error = as_utc(at, "at")
        if at_error or at_utc is None:
            raise ValueError("invalid_entry_instant")
        base_units = self.spec.to_base_units(quantity_decimal)
        return base_units * price_decimal * self._rate(self.spec.quote_currency, at_utc)

    def equity_account(self, at: datetime, bar: ForexQuoteBar | None = None) -> Decimal:
        at_utc, error = as_utc(at, "at")
        if error or at_utc is None:
            raise ValueError("invalid_equity_timestamp")
        if self.positions and bar is None:
            raise ValueError("mark_bar_required_for_open_positions")
        equity = sum(
            (amount * self._rate(currency, at_utc) for currency, amount in self.cash_by_currency.items()),
            Decimal(0),
        )
        return equity + self._position_revaluation(at_utc, bar)

    def gross_exposure_account(
        self, at: datetime, bar: ForexQuoteBar | None = None
    ) -> Decimal:
        if self.positions and bar is None:
            raise ValueError("mark_bar_required_for_open_positions")
        quote_rate = self._rate(self.spec.quote_currency, at)
        total = Decimal(0)
        for position in self.positions.values():
            mark = self._mark_price(position, bar) if bar is not None else position.entry_price
            total += abs(position.base_units * mark * quote_rate)
        return total

    def margin_account(self, at: datetime, bar: ForexQuoteBar | None = None) -> Decimal:
        if self.positions and bar is None:
            raise ValueError("mark_bar_required_for_open_positions")
        quote_rate = self._rate(self.spec.quote_currency, at)
        total = Decimal(0)
        for position in self.positions.values():
            mark = self._mark_price(position, bar) if bar is not None else position.entry_price
            notional_account = position.base_units * mark * quote_rate
            total += (
                notional_account
                / self.account.leverage_decimal
                * self.spec.margin_multiplier_decimal()
            )
        return total

    def currency_exposures_account(self, at: datetime) -> dict[str, Decimal]:
        """Non-account-currency cash exposures normalized to account currency.

        Account-currency cash is capital, not an FX exposure; including it
        would incorrectly breach currency exposure limits.
        """
        account_currency = self.account.account_currency_code
        return {
            currency: amount * self._rate(currency, at)
            for currency, amount in self.cash_by_currency.items()
            if currency != account_currency
        }

    def correlation_group_exposures_account(
        self, at: datetime, bar: ForexQuoteBar | None = None
    ) -> dict[str, Decimal]:
        if self.positions and bar is None:
            raise ValueError("mark_bar_required_for_open_positions")
        quote_rate = self._rate(self.spec.quote_currency, at)
        total = Decimal(0)
        for position in self.positions.values():
            mark = self._mark_price(position, bar) if bar is not None else position.entry_price
            total += position.side_sign * position.base_units * mark * quote_rate
        return {self.spec.correlation_group: total}

    def risk_context(
        self, at: datetime, bar: ForexQuoteBar | None = None
    ) -> RiskContext:
        realized_today, trades_today = self._daily_counters_at(at)
        context = RiskContext(
            equity_account=self.equity_account(at, bar),
            gross_exposure_account=self.gross_exposure_account(at, bar),
            currency_exposures_account=self.currency_exposures_account(at),
            correlation_group_exposures_account=self.correlation_group_exposures_account(at, bar),
            open_positions=len(self.positions),
            realized_pnl_today_account=realized_today,
            trades_today=trades_today,
        )
        validation = context.validate()
        if not validation.valid:
            raise ValueError("Invalid risk context: " + validation.reason)
        return context

    def open(
        self,
        *,
        position_id: str,
        side: OrderSide | str,
        quantity: Decimal,
        price: Decimal,
        at: datetime,
    ) -> Position:
        side_value = side.value if isinstance(side, OrderSide) else str(side).upper()
        try:
            order_side = OrderSide(side_value)
        except ValueError as exc:
            raise ValueError("invalid_side") from exc
        quantity_decimal, quantity_validation = self.spec.parse_quantity(quantity)
        if not quantity_validation.valid or quantity_decimal is None:
            raise ValueError("invalid_quantity")
        price_decimal, price_error = parse_decimal(price, "price")
        if price_error or price_decimal is None or price_decimal <= 0:
            raise ValueError("invalid_price")
        at_utc, at_error = as_utc(at, "at")
        if at_error or at_utc is None:
            raise ValueError("invalid_at")
        if position_id in self.positions:
            raise ValueError("duplicate_position_id")
        self._roll_daily_counters(at_utc)
        base_units = self.spec.to_base_units(quantity_decimal)
        quote_rate = self._rate(self.spec.quote_currency, at_utc)
        base_rate = self._rate(self.spec.base_currency, at_utc)
        notional_quote = base_units * price_decimal
        notional_account = notional_quote * quote_rate
        margin_account = (
            notional_account
            / self.account.leverage_decimal
            * self.spec.margin_multiplier_decimal()
        )
        costs = self.cost_model.entry_costs(
            base_units=base_units, price=price_decimal, at=at_utc
        )
        sign = 1 if order_side == OrderSide.BUY else -1
        self.cash_by_currency[self.spec.base_currency] = (
            self._cash(self.spec.base_currency) + sign * base_units
        )
        self.cash_by_currency[self.spec.quote_currency] = (
            self._cash(self.spec.quote_currency) - sign * notional_quote
        )
        self.cash_by_currency[self.account.account_currency_code] = (
            self._cash(self.account.account_currency_code) - costs.total_account
        )
        position = Position(
            position_id=position_id,
            pair=self.spec.canonical_pair or "",
            side=order_side,
            quantity=quantity_decimal,
            base_units=base_units,
            entry_price=price_decimal,
            entry_timestamp=at_utc,
            entry_quote_rate=quote_rate,
            entry_base_rate=base_rate,
            notional_account=notional_account,
            margin_account=margin_account,
            costs=costs,
            account_id=self.account.account_id,
            original_quantity=quantity_decimal,
            original_base_units=base_units,
            entry_costs_total_account=costs.total_account,
        )
        self.positions[position_id] = position
        self.trades_today += 1
        self.cost_total_account += costs.total_account
        self.rollover_total_account += costs.financing_account
        return position

    def close(
        self,
        position_id: str,
        *,
        price: Decimal,
        at: datetime,
        cutoffs: Sequence[datetime] = (),
        quantity: Decimal | None = None,
    ) -> ClosedTrade:
        """Close all of ``position_id``, or exactly ``quantity`` of it.

        Every check and every cost/rate lookup happens before the first mutation,
        so a refused or failing close leaves the book untouched (the engine can
        then mark the order unresolved without a half-applied position).  A
        partial close keeps the position open with the remainder, allocates the
        matching share of the entry costs to the closed slice, and accrues
        financing only on the slice being closed -- so repeated partial closes
        add up to exactly the same totals as one full close.
        """

        position = self.positions.get(position_id)
        if position is None:
            raise ValueError("unknown_position")
        price_decimal, price_error = parse_decimal(price, "price")
        if price_error or price_decimal is None or price_decimal <= 0:
            raise ValueError("invalid_price")
        at_utc, at_error = as_utc(at, "at")
        if at_error or at_utc is None:
            raise ValueError("invalid_at")

        remaining_quantity = position.quantity
        remaining_units = position.base_units
        if quantity is None:
            close_quantity = remaining_quantity
        else:
            parsed_quantity, quantity_error = parse_decimal(quantity, "close_quantity")
            if quantity_error or parsed_quantity is None or parsed_quantity <= 0:
                raise ValueError("invalid_close_quantity")
            close_quantity = parsed_quantity
        if close_quantity > remaining_quantity:
            raise ValueError("close_quantity_exceeds_position")
        partial = close_quantity < remaining_quantity
        if partial:
            # A partial close must land on a tradable quantity step; a full
            # close of the remainder is always allowed because the remainder is
            # itself the result of valid steps.
            _, step_validation = self.spec.parse_quantity(close_quantity)
            if not step_validation.valid:
                raise ValueError("invalid_close_quantity")
        # Base units come from the spec exactly as they do on the way in, so the
        # closed slice and the remaining slice always sum to the original size.
        close_units = (
            remaining_units
            if not partial
            else self.spec.to_base_units(close_quantity)
        )
        if close_units <= 0 or close_units > remaining_units:
            raise ValueError("invalid_close_base_units")
        fraction = close_units / remaining_units if remaining_units else Decimal(1)

        self._roll_daily_counters(at_utc)
        # Costs are computed on the slice being closed, not on the whole
        # position, so a partial close pays its own share of commission,
        # slippage, and financing.
        closing_view = replace(
            position,
            quantity=close_quantity,
            base_units=close_units,
            notional_account=position.notional_account * fraction,
            margin_account=position.margin_account * fraction,
        )
        costs = self.cost_model.exit_costs(
            position=closing_view, price=price_decimal, at=at_utc, cutoffs=cutoffs
        )
        entry_costs_share = position.costs.allocated(fraction)
        quote_rate = self._rate(self.spec.quote_currency, at_utc)
        realized_pnl_quote = (
            position.side_sign * (price_decimal - position.entry_price) * close_units
        )
        # Both sides cost money: the entry commission/slippage was already taken
        # out of the account when the position opened, so realized P&L is only
        # correct when its allocated share is subtracted here as well.
        realized_pnl_account = (
            realized_pnl_quote * quote_rate
            - entry_costs_share.total_account
            - costs.total_account
        )

        sign = position.side_sign
        self.cash_by_currency[self.spec.base_currency] = (
            self._cash(self.spec.base_currency) - sign * close_units
        )
        self.cash_by_currency[self.spec.quote_currency] = (
            self._cash(self.spec.quote_currency) + sign * close_units * price_decimal
        )
        self.cash_by_currency[self.account.account_currency_code] = (
            self._cash(self.account.account_currency_code) - costs.total_account
        )
        self.realized_pnl_today_account += realized_pnl_account
        # Entry costs were already added when the position opened; adding them
        # again here would double-count every trade's costs.
        self.cost_total_account += costs.total_account
        self.rollover_total_account += costs.financing_account
        trade = ClosedTrade(
            position_id=position_id,
            pair=position.pair,
            side=position.side,
            quantity=close_quantity,
            entry_price=position.entry_price,
            exit_price=price_decimal,
            opened_at=position.entry_timestamp,
            closed_at=at_utc,
            realized_pnl_account=realized_pnl_account,
            costs=costs,
            entry_costs_account=entry_costs_share.total_account,
            closed_base_units=close_units,
            partial=partial,
        )
        if partial:
            self.positions[position_id] = replace(
                position,
                quantity=remaining_quantity - close_quantity,
                base_units=remaining_units - close_units,
                notional_account=position.notional_account * (1 - fraction),
                margin_account=position.margin_account * (1 - fraction),
                costs=replace(
                    position.costs,
                    commission_account=position.costs.commission_account
                    - entry_costs_share.commission_account,
                    slippage_account=position.costs.slippage_account
                    - entry_costs_share.slippage_account,
                    financing_account=position.costs.financing_account
                    - entry_costs_share.financing_account,
                    total_account=position.costs.total_account
                    - entry_costs_share.total_account,
                    basis=f"{position.costs.basis}:remaining_after_partial",
                ),
            )
        else:
            del self.positions[position_id]
        self.closed_trades.append(trade)
        return trade

    def position_snapshots(
        self,
        pending_close_quantities: Mapping[str, Any] | None = None,
    ) -> tuple[PositionSnapshot, ...]:
        """Open positions exactly as the risk gate must see them.

        ``pending_close_quantities`` carries the quantity already promised to an
        in-flight close, so two concurrent closes cannot both consume the same
        remaining size.  Everything here comes from the ledger; nothing is taken
        from a request or a caller.
        """

        pending = dict(pending_close_quantities or {})
        snapshots: list[PositionSnapshot] = []
        for position in self.positions.values():
            raw_pending = pending.get(position.position_id, Decimal(0))
            pending_quantity, pending_error = parse_decimal(
                raw_pending, "pending_close_quantity"
            )
            if pending_error or pending_quantity is None or pending_quantity < 0:
                raise ValueError("invalid_pending_close_quantity")
            if pending_quantity > position.quantity:
                raise ValueError("pending_close_exceeds_position")
            snapshots.append(
                PositionSnapshot(
                    position_id=position.position_id,
                    account_id=position.account_id or self.account.account_id,
                    pair=position.pair,
                    side=position.side,
                    quantity=position.quantity,
                    quantity_unit=self.spec.quantity_unit_value,
                    base_units=position.base_units,
                    opened_at=position.entry_timestamp,
                    pending_close_quantity=pending_quantity,
                )
            )
        return tuple(snapshots)

    def snapshot(
        self,
        at: datetime,
        bar: ForexQuoteBar | None = None,
        *,
        allow_unmarked: bool = False,
    ) -> dict[str, Any]:
        """Full portfolio snapshot.

        ``allow_unmarked`` writes the durable ledger state without inventing a
        mark price: equity, gross exposure, margin, leverage, and correlation
        exposure become ``None``.  Settlement uses it so an order can be
        recorded atomically at the instant it filled without a market bar, while
        ``restore_snapshot`` (which needs none of the marked fields) can still
        restore the book exactly.
        """

        try:
            equity = self.equity_account(at, bar)
            gross = self.gross_exposure_account(at, bar)
            margin = self.margin_account(at, bar)
            correlation = self.correlation_group_exposures_account(at, bar)
        except ValueError:
            if not allow_unmarked:
                raise
            equity = gross = margin = None
            correlation = {}
        return {
            "at": at,
            "account_currency": self.account.account_currency_code,
            "equity_account": equity,
            "cash_by_currency": dict(self.cash_by_currency),
            "currency_exposures_account": self.currency_exposures_account(at),
            "correlation_group_exposures_account": correlation,
            "gross_exposure_account": gross,
            "margin_account": margin,
            "effective_leverage": (
                gross / equity if gross is not None and equity else None
            ),
            "open_positions": [position.audit_view() for position in self.positions.values()],
            "closed_trades": [trade.audit_view() for trade in self.closed_trades],
            "realized_pnl_today_account": self.realized_pnl_today_account,
            "trades_today": self.trades_today,
            "daily_counter_day": self.daily_counter_day,
            "daily_counter_basis": self.daily_counter_basis,
            "cost_total_account": self.cost_total_account,
            "rollover_total_account": self.rollover_total_account,
        }

    def restore_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Atomically restore a sanitized portfolio snapshot after a restart."""
        if not isinstance(snapshot, Mapping):
            raise ValueError("invalid_portfolio_snapshot")

        def dec(value: Any, name: str) -> Decimal:
            parsed, error = parse_decimal(value, name)
            if error or parsed is None:
                raise ValueError(error or f"invalid_{name}")
            return parsed

        def timestamp(value: Any, name: str) -> datetime:
            parsed = value
            if isinstance(parsed, str):
                try:
                    parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(f"invalid_{name}") from exc
            utc_value, error = as_utc(parsed, name)
            if error or utc_value is None:
                raise ValueError(error or f"invalid_{name}")
            return utc_value

        def costs_from_view(view: Any) -> ForexExecutionCosts:
            if not isinstance(view, Mapping):
                raise ValueError("invalid_costs_snapshot")
            events = view.get("rollover_events", ())
            if not isinstance(events, (list, tuple)):
                raise ValueError("invalid_rollover_events")
            costs = ForexExecutionCosts(
                commission_account=dec(view.get("commission_account"), "commission_account"),
                slippage_account=dec(view.get("slippage_account"), "slippage_account"),
                financing_account=dec(view.get("financing_account"), "financing_account"),
                total_account=dec(view.get("total_account"), "total_account"),
                basis=str(view.get("basis", "")),
                rollover_events=tuple(dict(item) for item in events if isinstance(item, Mapping)),
            )
            if not costs.validate().valid:
                raise ValueError("invalid_costs_snapshot")
            return costs

        def position_from_view(view: Any) -> Position:
            if not isinstance(view, Mapping):
                raise ValueError("invalid_position_snapshot")
            position_id = str(view.get("position_id", ""))
            if not position_id:
                raise ValueError("missing_position_id")
            try:
                side = OrderSide(str(view.get("side", "")).upper())
            except ValueError as exc:
                raise ValueError("invalid_position_side") from exc
            quantity_value = dec(view.get("quantity"), "position_quantity")
            base_units_value = dec(view.get("base_units"), "base_units")
            # A partially closed position must restore its *remaining* size and
            # its original size, so a close after a restart still knows how much
            # is left and how much of the entry costs is still unallocated.
            original_quantity_value = dec(
                view.get("original_quantity", quantity_value), "original_quantity"
            )
            original_base_units_value = dec(
                view.get("original_base_units", base_units_value), "original_base_units"
            )
            if original_quantity_value < quantity_value:
                raise ValueError("original_quantity_below_remaining")
            if original_base_units_value < base_units_value:
                raise ValueError("original_base_units_below_remaining")
            return Position(
                position_id=position_id,
                pair=str(view.get("pair", "")),
                side=side,
                quantity=quantity_value,
                base_units=base_units_value,
                entry_price=dec(view.get("entry_price"), "entry_price"),
                entry_timestamp=timestamp(view.get("entry_timestamp"), "entry_timestamp"),
                entry_quote_rate=dec(view.get("entry_quote_rate"), "entry_quote_rate"),
                entry_base_rate=dec(view.get("entry_base_rate"), "entry_base_rate"),
                notional_account=dec(view.get("notional_account"), "notional_account"),
                margin_account=dec(view.get("margin_account"), "margin_account"),
                costs=costs_from_view(view.get("costs")),
                # Older snapshots predate position identity; they belong to the
                # only account the snapshot can describe (restore validates the
                # account currency), so the owning account is not guessed.
                account_id=str(view.get("account_id", "") or self.account.account_id),
                original_quantity=original_quantity_value,
                original_base_units=original_base_units_value,
                entry_costs_total_account=dec(
                    view.get(
                        "entry_costs_total_account",
                        costs_from_view(view.get("costs")).total_account,
                    ),
                    "entry_costs_total_account",
                ),
            )

        def trade_from_view(view: Any) -> ClosedTrade:
            if not isinstance(view, Mapping):
                raise ValueError("invalid_trade_snapshot")
            try:
                side = OrderSide(str(view.get("side", "")).upper())
            except ValueError as exc:
                raise ValueError("invalid_trade_side") from exc
            return ClosedTrade(
                position_id=str(view.get("position_id", "")),
                pair=str(view.get("pair", "")),
                side=side,
                quantity=dec(view.get("quantity"), "trade_quantity"),
                entry_price=dec(view.get("entry_price"), "entry_price"),
                exit_price=dec(view.get("exit_price"), "exit_price"),
                opened_at=timestamp(view.get("opened_at"), "opened_at"),
                closed_at=timestamp(view.get("closed_at"), "closed_at"),
                realized_pnl_account=dec(view.get("realized_pnl_account"), "realized_pnl_account"),
                costs=costs_from_view(view.get("costs")),
                # Older snapshots predate the entry-cost field; they restore
                # with zero rather than failing the whole recovery.
                entry_costs_account=dec(
                    view.get("entry_costs_account", 0), "entry_costs_account"
                ),
                closed_base_units=dec(
                    view.get("closed_base_units", 0), "closed_base_units"
                ),
                partial=view.get("partial") is True,
            )

        account_currency = str(snapshot.get("account_currency", ""))
        if account_currency != self.account.account_currency_code:
            raise ValueError("account_currency_snapshot_mismatch")
        cash_view = snapshot.get("cash_by_currency")
        positions_view = snapshot.get("open_positions")
        trades_view = snapshot.get("closed_trades")
        if not isinstance(cash_view, Mapping) or not isinstance(positions_view, (list, tuple)):
            raise ValueError("incomplete_portfolio_snapshot")
        if not isinstance(trades_view, (list, tuple)):
            raise ValueError("incomplete_portfolio_snapshot")
        cash = {
            str(currency).upper(): dec(amount, "cash_amount")
            for currency, amount in cash_view.items()
        }
        positions_list = [position_from_view(view) for view in positions_view]
        trades_list = [trade_from_view(view) for view in trades_view]
        positions = {position.position_id: position for position in positions_list}
        if len(positions) != len(positions_list):
            raise ValueError("duplicate_position_id_in_snapshot")
        self.cash_by_currency = cash
        self.positions = positions
        self.closed_trades = trades_list
        self.realized_pnl_today_account = dec(
            snapshot.get("realized_pnl_today_account"), "realized_pnl_today_account"
        )
        self.trades_today = int(snapshot.get("trades_today", 0))
        stored_day = snapshot.get("daily_counter_day")
        if isinstance(stored_day, str) and stored_day.strip():
            try:
                self.daily_counter_day = date.fromisoformat(stored_day.strip())
            except ValueError as exc:
                raise ValueError("invalid_daily_counter_day") from exc
        elif isinstance(stored_day, date) and not isinstance(stored_day, datetime):
            self.daily_counter_day = stored_day
        else:
            # Snapshots written before the counters were day-scoped carry no
            # day.  They stay as they are -- an unknown day can only make the
            # gate stricter -- and are re-attributed to the first trading day
            # that records a fill.
            self.daily_counter_day = None
        stored_basis = snapshot.get("daily_counter_basis")
        if isinstance(stored_basis, str) and stored_basis.strip():
            self.daily_counter_basis = stored_basis.strip()
        self.cost_total_account = dec(snapshot.get("cost_total_account"), "cost_total_account")
        self.rollover_total_account = dec(
            snapshot.get("rollover_total_account"), "rollover_total_account"
        )
