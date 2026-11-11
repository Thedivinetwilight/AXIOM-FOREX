"""SCRIBE: the append-only ledger, including everything that did not happen.

A ledger that only records fills is a marketing document.  This one records the
opportunities that were refused, the probability that was too uncertain, the EV
that lost to the spread, the guardian's refusals, the orders, the partial fills, the
modifications, the exits and the P&L — losers first, because a strategy's losers are
what its expectancy is made of.

The ledger is **append-only by construction**:

* entries are numbered, and each entry's hash includes the previous entry's hash, so
  editing or removing one entry breaks every hash after it;
* there is no update and no delete method;
* :meth:`Scribe.verify` recomputes the chain and names the first entry that does not
  match, so a reader can check the file rather than trust it.

``close()`` seals the ledger for the run.  Positions are never closed because the
ledger was sealed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: Entry kinds.  One vocabulary for the whole desk, so a report can count them.
OPPORTUNITY = "opportunity"
SIGNAL = "signal"
PROBABILITY = "probability"
EVALUATION = "ev_evaluation"
REJECTION = "rejection"
RISK_DECISION = "risk_decision"
ORDER_REQUEST = "order_request"
EXECUTION_RESULT = "execution_result"
POSITION_OPENED = "position_opened"
POSITION_MODIFICATION = "position_modification"
POSITION_EXIT = "position_exit"
PNL = "pnl"
QUALITY = "data_quality"
SYSTEM_ERROR = "system_error"
LIMIT_CHANGE_REQUEST = "limit_change_request"
APPROVAL = "approval"
STATE = "state_change"
RAW_TICK = "raw_tick_input"

KINDS = (
    OPPORTUNITY,
    SIGNAL,
    PROBABILITY,
    EVALUATION,
    REJECTION,
    RISK_DECISION,
    ORDER_REQUEST,
    EXECUTION_RESULT,
    POSITION_OPENED,
    POSITION_MODIFICATION,
    POSITION_EXIT,
    PNL,
    QUALITY,
    SYSTEM_ERROR,
    LIMIT_CHANGE_REQUEST,
    APPROVAL,
    STATE,
    RAW_TICK,
)


def _canonical(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def entry_hash(*, seq: int, at_ms: int, kind: str, actor: str, payload: Any, prev_hash: str) -> str:
    body = json.dumps(
        {
            "seq": seq,
            "at_ms": at_ms,
            "kind": kind,
            "actor": actor,
            "payload": payload,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    at_ms: int
    kind: str
    actor: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    run_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at_ms": self.at_ms,
            "kind": self.kind,
            "actor": self.actor,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "run_id": self.run_id,
        }


class Scribe:
    """Append-only, hash-chained ledger for one desk run."""

    def __init__(self, *, run_id: str, mode: str = "PAPER") -> None:
        self._run_id = run_id
        self._mode = mode
        self._entries: list[LedgerEntry] = []
        self._sealed = False

    # -- writing ---------------------------------------------------------

    def record(self, kind: str, *, at_ms: int, actor: str = "desk", **payload: Any) -> LedgerEntry:
        if self._sealed:
            raise RuntimeError("the ledger is sealed for this run; start a new run to record more")
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}")
        if not actor.strip():
            raise ValueError("every entry is attributable: actor must not be empty")
        body = _canonical({"mode": self._mode, **payload})
        seq = len(self._entries) + 1
        prev_hash = self._entries[-1].hash if self._entries else "0" * 64
        digest = entry_hash(
            seq=seq, at_ms=int(at_ms), kind=kind, actor=actor, payload=body, prev_hash=prev_hash
        )
        entry = LedgerEntry(
            seq=seq,
            at_ms=int(at_ms),
            kind=kind,
            actor=actor,
            payload=body,
            prev_hash=prev_hash,
            hash=digest,
            run_id=self._run_id,
        )
        self._entries.append(entry)
        return entry

    def reject(self, *, at_ms: int, reason: str, stage: str, actor: str = "desk", **payload: Any) -> LedgerEntry:
        return self.record(
            REJECTION, at_ms=at_ms, actor=actor, reason=reason, stage=stage, **payload
        )

    def exit_record(
        self,
        *,
        at_ms: int,
        position_id: str,
        reason: str,
        gross_points: Any,
        gross_account: Any,
        won: bool | None = None,
        actor: str = "position_manager",
        **payload: Any,
    ) -> LedgerEntry:
        """A loser is recorded with exactly the same ceremony as a winner.

        ``won`` should be the *net* result.  When a caller has no cost model it may
        omit it, and the gross sign is used — a fallback that is labelled by the
        caller's own ``cost_complete`` field, never silently preferred.
        """

        return self.record(
            POSITION_EXIT,
            at_ms=at_ms,
            actor=actor,
            position_id=position_id,
            reason=reason,
            gross_points=str(gross_points),
            gross_account=None if gross_account is None else str(gross_account),
            won=(float(gross_points) > 0 if won is None else bool(won)),
            **payload,
        )

    def error(self, *, at_ms: int, error: str, detail: str = "", actor: str = "desk", **payload: Any) -> LedgerEntry:
        return self.record(SYSTEM_ERROR, at_ms=at_ms, actor=actor, error=error, detail=detail, **payload)

    def seal(self, *, at_ms: int = 0, actor: str = "operator") -> LedgerEntry:
        """Close the run with a seal that names the head it sealed.

        Without the seal, deleting the last few lines of a file is invisible to a
        forward hash chain: everything that remains still chains correctly.  The
        seal makes truncation detectable by a reader who knows a sealed run was
        expected (:func:`verify_jsonl` with ``require_seal=True``), and it records
        the head hash so an outside copy can be compared with the file.
        """

        head = self._entries[-1].hash if self._entries else "0" * 64
        entry = self.record(
            STATE,
            at_ms=at_ms,
            actor=actor,
            state="sealed",
            sealed_head=head,
            sealed_entries=len(self._entries),
            note="append-only ledger sealed for this run",
        )
        self._sealed = True
        return entry

    # -- reading ---------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def sealed(self) -> bool:
        return self._sealed

    def entries(self, kind: str | None = None) -> tuple[LedgerEntry, ...]:
        if kind is None:
            return tuple(self._entries)
        return tuple(entry for entry in self._entries if entry.kind == kind)

    def mark_persisted_prefix(self, count: int) -> None:
        """Adopt a ledger prefix already verified and reproduced by exact replay."""
        if count != len(self._entries):
            raise ValueError("persisted_prefix_must_match_replayed_entries")
        self._written = count

    def count(self, kind: str) -> int:
        return sum(1 for entry in self._entries if entry.kind == kind)

    def opportunities(self) -> dict[str, list[LedgerEntry]]:
        grouped: dict[str, list[LedgerEntry]] = {}
        for entry in self._entries:
            opportunity_id = entry.payload.get("opportunity_id")
            if opportunity_id:
                grouped.setdefault(str(opportunity_id), []).append(entry)
        return grouped

    def verify(self) -> dict[str, Any]:
        prev = "0" * 64
        for index, entry in enumerate(self._entries):
            if entry.seq != index + 1:
                return {"ok": False, "broken_at": entry.seq, "reason": "sequence_gap"}
            if entry.prev_hash != prev:
                return {"ok": False, "broken_at": entry.seq, "reason": "previous_hash_mismatch"}
            recomputed = entry_hash(
                seq=entry.seq,
                at_ms=entry.at_ms,
                kind=entry.kind,
                actor=entry.actor,
                payload=entry.payload,
                prev_hash=entry.prev_hash,
            )
            if recomputed != entry.hash:
                return {"ok": False, "broken_at": entry.seq, "reason": "hash_mismatch"}
            prev = entry.hash
        return {"ok": True, "entries": len(self._entries), "head": prev[:16]}

    def integrity(self) -> dict[str, Any]:
        """Structural checks a reader would otherwise have to do by hand."""

        requests = {
            entry.payload.get("request_id"): entry for entry in self.entries(ORDER_REQUEST)
        }
        results = {
            entry.payload.get("request_id"): entry for entry in self.entries(EXECUTION_RESULT)
        }
        missing_results = sorted(key for key in requests if key not in results)
        exits = self.entries(POSITION_EXIT)
        losers = [entry for entry in exits if entry.payload.get("won") is False]
        winners = [entry for entry in exits if entry.payload.get("won") is True]
        errors = self.entries(SYSTEM_ERROR)
        return {
            "chain": self.verify(),
            "entries": len(self._entries),
            "order_requests_without_result": missing_results,
            "exits": len(exits),
            "winners": len(winners),
            "losers": len(losers),
            "errors": len(errors),
            "kinds": {kind: self.count(kind) for kind in KINDS if self.count(kind)},
            "ok": not missing_results,
            "note": (
                "Every order request must have a result. A request without one is an "
                "unresolved submission, and the desk treats it as a halt condition."
            ),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self._run_id,
            "mode": self._mode,
            "sealed": self._sealed,
            "entries": len(self._entries),
            "by_kind": {kind: self.count(kind) for kind in KINDS if self.count(kind)},
            "integrity": self.integrity(),
        }

    # -- persistence -----------------------------------------------------

    def to_jsonl(self, path: str | Path) -> dict[str, Any]:
        """Append every entry to a JSONL file.  Opening in append mode is the point."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with target.open("a", encoding="utf-8") as handle:
            for entry in self._entries[getattr(self, "_written", 0) :]:
                handle.write(json.dumps(entry.as_dict(), sort_keys=True, default=str) + "\n")
                written += 1
        self._written = len(self._entries)
        return {
            "path": str(target),
            "appended": written,
            "total_entries": len(self._entries),
            "append_only": True,
        }


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a ledger back.  Refuses rather than repairs if a line is not an object."""

    target = Path(path)
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{target}:{number} is not a ledger object")
            rows.append(row)
    return rows


def verify_jsonl(
    path: str | Path,
    *,
    expected_head: str | None = None,
    require_seal: bool = False,
) -> dict[str, Any]:
    """Recompute the chain of a file, separately from the object that wrote it.

    ``expected_head`` compares this file with a head recorded elsewhere — the only
    way to detect that whole *trailing* entries were removed.  ``require_seal``
    demands the seal entry a closed run writes.
    """

    rows = read_jsonl(path)
    prev = "0" * 64
    seal: dict[str, Any] | None = None
    for index, row in enumerate(rows, start=1):
        if int(row.get("seq", -1)) != index:
            return {"ok": False, "broken_at": index, "reason": "sequence_gap"}
        if row.get("prev_hash") != prev:
            return {"ok": False, "broken_at": index, "reason": "previous_hash_mismatch"}
        recomputed = entry_hash(
            seq=int(row["seq"]),
            at_ms=int(row["at_ms"]),
            kind=str(row["kind"]),
            actor=str(row["actor"]),
            payload=row["payload"],
            prev_hash=str(row["prev_hash"]),
        )
        if recomputed != row.get("hash"):
            return {"ok": False, "broken_at": index, "reason": "hash_mismatch"}
        if row.get("payload", {}).get("state") == "sealed":
            seal = {"at": index, "head": row["payload"].get("sealed_head"), "entries": row["payload"].get("sealed_entries")}
        prev = recomputed
    if seal is not None:
        if seal["at"] != len(rows):
            return {"ok": False, "broken_at": seal["at"], "reason": "entries_appended_after_the_seal"}
        expected_entries = seal["entries"]
        if expected_entries is not None and int(expected_entries) != len(rows) - 1:
            return {"ok": False, "broken_at": len(rows), "reason": "seal_entry_count_mismatch"}
    elif require_seal:
        return {"ok": False, "broken_at": len(rows), "reason": "missing_seal"}
    if expected_head is not None:
        head = rows[-1].get("payload", {}).get("sealed_head") if seal else prev
        if not str(head or "").startswith(expected_head):
            return {"ok": False, "broken_at": len(rows), "reason": "head_mismatch"}
    return {"ok": True, "entries": len(rows), "head": prev[:16], "sealed": seal is not None}


__all__ = [
    "APPROVAL",
    "EVALUATION",
    "EXECUTION_RESULT",
    "KINDS",
    "LedgerEntry",
    "LIMIT_CHANGE_REQUEST",
    "OPPORTUNITY",
    "ORDER_REQUEST",
    "PNL",
    "POSITION_EXIT",
    "POSITION_MODIFICATION",
    "POSITION_OPENED",
    "PROBABILITY",
    "QUALITY",
    "REJECTION",
    "RISK_DECISION",
    "RAW_TICK",
    "SIGNAL",
    "STATE",
    "SYSTEM_ERROR",
    "Scribe",
    "entry_hash",
    "read_jsonl",
    "verify_jsonl",
]
