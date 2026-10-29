"""Idempotent local paper fills with unresolved-order no-resubmit behavior."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterator, Mapping, Sequence

from app.audit.log import AuditLog, make_decision_id
from app.common.types import OrderSide
from app.common.validation import as_utc, parse_decimal
from app.execution.broker import (
    BrokerCapabilities,
    LocalPaperBroker,
    PaperOrderReceipt,
    TradingBlocked,
)
from app.data.models import ForexQuote
from app.risk.models import GateContext, OrderRequest, UnresolvedOrder

from axiom_forex.portfolio.fx import MissingConversionRate
from axiom_forex.portfolio.ledger import ForexPortfolio

from .reservations import (
    KIND_CLOSE,
    KIND_ENTRY,
    OrderReservationBook,
    ReservationRequest,
    from_units,
    to_units_ceiling,
)
from .settlement import (
    LedgerSettlement,
    commit_settlement,
    load_committed_settlement,
    settlement_event,
    settlement_matches,
)
from .states import FINAL_STATES, OrderState


def _decimal(value: Any, name: str) -> Decimal:
    parsed, error = parse_decimal(value, name)
    if error or parsed is None:
        raise ValueError(error or f"invalid_{name}")
    return parsed


def _timestamp(value: Any, name: str) -> datetime:
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


@dataclass(frozen=True)
class RecoveredOrderIntent:
    """Audit-safe view of an approved local intent after process restart."""

    view: dict[str, Any]

    @property
    def evaluated_at(self) -> datetime:
        return _timestamp(self.view.get("evaluated_at"), "evaluated_at")

    def audit_view(self) -> dict[str, Any]:
        return dict(self.view)


def _receipt_from_audit_view(view: Any) -> PaperOrderReceipt:
    if not isinstance(view, Mapping):
        raise ValueError("invalid_receipt_snapshot")
    if view.get("live") is not False:
        raise ValueError("recovered_receipt_is_not_paper")
    mode = str(view.get("mode", "")).upper()
    status = str(view.get("status", "")).upper()
    simulation_id = str(view.get("simulation_id", ""))
    if mode != "PAPER" or status != "SIMULATED_INTENT" or not simulation_id:
        raise ValueError("invalid_receipt_snapshot")
    capability_view = view.get("capabilities")
    intent_view = view.get("intent")
    if not isinstance(capability_view, Mapping) or not isinstance(intent_view, Mapping):
        raise ValueError("incomplete_receipt_snapshot")
    capabilities = BrokerCapabilities(
        broker_id=str(capability_view.get("broker_id", "")),
        capability_version=str(capability_view.get("capability_version", "")),
        execution_mode=str(capability_view.get("execution_mode", "")),
        supports_live_orders=capability_view.get("supports_live_orders") is True,
        supports_partial_fills=capability_view.get("supports_partial_fills") is True,
        order_types=frozenset(
            str(item) for item in capability_view.get("order_types", ())
        ),
        calendar_id=str(capability_view.get("calendar_id", "")),
    )
    if not capabilities.validate().valid:
        raise ValueError("invalid_recovered_capabilities")
    return PaperOrderReceipt(
        simulation_id=simulation_id,
        status=status,
        live=False,
        mode=mode,
        created_at=_timestamp(view.get("created_at"), "receipt_created_at"),
        intent=RecoveredOrderIntent(dict(intent_view)),
        decision_id=str(view.get("decision_id", "")),
        capabilities=capabilities,
    )


@dataclass(frozen=True)
class PaperOrderRecord:
    request_id: str
    simulation_id: str
    state: str
    created_at: datetime
    updated_at: datetime
    reason: str = ""
    receipt: PaperOrderReceipt | None = None
    #: Stable digest of the request payload (side, quantity, close target,
    #: evaluation instant).  Rejected orders carry no receipt, so this is the
    #: only way to tell "the same order again" from "a different order reusing
    #: the id".  Empty on records written before it existed.
    payload_fingerprint: str = ""

    @property
    def is_unresolved(self) -> bool:
        return self.state not in FINAL_STATES

    def audit_view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "simulation_id": self.simulation_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "is_unresolved": self.is_unresolved,
            "payload_fingerprint": self.payload_fingerprint,
            "receipt": self.receipt.audit_view() if self.receipt else None,
        }

    @classmethod
    def from_audit_view(cls, view: Mapping[str, Any]) -> "PaperOrderRecord":
        if not isinstance(view, Mapping):
            raise ValueError("invalid_order_snapshot")
        request_id = str(view.get("request_id", ""))
        state = str(view.get("state", "")).upper()
        if not request_id or state not in {item.value for item in OrderState}:
            raise ValueError("invalid_order_snapshot")
        receipt_view = view.get("receipt")
        receipt = (
            _receipt_from_audit_view(receipt_view) if receipt_view is not None else None
        )
        return cls(
            request_id=request_id,
            simulation_id=str(view.get("simulation_id", "")),
            state=state,
            created_at=_timestamp(view.get("created_at"), "order_created_at"),
            updated_at=_timestamp(view.get("updated_at"), "order_updated_at"),
            reason=str(view.get("reason", "")),
            receipt=receipt,
            payload_fingerprint=str(view.get("payload_fingerprint", "")),
        )


@dataclass(frozen=True)
class PaperFill:
    simulation_id: str
    request_id: str
    action: str
    position_id: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    realized_pnl_account: Decimal
    cost_total_account: Decimal
    financing_account: Decimal
    rollover_events: tuple[dict[str, Any], ...]

    def audit_view(self) -> dict[str, Any]:
        return {
            "simulation_id": self.simulation_id,
            "request_id": self.request_id,
            "action": self.action,
            "position_id": self.position_id,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": self.price,
            "filled_at": self.filled_at,
            "realized_pnl_account": self.realized_pnl_account,
            "cost_total_account": self.cost_total_account,
            "financing_account": self.financing_account,
            "rollover_events": list(self.rollover_events),
        }

    @classmethod
    def from_audit_view(cls, view: Mapping[str, Any]) -> "PaperFill":
        if not isinstance(view, Mapping):
            raise ValueError("invalid_fill_snapshot")
        try:
            side = OrderSide(str(view.get("side", "")).upper())
        except ValueError as exc:
            raise ValueError("invalid_fill_side") from exc
        action = str(view.get("action", "")).upper()
        if action not in {"OPEN", "CLOSE"}:
            raise ValueError("invalid_fill_action")
        events = view.get("rollover_events", ())
        if not isinstance(events, (list, tuple)):
            raise ValueError("invalid_rollover_events")
        return cls(
            simulation_id=str(view.get("simulation_id", "")),
            request_id=str(view.get("request_id", "")),
            action=action,
            position_id=str(view.get("position_id", "")),
            side=side,
            quantity=_decimal(view.get("quantity"), "fill_quantity"),
            price=_decimal(view.get("price"), "fill_price"),
            filled_at=_timestamp(view.get("filled_at"), "filled_at"),
            realized_pnl_account=_decimal(
                view.get("realized_pnl_account"), "realized_pnl_account"
            ),
            cost_total_account=_decimal(view.get("cost_total_account"), "cost_total_account"),
            financing_account=_decimal(view.get("financing_account"), "financing_account"),
            rollover_events=tuple(dict(item) for item in events if isinstance(item, Mapping)),
        )


@dataclass(frozen=True)
class _SettlementRequest:
    """A validated settlement attempt, before the ledger is touched."""

    simulation_id: str
    request_id: str
    action: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    at: datetime
    position_id: str | None
    cutoffs: tuple[datetime, ...] = ()


class PaperExecutionEngine:
    """Idempotent local paper execution.

    ``reservations`` makes close quantities and entry exposure durable.  When it
    is provided, every submit/cancel/settle writes its reservation and its order
    row in one SQLite transaction, and the limits are enforced there, so two
    engine instances sharing a database cannot both take the same quantity.
    Without it the engine falls back to its own process-local bookkeeping, which
    is only safe for a single engine in a single process (unit fixtures).
    """

    def __init__(
        self,
        *,
        broker: LocalPaperBroker,
        portfolio: ForexPortfolio,
        audit_log: AuditLog,
        reservations: OrderReservationBook | None = None,
    ):
        self.broker = broker
        self.portfolio = portfolio
        self.audit_log = audit_log
        #: Durable reservation book, or ``None`` for process-local bookkeeping.
        self.reservations = reservations
        if reservations is not None and not reservations.account_currency:
            # The cap must be comparable to what the ledger actually computes,
            # so the account currency comes from the book this engine writes to,
            # never from a default.
            reservations.account_currency = portfolio.account.account_currency_code
        self._by_request: dict[str, PaperOrderRecord] = {}
        self._by_simulation: dict[str, PaperOrderRecord] = {}
        self._fills: dict[str, PaperFill] = {}

    @property
    def durable_reservations(self) -> bool:
        return self.reservations is not None

    def _remember(self, record: PaperOrderRecord) -> PaperOrderRecord:
        self._by_request[record.request_id] = record
        if record.simulation_id:
            self._by_simulation[record.simulation_id] = record
        return record

    def restore(
        self,
        records: Sequence[Mapping[str, Any]],
        fills: Sequence[Mapping[str, Any]],
    ) -> None:
        """Atomically recover idempotency state from sanitized SQLite views."""
        by_request: dict[str, PaperOrderRecord] = {}
        by_simulation: dict[str, PaperOrderRecord] = {}
        recovered_fills: dict[str, PaperFill] = {}
        for view in records:
            record = PaperOrderRecord.from_audit_view(view)
            if record.request_id in by_request:
                raise ValueError("duplicate_recovered_request_id")
            by_request[record.request_id] = record
            if record.simulation_id:
                by_simulation[record.simulation_id] = record
        for view in fills:
            fill = PaperFill.from_audit_view(view)
            if fill.simulation_id in recovered_fills:
                raise ValueError("duplicate_recovered_fill_id")
            record = by_simulation.get(fill.simulation_id)
            if record is None or record.state != OrderState.FILLED.value:
                raise ValueError("recovered_fill_without_final_order")
            if record.request_id != fill.request_id:
                raise ValueError("recovered_fill_request_mismatch")
            recovered_fills[fill.simulation_id] = fill
        self._by_request = by_request
        self._by_simulation = by_simulation
        self._fills = recovered_fills

    def records(self) -> tuple[PaperOrderRecord, ...]:
        return tuple(sorted(self._by_request.values(), key=lambda item: item.created_at))

    def order_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(record.audit_view() for record in self.records())

    def fills(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._fills[key].audit_view() for key in sorted(self._fills))

    def pending_close_quantities(self) -> dict[str, Decimal]:
        """Quantity already promised to closes that have not settled yet.

        A close is reserved from the moment its simulated intent exists and is
        released when the record reaches a final state (filled, rejected, or
        cancelled).  An unresolved record keeps its reservation: whether its
        fill landed is unknown, so the safest reading is that the quantity is
        still spoken for.  This is what makes two concurrent closes unable to
        consume the same position twice.

        With a durable reservation book the answer comes from SQLite, so it
        includes promises taken by other engine instances on the same database;
        without one it is this process's own in-memory view.
        """

        if self.reservations is not None:
            return self.reservations.pending_close_quantities()
        reserved: dict[str, Decimal] = {}
        for record in self.records():
            if record.state in FINAL_STATES:
                continue
            view = self._intent_view(record)
            if view is None:
                continue
            position_id = view.get("close_position_id")
            if not isinstance(position_id, str) or not position_id.strip():
                continue
            quantity, error = parse_decimal(view.get("quantity"), "close_quantity")
            if error or quantity is None or quantity <= 0:
                raise ValueError("invalid_pending_close_intent")
            reserved[position_id] = reserved.get(position_id, Decimal(0)) + quantity
        return reserved

    @staticmethod
    def _payload_fingerprint(request: OrderRequest) -> str:
        """Digest of everything that makes this a particular order."""

        return make_decision_id(
            "paper-request",
            request.side_value,
            str(request.quantity_decimal()),
            request.close_intent.position_id if request.close_intent else "",
            request.evaluated_at_utc().isoformat(),
        )

    @staticmethod
    def _intent_view(record: PaperOrderRecord) -> dict[str, Any] | None:
        if record.receipt is None:
            return None
        intent_view = getattr(record.receipt.intent, "audit_view", None)
        view = intent_view() if callable(intent_view) else None
        return dict(view) if isinstance(view, Mapping) else None

    def _reserve_close(self, request: OrderRequest) -> None:
        """Fail closed if a close would over-commit a live ledger position.

        The gate validated against the context it was given.  That context may
        be stale (another close settled, or was submitted, in between), so the
        ledger is re-checked here against every *other* in-flight close before
        the order is simulated.
        """

        intent = request.close_intent
        if intent is None:
            return
        quantity, error = parse_decimal(intent.quantity, "close_quantity")
        if error or quantity is None or quantity <= 0:
            raise ValueError("invalid_close_quantity")
        for record in self.records():
            if record.request_id == request.request_id:
                continue
            view = self._intent_view(record)
            if view is None or view.get("close_position_id") != intent.position_id:
                continue
            if record.state in FINAL_STATES:
                continue
            reserved, reserved_error = parse_decimal(
                view.get("quantity"), "close_quantity"
            )
            if reserved_error or reserved is None:
                raise ValueError("invalid_pending_close_intent")
            quantity += reserved
        position = self.portfolio.positions.get(intent.position_id)
        if position is None:
            raise ValueError("close_position_not_open")
        if quantity > position.quantity:
            raise ValueError("close_quantity_exceeds_position")

    def unresolved_orders(self) -> tuple[UnresolvedOrder, ...]:
        return tuple(
            UnresolvedOrder(
                record.request_id,
                record.state,
                updated_at=record.updated_at,
            )
            for record in self.records()
            if record.is_unresolved
        )

    @staticmethod
    def _request_payload_matches(
        record: PaperOrderRecord, request: OrderRequest
    ) -> bool:
        """True when an existing record was created from this same request.

        Idempotency is keyed by ``request_id``; the key must not silently absorb
        a *different* order.  The stored receipt carries the simulated intent,
        so side, quantity, and evaluation instant can be compared exactly.
        """

        if record.receipt is None:
            # A rejected or cancelled record produced no order.  Returning it
            # again is only correct when it was the *same* request: the stored
            # fingerprint (absent on older records) decides that.
            if record.payload_fingerprint:
                return record.payload_fingerprint == PaperExecutionEngine._payload_fingerprint(
                    request
                )
            return True
        intent_view = getattr(record.receipt.intent, "audit_view", None)
        intent = intent_view() if callable(intent_view) else None
        # Any field that cannot be read is treated as a mismatch, not as a
        # match: a reused request id whose stored payload cannot be compared
        # might be a different order, and answering "already done" to it would
        # silently drop the second order instead of refusing it.
        if not isinstance(intent, Mapping):
            return False
        if str(intent.get("side", "")).upper() != request.side_value:
            return False
        try:
            stored_quantity = _decimal(intent.get("quantity"), "intent_quantity")
            if stored_quantity != request.quantity_decimal():
                return False
        except (ValueError, AttributeError, TypeError):
            return False
        try:
            stored_at = _timestamp(intent.get("evaluated_at"), "intent_evaluated_at")
            if stored_at != request.evaluated_at_utc():
                return False
        except (ValueError, AttributeError, TypeError):
            return False
        # The close target is part of the payload identity.  Without this, a
        # retry that reuses the request id but names a different position would
        # be answered as a duplicate and silently dropped.
        stored_close = intent.get("close_position_id")
        stored_close = stored_close if isinstance(stored_close, str) else None
        if stored_close != (request.close_intent.position_id if request.close_intent else None):
            return False
        return True

    def _existing_record(self, request: OrderRequest) -> PaperOrderRecord | None:
        existing = self._by_request.get(request.request_id)
        if existing is None:
            return None
        if not self._request_payload_matches(existing, request):
            raise ValueError("request_id_reused_with_different_payload")
        return existing

    def _record_rejected(
        self, request: OrderRequest, created_at: datetime, reason: str
    ) -> PaperOrderRecord:
        """Record a refused order so the attempt is auditable and idempotent."""

        record = PaperOrderRecord(
            request_id=request.request_id,
            simulation_id="",
            state=OrderState.REJECTED.value,
            created_at=created_at,
            updated_at=created_at,
            reason=reason,
            payload_fingerprint=self._payload_fingerprint(request),
        )
        self._by_request[request.request_id] = record
        if self.reservations is not None:
            # The refusal itself is a durable idempotency record, and it lands
            # in the same unit as its audit event: a second process reusing
            # this request id must see the same answer, with the same evidence.
            with self._durable_unit():
                self.reservations.persist_order(record.audit_view())
                self.audit_log.record(
                    {
                        "event_type": "PAPER_ORDER_REJECTED",
                        "request_id": request.request_id,
                        "reason": reason,
                        "live": False,
                    }
                )
        else:
            self.audit_log.record(
                {
                    "event_type": "PAPER_ORDER_REJECTED",
                    "request_id": request.request_id,
                    "reason": reason,
                    "live": False,
                }
            )
        return record

    def _effective_context(
        self, context: GateContext
    ) -> GateContext:
        """Add this engine's own in-doubt orders to the gate's context."""

        combined_unresolved = list(context.unresolved_orders)
        known_ids = {order.order_id for order in combined_unresolved}
        for order in self.unresolved_orders():
            if order.order_id not in known_ids:
                combined_unresolved.append(order)
                known_ids.add(order.order_id)
        return replace(context, unresolved_orders=tuple(combined_unresolved))

    def _entry_exposure(self, request: OrderRequest) -> Decimal | None:
        """Entry notional in the pair's quote currency, or ``None``.

        The executable side of the caller's own quote is used (ask for a buy,
        bid for a sell): no invented mid price, no provider assumption.  A
        request without a complete quote cannot be priced, which the durable
        book treats as a fail-closed rejection when an exposure cap is
        configured.
        """

        quote = request.quote
        if not isinstance(quote, ForexQuote) or not quote.is_complete:
            return None
        try:
            bid, ask = quote.prices()
            price = ask if request.side_value == OrderSide.BUY.value else bid
            if price <= 0:
                return None
            # Notional in *account* currency, using the ledger's own timestamped
            # conversion book -- the same arithmetic the position would carry.
            # A missing rate fails closed (``None``), it is never defaulted to 1.
            notional = self.portfolio.entry_notional_account(
                quantity=request.quantity_decimal(),
                price=price,
                at=request.evaluated_at_utc(),
            )
        except (ValueError, MissingConversionRate):
            # No rate, no price, no exposure: the entry is refused as
            # unpriced rather than reserved at zero.
            return None
        # The notional is a product, so it can land off the store's 1e-8 grid.
        # Round it up: under-stating exposure would be the unsafe direction.
        return from_units(to_units_ceiling(notional, "exposure"))

    def _reservation_request(self, request: OrderRequest) -> ReservationRequest:
        intent = request.close_intent
        if intent is not None:
            return ReservationRequest(
                request_id=request.request_id,
                kind=KIND_CLOSE,
                quantity=request.quantity_decimal(),
                position_id=intent.position_id,
                position_quantity=self._position_quantity(intent.position_id),
                payload_fingerprint=self._payload_fingerprint(request),
            )
        return ReservationRequest(
            request_id=request.request_id,
            kind=KIND_ENTRY,
            quantity=request.quantity_decimal(),
            exposure=self._entry_exposure(request),
            payload_fingerprint=self._payload_fingerprint(request),
        )

    def _position_quantity(self, position_id: str) -> Decimal:
        position = self.portfolio.positions.get(position_id)
        if position is None:
            raise ValueError("close_position_not_open")
        return position.quantity

    def _submit_durable(
        self,
        request: OrderRequest,
        context: GateContext,
        created_at: datetime,
    ) -> PaperOrderRecord:
        """Submit under one SQLite transaction: reserve, re-check, persist.

        The local in-doubt check runs first so a single process reports the
        same refusal it always did; the durable check inside the transaction
        then covers every *other* connection sharing the database.
        """

        if self.unresolved_orders():
            return self._record_rejected(
                request, created_at, "UNRESOLVED_ORDER_PRESENT"
            )
        try:
            reservation = self._reservation_request(request)
            # Refuse here -- auditable and idempotent -- anything the durable
            # book cannot represent exactly (e.g. a quantity finer than 1e-8),
            # instead of failing inside the transaction.
            reservation.validate()
        except ValueError as exc:
            return self._record_rejected(request, created_at, str(exc))
        effective_context = self._effective_context(context)

        def simulate() -> Mapping[str, Any]:
            receipt = self.broker.simulate_order_intent(request, effective_context)
            record = PaperOrderRecord(
                request_id=request.request_id,
                simulation_id=receipt.simulation_id,
                state=OrderState.SIMULATED.value,
                created_at=created_at,
                updated_at=created_at,
                receipt=receipt,
                payload_fingerprint=self._payload_fingerprint(request),
            )
            payload = record.audit_view()
            self.audit_log.record(
                {
                    "event_type": "PAPER_ORDER_SIMULATED",
                    "request_id": request.request_id,
                    "simulation_id": receipt.simulation_id,
                    "reservation": {
                        "kind": reservation.kind,
                        "position_id": reservation.position_id,
                        "quantity": reservation.quantity,
                        "exposure": reservation.exposure,
                        "durable": True,
                    },
                    "live": False,
                }
            )
            return payload

        book = self.reservations
        if book is None:  # pragma: no cover - guarded by the caller
            raise ValueError("missing_reservation_book")
        with self._durable_unit():
            outcome = book.submit(reservation, at=created_at, simulate=simulate)
        if outcome.duplicate:
            stored = PaperOrderRecord.from_audit_view(dict(outcome.order or {}))
            if not self._request_payload_matches(stored, request):
                raise ValueError("request_id_reused_with_different_payload")
            self._remember(stored)
            return stored
        if outcome.rejected:
            return self._record_rejected(request, created_at, outcome.reason)
        record = PaperOrderRecord.from_audit_view(dict(outcome.order or {}))
        return self._remember(record)

    def submit(
        self,
        request: OrderRequest,
        context: GateContext,
    ) -> PaperOrderRecord:
        existing = self._existing_record(request)
        if existing is not None:
            return existing
        validation = request.validate()
        if not validation.valid:
            raise ValueError("invalid_order_request: " + validation.reason)
        # The record is stamped with the request's own evaluation instant.  A
        # fallback to wall-clock time would hide an invalid timestamp and write
        # an untraceable creation time into the audit trail.
        created_at = request.evaluated_at_utc()
        if self.reservations is not None:
            return self._submit_durable(request, context, created_at)
        if self.unresolved_orders():
            # An unresolved order means the book may not reflect reality; no new
            # order -- least of all a close -- may be added on top of it.
            return self._record_rejected(
                request, created_at, "UNRESOLVED_ORDER_PRESENT"
            )
        try:
            self._reserve_close(request)
        except ValueError as exc:
            # The gate validated against the context it was handed; that context
            # can be stale by the time the order arrives (another close already
            # reserved the quantity).  Refuse it as a rejected order so the
            # attempt is auditable and idempotent, never a silent partial close.
            return self._record_rejected(request, created_at, str(exc))
        effective_context = self._effective_context(context)
        try:
            receipt = self.broker.simulate_order_intent(request, effective_context)
        except TradingBlocked as exc:
            return self._record_rejected(request, created_at, exc.reason)
        record = PaperOrderRecord(
            request_id=request.request_id,
            simulation_id=receipt.simulation_id,
            state=OrderState.SIMULATED.value,
            created_at=created_at,
            updated_at=created_at,
            receipt=receipt,
            payload_fingerprint=self._payload_fingerprint(request),
        )
        self._by_request[request.request_id] = record
        self._by_simulation[receipt.simulation_id] = record
        self.audit_log.record(
            {
                "event_type": "PAPER_ORDER_SIMULATED",
                "request_id": request.request_id,
                "simulation_id": receipt.simulation_id,
                "live": False,
            }
        )
        return record

    def cancel(
        self,
        request_id: str,
        *,
        cancelled_at: datetime,
        reason: str = "cancelled_by_operator",
    ) -> PaperOrderRecord:
        """Cancel a not-yet-filled order and release its close reservation.

        Cancelling is idempotent, never invents a timestamp (the caller supplies
        the instant), and refuses to touch an order that already filled: a fill
        has changed the book, so "cancelling" it would desynchronise the ledger
        from its own history.
        """

        record = self._by_request.get(request_id)
        if record is None:
            raise ValueError("unknown_request_id")
        if record.state == OrderState.FILLED.value:
            raise ValueError("cannot_cancel_filled_order")
        if record.state == OrderState.CANCELLED.value:
            return record
        at_utc, at_error = as_utc(cancelled_at, "cancelled_at")
        if at_error or at_utc is None:
            raise ValueError("invalid_cancelled_at")
        if at_utc < record.updated_at:
            raise ValueError("cancel_before_order_update")
        cancelled = replace(
            record,
            state=OrderState.CANCELLED.value,
            updated_at=at_utc,
            reason=reason,
        )
        if self.reservations is not None:
            # The order row and the release travel together: a caller that
            # cancels in one process frees the quantity for every process.
            with self._durable_unit():
                self.reservations.finalize(
                    order_payload=cancelled.audit_view(),
                    state=OrderState.CANCELLED.value,
                    at=at_utc,
                    release=True,
                )
                self.audit_log.record(
                    {
                        "event_type": "PAPER_ORDER_CANCELLED",
                        "request_id": request_id,
                        "simulation_id": cancelled.simulation_id,
                        "reason": reason,
                        "cancelled_at": at_utc,
                        "live": False,
                    }
                )
        else:
            self.audit_log.record(
                {
                    "event_type": "PAPER_ORDER_CANCELLED",
                    "request_id": request_id,
                    "simulation_id": cancelled.simulation_id,
                    "reason": reason,
                    "cancelled_at": at_utc,
                    "live": False,
                }
            )
        self._by_request[request_id] = cancelled
        if cancelled.simulation_id:
            self._by_simulation[cancelled.simulation_id] = cancelled
        return cancelled

    # -- settlement -----------------------------------------------------

    @contextmanager
    def _durable_unit(self) -> Iterator[None]:
        """Open one database transaction whose audit events wait for the commit.

        The audit log owns the unit when it can (``DurableAuditLog.atomic``):
        it opens the transaction, stages the events, commits, and only then lets
        them into the in-memory cache.  A plain in-memory log has nothing to
        commit, so the block is a no-op and the legacy path stays unchanged.
        """

        atomic = getattr(self.audit_log, "atomic", None)
        if callable(atomic) and self.reservations is not None:
            with atomic():
                yield
        else:
            yield

    def _parse_settlement(
        self,
        record: PaperOrderRecord,
        *,
        fill_price: Any,
        fill_at: Any,
        action: str,
        side: OrderSide | str,
        quantity: Any,
        position_id: str | None,
        cutoffs: Sequence[datetime],
    ) -> "_SettlementRequest":
        """Validate a settlement attempt against the approved intent.

        Nothing here mutates the book: every check that can refuse a settlement
        runs before the ledger is touched, so a refusal leaves no half-applied
        position behind.
        """

        side_value = side.value if isinstance(side, OrderSide) else str(side).upper()
        try:
            order_side = OrderSide(side_value)
        except ValueError as exc:
            raise ValueError("invalid_side") from exc
        quantity_decimal, quantity_error = parse_decimal(quantity, "quantity")
        price_decimal, price_error = parse_decimal(fill_price, "fill_price")
        at_utc, at_error = as_utc(fill_at, "fill_at")
        if quantity_error or quantity_decimal is None or quantity_decimal <= 0:
            raise ValueError("invalid_quantity")
        if price_error or price_decimal is None or price_decimal <= 0:
            raise ValueError("invalid_fill_price")
        if at_error or at_utc is None:
            raise ValueError("invalid_fill_at")
        action_value = str(action or "").upper()
        intent_view = self._intent_view(record)
        approved_close = (
            intent_view.get("close_position_id") if intent_view is not None else None
        )
        if action_value == "OPEN" and approved_close:
            raise ValueError("entry_with_close_intent")
        if action_value == "CLOSE":
            if not position_id:
                raise ValueError("missing_position_id")
            if not isinstance(approved_close, str) or not approved_close.strip():
                # Never infer a close from side: an order without an approved
                # close target cannot settle as one.
                raise ValueError("close_without_approved_position")
            if approved_close != position_id:
                raise ValueError("close_position_mismatch")
            approved_quantity, approved_error = parse_decimal(
                intent_view.get("quantity"), "close_quantity"
            )
            if approved_error or approved_quantity is None or approved_quantity <= 0:
                raise ValueError("invalid_approved_close_quantity")
            if quantity_decimal != approved_quantity:
                raise ValueError("close_quantity_mismatch")
        elif action_value != "OPEN":
            raise ValueError("invalid_action")
        return _SettlementRequest(
            simulation_id=record.simulation_id,
            request_id=record.request_id,
            action=action_value,
            side=order_side,
            quantity=quantity_decimal,
            price=price_decimal,
            at=at_utc,
            position_id=position_id,
            cutoffs=tuple(cutoffs),
        )

    def settle(
        self,
        simulation_id: str,
        *,
        fill_price: Decimal,
        fill_at: datetime,
        action: str,
        side: OrderSide | str,
        quantity: Decimal,
        position_id: str | None = None,
        cutoffs: Sequence[datetime] = (),
    ) -> PaperFill:
        """Apply one fill, atomically when a durable book is configured."""

        if self.reservations is not None:
            return self._settle_durable(
                simulation_id,
                fill_price=fill_price,
                fill_at=fill_at,
                action=action,
                side=side,
                quantity=quantity,
                position_id=position_id,
                cutoffs=cutoffs,
            )
        return self._settle_in_memory(
            simulation_id,
            fill_price=fill_price,
            fill_at=fill_at,
            action=action,
            side=side,
            quantity=quantity,
            position_id=position_id,
            cutoffs=cutoffs,
        )

    def _apply_fill_to_ledger(
        self, book: ForexPortfolio, request: "_SettlementRequest"
    ) -> tuple[PaperFill, Decimal | None]:
        """Apply the fill to ``book`` and describe it.  No durable side effects."""

        if request.action == "OPEN":
            position = book.open(
                position_id=f"position:{request.simulation_id}",
                side=request.side,
                quantity=request.quantity,
                price=request.price,
                at=request.at,
            )
            fill_position_id = position.position_id
            realized_pnl = Decimal(0)
            costs = position.costs
            remaining: Decimal | None = None
        else:
            if request.position_id is None:  # pragma: no cover - validated
                raise ValueError("missing_position_id")
            trade = book.close(
                request.position_id,
                price=request.price,
                at=request.at,
                cutoffs=request.cutoffs,
                quantity=request.quantity,
            )
            fill_position_id = request.position_id
            realized_pnl = trade.realized_pnl_account
            costs = trade.costs
            closed_position = book.positions.get(fill_position_id)
            # A partial close leaves a remainder the next order must be judged
            # against; a full close leaves nothing.
            remaining = (
                closed_position.quantity if closed_position is not None else Decimal(0)
            )
        fill = PaperFill(
            simulation_id=request.simulation_id,
            request_id=request.request_id,
            action=request.action,
            position_id=fill_position_id,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            filled_at=request.at,
            realized_pnl_account=realized_pnl,
            cost_total_account=costs.total_account,
            financing_account=costs.financing_account,
            rollover_events=costs.rollover_events,
        )
        return fill, remaining

    def _settle_in_memory(
        self,
        simulation_id: str,
        *,
        fill_price: Decimal,
        fill_at: datetime,
        action: str,
        side: OrderSide | str,
        quantity: Decimal,
        position_id: str | None = None,
        cutoffs: Sequence[datetime] = (),
    ) -> PaperFill:
        """Single-process path: the book and the records live in this process."""

        record = self._by_simulation.get(simulation_id)
        if record is None or record.state != OrderState.SIMULATED.value:
            raise ValueError("order_not_simulated_or_already_final")
        request = self._parse_settlement(
            record,
            fill_price=fill_price,
            fill_at=fill_at,
            action=action,
            side=side,
            quantity=quantity,
            position_id=position_id,
            cutoffs=cutoffs,
        )
        fill, _ = self._apply_fill_to_ledger(self.portfolio, request)
        settled_record = replace(
            record,
            state=OrderState.FILLED.value,
            updated_at=request.at,
        )
        self._by_request[record.request_id] = settled_record
        self._by_simulation[simulation_id] = settled_record
        self._fills[simulation_id] = fill
        self.audit_log.record(
            {
                "event_type": "PAPER_ORDER_FILLED",
                "request_id": record.request_id,
                "simulation_id": simulation_id,
                "live": False,
                "fill": fill.audit_view(),
            }
        )
        return fill

    def _settle_durable(
        self,
        simulation_id: str,
        *,
        fill_price: Decimal,
        fill_at: datetime,
        action: str,
        side: OrderSide | str,
        quantity: Decimal,
        position_id: str | None = None,
        cutoffs: Sequence[datetime] = (),
    ) -> PaperFill:
        """Durable path: one transaction for order, fill, ledger, reservation, audit.

        The ledger is mutated on a throw-away clone.  Only after the transaction
        commits is the live book restored -- from the committed snapshot itself,
        so memory cannot disagree with the record.  A failure at any point
        leaves the live book exactly as it was, and a replay of an already
        committed settlement returns the stored fill instead of applying it a
        second time.
        """

        book = self.reservations
        if book is None:  # pragma: no cover - guarded by the caller
            raise ValueError("missing_reservation_book")
        repository = book.repository

        record = self._by_simulation.get(simulation_id)
        if record is None:
            raise ValueError("order_not_simulated_or_already_final")
        if record.state == OrderState.FILLED.value:
            return self._replay_committed_settlement(
                record,
                fill_price=fill_price,
                fill_at=fill_at,
                action=action,
                quantity=quantity,
                position_id=position_id,
            )
        if record.state != OrderState.SIMULATED.value:
            raise ValueError("order_not_simulated_or_already_final")
        request = self._parse_settlement(
            record,
            fill_price=fill_price,
            fill_at=fill_at,
            action=action,
            side=side,
            quantity=quantity,
            position_id=position_id,
            cutoffs=cutoffs,
        )

        # Trial ledger: the same code the live book runs, on a copy.
        trial = self.portfolio.empty_clone()
        trial.restore_snapshot(
            self.portfolio.snapshot(request.at, allow_unmarked=True)
        )
        fill, remaining = self._apply_fill_to_ledger(trial, request)
        settled_record = replace(
            record, state=OrderState.FILLED.value, updated_at=request.at
        )
        ledger_snapshot = dict(trial.snapshot(request.at, allow_unmarked=True))
        ledger_snapshot["settlement"] = {
            "simulation_id": simulation_id,
            "request_id": record.request_id,
            "action": request.action,
            "position_id": fill.position_id,
            "applied_at": request.at,
            "source": "PAPER_SETTLEMENT",
            "live": False,
        }
        closed_ids: tuple[str, ...] = ()
        if request.action == "CLOSE" and fill.position_id not in trial.positions:
            closed_ids = (fill.position_id,)
        settlement = LedgerSettlement(
            simulation_id=simulation_id,
            request_id=record.request_id,
            order_payload=settled_record.audit_view(),
            fill_payload=fill.audit_view(),
            ledger_snapshot=ledger_snapshot,
            position_id=fill.position_id,
            position_remaining=remaining,
            closed_position_ids=closed_ids,
        )

        committed: tuple[dict[str, Any], dict[str, Any]] | None = None
        with self._durable_unit():
            committed = load_committed_settlement(repository, simulation_id)
            if committed is not None:
                # Another writer (or an earlier attempt whose commit landed
                # without reaching this process) already applied this fill.
                stored_fill, _ = committed
                if not settlement_matches(
                    stored_fill,
                    action=request.action,
                    quantity=request.quantity,
                    price=request.price,
                    at=request.at,
                    position_id=(
                        request.position_id
                        if request.action == "CLOSE"
                        else fill.position_id
                    ),
                ):
                    raise ValueError("settlement_conflict")
            else:
                stored_order = repository.get_order(record.request_id)
                if stored_order is not None and str(
                    stored_order.get("state", "")
                ).upper() != OrderState.SIMULATED.value:
                    raise ValueError("order_not_simulated_or_already_final")
                commit_settlement(
                    repository, settlement, reservation_book=book, at=request.at
                )
                self.audit_log.record(
                    settlement_event(
                        order_payload=settled_record.audit_view(),
                        fill_payload=fill.audit_view(),
                        reservation=repository.get_reservation(record.request_id),
                        ledger_snapshot=ledger_snapshot,
                        cutoffs=request.cutoffs,
                    )
                )

        if committed is not None:
            stored_fill, stored_snapshot = committed
            applied = self._apply_committed_settlement(
                record, stored_fill, stored_snapshot
            )
            return applied

        # Post-commit: publish the committed ledger and the fill to this engine.
        self._apply_committed_settlement(record, fill.audit_view(), ledger_snapshot)
        return fill

    def _replay_committed_settlement(
        self,
        record: PaperOrderRecord,
        *,
        fill_price: Any,
        fill_at: Any,
        action: str,
        quantity: Any,
        position_id: str | None,
    ) -> PaperFill:
        """Answer a repeated settlement from the record, never by re-applying it."""

        book = self.reservations
        if book is None:  # pragma: no cover - guarded by the caller
            raise ValueError("missing_reservation_book")
        cached = self._fills.get(record.simulation_id)
        if cached is not None:
            if not settlement_matches(
                cached.audit_view(),
                action=action,
                quantity=_decimal(quantity, "quantity"),
                price=_decimal(fill_price, "fill_price"),
                at=_timestamp(fill_at, "fill_at"),
                position_id=position_id,
            ):
                raise ValueError("settlement_conflict")
            return cached
        committed = load_committed_settlement(
            book.repository, record.simulation_id
        )
        if committed is None:
            raise ValueError("filled_order_without_fill")
        stored_fill, stored_snapshot = committed
        quantity_decimal = _decimal(quantity, "quantity")
        price_decimal = _decimal(fill_price, "fill_price")
        at_utc = _timestamp(fill_at, "fill_at")
        if not settlement_matches(
            stored_fill,
            action=action,
            quantity=quantity_decimal,
            price=price_decimal,
            at=at_utc,
            position_id=position_id,
        ):
            raise ValueError("settlement_conflict")
        return self._apply_committed_settlement(record, stored_fill, stored_snapshot)

    def _apply_committed_settlement(
        self,
        record: PaperOrderRecord,
        fill_payload: Mapping[str, Any],
        ledger_snapshot: Mapping[str, Any],
    ) -> PaperFill:
        """Publish an already committed settlement to this engine.

        The ledger is restored from the committed snapshot -- not recomputed --
        so a retry cannot move P&L, fees, rollover, or position sizes.
        """

        fill = PaperFill.from_audit_view(fill_payload)
        if fill.simulation_id != record.simulation_id:
            raise ValueError("settlement_fill_mismatch")
        try:
            self.portfolio.restore_snapshot(ledger_snapshot)
        except Exception as exc:
            # The record is durable and the book is not: fail closed and make
            # the disagreement visible instead of reporting a clean fill.
            unresolved = replace(
                record,
                state=OrderState.UNRESOLVED.value,
                updated_at=record.updated_at,
                reason=f"ledger_apply_failed:{type(exc).__name__}",
            )
            self._remember(unresolved)
            self.audit_log.record(
                {
                    "event_type": "PAPER_ORDER_UNRESOLVED",
                    "request_id": record.request_id,
                    "simulation_id": record.simulation_id,
                    "reason": unresolved.reason,
                    "live": False,
                }
            )
            raise ValueError(unresolved.reason) from exc
        self._remember(
            replace(
                record,
                state=OrderState.FILLED.value,
                updated_at=fill.filled_at,
            )
        )
        self._fills[record.simulation_id] = fill
        return fill

    def execute(
        self,
        request: OrderRequest,
        context: GateContext,
        *,
        fill_price: Decimal,
        fill_at: datetime,
        action: str,
        position_id: str | None = None,
        cutoffs: Sequence[datetime] = (),
    ) -> tuple[PaperOrderRecord, PaperFill | None]:
        existing = self._existing_record(request)
        if existing is not None:
            return existing, self._fills.get(existing.simulation_id)
        record = self.submit(request, context)
        if record.state != OrderState.SIMULATED.value:
            return record, None
        try:
            fill = self.settle(
                record.simulation_id,
                fill_price=fill_price,
                fill_at=fill_at,
                action=action,
                side=request.side_value,
                quantity=request.quantity_decimal(),
                position_id=position_id,
                cutoffs=cutoffs,
            )
        except Exception as exc:
            unresolved = replace(
                record,
                state=OrderState.UNRESOLVED.value,
                updated_at=fill_at,
                reason=f"settlement_error:{type(exc).__name__}",
            )
            self._by_request[record.request_id] = unresolved
            self._by_simulation[record.simulation_id] = unresolved
            self.audit_log.record(
                {
                    "event_type": "PAPER_ORDER_UNRESOLVED",
                    "request_id": record.request_id,
                    "simulation_id": record.simulation_id,
                    "reason": unresolved.reason,
                    "live": False,
                }
            )
            return unresolved, None
        settled = self._by_request[record.request_id]
        return settled, fill
