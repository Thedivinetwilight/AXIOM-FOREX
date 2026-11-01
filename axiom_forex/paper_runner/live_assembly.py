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
from typing import Any, Callable, Mapping, Sequence

from .live_plan import PLAN_FILENAME
from .live_workflow import ArmedStrategy, LiveTrading


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
        candidate = next(
            (
                item
                for item in candidates
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

    return LiveTrading(
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


__all__ = ["build_live_workflow"]
