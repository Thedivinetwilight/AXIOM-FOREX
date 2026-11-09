"""Execution for the scalper: a simulated venue, and a trader that never guesses.

The venue is a **paper** venue.  It lives behind a small protocol
(:class:`ScalperVenue`) so that a future MT5 implementation has to satisfy exactly
the same contract — including the parts that are inconvenient: partial fills,
rejects, and submissions whose outcome is *unknown*.  This module must never import
the live MT5 order module; a regression test asserts that.

The trader's job is to be boring:

1. re-validate the signal against the quote that exists *now* (not the one that
   triggered the signal),
2. ask the guardian for permission and record its answer,
3. run the broker-side pre-check the adapter would run (volume step, minimum
   volume, stops level, market open),
4. submit exactly once under an idempotency key derived from the signal,
5. on an ambiguous outcome, **stop**: query, adopt, or refuse.  Never resubmit on
   the assumption that the first attempt failed,
6. verify that the resulting position carries broker-side protection, and treat a
   position it cannot verify as a blocker rather than as a success.

Every one of those steps produces an event the scribe records, including the
steps that ended in nothing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from app.execution.broker import TradingBlocked

from .config import CostTerms, InstrumentSpec, PAPER
from .guardian import Guardian, OpenPositionRisk, Proposal, RiskDecision
from .signals import Signal
from .ticks import Tick

BUY = "BUY"
SELL = "SELL"

#: Submission outcomes.  ``UNKNOWN`` is a first-class result, not an error.
ACCEPTED = "accepted"
REJECTED = "rejected"
UNKNOWN_OUTCOME = "unknown_outcome"
BLOCKED = "blocked"

#: Refusal reasons raised before anything is sent.
SIGNAL_EXPIRED = "signal_expired"
SIGNAL_STALE_QUOTE = "signal_quote_is_stale"
INSTRUMENT_UNKNOWN = "instrument_not_declared"
GUARDIAN_REFUSED = "guardian_refused"
PRECHECK_FAILED = "broker_precheck_failed"
PAPER_ONLY = "paper_mode_only"
ALREADY_SUBMITTED = "submission_already_in_flight"
UNRESOLVED_ORDER = "previous_order_outcome_unresolved"
PROTECTION_UNVERIFIED = "protective_orders_not_verified"
NO_QUOTE = "no_current_quote"
INVALID_VOLUME = "volume_outside_broker_terms"
STOPS_TOO_CLOSE = "stop_distance_inside_broker_stops_level"
MARKET_CLOSED = "market_closed"
LIVE_FORBIDDEN = "live_orders_are_not_available_in_this_build"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _q(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_HALF_UP) * step


@dataclass(frozen=True)
class VenueOrder:
    """What the venue is asked to do.  ``request_id`` is the idempotency key."""

    request_id: str
    symbol: str
    side: str
    lots: Decimal
    stop_price: Decimal | None
    target_price: Decimal | None
    requested_at_ms: int
    signal_fingerprint: str
    strategy: str
    point: Decimal = Decimal("0.00001")
    comment: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "symbol": self.symbol,
            "side": self.side,
            "lots": str(self.lots),
            "stop_price": None if self.stop_price is None else str(self.stop_price),
            "target_price": None if self.target_price is None else str(self.target_price),
            "requested_at_ms": self.requested_at_ms,
            "signal_fingerprint": self.signal_fingerprint,
            "strategy": self.strategy,
            "point": str(self.point),
        }


@dataclass(frozen=True)
class Fill:
    request_id: str
    status: str
    filled_lots: Decimal
    requested_lots: Decimal
    price: Decimal | None
    spread_points: Decimal
    slippage_points: Decimal
    latency_ms: int
    ticket: str | None
    reason: str = ""
    detail: str = ""
    simulated: bool = True

    @property
    def partial(self) -> bool:
        return 0 < self.filled_lots < self.requested_lots

    def audit_view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "filled_lots": str(self.filled_lots),
            "requested_lots": str(self.requested_lots),
            "price": None if self.price is None else str(self.price),
            "spread_points": str(self.spread_points),
            "slippage_points": str(self.slippage_points),
            "latency_ms": self.latency_ms,
            "ticket": self.ticket,
            "reason": self.reason,
            "detail": self.detail,
            "simulated": self.simulated,
        }


@runtime_checkable
class ScalperVenue(Protocol):
    """The contract a paper venue and a future MT5 venue both satisfy."""

    mode: str

    def submit(self, order: VenueOrder, quote: Tick) -> Fill: ...

    def query(self, request_id: str) -> Fill | None: ...

    def modify_protection(
        self, *, ticket: str, stop_price: Decimal | None, target_price: Decimal | None
    ) -> bool: ...

    def close(
        self, *, ticket: str, price: Decimal, spread_points: Decimal, reason: str
    ) -> Fill: ...

    def protection(self, ticket: str) -> tuple[Decimal | None, Decimal | None]: ...


@dataclass(frozen=True)
class VenueFaults:
    """Injected defects.  Every one of them is a case the trader must survive."""

    reject_every: int = 0
    partial_every: int = 0
    unknown_every: int = 0
    partial_fraction: Decimal = Decimal("0.5")
    max_latency_ms: int = 40
    seed: int = 7


class PaperVenue:
    """A deterministic simulated venue.  It prices from ticks, not from a model.

    Fills cross the spread and pay the declared slippage, so a strategy that looks
    profitable at the mid loses that advantage here.  Rejects, partial fills and
    ambiguous outcomes are injected by count, not by chance, so a test can name the
    exact submission that will be ambiguous.
    """

    mode = PAPER

    def __init__(
        self,
        *,
        cost: CostTerms,
        faults: VenueFaults | None = None,
        market_open: bool = True,
    ) -> None:
        self._cost = cost
        self._faults = faults or VenueFaults()
        self._rng = random.Random(self._faults.seed)
        self._market_open = market_open
        self._orders: dict[str, Fill] = {}
        self._protection: dict[str, tuple[Decimal | None, Decimal | None]] = {}
        self._submissions = 0
        self._rejects = 0
        self._partials = 0
        self._unknowns = 0
        self._next_ticket = 1000
        self._closed: dict[str, Fill] = {}

    # -- capability surface ---------------------------------------------

    @property
    def orders(self) -> dict[str, Fill]:
        return dict(self._orders)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "submissions": self._submissions,
            "rejects": self._rejects,
            "partials": self._partials,
            "unknown": self._unknowns,
        }

    def _latency(self, quote: Tick, received_at_ms: int) -> int:
        base = max(0, received_at_ms - _ms(quote.source_time))
        jitter = self._rng.randint(0, max(0, self._faults.max_latency_ms))
        return base + jitter

    def submit(self, order: VenueOrder, quote: Tick) -> Fill:
        self._submissions += 1
        existing = self._orders.get(order.request_id)
        if existing is not None:
            # Idempotency: a repeated key returns the first answer, never a second fill.
            return replace(existing, reason=existing.reason or "idempotent_replay")

        if not self._market_open:
            self._rejects += 1
            return self._reject(order, MARKET_CLOSED, "the simulated market is closed")

        point = order.point
        spread_points = quote.spread / point if point > 0 else Decimal(0)
        latency = self._latency(quote, order.requested_at_ms)
        if self._faults.reject_every and self._submissions % self._faults.reject_every == 0:
            self._rejects += 1
            return self._reject(order, "broker_rejected", "injected rejection (simulated)")

        if self._faults.unknown_every and self._submissions % self._faults.unknown_every == 0:
            self._unknowns += 1
            fill = Fill(
                request_id=order.request_id,
                status=UNKNOWN_OUTCOME,
                filled_lots=Decimal(0),
                requested_lots=order.lots,
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=latency,
                ticket=None,
                reason="timeout_without_confirmation",
                detail=(
                    "the submission timed out and the venue has not confirmed it; the order "
                    "may or may not exist, so it must be queried rather than resent"
                ),
            )
            self._orders[order.request_id] = fill
            # Half of the ambiguous submissions actually landed: that is exactly why a
            # blind retry is dangerous.
            if self._rng.random() < 0.5:
                self._orders[order.request_id] = self._book(order, quote, latency, spread_points)
            return fill

        return self._book(order, quote, latency, spread_points)

    def _book(self, order: VenueOrder, quote: Tick, latency: int, spread_points: Decimal) -> Fill:
        lots = order.lots
        if self._faults.partial_every and self._submissions % self._faults.partial_every == 0:
            self._partials += 1
            lots = _q(order.lots * self._faults.partial_fraction, Decimal("0.01"))
            if lots <= 0:
                lots = order.lots
        slip = _d(self._cost.slippage_points)
        if order.side == BUY:
            price = quote.ask + slip * order.point
        else:
            price = quote.bid - slip * order.point
        ticket = str(self._next_ticket)
        self._next_ticket += 1
        fill = Fill(
            request_id=order.request_id,
            status=ACCEPTED,
            filled_lots=lots,
            requested_lots=order.lots,
            price=price,
            spread_points=spread_points,
            slippage_points=slip,
            latency_ms=latency,
            ticket=ticket,
            reason="filled",
        )
        self._orders[order.request_id] = fill
        if order.stop_price is not None or order.target_price is not None:
            self._protection[ticket] = (order.stop_price, order.target_price)
        return fill

    @staticmethod
    def _reject(order: VenueOrder, reason: str, detail: str) -> Fill:
        return Fill(
            request_id=order.request_id,
            status=REJECTED,
            filled_lots=Decimal(0),
            requested_lots=order.lots,
            price=None,
            spread_points=Decimal(0),
            slippage_points=Decimal(0),
            latency_ms=0,
            ticket=None,
            reason=reason,
            detail=detail,
        )

    def query(self, request_id: str) -> Fill | None:
        return self._orders.get(request_id)

    def modify_protection(
        self, *, ticket: str, stop_price: Decimal | None, target_price: Decimal | None
    ) -> bool:
        if ticket not in self._protection:
            return False
        self._protection[ticket] = (stop_price, target_price)
        return True

    def protection(self, ticket: str) -> tuple[Decimal | None, Decimal | None]:
        return self._protection.get(ticket, (None, None))

    def close(
        self, *, ticket: str, price: Decimal, spread_points: Decimal, reason: str
    ) -> Fill:
        """Record a closing fill at the price the position manager resolved.

        The exit price has already crossed the spread, so this method must not cross
        it again: charging a second spread on the way out would make every strategy
        look worse than it is, which is as dishonest as making it look better.
        """

        fill = Fill(
            request_id=f"close:{ticket}",
            status=ACCEPTED,
            filled_lots=Decimal(0),
            requested_lots=Decimal(0),
            price=price,
            spread_points=spread_points,
            slippage_points=_d(self._cost.slippage_points),
            latency_ms=0,
            ticket=ticket,
            reason=reason,
            detail="closing fill is recorded by the position manager, which knows the side and size",
        )
        self._closed[ticket] = fill
        self._protection.pop(ticket, None)
        return fill


def _ms(moment: Any) -> int:
    return int(moment.timestamp() * 1000)


@dataclass(frozen=True)
class Precheck:
    """The broker-side order check, run before submission."""

    passed: bool
    reasons: tuple[str, ...]
    checks: tuple[tuple[str, bool, str], ...]

    def audit_view(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": list(self.reasons),
            "checks": [
                {"name": name, "passed": ok, "detail": detail}
                for name, ok, detail in self.checks
            ],
        }


def precheck(
    *,
    spec: InstrumentSpec,
    side: str,
    lots: Decimal,
    price: Decimal,
    stop_price: Decimal | None,
    target_price: Decimal | None,
    market_open: bool = True,
) -> Precheck:
    """Volume terms and stops level, checked the way the adapter would check them."""

    checks: list[tuple[str, bool, str]] = []
    reasons: list[str] = []

    open_ok = bool(market_open)
    checks.append(("market_open", open_ok, "" if open_ok else "the symbol is not trading"))
    if not open_ok:
        reasons.append(MARKET_CLOSED)

    step = _d(spec.volume_step)
    remainder = (lots / step) % 1 if step > 0 else Decimal(0)
    volume_ok = (
        lots >= _d(spec.volume_min) and lots <= _d(spec.volume_max) and remainder == 0 and lots > 0
    )
    checks.append(
        (
            "volume_terms",
            volume_ok,
            f"{lots} lots vs min {spec.volume_min}, max {spec.volume_max}, step {spec.volume_step}",
        )
    )
    if not volume_ok:
        reasons.append(INVALID_VOLUME)

    minimum_distance = _d(spec.stops_level_points) * _d(spec.point)
    distance_ok = True
    distance_detail = "no stop supplied"
    if stop_price is not None:
        distance = abs(price - stop_price)
        distance_ok = distance >= minimum_distance if minimum_distance > 0 else distance > 0
        distance_detail = f"{distance} vs broker minimum {minimum_distance}"
    checks.append(("stops_level", distance_ok, distance_detail))
    if not distance_ok:
        reasons.append(STOPS_TOO_CLOSE)

    side_ok = side in {BUY, SELL}
    if not side_ok:
        reasons.append("invalid_side")
    if stop_price is not None:
        wrong_side = (side == BUY and stop_price >= price) or (
            side == SELL and stop_price <= price
        )
        if wrong_side:
            reasons.append("stop_on_the_wrong_side_of_entry")
    if target_price is not None:
        wrong_side = (side == BUY and target_price <= price) or (
            side == SELL and target_price >= price
        )
        if wrong_side:
            reasons.append("target_on_the_wrong_side_of_entry")
    return Precheck(passed=not reasons, reasons=tuple(reasons), checks=tuple(checks))


# ---------------------------------------------------------------------------
# the trader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionReport:
    """Everything that happened on the way to (or away from) a fill."""

    request_id: str
    status: str
    reasons: tuple[str, ...]
    signal: Signal
    fill: Fill | None
    risk: RiskDecision | None
    precheck: Precheck | None
    latency_ms: int
    events: tuple[dict[str, Any], ...] = ()

    @property
    def filled(self) -> bool:
        return self.fill is not None and self.fill.status == ACCEPTED and self.fill.filled_lots > 0

    def audit_view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "reasons": list(self.reasons),
            "signal": self.signal.audit_view(),
            "fill": None if self.fill is None else self.fill.audit_view(),
            "risk": None if self.risk is None else self.risk.audit_view(),
            "precheck": None if self.precheck is None else self.precheck.audit_view(),
            "latency_ms": self.latency_ms,
            "events": [event for event in self.events],
        }


class UnresolvedOrder(Exception):
    """An ambiguous submission that has not been resolved.  Entries must stop."""

    def __init__(self, request_id: str, detail: str):
        super().__init__(f"{UNRESOLVED_ORDER}: {request_id} — {detail}")
        self.request_id = request_id
        self.detail = detail


class Trader:
    """Validates, asks, submits once, verifies, and refuses to guess."""

    def __init__(
        self,
        *,
        venue: ScalperVenue,
        guardian: Guardian,
        cost: CostTerms,
        max_quote_age_ms: int,
        specs: Mapping[str, InstrumentSpec],
        signal_ttl_ms: int = 750,
        engine: str = "scalper",
    ) -> None:
        if venue.mode != PAPER:
            # Enforced here as well as in configuration: a venue that is not paper is
            # never reachable from this class, whatever it is called.
            raise TradingBlocked(PAPER_ONLY)
        self._venue = venue
        self._guardian = guardian
        self._cost = cost
        self._max_quote_age_ms = max_quote_age_ms
        self._signal_ttl_ms = signal_ttl_ms
        self._engine = engine
        # Declared instrument terms, by symbol.  An unknown symbol is refused, never
        # guessed from the pair name.
        self._specs: dict[str, InstrumentSpec] = dict(specs)
        self._submitted: dict[str, ExecutionReport] = {}
        self._unresolved: dict[str, Fill] = {}
        self._positions: dict[str, str] = {}  # ticket -> request_id
        self._slippage: list[Decimal] = []
        self._latencies: list[int] = []

    # -- introspection ---------------------------------------------------

    @property
    def unresolved(self) -> dict[str, Fill]:
        return dict(self._unresolved)

    @property
    def has_unresolved(self) -> bool:
        return bool(self._unresolved)

    @property
    def submitted(self) -> dict[str, ExecutionReport]:
        return dict(self._submitted)

    def execution_stats(self) -> dict[str, Any]:
        return {
            "submitted": len(self._submitted),
            "unresolved": len(self._unresolved),
            "venue": self._venue.stats if hasattr(self._venue, "stats") else {},
            "mean_slippage_points": (
                str(round(sum(self._slippage) / len(self._slippage), 4)) if self._slippage else None
            ),
            "max_slippage_points": (str(max(self._slippage)) if self._slippage else None),
            "mean_latency_ms": (
                round(sum(self._latencies) / len(self._latencies), 1) if self._latencies else None
            ),
            "max_latency_ms": (max(self._latencies) if self._latencies else None),
            "note": "latency is measured inside this process against the quote timestamps it was given",
        }

    # -- the path --------------------------------------------------------

    def request_id_for(self, signal: Signal) -> str:
        return f"{signal.parameter_fingerprint[:12]}:{signal.tick_id[:12]}:{signal.side}"

    def execute(
        self,
        *,
        signal: Signal,
        lots: Decimal,
        risk_account: Decimal,
        notional_account: Decimal,
        quote: Tick,
        now_ms: int,
        decision_latency_ms: int,
        currencies: tuple[str, ...] = (),
    ) -> ExecutionReport:
        request_id = self.request_id_for(signal)
        events: list[dict[str, Any]] = []

        def finish(status: str, reasons: tuple[str, ...], **kwargs: Any) -> ExecutionReport:
            report = ExecutionReport(
                request_id=request_id,
                status=status,
                reasons=reasons,
                signal=signal,
                fill=kwargs.get("fill"),
                risk=kwargs.get("risk"),
                precheck=kwargs.get("precheck"),
                latency_ms=kwargs.get("latency_ms", 0),
                events=tuple(events),
            )
            self._submitted[request_id] = report
            return report

        if self._unresolved:
            events.append(
                {
                    "event": "entry_blocked",
                    "reason": UNRESOLVED_ORDER,
                    "detail": f"{len(self._unresolved)} submission(s) still unresolved",
                }
            )
            return finish(BLOCKED, (UNRESOLVED_ORDER,))
        if request_id in self._submitted:
            return finish(BLOCKED, (ALREADY_SUBMITTED,))

        spec = self._specs.get(signal.symbol)
        if spec is None:
            events.append(
                {
                    "event": "entry_blocked",
                    "reason": INSTRUMENT_UNKNOWN,
                    "detail": f"{signal.symbol} has no declared instrument terms",
                }
            )
            return finish(BLOCKED, (INSTRUMENT_UNKNOWN,))

        age = now_ms - _ms(quote.source_time)
        if age > self._max_quote_age_ms:
            events.append(
                {"event": "entry_blocked", "reason": SIGNAL_STALE_QUOTE, "detail": f"{age} ms old"}
            )
            return finish(BLOCKED, (SIGNAL_STALE_QUOTE,))
        if now_ms - _ms(signal.at) > self._signal_ttl_ms:
            events.append(
                {
                    "event": "entry_blocked",
                    "reason": SIGNAL_EXPIRED,
                    "detail": f"signal is {now_ms - _ms(signal.at)} ms old, ttl {self._signal_ttl_ms}",
                }
            )
            return finish(BLOCKED, (SIGNAL_EXPIRED,))

        stop_price = signal.stop_price
        target_price = signal.target_price
        if stop_price is None:
            events.append(
                {
                    "event": "entry_blocked",
                    "reason": PROTECTION_UNVERIFIED,
                    "detail": "the signal has no priceable stop, so protective orders cannot be placed",
                }
            )
            return finish(BLOCKED, (PROTECTION_UNVERIFIED,))

        proposal = Proposal(
            symbol=signal.symbol,
            pair=spec.pair,
            side=signal.side,
            risk_account=_d(risk_account),
            notional_account=_d(notional_account),
            stop_points=signal.stop_points,
            spread_points=quote.spread / spec.point if spec.point > 0 else None,
            quote_age_ms=age,
            decision_latency_ms=decision_latency_ms,
            fill_latency_ms=None,
            slippage_points=None,
            currencies=currencies,
        )
        decision = self._guardian.evaluate(proposal)
        events.append(
            {"event": "guardian_decision", "allowed": decision.allowed, "reasons": list(decision.reasons)}
        )
        if not decision.allowed:
            return finish(BLOCKED, (GUARDIAN_REFUSED, *decision.reasons), risk=decision)

        order = VenueOrder(
            request_id=request_id,
            symbol=signal.symbol,
            side=signal.side,
            lots=_d(lots),
            stop_price=stop_price,
            target_price=target_price,
            requested_at_ms=now_ms,
            signal_fingerprint=signal.parameter_fingerprint,
            strategy=signal.strategy,
            point=spec.point,
        )
        check = precheck(
            spec=spec,
            side=order.side,
            lots=order.lots,
            price=quote.ask if order.side == BUY else quote.bid,
            stop_price=order.stop_price,
            target_price=order.target_price,
        )
        events.append({"event": "precheck", "passed": check.passed, "reasons": list(check.reasons)})
        if not check.passed:
            return finish(BLOCKED, (PRECHECK_FAILED, *check.reasons), risk=decision, precheck=check)

        fill = self._venue.submit(order, quote)
        self._latencies.append(fill.latency_ms)
        events.append({"event": "submission", "status": fill.status, "simulated": fill.simulated})
        if fill.status == UNKNOWN_OUTCOME:
            self._unresolved[request_id] = fill
            events.append(
                {
                    "event": "outcome_unknown",
                    "detail": "querying instead of resending; entries are suspended until resolved",
                }
            )
            return finish(UNKNOWN_OUTCOME, ("outcome_unknown",), fill=fill, risk=decision, precheck=check, latency_ms=fill.latency_ms)
        if fill.status == REJECTED:
            events.append({"event": "rejected", "reason": fill.reason})
            return finish(REJECTED, (fill.reason or "rejected",), fill=fill, risk=decision, precheck=check, latency_ms=fill.latency_ms)

        self._slippage.append(fill.slippage_points)
        if fill.ticket is not None:
            self._positions[fill.ticket] = request_id
        if fill.partial:
            events.append(
                {
                    "event": "partial_fill",
                    "filled": str(fill.filled_lots),
                    "requested": str(fill.requested_lots),
                    "detail": "the position is booked at the filled size; the remainder is not chased",
                }
            )
        stop_now, target_now = (
            self._venue.protection(fill.ticket) if fill.ticket is not None else (None, None)
        )
        protected = stop_now is not None and (
            (fill.price is None)
            or (order.side == BUY and stop_now < fill.price)
            or (order.side == SELL and stop_now > fill.price)
        )
        events.append(
            {
                "event": "protection_verified" if protected else "protection_missing",
                "stop": None if stop_now is None else str(stop_now),
                "target": None if target_now is None else str(target_now),
            }
        )
        if not protected:
            # An unprotected position is a blocker, not a success.  It is reported with
            # the guard rails that must apply next to it.
            self._guardian.suspend_entries(PROTECTION_UNVERIFIED)
            return finish(
                ACCEPTED,
                (PROTECTION_UNVERIFIED,),
                fill=fill,
                risk=decision,
                precheck=check,
                latency_ms=fill.latency_ms,
            )
        return finish(ACCEPTED, (), fill=fill, risk=decision, precheck=check, latency_ms=fill.latency_ms)

    # -- recovery --------------------------------------------------------

    def resolve(self, request_id: str) -> tuple[str, Fill | None]:
        """Query an ambiguous submission.  Only a confirmed answer resolves it."""

        fill = self._venue.query(request_id)
        if fill is None:
            return "not_found", None
        if fill.status == UNKNOWN_OUTCOME:
            return "still_unknown", fill
        if fill.status == ACCEPTED and fill.filled_lots > 0:
            self._unresolved.pop(request_id, None)
            self._slippage.append(fill.slippage_points)
            if fill.ticket is not None:
                self._positions[fill.ticket] = request_id
            return "filled", fill
        self._unresolved.pop(request_id, None)
        return "rejected", fill

    def retry_after_not_found(self, request_id: str) -> bool:
        """A resend is legal only when the venue confirmed it never saw the order.

        The same idempotency key is reused, so a late arrival of the first attempt
        cannot turn into a second position.
        """

        return request_id not in self._unresolved and self._venue.query(request_id) is None

    def reconcile(self, *, tickets: Iterable[str]) -> dict[str, Any]:
        """Compare what this trader believes it opened with what the venue holds."""

        tickets = list(tickets)
        missing = [ticket for ticket in tickets if ticket not in self._positions]
        orphan = [ticket for ticket in self._positions if ticket not in tickets]
        unprotected = [
            ticket
            for ticket in tickets
            if self._venue.protection(ticket)[0] is None
        ]
        return {
            "matched": len(tickets) - len(missing),
            "unknown_to_trader": missing,
            "known_to_trader_only": orphan,
            "unprotected": unprotected,
            "ok": not missing and not orphan and not unprotected,
            "note": (
                "A ticket the venue holds but the trader does not know is an orphan: it is "
                "reported, never adopted silently, and never closed by a mode change."
            ),
        }

    def resolved_fill(self, request_id: str) -> Fill | None:
        report = self._submitted.get(request_id)
        return None if report is None else report.fill


__all__ = [
    "ACCEPTED",
    "ALREADY_SUBMITTED",
    "BLOCKED",
    "BUY",
    "ExecutionReport",
    "Fill",
    "GUARDIAN_REFUSED",
    "INSTRUMENT_UNKNOWN",
    "LIVE_FORBIDDEN",
    "MARKET_CLOSED",
    "PAPER_ONLY",
    "PRECHECK_FAILED",
    "PROTECTION_UNVERIFIED",
    "PaperVenue",
    "Precheck",
    "REJECTED",
    "SELL",
    "SIGNAL_EXPIRED",
    "SIGNAL_STALE_QUOTE",
    "STOPS_TOO_CLOSE",
    "ScalperVenue",
    "Trader",
    "UNKNOWN_OUTCOME",
    "UNRESOLVED_ORDER",
    "UnresolvedOrder",
    "VenueFaults",
    "VenueOrder",
    "precheck",
]
