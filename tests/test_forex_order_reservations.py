"""Durable, atomic order reservations across independent SQLite connections.

Every test here drives at least two engines over **separate** SQLite
connections, and the multi-process tests drive separate operating-system
processes.  The point is the same in each: a reservation is a promise kept by
the database, so at most one holder of a conflicting promise can exist even
when two callers read the same "remaining quantity" at the same moment.

Offline and research-only: synthetic fixture data, no network, no broker
connection, no live order path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex import build_fixture_platform
from axiom_forex.database.audit import DurableAuditLog
from axiom_forex.database.repository import ForexRepository
from axiom_forex.database.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_schema,
    schema_version,
)
from axiom_forex.execution.paper_engine import PaperExecutionEngine
from axiom_forex.execution.recovery import PaperStateRecovery
from axiom_forex.execution.reservations import (
    KIND_CLOSE,
    KIND_ENTRY,
    STATUS_REJECTED,
    STATUS_RESERVED,
    OrderReservationBook,
    ReservationRequest,
    from_units,
    to_units,
)
from axiom_forex.strategies.base import Signal

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Reasons that mean "the database refused a conflicting promise".  The
#: unresolved-order rule is checked first, so a losing racer can be refused
#: before the quantity or exposure arithmetic is reached; both are fail-closed.
FAIL_CLOSED_REASONS = frozenset(
    {
        "UNRESOLVED_ORDER_PRESENT",
        "close_quantity_exceeds_position",
        "entry_exposure_limit_exceeded",
        "entry_exposure_unpriced",
    }
)


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


def _open_position(platform, *, run_id: str, decision_index: int = 5):
    """Open one fixture position through the gate and the planner."""

    plan = platform.planner.plan(
        signal=Signal.BUY,
        positions={},
        bars=platform.bars,
        decision_index=decision_index,
        run_id=run_id,
        strategy_id=run_id,
    )
    record, fill = platform.paper_execution.execute(
        plan.request,
        plan.context,
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="OPEN",
        position_id=None,
    )
    assert record.state == "FILLED", record.reason
    return fill.position_id, plan.fill_at


def _close_plan(platform, position_id: str, *, run_id: str, decision_index: int = 8):
    return platform.planner.plan(
        signal=Signal.EXIT,
        positions=platform.portfolio.positions,
        bars=platform.bars,
        decision_index=decision_index,
        run_id=run_id,
        strategy_id=run_id,
    )


def _resized_close(plan, *, request_id: str, quantity: Decimal | None = None):
    """A close of ``quantity`` against the same position, under a new id."""

    request = replace(plan.request, request_id=request_id)
    if quantity is None:
        return request
    assert request.close_intent is not None
    return replace(
        request,
        quantity=quantity,
        close_intent=replace(request.close_intent, quantity=quantity),
    )


def _persist_portfolio(platform, *, at: datetime, decision_index: int = 8) -> None:
    """Write the portfolio snapshot recovery needs (as a real run would)."""

    bar = platform.bars[min(decision_index, len(platform.bars) - 1)]
    platform.repository.save_snapshot(
        f"portfolio:durable-reservation:{id(platform)}",
        "portfolio",
        platform.portfolio.snapshot(at=at, bar=bar),
    )


def _payload(request_id: str, *, state: str = "SIMULATED") -> dict:
    stamp = "2026-03-10T00:00:00+00:00"
    return {
        "request_id": request_id,
        "simulation_id": "simulation-" + request_id,
        "state": state,
        "created_at": stamp,
        "updated_at": stamp,
        "reason": "",
        "payload_fingerprint": "",
        "receipt": None,
        "is_unresolved": state
        not in {"FILLED", "REJECTED", "CANCELLED"},
    }


def _reservation(request_id: str, *, quantity: str, position_id: str = "position:probe"):
    return ReservationRequest(
        request_id=request_id,
        kind=KIND_CLOSE,
        quantity=Decimal(quantity),
        position_id=position_id,
        position_quantity=Decimal("0.10"),
    )


def _active_close_units(repository: ForexRepository) -> int:
    row = repository.connection.execute(
        "SELECT COALESCE(SUM(quantity_units), 0) FROM order_reservations "
        "WHERE released_at IS NULL AND kind = 'CLOSE'"
    ).fetchone()
    return int(row[0])


def _count(repository: ForexRepository, sql: str, params: tuple = ()) -> int:
    return int(repository.connection.execute(sql, params).fetchone()[0])


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


def test_schema_v2_adds_the_reservation_book_without_touching_v1_data(tmp_path):
    """An existing v1 database upgrades in place and keeps its rows."""

    import sqlite3

    assert SCHEMA_VERSION >= 2
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_meta(version) VALUES (1)")
        for statement in MIGRATIONS[1]:
            connection.execute(statement)
        import json as _json

        connection.execute(
            "INSERT INTO orders(request_id, simulation_id, state, payload, updated_at) "
            "VALUES ('legacy-order', 'legacy-sim', 'FILLED', ?, "
            "'2026-03-10T00:00:00+00:00')",
            (_json.dumps(_payload("legacy-order", state="FILLED")),),
        )
        connection.commit()
        assert schema_version(connection) == 1
    finally:
        connection.close()

    repository = ForexRepository(str(path))
    try:
        assert repository.schema_version == SCHEMA_VERSION == 2
        assert [order["request_id"] for order in repository.list_orders()] == [
            "legacy-order"
        ]
        tables = {
            row[0]
            for row in repository.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"order_reservations", "position_guards"} <= tables
        counts = repository.health()["counts"]
        assert counts["order_reservations"] == 0
        assert counts["position_guards"] == 0
        assert repository.active_reservations() == []
    finally:
        repository.close()


def test_a_newer_database_is_never_downgraded(tmp_path):
    import sqlite3

    path = tmp_path / "future.db"
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_meta(version) VALUES (99)")
        connection.commit()
        with pytest.raises(ValueError, match="newer than this repository version"):
            apply_schema(connection)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# the limit arithmetic lives in SQL, not in memory
# ---------------------------------------------------------------------------


def test_close_quantity_limits_are_decided_by_the_database(tmp_path):
    """A reservation already in the table reduces what the next one may take."""

    repository = ForexRepository(str(tmp_path / "limits.db"))
    try:
        book = OrderReservationBook(repository)
        stamp = "2026-03-10T00:00:00+00:00"
        # A resume row written by an earlier connection (no order row needed:
        # the arithmetic must not depend on the order table).
        repository.insert_reservation(
            request_id="earlier-close",
            kind=KIND_CLOSE,
            state="SIMULATED",
            position_id="position:probe",
            quantity_units=to_units(Decimal("0.06")),
            exposure_units=0,
            exposure_key="",
            payload_fingerprint="",
            created_at=stamp,
            updated_at=stamp,
        )
        at = datetime(2026, 3, 10, tzinfo=timezone.utc)

        denied = book.submit(
            _reservation("second-close", quantity="0.06"),
            at=at,
            simulate=lambda: _payload("second-close"),
        )
        assert denied.status == STATUS_REJECTED
        assert denied.reason == "close_quantity_exceeds_position"
        assert repository.get_order("second-close") is None
        assert repository.get_reservation("second-close") is None

        allowed = book.submit(
            _reservation("third-close", quantity="0.04"),
            at=at,
            simulate=lambda: _payload("third-close"),
        )
        assert allowed.status == STATUS_RESERVED
        assert from_units(_active_close_units(repository)) == Decimal("0.10")
        assert book.pending_close_quantities() == {
            "position:probe": Decimal("0.06") + Decimal("0.04")
        }
    finally:
        repository.close()


def test_entry_exposure_cap_is_decided_by_the_database(tmp_path):
    """Two entries cannot jointly exceed the configured exposure cap."""

    repository = ForexRepository(str(tmp_path / "exposure.db"))
    try:
        notional = Decimal("0.11034")
        cap = notional * Decimal("1.5")
        book = OrderReservationBook(
            repository, max_entry_exposure=cap, account_currency="CCC"
        )
        stamp = "2026-03-10T00:00:00+00:00"
        repository.insert_reservation(
            request_id="earlier-entry",
            kind=KIND_ENTRY,
            state="SIMULATED",
            position_id="",
            quantity_units=to_units(Decimal("0.10")),
            exposure_units=to_units(notional),
            exposure_key="",
            payload_fingerprint="",
            created_at=stamp,
            updated_at=stamp,
        )
        at = datetime(2026, 3, 10, tzinfo=timezone.utc)

        def entry(request_id: str, exposure: Decimal | None) -> ReservationRequest:
            return ReservationRequest(
                request_id=request_id,
                kind=KIND_ENTRY,
                quantity=Decimal("0.10"),
                exposure=exposure,
            )

        denied = book.submit(
            entry("second-entry", notional),
            at=at,
            simulate=lambda: _payload("second-entry"),
        )
        assert denied.status == STATUS_REJECTED
        assert denied.reason == "entry_exposure_limit_exceeded"
        assert repository.get_order("second-entry") is None

        unpriced = book.submit(
            entry("unpriced-entry", None),
            at=at,
            simulate=lambda: _payload("unpriced-entry"),
        )
        assert unpriced.status == STATUS_REJECTED
        assert unpriced.reason == "entry_exposure_unpriced"

        assert repository.reserved_entry_exposure_units("") == to_units(notional)
    finally:
        repository.close()


# ---------------------------------------------------------------------------
# two independent connections over one database
# ---------------------------------------------------------------------------


def test_two_connections_cannot_both_close_the_same_position(tmp_path):
    database_path = str(tmp_path / "two-connections.db")
    first = build_fixture_platform(database_path=database_path)
    second = None
    try:
        position_id, at = _open_position(first, run_id="durable-race")
        _persist_portfolio(first, at=at)

        second = build_fixture_platform(database_path=database_path)
        # The second connection's own ledger still holds the whole position:
        # nothing in its memory says a close is already in flight.
        assert second.portfolio.positions[position_id].quantity == Decimal("0.10")
        # The second connection recovered the filled entry from the database
        # but knows nothing about a close: only the database does.
        assert second.paper_execution.unresolved_orders() == ()

        plan = _close_plan(first, position_id, run_id="durable-race")
        first_record = first.paper_execution.submit(plan.request, plan.context)
        assert first_record.state == "SIMULATED"

        losing = second.paper_execution.submit(
            _resized_close(plan, request_id="durable-race-second"),
            _close_plan(second, position_id, run_id="durable-race").context,
        )
        assert losing.state == "REJECTED"
        assert losing.reason in FAIL_CLOSED_REASONS

        assert len(first.repository.active_reservations()) == 1
        assert from_units(_active_close_units(first.repository)) == Decimal("0.10")
        assert second.paper_execution.pending_close_quantities() == {
            position_id: Decimal("0.10")
        }
        # The durable refusal is idempotent: replaying it answers the same way.
        replay = second.paper_execution.submit(
            _resized_close(plan, request_id="durable-race-second"),
            _close_plan(second, position_id, run_id="durable-race").context,
        )
        assert replay.state == "REJECTED"
        assert len(first.repository.active_reservations()) == 1
    finally:
        if second is not None:
            second.close()
        first.close()


def test_a_cancel_releases_the_quantity_for_every_connection(tmp_path):
    database_path = str(tmp_path / "cancel-release.db")
    first = build_fixture_platform(database_path=database_path)
    second = None
    try:
        position_id, at = _open_position(first, run_id="durable-cancel")
        _persist_portfolio(first, at=at)
        second = build_fixture_platform(database_path=database_path)

        plan = _close_plan(first, position_id, run_id="durable-cancel")
        held = first.paper_execution.submit(plan.request, plan.context)
        assert held.state == "SIMULATED"

        cancelled = first.paper_execution.cancel(
            held.request_id,
            cancelled_at=plan.fill_at,
            reason="held_by_operator",
        )
        assert cancelled.state == "CANCELLED"
        assert first.repository.active_reservations() == []

        released = second.paper_execution.submit(
            _resized_close(plan, request_id="durable-cancel-other"),
            _close_plan(second, position_id, run_id="durable-cancel").context,
        )
        assert released.state == "SIMULATED", released.reason
        assert second.paper_execution.pending_close_quantities() == {
            position_id: Decimal("0.10")
        }
    finally:
        if second is not None:
            second.close()
        first.close()


def test_a_settled_partial_close_lowers_the_durable_ceiling(tmp_path):
    database_path = str(tmp_path / "partial-ceiling.db")
    first = build_fixture_platform(database_path=database_path)
    second = None
    try:
        position_id, at = _open_position(first, run_id="durable-partial")
        _persist_portfolio(first, at=at)
        second = build_fixture_platform(database_path=database_path)

        plan = _close_plan(first, position_id, run_id="durable-partial")
        half = _resized_close(
            plan, request_id="durable-partial-half", quantity=Decimal("0.05")
        )
        record = first.paper_execution.submit(half, plan.context)
        assert record.state == "SIMULATED"
        first.paper_execution.settle(
            record.simulation_id,
            fill_price=plan.fill_price,
            fill_at=plan.fill_at,
            action="CLOSE",
            side=half.side_value,
            quantity=half.quantity_decimal(),
            position_id=position_id,
        )
        assert first.portfolio.positions[position_id].quantity == Decimal("0.05")
        assert first.repository.active_reservations() == []
        guard = first.repository.get_position_guard(position_id)
        assert from_units(guard) == Decimal("0.05")

        # The second connection's ledger is stale: it still believes 0.10 is
        # open.  The database remembers what is actually left.
        assert second.portfolio.positions[position_id].quantity == Decimal("0.10")
        stale = _resized_close(plan, request_id="durable-partial-stale")
        refused = second.paper_execution.submit(
            stale, _close_plan(second, position_id, run_id="durable-partial").context
        )
        assert refused.state == "REJECTED"
        assert refused.reason == "close_quantity_exceeds_position"

        correct = _resized_close(
            plan, request_id="durable-partial-remainder", quantity=Decimal("0.05")
        )
        accepted = second.paper_execution.submit(
            correct, _close_plan(second, position_id, run_id="durable-partial").context
        )
        assert accepted.state == "SIMULATED", accepted.reason
        assert from_units(_active_close_units(first.repository)) == Decimal("0.05")
    finally:
        if second is not None:
            second.close()
        first.close()


def test_duplicate_request_ids_reserve_once_across_connections(tmp_path):
    database_path = str(tmp_path / "duplicate-ids.db")
    first = build_fixture_platform(database_path=database_path)
    second = None
    try:
        position_id, at = _open_position(first, run_id="durable-duplicate")
        _persist_portfolio(first, at=at)
        second = build_fixture_platform(database_path=database_path)

        plan = _close_plan(first, position_id, run_id="durable-duplicate")
        request = _resized_close(plan, request_id="durable-duplicate-id")
        original = first.paper_execution.submit(request, plan.context)
        assert original.state == "SIMULATED"

        duplicate = second.paper_execution.submit(
            request, _close_plan(second, position_id, run_id="durable-duplicate").context
        )
        assert duplicate.state == "SIMULATED"
        assert duplicate.simulation_id == original.simulation_id
        assert duplicate.request_id == original.request_id
        assert len(first.repository.active_reservations()) == 1
        assert from_units(_active_close_units(first.repository)) == Decimal("0.10")

        # The same id carrying a different order is refused, never absorbed.
        with pytest.raises(ValueError, match="request_id_reused_with_different_payload"):
            second.paper_execution.submit(
                _resized_close(
                    plan,
                    request_id="durable-duplicate-id",
                    quantity=Decimal("0.05"),
                ),
                _close_plan(
                    second, position_id, run_id="durable-duplicate"
                ).context,
            )
    finally:
        if second is not None:
            second.close()
        first.close()


def test_a_failed_transaction_leaves_no_reservation_and_no_order(tmp_path):
    """A crash inside the atomic unit rolls back everything it touched."""

    database_path = str(tmp_path / "rollback.db")
    platform = build_fixture_platform(database_path=database_path)
    try:
        position_id, at = _open_position(platform, run_id="durable-rollback")
        _persist_portfolio(platform, at=at)
        plan = _close_plan(platform, position_id, run_id="durable-rollback")
        request = _resized_close(plan, request_id="durable-rollback-failing")

        class ExplodingBroker:
            def __init__(self, inner):
                self.inner = inner

            def simulate_order_intent(self, request, context):
                raise RuntimeError("simulated_process_death")

        broken = PaperExecutionEngine(
            broker=ExplodingBroker(platform.broker),
            portfolio=platform.portfolio,
            audit_log=platform.audit_log,
            reservations=OrderReservationBook(platform.repository),
        )
        with pytest.raises(RuntimeError, match="simulated_process_death"):
            broken.submit(request, plan.context)

        assert platform.repository.get_order("durable-rollback-failing") is None
        assert platform.repository.get_reservation("durable-rollback-failing") is None
        assert platform.repository.active_reservations() == []
        assert from_units(_active_close_units(platform.repository)) == Decimal(0)
        assert platform.paper_execution.pending_close_quantities() == {}
        # The failed attempt consumed nothing: the same quantity is still
        # available to a healthy engine.
        healthy = platform.paper_execution.submit(request, plan.context)
        assert healthy.state == "SIMULATED", healthy.reason
        assert from_units(_active_close_units(platform.repository)) == Decimal("0.10")
    finally:
        platform.close()


def test_a_restart_recovers_the_reservation_and_keeps_blocking(tmp_path):
    database_path = str(tmp_path / "restart.db")
    first = build_fixture_platform(database_path=database_path)
    position_id = None
    plan = None
    try:
        position_id, at = _open_position(first, run_id="durable-restart")
        _persist_portfolio(first, at=at)
        plan = _close_plan(first, position_id, run_id="durable-restart")
        held = first.paper_execution.submit(plan.request, plan.context)
        assert held.state == "SIMULATED"
    finally:
        # Simulate the process dying before settlement.
        first.close()

    second = build_fixture_platform(database_path=database_path)
    try:
        recovery = second.recovery_result
        assert recovery is not None
        assert recovery.audit_view()["unresolved_order_count"] == 1
        assert second.paper_execution.pending_close_quantities() == {
            position_id: Decimal("0.10")
        }
        assert position_id in second.portfolio.positions
        assert [item.order_id for item in second.paper_execution.unresolved_orders()] == [
            plan.request.request_id
        ]
        blocked = second.paper_execution.submit(
            _resized_close(plan, request_id="durable-restart-second"),
            _close_plan(second, position_id, run_id="durable-restart").context,
        )
        assert blocked.state == "REJECTED"
        assert blocked.reason == "UNRESOLVED_ORDER_PRESENT"
        assert from_units(_active_close_units(second.repository)) == Decimal("0.10")
    finally:
        second.close()


# ---------------------------------------------------------------------------
# recovery must fail closed on ambiguous durable records
# ---------------------------------------------------------------------------


def _recover(platform):
    helper = PaperStateRecovery(
        platform.repository, DurableAuditLog(repository=platform.repository)
    )
    return helper.recover(
        engine=platform.paper_execution, portfolio=platform.portfolio
    )


def test_recovery_refuses_a_reservation_without_its_order(tmp_path):
    database_path = str(tmp_path / "ambiguous-a.db")
    platform = build_fixture_platform(database_path=database_path)
    try:
        position_id, at = _open_position(platform, run_id="ambiguous-a")
        _persist_portfolio(platform, at=at)
        plan = _close_plan(platform, position_id, run_id="ambiguous-a")
        held = platform.paper_execution.submit(plan.request, plan.context)
        assert held.state == "SIMULATED"

        platform.repository.connection.execute(
            "DELETE FROM orders WHERE request_id = ?", (held.request_id,)
        )
        platform.repository.connection.commit()

        with pytest.raises(ValueError, match="recovered_reservation_without_order"):
            _recover(platform)
    finally:
        platform.close()


def test_recovery_refuses_an_unresolved_order_without_a_reservation(tmp_path):
    database_path = str(tmp_path / "ambiguous-b.db")
    platform = build_fixture_platform(database_path=database_path)
    try:
        position_id, at = _open_position(platform, run_id="ambiguous-b")
        _persist_portfolio(platform, at=at)
        plan = _close_plan(platform, position_id, run_id="ambiguous-b")
        held = platform.paper_execution.submit(plan.request, plan.context)
        assert held.state == "SIMULATED"

        platform.repository.connection.execute(
            "DELETE FROM order_reservations WHERE request_id = ?", (held.request_id,)
        )
        platform.repository.connection.commit()

        with pytest.raises(
            ValueError, match="unresolved_order_without_reservation"
        ):
            _recover(platform)
    finally:
        platform.close()


# ---------------------------------------------------------------------------
# independent operating-system processes
# ---------------------------------------------------------------------------

WORKER = '''
"""One racing submitter: build the platform, wait for the start gun, submit."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

REPO_ROOT = sys.argv[1]
sys.path.insert(0, REPO_ROOT)

from axiom_forex import build_fixture_platform
from axiom_forex.execution.paper_engine import PaperExecutionEngine
from axiom_forex.execution.reservations import OrderReservationBook
from axiom_forex.strategies.base import Signal


def main() -> int:
    repo_root, database_path, mode, request_id, run_id, start_iso, cap, out, extra = (
        sys.argv[1:10]
    )
    if mode == "store":
        return _store_race(database_path, request_id, start_iso, out, extra)
    platform = build_fixture_platform(database_path=database_path)
    try:
        start = datetime.fromisoformat(start_iso)
        delay = (start - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            time.sleep(delay)
        limit = None if cap in ("", "none") else Decimal(cap)
        engine = PaperExecutionEngine(
            broker=platform.broker,
            portfolio=platform.portfolio,
            audit_log=platform.audit_log,
            reservations=OrderReservationBook(
                platform.repository,
                max_entry_exposure=limit,
                account_currency=platform.account.account_currency_code,
            ),
        )
        if mode == "close":
            plan = platform.planner.plan(
                signal=Signal.EXIT,
                positions=platform.portfolio.positions,
                bars=platform.bars,
                decision_index=8,
                run_id=run_id,
                strategy_id=run_id,
            )
        elif mode == "entry":
            plan = platform.planner.plan(
                signal=Signal.BUY,
                positions={},
                bars=platform.bars,
                decision_index=5,
                run_id=run_id,
                strategy_id=run_id,
            )
        else:
            raise SystemExit("unknown mode: " + mode)
        request = replace(plan.request, request_id=request_id)
        record = engine.submit(request, plan.context)
        result = {
            "request_id": record.request_id,
            "state": record.state,
            "reason": record.reason,
            "simulation_id": record.simulation_id,
        }
    finally:
        platform.close()
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return 0




def _store_race(database_path, request_id, start_iso, out, extra):
    """Reserve directly, holding the write lock for a while on purpose.

    The sleep inside ``simulate`` widens the interleaving so that a store
    without ``BEGIN IMMEDIATE`` would let both processes read "nothing
    reserved" and both insert.
    """

    from axiom_forex.database.repository import ForexRepository
    from axiom_forex.execution.reservations import (
        KIND_CLOSE,
        OrderReservationBook,
        ReservationRequest,
        STATUS_RESERVED,
    )

    position_id, quantity = extra.split("|")
    repository = ForexRepository(database_path)
    try:
        book = OrderReservationBook(repository)
        start = datetime.fromisoformat(start_iso)
        delay = (start - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            time.sleep(delay)

        def simulate() -> dict:
            time.sleep(1.0)
            return {
                "request_id": request_id,
                "simulation_id": "simulation-" + request_id,
                "state": "SIMULATED",
                "created_at": start_iso,
                "updated_at": start_iso,
                "reason": "",
                "payload_fingerprint": "",
                "receipt": None,
                "is_unresolved": True,
            }

        outcome = book.submit(
            ReservationRequest(
                request_id=request_id,
                kind=KIND_CLOSE,
                quantity=Decimal(quantity),
                position_id=position_id,
                position_quantity=Decimal("0.10"),
            ),
            at=start,
            simulate=simulate,
        )
        result = {
            "request_id": request_id,
            "state": "SIMULATED"
            if outcome.status == STATUS_RESERVED
            else "REJECTED",
            "reason": "" if outcome.status == STATUS_RESERVED else outcome.reason,
            "simulation_id": "",
        }
    finally:
        repository.close()
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _start_workers(
    tmp_path: Path,
    *,
    request: list[tuple[str, str, str, str]],
    database_path: str,
    extra: str = "",
) -> list[dict]:
    """Run one subprocess per tuple: (mode, request_id, run_id, cap)."""

    worker_path = tmp_path / "reservation_worker.py"
    worker_path.write_text(WORKER, encoding="utf-8")
    start_iso = (datetime.now(timezone.utc) + timedelta(seconds=2.0)).isoformat()
    processes = []
    for index, (mode, request_id, run_id, cap) in enumerate(request):
        out_path = tmp_path / f"worker-{index}.json"
        processes.append(
            (
                out_path,
                subprocess.Popen(
                    [
                        sys.executable,
                        str(worker_path),
                        str(REPO_ROOT),
                        database_path,
                        mode,
                        request_id,
                        run_id,
                        start_iso,
                        cap,
                        str(out_path),
                        extra,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(REPO_ROOT),
                ),
            )
        )
    results = []
    for out_path, process in processes:
        stdout, stderr = process.communicate(timeout=180)
        if process.returncode != 0:
            raise AssertionError(
                "worker failed: " + stdout.decode()[-2000:] + stderr.decode()[-4000:]
            )
        results.append(json.loads(out_path.read_text(encoding="utf-8")))
    return results


def test_two_processes_cannot_both_close_the_same_position(tmp_path):
    database_path = str(tmp_path / "process-close.db")
    platform = build_fixture_platform(database_path=database_path)
    try:
        position_id, at = _open_position(platform, run_id="process-close")
        _persist_portfolio(platform, at=at)
    finally:
        platform.close()

    results = _start_workers(
        tmp_path,
        database_path=database_path,
        request=[
            ("close", "process-close-a", "process-close-a", ""),
            ("close", "process-close-b", "process-close-b", ""),
        ],
    )

    accepted = [item for item in results if item["state"] == "SIMULATED"]
    refused = [item for item in results if item["state"] == "REJECTED"]
    assert len(accepted) == 1, results
    assert len(refused) == 1, results
    assert refused[0]["reason"] in FAIL_CLOSED_REASONS

    repository = ForexRepository(database_path)
    try:
        assert len(repository.active_reservations()) == 1
        assert from_units(_active_close_units(repository)) == Decimal("0.10")
        assert _count(
            repository,
            "SELECT COUNT(*) FROM orders WHERE state NOT IN "
            "('FILLED', 'REJECTED', 'CANCELLED')",
        ) == 1
        # The loser's row survives as history (released), so exactly one row
        # still holds quantity.
        assert _count(repository, "SELECT COUNT(*) FROM order_reservations") == 2
        assert (
            _count(
                repository,
                "SELECT COUNT(*) FROM order_reservations WHERE released_at IS NULL",
            )
            == 1
        )
    finally:
        repository.close()


def test_two_processes_sharing_a_request_id_reserve_once(tmp_path):
    database_path = str(tmp_path / "process-duplicate.db")
    platform = build_fixture_platform(database_path=database_path)
    try:
        position_id, at = _open_position(platform, run_id="process-duplicate")
        _persist_portfolio(platform, at=at)
    finally:
        platform.close()

    results = _start_workers(
        tmp_path,
        database_path=database_path,
        request=[
            ("close", "process-duplicate-id", "process-duplicate-a", ""),
            ("close", "process-duplicate-id", "process-duplicate-b", ""),
        ],
    )

    # Both processes answer, but they answer about the *same* order: one row,
    # one reservation, one simulation.
    assert all(item["state"] != "REJECTED" for item in results), results
    assert len({item["simulation_id"] for item in results}) == 1, results

    repository = ForexRepository(database_path)
    try:
        assert _count(
            repository,
            "SELECT COUNT(*) FROM order_reservations WHERE request_id = ?",
            ("process-duplicate-id",),
        ) == 1
        assert _count(
            repository,
            "SELECT COUNT(*) FROM orders WHERE request_id = ?",
            ("process-duplicate-id",),
        ) == 1
        assert from_units(_active_close_units(repository)) == Decimal("0.10")
    finally:
        repository.close()


def test_two_processes_cannot_exceed_one_entry_exposure_cap(tmp_path):
    database_path = str(tmp_path / "process-entry.db")
    platform = build_fixture_platform(database_path=database_path)
    try:
        probe = platform.planner.plan(
            signal=Signal.BUY,
            positions={},
            bars=platform.bars,
            decision_index=5,
            run_id="process-entry-probe",
            strategy_id="process-entry-probe",
        )
        quote = probe.request.quote
        assert quote is not None
        _, ask = quote.prices()
        notional = probe.request.quantity_decimal() * ask
        cap = notional * Decimal("1.5")
        # One notional is already spoken for by an earlier connection, so the
        # cap cannot accommodate a second entry no matter who wins the race.
        platform.repository.insert_reservation(
            request_id="process-entry-existing",
            kind=KIND_ENTRY,
            state="SIMULATED",
            position_id="",
            quantity_units=to_units(probe.request.quantity_decimal()),
            exposure_units=to_units(notional),
            exposure_key="",
            payload_fingerprint="",
            created_at="2026-03-10T00:00:00+00:00",
            updated_at="2026-03-10T00:00:00+00:00",
        )
    finally:
        platform.close()

    results = _start_workers(
        tmp_path,
        database_path=database_path,
        request=[
            ("entry", "process-entry-a", "process-entry-a", str(cap)),
            ("entry", "process-entry-b", "process-entry-b", str(cap)),
        ],
    )

    assert [item["state"] for item in results] == ["REJECTED", "REJECTED"], results
    assert {item["reason"] for item in results} == {"entry_exposure_limit_exceeded"}

    repository = ForexRepository(database_path)
    try:
        assert from_units(repository.reserved_entry_exposure_units("")) == notional
        assert _count(repository, "SELECT COUNT(*) FROM order_reservations") == 1
    finally:
        repository.close()


def test_simultaneous_processes_serialise_on_the_database_write_lock(tmp_path):
    """Both processes enter at the same instant; the database picks one.

    Each worker holds the write lock for a second inside its transaction.  A
    store without ``BEGIN IMMEDIATE`` would let both read "nothing reserved"
    and both insert 0.06 of a 0.10 position; with it the second waits, then
    sees the first and is refused.
    """

    database_path = str(tmp_path / "process-lock.db")
    repository = ForexRepository(database_path)
    try:
        repository.lower_position_guard(
            "position:contended",
            quantity_units=to_units(Decimal("0.10")),
            updated_at="2026-03-10T00:00:00+00:00",
        )
    finally:
        repository.close()

    results = _start_workers(
        tmp_path,
        database_path=database_path,
        request=[
            ("store", "contended-a", "contended-a", ""),
            ("store", "contended-b", "contended-b", ""),
        ],
        extra="position:contended|0.06",
    )

    accepted = [item for item in results if item["state"] == "SIMULATED"]
    refused = [item for item in results if item["state"] == "REJECTED"]
    assert len(accepted) == 1, results
    assert len(refused) == 1, results
    assert refused[0]["reason"] in FAIL_CLOSED_REASONS

    repository = ForexRepository(database_path)
    try:
        reserved = repository.reserved_close_units("position:contended")
        assert from_units(reserved) == Decimal("0.06")
        assert reserved <= to_units(Decimal("0.10"))
        assert len(repository.active_reservations()) == 1
    finally:
        repository.close()


def test_a_quantity_the_store_cannot_represent_is_refused_not_crashed(tmp_path):
    """A fine-grained quantity is a rejected order, not an exception at the DB.

    The reservation book stores 1e-8 units exactly.  Anything finer would have
    to be rounded (silently changing the order) or crash the transaction
    (leaving no auditable record), so it is refused up front.
    """

    platform = build_fixture_platform(database_path=str(tmp_path / "precision.db"))
    try:
        plan = platform.planner.plan(
            signal=Signal.BUY,
            positions={},
            bars=platform.bars,
            decision_index=5,
            run_id="precision",
            strategy_id="precision",
        )
        tiny = replace(
            plan.request,
            request_id="precision-too-fine",
            quantity=Decimal("0.000000001"),
        )
        record = platform.paper_execution.submit(tiny, plan.context)
        assert record.state == "REJECTED"
        assert record.reason == "unsupported_quantity_precision"
        assert platform.repository.active_reservations() == []
        assert platform.repository.get_order("precision-too-fine") is not None
    finally:
        platform.close()
