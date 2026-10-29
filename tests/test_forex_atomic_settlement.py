"""Atomic settlement: order, fill, ledger, reservation, guard, audit.

Each test drives the durable path (`PaperExecutionEngine` with an
`OrderReservationBook`) and checks the two properties that make the paper
workflow trustworthy:

* **all-or-nothing** -- a failure at any point inside settlement leaves no
  partial order, fill, position, reservation release, or audit event behind;
* **exactly-once** -- replaying a settlement (retry, restart, a second process)
  returns the committed fill instead of applying P&L, fees, rollover, or
  position changes a second time.

Offline and research-only: synthetic fixtures, no network, no broker, no live
order path.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex import build_fixture_platform
from axiom_forex.database.audit import DurableAuditLog
from axiom_forex.database.repository import ForexRepository
from axiom_forex.execution.reservations import (
    OrderReservationBook,
    ReservationRequest,
)
from axiom_forex.execution.settlement import settlement_snapshot_id
from axiom_forex.risk.entry_limits import (
    DATA_CLASS_OPERATOR_DECLARED,
    DATA_CLASS_SYNTHETIC_FIXTURE,
    EntryExposureLimit,
)
from axiom_forex.strategies.base import Signal

REPO_ROOT = Path(__file__).resolve().parents[1]

ATOMIC_RECORDS = (
    "orders",
    "fills",
    "positions",
    "snapshots",
    "order_reservations",
    "audit_events",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _plan_entry(platform, *, run_id: str = "atomic", decision_index: int = 5):
    return platform.planner.plan(
        signal=Signal.BUY,
        positions={},
        bars=platform.bars,
        decision_index=decision_index,
        run_id=run_id,
        strategy_id=run_id,
    )


def _open_position(platform, *, run_id: str = "atomic"):
    plan = _plan_entry(platform, run_id=run_id)
    record, fill = platform.paper_execution.execute(
        plan.request,
        plan.context,
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="OPEN",
        position_id=None,
    )
    assert record.state == "FILLED", record.reason
    return fill.position_id, plan


def _close_plan(platform, position_id: str, *, run_id: str = "atomic", index: int = 8):
    return platform.planner.plan(
        signal=Signal.EXIT,
        positions=platform.portfolio.positions,
        bars=platform.bars,
        decision_index=index,
        run_id=run_id,
        strategy_id=run_id,
    )


def _counts(repository: ForexRepository) -> dict[str, int]:
    counts = {}
    for table in ATOMIC_RECORDS:
        counts[table] = int(
            repository.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
    return counts


def _fill_events(platform) -> list[dict]:
    return [
        event
        for event in platform.audit_log.events
        if event.get("event_type") == "PAPER_ORDER_FILLED"
    ]


def _failing(platform, *, method: str):
    """Patch one repository write to raise, without touching anything else."""

    repository = platform.repository
    original = getattr(repository, method)

    def explode(*args, **kwargs):
        raise RuntimeError(f"injected_failure:{method}")

    setattr(repository, method, explode)
    return original


# ---------------------------------------------------------------------------
# one settlement, one transaction
# ---------------------------------------------------------------------------


def test_a_settlement_commits_every_record_together(tmp_path):
    platform = build_fixture_platform(database_path=str(tmp_path / "atomic.db"))
    try:
        plan = _plan_entry(platform)
        before = _counts(platform.repository)

        record, fill = platform.paper_execution.execute(
            plan.request,
            plan.context,
            fill_price=plan.fill_price,
            fill_at=plan.fill_at,
            action="OPEN",
            position_id=None,
        )
        assert record.state == "FILLED"
        assert fill is not None

        after = _counts(platform.repository)
        # Order, fill, position, settlement snapshot, released reservation, and
        # the audit event are all present -- written by the same transaction.
        assert after["orders"] == before["orders"] + 1
        assert after["fills"] == before["fills"] + 1
        assert after["positions"] > before["positions"]
        assert after["snapshots"] == before["snapshots"] + 1
        assert after["order_reservations"] == before["order_reservations"] + 1
        assert after["audit_events"] > before["audit_events"]

        order = platform.repository.get_order(plan.request.request_id)
        assert order["state"] == "FILLED"
        stored_fill = platform.repository.get_fill(fill.simulation_id)
        assert stored_fill["position_id"] == fill.position_id
        snapshot = platform.repository.get_snapshot(
            settlement_snapshot_id(fill.simulation_id)
        )
        assert snapshot["settlement"]["simulation_id"] == fill.simulation_id
        assert snapshot["settlement"]["action"] == "OPEN"
        # Daily counters travel in the same snapshot as the position.
        assert snapshot["trades_today"] == 1
        assert snapshot["realized_pnl_today_account"] == "0"
        reservation = platform.repository.get_reservation(plan.request.request_id)
        assert reservation["released_at"] is not None
        # And the ledger in memory equals the committed snapshot.
        assert platform.portfolio.positions[fill.position_id].quantity == Decimal("0.10")
        live = platform.repository.active_reservations()
        assert live == []
        events = _fill_events(platform)
        assert len(events) == 1
        assert events[0]["atomic"]["order"] == "FILLED"
    finally:
        platform.close()


@pytest.mark.parametrize(
    "method",
    ["save_fills", "save_positions", "save_snapshot", "release_reservation", "record_audit"],
)
def test_a_failure_at_any_settlement_boundary_rolls_everything_back(tmp_path, method):
    """Inject a failure at each write inside the unit; nothing may survive."""

    platform = build_fixture_platform(database_path=str(tmp_path / f"{method}.db"))
    try:
        plan = _plan_entry(platform)
        submitted = platform.paper_execution.submit(plan.request, plan.context)
        assert submitted.state == "SIMULATED"
        before = _counts(platform.repository)
        positions_before = dict(platform.portfolio.positions)
        trades_before = len(platform.portfolio.closed_trades)

        original = _failing(platform, method=method)
        try:
            with pytest.raises(RuntimeError, match=f"injected_failure:{method}"):
                platform.paper_execution.settle(
                    submitted.simulation_id,
                    fill_price=plan.fill_price,
                    fill_at=plan.fill_at,
                    action="OPEN",
                    side=plan.request.side_value,
                    quantity=plan.request.quantity_decimal(),
                )
        finally:
            setattr(platform.repository, method, original)

        after = _counts(platform.repository)
        assert after == before, method
        assert platform.repository.get_fill(submitted.simulation_id) is None
        assert platform.repository.get_snapshot(
            settlement_snapshot_id(submitted.simulation_id)
        ) is None
        assert platform.repository.get_order(plan.request.request_id)["state"] == "SIMULATED"
        reservation = platform.repository.get_reservation(plan.request.request_id)
        assert reservation["released_at"] is None
        assert len(platform.repository.active_reservations()) == 1
        # Neither the live book nor the in-memory audit cache moved.
        assert platform.portfolio.positions == positions_before
        assert len(platform.portfolio.closed_trades) == trades_before
        assert _fill_events(platform) == []
        assert platform.paper_execution.pending_close_quantities() == {}

        # The refused attempt consumed nothing: the same settlement succeeds
        # once, afterwards.
        fill = platform.paper_execution.settle(
            submitted.simulation_id,
            fill_price=plan.fill_price,
            fill_at=plan.fill_at,
            action="OPEN",
            side=plan.request.side_value,
            quantity=plan.request.quantity_decimal(),
        )
        assert platform.repository.get_fill(fill.simulation_id)["price"] == str(
            plan.fill_price
        ) or Decimal(
            platform.repository.get_fill(fill.simulation_id)["price"]
        ) == plan.fill_price
        assert len(platform.repository.list_fills()) == 1
        assert len(platform.portfolio.positions) == 1
        assert platform.repository.get_order(plan.request.request_id)["state"] == "FILLED"
    finally:
        platform.close()


# ---------------------------------------------------------------------------
# exactly-once
# ---------------------------------------------------------------------------


def test_replaying_a_settlement_never_applies_a_fill_twice(tmp_path):
    platform = build_fixture_platform(database_path=str(tmp_path / "replay.db"))
    try:
        position_id, plan = _open_position(platform, run_id="replay")
        close = _close_plan(platform, position_id, run_id="replay")
        record = platform.paper_execution.submit(close.request, close.context)
        assert record.state == "SIMULATED"

        first = platform.paper_execution.settle(
            record.simulation_id,
            fill_price=close.fill_price,
            fill_at=close.fill_at,
            action="CLOSE",
            side=close.request.side_value,
            quantity=close.request.quantity_decimal(),
            position_id=position_id,
        )
        trades_after_first = len(platform.portfolio.closed_trades)
        pnl_after_first = platform.portfolio.realized_pnl_today_account
        costs_after_first = platform.portfolio.cost_total_account
        fills_after_first = len(platform.repository.list_fills())
        assert position_id not in platform.portfolio.positions
        assert trades_after_first == 1

        # Exact same request again, in this process.
        second = platform.paper_execution.settle(
            record.simulation_id,
            fill_price=close.fill_price,
            fill_at=close.fill_at,
            action="CLOSE",
            side=close.request.side_value,
            quantity=close.request.quantity_decimal(),
            position_id=position_id,
        )
        assert second.audit_view() == first.audit_view()
        assert len(platform.portfolio.closed_trades) == trades_after_first
        assert platform.portfolio.realized_pnl_today_account == pnl_after_first
        assert platform.portfolio.cost_total_account == costs_after_first
        assert len(platform.repository.list_fills()) == fills_after_first
        assert len(platform.repository.list_snapshots(kind="portfolio")) >= 1

        # And again from a fresh engine that only knows the record.
        restarted = build_fixture_platform(
            database_path=str(tmp_path / "replay.db")
        )
        try:
            replayed = restarted.paper_execution.settle(
                record.simulation_id,
                fill_price=close.fill_price,
                fill_at=close.fill_at,
                action="CLOSE",
                side=close.request.side_value,
                quantity=close.request.quantity_decimal(),
                position_id=position_id,
            )
            assert replayed.audit_view() == first.audit_view()
            assert len(restarted.portfolio.closed_trades) == trades_after_first
            assert (
                restarted.portfolio.realized_pnl_today_account == pnl_after_first
            )
            assert len(restarted.repository.list_fills()) == fills_after_first
        finally:
            restarted.close()
    finally:
        platform.close()


def test_a_replay_with_different_numbers_is_a_conflict_not_a_second_fill(tmp_path):
    platform = build_fixture_platform(database_path=str(tmp_path / "conflict.db"))
    try:
        _position_id, plan = _open_position(platform, run_id="conflict")
        committed = next(
            item
            for item in platform.paper_execution.records()
            if item.state == "FILLED"
        )
        # Same simulation, different price: a second application would move the
        # book, so it is refused instead of silently re-priced.
        with pytest.raises(ValueError, match="settlement_conflict"):
            platform.paper_execution.settle(
                committed.simulation_id,
                fill_price=plan.fill_price + Decimal("0.01"),
                fill_at=plan.fill_at,
                action="OPEN",
                side=plan.request.side_value,
                quantity=plan.request.quantity_decimal(),
            )
        # Same simulation, different quantity: also a conflict.
        with pytest.raises(ValueError, match="settlement_conflict"):
            platform.paper_execution.settle(
                committed.simulation_id,
                fill_price=plan.fill_price,
                fill_at=plan.fill_at,
                action="OPEN",
                side=plan.request.side_value,
                quantity=Decimal("0.05"),
            )
        assert len(platform.portfolio.positions) == 1
        assert len(platform.repository.list_fills()) == 1
    finally:
        platform.close()


def test_partial_close_retries_do_not_double_count_the_slices(tmp_path):
    """Two partial closes, each retried, must total exactly one full close."""

    platform = build_fixture_platform(database_path=str(tmp_path / "partial.db"))
    try:
        position_id, plan = _open_position(platform, run_id="partial")
        close = _close_plan(platform, position_id, run_id="partial")
        halves = []
        for index, request_id in enumerate(("partial-a", "partial-b")):
            request = replace(
                close.request,
                request_id=request_id,
                quantity=Decimal("0.05"),
                close_intent=replace(close.request.close_intent, quantity=Decimal("0.05")),
            )
            record = platform.paper_execution.submit(request, close.context)
            assert record.state == "SIMULATED", record.reason
            fill = platform.paper_execution.settle(
                record.simulation_id,
                fill_price=close.fill_price,
                fill_at=close.fill_at,
                action="CLOSE",
                side=request.side_value,
                quantity=request.quantity_decimal(),
                position_id=position_id,
            )
            # Retry the same settlement twice: the committed fill is returned.
            for _ in range(2):
                again = platform.paper_execution.settle(
                    record.simulation_id,
                    fill_price=close.fill_price,
                    fill_at=close.fill_at,
                    action="CLOSE",
                    side=request.side_value,
                    quantity=request.quantity_decimal(),
                    position_id=position_id,
                )
                assert again.audit_view() == fill.audit_view()
            halves.append(fill)

        assert len(platform.portfolio.closed_trades) == 2
        assert position_id not in platform.portfolio.positions
        assert sum(trade.quantity for trade in platform.portfolio.closed_trades) == Decimal(
            "0.10"
        )
        assert len(platform.repository.list_fills()) == 3  # the entry + two halves
        assert platform.portfolio.positions == {}

        # A third close of the same position is refused, not silently ignored.
        retry = replace(
            close.request,
            request_id="partial-c",
            quantity=Decimal("0.05"),
            close_intent=replace(close.request.close_intent, quantity=Decimal("0.05")),
        )
        refused = platform.paper_execution.submit(retry, close.context)
        assert refused.state == "REJECTED"
        assert refused.reason in {
            "close_position_not_open",
            "close_quantity_exceeds_position",
            "UNRESOLVED_ORDER_PRESENT",
        }
        assert len(platform.portfolio.closed_trades) == 2
    finally:
        platform.close()


# ---------------------------------------------------------------------------
# audit cache follows the committed record
# ---------------------------------------------------------------------------


def test_the_audit_cache_only_shows_committed_events(tmp_path):
    platform = build_fixture_platform(database_path=str(tmp_path / "audit.db"))
    try:
        plan = _plan_entry(platform)
        record = platform.paper_execution.submit(plan.request, plan.context)
        cached_before = list(platform.audit_log.events)
        committed_before = len(platform.repository.recent_audit(limit=1000))

        original = _failing(platform, method="record_audit")
        try:
            with pytest.raises(RuntimeError, match="injected_failure:record_audit"):
                platform.paper_execution.settle(
                    record.simulation_id,
                    fill_price=plan.fill_price,
                    fill_at=plan.fill_at,
                    action="OPEN",
                    side=plan.request.side_value,
                    quantity=plan.request.quantity_decimal(),
                )
        finally:
            setattr(platform.repository, "record_audit", original)

        # Nothing new reached the cache, and nothing new reached SQLite.
        assert [event.get("event_type") for event in platform.audit_log.events] == [
            event.get("event_type") for event in cached_before
        ]
        assert len(platform.repository.recent_audit(limit=1000)) == committed_before
        assert platform.repository.get_fill(record.simulation_id) is None

        fill = platform.paper_execution.settle(
            record.simulation_id,
            fill_price=plan.fill_price,
            fill_at=plan.fill_at,
            action="OPEN",
            side=plan.request.side_value,
            quantity=plan.request.quantity_decimal(),
        )
        events = _fill_events(platform)
        assert len(events) == 1
        stored = [
            event
            for event in platform.repository.recent_audit(limit=1000)
            if event.get("event_type") == "PAPER_ORDER_FILLED"
        ]
        assert len(stored) == 1
        assert stored[0]["simulation_id"] == fill.simulation_id
        assert stored[0] == events[0]
    finally:
        platform.close()


def test_an_audit_event_recorded_inside_a_foreign_transaction_is_refused(tmp_path):
    """Recording outside ``atomic()`` could cache an event that rolls back."""

    repository = ForexRepository(str(tmp_path / "foreign.db"))
    try:
        log = DurableAuditLog(repository=repository)
        with repository.transaction():
            with pytest.raises(RuntimeError, match="audit_record_requires_atomic_unit"):
                log.record({"event_type": "SHOULD_NOT_SURVIVE", "live": False})
        assert log.events == []
        assert repository.recent_audit() == []
    finally:
        repository.close()


def test_recovery_rebuilds_the_cache_from_committed_rows(tmp_path):
    database_path = str(tmp_path / "rebuild.db")
    first = build_fixture_platform(database_path=database_path)
    try:
        _open_position(first, run_id="rebuild")
        committed = [
            event.get("event_type") for event in first.audit_log.events
        ]
    finally:
        first.close()

    second = build_fixture_platform(database_path=database_path)
    try:
        cached = [event.get("event_type") for event in second.audit_log.events]
        assert cached[: len(committed)] == committed
        types = [row.get("event_type") for row in second.repository.recent_audit(limit=1000)]
        assert "PAPER_ORDER_FILLED" in types
        assert "PAPER_ORDER_FILLED" in cached
        assert "PAPER_STATE_RECOVERED" in cached
    finally:
        second.close()


# ---------------------------------------------------------------------------
# a process that dies in the middle of settlement
# ---------------------------------------------------------------------------

CRASHING_SETTLER = '''
"""Settle one order, then die inside the transaction, before it can commit."""

from __future__ import annotations

import os
import sys
from dataclasses import replace

REPO_ROOT, DATABASE_PATH, REQUEST_ID, REQUESTED_AT = sys.argv[1:5]
sys.path.insert(0, REPO_ROOT)

from datetime import datetime

from axiom_forex import build_fixture_platform
from axiom_forex.database.repository import ForexRepository
from axiom_forex.strategies.base import Signal


def main() -> int:
    platform = build_fixture_platform(database_path=DATABASE_PATH)
    try:
        plan = platform.planner.plan(
            signal=Signal.BUY,
            positions={},
            bars=platform.bars,
            decision_index=5,
            run_id="crash-settle",
            strategy_id="crash-settle",
        )
        request = replace(plan.request, request_id=REQUEST_ID)
        record = platform.paper_execution.submit(request, plan.context)
        if record.state != "SIMULATED":
            raise SystemExit("submit_failed:" + record.reason)

        # Die exactly at the boundary *after* the fill, order, positions, and
        # ledger have been written but before the transaction commits.
        original = ForexRepository.save_snapshot

        def die(self, *args, **kwargs):
            os._exit(7)

        ForexRepository.save_snapshot = die
        platform.paper_execution.settle(
            record.simulation_id,
            fill_price=plan.fill_price,
            fill_at=plan.fill_at,
            action="OPEN",
            side=request.side_value,
            quantity=request.quantity_decimal(),
        )
        ForexRepository.save_snapshot = original
    finally:
        platform.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def test_a_process_dying_mid_settlement_leaves_a_consistent_database(tmp_path):
    database_path = str(tmp_path / "crash.db")
    worker = tmp_path / "crashing_settler.py"
    worker.write_text(CRASHING_SETTLER, encoding="utf-8")

    boot = build_fixture_platform(database_path=database_path)
    boot.close()

    completed = subprocess.run(
        [
            sys.executable,
            str(worker),
            str(REPO_ROOT),
            database_path,
            "crash-settle-order",
            datetime.now(timezone.utc).isoformat(),
        ],
        capture_output=True,
        timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 7, completed.stderr.decode()[-2000:]

    repository = ForexRepository(database_path)
    try:
        # The submit committed; the settlement did not.
        order = repository.get_order("crash-settle-order")
        assert order is not None and order["state"] == "SIMULATED"
        assert repository.list_fills() == []
        assert repository.get_snapshot(
            settlement_snapshot_id(order["simulation_id"])
        ) is None
        active = repository.active_reservations()
        assert len(active) == 1
        assert active[0]["released_at"] is None
        assert repository.get_reservation("crash-settle-order")["released_at"] is None
    finally:
        repository.close()

    recovered = build_fixture_platform(database_path=database_path)
    try:
        # Recovery is clean: the interrupted unit left nothing half-written.
        assert recovered.recovery_result is not None
        assert recovered.portfolio.positions == {}
        assert recovered.paper_execution.pending_close_quantities() == {}
        assert len(recovered.paper_execution.unresolved_orders()) == 1

        # Settlement can now be completed exactly once.
        record = next(
            item
            for item in recovered.paper_execution.records()
            if item.request_id == "crash-settle-order"
        )
        plan = recovered.planner.plan(
            signal=Signal.BUY,
            positions={},
            bars=recovered.bars,
            decision_index=5,
            run_id="crash-settle",
            strategy_id="crash-settle",
        )
        fill = recovered.paper_execution.settle(
            record.simulation_id,
            fill_price=plan.fill_price,
            fill_at=plan.fill_at,
            action="OPEN",
            side=plan.request.side_value,
            quantity=plan.request.quantity_decimal(),
        )
        assert len(recovered.portfolio.positions) == 1
        assert len(recovered.repository.list_fills()) == 1
        assert recovered.repository.active_reservations() == []
        assert recovered.repository.get_fill(fill.simulation_id) is not None

        # A second restart sees exactly the same, settled state.
        settled_state = {
            "fills": len(recovered.repository.list_fills()),
            "positions": len(recovered.portfolio.positions),
            "pnl": str(recovered.portfolio.realized_pnl_today_account),
        }
    finally:
        recovered.close()

    again = build_fixture_platform(database_path=database_path)
    try:
        assert len(again.repository.list_fills()) == settled_state["fills"] == 1
        assert len(again.portfolio.positions) == settled_state["positions"] == 1
        assert str(again.portfolio.realized_pnl_today_account) == settled_state["pnl"]
        assert again.repository.active_reservations() == []
        assert again.paper_execution.unresolved_orders() == ()
    finally:
        again.close()


# ---------------------------------------------------------------------------
# the entry cap must be declared, never assumed
# ---------------------------------------------------------------------------


def test_the_fixture_platform_declares_a_labeled_synthetic_entry_limit():
    platform = build_fixture_platform()
    try:
        book = platform.paper_execution.reservations
        assert book is not None
        view = book.entry_limit_view()
        assert view["configured"] is True
        assert view["data_class"] == DATA_CLASS_SYNTHETIC_FIXTURE
        assert view["synthetic"] is True
        assert view["compatible_with_account"] is True
        assert view["account_currency"] == "CCC"
        # Provenance is reported, not hidden: the runtime settings carry the
        # same label the reports do.
        settings_view = platform.runtime_settings.audit_view()
        assert settings_view["entry_exposure_limit"]["synthetic"] is True
        assert (
            settings_view["entry_exposure_limit"]["data_class"]
            == DATA_CLASS_SYNTHETIC_FIXTURE
        )
    finally:
        platform.close()


def test_an_undeclared_entry_limit_blocks_entries_but_not_closes(tmp_path):
    platform = build_fixture_platform(
        database_path=str(tmp_path / "no-limit.db"),
        entry_exposure_limit=None,
    )
    try:
        plan = _plan_entry(platform, run_id="no-limit")
        record = platform.paper_execution.submit(plan.request, plan.context)
        assert record.state == "REJECTED"
        assert record.reason == "ENTRY_LIMIT_NOT_CONFIGURED"
        assert platform.repository.active_reservations() == []
        # The refusal is durable and idempotent.
        replay = platform.paper_execution.submit(plan.request, plan.context)
        assert replay.state == "REJECTED"
        assert replay.reason == "ENTRY_LIMIT_NOT_CONFIGURED"
        assert platform.repository.get_order(plan.request.request_id) is not None

        # A close still works: it reduces exposure and needs no cap.
        position_id = platform.portfolio.open(
            position_id="position:manual-open",
            side="BUY",
            quantity=plan.request.quantity_decimal(),
            price=plan.fill_price,
            at=plan.fill_at,
        ).position_id
        close = _close_plan(platform, position_id, run_id="no-limit")
        closed = platform.paper_execution.execute(
            close.request,
            close.context,
            fill_price=close.fill_price,
            fill_at=close.fill_at,
            action="CLOSE",
            position_id=position_id,
        )
        assert closed[0].state == "FILLED", closed[0].reason
        assert position_id not in platform.portfolio.positions
    finally:
        platform.close()


def test_a_limit_in_another_currency_blocks_entries(tmp_path):
    platform = build_fixture_platform(
        database_path=str(tmp_path / "wrong-currency.db"),
        entry_exposure_limit=EntryExposureLimit(
            limit_account=Decimal("250"),
            account_currency="USD",  # the fixture account is CCC
            version="operator-limit-v2",
            source="OPERATOR_PROFILE",
            data_class=DATA_CLASS_OPERATOR_DECLARED,
        ),
    )
    try:
        plan = _plan_entry(platform, run_id="wrong-currency")
        record = platform.paper_execution.submit(plan.request, plan.context)
        assert record.state == "REJECTED"
        assert record.reason == "ENTRY_LIMIT_INCOMPATIBLE"
        assert platform.paper_execution.reservations.entry_limit_view()[
            "compatible_with_account"
        ] is False
        assert platform.repository.active_reservations() == []
    finally:
        platform.close()


def test_an_invalid_limit_stops_the_platform_and_blocks_entries(tmp_path):
    """A malformed cap fails at startup; the reservation book also refuses it."""

    broken = EntryExposureLimit(
        limit_account=Decimal("0"),
        account_currency="CCC",
        version="operator-limit-v3",
        source="OPERATOR_PROFILE",
        data_class=DATA_CLASS_OPERATOR_DECLARED,
    )
    with pytest.raises(ValueError, match="invalid_entry_exposure_limit"):
        build_fixture_platform(
            database_path=str(tmp_path / "invalid-limit.db"),
            entry_exposure_limit=broken,
        )

    repository = ForexRepository(str(tmp_path / "invalid-limit-book.db"))
    try:
        book = OrderReservationBook(
            repository, entry_limit=broken, account_currency="CCC"
        )
        assert book.entry_limit_block_reason() == "ENTRY_LIMIT_INVALID"
        outcome = book.submit(
            ReservationRequest(
                request_id="invalid-limit-entry",
                kind="ENTRY",
                quantity=Decimal("0.10"),
                exposure=Decimal("98.96"),
            ),
            at=datetime.now(timezone.utc),
            simulate=lambda: {"request_id": "invalid-limit-entry"},
        )
        assert outcome.status == "REJECTED"
        assert outcome.reason == "ENTRY_LIMIT_INVALID"
        assert repository.active_reservations() == []
    finally:
        repository.close()


def test_entry_notional_is_measured_in_account_currency(tmp_path):
    """The reserved exposure is the ledger's account-currency notional.

    A raw quote-currency product would under-state the cap by the quote-to-
    account rate (0.9 in the fixtures), so the same order would fit twice.
    """

    limit = EntryExposureLimit(
        limit_account=Decimal("250"),
        account_currency="CCC",
        version="test-limit-v1",
        source="TEST",
        data_class=DATA_CLASS_SYNTHETIC_FIXTURE,
    )
    platform = build_fixture_platform(
        database_path=str(tmp_path / "notional.db"), entry_exposure_limit=limit
    )
    try:
        plan = _plan_entry(platform, run_id="notional")
        record = platform.paper_execution.submit(plan.request, plan.context)
        assert record.state == "SIMULATED", record.reason
        reserved = Decimal(
            platform.repository.reserved_entry_exposure_units("")
        ) * Decimal("0.00000001")
        quote_currency_notional = plan.request.quantity_decimal() * plan.fill_price
        account_notional = platform.portfolio.entry_notional_account(
            quantity=plan.request.quantity_decimal(),
            price=plan.fill_price,
            at=plan.request.evaluated_at_utc(),
        )
        assert reserved >= account_notional
        assert reserved > quote_currency_notional
        # A missing conversion rate cannot be priced: the entry is refused
        # rather than reserved for zero.
        from axiom_forex.portfolio.fx import MissingConversionRate

        with pytest.raises(MissingConversionRate):
            platform.portfolio.entry_notional_account(
                quantity=plan.request.quantity_decimal(),
                price=plan.fill_price,
                at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
    finally:
        platform.close()


def test_an_engine_reservation_from_another_connection_consumes_the_cap(tmp_path):
    """Exposure already spoken for by one connection bounds the next entry.

    Two *live* entries cannot normally coexist in this engine -- an unfinished
    order blocks new ones before the arithmetic runs -- but a live reservation
    made by another connection (a second bundle, a second process, or a
    settlement that failed to release) still has to be counted.  That is the
    case this pins: the store, not the caller's memory, decides.
    """

    database_path = str(tmp_path / "cap-store.db")
    limit = EntryExposureLimit(
        limit_account=Decimal("250"),
        account_currency="CCC",
        version="test-limit-v1",
        source="TEST",
        data_class=DATA_CLASS_SYNTHETIC_FIXTURE,
    )
    stamp = datetime.now(timezone.utc)

    def payload(request_id: str) -> dict:
        return {
            "request_id": request_id,
            "simulation_id": "simulation-" + request_id,
            "state": "SIMULATED",
            "created_at": stamp.isoformat(),
            "updated_at": stamp.isoformat(),
            "reason": "",
            "payload_fingerprint": "",
            "receipt": None,
            "is_unresolved": True,
        }

    first_repository = ForexRepository(database_path)
    second_repository = ForexRepository(database_path)
    try:
        # Connection A already holds 200 CCC of entry exposure.
        first_repository.insert_reservation(
            request_id="other-engine-entry",
            kind="ENTRY",
            state="SIMULATED",
            position_id="",
            quantity_units=int(Decimal("0.10") / Decimal("0.00000001")),
            exposure_units=int(Decimal("200") / Decimal("0.00000001")),
            exposure_key="",
            payload_fingerprint="",
            created_at=stamp.isoformat(),
            updated_at=stamp.isoformat(),
        )
        book = OrderReservationBook(
            second_repository, entry_limit=limit, account_currency="CCC"
        )
        refused = book.submit(
            ReservationRequest(
                request_id="store-entry",
                kind="ENTRY",
                quantity=Decimal("0.10"),
                exposure=Decimal("98.96"),
            ),
            at=stamp,
            simulate=lambda: payload("store-entry"),
        )
        assert refused.status == "REJECTED"
        assert refused.reason == "entry_exposure_limit_exceeded"
        assert refused.detail["already_reserved"] == Decimal("200.00000000")
        assert second_repository.get_order("store-entry") is None

        # A close needs no entry cap, so the same exhausted book still accepts it.
        accepted_close = book.submit(
            ReservationRequest(
                request_id="store-close",
                kind="CLOSE",
                quantity=Decimal("0.05"),
                position_id="position:cap",
                position_quantity=Decimal("0.10"),
            ),
            at=stamp,
            simulate=lambda: payload("store-close"),
        )
        assert accepted_close.status == "RESERVED"
    finally:
        second_repository.close()
        first_repository.close()
