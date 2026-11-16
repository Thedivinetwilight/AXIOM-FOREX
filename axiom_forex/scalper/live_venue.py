"""The broker venue: the scalper's order contract, satisfied by the live leg.

The scalper already talks to its execution through one protocol
(:class:`~axiom_forex.scalper.execution.ScalperVenue`): submit one order under an
idempotency key, query an ambiguous one, modify protection, close, and read back
the protection the broker actually holds.  :class:`PaperVenue` implements it
against a simulation.  This class implements the *same* contract against the
guarded live path the rest of the system already uses:

```
BrokerVenue.submit / close
      │                 builds one OrderRequest for the caller's plan factory
      ▼
LiveLeg.execute(plan)     → blockers (mode, arming, real account, unresolved,
      │                     ledger==terminal, batch limits, protective stop)
      │                   → PreTradeGate.evaluate
      │                   → ExecutionRouter.route_live
      ▼                   → MT5TradingAdapter.submit (write-ahead, order_send)
```

There is no second MT5 route here: this module imports nothing from
``axiom_forex.brokers.mt5``, holds no adapter of its own, and cannot send
anything the leg would not send.  It maps the leg's durable answer into the
scalper's :class:`~axiom_forex.scalper.execution.Fill`, including the parts that
matter most to an autonomous loop:

* a **partial fill** is booked at the size the broker actually filled;
* an **unknown outcome** is reported as ``UNKNOWN_OUTCOME`` *and* latched, so
  every later submission is refused until :meth:`query` proves what happened;
* a **close** goes through the same leg, naming the exact position the caller
  asked to close (the gate checks it against the ledger);
* protection is read from the broker's own position record, never assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable, Mapping

from .config import LIVE
from .execution import (
    ACCEPTED,
    BLOCKED,
    REJECTED,
    UNKNOWN_OUTCOME,
    Fill,
    VenueOrder,
)
from .ticks import Tick

#: Reasons this venue reports in its own right.
NO_PLAN = "venue_plan_unavailable"
LEG_REFUSED = "venue_leg_refused"
UNRESOLVED_LATCHED = "venue_unresolved_order"
MODIFICATION_UNSUPPORTED = "venue_modification_unsupported"
NOT_AN_ENTRY = "venue_not_an_entry"


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(0)


@dataclass
class VenueRecord:
    """Everything this venue did, in order.  Read-only from outside."""

    submissions: list[dict[str, Any]] = field(default_factory=list)
    closes: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)

    def audit_view(self) -> dict[str, Any]:
        return {
            "submissions": [dict(item) for item in self.submissions[-20:]],
            "closes": [dict(item) for item in self.closes[-20:]],
            "refusals": [dict(item) for item in self.refusals[-20:]],
        }


class BrokerVenue:
    """``ScalperVenue`` over the existing guarded live leg.  Sends nothing itself."""

    #: The scalper's protocol reads this to know what it is talking to.
    mode = LIVE
    #: A venue that reaches a broker says so in one word, and the trader checks it.
    live_execution = True
    simulated = False

    def __init__(
        self,
        *,
        leg: Any,
        plan_factory: Callable[..., Any],
        protection_reader: Callable[[str], tuple[Decimal | None, Decimal | None]] | None = None,
        position_reader: Callable[[], list[dict[str, Any]]] | None = None,
        clock_ms: Callable[[], int] | None = None,
        symbols: Mapping[str, str] | None = None,
    ) -> None:
        self.leg = leg
        #: ``(order_or_ticket, quote) -> plan``.  The caller owns how a scalper
        #: order becomes an ``OrderRequest``; this venue never invents one.
        self._plan_factory = plan_factory
        self._protection_reader = protection_reader
        self._position_reader = position_reader
        self._clock_ms = clock_ms or (lambda: 0)
        #: scalper symbol -> broker symbol, so a close can name the right position.
        self._symbols = dict(symbols or {})
        self.record = VenueRecord()
        self._unresolved: set[str] = set()
        self._tickets: dict[str, str] = {}

    # -- introspection ---------------------------------------------------

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "venue": "mt5_guarded_live_leg",
            "mode": self.mode,
            "submitted": len(self.record.submissions),
            "closes": len(self.record.closes),
            "refusals": len(self.record.refusals),
            "unresolved": sorted(self._unresolved),
        }

    @property
    def has_unresolved(self) -> bool:
        return bool(self._unresolved)

    def _new_ticket(self) -> str | None:
        """The one position the broker opened that this venue had not seen."""

        if self._position_reader is None:
            return None
        try:
            tickets = [str(row.get("ticket")) for row in (self._position_reader() or ()) if row.get("ticket")]
        except Exception:
            return None
        unseen = [ticket for ticket in tickets if ticket not in self._tickets.values()]
        return unseen[-1] if len(unseen) == 1 else None

    def protection(self, ticket: str) -> tuple[Decimal | None, Decimal | None]:
        if self._protection_reader is None:
            return (None, None)
        try:
            return self._protection_reader(str(ticket))
        except Exception:
            return (None, None)

    def position_ids(self) -> list[str]:
        if self._position_reader is None:
            return []
        try:
            rows = list(self._position_reader() or ())
        except Exception:
            return []
        return [str(row.get("ticket") or "") for row in rows if row.get("ticket")]

    # -- submission ------------------------------------------------------

    def submit(self, order: VenueOrder, quote: Tick) -> Fill:
        if self._unresolved:
            return self._refuse(
                order,
                UNRESOLVED_LATCHED,
                "a previous submission is still unresolved; nothing is re-sent",
            )
        if order.request_id in self._tickets:
            return self._refuse(
                order,
                "duplicate_request",
                "this request id already produced an order",
            )
        try:
            plan = self._plan_factory(order=order, quote=quote)
        except Exception as exc:
            return self._refuse(order, NO_PLAN, type(exc).__name__)
        if plan is None:
            return self._refuse(order, NO_PLAN, "the caller could not build a request")
        started = self._clock_ms()
        try:
            outcome = dict(self.leg.execute(plan) or {})
        except Exception as exc:
            # The leg raised: the broker may or may not have the order.  Latch it.
            self._unresolved.add(order.request_id)
            self.record.submissions.append(
                {
                    "request_id": order.request_id,
                    "status": UNKNOWN_OUTCOME,
                    "error": type(exc).__name__,
                }
            )
            return Fill(
                request_id=order.request_id,
                status=UNKNOWN_OUTCOME,
                filled_lots=Decimal(0),
                requested_lots=order.lots,
                price=None,
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=max(0, self._clock_ms() - started),
                ticket=None,
                reason="venue_execute_raised",
                detail=type(exc).__name__,
                simulated=False,
            )
        fill = self._fill_from(order, outcome, latency_ms=max(0, self._clock_ms() - started))
        if fill.status == ACCEPTED and not fill.ticket:
            # The broker is the authority on which position the fill created.  If
            # it cannot name exactly one new ticket, the fill carries none and the
            # trader reports the protection as unverified rather than guessing.
            fill = replace(fill, ticket=self._new_ticket())
        self.record.submissions.append(
            {"request_id": order.request_id, **fill.audit_view()}
        )
        if fill.status == UNKNOWN_OUTCOME:
            self._unresolved.add(order.request_id)
        if fill.ticket:
            self._tickets[order.request_id] = fill.ticket
        return fill

    def _fill_from(self, order: VenueOrder, outcome: Mapping[str, Any], *, latency_ms: int) -> Fill:
        submitted = bool(outcome.get("submitted"))
        unknown = bool(outcome.get("unknown")) or str(outcome.get("state") or "").upper() == (
            "UNRESOLVED"
        )
        filled = _d(outcome.get("volume_filled"))
        requested = _d(outcome.get("volume_requested")) or _d(order.lots)
        price = outcome.get("price")
        # The leg books the fill and reports the broker's own ticket in that
        # booking; it is the identity the position manager needs, so it is read
        # from wherever the leg actually put it.
        booking = outcome.get("booking") if isinstance(outcome.get("booking"), Mapping) else {}
        booked_position = str((booking or {}).get("position_id") or "")
        ticket = (
            outcome.get("order_ticket")
            or outcome.get("deal_ticket")
            or outcome.get("position_ticket")
            or (booking or {}).get("order_ticket")
            # The leg books at ``mt5:<ticket>``; that suffix is the broker's own id.
            or (booked_position.split(":", 1)[1] if ":" in booked_position else booked_position)
        )
        reasons = [str(item) for item in (outcome.get("reasons") or ())]
        if not submitted:
            return Fill(
                request_id=order.request_id,
                status=BLOCKED,
                filled_lots=Decimal(0),
                requested_lots=requested,
                price=None,
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=latency_ms,
                ticket=None,
                reason=(reasons[0] if reasons else LEG_REFUSED),
                detail=str(outcome.get("detail") or ""),
                simulated=False,
            )
        if unknown:
            return Fill(
                request_id=order.request_id,
                status=UNKNOWN_OUTCOME,
                filled_lots=filled,
                requested_lots=requested,
                price=None if price is None else _d(price),
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=latency_ms,
                ticket=str(ticket) if ticket else None,
                reason=", ".join(reasons) or "outcome_unknown",
                detail=str(outcome.get("state") or ""),
                simulated=False,
            )
        state = str(outcome.get("state") or "").upper()
        if state == "REJECTED" or "REJECT" in str(outcome.get("classification") or "").upper():
            return Fill(
                request_id=order.request_id,
                status=REJECTED,
                filled_lots=Decimal(0),
                requested_lots=requested,
                price=None,
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=latency_ms,
                ticket=None,
                reason=str(outcome.get("retcode_name") or "rejected"),
                detail=str(outcome.get("retcode") or ""),
                simulated=False,
            )
        if filled <= 0:
            return Fill(
                request_id=order.request_id,
                status=REJECTED,
                filled_lots=Decimal(0),
                requested_lots=requested,
                price=None,
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=latency_ms,
                ticket=None,
                reason="no_fill_reported",
                detail=state,
                simulated=False,
            )
        reference = order.stop_price
        slippage = Decimal(0)
        return Fill(
            request_id=order.request_id,
            status=ACCEPTED,
            filled_lots=filled,
            requested_lots=requested,
            price=None if price is None else _d(price),
            spread_points=Decimal(0),
            slippage_points=slippage if reference is not None else slippage,
            latency_ms=latency_ms,
            ticket=str(ticket) if ticket else None,
            # The broker's own stop, read back below by the trader.  Nothing here
            # claims a protection the terminal has not been asked about.
            detail=str(outcome.get("retcode_name") or ""),
            simulated=False,
        )

    def _refuse(self, order: VenueOrder, reason: str, detail: str) -> Fill:
        self.record.refusals.append(
            {"request_id": order.request_id, "reason": reason, "detail": detail}
        )
        return Fill(
            request_id=order.request_id,
            status=BLOCKED,
            filled_lots=Decimal(0),
            requested_lots=_d(order.lots),
            price=None,
            spread_points=Decimal(0),
            slippage_points=Decimal(0),
            latency_ms=0,
            ticket=None,
            reason=reason,
            detail=detail,
            simulated=False,
        )

    # -- query / modify / close ------------------------------------------

    def query(self, request_id: str) -> Fill | None:
        """Ask the durable record what became of one submission.  Sends nothing."""

        reader = getattr(self.leg, "last_outcome", None)
        outcome: Mapping[str, Any] = {}
        repository = getattr(getattr(self.leg, "repository", None), "get_order", None)
        if callable(repository):
            try:
                outcome = dict(repository(request_id) or {})
            except Exception:
                outcome = {}
        elif isinstance(reader, Mapping):
            outcome = dict(reader)
        if not outcome:
            return None
        state = str(outcome.get("state") or "").upper()
        if state in {"SUBMITTING", "ACCEPTED", "PARTIAL", ""}:
            # Still in doubt: report it as unknown and keep the latch closed.
            return Fill(
                request_id=request_id,
                status=UNKNOWN_OUTCOME,
                filled_lots=_d(outcome.get("volume_filled")),
                requested_lots=_d(outcome.get("volume_requested")),
                price=None,
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=None,
                reason="still_unknown",
                detail=state,
                simulated=False,
            )
        self._unresolved.discard(request_id)
        filled = _d(outcome.get("volume_filled"))
        return Fill(
            request_id=request_id,
            status=ACCEPTED if filled > 0 else REJECTED,
            filled_lots=filled,
            requested_lots=_d(outcome.get("volume_requested")),
            price=None,
            spread_points=Decimal(0),
            slippage_points=Decimal(0),
            latency_ms=0,
            ticket=str(outcome.get("order_ticket") or outcome.get("deal_ticket") or "") or None,
            reason=str(outcome.get("retcode_name") or state),
            detail=state,
            simulated=False,
        )

    def modify_protection(
        self, *, ticket: str, stop_price: Decimal | None, target_price: Decimal | None
    ) -> bool:
        """AXIOM's guarded path has no stop-modification route, and says so.

        Returning ``False`` is the honest answer: the position book then reports
        the protection it could not set instead of believing it was set.
        """

        self.record.refusals.append(
            {
                "request_id": f"modify:{ticket}",
                "reason": MODIFICATION_UNSUPPORTED,
                "detail": "no stop-modification route exists on the guarded live path",
            }
        )
        return False

    def close(
        self, *, ticket: str, price: Decimal, spread_points: Decimal, reason: str
    ) -> Fill:
        if self._unresolved:
            return Fill(
                request_id=f"close:{ticket}",
                status=BLOCKED,
                filled_lots=Decimal(0),
                requested_lots=Decimal(0),
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=str(ticket),
                reason=UNRESOLVED_LATCHED,
                detail="an earlier submission is unresolved",
                simulated=False,
            )
        try:
            plan = self._plan_factory(ticket=str(ticket), price=_d(price), reason=reason)
        except Exception as exc:
            plan = None
            detail = type(exc).__name__
        else:
            detail = ""
        if plan is None:
            fill = Fill(
                request_id=f"close:{ticket}",
                status=BLOCKED,
                filled_lots=Decimal(0),
                requested_lots=Decimal(0),
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=str(ticket),
                reason=NO_PLAN,
                detail=detail,
                simulated=False,
            )
            self.record.refusals.append({"request_id": fill.request_id, **fill.audit_view()})
            return fill
        try:
            outcome = dict(self.leg.execute(plan) or {})
        except Exception as exc:
            self._unresolved.add(f"close:{ticket}")
            return Fill(
                request_id=f"close:{ticket}",
                status=UNKNOWN_OUTCOME,
                filled_lots=Decimal(0),
                requested_lots=Decimal(0),
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=str(ticket),
                reason="venue_execute_raised",
                detail=type(exc).__name__,
                simulated=False,
            )
        submitted = bool(outcome.get("submitted"))
        filled = _d(outcome.get("volume_filled"))
        unknown = bool(outcome.get("unknown"))
        status = (
            UNKNOWN_OUTCOME
            if unknown
            else ACCEPTED
            if submitted and filled > 0
            else REJECTED
            if submitted
            else BLOCKED
        )
        if unknown:
            self._unresolved.add(f"close:{ticket}")
        fill = Fill(
            request_id=f"close:{ticket}",
            status=status,
            filled_lots=filled,
            requested_lots=_d(outcome.get("volume_requested")),
            price=_d(price),
            spread_points=spread_points,
            slippage_points=Decimal(0),
            latency_ms=0,
            ticket=str(ticket),
            reason=str(outcome.get("retcode_name") or ""),
            detail=", ".join(str(item) for item in (outcome.get("reasons") or ())),
            simulated=False,
        )
        self.record.closes.append({"request_id": fill.request_id, "reason": reason, **fill.audit_view()})
        return fill


__all__ = ["BrokerVenue", "VenueRecord", "MODIFICATION_UNSUPPORTED", "NOT_AN_ENTRY"]


class BrokerPlanFactory:
    """Turn one scalper order (or close) into the request the live leg judges.

    The leg takes an ``OrderRequest`` and the ``GateContext`` it is judged in;
    this class builds both from the provider platform the caller already holds.
    It invents nothing: the side, the quantity and the close target come from the
    caller, the provider id and broker symbol come from the platform, and the
    quote is the one the decision was made on.
    """

    def __init__(self, *, platform: Any, strategy_id: str, clock: Any = None) -> None:
        self.platform = platform
        self.strategy_id = str(strategy_id or "")
        self._clock = clock

    @staticmethod
    def _as_quote(quote: Any, pair: str) -> Any:
        """The scalper's ``Tick`` (or a broker quote) as an ``OrderRequest`` quote.

        The scalper hands over its own validated tick; the gate wants a
        ``ForexQuote``.  The conversion is a re-labelling of the same bid/ask and
        the same source instant — no price is touched.
        """

        if quote is None:
            raise RuntimeError("no_quote")
        if hasattr(quote, "timestamp_utc"):
            return quote
        from app.data.models import ForexQuote

        at = getattr(quote, "source_time", None)
        if at is None:
            raise RuntimeError("quote_without_timestamp")
        return ForexQuote(
            pair=pair,
            timestamp=at,
            bid=getattr(quote, "bid"),
            ask=getattr(quote, "ask"),
            source="MT5_TICK",
            quote_id=str(getattr(quote, "tick_id", "") or ""),
            received_at=getattr(quote, "received_at", at),
            metadata={"basis": "AX_SCALPER_LIVE_TICK"},
        )

    def _context(self, at: Any, bar: Any) -> Any:
        factory = getattr(self.platform, "context_factory", None)
        if factory is None:
            raise RuntimeError("no_gate_context_factory")
        return factory.build(at=at, bar=bar)

    def _bar(self, *, at: Any, bid: Any, ask: Any, pair: str) -> Any:
        from app.data.models import ForexQuoteBar, QuoteSideBar

        return ForexQuoteBar(
            pair=pair,
            timeframe="M1",
            timestamp=at.replace(microsecond=0),
            duration_seconds=60,
            bid=QuoteSideBar(open=bid, high=bid, low=bid, close=bid),
            ask=QuoteSideBar(open=ask, high=ask, low=ask, close=ask),
            source="AXIOM_SCALPER_LIVE_SINGLE_QUOTE",
            quote_count=1,
            last_quote_at=at,
            complete=True,
        )

    def __call__(self, *, order: Any = None, quote: Any = None, ticket: str = "",
                 price: Any = None, reason: str = "") -> Any:
        from app.common.types import OrderSide
        from app.risk.models import CloseIntent, OrderRequest

        spec = getattr(self.platform, "spec", None)
        config = getattr(self.platform, "execution_config", None)
        if spec is None or config is None:
            raise RuntimeError("platform_incomplete")
        pair = str(getattr(spec, "canonical_pair", "") or getattr(spec, "pair", ""))
        provider_id = str(getattr(config, "provider_id", "") or "")
        broker_symbol = str(getattr(self.platform, "broker_symbol", "") or "")
        if not provider_id or not broker_symbol:
            raise RuntimeError("platform_identity_missing")

        if ticket:
            # A close names the exact broker position the ledger booked for it.
            if quote is None:
                raise RuntimeError("close_without_quote")
            as_quote = self._as_quote(quote, pair)
            at = as_quote.timestamp_utc()
            bar = self._bar(at=at, bid=as_quote.bid, ask=as_quote.ask, pair=pair)
            position = self._position(ticket)
            if position is None:
                raise RuntimeError("close_target_not_in_ledger")
            request = OrderRequest(
                request_id=f"scalper-close:{ticket}",
                provider_id=provider_id,
                provider_symbol=broker_symbol,
                side=position["side"],
                quantity=position["quantity"],
                target_mode="PAPER",
                evaluated_at=at,
                quote=as_quote,
                strategy_id=self.strategy_id,
                close_intent=CloseIntent(
                    position_id=str(position["position_id"]),
                    account_id=str(position["account_id"]),
                    quantity=position["quantity"],
                    pair=pair,
                    quantity_unit=str(position["quantity_unit"]),
                ),
            )
            return _Plan(request=request, context=self._context(at, bar), action="CLOSE",
                         position_id=str(position["position_id"]), quantity=position["quantity"])

        if order is None or quote is None:
            raise RuntimeError("entry_without_order_or_quote")
        as_quote = self._as_quote(quote, pair)
        at = as_quote.timestamp_utc()
        bar = self._bar(at=at, bid=as_quote.bid, ask=as_quote.ask, pair=pair)
        request = OrderRequest(
            request_id=order.request_id,
            provider_id=provider_id,
            provider_symbol=broker_symbol,
            side=OrderSide(str(order.side).upper()),
            quantity=order.lots,
            target_mode="PAPER",
            evaluated_at=at,
            quote=as_quote,
            strategy_id=self.strategy_id,
            close_intent=None,
        )
        return _Plan(request=request, context=self._context(at, bar), action="OPEN",
                     position_id="", quantity=order.lots)

    def _now(self) -> Any:
        from datetime import datetime, timezone

        return self._clock() if callable(self._clock) else datetime.now(timezone.utc)

    def _position(self, ticket: str) -> dict[str, Any] | None:
        portfolio = getattr(self.platform, "portfolio", None)
        positions = getattr(portfolio, "positions", {}) or {}
        target = f"mt5:{ticket}"
        position = positions.get(target)
        if position is None:
            return None
        spec = getattr(self.platform, "spec", None)
        return {
            "position_id": target,
            "account_id": str(getattr(position, "account_id", "")),
            "side": str(getattr(position, "side", "")).upper(),
            "quantity": getattr(position, "remaining_quantity", None) or getattr(position, "quantity", None),
            "quantity_unit": str(getattr(spec, "quantity_unit_value", "") or "LOTS"),
        }


class _Plan:
    """The two things the live leg reads, and nothing else."""

    __slots__ = ("request", "context", "action", "position_id", "quantity")

    def __init__(self, *, request: Any, context: Any, action: str, position_id: str, quantity: Any) -> None:
        self.request = request
        self.context = context
        self.action = action
        self.position_id = position_id
        self.quantity = quantity
