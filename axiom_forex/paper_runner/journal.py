"""Durable record of what the paper runner decided, before and after it acted.

Two kinds of record, both in the platform's own SQLite database:

``paper_runner_intent``
    Written **before** an order is executed, one row per intent.  If the process
    dies between the write and the fill, the next start finds an intent with no
    matching order and refuses to trade until an operator looks at it.  This is
    the runner's write-ahead record; the paper engine's own write-ahead order row
    and reservation then sit inside the settlement transaction as usual.

``paper_runner``
    The run state after each cycle: counters, the last tick/signal/decision/fill
    instants, the last reason code, and the feed's position.  One row per run,
    replaced each cycle, so a restart resumes *counters* without ever resuming
    orders.

The journal writes no price history, no credential, and no account identifier.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

KIND_STATE = "paper_runner"
KIND_INTENT = "paper_runner_intent"
#: Whole-book snapshots written by the runner, so recovery always has a base that
#: is not a single settlement's book.
PORTFOLIO_SNAPSHOT_PREFIX = "portfolio:paper-runner:"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunnerJournal:
    """Thin, explicit persistence for the runner.  No business logic here."""

    def __init__(
        self,
        *,
        repository: Any,
        audit_log: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.audit_log = audit_log
        self.clock = clock or _utc_now

    # -- intents ---------------------------------------------------------

    @staticmethod
    def intent_id(run_id: str, sequence: int) -> str:
        return f"{run_id}:intent:{sequence}"

    def write_intent(self, *, run_id: str, sequence: int, payload: Mapping[str, Any]) -> str:
        snapshot_id = self.intent_id(run_id, sequence)
        record = dict(payload)
        record.setdefault("status", "PENDING")
        record.update({"run_id": run_id, "sequence": sequence, "kind": KIND_INTENT})
        self.repository.save_snapshot(snapshot_id, KIND_INTENT, record)
        self._audit(
            {
                "event_type": "PAPER_RUNNER_INTENT",
                "run_id": run_id,
                "sequence": sequence,
                "request_id": record.get("request_id", ""),
                "action": record.get("action", ""),
                "live": False,
            }
        )
        return snapshot_id

    def resolve_intent(
        self,
        *,
        run_id: str,
        sequence: int,
        status: str,
        reason: str = "",
        request_id: str = "",
    ) -> str:
        """Record how one intent ended, so a refusal is not mistaken for a crash.

        An intent left ``PENDING`` means a process stopped between the decision
        and its outcome, which stays a blocker.  ``REFUSED``/``FILLED``/``SENT``
        are clean outcomes and are recorded as such.
        """

        snapshot_id = self.intent_id(run_id, sequence)
        existing: dict[str, Any] = {}
        for snapshot in self.repository.list_snapshots(kind=KIND_INTENT):
            if str(snapshot.get("snapshot_id", "")) == snapshot_id:
                existing = dict(snapshot)
                break
        record = {
            **existing,
            "run_id": run_id,
            "sequence": sequence,
            "kind": KIND_INTENT,
            "request_id": request_id or str(existing.get("request_id", "")),
            "status": str(status),
            "reason": str(reason),
            "resolved_at": self.clock().isoformat(),
        }
        self.repository.save_snapshot(snapshot_id, KIND_INTENT, record)
        self._audit(
            {
                "event_type": "PAPER_RUNNER_INTENT_RESOLVED",
                "run_id": run_id,
                "sequence": sequence,
                "status": str(status),
                "reason": str(reason),
                "live": False,
            }
        )
        return snapshot_id

    def intents(self, run_id: str) -> list[dict[str, Any]]:
        prefix = f"{run_id}:intent:"
        return [
            dict(snapshot)
            for snapshot in self.repository.list_snapshots(kind=KIND_INTENT)
            if str(snapshot.get("snapshot_id", "")).startswith(prefix)
        ]

    # -- state -----------------------------------------------------------

    @staticmethod
    def state_id(run_id: str) -> str:
        return f"paper_runner:{run_id}"

    def save_state(self, *, run_id: str, payload: Mapping[str, Any]) -> None:
        record = dict(payload)
        record.setdefault("run_id", run_id)
        self.repository.save_snapshot(self.state_id(run_id), KIND_STATE, record)

    def load_state(self, run_id: str) -> dict[str, Any] | None:
        for snapshot in self.repository.list_snapshots(kind=KIND_STATE):
            if snapshot.get("snapshot_id") == self.state_id(run_id):
                return dict(snapshot)
        return None

    # -- the book --------------------------------------------------------

    def save_portfolio(self, *, run_id: str, snapshot: Mapping[str, Any]) -> None:
        self.repository.save_snapshot(
            f"{PORTFOLIO_SNAPSHOT_PREFIX}{run_id}", "portfolio", dict(snapshot)
        )

    def _audit(self, payload: Mapping[str, Any]) -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.record(dict(payload))
        except Exception:
            # An audit failure must never be swallowed silently by the caller's
            # happy path -- but the runner's guard already treats a failed
            # write-ahead as fatal.  Here it is only the mirror event.
            pass


__all__ = [
    "KIND_INTENT",
    "KIND_STATE",
    "PORTFOLIO_SNAPSHOT_PREFIX",
    "RunnerJournal",
]
