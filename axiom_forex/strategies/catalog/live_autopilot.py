"""The selected strategy, running on AXIOM's existing LIVE path.

Selection (``selection.py``), the selection-to-paper handoff (``autopilot.py``)
and this module are three steps of one chain:

* :class:`~axiom_forex.strategies.catalog.selection.SelectionPolicy` ranks the
  evaluated candidates and picks at most a handful;
* :class:`~axiom_forex.strategies.catalog.autopilot.Autopilot` folds the chosen
  candidate into one strategy and runs it against simulated fills;
* :class:`LiveAutopilot` points that same, already-running strategy at the LIVE
  path AXIOM already had before any of this existed.

There is no new order path here.  ``LiveAutopilot`` owns no router, no gate, no
adapter client and no plan of its own; it *starts* the operator-facing host that
already does all of that:

* :class:`~axiom_forex.paper_runner.live_host.LiveRunnerHost` refuses to start
  unless the connected account is armed, the mode is LIVE, and the plan matches
  the lane it is about to run;
* its runner then executes each signal through
  :meth:`~axiom_forex.paper_runner.live.LiveLeg.submit_plan`, which consults the
  same execution gate, the same risk limits, the same exact-account
  confirmation, the same stop requirements and the same unresolved-order lockout
  as an operator-armed run.

So this module adds a *front door* to an execution path, not a way around one.
It cannot:

* arm anything (``live_workflow.arm`` stays the operator's, with its phrase and
  its acknowledged review);
* widen a risk limit (the plan may still only tighten the resolved setup);
* send an order (the host sends; this module only starts the host);
* make PAPER send one (a paper lane has no live leg at all);
* let Kronos open, close or size a trade (the veto gate can only decline a
  signal the strategy already produced).

Everything it decides is visible through :meth:`LiveAutopilot.view`: the
strategy the plan names, the pair and timeframe, the last signal, the last order
result or refusal, how old the quote it reads is, and how long the last decision
and submission actually took.  A missing prerequisite becomes one named blocker
and submission stays disabled; nothing is inferred, defaulted or faked.

The caller supplies ``spec`` (the resolved broker symbol) and ``adapter`` when it
wants a measured feed age; without them the feed is reported as *not measured*,
never as fresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

# -- the states a live attempt can be in -------------------------------------

IDLE = "IDLE"
BLOCKED = "BLOCKED"
STARTING = "STARTING"
RUNNING = "RUNNING"
STOPPED = "STOPPED"

STATES = (IDLE, BLOCKED, STARTING, RUNNING, STOPPED)

# -- blockers, named one at a time -------------------------------------------
#
# Each of these means "this specific prerequisite is absent", never "something
# went wrong".  The caller shows the message and leaves the control disabled;
# none of them is a reason to fall back to paper, to retry, or to relax a check.

NO_AUTOPILOT = "no_autopilot"
SELECTION_NOT_APPLIED = "selection_not_applied"
PAPER_NOT_RUNNING = "paper_not_running"
NO_LIVE_WORKFLOW = "live_workflow_unavailable"
NOT_CONNECTED = "mt5_not_connected"
NOT_ARMED = "strategy_not_armed"
PLAN_MISSING = "plan_missing"
PLAN_NOT_SELECTED = "plan_not_for_selected_strategy"
STACK_UNAVAILABLE = "live_stack_unavailable"

FLAT = "flat"
LONG = "long"
SHORT = "short"

SUBMITTED = "submitted"
REFUSED = "refused"
HELD = "held"
NONE = "none"


def _now() -> datetime:
    return datetime.now(UTC)


def _text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class Blocker:
    """One missing prerequisite, in the operator's words."""

    reason: str
    message: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class FeedState:
    """How old the quote the strategy reads actually was.

    ``fresh`` is ``None`` when the age could not be measured at all -- which is
    not the same as fresh, and is reported as such.
    """

    symbol: str = ""
    timeframe: str = ""
    quoted_at: str = ""
    age_seconds: float | None = None
    stale: bool | None = None
    bid: str = ""
    ask: str = ""
    source: str = ""
    detail: str = ""

    @property
    def fresh(self) -> bool | None:
        if self.stale is None:
            return None
        return not self.stale

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "quoted_at": self.quoted_at,
            "age_seconds": self.age_seconds,
            "fresh": self.fresh,
            "bid": self.bid,
            "ask": self.ask,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SignalState:
    """The last thing the strategy said, and what became of it.

    ``outcome`` is one of ``submitted``, ``refused``, ``held`` (a signal the
    gate or the Kronos veto declined to act on -- the operator can see which) or
    ``none``.  In PAPER, ``submitted`` means a *simulated* fill.
    """

    side: str = FLAT
    at: str = ""
    why: str = ""
    outcome: str = NONE
    detail: str = ""
    order_ref: str = ""

    @property
    def acted(self) -> bool:
        return self.outcome == SUBMITTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "at": self.at,
            "why": self.why,
            "outcome": self.outcome,
            "detail": self.detail,
            "order_ref": self.order_ref,
        }


@dataclass(frozen=True)
class LiveOrder:
    """One order the *existing* path produced, as its audit trail records it."""

    request_id: str
    at: str
    status: str
    refusal: str = ""
    detail: str = ""
    unresolved: bool = False
    booked: bool = False

    @property
    def refused(self) -> bool:
        return self.status == REFUSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "at": self.at,
            "status": self.status,
            "refusal": self.refusal,
            "detail": self.detail,
            "unresolved": self.unresolved,
            "booked": self.booked,
        }


@dataclass(frozen=True)
class Latency:
    """Measured durations.  Only what was actually timed appears here.

    ``decision_ms`` is wall-clock time the runner spent deciding one signal;
    ``submit_ms`` is the time spent inside the live submission call.  Neither is
    a fill latency, neither is measured against the broker's clock, and neither
    is an execution promise: both are observed on whatever machine runs it.
    """

    decision_ms: float | None = None
    submit_ms: float | None = None
    start_ms: float | None = None
    view_ms: float | None = None
    cycles: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_ms": self.decision_ms,
            "submit_ms": self.submit_ms,
            "start_ms": self.start_ms,
            "view_ms": self.view_ms,
            "cycles": self.cycles,
        }


@dataclass(frozen=True)
class LiveView:
    """Everything one look at the live attempt should show."""

    state: str
    mode: str
    paper: bool
    order_sending: bool
    strategy: Mapping[str, Any] = field(default_factory=dict)
    selection: Mapping[str, Any] = field(default_factory=dict)
    pair: str = ""
    broker_symbol: str = ""
    timeframe: str = ""
    blockers: tuple[Blocker, ...] = ()
    feed: FeedState = field(default_factory=FeedState)
    signal: SignalState = field(default_factory=SignalState)
    orders: tuple[LiveOrder, ...] = ()
    latency: Latency = field(default_factory=Latency)
    positions: tuple[Mapping[str, Any], ...] = ()
    kronos: Mapping[str, Any] = field(default_factory=dict)
    host: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""
    message: str = ""
    reason: str = ""

    @property
    def armed(self) -> bool:
        return bool(self.strategy.get("armed"))

    @property
    def open_positions(self) -> int:
        return len(self.positions)

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def plan_matches_selection(self) -> bool | None:
        known = self.selection.get("candidate_id")
        armed = self.strategy.get("candidate_id")
        if not known or not armed:
            return None
        return str(known) == str(armed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "mode": self.mode,
            "paper": self.paper,
            "order_sending": self.order_sending,
            "armed": self.armed,
            "ready": self.ready,
            "plan_matches_selection": self.plan_matches_selection,
            "strategy": dict(self.strategy),
            "selection": dict(self.selection),
            "pair": self.pair,
            "broker_symbol": self.broker_symbol,
            "timeframe": self.timeframe,
            "blockers": [item.as_dict() for item in self.blockers],
            "feed": self.feed.as_dict(),
            "signal": self.signal.as_dict(),
            "orders": [item.as_dict() for item in self.orders],
            "positions": [dict(item) for item in self.positions],
            "open_positions": self.open_positions,
            "latency": self.latency.as_dict(),
            "kronos": dict(self.kronos),
            "host": dict(self.host),
            "note": self.note,
            "message": self.message,
            "reason": self.reason,
        }


class LiveAutopilot:
    """Point the selected strategy at AXIOM's existing LIVE path.

    ``trading`` is the :class:`~axiom_forex.paper_runner.live_workflow.LiveTrading`
    the workstation already builds for the Live execution page.  This class adds
    no execution machinery of its own: it calls that object's ``start_session``
    and ``stop_session``, which refuse unless the operator has armed a plan on
    the current account, and it renders that object's ``view``.

    Nothing is connected, armed or started in the constructor:
    :meth:`start` is the only method that can begin sending, and it refuses --
    with a named blocker -- unless every prerequisite is present *now*.

    ``spec`` and ``feed_provider`` only affect what the *feed freshness* row can
    say when no session is running.  A running session's own feed is read first,
    because it recorded the quote it actually accepted; a page that cannot measure
    freshness reports "not measured" rather than guessing.
    """

    def __init__(
        self,
        *,
        autopilot: Any = None,
        trading: Any = None,
        mode: Any = None,
        spec: Any = None,
        feed_provider: Callable[[], FeedState | Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._autopilot = autopilot
        self._trading = trading
        self._mode = mode
        self._spec = spec
        self._feed_provider = feed_provider
        self._clock = clock
        self._state = IDLE
        self._reason = ""
        self._message = ""
        self._started_at = ""
        self._stopped_at = ""
        self.last_error = ""
        self.latency = Latency()

    # -- what the caller can see ------------------------------------------

    @property
    def trading(self) -> Any:
        return self._trading

    @property
    def state(self) -> str:
        return self._state

    # -- where the strategy and the plan come from ------------------------

    def _autopilot_status(self) -> dict[str, Any]:
        if self._autopilot is None:
            return {}
        try:
            status = self._autopilot.status()
        except Exception as got:  # noqa: BLE001 - a broken autopilot is a blocker
            self.last_error = f"{type(got).__name__}: {got}"
            return {}
        return _mapping(status)

    def selection(self) -> dict[str, Any]:
        """What the selection chain says is active, and whether it is running.

        This is the operator's own answer to "which strategy is this?", taken
        from the autopilot that folded the candidates -- never guessed from a
        symbol name or from a record on disk.
        """

        status = self._autopilot_status()
        if not status:
            return {}
        handoff: dict[str, Any] = {}
        if callable(getattr(self._autopilot, "handoff", None)):
            try:
                handoff = _mapping(self._autopilot.handoff())
            except Exception as got:  # noqa: BLE001
                self.last_error = f"{type(got).__name__}: {got}"
        proposed = [
            dict(item) for item in handoff.get("proposed") or () if isinstance(item, Mapping)
        ]
        summary = _mapping(status.get("selection"))
        single = proposed[0] if len(proposed) == 1 else {}
        return {
            "state": _text(status.get("state")),
            "reason": str(status.get("reason") or ""),
            "paper_running": bool(_mapping(status.get("paper")).get("running")),
            "proposed": proposed,
            "proposed_count": len(proposed),
            "candidate_id": str(single.get("candidate_id") or ""),
            "pair": str(single.get("pair") or ""),
            "timeframe": str(single.get("timeframe") or ""),
            "selected_count": summary.get("selected"),
            "evaluated_at": str(summary.get("evaluated_at") or ""),
            "data_origin": str(summary.get("data_origin") or ""),
        }

    def _trading_view(self) -> dict[str, Any]:
        if self._trading is None:
            return {}
        try:
            return _mapping(self._trading.view())
        except Exception as got:  # noqa: BLE001 - unreadable is a blocker, not a pass
            self.last_error = f"{type(got).__name__}: {got}"
            return {}

    def _resolved_plan(self, view: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The plan the operator armed -- read from the live path, not invented."""

        if view is None:
            view = self._trading_view()
        plan = view.get("plan")
        if isinstance(plan, Mapping) and plan and plan.get("present", True) is not False:
            return dict(plan)
        stored = getattr(self._trading, "plan", None)
        if isinstance(stored, Mapping) and stored and stored.get("present", True) is not False:
            return dict(stored)
        return {}

    def _runner_view(self, view: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if view is None:
            view = self._trading_view()
        return _mapping(view.get("runner"))

    def _strategy_view(self, view: Mapping[str, Any] | None = None) -> dict[str, Any]:
        plan = self._resolved_plan(view)
        selection = self.selection()
        connected = bool((view or self._trading_view()).get("connected", False))
        armed = bool((view or self._trading_view()).get("armed", False))
        return {
            "candidate_id": str(plan.get("candidate_id") or ""),
            "strategy_label": str(plan.get("strategy_label") or ""),
            "pair": str(plan.get("pair") or ""),
            "broker_symbol": str(plan.get("broker_symbol") or ""),
            "quantity": str(plan.get("quantity") or ""),
            "max_open_positions": plan.get("max_open_positions"),
            "max_daily_loss_account": str(plan.get("max_daily_loss_account") or ""),
            "account_currency": str(plan.get("account_currency") or ""),
            "evidence_state": str(plan.get("evidence_state") or ""),
            "plan_id": str(plan.get("plan_id") or ""),
            "armed": armed,
            "connected": connected,
            "selected_candidate_id": str(selection.get("candidate_id") or ""),
        }

    def _kronos_view(self) -> dict[str, Any]:
        status = self._autopilot_status()
        if not status:
            return {
                "available": False,
                "policy": "off",
                "note": "No selection is running, so nothing consults a forecast.",
            }
        return {
            "available": True,
            "policy": str(status.get("kronos_policy") or "off"),
            "view": _mapping(status.get("kronos")),
            "note": (
                "Kronos is an optional input that can only decline a signal the strategy "
                "already produced. It never opens, closes or sizes a trade."
            ),
        }

    # -- blockers ---------------------------------------------------------

    def blockers(self) -> tuple[Blocker, ...]:
        """Every reason submission is not allowed right now, one at a time.

        The selection prerequisites come first because they answer "which
        strategy"; the live path's own blockers follow, unchanged and unfiltered,
        so the operator sees the same list the Live execution page shows.
        """

        found: list[Blocker] = []
        selection = self.selection()
        state = _text(selection.get("state")).upper()
        if self._autopilot is None:
            found.append(
                Blocker(
                    NO_AUTOPILOT,
                    "No selection is connected, so no strategy is active for live execution.",
                )
            )
        elif not state or (state == IDLE and not selection.get("proposed_count")):
            found.append(
                Blocker(
                    SELECTION_NOT_APPLIED,
                    "No strategy has been selected and applied yet. Select a candidate on the "
                    "autopilot page first; LIVE starts from that same strategy.",
                )
            )
        elif state != RUNNING:
            found.append(
                Blocker(
                    PAPER_NOT_RUNNING,
                    f"The selected strategy is {state or 'not running'}, not running in the paper "
                    "test. Start it there first: that is how the active strategy is established.",
                    detail=str(selection.get("reason") or ""),
                )
            )

        if self._trading is None:
            found.append(
                Blocker(
                    NO_LIVE_WORKFLOW,
                    "This workstation was not started with the live wiring, so no live session "
                    "can be started here.",
                )
            )
            return tuple(found)

        live = self._trading_view()
        plan = self._resolved_plan(live)
        selected = str(selection.get("candidate_id") or "")
        armed = str(plan.get("candidate_id") or "")
        if not plan:
            found.append(
                Blocker(
                    PLAN_MISSING,
                    "No live plan is written for this strategy. Review and arm a plan on the "
                    "Live execution page first.",
                )
            )
        elif selected and armed and selected != armed:
            found.append(
                Blocker(
                    PLAN_NOT_SELECTED,
                    f"The armed plan is for {armed}, but the selected strategy is {selected}. "
                    "Arm a plan for the selected strategy; this page will not start another one.",
                )
            )
        if plan and not bool(live.get("armed", False)):
            found.append(
                Blocker(
                    NOT_ARMED,
                    "The live plan is written but not armed. Arming needs the reviewed plan, "
                    "the account signature the terminal is on now, and the typed phrase.",
                )
            )

        # The live path's own list, unchanged: mode, arming, connection, account,
        # specs, risk limits.  Prefixing nothing, hiding nothing.
        for item in live.get("blockers") or ():
            if isinstance(item, Mapping):
                found.append(
                    Blocker(
                        str(item.get("reason") or ""),
                        str(item.get("message") or ""),
                        str(item.get("hint") or ""),
                    )
                )
        return tuple(found)

    # -- the two things this object can do --------------------------------

    def start(self) -> LiveView:
        """Begin automatic live execution through the existing live session.

        Refuses -- and stays disabled -- when any prerequisite is missing.  The
        live path re-checks arming, mode, connection and the plan itself, so this
        is a second door onto the same lock, never a bypass of it.
        """

        if self._state == RUNNING:
            return self.view()

        blockers = self.blockers()
        if blockers:
            self._state = BLOCKED
            self._reason = blockers[0].reason
            self._message = "; ".join(item.message for item in blockers if item.message)
            return self.view()

        started = self._clock()
        self._state = STARTING
        try:
            result = _mapping(self._trading.start_session())
        except Exception as got:  # noqa: BLE001 - the live path owns its own refusals
            self.last_error = f"{type(got).__name__}: {got}"
            self._state = BLOCKED
            self._reason = STACK_UNAVAILABLE
            self._message = f"The live session could not be started: {got}"
            return self.view()
        ended = self._clock()

        if not bool(result.get("started", False)):
            self._state = BLOCKED
            self._reason = str(result.get("reason") or STACK_UNAVAILABLE)
            self._message = str(result.get("message") or "The live session did not start.")
            return self.view()

        self._state = RUNNING
        self._reason = ""
        self._message = (
            "Live session running: every signal now goes through the existing guarded path "
            "(gate, risk limits, account check, unresolved-order lockout). "
            + str(result.get("message") or "")
        ).strip()
        self._started_at = started.isoformat()
        self.latency = Latency(start_ms=(ended - started).total_seconds() * 1000.0)
        return self.view()

    def stop(self) -> LiveView:
        """Stop new submissions and disarm.  Open positions stay open."""

        if self._trading is not None and self._state in {RUNNING, STARTING}:
            try:
                self._trading.stop_session(reason="operator_stopped_autopilot")
            except Exception as got:  # noqa: BLE001 - report, do not hide
                self.last_error = f"{type(got).__name__}: {got}"
        self._state = STOPPED
        self._reason = ""
        self._message = (
            "Stopped and disarmed: no new order will be sent. Any position already open stays "
            "open in the terminal until you close it."
        )
        self._stopped_at = self._clock().isoformat()
        return self.view()

    # -- reading what happened --------------------------------------------

    def _orders(self, runner: Mapping[str, Any], limit: int = 5) -> tuple[LiveOrder, ...]:
        """The live leg's own history: ``LiveOutcome.as_result()``, unchanged.

        Reading it never sends.  A refusal is reported as a refusal, an order the
        broker left in an unknown state keeps ``unresolved`` set, and nothing is
        re-worded into a success.
        """

        leg = _mapping(runner.get("live")) or runner
        history = leg.get("history")
        if not isinstance(history, (list, tuple)):
            return ()
        rows: list[LiveOrder] = []
        for entry in list(history)[-limit:]:
            if not isinstance(entry, Mapping):
                continue
            submitted = bool(entry.get("submitted", False))
            reasons = [str(item) for item in entry.get("reasons") or () if str(item)]
            refusal = str(entry.get("reason") or (reasons[0] if reasons else ""))
            rows.append(
                LiveOrder(
                    request_id=str(entry.get("request_id") or ""),
                    at=str(entry.get("at") or ""),
                    status=str(entry.get("state") or "") if submitted else REFUSED,
                    refusal="" if submitted else refusal,
                    detail=str(entry.get("detail") or ""),
                    unresolved=bool(entry.get("unresolved", False)),
                    booked=bool(entry.get("booked", False)),
                )
            )
        return tuple(rows)

    def _signal(self, runner: Mapping[str, Any], view: Mapping[str, Any]) -> SignalState:
        """The last signal the strategy produced, and what became of it.

        The strategy's own ``last_signal`` and the runner's newest record are the
        source; the live leg's history turns that signal into "submitted" or
        "refused".  A signal with no order behind it is shown as held, never as
        an order.
        """

        orders = self._orders(runner, limit=1)
        order = orders[0] if orders else None
        raw = str(runner.get("last_signal") or "").strip().upper()
        side = {"BUY": LONG, "SELL": SHORT, "LONG": LONG, "SHORT": SHORT}.get(raw, FLAT)
        recent = [item for item in runner.get("recent") or () if isinstance(item, Mapping)]
        newest = recent[0] if recent else {}
        outcome_view = _mapping(view.get("last_outcome"))
        why = str(newest.get("reason") or "")
        outcome = NONE
        if order is not None:
            outcome = REFUSED if order.refused else SUBMITTED
            why = why or order.refusal or order.detail
        elif raw and raw != "HOLD":
            outcome = HELD
        return SignalState(
            side=side,
            at=str(runner.get("last_signal_at") or runner.get("last_decision_at") or ""),
            why=why,
            outcome=outcome,
            detail=str(newest.get("detail") or outcome_view.get("detail") or ""),
            order_ref=order.request_id if order else "",
        )

    def _feed(self, strategy: Mapping[str, Any], runner: Mapping[str, Any] | None = None) -> FeedState:
        """How old the quote the strategy reads is.

        The running session's own feed answers first: it already recorded the
        timestamp of the newest quote it accepted, so nothing extra is polled and
        no window is consumed.  Only when no session is running does this read a
        single quote from the adapter.  It never sends, never retries, and never
        reports an unreadable feed as fresh.
        """

        symbol = str(strategy.get("pair") or "")
        timeframe = str(self.selection().get("timeframe") or "")
        from_session = self._feed_from_session(symbol, timeframe, runner or {})
        if from_session is not None:
            return from_session
        if self._feed_provider is not None:
            try:
                given = self._feed_provider()
            except Exception as got:  # noqa: BLE001
                return FeedState(
                    symbol=symbol,
                    timeframe=timeframe,
                    detail=f"feed unreadable: {type(got).__name__}: {got}",
                )
            if isinstance(given, FeedState):
                return given
            data = _mapping(given)
            if not data:
                return FeedState(
                    symbol=symbol, timeframe=timeframe, detail="the feed provider returned nothing"
                )
            return FeedState(
                symbol=str(data.get("symbol") or symbol),
                timeframe=str(data.get("timeframe") or timeframe),
                quoted_at=str(data.get("quoted_at") or ""),
                age_seconds=data.get("age_seconds"),
                stale=data.get("stale"),
                bid=str(data.get("bid") or ""),
                ask=str(data.get("ask") or ""),
                source=str(data.get("source") or "supplied"),
                detail=str(data.get("detail") or ""),
            )

        adapter = getattr(self._trading, "adapter", None)
        if adapter is None or self._spec is None:
            return FeedState(
                symbol=symbol,
                timeframe=timeframe,
                detail="feed age is not measured here: no broker quote is available to this page",
            )
        try:
            quote = adapter.quote(self._spec)
            quoted_at = quote.timestamp_utc()
        except Exception as got:  # noqa: BLE001 - an unreadable feed is not fresh
            return FeedState(
                symbol=symbol,
                timeframe=timeframe,
                detail=f"quote unavailable: {type(got).__name__}: {got}",
            )
        if quoted_at.tzinfo is None:
            quoted_at = quoted_at.replace(tzinfo=UTC)
        age = (self._clock() - quoted_at).total_seconds()
        # Five seconds is the runner's own bound on a usable quote: the paper feed
        # refuses anything older, so it is shown as stale here too.
        stale = age > 5.0
        return FeedState(
            symbol=symbol,
            timeframe=timeframe,
            quoted_at=quoted_at.isoformat(),
            age_seconds=age,
            stale=stale,
            bid=str(getattr(quote, "bid", "") or ""),
            ask=str(getattr(quote, "ask", "") or ""),
            source="adapter_quote",
            detail=f"the newest quote is {age:.1f}s old" if stale else "",
        )

    def _feed_from_session(
        self, symbol: str, timeframe: str, runner: Mapping[str, Any]
    ) -> FeedState | None:
        """Freshness straight from the running feed, or ``None`` if it is silent."""

        data = _mapping(runner.get("data"))
        tick = str(data.get("last_tick_at") or runner.get("last_tick_at") or "")
        if not tick:
            return None
        policy = _mapping(data.get("policy"))
        try:
            quoted_at = datetime.fromisoformat(tick)
        except ValueError:
            return FeedState(
                symbol=str(data.get("symbol") or symbol),
                timeframe=timeframe,
                quoted_at=tick,
                source="session_feed",
                detail="the feed's newest quote timestamp could not be read",
            )
        if quoted_at.tzinfo is None:
            quoted_at = quoted_at.replace(tzinfo=UTC)
        age = (self._clock() - quoted_at).total_seconds()
        limit = _optional_float(policy.get("max_quote_age_seconds"))
        bound = limit if limit is not None else 5.0
        stale = age > bound
        return FeedState(
            symbol=str(data.get("symbol") or symbol),
            timeframe=timeframe,
            quoted_at=quoted_at.isoformat(),
            age_seconds=age,
            stale=stale,
            source="session_feed",
            detail=(
                f"the newest quote the feed accepted is {age:.1f}s old "
                f"(the feed refuses past {bound:g}s)"
                if stale
                else ""
            ),
        )

    def view(self) -> LiveView:
        read_started = self._clock()
        live = self._trading_view()
        read_ended = self._clock()
        strategy = self._strategy_view(live)
        selection = self.selection()
        runner = self._runner_view(live)
        mode = str(live.get("mode") or "") or _text(getattr(self._mode, "mode", ""))
        running = self._state == RUNNING or bool(live.get("session_running", False))
        if live.get("session_running") and self._state == IDLE:
            # A session started from the Live execution page is still this
            # strategy on this account; show it rather than pretending it is idle.
            running = True
        # Both the live path's own verdict and the mode's name must agree.  A paper
        # lane can never be shown as sending, whatever a view claims.
        order_sending = bool(live.get("next_signal_sends_real_order", False)) and (
            mode.upper() == "LIVE"
        )
        state = self._state
        reason = self._reason
        message = self._message
        session_running = bool(live.get("session_running", False))
        if state in {IDLE, STOPPED} and session_running:
            # Started from the Live execution page: the same strategy, on the same
            # account, through the same path.  Shown as running rather than hidden.
            state = RUNNING
        elif state == RUNNING and live and not session_running:
            # The live path stopped the session itself (disconnect, unknown
            # outcome, a failed booking).  Saying RUNNING here would be a lie.
            state = BLOCKED
            reason = str(
                runner.get("stopped_reason") or live.get("stopped_reason") or "live_session_stopped"
            )
            message = str(runner.get("stopped_detail") or reason)
        read_ms = (read_ended - read_started).total_seconds() * 1000.0
        latency = Latency(
            decision_ms=self.latency.decision_ms,
            submit_ms=self.latency.submit_ms,
            start_ms=self.latency.start_ms,
            view_ms=read_ms,
            cycles=self.latency.cycles,
        )
        if running and runner:
            # Only what the runner actually recorded.  An absent duration stays
            # absent rather than becoming a zero the page would read as instant.
            latency = Latency(
                decision_ms=_optional_float(runner.get("decision_ms")) or latency.decision_ms,
                submit_ms=_optional_float(runner.get("submit_ms")) or latency.submit_ms,
                start_ms=latency.start_ms,
                view_ms=read_ms,
                cycles=int(runner.get("cycles") or latency.cycles or 0),
            )
        if read_ms > 0 and self.latency.view_ms != read_ms:
            self.latency = Latency(
                decision_ms=latency.decision_ms,
                submit_ms=latency.submit_ms,
                start_ms=latency.start_ms,
                view_ms=read_ms,
                cycles=latency.cycles,
            )
        return LiveView(
            state=state,
            mode=mode,
            paper=mode.upper() != "LIVE",
            order_sending=order_sending,
            strategy=strategy,
            selection=selection,
            pair=str(strategy.get("pair") or selection.get("pair") or ""),
            broker_symbol=str(strategy.get("broker_symbol") or ""),
            timeframe=str(selection.get("timeframe") or ""),
            blockers=self._live_blockers(live, runner) if running else self.blockers(),
            feed=self._feed(strategy, runner),
            signal=self._signal(runner, live),
            orders=self._orders(runner),
            latency=latency,
            positions=tuple(
                dict(item) for item in live.get("positions") or () if isinstance(item, Mapping)
            ),
            kronos=self._kronos_view(),
            host=runner,
            note=self._note(mode),
            message=message,
            reason=reason,
        )

    def _live_blockers(
        self, live: Mapping[str, Any], runner: Mapping[str, Any]
    ) -> tuple[Blocker, ...]:
        """While running, the live path's own refusals are the ones that matter."""

        found: list[Blocker] = []
        for item in runner.get("blockers") or ():
            if isinstance(item, Mapping):
                found.append(
                    Blocker(
                        str(item.get("reason") or item.get("code") or ""),
                        str(item.get("message") or item.get("detail") or ""),
                        str(item.get("detail") or item.get("hint") or ""),
                    )
                )
            else:
                found.append(Blocker(str(item), str(item)))
        stopped = str(runner.get("stopped_reason") or live.get("stopped_reason") or "")
        if stopped:
            found.append(
                Blocker(stopped, str(runner.get("stopped_detail") or stopped))
            )
        if not found and not live.get("connected", False):
            found.append(
                Blocker(NOT_CONNECTED, "MetaTrader 5 is no longer connected; no order can be sent.")
            )
        return tuple(found)

    def _note(self, mode: str) -> str:
        if mode.upper() != "LIVE":
            return (
                "PAPER: fills are simulated by the same runner that would send in LIVE. "
                "No order submission is reachable from this mode."
            )
        return (
            "LIVE: each signal goes through the existing gate, risk limits, account check and "
            "unresolved-order lockout. A refusal is shown as-is; nothing is retried, and this "
            "page never claims a fill the adapter did not report."
        )

    def status(self) -> dict[str, Any]:
        return self.view().as_dict()


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_live_autopilot(
    *,
    autopilot: Any = None,
    trading: Any = None,
    mode: Any = None,
    spec: Any = None,
    feed_provider: Callable[[], FeedState | Mapping[str, Any]] | None = None,
    clock: Callable[[], datetime] = _now,
) -> LiveAutopilot:
    """One call site for the workstation, so the wiring stays obvious."""

    return LiveAutopilot(
        autopilot=autopilot,
        trading=trading,
        mode=mode,
        spec=spec,
        feed_provider=feed_provider,
        clock=clock,
    )


__all__ = [
    "BLOCKED",
    "Blocker",
    "FeedState",
    "FLAT",
    "HELD",
    "IDLE",
    "Latency",
    "LiveAutopilot",
    "LiveOrder",
    "LiveView",
    "LONG",
    "NONE",
    "NO_AUTOPILOT",
    "NO_LIVE_WORKFLOW",
    "NOT_ARMED",
    "NOT_CONNECTED",
    "PAPER_NOT_RUNNING",
    "PLAN_MISSING",
    "PLAN_NOT_SELECTED",
    "REFUSED",
    "RUNNING",
    "SELECTION_NOT_APPLIED",
    "SHORT",
    "STACK_UNAVAILABLE",
    "STARTING",
    "STATES",
    "STOPPED",
    "SUBMITTED",
    "SignalState",
    "build_live_autopilot",
]
