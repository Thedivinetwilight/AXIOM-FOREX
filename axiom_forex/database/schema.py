"""Additive SQLite schema migrations; no shared running-system database."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS instrument_specs (
            catalog_version TEXT NOT NULL,
            pair TEXT NOT NULL,
            signature TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (catalog_version, pair)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS datasets (
            dataset_id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS backtests (
            run_id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS orders (
            request_id TEXT PRIMARY KEY,
            simulation_id TEXT NOT NULL,
            state TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS fills (
            fill_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            filled_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS positions (
            position_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS lifecycle (
            strategy_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS monitoring (
            event_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            payload TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
        """,
    ),
    # Version 2 adds the durable order-reservation book.  Quantities and
    # exposure are stored as exact scaled integers (1e-8 units) so the limit
    # checks can be summed and compared inside SQL without float rounding.
    2: (
        """
        CREATE TABLE IF NOT EXISTS order_reservations (
            request_id TEXT PRIMARY KEY,
            simulation_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            position_id TEXT NOT NULL DEFAULT '',
            quantity_units INTEGER NOT NULL DEFAULT 0,
            exposure_units INTEGER NOT NULL DEFAULT 0,
            exposure_key TEXT NOT NULL DEFAULT '',
            payload_fingerprint TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            released_at TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS order_reservations_active_idx
            ON order_reservations(released_at, kind)
        """,
        """
        CREATE INDEX IF NOT EXISTS order_reservations_position_idx
            ON order_reservations(position_id, released_at)
        """,
        # The durable position guard records the smallest remaining quantity
        # any engine has claimed for a position.  A claim can only lower it, so
        # a stale engine that still believes the position is larger than it is
        # cannot raise the ceiling it is checked against.
        """
        CREATE TABLE IF NOT EXISTS position_guards (
            position_id TEXT PRIMARY KEY,
            quantity_units INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ),
}


def schema_version(connection: sqlite3.Connection) -> int:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)"
    )
    row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    return int(row[0]) if row else 0


def apply_schema(connection: sqlite3.Connection) -> int:
    """Apply every pending migration, or none of them.

    The version bump and the table creation must travel together: a process
    that dies half-way through a migration would otherwise leave a database
    that claims a version whose tables do not exist.  ``BEGIN IMMEDIATE`` also
    serialises two processes opening the same fresh database at once.
    """

    current = schema_version(connection)
    if current > SCHEMA_VERSION:
        raise ValueError("Database schema is newer than this repository version")
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        if row is None:
            connection.execute("INSERT INTO schema_meta(version) VALUES (0)")
            current = 0
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            for statement in MIGRATIONS[version]:
                connection.execute(statement)
            connection.execute("UPDATE schema_meta SET version = ?", (version,))
            current = version
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
    return current
