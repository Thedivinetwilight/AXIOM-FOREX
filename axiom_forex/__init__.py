"""Isolated AXIOM-FOREX adaptation of AXIOM's full architecture.

This package is repository-local and research/paper oriented.  It does not
import, configure, stop, restart, or deploy the running live system.  Concrete
broker and data-provider integrations remain explicit injection points.

The four names below are loaded on first use rather than at import.  Importing
``axiom_forex`` used to pull in :mod:`axiom_forex.facade`, which imports the
synthetic fixture module, which resolves ``ZoneInfo("America/New_York")`` at
import time -- so on a Windows machine without ``tzdata`` the *whole package*
raised ``ZoneInfoNotFoundError`` before any code of ours could run, including
the workstation launcher that would have reported it.  Deferring the import
means a missing time-zone database is a named, actionable failure at the one
place that needs it instead of a traceback at the entry point.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.2.0-forex-adaptation"
__all__ = [
    "ForexPlatform",
    "PaperWorkflowResult",
    "build_fixture_platform",
    "execute_paper_workflow",
]

#: Public name -> submodule that defines it.
_LAZY: dict[str, str] = {
    "ForexPlatform": ".facade",
    "build_fixture_platform": ".facade",
    "PaperWorkflowResult": ".workflow",
    "execute_paper_workflow": ".workflow",
}


def __getattr__(name: str) -> Any:
    """Import the defining module on first access (PEP 562)."""

    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value  # cached: the second access is a plain lookup
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
