"""The durable record of one catalog evaluation.

The paper test must never start from an idea nobody measured.  This module writes
the evaluation report to one local JSON file, atomically, and reads it back with
everything it needs to say *what* was evaluated and *where the data came from*:

* the run's own summary (counts, budget, whether it finished or hit the budget);
* the cost labels, so a result that rests on an assumed commission says so;
* the data origin of every series, so a sample run can never be mistaken for the
  terminal's own history;
* the paper set: the rows that passed the screen, with their folds.

Rewriting replaces the file in one step, so a crash mid-write cannot leave a
half-parsed record behind.  Nothing secret is written: there is no account number,
no balance, no credential and no path outside the data directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

RECORD_VERSION = 1
RECORD_NAME = "catalog-evaluation.json"

#: Why a paper test may refuse to start from a stored record.
NO_RECORD = "no_evaluation_on_record"
STALE_RECORD = "evaluation_record_is_stale"
NO_PAPER_SET = "no_screened_candidate_in_the_record"
PARTIAL_RECORD = "evaluation_did_not_finish"
VERSION_MISMATCH = "evaluation_record_version_mismatch"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StoredEvaluation:
    """A record read back from disk, with the questions the paper path asks."""

    path: Path
    payload: Mapping[str, Any]

    @property
    def written_at(self) -> str:
        return str(self.payload.get("written_at") or "")

    @property
    def status(self) -> str:
        return str(self.payload.get("status") or "")

    @property
    def counts(self) -> dict[str, Any]:
        return dict(self.payload.get("counts") or {})

    @property
    def plan(self) -> dict[str, Any]:
        return dict(self.payload.get("plan") or {})

    @property
    def paper_set(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.payload.get("paper_set") or ())

    @property
    def data_origin(self) -> str:
        return str(self.payload.get("data_origin") or "")

    @property
    def age_seconds(self) -> float | None:
        stamp = self.written_at
        if not stamp:
            return None
        try:
            written = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - written).total_seconds())

    def blockers(self, *, max_age_seconds: float | None = None) -> tuple[dict[str, str], ...]:
        """Why this record cannot start a paper test, in the UI's own words."""

        found: list[dict[str, str]] = []
        version = int(self.payload.get("record_version") or 0)
        if version != RECORD_VERSION:
            found.append(
                {
                    "reason": VERSION_MISMATCH,
                    "detail": "The stored evaluation was written by a different version "
                    "of the catalog. Run the evaluation again.",
                }
            )
        if self.status not in ("COMPLETE", "TIMEOUT_PARTIAL"):
            found.append(
                {
                    "reason": PARTIAL_RECORD,
                    "detail": "The stored evaluation did not finish, so it cannot seed a "
                    "paper test. Run it again and let it complete.",
                }
            )
        if not self.paper_set:
            found.append(
                {
                    "reason": NO_PAPER_SET,
                    "detail": "No candidate passed the screen on the data that was "
                    "evaluated. Nothing is eligible for the paper test yet; more "
                    "history or another symbol may change that.",
                }
            )
        if max_age_seconds is not None:
            age = self.age_seconds
            if age is None:
                found.append(
                    {
                        "reason": STALE_RECORD,
                        "detail": "The stored evaluation has no readable timestamp.",
                    }
                )
            elif age > max_age_seconds:
                found.append(
                    {
                        "reason": STALE_RECORD,
                        "detail": f"The stored evaluation is {age / 3600:.1f} hours old, "
                        f"older than the {max_age_seconds / 3600:.1f} hours allowed. Run it "
                        "again before paper trading.",
                    }
                )
        return tuple(found)


def _data_origin(plan: Mapping[str, Any]) -> str:
    origins = {
        str(slot.get("origin") or "").upper()
        for slot in plan.get("slots", [])
        if isinstance(slot, Mapping)
    }
    if not origins:
        return ""
    if any(
        marker in origin
        for origin in origins
        for marker in ("TERMINAL", "MT5_COPY_RATES")
    ):
        return "MT5_TERMINAL_READ_ONLY" if len(origins) == 1 else "MIXED_TERMINAL_AND_SAMPLE"
    return "SAMPLE_OR_FIXTURE_DATA"


class CatalogStore:
    """One JSON file per data directory: the last evaluation, and its paper set."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.path = self.root / RECORD_NAME

    # -- writing ----------------------------------------------------------

    def save(self, report: Any, *, extra: Mapping[str, Any] | None = None) -> Path:
        payload = report.to_payload(limit=0) if hasattr(report, "to_payload") else dict(report)
        payload["record_version"] = RECORD_VERSION
        payload["written_at"] = _now().isoformat()
        payload["data_origin"] = _data_origin(payload.get("plan") or {})
        if extra:
            payload["extra"] = dict(extra)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)
        return self.path

    # -- reading ----------------------------------------------------------

    def load(self) -> StoredEvaluation | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        return StoredEvaluation(path=self.path, payload=payload)

    def blockers(self, *, max_age_seconds: float | None = None) -> tuple[dict[str, str], ...]:
        record = self.load()
        if record is None:
            return (
                {
                    "reason": NO_RECORD,
                    "detail": "No catalog evaluation is on record yet. Run the evaluation "
                    "on the terminal's history first; the paper test starts from it.",
                },
            )
        return record.blockers(max_age_seconds=max_age_seconds)

    def paper_rows(self) -> tuple[dict[str, Any], ...]:
        record = self.load()
        return record.paper_set if record is not None else ()


def summarise_paper_set(rows: Iterable[Mapping[str, Any]]) -> str:
    """One line for the UI: how many, on which symbols."""

    items = list(rows)
    if not items:
        return "none"
    symbols = sorted({str(item.get("symbol") or "") for item in items})
    return f"{len(items)} candidate row(s) on {', '.join(symbols)}"


__all__ = [
    "CatalogStore",
    "NO_PAPER_SET",
    "NO_RECORD",
    "PARTIAL_RECORD",
    "RECORD_NAME",
    "RECORD_VERSION",
    "STALE_RECORD",
    "StoredEvaluation",
    "VERSION_MISMATCH",
    "summarise_paper_set",
]
