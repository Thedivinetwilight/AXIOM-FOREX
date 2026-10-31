"""The 24-hour paper qualification: a stopwatch that only counts healthy time.

The point of a forward test is not that a process was *started*; it is that the
strategy was actually watched, on live quotes, for a full day.  So this module
keeps a clock with three rules, and nothing else:

* **Only healthy time counts.**  A tick that reports a disconnected terminal, a
  stale quote, a stopped runner or a failed cycle pauses the clock.  The pause is
  recorded with its reason and duration.  A broken period can never be mistaken
  for successful testing.
* **A gap is not activity.**  If two heartbeats are further apart than
  ``max_tick_gap_seconds`` -- a sleeping laptop, a suspended process, a clock that
  jumped -- the whole gap is booked as a pause, not as active time.  This is what
  makes a restarted or wedged runner impossible to count.
* **The criteria are fixed when the test starts.**  The pass criteria are written
  into the record at ``start`` and never re-read from the caller during the run, so
  a test cannot be graded against a rule invented after seeing the result.

What it deliberately does not do: it holds no adapter, no mode controller, no
order surface.  Passing this test is a *precondition* the live page reads; it is
not a licence, and it authorises nothing by itself.

The state lives in one JSON document under ``data/catalog/``: local, Git-ignored,
and containing no identifier, balance or credential -- only timestamps, reasons
and counters.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

QUALIFICATION_FILENAME = "qualification.json"
SCHEMA_VERSION = 1

#: Where the test is, as far as the operator is concerned.
NOT_STARTED = "NOT_STARTED"
RUNNING = "RUNNING"
PASSED = "PASSED"
FAILED = "FAILED"
STOPPED = "STOPPED"

#: Every pause has one of these reasons.  The page prints them; the report counts
#: the seconds under each.
PAUSE_DISCONNECTED = "terminal_disconnected"
PAUSE_STALE = "stale_quote"
PAUSE_RUNNER_STOPPED = "runner_stopped"
PAUSE_RUNNER_RESTARTED = "runner_restarted"
PAUSE_UNRESPONSIVE = "runner_unresponsive"
PAUSE_MARKET_CLOSED = "market_closed"
PAUSE_OPERATOR = "operator_stopped"
PAUSE_CYCLE_ERROR = "cycle_error"
PAUSE_UNKNOWN = "unhealthy_unknown"

#: A failure is something that means the test itself could not be trusted.
FAILURE_CYCLE = "cycle_error"
FAILURE_LEDGER = "ledger_not_writable"
FAILURE_DUPLICATE_START = "duplicate_start_attempt"
FAILURE_WATCH_LOST = "screened_row_lost"

#: How many pauses and events the document keeps.  The totals are kept forever;
#: the itemised list is what is bounded.
MAX_PAUSES = 400
MAX_EVENTS = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat() if value is not None else ""


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class QualificationPolicy:
    """The pass criteria.  Written into the record at ``start`` and never changed."""

    #: 24 hours of counted, healthy time.
    required_active_seconds: float = 24.0 * 3600.0
    #: The wall-clock span must be at least this long, so a jumped clock cannot
    #: compress a day of active time into an afternoon.
    required_span_seconds: float = 24.0 * 3600.0
    #: Downtime may not dominate: paused time as a share of the span.
    max_pause_fraction: float = 0.30
    #: Stale quotes may not dominate: stale ticks as a share of all ticks.
    max_stale_fraction: float = 0.25
    #: A day in which no decision was ever evaluated is not a test.
    min_decisions: int = 1
    #: Failures are disqualifying by default.
    max_failures: int = 0
    #: Two heartbeats further apart than this are a gap, not activity.
    max_tick_gap_seconds: float = 300.0
    #: A restarted runner is reported, and its dark period is not counted.
    max_restarts: int = 5

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_active_seconds": self.required_active_seconds,
            "required_span_seconds": self.required_span_seconds,
            "max_pause_fraction": self.max_pause_fraction,
            "max_stale_fraction": self.max_stale_fraction,
            "min_decisions": self.min_decisions,
            "max_failures": self.max_failures,
            "max_tick_gap_seconds": self.max_tick_gap_seconds,
            "max_restarts": self.max_restarts,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "QualificationPolicy":
        data = dict(payload or {})
        base = cls()
        return cls(
            required_active_seconds=float(
                data.get("required_active_seconds", base.required_active_seconds)
            ),
            required_span_seconds=float(
                data.get("required_span_seconds", base.required_span_seconds)
            ),
            max_pause_fraction=float(data.get("max_pause_fraction", base.max_pause_fraction)),
            max_stale_fraction=float(data.get("max_stale_fraction", base.max_stale_fraction)),
            min_decisions=int(data.get("min_decisions", base.min_decisions)),
            max_failures=int(data.get("max_failures", base.max_failures)),
            max_tick_gap_seconds=float(
                data.get("max_tick_gap_seconds", base.max_tick_gap_seconds)
            ),
            max_restarts=int(data.get("max_restarts", base.max_restarts)),
        )


@dataclass(frozen=True)
class Criterion:
    """One pass criterion: what was required, what happened, and whether it held."""

    key: str
    label: str
    required: str
    actual: str
    met: bool
    note: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "required": self.required,
            "actual": self.actual,
            "met": self.met,
            "note": self.note,
        }


def _hours(seconds: float) -> str:
    return f"{seconds / 3600.0:.2f} h"


def _pct(fraction: float) -> str:
    return f"{100.0 * fraction:.1f}%"


class QualificationClock:
    """The durable stopwatch.  One instance per data directory."""

    def __init__(
        self,
        root: Path | str,
        *,
        policy: QualificationPolicy | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / QUALIFICATION_FILENAME
        self._clock = clock
        self._policy = policy or QualificationPolicy()
        #: Kept apart from ``self._policy``: loading an old record adopts *its*
        #: policy for continuity, but a person who states criteria for a new test
        #: means them, so ``start`` prefers this one.
        self._explicit_policy = policy
        self._lock = threading.RLock()
        self._document: dict[str, Any] = self._blank()
        self._loaded = False

    # -- documents ---------------------------------------------------------

    def _blank(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "status": NOT_STARTED,
            "policy": self._policy.as_dict(),
            "started_at": "",
            "finished_at": "",
            "last_tick_at": "",
            "halted_at": "",
            "halt_reason": "",
            "active_seconds": 0.0,
            "paused_seconds": 0.0,
            "ticks": 0,
            "healthy_ticks": 0,
            "stale_ticks": 0,
            "decisions": 0,
            "unfilled_signals": 0,
            "pause_seconds_by_reason": {},
            "pauses": [],
            "events": [],
            "failures": [],
            "recoveries": 0,
            "restarts": 0,
            "symbols": [],
            "candidates": [],
            "claim": (
                "One day of forward paper running is an operational check of the "
                "pipeline, not proof of profitability, safety, or readiness."
            ),
        }

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            self._document = self._blank()
            return
        if not isinstance(payload, dict) or int(payload.get("schema") or 0) != SCHEMA_VERSION:
            self._document = self._blank()
            return
        document = self._blank()
        document.update(payload)
        self._document = document
        # A record already on disk governs a resumption; a policy stated to this
        # constructor governs only a test this object starts itself (see ``start``).
        self._policy = QualificationPolicy.from_dict(document.get("policy"))

    def _write(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(
                json.dumps(self._document, indent=2, sort_keys=True), encoding="utf-8"
            )
            temporary.replace(self.path)
        except OSError:
            # A clock that cannot be written is a halt: the report must not claim a
            # record it does not have.  The caller sees the failure on the next read.
            self._document["halt_reason"] = FAILURE_LEDGER
            self._document["status"] = FAILED

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        *,
        policy: QualificationPolicy | None = None,
        symbols: Sequence[str] = (),
        candidates: Sequence[str] = (),
        note: str = "",
    ) -> dict[str, Any]:
        """Begin a new test.  The criteria are fixed here, once."""

        with self._lock:
            self._load()
            now = self._clock()
            self._policy = policy or self._explicit_policy or self._policy
            document = self._blank()
            document["policy"] = self._policy.as_dict()
            document["status"] = RUNNING
            document["started_at"] = _iso(now)
            document["last_tick_at"] = _iso(now)
            document["symbols"] = [str(item) for item in symbols][:24]
            document["candidates"] = [str(item) for item in candidates][:64]
            document["events"].append(
                {"at": _iso(now), "kind": "started", "note": str(note or "")}
            )
            self._document = document
            self._write()
            return self.state()

    def reset(self, *, reason: str = "operator_reset") -> dict[str, Any]:
        """Discard the record and go back to NOT_STARTED.  Records the act."""

        with self._lock:
            self._load()
            previous = self._document.get("status", NOT_STARTED)
            document = self._blank()
            document["events"].append(
                {
                    "at": _iso(self._clock()),
                    "kind": "reset",
                    "note": f"previous status {previous}: {reason}",
                }
            )
            self._document = document
            self._write()
            return self.state()

    def recover(self) -> dict[str, Any]:
        """Resume after a restart without counting the dark period.

        Called once when the process that owns the test comes up.  If the record
        says a test was running, the interval is closed at the *last heartbeat* --
        never at "now", which would credit the process for the time it was gone --
        and the gap is booked as a ``runner_restarted`` pause with its real length.
        """

        with self._lock:
            self._load()
            now = self._clock()
            if self._document.get("status") != RUNNING:
                return self.state()
            last = _parse(self._document.get("last_tick_at")) or _parse(
                self._document.get("started_at")
            )
            if last is None:
                return self.state()
            gap = max(0.0, (now - last).total_seconds())
            self._document["restarts"] = int(self._document.get("restarts", 0)) + 1
            self._document["recoveries"] = int(self._document.get("recoveries", 0)) + 1
            if gap > 0:
                self._add_pause(last, now, PAUSE_RUNNER_RESTARTED, gap)
            self._document["last_tick_at"] = _iso(now)
            self._document["events"].append(
                {
                    "at": _iso(now),
                    "kind": "recovered",
                    "note": f"unwatched for {gap:.0f}s; that time is not counted",
                }
            )
            self._write()
            return self.state()

    def stop(self, *, reason: str = PAUSE_OPERATOR, failed: bool = False) -> dict[str, Any]:
        """End the test.  Stops accruing time; keeps every number."""

        with self._lock:
            self._load()
            now = self._clock()
            if self._document.get("status") == RUNNING:
                self._close_open_interval(now, reason)
                self._document["finished_at"] = _iso(now)
                self._document["status"] = FAILED if failed else STOPPED
                self._document["events"].append(
                    {"at": _iso(now), "kind": "stopped", "note": str(reason)}
                )
                self._write()
            return self.state()

    # -- the heartbeat -----------------------------------------------------

    def tick(
        self,
        *,
        healthy: bool,
        reason: str = "",
        quote_fresh: bool | None = None,
        decisions: int = 0,
        unfilled: int = 0,
    ) -> str:
        """Record one heartbeat.  Returns the pause reason, or ``""`` if counting.

        ``healthy`` is the caller's verdict: the terminal is connected, the quote is
        fresh, and the runner did a real cycle.  Anything else pauses the clock.
        """

        with self._lock:
            self._load()
            if self._document.get("status") != RUNNING:
                return "not_running"
            now = self._clock()
            last = _parse(self._document.get("last_tick_at")) or _parse(
                self._document.get("started_at")
            )
            gap = 0.0 if last is None else max(0.0, (now - last).total_seconds())
            self._document["ticks"] = int(self._document.get("ticks", 0)) + 1
            if quote_fresh is True:
                self._document["healthy_ticks"] = int(self._document.get("healthy_ticks", 0)) + 1
            elif quote_fresh is False:
                self._document["stale_ticks"] = int(self._document.get("stale_ticks", 0)) + 1
            if decisions:
                self._document["decisions"] = int(self._document.get("decisions", 0)) + int(
                    decisions
                )
            if unfilled:
                self._document["unfilled_signals"] = int(
                    self._document.get("unfilled_signals", 0)
                ) + int(unfilled)
            pause_reason = "" if healthy else (reason or PAUSE_UNKNOWN)
            if gap > self._policy.max_tick_gap_seconds:
                # A gap this long is not activity, however healthy this one tick
                # looks: the process was not watching.
                self._add_pause(last or now, now, PAUSE_UNRESPONSIVE, gap)
                self._document["last_tick_at"] = _iso(now)
                self._write()
                return PAUSE_UNRESPONSIVE
            if gap > 0:
                if pause_reason:
                    self._document["paused_seconds"] = (
                        float(self._document.get("paused_seconds", 0.0)) + gap
                    )
                    self._count_pause(pause_reason, gap)
                else:
                    self._document["active_seconds"] = (
                        float(self._document.get("active_seconds", 0.0)) + gap
                    )
            self._document["last_tick_at"] = _iso(now)
            self._write()
            return pause_reason

    def record_failure(self, kind: str, detail: str = "") -> None:
        with self._lock:
            self._load()
            failures = list(self._document.get("failures") or [])
            failures.append({"at": _iso(self._clock()), "kind": str(kind), "detail": str(detail)})
            self._document["failures"] = failures[-40:]
            self._write()

    def record_event(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self._load()
            events = list(self._document.get("events") or [])
            event = {"at": _iso(self._clock()), "kind": str(kind)}
            event.update({key: value for key, value in fields.items()})
            events.append(event)
            self._document["events"] = events[-MAX_EVENTS:]
            self._write()

    def note_quote(self, *, fresh: bool) -> None:
        """Record freshness without advancing the clock (a page read may do this)."""

        with self._lock:
            self._load()
            if fresh:
                self._document["healthy_ticks"] = int(self._document.get("healthy_ticks", 0)) + 1
            else:
                self._document["stale_ticks"] = int(self._document.get("stale_ticks", 0)) + 1
            self._write()

    # -- internals ---------------------------------------------------------

    def _count_pause(self, reason: str, seconds: float) -> None:
        tally = dict(self._document.get("pause_seconds_by_reason") or {})
        tally[reason] = float(tally.get(reason, 0.0)) + float(seconds)
        self._document["pause_seconds_by_reason"] = tally

    def _add_pause(self, start: datetime, end: datetime, reason: str, seconds: float) -> None:
        self._document["paused_seconds"] = (
            float(self._document.get("paused_seconds", 0.0)) + float(seconds)
        )
        self._count_pause(reason, seconds)
        pauses = list(self._document.get("pauses") or [])
        pauses.append(
            {
                "reason": str(reason),
                "from": _iso(start),
                "to": _iso(end),
                "seconds": round(float(seconds), 1),
            }
        )
        self._document["pauses"] = pauses[-MAX_PAUSES:]
        if self._policy.max_pause_fraction <= 0:  # pragma: no cover - defensive
            return

    def _close_open_interval(self, now: datetime, reason: str) -> None:
        last = _parse(self._document.get("last_tick_at")) or _parse(
            self._document.get("started_at")
        )
        if last is None:
            return
        gap = max(0.0, (now - last).total_seconds())
        if gap > 0:
            self._add_pause(last, now, reason, gap)

    # -- the report --------------------------------------------------------

    @property
    def policy(self) -> QualificationPolicy:
        with self._lock:
            self._load()
            return self._policy

    def state(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            document = dict(self._document)
            document["pauses"] = [dict(item) for item in document.get("pauses") or []]
            document["events"] = [dict(item) for item in document.get("events") or []]
            document["failures"] = [dict(item) for item in document.get("failures") or []]
            return document

    def _span(self, now: datetime) -> float:
        started = _parse(self._document.get("started_at"))
        if started is None:
            return 0.0
        finished = _parse(self._document.get("finished_at"))
        if finished is not None:
            return max(0.0, (finished - started).total_seconds())
        last = _parse(self._document.get("last_tick_at")) or started
        return max(0.0, (last - started).total_seconds())

    def criteria(self) -> tuple[Criterion, ...]:
        with self._lock:
            self._load()
            now = self._clock()
            policy = self._policy
            span = self._span(now)
            active = float(self._document.get("active_seconds", 0.0))
            paused = float(self._document.get("paused_seconds", 0.0))
            ticks = int(self._document.get("ticks", 0))
            stale = int(self._document.get("stale_ticks", 0))
            decisions = int(self._document.get("decisions", 0))
            failures = len(self._document.get("failures") or [])
            restarts = int(self._document.get("restarts", 0))
            pause_fraction = (paused / span) if span > 0 else 0.0
            stale_fraction = (stale / ticks) if ticks > 0 else 0.0
            started = bool(self._document.get("started_at"))
            return (
                Criterion(
                    key="active_time",
                    label="Counted (healthy) time",
                    required=f"at least {_hours(policy.required_active_seconds)}",
                    actual=_hours(active) if started else "not started",
                    met=started and active >= policy.required_active_seconds,
                    note="Only connected, fresh-quote, running time counts.",
                ),
                Criterion(
                    key="wall_span",
                    label="Wall-clock span",
                    required=f"at least {_hours(policy.required_span_seconds)}",
                    actual=_hours(span) if started else "not started",
                    met=started and span >= policy.required_span_seconds,
                    note="A jumped clock cannot compress the day.",
                ),
                Criterion(
                    key="pause_share",
                    label="Share of the span spent paused",
                    required=f"at most {_pct(policy.max_pause_fraction)}",
                    actual=_pct(pause_fraction),
                    met=pause_fraction <= policy.max_pause_fraction,
                    note="Downtime may not dominate the test.",
                ),
                Criterion(
                    key="stale_share",
                    label="Share of heartbeats with a stale quote",
                    required=f"at most {_pct(policy.max_stale_fraction)}",
                    actual=_pct(stale_fraction),
                    met=stale_fraction <= policy.max_stale_fraction,
                    note="Stale quotes pause the clock; too many mean the feed was not there.",
                ),
                Criterion(
                    key="decisions",
                    label="Decisions evaluated",
                    required=f"at least {policy.min_decisions}",
                    actual=str(decisions),
                    met=decisions >= policy.min_decisions,
                    note="A day with no decision at all is not a test of the strategy.",
                ),
                Criterion(
                    key="failures",
                    label="Recorded failures",
                    required=f"at most {policy.max_failures}",
                    actual=str(failures),
                    met=failures <= policy.max_failures,
                    note="A cycle error or an unwritable ledger disqualifies the run.",
                ),
                Criterion(
                    key="restarts",
                    label="Runner restarts",
                    required=f"at most {policy.max_restarts}",
                    actual=str(restarts),
                    met=restarts <= policy.max_restarts,
                    note="Restart gaps are never counted as active time.",
                ),
            )

    def verdict(self) -> str:
        """The operator-facing word: are we there, not yet, or never from here?"""

        with self._lock:
            self._load()
            status = self._document.get("status")
            if status == NOT_STARTED:
                return NOT_STARTED
            if status == FAILED:
                return FAILED
            criteria = self.criteria()
            disqualifying = [item for item in criteria if item.key in {"failures", "restarts"}]
            if any(not item.met for item in disqualifying):
                return FAILED
            if status == STOPPED:
                return PASSED if all(item.met for item in criteria) else STOPPED
            if status == PASSED:
                return PASSED
            if all(item.met for item in criteria):
                # Every criterion held: the test stands as passed, even before an
                # explicit stop. The record keeps running so more time accrues.
                return PASSED
            return RUNNING

    def live_available(self) -> bool:
        """The one question the live page asks.  Passing authorises nothing else."""

        return self.verdict() == PASSED

    def report(self) -> dict[str, Any]:
        """The end-of-test report: what happened, what held, and what it cannot say."""

        with self._lock:
            self._load()
            now = self._clock()
            policy = self._policy
            span = self._span(now)
            criteria = self.criteria()
            verdict = self.verdict()
            document = self._document
            pauses = [dict(item) for item in document.get("pauses") or []]
            active = float(document.get("active_seconds", 0.0))
            paused = float(document.get("paused_seconds", 0.0))
            return {
                "verdict": verdict,
                "live_available": verdict == PASSED,
                "status": document.get("status"),
                "started_at": document.get("started_at"),
                "finished_at": document.get("finished_at"),
                "last_heartbeat_at": document.get("last_tick_at"),
                "active_seconds": round(active, 1),
                "paused_seconds": round(paused, 1),
                "span_seconds": round(span, 1),
                "active_hours": round(active / 3600.0, 2),
                "required_hours": round(policy.required_active_seconds / 3600.0, 2),
                "remaining_seconds": round(
                    max(0.0, policy.required_active_seconds - active), 1
                ),
                "pause_seconds_by_reason": {
                    str(key): round(float(value), 1)
                    for key, value in (document.get("pause_seconds_by_reason") or {}).items()
                },
                "pauses": pauses[-40:],
                "pause_count": len(pauses),
                "ticks": int(document.get("ticks", 0)),
                "healthy_ticks": int(document.get("healthy_ticks", 0)),
                "stale_ticks": int(document.get("stale_ticks", 0)),
                "decisions": int(document.get("decisions", 0)),
                "unfilled_signals": int(document.get("unfilled_signals", 0)),
                "restarts": int(document.get("restarts", 0)),
                "failures": [dict(item) for item in document.get("failures") or []],
                "symbols": list(document.get("symbols") or []),
                "candidates": list(document.get("candidates") or []),
                "criteria": [item.audit_view() for item in criteria],
                "criteria_met": sum(1 for item in criteria if item.met),
                "criteria_total": len(criteria),
                "policy": policy.as_dict(),
                "events": [dict(item) for item in (document.get("events") or [])][-20:],
                "claim": str(document.get("claim") or ""),
                "summary": self.summary_line(),
            }

    def summary_line(self) -> str:
        with self._lock:
            active = float(self._document.get("active_seconds", 0.0))
            status = self._document.get("status")
            verdict = self.verdict()
            if status == NOT_STARTED:
                return "No qualification test has been started."
            return (
                f"{verdict}: {active / 3600.0:.2f} h of healthy time counted, "
                f"{float(self._document.get('paused_seconds', 0.0)) / 3600.0:.2f} h paused, "
                f"{len(self._document.get('pauses') or [])} pause(s), "
                f"{len(self._document.get('failures') or [])} failure(s)."
            )

    def blockers(self) -> tuple[dict[str, str], ...]:
        """Why LIVE may not be made available yet, in the page's words."""

        with self._lock:
            verdict = self.verdict()
            if verdict == PASSED:
                return ()
            if verdict == NOT_STARTED:
                return (
                    {
                        "reason": "qualification_not_started",
                        "detail": "No forward paper test has been run. Start the paper "
                        "test and let it count at least 24 hours of healthy time.",
                    },
                )
            if verdict == FAILED:
                failed = [item for item in self.criteria() if not item.met]
                return tuple(
                    {
                        "reason": "qualification_failed",
                        "detail": f"{item.label}: required {item.required}, saw {item.actual}.",
                    }
                    for item in failed
                ) or (
                    {
                        "reason": "qualification_failed",
                        "detail": "The forward test recorded a disqualifying failure.",
                    },
                )
            remaining = max(
                0.0,
                self._policy.required_active_seconds
                - float(self._document.get("active_seconds", 0.0)),
            )
            return (
                {
                    "reason": "qualification_in_progress",
                    "detail": (
                        f"{remaining / 3600.0:.2f} h of healthy time still to count before "
                        "the paper test can be considered complete."
                    ),
                },
            )


__all__ = [
    "Criterion",
    "FAILED",
    "FAILURE_CYCLE",
    "FAILURE_DUPLICATE_START",
    "FAILURE_LEDGER",
    "FAILURE_WATCH_LOST",
    "NOT_STARTED",
    "PASSED",
    "PAUSE_CYCLE_ERROR",
    "PAUSE_DISCONNECTED",
    "PAUSE_MARKET_CLOSED",
    "PAUSE_OPERATOR",
    "PAUSE_RUNNER_RESTARTED",
    "PAUSE_RUNNER_STOPPED",
    "PAUSE_STALE",
    "PAUSE_UNKNOWN",
    "PAUSE_UNRESPONSIVE",
    "QUALIFICATION_FILENAME",
    "QualificationClock",
    "QualificationPolicy",
    "RUNNING",
    "SCHEMA_VERSION",
    "STOPPED",
]
