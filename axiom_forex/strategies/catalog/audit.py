"""An audit of the whole declared catalog: every candidate, and what became of it.

The evaluation record already carries one row per (candidate, symbol, timeframe),
including the ones it never reached, because a row that was skipped is still a
result.  What it does not do is answer the operator's actual question:

    I declared 1,566 strategies.  Which ones were tested, which were skipped,
    which failed -- and why, for each one?

This module folds that record into one row per *declared* candidate, so nothing
in the catalog can quietly go missing from the report:

* ``TESTED`` -- at least one row ran and produced a measurement;
* ``FAILED`` -- a row that ran raised or could not be measured;
* ``SKIPPED`` -- the evaluation reached it but did not run it (no history, a bar
  size the candidate is not offered on, the time budget, a cancelled run);
* ``NOT_CONSIDERED`` -- the run's own filters never planned it at all, which is
  also a reason and is reported as one.

It reads the stored record and nothing else.  It does not evaluate, re-rank,
tune, or touch the data: the walk-forward and chronological split rules are the
ones ``evaluate.py`` already applied, and the locked test window is used exactly
as that module used it -- read, never fitted.  Running this audit cannot change
a result, and it cannot promote anything.

Two sentences are therefore repeated verbatim in every report it writes:

* a historical evaluation is evidence about the past, not a forecast;
* nothing here shows that any candidate will do well on a live account.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "axiom-catalog-audit-v1"

#: The record this audit reads, and the record it writes.  Both sit beside the
#: evaluation record and both are ordinary local files.
SOURCE_RECORD = "catalog-evaluation.json"
AUDIT_RECORD = "catalog-audit.json"

TESTED = "TESTED"
SKIPPED = "SKIPPED"
FAILED = "FAILED"
NOT_CONSIDERED = "NOT_CONSIDERED"

STATUSES = (TESTED, SKIPPED, FAILED, NOT_CONSIDERED)

#: Why a declared candidate has no cell at all in the record.
REASON_NEVER_PLANNED = "not_planned_by_this_evaluation"
REASON_NO_RECORD = "no_evaluation_record"
REASON_UNREADABLE = "evaluation_record_unreadable"

#: One wording, used by the report and by every page that shows it.
CLAIM = (
    "A historical evaluation on the bars this deployment could supply. It is "
    "evidence about the past under the stated cost assumptions, not a forecast, "
    "and it does not show that any candidate will do well on a live account. No "
    "candidate here is claimed to be profitable."
)

TUNING_NOTE = (
    "The chronological split and walk-forward rules are the evaluation module's "
    "own, applied unchanged: the test window is read once, never fitted, and this "
    "audit cannot re-rank or re-tune anything."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CandidateAudit:
    """One declared candidate, and every reason the record gives for its status."""

    candidate_id: str
    family: str
    structure: str
    status: str
    reasons: tuple[str, ...]
    cells: tuple[Mapping[str, Any], ...]
    tested_cells: int
    skipped_cells: int
    failed_cells: int

    def audit_view(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "family": self.family,
            "structure": self.structure,
            "status": self.status,
            "reasons": list(self.reasons),
            "tested_cells": self.tested_cells,
            "skipped_cells": self.skipped_cells,
            "failed_cells": self.failed_cells,
            "cells": [dict(cell) for cell in self.cells],
        }


def _status_for_cells(cells: Sequence[Mapping[str, Any]]) -> str:
    if any(str(cell.get("status") or "") == TESTED for cell in cells):
        return TESTED
    if any(str(cell.get("status") or "") == FAILED for cell in cells):
        return FAILED
    if cells:
        return SKIPPED
    return NOT_CONSIDERED


def _reasons_for(cells: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    reasons: list[str] = []
    for cell in cells:
        for reason in cell.get("reasons") or ():
            text = str(reason or "").strip()
            if text and text not in reasons:
                reasons.append(text)
        detail = str(cell.get("detail_class") or "").strip()
        if str(cell.get("status") or "") == FAILED and detail and detail not in reasons:
            reasons.append(detail)
    if not cells:
        reasons.append(REASON_NEVER_PLANNED)
    return tuple(reasons)


def _cell(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(row.get("symbol") or ""),
        "timeframe": str(row.get("timeframe") or ""),
        "status": str(row.get("status") or ""),
        "reasons": [str(item) for item in row.get("reasons") or ()],
        "screen": str(row.get("screen") or ""),
        "trades": int(row.get("trades") or 0),
        "score": row.get("score"),
        "folds_positive": int(row.get("folds_positive") or 0),
        "costs_incomplete": bool(row.get("costs_incomplete", False)),
        # Present on a full audit view; absent from the compact record.  When it is
        # there it is a reason a row failed, so it is carried through.
        "detail_class": str(row.get("detail_class") or ""),
    }


def declared_ids(candidates: Iterable[Any]) -> tuple[str, ...]:
    """Every declared candidate id, in declaration order, without duplicates."""

    seen: list[str] = []
    for item in candidates:
        name = str(getattr(item, "candidate_id", item) or "").strip()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def build_audit(
    payload: Mapping[str, Any] | None,
    *,
    declared: Sequence[Any] | None = None,
    now: datetime | None = None,
    source: str = SOURCE_RECORD,
) -> dict[str, Any]:
    """Fold one evaluation record into a per-candidate audit.  Reads only.

    ``declared`` is the catalog the caller wants accounted for.  With ``None`` the
    module's own declared catalog is used, so "every candidate" means every
    candidate this build ships, not only the ones the last run happened to plan.
    """

    stamp = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    if payload is None:
        return _empty_audit(stamp, reason=REASON_NO_RECORD, source=source)
    if not isinstance(payload, Mapping):
        return _empty_audit(stamp, reason=REASON_UNREADABLE, source=source)

    if declared is None:
        from .candidates import catalog

        declared = catalog()
    ids = declared_ids(declared)
    families: dict[str, str] = {}
    structures: dict[str, str] = {}
    for item in declared or ():
        name = str(getattr(item, "candidate_id", item) or "")
        if name:
            families[name] = str(getattr(item, "family", "") or "")
            structures[name] = str(getattr(item, "structure", "") or "")

    rows = [row for row in payload.get("rows") or () if isinstance(row, Mapping)]
    by_candidate: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        name = str(row.get("candidate_id") or "")
        by_candidate.setdefault(name, []).append(row)
        if name and name not in families:
            families[name] = str(row.get("family") or "")
            structures[name] = str(row.get("structure") or "")

    # A row for a candidate that is no longer declared is still reported: a record
    # that mentions a strategy this build does not ship is a finding, not noise.
    unknown = tuple(sorted(name for name in by_candidate if name and name not in set(ids)))

    entries: list[CandidateAudit] = []
    for name in list(ids) + list(unknown):
        cells = tuple(_cell(row) for row in by_candidate.get(name, ()))
        entries.append(
            CandidateAudit(
                candidate_id=name,
                family=families.get(name, ""),
                structure=structures.get(name, ""),
                status=_status_for_cells(cells),
                reasons=_reasons_for(cells),
                cells=cells,
                tested_cells=sum(1 for cell in cells if cell["status"] == TESTED),
                skipped_cells=sum(1 for cell in cells if cell["status"] == SKIPPED),
                failed_cells=sum(1 for cell in cells if cell["status"] == FAILED),
            )
        )

    counts = dict(payload.get("counts") or {})
    summary = {
        "declared": len(ids),
        "listed": len(entries),
        "tested": sum(1 for entry in entries if entry.status == TESTED),
        "skipped": sum(1 for entry in entries if entry.status == SKIPPED),
        "failed": sum(1 for entry in entries if entry.status == FAILED),
        "not_considered": sum(1 for entry in entries if entry.status == NOT_CONSIDERED),
        "cells": len(rows),
        "cells_tested": sum(1 for row in rows if row.get("status") == TESTED),
        "cells_skipped": sum(1 for row in rows if row.get("status") == SKIPPED),
        "cells_failed": sum(1 for row in rows if row.get("status") == FAILED),
        "undeclared_in_record": len(unknown),
        "evaluation_status": str(payload.get("status") or ""),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "budget_seconds": payload.get("budget_seconds"),
        "started_at": str(payload.get("started_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
        "skip_reasons": dict(counts.get("skip_reasons") or {}),
        "screen": dict(counts.get("screen") or {}),
        "survivors": payload.get("survivors"),
        "paper_set": len(payload.get("paper_set") or ()),
    }
    notes = [CLAIM, TUNING_NOTE]
    if not rows:
        notes.append(
            "The record holds no rows at all, so no candidate could be tested. "
            "That is a missing measurement, not a clean result."
        )
    if unknown:
        notes.append(
            f"{len(unknown)} candidate(s) in the record are not in this build's "
            "declared catalog; they are listed rather than dropped."
        )
    return {
        "schema": SCHEMA,
        "generated_at": stamp,
        "source": str(source or SOURCE_RECORD),
        "claim": CLAIM,
        "tuning_note": TUNING_NOTE,
        "notes": notes,
        "summary": summary,
        "candidates": [entry.audit_view() for entry in entries],
    }


def _empty_audit(stamp: str, *, reason: str, source: str) -> dict[str, Any]:
    from .candidates import catalog

    ids = declared_ids(catalog())
    entries = [
        CandidateAudit(
            candidate_id=name,
            family="",
            structure="",
            status=NOT_CONSIDERED,
            reasons=(reason,),
            cells=(),
            tested_cells=0,
            skipped_cells=0,
            failed_cells=0,
        ).audit_view()
        for name in ids
    ]
    return {
        "schema": SCHEMA,
        "generated_at": stamp,
        "source": str(source or SOURCE_RECORD),
        "claim": CLAIM,
        "tuning_note": TUNING_NOTE,
        "notes": [
            CLAIM,
            TUNING_NOTE,
            "There is no usable evaluation record, so nothing is measured. Every "
            "declared candidate is listed as not considered -- absence of a test is "
            "not a pass.",
        ],
        "summary": {
            "declared": len(ids),
            "listed": len(entries),
            "tested": 0,
            "skipped": 0,
            "failed": 0,
            "not_considered": len(entries),
            "cells": 0,
            "cells_tested": 0,
            "cells_skipped": 0,
            "cells_failed": 0,
            "undeclared_in_record": 0,
            "evaluation_status": "",
            "elapsed_seconds": None,
            "budget_seconds": None,
            "started_at": "",
            "finished_at": "",
            "skip_reasons": {},
            "screen": {},
            "survivors": None,
            "paper_set": 0,
        },
        "candidates": entries,
    }


def write_audit(document: Mapping[str, Any], path: str | Path) -> Path:
    """Write the audit atomically.  It is a report, so it is always replaced."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        prefix=".catalog-audit-",
        suffix=".json",
        dir=str(target.parent),
        delete=False,
        encoding="utf-8",
    )
    try:
        handle.write(text)
        handle.close()
        temporary = Path(handle.name)
        temporary.replace(target)
    finally:
        leftover = Path(handle.name)
        if leftover.exists():
            leftover.unlink()
    return target


def read_audit(path: str | Path) -> dict[str, Any] | None:
    """Read a stored audit, or ``None`` when it cannot be used.  Never raises."""

    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def lines(document: Mapping[str, Any], *, limit: int = 20) -> tuple[str, ...]:
    """A plain-text rendering, for the console and for a copy-pasted report."""

    summary = dict(document.get("summary") or {})
    head = (
        f"declared {summary.get('declared', 0)} · tested {summary.get('tested', 0)} · "
        f"skipped {summary.get('skipped', 0)} · failed {summary.get('failed', 0)} · "
        f"not considered {summary.get('not_considered', 0)}"
    )
    out = [head, str(document.get("claim") or CLAIM)]
    for entry in list(document.get("candidates") or ())[:limit]:
        reasons = ", ".join(entry.get("reasons") or ()) or "—"
        out.append(
            f"{str(entry.get('status', '')):15s} {str(entry.get('candidate_id', '')):24s} {reasons}"
        )
    total = len(document.get("candidates") or ())
    if total > limit:
        out.append(f"(+{total - limit} more candidates in the record)")
    return tuple(out)


def summarise(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """The subset a page needs to show the audit without loading every row."""

    if not document:
        return {"present": False}
    summary = dict(document.get("summary") or {})
    return {
        "present": True,
        "generated_at": str(document.get("generated_at") or ""),
        "source": str(document.get("source") or ""),
        "claim": str(document.get("claim") or CLAIM),
        "tuning_note": str(document.get("tuning_note") or TUNING_NOTE),
        "notes": [str(item) for item in document.get("notes") or ()],
        **summary,
    }


__all__ = [
    "AUDIT_RECORD",
    "CLAIM",
    "CandidateAudit",
    "FAILED",
    "NOT_CONSIDERED",
    "REASON_NEVER_PLANNED",
    "REASON_NO_RECORD",
    "REASON_UNREADABLE",
    "SCHEMA",
    "SKIPPED",
    "SOURCE_RECORD",
    "STATUSES",
    "TESTED",
    "TUNING_NOTE",
    "build_audit",
    "declared_ids",
    "lines",
    "read_audit",
    "summarise",
    "write_audit",
]
