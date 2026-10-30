"""The live session host: one strategy loop, on its own thread, sending real orders.

This is the live counterpart of :class:`axiom_forex.paper_runner.host.PaperRunnerHost`.
It owns exactly one worker thread, and everything that touches the broker stack
happens there: the platform is built on that thread from the terminal as it is at
that moment, the runner is restored and stepped there, and the page's reads are
submitted as small callables.

What it will not do
-------------------
* It does not connect, sign in, arm, or change the execution mode.  It starts
  only when the adapter is already connected **and** armed by an explicit
  operator decision, and the mode object it is given already says LIVE and
  confirmed.
* It does not retry a refused order and it never re-sends an in-doubt one.  The
  live leg refuses those before the broker is reached.
* It does not keep running through a failure.  After every cycle it re-reads the
  live leg's blockers; the first one that appears -- a disconnect, an account
  that no longer matches, a position the ledger cannot explain, an unresolved
  order, a symbol whose terms changed -- stops the loop and disarms.  A runner
  that cannot explain the world must not trade in it.

The one thing it *does* do is the point of the milestone: when armed, every
strategy signal goes through the same planner, the same pre-trade gate, and the
same risk limits as paper, and then to the broker.
"""

from __future__ import annotations

import queue
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .host import READ_TIMEOUT_SECONDS, RunnerSetupRefused
from .live import LiveLeg
from .live_plan import PlanError, plan_view
from .runner import PaperStrategyRunner, RunnerConfig

#: Why a live loop stopped itself.  These are the codes the page shows.
STOP_DISCONNECTED = "live_disconnected"
STOP_BLOCKED = "live_check_failed"
STOP_UNKNOWN_OUTCOME = "live_order_outcome_unknown"
STOP_BOOKING_FAILED = "live_fill_not_booked"
STOP_OPERATOR = "operator_stopped"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Reply:
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


class LiveRunnerHost:
    """Owns the live stack and the strategy loop on one worker thread."""

    def __init__(
        self,
        *,
        adapter: Any,
        mode: Any,
        plan: dict[str, Any],
        build_stack: Callable[[], Any],
        build_runner: Callable[[Any, Any, Any], PaperStrategyRunner],
        lane_from_platform: Callable[[Any], Any],
        lifecycle: Any = None,
        pause_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.adapter = adapter
        self.mode = mode
        self.plan = dict(plan or {})
        self._build_stack = build_stack
        self._build_runner = build_runner
        self._lane_from_platform = lane_from_platform
        self.lifecycle = lifecycle
        self._pause = max(float(pause_seconds), 0.05)
        self._clock = clock or _utc_now

        self._queue: queue.Queue[tuple[Callable[[], Any], _Reply]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._ready = threading.Event()

        self.platform: Any = None
        self.feed: Any = None
        self.leg: LiveLeg | None = None
        self.runner: PaperStrategyRunner | None = None
        self.error = ""
        self.blockers: list[dict[str, str]] = []
        #: How the loop last ended, as a code the page can show.
        self.stopped_reason = ""
        self.stopped_detail = ""

    # -- lifecycle -------------------------------------------------------

    def start(self, *, timeout: float = 30.0) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            return self.view()
        self._shutdown.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="axiom-live-runner-host", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout)
        return self.view()

    def close(self, *, timeout: float = 10.0) -> None:
        self._shutdown.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    @property
    def alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _run(self) -> None:
        self._ready.set()
        while not self._shutdown.is_set():
            self._drain()
            runner = self.runner
            if runner is None or runner.state != "RUNNING":
                self._shutdown.wait(self._pause)
                continue
            try:
                runner.step()
            except Exception as exc:  # runner.step fails closed; the loop must not
                self._stop_loop(STOP_BLOCKED, type(exc).__name__)
                continue
            self._guard()
            self._shutdown.wait(self._pause)

    def _guard(self) -> None:
        """Stop the loop the moment a precondition stops holding."""

        if not getattr(self.adapter, "connected", False):
            self._stop_loop(STOP_DISCONNECTED, "the terminal connection ended")
            return
        leg = self.leg
        if leg is None:
            return
        try:
            blockers = leg.blockers()
        except Exception as exc:  # an unreadable state is not a good state
            self._stop_loop(STOP_BLOCKED, type(exc).__name__)
            return
        if not blockers:
            return
        reasons = [item.reason for item in blockers]
        if any(reason == "live_order_outcome_unknown" for reason in reasons):
            self._stop_loop(STOP_UNKNOWN_OUTCOME, reasons[0])
        elif "live_fill_not_booked" in reasons:
            self._stop_loop(STOP_BOOKING_FAILED, reasons[0])
        elif "mt5_not_connected" in reasons:
            self._stop_loop(STOP_DISCONNECTED, reasons[0])
        else:
            self._stop_loop(STOP_BLOCKED, reasons[0])

    def _stop_loop(self, reason: str, detail: str = "") -> None:
        self.stopped_reason = reason
        self.stopped_detail = detail
        runner = self.runner
        if runner is not None and runner.state == "RUNNING":
            try:
                runner.stop(reason=reason, detail=detail)
            except Exception:
                pass
        # Fail closed: a loop that stopped for a broker-side reason is also
        # disarmed, so nothing can send from a stale authorisation.
        try:
            self.adapter.disarm(reason=reason)
        except Exception:
            pass

    # -- the queue -------------------------------------------------------

    def _drain(self) -> None:
        while True:
            try:
                task, reply = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                reply.value = task()
            except BaseException as exc:  # noqa: BLE001 - relayed, not swallowed
                reply.error = exc
            finally:
                reply.done.set()

    def submit(self, task: Callable[[], Any], *, timeout: float | None = None) -> Any:
        if not self.alive:
            raise RuntimeError(self.error or "live_runner_host_not_running")
        reply = _Reply()
        self._queue.put((task, reply))
        if not reply.done.wait(timeout or READ_TIMEOUT_SECONDS):
            raise TimeoutError("live_runner_host_busy")
        if reply.error is not None:
            raise reply.error
        return reply.value

    # -- actions ---------------------------------------------------------

    def start_runner(self) -> dict[str, Any]:
        """Build the live stack and start the loop.  Sends nothing on its own."""

        if not self.alive:
            return self._unavailable("live_runner_host_not_started")

        def task() -> dict[str, Any]:
            if self.runner is not None and self.runner.state == "RUNNING":
                return self.view()
            try:
                self._build_now()
            except RunnerSetupRefused as exc:
                self.error = exc.reason
                self.blockers = [exc.audit_view()]
                return self.view()
            except PlanError as exc:
                self.error = exc.reason
                self.blockers = [
                    {
                        "reason": exc.reason,
                        "message": "The live plan could not be used, so nothing was started.",
                        "hint": "Open Live execution and re-read the plan.",
                    }
                ]
                return self.view()
            except Exception as exc:
                print(
                    f"[axiom-workstation] live runner build failed: {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                self.error = "live_runner_build_failed"
                self.blockers = [
                    {
                        "reason": "live_runner_build_failed",
                        "message": (
                            "The live stack could not be built. Nothing was started and "
                            "no order was sent."
                        ),
                        "hint": "Check the console, then Start again. The mode was not changed.",
                    }
                ]
                return self.view()
            assert self.runner is not None
            started = self.runner.start()
            if started.get("state") != "RUNNING":
                self.blockers = [dict(item) for item in started.get("blockers", [])]
                self.error = str(started.get("reason") or "live_runner_refused")
            return self.view()

        return self.submit(task)

    def stop_runner(self, *, reason: str = STOP_OPERATOR, disarm: bool = True) -> dict[str, Any]:
        """Stop new submissions, and (by default) disarm the broker."""

        if not self.alive:
            return self._unavailable("live_runner_host_not_started")

        def task() -> dict[str, Any]:
            self.stopped_reason = reason
            if self.runner is not None and self.runner.state == "RUNNING":
                try:
                    self.runner.stop(reason=reason, detail="operator")
                except Exception:
                    pass
            if disarm:
                try:
                    self.adapter.disarm(reason=reason)
                except Exception:
                    pass
            return self.view()

        return self.submit(task)

    def _build_now(self) -> None:
        """Owner thread only: assemble the platform, the leg, the feed, the runner."""

        adapter = self.adapter
        if not getattr(adapter, "connected", False):
            raise RunnerSetupRefused(
                "mt5_not_connected",
                "The terminal is not connected, so there is no broker to send to.",
                "Press Connect, then arm and start again.",
            )
        if getattr(adapter, "armed", False) is not True:
            raise RunnerSetupRefused(
                "live_not_armed",
                "Live trading is not armed, so no order may be sent.",
                "Complete the review on Live execution and arm the plan.",
            )
        mode_name = str(getattr(self.mode, "mode", "") or "").upper()
        if mode_name != "LIVE" or not bool(getattr(self.mode, "live_effective", False)):
            raise RunnerSetupRefused(
                "live_mode_required",
                "The execution mode is not LIVE (confirmed) in this process.",
                "Enable LIVE on the Trading mode screen, then arm this plan.",
            )

        stack = self._build_stack()
        platform = stack[0] if isinstance(stack, tuple) else getattr(stack, "platform", None)
        feed = stack[1] if isinstance(stack, tuple) else getattr(stack, "feed", None)
        spec = stack[2] if isinstance(stack, tuple) else getattr(stack, "spec", None)
        if platform is None or feed is None or spec is None:
            raise RunnerSetupRefused(
                "live_stack_unavailable",
                "The broker stack could not be assembled from the provider setup.",
                "Check the provider setup page for the remaining blockers.",
            )
        if str(getattr(platform, "pair", "")) != str(self.plan.get("pair", "")):
            raise RunnerSetupRefused(
                "live_plan_pair_mismatch",
                "The live plan's symbol is not the symbol this stack resolved.",
                "Re-save the plan for the resolved symbol.",
            )
        self.platform = platform
        self.feed = feed
        self.leg = self._make_leg(platform, spec)
        self.runner = self._build_runner(platform, feed, self.leg)
        self.runner.restore()
        self.error = ""
        self.blockers = []
        self.stopped_reason = ""
        self.stopped_detail = ""

    def _make_leg(self, platform: Any, spec: Any) -> LiveLeg:
        from axiom_forex.execution.mode_router import ExecutionRouter

        router = ExecutionRouter(
            mode=self.mode,
            paper=None,
            live=self.adapter,
            audit=getattr(platform, "audit_log", None),
        )
        return LiveLeg(
            router=router,
            adapter=self.adapter,
            gate=getattr(platform, "gate", None),
            mode=self.mode,
            spec=spec,
            strategy_id=str(self.plan.get("candidate_id", "")),
            repository=getattr(platform, "repository", None),
            audit_log=getattr(platform, "audit_log", None),
            lifecycle=self.lifecycle,
            portfolio=getattr(platform, "portfolio", None),
            rollover_cutoffs=tuple(
                getattr(getattr(getattr(platform, "setup", None), "rollover", None), "cutoffs", ())
                or ()
            ),
            clock=self._clock,
        )

    # -- read model ------------------------------------------------------

    def view(self) -> dict[str, Any]:
        if self.error and self.runner is None:
            return self._blocked()
        if not self.alive:
            return self._unavailable("live_runner_host_not_started")
        if self.runner is None:
            return self._idle()
        try:
            # A task already running on the owner thread must read the runner
            # directly: submitting to the queue it is draining would deadlock.
            runner_view = (
                self.runner.view()
                if threading.current_thread() is self._thread
                else self.submit(lambda: self.runner.view())
            )
        except TimeoutError:
            return self._unavailable("busy")
        except Exception as exc:
            return self._unavailable(type(exc).__name__)
        view = dict(runner_view or {})
        leg_status = view.get("live") or {}
        if view.get("unvalidated"):
            view.setdefault("warnings", [])
            if not view["warnings"]:
                view["warnings"] = [
                    "This strategy has no measured result behind it; the operator armed "
                    "it deliberately."
                ]
        armed = bool(getattr(self.adapter, "armed", False))
        running = self.runner.state == "RUNNING"
        blockers = [dict(item) for item in leg_status.get("blockers", [])]
        view.update(
            {
                "available": True,
                "paper_only": False,
                "live_configured": True,
                "order_sending": True,
                "armed": armed,
                "stopped_reason": self.stopped_reason,
                "stopped_detail": self.stopped_detail,
                "plan": plan_view(self.plan),
                "next_signal_sends_real_order": bool(
                    running
                    and armed
                    and getattr(self.adapter, "connected", False)
                    and not blockers
                ),
            }
        )
        return view

    def _idle(self) -> dict[str, Any]:
        return {
            "available": True,
            "pending": True,
            "state": "READY",
            "running": False,
            "paper_only": False,
            "live_configured": True,
            "order_sending": True,
            "armed": bool(getattr(self.adapter, "armed", False)),
            "next_signal_sends_real_order": False,
            "plan": plan_view(self.plan),
            "blockers": [dict(item) for item in self.blockers],
            "reason": self.error,
            "stopped_reason": self.stopped_reason,
            "counters": {},
            "recent": [],
            "positions": [],
        }

    def _blocked(self) -> dict[str, Any]:
        return {
            "available": True,
            "pending": False,
            "blocked": True,
            "state": "BLOCKED",
            "running": False,
            "paper_only": False,
            "live_configured": True,
            "order_sending": True,
            "armed": bool(getattr(self.adapter, "armed", False)),
            "next_signal_sends_real_order": False,
            "plan": plan_view(self.plan),
            "blockers": [dict(item) for item in self.blockers],
            "reason": self.error,
            "stopped_reason": self.stopped_reason,
            "counters": {},
            "recent": [],
            "positions": [],
        }

    def _unavailable(self, reason: str = "") -> dict[str, Any]:
        return {
            "available": False,
            "state": "UNAVAILABLE",
            "running": False,
            "paper_only": False,
            "live_configured": True,
            "order_sending": True,
            "armed": bool(getattr(self.adapter, "armed", False)),
            "next_signal_sends_real_order": False,
            "plan": plan_view(self.plan),
            "reason": reason or self.error,
            "blockers": [dict(item) for item in self.blockers],
            "counters": {},
            "recent": [],
            "positions": [],
        }


__all__ = [
    "LiveRunnerHost",
    "STOP_BLOCKED",
    "STOP_BOOKING_FAILED",
    "STOP_DISCONNECTED",
    "STOP_OPERATOR",
    "STOP_UNKNOWN_OUTCOME",
]
