"""The operator's live-execution workflow, assembled from the parts that already exist.

This module is the *only* place a workstation session decides to arm real
trading, and it decides nothing about markets.  It:

* collects the operator's plan (strategy, symbol, order size, open-position
  count, daily loss limit) and validates every field;
* builds the final review -- the exact terms, the account it would trade, the
  broker's own volume rules -- so the decision is taken on the numbers, not on a
  memory of them;
* requires an explicit acknowledgement *and* the documented confirmation phrase
  before it arms anything;
* hands the arming decision to :meth:`MT5TradingAdapter.arm_trading`, which
  revalidates the account, the terminal's trading permission, and unresolved
  orders, and which sends nothing;
* can always disarm, and does so on every failure it sees.

What it never does
------------------
It lives beside the live leg rather than in the workstation package on purpose:
the browser layer must not be able to name an arming or order-submitting API, so
the object it holds can arm and stop a session but the UI code cannot construct
one of those calls itself.

* It does not connect, sign in, switch the execution mode, or start a runner.
  Enabling LIVE arms nothing; arming sends nothing; starting the session is a
  separate click.
* It does not choose a strategy, a symbol, a size, or a limit.  Empty fields are
  named problems, not defaults.
* It does not widen the provider setup.  A plan is a ceiling only if it is at or
  below the limits the risk gate already enforces.
* It never reads, writes, or echoes a credential.  The password lives in the OS
  vault and the terminal signs in itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from . import live_plan as plans

#: What the operator types to authorise real orders.  It is the phrase the
#: adapter itself checks, so the check is never merely a UI gesture.
ARM_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"

#: Why arming was refused.  Each is a code the page can show next to a hint.
MODE_NOT_LIVE = "live_mode_required"
NOT_CONFIRMED = "live_not_confirmed"
ADAPTER_MISSING = "live_adapter_missing"
NOT_CONNECTED = "mt5_not_connected"
TRADING_DISABLED = "mt5_trading_disabled"
SETUP_MISSING = "provider_setup_missing"
SETUP_INVALID = "provider_setup_invalid"
PLAN_INVALID = "live_plan_invalid"
REVIEW_NOT_ACKNOWLEDGED = "review_not_acknowledged"
CONFIRMATION_MISMATCH = "live_confirmation_mismatch"
SYMBOL_UNRESOLVED = "symbol_unresolved"
SYMBOL_RULES_UNAVAILABLE = "symbol_rules_unavailable"
QUANTITY_NOT_TRADABLE = "quantity_not_tradable"
UNRESOLVED_ORDER = "unresolved_order_present"
ARM_REFUSED = "live_arming_refused"
PLAN_WRITE_FAILED = "live_plan_not_written"
ALREADY_ARMED = "already_armed"
ACCOUNT_INVALID = "mt5_account_profile_invalid"
#: The session's LIVE confirmation named one account.  Orders are refused if the
#: terminal is on another one, or if LIVE was selected with nothing connected.
ACCOUNT_NOT_CONFIRMED = "session_account_not_confirmed"
ACCOUNT_CHANGED = "session_account_changed"
#: Arming names one account, and the operator must type that account's
#: fingerprint.  A different account in the terminal is refused, not adopted.
ACCOUNT_NOT_IDENTIFIED = "account_not_identified"
ACCOUNT_CONFIRMATION_MISMATCH = "account_confirmation_mismatch"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ArmedStrategy:
    """Answers "is this strategy approved and active?" for the broker adapter.

    The adapter asks for a strategy lifecycle before it submits.  AXIOM's live
    authority is the operator's armed plan, so this answers *yes only* for the
    one strategy that plan names, and *only* while it is armed.  It is not a
    promotion mechanism: it cannot outlive the arming, and it names one strategy
    at a time.  Whether that strategy has measured results is recorded in the
    plan and shown on the page; it is not invented here.
    """

    candidate_id: str = ""
    armed: bool = False

    def approve_active(self) -> bool:
        return bool(self.armed and self.candidate_id)

    def for_strategy(self, strategy_id: str) -> "ArmedStrategy | None":
        if not self.candidate_id or str(strategy_id) != self.candidate_id:
            return None
        return self

    def audit_view(self) -> dict[str, Any]:
        return {"strategy_id": self.candidate_id, "active": self.armed}


@dataclass(frozen=True)
class Blocker:
    reason: str
    message: str
    hint: str = ""

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "message": self.message, "hint": self.hint}


@dataclass(frozen=True)
class ArmResult:
    armed: bool
    reason: str = ""
    message: str = ""
    hints: tuple[str, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    plan: Mapping[str, Any] = field(default_factory=dict)

    def audit_view(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "reason": self.reason,
            "message": self.message,
            "blockers": [item.audit_view() for item in self.blockers],
        }


class LiveTrading:
    """Holds the live plan, the arming state, and nothing else that can send."""

    def __init__(
        self,
        *,
        adapter: Any = None,
        mode: Any = None,
        plan_path: Any = None,
        setup_path: Any = None,
        stack_builder: Callable[[str], Any] | None = None,
        candidates: Sequence[Any] = (),
        evidence_lookup: Callable[[str], Mapping[str, Any]] | None = None,
        paired_symbols: Sequence[str] = (),
        session_factory: Callable[[Mapping[str, Any]], Any] | None = None,
        armed_strategy: ArmedStrategy | None = None,
        clock: Callable[[], datetime] | None = None,
        audit_log: Any = None,
    ) -> None:
        self.adapter = adapter
        self.mode = mode
        self.plan_path = plan_path
        self.setup_path = setup_path
        self.stack_builder = stack_builder
        self.candidates = tuple(candidates)
        self.evidence_lookup = evidence_lookup
        self.paired_symbols = tuple(paired_symbols)
        self.clock = clock or _utc_now
        self.audit_log = audit_log
        self.session_factory = session_factory
        self.armed_strategy = armed_strategy
        self.session: Any = None
        self.plan: dict[str, Any] = {}
        self.armed_at: datetime | None = None
        self.last_review: dict[str, Any] = {}
        self.last_refusal: dict[str, Any] = {}
        self._load_plan()

    # -- documents -------------------------------------------------------

    def _load_plan(self) -> None:
        path = self.plan_path
        if path is None:
            return
        try:
            self.plan = plans.load_plan(path)
        except plans.PlanError:
            self.plan = {}

    def _setup(self) -> tuple[Any, str]:
        """The provider setup document, or a reason code.  Never raises."""

        if self.setup_path is None:
            return None, SETUP_MISSING
        from ..config.provider_setup import ProviderSetupError, load_provider_setup

        try:
            return load_provider_setup(self.setup_path), ""
        except ProviderSetupError as exc:
            return None, f"{SETUP_INVALID}:{exc.reason}"
        except Exception:
            return None, SETUP_INVALID

    def candidate_choices(self) -> list[dict[str, Any]]:
        """Every candidate, each labelled with what (if anything) measured it."""

        items = []
        for item in self.candidates:
            evidence = dict(getattr(item, "evidence", {}) or {})
            if self.evidence_lookup is not None:
                try:
                    evidence = {**evidence, **dict(self.evidence_lookup(
                        str(getattr(item, "candidate_id", ""))
                    ) or {})}
                except Exception:
                    pass
            state = plans.evidence_state(evidence)
            items.append(
                {
                    "candidate_id": str(getattr(item, "candidate_id", "")),
                    "parameters": dict(getattr(item, "parameters", {}) or {}),
                    "rationale": str(getattr(item, "rationale", "")),
                    "evidence_state": state,
                    "evidence_note": plans.evidence_note(state, evidence),
                }
            )
        return items

    def blank_values(self) -> dict[str, str]:
        """The form starts empty: nothing is preselected or defaulted."""

        return {
            "candidate_id": "",
            "pair": "",
            "quantity": "",
            "max_open_positions": "",
            "max_daily_loss_account": "",
        }

    def symbol_choices(self) -> list[str]:
        setup, reason = self._setup()
        if setup is not None:
            return list(setup.pairs())
        return list(self.paired_symbols)

    # -- validation ------------------------------------------------------

    def build(self, values: Mapping[str, Any]) -> tuple[dict[str, Any] | None, tuple[plans.PlanProblem, ...]]:
        setup, _ = self._setup()
        items = []
        for item in self.candidates:
            evidence = dict(getattr(item, "evidence", {}) or {})
            if self.evidence_lookup is not None:
                try:
                    evidence = {**evidence, **dict(self.evidence_lookup(
                        str(getattr(item, "candidate_id", ""))
                    ) or {})}
                except Exception:
                    pass
            items.append(_with_evidence(item, evidence))
        return plans.build_plan(values, setup=setup, candidates=items, now=self.clock())

    def review(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Validate the form and describe exactly what arming would authorise."""

        setup, setup_reason = self._setup()
        document, problems = self.build(values)
        blockers = self.blockers(values, document=document)
        view = {
            "ok": not problems and not blockers,
            "values": {key: str(values.get(key, "") or "").strip() for key in
                       ("candidate_id", "pair", "quantity", "max_open_positions",
                        "max_daily_loss_account")},
            "problems": [item.audit_view() for item in problems],
            "blockers": [item.audit_view() for item in blockers],
            "plan": plans.plan_view(document),
            "setup_reason": setup_reason,
            "setup_loaded": setup is not None,
            "confirmation_phrase": ARM_CONFIRMATION_PHRASE,
            "account_fingerprint": self.confirmed_account_fingerprint(),
            "account": self._account_facts(),
            "symbol": self._symbol_facts(str(values.get("pair", "") or "").strip()),
            "at": self.clock().isoformat(),
            "what_will_happen": self._what_will_happen(document, blockers),
        }
        self.last_review = view
        return view

    def blockers(
        self, values: Mapping[str, Any] | None = None, *, document: Mapping[str, Any] | None = None
    ) -> tuple[Blocker, ...]:
        """Everything that would stop an order right now.  Sends nothing."""

        blockers: list[Blocker] = []
        mode = self.mode
        mode_name = str(getattr(mode, "mode", "") or "").upper()
        if mode is None or mode_name != "LIVE":
            blockers.append(
                Blocker(
                    MODE_NOT_LIVE,
                    "The execution mode is not LIVE, so no real order can be authorised.",
                    "Enable LIVE on the Trading mode screen first; that step alone sends nothing.",
                )
            )
        elif not bool(getattr(mode, "live_effective", False)):
            blockers.append(
                Blocker(
                    NOT_CONFIRMED,
                    "LIVE is selected but not confirmed in this process.",
                    "Complete the live confirmation on the Trading mode screen.",
                )
            )
        adapter = self.adapter
        if adapter is None:
            blockers.append(
                Blocker(
                    ADAPTER_MISSING,
                    "No MetaTrader 5 trading adapter is wired into this workstation.",
                    "Start the workstation with the terminal settings so the adapter can be built.",
                )
            )
            blockers.extend(self._plan_blockers(values, document))
            return tuple(blockers)
        if not getattr(adapter, "connected", False):
            blockers.append(
                Blocker(
                    NOT_CONNECTED,
                    "The terminal is not connected.",
                    "Press Connect and sign in inside MetaTrader 5, then arm again.",
                )
            )
            blockers.extend(self._plan_blockers(values, document))
            return tuple(blockers)
        try:
            status = dict(adapter.status())
        except Exception as exc:
            blockers.append(
                Blocker(NOT_CONNECTED, f"The adapter could not report its status ({type(exc).__name__}).")
            )
            blockers.extend(self._plan_blockers(values, document))
            return tuple(blockers)
        if not status.get("trading_enabled_setting"):
            blockers.append(
                Blocker(
                    TRADING_DISABLED,
                    "Trading is not enabled in the local settings document.",
                    "Set the execution mode to TRADING_ENABLED in mt5_settings.local.json.",
                )
            )
        confirmed_account = str(getattr(self.mode, "session_account", "") or "").strip()
        if not confirmed_account:
            blockers.append(
                Blocker(
                    ACCOUNT_NOT_CONFIRMED,
                    "No account was confirmed for this live session.",
                    "On the Trading mode screen, switch to LIVE and confirm the account "
                    "signature shown there. Nothing is armed until then.",
                )
            )
        else:
            current = self.confirmed_account_fingerprint()
            if current and current != confirmed_account:
                blockers.append(
                    Blocker(
                        ACCOUNT_CHANGED,
                        "The terminal is on a different account than the one this session confirmed.",
                        "Switch LIVE off and on again against the account you mean, or sign "
                        "the other account in inside MetaTrader 5. No order is sent either way.",
                    )
                )
        if status.get("unresolved_orders"):
            blockers.append(
                Blocker(
                    UNRESOLVED_ORDER,
                    "An earlier order is still in doubt at the broker and must be explained first.",
                    "Open Trading mode and reconcile before arming anything new.",
                )
            )
        blockers.extend(self._plan_blockers(values, document))
        return tuple(blockers)

    def _plan_blockers(
        self, values: Mapping[str, Any] | None, document: Mapping[str, Any] | None
    ) -> list[Blocker]:
        blockers: list[Blocker] = []
        setup, reason = self._setup()
        if setup is None:
            blockers.append(
                Blocker(
                    SETUP_MISSING,
                    "No usable provider setup document, so no symbol terms or risk limits exist.",
                    "Complete the Provider setup page first.",
                )
            )
            return blockers

        if values is not None:
            if document is None:
                document, problems = self.build(values)
                for problem in problems:
                    blockers.append(Blocker(PLAN_INVALID, f"{problem.path}: {problem.message}"))
                if problems:
                    return blockers
            document = document or {}
        else:
            document = self.plan

        pair = str(document.get("pair", "") or "")
        if not pair:
            return blockers
        stack = self._stack(pair)
        if stack is None:
            blockers.append(
                Blocker(
                    SYMBOL_UNRESOLVED,
                    f"{pair} could not be resolved against the terminal just now.",
                    "Connect, check the symbol is in Market Watch, and try again.",
                )
            )
            return blockers
        spec = getattr(stack, "spec", None)
        quantity = _decimal(document.get("quantity"))
        if spec is None or quantity is None:
            return blockers
        minimum = _decimal(getattr(spec, "min_quantity", None))
        maximum = _decimal(getattr(spec, "max_quantity", None))
        step = _decimal(getattr(spec, "quantity_step", None))
        if minimum is not None and quantity < minimum:
            blockers.append(
                Blocker(
                    QUANTITY_NOT_TRADABLE,
                    f"{quantity} lots is below the broker's minimum of {minimum} for {pair}.",
                )
            )
        if maximum is not None and maximum > 0 and quantity > maximum:
            blockers.append(
                Blocker(
                    QUANTITY_NOT_TRADABLE,
                    f"{quantity} lots is above the broker's maximum of {maximum} for {pair}.",
                )
            )
        if step is not None and step > 0 and minimum is not None:
            steps = (quantity - minimum) / step
            if steps != steps.to_integral_value():
                blockers.append(
                    Blocker(
                        QUANTITY_NOT_TRADABLE,
                        f"{quantity} lots does not sit on the broker's size grid "
                        f"(min {minimum}, step {step}).",
                    )
                )
        return blockers

    def _stack(self, pair: str) -> Any:
        if self.stack_builder is None:
            return None
        try:
            return self.stack_builder(pair)
        except Exception:
            return None

    def _account_facts(self) -> dict[str, Any]:
        adapter = self.adapter
        if adapter is None:
            return {}
        try:
            status = dict(adapter.status())
        except Exception:
            return {}
        terminal = dict(status.get("terminal") or {})
        redacted = dict(status.get("redacted_account") or {})
        return {
            "connected": bool(status.get("connected")),
            "account_mode": str(status.get("account_mode", "")),
            "account_match": bool(
                str(status.get("account_fingerprint", ""))
                == str(status.get("armed_account_fingerprint", ""))
            )
            if status.get("armed")
            else None,
            "server": str(terminal.get("server", "")),
            "login_masked": str(terminal.get("login_masked", "") or redacted.get("login_masked", "")),
            "fingerprint": str(status.get("account_fingerprint", ""))[:12],
            "trading_allowed": bool(terminal.get("trade_allowed", False)),
            "currency": str(terminal.get("currency", "")),
            "leverage": str(terminal.get("leverage", "")),
        }

    def _symbol_facts(self, pair: str) -> dict[str, Any]:
        if not pair:
            return {}
        stack = self._stack(pair)
        if stack is None:
            return {"pair": pair, "resolved": False}
        spec = getattr(stack, "spec", None)
        facts: dict[str, Any] = {
            "pair": pair,
            "resolved": spec is not None,
            "broker_symbol": str(getattr(stack, "broker_symbol", "")),
        }
        for name in ("min_quantity", "quantity_step", "max_quantity", "price_tick_size", "contract_size"):
            value = getattr(spec, name, None)
            if value is not None:
                facts[name] = str(value)
        info = getattr(stack, "symbol_info", None)
        if isinstance(info, Mapping):
            for name in ("volume_min", "volume_step", "volume_max", "digits", "trade_mode"):
                if name in info:
                    facts[f"broker_{name}"] = str(info[name])
        return facts

    def _what_will_happen(
        self, document: Mapping[str, Any] | None, blockers: Sequence[Blocker]
    ) -> list[str]:
        if document is None:
            return ["Nothing: the plan above is incomplete, so it cannot be armed."]
        return [
            (
                f"Each {document.get('candidate_id')} signal on {document.get('pair')} would "
                f"send ONE market order for {document.get('quantity')} lots through "
                f"MetaTrader 5, on the account the terminal has selected."
            ),
            (
                "AXIOM enforces the daily loss limit and the open-position count itself; it "
                "does not place a stop-loss or take-profit order at the broker, so a terminal "
                "shutdown does not cancel that risk."
            ),
            (
                "If any check fails -- account identity, quote age, symbol rules, margin, "
                "risk limits, trading permission -- no order is sent and the session stops."
            ),
            "Nothing is sent until the strategy produces a signal AND you press Start.",
        ]

    # -- operator actions ------------------------------------------------

    def confirmed_account_fingerprint(self) -> str:
        """The fingerprint of the account in front of the operator right now."""

        adapter = self.adapter
        if adapter is None:
            return ""
        try:
            return str(adapter.account_fingerprint() or "")
        except Exception:
            return ""

    def arm(
        self,
        values: Mapping[str, Any],
        *,
        confirmation: str = "",
        reviewed: bool = False,
        account_confirmation: str = "",
    ) -> ArmResult:
        """Write the plan and arm the broker, or refuse without doing either.

        ``account_confirmation`` must be the fingerprint of the account the
        terminal is on *now*.  Arming is bound to that one account: a different
        account in the terminal later refuses the order instead of receiving it.
        """

        document, problems = self.build(values)
        blockers = list(self.blockers(values, document=document))
        if problems:
            blockers.extend(Blocker(PLAN_INVALID, plans.describe_problem(item)) for item in problems)
        if not reviewed:
            blockers.append(
                Blocker(
                    REVIEW_NOT_ACKNOWLEDGED,
                    "The review was not acknowledged.",
                    "Tick the box that says you read the review above.",
                )
            )
        fingerprint = self.confirmed_account_fingerprint()
        if not fingerprint:
            blockers.append(
                Blocker(
                    ACCOUNT_NOT_IDENTIFIED,
                    "The terminal has not identified the account in front of you.",
                    "Connect first, and check the account badge shows the account you mean.",
                )
            )
        elif str(account_confirmation or "").strip().lower() != fingerprint.lower():
            blockers.append(
                Blocker(
                    ACCOUNT_CONFIRMATION_MISMATCH,
                    "The account confirmation does not match the account the terminal is on.",
                    f"Type the account fingerprint shown on this page ({fingerprint}) "
                    "exactly. LIVE also refuses to arm if that account is not the "
                    "confirmed one.",
                )
            )
        phrase = str(confirmation or "").strip().upper()
        if phrase != ARM_CONFIRMATION_PHRASE:
            blockers.append(
                Blocker(
                    CONFIRMATION_MISMATCH,
                    f'Type "{ARM_CONFIRMATION_PHRASE}" exactly to authorise real orders.',
                )
            )
        if blockers:
            result = self._refuse(blockers)
            return result
        assert document is not None

        try:
            plans.write_plan(document, self.plan_path, overwrite=True)
        except Exception as exc:
            return self._refuse(
                [
                    Blocker(
                        PLAN_WRITE_FAILED,
                        "The plan could not be written, so nothing was armed.",
                        "Check the project folder is writable, then try again.",
                    )
                ]
            )
        try:
            self.adapter.arm_trading(
                confirm=ARM_CONFIRMATION_PHRASE, account_fingerprint=fingerprint
            )
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            mapped = {
                "mt5_not_connected": NOT_CONNECTED,
                "mt5_trading_not_enabled": TRADING_DISABLED,
                "mt5_terminal_trading_not_allowed": TRADING_DISABLED,
                "mt5_arming_confirmation_mismatch": CONFIRMATION_MISMATCH,
                "mt5_unresolved_order_present": UNRESOLVED_ORDER,
                "mt5_account_profile_invalid": ACCOUNT_INVALID,
                "mt5_arming_account_mismatch": ACCOUNT_INVALID,
                "mt5_arming_server_mismatch": ACCOUNT_INVALID,
                "mt5_arming_account_confirmation_mismatch": ACCOUNT_CONFIRMATION_MISMATCH,
                "mt5_account_changed_since_confirmation": ACCOUNT_CONFIRMATION_MISMATCH,
            }.get(reason, ARM_REFUSED)
            return self._refuse(
                [
                    Blocker(
                        mapped,
                        "The broker refused to arm live trading; the plan was written but "
                        "no order may be sent.",
                        f"Reason reported by the adapter: {reason}",
                    )
                ]
            )
        self.plan = document
        self.armed_at = self.clock()
        if self.armed_strategy is not None:
            self.armed_strategy.candidate_id = str(document.get("candidate_id", ""))
            self.armed_strategy.armed = True
        self._audit(
            "LIVE_PLAN_ARMED",
            account_fingerprint=fingerprint,
            candidate_id=document.get("candidate_id", ""),
            pair=document.get("pair", ""),
            quantity=document.get("quantity", ""),
            max_open_positions=document.get("max_open_positions", 0),
            max_daily_loss=document.get("max_daily_loss_account", ""),
            evidence_state=document.get("evidence_state", ""),
        )
        return ArmResult(
            armed=True,
            reason="armed",
            message=(
                "Armed. The next strategy signal would send a real order. "
                "Press Start to begin the session; Stop disarms immediately."
            ),
            plan=document,
        )

    # -- connection -------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        """Adopt the terminal the operator already connected.  Sends nothing."""

        if self.adapter is None:
            return {"connected": False, "reason": ADAPTER_MISSING}
        try:
            self.adapter.connect()
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            self._audit("LIVE_ADAPTER_CONNECT_REFUSED", reason=reason)
            return {"connected": False, "reason": reason}
        return {"connected": bool(getattr(self.adapter, "connected", False)), "reason": ""}

    def disconnect(self, *, reason: str = "operator") -> None:
        """Stop the session, disarm, and close the adapter's side of the terminal."""

        if self.session is not None:
            self.stop_session(reason=reason, close=True)
        if self.adapter is None:
            return
        try:
            self.adapter.disconnect(reason=reason)
        except Exception as exc:
            self._audit("LIVE_ADAPTER_DISCONNECT_FAILED", reason=type(exc).__name__)

    # -- the session ------------------------------------------------------

    def start_session(self) -> dict[str, Any]:
        """Start the strategy loop.  Only an armed, connected, live session gets one."""

        if self.session_factory is None:
            return {
                "started": False,
                "reason": "live_session_unavailable",
                "message": "This workstation was not started with the live session wiring.",
            }
        if not self.armed:
            return {
                "started": False,
                "reason": "live_not_armed",
                "message": "Arm the plan on this page first; nothing was started.",
            }
        blockers = self.blockers()
        if blockers:
            first = blockers[0]
            return {
                "started": False,
                "reason": first.reason,
                "message": first.message,
            }
        if self.session is not None:
            try:
                view = self.session.start_runner()
            except Exception as exc:
                return {
                    "started": False,
                    "reason": "live_session_failed",
                    "message": f"The session could not be started ({type(exc).__name__}).",
                }
            return {
                "started": bool(view.get("running")),
                "reason": str(view.get("reason") or ("" if view.get("running") else "blocked")),
                "message": (
                    "Live session running. Every signal now goes to the broker."
                    if view.get("running")
                    else "The live session did not start; see the blockers on this page."
                ),
                "view": view,
            }
        try:
            self.session = self.session_factory(self.plan)
            view = self.session.start()
            started = self.session.start_runner()
        except Exception as exc:
            self.session = None
            return {
                "started": False,
                "reason": "live_session_failed",
                "message": f"The live session could not be built ({type(exc).__name__}).",
            }
        self._audit(
            "LIVE_SESSION_STARTED",
            candidate_id=self.plan.get("candidate_id", ""),
            pair=self.plan.get("pair", ""),
        )
        return {
            "started": bool(started.get("running")),
            "reason": str(started.get("reason") or ("" if started.get("running") else "blocked")),
            "message": (
                "Live session running. Every signal now goes to the broker."
                if started.get("running")
                else "The live session did not start; see the blockers on this page."
            ),
            "view": started or view,
        }

    def stop_session(self, *, reason: str = "operator_stopped", close: bool = False) -> dict[str, Any]:
        """Stop new submissions and disarm.  Always allowed, never refused."""

        stopped: dict[str, Any] = {}
        if self.session is not None:
            try:
                stopped = self.session.stop_runner(reason=reason, disarm=True)
            except Exception:
                stopped = {}
            if close:
                try:
                    self.session.close()
                except Exception:
                    pass
                self.session = None
        self.disarm(reason=reason)
        self._audit("LIVE_SESSION_STOPPED", reason=reason)
        return {"stopped": True, "reason": reason, "view": stopped}

    def disarm(self, *, reason: str = "operator") -> dict[str, Any]:
        if self.armed_strategy is not None:
            self.armed_strategy.armed = False
        if self.adapter is not None:
            try:
                self.adapter.disarm(reason=reason)
            except Exception:
                pass
        self._audit("LIVE_PLAN_DISARMED", reason=reason)
        return {"armed": False, "reason": reason}

    @property
    def armed(self) -> bool:
        return bool(getattr(self.adapter, "armed", False))

    def _refuse(self, blockers: Sequence[Blocker]) -> ArmResult:
        items = tuple(blockers)
        self.last_refusal = {
            "blockers": [item.audit_view() for item in items],
            "at": self.clock().isoformat(),
        }
        for item in items:
            self._audit("LIVE_ARM_REFUSED", reason=item.reason)
        first = items[0] if items else None
        return ArmResult(
            armed=False,
            reason=first.reason if first else ARM_REFUSED,
            message=first.message if first else "Nothing was armed.",
            hints=tuple(item.hint for item in items if item.hint),
            blockers=items,
        )

    def _audit(self, event_type: str, **fields: Any) -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.record({"event_type": event_type, "live": True, **fields})
        except Exception:
            pass

    # -- read model ------------------------------------------------------

    def view(self, *, runner: Any = None) -> dict[str, Any]:
        """Everything the Live execution page shows.  Reads only."""

        adapter = self.adapter
        blockers = list(self.blockers())
        status: dict[str, Any] = {}
        if adapter is not None:
            try:
                status = dict(adapter.status())
            except Exception:
                status = {}
        runner_view: dict[str, Any] = {}
        if runner is not None:
            try:
                runner_view = dict(runner.view() or {})
            except Exception:
                runner_view = {}
        connected = bool(status.get("connected", False))
        armed = bool(status.get("armed", False))
        live_effective = str(getattr(self.mode, "mode", "") or "").upper() == "LIVE" and bool(
            getattr(self.mode, "live_effective", False)
        )
        plan = plans.plan_view(self.plan)
        session_view: dict[str, Any] = {}
        if self.session is not None:
            try:
                session_view = dict(self.session.view() or {})
            except Exception:
                session_view = {}
        return {
            "configured": adapter is not None,
            "adapter_present": adapter is not None,
            "connected": connected,
            "armed": armed,
            "mode": str(getattr(self.mode, "mode", "") or "PAPER"),
            "live_effective": live_effective,
            "trading_enabled": bool(status.get("trading_enabled_setting", False)),
            "account": self._account_facts(),
            "account_mode": str(status.get("account_mode", "")),
            "unresolved": list(status.get("unresolved_orders", []) or []),
            "positions": self._broker_positions(),
            "plan": plan,
            "plan_path": str(self.plan_path or ""),
            "blockers": [item.audit_view() for item in blockers],
            "ready_to_arm": not blockers,
            "review": dict(self.last_review),
            "last_refusal": dict(self.last_refusal),
            "confirmation_phrase": ARM_CONFIRMATION_PHRASE,
            "account_fingerprint": self.confirmed_account_fingerprint(),
            "candidates": self.candidate_choices(),
            "symbols": self.symbol_choices(),
            "runner": runner_view or session_view,
            "session_running": bool(
                runner_view.get("running", session_view.get("running", False))
            ),
            "next_signal_sends_real_order": bool(
                armed
                and connected
                and live_effective
                and bool(
                    runner_view.get("running", session_view.get("running", False))
                )
                and not blockers
            ),
            "orders_sent": int(
                runner_view.get("live_orders", session_view.get("live_orders", 0)) or 0
            ),
            "last_outcome": dict(
                runner_view.get("last_live_outcome", session_view.get("last_live_outcome", {})) or {}
            ),
            "session_state": str(session_view.get("state", "")),
            "stopped_reason": str(session_view.get("stopped_reason", "")),
        }

    def summary(self) -> dict[str, Any]:
        """One sentence every page may show: would the next signal send an order?"""

        adapter = self.adapter
        connected = bool(getattr(adapter, "connected", False))
        armed = self.armed
        live_effective = str(getattr(self.mode, "mode", "") or "").upper() == "LIVE" and bool(
            getattr(self.mode, "live_effective", False)
        )
        running = False
        if self.session is not None:
            try:
                running = bool(self.session.view().get("running", False))
            except Exception:
                running = False
        sending = bool(armed and connected and live_effective and running)
        if not adapter:
            sentence = (
                "No live trading adapter is wired in: a signal can only be simulated."
            )
        elif sending:
            sentence = (
                "The next strategy signal WILL send a real order to the broker."
            )
        elif armed:
            sentence = (
                "Armed, but the live session is not running: no signal is being read, "
                "so no order can be sent right now."
            )
        elif live_effective:
            sentence = "LIVE is enabled but nothing is armed: no order can be sent."
        else:
            sentence = "Paper mode: every fill is simulated locally; no order is sent."
        return {
            "present": True,
            "armed": armed,
            "connected": connected,
            "mode": str(getattr(self.mode, "mode", "") or "PAPER"),
            "live_effective": live_effective,
            "session_running": running,
            "next_signal_sends_real_order": sending,
            "sentence": sentence,
        }

    def _broker_positions(self) -> list[dict[str, Any]]:
        adapter = self.adapter
        if adapter is None:
            return []
        try:
            return [
                item.audit_view() if hasattr(item, "audit_view") else dict(item)
                for item in adapter.positions()
            ]
        except Exception:
            return []


def _with_evidence(candidate: Any, evidence: Mapping[str, Any]) -> Any:
    from dataclasses import replace

    try:
        return replace(candidate, evidence=dict(evidence or {}))
    except Exception:
        return candidate


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


__all__ = [
    "ACCOUNT_INVALID",
    "ArmedStrategy",
    "ADAPTER_MISSING",
    "ALREADY_ARMED",
    "ARM_CONFIRMATION_PHRASE",
    "ARM_REFUSED",
    "ArmResult",
    "Blocker",
    "CONFIRMATION_MISMATCH",
    "LiveTrading",
    "MODE_NOT_LIVE",
    "NOT_CONFIRMED",
    "NOT_CONNECTED",
    "PLAN_INVALID",
    "PLAN_WRITE_FAILED",
    "QUANTITY_NOT_TRADABLE",
    "REVIEW_NOT_ACKNOWLEDGED",
    "SETUP_INVALID",
    "SETUP_MISSING",
    "SYMBOL_RULES_UNAVAILABLE",
    "SYMBOL_UNRESOLVED",
    "TRADING_DISABLED",
    "UNRESOLVED_ORDER",
]
