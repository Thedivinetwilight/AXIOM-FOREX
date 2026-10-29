"""Broker history, reconciliation, and restart recovery.

Two questions, both answered from broker evidence and never from assumption:

1. *What does the broker hold?* -- open positions and deal history, mapped onto
   AXIOM's ledger so a mismatch is visible instead of being averaged away.
2. *What happened to an order whose outcome we never learned?* -- an
   ``UNRESOLVED`` record is reconciled against order and deal history.  It is
   marked resolved only when the evidence covers the window in which the order
   was sent; otherwise it stays unresolved and keeps blocking new orders.

No function here submits, modifies, or cancels anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .orders import POSITION_TYPE_BUY, MT5Position
from .terminal import MT5Error, MT5Terminal, _decimal, _field, _sequence, _utc

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

ORDER_STATE_PLACED = 1
ORDER_STATE_CANCELED = 2
ORDER_STATE_PARTIAL = 3
ORDER_STATE_FILLED = 4
ORDER_STATE_REJECTED = 5
ORDER_STATE_EXPIRED = 6

#: Durable states that mean "nothing further can happen to this request".
MT5_FINAL_STATES = frozenset({"FILLED", "REJECTED", "CANCELLED", "RESOLVED_NOT_PLACED"})
#: Durable states in which another order must not be sent.
MT5_BLOCKING_STATES = frozenset({"SUBMITTING", "ACCEPTED", "PARTIAL", "UNRESOLVED"})

#: How far before the submit instant the recovery window opens.  Broker clocks
#: and our own can differ by seconds; five minutes is generous and cheap.
RECOVERY_WINDOW_PAD = timedelta(minutes=5)


class MT5ReconciliationError(MT5Error):
    """History or positions could not be read; nothing may be concluded."""


@dataclass(frozen=True)
class MT5Deal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    side: str
    entry: int
    volume: Decimal
    price: Decimal
    profit: Decimal | None
    swap: Decimal | None
    commission: Decimal | None
    magic: int
    comment: str
    time: datetime | None

    @property
    def entry_name(self) -> str:
        return {
            DEAL_ENTRY_IN: "IN",
            DEAL_ENTRY_OUT: "OUT",
            DEAL_ENTRY_INOUT: "INOUT",
            DEAL_ENTRY_OUT_BY: "OUT_BY",
        }.get(self.entry, f"UNKNOWN_{self.entry}")

    def audit_view(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "order": self.order,
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry": self.entry_name,
            "volume": self.volume,
            "price": self.price,
            "profit": self.profit,
            "swap": self.swap,
            "commission": self.commission,
            "magic": self.magic,
            "comment": self.comment,
            "time": self.time,
            "source": "mt5_history_deals_get",
        }


@dataclass(frozen=True)
class MT5HistoryOrder:
    ticket: int
    symbol: str
    side: str
    volume_initial: Decimal
    volume_current: Decimal
    price_open: Decimal | None
    state: int
    magic: int
    comment: str
    time_setup: datetime | None
    time_done: datetime | None

    @property
    def state_name(self) -> str:
        return {
            ORDER_STATE_PLACED: "PLACED",
            ORDER_STATE_CANCELED: "CANCELED",
            ORDER_STATE_PARTIAL: "PARTIAL",
            ORDER_STATE_FILLED: "FILLED",
            ORDER_STATE_REJECTED: "REJECTED",
            ORDER_STATE_EXPIRED: "EXPIRED",
        }.get(self.state, f"UNKNOWN_{self.state}")

    def audit_view(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "side": self.side,
            "volume_initial": self.volume_initial,
            "volume_current": self.volume_current,
            "price_open": self.price_open,
            "state": self.state_name,
            "magic": self.magic,
            "comment": self.comment,
            "time_setup": self.time_setup,
            "time_done": self.time_done,
            "source": "mt5_history_orders_get",
        }


def _time_window(record: Mapping[str, Any]) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    raw = record.get("sent_at") or record.get("updated_at")
    start = None
    if isinstance(raw, datetime):
        start = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    elif isinstance(raw, str):
        try:
            start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except ValueError:
            start = None
    if start is None:
        start = end - timedelta(days=1)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start > end:
        start = end
    return start - RECOVERY_WINDOW_PAD, end


def history_deals(
    terminal: MT5Terminal, *, start: datetime, end: datetime
) -> list[MT5Deal]:
    try:
        raw = terminal.module.history_deals_get(start, end)
    except Exception as exc:
        raise MT5ReconciliationError(
            "mt5_deal_history_failed", type(exc).__name__
        ) from exc
    if raw is None:
        code, text = terminal._last_error()
        raise MT5ReconciliationError("mt5_deal_history_unavailable", f"{code}:{text}")
    deals: list[MT5Deal] = []
    for entry in _sequence(raw):
        volume = _decimal(_field(entry, "volume"))
        price = _decimal(_field(entry, "price"))
        if volume is None or price is None:
            continue
        deals.append(
            MT5Deal(
                ticket=int(_field(entry, "ticket", 0) or 0),
                order=int(_field(entry, "order", 0) or 0),
                position_id=int(_field(entry, "position_id", 0) or 0),
                symbol=str(_field(entry, "symbol", "") or ""),
                side="BUY" if int(_field(entry, "type", POSITION_TYPE_BUY)) == POSITION_TYPE_BUY else "SELL",
                entry=int(_field(entry, "entry", DEAL_ENTRY_IN) or 0),
                volume=volume,
                price=price,
                profit=_decimal(_field(entry, "profit")),
                swap=_decimal(_field(entry, "swap")),
                commission=_decimal(_field(entry, "commission")),
                magic=int(_field(entry, "magic", 0) or 0),
                comment=str(_field(entry, "comment", "") or ""),
                time=_utc(_field(entry, "time")),
            )
        )
    return sorted(deals, key=lambda item: (item.time or datetime.min.replace(tzinfo=timezone.utc), item.ticket))


def history_orders(
    terminal: MT5Terminal, *, start: datetime, end: datetime
) -> list[MT5HistoryOrder]:
    try:
        raw = terminal.module.history_orders_get(start, end)
    except Exception as exc:
        raise MT5ReconciliationError(
            "mt5_order_history_failed", type(exc).__name__
        ) from exc
    if raw is None:
        code, text = terminal._last_error()
        raise MT5ReconciliationError(
            "mt5_order_history_unavailable", f"{code}:{text}"
        )
    orders: list[MT5HistoryOrder] = []
    for entry in _sequence(raw):
        initial = _decimal(_field(entry, "volume_initial"))
        current = _decimal(_field(entry, "volume_current"))
        if initial is None:
            continue
        orders.append(
            MT5HistoryOrder(
                ticket=int(_field(entry, "ticket", 0) or 0),
                symbol=str(_field(entry, "symbol", "") or ""),
                side="BUY"
                if int(_field(entry, "type", POSITION_TYPE_BUY)) == POSITION_TYPE_BUY
                else "SELL",
                volume_initial=initial,
                volume_current=current if current is not None else initial,
                price_open=_decimal(_field(entry, "price_open")),
                state=int(_field(entry, "state", 0) or 0),
                magic=int(_field(entry, "magic", 0) or 0),
                comment=str(_field(entry, "comment", "") or ""),
                time_setup=_utc(_field(entry, "time_setup")),
                time_done=_utc(_field(entry, "time_done")),
            )
        )
    return sorted(orders, key=lambda item: (item.time_setup or datetime.min.replace(tzinfo=timezone.utc), item.ticket))


# -- reconciliation ---------------------------------------------------------


@dataclass(frozen=True)
class PositionReconciliation:
    matched: tuple[dict[str, Any], ...]
    ledger_only: tuple[dict[str, Any], ...]
    broker_only: tuple[dict[str, Any], ...]
    volume_mismatch: tuple[dict[str, Any], ...]
    side_mismatch: tuple[dict[str, Any], ...]
    broker_positions: int
    ledger_positions: int

    @property
    def reconciled(self) -> bool:
        return not (
            self.ledger_only
            or self.broker_only
            or self.volume_mismatch
            or self.side_mismatch
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "reconciled": self.reconciled,
            "broker_positions": self.broker_positions,
            "ledger_positions": self.ledger_positions,
            "matched": list(self.matched),
            "ledger_only": list(self.ledger_only),
            "broker_only": list(self.broker_only),
            "volume_mismatch": list(self.volume_mismatch),
            "side_mismatch": list(self.side_mismatch),
            "source": "mt5_position_reconciliation",
        }


def reconcile_positions(
    broker_positions: Sequence[MT5Position],
    ledger_positions: Iterable[Mapping[str, Any]],
    *,
    pair_for_symbol: Mapping[str, str] | None = None,
) -> PositionReconciliation:
    """Compare broker positions with the ledger, by pair.

    The ledger is the authority for *intent*; the broker is the authority for
    *fact*.  Any difference is reported, never auto-corrected: correcting a
    ledger from a broker read is a settlement decision that must go through the
    existing atomic settlement path.
    """

    from .orders import parse_positions as _parse_positions

    mapping = {key.upper(): value for key, value in (pair_for_symbol or {}).items()}
    broker_positions = _parse_positions(list(broker_positions))

    def normalise(value: str) -> str:
        return "".join(character for character in str(value).upper() if character.isalnum())

    def key_for(symbol: str, pair: str) -> str:
        return normalise(mapping.get(symbol.upper()) or pair)

    broker_by_pair: dict[str, list[MT5Position]] = {}
    for position in broker_positions:
        broker_by_pair.setdefault(key_for(position.symbol, position.symbol), []).append(position)
    ledger_by_pair: dict[str, list[Mapping[str, Any]]] = {}
    for position in ledger_positions:
        pair = key_for(str(position.get("pair", "")), str(position.get("pair", "")))
        ledger_by_pair.setdefault(pair, []).append(position)

    matched: list[dict[str, Any]] = []
    ledger_only: list[dict[str, Any]] = []
    broker_only: list[dict[str, Any]] = []
    volume_mismatch: list[dict[str, Any]] = []
    side_mismatch: list[dict[str, Any]] = []

    for pair in sorted(set(broker_by_pair) | set(ledger_by_pair)):
        broker = broker_by_pair.get(pair, [])
        ledger = ledger_by_pair.get(pair, [])
        broker_volume = sum((item.volume for item in broker), Decimal(0))
        ledger_volume = sum(
            (Decimal(str(item.get("quantity", 0))) for item in ledger), Decimal(0)
        )
        if not ledger:
            for item in broker:
                broker_only.append({"pair": pair, **item.audit_view()})
            continue
        if not broker:
            for item in ledger:
                ledger_only.append(
                    {
                        "pair": pair,
                        "position_id": item.get("position_id"),
                        "quantity": item.get("quantity"),
                    }
                )
            continue
        if broker_volume != ledger_volume:
            volume_mismatch.append(
                {
                    "pair": pair,
                    "broker_volume": broker_volume,
                    "ledger_quantity": ledger_volume,
                    "broker_tickets": [item.ticket for item in broker],
                    "ledger_ids": [item.get("position_id") for item in ledger],
                }
            )
            continue
        broker_sides = {item.side for item in broker}
        ledger_sides = {str(item.get("side", "")).upper() for item in ledger}
        if len(broker_sides) == 1 and len(ledger_sides) == 1 and broker_sides != ledger_sides:
            side_mismatch.append(
                {
                    "pair": pair,
                    "broker_sides": sorted(broker_sides),
                    "ledger_sides": sorted(ledger_sides),
                }
            )
            continue
        matched.append(
            {
                "pair": pair,
                "volume": broker_volume,
                "broker_tickets": [item.ticket for item in broker],
                "ledger_ids": [item.get("position_id") for item in ledger],
            }
        )

    return PositionReconciliation(
        matched=tuple(matched),
        ledger_only=tuple(ledger_only),
        broker_only=tuple(broker_only),
        volume_mismatch=tuple(volume_mismatch),
        side_mismatch=tuple(side_mismatch),
        broker_positions=len(broker_positions),
        ledger_positions=sum(len(items) for items in ledger_by_pair.values()),
    )


# -- recovery ---------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryResult:
    request_id: str
    previous_state: str
    state: str
    evidence: str
    order_ticket: int
    deal_tickets: tuple[int, ...]
    volume_matched: Decimal
    resolved: bool
    resend_allowed: bool
    detail: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "previous_state": self.previous_state,
            "state": self.state,
            "evidence": self.evidence,
            "order_ticket": self.order_ticket,
            "deal_tickets": list(self.deal_tickets),
            "volume_matched": self.volume_matched,
            "resolved": self.resolved,
            # Recovery never authorises a resend: a new order needs a new
            # gate-approved request.
            "resend_allowed": False,
            "detail": self.detail,
            "source": "mt5_restart_recovery",
        }


def reconcile_record(
    terminal: MT5Terminal,
    record: Mapping[str, Any],
) -> RecoveryResult:
    """Determine what happened to one unresolved order.  Sends nothing.

    Evidence order: the order ticket (if one was returned before the failure),
    then deal history for the send window.  Only when *both* history queries
    succeed and cover the window, and neither finds a matching order or deal,
    and no matching open position exists, is the request concluded
    ``RESOLVED_NOT_PLACED``.
    """

    previous = str(record.get("state", "UNRESOLVED")).upper()
    request_id = str(record.get("request_id", ""))
    magic = int(record.get("magic") or 0)
    comment = str(record.get("comment") or request_id)[:31]
    symbol = str(record.get("provider_symbol") or record.get("symbol") or "")
    side = str(record.get("side") or "")
    ticket = int(record.get("order_ticket") or 0)

    start, end = _time_window(record)
    try:
        orders = history_orders(terminal, start=start, end=end)
        deals = history_deals(terminal, start=start, end=end)
        positions = terminal_positions(terminal)
        history_ok = True
    except MT5ReconciliationError as exc:
        return RecoveryResult(
            request_id=request_id,
            previous_state=previous,
            state="UNRESOLVED",
            evidence="history_unavailable",
            order_ticket=ticket,
            deal_tickets=(),
            volume_matched=Decimal(0),
            resolved=False,
            resend_allowed=False,
            detail=exc.reason,
        )

    matching_orders = [
        item
        for item in orders
        if (ticket and item.ticket == ticket)
        or (
            (not ticket)
            and item.magic == magic
            and item.comment[: len(comment)] == comment
            and item.symbol.upper() == symbol.upper()
        )
    ]
    matching_deals = [
        item
        for item in deals
        if (ticket and item.order == ticket)
        or (
            item.magic == magic
            and item.comment[: len(comment)] == comment
            and item.symbol.upper() == symbol.upper()
            and item.side.upper() == side.upper()
        )
    ]
    filled = sum(
        (item.volume for item in matching_deals if item.entry in (DEAL_ENTRY_IN, DEAL_ENTRY_INOUT)),
        Decimal(0),
    )
    requested = Decimal(str(record.get("volume") or record.get("volume_requested") or 0))
    deal_tickets = tuple(item.ticket for item in matching_deals)

    if matching_orders:
        state = matching_orders[-1].state_name
        if state == "FILLED":
            return RecoveryResult(
                request_id=request_id,
                previous_state=previous,
                state="FILLED",
                evidence="order_history",
                order_ticket=matching_orders[-1].ticket,
                deal_tickets=deal_tickets,
                volume_matched=filled,
                resolved=True,
                resend_allowed=False,
            )
        if state in {"CANCELED", "REJECTED", "EXPIRED"}:
            return RecoveryResult(
                request_id=request_id,
                previous_state=previous,
                state="REJECTED",
                evidence=f"order_history_{state.lower()}",
                order_ticket=matching_orders[-1].ticket,
                deal_tickets=deal_tickets,
                volume_matched=filled,
                resolved=True,
                resend_allowed=False,
            )
        if state == "PARTIAL":
            return RecoveryResult(
                request_id=request_id,
                previous_state=previous,
                state="PARTIAL",
                evidence="order_history_partial",
                order_ticket=matching_orders[-1].ticket,
                deal_tickets=deal_tickets,
                volume_matched=filled,
                resolved=False,
                resend_allowed=False,
            )
        # PLACED: still live at the broker.
        return RecoveryResult(
            request_id=request_id,
            previous_state=previous,
            state="ACCEPTED",
            evidence="order_history_placed",
            order_ticket=matching_orders[-1].ticket,
            deal_tickets=deal_tickets,
            volume_matched=filled,
            resolved=False,
            resend_allowed=False,
        )

    if matching_deals:
        state = "PARTIAL" if filled < requested else "FILLED"
        return RecoveryResult(
            request_id=request_id,
            previous_state=previous,
            state=state,
            evidence="deal_history",
            order_ticket=ticket,
            deal_tickets=deal_tickets,
            volume_matched=filled,
            resolved=state == "FILLED",
            resend_allowed=False,
        )

    matching_positions = [
        item
        for item in positions
        if item.magic == magic and item.symbol.upper() == symbol.upper()
    ]
    if matching_positions:
        # A position exists with our magic but no history row in the window:
        # the order did execute, but the volume cannot be proven.
        return RecoveryResult(
            request_id=request_id,
            previous_state=previous,
            state="ACCEPTED",
            evidence="open_position_magic_match",
            order_ticket=ticket,
            deal_tickets=(),
            volume_matched=sum((item.volume for item in matching_positions), Decimal(0)),
            resolved=False,
            resend_allowed=False,
        )

    if history_ok:
        return RecoveryResult(
            request_id=request_id,
            previous_state=previous,
            state="RESOLVED_NOT_PLACED",
            evidence="history_absence",
            order_ticket=ticket,
            deal_tickets=(),
            volume_matched=Decimal(0),
            resolved=True,
            resend_allowed=False,
            detail=f"window={start.isoformat()}..{end.isoformat()}",
        )
    return RecoveryResult(  # pragma: no cover - guarded above
        request_id=request_id,
        previous_state=previous,
        state="UNRESOLVED",
        evidence="history_unavailable",
        order_ticket=ticket,
        deal_tickets=(),
        volume_matched=Decimal(0),
        resolved=False,
        resend_allowed=False,
    )


def terminal_positions(terminal: MT5Terminal) -> list[MT5Position]:
    from .orders import open_positions

    return open_positions(terminal)
