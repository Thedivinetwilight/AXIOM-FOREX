"""Durable, transactional order reservations for local paper execution.

An order reservation is the promise that a quantity of an open position, or a
slice of the entry-exposure budget, is already spoken for.  Reserving in the
engine's own memory is not enough once more than one engine instance shares a
database: two processes would each read "0.10 remaining", each decide they fit,
and together close 0.20.

This module keeps the promise in SQLite instead.  A reservation is taken inside
one ``BEGIN IMMEDIATE`` transaction, so the reads that decide whether the order
fits (reserved quantity, reserved exposure, unresolved orders, the durable
idempotency key) cannot be invalidated by another connection before the commit.
SQLite serialises the writers; the loser of a race sees the winner's row and is
refused.

Ordering inside :meth:`OrderReservationBook.submit`:

1. read the durable idempotency key (``orders``) -- an existing row means this
   request was already handled, here or in another process;
2. read the durable unresolved-order set -- an order in doubt blocks new ones;
3. check the limit (close quantity or entry exposure) against durable sums;
4. insert the reservation row (primary key is the request id: a duplicate
   insert is impossible even if step 1 were somehow missed);
5. ask the caller to simulate the intent and hand back the order payload;
6. persist that payload and mark the reservation ``SIMULATED``.

Everything above commits together or not at all.  A rejection releases its
reservation row (kept, with ``released_at``, for history) so the quantity is
available again; the caller writes the auditable rejected order afterwards.

Quantities and exposure are stored as exact integers scaled by
:data:`RESERVATION_UNITS` so the sums and comparisons happen in SQLite on
integers, never on floats or on locale-sensitive decimal text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from app.common.validation import as_utc
from app.execution.broker import TradingBlocked

from axiom_forex.database.repository import ForexRepository
from axiom_forex.risk.entry_limits import (
    DATA_CLASS_SYNTHETIC_FIXTURE,
    EntryExposureLimit,
)

from .states import FINAL_STATES, OrderState

#: One unit is 1e-8 of a base-currency unit (or of an account-currency notional).
RESERVATION_UNITS = Decimal("0.00000001")

SYNTHETIC_LIMIT_DATA_CLASS = DATA_CLASS_SYNTHETIC_FIXTURE

KIND_CLOSE = "CLOSE"
KIND_ENTRY = "ENTRY"

STATUS_RESERVED = "RESERVED"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_REJECTED = "REJECTED"


def to_units(value: Any, name: str = "quantity") -> int:
    """Exact scaled-integer form; refuse values the store cannot represent."""

    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{name}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid_{name}")
    scaled = parsed / RESERVATION_UNITS
    if scaled != scaled.to_integral_value():
        raise ValueError(f"unsupported_{name}_precision")
    return int(scaled)


def to_units_ceiling(value: Any, name: str = "exposure") -> int:
    """Round *up* to the unit grid.

    Entry notional is a product of a quantity and a price, so it can land off the
    grid.  Rounding it up keeps the exposure accounting conservative: the store
    can never under-report what an order has spoken for.
    """

    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{name}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid_{name}")
    return int((parsed / RESERVATION_UNITS).to_integral_value(rounding=ROUND_CEILING))


def from_units(units: int) -> Decimal:
    return Decimal(int(units)) * RESERVATION_UNITS


@dataclass(frozen=True)
class ReservationRequest:
    """What the caller wants reserved, before the database has judged it."""

    request_id: str
    kind: str
    quantity: Decimal
    position_id: str = ""
    #: Remaining quantity the caller's ledger believes the position has.  The
    #: durable guard only ever moves down to it, so a stale caller cannot raise
    #: the ceiling another engine already lowered.
    position_quantity: Decimal | None = None
    #: Entry notional in account currency, or ``None`` when it cannot be priced.
    exposure: Decimal | None = None
    exposure_key: str = ""
    payload_fingerprint: str = ""

    def validate(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("missing_request_id")
        if self.kind not in {KIND_CLOSE, KIND_ENTRY}:
            raise ValueError("invalid_reservation_kind")
        to_units(self.quantity, "quantity")
        if self.quantity <= 0:
            raise ValueError("invalid_quantity")
        if self.kind == KIND_CLOSE:
            if not isinstance(self.position_id, str) or not self.position_id.strip():
                raise ValueError("missing_position_id")
            if self.position_quantity is None:
                raise ValueError("missing_position_quantity")
            to_units(self.position_quantity, "position_quantity")


@dataclass(frozen=True)
class ReservationOutcome:
    status: str
    reason: str = ""
    order: Mapping[str, Any] | None = None
    reserved_quantity: Decimal = Decimal(0)
    reserved_exposure: Decimal = Decimal(0)
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def reserved(self) -> bool:
        return self.status == STATUS_RESERVED

    @property
    def duplicate(self) -> bool:
        return self.status == STATUS_DUPLICATE

    @property
    def rejected(self) -> bool:
        return self.status == STATUS_REJECTED

    def audit_view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "reserved_quantity": self.reserved_quantity,
            "reserved_exposure": self.reserved_exposure,
            "detail": dict(self.detail),
            "live": False,
        }


class OrderReservationBook:
    """SQLite-backed reservation book over one repository connection."""

    def __init__(
        self,
        repository: ForexRepository,
        *,
        entry_limit: EntryExposureLimit | None = None,
        account_currency: str | None = None,
        max_entry_exposure: Decimal | None = None,
        exposure_key: str = "",
    ):
        """Build the book.

        ``entry_limit`` is the declared hard cap on in-flight entry notional.
        ``max_entry_exposure`` stays as the plain-number form used by tests and
        by callers that already validated their own policy; it is wrapped into
        an operator-declared limit in the account currency so both spellings go
        through exactly one check.

        With neither, entries are refused (``ENTRY_LIMIT_NOT_CONFIGURED``) but
        closes still work: an unconfigured cap must never silently mean
        "unlimited".
        """

        if entry_limit is not None and max_entry_exposure is not None:
            raise ValueError("entry_limit_declared_twice")
        if entry_limit is not None:
            self.entry_limit = entry_limit
        elif max_entry_exposure is not None:
            if max_entry_exposure <= 0:
                raise ValueError("invalid_max_entry_exposure")
            to_units(max_entry_exposure, "max_entry_exposure")
            self.entry_limit = EntryExposureLimit(
                limit_account=max_entry_exposure,
                account_currency=str(account_currency or ""),
                version="caller-supplied-entry-limit-v1",
                source="CALLER",
            )
        else:
            self.entry_limit = None
        self.repository = repository
        #: The account currency the cap must be denominated in to be comparable.
        self.account_currency = account_currency
        self.exposure_key = exposure_key

    @property
    def max_entry_exposure(self) -> Decimal | None:
        return None if self.entry_limit is None else self.entry_limit.decimal()

    def entry_limit_view(self) -> dict[str, Any]:
        if self.entry_limit is None:
            return {"configured": False}
        view = self.entry_limit.audit_view()
        return {
            "configured": True,
            **view,
            "compatible_with_account": self.entry_limit.compatible_with(
                self.account_currency
            ),
            "account_currency": self.account_currency,
        }

    def entry_limit_block_reason(self) -> str | None:
        """Why a new entry cannot be reserved, or ``None`` when it can."""

        limit = self.entry_limit
        if limit is None:
            return "ENTRY_LIMIT_NOT_CONFIGURED"
        if not limit.validate().valid:
            return "ENTRY_LIMIT_INVALID"
        if not limit.compatible_with(self.account_currency):
            return "ENTRY_LIMIT_INCOMPATIBLE"
        return None

    # -- writes ---------------------------------------------------------

    def submit(
        self,
        request: ReservationRequest,
        *,
        at: datetime,
        simulate: Callable[[], Mapping[str, Any]],
    ) -> ReservationOutcome:
        """Reserve, re-check every limit, simulate, and persist -- atomically.

        ``simulate`` runs inside the transaction and must return the complete
        order payload that is written to ``orders``.  It may raise
        :class:`TradingBlocked` to refuse the order at the gate; that refusal
        releases the reservation and is returned as a rejection.  Any other
        exception rolls the whole unit back: no reservation, no order row.
        """

        at_utc, at_error = as_utc(at, "reservation_at")
        if at_error or at_utc is None:
            raise ValueError(at_error or "invalid_reservation_at")
        request.validate()
        stamp = at_utc.isoformat()
        quantity_units = to_units(request.quantity, "quantity")

        with self.repository.transaction():
            existing = self.repository.get_order(request.request_id)
            if existing is not None:
                return ReservationOutcome(
                    status=STATUS_DUPLICATE, order=existing
                )
            if self.repository.get_reservation(request.request_id) is not None:
                # A reservation row without its order row contradicts the
                # atomic write above; treat the database as untrustworthy.
                raise ValueError("reservation_without_order_record")
            unresolved = self.repository.unresolved_order_ids(
                final_states=FINAL_STATES
            )
            if unresolved:
                return ReservationOutcome(
                    status=STATUS_REJECTED,
                    reason="UNRESOLVED_ORDER_PRESENT",
                    detail={"unresolved_request_ids": unresolved},
                )

            claimed_quantity = Decimal(0)
            if request.kind == KIND_CLOSE:
                guard_units = self.repository.lower_position_guard(
                    request.position_id,
                    quantity_units=to_units(
                        request.position_quantity, "position_quantity"
                    ),
                    updated_at=stamp,
                )
                reserved_units = self.repository.reserved_close_units(
                    request.position_id
                )
                if reserved_units + quantity_units > guard_units:
                    return ReservationOutcome(
                        status=STATUS_REJECTED,
                        reason="close_quantity_exceeds_position",
                        detail={
                            "position_id": request.position_id,
                            "requested_quantity": request.quantity,
                            "already_reserved": from_units(reserved_units),
                            "position_quantity": from_units(guard_units),
                        },
                    )
                claimed_quantity = request.quantity

            exposure_units = 0
            if request.kind == KIND_ENTRY:
                # A cap is mandatory for entries: "no limit configured" must
                # refuse the order, never mean "unlimited".  Closes never reach
                # this branch, so a missing or unusable cap cannot block a close.
                block_reason = self.entry_limit_block_reason()
                if block_reason is not None:
                    return ReservationOutcome(
                        status=STATUS_REJECTED,
                        reason=block_reason,
                        detail={
                            "entry_limit": self.entry_limit_view(),
                            "account_currency": self.account_currency,
                        },
                    )
                limit = self.entry_limit
                if limit is None:  # pragma: no cover - block_reason covers it
                    raise ValueError("missing_entry_limit")
                if request.exposure is None or request.exposure <= 0:
                    return ReservationOutcome(
                        status=STATUS_REJECTED,
                        reason="entry_exposure_unpriced",
                        detail={"request_id": request.request_id},
                    )
                exposure_units = to_units(request.exposure, "exposure")
                reserved_exposure = self.repository.reserved_entry_exposure_units(
                    request.exposure_key or self.exposure_key
                )
                limit_units = to_units(limit.decimal(), "entry_exposure_limit")
                if reserved_exposure + exposure_units > limit_units:
                    return ReservationOutcome(
                        status=STATUS_REJECTED,
                        reason="entry_exposure_limit_exceeded",
                        detail={
                            "requested_exposure": request.exposure,
                            "already_reserved": from_units(reserved_exposure),
                            "limit": limit.decimal(),
                            "limit_version": limit.version,
                            "limit_data_class": limit.data_class,
                        },
                    )

            self.repository.insert_reservation(
                request_id=request.request_id,
                kind=request.kind,
                state=OrderState.NEW.value,
                position_id=request.position_id,
                quantity_units=quantity_units,
                exposure_units=exposure_units,
                exposure_key=request.exposure_key or self.exposure_key,
                payload_fingerprint=request.payload_fingerprint,
                created_at=stamp,
                updated_at=stamp,
            )
            try:
                order_payload = simulate()
            except TradingBlocked as exc:
                self.repository.release_reservation(
                    request.request_id,
                    state=OrderState.REJECTED.value,
                    released_at=stamp,
                )
                return ReservationOutcome(
                    status=STATUS_REJECTED, reason=exc.reason
                )
            if not isinstance(order_payload, Mapping):
                raise ValueError("invalid_order_payload")
            simulation_id = str(order_payload.get("simulation_id", ""))
            if not simulation_id:
                raise ValueError("missing_simulation_id")
            self.repository.update_reservation(
                request.request_id,
                state=OrderState.SIMULATED.value,
                simulation_id=simulation_id,
                updated_at=stamp,
            )
            self.repository.save_orders([order_payload])
            return ReservationOutcome(
                status=STATUS_RESERVED,
                order=dict(order_payload),
                reserved_quantity=claimed_quantity,
                reserved_exposure=from_units(exposure_units),
                detail={
                    "position_id": request.position_id,
                    "simulation_id": simulation_id,
                },
            )

    def finalize(
        self,
        *,
        order_payload: Mapping[str, Any],
        state: str,
        at: datetime,
        release: bool = True,
        position_remaining: Decimal | None = None,
    ) -> None:
        """Persist a terminal order state and release its reservation together.

        A filled entry releases exposure; a filled close releases the close
        quantity *and* lowers the position guard to what is genuinely left, so
        the next order is judged against the post-fill book.
        """

        at_utc, at_error = as_utc(at, "finalize_at")
        if at_error or at_utc is None:
            raise ValueError(at_error or "invalid_finalize_at")
        stamp = at_utc.isoformat()
        request_id = str(order_payload.get("request_id", ""))
        if not request_id:
            raise ValueError("missing_request_id")
        with self.repository.transaction():
            reservation = self.repository.get_reservation(request_id)
            self.repository.save_orders([order_payload])
            if release and reservation is not None:
                if reservation.get("released_at") is not None:
                    # Releasing twice is only acceptable when both sides agree
                    # on what the order became; anything else is an ambiguous
                    # record and must not be guessed at.
                    if str(reservation.get("state", "")).upper() != str(state).upper():
                        raise ValueError("reservation_already_released")
                else:
                    self.repository.release_reservation(
                        request_id, state=state, released_at=stamp
                    )
            if position_remaining is not None:
                position_id = str(order_payload.get("position_id", "")) or str(
                    (reservation or {}).get("position_id", "")
                )
                if not position_id:
                    raise ValueError("missing_position_id")
                self.repository.lower_position_guard(
                    position_id,
                    quantity_units=to_units(position_remaining, "position_remaining"),
                    updated_at=stamp,
                )

    def persist_order(self, order_payload: Mapping[str, Any]) -> None:
        """Write an order row that carries no reservation (a refusal)."""

        if not isinstance(order_payload, Mapping):
            raise ValueError("invalid_order_payload")
        with self.repository.transaction():
            self.repository.save_orders([order_payload])

    # -- reads ----------------------------------------------------------

    def pending_close_quantities(self) -> dict[str, Decimal]:
        """Quantity promised to closes that have not settled, per position."""

        pending: dict[str, Decimal] = {}
        for reservation in self.repository.active_reservations():
            if str(reservation.get("kind", "")).upper() != KIND_CLOSE:
                continue
            position_id = str(reservation.get("position_id", ""))
            if not position_id:
                raise ValueError("invalid_pending_close_intent")
            units = int(reservation.get("quantity_units", 0))
            if units <= 0:
                raise ValueError("invalid_pending_close_intent")
            pending[position_id] = pending.get(
                position_id, Decimal(0)
            ) + from_units(units)
        return pending

    def active(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.repository.active_reservations())

    def reserved_exposure(self, exposure_key: str | None = None) -> Decimal:
        return from_units(
            self.repository.reserved_entry_exposure_units(
                self.exposure_key if exposure_key is None else exposure_key
            )
        )
