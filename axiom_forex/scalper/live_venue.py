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
from uuid import uuid4

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
CLOSE_QUOTE_REQUIRED = "venue_close_quote_required"
CLOSE_OUTCOME_UNCONFIRMED = "venue_close_outcome_unconfirmed"
STRATEGY_STOP_NOT_ALIGNED = "strategy_stop_not_aligned_with_broker_protection"


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
    protection_modifications: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)

    def audit_view(self) -> dict[str, Any]:
        return {
            "submissions": [dict(item) for item in self.submissions[-20:]],
            "closes": [dict(item) for item in self.closes[-20:]],
            "protection_modifications": [dict(item) for item in self.protection_modifications[-20:]],
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
        stop_requirement: Callable[[], Mapping[str, Any]] | None = None,
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
        #: Current guarded-adapter stop requirements. A strategy is refused
        #: before its plan reaches LiveLeg if its risk stop differs from the
        #: broker-side stop the adapter will actually attach.
        self._stop_requirement = stop_requirement
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
            "protection_modifications": len(self.record.protection_modifications),
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

    def position_details(self) -> list[dict[str, Any]]:
        """Read position identity and size without turning read failures into flat."""

        if self._position_reader is None:
            raise RuntimeError("venue_position_reader_unavailable")
        try:
            source_rows = list(self._position_reader() or ())
        except Exception as exc:
            raise RuntimeError("venue_position_read_failed") from exc
        rows: list[dict[str, Any]] = []
        for source in source_rows:
            if isinstance(source, Mapping):
                row = dict(source)
            else:
                audit = getattr(source, "audit_view", None)
                if not callable(audit):
                    raise RuntimeError("venue_position_row_unreadable")
                row = dict(audit())
            ticket = str(row.get("ticket") or "")
            if not ticket:
                raise RuntimeError("venue_position_ticket_missing")
            row["ticket"] = ticket
            row.setdefault("position_id", ticket)
            side = str(row.get("side") or "").upper()
            if side not in {"BUY", "SELL"}:
                raw_type = row.get("type")
                type_name = "" if isinstance(raw_type, bool) else str(raw_type or "").upper()
                if not isinstance(raw_type, bool) and (
                    raw_type == 0 or type_name in {"0", "BUY", "ORDER_TYPE_BUY", "POSITION_TYPE_BUY"}
                ):
                    side = "BUY"
                elif not isinstance(raw_type, bool) and (
                    raw_type == 1 or type_name in {"1", "SELL", "ORDER_TYPE_SELL", "POSITION_TYPE_SELL"}
                ):
                    side = "SELL"
                else:
                    side = ""
            row["side"] = side
            row["symbol"] = str(row.get("symbol") or "")
            row["lots"] = row.get("lots", row.get("volume"))
            row["entry_price"] = row.get("entry_price", row.get("price_open"))
            row["stop_price"] = row.get("stop_price", row.get("stop_loss", row.get("sl")))
            row["target_price"] = row.get(
                "target_price", row.get("take_profit", row.get("tp"))
            )
            rows.append(row)
        return rows

    def position_ids(self) -> list[str]:
        try:
            return [row["ticket"] for row in self.position_details()]
        except Exception:
            return []

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
        if self._stop_requirement is not None:
            try:
                requirement = dict(self._stop_requirement() or {})
                required_points = requirement.get("distance_points")
                point = _d(order.point)
                side = str(order.side).upper()
                reference = _d(quote.ask if side == "BUY" else quote.bid)
                stop = None if order.stop_price is None else _d(order.stop_price)
                if (not requirement.get("valid") or isinstance(required_points, bool)
                        or int(required_points) <= 0 or point <= 0 or stop is None):
                    raise ValueError("broker_stop_unavailable")
                distance = reference - stop if side == "BUY" else stop - reference
                strategy_points = distance / point
                if distance <= 0 or strategy_points != Decimal(int(strategy_points)):
                    raise ValueError("strategy_stop_not_on_point_grid")
                if int(strategy_points) != int(required_points):
                    raise ValueError(
                        f"strategy declares {int(strategy_points)} points; broker attaches {int(required_points)}"
                    )
            except Exception as exc:
                detail = str(exc) or type(exc).__name__
                return self._refuse(order, STRATEGY_STOP_NOT_ALIGNED, detail)
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
        """Read one exact durable outcome.  A matching position is not a fill receipt."""

        reader = getattr(self.leg, "last_outcome", None)
        raw: Mapping[str, Any] = {}
        repository = getattr(getattr(self.leg, "repository", None), "get_order", None)
        if callable(repository):
            try:
                raw = dict(repository(request_id) or {})
            except Exception:
                raw = {}
        if isinstance(reader, Mapping):
            candidate = dict(reader)
            if str(candidate.get("request_id") or "") == str(request_id):
                raw = {**raw, **candidate}
        if not raw:
            history = getattr(self.leg, "history", ())
            for candidate in reversed(tuple(history or ())):
                if isinstance(candidate, Mapping) and str(candidate.get("request_id") or "") == str(request_id):
                    raw = dict(candidate)
                    break
        if not raw or str(raw.get("request_id") or "") != str(request_id):
            return None

        leg_outcome = raw.get("leg_outcome")
        if isinstance(leg_outcome, Mapping):
            combined = {**raw, **dict(leg_outcome)}
        else:
            broker_outcome = raw.get("outcome")
            combined = {
                **(dict(broker_outcome) if isinstance(broker_outcome, Mapping) else {}),
                **raw,
            }
        booking = combined.get("booking")
        booking = dict(booking) if isinstance(booking, Mapping) else {}
        state = str(combined.get("state") or "").upper()
        submitted = bool(combined.get("submitted", True))
        unknown = bool(combined.get("unknown")) or state in {"SUBMITTING", "UNRESOLVED", ""}
        filled = _d(combined.get("volume_filled"))
        requested = _d(
            combined.get("volume_requested")
            or combined.get("volume")
            or combined.get("quantity")
        )
        price_value = (
            booking.get("price")
            or combined.get("price")
        )
        price = None if price_value is None else _d(price_value)
        booking_id = str(booking.get("position_id") or "")
        expected_id = str(combined.get("close_position_id") or "")
        action = str(booking.get("action") or "").upper()
        booked = bool(combined.get("booked")) or bool(booking.get("booked"))
        if expected_id:
            identity_matches = action == "CLOSE" and booking_id == expected_id
            position_id = expected_id
        elif action == "CLOSE":
            identity_matches = bool(booking_id)
            position_id = booking_id
        else:
            identity_matches = action == "OPEN" and bool(booking_id)
            position_id = booking_id

        if not submitted:
            self._unresolved.discard(request_id)
            return Fill(
                request_id=request_id,
                status=BLOCKED,
                filled_lots=Decimal(0),
                requested_lots=requested,
                price=None,
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=None,
                reason=str(combined.get("reason") or LEG_REFUSED),
                detail=str(combined.get("detail") or ""),
                simulated=False,
            )
        if state == "REJECTED" or "REJECT" in str(combined.get("classification") or "").upper():
            self._unresolved.discard(request_id)
            return Fill(
                request_id=request_id,
                status=REJECTED,
                filled_lots=Decimal(0),
                requested_lots=requested,
                price=None,
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=None,
                reason=str(combined.get("retcode_name") or combined.get("reason") or "rejected"),
                detail=state,
                simulated=False,
            )

        if (
            unknown
            or state not in {"FILLED", "PARTIAL"}
            or not booked
            or not identity_matches
            or not filled.is_finite()
            or filled <= 0
            or not requested.is_finite()
            or requested <= 0
            or filled > requested
            or price is None
            or not price.is_finite()
            or price <= 0
        ):
            self._unresolved.add(request_id)
            return Fill(
                request_id=request_id,
                status=UNKNOWN_OUTCOME,
                filled_lots=filled,
                requested_lots=requested,
                price=price,
                spread_points=Decimal(0),
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=None,
                reason="close_outcome_unconfirmed" if expected_id else "outcome_unconfirmed",
                detail=state,
                simulated=False,
            )

        self._unresolved.discard(request_id)
        ticket = position_id.split(":", 1)[1] if ":" in position_id else position_id
        return Fill(
            request_id=request_id,
            status=ACCEPTED,
            filled_lots=filled,
            requested_lots=requested,
            price=price,
            spread_points=Decimal(0),
            slippage_points=Decimal(0),
            latency_ms=0,
            ticket=ticket,
            reason=str(combined.get("retcode_name") or state),
            detail="durable broker fill and ledger booking confirmed",
            simulated=False,
        )

    def modify_protection(
        self,
        *,
        ticket: str,
        stop_price: Decimal | None,
        target_price: Decimal | None,
        request_id: str | None = None,
    ) -> bool:
        """Modify protection through the existing LiveLeg/router/adapter path.

        The position is resolved from a fresh broker read and its identity is
        passed through the guarded leg. The returned bool is only an execution
        hint; :class:`PositionBook` still requires its own broker read-back
        before it updates managed protection.
        """

        request_id = str(request_id or f"modify:{ticket}:{uuid4().hex}")
        position_ticket = str(ticket or "")
        if position_ticket.startswith("mt5:"):
            position_ticket = position_ticket.split(":", 1)[1]
        if self._unresolved:
            detail = "an earlier broker order is unresolved"
            self.record.refusals.append(
                {"request_id": request_id, "ticket": position_ticket,
                 "reason": UNRESOLVED_LATCHED, "detail": detail}
            )
            return False
        if self._position_reader is None:
            self.record.refusals.append(
                {"request_id": request_id, "ticket": position_ticket,
                 "reason": "venue_position_reader_unavailable"}
            )
            return False
        try:
            rows = self.position_details()
        except Exception as exc:
            self.record.refusals.append(
                {"request_id": request_id, "ticket": position_ticket,
                 "reason": "venue_position_read_failed", "detail": type(exc).__name__}
            )
            return False
        matches = [row for row in rows if str(row.get("ticket") or "") == position_ticket]
        if len(matches) != 1:
            self.record.refusals.append(
                {"request_id": request_id, "ticket": position_ticket,
                 "reason": "venue_protection_position_not_unique"}
            )
            return False
        position = matches[0]
        symbol = str(position.get("symbol") or "")
        if self._symbols and symbol.upper() not in {
            str(item).upper() for item in self._symbols.values()
        }:
            self.record.refusals.append(
                {"request_id": request_id, "ticket": position_ticket,
                 "symbol": symbol, "reason": "venue_protection_symbol_unknown"}
            )
            return False
        side = str(position.get("side") or "").upper()
        lots = _d(position.get("lots"))
        entry = _d(position.get("entry_price"))
        stop = _d(stop_price)
        target = None if target_price is None else _d(target_price)
        if (
            side not in {"BUY", "SELL"}
            or lots <= 0
            or entry <= 0
            or stop <= 0
            or (target is not None and target <= 0)
        ):
            self.record.refusals.append(
                {"request_id": request_id, "ticket": position_ticket,
                 "symbol": symbol, "reason": "venue_protection_request_invalid"}
            )
            return False
        modify = getattr(self.leg, "modify_protection", None)
        if not callable(modify):
            self.record.refusals.append(
                {
                    "request_id": request_id,
                    "ticket": position_ticket,
                    "symbol": symbol,
                    "reason": MODIFICATION_UNSUPPORTED,
                    "detail": "the configured LiveLeg has no guarded protection action",
                }
            )
            return False
        try:
            result = modify(
                request_id=request_id,
                ticket=position_ticket,
                symbol=symbol,
                stop_price=stop,
                target_price=target,
                expected_position=dict(position),
            )
        except Exception as exc:
            record = {
                "request_id": request_id,
                "ticket": position_ticket,
                "symbol": symbol,
                "state": "UNRESOLVED",
                "sent": False,
                "unknown": True,
                "verified": False,
                "reason": "venue_protection_route_raised",
                "detail": type(exc).__name__,
            }
            self.record.protection_modifications.append(record)
            return False
        if isinstance(result, Mapping):
            record = dict(result)
        else:
            record = {
                "request_id": request_id,
                "ticket": position_ticket,
                "symbol": symbol,
                "state": "ACCEPTED" if result is True else "REFUSED",
                "sent": bool(result),
                "unknown": False,
                "verified": False,
                "reason": "" if result else "venue_protection_refused",
            }
        self.record.protection_modifications.append(record)
        if not record.get("verified") and not record.get("sent"):
            self.record.refusals.append(
                {"request_id": request_id, "ticket": position_ticket,
                 "symbol": symbol, "reason": record.get("reason", "venue_protection_refused")}
            )
        return bool(record.get("verified"))

    def close_request_id(self, ticket: str) -> str:
        """Prepare a unique request identity that can be fsynced before sending."""

        return f"c{ticket}-{uuid4().hex}"

    def close(
        self,
        *,
        ticket: str,
        price: Decimal,
        spread_points: Decimal,
        reason: str,
        quote: Tick | None = None,
        quantity: Decimal | None = None,
        request_id: str | None = None,
    ) -> Fill:
        request_id = str(request_id or self.close_request_id(ticket))
        if self._unresolved:
            return Fill(
                request_id=request_id,
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
        if quote is None:
            fill = Fill(
                request_id=request_id,
                status=BLOCKED,
                filled_lots=Decimal(0),
                requested_lots=Decimal(0),
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=str(ticket),
                reason=CLOSE_QUOTE_REQUIRED,
                detail="a close needs the exact current quote used to build its gate context",
                simulated=False,
            )
            self.record.refusals.append({"request_id": fill.request_id, **fill.audit_view()})
            return fill
        close_quantity = None if quantity is None else _d(quantity)
        if close_quantity is not None and (
            not close_quantity.is_finite() or close_quantity <= 0
        ):
            fill = Fill(
                request_id=request_id,
                status=BLOCKED,
                filled_lots=Decimal(0),
                requested_lots=Decimal(0),
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=str(ticket),
                reason="invalid_close_quantity",
                simulated=False,
            )
            self.record.refusals.append({"request_id": fill.request_id, **fill.audit_view()})
            return fill
        try:
            plan = self._plan_factory(
                ticket=str(ticket), price=_d(price), reason=reason,
                quote=quote, quantity=close_quantity, request_id=request_id,
            )
        except Exception as exc:
            plan = None
            detail = type(exc).__name__
        else:
            detail = ""
        if plan is None:
            fill = Fill(
                request_id=request_id,
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

        plan_request_id = str(
            getattr(getattr(plan, "request", None), "request_id", "") or request_id
        )
        if plan_request_id != request_id:
            fill = Fill(
                request_id=request_id,
                status=BLOCKED,
                filled_lots=Decimal(0),
                requested_lots=close_quantity or Decimal(0),
                price=None,
                spread_points=spread_points,
                slippage_points=Decimal(0),
                latency_ms=0,
                ticket=str(ticket),
                reason="close_request_identity_mismatch",
                detail="the plan factory did not preserve the durably prepared request id",
                simulated=False,
            )
            self.record.refusals.append({"request_id": fill.request_id, **fill.audit_view()})
            return fill
        try:
            outcome = dict(self.leg.execute(plan) or {})
        except Exception as exc:
            self._unresolved.add(request_id)
            fill = Fill(
                request_id=request_id,
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
            self.record.closes.append({"request_id": fill.request_id, "reason": reason, **fill.audit_view()})
            return fill

        submitted = bool(outcome.get("submitted"))
        state = str(outcome.get("state") or "").upper()
        filled = _d(outcome.get("volume_filled"))
        requested = _d(outcome.get("volume_requested"))
        unknown = bool(outcome.get("unknown")) or state == "UNRESOLVED"
        booking = outcome.get("booking") if isinstance(outcome.get("booking"), Mapping) else {}
        actual_price = _d(booking.get("price") or outcome.get("price"))
        expected_position = str(getattr(plan, "position_id", "") or f"mt5:{ticket}")
        booking_matches = str(booking.get("position_id") or "") == expected_position
        booked = bool(outcome.get("booked")) and booking_matches
        if unknown or (submitted and filled > 0 and not booked):
            status = UNKNOWN_OUTCOME
            self._unresolved.add(request_id)
        elif not submitted:
            status = BLOCKED
        elif state == "REJECTED" or filled <= 0:
            status = REJECTED
        elif (
            not booked
            or requested <= 0
            or filled > requested
            or actual_price <= 0
        ):
            status = UNKNOWN_OUTCOME
            self._unresolved.add(request_id)
        else:
            status = ACCEPTED

        if status == UNKNOWN_OUTCOME and request_id not in self._unresolved:
            self._unresolved.add(request_id)
        fill = Fill(
            request_id=request_id,
            status=status,
            filled_lots=filled,
            requested_lots=requested,
            price=actual_price if actual_price > 0 else None,
            spread_points=spread_points,
            slippage_points=Decimal(0),
            latency_ms=0,
            ticket=str(ticket),
            reason=str(
                outcome.get("reason")
                or (
                    CLOSE_OUTCOME_UNCONFIRMED
                    if status == UNKNOWN_OUTCOME
                    else outcome.get("retcode_name") or state
                )
                or ""
            ),
            detail=", ".join(str(item) for item in (outcome.get("reasons") or ()))
            or str(outcome.get("detail") or ""),
            simulated=False,
        )
        self.record.closes.append({"request_id": fill.request_id, "reason": reason, **fill.audit_view()})
        return fill


__all__ = [
    "BrokerVenue",
    "VenueRecord",
    "MODIFICATION_UNSUPPORTED",
    "NOT_AN_ENTRY",
    "CLOSE_QUOTE_REQUIRED",
    "CLOSE_OUTCOME_UNCONFIRMED",
    "STRATEGY_STOP_NOT_ALIGNED",
]


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

    def __call__(
        self,
        *,
        order: Any = None,
        quote: Any = None,
        ticket: str = "",
        price: Any = None,
        reason: str = "",
        quantity: Any = None,
        request_id: str = "",
    ) -> Any:
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
            position_side = str(position["side"]).upper()
            if position_side not in {"BUY", "SELL"}:
                raise RuntimeError("close_position_side_invalid")
            try:
                available = Decimal(str(position["quantity"]))
                close_quantity = available if quantity is None else Decimal(str(quantity))
            except Exception as exc:
                raise RuntimeError("close_quantity_invalid") from exc
            if (
                not available.is_finite()
                or available <= 0
                or not close_quantity.is_finite()
                or close_quantity <= 0
                or close_quantity > available
            ):
                raise RuntimeError("close_quantity_outside_position")
            close_side = "SELL" if position_side == "BUY" else "BUY"
            request = OrderRequest(
                request_id=str(request_id or f"c{ticket}-{uuid4().hex}"),
                provider_id=provider_id,
                provider_symbol=broker_symbol,
                side=OrderSide(close_side),
                quantity=close_quantity,
                target_mode="PAPER",
                evaluated_at=at,
                quote=as_quote,
                strategy_id=self.strategy_id,
                close_intent=CloseIntent(
                    position_id=str(position["position_id"]),
                    account_id=str(position["account_id"]),
                    quantity=close_quantity,
                    pair=pair,
                    quantity_unit=str(position["quantity_unit"]),
                ),
            )
            return _Plan(
                request=request,
                context=self._context(at, bar),
                action="CLOSE",
                position_id=str(position["position_id"]),
                quantity=close_quantity,
            )

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
        raw_side = getattr(position, "side", "")
        side = str(getattr(raw_side, "value", raw_side) or "").upper()
        remaining = getattr(position, "remaining_quantity", None)
        if remaining is None:
            remaining = getattr(position, "quantity", None)
        return {
            "position_id": target,
            "account_id": str(getattr(position, "account_id", "")),
            "side": side,
            "quantity": remaining,
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
