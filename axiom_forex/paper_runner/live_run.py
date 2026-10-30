"""A live run: the same runner, the same strategy, the same gates -- real orders.

This module is the *operator entry point* for live execution.  It assembles the
same objects the paper lane uses (catalogue-resolved spec, the operator's setup,
the planner, the pre-trade gate) and adds the live leg, so the strategy and the
risk controls are literally the same code in both modes.

What it does **not** do
-----------------------
* It does not connect, sign in, or arm anything.  The caller must hand it an
  adapter that is already connected and already armed by an explicit operator
  confirmation, because arming is a decision, not a side effect of starting a
  runner.
* It does not switch the execution mode.  The runner is given a mode object that
  already says LIVE and confirmed; if it does not, the leg refuses.
* It does not retry, re-send, or "clean up" an unresolved order.
* It places no order of its own: every order comes from a strategy signal that
  passed the same gate the paper lane uses.

The report it returns is the operator's record of the run: mode, symbol, how many
cycles ran, what was signalled, what was sent, what was booked, and what the
terminal says about positions afterwards.  Identifiers are the broker's own; no
credential, balance, or equity appears anywhere in it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from axiom_forex.config.provider_setup import ProviderSetup

from .journal import RunnerJournal
from .live import LiveLeg
from .provider import build_provider_feed, build_provider_platform
from .runner import PaperStrategyRunner, RunnerConfig
from .strategy import candidate_family


class LiveRunRefused(Exception):
    """The live run could not be assembled; nothing was sent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class LiveRunConfig:
    pair: str
    candidate_id: str
    quantity: Decimal
    max_cycles: int = 1
    interval_seconds: int = 60
    run_id: str = "live-run"

    def validate(self) -> tuple[str, ...]:
        problems: list[str] = []
        if not self.pair.strip():
            problems.append("missing_pair")
        if not self.candidate_id.strip():
            problems.append("missing_candidate")
        if self.quantity <= 0:
            problems.append("invalid_quantity")
        if self.max_cycles < 1:
            problems.append("invalid_max_cycles")
        return tuple(problems)


@dataclass(frozen=True)
class _LiveMode:
    """A read-only statement of the mode the runner must execute in."""

    mode: str = "LIVE"
    live_effective: bool = True


def run_live_session(
    *,
    adapter: Any,
    terminal: Any,
    setup: ProviderSetup,
    config: LiveRunConfig,
    repository: Any,
    resolved: Any = None,
    catalogue: Any = None,
    audit_log: Any = None,
    lifecycle: Any = None,
    max_quote_age_seconds: Decimal = Decimal("5"),
) -> dict[str, Any]:
    """Run the approved strategy against the live leg, bounded and reconciled.

    The adapter must already be connected **and armed**.  Everything that could
    make an order unsafe is checked again, per order, by the leg and by the
    adapter itself.
    """

    problems = config.validate()
    if problems:
        raise LiveRunRefused("live_run_config_invalid", ",".join(problems))
    if adapter is None:
        raise LiveRunRefused("live_adapter_missing", "no MT5 adapter")
    if not getattr(adapter, "connected", False):
        raise LiveRunRefused("mt5_not_connected", "connect before running live")
    if getattr(adapter, "armed", False) is not True:
        # A runner never arms a broker.  Say so plainly.
        raise LiveRunRefused(
            "live_not_armed",
            "arm live trading with an explicit operator confirmation first; a runner "
            "must never arm itself",
        )
    if terminal is None:
        raise LiveRunRefused("terminal_not_connected", "no terminal")

    candidate = next(
        (item for item in candidate_family() if item.candidate_id == config.candidate_id),
        None,
    )
    if candidate is None:
        raise LiveRunRefused("unknown_candidate_id", config.candidate_id)

    platform, refusal = build_provider_platform(
        setup=setup,
        resolved=resolved,
        repository=repository,
        audit_log=audit_log,
        market=terminal,
        terminal=terminal,
        symbols=tuple(terminal.symbol_names()),
        symbol_of_pair=((catalogue_symbols(catalogue) if catalogue is not None else {})),
    )
    if platform is None:
        raise LiveRunRefused(
            getattr(refusal, "reason", "provider_setup_refused"),
            getattr(refusal, "detail", ""),
        )

    from axiom_forex.execution.mode_router import ExecutionRouter

    router = ExecutionRouter(
        mode=_LiveMode(), paper=None, live=adapter, audit=platform.audit_log
    )
    leg = LiveLeg(
        router=router,
        adapter=adapter,
        gate=platform.gate,
        mode=_LiveMode(),
        spec=platform.spec,
        strategy_id=candidate.candidate_id,
        repository=platform.repository,
        audit_log=platform.audit_log,
        lifecycle=lifecycle,
        portfolio=platform.portfolio,
        rollover_cutoffs=tuple(
            getattr(getattr(platform.setup, "rollover", None), "cutoffs", ()) or ()
        ),
        clock=lambda: datetime.now(timezone.utc),
    )
    feed = build_provider_feed(
        platform=platform,
        terminal=terminal,
        interval_seconds=max(int(config.interval_seconds), 1),
        max_quote_age_seconds=max_quote_age_seconds,
    )
    runner = PaperStrategyRunner(
        lane=_lane_from(platform),
        feed=feed,
        strategy=candidate.build(),
        candidate=candidate,
        config=RunnerConfig(
            run_id=config.run_id,
            symbol=platform.pair,
            quantity=config.quantity,
            pause_seconds=0,
            max_cycles=max(int(config.max_cycles), 1),
        ),
        mode=_LiveMode(),
        journal=RunnerJournal(
            repository=platform.repository, audit_log=platform.audit_log
        ),
        live=leg,
    )
    runner.restore()
    started = runner.start()
    if started.get("state") != "RUNNING":
        return {
            "ran": False,
            "state": started.get("state"),
            "reason": started.get("reason"),
            "detail": started.get("reason_detail"),
            "blockers": started.get("blockers", []),
            "live": leg.status(),
            "orders_sent": 0,
        }
    view = runner.run(max_steps=max(int(config.max_cycles), 1))
    reconciliation = leg.reconcile(
        [
            position.audit_view()
            for position in platform.portfolio.positions.values()
        ]
    )
    return {
        "ran": True,
        "mode": "LIVE",
        "pair": platform.pair,
        "broker_symbol": platform.broker_symbol,
        "candidate_id": candidate.candidate_id,
        "state": view.get("state"),
        "reason": view.get("reason"),
        "counters": dict(view.get("counters", {})),
        "last_signal": view.get("last_signal"),
        "last_live_outcome": view.get("last_live_outcome", {}),
        "recent": view.get("recent", [])[:12],
        "orders_sent": view.get("live_orders", 0),
        "positions": view.get("positions", []),
        "reconciliation": reconciliation,
        "live": leg.status(),
    }


def catalogue_symbols(catalogue: Any) -> dict[tuple[str, str], str]:
    """``(base, quote) -> broker symbol`` for every pair the catalogue resolved."""

    mapping: dict[tuple[str, str], str] = {}
    for item in getattr(catalogue, "resolved", ()) or ():
        spec = getattr(item, "spec", None)
        if spec is None:
            continue
        mapping[(spec.base_currency, spec.quote_currency)] = str(item.broker_symbol)
    return mapping


def _lane_from(platform: Any) -> Any:
    from .host import lane_from_platform

    return lane_from_platform(platform)


__all__ = [
    "LiveRunConfig",
    "LiveRunRefused",
    "catalogue_symbols",
    "run_live_session",
]
