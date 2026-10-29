"""Local workstation for AXIOM-FOREX: paper and live, chosen explicitly.

The workstation reads what the paper platform and an already-signed-in
MetaTrader 5 terminal report, and it owns exactly one trading-shaped control: the
execution mode.  Paper simulates every fill locally; live routes an approved
order through the MetaTrader 5 execution path.  There is still no order entry, no
arming, no account switching, and no automatic reconnection anywhere in it, and
the mode switch itself never places an order.

Modules
-------
``probes``
    Offline checks of the machine (OS, Python, packages, terminal, settings file).
``session``
    The data-only terminal session. Connects on request, never on its own.
``mode``
    Paper/live execution mode: persisted, audited, one confirmation for live.
``preflight``
    Every genuine blocker to live execution, each with the step that clears it.
``setup``
    The first-run wizard state machine and its read-only guarantees.
``context``
    Read model shared by every page; labels real / paper / fixture data.
``views``
    The server-rendered UI: one design system, all states, no scripts.
``app``
    Stdlib HTTP server bound to loopback, read-only GETs and explicit POSTs.
"""

from __future__ import annotations

__all__ = [
    "app",
    "context",
    "probes",
    "session",
    "setup",
    "views",
]
