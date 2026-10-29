"""One bounded MT5 startup/session cycle.

This is the entry point an operator (or the CLI) uses to *run* the MT5
workflow: connect to the already-running terminal, show and audit the account
mode, verify the configured strategy and risk approval, and — only if a
qualified signal appears and every gate passes — submit at most a fixed number
of orders.

What the cycle will not do:

* it never asks for, reads, or carries a password (the terminal session is the
  one the operator already opened, or the OS vault entry when one is configured);
* it never switches accounts, logs in, or changes MT5 settings;
* it never invents a pair, strategy, volume, stop, risk limit, or order: the
  spec, the strategy, the request, and the gate context are all supplied;
* it never sends a probe order;
* it never keeps a session open beyond its budget, and it always disconnects.

The account is the one the operator has selected in the MT5 terminal.  The
default policy, ``SELECTED_ACCOUNT``, routes eligible orders to that account
whether it is demo or real, using one adapter and one workflow.  Account mode is
detected, shown, and audited — it is reported, never used as a proxy for "may
trade": the trading setting (``TRADING_ENABLED`` + an explicit arming call) is
separate.

``DEMO_ONLY`` remains available as an explicit opt-in for a run that must never
touch a real account; nothing defaults to it.  A switch of account inside the
terminal is detected and audited, keeps the trading authorisation, drops the
previous account's cached state, and revalidates the newly selected account's
own currency, leverage, margin mode, symbols, and execution terms before any
order is routed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from .account import MT5AccountMismatch
from .adapter import MT5SubmitRefused, MT5TradingAdapter
from .terminal import ACCOUNT_MODE_UNCONFIRMED, MT5DataRefused, MT5Error

#: Route to whichever account the terminal has selected (default).
SELECTED_ACCOUNT = "SELECTED_ACCOUNT"
#: Backwards-compatible alias for the same behaviour.
ANY_ACCOUNT = SELECTED_ACCOUNT
#: Explicit opt-in: refuse to arm unless the account is positively demo.
DEMO_ONLY = "DEMO_ONLY"

#: Ceiling on orders per cycle.  A bounded cycle is one decision, not a loop.
MAX_ORDERS_PER_CYCLE = 1


class MT5SessionRefused(Exception):
    """The cycle could not even start (bad policy or missing adapter)."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


@dataclass(frozen=True)
class SessionPolicy:
    """How strictly this run is scoped.  Not a broker or strategy choice."""

    #: ``SELECTED_ACCOUNT`` (default) or an explicit ``DEMO_ONLY`` run.
    account_mode_policy: str = SELECTED_ACCOUNT
    max_orders: int = 1
    max_duration_seconds: float = 60.0
    arm_when_authorised: bool = True
    max_quote_age_seconds: Decimal | None = Decimal("5")

    def validate(self) -> None:
        if self.account_mode_policy not in {SELECTED_ACCOUNT, DEMO_ONLY}:
            raise MT5SessionRefused("invalid_account_mode_policy")
        if (
            not isinstance(self.max_orders, int)
            or isinstance(self.max_orders, bool)
            or self.max_orders < 0
        ):
            raise MT5SessionRefused("invalid_max_orders")
        if self.max_orders > MAX_ORDERS_PER_CYCLE:
            raise MT5SessionRefused(
                "max_orders_exceeds_cycle_limit", str(MAX_ORDERS_PER_CYCLE)
            )
        if not isinstance(self.max_duration_seconds, (int, float)):
            raise MT5SessionRefused("invalid_max_duration")
        if self.max_duration_seconds <= 0:
            raise MT5SessionRefused("invalid_max_duration")


@dataclass(frozen=True)
class SessionCheck:
    name: str
    passed: bool
    detail: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class SessionReport:
    """Everything the operator may see.  No identifiers, balances, or secrets."""

    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    policy: str
    connected: bool
    account: Mapping[str, Any]
    checks: tuple[SessionCheck, ...]
    blockers: tuple[str, ...]
    signal: Mapping[str, Any]
    order: Mapping[str, Any] | None
    account_validation: Mapping[str, Any]
    armed_during_cycle: bool
    submission_attempted: bool
    orders_sent: int
    observe_only: bool
    demo_only_enforced: bool
    trading_enabled_setting: bool
    live_account_involved: bool
    audited_events: tuple[str, ...]
    account_changes: tuple[Mapping[str, Any], ...] = ()
    detail: str = ""

    @property
    def passed(self) -> bool:
        return not self.blockers

    def audit_view(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "connected": self.connected,
            "account": dict(self.account),
            "account_validation": dict(self.account_validation),
            "trading_enabled_setting": self.trading_enabled_setting,
            "checks": [check.audit_view() for check in self.checks],
            "blockers": list(self.blockers),
            "signal": dict(self.signal),
            "order": dict(self.order) if self.order is not None else None,
            "armed_during_cycle": self.armed_during_cycle,
            "submission_attempted": self.submission_attempted,
            "orders_sent": self.orders_sent,
            "observe_only": self.observe_only,
            "demo_only_enforced": self.demo_only_enforced,
            "live_account_involved": self.live_account_involved,
            "audited_events": list(self.audited_events),
            "account_changes": [dict(item) for item in self.account_changes],
            "passed": self.passed,
            "detail": self.detail,
            "balance_reporting": "withheld",
            "credential_reporting": "never",
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _event_list(audit_log: Any) -> list[Mapping[str, Any]]:
    """Read the audit events an in-memory or durable log exposes."""

    events = getattr(audit_log, "events", None)
    if events is None:
        cache = getattr(audit_log, "cache", None)
        events = getattr(cache, "events", None)
    try:
        return list(events or [])
    except TypeError:
        return []


class MT5SessionCycle:
    """Run exactly one bounded cycle against an injected adapter."""

    def __init__(
        self,
        adapter: MT5TradingAdapter,
        *,
        policy: SessionPolicy | None = None,
        spec: Any | None = None,
        strategy_id: str | None = None,
        lifecycle: Any | None = None,
        gate: Any | None = None,
        request_factory: Callable[[Any], Any] | None = None,
        context_factory: Callable[[datetime], Any] | None = None,
        signal_source: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        if not isinstance(adapter, MT5TradingAdapter):
            raise MT5SessionRefused("invalid_mt5_adapter")
        self.adapter = adapter
        self.policy = policy or SessionPolicy()
        self.policy.validate()
        self.spec = spec
        self.strategy_id = strategy_id
        self.lifecycle = lifecycle
        self.gate = gate
        self.request_factory = request_factory
        self.context_factory = context_factory
        self.signal_source = signal_source
        self.clock = clock or _utc_now
        self._audit_start = len(_event_list(adapter.audit_log))
        self._checks: list[SessionCheck] = []
        self._blockers: list[str] = []
        self._signal: dict[str, Any] = {"qualified": False, "reason": "not_evaluated"}
        self._order: dict[str, Any] | None = None
        self._orders_sent = 0
        self._submission_attempted = False
        self._armed_during_cycle = False
        self._connected = False
        self._connect_reason = ""

    # -- helpers --------------------------------------------------------

    def _check(self, name: str, passed: bool, detail: str = "") -> bool:
        self._checks.append(SessionCheck(name=name, passed=passed, detail=detail))
        if not passed:
            self._blockers.append(name if not detail else f"{name}:{detail}")
        return passed

    def _note(self, name: str, detail: str = "", passed: bool = True) -> None:
        """Record information that never blocks a run (e.g. account mode)."""

        self._checks.append(SessionCheck(name=name, passed=passed, detail=detail))

    def _block(self, reason: str, detail: str = "") -> None:
        self._checks.append(SessionCheck(name=reason, passed=False, detail=detail))
        self._blockers.append(reason if not detail else f"{reason}:{detail}")

    def _expired(self, started: datetime) -> bool:
        elapsed = (self.clock() - started).total_seconds()
        return elapsed > float(self.policy.max_duration_seconds)

    def _audited_events(self) -> tuple[str, ...]:
        events = _event_list(self.adapter.audit_log)
        return tuple(
            str(event.get("event_type", "")) for event in events[self._audit_start :]
        )

    # -- the cycle ------------------------------------------------------

    def run(self) -> SessionReport:
        started = self.clock()
        adapter = self.adapter
        demo_policy = self.policy.account_mode_policy == DEMO_ONLY

        # 1. connect (never sends an order)
        try:
            if not adapter.connected:
                adapter.connect()
            self._connected = adapter.connected
            self._check("terminal_connected", self._connected)
        except MT5Error as exc:
            self._connect_reason = exc.reason
            self._block("terminal_connection_failed", exc.reason)
        except Exception as exc:  # defensive: never leak a credential-bearing message
            self._connect_reason = type(exc).__name__
            self._block("terminal_connection_failed", type(exc).__name__)

        account = adapter.redacted_account()
        account_validation: dict[str, Any] = {
            "valid": False,
            "problems": ["account_not_validated"],
            "notes": [],
        }
        if adapter.connected:
            try:
                account = adapter.refresh_account()
                # Read the account *and* validate its own terms: a switch must
                # never inherit the previous account's currency, leverage,
                # margin mode, symbols, or execution terms.
                account_validation = adapter.validate_account_terms()
            except (MT5Error, MT5SubmitRefused) as exc:
                self._block(
                    "account_refresh_failed",
                    getattr(exc, "reason", type(exc).__name__),
                )

        mode = str(account.get("account_mode", ACCOUNT_MODE_UNCONFIRMED))
        mode_confirmed = bool(account.get("account_mode_confirmed"))
        is_demo = mode == "DEMO" and mode_confirmed
        live_involved = mode == "REAL"

        # Account mode is reported and audited; it is not a trading gate unless
        # the operator explicitly asked for a demo-only run.
        self._note(
            "account_mode_detected",
            f"mode={mode},policy={self.policy.account_mode_policy}",
            passed=mode_confirmed,
        )
        if demo_policy:
            self._check(
                "account_mode_is_demo",
                is_demo,
                "" if is_demo else f"detected={mode}",
            )

        if adapter.connected:
            problems = ",".join(account_validation.get("problems", []) or [])
            self._check(
                "account_terms_valid",
                bool(account_validation.get("valid")),
                problems,
            )
            for note in account_validation.get("notes", []) or []:
                self._note("account_note", str(note))

        # 2. configuration must already exist: no invention, no defaults
        settings = adapter.settings
        self._check("settings_valid", settings.validate().valid)
        self._check(
            "trading_mode_declared",
            settings.trading_enabled,
            settings.mode_value,
        )
        if self.spec is None:
            self._block("no_instrument_configured")
        if not self.strategy_id:
            self._block("no_approved_strategy_configured")
        if self.gate is None:
            self._block("no_pretrade_gate_configured")
        if self.request_factory is None:
            self._block("no_order_request_factory_configured")
        if self.context_factory is None:
            self._block("no_gate_context_factory_configured")
        if self.signal_source is None:
            self._block("no_signal_source_configured")

        lifecycle = self.lifecycle
        if lifecycle is None and self.strategy_id and adapter.lifecycle_lookup is not None:
            lifecycle = adapter.lifecycle_lookup(self.strategy_id)
        approved = False
        if lifecycle is not None:
            approve = getattr(lifecycle, "approve_active", None)
            approved = bool(approve()) if callable(approve) else False
        self._check(
            "strategy_approved_active",
            approved,
            str(getattr(lifecycle, "stage", "") or "unknown"),
        )

        if self.spec is not None and adapter.connected:
            try:
                symbol = adapter.symbol_for(self.spec)
                _, cross = adapter.cross_check(self.spec)
                self._check("symbol_resolved", True, str(symbol))
                self._check(
                    "broker_terms_match",
                    bool(cross.trading_ok),
                    ",".join(cross.errors),
                )
            except MT5DataRefused as exc:
                self._block("instrument_not_tradeable", exc.reason)

        unresolved = adapter.unresolved_request_ids() if adapter.connected else []
        self._check(
            "no_unresolved_orders",
            not unresolved,
            ",".join(unresolved),
        )
        if adapter.connected:
            unattributed = adapter.unattributed_blocking_orders()
            if unattributed:
                self._note("unattributed_orders_reported", ",".join(unattributed))

        if self._expired(started):
            self._block("session_time_budget_exceeded")

        # 3. arm only when everything above is clean, and only within policy
        eligible = not self._blockers
        if eligible and self.policy.arm_when_authorised:
            try:
                # The account is the terminal's current selection; the optional
                # acknowledgements are omitted on purpose.
                adapter.arm_trading(confirm="ENABLE LIVE TRADING")
                self._armed_during_cycle = True
                self._check("trading_armed", True)
            except (MT5SubmitRefused, MT5Error) as exc:
                self._block("arming_refused", getattr(exc, "reason", type(exc).__name__))

        # 4. one qualified signal, from the configured strategy only
        decision = None
        intent = None
        if not self._blockers and self._orders_sent < self.policy.max_orders:
            if self._expired(started):
                self._block("session_time_budget_exceeded")
            else:
                try:
                    side = self.signal_source()
                except Exception as exc:
                    side = None
                    self._block("signal_source_failed", type(exc).__name__)
                if side is None and not self._blockers:
                    self._signal = {"qualified": False, "reason": "no_signal"}
                    self._check("signal_qualified", False, "no_signal")
                elif side is not None:
                    self._signal = {
                        "qualified": True,
                        "side": getattr(side, "value", str(side)).upper(),
                        "strategy_id": self.strategy_id,
                    }
                    self._check("signal_qualified", True, str(self._signal["side"]))

        # 5. the existing hard-risk gate, on the configured request
        if self._signal.get("qualified") and not self._blockers:
            try:
                at = self.clock()
                request = self.request_factory(self.spec)
                context = self.context_factory(at)
                decision = self.gate.evaluate(request, context)
                self._check(
                    "risk_decision_allowed",
                    bool(getattr(decision, "allowed", False)),
                    str(getattr(getattr(decision, "code", ""), "value", "")),
                )
                intent = getattr(decision, "order_intent", None)
                if intent is None:
                    self._block("risk_decision_missing_intent")
            except Exception as exc:
                self._block("risk_evaluation_failed", type(exc).__name__)

        # 6. submit through the write-ahead / reconciliation path
        if decision is not None and intent is not None and not self._blockers:
            if self._expired(started):
                self._block("session_time_budget_exceeded")
            else:
                self._submission_attempted = True
                try:
                    receipt = adapter.submit(
                        decision,
                        intent,
                        spec=self.spec,
                        max_quote_age_seconds=self.policy.max_quote_age_seconds,
                    )
                    self._orders_sent += 1
                    self._order = {
                        "state": receipt.state,
                        "live": receipt.live,
                        "mode": receipt.mode,
                        "retcode": receipt.outcome.retcode,
                        "retcode_name": receipt.outcome.retcode_name,
                        "classification": receipt.outcome.classification,
                        "volume_requested": str(receipt.outcome.volume_requested),
                        "volume_filled": str(receipt.outcome.volume_filled),
                        "unknown": receipt.outcome.unknown,
                        "permits_resend": receipt.outcome.permits_resend,
                        "partially_filled": receipt.outcome.partially_filled,
                        "account": "<redacted>",
                    }
                    self._check("order_outcome_known", not receipt.outcome.unknown)
                except MT5SubmitRefused as exc:
                    self._block("submission_refused", exc.reason)
                except MT5Error as exc:
                    self._block("submission_failed", exc.reason)

        # 7. disarm and disconnect: the cycle is bounded
        if self._armed_during_cycle:
            adapter.disarm(reason="session_cycle_complete")
        account_changes = adapter.account_changes
        try:
            adapter.disconnect(reason="session_cycle_complete")
        except Exception:
            pass

        completed = self.clock()
        return SessionReport(
            started_at=started,
            completed_at=completed,
            duration_seconds=round((completed - started).total_seconds(), 3),
            policy=self.policy.account_mode_policy,
            connected=self._connected,
            account=account,
            account_validation=account_validation,
            trading_enabled_setting=settings.trading_enabled,
            checks=tuple(self._checks),
            blockers=tuple(dict.fromkeys(self._blockers)),
            signal=self._signal,
            order=self._order,
            armed_during_cycle=self._armed_during_cycle,
            submission_attempted=self._submission_attempted,
            orders_sent=self._orders_sent,
            observe_only=not self._submission_attempted,
            demo_only_enforced=demo_policy,
            live_account_involved=live_involved,
            audited_events=self._audited_events(),
            account_changes=account_changes,
        )


def run_session(
    adapter: MT5TradingAdapter,
    *,
    policy: SessionPolicy | None = None,
    **kwargs: Any,
) -> SessionReport:
    """Convenience wrapper: build and run one cycle."""

    return MT5SessionCycle(adapter, policy=policy, **kwargs).run()


__all__ = [
    "ANY_ACCOUNT",
    "DEMO_ONLY",
    "MAX_ORDERS_PER_CYCLE",
    "MT5SessionCycle",
    "MT5SessionRefused",
    "SessionCheck",
    "SessionPolicy",
    "SessionReport",
    "run_session",
]
