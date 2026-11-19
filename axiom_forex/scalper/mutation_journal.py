"""Durable, account- and run-bound write-ahead identities for live mutations.

The journal is deliberately separate from the mutable run snapshot. A PREPARED
record is committed *before* any broker mutation. A crash at any point leaves
it unresolved; absence of a receipt is never evidence that a request was not
sent. SQLite's transaction and unique key also prevent a second process from
preparing the same request id. This module never contacts a broker.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

SCHEMA = 1
ACTIONS = frozenset({"entry", "close", "protection"})
_REQUIRED_IDENTITY = frozenset({"run_id", "account_fingerprint", "account_server",
                                "account_currency", "account_mode", "strategy_fingerprint",
                                "evidence_digest", "config_digest", "symbol"})


class JournalError(RuntimeError):
    """The journal cannot be safely read or committed."""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise JournalError("journal_invalid_json") from exc


def _decode(value: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for key, item in pairs:
            if key in obj:
                raise ValueError("duplicate_key")
            obj[key] = item
        return obj
    try:
        return json.loads(value, object_pairs_hook=unique,
                          parse_constant=lambda _v: (_ for _ in ()).throw(ValueError("nonfinite")))
    except Exception as exc:
        raise JournalError("journal_invalid_json") from exc


class MutationJournal:
    """Single-owner journal with transactional cross-process duplicate prevention."""

    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        if not create and not self.path.is_file():
            raise JournalError("journal_missing")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
            self._db.execute("PRAGMA synchronous=EXTRA")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA foreign_keys=ON")
            if create:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._db.execute("CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), schema_version INTEGER NOT NULL)")
                    self._db.execute("CREATE TABLE IF NOT EXISTS mutations (request_id TEXT PRIMARY KEY, action TEXT NOT NULL, identity TEXT NOT NULL, intent TEXT NOT NULL, result TEXT, readback TEXT, resolution TEXT)")
                    self._db.execute("INSERT OR IGNORE INTO metadata(id,schema_version) VALUES(1,?)", (SCHEMA,))
                    self._db.execute("COMMIT")
                except Exception:
                    self._db.execute("ROLLBACK")
                    raise
            row = self._db.execute("SELECT schema_version FROM metadata WHERE id=1").fetchone()
            if row != (SCHEMA,) or self._db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise JournalError("journal_schema_or_integrity_invalid")
            self.list_all()  # reject malformed records before any further operation
        except Exception as exc:
            if hasattr(self, "_db"):
                self._db.close()
            if isinstance(exc, JournalError):
                raise
            raise JournalError("journal_unreadable") from exc

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _validate(request_id: str, action: str, identity: Mapping[str, Any], intent: Mapping[str, Any]) -> None:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise JournalError("journal_request_id_invalid")
        if action not in ACTIONS or not isinstance(identity, Mapping) or not isinstance(intent, Mapping):
            raise JournalError("journal_record_invalid")
        if any(not isinstance(identity.get(k), str) or not identity[k].strip() for k in _REQUIRED_IDENTITY):
            raise JournalError("journal_identity_incomplete")
        if identity["account_mode"] not in {"DEMO", "REAL"}:
            raise JournalError("journal_account_mode_invalid")
        if str(intent.get("request_id") or "") != request_id:
            raise JournalError("journal_intent_id_mismatch")
        if action in {"close", "protection"} and not str(intent.get("position_id") or ""):
            raise JournalError("journal_position_missing")

    def prepare(self, *, request_id: str, action: str, identity: Mapping[str, Any], intent: Mapping[str, Any]) -> None:
        self._validate(request_id, action, identity, intent)
        try:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("SELECT 1 FROM mutations WHERE request_id=?", (request_id,)).fetchone():
                raise JournalError("journal_request_id_duplicate")
            if self._db.execute("SELECT 1 FROM mutations WHERE resolution IS NULL LIMIT 1").fetchone():
                raise JournalError("journal_unresolved_mutation_present")
            self._db.execute("INSERT INTO mutations(request_id,action,identity,intent) VALUES(?,?,?,?)",
                             (request_id, action, _json(dict(identity)), _json(dict(intent))))
            self._db.execute("COMMIT")
        except Exception as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(exc, JournalError):
                raise
            if isinstance(exc, sqlite3.IntegrityError):
                raise JournalError("journal_request_id_duplicate") from exc
            raise JournalError("journal_prepare_failed") from exc

    def record_result(self, request_id: str, *, result: Mapping[str, Any], readback: Mapping[str, Any] | None) -> None:
        if not isinstance(result, Mapping) or (readback is not None and not isinstance(readback, Mapping)):
            raise JournalError("journal_result_invalid")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT result,resolution FROM mutations WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row[0] is not None or row[1] is not None:
                raise JournalError("journal_result_missing_or_already_recorded")
            self._db.execute("UPDATE mutations SET result=?,readback=? WHERE request_id=?",
                             (_json(dict(result)), None if readback is None else _json(dict(readback)), request_id))
            self._db.execute("COMMIT")
        except Exception as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(exc, JournalError):
                raise
            raise JournalError("journal_result_commit_failed") from exc

    def resolve(self, request_id: str, resolution: Mapping[str, Any]) -> None:
        """Commit an already-verified conclusion; callers must validate evidence."""
        if not isinstance(resolution, Mapping) or not str(resolution.get("reason") or ""):
            raise JournalError("journal_resolution_invalid")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT resolution FROM mutations WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row[0] is not None:
                raise JournalError("journal_missing_or_already_resolved")
            self._db.execute("UPDATE mutations SET resolution=? WHERE request_id=?", (_json(dict(resolution)), request_id))
            self._db.execute("COMMIT")
        except Exception as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(exc, JournalError):
                raise
            raise JournalError("journal_resolution_commit_failed") from exc

    def list_all(self) -> list[dict[str, Any]]:
        try:
            rows = self._db.execute("SELECT request_id,action,identity,intent,result,readback,resolution FROM mutations ORDER BY rowid").fetchall()
            result = []
            for request_id, action, identity, intent, response, readback, resolution in rows:
                decoded_identity, decoded_intent = _decode(identity), _decode(intent)
                self._validate(request_id, action, decoded_identity, decoded_intent)
                values = [_decode(v) if v is not None else None for v in (response, readback, resolution)]
                if any(v is not None and not isinstance(v, dict) for v in values):
                    raise JournalError("journal_record_invalid")
                if values[2] is not None and not str(values[2].get("reason") or ""):
                    raise JournalError("journal_resolution_invalid")
                result.append(dict(request_id=request_id, action=action, identity=decoded_identity,
                                   intent=decoded_intent, result=values[0], readback=values[1], resolution=values[2]))
            return result
        except JournalError:
            raise
        except Exception as exc:
            raise JournalError("journal_read_failed") from exc

    def unresolved(self) -> list[dict[str, Any]]:
        return [item for item in self.list_all() if item["resolution"] is None]
