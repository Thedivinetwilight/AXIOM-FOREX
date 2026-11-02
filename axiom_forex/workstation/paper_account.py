"""The paper account, as every page reads it.

This module exists so that the dashboard, the positions page, the activity page
and the performance page cannot disagree: they all render the *same* dictionary,
built once per request by :func:`read_paper_account`.  It contains no arithmetic
and no formatting of its own -- the numbers and their wording come from
:mod:`axiom_forex.paper_runner.accounting`, which reads the durable ledger.

Where the pieces come from:

* the ledger and the state file, from the catalog panel's paper folder (or the
  repository's ``data/catalog/paper`` when no panel is wired);
* the newest quotes, from the running paper test's snapshot -- and, when it is
  not running, from the last fill quotes the ledger recorded, labelled as the
  historical marks they are;
* the configured local simulated starting capital, from
  ``paper-capital.local.json``;
* commission and swap terms, from the terminal's own symbol records, when they
  are available.

Nothing here connects, starts, stops, sizes or sends anything.  It reads files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from axiom_forex.paper_runner.accounting import (
    QuoteMark,
    marks_from_snapshot,
    read_account,
)

#: The folder the catalog panel gives its paper test, relative to the repo root.
DEFAULT_PAPER_PATH = Path("data") / "catalog" / "paper"


def paper_root(app: Any) -> Path:
    """Where this workstation's paper ledger lives, without inventing a path."""

    catalog = getattr(app, "catalog", None)
    getter = getattr(catalog, "paper_root", None)
    if callable(getter):
        try:
            return Path(getter())
        except Exception:  # a read model never raises into a page
            pass
    project = str(getattr(app, "project_root", "") or "")
    if project:
        return Path(project) / DEFAULT_PAPER_PATH
    return DEFAULT_PAPER_PATH


def read_paper_account(app: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Build the one paper-account view every page renders.

    It never raises: a ledger that cannot be read becomes an integrity warning on
    the page, not a broken page.
    """

    when = now or datetime.now(timezone.utc)
    root = paper_root(app)
    snapshot: dict[str, Any] = {}
    cost_model = None
    catalog = getattr(app, "catalog", None)
    if catalog is not None:
        getter = getattr(catalog, "paper_snapshot", None)
        if callable(getter):
            try:
                snapshot = dict(getter() or {})
            except Exception:
                snapshot = {}
        candidate = getattr(catalog, "cost_model", None)
        if callable(candidate):
            cost_model = candidate
    marks: dict[str, QuoteMark] = marks_from_snapshot(
        snapshot.get("quotes") or (), source="paper session (live quotes)"
    )
    allowance = snapshot.get("max_quote_age_seconds")
    try:
        account = read_account(
            root,
            now=when,
            marks=marks,
            cost_model=cost_model,
            max_quote_age_seconds=float(allowance) if allowance else 120.0,
        )
    except Exception as exc:  # the accounting promises not to raise; this is the wall
        return {
            "available": False,
            "error": type(exc).__name__,
            "read_at": when.isoformat(),
            "source": "durable paper ledger",
            "metrics": [],
            "metric_by_key": {},
            "counts": {},
            "positions": [],
            "trades": [],
            "quotes": [],
            "trading": {"paused": True, "reason": "the paper ledger could not be read"},
            "integrity": {
                "ok": False,
                "warnings": [
                    {
                        "code": "ledger_unreadable",
                        "detail": f"the paper ledger could not be read ({type(exc).__name__})",
                        "metrics": [],
                    }
                ],
            },
            "values": {},
            "unavailable": {},
            "definitions": {},
            "capital": {"configured": False, "reason": "the ledger could not be read"},
            "costs": {"complete": False, "missing": ["the ledger could not be read"]},
            "equity_curve": [],
            "session": {},
        }
    account["session"]["live_status"] = str(snapshot.get("status") or "")
    account["session"]["live_running"] = bool(snapshot.get("running"))
    if snapshot.get("max_quote_age_seconds"):
        account["session"]["max_quote_age_seconds"] = float(snapshot["max_quote_age_seconds"])
    return account


__all__ = ["DEFAULT_PAPER_PATH", "paper_root", "read_paper_account"]
