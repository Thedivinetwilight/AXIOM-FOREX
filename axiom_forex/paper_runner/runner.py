"""The paper strategy runner: one explicit loop, one order path, nothing live.

What one cycle does, in order:

1. **Guard.**  Refuse to continue unless the execution mode is ``PAPER``, the
   candidate strategy has been selected by the recorded evaluation, the
   instrument specification validates, there is no unresolved paper order, and
   no earlier intent was left without an order.  Any failure stops the loop and
   is reported with a reason code.
2. **Poll.**  Ask the feed for the newest completed bar.  Ticks update the
   ``last tick`` instant; a stale tick, a data gap, or a lost terminal produces a
   reason code and no order.
3. **Decide.**  Hand the strategy the bounded window of completed bars that ended
   at or before the decision instant.  ``HOLD`` is reported as
   ``hold_no_signal`` -- an explicit "nothing to do", not a silent pause.
4. **Plan.**  The *existing* planner turns the signal into an order request and a
   gate context, refusing any quote stamped before the decision bar closed.
5. **Journal.**  The intent is written durably before anything is executed.
6. **Execute.**  The *existing* paper engine re-runs the pre-trade gate, reserves,
   settles, and writes the fill to the local ledger.  A gate refusal is recorded
   with its own code; nothing is retried.
7. **Persist.**  Counters, instants, and the reason reach the journal; the book
   reaches a portfolio snapshot.

The runner holds no MetaTrader 5 adapter, no arming control, and no order-sending
surface.  It cannot connect, cannot change the mode, and cannot call
``order_send``: the only execution object in the graph is the paper engine.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from app.execution.broker import TradingBlocked

from axiom_forex.execution.paper_engine import PaperExecutionEngine
from axiom_forex.execution.planner import ExecutionPlan, ExecutionRejection
from axiom_forex.strategies.base import Signal

from .feed import (
    BAR,
    DISCONNECTED,
    END,
    FATAL_EVENTS,
    GAP,
    INVALID,
    KIND_REPLAY,
    STALE,
    TICK,
    BarFeed,
)
from .journal import RunnerJournal
from .strategy import STATUS_SELECTED, Candidate, status_of

# -- states -----------------------------------------------------------------
STOPPED = "STOPPED"
RUNNING = "RUNNING"
BLOCKED = "BLOCKED"

# -- reason codes -----------------------------------------------------------
REASON_FRESH_START = "fresh_start"
REASON_RESTORED_STOPPED = "restored_after_restart"
REASON_OPERATOR_START = "operator_started"
REASON_OPERATOR_STOP = "operator_stopped"
REASON_NO_NEW_BAR = "no_new_bar"
REASON_WAITING_FOR_HISTORY = "waiting_for_history"
REASON_HOLD = "hold_no_signal"
REASON_NO_ACTION = "signal_without_action"
REASON_MARKET_CLOSED = "market_closed"
REASON_STALE_QUOTE = "stale_quote"
REASON_DATA_GAP = "data_gap"
REASON_INVALID_BAR = "invalid_bar"
REASON_NO_EXECUTION_PRICE = "no_execution_price"
REASON_FEED_ENDED = "feed_exhausted"
REASON_DISCONNECTED = "feed_disconnected"
REASON_OPENED = "paper_position_opened"
REASON_CLOSED = "paper_position_closed"
REASON_GATE_REFUSED = "gate_refused"
REASON_LIVE_MODE_ACTIVE = "live_mode_active"
REASON_MODE_UNKNOWN = "execution_mode_unknown"
REASON_STRATEGY_NOT_SELECTED = "candidate_not_selected"
REASON_INVALID_SPEC = "invalid_instrument_spec"
REASON_UNRESOLVED_ORDER = "unresolved_order_present"
REASON_INTENT_UNMATCHED = "intent_without_order"
REASON_JOURNAL_FAILED = "journal_write_failed"
REASON_RECOVERY_FAILED = "state_recovery_failed"
REASON_CYCLE_LIMIT = "cycle_limit_reached"
REASON_CYCLE_FAILED = "cycle_failed"
REASON_LIVE_NOT_CONFIGURED = "live_path_not_configured"
REASON_LIVE_NOT_READY = "live_not_ready"

#: Reasons that stop the loop and require an operator decision.
STOPPING_REASONS = frozenset(
    {
        REASON_LIVE_MODE_ACTIVE,
        REASON_MODE_UNKNOWN,
        REASON_LIVE_NOT_CONFIGURED,
        REASON_LIVE_NOT_READY,
        REASON_STRATEGY_NOT_SELECTED,
        REASON_INVALID_SPEC,
        REASON_UNRESOLVED_ORDER,
        REASON_INTENT_UNMATCHED,
        REASON_JOURNAL_FAILED,
        REASON_RECOVERY_FAILED,
        REASON_FEED_ENDED,
        REASON_DISCONNECTED,
        REASON_CYCLE_LIMIT,
    }
)

#: How many cycle records the read model keeps for the UI.
MAX_RECENT = 25


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


@dataclass(frozen=True)
class RunnerBlocker:
    """One reason the runner cannot run, and what clears it."""

    reason: str
    message: str
    hint: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": self.message, "hint": self.hint}


@dataclass(frozen=True)
class RunnerConfig:
    """Everything the runner needs to know that is not a live object."""

    run_id: str
    symbol: str
    quantity: Decimal
    max_cycles: int = 0
    pause_seconds: float = 1.0

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not str(self.run_id).strip():
            errors.append("missing_run_id")
        if not str(self.symbol).strip():
            errors.append("missing_symbol")
        try:
            if Decimal(str(self.quantity)) <= 0:
                errors.append("invalid_quantity")
        except Exception:
            errors.append("invalid_quantity")
        if self.max_cycles < 0:
            errors.append("invalid_max_cycles")
        return tuple(errors)


@dataclass
class Lane:
    """One instrument's paper stack, reusing the platform's own components.

    ``portfolio`` and ``paper_execution`` are mutable on purpose: restart
    recovery restores the book *in place* so every object that already holds a
    reference (the context factory, the planner, the gate) keeps seeing the
    restored book instead of a stale one.
    """

    spec: Any
    planner: Any
    portfolio: Any
    paper_execution: PaperExecutionEngine
    quote_freshness: Any
    audit_log: Any = None
    repository: Any = None

    @property
    def pair(self) -> str:
        return str(getattr(self.spec, "canonical_pair", "") or "")

    @property
    def config(self) -> Any:
        return getattr(self.planner, "config", None)


class PaperStrategyRunner:
    """Runs one documented candidate on one instrument, in paper mode only."""

    def __init__(
        self,
        *,
        lane: Lane,
        feed: BarFeed,
        strategy: Any,
        candidate: Candidate,
        config: RunnerConfig,
        mode: Any,
        journal: RunnerJournal | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] | None = None,
        live: Any = None,
        allow_unvalidated: bool = False,
    ) -> None:
        errors = config.validate()
        if errors:
            raise ValueError("invalid_runner_config: " + ",".join(errors))
        self.lane = lane
        self.feed = feed
        self.strategy = strategy
        self.candidate = candidate
        self.config = config
        self.mode = mode
        #: The live execution leg, when this deployment has one.  Its presence
        #: does not enable live trading: the mode, the arming, and every gate in
        #: the leg still have to be satisfied first.
        self.live = live
        #: Live only: a strategy the operator armed on purpose may run without
        #: measured results.  It is labelled UNVALIDATED everywhere, and paper
        #: still refuses it -- this relaxes nothing about the order path.
        self.allow_unvalidated = bool(allow_unvalidated)
        self.unvalidated_live = False
        self.journal = journal
        self.clock = clock or _utc_now
        import time as _time

        self._sleep = sleep or _time.sleep
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self.state = STOPPED
        self.reason = REASON_FRESH_START
        self.reason_detail = ""
        self.blockers: tuple[RunnerBlocker, ...] = ()
        self.started_at: datetime | None = None
        self.stopped_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.last_signal_at: datetime | None = None
        self.last_decision_at: datetime | None = None
        self.last_fill_at: datetime | None = None
        self.last_signal = Signal.HOLD.value
        self.counters: dict[str, int] = {
            "cycles": 0,
            "bars": 0,
            "decisions": 0,
            "signals": 0,
            "orders": 0,
            "fills": 0,
            "closes": 0,
            "refusals": 0,
        }
        self.recent: list[dict[str, Any]] = []
        self._sequence = 0
        self._bars: list[Any] = []
        self._restored = False
        self._restored_from: str = ""
        self.recovered_orders = 0
        self.recovered_fills = 0
        self.live_orders = 0
        self.last_live_outcome: dict[str, Any] = {}
        self.last_reconciliation: dict[str, Any] = {}
        self._equity: Decimal | None = None
        self._realized: Decimal = Decimal(0)

    # -- read model ------------------------------------------------------

    def status(self) -> str:
        return status_of(
            self.candidate.candidate_id,
            {**self.candidate.evidence, "evaluated": bool(self.candidate.evidence)},
        )

    def _may_run_unvalidated_live(self) -> bool:
        """True only for a live leg, a live mode, and an explicit operator opt-in."""

        if not self.allow_unvalidated or self.live is None:
            return False
        mode_name = str(getattr(self.mode, "mode", "") or "").upper()
        return mode_name == "LIVE" and bool(getattr(self.mode, "live_effective", False))

    def guard_blockers(self) -> tuple[RunnerBlocker, ...]:
        """Everything that must be true before an order may be placed."""

        blockers: list[RunnerBlocker] = []

        mode_name = str(getattr(self.mode, "mode", "") or "").upper()
        live_effective = bool(getattr(self.mode, "live_effective", False))
        if not mode_name:
            blockers.append(
                RunnerBlocker(
                    REASON_MODE_UNKNOWN,
                    "The execution mode could not be read, so nothing may run.",
                    "Open the Trading mode screen and choose paper or live.",
                )
            )
        elif mode_name == "LIVE" or live_effective:
            # Live is a real path, but only when a live leg exists and every one
            # of its preconditions holds.  There is no fallback to paper.
            if self.live is None:
                blockers.append(
                    RunnerBlocker(
                        REASON_LIVE_NOT_CONFIGURED,
                        "Live mode is selected but this runner has no live execution "
                        "path configured, so no order can be placed.",
                        "Start the workstation with the MT5 live path configured, or "
                        "switch to paper. The runner never changes the mode itself.",
                    )
                )
            else:
                for item in self.live.blockers():
                    blockers.append(
                        RunnerBlocker(
                            REASON_LIVE_NOT_READY,
                            str(getattr(item, "message", "") or item.reason),
                            str(getattr(item, "hint", "") or ""),
                        )
                    )
        elif mode_name != "PAPER":
            blockers.append(
                RunnerBlocker(
                    REASON_MODE_UNKNOWN,
                    f"The execution mode is {mode_name}, which this runner cannot execute.",
                    "Choose paper or live on the Trading mode screen.",
                )
            )

        status = self.status()
        self.unvalidated_live = False
        if status != STATUS_SELECTED:
            if self._may_run_unvalidated_live():
                # The operator armed this strategy deliberately, the plan says
                # UNVALIDATED, and the page says so.  Live only: paper keeps the
                # selection rule.
                self.unvalidated_live = True
            else:
                blockers.append(
                    RunnerBlocker(
                        REASON_STRATEGY_NOT_SELECTED,
                        f"{self.candidate.candidate_id} is {status}, so it may not run on "
                        "paper yet.",
                        "Run the chronological evaluation on real imported history and let it "
                        "select the candidate; synthetic fixtures and demo exports can never "
                        "select one.",
                    )
                )

        try:
            validation = self.lane.spec.validate()
            spec_ok = bool(getattr(validation, "valid", False))
            spec_reason = str(getattr(validation, "reason", "") or "")
        except Exception as exc:  # a spec that cannot even validate is invalid
            spec_ok = False
            spec_reason = type(exc).__name__
        if not spec_ok:
            blockers.append(
                RunnerBlocker(
                    REASON_INVALID_SPEC,
                    "The instrument specification is invalid"
                    + (f" ({spec_reason})" if spec_reason else "")
                    + ", so no order size, cost, or tick size can be trusted.",
                    "Fix the instrument catalogue entry for this pair, then restart the "
                    "runner.",
                )
            )

        try:
            unresolved = self.lane.paper_execution.unresolved_orders()
        except Exception as exc:
            unresolved = (type(exc).__name__,)
        if unresolved:
            blockers.append(
                RunnerBlocker(
                    REASON_UNRESOLVED_ORDER,
                    f"{len(unresolved)} paper order(s) are in an unresolved state, so the "
                    "book may not reflect reality.",
                    "Inspect the paper orders and resolve them; the runner will not stack a "
                    "new order on top of an in-doubt one.",
                )
            )

        unmatched = self._unmatched_intents()
        if unmatched:
            blockers.append(
                RunnerBlocker(
                    REASON_INTENT_UNMATCHED,
                    f"{len(unmatched)} recorded intent(s) have no matching paper order, so a "
                    "previous process stopped between planning and execution.",
                    "Check whether that order exists in the ledger; if it does not, the "
                    "intent is stale and this run needs an operator decision before trading "
                    "again.",
                )
            )
        return tuple(blockers)

    def view(self) -> dict[str, Any]:
        # The read model never raises: a page must be able to show *why* the
        # runner is unhappy even when the objects it reads are the problem.
        try:
            positions = [
                position.audit_view()
                for position in self.lane.portfolio.positions.values()
            ]
        except Exception:
            positions = []
        try:
            fills = [dict(fill) for fill in self.lane.paper_execution.fills()]
        except Exception:
            fills = []
        blockers = [blocker.audit_view() for blocker in self.blockers]
        return {
            "run_id": self.config.run_id,
            "state": self.state,
            "running": self.state == RUNNING,
            "reason": self.reason,
            "reason_detail": self.reason_detail,
            "blockers": blockers,
            "blocked": bool(blockers),
            "strategy": {
                "candidate_id": self.candidate.candidate_id,
                "status": self.status(),
                "parameters": dict(self.candidate.parameters),
                "rationale": self.candidate.rationale,
                "assumptions": list(self.candidate.assumptions),
                "limitations": list(self.candidate.limitations),
                "evidence": dict(self.candidate.evidence),
            },
            "symbol": self.config.symbol,
            "pair": self.lane.pair,
            "quantity": str(self.config.quantity),
            "data": self.feed.describe(),
            "last_tick_at": _iso(self.last_tick_at),
            "last_signal_at": _iso(self.last_signal_at),
            "last_decision_at": _iso(self.last_decision_at),
            "last_fill_at": _iso(self.last_fill_at),
            "last_signal": self.last_signal,
            "counters": dict(self.counters),
            "recent": [dict(item) for item in reversed(self.recent[-MAX_RECENT:])],
            "positions": positions,
            "fills": fills[-25:],
            "open_positions": len(positions),
            "fill_count": len(fills),
            "equity_account": str(self._equity) if self._equity is not None else "",
            "realized_pnl_account": str(self._realized),
            "started_at": _iso(self.started_at),
            "stopped_at": _iso(self.stopped_at),
            "restored": self._restored,
            "restored_from": self._restored_from,
            "recovered_orders": self.recovered_orders,
            "recovered_fills": self.recovered_fills,
            "max_cycles": self.config.max_cycles,
            "execution_mode": str(getattr(self.mode, "mode", "") or "UNKNOWN"),
            "live_effective": bool(getattr(self.mode, "live_effective", False)),
            "live_configured": self.live is not None,
            "live": self.live.status() if self.live is not None else {},
            "live_orders": self.live_orders,
            "last_live_outcome": dict(self.last_live_outcome),
            "last_reconciliation": dict(self.last_reconciliation),
            "paper_only": self.live is None,
            "trading_capability": (
                "live_broker_orders" if self.live is not None else "paper_simulation_only"
            ),
            "order_sending": self.live is not None,
            # A strategy the operator armed without measured results is labelled
            # here, in the plan, and on the page -- never quietly upgraded.
            "unvalidated": bool(self.unvalidated_live),
            "evidence_state": "UNVALIDATED" if self.unvalidated_live else "MEASURED",
            "warnings": (
                [
                    "This strategy has no measured result behind it. It is running LIVE "
                    "because an operator armed it deliberately; that is not evidence of "
                    "anything except the decision."
                ]
                if self.unvalidated_live
                else []
            ),
            "stops": sorted(STOPPING_REASONS),
        }

    # -- lifecycle -------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Begin cycling.  Refuses -- and says why -- if any guard fails."""

        with self._lock:
            if self.state == RUNNING:
                return self.view()
            blockers = self.guard_blockers()
            if blockers:
                self.blockers = blockers
                self.state = BLOCKED
                self.reason = blockers[0].reason
                self.reason_detail = blockers[0].message
                self._record("BLOCKED", self.reason, self.reason_detail, set_reason=False)
                self._persist()
                self._audit(
                    "PAPER_RUNNER_BLOCKED",
                    reason=blockers[0].reason,
                    blockers=[blocker.reason for blocker in blockers],
                )
                return self.view()
            self.blockers = ()
            self.state = RUNNING
            self.reason = REASON_OPERATOR_START
            self.reason_detail = ""
            self.started_at = self.clock()
            self.stopped_at = None
            self._stop_event.clear()
            self._record("STARTED", self.reason, "", set_reason=False)
            self._persist()
            self._audit("PAPER_RUNNER_STARTED", strategy=self.candidate.candidate_id)
            return self.view()

    def stop(self, *, reason: str = REASON_OPERATOR_STOP, detail: str = "") -> dict[str, Any]:
        """Stop cycling.  Never places, cancels, or changes anything."""

        with self._lock:
            was_running = self.state == RUNNING
            self._stop_event.set()
            self.state = STOPPED
            self.reason = reason
            self.reason_detail = detail
            self.stopped_at = self.clock()
            self.blockers = ()
            if was_running or reason != REASON_OPERATOR_STOP:
                self._record("STOPPED", reason, detail, set_reason=False)
            self._persist()
            if was_running:
                self._audit("PAPER_RUNNER_STOPPED", reason=reason)
            return self.view()

    def _block(self, blocker: RunnerBlocker) -> None:
        self.state = BLOCKED
        self.reason = blocker.reason
        self.reason_detail = blocker.message
        self.blockers = (blocker,)
        self._stop_event.set()
        self._record("BLOCKED", blocker.reason, blocker.message, set_reason=False)
        self._persist()
        self._audit("PAPER_RUNNER_BLOCKED", reason=blocker.reason)

    # -- the loop --------------------------------------------------------

    def step(self) -> dict[str, Any]:
        """Run exactly one cycle.  Safe to call directly, from tests or the loop."""

        with self._lock:
            if self.state != RUNNING:
                return {"reason": "not_running", "state": self.state, "filled": False}
            blockers = self.guard_blockers()
            if blockers:
                self._block(blockers[0])
                return {"reason": blockers[0].reason, "state": self.state, "filled": False}
            if self._at_cycle_limit():
                self.stop(reason=REASON_CYCLE_LIMIT, detail=f"max_cycles={self.config.max_cycles}")
                return {"reason": REASON_CYCLE_LIMIT, "state": self.state, "filled": False}
            try:
                return self._cycle()
            except Exception as exc:  # a crash must never leave the loop "running"
                self.stop(
                    reason=REASON_CYCLE_FAILED,
                    detail=f"{type(exc).__name__}: {exc}",
                )
                return {"reason": REASON_CYCLE_FAILED, "state": self.state, "filled": False}

    def run(self, *, max_steps: int | None = None) -> dict[str, Any]:
        """Step synchronously until stopped or the step budget is spent.

        The UI runs the loop on its own thread; tests use this so nothing is
        timing-dependent.
        """

        budget = max_steps if max_steps is not None else 0
        steps = 0
        while self.state == RUNNING:
            if budget and steps >= budget:
                break
            self.step()
            steps += 1
            if self.state != RUNNING:
                break
            self._sleep(max(float(self.config.pause_seconds), 0.0))
        return self.view()

    def start_thread(self) -> dict[str, Any]:
        """Start the loop on a daemon thread (the UI's Start button)."""

        view = self.start()
        if view["state"] != RUNNING:
            return view
        self._thread = threading.Thread(
            target=self._loop, name=f"paper-runner-{self.config.run_id}", daemon=True
        )
        self._thread.start()
        return self.view()

    def _loop(self) -> None:
        while not self._stop_event.is_set() and self.state == RUNNING:
            self.step()
            if self.state != RUNNING:
                break
            self._stop_event.wait(max(float(self.config.pause_seconds), 0.0))

    def stop_thread(self, *, reason: str = REASON_OPERATOR_STOP, detail: str = "") -> dict[str, Any]:
        view = self.stop(reason=reason, detail=detail)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        return view

    # -- one cycle -------------------------------------------------------

    def _cycle(self) -> dict[str, Any]:
        self.counters["cycles"] += 1
        event = self.feed.poll()

        if event.kind == TICK:
            self.last_tick_at = event.quote.timestamp_utc() if event.quote else self.clock()
            self._record(REASON_NO_NEW_BAR, "", "")
            self._persist()
            return {"reason": REASON_NO_NEW_BAR, "state": self.state, "filled": False}

        if event.kind in FATAL_EVENTS:
            reason = (
                REASON_FEED_ENDED
                if event.kind == END
                else REASON_DISCONNECTED
                if event.kind == DISCONNECTED
                else INVALID
            )
            detail = event.reason or event.detail or event.kind
            self.stop(reason=reason, detail=detail)
            return {"reason": reason, "state": self.state, "filled": False}

        if event.kind == STALE:
            self.counters["refusals"] += 1
            self._record(REASON_STALE_QUOTE, event.reason, event.detail)
            self._persist()
            return {"reason": REASON_STALE_QUOTE, "state": self.state, "filled": False}

        if event.kind == GAP:
            self.counters["refusals"] += 1
            self._record(REASON_DATA_GAP, event.reason, event.detail)
            self._persist()
            return {"reason": REASON_DATA_GAP, "state": self.state, "filled": False}

        if event.kind != BAR:
            self.counters["refusals"] += 1
            self._record(REASON_INVALID_BAR, event.reason, event.detail)
            self._persist()
            return {"reason": REASON_INVALID_BAR, "state": self.state, "filled": False}

        bar = event.bar
        assert bar is not None
        validation = bar.validate(self.lane.spec)
        if not validation.valid:
            self.counters["refusals"] += 1
            self._record(REASON_INVALID_BAR, "bar_invalid", validation.reason)
            self._persist()
            return {"reason": REASON_INVALID_BAR, "state": self.state, "filled": False}

        self.last_tick_at = bar.last_quote_at
        self.counters["bars"] += 1
        self._bars.append(bar)
        history_limit = int(getattr(self.strategy, "history_limit", 1))
        if len(self._bars) > history_limit:
            del self._bars[: len(self._bars) - history_limit]

        if len(self._bars) < history_limit:
            self._record(REASON_WAITING_FOR_HISTORY, "", f"{len(self._bars)}/{history_limit} bars")
            self._persist()
            return {"reason": REASON_WAITING_FOR_HISTORY, "state": self.state, "filled": False}

        signal = self.strategy.generate_signal(tuple(self._bars))
        self.counters["decisions"] += 1
        self.last_decision_at = bar.end_at
        self.last_signal = signal.value
        self.last_signal_at = self.clock()
        if signal == Signal.HOLD:
            self._record(REASON_HOLD, "HOLD", "no entry or exit condition was met")
            self._persist()
            return {"reason": REASON_HOLD, "state": self.state, "filled": False}

        self.counters["signals"] += 1
        if event.quote is None:
            self.counters["refusals"] += 1
            self._record(
                REASON_NO_EXECUTION_PRICE,
                signal.value,
                "the feed has no later price to fill at; the final bar is never traded",
            )
            self._persist()
            return {"reason": REASON_NO_EXECUTION_PRICE, "state": self.state, "filled": False}

        return self._execute(signal=signal, bar=bar, quote=event.quote)

    def _execute(self, *, signal: Signal, bar: Any, quote: Any) -> dict[str, Any]:
        freshness = self.lane.planner.context_factory.quote_freshness
        decision = freshness.check(quote, self._evaluation_instant(quote))
        if not decision.ok:
            self.counters["refusals"] += 1
            self._record(REASON_STALE_QUOTE, decision.reason.value, f"age={decision.age_seconds}")
            self._persist()
            return {"reason": REASON_STALE_QUOTE, "state": self.state, "filled": False}

        plan = self.lane.planner.plan_at_quote(
            signal=signal,
            positions=self.lane.portfolio.positions,
            quote=quote,
            decision_bar=bar,
            decision_index=self._sequence,
            run_id=self.config.run_id,
            strategy_id=self.candidate.candidate_id,
        )
        if plan is None:
            self._record(REASON_NO_ACTION, signal.value, "the book is already in that position")
            self._persist()
            return {"reason": REASON_NO_ACTION, "state": self.state, "filled": False}
        if isinstance(plan, ExecutionRejection):
            self.counters["refusals"] += 1
            reason = (
                REASON_MARKET_CLOSED
                if plan.code == "MARKET_CLOSED"
                else REASON_STALE_QUOTE
                if plan.code == "STALE_QUOTE"
                else REASON_GATE_REFUSED
            )
            self._record(reason, plan.code, plan.reason)
            self._persist()
            return {"reason": reason, "state": self.state, "filled": False}

        assert isinstance(plan, ExecutionPlan)
        self._sequence += 1
        try:
            self._write_intent(plan, bar)
        except Exception as exc:
            self._block(
                RunnerBlocker(
                    REASON_JOURNAL_FAILED,
                    f"The intent could not be recorded durably ({type(exc).__name__}), so no "
                    "order was executed.",
                    "Check the paper database is writable, then start the runner again.",
                )
            )
            return {"reason": REASON_JOURNAL_FAILED, "state": self.state, "filled": False}

        if self._live_active():
            result = self._execute_live(plan=plan, bar=bar)
            self._resolve_intent(plan, result)
            return result

        try:
            record, fill = self.lane.paper_execution.execute(
                plan.request,
                plan.context,
                fill_price=plan.fill_price,
                fill_at=plan.fill_at,
                action=plan.action,
                position_id=plan.position_id,
                cutoffs=tuple(getattr(self.lane.config, "rollover_cutoffs", ()) or ()),
            )
        except TradingBlocked as exc:
            self.counters["refusals"] += 1
            self._record(REASON_GATE_REFUSED, str(exc.reason), "")
            self._persist()
            self._audit(
                "PAPER_RUNNER_ORDER_REFUSED",
                reason=str(exc.reason),
                request_id=plan.request.request_id,
            )
            return {"reason": REASON_GATE_REFUSED, "state": self.state, "filled": False}

        if fill is None or str(getattr(record, "state", "")) != "FILLED":
            self.counters["refusals"] += 1
            self._record(
                REASON_GATE_REFUSED,
                str(getattr(record, "state", "")),
                str(getattr(record, "reason", "")),
            )
            self._persist()
            return {"reason": REASON_GATE_REFUSED, "state": self.state, "filled": False}

        self.counters["orders"] += 1
        self.counters["fills"] += 1
        if fill.action == "CLOSE":
            self.counters["closes"] += 1
        self.last_fill_at = fill.filled_at
        self._realized += fill.realized_pnl_account
        try:
            self._equity = self.lane.portfolio.equity_account(fill.filled_at, bar)
        except Exception:
            self._equity = None
        reason = REASON_OPENED if fill.action == "OPEN" else REASON_CLOSED
        self._record(
            reason,
            plan.signal.value,
            f"{fill.side.value} {fill.quantity} @ {fill.price}",
        )
        self._persist(fill_at=fill.filled_at)
        self._audit(
            "PAPER_RUNNER_ORDER_FILLED",
            request_id=plan.request.request_id,
            action=fill.action,
            side=fill.side.value,
            price=str(fill.price),
            simulation_id=fill.simulation_id,
        )
        return {
            "reason": reason,
            "state": self.state,
            "filled": True,
            "action": fill.action,
            "price": str(fill.price),
        }

    # -- the live path ---------------------------------------------------

    def _live_active(self) -> bool:
        """True only when the operator has live enabled *and* a leg exists."""

        mode_name = str(getattr(self.mode, "mode", "") or "").upper()
        live_effective = bool(getattr(self.mode, "live_effective", False))
        return self.live is not None and (mode_name == "LIVE" or live_effective)

    def _execute_live(self, *, plan: ExecutionPlan, bar: Any) -> dict[str, Any]:
        """One plan, one live order.  There is no paper fallback here."""

        outcome = self.live.execute(plan)
        reason = str(outcome.get("reason", "") or "")
        if not outcome.get("submitted"):
            self.counters["refusals"] += 1
            blockers = outcome.get("blockers") or ()
            first = blockers[0] if blockers else {}
            self._record(
                reason or REASON_LIVE_NOT_READY,
                str(first.get("reason", "") or outcome.get("detail", "")),
                str(first.get("message", "") or outcome.get("detail_hint", "")),
            )
            self.last_live_outcome = dict(outcome)
            self._persist()
            return {"reason": reason, "state": self.state, "filled": False}

        self.live_orders += 1
        self.last_live_outcome = dict(outcome)
        booked = bool(outcome.get("booked"))
        action = str(getattr(plan, "action", "") or "").upper()
        if booked:
            self.counters["orders"] += 1
            self.counters["fills"] += 1
            if action == "CLOSE":
                self.counters["closes"] += 1
            self.last_fill_at = self.clock()
            realized = (outcome.get("booking") or {}).get("realized_pnl_account")
            if realized not in (None, ""):
                try:
                    self._realized += Decimal(str(realized))
                except Exception:
                    pass
            try:
                self._equity = self.lane.portfolio.equity_account(
                    self.last_fill_at, bar
                )
            except Exception:
                self._equity = None
        self._record(
            reason,
            str(outcome.get("retcode_name", "") or action or ""),
            f"state={outcome.get('state', '')} filled={outcome.get('volume_filled', '')}"
            + (" booked" if booked else " not booked"),
        )
        self._persist(fill_at=self.last_fill_at)
        self._audit(
            "PAPER_RUNNER_LIVE_ORDER_RESULT",
            request_id=str(outcome.get("request_id", "")),
            state=str(outcome.get("state", "")),
            unknown=bool(outcome.get("unknown")),
            booked=booked,
        )
        if outcome.get("unknown"):
            # The adapter is disarmed on an unknown outcome; stop and let a human
            # reconcile instead of pretending the order did or did not fill.
            self._block(
                RunnerBlocker(
                    "live_order_outcome_unknown",
                    "The broker did not confirm the order, so its outcome is unknown "
                    "and the adapter is disarmed.",
                    "Reconcile on the Trading mode screen before starting again. "
                    "Nothing will be re-sent automatically.",
                )
            )
        elif not booked:
            self._block(
                RunnerBlocker(
                    "live_fill_not_booked",
                    "The broker filled the order but the fill could not be booked into "
                    "the ledger, so the book and the terminal may disagree.",
                    "Reconcile on the Trading mode screen; the ledger is never silently "
                    "edited to match a broker read.",
                )
            )
        return {"reason": reason, "state": self.state, "filled": booked}

    def reconcile(self) -> dict[str, Any]:
        """Resolve in-doubt orders and compare broker positions with the ledger."""

        if self.live is None:
            return {"available": False, "reason": REASON_LIVE_NOT_CONFIGURED}
        positions = []
        try:
            positions = [
                position.audit_view()
                for position in self.lane.portfolio.positions.values()
            ]
        except Exception:
            positions = []
        view = self.live.reconcile(positions)
        self.last_reconciliation = dict(view)
        self._persist()
        return view

    def _resolve_intent(self, plan: ExecutionPlan, result: Mapping[str, Any]) -> None:
        """Record how the intent ended.  Never a reason to stop the loop."""

        if self.journal is None:
            return
        if result.get("submitted"):
            status = "FILLED" if result.get("filled") else "SENT"
        elif result.get("filled"):
            status = "FILLED"
        else:
            status = "REFUSED"
        try:
            self.journal.resolve_intent(
                run_id=self.config.run_id,
                sequence=self._sequence,
                status=status,
                reason=str(result.get("reason", "") or ""),
                request_id=plan.request.request_id,
            )
        except Exception:
            # If the resolution cannot be written, the guard stays strict: the
            # next start sees a pending intent and asks the operator.
            pass

    # -- persistence -----------------------------------------------------

    def _write_intent(self, plan: ExecutionPlan, bar: Any) -> None:
        if self.journal is None:
            return
        self.journal.write_intent(
            run_id=self.config.run_id,
            sequence=self._sequence,
            payload={
                "request_id": plan.request.request_id,
                "action": plan.action,
                "side": plan.request.side.value,
                "quantity": str(plan.quantity),
                "fill_price": str(plan.fill_price),
                "fill_at": _iso(plan.fill_at),
                "decision_bar_end": _iso(bar.end_at),
                "basis": plan.basis,
                "signal": plan.signal.value,
                "strategy_id": self.candidate.candidate_id,
                "paper_only": True,
            },
        )

    def _evaluation_instant(self, quote: Any) -> datetime:
        """When freshness is judged.

        Terminal ticks are judged against the wall clock: that is what makes a
        frozen feed visible.  An offline replay is an explicitly labelled
        historic sequence, so its quotes are judged against their own timeline --
        its data class is ``HISTORICAL_REPLAY``/replay and it can never be
        mistaken for a live feed in the UI or the audit trail.
        """

        if str(getattr(self.feed, "kind", "")) == KIND_REPLAY:
            try:
                return quote.timestamp_utc()
            except Exception:
                return self.clock()
        return self.clock()

    def _persist(self, *, fill_at: datetime | None = None) -> None:
        if self.journal is None:
            return
        try:
            self.journal.save_state(run_id=self.config.run_id, payload=self._state_payload())
            anchor = fill_at or self.last_tick_at or self.clock()
            self.journal.save_portfolio(
                run_id=self.config.run_id,
                snapshot=self.lane.portfolio.snapshot(anchor, allow_unmarked=True),
            )
        except Exception:
            # Persistence failure is reported by the next guard through the
            # intent/ledger checks; it must not crash a running loop.
            pass

    def _state_payload(self) -> dict[str, Any]:
        return {
            "status": self.state,
            "reason": self.reason,
            "reason_detail": self.reason_detail,
            "counters": dict(self.counters),
            "sequence": self._sequence,
            "last_tick_at": _iso(self.last_tick_at),
            "last_signal_at": _iso(self.last_signal_at),
            "last_decision_at": _iso(self.last_decision_at),
            "last_fill_at": _iso(self.last_fill_at),
            "last_signal": self.last_signal,
            "started_at": _iso(self.started_at),
            "stopped_at": _iso(self.stopped_at),
            "candidate_id": self.candidate.candidate_id,
            "symbol": self.config.symbol,
            "execution_mode": str(getattr(self.mode, "mode", "") or "UNKNOWN"),
            "paper_only": self.live is None,
        }

    def _record(
        self, reason: str, code: str = "", detail: str = "", *, set_reason: bool = True
    ) -> None:
        # ``set_reason=False`` for a lifecycle transition: the log says "STARTED",
        # while the runner's *reason* stays the code the caller already set.
        if set_reason:
            self.reason = reason
            self.reason_detail = detail or code
        previous = self.recent[-1] if self.recent else None
        if (
            previous is not None
            and previous["reason"] == reason
            and previous["code"] == code
            and previous["detail"] == detail
            and previous["state"] == self.state
        ):
            # A quiet market must not push the interesting events out of the
            # journal: repeat the same "nothing to do" only when it changes.
            previous["at"] = _iso(self.clock())
            return
        self.recent.append(
            {
                "at": _iso(self.clock()),
                "reason": reason,
                "code": code,
                "detail": detail,
                "state": self.state,
            }
        )
        if len(self.recent) > MAX_RECENT:
            del self.recent[: len(self.recent) - MAX_RECENT]

    def _audit(self, event_type: str, **fields: Any) -> None:
        log = self.lane.audit_log
        if log is None:
            return
        payload = {
            "event_type": event_type,
            "run_id": self.config.run_id,
            "symbol": self.config.symbol,
            "strategy_id": self.candidate.candidate_id,
            "live": False,
            "order_sent": False,
            **fields,
        }
        log.record(payload)

    def _at_cycle_limit(self) -> bool:
        return bool(self.config.max_cycles) and self.counters["cycles"] >= self.config.max_cycles

    def _unmatched_intents(self) -> list[dict[str, Any]]:
        """Intents written before an execution that has no order row."""

        if self.journal is None or self.lane.repository is None:
            return []
        try:
            orders = {str(row.get("request_id", "")) for row in self.lane.repository.list_orders()}
        except Exception:
            return []
        unmatched = []
        for intent in self.journal.intents(self.config.run_id):
            request_id = str(intent.get("request_id", ""))
            # A resolved intent (refused, filled, or sent) is a known outcome.
            # Only an intent still PENDING -- or one written before this field
            # existed -- means the process stopped between decision and outcome.
            if str(intent.get("status", "PENDING")).upper() != "PENDING":
                continue
            if request_id and request_id not in orders:
                unmatched.append(intent)
        return unmatched

    # -- restart ---------------------------------------------------------

    def restore(self) -> dict[str, Any]:
        """Rebuild the book and counters from durable state.  Starts nothing.

        ``PaperStateRecovery`` is the platform's own invariant check: it refuses a
        database whose orders, fills, reservations, and settlement snapshots
        disagree.  The runner runs it first and only then restores *in place*, so
        every object already holding a reference to the book -- the context
        factory, the planner, the gate -- sees the restored positions instead of
        a stale copy.
        """

        from axiom_forex.database.audit import DurableAuditLog
        from axiom_forex.execution.recovery import PaperStateRecovery

        previous = self.journal.load_state(self.config.run_id) if self.journal else None
        repository = self.lane.repository
        if repository is not None:
            try:
                audit = self.lane.audit_log
                if not isinstance(audit, DurableAuditLog):
                    audit = DurableAuditLog(repository=repository)
                recovery = PaperStateRecovery(repository=repository, audit_log=audit)
                recovery.recover(
                    engine=self.lane.paper_execution, portfolio=self.lane.portfolio
                )
                state = repository.recovery_snapshot()
                base = PaperStateRecovery.base_portfolio_snapshot(state["snapshots"])
                if base is not None:
                    self.lane.portfolio.restore_snapshot(base)
                self.lane.paper_execution.restore(state["orders"], state["fills"])
                self._restored = True
                self._restored_from = (
                    str(previous.get("status")) if previous else "repository"
                )
                self.recovered_orders = len(state["orders"])
                self.recovered_fills = len(state["fills"])
            except Exception as exc:
                self.state = BLOCKED
                self.reason = REASON_RECOVERY_FAILED
                self.reason_detail = f"{type(exc).__name__}: {exc}"
                self.blockers = (
                    RunnerBlocker(
                        REASON_RECOVERY_FAILED,
                        "The durable paper state could not be recovered, so the runner "
                        f"refuses to start ({type(exc).__name__}).",
                        "Inspect the paper database; nothing was traded and nothing will be "
                        "until the state is consistent.",
                    ),
                )
                self._record(REASON_RECOVERY_FAILED, type(exc).__name__, str(exc))
                return self.view()

        if previous:
            counters = previous.get("counters") or {}
            for key in self.counters:
                try:
                    self.counters[key] = int(counters.get(key, self.counters[key]))
                except (TypeError, ValueError):
                    pass
            try:
                self._sequence = max(int(previous.get("sequence", 0)), self._sequence)
            except (TypeError, ValueError):
                pass
            self.last_tick_at = _parse(previous.get("last_tick_at"))
            self.last_signal_at = _parse(previous.get("last_signal_at"))
            self.last_decision_at = _parse(previous.get("last_decision_at"))
            self.last_fill_at = _parse(previous.get("last_fill_at"))
            self.last_signal = str(previous.get("last_signal", Signal.HOLD.value))

        # Restored state never auto-runs: an explicit Start is required, and an
        # unresolved book blocks that Start with a stated reason.
        self.state = STOPPED
        self.reason = REASON_RESTORED_STOPPED if previous else REASON_FRESH_START
        self.reason_detail = (
            "restored from durable state; press Start to resume cycles"
            if previous
            else ""
        )
        self.blockers = self.guard_blockers()
        if self.blockers:
            self.state = BLOCKED
            self.reason = self.blockers[0].reason
            self.reason_detail = self.blockers[0].message
        self._record(self.reason, "", self.reason_detail)
        self._persist()
        return self.view()


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
    return parsed


__all__ = [
    "BLOCKED",
    "Lane",
    "MAX_RECENT",
    "PaperStrategyRunner",
    "REASON_CYCLE_FAILED",
    "REASON_CYCLE_LIMIT",
    "REASON_DATA_GAP",
    "REASON_DISCONNECTED",
    "REASON_FEED_ENDED",
    "REASON_FRESH_START",
    "REASON_GATE_REFUSED",
    "REASON_HOLD",
    "REASON_INTENT_UNMATCHED",
    "REASON_INVALID_BAR",
    "REASON_INVALID_SPEC",
    "REASON_JOURNAL_FAILED",
    "REASON_LIVE_MODE_ACTIVE",
    "REASON_MARKET_CLOSED",
    "REASON_MODE_UNKNOWN",
    "REASON_NO_ACTION",
    "REASON_NO_EXECUTION_PRICE",
    "REASON_NO_NEW_BAR",
    "REASON_OPERATOR_START",
    "REASON_OPERATOR_STOP",
    "REASON_OPENED",
    "REASON_CLOSED",
    "REASON_RECOVERY_FAILED",
    "REASON_RESTORED_STOPPED",
    "REASON_STALE_QUOTE",
    "REASON_STRATEGY_NOT_SELECTED",
    "REASON_UNRESOLVED_ORDER",
    "REASON_WAITING_FOR_HISTORY",
    "RUNNING",
    "RunnerBlocker",
    "RunnerConfig",
    "STOPPED",
    "STOPPING_REASONS",
]
