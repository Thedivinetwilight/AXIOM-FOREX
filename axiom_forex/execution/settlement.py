"""One atomic settlement unit: order, fill, ledger, reservation, audit.

Settlement is the moment paper money moves.  It changes six things that must
never be observed half-applied:

1. the order row (``SIMULATED`` -> ``FILLED``, or ``UNRESOLVED``);
2. the fill row (the immutable record of price, quantity, P&L, costs, rollover);
3. the positions index (open rows, closed slices, partial-close remainders);
4. the portfolio snapshot (cash, realized P&L, daily counters, cost totals);
5. the reservation (released) and the position guard (lowered to what is left);
6. the durable audit event for the fill.

Everything above is written inside **one** ``BEGIN IMMEDIATE`` transaction, so
the database either has all of it or none of it.  The ledger is mutated on a
clone first: if the transaction fails, the live book was never touched, so
memory cannot drift from the record.  Only after the commit is the live book
restored from the very snapshot that was committed, which makes "what memory
shows" and "what SQLite holds" the same object by construction.

Idempotency is the same idea from the other side: the fill id is the simulation
id and its row is unique, so replaying a settlement -- after a retry, after a
crash, after a restart -- finds the committed fill and returns it instead of
applying the ledger a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from axiom_forex.database.repository import ForexRepository

from .reservations import OrderReservationBook
from .states import OrderState

#: Snapshot kind and id prefix for the ledger state committed with a fill.
SETTLEMENT_SNAPSHOT_PREFIX = "portfolio-settlement:"


def settlement_snapshot_id(simulation_id: str) -> str:
    """Deterministic id: re-settling rewrites the same row, never a second one."""

    if not isinstance(simulation_id, str) or not simulation_id.strip():
        raise ValueError("missing_simulation_id")
    return SETTLEMENT_SNAPSHOT_PREFIX + simulation_id


@dataclass(frozen=True)
class LedgerSettlement:
    """What a settlement writes, computed before the transaction opens."""

    simulation_id: str
    request_id: str
    order_payload: Mapping[str, Any]
    fill_payload: Mapping[str, Any]
    ledger_snapshot: Mapping[str, Any]
    position_id: str
    position_remaining: Decimal | None
    closed_position_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.simulation_id.strip():
            raise ValueError("missing_simulation_id")
        if not self.request_id.strip():
            raise ValueError("missing_request_id")
        if not self.position_id.strip():
            raise ValueError("missing_position_id")

    @property
    def snapshot_id(self) -> str:
        return settlement_snapshot_id(self.simulation_id)


def commit_settlement(
    repository: ForexRepository,
    settlement: LedgerSettlement,
    *,
    reservation_book: OrderReservationBook | None = None,
    at: datetime,
) -> None:
    """Write one settlement.  Must be called inside an open transaction.

    ``repository.in_transaction`` is asserted rather than assumed: calling this
    outside a transaction would turn six writes back into six independent
    commits, which is exactly the failure mode this module exists to prevent.
    """

    if not repository.in_transaction:
        raise RuntimeError("settlement_requires_open_transaction")

    state = OrderState.FILLED.value
    repository.save_orders([settlement.order_payload])
    repository.save_fills([settlement.fill_payload])
    snapshot = dict(settlement.ledger_snapshot)
    repository.save_positions(snapshot.get("open_positions", ()), state="OPEN")
    repository.save_positions(snapshot.get("closed_trades", ()), state="CLOSED")
    if settlement.closed_position_ids:
        repository.close_position_rows(
            settlement.closed_position_ids, updated_at=at.isoformat()
        )
    repository.save_snapshot(settlement.snapshot_id, "portfolio", snapshot)
    if reservation_book is not None:
        reservation_book.finalize(
            order_payload=settlement.order_payload,
            state=state,
            at=at,
            release=True,
            position_remaining=settlement.position_remaining,
        )


def load_committed_settlement(
    repository: ForexRepository, simulation_id: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The committed (fill, ledger snapshot) for a simulation, if any."""

    fill = repository.get_fill(simulation_id)
    if fill is None:
        return None
    snapshot = repository.get_snapshot(settlement_snapshot_id(simulation_id))
    if snapshot is None:
        # A fill without the ledger state it belongs to is ambiguous: the book
        # it produced cannot be rebuilt, so the caller must fail closed.
        raise ValueError("settlement_snapshot_missing:" + simulation_id)
    return fill, snapshot


def settlement_matches(
    fill_payload: Mapping[str, Any],
    *,
    action: str,
    quantity: Decimal,
    price: Decimal,
    at: datetime,
    position_id: str | None,
) -> bool:
    """True when a stored fill describes exactly this settlement request."""

    def same_number(left: Any, right: Decimal) -> bool:
        try:
            return Decimal(str(left)) == right
        except Exception:
            return False

    def same_instant(left: Any, right: datetime) -> bool:
        # Audit views carry datetimes; durable rows carry ISO strings.
        if isinstance(left, datetime):
            return left == right
        try:
            parsed = datetime.fromisoformat(str(left).replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed == right

    stored_position = str(fill_payload.get("position_id", ""))
    if str(fill_payload.get("action", "")).upper() != action.upper():
        return False
    if not same_number(fill_payload.get("quantity"), quantity):
        return False
    if not same_number(fill_payload.get("price"), price):
        return False
    if not same_instant(fill_payload.get("filled_at"), at):
        return False
    if position_id is not None and stored_position != position_id:
        return False
    return True


def settlement_event(
    *,
    order_payload: Mapping[str, Any],
    fill_payload: Mapping[str, Any],
    reservation: Mapping[str, Any] | None,
    ledger_snapshot: Mapping[str, Any],
    cutoffs: Sequence[datetime] = (),
) -> dict[str, Any]:
    """The audit event for a fill, built from committed-shape payloads only."""

    return {
        "event_type": "PAPER_ORDER_FILLED",
        "request_id": order_payload.get("request_id", ""),
        "simulation_id": fill_payload.get("simulation_id", ""),
        "live": False,
        "fill": dict(fill_payload),
        "atomic": {
            "order": order_payload.get("state"),
            "fill": fill_payload.get("action"),
            "reservation": dict(reservation) if reservation is not None else None,
            "ledger": {
                "open_positions": len(ledger_snapshot.get("open_positions", ())),
                "closed_trades": len(ledger_snapshot.get("closed_trades", ())),
                "realized_pnl_today_account": ledger_snapshot.get(
                    "realized_pnl_today_account"
                ),
                "trades_today": ledger_snapshot.get("trades_today"),
                "cost_total_account": ledger_snapshot.get("cost_total_account"),
                "rollover_total_account": ledger_snapshot.get(
                    "rollover_total_account"
                ),
            },
            "rollover_cutoffs": list(cutoffs),
        },
    }
