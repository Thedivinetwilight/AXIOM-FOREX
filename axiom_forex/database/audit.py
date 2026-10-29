"""Durable audit log implementing the M1 AuditLog protocol.

SQLite is the record; the in-run cache is a *view* of it.  Two rules keep them
from disagreeing:

* an event is appended to the cache only after its row is committed -- a plain
  ``record`` writes and commits first, and a ``record`` inside
  :meth:`DurableAuditLog.atomic` is staged until that unit commits;
* recording inside somebody else's transaction is refused outright, because the
  cache could then show an event whose row is later rolled back.  Use
  ``atomic()`` to own the unit.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from app.audit.log import AuditLog, InMemoryAuditLog, sanitize_audit_value

from .repository import ForexRepository


@dataclass(frozen=True)
class DurableAuditLog(AuditLog):
    """Write each sanitized decision once to SQLite and expose an in-run cache."""

    repository: ForexRepository
    cache: InMemoryAuditLog = field(default_factory=InMemoryAuditLog)
    #: Mutable scratch space (a frozen dataclass may hold a mutable container):
    #: ``staging`` holds events written but not yet committed.
    _state: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False, hash=False
    )

    @property
    def _staging(self) -> list[dict[str, Any]] | None:
        return self._state.get("staging")

    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the sanitized event and only then expose it in the cache."""

        sanitized = sanitize_audit_value(dict(event))
        if not isinstance(sanitized, dict):
            raise TypeError("Audit event must sanitize to a mapping")
        staging = self._staging
        if staging is None and self.repository.in_transaction:
            # Somebody opened a transaction without ``atomic()``.  Appending to
            # the cache now would leave it describing work that may roll back.
            raise RuntimeError("audit_record_requires_atomic_unit")
        self.repository.record_audit(sanitized)
        if staging is not None:
            staging.append(sanitized)
        else:
            self.cache.events.append(sanitized)
        return sanitized

    @contextmanager
    def atomic(self) -> Iterator["DurableAuditLog"]:
        """One database transaction whose audit events reach the cache on commit.

        The block *is* the transaction: writes made through the same repository
        join it, and the events recorded inside are staged.  On success the
        transaction commits and the staged events are appended to the cache; on
        any exception the transaction rolls back and the staged events are
        dropped, so the cache never runs ahead of the committed record.
        """

        if self._staging is not None:
            # Nested use joins the enclosing unit: it must not commit early.
            yield self
            return
        staging: list[dict[str, Any]] = []
        self._state["staging"] = staging
        try:
            with self.repository.transaction():
                yield self
        except BaseException:
            self._state.pop("staging", None)
            raise
        self._state.pop("staging", None)
        # After the commit: the rows are durable, so the cache may show them.
        self.cache.events.extend(staging)

    def rebuild_cache(self, *, limit: int = 1000) -> int:
        """Replace the cache with the committed rows, in order.

        Recovery calls this so a restarted process starts from what SQLite
        actually holds rather than from whatever a previous run remembered.
        """

        committed = self.repository.recent_audit(limit=limit)
        self.cache.events[:] = committed
        return len(committed)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.repository.recent_audit(limit=limit)

    def find(self, **attributes: Any) -> list[dict[str, Any]]:
        return self.cache.find(**attributes)

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.cache.events
