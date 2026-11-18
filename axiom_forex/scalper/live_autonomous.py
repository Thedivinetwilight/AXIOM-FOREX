"""Autonomous AX-SCALPER on one explicitly bound MT5 account.

This is the loop the LIVE lane was missing: fresh broker quotes in, decisions by
one *admitted* strategy, the existing risk guardian, one submission through the
existing guarded leg, protective exits, and reconciliation — with a stop that
means "no new entries" and says exactly what is still open at the broker.

What it is not
--------------
It is not a second execution path.  Every order goes through
:class:`~axiom_forex.scalper.live_venue.BrokerVenue`, which calls
``LiveLeg.execute`` — the same blockers, gate, router and ``MT5TradingAdapter``
a strategy order already uses.  It is not a strategy shortcut: the only strategy
it may run is the one named by an admission verdict it was handed at
construction, and it refuses to start without one.  The venue uses MT5 for either a verified DEMO or REAL account, and PAPER keeps the
desk it already has.

The steps of one decision, in order
-----------------------------------
1. **the account** — :func:`~axiom_forex.scalper.live_account.verify_mt5_account`
   on the adapter's own status, before every decision, not once at start-up;
2. **the quote** — the tick core validates symbol, sequence, age, spread and
   gap; a stale or crossed quote cannot produce a decision;
3. **the strategy** — the admitted strategy (and only it) evaluates on the
   validated window;
4. **the risk** — the guardian decides, with the account's own equity and the
   run's declared limits;
5. **the size** — risk per trade / stop distance, clamped to the broker's own
   volume grid; a size that rounds to something the broker would refuse is a
   refusal, never a rounding;
6. **the order** — ``Trader`` submits once under an idempotency key derived from
   the signal, then verifies the protection the broker actually holds;
7. **the management** — ``PositionBook`` manages stop/target/time exits through
   the same venue;
8. **the record** — every step appends an event the page and the audit log read.

Recovery
--------
A run that cannot prove its previous state does not guess: :meth:`recover`
returns ``RECOVERY_REQUIRED`` with the missing pieces, and while it does, no new
entry may be submitted.  The operator sees the concrete path (reconcile the
broker positions, confirm none belong to this run, then start again).
"""

from __future__ import annotations

import json
import ctypes
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from os import O_RDONLY, close, fdopen, fsync, name as os_name
from os import open as open_file
from os import replace as replace_file
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import LIVE, InstrumentSpec
from .execution import (
    ACCEPTED,
    BLOCKED as FILL_BLOCKED,
    PROTECTION_UNVERIFIED,
    REJECTED,
    UNKNOWN_OUTCOME,
)
from .guardian import Guardian, OpenPositionRisk
from .live_account import AccountVerdict, ExpectedAccount, verify_mt5_account
from .positions import CLOSE_UNCONFIRMED, PositionBook
from .signals import Strategy
from .ticks import Tick, TickCore

# -- run states ------------------------------------------------------------

IDLE = "IDLE"
BLOCKED = "BLOCKED"
ARMED = "ARMED"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
STOPPED = "STOPPED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

# -- refusal reasons -------------------------------------------------------

NO_ADMISSION = "no_admitted_candidate"
ACCOUNT_NOT_REAL = "real_account_required"
ACCOUNT_UNVERIFIED = "account_unverified"
NOT_ARMED = "live_not_armed"
MODE_NOT_LIVE = "mode_not_live"
ALREADY_RUNNING = "already_running"
NOT_RUNNING = "not_running"
QUOTE_STALE = "quote_stale"
QUOTE_REFUSED = "quote_refused"
SIZE_REFUSED = "size_refused"
ENTRY_BLOCKED = "entry_blocked"
UNRESOLVED_ORDER = "unresolved_order_present"
POSITION_LIMIT = "position_limit_reached"
COOLDOWN = "cooldown"
RECOVERY = "recovery_required"
SPEC_UNKNOWN = "instrument_unknown"
NOT_MANAGEABLE = "protection_not_restorable"
DURABLE_STATE_REQUIRED = "durable_run_state_required"
STATE_PERSISTENCE_FAILED = "run_state_persistence_failed"
ADMISSION_REVOKED = "admission_revoked"
LIVE_SESSION_CONFLICT = "another_live_session_running"
RUN_STATE_SCHEMA = "axiom-scalper-live-run-v3"


def _fsync_directory(path: Path) -> None:
    """Flush the rename's parent directory on POSIX and Windows.

    ``os.open(directory, O_RDONLY)`` works on POSIX but Windows rejects directory
    handles through the CRT.  Windows needs a native handle opened with
    ``FILE_FLAG_BACKUP_SEMANTICS``; silently skipping this flush would weaken the
    run-state durability guarantee on the machine that can actually trade.
    """

    if os_name != "nt":
        directory_fd = open_file(path, O_RDONLY)
        try:
            fsync(directory_fd)
        finally:
            close(directory_fd)
        return

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    # GENERIC_WRITE, share read/write/delete, OPEN_EXISTING,
    # FILE_FLAG_BACKUP_SEMANTICS (required for a directory handle).
    handle = create_file(str(path.resolve()), 0x40000000, 0x1 | 0x2 | 0x4,
                         None, 3, 0x02000000, None)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, "Could not open run-state directory for durable flush", str(path))
    try:
        if not flush_file_buffers(handle):
            error = ctypes.get_last_error()
            raise OSError(error, "Could not flush run-state directory", str(path))
    finally:
        close_handle(handle)


class RunStateStoreError(RuntimeError):
    """Durable run state could not be read or committed atomically."""


class JsonRunStateStore:
    """Small atomic, fsynced JSON store for the live manager's recovery snapshot."""

    durable = True

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, state: Mapping[str, Any]) -> None:
        if not isinstance(state, Mapping):
            raise RunStateStoreError("run_state_must_be_mapping")
        try:
            payload = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except Exception as exc:
            raise RunStateStoreError("run_state_not_json_serializable") from exc
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=parent)
        tmp = Path(raw_tmp)
        try:
            with fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                fsync(handle.fileno())
            replace_file(tmp, self.path)
            _fsync_directory(parent)
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RunStateStoreError("run_state_commit_failed") from exc

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RunStateStoreError("run_state_read_failed") from exc
        if not isinstance(value, dict):
            raise RunStateStoreError("run_state_root_invalid")
        return value


#: A run needs all of these before an entry may be considered.
_ENTRY_READY = frozenset({RUNNING})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _d(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class AdmittedStrategy:
    """The one strategy this run may trade, and the evidence that admitted it."""

    strategy: Strategy
    candidate_id: str
    evidence_digest: str
    policy_version: str
    approved_at: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "strategy": self.strategy.name,
            "family": self.strategy.family,
            "fingerprint": self.strategy.fingerprint,
            "evidence_digest": self.evidence_digest,
            "policy_version": self.policy_version,
            "approved_at": self.approved_at,
        }


@dataclass
class RunEvent:
    at: str
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def audit_view(self) -> dict[str, Any]:
        return {"at": self.at, "kind": self.kind, **self.detail}


class AutonomousScalper:
    """The LIVE loop: verified MT5 account, admitted strategy, guarded orders."""

    def __init__(
        self,
        *,
        adapter: Any,
        mode: Any,
        expected_account: ExpectedAccount | None,
        admitted: AdmittedStrategy | None,
        venue: Any,
        trader: Any,
        guardian: Guardian,
        book: PositionBook,
        core: TickCore,
        specs: Mapping[str, InstrumentSpec],
        symbols: Mapping[str, str],
        max_open_positions: int,
        cooldown_ms: int = 0,
        decision_interval_ms: int = 0,
        starting_equity: Decimal = Decimal("0"),
        risk_fraction: Decimal = Decimal("0.002"),
        clock: Callable[[], datetime] | None = None,
        audit_log: Any = None,
        session_running: Callable[[], bool] | None = None,
        approval_active: Callable[[], bool] | None = None,
        state_store: JsonRunStateStore | Any = None,
        expected_account_mode: str = "REAL",
    ) -> None:
        self.adapter = adapter
        self.mode = mode
        self.expected_account = expected_account
        self.expected_account_mode = expected_account_mode
        self.admitted = admitted
        self.venue = venue
        self.trader = trader
        self.guardian = guardian
        self.book = book
        self.core = core
        self.specs = dict(specs)
        #: scalper symbol -> the broker symbol the terminal speaks
        self.symbols = dict(symbols)
        self.max_open_positions = int(max_open_positions)
        self.cooldown_ms = int(cooldown_ms)
        self.decision_interval_ms = int(decision_interval_ms)
        self.starting_equity = Decimal(starting_equity)
        self.risk_fraction = Decimal(risk_fraction)
        self.clock = clock or _utc_now
        self.audit_log = audit_log
        self.session_running = session_running
        self.approval_active = approval_active

        self.state = IDLE
        self.stop_reason = ""
        self.pause_reason = ""
        self.events: list[RunEvent] = []
        self.decisions: list[dict[str, Any]] = []
        self._last_entry_ms: int | None = None
        self._last_decision_ms: dict[str, int] = {}
        self._recovery: dict[str, Any] = {}
        self._equity = Decimal(starting_equity)
        self._entries_blocked: dict[str, int] = {}
        self.state_store = state_store
        self._state_persistence_error = ""
        self._pending_submission: dict[str, Any] | None = None
        close_hook = getattr(self.book, "set_close_intent_hook", None)
        if callable(close_hook):
            close_hook(self._persist_close_intent)
        protection_hook = getattr(self.book, "set_protection_intent_hook", None)
        if callable(protection_hook):
            protection_hook(self._persist_protection_intent)
        protection_result_hook = getattr(self.book, "set_protection_result_hook", None)
        if callable(protection_result_hook):
            protection_result_hook(self._persist_protection_result)

    # -- read model ------------------------------------------------------

    def _account(self) -> AccountVerdict:
        status: Mapping[str, Any] = {}
        try:
            status = dict(self.adapter.status() or {})
        except Exception:
            status = {}
        return verify_mt5_account(status, self.expected_account, expected_mode=self.expected_account_mode)

    def _mode_name(self) -> str:
        return str(getattr(self.mode, "mode", "") or "").upper()

    def _live_effective(self) -> bool:
        return bool(getattr(self.mode, "live_effective", False))

    def readiness(self) -> list[dict[str, str]]:
        """Everything that would stop an entry right now, with its fix."""

        blockers: list[dict[str, str]] = []
        if (
            self.state_store is None
            or getattr(self.state_store, "durable", False) is not True
            or not callable(getattr(self.state_store, "save", None))
            or not callable(getattr(self.state_store, "load", None))
        ):
            blockers.append(
                {
                    "reason": DURABLE_STATE_REQUIRED,
                    "message": "No atomic durable store is configured for exact position-management recovery.",
                    "hint": "Configure a durable run-state path and verify it can be read and fsynced before arming.",
                }
            )
        if self._state_persistence_error:
            blockers.append(
                {
                    "reason": STATE_PERSISTENCE_FAILED,
                    "message": self._state_persistence_error,
                    "hint": "Do not resume; restore durable storage and reconcile the broker against the last verified state.",
                }
            )
        if self.admitted is None:
            blockers.append(
                {
                    "reason": NO_ADMISSION,
                    "message": (
                        "No AX-SCALPER strategy has passed signed-evidence and "
                        "independent raw-replay admission, so nothing may trade."
                    ),
                    "hint": (
                        "Run the admission evaluation on a signed evidence artifact and "
                        "load its policy; until one passes, order submission stays blocked."
                    ),
                }
            )
        if self.approval_active is not None:
            try:
                approval_active = bool(self.approval_active())
            except Exception:
                approval_active = False
            if not approval_active:
                blockers.append(
                    {
                        "reason": ADMISSION_REVOKED,
                        "message": "The signed candidate evidence or its exact LIVE approval changed or is no longer active.",
                        "hint": "Stop this run, re-evaluate the exact signed evidence and arm it again only if it still passes.",
                    }
                )
        if self.session_running is not None:
            try:
                competing_session = bool(self.session_running())
            except Exception:
                competing_session = True
            if competing_session:
                blockers.append(
                    {
                        "reason": LIVE_SESSION_CONFLICT,
                        "message": "The ordinary LIVE strategy session is already running.",
                        "hint": "Stop the other LIVE session before selecting the AX-SCALPER lane.",
                    }
                )
        verdict = self._account()
        if not verdict.ok:
            blockers.append(
                {
                    "reason": ACCOUNT_NOT_REAL if verdict.mode not in {"REAL", "DEMO"} else ACCOUNT_UNVERIFIED,
                    "message": "The account is not the verified run account: "
                    + (verdict.detail or verdict.mode),
                    "hint": "Sign the terminal in to the declared account and connect "
                    "again. AXIOM never switches accounts by itself.",
                }
            )
        if self._mode_name() != LIVE or not self._live_effective():
            blockers.append(
                {
                    "reason": MODE_NOT_LIVE,
                    "message": f"Execution mode is {self._mode_name() or 'UNKNOWN'} and "
                    "must be confirmed LIVE for this lane.",
                    "hint": "Switch to LIVE on the Trading mode screen and complete its "
                    "preflight.",
                }
            )
        if not bool(getattr(self.adapter, "armed", False)):
            blockers.append(
                {
                    "reason": NOT_ARMED,
                    "message": "Live trading is not armed.",
                    "hint": "Arm the reviewed plan on the Trading mode screen; this page "
                    "cannot arm.",
                }
            )
        if self._pending_submission is not None:
            blockers.append(
                {
                    "reason": UNRESOLVED_ORDER,
                    "message": "A submission intent remains durably unresolved for this run.",
                    "hint": "Reconcile the exact request id and broker outcome; do not resend it.",
                }
            )
        if self.venue is not None and getattr(self.venue, "has_unresolved", False):
            blockers.append(
                {
                    "reason": UNRESOLVED_ORDER,
                    "message": "An earlier submission is unresolved at the broker.",
                    "hint": "Reconcile the same request id; nothing is re-sent until it is "
                    "explained.",
                }
            )
        # A status/readiness read must not adopt an ambiguous SL/TP mutation.
        # Only explicit recovery/management can commit that change durably.
        protection = self.book.verify_protection(update=False)
        if protection.get("missing"):
            blockers.append(
                {
                    "reason": PROTECTION_UNVERIFIED,
                    "message": "One or more managed positions lack a matching broker-confirmed stop.",
                    "hint": "Verify the exact broker-side stop for each listed ticket. No new entry is permitted while protection is unconfirmed.",
                }
            )
        if protection.get("pending_modification"):
            blockers.append({
                "reason": "protection_reconciliation_required",
                "message": "A protection request remains pending durable reconciliation.",
                "hint": "Reconcile the exact request and broker read-back before any new entry.",
            })
        if self.book.close_blockers or self.book.pending_closes:
            blockers.append(
                {
                    "reason": CLOSE_UNCONFIRMED,
                    "message": "A managed position close has not been confirmed and reconciled.",
                    "hint": "Reconcile the exact close request against the broker and ledger before any new entry.",
                }
            )
        if self._recovery:
            blockers.append(
                {
                    "reason": RECOVERY,
                    "message": "This run's previous state could not be restored exactly: "
                    + str(self._recovery.get("detail") or ""),
                    "hint": str(self._recovery.get("hint") or ""),
                }
            )
        return blockers

    def status(self) -> dict[str, Any]:
        verdict = self._account()
        blockers = self.readiness()
        positions = [item.audit_view() for item in self.book.open_positions]
        broker_positions = self._broker_positions()
        return {
            "state": self.state,
            "mode": self._mode_name(),
            "live_effective": self._live_effective(),
            "armed": bool(getattr(self.adapter, "armed", False)),
            "account": {
                **verdict.audit_view(),
                "expected": None if self.expected_account is None
                else self.expected_account.audit_view(),
            },
            "admitted": None if self.admitted is None else self.admitted.audit_view(),
            "symbols": dict(self.symbols),
            "equity": str(self._equity),
            "starting_equity": str(self.starting_equity),
            "open_positions": positions,
            "broker_positions": broker_positions,
            "unresolved": sorted(getattr(self.venue, "_unresolved", ()) or ()),
            "decisions": [dict(item) for item in self.decisions[-12:]],
            "events": [item.audit_view() for item in self.events[-25:]],
            "blockers": blockers,
            "close_blockers": list(self.book.close_blockers),
            "entries_blocked": dict(self._entries_blocked),
            "stop_reason": self.stop_reason,
            "pause_reason": self.pause_reason,
            "recovery": dict(self._recovery),
            "state_persistence_error": self._state_persistence_error,
            "pending_submission": None if self._pending_submission is None else dict(self._pending_submission),
            "pending_closes": list(self.book.pending_closes),
            "pending_protection_modifications": self.book.snapshot()["pending_protection_modifications"],
            "venue": self.venue.stats if hasattr(self.venue, "stats") else {},
            "order_sending": self.state == RUNNING,
            "paper_only": False,
        }

    def _broker_positions(self) -> list[dict[str, Any]]:
        reader = getattr(self.venue, "position_ids", None)
        if not callable(reader):
            return []
        try:
            return [{"ticket": ticket} for ticket in reader()]
        except Exception:
            return []

    # -- lifecycle -------------------------------------------------------

    def _persist_state(self) -> bool:
        store = self.state_store
        if (
            store is None
            or getattr(store, "durable", False) is not True
            or not callable(getattr(store, "save", None))
            or not callable(getattr(store, "load", None))
        ):
            self._state_persistence_error = "durable run-state storage is not configured"
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": self._state_persistence_error,
                "hint": "Configure a durable store, then reconcile every broker position before resuming.",
            }
            return False
        try:
            state = self.snapshot()
            store.save(state)
            observed = store.load()
            if observed != state:
                raise RunStateStoreError("run_state_read_after_write_mismatch")
        except Exception as exc:
            self._state_persistence_error = f"durable run-state commit failed: {type(exc).__name__}"
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": self._state_persistence_error,
                "hint": "Do not resume; restore durable storage and reconcile the broker against the last verified state.",
            }
            self._emit("state_persistence_failed", error=type(exc).__name__)
            return False
        self._state_persistence_error = ""
        return True

    def _persist_close_intent(self, intent: Mapping[str, Any]) -> bool:
        """Fsync an exact prepared close before the venue can be called."""

        self._emit(
            "close_intent_prepared",
            position_id=str(intent.get("position_id") or ""),
            request_id=str(intent.get("request_id") or ""),
            requested_lots=str(intent.get("requested_lots") or ""),
        )
        return self._persist_state()

    def _persist_protection_intent(self, intent: Mapping[str, Any]) -> bool:
        """Fsync the exact requested stop and target before the broker can change."""

        position_id = str(intent.get("position_id") or "")
        self.guardian.record_protection(position_id, protected=False)
        self._emit(
            "protection_modification_prepared",
            position_id=str(intent.get("position_id") or ""),
            request_id=str(intent.get("request_id") or ""),
            stop_price=str(intent.get("stop_price") or ""),
            target_price=intent.get("target_price"),
        )
        return self._persist_state()

    def _persist_protection_result(self, result: Mapping[str, Any]) -> bool:
        """Commit a broker-read-back-confirmed SL/TP pair after the send."""

        position_id = str(result.get("position_id") or "")
        position = self.book.get(position_id)
        if position is None or not self._record_position_risk(position):
            self.guardian.record_protection(position_id, protected=False)
            return False
        self.guardian.record_protection(
            position_id, protected=bool(position.protection_verified)
        )
        self._emit(
            "protection_modification_confirmed",
            position_id=str(result.get("position_id") or ""),
            request_id=str(result.get("request_id") or ""),
            stop_price=str(result.get("stop_price") or ""),
            target_price=result.get("target_price"),
            broker_stop_price=result.get("broker_stop_price"),
            broker_target_price=result.get("broker_target_price"),
        )
        persisted = self._persist_state()
        if not persisted:
            self.guardian.record_protection(position_id, protected=False)
        return persisted

    def _emit(self, kind: str, **detail: Any) -> None:
        event = RunEvent(at=self.clock().astimezone(timezone.utc).isoformat(), kind=kind, detail=detail)
        self.events.append(event)
        if self.audit_log is not None:
            try:
                self.audit_log.record({"event_type": f"AX_SCALPER_LIVE_{kind.upper()}", **event.audit_view()})
            except Exception:
                pass

    def arm(self) -> dict[str, Any]:
        """Move to ARMED when every precondition holds.  Sends nothing."""

        if self.state == RECOVERY_REQUIRED:
            return {"ok": False, "state": self.state, "reason": RECOVERY,
                    "recovery": dict(self._recovery)}
        blockers = self.readiness()
        if blockers:
            self.state = BLOCKED
            self._emit("arm_refused", reasons=[item["reason"] for item in blockers])
            return {"ok": False, "state": self.state, "blockers": blockers}
        self.state = ARMED
        self._emit("armed", strategy=(self.admitted.candidate_id if self.admitted else ""))
        if not self._persist_state():
            return {
                "ok": False,
                "state": self.state,
                "blockers": [{"reason": STATE_PERSISTENCE_FAILED, "message": self._state_persistence_error}],
            }
        return {"ok": True, "state": self.state, "blockers": []}

    def start(self) -> dict[str, Any]:
        """Begin autonomous entries.  Refuses unless armed and ready."""

        if self.state == RECOVERY_REQUIRED:
            return {"ok": False, "state": self.state, "reason": RECOVERY,
                    "recovery": dict(self._recovery)}
        if self.state in {RUNNING}:
            return {"ok": False, "state": self.state, "reason": ALREADY_RUNNING}
        if self.state in {BLOCKED, IDLE, STOPPED}:
            armed = self.arm()
            if not armed.get("ok"):
                return armed
        blockers = self.readiness()
        if blockers:
            self.state = BLOCKED
            return {"ok": False, "state": self.state, "blockers": blockers}
        self.state = RUNNING
        self.stop_reason = ""
        self.pause_reason = ""
        self._emit("started")
        if not self._persist_state():
            return {"ok": False, "state": self.state, "reason": STATE_PERSISTENCE_FAILED}
        return {"ok": True, "state": self.state}

    def pause(self, *, reason: str = "operator_pause") -> dict[str, Any]:
        if self.state != RUNNING:
            return {"ok": False, "state": self.state, "reason": NOT_RUNNING}
        self.state = PAUSED
        self.pause_reason = reason
        self._emit("paused", reason=reason)
        if not self._persist_state():
            return {"ok": False, "state": self.state, "reason": STATE_PERSISTENCE_FAILED}
        return {"ok": True, "state": self.state}

    def resume(self) -> dict[str, Any]:
        if self.state != PAUSED:
            return {"ok": False, "state": self.state, "reason": NOT_RUNNING}
        blockers = self.readiness()
        if blockers:
            self.state = BLOCKED
            return {"ok": False, "state": self.state, "blockers": blockers}
        self.state = RUNNING
        self.pause_reason = ""
        self._emit("resumed")
        if not self._persist_state():
            return {"ok": False, "state": self.state, "reason": STATE_PERSISTENCE_FAILED}
        return {"ok": True, "state": self.state}

    def stop(
        self,
        *,
        reason: str = "operator_stop",
        close_positions: bool = False,
        ticks: Mapping[str, Tick] | None = None,
    ) -> dict[str, Any]:
        """Stop new entries; only explicitly close positions against fresh ticks."""

        # A recovery-required snapshot must remain byte-for-byte intact. A
        # stop is still effective in memory (RECOVERY_REQUIRED cannot enter),
        # but cannot rewrite the last durable evidence or flatten the broker.
        if self.state == RECOVERY_REQUIRED:
            self.stop_reason = reason
            return {
                "ok": True, "state": RECOVERY_REQUIRED, "reason": reason,
                "open_positions": [item.audit_view() for item in self.book.open_positions],
                "broker_positions": self._broker_positions(), "closed": [],
                "state_persisted": None, "state_preserved": True,
                "message": "New entries are blocked. Unresolved recovery state was preserved; no position was closed.",
            }
        previous = self.state
        self.state = STOPPED
        self.stop_reason = reason
        state_saved_before_close = self._persist_state()
        close_results: list[dict[str, Any]] = []
        if close_positions:
            close_results = self.flatten(reason=reason, ticks=ticks)
        open_positions = [item.audit_view() for item in self.book.open_positions]
        broker = self._broker_positions()
        self._emit(
            "stopped",
            reason=reason,
            previous=previous,
            open_positions=[item.get("position_id") for item in open_positions],
            broker_positions=broker,
            flatten_requested=bool(close_positions),
            close_results=close_results,
        )
        state_saved_after_close = self._persist_state()
        remaining = len(open_positions) or len(broker)
        if remaining:
            message = (
                f"No new entry will be submitted. {len(open_positions)} managed and "
                f"{len(broker)} broker position(s) still report open. A stop request "
                "does not close them unless an exact fresh quote and broker-confirmed "
                "close outcome are available."
            )
        else:
            message = "No new entry will be submitted. No positions are currently reported open."
        return {
            "ok": True,
            "state": self.state,
            "reason": reason,
            "open_positions": open_positions,
            "broker_positions": broker,
            "closed": close_results,
            "state_persisted": bool(state_saved_before_close and state_saved_after_close),
            "message": message,
        }

    def flatten(
        self,
        *,
        reason: str = "operator_flatten",
        ticks: Mapping[str, Tick] | None = None,
    ) -> list[dict[str, Any]]:
        """Explicitly request exits at caller-supplied current quotes; never invent a price."""

        if self.state == RECOVERY_REQUIRED:
            return []
        now = self.clock()
        now_ms = int(now.timestamp() * 1000)
        actions = self.book.flatten_all(ticks=ticks or {}, now_ms=now_ms, reason=reason)
        closed = [action.audit_view() for action in actions]
        self._emit("flattened", closed=closed, reason=reason)
        self._persist_state()
        return closed

    def reconcile_close(self, *, position_id: str) -> dict[str, Any]:
        """Resolve one latched close from its exact durable request, never by guessing."""

        pending = next(
            (item for item in self.book.pending_closes if item.get("position_id") == position_id),
            None,
        )
        if pending is None:
            return {"ok": False, "reason": "no_pending_close", "position_id": position_id}
        request_id = str(pending.get("request_id") or "")
        query = getattr(self.venue, "query", None)
        if not request_id or not callable(query):
            return {
                "ok": False,
                "resolved": False,
                "reason": "durable_close_query_unavailable",
                "position_id": position_id,
                "request_id": request_id,
            }
        try:
            fill = query(request_id)
        except Exception as exc:
            return {
                "ok": False,
                "resolved": False,
                "reason": "durable_close_query_failed",
                "detail": type(exc).__name__,
                "position_id": position_id,
                "request_id": request_id,
            }
        if fill is None:
            return {
                "ok": False,
                "resolved": False,
                "reason": "close_outcome_not_found",
                "position_id": position_id,
                "request_id": request_id,
            }
        now_ms = int(self.clock().timestamp() * 1000)
        closed_before = len(self.book.closed)
        action = self.book.resolve_pending_close(
            position_id=position_id, fill=fill, now_ms=now_ms
        )
        if (any(item.get("position_id") == position_id for item in self.book.pending_closes)
                and len(self.book.closed) == closed_before):
            # No exact conclusion: keep the original durable bytes unchanged.
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": "the exact close outcome remains unresolved or mismatched",
                "hint": "Preserve the original request and broker records; do not resend.",
            }
            return {"ok": False, "resolved": False, "state": self.state,
                    "reason": "close_outcome_unresolved", "position_id": position_id,
                    "request_id": request_id,
                    "action": None if action is None else action.audit_view()}
        for trade in self.book.closed[closed_before:]:
            if trade.gross_account is None:
                self.guardian.suspend_entries("position_realized_pnl_unavailable")
            elif self.book.get(trade.position_id) is not None:
                self.guardian.record_partial_realised(
                    realised=trade.gross_account, at_ms=now_ms
                )
            else:
                rows = [
                    item for item in self.book.closed
                    if item.position_id == trade.position_id
                ]
                total_realised = (
                    None
                    if any(item.gross_account is None for item in rows)
                    else sum((item.gross_account for item in rows), Decimal(0))
                )
                self.guardian.record_close(
                    trade.position_id,
                    realised=trade.gross_account,
                    total_realised=total_realised,
                    at_ms=now_ms,
                )
        for position in self.book.open_positions:
            self._record_position_risk(position)

        was_pending_close_recovery = (
            self._recovery.get("detail")
            == "one or more close requests were unresolved at shutdown"
        )
        if not self.book.pending_closes and not self.book.close_blockers:
            broker_problems = self._broker_position_problems(
                [position.audit_view() for position in self.book.open_positions]
            )
            protection = self.book.verify_protection()
            for position in self.book.open_positions:
                self.guardian.record_protection(
                    position.position_id, protected=position.protection_verified
                )
            unprotected = protection.get("missing", [])
            if broker_problems:
                self._recovery = {
                    "detail": "; ".join(broker_problems),
                    "hint": "Do not resume; reconcile the exact broker tickets, volume, entry, and protective stop against the managed book.",
                }
                self.state = RECOVERY_REQUIRED
            elif unprotected:
                self._recovery = {
                    "detail": "one or more reconciled positions do not have broker-confirmed stops",
                    "hint": "Do not resume entries; restore approved protection and verify it by broker read-back.",
                }
                self.state = RECOVERY_REQUIRED
            elif was_pending_close_recovery:
                self._recovery = {}
                self.state = ARMED
                self.pause_reason = ""
        elif was_pending_close_recovery:
            self._recovery = {
                "detail": "the close outcome is not yet reconciled or a close blocker remains",
                "hint": "Keep entries disabled; resolve the exact durable request and any close blocker before resuming.",
            }
            self.state = RECOVERY_REQUIRED
        self._emit(
            "close_reconciled",
            position_id=position_id,
            request_id=request_id,
            status=getattr(fill, "status", ""),
            action=None if action is None else action.audit_view(),
        )
        state_saved = self._persist_state()
        success = (
            action is not None
            and action.kind in {"close", "partial_exit"}
            and state_saved
            and not self._recovery
            and not self.book.pending_closes
            and not self.book.close_blockers
        )
        return {
            "ok": success,
            "resolved": getattr(fill, "status", "") != UNKNOWN_OUTCOME,
            "position_id": position_id,
            "request_id": request_id,
            "action": None if action is None else action.audit_view(),
            "pending_closes": list(self.book.pending_closes),
            "close_blockers": list(self.book.close_blockers),
        }

    def reconcile_submission(self) -> dict[str, Any]:
        """Resolve a persisted entry intent using only its exact durable request id."""

        pending = self._pending_submission
        if not isinstance(pending, Mapping):
            return {"ok": False, "reason": "no_pending_submission"}
        request_id = str(pending.get("request_id") or "")
        query = getattr(self.venue, "query", None)
        if not request_id or not callable(query):
            return {"ok": False, "reason": "durable_submission_query_unavailable"}
        try:
            fill = query(request_id)
        except Exception as exc:
            fill = None
            query_error = type(exc).__name__
        else:
            query_error = ""
        if fill is None or str(getattr(fill, "status", "")) == UNKNOWN_OUTCOME:
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": "the persisted entry request remains unresolved",
                "hint": "Query the same durable request id again after the venue recovers; do not resubmit or adopt a position by ticket coincidence.",
            }
            if query_error:
                self._recovery["query_error"] = query_error
            return {
                "ok": False,
                "resolved": False,
                "reason": "submission_outcome_unresolved",
                "request_id": request_id,
                "query_error": query_error,
            }
        if str(getattr(fill, "request_id", "")) != request_id:
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": "the durable outcome does not match the persisted entry request id",
                "hint": "Keep entries disabled and reconcile the exact request identity with the durable order record.",
            }
            return {"ok": False, "resolved": False, "reason": "submission_request_mismatch"}

        status = str(getattr(fill, "status", ""))
        if status not in {ACCEPTED, REJECTED, FILL_BLOCKED} or bool(getattr(fill, "simulated", True)):
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": "the queried entry outcome is not a durable terminal broker result",
                "hint": "Keep entries disabled until the exact request has a confirmed booked fill or terminal refusal.",
            }
            return {"ok": False, "resolved": False, "reason": "submission_outcome_not_terminal"}

        accepted_ticket = ""
        if status == ACCEPTED:
            symbol = str(pending.get("symbol") or "")
            side = str(pending.get("side") or "").upper()
            spec = self.specs.get(symbol)
            intended_lots = _d(pending.get("lots"))
            requested_lots = _d(getattr(fill, "requested_lots", None))
            filled_lots = _d(getattr(fill, "filled_lots", None))
            price = _d(getattr(fill, "price", None))
            ticket = str(getattr(fill, "ticket", "") or "")
            stop = _d(pending.get("stop_price"))
            target = _d(pending.get("target_price")) if pending.get("target_price") is not None else None
            fingerprint = str(pending.get("signal_fingerprint") or "")
            requested_at_ms = pending.get("requested_at_ms")
            try:
                requested_at = int(requested_at_ms)
            except Exception:
                requested_at = -1
            valid_quantity = (
                spec is not None
                and intended_lots is not None
                and requested_lots == intended_lots
                and filled_lots is not None
                and filled_lots > 0
                and filled_lots <= intended_lots
                and spec.volume_min <= filled_lots <= spec.volume_max
                and filled_lots % spec.volume_step == 0
            )
            stop_valid = (
                spec is not None
                and stop is not None
                and price is not None
                and ((side == "BUY" and stop < price) or (side == "SELL" and stop > price))
            )
            target_valid = (
                target is None
                or (
                    price is not None
                    and ((side == "BUY" and target > price) or (side == "SELL" and target < price))
                )
            )
            if (
                spec is None
                or side not in {"BUY", "SELL"}
                or not ticket
                or price is None
                or price <= 0
                or not valid_quantity
                or not stop_valid
                or not target_valid
                or not fingerprint
                or requested_at < 0
            ):
                self.state = RECOVERY_REQUIRED
                self._recovery = {
                    "detail": "the accepted entry fill cannot be safely reconstructed from its persisted intent",
                    "hint": "Do not resume or resend. Reconcile the exact durable fill, actual ticket, volume, price, and valid protection terms.",
                    "request_id": request_id,
                    "position_id": ticket,
                }
                self._persist_state()
                return {"ok": False, "resolved": False, "reason": "accepted_fill_not_manageable"}

            accepted_ticket = ticket
            duplicate = next(
                (item for item in self.book.open_positions if item.entry_request_id == request_id),
                None,
            )
            existing = self.book.get(ticket)
            if duplicate is not None and duplicate.position_id != ticket:
                self.state = RECOVERY_REQUIRED
                self._recovery = {
                    "detail": "the accepted request is already associated with a different managed ticket",
                    "hint": "Reconcile the exact request-to-ticket mapping before continuing.",
                    "request_id": request_id,
                }
                self._persist_state()
                return {"ok": False, "resolved": False, "reason": "submission_ticket_mismatch"}
            if existing is not None:
                if (
                    existing.entry_request_id != request_id
                    or existing.symbol != symbol
                    or existing.side != side
                    or existing.lots != filled_lots
                    or existing.entry_price != price
                    or existing.stop_price != stop
                    or existing.target_price != target
                ):
                    self.state = RECOVERY_REQUIRED
                    self._recovery = {
                        "detail": "the accepted fill conflicts with an already managed ticket",
                        "hint": "Do not overwrite either record; reconcile the broker outcome and managed state manually.",
                        "request_id": request_id,
                        "position_id": ticket,
                    }
                    self._persist_state()
                    return {"ok": False, "resolved": False, "reason": "submission_ticket_conflict"}
                position = existing
            else:
                try:
                    position = self.book.open_from_fill(
                        fill=fill,
                        symbol=symbol,
                        side=side,
                        stop_price=stop,
                        target_price=target,
                        strategy_fingerprint=fingerprint,
                        now_ms=requested_at,
                    )
                except Exception as exc:
                    self.state = RECOVERY_REQUIRED
                    self._recovery = {
                        "detail": f"accepted entry could not be adopted:{type(exc).__name__}",
                        "hint": "Do not resume. Reconcile the exact broker ticket and protection before any further action.",
                        "request_id": request_id,
                        "position_id": ticket,
                    }
                    self._persist_state()
                    return {"ok": False, "resolved": False, "reason": "accepted_fill_adoption_failed"}
            if not self._record_position_risk(position):
                self._pending_submission = {**dict(pending), "accepted_ticket": ticket}
                self._persist_state()
                return {"ok": False, "resolved": False, "reason": "accepted_fill_risk_state_failed"}
            self._last_entry_ms = max(self._last_entry_ms or 0, requested_at)

        # A durable refusal consumes the intent. A booked fill has now been
        # converted into a managed ticket; either way there is no retry.
        self._pending_submission = None
        self._recovery = {}
        broker_problems = self._broker_position_problems(
            [position.audit_view() for position in self.book.open_positions]
        )
        protection = self.book.verify_protection()
        for position in self.book.open_positions:
            self.guardian.record_protection(
                position.position_id, protected=position.protection_verified
            )
        if broker_problems:
            self._recovery = {
                "detail": "; ".join(broker_problems),
                "hint": "Do not resume; reconcile the exact broker positions, volumes, entries, and stops against the managed book.",
            }
        elif protection.get("missing"):
            self._recovery = {
                "detail": "one or more positions lack matching broker-confirmed stops after submission reconciliation",
                "hint": "Do not resume entries until every accepted position has an approved, broker-confirmed stop.",
            }
        elif self.book.pending_closes or self.book.close_blockers:
            self._recovery = {
                "detail": "a close remains unresolved while the entry intent is being reconciled",
                "hint": "Resolve all exact pending close outcomes before allowing any new entry.",
            }
        if self._recovery:
            self.state = RECOVERY_REQUIRED
        else:
            self.state = ARMED
            self.pause_reason = ""
        self._emit(
            "submission_reconciled",
            request_id=request_id,
            status=status,
            position_id=accepted_ticket,
        )
        state_saved = self._persist_state()
        return {
            "ok": state_saved and not self._recovery,
            "resolved": True,
            "request_id": request_id,
            "status": status,
            "position_id": accepted_ticket or None,
            "state": self.state,
            "recovery": dict(self._recovery),
        }

    def _broker_position_problems(
        self,
        local_rows: Sequence[Mapping[str, Any]],
        *,
        pending_close_ids: Iterable[str] = (),
        pending_protection: Sequence[Mapping[str, Any]] = (),
    ) -> list[str]:
        """Compare broker positions with a saved or restored managed book."""

        pending = {str(item) for item in pending_close_ids}
        pending_stops: dict[str, Decimal] = {}
        pending_targets: dict[str, Decimal | None] = {}
        for item in pending_protection:
            if isinstance(item, Mapping):
                position_id = str(item.get("position_id") or "")
                stop = _d(item.get("stop_price"))
                if position_id and stop is not None and stop > 0:
                    pending_stops[position_id] = stop
                    if "target_price" in item:
                        target_raw = item.get("target_price")
                        target = None if target_raw is None else _d(target_raw)
                        if target_raw is None or (target is not None and target > 0):
                            pending_targets[position_id] = target
        try:
            reader = getattr(self.venue, "position_details", None)
            if not callable(reader):
                raise RuntimeError("venue_position_reader_unavailable")
            source_rows = reader()
            broker_by_id: dict[str, dict[str, Any]] = {}
            for broker_row in source_rows:
                if not isinstance(broker_row, Mapping):
                    raise RuntimeError("venue_position_row_unreadable")
                ticket = str(broker_row.get("position_id") or broker_row.get("ticket") or "")
                if not ticket or ticket in broker_by_id:
                    raise RuntimeError("venue_position_ticket_empty_or_duplicated")
                broker_by_id[ticket] = dict(broker_row)
        except Exception as exc:
            return [f"broker positions could not be reconciled:{type(exc).__name__}"]

        local_by_id: dict[str, Mapping[str, Any]] = {}
        for row in local_rows:
            if not isinstance(row, Mapping):
                return ["stored open-position rows are invalid"]
            position_id = str(row.get("position_id") or "")
            if not position_id or position_id in local_by_id:
                return ["stored position tickets are empty or duplicated"]
            local_by_id[position_id] = row

        problems: list[str] = []
        query = getattr(self.venue, "query", None)
        for position_id, row in local_by_id.items():
            request_id = str(row.get("entry_request_id") or "")
            spec = self.specs.get(str(row.get("symbol") or ""))
            if not request_id or spec is None or not callable(query):
                problems.append(f"position {position_id} lacks a durable entry query basis")
                continue
            try:
                fill = query(request_id)
            except Exception as exc:
                problems.append(
                    f"position {position_id} durable entry query failed:{type(exc).__name__}"
                )
                continue
            lots = _d(row.get("lots"))
            entry = _d(row.get("entry_price"))
            fill_lots = _d(getattr(fill, "filled_lots", None))
            requested_lots = _d(getattr(fill, "requested_lots", None))
            fill_price = _d(getattr(fill, "price", None))
            if (
                fill is None
                or str(getattr(fill, "request_id", "")) != request_id
                or str(getattr(fill, "status", "")) != ACCEPTED
                or bool(getattr(fill, "simulated", True))
                or str(getattr(fill, "ticket", "")) != position_id
                or lots is None
                or fill_lots != lots
                or requested_lots is None
                or requested_lots < fill_lots
                or fill_price is None
                or entry is None
                or abs(fill_price - entry) > spec.point
            ):
                problems.append(
                    f"position {position_id} lacks a matching durable entry fill"
                )
        broker_ids, local_ids = set(broker_by_id), set(local_by_id)
        for position_id in sorted(broker_ids - local_ids):
            problems.append(f"unmanaged broker position {position_id} is present")
        for position_id in sorted(local_ids - broker_ids - pending):
            problems.append(f"managed broker position {position_id} is missing")

        for position_id in sorted(local_ids & broker_ids):
            row = local_by_id[position_id]
            broker = broker_by_id[position_id]
            symbol = str(row.get("symbol") or "")
            spec = self.specs.get(symbol)
            broker_symbol = str(broker.get("symbol") or "").upper()
            expected_symbol = str(self.symbols.get(symbol) or "").upper()
            side = str(row.get("side") or "").upper()
            broker_side = str(broker.get("side") or "").upper()
            lots = _d(row.get("lots"))
            broker_lots = _d(broker.get("lots"))
            entry = _d(row.get("entry_price"))
            broker_entry = _d(broker.get("entry_price"))
            expected_stop = pending_stops.get(position_id, _d(row.get("stop_price")))
            broker_stop = _d(broker.get("stop_price"))
            expected_target = (
                pending_targets[position_id]
                if position_id in pending_targets
                else (None if row.get("target_price") is None else _d(row.get("target_price")))
            )
            broker_target = _d(broker.get("target_price"))
            if broker_target is not None and broker_target <= 0:
                broker_target = None
            if (
                spec is None
                or not expected_symbol
                or broker_symbol != expected_symbol
                or broker_side != side
                or entry is None
                or broker_entry is None
                or abs(broker_entry - entry) > (spec.point if spec is not None else Decimal(0))
            ):
                problems.append(f"broker position {position_id} identity or entry differs")
                continue
            if position_id in pending:
                # The exact durable close outcome decides whether the position
                # is unchanged, partially filled, or absent. No second close is
                # permitted until that request is resolved.
                continue
            if (
                lots is None
                or broker_lots is None
                or broker_lots != lots
                or expected_stop is None
                or broker_stop is None
                or not self.book._stop_matches(broker_stop, expected_stop, spec.point, side)
                or expected_target is None and broker_target is not None
                or expected_target is not None
                and (
                    broker_target is None
                    or abs(broker_target - expected_target) > spec.point
                )
            ):
                problems.append(f"broker position {position_id} size or protection differs")
                continue
            try:
                observed_stop, observed_target = self.venue.protection(position_id)
            except Exception:
                observed_stop, observed_target = None, None
            target_readback = _d(observed_target)
            if target_readback is not None and target_readback <= 0:
                target_readback = None
            if (
                not self.book._stop_matches(observed_stop, expected_stop, spec.point, side)
                or expected_target is None and target_readback is not None
                or expected_target is not None
                and (
                    target_readback is None
                    or abs(target_readback - expected_target) > spec.point
                )
            ):
                problems.append(f"position {position_id} lacks matching broker protection read-back")
        return problems

    # -- recovery --------------------------------------------------------

    def recover(self, *, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Restore a full run only after state, risk, account, and broker facts match."""

        self._recovery = {}
        if state is None and self.state_store is not None:
            try:
                state = self.state_store.load()
            except Exception as exc:
                self._recovery = {
                    "detail": f"durable run-state could not be read: {type(exc).__name__}",
                    "hint": "Do not start entries. Repair the state store and reconcile all broker positions and order outcomes.",
                }
                self.state = RECOVERY_REQUIRED
                self._emit("recovery_required", reason="state_store_unreadable")
                return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}
        if state is None:
            self._recovery = {
                "detail": "no stored run state was found",
                "hint": (
                    "Reconcile the broker's open positions, confirm none belong to a "
                    "previous AX-SCALPER run, then start a new run. Nothing is adopted."
                ),
            }
            self.state = RECOVERY_REQUIRED
            self._emit("recovery_required", reason="no_state")
            return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}
        if not isinstance(state, Mapping):
            state = {}

        problems: list[str] = []
        if str(state.get("schema") or "") != RUN_STATE_SCHEMA:
            problems.append("stored run-state schema is missing or unsupported")
        if str(state.get("mode") or "") != LIVE:
            problems.append("stored mode is not LIVE")
        if state.get("state") not in {IDLE, BLOCKED, ARMED, RUNNING, PAUSED, STOPPED, RECOVERY_REQUIRED}:
            problems.append("stored run lifecycle state is missing or invalid")
        if self._mode_name() != LIVE or not self._live_effective():
            problems.append("current execution mode is not confirmed LIVE")

        current_account = self._account()
        stored_account = state.get("account_identity")
        expected_account = {
            "server": current_account.server,
            "fingerprint": current_account.fingerprint,
            "currency": current_account.currency,
            "account_mode": current_account.mode,
        }
        if not current_account.ok:
            problems.append("current MT5 account is not verified")
        if not isinstance(stored_account, Mapping) or dict(stored_account) != expected_account:
            problems.append("account identity differs or was not stored")

        stored_symbols = state.get("symbols")
        if not isinstance(stored_symbols, Mapping) or dict(stored_symbols) != self.symbols:
            problems.append("symbol map differs")
        expected_specs = {
            symbol: spec.audit_view() for symbol, spec in sorted(self.specs.items())
        }
        stored_specs = state.get("instrument_specs")
        if not isinstance(stored_specs, Mapping) or dict(stored_specs) != expected_specs:
            problems.append("instrument terms differ")

        expected_admission = None if self.admitted is None else self.admitted.audit_view()
        if state.get("admission") != expected_admission:
            problems.append("admitted strategy or evidence identity differs")
        stored_strategy = str(state.get("strategy_fingerprint") or "")
        current_strategy = "" if self.admitted is None else self.admitted.strategy.fingerprint
        if stored_strategy != current_strategy:
            problems.append("strategy fingerprint differs")

        book_state = state.get("position_book")
        expected_risk_policy = {
            "guardian_limits": self.guardian.limits.audit_view(),
            "risk_fraction": str(self.risk_fraction),
            "max_open_positions": self.max_open_positions,
            "cooldown_ms": self.cooldown_ms,
            "decision_interval_ms": self.decision_interval_ms,
            "starting_equity": str(self.starting_equity),
            "management_settings": self.book.snapshot()["management_settings"],
        }
        stored_policy = state.get("risk_policy")
        if not isinstance(book_state, Mapping):
            problems.append("full position-management state is missing")
        else:
            if not isinstance(stored_policy, Mapping) or dict(stored_policy) != expected_risk_policy:
                problems.append("risk or position-management policy differs")
            try:
                self.book.validate_state(book_state)
            except Exception as exc:
                problems.append(f"position-book state is not restorable:{type(exc).__name__}")
            if state.get("open_positions") != book_state.get("open_positions"):
                problems.append("open-position compatibility view differs from full state")
        if state.get("starting_equity") != str(self.starting_equity):
            problems.append("starting equity differs")
        guardian_state = state.get("guardian")
        if not isinstance(guardian_state, Mapping):
            problems.append("durable guardian counters and suspensions are missing")
        else:
            try:
                self.guardian.validate_state(guardian_state)
            except Exception as exc:
                problems.append(f"guardian state is not restorable:{type(exc).__name__}")
            if isinstance(book_state, Mapping):
                managed_rows = book_state.get("open_positions")
                guardian_rows = guardian_state.get("positions")
                if not isinstance(managed_rows, list) or not isinstance(guardian_rows, list):
                    problems.append("guardian and position-book exposure state is incomplete")
                else:
                    managed_by_id = {
                        str(row.get("position_id") or ""): row
                        for row in managed_rows
                        if isinstance(row, Mapping)
                    }
                    guarded_by_id = {
                        str(row.get("position_id") or ""): row
                        for row in guardian_rows
                        if isinstance(row, Mapping)
                    }
                    if set(managed_by_id) != set(guarded_by_id):
                        problems.append("guardian exposure tickets differ from managed positions")
                    for position_id, managed_row in managed_by_id.items():
                        guarded = guarded_by_id.get(position_id)
                        if guarded is None:
                            continue
                        spec = self.specs.get(str(managed_row.get("symbol") or ""))
                        try:
                            lots = Decimal(str(managed_row.get("lots")))
                            entry = Decimal(str(managed_row.get("entry_price")))
                            stop = Decimal(str(managed_row.get("stop_price")))
                            conversion = (
                                Decimal(1) if spec is not None and spec.conversion_rate is None
                                and spec.quote_currency == spec.account_currency
                                else spec.conversion_rate if spec is not None else None
                            )
                            expected_risk = (
                                abs(entry - stop) / spec.point * spec.point_value_per_lot * lots
                                if spec is not None and spec.point_value_per_lot is not None
                                else None
                            )
                            expected_notional = (
                                spec.contract_size * entry * conversion * lots
                                if spec is not None and conversion is not None
                                else None
                            )
                            guarded_risk = Decimal(str(guarded.get("risk_account")))
                            guarded_notional = Decimal(str(guarded.get("notional_account")))
                        except Exception:
                            expected_risk = expected_notional = None
                            guarded_risk = guarded_notional = Decimal(-1)
                        if (
                            expected_risk is None
                            or expected_notional is None
                            or guarded_risk != expected_risk
                            or guarded_notional != expected_notional
                            or spec is None
                            or str(guarded.get("symbol") or "") != str(managed_row.get("symbol") or "")
                            or str(guarded.get("pair") or "") != spec.pair
                            or str(guarded.get("side") or "").upper() != str(managed_row.get("side") or "").upper()
                            or guarded.get("currencies") != [spec.quote_currency]
                            or guarded.get("protected") is not bool(managed_row.get("protection_verified"))
                        ):
                            problems.append(f"guardian risk for position {position_id} differs from the managed exposure")

        pending_submission = state.get("pending_submission")
        pending_submission_state: dict[str, Any] | None = None
        if pending_submission is not None:
            if not isinstance(pending_submission, Mapping) or not str(pending_submission.get("request_id") or ""):
                problems.append("persisted submission intent is malformed or lacks its exact request id")
            else:
                pending_submission_state = dict(pending_submission)

        def parse_timestamp(value: Any, name: str, *, optional: bool = False) -> int | None:
            if value is None and optional:
                return None
            if isinstance(value, bool):
                raise ValueError(f"{name}_invalid")
            try:
                parsed = int(value)
            except Exception as exc:
                raise ValueError(f"{name}_invalid") from exc
            if parsed < 0 or str(parsed) != str(value):
                raise ValueError(f"{name}_invalid")
            return parsed

        restored_run: dict[str, Any] = {}
        try:
            restored_equity = _d(state.get("equity"))
            if restored_equity is None or restored_equity <= 0:
                raise ValueError("equity_invalid")
            restored_last_entry = parse_timestamp(
                state.get("last_entry_ms"), "last_entry_ms", optional=True
            )
            raw_decisions = state.get("last_decision_ms")
            if not isinstance(raw_decisions, Mapping):
                raise ValueError("last_decision_state_invalid")
            restored_decisions = {
                str(key): parse_timestamp(value, "last_decision_ms")
                for key, value in raw_decisions.items()
            }
            if any(key not in self.symbols for key in restored_decisions):
                raise ValueError("last_decision_symbol_invalid")
            raw_entry_blocks = state.get("entries_blocked")
            if not isinstance(raw_entry_blocks, Mapping):
                raise ValueError("entry_block_counters_invalid")
            restored_blocks = {
                str(key): parse_timestamp(value, "entry_block_counter")
                for key, value in raw_entry_blocks.items()
            }
            for name in ("stop_reason", "pause_reason"):
                if not isinstance(state.get(name), str):
                    raise ValueError(f"{name}_invalid")
            restored_run = {
                "equity": restored_equity,
                "last_entry_ms": restored_last_entry,
                "last_decision_ms": restored_decisions,
                "entries_blocked": restored_blocks,
                "stop_reason": state["stop_reason"],
                "pause_reason": state["pause_reason"],
            }
        except Exception as exc:
            problems.append(f"run lifecycle counters are invalid:{type(exc).__name__}")

        destination = self.book.snapshot()
        if (
            self.book.open_positions
            or self.book.closed
            or self.book.close_blockers
            or self.book.pending_closes
            or destination["pending_protection_modifications"]
            or destination["blocked_exits"]
        ):
            problems.append("the destination position book is not empty")

        positions: list[dict[str, Any]] = []
        if isinstance(book_state, Mapping):
            raw_positions = book_state.get("open_positions")
            if isinstance(raw_positions, list) and all(isinstance(item, Mapping) for item in raw_positions):
                positions = [dict(item) for item in raw_positions]
            else:
                problems.append("stored open-position rows are invalid")
        pending_close_ids: list[str] = []
        pending_protection_rows: list[dict[str, Any]] = []
        if isinstance(book_state, Mapping):
            pending_rows = book_state.get("pending_closes")
            if isinstance(pending_rows, list) and all(isinstance(item, Mapping) for item in pending_rows):
                pending_close_ids = [str(item.get("position_id") or "") for item in pending_rows]
            else:
                problems.append("stored pending-close rows are invalid")
            raw_protection_rows = book_state.get("pending_protection_modifications")
            if isinstance(raw_protection_rows, list) and all(
                isinstance(item, Mapping) for item in raw_protection_rows
            ):
                pending_protection_rows = [dict(item) for item in raw_protection_rows]
            else:
                problems.append("stored pending-protection rows are invalid")
        if pending_submission_state is not None and pending_close_ids:
            problems.append("a submission intent and unresolved close cannot be adopted together")
        if pending_submission_state is None:
            problems.extend(
                self._broker_position_problems(
                    positions,
                    pending_close_ids=pending_close_ids,
                    pending_protection=pending_protection_rows,
                )
            )

        if problems:
            self._recovery = {
                "detail": "; ".join(dict.fromkeys(problems)),
                "hint": (
                    "Do not resume: reconcile the exact account, request IDs, broker tickets, "
                    "sizes, entry prices, stops, and full management state. Nothing is adopted "
                    "from a partial or mismatched snapshot."
                ),
            }
            self.state = RECOVERY_REQUIRED
            self._emit("recovery_required", problems=problems)
            return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}

        try:
            # Each component validates before mutating; both validations above
            # ensure this section cannot leave a partially adopted risk state.
            self.book.restore_state(book_state)
            self.guardian.restore_state(guardian_state)
            self._equity = restored_run["equity"]
            self._last_entry_ms = restored_run["last_entry_ms"]
            self._last_decision_ms = restored_run["last_decision_ms"]
            self._entries_blocked = restored_run["entries_blocked"]
            self.stop_reason = restored_run["stop_reason"]
            self.pause_reason = restored_run["pause_reason"]
            self._pending_submission = pending_submission_state
        except Exception as exc:
            self._recovery = {
                "detail": f"full run-state restore failed: {type(exc).__name__}",
                "hint": "Do not resume. Reconcile the broker and restore from a verified durable snapshot.",
            }
            self.state = RECOVERY_REQUIRED
            self._emit("recovery_required", reason="restore_failed")
            return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}

        protection = self.book.verify_protection()
        risk_state_restored = True
        for position in self.book.open_positions:
            if not self._record_position_risk(position):
                risk_state_restored = False
            self.guardian.record_protection(
                position.position_id, protected=position.protection_verified
            )
        if not risk_state_restored:
            self.state = RECOVERY_REQUIRED
            return {
                "ok": False,
                "state": self.state,
                "recovery": dict(self._recovery),
            }
        pending_ids = {
            str(item.get("position_id") or "") for item in self.book.pending_closes
        }
        unprotected = [
            position_id
            for position_id in protection.get("missing", [])
            if position_id not in pending_ids
        ]
        if unprotected:
            self._recovery = {
                "detail": "one or more restored positions do not have matching broker-confirmed stops",
                "hint": "Do not resume entries. Reconcile every listed ticket and repair protection through an approved guarded route.",
            }
            self.state = RECOVERY_REQUIRED
            self._emit("recovery_required", reason="protection_unverified")
            return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}

        if self.book.pending_closes:
            self._recovery = {
                "detail": "one or more close requests were unresolved at shutdown",
                "hint": "Use reconcile_close with the exact durable request id. No second close or entry is sent until the broker fill is resolved.",
            }
            self.state = RECOVERY_REQUIRED
            self._emit("recovery_required", reason="pending_close")
            return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}

        if self._pending_submission is not None:
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": "a persisted entry submission must be reconciled by its exact durable request id",
                "hint": "Do not submit another entry. Query the stored request id; only a confirmed booked fill or terminal refusal can clear this intent.",
            }
            self._emit(
                "recovery_required",
                reason="pending_submission",
                request_id=self._pending_submission.get("request_id", ""),
            )
            return self.reconcile_submission()

        self._pending_submission = None
        self.state = ARMED
        self._emit("recovered", positions=len(positions))
        if not self._persist_state():
            return {"ok": False, "state": self.state, "reason": STATE_PERSISTENCE_FAILED,
                    "recovery": dict(self._recovery)}
        return {"ok": True, "state": self.state, "positions": len(positions)}

    def snapshot(self) -> dict[str, Any]:
        """Return the complete versioned state required for an exact restart."""

        # Persist the run's declared identity, never the newly selected terminal
        # account after a mismatch or disconnect. The type was bound at arm.
        expected = self.expected_account
        book_state = self.book.snapshot()
        return {
            "schema": RUN_STATE_SCHEMA,
            "mode": LIVE,
            "state": self.state,
            "account_identity": {
                "server": "" if expected is None else expected.server,
                "fingerprint": "" if expected is None else expected.fingerprint,
                "currency": "" if expected is None else expected.currency,
                "account_mode": self.expected_account_mode,
            },
            "symbols": dict(self.symbols),
            "instrument_specs": {
                symbol: spec.audit_view() for symbol, spec in sorted(self.specs.items())
            },
            "admission": None if self.admitted is None else self.admitted.audit_view(),
            "strategy_fingerprint": "" if self.admitted is None else self.admitted.strategy.fingerprint,
            "candidate_id": "" if self.admitted is None else self.admitted.candidate_id,
            "risk_policy": {
                "guardian_limits": self.guardian.limits.audit_view(),
                "risk_fraction": str(self.risk_fraction),
                "max_open_positions": self.max_open_positions,
                "cooldown_ms": self.cooldown_ms,
                "decision_interval_ms": self.decision_interval_ms,
                "starting_equity": str(self.starting_equity),
                "management_settings": book_state["management_settings"],
            },
            "guardian": self.guardian.snapshot(),
            "equity": str(self._equity),
            "starting_equity": str(self.starting_equity),
            "last_entry_ms": self._last_entry_ms,
            "last_decision_ms": dict(self._last_decision_ms),
            "pending_submission": (
                None if self._pending_submission is None else dict(self._pending_submission)
            ),
            "position_book": book_state,
            # Kept as a read-only compatibility view. Recovery cross-checks it
            # against the authoritative full PositionBook snapshot.
            "open_positions": list(book_state["open_positions"]),
            "entries_blocked": dict(self._entries_blocked),
            "stop_reason": self.stop_reason,
            "pause_reason": self.pause_reason,
        }

    # -- one decision ----------------------------------------------------

    def on_tick(
        self,
        *,
        symbol: str,
        bid: Any,
        ask: Any,
        source_time: datetime,
        now_ms: int,
        equity: Any = None,
        balance: Any = None,
    ) -> dict[str, Any]:
        """Feed one quote.  Manages positions always; decides only when RUNNING."""

        if self.state == RECOVERY_REQUIRED:
            return {"state": self.state, "blocked": RECOVERY, "execution": None,
                    "managed": [], "detail": dict(self._recovery)}
        spec = self.specs.get(symbol)
        if spec is None:
            return self._refuse(SPEC_UNKNOWN, symbol=symbol)
        # Validate account and mode *before* PositionBook sees the tick.  The
        # book may submit a close or protection change while managing exposure;
        # doing that first could address a ticket on a newly selected account.
        # The host refreshes the connected terminal on its owner thread before
        # every call here.  A refused identity never reaches management or entry.
        verdict = self._account()
        if not verdict.ok:
            if self.state == RUNNING:
                self.state = PAUSED
                self.pause_reason = ACCOUNT_NOT_REAL
                self._emit("account_lost", detail=verdict.detail)
                self._persist_state()
            return self._refuse(ACCOUNT_NOT_REAL, symbol=symbol, detail=verdict.detail)
        if self._mode_name() != LIVE or not self._live_effective():
            if self.state == RUNNING:
                self.state = PAUSED
                self.pause_reason = MODE_NOT_LIVE
                self._emit("mode_lost")
                self._persist_state()
            return self._refuse(MODE_NOT_LIVE, symbol=symbol)
        accepted = self.core.ingest(
            symbol=symbol,
            bid=bid,
            ask=ask,
            source_time=source_time,
            source="MT5_TICK",
            volume=None,
        )
        if not getattr(accepted, "accepted", False):
            reason = getattr(accepted, "refusal", None) or QUOTE_REFUSED
            return self._refuse(QUOTE_REFUSED, symbol=symbol, detail=str(reason))
        tick: Tick = accepted.tick
        if equity is not None and _d(equity) is not None:
            self._equity = _d(equity)
        self.guardian.observe_account(
            at_ms=now_ms,
            equity=self._equity,
            balance=self._equity if balance is None else _d(balance),
            day_key=datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat(),
        )

        closed_before = len(self.book.closed)
        managed = [
            self.book.on_tick(position_id=position.position_id, tick=tick, now_ms=now_ms)
            for position in list(self.book.open_positions)
            if position.symbol == symbol
        ]
        for trade in self.book.closed[closed_before:]:
            if trade.gross_account is None:
                self.guardian.suspend_entries("position_realized_pnl_unavailable")
            elif self.book.get(trade.position_id) is not None:
                self.guardian.record_partial_realised(
                    realised=trade.gross_account, at_ms=now_ms
                )
            else:
                rows = [
                    item for item in self.book.closed
                    if item.position_id == trade.position_id
                ]
                total_realised = (
                    None
                    if any(item.gross_account is None for item in rows)
                    else sum((item.gross_account for item in rows), Decimal(0))
                )
                self.guardian.record_close(
                    trade.position_id,
                    realised=trade.gross_account,
                    total_realised=total_realised,
                    at_ms=now_ms,
                )
        for position in self.book.open_positions:
            self._record_position_risk(position)
        if self.book.open_positions or any(managed):
            if not self._persist_state():
                return {
                    "tick": tick.tick_id,
                    "state": self.state,
                    "managed": [[item.audit_view() for item in group] for group in managed],
                    "execution": None,
                    "blocked": STATE_PERSISTENCE_FAILED,
                }

        if self.state != RUNNING:
            return {
                "tick": tick.tick_id,
                "state": self.state,
                "managed": [[item.audit_view() if hasattr(item, "audit_view") else str(item) for item in group] for group in managed],
                "execution": None,
                "blocked": "not_running",
            }

        blocked = self._entry_block(symbol, now_ms)
        if blocked is not None:
            return {
                "tick": tick.tick_id,
                "state": self.state,
                "managed": [],
                "execution": None,
                "blocked": blocked,
            }

        signal = self._decide(tick, now_ms)
        if signal is None:
            return {
                "tick": tick.tick_id,
                "state": self.state,
                "managed": [],
                "execution": None,
                "blocked": "no_signal",
            }

        lots, size_reason = self._size(signal, spec, tick)
        self.decisions.append(
            {
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": signal.side,
                "strategy": signal.strategy,
                "stop_points": str(signal.stop_points),
                "target_points": str(signal.target_points),
                "lots": None if lots is None else str(lots),
                "size_refused": size_reason,
                "quote": {"bid": str(tick.bid), "ask": str(tick.ask)},
                "quote_age_ms": int(now_ms - int(tick.source_time.timestamp() * 1000)),
            }
        )
        if lots is None:
            return self._refuse(SIZE_REFUSED, symbol=symbol, detail=size_reason)

        risk_account = self._risk_account(lots, signal, spec)
        notional_account = self._notional(lots, tick, spec)
        request_id = str(self.trader.request_id_for(signal))
        self._pending_submission = {
            "request_id": request_id,
            "symbol": symbol,
            "side": signal.side,
            "lots": str(lots),
            "signal_fingerprint": signal.parameter_fingerprint,
            "tick_id": tick.tick_id,
            "source_time": tick.source_time.isoformat(),
            "bid": str(tick.bid),
            "ask": str(tick.ask),
            "stop_price": None if signal.stop_price is None else str(signal.stop_price),
            "target_price": None if signal.target_price is None else str(signal.target_price),
            "requested_at_ms": int(now_ms),
            "risk_account": str(risk_account),
            "notional_account": str(notional_account),
        }
        if not self._persist_state():
            return {
                "tick": tick.tick_id,
                "state": self.state,
                "managed": [],
                "execution": None,
                "blocked": STATE_PERSISTENCE_FAILED,
            }

        report = self.trader.execute(
            signal=signal,
            lots=lots,
            risk_account=risk_account,
            notional_account=notional_account,
            quote=tick,
            now_ms=now_ms,
            decision_latency_ms=0,
            currencies=(spec.quote_currency,),
        )
        self._record_execution(report, tick, now_ms)
        if report.status == ACCEPTED and report.fill is not None and report.fill.ticket:
            fill = report.fill
            stop = signal.stop_price
            target = signal.target_price
            stop_on_valid_side = (
                stop is not None
                and fill.price is not None
                and ((signal.side == "BUY" and stop < fill.price)
                     or (signal.side == "SELL" and stop > fill.price))
            )
            target_on_valid_side = (
                target is None
                or fill.price is None
                or ((signal.side == "BUY" and target > fill.price)
                    or (signal.side == "SELL" and target < fill.price))
            )
            if not stop_on_valid_side or not target_on_valid_side:
                self.state = RECOVERY_REQUIRED
                self._recovery = {
                    "detail": (
                        "broker accepted an entry whose actual fill is inconsistent with "
                        "the signal's stop/target; the broker position must be reconciled"
                    ),
                    "hint": "Do not resume entries. Reconcile the exact broker ticket, actual fill, and broker-side protection before managing or restarting this run.",
                    "position_id": str(fill.ticket),
                    "request_id": str(fill.request_id),
                }
                self._emit(
                    "fill_not_manageable",
                    request_id=fill.request_id,
                    position_id=fill.ticket,
                    fill_price=None if fill.price is None else str(fill.price),
                    stop_price=None if stop is None else str(stop),
                    target_price=None if target is None else str(target),
                )
            else:
                try:
                    managed_position = self.book.open_from_fill(
                        fill=fill,
                        symbol=symbol,
                        side=signal.side,
                        stop_price=stop,
                        target_price=target,
                        strategy_fingerprint=signal.parameter_fingerprint,
                        now_ms=now_ms,
                    )
                    if not self._record_position_risk(managed_position):
                        self._pending_submission = {
                            **dict(self._pending_submission or {}),
                            "accepted_ticket": str(fill.ticket),
                            "reason": "risk_state_not_restorable",
                        }
                    else:
                        self._last_entry_ms = now_ms
                        self._pending_submission = None
                except Exception as exc:
                    self.state = RECOVERY_REQUIRED
                    self._recovery = {
                        "detail": f"broker fill could not be adopted into the position manager: {type(exc).__name__}",
                        "hint": "Do not resume entries. Reconcile the exact broker ticket and restore its position-management state before continuing.",
                        "position_id": str(fill.ticket),
                        "request_id": str(fill.request_id),
                    }
                    self._emit(
                        "fill_not_manageable",
                        request_id=fill.request_id,
                        position_id=fill.ticket,
                        error=type(exc).__name__,
                    )
        elif report.status == ACCEPTED and report.fill is not None:
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": "broker accepted an entry but its ticket identity is unavailable",
                "hint": "Do not resume entries. Reconcile the exact broker order and position ticket before continuing.",
                "request_id": str(report.fill.request_id),
            }
            self._emit("fill_not_manageable", request_id=report.fill.request_id)
        if report.status == UNKNOWN_OUTCOME:
            self.state = PAUSED
            self.pause_reason = "unresolved_submission"
            self._emit("unresolved", request_id=report.request_id)
        elif report.status in {REJECTED, FILL_BLOCKED}:
            self._pending_submission = None
        self._persist_state()
        return {
            "tick": tick.tick_id,
            "state": self.state,
            "managed": [],
            "execution": report.audit_view(),
            "blocked": None if report.status == ACCEPTED else report.status,
        }

    # -- internals -------------------------------------------------------

    def _record_position_risk(self, position: Any) -> bool:
        spec = self.specs.get(position.symbol)
        if spec is None or spec.point <= 0 or spec.point_value_per_lot is None:
            self.guardian.suspend_entries("position_risk_unavailable")
            self.state = RECOVERY_REQUIRED
            self._recovery = {
                "detail": f"risk terms are unavailable for open position {position.position_id}",
                "hint": "Do not resume entries; reconcile the instrument terms and broker position first.",
            }
            return False
        conversion = spec.conversion_rate
        if conversion is None:
            if spec.quote_currency != spec.account_currency:
                self.guardian.suspend_entries("position_conversion_unavailable")
                self.state = RECOVERY_REQUIRED
                self._recovery = {
                    "detail": f"account-currency conversion is unavailable for open position {position.position_id}",
                    "hint": "Do not resume entries; restore the exact conversion basis and reconcile the position.",
                }
                return False
            conversion = Decimal(1)
        risk = (
            abs(position.entry_price - position.stop_price)
            / spec.point
            * spec.point_value_per_lot
            * position.lots
        )
        notional = spec.contract_size * position.entry_price * conversion * position.lots
        self.guardian.record_open(
            OpenPositionRisk(
                position_id=position.position_id,
                symbol=position.symbol,
                pair=spec.pair,
                side=position.side,
                risk_account=risk,
                notional_account=notional,
                currencies=(spec.quote_currency,),
                protected=bool(position.protection_verified),
            )
        )
        return True

    def _refuse(self, reason: str, **detail: Any) -> dict[str, Any]:
        self._entries_blocked[reason] = self._entries_blocked.get(reason, 0) + 1
        return {"state": self.state, "blocked": reason, "detail": detail, "execution": None}

    def _entry_block(self, symbol: str, now_ms: int) -> str | None:
        if not _ENTRY_READY & {self.state}:
            return NOT_RUNNING
        if getattr(self.venue, "has_unresolved", False):
            return UNRESOLVED_ORDER
        if self.book.close_blockers or self.book.pending_closes:
            return CLOSE_UNCONFIRMED
        protection = self.book.verify_protection(update=False)
        if protection.get("pending_modification"):
            return "protection_reconciliation_required"
        if protection.get("missing"):
            return PROTECTION_UNVERIFIED
        if len(self.book.open_positions) >= self.max_open_positions:
            return POSITION_LIMIT
        if self._last_entry_ms is not None and now_ms - self._last_entry_ms < self.cooldown_ms:
            return COOLDOWN
        if self.decision_interval_ms:
            previous = self._last_decision_ms.get(symbol)
            if previous is not None and now_ms - previous < self.decision_interval_ms:
                return COOLDOWN
        allowed, reasons = self.guardian.may_open()
        if not allowed:
            self._entries_blocked["guardian:" + ",".join(reasons)] = (
                self._entries_blocked.get("guardian:" + ",".join(reasons), 0) + 1
            )
            return ENTRY_BLOCKED + ":" + ",".join(reasons)
        return None

    def _decide(self, tick: Tick, now_ms: int):
        if self.admitted is None:
            return None
        self._last_decision_ms[tick.symbol] = now_ms
        window = self.core.window(tick.symbol)
        if not window:
            return None
        from .regime import RegimeThresholds, classify
        from .signals import SignalContext

        spec = self.specs[tick.symbol]
        state = classify(window, spec=spec, thresholds=RegimeThresholds())
        if not getattr(state, "tradeable", False):
            return None
        context = SignalContext(
            ticks=window,
            spec=spec,
            regime=state,
            spread_limit_points=self.guardian.limits.max_spread_points,
            max_quote_age_ms=self.guardian.limits.max_quote_age_ms,
        )
        evaluation = self.admitted.strategy.evaluate(context)
        if evaluation.signal is not None and spec.point > 0:
            # The signal carries distances in points; the priceable stop and target
            # exist only once the instrument's point size is attached, and the
            # point size is the broker's, never a default.
            evaluation = evaluation.__class__(
                strategy=evaluation.strategy,
                family=evaluation.family,
                outcome=evaluation.outcome,
                detail=evaluation.detail,
                signal=evaluation.signal.with_point(spec.point),
            )
        self.decisions.append(
            {
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "strategy": evaluation.strategy,
                "outcome": evaluation.outcome,
                "detail": evaluation.detail,
                "signal": evaluation.signal is not None,
            }
        )
        return evaluation.signal

    def _size(self, signal: Any, spec: InstrumentSpec, tick: Tick) -> tuple[Decimal | None, str]:
        if signal.stop_points <= 0 or spec.point <= 0:
            return None, "stop_distance_unusable"
        point_value = spec.point_value_per_lot
        if point_value is None or point_value <= 0:
            return None, "point_value_unavailable"
        risk = self._equity * self.risk_fraction
        per_lot = signal.stop_points * point_value
        if per_lot <= 0:
            return None, "risk_per_lot_zero"
        raw = risk / per_lot
        step = spec.volume_step if spec.volume_step > 0 else Decimal("0.01")
        size = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
        if spec.volume_max > 0:
            size = min(size, spec.volume_max)
        if size < spec.volume_min:
            return None, "below_broker_minimum"
        if size <= 0:
            return None, "size_rounds_to_zero"
        return size, ""

    def _risk_account(self, lots: Decimal, signal: Any, spec: InstrumentSpec) -> Decimal:
        point_value = spec.point_value_per_lot or Decimal(0)
        return lots * signal.stop_points * point_value

    def _notional(self, lots: Decimal, tick: Tick, spec: InstrumentSpec) -> Decimal:
        # The ask is the more expensive side, so an exposure estimate built from it
        # can only over-state what a limit check sees.
        price = tick.ask
        conversion = spec.conversion_rate if spec.conversion_rate is not None else Decimal(1)
        return lots * spec.contract_size * price * conversion

    def _record_execution(self, report: Any, tick: Tick, now_ms: int) -> None:
        self.guardian.observe_execution(
            slippage_points=(report.fill.slippage_points if report.fill else None),
            fill_latency_ms=(report.fill.latency_ms if report.fill else None),
        )
        self._emit(
            "execution",
            request_id=report.request_id,
            status=report.status,
            reasons=list(report.reasons),
            simulated=bool(getattr(report.fill, "simulated", False)),
        )
        if report.risk is not None and not report.risk.allowed:
            self._emit("risk_refused", reasons=list(report.risk.reasons))


__all__ = [
    "ARMED",
    "AdmittedStrategy",
    "AutonomousScalper",
    "BLOCKED",
    "IDLE",
    "NO_ADMISSION",
    "PAUSED",
    "RECOVERY_REQUIRED",
    "RUNNING",
    "RunEvent",
    "STOPPED",
]
