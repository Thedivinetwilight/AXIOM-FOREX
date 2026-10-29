"""Shared signal-to-paper-order planner for backtests and live-shaped paper runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.calendar.trading import CalendarState, TradingCalendar
from app.common.types import OrderSide, TradingMode
from app.data.models import ForexQuote, ForexQuoteBar
from app.risk.models import CloseIntent, GateContext, OrderRequest
from app.universe.specs import CurrencyPairSpec

from axiom_forex.risk.context import ForexExecutionContextFactory
from axiom_forex.strategies.base import Signal

from .config import ExecutionConfig


@dataclass(frozen=True)
class ExecutionPlan:
    request: OrderRequest
    context: GateContext
    fill_price: Decimal
    fill_at: datetime
    action: str
    position_id: str | None
    quantity: Decimal
    signal: Signal
    decision_index: int
    execution_index: int
    calendar_state: CalendarState

    def audit_view(self) -> dict[str, Any]:
        return {
            "request": self.request.audit_view(),
            "fill_price": self.fill_price,
            "fill_at": self.fill_at,
            "action": self.action,
            "position_id": self.position_id,
            "quantity": self.quantity,
            "signal": self.signal.value,
            "decision_index": self.decision_index,
            "execution_index": self.execution_index,
            "calendar": self.calendar_state.audit_view(),
        }


@dataclass(frozen=True)
class ExecutionRejection:
    code: str
    reason: str
    decision_index: int
    request_id: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "reason": self.reason,
            "decision_index": self.decision_index,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class ForexExecutionPlanner:
    spec: CurrencyPairSpec
    calendar: TradingCalendar
    config: ExecutionConfig
    context_factory: ForexExecutionContextFactory

    @staticmethod
    def quote_from_bar(bar: ForexQuoteBar, *, quote_id: str) -> ForexQuote:
        return ForexQuote(
            pair=bar.pair,
            timestamp=bar.timestamp,
            bid=bar.bid.open,
            ask=bar.ask.open,
            source=bar.source,
            quote_id=quote_id,
            received_at=bar.timestamp + timedelta(milliseconds=100),
            metadata={"fixture": "integrated-paper-planner"},
        )

    def _action(
        self, signal: Signal, positions: Mapping[str, Any]
    ) -> tuple[str, OrderSide, str | None] | None:
        if signal == Signal.EXIT and positions:
            position_id = next(iter(positions))
            position = positions[position_id]
            side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
            return "CLOSE", side, position_id
        if signal == Signal.BUY:
            if positions:
                position_id = next(iter(positions))
                position = positions[position_id]
                if position.side == OrderSide.SELL:
                    return "CLOSE", OrderSide.BUY, position_id
                return None
            if len(positions) >= self.config.max_open_positions:
                return None
            return "OPEN", OrderSide.BUY, None
        if signal == Signal.SELL:
            if positions:
                position_id = next(iter(positions))
                position = positions[position_id]
                if position.side == OrderSide.BUY:
                    return "CLOSE", OrderSide.SELL, position_id
                return None
            if not self.config.allow_short or len(positions) >= self.config.max_open_positions:
                return None
            return "OPEN", OrderSide.SELL, None
        return None

    def plan(
        self,
        *,
        signal: Signal,
        positions: Mapping[str, Any],
        bars: Sequence[ForexQuoteBar],
        decision_index: int,
        run_id: str,
        strategy_id: str,
    ) -> ExecutionPlan | ExecutionRejection | None:
        action = self._action(signal, positions)
        if action is None:
            return None
        execution_index = decision_index + 1
        if execution_index >= len(bars):
            return None
        kind, side, position_id = action
        execution_bar = bars[execution_index]
        calendar_state = self.calendar.status(
            self.spec.canonical_pair or "", execution_bar.timestamp
        )
        request_id = f"{run_id}:{decision_index}:{kind}"
        if not calendar_state.is_open:
            return ExecutionRejection(
                "MARKET_CLOSED",
                calendar_state.status.value,
                decision_index,
                request_id,
            )
        quote = self.quote_from_bar(execution_bar, quote_id=request_id)
        close_intent = None
        if kind == "OPEN":
            quantity = self.config.quantity
        else:
            position = positions[position_id or ""]
            # Close the position's remaining size.  ``remaining_quantity`` is the
            # ledger's field; the fallback keeps older position objects working
            # and never enlarges the close.
            quantity = getattr(position, "remaining_quantity", None) or position.quantity
            close_intent = CloseIntent(
                position_id=str(getattr(position, "position_id", position_id)),
                account_id=self.context_factory.account.account_id,
                quantity=quantity,
                pair=self.spec.canonical_pair,
                quantity_unit=self.spec.quantity_unit_value,
            )
        request = OrderRequest(
            request_id=request_id,
            provider_id=self.config.provider_id,
            provider_symbol=self.config.provider_symbol,
            side=side,
            quantity=quantity,
            target_mode=TradingMode.PAPER,
            evaluated_at=execution_bar.timestamp,
            quote=quote,
            strategy_id=strategy_id,
            close_intent=close_intent,
        )
        context = self.context_factory.build(
            at=execution_bar.timestamp, bar=execution_bar
        )
        return ExecutionPlan(
            request=request,
            context=context,
            fill_price=quote.executable_price(side),
            fill_at=execution_bar.timestamp,
            action=kind,
            position_id=position_id,
            quantity=quantity,
            signal=signal,
            decision_index=decision_index,
            execution_index=execution_index,
            calendar_state=calendar_state,
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.spec.canonical_pair,
            "calendar_id": self.calendar.calendar_id,
            "config": self.config.audit_view(),
            "context_factory": self.context_factory.audit_view(),
        }
