"""In-memory structured audit log for M1 decisions.

Persistence is deliberately deferred.  The log accepts only JSON-safe
primitives after sanitization: Decimals become strings, datetimes become ISO
timestamps, arbitrary objects become type labels, URLs and credential-like
keys are redacted, and non-finite floats cannot enter the audit record.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, uuid5

_SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "private_key",
    "api_key",
    "access_key",
)
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def sanitize_audit_value(value: Any, key: str | None = None, depth: int = 0) -> Any:
    if key is not None and any(part in key.lower() for part in _SECRET_KEY_PARTS):
        return "[REDACTED]"
    if depth > 8:
        return "[DEPTH_LIMIT]"
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and _URL.match(value):
            return "[URL_REDACTED]"
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[NON_FINITE]"
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else "[NON_FINITE]"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(child_key): sanitize_audit_value(child_value, str(child_key), depth + 1)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_audit_value(item, None, depth + 1) for item in value]
    return f"[{type(value).__name__}]"


def make_decision_id(*parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return uuid5(NAMESPACE_URL, "axiom-forex-m1|" + payload).hex


@runtime_checkable
class AuditLog(Protocol):
    def record(self, event: Mapping[str, Any]) -> dict[str, Any]: ...


@dataclass
class InMemoryAuditLog:
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        sanitized = sanitize_audit_value(dict(event))
        if not isinstance(sanitized, dict):
            raise TypeError("Audit event must sanitize to a mapping")
        self.events.append(sanitized)
        return sanitized

    def clear(self) -> None:
        self.events.clear()

    def last(self) -> dict[str, Any] | None:
        return self.events[-1] if self.events else None

    def find(self, **attributes: Any) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if all(event.get(key) == value for key, value in attributes.items())
        ]
