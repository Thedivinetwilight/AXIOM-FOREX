"""Local workstation for AXIOM-FOREX: paper and live, chosen explicitly.

The workstation reads what the paper platform and an already-signed-in
MetaTrader 5 terminal report. Paper simulates fills locally; guarded live
execution requires explicit review and arming. Setup offers an explicit account
switch on the shared terminal. Nothing reconnects or switches automatically, and
the account and execution-mode switches themselves never place an order.

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
``ui``
    The server-rendered UI: one design system, one page registry, no scripts.
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
    "ui",
]
