"""Autonomous AX-SCALPER, on one declared real-money account, once armed.

This is the loop the LIVE lane was missing: fresh broker quotes in, decisions by
one *admitted* strategy, the existing risk guardian, one submission through the
existing guarded leg, protective exits, and reconciliation — with a stop that
means "no new entries" and says exactly what is still open at the broker.

What it is not
--------------
It is not a second execution path.  Every order goes through
:class:`~axiom_forex.scalper.live_venue.BrokerVenue`, which calls
``LiveLeg.execute`` — the same blockers, gate, router and ``MT5TradingAdapter``
a strategy order already uses.  It is not a strategy shortcut: the only strategy
it may run is the one named by an admission verdict it was handed at
construction, and it refuses to start without one.  It is not a demo/PAPER mode:
the venue is a live venue, the account must verify as REAL, and PAPER keeps the
desk it already has.

The steps of one decision, in order
-----------------------------------
1. **the account** — :func:`~axiom_forex.scalper.live_account.verify_real_account`
   on the adapter's own status, before every decision, not once at start-up;
2. **the quote** — the tick core validates symbol, sequence, age, spread and
   gap; a stale or crossed quote cannot produce a decision;
3. **the strategy** — the admitted strategy (and only it) evaluates on the
   validated window;
4. **the risk** — the guardian decides, with the account's own equity and the
   run's declared limits;
5. **the size** — risk per trade / stop distance, clamped to the broker's own
   volume grid; a size that rounds to something the broker would refuse is a
   refusal, never a rounding;
6. **the order** — ``Trader`` submits once under an idempotency key derived from
   the signal, then verifies the protection the broker actually holds;
7. **the management** — ``PositionBook`` manages stop/target/time exits through
   the same venue;
8. **the record** — every step appends an event the page and the audit log read.

Recovery
--------
A run that cannot prove its previous state does not guess: :meth:`recover`
returns ``RECOVERY_REQUIRED`` with the missing pieces, and while it does, no new
entry may be submitted.  The operator sees the concrete path (reconcile the
broker positions, confirm none belong to this run, then start again).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import LIVE, InstrumentSpec
from .execution import ACCEPTED, REJECTED, UNKNOWN_OUTCOME, BLOCKED
from .guardian import Guardian, OpenPositionRisk
from .live_account import AccountVerdict, ExpectedAccount, verify_real_account
from .positions import ManagementSettings, PositionBook
from .signals import Strategy
from .ticks import Tick, TickCore

# -- run states ------------------------------------------------------------

IDLE = "IDLE"
BLOCKED = "BLOCKED"
ARMED = "ARMED"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
STOPPED = "STOPPED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

# -- refusal reasons -------------------------------------------------------

NO_ADMISSION = "no_admitted_candidate"
ACCOUNT_NOT_REAL = "real_account_required"
ACCOUNT_UNVERIFIED = "account_unverified"
NOT_ARMED = "live_not_armed"
MODE_NOT_LIVE = "mode_not_live"
ALREADY_RUNNING = "already_running"
NOT_RUNNING = "not_running"
QUOTE_STALE = "quote_stale"
QUOTE_REFUSED = "quote_refused"
SIZE_REFUSED = "size_refused"
ENTRY_BLOCKED = "entry_blocked"
UNRESOLVED_ORDER = "unresolved_order_present"
POSITION_LIMIT = "position_limit_reached"
COOLDOWN = "cooldown"
RECOVERY = "recovery_required"
SPEC_UNKNOWN = "instrument_unknown"
NOT_MANAGEABLE = "protection_not_restorable"

#: A run needs all of these before an entry may be considered.
_ENTRY_READY = frozenset({RUNNING})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _d(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class AdmittedStrategy:
    """The one strategy this run may trade, and the evidence that admitted it."""

    strategy: Strategy
    candidate_id: str
    evidence_digest: str
    policy_version: str
    approved_at: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "strategy": self.strategy.name,
            "family": self.strategy.family,
            "fingerprint": self.strategy.fingerprint,
            "evidence_digest": self.evidence_digest,
            "policy_version": self.policy_version,
            "approved_at": self.approved_at,
        }


@dataclass
class RunEvent:
    at: str
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def audit_view(self) -> dict[str, Any]:
        return {"at": self.at, "kind": self.kind, **self.detail}


class AutonomousScalper:
    """The LIVE loop: verified REAL account, admitted strategy, guarded orders."""

    def __init__(
        self,
        *,
        adapter: Any,
        mode: Any,
        expected_account: ExpectedAccount | None,
        admitted: AdmittedStrategy | None,
        venue: Any,
        trader: Any,
        guardian: Guardian,
        book: PositionBook,
        core: TickCore,
        specs: Mapping[str, InstrumentSpec],
        symbols: Mapping[str, str],
        max_open_positions: int,
        cooldown_ms: int = 0,
        decision_interval_ms: int = 0,
        starting_equity: Decimal = Decimal("0"),
        risk_fraction: Decimal = Decimal("0.002"),
        clock: Callable[[], datetime] | None = None,
        audit_log: Any = None,
        session_running: Callable[[], bool] | None = None,
        approval_active: Callable[[], bool] | None = None,
    ) -> None:
        self.adapter = adapter
        self.mode = mode
        self.expected_account = expected_account
        self.admitted = admitted
        self.venue = venue
        self.trader = trader
        self.guardian = guardian
        self.book = book
        self.core = core
        self.specs = dict(specs)
        #: scalper symbol -> the broker symbol the terminal speaks
        self.symbols = dict(symbols)
        self.max_open_positions = int(max_open_positions)
        self.cooldown_ms = int(cooldown_ms)
        self.decision_interval_ms = int(decision_interval_ms)
        self.starting_equity = Decimal(starting_equity)
        self.risk_fraction = Decimal(risk_fraction)
        self.clock = clock or _utc_now
        self.audit_log = audit_log
        self.session_running = session_running
        self.approval_active = approval_active

        self.state = IDLE
        self.stop_reason = ""
        self.pause_reason = ""
        self.events: list[RunEvent] = []
        self.decisions: list[dict[str, Any]] = []
        self._last_entry_ms: int | None = None
        self._last_decision_ms: dict[str, int] = {}
        self._recovery: dict[str, Any] = {}
        self._equity = Decimal(starting_equity)
        self._entries_blocked: dict[str, int] = {}

    # -- read model ------------------------------------------------------

    def _account(self) -> AccountVerdict:
        status: Mapping[str, Any] = {}
        try:
            status = dict(self.adapter.status() or {})
        except Exception:
            status = {}
        return verify_real_account(status, self.expected_account)

    def _mode_name(self) -> str:
        return str(getattr(self.mode, "mode", "") or "").upper()

    def _live_effective(self) -> bool:
        return bool(getattr(self.mode, "live_effective", False))

    def readiness(self) -> list[dict[str, str]]:
        """Everything that would stop an entry right now, with its fix."""

        blockers: list[dict[str, str]] = []
        if self.admitted is None:
            blockers.append(
                {
                    "reason": NO_ADMISSION,
                    "message": (
                        "No AX-SCALPER strategy has passed signed-evidence and "
                        "independent raw-replay admission, so nothing may trade."
                    ),
                    "hint": (
                        "Run the admission evaluation on a signed evidence artifact and "
                        "load its policy; until one passes, order submission stays blocked."
                    ),
                }
            )
        verdict = self._account()
        if not verdict.ok:
            blockers.append(
                {
                    "reason": ACCOUNT_NOT_REAL if verdict.mode != "REAL" else ACCOUNT_UNVERIFIED,
                    "message": "The account is not the verified real-money account: "
                    + (verdict.detail or verdict.mode),
                    "hint": "Sign the terminal in to the declared real account and connect "
                    "again. AXIOM never switches accounts by itself.",
                }
            )
        if self._mode_name() != LIVE or not self._live_effective():
            blockers.append(
                {
                    "reason": MODE_NOT_LIVE,
                    "message": f"Execution mode is {self._mode_name() or 'UNKNOWN'} and "
                    "must be confirmed LIVE for this lane.",
                    "hint": "Switch to LIVE on the Trading mode screen and complete its "
                    "preflight.",
                }
            )
        if not bool(getattr(self.adapter, "armed", False)):
            blockers.append(
                {
                    "reason": NOT_ARMED,
                    "message": "Live trading is not armed.",
                    "hint": "Arm the reviewed plan on the Trading mode screen; this page "
                    "cannot arm.",
                }
            )
        if self.venue is not None and getattr(self.venue, "has_unresolved", False):
            blockers.append(
                {
                    "reason": UNRESOLVED_ORDER,
                    "message": "An earlier submission is unresolved at the broker.",
                    "hint": "Reconcile the same request id; nothing is re-sent until it is "
                    "explained.",
                }
            )
        if self._recovery:
            blockers.append(
                {
                    "reason": RECOVERY,
                    "message": "This run's previous state could not be restored exactly: "
                    + str(self._recovery.get("detail") or ""),
                    "hint": str(self._recovery.get("hint") or ""),
                }
            )
        return blockers

    def status(self) -> dict[str, Any]:
        verdict = self._account()
        positions = [item.audit_view() for item in self.book.open_positions]
        broker_positions = self._broker_positions()
        return {
            "state": self.state,
            "mode": self._mode_name(),
            "live_effective": self._live_effective(),
            "armed": bool(getattr(self.adapter, "armed", False)),
            "account": {
                **verdict.audit_view(),
                "expected": None if self.expected_account is None
                else self.expected_account.audit_view(),
            },
            "admitted": None if self.admitted is None else self.admitted.audit_view(),
            "symbols": dict(self.symbols),
            "equity": str(self._equity),
            "starting_equity": str(self.starting_equity),
            "open_positions": positions,
            "broker_positions": broker_positions,
            "unresolved": sorted(getattr(self.venue, "_unresolved", ()) or ()),
            "decisions": [dict(item) for item in self.decisions[-12:]],
            "events": [item.audit_view() for item in self.events[-25:]],
            "blockers": self.readiness(),
            "entries_blocked": dict(self._entries_blocked),
            "stop_reason": self.stop_reason,
            "pause_reason": self.pause_reason,
            "recovery": dict(self._recovery),
            "venue": self.venue.stats if hasattr(self.venue, "stats") else {},
            "order_sending": True,
            "paper_only": False,
        }

    def _broker_positions(self) -> list[dict[str, Any]]:
        reader = getattr(self.venue, "position_ids", None)
        if not callable(reader):
            return []
        try:
            return [{"ticket": ticket} for ticket in reader()]
        except Exception:
            return []

    # -- lifecycle -------------------------------------------------------

    def _emit(self, kind: str, **detail: Any) -> None:
        event = RunEvent(at=self.clock().astimezone(timezone.utc).isoformat(), kind=kind, detail=detail)
        self.events.append(event)
        if self.audit_log is not None:
            try:
                self.audit_log.record({"event_type": f"AX_SCALPER_LIVE_{kind.upper()}", **event.audit_view()})
            except Exception:
                pass

    def arm(self) -> dict[str, Any]:
        """Move to ARMED when every precondition holds.  Sends nothing."""

        blockers = self.readiness()
        if blockers:
            self.state = BLOCKED
            self._emit("arm_refused", reasons=[item["reason"] for item in blockers])
            return {"ok": False, "state": self.state, "blockers": blockers}
        self.state = ARMED
        self._emit("armed", strategy=(self.admitted.candidate_id if self.admitted else ""))
        return {"ok": True, "state": self.state, "blockers": []}

    def start(self) -> dict[str, Any]:
        """Begin autonomous entries.  Refuses unless armed and ready."""

        if self.state in {RUNNING}:
            return {"ok": False, "state": self.state, "reason": ALREADY_RUNNING}
        if self.state in {BLOCKED, IDLE, STOPPED}:
            armed = self.arm()
            if not armed.get("ok"):
                return armed
        blockers = self.readiness()
        if blockers:
            self.state = BLOCKED
            return {"ok": False, "state": self.state, "blockers": blockers}
        self.state = RUNNING
        self.stop_reason = ""
        self.pause_reason = ""
        self._emit("started")
        return {"ok": True, "state": self.state}

    def pause(self, *, reason: str = "operator_pause") -> dict[str, Any]:
        if self.state != RUNNING:
            return {"ok": False, "state": self.state, "reason": NOT_RUNNING}
        self.state = PAUSED
        self.pause_reason = reason
        self._emit("paused", reason=reason)
        return {"ok": True, "state": self.state}

    def resume(self) -> dict[str, Any]:
        if self.state != PAUSED:
            return {"ok": False, "state": self.state, "reason": NOT_RUNNING}
        blockers = self.readiness()
        if blockers:
            self.state = BLOCKED
            return {"ok": False, "state": self.state, "blockers": blockers}
        self.state = RUNNING
        self.pause_reason = ""
        self._emit("resumed")
        return {"ok": True, "state": self.state}

    def stop(self, *, reason: str = "operator_stop", close_positions: bool = False) -> dict[str, Any]:
        """Stop new entries.  Existing broker positions are reported, not guessed."""

        previous = self.state
        self.state = STOPPED
        self.stop_reason = reason
        open_positions = [item.audit_view() for item in self.book.open_positions]
        broker = self._broker_positions()
        closed: list[dict[str, Any]] = []
        if close_positions:
            closed = self.flatten(reason=reason)
        self._emit(
            "stopped",
            reason=reason,
            previous=previous,
            open_positions=[item.get("position_id") for item in open_positions],
            broker_positions=broker,
            flattened=bool(close_positions),
        )
        return {
            "ok": True,
            "state": self.state,
            "reason": reason,
            "open_positions": open_positions,
            "broker_positions": broker,
            "closed": closed,
            "message": (
                "No new entry will be submitted. "
                + (
                    f"{len(open_positions)} managed position(s) and {len(broker)} broker "
                    "position(s) remain open and are now unattended: their protective "
                    "stops stay at the broker, and AXIOM will not close them unless you "
                    "ask it to."
                    if (open_positions or broker)
                    else "Nothing is open at the broker."
                )
            ),
        }

    def flatten(self, *, reason: str = "operator_flatten") -> list[dict[str, Any]]:
        """Explicitly close every managed position through the same venue."""

        closed: list[dict[str, Any]] = []
        for position in list(self.book.open_positions):
            fill = self.venue.close(
                ticket=position.position_id,
                price=position.entry_price,
                spread_points=Decimal(0),
                reason=reason,
            )
            closed.append(
                {"position_id": position.position_id, "status": fill.status, "reason": fill.reason}
            )
        self._emit("flattened", closed=closed, reason=reason)
        return closed

    # -- recovery --------------------------------------------------------

    def recover(self, *, state: Mapping[str, Any] | None) -> dict[str, Any]:
        """Restore exactly, or block entries with a specific path.  Never guesses."""

        self._recovery = {}
        if state is None:
            self._recovery = {
                "detail": "no stored run state was found",
                "hint": (
                    "Reconcile the broker's open positions, confirm none belong to a "
                    "previous AX-SCALPER run, then start a new run. Nothing is adopted."
                ),
            }
            self.state = RECOVERY_REQUIRED
            self._emit("recovery_required", reason="no_state")
            return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}

        stored_symbols = dict(state.get("symbols") or {})
        stored_strategy = str(state.get("strategy_fingerprint") or "")
        problems: list[str] = []
        if stored_symbols != self.symbols:
            problems.append("symbol map differs")
        if self.admitted is not None and stored_strategy and stored_strategy != self.admitted.strategy.fingerprint:
            problems.append("strategy fingerprint differs")
        if str(state.get("mode") or "") != LIVE:
            problems.append("stored mode is not LIVE")
        positions = [dict(item) for item in (state.get("open_positions") or ())]
        for row in positions:
            ticket = str(row.get("position_id") or "")
            if not ticket or self.venue.protection(ticket) == (None, None):
                problems.append(f"position {ticket or '?'} has no restorable protection")
        if problems:
            self._recovery = {
                "detail": "; ".join(problems),
                "hint": (
                    "Do not resume: reconcile the broker positions by ticket, restore the "
                    "protective stops in the terminal, then acknowledge and start a new "
                    "run. No entry is submitted while this stands."
                ),
            }
            self.state = RECOVERY_REQUIRED
            self._emit("recovery_required", problems=problems)
            return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}

        for row in positions:
            try:
                self.book.open_position(
                    position_id=str(row.get("position_id")),
                    symbol=str(row.get("symbol")),
                    side=str(row.get("side")),
                    lots=Decimal(str(row.get("lots"))),
                    entry_price=Decimal(str(row.get("entry_price"))),
                    stop_price=Decimal(str(row.get("stop_price"))),
                    target_price=None if row.get("target_price") is None else Decimal(str(row["target_price"])),
                    now_ms=int(row.get("opened_at_ms") or 0),
                    request_id=str(row.get("request_id") or ""),
                )
            except Exception as exc:
                self._recovery = {
                    "detail": f"position {row.get('position_id')} could not be restored: {type(exc).__name__}",
                    "hint": "Reconcile by ticket and start a new run; nothing is adopted.",
                }
                self.state = RECOVERY_REQUIRED
                return {"ok": False, "state": self.state, "recovery": dict(self._recovery)}
        self.state = ARMED
        self._emit("recovered", positions=len(positions))
        return {"ok": True, "state": self.state, "positions": len(positions)}

    def snapshot(self) -> dict[str, Any]:
        """The exact state a restart would need to resume this run."""

        return {
            "schema": "axiom-scalper-live-run-v1",
            "mode": LIVE,
            "symbols": dict(self.symbols),
            "strategy_fingerprint": "" if self.admitted is None else self.admitted.strategy.fingerprint,
            "candidate_id": "" if self.admitted is None else self.admitted.candidate_id,
            "equity": str(self._equity),
            "open_positions": [
                {
                    "position_id": item.position_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "lots": str(item.lots),
                    "entry_price": str(item.entry_price),
                    "stop_price": str(item.stop_price),
                    "target_price": None if item.target_price is None else str(item.target_price),
                    "opened_at_ms": item.opened_at_ms,
                    "request_id": item.request_id,
                }
                for item in self.book.open_positions
            ],
        }

    # -- one decision ----------------------------------------------------

    def on_tick(
        self,
        *,
        symbol: str,
        bid: Any,
        ask: Any,
        source_time: datetime,
        now_ms: int,
        equity: Any = None,
    ) -> dict[str, Any]:
        """Feed one quote.  Manages positions always; decides only when RUNNING."""

        spec = self.specs.get(symbol)
        if spec is None:
            return self._refuse(SPEC_UNKNOWN, symbol=symbol)
        accepted = self.core.ingest(
            symbol=symbol,
            bid=bid,
            ask=ask,
            source_time=source_time,
            source="MT5_TICK",
            volume=None,
        )
        if not getattr(accepted, "accepted", False):
            reason = getattr(accepted, "refusal", None) or QUOTE_REFUSED
            return self._refuse(QUOTE_REFUSED, symbol=symbol, detail=str(reason))
        tick: Tick = accepted.tick
        if equity is not None and _d(equity) is not None:
            self._equity = _d(equity)
        self.guardian.observe_account(
            at_ms=now_ms, equity=self._equity, balance=self._equity, day_key=None
        )

        managed = [
            self.book.on_tick(position_id=position.position_id, tick=tick, now_ms=now_ms)
            for position in list(self.book.open_positions)
            if position.symbol == symbol
        ]

        if self.state != RUNNING:
            return {
                "tick": tick.tick_id,
                "state": self.state,
                "managed": [[item.audit_view() if hasattr(item, "audit_view") else str(item) for item in group] for group in managed],
                "execution": None,
                "blocked": "not_running",
            }

        verdict = self._account()
        if not verdict.ok:
            self.state = BLOCKED
            self._emit("account_lost", detail=verdict.detail)
            return self._refuse(ACCOUNT_NOT_REAL, symbol=symbol, detail=verdict.detail)

        blocked = self._entry_block(symbol, now_ms)
        if blocked is not None:
            return {
                "tick": tick.tick_id,
                "state": self.state,
                "managed": [],
                "execution": None,
                "blocked": blocked,
            }

        signal = self._decide(tick, now_ms)
        if signal is None:
            return {
                "tick": tick.tick_id,
                "state": self.state,
                "managed": [],
                "execution": None,
                "blocked": "no_signal",
            }

        lots, size_reason = self._size(signal, spec, tick)
        self.decisions.append(
            {
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": signal.side,
                "strategy": signal.strategy,
                "stop_points": str(signal.stop_points),
                "target_points": str(signal.target_points),
                "lots": None if lots is None else str(lots),
                "size_refused": size_reason,
                "quote": {"bid": str(tick.bid), "ask": str(tick.ask)},
                "quote_age_ms": int(now_ms - int(tick.source_time.timestamp() * 1000)),
            }
        )
        if lots is None:
            return self._refuse(SIZE_REFUSED, symbol=symbol, detail=size_reason)

        report = self.trader.execute(
            signal=signal,
            lots=lots,
            risk_account=self._risk_account(lots, signal, spec),
            notional_account=self._notional(lots, tick, spec),
            quote=tick,
            now_ms=now_ms,
            decision_latency_ms=0,
            currencies=(spec.quote_currency,),
        )
        self._record_execution(report, tick, now_ms)
        if report.status in {ACCEPTED} and report.fill is not None and report.fill.ticket:
            fill = report.fill
            if fill.price is not None and signal.stop_price is not None:
                self.book.open_from_fill(
                    fill=fill,
                    symbol=symbol,
                    side=signal.side,
                    stop_price=signal.stop_price,
                    target_price=signal.target_price,
                    strategy_fingerprint=signal.parameter_fingerprint,
                    now_ms=now_ms,
                )
                self._last_entry_ms = now_ms
        if report.status == UNKNOWN_OUTCOME:
            self.state = PAUSED
            self.pause_reason = "unresolved_submission"
            self._emit("unresolved", request_id=report.request_id)
        return {
            "tick": tick.tick_id,
            "state": self.state,
            "managed": [],
            "execution": report.audit_view(),
            "blocked": None if report.status == ACCEPTED else report.status,
        }

    # -- internals -------------------------------------------------------

    def _refuse(self, reason: str, **detail: Any) -> dict[str, Any]:
        self._entries_blocked[reason] = self._entries_blocked.get(reason, 0) + 1
        return {"state": self.state, "blocked": reason, "detail": detail, "execution": None}

    def _entry_block(self, symbol: str, now_ms: int) -> str | None:
        if not _ENTRY_READY & {self.state}:
            return NOT_RUNNING
        if getattr(self.venue, "has_unresolved", False):
            return UNRESOLVED_ORDER
        if len(self.book.open_positions) >= self.max_open_positions:
            return POSITION_LIMIT
        if self._last_entry_ms is not None and now_ms - self._last_entry_ms < self.cooldown_ms:
            return COOLDOWN
        if self.decision_interval_ms:
            previous = self._last_decision_ms.get(symbol)
            if previous is not None and now_ms - previous < self.decision_interval_ms:
                return COOLDOWN
        allowed, reasons = self.guardian.may_open()
        if not allowed:
            self._entries_blocked["guardian:" + ",".join(reasons)] = (
                self._entries_blocked.get("guardian:" + ",".join(reasons), 0) + 1
            )
            return ENTRY_BLOCKED + ":" + ",".join(reasons)
        return None

    def _decide(self, tick: Tick, now_ms: int):
        if self.admitted is None:
            return None
        self._last_decision_ms[tick.symbol] = now_ms
        window = self.core.window(tick.symbol)
        if not window:
            return None
        from .signals import SignalContext
        from .regime import classify, RegimeThresholds

        spec = self.specs[tick.symbol]
        state = classify(window, spec=spec, thresholds=RegimeThresholds())
        if not getattr(state, "tradeable", False):
            return None
        context = SignalContext(
            ticks=window,
            spec=spec,
            regime=state,
            spread_limit_points=self.guardian.limits.max_spread_points,
            max_quote_age_ms=self.guardian.limits.max_quote_age_ms,
        )
        evaluation = self.admitted.strategy.evaluate(context)
        if evaluation.signal is not None and spec.point > 0:
            # The signal carries distances in points; the priceable stop and target
            # exist only once the instrument's point size is attached, and the
            # point size is the broker's, never a default.
            evaluation = evaluation.__class__(
                strategy=evaluation.strategy,
                family=evaluation.family,
                outcome=evaluation.outcome,
                detail=evaluation.detail,
                signal=evaluation.signal.with_point(spec.point),
            )
        self.decisions.append(
            {
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "strategy": evaluation.strategy,
                "outcome": evaluation.outcome,
                "detail": evaluation.detail,
                "signal": evaluation.signal is not None,
            }
        )
        return evaluation.signal

    def _size(self, signal: Any, spec: InstrumentSpec, tick: Tick) -> tuple[Decimal | None, str]:
        if signal.stop_points <= 0 or spec.point <= 0:
            return None, "stop_distance_unusable"
        point_value = spec.point_value_per_lot
        if point_value is None or point_value <= 0:
            return None, "point_value_unavailable"
        risk = self._equity * self.risk_fraction
        per_lot = signal.stop_points * point_value
        if per_lot <= 0:
            return None, "risk_per_lot_zero"
        raw = risk / per_lot
        step = spec.volume_step if spec.volume_step > 0 else Decimal("0.01")
        size = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
        if spec.volume_max > 0:
            size = min(size, spec.volume_max)
        if size < spec.volume_min:
            return None, "below_broker_minimum"
        if size <= 0:
            return None, "size_rounds_to_zero"
        return size, ""

    def _risk_account(self, lots: Decimal, signal: Any, spec: InstrumentSpec) -> Decimal:
        point_value = spec.point_value_per_lot or Decimal(0)
        return lots * signal.stop_points * point_value

    def _notional(self, lots: Decimal, tick: Tick, spec: InstrumentSpec) -> Decimal:
        # The ask is the more expensive side, so an exposure estimate built from it
        # can only over-state what a limit check sees.
        price = tick.ask
        conversion = spec.conversion_rate if spec.conversion_rate is not None else Decimal(1)
        return lots * spec.contract_size * price * conversion

    def _record_execution(self, report: Any, tick: Tick, now_ms: int) -> None:
        self.guardian.observe_execution(
            slippage_points=(report.fill.slippage_points if report.fill else None),
            fill_latency_ms=(report.fill.latency_ms if report.fill else None),
        )
        self._emit(
            "execution",
            request_id=report.request_id,
            status=report.status,
            reasons=list(report.reasons),
            simulated=bool(getattr(report.fill, "simulated", False)),
        )
        if report.risk is not None and not report.risk.allowed:
            self._emit("risk_refused", reasons=list(report.risk.reasons))


__all__ = [
    "ARMED",
    "AdmittedStrategy",
    "AutonomousScalper",
    "BLOCKED",
    "IDLE",
    "NO_ADMISSION",
    "PAUSED",
    "RECOVERY_REQUIRED",
    "RUNNING",
    "RunEvent",
    "STOPPED",
]
