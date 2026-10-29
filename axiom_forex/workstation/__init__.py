"""Local, observe-only workstation for AXIOM-FOREX.

The workstation is a *reader*. It renders what the paper platform and an
already-signed-in MetaTrader 5 terminal report, and it has no order entry, no
arming, no account switching, and no automatic reconnection anywhere in it.

Modules
-------
``probes``
    Offline checks of the machine (OS, Python, packages, terminal, settings file).
``session``
    The observe-only terminal session. Connects on request, never on its own.
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
