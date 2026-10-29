"""Restart recovery of durable local paper order and portfolio state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from app.audit.log import sanitize_audit_value
from app.risk.models import UnresolvedOrder

from axiom_forex.database.audit import DurableAuditLog
from axiom_forex.database.repository import ForexRepository
from axiom_forex.portfolio.ledger import ForexPortfolio

from .paper_engine import PaperExecutionEngine
from .settlement import SETTLEMENT_SNAPSHOT_PREFIX
from .states import FINAL_STATES


@dataclass(frozen=True)
class RecoveryResult:
    portfolio: ForexPortfolio
    paper_execution: PaperExecutionEngine
    portfolio_snapshot_id: str | None
    order_count: int
    fill_count: int
    audit_event_count: int
    unresolved_orders: tuple[UnresolvedOrder, ...]

    @property
    def recovered(self) -> bool:
        return self.order_count > 0 or self.fill_count > 0

    def audit_view(self) -> dict[str, Any]:
        view = {
            "recovered": self.recovered,
            "portfolio_snapshot_id": self.portfolio_snapshot_id,
            "order_count": self.order_count,
            "fill_count": self.fill_count,
            "audit_event_count": self.audit_event_count,
            "unresolved_orders": [
                order.audit_view() for order in self.unresolved_orders
            ],
            "unresolved_order_count": len(self.unresolved_orders),
        }
        clean = sanitize_audit_value(view)
        return clean if isinstance(clean, dict) else {"recovered": self.recovered}


@dataclass(frozen=True)
class PaperStateRecovery:
    """Rebuild a clean in-memory paper state from sanitized SQLite records."""

    repository: ForexRepository
    audit_log: DurableAuditLog

    def has_paper_state(self) -> bool:
        counts = self.repository.health()["counts"]
        return bool(
            counts.get("orders", 0)
            or counts.get("fills", 0)
            or any(
                snapshot.get("kind") == "portfolio"
                for snapshot in self.repository.list_snapshots(kind="portfolio")
            )
        )

    @staticmethod
    def base_portfolio_snapshot(snapshots: Any) -> Mapping[str, Any] | None:
        """The book recovery restores: the newest *whole-platform* portfolio.

        Per-settlement snapshots describe one fill each, so they are only a
        fallback for a database that has nothing else.  Preferring the platform's
        own snapshot keeps recovery from mistaking a single fill's book for the
        account's book.
        """

        portfolios = [
            snapshot
            for snapshot in snapshots
            if isinstance(snapshot, Mapping) and snapshot.get("kind") == "portfolio"
        ]
        whole = [
            snapshot
            for snapshot in portfolios
            if not str(snapshot.get("snapshot_id", "")).startswith(
                SETTLEMENT_SNAPSHOT_PREFIX
            )
        ]
        candidates = whole or portfolios
        return candidates[-1] if candidates else None

    def recover(
        self, *, engine: PaperExecutionEngine, portfolio: ForexPortfolio
    ) -> RecoveryResult:
        state = self.repository.recovery_snapshot()
        database_health = state["health"]
        if database_health.get("schema_version") != database_health.get(
            "expected_schema_version"
        ):
            raise ValueError("recovery_schema_version_mismatch")
        orders = state["orders"]
        fills = state["fills"]
        portfolio_snapshot = self.base_portfolio_snapshot(state["snapshots"])
        # Only a fill implies ledger state.  Orders alone (a submit whose
        # settlement never committed) recover into an empty book.
        if fills and portfolio_snapshot is None:
            raise ValueError("missing_portfolio_recovery_snapshot")
        recovered_portfolio = portfolio.empty_clone()
        if portfolio_snapshot is not None:
            recovered_portfolio.restore_snapshot(portfolio_snapshot)
        recovered_engine = PaperExecutionEngine(
            broker=engine.broker,
            portfolio=recovered_portfolio,
            audit_log=self.audit_log,
            reservations=engine.reservations,
        )
        recovered_engine.restore(orders, fills)
        recovered_position_ids = {
            position.position_id
            for position in recovered_portfolio.positions.values()
        }
        if recovered_engine.durable_reservations:
            self._check_reservations(
                state["order_reservations"],
                orders,
                recovered_position_ids=recovered_position_ids,
            )
            # Each fill carries the ledger state it produced, written in the
            # same transaction.  Check them together: a fill whose book was
            # rolled back (or written by an older, non-atomic path) cannot be
            # recovered without inventing history.
            self._check_settlements(
                state["snapshots"],
                recovered_engine.fills(),
                base_snapshot=portfolio_snapshot,
                recovered_position_ids=recovered_position_ids,
            )
        # The in-run audit cache is a view of the record: rebuild it from the
        # committed rows so a restarted process never reports an event that
        # this database does not hold.
        if isinstance(self.audit_log, DurableAuditLog):
            self.audit_log.rebuild_cache()
        unresolved = recovered_engine.unresolved_orders()
        result = RecoveryResult(
            portfolio=recovered_portfolio,
            paper_execution=recovered_engine,
            portfolio_snapshot_id=(
                str(portfolio_snapshot.get("snapshot_id"))
                if portfolio_snapshot is not None
                else None
            ),
            order_count=len(orders),
            fill_count=len(fills),
            audit_event_count=int(database_health["counts"]["audit_events"]),
            unresolved_orders=unresolved,
        )
        self.audit_log.record(
            {
                "event_type": "PAPER_STATE_RECOVERED",
                "live": False,
                "recovery": result.audit_view(),
            }
        )
        return result

    @staticmethod
    def _check_settlements(
        snapshots: Any,
        fills: Any,
        *,
        base_snapshot: Mapping[str, Any] | None,
        recovered_position_ids: set[str],
    ) -> None:
        """Reconcile every committed fill with the ledger state it produced.

        Settlement writes order, fill, ledger snapshot, reservation and audit
        event in one transaction, so these can only disagree if the database was
        tampered with or written by an older, non-atomic path.  Two invariants
        are enforced:

        * every fill has its own settlement snapshot, and that snapshot really
          contains the position the fill says it moved;
        * the book recovery restores is at least as new as the newest
          settlement, and already contains every position that settlement left
          open -- otherwise the gate would be handed a stale book.
        """

        def instant(value: Any) -> str:
            # Engine-side views carry datetimes; rows carry ISO strings.
            if isinstance(value, datetime):
                return value.isoformat()
            return str(value)

        settlement_snapshots: dict[str, Mapping[str, Any]] = {}
        for snapshot in snapshots:
            snapshot_id = str(snapshot.get("snapshot_id", ""))
            if snapshot_id.startswith(SETTLEMENT_SNAPSHOT_PREFIX):
                settlement_snapshots[snapshot_id] = snapshot
        if not settlement_snapshots:
            if fills:
                raise ValueError("settlement_snapshot_missing")
            return

        for fill in fills:
            simulation_id = str(fill.get("simulation_id", ""))
            snapshot = settlement_snapshots.get(
                SETTLEMENT_SNAPSHOT_PREFIX + simulation_id
            )
            if snapshot is None:
                raise ValueError("settlement_snapshot_missing:" + simulation_id)
            stored = snapshot.get("settlement")
            if not isinstance(stored, Mapping):
                raise ValueError("settlement_snapshot_incomplete:" + simulation_id)
            if str(stored.get("simulation_id", "")) != simulation_id:
                raise ValueError("settlement_snapshot_mismatch:" + simulation_id)
            position_id = str(fill.get("position_id", ""))
            action = str(fill.get("action", "")).upper()
            open_ids = {
                str(position.get("position_id", ""))
                for position in snapshot.get("open_positions", ())
                if isinstance(position, Mapping)
            }
            closed_keys = {
                (
                    str(trade.get("position_id", "")),
                    instant(trade.get("closed_at")),
                )
                for trade in snapshot.get("closed_trades", ())
                if isinstance(trade, Mapping)
            }
            if action == "OPEN" and position_id not in open_ids:
                raise ValueError(
                    "settlement_snapshot_missing_open_position:"
                    + simulation_id
                    + ":"
                    + position_id
                )
            if action == "CLOSE" and (
                position_id,
                instant(fill.get("filled_at")),
            ) not in closed_keys:
                raise ValueError(
                    "settlement_snapshot_missing_closed_trade:"
                    + simulation_id
                    + ":"
                    + position_id
                )

        newest = max(
            settlement_snapshots.values(),
            key=lambda snapshot: str(snapshot.get("recorded_at", "")),
        )
        base_recorded_at = str((base_snapshot or {}).get("recorded_at", ""))
        if not base_recorded_at:
            raise ValueError("missing_portfolio_recovery_snapshot")
        if base_recorded_at < str(newest.get("recorded_at", "")):
            # The book recovery would start from predates a fill it claims to
            # include: the gate would be handed a stale position list.
            raise ValueError("recovered_portfolio_predates_settlement")
        for position in newest.get("open_positions", ()):
            if not isinstance(position, Mapping):
                continue
            position_id = str(position.get("position_id", ""))
            if position_id not in recovered_position_ids:
                raise ValueError(
                    "recovered_portfolio_missing_settled_position:" + position_id
                )

    @staticmethod
    def _check_reservations(
        reservations: Any,
        orders: Any,
        *,
        recovered_position_ids: set[str],
    ) -> None:
        """Refuse to recover a book whose reservations contradict its orders.

        A reservation without a matching, still-open order would hold quantity
        nobody can release; an in-doubt order without a reservation would let
        the next submit promise quantity that is already spent.  Both are
        ambiguous, so recovery stops instead of guessing.
        """

        orders_by_id = {
            str(order.get("request_id", "")): order
            for order in orders
            if isinstance(order, dict)
        }
        active_ids: set[str] = set()
        for reservation in reservations:
            request_id = str(reservation.get("request_id", ""))
            if not request_id:
                raise ValueError("recovered_reservation_without_request_id")
            order = orders_by_id.get(request_id)
            if order is None:
                raise ValueError(
                    "recovered_reservation_without_order:" + request_id
                )
            if str(order.get("state", "")).upper() in FINAL_STATES:
                raise ValueError(
                    "recovered_reservation_for_final_order:" + request_id
                )
            if int(reservation.get("quantity_units") or 0) <= 0:
                raise ValueError(
                    "recovered_reservation_without_quantity:" + request_id
                )
            if str(reservation.get("kind", "")).upper() == "CLOSE":
                position_id = str(reservation.get("position_id", ""))
                if position_id not in recovered_position_ids:
                    raise ValueError(
                        "recovered_reservation_for_unknown_position:" + position_id
                    )
            active_ids.add(request_id)
        for request_id, order in orders_by_id.items():
            if request_id in active_ids:
                continue
            if str(order.get("state", "")).upper() in FINAL_STATES:
                continue
            raise ValueError("unresolved_order_without_reservation:" + request_id)
