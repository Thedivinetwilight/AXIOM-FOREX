"""Explicit-path SQLite repository; secrets are sanitized before storage."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping

from app.audit.log import sanitize_audit_value

from ..config.runtime import redact_local_path
from .schema import SCHEMA_VERSION, apply_schema, schema_version


def _json(payload: Any) -> str:
    return json.dumps(sanitize_audit_value(payload), sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ForexRepository:
    """One explicit SQLite connection with an opt-in multi-statement transaction.

    The connection runs in autocommit mode so that the module can open an
    explicit ``BEGIN IMMEDIATE`` block: every durable reservation is taken under
    a write lock, which is what stops two independent connections (or two
    processes) from reserving the same remaining quantity at once.  Methods
    outside :meth:`transaction` still commit their own work, so nothing else
    changes.
    """

    def __init__(self, database_path: str):
        if not isinstance(database_path, str) or not database_path.strip():
            raise ValueError("explicit_database_path_required")
        self.database_path = database_path
        self.connection = sqlite3.connect(
            database_path, timeout=30.0, isolation_level=None
        )
        self.connection.row_factory = sqlite3.Row
        # Wait for a competing writer instead of failing the reservation: the
        # conflict is resolved by the database, in order, not by a race.
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self._transaction_depth = 0
        apply_schema(self.connection)

    def close(self) -> None:
        self.connection.close()

    @property
    def in_transaction(self) -> bool:
        return self._transaction_depth > 0

    def _commit(self) -> None:
        """Commit unless an enclosing :meth:`transaction` owns the commit."""

        if self._transaction_depth:
            return
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialise writers and make a group of writes all-or-nothing.

        ``BEGIN IMMEDIATE`` takes the database write lock up front, so the
        reads inside the block (reserved quantity, exposure, unresolved orders)
        cannot be invalidated by another connection before the commit.
        """

        if self._transaction_depth:
            # Join the open transaction instead of opening a nested one:
            # callers compose (a reservation and its order row), and the
            # outermost block owns the commit or the rollback.
            yield self.connection
            return
        self.connection.execute("BEGIN IMMEDIATE")
        self._transaction_depth = 1
        try:
            yield self.connection
        except BaseException:
            self._transaction_depth = 0
            self.connection.rollback()
            raise
        else:
            self._transaction_depth = 0
            self._commit()
        finally:
            self._transaction_depth = 0

    @property
    def schema_version(self) -> int:
        return schema_version(self.connection)

    def health(self) -> dict[str, Any]:
        tables = (
            "audit_events",
            "instrument_specs",
            "datasets",
            "backtests",
            "orders",
            "fills",
            "positions",
            "snapshots",
            "lifecycle",
            "monitoring",
            "order_reservations",
            "position_guards",
        )
        counts = {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        return {
            "database_path": redact_local_path(self.database_path),
            "schema_version": self.schema_version,
            "expected_schema_version": SCHEMA_VERSION,
            "counts": counts,
        }

    def record_audit(self, event: Mapping[str, Any]) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO audit_events(event_type, payload, recorded_at) "
                "VALUES (?, ?, ?)",
                (str(event.get("event_type", "UNKNOWN")), _json(event), _now()),
            )
        self._commit()

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def save_specs(self, *, version: str, specs: Iterable[Mapping[str, Any]]) -> None:
        with self.transaction():
            for spec in specs:
                payload = dict(spec)
                pair = str(payload.get("pair") or payload.get("canonical_pair") or "")
                signature = str(payload.get("signature", ""))
                self.connection.execute(
                    """
                    INSERT INTO instrument_specs(catalog_version, pair, signature, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(catalog_version, pair) DO UPDATE SET
                        signature = excluded.signature,
                        payload = excluded.payload
                    """,
                    (version, pair, signature, _json(payload)),
                )
        self._commit()

    def list_specs(self, *, version: str | None = None) -> list[dict[str, Any]]:
        if version is None:
            rows = self.connection.execute(
                "SELECT payload FROM instrument_specs ORDER BY catalog_version, pair"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM instrument_specs WHERE catalog_version = ? ORDER BY pair",
                (version,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_dataset(self, dataset_id: str, sha256: str, payload: Mapping[str, Any]) -> None:
        stored = dict(payload)
        stored.setdefault("dataset_id", dataset_id)
        stored.setdefault("sha256", sha256)
        self.connection.execute(
            """
            INSERT INTO datasets(dataset_id, sha256, payload) VALUES (?, ?, ?)
            ON CONFLICT(dataset_id) DO UPDATE SET
                sha256 = excluded.sha256,
                payload = excluded.payload
            """,
            (dataset_id, sha256, _json(stored)),
        )
        self._commit()

    def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_backtest(self, payload: Mapping[str, Any]) -> None:
        config = payload.get("config", {})
        self.connection.execute(
            """
            INSERT INTO backtests(run_id, dataset_id, strategy_id, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                dataset_id = excluded.dataset_id,
                strategy_id = excluded.strategy_id,
                payload = excluded.payload,
                created_at = excluded.created_at
            """,
            (
                str(payload.get("run_id", "")),
                str(config.get("dataset_id", "")),
                str(config.get("strategy_id", "")),
                _json(payload),
                _now(),
            ),
        )
        self._commit()

    def list_backtests(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM backtests ORDER BY created_at, run_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_orders(self, records: Iterable[Mapping[str, Any]]) -> None:
        with self.transaction():
            for record in records:
                self.connection.execute(
                    """
                    INSERT INTO orders(request_id, simulation_id, state, payload, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(request_id) DO UPDATE SET
                        simulation_id = excluded.simulation_id,
                        state = excluded.state,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(record.get("request_id", "")),
                        str(record.get("simulation_id", "")),
                        str(record.get("state", "")),
                        _json(record),
                        str(record.get("updated_at", _now())),
                    ),
                )
        self._commit()

    def list_orders(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM orders ORDER BY updated_at, request_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_order(self, request_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM orders WHERE request_id = ?", (request_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def unresolved_order_ids(self, *, final_states: Iterable[str]) -> list[str]:
        """Request ids whose durable order state is not final.

        Read inside a reservation transaction this is the durable answer to
        "is another order still in doubt?", including orders written by a
        different process.
        """

        states = tuple(str(state).upper() for state in final_states)
        if not states:
            return [
                str(row[0])
                for row in self.connection.execute(
                    "SELECT request_id FROM orders ORDER BY request_id"
                ).fetchall()
            ]
        placeholders = ", ".join("?" for _ in states)
        rows = self.connection.execute(
            f"SELECT request_id FROM orders WHERE UPPER(state) NOT IN ({placeholders}) "
            "ORDER BY request_id",
            states,
        ).fetchall()
        return [str(row[0]) for row in rows]

    # -- durable order reservations -------------------------------------
    #
    # Quantities and exposure are exact scaled integers (1e-8 units) so the
    # limit arithmetic happens in SQLite, under the write lock, on integers.

    def insert_reservation(
        self,
        *,
        request_id: str,
        kind: str,
        state: str,
        position_id: str,
        quantity_units: int,
        exposure_units: int,
        exposure_key: str,
        payload_fingerprint: str,
        created_at: str,
        updated_at: str,
        simulation_id: str = "",
    ) -> None:
        """Insert a new reservation; a duplicate request id raises IntegrityError."""

        with self.transaction():
            self.connection.execute(
                """
                INSERT INTO order_reservations(
                    request_id, simulation_id, kind, state, position_id,
                    quantity_units, exposure_units, exposure_key,
                    payload_fingerprint, created_at, updated_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    request_id,
                    simulation_id,
                    kind,
                    state,
                    position_id,
                    int(quantity_units),
                    int(exposure_units),
                    exposure_key,
                    payload_fingerprint,
                    created_at,
                    updated_at,
                ),
            )
        self._commit()

    def get_reservation(self, request_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM order_reservations WHERE request_id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_reservation(
        self,
        request_id: str,
        *,
        state: str,
        updated_at: str,
        simulation_id: str | None = None,
    ) -> None:
        with self.transaction():
            if simulation_id is None:
                self.connection.execute(
                    "UPDATE order_reservations SET state = ?, updated_at = ? "
                    "WHERE request_id = ? AND released_at IS NULL",
                    (state, updated_at, request_id),
                )
            else:
                self.connection.execute(
                    "UPDATE order_reservations SET state = ?, updated_at = ?, "
                    "simulation_id = ? WHERE request_id = ? AND released_at IS NULL",
                    (state, updated_at, simulation_id, request_id),
                )
        self._commit()

    def release_reservation(
        self, request_id: str, *, state: str, released_at: str
    ) -> bool:
        """Release a reservation, keeping its row as history.  Idempotent."""

        with self.transaction():
            cursor = self.connection.execute(
                "UPDATE order_reservations SET state = ?, updated_at = ?, "
                "released_at = ? WHERE request_id = ? AND released_at IS NULL",
                (state, released_at, released_at, request_id),
            )
            changed = cursor.rowcount
        self._commit()
        return changed > 0

    def active_reservations(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM order_reservations WHERE released_at IS NULL "
            "ORDER BY created_at, request_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def reserved_close_units(self, position_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(quantity_units), 0) FROM order_reservations "
            "WHERE released_at IS NULL AND kind = 'CLOSE' AND position_id = ?",
            (position_id,),
        ).fetchone()
        return int(row[0])

    def reserved_entry_exposure_units(self, exposure_key: str = "") -> int:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(exposure_units), 0) FROM order_reservations "
            "WHERE released_at IS NULL AND kind = 'ENTRY' AND exposure_key = ?",
            (exposure_key,),
        ).fetchone()
        return int(row[0])

    def get_position_guard(self, position_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT quantity_units FROM position_guards WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        return int(row[0]) if row else None

    def lower_position_guard(
        self, position_id: str, *, quantity_units: int, updated_at: str
    ) -> int:
        """Record a claimed remaining quantity, keeping the smallest ever seen.

        The guard only ever decreases, so a stale engine claiming the original
        position size cannot raise the ceiling another engine already lowered.
        """

        with self.transaction():
            self.connection.execute(
                """
                INSERT INTO position_guards(position_id, quantity_units, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    quantity_units = MIN(position_guards.quantity_units,
                                         excluded.quantity_units),
                    updated_at = excluded.updated_at
                """,
                (position_id, int(quantity_units), updated_at),
            )
            guard = self.get_position_guard(position_id)
        self._commit()
        if guard is None:
            raise ValueError("position_guard_not_persisted")
        return guard

    def save_fills(self, fills: Iterable[Mapping[str, Any]]) -> None:
        with self.transaction():
            for fill in fills:
                stored = dict(fill)
                stored.setdefault("filled_at", _now())
                self.connection.execute(
                    """
                    INSERT INTO fills(fill_id, request_id, payload, filled_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(fill_id) DO UPDATE SET
                        request_id = excluded.request_id,
                        payload = excluded.payload,
                        filled_at = excluded.filled_at
                    """,
                    (
                        str(stored.get("simulation_id", "")),
                        str(stored.get("request_id", "")),
                        _json(stored),
                        str(stored.get("filled_at", _now())),
                    ),
                )
        self._commit()

    def save_positions(self, positions: Iterable[Mapping[str, Any]], *, state: str) -> None:
        with self.transaction():
            for position in positions:
                stored = dict(position)
                stored.setdefault("state", state)
                stored.setdefault("updated_at", _now())
                # A partially closed position produces several closed trades under
                # one position id.  Key each slice by its own close instant so the
                # index keeps every slice instead of overwriting the previous one.
                row_id = str(stored.get("position_id", ""))
                if state == "CLOSED" and stored.get("closed_at"):
                    row_id = f"{row_id}@{stored['closed_at']}"
                self.connection.execute(
                    """
                    INSERT INTO positions(position_id, state, payload, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(position_id) DO UPDATE SET
                        state = excluded.state,
                        payload = excluded.payload,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row_id,
                        state,
                        _json(stored),
                        str(stored.get("updated_at", _now())),
                    ),
                )
        self._commit()

    def close_position_rows(
        self, position_ids: Iterable[str], *, updated_at: str
    ) -> int:
        """Mark the open index row of each fully closed position as CLOSED.

        The closed *slice* keeps its own row keyed by position and close
        instant; this only stops the position's base row from still claiming to
        be open.  Only the ids this settlement closed are touched, so another
        engine's positions are never rewritten.
        """

        changed = 0
        with self.transaction():
            for position_id in position_ids:
                cursor = self.connection.execute(
                    "UPDATE positions SET state = 'CLOSED', updated_at = ? "
                    "WHERE position_id = ? AND state = 'OPEN'",
                    (updated_at, str(position_id)),
                )
                changed += cursor.rowcount
        self._commit()
        return changed

    def save_snapshot(self, snapshot_id: str, kind: str, payload: Mapping[str, Any]) -> None:
        stored = dict(payload)
        recorded_at = _now()
        stored.setdefault("snapshot_id", snapshot_id)
        stored.setdefault("kind", kind)
        stored.setdefault("recorded_at", recorded_at)
        self.connection.execute(
            """
            INSERT INTO snapshots(snapshot_id, kind, payload, recorded_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                kind = excluded.kind,
                payload = excluded.payload,
                recorded_at = excluded.recorded_at
            """,
            (snapshot_id, kind, _json(stored), recorded_at),
        )
        self._commit()

    def list_snapshots(self, *, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM snapshots ORDER BY recorded_at, snapshot_id"
        ).fetchall()
        snapshots = [json.loads(row[0]) for row in rows]
        if kind is None:
            return snapshots
        return [snapshot for snapshot in snapshots if snapshot.get("kind") == kind]

    def save_lifecycle(self, strategy_id: str, payload: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO lifecycle(strategy_id, payload, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(strategy_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (strategy_id, _json(payload), _now()),
        )
        self._commit()

    def list_lifecycle(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM lifecycle ORDER BY strategy_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def record_monitoring(
        self, event_id: str, kind: str, severity: str, payload: Mapping[str, Any]
    ) -> None:
        stored = dict(payload)
        recorded_at = _now()
        stored.setdefault("event_id", event_id)
        stored.setdefault("kind", kind)
        stored.setdefault("severity", severity)
        stored.setdefault("recorded_at", recorded_at)
        self.connection.execute(
            """
            INSERT INTO monitoring(event_id, kind, severity, payload, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                kind = excluded.kind,
                severity = excluded.severity,
                payload = excluded.payload,
                recorded_at = excluded.recorded_at
            """,
            (event_id, kind, severity, _json(stored), recorded_at),
        )
        self._commit()

    def list_monitoring(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM monitoring ORDER BY recorded_at, event_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_datasets(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM datasets ORDER BY dataset_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def get_fill(self, fill_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM fills WHERE fill_id = ?", (fill_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def list_fills(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM fills ORDER BY filled_at, fill_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_positions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload FROM positions ORDER BY updated_at, position_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recovery_snapshot(self) -> dict[str, Any]:
        """Return every persisted, sanitized state needed to recover paper state."""
        return {
            "health": self.health(),
            "specs": self.list_specs(),
            "datasets": self.list_datasets(),
            "orders": self.list_orders(),
            "order_reservations": self.active_reservations(),
            "fills": self.list_fills(),
            "positions": self.list_positions(),
            "snapshots": self.list_snapshots(),
            "lifecycle": self.list_lifecycle(),
            "monitoring": self.list_monitoring(),
            "audit": self.recent_audit(limit=1000),
        }
