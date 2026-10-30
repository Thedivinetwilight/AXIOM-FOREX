"""One owner thread for the paper stack, so a running runner and the UI cannot race.

The paper platform's SQLite connection is bound to the thread that opened it, and
its portfolio, gate, and engine are plain mutable objects.  Running the strategy
loop on the web server's request threads would let two threads touch all three at
once.  So the runner gets its own thread, and *everything* that touches the paper
stack happens there:

* the platform is built on that thread;
* the runner restores and steps there;
* the UI's reads (the paper snapshot, the runner view) are submitted as small
  callables and executed there, one at a time, between cycles.

The relay is a queue of callables plus a future, nothing more.  A caller that
waits too long gets an explicit "busy" answer rather than a torn read, and no
caller can ever reach the platform object directly.
"""

from __future__ import annotations

import queue
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .runner import Lane, PaperStrategyRunner, RunnerConfig
from .strategy import Candidate

#: How long a UI read waits for the owner thread before giving up.
READ_TIMEOUT_SECONDS = 5.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunnerSetupRefused(Exception):
    """The runner cannot be built, for a reason the operator can act on.

    Carries a stable reason code, an operator-facing message, and a hint.  It is
    never rendered as raw exception text: the page shows the code, the message,
    and the hint, and nothing else.
    """

    def __init__(self, reason: str, detail: str = "", hint: str = "") -> None:
        super().__init__(reason)
        self.reason = str(reason)
        self.detail = str(detail)
        self.hint = str(hint)

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "message": self.detail, "hint": self.hint}


def lane_from_platform(platform: Any) -> Lane:
    """Compose the runner's lane from a platform's own, already-wired objects."""

    return Lane(
        spec=platform.spec,
        planner=platform.planner,
        portfolio=platform.portfolio,
        paper_execution=platform.paper_execution,
        quote_freshness=platform.quote_freshness,
        audit_log=platform.audit_log,
        repository=platform.repository,
    )


@dataclass
class _Reply:
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


class PaperRunnerHost:
    """Owns a paper platform and a runner on one worker thread.

    ``build_platform`` and ``build_feed`` are called on the worker thread, so a
    caller must not capture thread-affine objects inside them.
    """

    def __init__(
        self,
        *,
        build_platform: Callable[[], Any],
        build_feed: Callable[[Any], Any],
        build_runner: Callable[[Any, Any], PaperStrategyRunner] | None = None,
        runner_config: RunnerConfig | None = None,
        candidate: Candidate | None = None,
        strategy: Any = None,
        mode: Any = None,
        journal_factory: Callable[[Any], Any] | None = None,
        pause_seconds: float = 0.25,
        clock: Callable[[], datetime] | None = None,
        defer_platform: bool = False,
    ) -> None:
        self._build_platform = build_platform
        self._build_feed = build_feed
        self._build_runner = build_runner
        self._config = runner_config
        self._candidate = candidate
        self._strategy = strategy
        self._mode = mode
        self._journal_factory = journal_factory
        self._pause = max(float(pause_seconds), 0.0)
        self._clock = clock or _utc_now
        #: A provider platform needs a *connected* terminal to build its
        #: catalogue, and the operator connects from the browser after this
        #: process starts.  Deferring the build to the first Start means the
        #: runner is created from real, current terminal symbols instead of from
        #: whatever was (not) there at launch.
        self._defer = bool(defer_platform)

        self._queue: queue.Queue[tuple[Callable[[], Any], _Reply]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        #: A reason code (never exception text) for a failed build, plus the
        #: blocker the page should show.
        self.error = ""
        self.blockers: list[dict[str, str]] = []
        self.platform: Any = None
        self.runner: PaperStrategyRunner | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self, *, timeout: float = 30.0) -> dict[str, Any]:
        """Start the owner thread and let it build, restore, and idle."""

        if self._thread is not None and self._thread.is_alive():
            return self.view()
        self._shutdown.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="axiom-paper-runner-host", daemon=True
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

    # -- owner thread ----------------------------------------------------

    def _run(self) -> None:
        if not self._defer:
            try:
                self._build_now()
            except Exception as exc:  # reported, never raised into a request thread
                self._record_failure(exc)
                self._ready.set()
                self._drain_until_shutdown()
                return
        self._ready.set()
        while not self._shutdown.is_set():
            self._drain()
            runner = self.runner
            if runner is not None and runner.state == "RUNNING":
                try:
                    runner.step()
                except Exception as exc:  # runner.step already fails closed
                    runner.stop(reason="runner_error", detail=type(exc).__name__)
            else:
                self._shutdown.wait(self._pause)

    def _build_now(self) -> None:
        """Build the platform, feed, and runner.  Owner thread only."""

        self.platform = self._build_platform()
        feed = self._build_feed(self.platform)
        if feed is None:
            raise RunnerSetupRefused(
                "market_feed_unavailable",
                "the runner has no market data feed, so no cycle could be priced",
                "Connect to your terminal, or start with a replay source.",
            )
        self.runner = self._make_runner(self.platform, feed)
        self.runner.restore()
        self.error = ""
        self.blockers = []

    def _record_failure(self, exc: BaseException) -> None:
        """Turn a build failure into a blocker.  Never keeps exception text."""

        if isinstance(exc, RunnerSetupRefused):
            self.error = exc.reason
            self.blockers = [exc.audit_view()]
            return
        # Anything else is unexpected: the page gets a stable code and a plain
        # sentence, and the exception type goes to the console for whoever is
        # debugging.  Exception text can name internal objects, so it never
        # reaches a page.
        print(
            f"[axiom-workstation] paper runner build failed: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        self.error = "runner_build_failed"
        self.blockers = [
            {
                "reason": "runner_build_failed",
                "message": (
                    "The runner could not be built from the terminal session. "
                    "Nothing was started and nothing traded."
                ),
                "hint": (
                    "Check the console for the traceback, then press Start again. "
                    "The execution mode was not changed."
                ),
            }
        ]

    def _make_runner(self, platform: Any, feed: Any) -> PaperStrategyRunner:
        if self._build_runner is not None:
            return self._build_runner(platform, feed)
        if self._config is None or self._candidate is None or self._strategy is None:
            raise ValueError("runner_construction_arguments_missing")
        lane = lane_from_platform(platform)
        journal = self._journal_factory(lane) if self._journal_factory else None
        return PaperStrategyRunner(
            lane=lane,
            feed=feed,
            strategy=self._strategy,
            candidate=self._candidate,
            config=self._config,
            mode=self._mode,
            journal=journal,
            clock=self._clock,
        )

    def _drain(self) -> None:
        while True:
            try:
                task, reply = self._queue.get_nowait()
            except queue.Empty:
                return
            self._execute(task, reply)

    def _drain_until_shutdown(self) -> None:
        while not self._shutdown.is_set():
            try:
                task, reply = self._queue.get(timeout=self._pause or 0.05)
            except queue.Empty:
                continue
            self._execute(task, reply)

    def _execute(self, task: Callable[[], Any], reply: _Reply) -> None:
        try:
            reply.value = task()
        except BaseException as exc:  # noqa: BLE001 - relayed, not swallowed
            reply.error = exc
        finally:
            reply.done.set()

    def submit(self, task: Callable[[Any], Any], *, timeout: float | None = None):
        """Run ``task(platform)`` on the owner thread and return its result."""

        if not self.alive:
            raise RuntimeError(self.error or "runner_host_not_running")
        reply = _Reply()
        self._queue.put((lambda: task(self.platform), reply))
        if not reply.done.wait(timeout or READ_TIMEOUT_SECONDS):
            raise TimeoutError("runner_host_busy")
        if reply.error is not None:
            raise reply.error
        return reply.value

    # -- actions ---------------------------------------------------------

    def start_runner(self) -> dict[str, Any]:
        if not self.alive:
            return self._unavailable("runner_host_not_started")

        def task(platform: Any) -> dict[str, Any]:
            if self.runner is None:
                if not self._defer:
                    return self._unavailable("runner_not_built")
                try:
                    # The catalogue, the setup terms, and the feed are all built
                    # now, on this thread, from the terminal as it is right now.
                    self._build_now()
                except Exception as exc:
                    self._record_failure(exc)
                    return self._blocked()
            return self.runner.start()

        return self.submit(task)

    def stop_runner(self, *, reason: str = "operator_stopped") -> dict[str, Any]:
        if self.runner is None:
            return self._unavailable()
        return self.submit(lambda platform: self.runner.stop(reason=reason))

    def view(self) -> dict[str, Any]:
        """The runner's state, or an explicit unavailable/starting view."""

        if self.error:
            return self._blocked()
        if not self.alive:
            return self._unavailable("runner_host_not_started")
        if self.runner is None:
            # Deferred until the operator presses Start (a provider platform needs
            # a connected terminal).  This is *not* a failure: the host is alive and
            # will build on Start, so the page keeps offering the button and says
            # plainly what it is waiting for.
            if self._defer:
                return self._pending("waiting_for_terminal")
            return self._unavailable("runner_not_built")
        try:
            view = self.submit(lambda platform: self.runner.view())
            if isinstance(view, dict):
                view.setdefault("available", True)
            return view
        except TimeoutError:
            return self._unavailable("busy")
        except Exception as exc:
            return self._unavailable(type(exc).__name__)

    def _pending(self, reason: str) -> dict[str, Any]:
        return {
            "available": True,
            "pending": True,
            "state": "WAITING_FOR_TERMINAL",
            "running": False,
            "reason": reason,
            "reason_detail": (
                "Connect to your terminal in the browser, then press Start.  The "
                "platform is built from the terminal's own symbols at that moment; "
                "nothing is substituted in the meantime."
            ),
            "error": "",
            "blockers": [],
            "paper_only": True,
            "order_sending": False,
            "live_configured": False,
            "strategy": {},
            "counters": {},
            "recent": [],
            "positions": [],
            "fills": [],
            "max_cycles": 0,
            "data": {},
        }

    def _blocked(self) -> dict[str, Any]:
        """The view for a runner that could not be built, with a clear blocker."""

        blockers = [dict(item) for item in self.blockers]
        return {
            "available": True,
            "pending": False,
            "blocked": True,
            "state": "BLOCKED",
            "running": False,
            "reason": self.error or "runner_blocked",
            "reason_detail": "",
            "error": "",
            "blockers": blockers,
            "paper_only": True,
            "order_sending": False,
            "live_configured": False,
            "strategy": {},
            "counters": {},
            "recent": [],
            "positions": [],
            "fills": [],
            "max_cycles": 0,
            "data": {},
        }

    def _unavailable(self, reason: str = "") -> dict[str, Any]:
        return {
            "available": False,
            "error": reason or self.error,
            "state": "UNAVAILABLE",
            "running": False,
            "paper_only": True,
            "order_sending": False,
            "reason": reason or self.error,
            "blockers": [],
        }


__all__ = [
    "PaperRunnerHost",
    "RunnerSetupRefused",
    "READ_TIMEOUT_SECONDS",
    "lane_from_platform",
]
