"""Assemble the live execution workflow from an operator's own configuration.

This module is deliberately *outside* :mod:`axiom_forex.workstation`: the browser
layer must not be able to name an order-submitting API, so the object that owns
the broker adapter is built here and handed to the UI as an opaque controller.
The UI can ask it to arm, start, and stop -- and that is the whole surface.

Assembly sends nothing, connects nothing, and arms nothing.  It builds:

* the MT5 trading adapter, pointing at the same terminal session the operator
  opened in the browser (one process, one terminal connection);
* a thread-safe view of the durable database, because page requests and the
  strategy loop run on different threads;
* the live session host, which builds its stack on its own thread when -- and
  only when -- the operator starts a session.
"""

from __future__ import annotations

from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Sequence

from ..scalper.live_approval import plan_fingerprint
from ..scalper.live_bridge import (
    ScalperLiveIntentBridge,
    blocked_evidence_gate,
    paper_host_evidence_gate,
    scalper_host_policy,
)
from .live_host import LiveRunnerHost, OwnerThreadLeg, OwnerThreadPlatform
from .live_plan import PLAN_FILENAME, SCALPER_BINDING_FIELDS
from .live_workflow import ArmedStrategy, LiveTrading


class UnavailableOneShotPlatform:
    """A planning stack has not been resolved; every exact identity check fails closed."""


class UnavailableOneShotLeg:
    """No owner-thread one-shot stack exists; no order can be executed."""

    one_shot_ready = False

    def status(self) -> dict[str, Any]:
        return {"configured": False, "ready": False, "active": False,
                "blockers": [{"reason": "one_shot_stack_unavailable",
                              "message": "The exact one-shot stack is not prepared.", "hint": ""}]}

    def execute(self, _plan: Any) -> dict[str, Any]:
        raise RuntimeError("one_shot_stack_unavailable")

    def reconcile(self) -> dict[str, Any]:
        return {"available": False, "reconciled": False,
                "reason": "one_shot_stack_unavailable", "recovered": []}

    def close(self) -> None:
        return None


class OneShotStackFactory:
    """Resolve the exact planning platform and the one owner-thread live leg.

    Read-only exact checks use a planning platform built on the calling thread.
    An armed submission uses a dedicated LiveRunnerHost whose worker thread
    builds the provider platform and the *existing* LiveLeg; the bridge receives
    only owner-thread proxies. Starting a host here starts no strategy runner.
    """

    def __init__(self, *, live: LiveTrading,
                 platform_factory: Callable[[str], Any],
                 session_factory: Callable[[Mapping[str, Any]], Any]):
        self.live = live
        self.platform_factory = platform_factory
        self.session_factory = session_factory
        self._lock = threading.RLock()
        self._unavailable_leg = UnavailableOneShotLeg()
        self._planning_pair = ""
        self._planning_platform: Any = None
        self._owner_key = ""
        self._owner_host: LiveRunnerHost | None = None
        self._owner_stack: tuple[OwnerThreadPlatform, OwnerThreadLeg] | None = None

    @staticmethod
    def _close_platform(platform: Any) -> None:
        repository = getattr(platform, "repository", None)
        close = getattr(repository, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _close_owner_locked(self) -> None:
        host = self._owner_host
        if host is not None:
            if host.alive:
                try:
                    host.submit(lambda: self._close_platform(host.platform))
                except Exception:
                    pass
            try:
                host.close()
            except Exception:
                pass
        self._owner_host = None
        self._owner_stack = None
        self._owner_key = ""

    def _close_planning_locked(self) -> None:
        if self._planning_platform is not None:
            self._close_platform(self._planning_platform)
        self._planning_platform = None
        self._planning_pair = ""

    def release(self) -> None:
        with self._lock:
            self._close_owner_locked()
            self._close_planning_locked()

    def _owner_ready(self, state: Mapping[str, Any], intent: Any) -> bool:
        try:
            if not bool(getattr(self.live, "armed", False)):
                return False
            session = getattr(self.live, "session", None)
            if session is not None:
                session_view = dict(session.view() or {})
                if bool(session_view.get("running") or session_view.get("next_signal_sends_real_order")):
                    return False
            plan = dict(getattr(self.live, "plan", {}) or {})
            evidence = dict(state.get("evidence") or {})
            digest = str(evidence.get("evidence_digest") or "")
            fingerprint = plan_fingerprint(intent=intent.as_payload(), plan=plan,
                                           evidence_digest=digest)
            if fingerprint != str(state.get("plan_fingerprint") or ""):
                return False
            bindings = dict(plan.get("scalper_live") or {})
            if set(bindings) != set(SCALPER_BINDING_FIELDS):
                return False
            values = dict(state.get("values") or {})
            for field in SCALPER_BINDING_FIELDS:
                if str(bindings.get(field, "") or "") != str(values.get(f"scalper_{field}", "") or ""):
                    return False
            return True
        except Exception:
            return False

    def _owner_stack_locked(self, key: str) -> tuple[OwnerThreadPlatform, OwnerThreadLeg]:
        if self._owner_stack is not None and self._owner_key == key:
            return self._owner_stack
        self._close_owner_locked()
        plan = dict(getattr(self.live, "plan", {}) or {})
        host = self.session_factory(dict(plan))
        try:
            stack = host.one_shot_stack()
        except Exception:
            try:
                host.close()
            except Exception:
                pass
            raise
        self._owner_host = host
        self._owner_stack = stack
        self._owner_key = key
        return stack

    def _planning_stack_locked(self, pair: str) -> tuple[Any, Any]:
        if self._planning_pair == pair and self._planning_platform is not None:
            return self._planning_platform, self._unavailable_leg
        try:
            platform = self.platform_factory(pair)
        except Exception:
            return UnavailableOneShotPlatform(), self._unavailable_leg
        self._close_planning_locked()
        self._planning_pair = pair
        self._planning_platform = platform
        return platform, self._unavailable_leg

    def __call__(self, state: Mapping[str, Any], intent: Any, *, prepare: bool = False):
        key = str(state.get("plan_fingerprint") or "")
        with self._lock:
            if self._owner_key and key != self._owner_key:
                self._close_owner_locked()
            if prepare and self._owner_ready(state, intent):
                return self._owner_stack_locked(key)
            return self._planning_stack_locked(str(intent.symbol))


def build_live_workflow(
    *,
    settings: Any,
    mode: Any,
    project_root: str | Path,
    db_path: str,
    platform_factory: Callable[[str], Any],
    feed_factory: Callable[[Any], Any],
    terminal_getter: Callable[[], Any],
    provider_setup_path: str | Path | None,
    settings_path: str | Path | None = None,
    interval_seconds: int = 60,
    quote_age_seconds: str = "5",
    candidates: Sequence[Any] = (),
    symbol_resolver: Callable[[str], Any] | None = None,
    audit_log: Any = None,
    vault: Any = None,
    pause_seconds: float = 1.0,
    scalper_host: Any = None,
    scalper_approval_path: str | Path | None = None,
) -> LiveTrading | None:
    """Return the workflow, or ``None`` when this deployment cannot trade live."""

    if settings is None:
        return None
    from decimal import Decimal

    from ..brokers.mt5 import MT5PasswordStore, make_mt5_adapter
    from ..config.vault import default_vault
    from ..database.audit import DurableAuditLog
    from ..database.threaded import ThreadLocalRepository
    from .host import lane_from_platform
    from .journal import RunnerJournal
    from .live_host import LiveRunnerHost
    from .provider import build_provider_feed
    from .runner import PaperStrategyRunner, RunnerConfig

    repository = ThreadLocalRepository(db_path)
    if audit_log is None:
        try:
            audit_log = DurableAuditLog(repository=repository)
        except Exception:
            audit_log = None

    #: Live authority is the operator's armed plan, so the adapter's "approved
    #: and active" question is answered by that plan and nothing else.
    armed_strategy = ArmedStrategy()
    live: LiveTrading | None = None

    adapter = make_mt5_adapter(
        settings=settings,
        password_store=MT5PasswordStore(
            settings=settings,
            vault=vault if vault is not None else default_vault(),
            audit_log=audit_log,
        ),
        repository=repository,
        audit_log=audit_log,
        terminal_factory=terminal_getter,
        lifecycle_lookup=armed_strategy.for_strategy,
    )

    def build_stack(pair: str) -> tuple[Any, Any, Any]:
        platform = platform_factory(pair)
        feed = feed_factory(platform)
        return platform, feed, platform.spec

    def session_factory(plan: Mapping[str, Any]) -> LiveRunnerHost:
        available = tuple(getattr(live, "candidates", ()) or ()) if live is not None else tuple(candidates)
        candidate = next(
            (
                item
                for item in available
                if str(getattr(item, "candidate_id", "")) == str(plan.get("candidate_id", ""))
            ),
            None,
        )
        if candidate is None:
            raise RuntimeError("unknown_candidate_id")

        def build_runner(platform: Any, feed: Any, leg: Any) -> PaperStrategyRunner:
            return PaperStrategyRunner(
                lane=lane_from_platform(platform),
                feed=feed,
                strategy=candidate.build(),
                candidate=candidate,
                config=RunnerConfig(
                    run_id=f"live-{candidate.candidate_id}",
                    symbol=platform.pair,
                    quantity=Decimal(str(plan.get("quantity", "0"))),
                    pause_seconds=0,
                ),
                mode=mode,
                journal=RunnerJournal(
                    repository=platform.repository, audit_log=platform.audit_log
                ),
                live=leg,
                # Live only: the operator armed this strategy on this page.  The
                # plan and every screen label it UNVALIDATED when it has no
                # measured result.  Paper keeps the stricter selection rule.
                allow_unvalidated=True,
            )

        return LiveRunnerHost(
            adapter=adapter,
            mode=mode,
            plan=dict(plan),
            build_stack=lambda: build_stack(str(plan.get("pair", ""))),
            build_runner=build_runner,
            lane_from_platform=lane_from_platform,
            lifecycle=armed_strategy,
            pause_seconds=pause_seconds,
        )

    live = LiveTrading(
        adapter=adapter,
        mode=mode,
        plan_path=Path(project_root) / PLAN_FILENAME,
        setup_path=provider_setup_path or None,
        stack_builder=symbol_resolver,
        candidates=tuple(candidates),
        session_factory=session_factory,
        armed_strategy=armed_strategy,
        audit_log=audit_log,
        # The bar interval this stack's feed reads.  The plan's timeframe is
        # compared with it, so a plan that would decide on other bars than the
        # ones its evidence came from is refused rather than silently run.
        live_interval_seconds=int(interval_seconds or 0),
        settings_path=settings_path,
    )

    approval_path = (
        Path(scalper_approval_path)
        if scalper_approval_path is not None
        else Path(project_root) / "scalper_live_approval.local.json"
    )
    stacks = OneShotStackFactory(
        live=live,
        platform_factory=platform_factory,
        session_factory=session_factory,
    )
    bridge = ScalperLiveIntentBridge(
        repository=repository,
        live=live,
        leg=stacks._unavailable_leg,
        platform=UnavailableOneShotPlatform(),
        policy=None,
        approval_path=approval_path,
        evidence_gate=(
            paper_host_evidence_gate(scalper_host)
            if scalper_host is not None
            else blocked_evidence_gate("independent_replay_admission_unavailable")
        ),
        policy_loader=scalper_host_policy(scalper_host) if scalper_host is not None else None,
        stack_loader=stacks,
        audit_log=audit_log,
    )
    live.scalper_bridge = bridge
    return live


__all__ = [
    "OneShotStackFactory",
    "UnavailableOneShotLeg",
    "UnavailableOneShotPlatform",
    "build_live_workflow",
]
