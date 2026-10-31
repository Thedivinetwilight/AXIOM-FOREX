"""Paper vs live execution mode, as one explicit, audited operator decision.

Two independent ideas are kept apart on purpose:

``execution mode`` (:data:`PAPER` / :data:`LIVE`)
    *how* an approved order would be executed.  Paper simulates the fill
    locally; live routes the order through the MetaTrader 5 execution path.

``account mode`` (DEMO / REAL)
    *what* the selected account is.  It is read from the terminal, never set
    here, and AXIOM never switches it.

The rules this module enforces:

* **Both modes are always offered.**  Nothing about the account, the terminal,
  the settings, a strategy, or a test done earlier hides or disables either
  choice: the operator may switch at any time.  Checks belong to the order path,
  which refuses with a reason; they never take the switch away.
* **Paper is immediate.**  Moving to paper needs no confirmation and can never be
  refused: the safe direction is always open.  It ends live authorisation at
  once and closes no position.
* **Live needs exactly one explicit confirmation, and names the account.**  The
  operator types :data:`LIVE_CONFIRMATION_PHRASE` and confirms the signature of
  the account connected *right now*, so "LIVE" means "LIVE against this
  account".  A different account in the terminal later refuses the order; it
  never quietly adopts the new one.
* **Live never survives a restart unconfirmed.**  The chosen mode is persisted,
  but the in-process confirmation is *not*: a new process that finds ``LIVE`` on
  disk restores it as "requested, not confirmed", so nothing can start trading
  because of a file written earlier.
* **Every change is recorded.**  Each transition and each refusal appends an
  audit event to a local JSON document next to the settings file, and to any
  sink the caller supplies.  No credential, token, password, account number, or
  balance is ever written: the payload is only mode names, reason codes, the
  sequence number, and a timestamp.
* **If the change cannot be recorded, it does not happen.**  A failed write of
  the state document refuses the transition to live rather than proceeding
  unaudited.

This module holds no broker, terminal, or order object of any kind.  It cannot
place, arm, cancel, or modify anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.audit.log import sanitize_audit_value

PAPER = "PAPER"
LIVE = "LIVE"
MODES: tuple[str, ...] = (PAPER, LIVE)

PAPER_LABEL = "Paper trading — live market data"
LIVE_LABEL = "Live trading — MetaTrader 5 execution"
PAPER_SUMMARY = "Reads live prices from the selected terminal and simulates every fill locally."
LIVE_SUMMARY = "Routes an approved order through the MetaTrader 5 execution path."

#: The operator must type this, exactly, to enable live execution.
LIVE_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"

STATE_SCHEMA = 1
STATE_FILENAME = "execution_mode.local.json"
AUDIT_FILENAME = "execution_mode_audit.local.json"

#: How many audit events the local document keeps.  Older events are dropped;
#: the durable audit log (if one is configured) keeps them all.
MAX_AUDIT_EVENTS = 250

#: Reason codes.  They are the vocabulary of the audit trail and of the UI.
REASON_STARTUP_DEFAULT = "startup_default"
REASON_RESTORED = "restored_from_disk"
REASON_OPERATOR_PAPER = "operator_switched_to_paper"
REASON_OPERATOR_LIVE = "operator_enabled_live"
REASON_BLOCKED = "live_preflight_blocked"
REASON_CONFIRMATION_MISMATCH = "live_confirmation_mismatch"
REASON_ACCOUNT_UNCONFIRMED = "live_account_not_confirmed"
REASON_ALREADY_LIVE = "already_live"
REASON_STATE_UNREADABLE = "mode_state_unreadable"
REASON_STATE_UNWRITABLE = "mode_state_not_recorded"
REASON_SESSION_ENDED = "session_ended"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat()


def _read_timestamp(value: Any) -> datetime | None:
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


def mode_label(mode: str) -> str:
    return LIVE_LABEL if str(mode).upper() == LIVE else PAPER_LABEL


def mode_short(mode: str) -> str:
    return LIVE if str(mode).upper() == LIVE else PAPER


def _normalise_mode(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if text in MODES else None


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeState:
    """What the state document said, plus how much of it can be trusted."""

    mode: str = PAPER
    sequence: int = 0
    changed_at: datetime | None = None
    readable: bool = True
    note: str = ""


def _atomic_write(path: Path, text: str) -> None:
    """Write beside the target, then rename over it.

    ``Path.replace`` is atomic on the same filesystem, so a crash mid-write can
    never leave a half-written document that the next start would misread.
    """

    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class ExecutionModeStore:
    """The small local document that remembers the chosen mode.

    It deliberately holds *only* the mode, a sequence number, and a timestamp.
    There is no account, no terminal path, no strategy, and no credential: the
    account is whatever the operator signed in to inside MetaTrader 5.
    """

    path: Path | None = None

    def load(self) -> ModeState:
        if self.path is None:
            return ModeState()
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ModeState()
        except Exception:
            return ModeState(readable=False, note=REASON_STATE_UNREADABLE)
        try:
            document = json.loads(text)
        except Exception:
            return ModeState(readable=False, note=REASON_STATE_UNREADABLE)
        if not isinstance(document, Mapping) or document.get("schema") != STATE_SCHEMA:
            return ModeState(readable=False, note=REASON_STATE_UNREADABLE)
        mode = _normalise_mode(document.get("mode"))
        if mode is None:
            return ModeState(readable=False, note=REASON_STATE_UNREADABLE)
        try:
            sequence = int(document.get("sequence") or 0)
        except (TypeError, ValueError):
            return ModeState(readable=False, note=REASON_STATE_UNREADABLE)
        return ModeState(
            mode=mode,
            sequence=max(sequence, 0),
            changed_at=_read_timestamp(document.get("changed_at")),
        )

    def save(self, state: ModeState) -> bool:
        if self.path is None:
            return True
        payload = {
            "schema": STATE_SCHEMA,
            "mode": state.mode,
            "sequence": state.sequence,
            "changed_at": _timestamp(state.changed_at),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except Exception:
            return False
        return True


@dataclass(frozen=True)
class ModeAuditTrail:
    """Append-only local record of every mode decision.

    Two sinks: the capped JSON document next to the settings file (so the
    operator can read it without a database), and an optional in-process sink
    such as a durable audit log.
    """

    path: Path | None = None
    sink: Any = None
    clock: Callable[[], datetime] | None = None

    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(event)
        payload.setdefault("at", _timestamp((self.clock or _utc_now)()))
        clean = sanitize_audit_value(payload)
        if not isinstance(clean, dict):  # defensive: sanitizer must keep the shape
            clean = {"event_type": "EXECUTION_MODE_EVENT_INVALID"}
        if self.sink is not None:
            try:
                self.sink.record(clean)
            except Exception:
                pass
        if self.path is not None:
            try:
                self._append(clean)
            except Exception:
                pass
        return clean

    def _append(self, event: Mapping[str, Any]) -> None:
        path = self.path
        if path is None:
            return
        events: list[Any] = []
        if self.path is not None and self.path.exists():
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                document = None
            if isinstance(document, Mapping) and isinstance(document.get("events"), list):
                events = [item for item in document["events"] if isinstance(item, Mapping)]
        events.append(dict(event))
        trimmed = events[-MAX_AUDIT_EVENTS:]
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            path,
            json.dumps(
                {"schema": STATE_SCHEMA, "events": trimmed},
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
        )

    def recent(self, limit: int = 8) -> tuple[dict[str, Any], ...]:
        path = self.path
        if path is None:
            return ()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ()
        if not isinstance(document, Mapping):
            return ()
        events = document.get("events")
        if not isinstance(events, list):
            return ()
        return tuple(dict(item) for item in events[-max(limit, 1):] if isinstance(item, Mapping))


# --------------------------------------------------------------------------
# the controller
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeChange:
    """The outcome of one operator decision about the execution mode."""

    from_mode: str
    to_mode: str
    changed: bool
    reason: str
    message: str
    sequence: int
    at: datetime | None = None
    blockers: tuple[Mapping[str, Any], ...] = ()

    @property
    def refused(self) -> bool:
        return not self.changed

    def audit_view(self) -> dict[str, Any]:
        return {
            "from": self.from_mode,
            "to": self.to_mode,
            "changed": self.changed,
            "reason": self.reason,
            "message": self.message,
            "sequence": self.sequence,
            "at": _timestamp(self.at),
            "blockers": [dict(item) for item in self.blockers],
        }


@dataclass
class ExecutionModeController:
    """Holds the current mode; changes it only on an explicit operator call."""

    store: ExecutionModeStore | None = None
    audit: ModeAuditTrail | None = None
    clock: Callable[[], datetime] | None = None
    mode: str = PAPER
    changed_at: datetime | None = None
    sequence: int = 0
    #: True only after *this process* ran a clean preflight and took the typed
    #: confirmation.  Never restored from disk.
    confirmed: bool = False
    confirmed_at: datetime | None = None
    #: The account signature the operator confirmed when LIVE was selected.  Held
    #: in memory only, like ``confirmed``: a new process must confirm again.  An
    #: empty string means LIVE was selected while no account was connected, which
    #: authorises nothing until one is confirmed.
    session_account: str = ""
    session_account_at: datetime | None = None
    restored: bool = False
    restore_note: str = ""
    last_change: ModeChange | None = None

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = ExecutionModeStore()
        state = self.store.load()
        self.restore_note = state.note
        self.sequence = max(state.sequence, 0)
        self.changed_at = state.changed_at
        if state.note:
            self.mode = PAPER
            self.restored = False
            self._record(
                "EXECUTION_MODE_STATE_REFUSED",
                reason=state.note,
                note="the saved mode could not be read; paper is the safe default",
            )
            return
        if state.sequence == 0 and state.changed_at is None and state.mode == PAPER:
            # Nothing was ever saved: this is the built-in default, not a change.
            self.mode = PAPER
            self.restored = False
            return
        # A stored LIVE is remembered but *not* effective in a new process.
        self.mode = state.mode
        self.restored = True
        self.confirmed = False
        if state.mode == LIVE:
            self._record(
                "EXECUTION_MODE_RESTORED",
                reason=REASON_RESTORED,
                mode=LIVE,
                note=(
                    "live was selected earlier, but a new process always starts "
                    "unconfirmed; live execution needs a fresh confirmation naming the "
                    "connected account"
                ),
            )
        else:
            self._record("EXECUTION_MODE_RESTORED", reason=REASON_RESTORED, mode=PAPER)

    # -- read model ------------------------------------------------------

    @property
    def live(self) -> bool:
        return self.mode == LIVE

    @property
    def live_effective(self) -> bool:
        """True only for a confirmed live mode in *this* process."""

        return self.mode == LIVE and self.confirmed

    @property
    def paper(self) -> bool:
        return self.mode == PAPER

    def view(self, *, blockers: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        blocker_list = [dict(item) for item in blockers]
        return {
            "mode": self.mode,
            "short": mode_short(self.mode),
            "label": mode_label(self.mode),
            "summary": LIVE_SUMMARY if self.live else PAPER_SUMMARY,
            "live": self.live,
            "paper": self.paper,
            "live_effective": self.live_effective,
            "confirmation_required": self.live and not self.confirmed,
            "confirmation_phrase": LIVE_CONFIRMATION_PHRASE,
            "confirmed_at": _timestamp(self.confirmed_at),
            "session_account": self.session_account,
            "account_confirmed": bool(self.session_account),
            "account_confirmed_at": _timestamp(self.session_account_at),
            "changed_at": _timestamp(self.changed_at),
            "sequence": self.sequence,
            "restored": self.restored,
            "restore_note": self.restore_note,
            "blockers": blocker_list,
            "blocked": bool(blocker_list),
            "paper_always_available": True,
            "live_label": LIVE_LABEL,
            "paper_label": PAPER_LABEL,
            "recent": [dict(item) for item in (self.audit.recent() if self.audit else ())],
            "trading_effect": (
                "Live: an approved order would be routed to MetaTrader 5 execution."
                if self.live_effective
                else (
                    "Live requested but not confirmed in this process: nothing is routed."
                    if self.live
                    else "Paper: every fill is simulated locally; no order is sent."
                )
            ),
        }

    # -- operator actions ------------------------------------------------

    def set_paper(self, *, reason: str = REASON_OPERATOR_PAPER, detail: str = "") -> ModeChange:
        """Move to paper immediately.  Never refused, never asks anything."""

        now = (self.clock or _utc_now)()
        previous = self.mode
        if previous == PAPER and not self.confirmed:
            self.last_change = ModeChange(
                from_mode=previous,
                to_mode=PAPER,
                changed=False,
                reason=REASON_OPERATOR_PAPER,
                message="Already in paper mode. Nothing was changed.",
                sequence=self.sequence,
                at=now,
            )
            return self.last_change
        self.sequence += 1
        self.mode = PAPER
        self.confirmed = False
        self.confirmed_at = None
        self.session_account = ""
        self.session_account_at = None
        self.changed_at = now
        saved = self._persist()
        change = ModeChange(
            from_mode=previous,
            to_mode=PAPER,
            changed=True,
            reason=reason,
            message=(
                "Paper mode is active: fills are simulated locally and no order is sent."
                if saved
                else "Paper mode is active. The mode file could not be written, so the "
                "choice will not be remembered after a restart."
            ),
            sequence=self.sequence,
            at=now,
        )
        self.last_change = change
        self._record(
            "EXECUTION_MODE_CHANGED",
            reason=reason,
            detail=detail,
            from_mode=previous,
            to_mode=PAPER,
            sequence=self.sequence,
            recorded_on_disk=saved,
            live=False,
        )
        return change

    def enable_live(
        self,
        *,
        confirmation: str | None = None,
        detail: str = "",
        account_fingerprint: str = "",
        account_confirmation: str = "",
    ) -> ModeChange:
        """Select LIVE: one typed phrase and one account confirmation.

        No preflight is consulted here.  AXIOM always offers both modes; what the
        account, the settings, or a strategy rule out is a reason an *order* is
        refused later, with the reason shown, not a reason to take the choice
        away.  What this method will not do is pretend: the phrase must be typed,
        and when an account is connected its signature must be confirmed exactly,
        so LIVE is always LIVE-against-a-named-account.
        """

        now = (self.clock or _utc_now)()
        if self.live_effective:
            change = ModeChange(
                from_mode=LIVE,
                to_mode=LIVE,
                changed=False,
                reason=REASON_ALREADY_LIVE,
                message="Live mode is already enabled in this session.",
                sequence=self.sequence,
                at=now,
            )
            self.last_change = change
            return change
        if str(confirmation or "") != LIVE_CONFIRMATION_PHRASE:
            change = ModeChange(
                from_mode=self.mode,
                to_mode=self.mode,
                changed=False,
                reason=REASON_CONFIRMATION_MISMATCH,
                message=(
                    "Live mode was not enabled: the confirmation phrase did not match. "
                    f"Type {LIVE_CONFIRMATION_PHRASE} exactly. The mode did not change."
                ),
                sequence=self.sequence,
                at=now,
            )
            self.last_change = change
            self._record(
                "EXECUTION_MODE_REFUSED",
                reason=REASON_CONFIRMATION_MISMATCH,
                detail=detail,
                confirmation_recorded=False,
                from_mode=self.mode,
                sequence=self.sequence,
                live=False,
            )
            return change
        fingerprint = str(account_fingerprint or "").strip()
        if fingerprint and str(account_confirmation or "").strip().lower() != fingerprint.lower():
            change = ModeChange(
                from_mode=self.mode,
                to_mode=self.mode,
                changed=False,
                reason=REASON_ACCOUNT_UNCONFIRMED,
                message=(
                    "Live mode was not enabled: the account confirmation did not match the "
                    "account the terminal is on. Type the signature shown on this page "
                    "exactly. The mode did not change."
                ),
                sequence=self.sequence,
                at=now,
            )
            self.last_change = change
            self._record(
                "EXECUTION_MODE_REFUSED",
                reason=REASON_ACCOUNT_UNCONFIRMED,
                detail=detail,
                from_mode=self.mode,
                sequence=self.sequence,
                live=False,
            )
            return change

        previous = self.mode
        self.sequence += 1
        self.mode = LIVE
        self.confirmed = True
        self.confirmed_at = now
        self.session_account = fingerprint
        self.session_account_at = now if fingerprint else None
        self.changed_at = now
        saved = self._persist()
        if not saved:
            # Fail closed: an unaudited, unrecorded live switch is not allowed.
            self.mode = PAPER
            self.confirmed = False
            self.confirmed_at = None
            self.session_account = ""
            self.session_account_at = None
            self.sequence -= 1
            change = ModeChange(
                from_mode=previous,
                to_mode=previous,
                changed=False,
                reason=REASON_STATE_UNWRITABLE,
                message=(
                    "Live mode was not enabled: the mode could not be recorded on disk, "
                    "and an unrecorded mode change is not allowed. The mode did not change."
                ),
                sequence=self.sequence,
                at=now,
            )
            self.last_change = change
            self._record(
                "EXECUTION_MODE_REFUSED",
                reason=REASON_STATE_UNWRITABLE,
                detail=detail,
                from_mode=previous,
                sequence=self.sequence,
                live=False,
            )
            return change

        change = ModeChange(
            from_mode=previous,
            to_mode=LIVE,
            changed=True,
            reason=REASON_OPERATOR_LIVE,
            message=(
                "Live mode is enabled for this session against the account you "
                "confirmed. Orders still require the pre-trade gate, an approved "
                "active strategy, and arming; nothing is sent until a strategy "
                "submits an approved order."
                if fingerprint
                else "Live mode is enabled, but no account is connected, so no order "
                "can be sent. Connect and confirm the account before arming."
            ),
            sequence=self.sequence,
            at=now,
        )
        self.last_change = change
        self._record(
            "EXECUTION_MODE_CHANGED",
            reason=REASON_OPERATOR_LIVE,
            detail=detail,
            from_mode=previous,
            to_mode=LIVE,
            sequence=self.sequence,
            confirmation_recorded=True,
            recorded_on_disk=True,
            live=True,
            account_fingerprint=fingerprint or "",
        )
        return change

    def end_live(self, *, reason: str = REASON_SESSION_ENDED, detail: str = "") -> ModeChange:
        """Return to paper because the session that live depended on ended.

        Only ever called from an explicit operator action (disconnect, quit);
        nothing here runs on a timer or at startup.
        """

        if not self.live and not self.confirmed:
            return ModeChange(
                from_mode=self.mode,
                to_mode=self.mode,
                changed=False,
                reason=reason,
                message="Already in paper mode.",
                sequence=self.sequence,
                at=(self.clock or _utc_now)(),
            )
        return self.set_paper(reason=reason, detail=detail)

    def note_blockers(self, blockers: Iterable[Mapping[str, Any]], *, detail: str = "") -> None:
        """Record what would refuse an order right now, without changing anything.

        These checks never decide a mode: they are recorded here so the operator can
        see, in the same audit as the switches, what the order path would have said.
        """

        reasons = [str(dict(item).get("reason", "")) for item in blockers]
        self._record(
            "EXECUTION_MODE_PREFLIGHT",
            reason=REASON_BLOCKED if reasons else "live_preflight_clear",
            detail=detail,
            blockers=reasons,
            from_mode=self.mode,
            sequence=self.sequence,
            live=self.live_effective,
        )

    # -- internals -------------------------------------------------------

    def _persist(self) -> bool:
        if self.store is None:
            return True
        return self.store.save(
            ModeState(mode=self.mode, sequence=self.sequence, changed_at=self.changed_at)
        )

    def _record(self, event_type: str, **fields: Any) -> None:
        if self.audit is None:
            return
        self.audit.record({"event_type": event_type, **fields})


def mode_paths(settings_path: Path | None) -> tuple[Path | None, Path | None]:
    """Where the state and audit documents live, given the settings file.

    Both are ``*.local.json`` beside the settings document, so they are ignored
    by Git and travel with the operator's own machine -- never with the repo.
    """

    if settings_path is None:
        return None, None
    directory = settings_path.parent
    return directory / STATE_FILENAME, directory / AUDIT_FILENAME


def default_mode_view() -> dict[str, Any]:
    """The view a page shows when no controller was wired in (paper, no files)."""

    return ExecutionModeController().view()


def build_controller(
    *,
    settings_path: Path | None = None,
    sink: Any = None,
    clock: Callable[[], datetime] | None = None,
) -> ExecutionModeController:
    """The controller the launcher uses: local documents plus an audit sink."""

    state_path, audit_path = mode_paths(settings_path)
    return ExecutionModeController(
        store=ExecutionModeStore(path=state_path),
        audit=ModeAuditTrail(path=audit_path, sink=sink, clock=clock),
        clock=clock,
    )


__all__ = [
    "AUDIT_FILENAME",
    "ExecutionModeController",
    "ExecutionModeStore",
    "LIVE",
    "LIVE_CONFIRMATION_PHRASE",
    "LIVE_LABEL",
    "LIVE_SUMMARY",
    "MODES",
    "ModeAuditTrail",
    "ModeChange",
    "ModeState",
    "PAPER",
    "PAPER_LABEL",
    "PAPER_SUMMARY",
    "REASON_ALREADY_LIVE",
    "REASON_BLOCKED",
    "REASON_CONFIRMATION_MISMATCH",
    "REASON_OPERATOR_LIVE",
    "REASON_OPERATOR_PAPER",
    "REASON_RESTORED",
    "REASON_SESSION_ENDED",
    "REASON_STARTUP_DEFAULT",
    "REASON_STATE_UNREADABLE",
    "REASON_STATE_UNWRITABLE",
    "STATE_FILENAME",
    "build_controller",
    "default_mode_view",
    "mode_label",
    "mode_paths",
    "mode_short",
]
