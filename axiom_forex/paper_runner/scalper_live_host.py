"""Owner-thread host for the autonomous MT5 AX-SCALPER lane.

The only submission surface built here is the existing ``LiveLeg``. The host
polls the already-connected terminal, persists the full manager state, and keeps
managing an open position after Stop; Stop disables entries and never flattens.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from ..scalper.admission import AdmissionError, evidence_digest, load_policy, sha256_file
from ..scalper.config import LIVE, ScalperConfig
from ..scalper.execution import Trader
from ..scalper.guardian import Guardian, GuardianLimits
from ..scalper.live_account import ExpectedAccount, verify_mt5_account
from ..scalper.live_autonomous import (
    ARMED,
    BLOCKED,
    IDLE,
    PAUSED,
    RECOVERY_REQUIRED,
    RUNNING,
    STOPPED,
    AdmittedStrategy,
    AutonomousScalper,
    JsonRunStateStore,
)
from ..scalper.mutation_journal import MutationJournal
from ..scalper.run_lock import RunLock
from ..scalper.live_config import (
    RISK_KEYS,
    LiveScalperConfigError,
    load_live_scalper_config,
)
from ..scalper.live_venue import BrokerPlanFactory, BrokerVenue
from ..scalper.positions import ManagementSettings, PositionBook
from ..scalper.raw_replay import source_digest
from ..scalper.signals import default_strategies
from ..scalper.ticks import TickCore

UTC = timezone.utc
ARM_PHRASE = "ARM AX-SCALPER LIVE"
START_PHRASE = "START AX-SCALPER LIVE"


class _Reply:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.started = False
        self.cancelled = False
        self.value: Any = None
        self.error: BaseException | None = None


class _AdmissionLifecycle:
    """Exact evidence binding used by both LiveLeg and the MT5 adapter."""

    def __init__(self, owner: "ScalperLiveAutonomousHost", strategy_id: str,
                 fingerprints: Mapping[str, str], *, data_path: Path,
                 evidence_root: Path, expires_at: datetime, replay_source_digest: str):
        self.owner = owner
        self.strategy_id = strategy_id
        self.fingerprints = dict(fingerprints)
        self.data_path = data_path
        self.evidence_root = evidence_root
        self.expires_at = expires_at
        self.replay_source_digest = replay_source_digest

    def approve_active(self) -> bool:
        return self.owner._lifecycle_active(self)


class ScalperLiveAutonomousHost:
    """Explicitly armed DEMO or REAL one-symbol autonomous runner.

    Public methods are thread-safe RPCs. All provider platform, portfolio,
    ``LiveLeg``, broker reads and runner state are created/read on one owner
    thread, matching the existing live-host ownership model.
    """

    def __init__(self, *, session: Any, adapter: Any, mode: Any,
                 platform_factory: Callable[[str], Any], admission_host: Any,
                 live_workflow: Any, settings_path: str | Path,
                 state_path: str | Path, audit_log: Any = None,
                 poll_seconds: float = 0.25):
        self.session = session
        self.adapter = adapter
        self.mode = mode
        self.platform_factory = platform_factory
        self.admission_host = admission_host
        self.live_workflow = live_workflow
        self.settings_path = Path(settings_path)
        from ..scalper.account_run_state import AccountRunStates
        self.account_states = AccountRunStates(state_path)
        self.state_store = JsonRunStateStore(state_path)
        self.journal_path = self.state_store.path.with_name(self.state_store.path.name + ".mutations.sqlite3")
        self._journal: MutationJournal | None = None
        self._run_lock: RunLock | None = None
        self.audit_log = audit_log
        self.poll_seconds = max(0.1, float(poll_seconds))
        self._lock = threading.RLock()
        self._queue: queue.Queue[tuple[str, dict[str, Any], _Reply]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closing = False
        self._runtime: AutonomousScalper | None = None
        self._platform: Any = None
        self._leg: Any = None
        self._candidate_id = ""
        self._strategy_id = ""
        self._admitted: AdmittedStrategy | None = None
        self._expected: ExpectedAccount | None = None
        self._bound_mode: str | None = None
        self._lifecycle: _AdmissionLifecycle | None = None
        self._armed_by_host = False
        self._last_tick: dict[str, Any] = {}
        self._last_result: dict[str, Any] = {}
        self._state = "STOPPED"
        self._reason = "live_scalper_not_started"

    # -- operator API -------------------------------------------------

    def view(self) -> dict[str, Any]:
        # All reads that touch the terminal, provider, admission store or run
        # objects stay on the same owner thread as order/position management.
        result = self._call("view", {}, timeout=10.0)
        if not result.get("ok"):
            return {"available": True, "lane": "LIVE_MT5",
                    "state": "UNAVAILABLE", "reason": str(result.get("reason") or "live_view_unavailable"),
                    "settings_path": str(self.settings_path), "state_path": str(self.state_store.path),
                    "connected": False, "account_mode": "UNKNOWN", "blockers": [
                        self._blocker("live_view_unavailable",
                                      "The MT5 runner status could not be read.",
                                      "Do not arm or start until this status is readable.")],
                    "candidates": [], "eligible_candidates": [], "config": {}, "run": {}}
        preview = dict(result.get("preview") or {})
        runtime = dict(result.get("runtime") or {})
        state = str(result.get("state") or self._state)
        reason = str(result.get("reason") or self._reason)
        armed = bool(result.get("armed"))
        tick = dict(result.get("latest_tick") or {})
        last_result = dict(result.get("last_result") or {})
        adapter_status = dict(result.get("adapter_status") or {})
        selected = next((item for item in preview.get("candidates", ())
                         if item.get("fingerprint") == self._candidate_id), None)
        blockers = list(preview.get("blockers") or ())
        if not preview.get("eligible_candidates") and not any(
            item.get("reason") == "no_admitted_candidate" for item in blockers
        ):
            blockers.append({"reason": "no_admitted_candidate",
                             "message": "No candidate currently passes signed evidence and independent raw replay.",
                             "hint": "Supply a genuine signed evidence artifact that passes the configured policy and replay."})
        account = dict(adapter_status.get("redacted_account") or {})
        return {
            "available": True,
            "lane": "LIVE_MT5",
            "state": state,
            "reason": reason,
            "armed": armed or bool(adapter_status.get("armed")),
            "adapter_armed": bool(adapter_status.get("armed")),
            "account_mode": str(adapter_status.get("account_mode") or "UNKNOWN"),
            "account_mode_confirmed": bool(adapter_status.get("account_mode_confirmed")),
            "account_fingerprint": str(adapter_status.get("account_fingerprint") or ""),
            "account_server": str(account.get("server") or ""),
            "account_currency": str(account.get("currency") or ""),
            "connected": bool(adapter_status.get("connected")),
            "mode": str(getattr(self.mode, "mode", "") or ""),
            "live_effective": bool(getattr(self.mode, "live_effective", False)),
            "candidate_id": self._candidate_id,
            "candidate": dict(selected or {}),
            "candidates": list(preview.get("candidates") or ()),
            "eligible_candidates": list(preview.get("eligible_candidates") or ()),
            "review": dict(preview.get("review") or {}),
            "settings_path": str(self.settings_path),
            "state_path": str(self.state_store.path),
            "config": dict(preview.get("config") or {}),
            "blockers": blockers,
            "run": dict(runtime.get("run") or {}),
            "latest_tick": tick,
            "last_result": last_result,
            "broker_monitor": dict(result.get("monitor") or {}),
            "saved_recovery": dict(result.get("saved_recovery") or {}),
            "open_positions": list((runtime.get("run") or {}).get("open_positions") or ()),
            "pending_submission": (runtime.get("run") or {}).get("pending_submission"),
            "order_sending": state == RUNNING,
            "paper_only": False,
            "sample_data": False,
            "live_ready": bool(selected and state in {ARMED, RUNNING}
                                and adapter_status.get("armed") and not blockers),
        }

    def arm(self, *, candidate_id: str, confirmation: str,
            account_confirmation: str) -> dict[str, Any]:
        return self._call("arm", {
            "candidate_id": str(candidate_id or "").strip(),
            "confirmation": str(confirmation or "").strip(),
            "account_confirmation": str(account_confirmation or "").strip(),
        })

    def start(self, *, candidate_id: str, confirmation: str,
              account_confirmation: str) -> dict[str, Any]:
        return self._call("start", {
            "candidate_id": str(candidate_id or "").strip(),
            "confirmation": str(confirmation or "").strip(),
            "account_confirmation": str(account_confirmation or "").strip(),
        })

    def switch_account(self, *, login: int, server: str, password: str | None = None) -> dict[str, Any]:
        """Enqueue an explicit switch on the same owner thread as the live run."""
        return self._call("switch_account", {"login": login, "server": server, "password": password}, timeout=120.0)

    def _bind_account_state(self) -> None:
        if self._runtime is not None:
            return
        if not self.account_states.marker.exists():
            return
        terminal = self.session.source.terminal
        if terminal is None or terminal is not self.adapter.terminal:
            raise ValueError("shared_terminal_unavailable")
        identity = self.account_states.identity(terminal.status())
        path = self.account_states.path_for(identity)
        self.state_store = JsonRunStateStore(path)
        self.journal_path = path.with_name(path.name + ".mutations.sqlite3")

    def _invalidate_proposal_approvals(self) -> None:
        from ..scalper.live_bridge import KIND
        repo = self.adapter.repository
        snapshots = repo.list_snapshots(kind=KIND)
        with repo.transaction():
            repo.save_snapshot("axiom-account-switch-approval-epoch", "MT5_ACCOUNT_SWITCH_EPOCH",
                               {"kind": "MT5_ACCOUNT_SWITCH_EPOCH", "at": datetime.now(UTC).isoformat()})
            for state in snapshots:
                if not isinstance(state, dict) or not state.get("snapshot_id"):
                    raise ValueError("proposal_identity_unavailable")
                state = dict(state)
                for key in ("operator_review", "operator_approval", "signed_approval", "armed"):
                    state[key] = {}
                if str(state.get("status") or "") in {"RECEIVED", "REVIEWED", "APPROVED", "ARMED", "BLOCKED"}:
                    state["status"] = "BLOCKED"
                repo.save_snapshot(str(state["snapshot_id"]), KIND, state)

    @staticmethod
    def _account_description(status: Any) -> str:
        if status is None or not status.connected or not status.terminal_connected:
            return "Actual account is unavailable; verify the terminal before proceeding."
        from ..brokers.mt5.identity import account_fingerprint
        digits = str(status.account_login)
        masked = "***" + digits[-4:] if status.account_login > 0 else "unknown"
        return (f"Actual account: {status.account_mode}, server {status.account_server or 'unknown'}, "
                f"login {masked}, currency {status.account_currency or 'unknown'}, "
                f"fingerprint {account_fingerprint(status.account_login) if status.account_login > 0 else 'unknown'}.")

    def _do_switch_account(self, *, login: int, server: str, password: str | None) -> dict[str, Any]:
        """No login until all ownership, broker and recovery checks pass."""
        from dataclasses import replace
        from ..brokers.mt5.reconcile import MT5_FINAL_STATES
        from ..brokers.mt5.identity import account_fingerprint
        lock = None
        attempted = False
        actual = None
        adapter = self.adapter
        if (isinstance(login, bool) or not isinstance(login, int) or not 0 < login < 2**64
                or not isinstance(server, str) or not server or server != server.strip()
                or len(server) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in server)
                or (password is not None and (not isinstance(password, str) or len(password) > 1024))):
            return self._refuse("switch_target_invalid", "Enter and confirm a valid MT5 login and server before switching.")
        # A stopped legacy LIVE host can retain an owner thread and adapter
        # references. Do not query that thread while holding the adapter lock:
        # its view may itself need the lock. Require the session to be closed.
        if getattr(self.live_workflow, "session", None) is not None:
            return self._refuse("live_session_open", "Stop and close the other LIVE session. If it remains open, Disconnect and reconnect the current account before switching.")
        bridge = getattr(self.live_workflow, "scalper_bridge", None)
        loader = getattr(bridge, "stack_loader", None) if bridge is not None else None
        if bridge is not None and (getattr(bridge, "_stack_prepared", False)
                                   or getattr(loader, "_owner_host", None) is not None):
            return self._refuse("one_shot_session_open", "An exact-plan one-shot owner is still open. Stop and reconcile it, then close or restart the workstation before switching.")
        try:
            with adapter.account_switch_lock:
                terminal = self.session.source.terminal
                if terminal is None or not self.session.connected or terminal is not adapter.terminal:
                    return self._refuse("shared_terminal_unavailable", "Connect the shared MT5 observation and trading adapter first; no login was attempted.")
                if (self._runtime is not None or self._run_lock is not None
                        or self._state in {RUNNING, PAUSED, RECOVERY_REQUIRED}
                        or getattr(self.live_workflow, "session", None) is not None
                        or (bridge is not None and (getattr(bridge, "_stack_prepared", False)
                            or getattr(loader, "_owner_host", None) is not None))):
                    return self._refuse("runner_active", "Stop all live sessions and reconcile their positions and requests before switching.")
                try:
                    lock = RunLock(str(self.account_states.legacy) + ".owner.lock")
                except Exception:
                    return self._refuse("runner_owned_elsewhere", "Another process owns the live run. Stop it and finish reconciliation before switching.")
                try:
                    before = terminal.status()
                    actual = before
                    before_identity = self.account_states.identity(before)
                    self.session.refresh()
                    adapter.refresh_account()
                    observed = dict(self.session.account() or {})
                    if (adapter._status is None or self.account_states.identity(adapter._status) != before_identity
                            or observed.get("fingerprint") != account_fingerprint(before.account_login)
                            or observed.get("server") != before.account_server
                            or observed.get("mode") != before.account_mode
                            or observed.get("currency") != before.account_currency
                            or observed.get("mode_confirmed") is not True):
                        raise ValueError("current_account_disagreement")
                except Exception:
                    adapter._switch_identity_verified = False
                    adapter.disarm(reason="account_identity_unverified")
                    self.mode.confirmed = False
                    self.mode.session_account = ""
                    return self._refuse("current_account_unverified", "The current shared account could not be independently verified. " + self._account_description(actual) + " Verify the terminal before retrying; Arm and Start remain blocked.")
                try:
                    self.account_states.assert_all_resolved()
                except Exception:
                    return self._refuse("saved_recovery_unresolved", "A saved account run or mutation journal is active, unresolved, missing, or unreadable. Preserve the files and reconcile that exact run before switching.")
                if adapter.positions():
                    return self._refuse("broker_positions_open", "Broker positions are open. Manage or close them independently, then reconcile before switching; this action closes nothing.")
                orders = terminal.module.orders_get()
                if orders is None:
                    raise ValueError("broker_orders_unavailable")
                if orders:
                    return self._refuse("broker_orders_open", "Broker orders are open. Resolve them in the broker terminal and reconcile before switching; nothing was cancelled.")
                records = adapter.repository.list_orders()
                if not isinstance(records, list):
                    raise ValueError("order_history_unavailable")
                if any(not isinstance(r, Mapping) or (str(r.get("broker_id") or "").lower() == "mt5_terminal" and str(r.get("state") or "").upper() not in MT5_FINAL_STATES) for r in records):
                    return self._refuse("orders_unresolved", "Reconcile all unresolved MT5 orders and closes before switching; do not resend them.")
                if adapter.unresolved_protection_modification_ids():
                    return self._refuse("protection_unresolved", "Reconcile the unresolved protection changes before switching.")
                if self.account_states.identity(terminal.status()) != self.account_states.identity(before):
                    raise ValueError("account_changed_during_checks")
                # Discard any read-only one-shot planning platform built for
                # the old account; it must not be reused on the new account.
                if loader is not None:
                    try:
                        loader.release()
                        if getattr(loader, "_planning_platform", None) is not None or getattr(loader, "_owner_host", None) is not None:
                            raise ValueError("one_shot_stack_not_released")
                    except Exception:
                        return self._refuse("one_shot_stack_unavailable", "The old account's planning stack could not be safely closed. Preserve its state and restart the workstation before switching.")
                try:
                    self.account_states.enable(before_identity)
                    self.account_states.path_for(before_identity)  # verify marker and legacy binding before login
                except Exception:
                    return self._refuse("account_scope_unavailable", "The saved run cannot be safely bound to the current account. Preserve the files, connect the account that owns the legacy run, and reconcile it before switching.")
                # Invalidate authority and approvals *before* any login; failure
                # cannot leave stale authorization attached to an unknown account.
                adapter._switch_identity_verified = False
                adapter.disarm(reason="explicit_account_switch")
                self._release_arm()
                self.live_workflow.disarm(reason="explicit_account_switch")
                # Keep the selected PAPER/LIVE mode, but LIVE authority never
                # survives a switch. A fresh explicit mode confirmation is required.
                self.mode.confirmed = False
                self.mode.confirmed_at = None
                self.mode.session_account = ""
                self.mode.session_account_at = None
                self._invalidate_proposal_approvals()
                self._candidate_id = self._strategy_id = ""
                self._admitted = self._expected = self._bound_mode = None
                self._state, self._reason = STOPPED, "account_switch"
                adapter._fingerprint_cache = ""
                adapter._account_identity = None
                adapter._status = adapter._terms = None
                adapter._symbol_cache.clear()
                adapter._account_validated = None
                adapter._last_recovery = ()
                adapter._protection_modification_results.clear()
                self._last_result = {}
                self._last_tick = {}
                self.session.snapshot_data = {}
                attempted = True
                kwargs = {"server": server, "timeout": int(terminal.settings.timeout_ms)}
                if password:
                    kwargs["password"] = password
                try:
                    success = terminal.module.login(login, **kwargs) is True
                except Exception:
                    success = False
                finally:
                    kwargs.pop("password", None)
                    del password
                try:
                    actual = terminal.status()
                    self.session.refresh()
                    adapter.refresh_account()
                    observed = dict(self.session.account() or {})
                    adapter_status = adapter._status
                    ident = self.account_states.identity(actual)
                    if not (success and actual.account_login == login and actual.account_server == server
                            and adapter_status is not None and self.account_states.identity(adapter_status) == ident
                            and observed.get("fingerprint") == account_fingerprint(login)
                            and observed.get("server") == server and observed.get("currency") == actual.account_currency
                            and observed.get("mode") == actual.account_mode and observed.get("mode_confirmed") is True):
                        raise ValueError("account_identity_disagreement")
                    # The target cannot be queried before login. An unexpected
                    # target exposure means an ambiguous outcome, not a rollback.
                    target_orders = terminal.module.orders_get()
                    if target_orders is None:
                        return {"ok": False, "attempted": True, "reason": "target_orders_unreadable",
                                "message": self._account_description(actual) + " Target broker orders could not be read. Verify them before retrying; Arm and Start remain blocked."}
                    if target_orders or adapter.positions():
                        return {"ok": False, "attempted": True, "reason": "target_exposure_open",
                                "message": self._account_description(actual) + " The target has broker positions or orders. Resolve them independently and reconcile before retrying; Arm and Start remain blocked. Nothing was closed or cancelled."}
                    self._bind_account_state()
                    if self.account_states.identity(terminal.status()) != ident:
                        raise ValueError("account_changed_during_state_bind")
                    # In-memory only. The settings file and credentials are untouched.
                    terminal.settings = replace(terminal.settings, account_login=login, server=server)
                    self.session.source.settings = replace(self.session.source.settings, account_login=login, server=server)
                    adapter.settings = replace(adapter.settings, account_login=login, server=server)
                    adapter._switch_identity_verified = True
                    return {"ok": True, "attempted": True, "reason": "account_switched",
                            "message": self._account_description(actual) + " Review the account and explicitly select LIVE, Arm and Start again. No order was sent."}
                except Exception:
                    # Report a fresh observation when possible, but never turn it
                    # into authority after any false/ambiguous login or read.
                    try:
                        actual = terminal.status()
                    except Exception:
                        actual = None
                    return {"ok": False, "attempted": True, "reason": "account_switch_unverified",
                            "message": "The login or account verification failed or was ambiguous. " + self._account_description(actual) + " Arm and Start are blocked; verify the terminal and explicitly retry. No order was sent."}
        except Exception:
            return {"ok": False, "attempted": attempted, "reason": "account_switch_blocked",
                    "message": ("Switch blocked: a broker, ownership, approval or saved recovery check could not be verified. "
                                "Keep all records, stop runners, and reconcile before retrying. No order was sent. "
                                + (self._account_description(actual) if attempted or not adapter._switch_identity_verified else ""))}
        finally:
            if lock is not None:
                lock.close()

    def stop(self) -> dict[str, Any]:
        return self._call("stop", {})

    def reconcile(self) -> dict[str, Any]:
        return self._call("reconcile", {})

    def reconcile_close(self, *, position_id: str) -> dict[str, Any]:
        return self._call("reconcile_close", {"position_id": str(position_id or "").strip()})

    def reconcile_request(self, *, request_id: str) -> dict[str, Any]:
        return self._call("reconcile_request", {"request_id": str(request_id or "").strip()})

    # -- worker lifecycle ---------------------------------------------

    def _call(self, operation: str, payload: dict[str, Any], *, timeout: float = 45.0) -> dict[str, Any]:
        if self._closing:
            return {"ok": False, "reason": "host_shutting_down", "message": "The LIVE runner is shutting down."}
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="axiom-scalper-live", daemon=True)
                self._thread.start()
        reply = _Reply()
        self._queue.put((operation, payload, reply))
        if not reply.ready.wait(timeout):
            with reply.lock:
                if not reply.ready.is_set():
                    if not reply.started:
                        reply.cancelled = True
                        return {"ok": False, "reason": "live_runner_busy",
                                "message": "The command timed out before execution and was cancelled."}
                    return {"ok": False, "reason": "live_runner_action_in_progress",
                            "message": "The command started but has not answered. Its outcome is not yet known; inspect the runner state and do not retry blindly."}
        if reply.error is not None:
            return {"ok": False, "reason": type(reply.error).__name__, "message": "The guarded LIVE runner refused the action; no retry was attempted."}
        value = reply.value
        return dict(value) if isinstance(value, Mapping) else {"ok": False, "reason": "invalid_runner_reply"}

    def _worker(self) -> None:
        while not self._closing:
            task: tuple[str, dict[str, Any], _Reply] | None = None
            try:
                task = self._queue.get(timeout=self.poll_seconds)
            except queue.Empty:
                pass
            if task is not None:
                operation, payload, reply = task
                with reply.lock:
                    if reply.cancelled:
                        if operation == "switch_account":
                            payload.clear()
                        reply.ready.set()
                        continue
                    reply.started = True
                try:
                    handler = getattr(self, f"_do_{operation}", None)
                    if not callable(handler):
                        raise ValueError("live_scalper_action_unknown")
                    reply.value = handler(**payload)
                except BaseException as exc:
                    reply.error = exc
                finally:
                    if operation == "switch_account":
                        payload.clear()
                    with reply.lock:
                        reply.ready.set()
            else:
                try:
                    self._cycle()
                except Exception as exc:
                    self._reason = f"runner_cycle_failed:{type(exc).__name__}"

    def _do_view(self) -> dict[str, Any]:
        if self._runtime is None and self.account_states.marker.exists():
            try:
                self._bind_account_state()
            except Exception:
                return {"ok": False, "reason": "account_run_state_unavailable",
                        "message": "Account-specific run state is unavailable; preserve files and do not arm."}
        run_view = self._runtime.status() if self._runtime is not None else {}
        preview = self._configuration_review()
        adapter_status = self._adapter_status()
        return {"ok": True, "state": str(run_view.get("state") or self._state),
                "reason": self._reason, "armed": self._armed_by_host,
                "run": run_view, "preview": preview, "adapter_status": adapter_status,
                "monitor": self._broker_monitor(preview),
                "saved_recovery": self._saved_recovery_view(),
                "runtime": {"run": run_view}, "latest_tick": dict(self._last_tick),
                "last_result": dict(self._last_result)}

    def _saved_recovery_view(self) -> dict[str, Any]:
        """Merge journal and snapshot identities without adopting or changing either."""
        view = self._saved_state_recovery_view()
        rows = list(view.get("requests") or ())
        if not self.journal_path.exists():
            if view.get("present"):
                view["error"] = view.get("error") or "stored_mutation_journal_missing"
                view["note"] = "The run journal is missing. Preserve the state and broker records; do not resend or arm."
            return view
        try:
            journal = MutationJournal(self.journal_path)
            try:
                records = journal.unresolved()
            finally:
                journal.close()
        except Exception:
            view["present"] = True
            view["error"] = view.get("error") or "mutation_journal_unreadable"
            view["note"] = "The mutation journal is unreadable. Preserve it and the run state; do not resend."
            return view
        if not view.get("present"):
            view["present"] = True
            view["error"] = "run_state_missing_with_journal"
            view["note"] = "Run state is missing while a mutation journal exists. Preserve both; do not arm."
        by_id = {str(item.get("request_id") or ""): item for item in rows}
        for record in records:
            identity, intent = record["identity"], record["intent"]
            request_id = record["request_id"]
            row = by_id.get(request_id)
            if row is None:
                row = {"request_id": request_id, "kind": record["action"],
                       "position_id": str(intent.get("position_id") or "")}
                rows.append(row)
                by_id[request_id] = row
            elif row.get("kind") != record["action"]:
                view["error"] = "journal_snapshot_request_mismatch"
            row.update({"run_id": identity.get("run_id", ""),
                        "account_mode": identity.get("account_mode", ""),
                        "account_server": identity.get("account_server", ""),
                        "account_fingerprint": identity.get("account_fingerprint", ""),
                        "account_currency": identity.get("account_currency", ""),
                        "strategy_fingerprint": identity.get("strategy_fingerprint", ""),
                        "candidate_id": identity.get("candidate_id", ""),
                        "policy_version": identity.get("policy_version", ""),
                        "evidence_digest": identity.get("evidence_digest", ""),
                        "symbol": identity.get("symbol", ""),
                        "requested_values": ", ".join(
                            f"{key}={intent[key]}" for key in (
                                "side", "lots", "requested_lots", "stop_price", "target_price",
                                "previous_stop_price", "previous_target_price", "reason"
                            ) if key in intent),
                        "reason": (str(self._last_result.get("reason"))
                                   if self._last_result.get("request_id") == request_id and self._last_result.get("reason")
                                   else "broker response not durably recorded" if record["result"] is None
                                   else "durable outcome or manager state requires exact reconciliation")})
        view["requests"] = rows
        return view

    def _saved_state_recovery_view(self) -> dict[str, Any]:
        """Show untrusted stored request identities without adopting or modifying state."""
        try:
            state = self.state_store.load()
        except Exception:
            return {"present": True, "error": "stored_run_state_unreadable",
                    "requests": [], "note": "Stored identities are unavailable. Preserve the state and broker records; do not resend."}
        if state is None:
            return {"present": False}
        rows: list[dict[str, str]] = []
        damaged = False
        entry = state.get("pending_submission")
        if entry is not None:
            if isinstance(entry, Mapping):
                rows.append({"kind": "entry", "request_id": str(entry.get("request_id") or ""),
                             "position_id": ""})
            else:
                damaged = True
        book = state.get("position_book")
        if not isinstance(book, Mapping):
            damaged = True
        else:
            for key, kind in (("pending_closes", "close"),
                              ("pending_protection_modifications", "protection")):
                pending = book.get(key)
                if not isinstance(pending, list):
                    damaged = True
                    continue
                for item in pending:
                    if not isinstance(item, Mapping):
                        damaged = True
                        continue
                    rows.append({"kind": kind, "request_id": str(item.get("request_id") or ""),
                                 "position_id": str(item.get("position_id") or "")})
        # All fields below are *stored* claims, not verified account facts. Do
        # not infer a missing run identity or attribute a ticket on coincidence.
        account = state.get("account_identity")
        account = account if isinstance(account, Mapping) else {}
        admission = state.get("admission")
        admission = admission if isinstance(admission, Mapping) else {}
        stored_positions = book.get("open_positions") if isinstance(book, Mapping) else []
        stored_positions = stored_positions if isinstance(stored_positions, list) else []
        source_by_id = {}
        if isinstance(entry, Mapping) and entry.get("request_id"):
            source_by_id[("entry", str(entry["request_id"]))] = [entry]
        if isinstance(book, Mapping):
            for key, kind in (("pending_closes", "close"),
                              ("pending_protection_modifications", "protection")):
                values = book.get(key)
                if isinstance(values, list):
                    for item in values:
                        if isinstance(item, Mapping) and item.get("request_id"):
                            source_by_id.setdefault((kind, str(item["request_id"])), []).append(item)
        request_ids = [row["request_id"] for row in rows if row["request_id"]]
        if (len(request_ids) != len(set(request_ids))
                or any(len(items) != 1 for items in source_by_id.values())):
            damaged = True
        for row in rows:
            sources = source_by_id.get((row["kind"], row["request_id"]), [])
            source = sources[0] if len(sources) == 1 else {}
            positions = [item for item in stored_positions if isinstance(item, Mapping)
                         and str(item.get("position_id") or "") == row["position_id"]]
            symbol = source.get("symbol")
            if not symbol and row["position_id"] and len(positions) == 1:
                symbol = positions[0].get("symbol")
            row.update({
                "account_mode": str(account.get("account_mode") or ""),
                "account_server": str(account.get("server") or ""),
                "account_fingerprint": str(account.get("fingerprint") or ""),
                "account_currency": str(account.get("currency") or ""),
                "run_id": str(state.get("run_id") or ""),
                "run_schema": str(state.get("schema") or ""),
                "run_state": str(state.get("state") or ""),
                "strategy_fingerprint": str(state.get("strategy_fingerprint") or ""),
                "candidate_id": str(admission.get("candidate_id") or ""),
                "evidence_digest": str(admission.get("evidence_digest") or ""),
                "policy_version": str(admission.get("policy_version") or ""),
                "symbol": str(symbol or ""),
                "requested_values": ", ".join(
                    f"{key}={source[key]}" for key in (
                        "side", "lots", "requested_lots", "stop_price", "target_price",
                        "previous_stop_price", "previous_target_price", "reason"
                    ) if key in source
                ),
            })
        damaged = damaged or any(not row["request_id"] for row in rows)
        return {"present": True, "requests": rows,
                "error": "stored_run_state_unreadable" if damaged else "",
                "note": ("Stored request list is incomplete or damaged; preserve state and broker records; do not resend."
                         if damaged else "Unverified stored identities only; viewing does not reconcile or adopt them.")}

    def _broker_monitor(self, preview: Mapping[str, Any]) -> dict[str, Any]:
        """Read account-wide broker positions and durable orders, never infer attribution."""
        view: dict[str, Any] = {
            "available": False, "positions": [], "orders": [],
            "positions_reason": "verified_real_account_required",
            "orders_reason": "verified_real_account_required",
            "pnl": {"available": False, "reason": "verified_real_account_required",
                    "basis": "MT5 account-wide open-position profit only; excludes closed P&L and costs not reported in profit"},
        }
        try:
            account = dict((preview.get("config") or {}).get("expected_account") or {})
            expected = ExpectedAccount(**account)
            self.adapter.refresh_account()
            status = dict(self.adapter.status() or {})
            if not verify_mt5_account(status, expected).ok:
                return view
        except Exception:
            return view
        view["available"] = True
        view["observed_at"] = datetime.now(UTC).isoformat()
        view["account_fingerprint"] = expected.fingerprint
        view["currency"] = expected.currency
        try:
            rows = [dict(item.audit_view()) for item in self.adapter.positions()]
            view["positions"] = rows
            view["positions_reason"] = ""
            try:
                profits = [Decimal(str(row.get("profit"))) for row in rows]
                if rows and all(value.is_finite() for value in profits):
                    view["pnl"] = {"available": True, "total": str(sum(profits, Decimal(0))),
                                   "currency": expected.currency, "basis": view["pnl"]["basis"]}
                else:
                    view["pnl"]["reason"] = "no_open_positions" if not rows else "broker_profit_unavailable"
            except (InvalidOperation, TypeError, ValueError):
                view["pnl"]["reason"] = "broker_profit_unavailable"
        except Exception:
            # Unknown is not flat and missing profit is not zero.
            view["positions_reason"] = "broker_positions_unavailable"
            view["pnl"]["reason"] = "broker_positions_unavailable"
        try:
            records = self.adapter.repository.list_orders()
            view["orders"] = [{key: row.get(key) for key in (
                                  "request_id", "state", "provider_symbol", "side", "volume",
                                  "strategy_id", "sent_at", "updated_at", "order_ticket",
                                  "booked", "retcode_name", "close_position_id")}
                              for row in records
                              if str(row.get("broker_id") or "").lower() == "mt5_terminal"
                              and self.adapter._record_account_fingerprint(row) == expected.fingerprint
                              and str(row.get("account_server") or "") == expected.server]
            view["orders_reason"] = ""
        except Exception:
            view["orders_reason"] = "durable_order_history_unavailable"
        try:
            self.adapter.refresh_account()
            if not verify_mt5_account(dict(self.adapter.status() or {}), expected,
                                      expected_mode=str(status.get("account_mode") or "")).ok:
                raise ValueError("account_changed")
        except Exception:
            return {"available": False, "positions": [], "orders": [],
                    "positions_reason": "account_changed_or_unavailable_during_read",
                    "orders_reason": "account_changed_or_unavailable_during_read",
                    "pnl": {"available": False, "reason": "account_changed_or_unavailable_during_read",
                            "basis": "MT5 account-wide open-position profit only"}}
        return view

    def _do_arm(self, *, candidate_id: str, confirmation: str,
                account_confirmation: str) -> dict[str, Any]:
        try:
            self._bind_account_state()
        except Exception:
            return self._refuse("account_run_state_unavailable", "The account-specific run state could not be verified. Preserve all state files and resolve recovery before arming.")
        if confirmation != ARM_PHRASE:
            return self._refuse("arm_confirmation_mismatch", f"Type {ARM_PHRASE!r} exactly. No trading permission was changed.")
        if self._state in {RUNNING, PAUSED, RECOVERY_REQUIRED}:
            return self._refuse("live_run_already_active", "Stop and reconcile the current AX-SCALPER run before another arm request.")
        saved = self._saved_recovery_view()
        if saved.get("error") or saved.get("requests"):
            return self._refuse("stored_reconciliation_required",
                                "Stored requests are unresolved or unreadable. Preserve the state and broker records; do not resend or arm.")
        prepared = self._prepare_candidate(candidate_id)
        if not prepared[0]:
            return prepared[1]
        config_view, currency, expected, strategy, evidence, policy_version, fingerprints, binding = prepared[1]
        account_mode = account_confirmation.split(" ", 1)[0]
        account_blocker = self._check_live_account(expected, account_confirmation=account_confirmation,
                                                   expected_mode=account_mode)
        if account_blocker:
            return account_blocker
        platform = None
        try:
            platform = self.platform_factory(str(config_view["pair"]))
            if str(getattr(platform, "broker_symbol", "")) != str(config_view["symbol"]):
                return self._refuse("provider_symbol_identity_mismatch", "The provider setup resolves to a different broker symbol. Use the exact terminal-verified symbol in both setups.")
            requirement = dict(self.adapter.stop_requirement(
                platform.spec, symbol=str(config_view["symbol"])
            ) or {})
            if not requirement.get("valid"):
                return self._refuse(str(requirement.get("reason") or "broker_stop_unavailable"),
                                    str(requirement.get("detail") or "Declare and verify the broker-side stop for this exact symbol before arming."))
        except Exception as exc:
            return self._refuse(str(getattr(exc, "reason", "provider_setup_unavailable")),
                                f"The guarded broker terms could not be prepared ({type(exc).__name__}). Configure the provider and exact MT5 symbol first.")
        finally:
            if platform is not None:
                self._close_platform(platform)
        if self._other_live_session_running():
            return self._refuse("another_live_session_running", "Stop the other LIVE session before arming AX-SCALPER.")
        if bool(getattr(self.adapter, "armed", False)) and not self._armed_by_host:
            return self._refuse("live_adapter_already_armed", "Disarm the existing LIVE plan before arming the separate AX-SCALPER lane.")
        strategy_id = f"ax-scalper-live:{strategy.fingerprint}"
        lifecycle = _AdmissionLifecycle(self, strategy_id, fingerprints, **binding)
        registry = getattr(self.live_workflow, "armed_strategy", None)
        try:
            if registry is None:
                return self._refuse("live_lifecycle_registry_missing", "The existing MT5 adapter has no strategy lifecycle registry.")
            registry.register_delegate(strategy_id, lifecycle)
            result = self.adapter.arm_trading(
                confirm="ENABLE LIVE TRADING",
                account_login=expected.login,
                server=expected.server,
                account_fingerprint=expected.fingerprint,
                account_mode=account_mode,
                account_currency=expected.currency,
            )
            if (not isinstance(result, Mapping) or result.get("armed") is not True
                    or not bool(getattr(self.adapter, "armed", False))):
                registry.unregister_delegate(strategy_id, lifecycle)
                try:
                    self.adapter.disarm(reason="ax_scalper_arm_confirmation_missing")
                except Exception:
                    pass
                return self._refuse(str(result.get("reason") or "live_arm_refused"),
                                    str(result.get("message") or "The MT5 adapter refused arming."),
                                    blockers=result.get("blockers") or ()) if isinstance(result, Mapping) else self._refuse("live_arm_confirmation_missing", "The MT5 adapter did not positively confirm arming; the adapter was disarmed.")
        except Exception as exc:
            registry.unregister_delegate(strategy_id, lifecycle)
            return self._refuse("live_arm_refused", f"The guarded adapter refused arming ({type(exc).__name__}).")
        self._candidate_id = candidate_id
        self._strategy_id = strategy_id
        self._admitted = AdmittedStrategy(
            strategy=strategy, candidate_id=candidate_id,
            evidence_digest=evidence, policy_version=policy_version,
            approved_at=datetime.now(UTC).isoformat(),
        )
        self._expected = expected
        self._bound_mode = account_mode
        self._lifecycle = lifecycle
        self._armed_by_host = True
        self._state, self._reason = ARMED, "explicit_ax_scalper_live_arm"
        self._audit("AX_SCALPER_LIVE_ARMED", candidate_id=candidate_id,
                    evidence_digest=evidence, account_fingerprint=expected.fingerprint)
        self._last_result = {"ok": True, "reason": "armed", "message": f"AX-SCALPER is armed for the exact {account_mode} account and admitted candidate. No order was sent; press Start to begin."}
        return dict(self._last_result)

    def _do_start(self, *, candidate_id: str, confirmation: str,
                  account_confirmation: str) -> dict[str, Any]:
        if confirmation != START_PHRASE:
            return self._refuse("start_confirmation_mismatch", f"Type {START_PHRASE!r} exactly. Nothing started.")
        if not self._armed_by_host or self._lifecycle is None or not bool(getattr(self.adapter, "armed", False)):
            return self._refuse("ax_scalper_not_armed", "Arm AX-SCALPER for the verified account first.")
        if candidate_id != self._candidate_id:
            return self._refuse("candidate_changed_after_arm", "The selected strategy differs from the one explicitly armed. Arm the exact candidate again.")
        if (self._expected is None or self._bound_mode not in {"DEMO", "REAL"}
                or account_confirmation != f"{self._bound_mode} {self._expected.fingerprint}"):
            return self._refuse("account_confirmation_mismatch", "Repeat the displayed account type and fingerprint exactly.")
        if self._state not in {ARMED, PAUSED}:
            return self._refuse("ax_scalper_not_armed", "Start requires the exact AX-SCALPER arm state; arm or recover that lane first.")
        account_blocker = self._check_live_account(self._expected, account_confirmation=account_confirmation,
                                                   expected_mode=self._bound_mode)
        if account_blocker:
            return account_blocker
        prepared = self._prepare_candidate(candidate_id)
        if not prepared[0]:
            return prepared[1]
        _config_view, currency, expected, strategy, evidence, policy_version, fingerprints, binding = prepared[1]
        if expected != self._expected or evidence != (self._admitted.evidence_digest if self._admitted else ""):
            return self._refuse("candidate_or_account_changed_after_arm", "The signed evidence or account declaration changed after arming. Stop and review again.")
        if self._other_live_session_running():
            return self._refuse("another_live_session_running", "Stop the other LIVE session before starting AX-SCALPER.")
        if not self._lifecycle_active(self._lifecycle):
            return self._refuse("admission_revoked", "The exact signed evidence changed or is no longer active.")
        if self._runtime is not None and self._runtime.state == PAUSED:
            outcome = self._runtime.resume()
            if not outcome.get("ok"):
                self._state = str(outcome.get("state") or self._runtime.state)
                return self._remember(outcome)
            self._state, self._reason = RUNNING, ""
            self._audit("AX_SCALPER_LIVE_RESUMED", candidate_id=candidate_id,
                        evidence_digest=evidence, account_fingerprint=expected.fingerprint)
            return self._remember({"ok": True, "state": RUNNING,
                                   "message": "The exact paused AX-SCALPER run resumed after account, evidence and risk checks. No new run or order was created by Resume."})
        try:
            runtime = self._build_runtime(expected=expected, strategy=strategy,
                                          evidence=evidence, policy_version=policy_version)
        except Exception as exc:
            return self._refuse(str(getattr(exc, "reason", "live_runner_build_refused")),
                                str(exc) or type(exc).__name__)
        if runtime is None:
            return self._refuse("live_runner_build_refused", "The guarded LIVE runner could not be built.")
        outcome = runtime.start()
        if not outcome.get("ok"):
            self._runtime = runtime
            self._state = str(outcome.get("state") or runtime.state)
            self._reason = str(outcome.get("reason") or "start_refused")
            return self._remember(outcome)
        self._runtime = runtime
        self._state, self._reason = RUNNING, ""
        self._audit("AX_SCALPER_LIVE_STARTED", candidate_id=candidate_id,
                    starting_equity=str(runtime.starting_equity), currency=currency)
        return self._remember({"ok": True, "state": RUNNING,
                               "message": f"AX-SCALPER LIVE is running on the verified {self._bound_mode} account. It manages open positions; Stop prevents new entries and does not close them."})

    def _do_stop(self) -> dict[str, Any]:
        if self._runtime is None:
            self._release_arm()
            self._state, self._reason = STOPPED, "operator_stop"
            return self._remember({"ok": True, "state": STOPPED,
                                   "message": "No AX-SCALPER run was active. The adapter is disarmed if this lane armed it; no position was closed."})
        result = dict(self._runtime.stop(reason="operator_stop", close_positions=False))
        self._state = str(result.get("state") or STOPPED)
        self._reason = "operator_stop"
        try:
            broker_positions = self._runtime.venue.position_details()
            result["broker_positions"] = broker_positions
            positions_read = True
        except Exception as exc:
            broker_positions = []
            positions_read = False
            result["position_read_error"] = type(exc).__name__
        still_open = bool(result.get("open_positions") or broker_positions or not positions_read)
        if not still_open:
            self._release_arm()
            self._release_flat_runtime()
        self._audit("AX_SCALPER_LIVE_STOPPED", open_positions=len(result.get("open_positions") or ()))
        result["message"] = str(result.get("message") or "No new entry will be submitted.")
        if still_open and result.get("state") == RECOVERY_REQUIRED:
            result["message"] += (
                " Local management is paused during recovery; verify broker protection independently."
            )
        elif still_open:
            result["message"] += (
                " The owner thread will attempt local management while connected and "
                "all account, quote and execution guards pass. Broker protection must "
                "be verified separately; it is not assumed by this stop response."
            )
        if result.get("state_persisted") is False:
            result["ok"] = False
            result["reason"] = "run_state_persistence_failed"
            result["message"] += " The stop state could not be durably saved; exact recovery is required."
        return self._remember(result)

    def _do_shutdown(self) -> dict[str, Any]:
        """End the owner thread without closing positions or leaving it armed."""
        result = self._do_stop()
        self._release_arm()
        if self._journal is not None:
            self._journal.close()
            self._journal = None
        if self._run_lock is not None:
            self._run_lock.close()
            self._run_lock = None
        result["message"] = (
            "Shutdown requested: new entries stopped and this lane was disarmed; no "
            "position was closed. Verify broker-side protection and recover the exact "
            "durable state before resuming."
            + (" The stop state could not be durably saved." if result.get("state_persisted") is False else "")
            + (" Local management stops with the owner thread." if result.get("ok") else
               " The owner thread was not confirmed stopped; inspect its status.")
        )
        return result

    def _do_reconcile(self) -> dict[str, Any]:
        if self._runtime is None:
            return self._refuse("live_run_missing", "There is no in-memory AX-SCALPER run to reconcile. The durable run state was left untouched.")
        if self._expected is None:
            return self._refuse("live_account_identity_unavailable", "The account bound to this run is unavailable; nothing was reconciled.")
        account_blocker = self._check_live_account(
            self._expected, account_confirmation=f"{self._bound_mode} {self._expected.fingerprint}",
            expected_mode=self._bound_mode
        )
        if account_blocker:
            return account_blocker
        result = dict(self._runtime.reconcile_submission())
        self._state = str(result.get("state") or self._runtime.state)
        self._reason = str(result.get("reason") or "")
        return self._remember(result)

    def _do_reconcile_close(self, *, position_id: str) -> dict[str, Any]:
        if self._runtime is None:
            return self._refuse("live_run_missing", "There is no in-memory AX-SCALPER run to reconcile. Durable state was left untouched.")
        if self._expected is None:
            return self._refuse("live_account_identity_unavailable", "The account bound to this run is unavailable; nothing was reconciled.")
        account_blocker = self._check_live_account(
            self._expected, account_confirmation=f"{self._bound_mode} {self._expected.fingerprint}",
            expected_mode=self._bound_mode
        )
        if account_blocker:
            return account_blocker
        result = dict(self._runtime.reconcile_close(position_id=position_id))
        self._state = str(result.get("state") or self._runtime.state)
        self._reason = str(result.get("reason") or "")
        return self._remember(result)

    def _prepare_recovery_runtime(self) -> dict[str, Any]:
        """Build a non-arming run from saved identities; never reconnect or send."""
        if self._runtime is None:
            try:
                self._bind_account_state()
            except Exception:
                return self._refuse("account_run_state_unavailable", "The account-specific recovery state could not be verified. Preserve every saved run.")
        if self._runtime is not None:
            run = self._runtime
            if run.state in {RUNNING, ARMED} and not run._reconciliation_only:
                return self._refuse("active_run_not_recovery_only", "Pause or stop and preserve the active run before exact reconciliation.")
            if self._armed_by_host:
                self._release_arm()
            try:
                state = self.state_store.load()
                result = run.recover(state=state, reconciliation_only=True)
            except Exception as exc:
                return self._refuse("recovery_revalidation_failed", f"Saved evidence was preserved ({type(exc).__name__}).")
            self._state = RECOVERY_REQUIRED
            return {"ok": True, "prepared": bool(result.get("prepared")),
                    "reason": "recovery_runtime_revalidated", "recovery": dict(run._recovery)}
        if self._other_live_session_running() or bool(getattr(self.adapter, "armed", False)):
            return self._refuse("other_live_session_active", "Stop and disarm the other LIVE session before reconciliation; no order was sent.")
        try:
            state = self.state_store.load()
        except Exception:
            return self._refuse("stored_run_state_unreadable", "Preserve the run state and broker records; no query was made.")
        if not isinstance(state, Mapping):
            return self._refuse("stored_run_state_missing", "No valid run state is available. Preserve the journal and broker records.")
        admission = state.get("admission")
        account = state.get("account_identity")
        if not isinstance(admission, Mapping) or not isinstance(account, Mapping):
            return self._refuse("stored_identity_missing", "The saved account or admission identity is incomplete.")
        candidate = str(admission.get("candidate_id") or "")
        prepared = self._prepare_candidate(candidate)
        if not prepared[0]:
            return prepared[1]
        _, _, expected, strategy, evidence, policy_version, fingerprints, binding = prepared[1]
        mode = str(account.get("account_mode") or "")
        if (mode not in {"DEMO", "REAL"} or account.get("fingerprint") != expected.fingerprint
                or account.get("server") != expected.server or account.get("currency") != expected.currency
                or admission.get("evidence_digest") != evidence or admission.get("policy_version") != policy_version):
            return self._refuse("stored_identity_mismatch", "The exact saved account, strategy, evidence or policy does not match. Nothing was adopted.")
        try:
            self.adapter.refresh_account()
            verdict = verify_mt5_account(dict(self.adapter.status() or {}), expected, expected_mode=mode)
        except Exception:
            return self._refuse("mt5_account_refresh_failed", "Connect explicitly to the original account; no reconnect was attempted.")
        if not verdict.ok:
            return self._refuse("account_identity_mismatch", verdict.detail or "The connected account is not the saved account.")
        self._candidate_id = candidate
        self._strategy_id = f"ax-scalper-live:{strategy.fingerprint}"
        self._expected, self._bound_mode = expected, mode
        self._lifecycle = _AdmissionLifecycle(self, self._strategy_id, fingerprints, **binding)
        try:
            run = self._build_runtime(expected=expected, strategy=strategy, evidence=evidence,
                                      policy_version=policy_version, reconciliation_only=True)
        except Exception as exc:
            return self._refuse("recovery_runtime_unavailable", f"Saved state was preserved; recovery preparation failed ({type(exc).__name__}: {exc}).")
        self._runtime = run
        self._state = RECOVERY_REQUIRED
        return {"ok": True, "prepared": bool(run._recovery_prepared),
                "reason": "recovery_runtime_loaded", "recovery": dict(run._recovery)}

    def _inspect_history_request(self, run: AutonomousScalper, journal: MutationJournal,
                                 leg: Any, *, expected: ExpectedAccount,
                                 symbol: str, strategy_id: str, request_id: str) -> dict[str, Any]:
        from ..scalper.history_recovery import inspect_exact_history
        try:
            rows = [row for row in journal.unresolved() if row["request_id"] == request_id]
            record = leg.repository.get_order(request_id)
            internal_symbols = [key for key, value in run.symbols.items() if value == symbol]
            if (len(rows) != 1 or len(internal_symbols) != 1 or not isinstance(record, Mapping)
                    or rows[0]["identity"] != run._journal_identity(internal_symbols[0])
                    or str(record.get("account_login") or "") != str(expected.login)
                    or int(record.get("magic") or 0) != int(self.adapter.settings.magic)):
                return {"resolved": False, "reason": "durable_request_binding_mismatch"}
            check = lambda: self._matches_durable_request(record, expected=expected,
                                                          symbol=symbol, strategy_id=strategy_id)
            return inspect_exact_history(adapter=self.adapter, order=record, journal=rows[0],
                                         account_matches=check)
        except Exception as exc:
            return {"resolved": False, "reason": "history_inspection_failed:" + type(exc).__name__}

    def _do_reconcile_request(self, *, request_id: str) -> dict[str, Any]:
        if not request_id:
            return self._refuse("request_id_required", "Select the original durable request id; no mutation was sent.")
        prepared = self._prepare_recovery_runtime()
        if not prepared.get("ok"):
            return prepared
        run = self._runtime
        if run is None or not run._reconciliation_only or not run._recovery_prepared or self._expected is None:
            return self._refuse("recovery_state_not_validated", "The saved state did not pass exact restoration checks; preserve it and broker records.")
        try:
            self.adapter.refresh_account()
            if not verify_mt5_account(dict(self.adapter.status() or {}), self._expected,
                                      expected_mode=self._bound_mode).ok:
                return self._refuse("account_identity_mismatch", "The saved account is not currently verified.")
            records = [row for row in self._journal.unresolved() if row["request_id"] == request_id]
        except Exception:
            return self._refuse("journal_or_account_unavailable", "The exact request and account could not be verified.")
        if len(records) != 1:
            return self._refuse("request_not_unique_or_not_pending", "The requested identity is absent, duplicated or already resolved.")
        row = records[0]
        snapshot_has_request = (
            str((run._pending_submission or {}).get("request_id") or "") == request_id
            or any(item.get("request_id") == request_id for item in run.book.pending_closes)
            or any(item.get("request_id") == request_id
                   for item in run.book.snapshot()["pending_protection_modifications"])
        )
        if snapshot_has_request and row["action"] in {"entry", "close"}:
            existing = run.venue.query(request_id)
            if existing is None or str(getattr(existing, "status", "")) == "unknown_outcome":
                symbol = str(row["identity"].get("symbol") or "")
                proof = self._inspect_history_request(run, self._journal, self._leg,
                    expected=self._expected, symbol=symbol, strategy_id=self._strategy_id,
                    request_id=request_id)
                if not proof.get("resolved"):
                    return self._remember({"ok": False, "resolved": False, "request_id": request_id,
                        "reason": proof.get("reason") or "broker_history_inconclusive",
                        "message": "The original request remains blocked; no broker mutation was sent."})
                record = self._leg.repository.get_order(request_id)
                check = lambda: self._matches_durable_request(record, expected=self._expected,
                    symbol=symbol, strategy_id=self._strategy_id)
                if not self._leg.commit_scalper_history(record=record, proof=proof, account_matches=check):
                    return self._remember({"ok": False, "resolved": False, "request_id": request_id,
                        "reason": "history_ledger_settlement_failed",
                        "message": "The original request remains blocked; no broker mutation was sent."})
        if not snapshot_has_request:
            try:
                run._settle_durable_mutations()
                result = {"ok": True, "resolved": True, "reason": "exact_saved_state_rechecked"}
            except Exception:
                result = {"ok": False, "resolved": False, "reason": "durable_recheck_failed"}
        elif row["action"] == "entry" and str((run._pending_submission or {}).get("request_id") or "") == request_id:
            result = dict(run.reconcile_submission())
        elif row["action"] == "close":
            matches = [item for item in run.book.pending_closes if item.get("request_id") == request_id]
            if len(matches) != 1:
                result = {"ok": False, "resolved": False, "reason": "pending_close_identity_mismatch"}
            else:
                result = dict(run.reconcile_close(position_id=str(matches[0].get("position_id") or "")))
        elif row["action"] == "protection":
            result = dict(run.reconcile_protection(request_id=request_id))
        else:
            result = {"ok": False, "resolved": False, "reason": "snapshot_request_action_mismatch"}
        try:
            self.adapter.refresh_account()
            same_account = verify_mt5_account(dict(self.adapter.status() or {}), self._expected,
                                               expected_mode=self._bound_mode).ok
            remaining = self._journal.unresolved()
        except Exception:
            same_account, remaining = False, [row]
        resolved = same_account and not any(item["request_id"] == request_id for item in remaining)
        if not resolved:
            result.update(ok=False, resolved=False, request_id=request_id,
                          reason=str(result.get("reason") or "request_still_unresolved"),
                          message="The exact request remains blocked. No broker mutation was sent; preserve the records.")
            self._state = RECOVERY_REQUIRED
            return self._remember(result)
        if not remaining and not run.book.pending_closes and run._pending_submission is None and not run.book.snapshot()["pending_protection_modifications"]:
            self._close_platform(self._platform)
            self._journal.close()
            self._runtime = self._platform = self._leg = self._journal = None
            if self._run_lock is not None:
                self._run_lock.close()
                self._run_lock = None
            self._state = STOPPED
            self._reason = "reconciled_explicit_rearm_required"
        return self._remember({"ok": True, "resolved": True, "request_id": request_id,
            "state": self._state, "reason": "exact_request_reconciled",
            "message": "The same request was reconciled without sending again. Explicit Arm and Start are still required."})

    # -- build and admission ------------------------------------------

    def _configuration_review(self) -> dict[str, Any]:
        try:
            config, currency, expected = load_live_scalper_config(
                session=self.session, settings_path=self.settings_path
            )
            spec = config.instruments[0]
            costs = {"currency": currency, **config.cost.audit_view()}
            review = self.admission_host._strategy_review(
                currency=currency, instrument=spec.audit_view(),
                expected_costs=costs, config=config,
            )
            candidates = list(review.get("candidates") or ())
            eligible = [item for item in candidates if item.get("eligible") is True]
            blockers = []
            adapter_status = self._adapter_status()
            if not self.session.connected:
                blockers.append(self._blocker("terminal_not_connected", "Connect to MT5 explicitly on Setup.", "No background connection is attempted."))
            verdict = verify_mt5_account(adapter_status, expected, expected_mode=self._bound_mode if self._armed_by_host else None)
            if not verdict.ok:
                blockers.append(self._blocker("account_identity_mismatch",
                                              verdict.detail or "The current terminal account does not match the declared account.",
                                              "Sign in to the declared DEMO or REAL account in MT5, then use Connect."))
            if str(getattr(self.mode, "mode", "") or "").upper() != LIVE or not bool(getattr(self.mode, "live_effective", False)):
                blockers.append(self._blocker("live_mode_not_confirmed", "LIVE mode is not selected and confirmed.", "Confirm LIVE mode on the Trading mode page."))
            if not eligible:
                blockers.append(self._blocker("no_admitted_candidate", "No candidate currently passes the signed policy and independent raw-data replay.", "No strategy may trade until one passes; sample/test results do not count."))
            if self._other_live_session_running():
                blockers.append(self._blocker("another_live_session_running", "Another LIVE strategy session is running.", "Stop the other LIVE session before arming AX-SCALPER."))
            if bool(adapter_status.get("armed")) and not self._armed_by_host:
                blockers.append(self._blocker("live_adapter_already_armed", "The guarded MT5 adapter is already armed by another LIVE workflow.", "Disarm that workflow before arming AX-SCALPER."))
            return {"ok": True, "config": {"symbol": spec.symbol, "pair": spec.pair,
                    "account_currency": currency,
                    "risk": {key: str(getattr(config, key)) for key in RISK_KEYS},
                    "costs": costs, "instrument": spec.audit_view(),
                    "expected_account": expected.audit_view()},
                    "review": dict(review), "candidates": candidates,
                    "eligible_candidates": eligible, "blockers": blockers}
        except LiveScalperConfigError as exc:
            return {"ok": False, "reason": exc.reason, "config": {}, "review": {},
                    "candidates": [], "eligible_candidates": [],
                    "blockers": [self._blocker(exc.reason, str(exc), exc.remediation)]}
        except Exception as exc:
            return {"ok": False, "reason": "live_admission_review_unavailable", "config": {},
                    "review": {}, "candidates": [], "eligible_candidates": [],
                    "blockers": [self._blocker("live_admission_review_unavailable",
                                  f"The evidence review could not be read ({type(exc).__name__}).",
                                  "Repair the signed policy/evidence and reload; no candidate is assumed approved.")]}

    def _prepare_candidate(self, candidate_id: str):
        preview = self._configuration_review()
        if not preview.get("ok"):
            return False, self._refuse(str(preview.get("reason") or "live_settings_invalid"),
                                       str((preview.get("blockers") or [{}])[0].get("hint") or "Correct the local LIVE settings."),
                                       blockers=preview.get("blockers") or ())
        candidate = next((item for item in preview.get("eligible_candidates", ())
                          if str(item.get("fingerprint") or "") == candidate_id), None)
        if candidate is None:
            return False, self._refuse("no_admitted_candidate", "Select an exact candidate that currently passes signed evidence and independent raw replay.",
                                       blockers=preview.get("blockers") or ())
        evidence = dict(candidate.get("evidence") or {})
        digest = str(evidence.get("evidence_digest") or "")
        if not digest:
            return False, self._refuse("evidence_digest_missing", "The admission review did not return the exact signed artifact digest.")
        strategy = next((item for item in default_strategies()
                         if item.fingerprint == candidate_id), None)
        if strategy is None:
            return False, self._refuse("strategy_fingerprint_unknown", "The candidate no longer matches a built-in AX-SCALPER strategy.")
        policy_version = self._sha256_file(Path(str(preview["review"].get("policy_path") or "")))
        if not policy_version:
            return False, self._refuse("admission_policy_digest_unavailable", "The signed-admission policy could not be fingerprinted.")
        artifact_path = Path(str(candidate.get("artifact_path") or ""))
        policy_path = Path(str(preview["review"].get("policy_path") or ""))
        try:
            # Admission's identity hashes canonical signed claims *and policy*,
            # not the raw file. Keep a separate byte hash for lifecycle checks.
            artifact_bytes = artifact_path.read_bytes()
            artifact_document = json.loads(artifact_bytes)
            policy = load_policy(policy_path, expected_currency=preview["config"]["account_currency"])
            current_digest = evidence_digest(artifact_document, policy)
            artifact_digest = hashlib.sha256(artifact_bytes).hexdigest()
        except (OSError, ValueError, TypeError, AdmissionError):
            return False, self._refuse("admission_binding_unavailable", "The evidence or policy could not be re-read and fingerprinted.")
        if current_digest != digest or self._sha256_file(policy_path) != policy_version:
            return False, self._refuse("evidence_digest_changed", "The candidate evidence or policy changed after it was evaluated.")
        try:
            data = artifact_document["data"]
            replay_source_digest = str(artifact_document["replay_config"]["strategy_source_digest"])
            if not replay_source_digest or replay_source_digest != source_digest():
                raise ValueError("replay source changed")
            root = artifact_path.resolve(strict=True).parent
            data_path = root / str(data["file"])
            resolved = data_path.resolve(strict=True)
            end = datetime.fromisoformat(str(data["end"]))
            if (data_path.is_symlink() or not resolved.is_relative_to(root)
                    or not resolved.is_file() or end.tzinfo is None):
                raise ValueError("unsafe data path or time")
            raw_digest = self._sha256_file(data_path)
            if not raw_digest or raw_digest != str(data["sha256"]).lower():
                raise ValueError("data digest changed")
            expires_at = end + timedelta(days=policy.maximum_artifact_age_days + 1)
            if datetime.now(UTC) < end or datetime.now(UTC) >= expires_at:
                raise ValueError("data outside admission age")
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            return False, self._refuse("evidence_data_binding_invalid", "The independently replayed dataset is missing, changed, unsafe or outside the admission age.")
        fingerprints = {
            str(self.settings_path): self._sha256_file(self.settings_path),
            str(policy_path): policy_version,
            str(artifact_path): artifact_digest,
            str(data_path): raw_digest,
        }
        for path in (getattr(self.live_workflow, "setup_path", None),
                     getattr(self.live_workflow, "settings_path", None)):
            if path is not None:
                fingerprints[str(path)] = self._sha256_file(Path(path))
        if not all(fingerprints.values()):
            return False, self._refuse("admission_binding_unavailable", "The local evidence/configuration bindings could not be verified.")
        binding = {"data_path": data_path, "evidence_root": root, "expires_at": expires_at,
                   "replay_source_digest": replay_source_digest}
        return True, (preview["config"], preview["config"]["account_currency"],
                      ExpectedAccount(**preview["config"]["expected_account"]), strategy,
                      digest, policy_version, fingerprints, binding)

    # -- run construction and polling ---------------------------------

    def _matches_durable_request(self, record: Mapping[str, Any], *,
                                 expected: ExpectedAccount, symbol: str,
                                 strategy_id: str) -> bool:
        """Attribute an exact durable outcome to this run, or keep it unknown."""
        try:
            if self._bound_mode not in {"DEMO", "REAL"}:
                return False
            self.adapter.refresh_account()
            verdict = verify_mt5_account(dict(self.adapter.status() or {}), expected,
                                         expected_mode=self._bound_mode)
            return (verdict.ok and str(record.get("broker_id") or "").lower() == "mt5_terminal"
                    and str(record.get("account_fingerprint") or "") == expected.fingerprint
                    and str(record.get("account_server") or "") == expected.server
                    and str(record.get("account_mode") or "") == verdict.mode
                    and str(record.get("account_currency") or "") == expected.currency
                    and str(record.get("provider_symbol") or "") == symbol
                    and str(record.get("strategy_id") or "") == strategy_id)
        except Exception:
            return False

    def _build_runtime(self, *, expected: ExpectedAccount,
                       strategy: Any, evidence: str, policy_version: str,
                       reconciliation_only: bool = False) -> AutonomousScalper:
        if self._run_lock is not None:
            raise ValueError("live_run_owner_already_exists")
        # A single owner across all account-scoped runs, also compatible with
        # the original legacy owner lock held by earlier workstation versions.
        lock = RunLock(str(self.account_states.legacy) + ".owner.lock")
        self._run_lock = lock
        try:
            return self._build_runtime_owned(expected=expected, strategy=strategy,
                evidence=evidence, policy_version=policy_version, reconciliation_only=reconciliation_only)
        except BaseException:
            self._run_lock = None
            lock.close()
            raise

    def _build_runtime_owned(self, *, expected: ExpectedAccount,
                             strategy: Any, evidence: str, policy_version: str,
                             reconciliation_only: bool = False) -> AutonomousScalper:
        from ..execution.mode_router import ExecutionRouter
        from .live import LiveLeg

        if self._bound_mode not in {"DEMO", "REAL"}:
            raise ValueError("bound_account_mode_missing")
        config, currency, parsed_expected = load_live_scalper_config(
            session=self.session, settings_path=self.settings_path
        )
        if parsed_expected != expected:
            raise ValueError("live_account_declaration_changed")
        spec = config.instruments[0]
        platform = self.platform_factory(spec.pair)
        if str(getattr(platform, "broker_symbol", "")) != spec.symbol:
            self._close_platform(platform)
            raise ValueError("provider_symbol_identity_mismatch")
        if str(getattr(getattr(platform, "spec", None), "canonical_pair", "") or "") != spec.pair:
            self._close_platform(platform)
            raise ValueError("provider_pair_identity_mismatch")
        if self._lifecycle is None:
            self._close_platform(platform)
            raise ValueError("live_admission_lifecycle_missing")
        symbol = spec.symbol
        strategy_id = self._strategy_id
        position_reader = lambda: [item.audit_view() for item in self.adapter.positions(symbol=symbol)]

        def protection_reader(ticket: str):
            matches = [item for item in self.adapter.positions(symbol=symbol)
                       if str(item.ticket) == str(ticket)]
            if len(matches) != 1:
                return None, None
            item = matches[0]
            target = item.take_profit
            return item.stop_loss, target if target is not None and Decimal(str(target)) > 0 else None

        requirement = lambda: self.adapter.stop_requirement(
            platform.spec, symbol=symbol
        )
        run_holder: list[AutonomousScalper] = []
        journal_holder: list[MutationJournal] = []
        def query_guard(record: Mapping[str, Any]) -> bool:
            if not self._matches_durable_request(record, expected=expected,
                                                 symbol=symbol, strategy_id=strategy_id):
                return False
            if not run_holder or not journal_holder:
                return False
            request_id = str(record.get("request_id") or "")
            matching = [item for item in journal_holder[0].list_all() if item["request_id"] == request_id]
            return (len(matching) == 1
                    and matching[0]["identity"] == run_holder[0]._journal_identity(symbol))

        venue = BrokerVenue(
            leg=None,
            plan_factory=BrokerPlanFactory(platform=platform, strategy_id=strategy_id),
            protection_reader=protection_reader,
            position_reader=position_reader,
            clock_ms=lambda: int(datetime.now(UTC).timestamp() * 1000),
            symbols={symbol: symbol},
            stop_requirement=requirement,
            query_guard=query_guard,
        )
        leg = LiveLeg(
            router=ExecutionRouter(mode=self.mode, paper=None, live=self.adapter,
                                   audit=getattr(platform, "audit_log", None)),
            adapter=self.adapter, gate=getattr(platform, "gate", None), mode=self.mode,
            spec=platform.spec, strategy_id=strategy_id,
            repository=getattr(platform, "repository", None),
            audit_log=getattr(platform, "audit_log", None),
            lifecycle=self._lifecycle, portfolio=getattr(platform, "portfolio", None),
            rollover_cutoffs=tuple(getattr(getattr(getattr(platform, "setup", None),
                                               "rollover", None), "cutoffs", ()) or ()),
            expected_account=expected, required_account_mode=self._bound_mode,
        )
        venue.leg = leg
        guardian = Guardian(
            GuardianLimits.from_config(config),
            declared_slippage_points=config.cost.slippage_points,
        )
        book = PositionBook(
            venue=venue, settings=ManagementSettings.from_config(config),
            specs={symbol: spec},
        )
        trader = Trader(
            venue=venue, guardian=guardian, cost=config.cost,
            max_quote_age_ms=config.max_quote_age_ms, specs={symbol: spec},
            signal_ttl_ms=max(1, min(config.max_quote_age_ms, 750)),
            engine="AX-SCALPER-LIVE", live_venue_ok=True,
        )
        current = self._current_funds()
        if current is None:
            self._close_platform(platform)
            raise ValueError("real_account_balance_or_equity_unavailable")
        store_state = self.state_store.load()
        starting_equity = current[1]
        if store_state is not None:
            try:
                stored_equity = Decimal(str(store_state.get("starting_equity")))
            except (InvalidOperation, TypeError, ValueError):
                stored_equity = Decimal(0)
            if not stored_equity.is_finite() or stored_equity <= 0:
                self._close_platform(platform)
                raise ValueError("stored_starting_equity_invalid")
            starting_equity = stored_equity
        if store_state is not None and not self.journal_path.is_file():
            self._close_platform(platform)
            raise ValueError("stored_mutation_journal_missing")
        if store_state is None and self.journal_path.exists():
            self._close_platform(platform)
            raise ValueError("run_state_missing_with_journal")
        try:
            journal = MutationJournal(self.journal_path, create=store_state is None)
        except Exception:
            self._close_platform(platform)
            raise
        journal_holder.append(journal)
        run = AutonomousScalper(
            adapter=self.adapter, mode=self.mode, expected_account=expected,
            admitted=AdmittedStrategy(strategy=strategy, candidate_id=strategy.fingerprint,
                                      evidence_digest=evidence, policy_version=policy_version,
                                      approved_at=""),
            venue=venue, trader=trader, guardian=guardian, book=book,
            core=TickCore(
                instruments=[spec], max_quote_age_ms=config.max_quote_age_ms,
                max_spread_points=config.max_spread_points,
                gap_threshold_ms=config.gap_threshold_ms,
                spread_expansion_multiple=config.spread_expansion_multiple,
                tick_window=config.tick_window, min_window_ticks=config.min_window_ticks,
                duplicate_window=config.duplicate_window,
                clock_reset_threshold_ms=config.clock_reset_threshold_ms,
                resync_ticks=config.resync_ticks,
            ),
            specs={symbol: spec}, symbols={symbol: symbol},
            max_open_positions=config.max_open_positions,
            cooldown_ms=config.cooldown_ms, decision_interval_ms=0,
            starting_equity=starting_equity, risk_fraction=config.risk_fraction,
            audit_log=self.audit_log, state_store=self.state_store,
            session_running=self._other_live_session_running,
            approval_active=self._lifecycle.approve_active,
            expected_account_mode=self._bound_mode, mutation_journal=journal,
        )
        if store_state is not None:
            # The normal recovery validator still checks format and every other
            # identity before adopting the book; the query guard needs the
            # declared run identity to locate its journal records first.
            run.run_id = str(store_state.get("run_id") or "")
        run_holder.append(run)
        leg.scalper_ledger_identity = lambda: run._journal_identity(symbol)
        venue.history_preview = lambda request_id: self._inspect_history_request(
            run, journal, leg, expected=expected, symbol=symbol,
            strategy_id=strategy_id, request_id=request_id)
        if store_state is not None:
            # The provider platform is constructed empty on every restart.
            # Restore only the account/run-bound ledger committed atomically
            # with its exact booking; never synthesize it from a broker echo.
            try:
                ledger_rows = [row for row in platform.repository.list_snapshots(kind="ax-scalper-live-ledger")
                               if row.get("snapshot_id") == "ax-scalper-live-ledger:" + run.run_id]
                if len(ledger_rows) > 1:
                    raise ValueError("duplicate_scalper_ledger")
                if ledger_rows:
                    ledger_row = ledger_rows[0]
                    if (ledger_row.get("identity") != run._journal_identity(symbol)
                            or not isinstance(ledger_row.get("ledger"), Mapping)):
                        raise ValueError("scalper_ledger_identity_mismatch")
                    platform.portfolio.restore_snapshot(ledger_row["ledger"])
                    leg.scalper_ledger_head = str(ledger_row.get("request_id") or "")
                    if not leg.scalper_ledger_head:
                        raise ValueError("scalper_ledger_head_missing")
            except Exception:
                journal.close()
                self._close_platform(platform)
                raise
            recovered = run.recover(state=store_state, reconciliation_only=reconciliation_only)
            if not recovered.get("ok") and not reconciliation_only:
                journal.close()
                self._close_platform(platform)
                raise ValueError("exact_live_recovery_refused:" + str(recovered.get("recovery") or {}))
        else:
            try:
                existing = list(self.adapter.positions() or ())
            except Exception as exc:
                journal.close()
                self._close_platform(platform)
                raise ValueError("broker_positions_unreadable") from exc
            status = dict(self.adapter.status() or {})
            if existing or status.get("unresolved_orders") or status.get("unresolved_protection_modifications") or status.get("unattributed_orders"):
                journal.close()
                self._close_platform(platform)
                raise ValueError("unattributed_broker_exposure_or_order_present")
        self._platform, self._leg, self._journal = platform, leg, journal
        return run

    def _cycle(self) -> None:
        run = self._runtime
        if run is None:
            return
        if run.state == STOPPED and not run.book.open_positions:
            return
        if run.state not in {RUNNING, PAUSED, STOPPED}:
            return
        if run.state != RUNNING and not run.book.open_positions:
            return
        try:
            self.adapter.refresh_account()
            status = dict(self.adapter.status() or {})
            funds = self._funds_from_status(status)
            if funds is None:
                raise ValueError("real_account_balance_or_equity_unavailable")
            source = self.session.market_data_source()
            if source is None or not self.session.connected:
                raise ValueError("terminal_disconnected")
            symbol = run.specs[next(iter(run.specs))].symbol
            broker_symbol = run.symbols[symbol]
            tick = source.tick(broker_symbol)
            now = datetime.now(UTC)
            now_ms = int(now.timestamp() * 1000)
            result = run.on_tick(symbol=symbol, bid=tick.bid, ask=tick.ask,
                                 source_time=tick.time, now_ms=now_ms,
                                 equity=funds[1], balance=funds[0])
            age = max(0, now_ms - int(tick.time.timestamp() * 1000))
            self._last_tick = {"symbol": broker_symbol, "bid": str(tick.bid), "ask": str(tick.ask),
                               "source_time": tick.time.astimezone(UTC).isoformat(),
                               "received_at": now.isoformat(), "age_ms": age,
                               "fresh": age <= int(run.guardian.limits.max_quote_age_ms),
                               "accepted": bool(result.get("tick")),
                               "reason": str(result.get("blocked") or "")}
            self._last_result = dict(result)
            self._state = str(result.get("state") or run.state)
            if run.state == RUNNING and result.get("blocked") in {"terminal_disconnected", "real_account_balance_or_equity_unavailable"}:
                run.pause(reason=str(result["blocked"]))
            if run.state == STOPPED and not run.book.open_positions:
                positions = run.venue.position_details()
                if not positions:
                    self._release_arm()
                    self._release_flat_runtime()
        except Exception as exc:
            self._reason = str(exc) or type(exc).__name__
            if run.state == RUNNING:
                run.pause(reason=self._reason)
            self._last_tick = {**self._last_tick, "fresh": False,
                               "reason": self._reason, "received_at": datetime.now(UTC).isoformat()}
            self._state = str(run.state)

    # -- validation, account, lifecycle --------------------------------

    def _check_live_account(self, expected: ExpectedAccount, *, account_confirmation: str,
                            expected_mode: str | None) -> dict[str, Any] | None:
        if (expected_mode not in {"REAL", "DEMO"}
                or account_confirmation != f"{expected_mode} {expected.fingerprint}"):
            return self._refuse("account_confirmation_mismatch", "Type the exact displayed account type and fingerprint.")
        if str(getattr(self.mode, "mode", "") or "").upper() != LIVE or not bool(getattr(self.mode, "live_effective", False)):
            return self._refuse("live_mode_not_confirmed", "Confirm LIVE mode on the Trading mode page before arming.")
        try:
            self.adapter.refresh_account()
            status = dict(self.adapter.status() or {})
        except Exception as exc:
            return self._refuse("mt5_account_refresh_failed", f"The already-connected terminal could not refresh its account ({type(exc).__name__}); no reconnect was attempted.")
        verdict = verify_mt5_account(status, expected, expected_mode=expected_mode)
        if not verdict.ok:
            return self._refuse("account_identity_mismatch", verdict.detail or "The terminal did not verify the declared account.")
        if not status.get("trading_enabled_setting"):
            return self._refuse("trading_disabled", "Enable the existing MT5 trading setting and re-review the LIVE lane.")
        return None

    def _other_live_session_running(self) -> bool:
        session = getattr(self.live_workflow, "session", None)
        if session is None:
            return False
        try:
            view = dict(session.view() or {})
            return bool(view.get("available") is False or view.get("running")
                        or view.get("next_signal_sends_real_order")
                        or str(view.get("state") or "").upper() in {
                            "RUNNING", "STARTING", "PREPARING", "PAUSED", "RECOVERY_REQUIRED", "UNAVAILABLE"})
        except Exception:
            return True

    def _lifecycle_active(self, lifecycle: _AdmissionLifecycle) -> bool:
        if lifecycle is not self._lifecycle or not self._armed_by_host:
            return False
        if self._candidate_id == "" or self._strategy_id != lifecycle.strategy_id:
            return False
        if self._other_live_session_running():
            return False
        try:
            if (datetime.now(UTC) >= lifecycle.expires_at
                    or source_digest() != lifecycle.replay_source_digest):
                return False
            if (lifecycle.data_path.is_symlink()
                    or not lifecycle.data_path.resolve(strict=True).is_relative_to(lifecycle.evidence_root)):
                return False
            return all(self._sha256_file(Path(path)) == digest
                       for path, digest in lifecycle.fingerprints.items())
        except Exception:
            return False

    def _release_flat_runtime(self) -> None:
        """Drop a stopped, flat owner only after its journal is known clear."""
        run = self._runtime
        if run is None or run.state != STOPPED or run.book.open_positions:
            return
        try:
            if self._journal is not None and self._journal.unresolved():
                return
        except Exception:
            return
        self._close_platform(self._platform)
        if self._journal is not None:
            self._journal.close()
        self._runtime = self._platform = self._leg = self._journal = None
        if self._run_lock is not None:
            self._run_lock.close()
            self._run_lock = None

    def _release_arm(self) -> None:
        lifecycle = self._lifecycle
        strategy_id = self._strategy_id
        if self._armed_by_host:
            try:
                self.adapter.disarm(reason="ax_scalper_stopped_flat")
            except Exception:
                pass
        registry = getattr(self.live_workflow, "armed_strategy", None)
        if registry is not None and lifecycle is not None:
            try:
                registry.unregister_delegate(strategy_id, lifecycle)
            except Exception:
                pass
        self._armed_by_host = False
        self._lifecycle = None

    # -- helpers -------------------------------------------------------

    def _adapter_status(self) -> dict[str, Any]:
        try:
            return dict(self.adapter.status() or {}) if self.adapter is not None else {}
        except Exception:
            return {}

    def _current_funds(self) -> tuple[Decimal, Decimal] | None:
        try:
            self.adapter.refresh_account()
            return self._funds_from_status(dict(self.adapter.status() or {}))
        except Exception:
            return None

    @staticmethod
    def _funds_from_status(status: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
        terminal = status.get("terminal")
        if not isinstance(terminal, Mapping):
            return None
        try:
            balance = Decimal(str(terminal.get("balance")))
            equity = Decimal(str(terminal.get("equity")))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not balance.is_finite() or not equity.is_finite() or balance <= 0 or equity <= 0:
            return None
        return balance, equity

    @staticmethod
    def _sha256_file(path: Path) -> str:
        try:
            return sha256_file(path)
        except OSError:
            return ""

    @staticmethod
    def _blocker(reason: str, message: str, hint: str) -> dict[str, str]:
        return {"reason": str(reason), "message": str(message), "hint": str(hint)}

    @staticmethod
    def _refuse(reason: str, message: str, *, blockers: Any = ()) -> dict[str, Any]:
        return {"ok": False, "state": BLOCKED, "reason": str(reason),
                "message": str(message), "blockers": list(blockers or ())}

    def _remember(self, result: Mapping[str, Any]) -> dict[str, Any]:
        self._last_result = dict(result)
        if result.get("state"):
            self._state = str(result["state"])
        self._reason = str(result.get("reason") or "")
        return dict(result)

    def _audit(self, event_type: str, **fields: Any) -> None:
        if self.audit_log is not None:
            try:
                self.audit_log.record({"event_type": event_type, "live": True, **fields})
            except Exception:
                pass

    @staticmethod
    def _close_platform(platform: Any) -> None:
        repository = getattr(platform, "repository", None)
        close = getattr(repository, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def close(self) -> dict[str, Any]:
        """Stop entries and disarm this lane before process exit; never flatten."""
        if self._thread is None or not self._thread.is_alive():
            return {"ok": True, "state": self._state}
        result = self._call("shutdown", {}, timeout=45.0)
        if not result.get("ok"):
            # Do not kill the owner thread after a cancelled or in-flight stop.
            # Its state and any continuing management remain explicitly unknown.
            return result
        with self._lock:
            self._closing = True
        self._thread.join(timeout=5.0)
        # The owner thread removed this lane's permission without touching
        # broker positions; fsynced state remains for exact recovery next time.
        return result


__all__ = ["ARM_PHRASE", "START_PHRASE", "ScalperLiveAutonomousHost"]
