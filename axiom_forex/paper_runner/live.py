"""The live leg: one strategy plan, through the gate, to the broker.

This module is the *only* place in the running system that can cause a broker
order, and it is deliberately thin.  It decides nothing itself.  It refuses
unless every precondition already holds, hands the planner's own request to the
existing pre-trade gate, and then gives the gate's approved intent to
``ExecutionRouter.route_live``, which dispatches to the existing
``MT5TradingAdapter`` -- the component that writes a durable record *before*
``order_send`` and disarms on an unknown outcome.

Order of operations, all fail-closed
------------------------------------
1. **Duplicate check.**  A request id that already produced a broker order is a
   duplicate, never a second order.
2. **Blocker sweep** (read-only, sends nothing): live mode *and* confirmed, a
   router present, adapter connected, adapter armed, the armed account identical
   to the account the terminal has selected now, trading enabled in settings, the
   strategy approved and active, the symbol still matching the broker's terms,
   no unresolved order, and the ledger in agreement with the terminal's
   positions.
3. **Gate.**  ``PreTradeGate.evaluate`` on the plan's own request and context.  A
   rejection is reported with the gate's own code; nothing is sent.
4. **Route.**  ``ExecutionRouter.route_live`` -> ``MT5TradingAdapter.submit``,
   which re-checks everything above and then checks price drift, volume grid,
   netting conflict, margin, and quote age.
5. **Book.**  A broker-confirmed fill is booked into the same ledger the paper
   lane uses, at the terminal's own price and the filled volume -- so the gate
   sees the same exposure live as it did on paper.  A fill that cannot be booked
   blocks further orders rather than being quietly dropped.

Nothing here retries.  An unknown outcome leaves the adapter disarmed and the
order in doubt; the next order is refused until reconciliation explains it.  No
order is ever re-sent automatically, and nothing here reacts to a mode *change*
-- a mode only gates, it never triggers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

# -- refusal reasons --------------------------------------------------------

MODE_NOT_LIVE = "mode_not_live"
NOT_CONFIRMED = "live_not_confirmed"
ROUTER_MISSING = "live_router_missing"
ADAPTER_MISSING = "live_adapter_missing"
NOT_CONNECTED = "mt5_not_connected"
NOT_ARMED = "live_not_armed"
ACCOUNT_MISMATCH = "live_account_mismatch"
TRADING_DISABLED = "mt5_trading_disabled"
STRATEGY_NOT_APPROVED = "strategy_not_approved_for_live"
SPEC_MISMATCH = "live_symbol_spec_mismatch"
UNRESOLVED_ORDER = "unresolved_order_present"
DUPLICATE_REQUEST = "duplicate_request"
POSITIONS_MISMATCH = "live_positions_mismatch"
GATE_REFUSED = "gate_refused"
ROUTE_REFUSED = "live_route_refused"
SUBMIT_REFUSED = "live_submit_refused"
BOOKING_FAILED = "live_fill_not_booked"
STATUS_UNREADABLE = "live_status_unreadable"

#: Reasons that mean "the broker may have an order we cannot account for".
BLOCKING_REASONS = frozenset({UNRESOLVED_ORDER, "live_order_outcome_unknown"})

#: The one thing a live leg must never do by itself.
ORDER_SENDING = True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LiveBlocker:
    reason: str
    message: str
    hint: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": self.message, "hint": self.hint}


@dataclass
class LiveOutcome:
    """One submission attempt, as the operator and the audit trail see it."""

    submitted: bool
    reasons: tuple[str, ...] = ()
    detail: str = ""
    request_id: str = ""
    state: str = ""
    retcode: int = 0
    retcode_name: str = ""
    classification: str = ""
    volume_requested: Decimal = Decimal(0)
    volume_filled: Decimal = Decimal(0)
    unknown: bool = False
    permits_resend: bool = False
    booked: bool = False
    booking: Mapping[str, Any] = field(default_factory=dict)
    gate: Mapping[str, Any] | None = None
    blockers: tuple[LiveBlocker, ...] = ()
    at: datetime | None = None

    @property
    def unresolved(self) -> bool:
        """The broker did not confirm; nothing may be re-sent."""

        return bool(self.submitted) and (self.unknown or self.state == "UNRESOLVED")

    def refusal(self) -> dict[str, Any]:
        return {
            "reasons": list(self.reasons),
            "detail": self.detail,
            "blockers": [item.audit_view() for item in self.blockers],
            "gate": dict(self.gate or {}),
        }

    def audit_view(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "reasons": list(self.reasons),
            "detail": self.detail,
            "request_id": self.request_id,
            "state": self.state,
            "retcode": self.retcode,
            "retcode_name": self.retcode_name,
            "classification": self.classification,
            "volume_requested": str(self.volume_requested),
            "volume_filled": str(self.volume_filled),
            "unknown": self.unknown,
            "unresolved": self.unresolved,
            "booked": self.booked,
            "booking": dict(self.booking),
            "at": self.at.isoformat() if self.at else "",
        }

    #: The runner and the workstation both read this shape.
    def as_result(self) -> dict[str, Any]:
        payload = self.audit_view()
        payload["reason"] = (
            self.reasons[0] if self.reasons else ("submitted" if self.submitted else "")
        )
        payload["filled"] = bool(self.submitted and self.booked)
        payload["blockers"] = [item.audit_view() for item in self.blockers]
        payload["gate"] = dict(self.gate or {})
        # The refusal block is always present so an operator view never has to
        # guess whether "submitted: false" came with a reason.
        payload["refusal"] = self.refusal()
        return payload


class LiveDataRefused(Exception):
    """A read of the broker's data could not be served.  Carries a reason code.

    Used by :meth:`LiveLeg.bars`, so the forecasting panel can ask this leg for
    bars without ever being handed the order-capable adapter itself.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


class LiveLeg:
    """One live order path, shared by every strategy that may trade live."""

    def __init__(
        self,
        *,
        router: Any,
        adapter: Any,
        gate: Any,
        mode: Any,
        spec: Any,
        strategy_id: str,
        repository: Any = None,
        audit_log: Any = None,
        lifecycle: Any = None,
        portfolio: Any = None,
        ledger_positions: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        rollover_cutoffs: Sequence[datetime] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.router = router
        self.adapter = adapter
        self.gate = gate
        self.mode = mode
        self.spec = spec
        self.strategy_id = str(strategy_id or "")
        self.repository = repository
        self.audit_log = audit_log
        self.lifecycle = lifecycle
        #: The ledger broker fills are booked into; the broker stays authoritative.
        self.portfolio = portfolio
        self.ledger_positions = ledger_positions
        self.rollover_cutoffs = tuple(rollover_cutoffs or ())
        self.clock = clock or _utc_now
        self.history: list[dict[str, Any]] = []
        self.booked: list[dict[str, Any]] = []
        self.booking_failures: list[dict[str, Any]] = []
        self.last_outcome: dict[str, Any] = {}
        self.last_reconciliation: dict[str, Any] = {}

    # -- read model ------------------------------------------------------

    def bars(self, symbol: str, timeframe: str, *, count: int):
        """Read the broker's bars for this leg's instrument.  Data only.

        The forecasting panel needs bars and nothing else, so it asks this method
        instead of being given the adapter: the object it receives here can read
        prices and has no way to reach an order path.  Every refusal is a reason
        code the page can show.
        """

        if self.adapter is None:
            raise LiveDataRefused(
                "no_terminal",
                "no trading adapter is attached, so there are no broker bars",
            )
        wanted = str(getattr(self.spec, "canonical_pair", "") or getattr(self.spec, "pair", ""))
        if str(symbol) != wanted:
            raise LiveDataRefused(
                "symbol_not_on_terminal",
                f"this live leg is configured for {wanted}, not {symbol}",
            )
        from datetime import timedelta

        from axiom_forex.kronos.settings import TIMEFRAMES

        seconds = TIMEFRAMES.get(str(timeframe), 0)
        if seconds <= 0:
            raise LiveDataRefused("unsupported_timeframe", str(timeframe))
        end = self.clock()
        start = end - timedelta(seconds=seconds * (int(count) + 2))
        try:
            rows = list(self.adapter.bars(self.spec, str(timeframe), start=start, end=end))
        except Exception as exc:
            raise LiveDataRefused(
                "bars_unavailable", str(getattr(exc, "reason", "") or type(exc).__name__)
            ) from exc
        if not rows:
            raise LiveDataRefused(
                "no_bars", "the terminal returned no completed bars for that window"
            )
        return rows

    @property
    def mode_name(self) -> str:
        return str(getattr(self.mode, "mode", "") or "").upper()

    @property
    def live_effective(self) -> bool:
        return bool(getattr(self.mode, "live_effective", False))

    @property
    def active(self) -> bool:
        return self.mode_name == "LIVE" and self.live_effective

    def blockers(
        self,
        *,
        request_id: str = "",
        strategy_approved: bool = True,
        ledger_positions: Sequence[Mapping[str, Any]] | None = None,
    ) -> tuple[LiveBlocker, ...]:
        """Everything that would stop a live order right now.  Sends nothing."""

        blockers: list[LiveBlocker] = []
        if self.mode_name != "LIVE":
            blockers.append(
                LiveBlocker(
                    MODE_NOT_LIVE,
                    f"Execution mode is {self.mode_name or 'UNKNOWN'}; a live order needs "
                    "LIVE, confirmed by the operator.",
                    "Switch to LIVE on the Trading mode screen and complete its preflight.",
                )
            )
        elif not self.live_effective:
            blockers.append(
                LiveBlocker(
                    NOT_CONFIRMED,
                    "LIVE is selected but the mode has not been confirmed, so no order "
                    "may be sent.",
                    "Complete the live confirmation step on the Trading mode screen.",
                )
            )

        if self.router is None:
            blockers.append(
                LiveBlocker(
                    ROUTER_MISSING,
                    "No execution router is wired in, so nothing can be dispatched.",
                )
            )
        if self.adapter is None:
            blockers.append(
                LiveBlocker(
                    ADAPTER_MISSING,
                    "No MT5 adapter is configured, so nothing can be sent.",
                    "Configure the terminal settings and connect.",
                )
            )
            return tuple(blockers)
        if not getattr(self.adapter, "connected", False):
            blockers.append(
                LiveBlocker(
                    NOT_CONNECTED,
                    "The MT5 terminal is not connected.",
                    "Press Connect and sign in to the terminal you want to trade.",
                )
            )
            return tuple(blockers)

        status: dict[str, Any] = {}
        try:
            status = dict(self.adapter.status())
        except Exception as exc:
            blockers.append(
                LiveBlocker(NOT_CONNECTED, f"The adapter could not report its status ({type(exc).__name__}).")
            )
            return tuple(blockers)

        if not status.get("trading_enabled_setting"):
            blockers.append(
                LiveBlocker(
                    TRADING_DISABLED,
                    "Trading is disabled in the settings document.",
                    "Set the execution mode to TRADING_ENABLED in the local settings file.",
                )
            )
        if status.get("armed") is not True:
            blockers.append(
                LiveBlocker(
                    NOT_ARMED,
                    "Live trading is not armed, so no order can be sent.",
                    "Arm live trading from the Trading mode screen; it is a separate, "
                    "explicit step after the mode switch.",
                )
            )
        fingerprint = str(status.get("account_fingerprint") or "")
        armed_fingerprint = str(status.get("armed_account_fingerprint") or "")
        if armed_fingerprint and fingerprint and armed_fingerprint != fingerprint:
            blockers.append(
                LiveBlocker(
                    ACCOUNT_MISMATCH,
                    "The account armed for trading is not the account the terminal has "
                    "selected now.",
                    "Sign in to the intended account and connect again; the match is never "
                    "relaxed to make an order possible.",
                )
            )
        if status.get("unresolved_orders"):
            blockers.append(
                LiveBlocker(
                    UNRESOLVED_ORDER,
                    f"{len(status['unresolved_orders'])} order(s) are in doubt at the broker "
                    "and must be explained before another is sent.",
                    "Open the Trading mode screen and reconcile; an unresolved order is "
                    "never re-sent automatically.",
                )
            )
        if not strategy_approved:
            blockers.append(
                LiveBlocker(
                    STRATEGY_NOT_APPROVED,
                    f"{self.strategy_id or 'this strategy'} is not approved and active for "
                    "live trading.",
                    "Live orders need the same approved strategy paper ran; nothing is "
                    "promoted here.",
                )
            )
        if request_id and self._duplicate(request_id):
            blockers.append(
                LiveBlocker(
                    DUPLICATE_REQUEST,
                    f"An order record for {request_id} already exists.",
                    "A repeated request id is a duplicate, not a second order.",
                )
            )
        if self.booking_failures:
            failure = self.booking_failures[-1]
            blockers.append(
                LiveBlocker(
                    BOOKING_FAILED,
                    "A live fill from the broker could not be booked into the ledger "
                    f"({failure.get('error', 'unknown')}), so the book may not match the "
                    "terminal.",
                    "Reconcile on the Trading mode screen; the ledger is never silently "
                    "edited to match a broker read.",
                )
            )
        rows = (
            [dict(row) for row in ledger_positions]
            if ledger_positions is not None
            else self._ledger_rows()
        )
        if rows is not None:
            try:
                comparison = self.adapter.reconcile(rows)
                reconciled = bool(getattr(comparison, "reconciled", False))
                detail = "" if reconciled else _differences(comparison)
            except Exception as exc:
                reconciled = False
                detail = type(exc).__name__
            if not reconciled:
                blockers.append(
                    LiveBlocker(
                        POSITIONS_MISMATCH,
                        "The ledger and the terminal disagree about open positions"
                        + (f" ({detail})" if detail else "")
                        + ".",
                        "Reconcile before sending anything: an unexplained position is "
                        "never overwritten or ignored.",
                    )
                )
        try:
            _, check = self.adapter.cross_check(self.spec)
            if not getattr(check, "ok", False):
                blockers.append(
                    LiveBlocker(
                        SPEC_MISMATCH,
                        "The instrument specification no longer matches the broker's terms "
                        f"({', '.join(getattr(check, 'errors', ()) or ()) or 'differences'}).",
                        "Re-resolve the symbol from the terminal and update the catalogue.",
                    )
                )
        except Exception as exc:
            blockers.append(
                LiveBlocker(
                    SPEC_MISMATCH,
                    f"The symbol could not be cross-checked against the broker "
                    f"({type(exc).__name__}).",
                    "Check the symbol exists in Market Watch and is tradable.",
                )
            )
        return tuple(blockers)

    def status(self) -> dict[str, Any]:
        """The operator's view of the live leg.  Reads only, never sends."""

        try:
            approved = self._strategy_approved()
            blockers = [
                item.audit_view()
                for item in self.blockers(strategy_approved=approved)
            ]
        except Exception as exc:  # a read model never raises
            blockers = [
                {"reason": STATUS_UNREADABLE, "message": type(exc).__name__, "hint": ""}
            ]
        account: dict[str, Any] = {}
        if self.adapter is not None:
            try:
                account = dict(self.adapter.status().get("redacted_account") or {})
            except Exception:
                account = {}
        return {
            "configured": self.adapter is not None and self.router is not None,
            "active": self.active,
            "mode": self.mode_name or "UNKNOWN",
            "live_effective": self.live_effective,
            "strategy_id": self.strategy_id,
            "strategy_approved": self._strategy_approved(),
            "blockers": blockers,
            "ready": not blockers,
            "account": account,
            "orders_sent": len(self.history),
            "fills_booked": len(self.booked),
            "booking_failures": [dict(item) for item in self.booking_failures[-5:]],
            "last_outcome": dict(self.last_outcome),
            "last_reconciliation": dict(self.last_reconciliation),
            "order_sending": ORDER_SENDING,
            "history": [dict(item) for item in self.history[-12:]],
        }

    # -- the order path --------------------------------------------------

    def submit_plan(self, plan: Any, *, strategy_approved: bool = True) -> dict[str, Any]:
        """Gate, route, and book exactly one order.  Never retries."""

        request = getattr(plan, "request", None)
        context = getattr(plan, "context", None)
        request_id = str(getattr(request, "request_id", "") or "")
        blockers = self.blockers(
            request_id=request_id, strategy_approved=strategy_approved
        )
        if blockers:
            return self._refuse(
                [item.reason for item in blockers],
                request_id=request_id,
                detail=blockers[0].message,
                blockers=blockers,
            )

        try:
            decision = self.gate.evaluate(request, context)
        except Exception as exc:
            return self._refuse(
                [GATE_REFUSED], request_id=request_id, detail=type(exc).__name__
            )
        if not getattr(decision, "allowed", False):
            code = str(getattr(getattr(decision, "code", ""), "value", "") or "rejected")
            gate_view = (
                decision.audit_view() if hasattr(decision, "audit_view") else {"code": code}
            )
            return self._refuse(
                [GATE_REFUSED], request_id=request_id, detail=code, gate=gate_view
            )
        intent = getattr(decision, "order_intent", None)
        if intent is None:
            return self._refuse(
                [GATE_REFUSED], request_id=request_id, detail="approved_without_intent"
            )

        try:
            receipt = self.router.route_live(decision, intent, spec=self.spec)
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or "")
            mapped = {
                "live_not_armed": NOT_ARMED,
                "live_mode_required": MODE_NOT_LIVE,
                "live_not_confirmed": NOT_CONFIRMED,
                "live_unavailable": ADAPTER_MISSING,
            }.get(reason, SUBMIT_REFUSED if reason else ROUTE_REFUSED)
            return self._refuse([mapped], request_id=request_id, detail=reason or type(exc).__name__)

        broker = getattr(receipt, "outcome", None)
        unknown = bool(getattr(broker, "unknown", False))
        outcome = LiveOutcome(
            submitted=True,
            reasons=("live_order_unresolved",) if unknown else (),
            request_id=request_id,
            state=str(getattr(receipt, "state", "") or ""),
            retcode=int(getattr(broker, "retcode", 0) or 0),
            retcode_name=str(getattr(broker, "retcode_name", "") or ""),
            classification=str(getattr(broker, "classification", "") or ""),
            volume_requested=Decimal(str(getattr(broker, "volume_requested", 0) or 0)),
            volume_filled=Decimal(str(getattr(broker, "volume_filled", 0) or 0)),
            unknown=unknown,
            permits_resend=bool(getattr(broker, "permits_resend", False)),
            at=self.clock(),
        )
        if not unknown and outcome.volume_filled > 0:
            booking = self._book(plan, broker)
            outcome.booking = booking
            outcome.booked = bool(booking.get("booked"))
            if not outcome.booked:
                outcome.reasons = (BOOKING_FAILED,)
        result = outcome.as_result()
        self.last_outcome = result
        self.history.append(result)
        self._audit(
            "PAPER_RUNNER_LIVE_ORDER" if not unknown else "PAPER_RUNNER_LIVE_ORDER_UNKNOWN",
            request_id=request_id,
            state=outcome.state,
            retcode=outcome.retcode,
            retcode_name=outcome.retcode_name,
            classification=outcome.classification,
            volume_requested=str(outcome.volume_requested),
            volume_filled=str(outcome.volume_filled),
            booked=outcome.booked,
        )
        if unknown:
            # Fail closed here as well as inside the adapter: no new order until
            # a human explains this one.
            try:
                self.adapter.disarm(reason="unknown_order_outcome")
            except Exception:
                pass
        return result

    def execute(self, plan: Any) -> dict[str, Any]:
        """Runner-facing wrapper: same path, strategy approval read from the lifecycle."""

        return self.submit_plan(plan, strategy_approved=self._strategy_approved())

    # -- reconciliation --------------------------------------------------

    def reconcile(
        self, ledger_positions: Sequence[Mapping[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Resolve in-doubt orders, then compare broker positions with the ledger."""

        if self.adapter is None:
            view = {"available": False, "reconciled": False, "reason": ADAPTER_MISSING}
            self.last_reconciliation = view
            return view
        recovered: list[dict[str, Any]] = []
        try:
            for item in self.adapter.recover():
                recovered.append(
                    item.audit_view() if hasattr(item, "audit_view") else dict(item)
                )
        except Exception as exc:
            recovered = [{"error": type(exc).__name__}]
        rows = (
            [dict(row) for row in ledger_positions]
            if ledger_positions is not None
            else (self._ledger_rows() or [])
        )
        try:
            comparison = self.adapter.reconcile(rows)
            positions = (
                comparison.audit_view()
                if hasattr(comparison, "audit_view")
                else dict(comparison)
            )
        except Exception as exc:
            positions = {"error": type(exc).__name__, "reconciled": False}
        view = {
            "available": True,
            "at": _iso(self.clock()),
            "recovered": recovered,
            "positions": positions,
            "reconciled": bool(positions.get("reconciled", False)),
        }
        self.last_reconciliation = view
        self._audit(
            "PAPER_RUNNER_LIVE_RECONCILED",
            reconciled=view["reconciled"],
            recovered=len(recovered),
        )
        return view

    # -- internals -------------------------------------------------------

    def _strategy_approved(self) -> bool:
        lifecycle = self.lifecycle
        if lifecycle is None:
            return True
        approve = getattr(lifecycle, "approve_active", None)
        return bool(approve()) if callable(approve) else False

    def _ledger_rows(self) -> list[dict[str, Any]] | None:
        if self.ledger_positions is not None:
            try:
                return [dict(row) for row in self.ledger_positions()]
            except Exception:
                return None
        portfolio = self.portfolio
        if portfolio is None:
            return None
        try:
            return [position.audit_view() for position in portfolio.positions.values()]
        except Exception:
            return None

    def _duplicate(self, request_id: str) -> bool:
        if not request_id:
            return False
        if any(item.get("request_id") == request_id for item in self.history):
            return True
        if any(item.get("request_id") == request_id for item in getattr(self, "_seen", ())):
            return True
        repository = self.repository
        if repository is None:
            return False
        try:
            records = repository.list_orders()
        except Exception:
            # An unreadable ledger cannot prove there is no duplicate.
            return True
        return any(str(record.get("request_id", "")) == request_id for record in records)

    def _book(self, plan: Any, outcome: Any) -> dict[str, Any]:
        """Book a broker-confirmed fill into the ledger, using the ledger's own API.

        Quantity and price come from the terminal's answer, never from the plan: a
        partial fill is booked as the size that actually traded.
        """

        if self.portfolio is None:
            return {"booked": False, "error": "no_ledger"}
        action = str(getattr(plan, "action", "") or "").upper()
        quantity = Decimal(str(getattr(outcome, "volume_filled", 0) or 0))
        price = getattr(outcome, "price", None) or getattr(
            getattr(plan, "request", None), "executable_price", None
        )
        if price is None or quantity <= 0:
            error = "no_fill_price_or_volume"
            self.booking_failures.append({"error": error, "action": action})
            return {"booked": False, "error": error}
        at = self.clock()
        ticket = int(
            getattr(outcome, "order_ticket", 0) or getattr(outcome, "deal_ticket", 0) or 0
        )
        try:
            if action == "CLOSE":
                position_id = str(getattr(plan, "position_id", "") or "")
                if not position_id:
                    raise ValueError("close_without_position")
                trade = self.portfolio.close(
                    position_id,
                    price=Decimal(str(price)),
                    at=at,
                    cutoffs=self.rollover_cutoffs,
                    quantity=quantity,
                )
                entry = {
                    "booked": True,
                    "action": "CLOSE",
                    "position_id": position_id,
                    "quantity": str(quantity),
                    "price": str(price),
                    "realized_pnl_account": str(getattr(trade, "realized_pnl_account", "")),
                    "order_ticket": ticket,
                }
            else:
                position_id = f"mt5:{ticket}" if ticket else f"mt5:{at.isoformat()}"
                side = getattr(getattr(plan, "request", None), "side", None)
                self.portfolio.open(
                    position_id=position_id,
                    side=side,
                    quantity=quantity,
                    price=Decimal(str(price)),
                    at=at,
                )
                entry = {
                    "booked": True,
                    "action": "OPEN",
                    "position_id": position_id,
                    "quantity": str(quantity),
                    "price": str(price),
                    "order_ticket": ticket,
                }
        except Exception as exc:
            failure = {"error": type(exc).__name__, "detail": str(exc), "action": action}
            self.booking_failures.append(failure)
            self._audit("PAPER_RUNNER_LIVE_FILL_NOT_BOOKED", **failure)
            return {"booked": False, **failure}
        self.booked.append(entry)
        self._audit(
            "PAPER_RUNNER_LIVE_FILL_BOOKED",
            action=entry["action"],
            position_id=entry["position_id"],
            quantity=entry["quantity"],
            price=entry["price"],
            order_ticket=ticket,
        )
        return entry

    def _refuse(
        self,
        reasons: Sequence[str],
        *,
        request_id: str = "",
        detail: str = "",
        blockers: tuple[LiveBlocker, ...] = (),
        gate: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        outcome = LiveOutcome(
            submitted=False,
            reasons=tuple(dict.fromkeys(str(reason) for reason in reasons if reason)),
            detail=detail,
            request_id=request_id,
            blockers=blockers,
            gate=gate,
            at=self.clock(),
        )
        result = outcome.as_result()
        self.last_outcome = result
        self.history.append(result)
        self._audit(
            "PAPER_RUNNER_LIVE_ORDER_REFUSED",
            reasons=list(outcome.reasons),
            request_id=request_id,
            detail=detail,
        )
        return result

    def _audit(self, event_type: str, **fields: Any) -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.record({"event_type": event_type, **fields})
        except Exception:
            pass


def _differences(comparison: Any) -> str:
    """A short, non-secret summary of what the broker and ledger disagree about."""

    parts: list[str] = []
    for name in ("broker_only", "ledger_only", "volume_mismatch", "side_mismatch"):
        try:
            value = getattr(comparison, name, ()) or ()
        except Exception:
            value = ()
        if value:
            parts.append(f"{name}={len(value)}")
    return ",".join(parts) or "unknown_difference"


def _iso(value: Any) -> str:
    try:
        return value.isoformat()
    except AttributeError:
        return str(value or "")


#: Backwards-compatible alias for the earlier name of this class.
LiveExecutionLeg = LiveLeg

__all__ = [
    "ACCOUNT_MISMATCH",
    "ADAPTER_MISSING",
    "BLOCKING_REASONS",
    "BOOKING_FAILED",
    "DUPLICATE_REQUEST",
    "GATE_REFUSED",
    "LiveBlocker",
    "LiveExecutionLeg",
    "LiveLeg",
    "LiveOutcome",
    "MODE_NOT_LIVE",
    "NOT_ARMED",
    "NOT_CONFIRMED",
    "NOT_CONNECTED",
    "POSITIONS_MISMATCH",
    "ROUTER_MISSING",
    "ROUTE_REFUSED",
    "SPEC_MISMATCH",
    "STATUS_UNREADABLE",
    "STRATEGY_NOT_APPROVED",
    "SUBMIT_REFUSED",
    "TRADING_DISABLED",
    "UNRESOLVED_ORDER",
]
