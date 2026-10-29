"""Read-only description of integration boundaries with the running system.

M1/M2 integration is intentionally one-way: AXIOM-FOREX accepts explicit
objects from its caller, but never imports a running AXIOM process, reads its
environment, opens its database, or calls its broker.  This module documents
those boundaries; it does not connect to anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

INTEGRATION_BOUNDARIES: Mapping[str, str] = {
    "market_data": "inject MarketDataProvider; no shared feed process or credentials",
    "broker": "inject BrokerAdapter; live bridge disabled until reviewed",
    "persistence": "inject an explicit SQLite path; no shared database file",
    "api": "create an in-process WSGI callable; no deployment to the running API",
    "desktop": "pure view models and request specifications; no backend process",
    "monitoring": "read recorded repository-local state only",
    "research": "local fixture datasets with explicit versions",
}


@dataclass(frozen=True)
class RunningSystemBoundary:
    name: str
    touched: bool = False
    note: str = "read-only inspection; no control actions"

    def audit_view(self) -> dict[str, object]:
        return {"name": self.name, "touched": self.touched, "note": self.note}


DEFAULT_BOUNDARIES = tuple(
    RunningSystemBoundary(name) for name in sorted(INTEGRATION_BOUNDARIES)
)
