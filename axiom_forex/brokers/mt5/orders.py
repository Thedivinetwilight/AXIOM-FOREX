"""Order construction, pre-check, submission, and outcome classification.

The rules encoded here are the ones that keep a live integration honest:

* an order always carries the gated volume, side, price, deviation, and magic;
* ``order_check`` runs first, and a failed check means *no submission*;
* a partially filled or unanswered request is reported as exactly that, never
  as success and never as a clean failure;
* an outcome whose classification is ``UNKNOWN`` sets ``permits_resend=False``
  and must be reconciled from broker history before anything else is sent;
* opposite-side handling differs between netting and hedging accounts, and a
  mismatch is refused instead of silently flipping a position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from .retcodes import RetcodeClass, classify, is_definitive_rejection, label
from .terminal import (
    MT5Error,
    MT5SymbolInfo,
    MT5Terminal,
    _decimal,
    _field,
    _sequence,
    _utc,
)

# Order/action constants from the official integration.
TRADE_ACTION_DEAL = 1
TRADE_ACTION_REMOVE = 8
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
SYMBOL_FILLING_BOC = 4
POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1


class MT5OrderRefused(MT5Error):
    """The order was not submitted; the reason is definitive."""


class MT5OrderOutcomeUnknown(MT5Error):
    """The terminal gave no usable answer.  The order may exist."""


@dataclass(frozen=True)
class MT5Position:
    ticket: int
    symbol: str
    side: str
    volume: Decimal
    price_open: Decimal
    price_current: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    swap: Decimal | None
    profit: Decimal | None
    magic: int
    comment: str
    identifier: int
    time: datetime | None
    source: str = "mt5_positions_get"

    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"

    def audit_view(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "symbol": self.symbol,
            "side": self.side,
            "volume": self.volume,
            "price_open": self.price_open,
            "price_current": self.price_current,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "swap": self.swap,
            "profit": self.profit,
            "magic": self.magic,
            "comment": self.comment,
            "identifier": self.identifier,
            "time": self.time,
            "source": self.source,
        }


def parse_positions(raw: Any, symbols: Sequence[str] | None = None) -> list[MT5Position]:
    wanted = {symbol.upper() for symbol in symbols} if symbols else None
    positions: list[MT5Position] = []
    for entry in _sequence(raw):
        symbol = str(_field(entry, "symbol", "") or "")
        if wanted is not None and symbol.upper() not in wanted:
            continue
        type_value = _field(entry, "type", POSITION_TYPE_BUY)
        volume = _decimal(_field(entry, "volume"))
        price_open = _decimal(_field(entry, "price_open"))
        if volume is None or price_open is None or not symbol:
            # An unreadable position cannot be reconciled; surfacing it as a
            # gap is better than dropping it silently.
            continue
        positions.append(
            MT5Position(
                ticket=int(_field(entry, "ticket", 0) or 0),
                symbol=symbol,
                side="BUY" if int(type_value) == POSITION_TYPE_BUY else "SELL",
                volume=volume,
                price_open=price_open,
                price_current=_decimal(_field(entry, "price_current"), Decimal(0))
                or Decimal(0),
                stop_loss=_decimal(_field(entry, "sl")),
                take_profit=_decimal(_field(entry, "tp")),
                swap=_decimal(_field(entry, "swap")),
                profit=_decimal(_field(entry, "profit")),
                magic=int(_field(entry, "magic", 0) or 0),
                comment=str(_field(entry, "comment", "") or ""),
                identifier=int(_field(entry, "identifier", 0) or 0),
                time=_utc(_field(entry, "time")),
            )
        )
    return sorted(positions, key=lambda item: item.ticket)


def open_positions(
    terminal: MT5Terminal, *, symbol: str | None = None
) -> list[MT5Position]:
    try:
        raw = terminal.module.positions_get(symbol=symbol) if symbol else terminal.module.positions_get()
    except Exception as exc:
        raise MT5Error("mt5_positions_unavailable", type(exc).__name__) from exc
    if raw is None:
        code, text = terminal._last_error()
        raise MT5Error("mt5_positions_unavailable", f"{code}:{text}")
    return parse_positions(raw)


def filling_for(info: MT5SymbolInfo) -> tuple[int, str]:
    """Pick a filling mode the symbol allows, or refuse.

    The value comes from the broker's ``filling_mode`` mask.  Guessing (for
    example always sending IOC) would produce ``INVALID_FILL`` rejections on
    some brokers and silent behaviour changes on others.
    """

    mask = int(info.filling_mask)
    if mask & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK, "FOK"
    if mask & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC, "IOC"
    if mask & SYMBOL_FILLING_BOC:
        return ORDER_FILLING_RETURN, "RETURN"
    raise MT5OrderRefused("mt5_filling_mode_unknown", f"mask={mask}")


@dataclass(frozen=True)
class MT5OrderCheck:
    allowed: bool
    retcode: int
    retcode_name: str
    comment: str
    margin: Decimal | None

    def audit_view(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "retcode": self.retcode,
            "retcode_name": self.retcode_name,
            "comment": self.comment,
            "margin": self.margin,
        }


@dataclass(frozen=True)
class MT5OrderOutcome:
    """Exactly what the terminal answered, and what may happen next."""

    request_id: str
    retcode: int
    retcode_name: str
    classification: str
    order_ticket: int
    deal_ticket: int
    volume_requested: Decimal
    volume_filled: Decimal
    price: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    comment: str
    retcode_external: int
    unknown: bool
    permits_resend: bool
    partially_filled: bool
    sent_at: datetime | None = None
    request: dict[str, Any] | None = None

    @property
    def accepted(self) -> bool:
        return self.classification == RetcodeClass.OK.value and not self.unknown

    @property
    def state(self) -> str:
        """Durable order state written to the repository."""

        if self.classification == RetcodeClass.OK.value:
            if self.partially_filled:
                return "PARTIAL"
            return "FILLED"
        if self.classification == RetcodeClass.REJECTED.value:
            return "REJECTED"
        if self.classification == RetcodeClass.RETRYABLE.value:
            return "REJECTED"
        return "UNRESOLVED"

    def audit_view(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "retcode": self.retcode,
            "retcode_name": self.retcode_name,
            "classification": self.classification,
            "order_ticket": self.order_ticket,
            "deal_ticket": self.deal_ticket,
            "volume_requested": self.volume_requested,
            "volume_filled": self.volume_filled,
            "price": self.price,
            "bid": self.bid,
            "ask": self.ask,
            "comment": self.comment,
            "retcode_external": self.retcode_external,
            "unknown": self.unknown,
            "permits_resend": self.permits_resend,
            "partially_filled": self.partially_filled,
            "state": self.state,
            "request": dict(self.request or {}),
        }


def _order_type(side: str) -> int:
    return ORDER_TYPE_BUY if str(side).upper() == "BUY" else ORDER_TYPE_SELL


def build_market_request(
    *,
    symbol: str,
    side: str,
    volume: Decimal,
    price: Decimal | None,
    deviation: int,
    magic: int,
    comment: str,
    filling: int,
    position_ticket: int | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "action": TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": _order_type(side),
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": comment[:31],
        "type_time": ORDER_TIME_GTC,
        "type_filling": int(filling),
    }
    if price is not None:
        request["price"] = float(price)
    if position_ticket is not None:
        request["position"] = int(position_ticket)
    return request


def check_order(terminal: MT5Terminal, request: dict[str, Any]) -> MT5OrderCheck:
    try:
        result = terminal.module.order_check(request)
    except Exception as exc:
        raise MT5OrderRefused(
            "mt5_order_check_failed", type(exc).__name__
        ) from exc
    if result is None:
        code, text = terminal._last_error()
        return MT5OrderCheck(
            allowed=False,
            retcode=int(code),
            retcode_name=label(code),
            comment=text or "order_check_returned_none",
            margin=None,
        )
    retcode = int(_field(result, "retcode", 0) or 0)
    return MT5OrderCheck(
        allowed=retcode == 0 or classify(retcode) is RetcodeClass.OK,
        retcode=retcode,
        retcode_name=label(retcode),
        comment=str(_field(result, "comment", "") or ""),
        margin=_decimal(_field(result, "margin")),
    )


def send_order(
    terminal: MT5Terminal,
    request: dict[str, Any],
    *,
    request_id: str,
    sent_at: datetime | None = None,
) -> MT5OrderOutcome:
    """Submit exactly once and classify the answer.  No retry happens here."""

    if not terminal.connected:
        raise MT5OrderRefused("mt5_not_connected")
    try:
        result = terminal.module.order_send(request)
    except Exception as exc:
        # An exception from the module is *not* proof that the broker did not
        # act; it is an unknown outcome.
        return MT5OrderOutcome(
            request_id=request_id,
            retcode=0,
            retcode_name="NO_RETCODE",
            classification=RetcodeClass.UNKNOWN.value,
            order_ticket=0,
            deal_ticket=0,
            volume_requested=Decimal(str(request.get("volume", 0))),
            volume_filled=Decimal(0),
            price=None,
            bid=None,
            ask=None,
            comment=f"order_send_exception:{type(exc).__name__}",
            retcode_external=0,
            unknown=True,
            permits_resend=False,
            partially_filled=False,
            sent_at=sent_at,
            request=request,
        )
    if result is None:
        code, text = terminal._last_error()
        return MT5OrderOutcome(
            request_id=request_id,
            retcode=int(code),
            retcode_name=label(code),
            classification=RetcodeClass.UNKNOWN.value,
            order_ticket=0,
            deal_ticket=0,
            volume_requested=Decimal(str(request.get("volume", 0))),
            volume_filled=Decimal(0),
            price=None,
            bid=None,
            ask=None,
            comment=f"order_send_returned_none:{text}",
            retcode_external=0,
            unknown=True,
            permits_resend=False,
            partially_filled=False,
            sent_at=sent_at,
            request=request,
        )
    retcode = int(_field(result, "retcode", 0) or 0)
    classification = classify(retcode, has_result=True)
    requested = Decimal(str(request.get("volume", 0)))
    filled = _decimal(_field(result, "volume"), Decimal(0)) or Decimal(0)
    unknown = classification is RetcodeClass.UNKNOWN
    ok = classification is RetcodeClass.OK
    partial = ok and filled > 0 and filled < requested
    # A "done" answer that reports no volume at all is treated as unknown: the
    # deal exists but this build cannot prove the fill quantity.
    if ok and filled <= 0:
        unknown = True
        partial = False
    permits_resend = (not unknown) and (
        classification is RetcodeClass.RETRYABLE
        or (classification is RetcodeClass.REJECTED and is_definitive_rejection(retcode))
    )
    return MT5OrderOutcome(
        request_id=request_id,
        retcode=retcode,
        retcode_name=label(retcode),
        classification=(
            RetcodeClass.UNKNOWN.value if unknown else classification.value
        ),
        order_ticket=int(_field(result, "order", 0) or 0),
        deal_ticket=int(_field(result, "deal", 0) or 0),
        volume_requested=requested,
        volume_filled=filled,
        price=_decimal(_field(result, "price")),
        bid=_decimal(_field(result, "bid")),
        ask=_decimal(_field(result, "ask")),
        comment=str(_field(result, "comment", "") or ""),
        retcode_external=int(_field(result, "retcode_external", 0) or 0),
        unknown=unknown,
        permits_resend=permits_resend,
        partially_filled=partial,
        sent_at=sent_at,
        request=request,
    )


def cancel_pending_order(
    terminal: MT5Terminal, *, ticket: int, request_id: str
) -> MT5OrderOutcome:
    """Remove a pending order (never used for market deals)."""

    request = {
        "action": TRADE_ACTION_REMOVE,
        "order": int(ticket),
        "magic": 0,
    }
    return send_order(terminal, request, request_id=request_id)


# -- netting vs hedging ------------------------------------------------------


def netting_conflict(
    positions: Sequence[MT5Position],
    *,
    symbol: str,
    side: str,
    accounting: str,
    approved_close: bool,
) -> str | None:
    """Refuse an order whose effect depends on position accounting.

    On a NETTING account an opposite-side deal *reduces the existing position*
    instead of opening a new one.  Sending one as if it were an entry would
    silently close risk the gate believes is still open.  Hedging accounts allow
    both directions, so no conflict exists there.
    """

    if accounting != "NETTING":
        return None
    wanted = str(side).upper()
    for position in positions:
        if position.symbol.upper() != str(symbol).upper():
            continue
        opposite = position.is_buy != (wanted == "BUY")
        if opposite and not approved_close:
            return "mt5_netting_account_opposite_order_would_reduce_position"
        if (not opposite) and approved_close:
            # "Closing" with the same side as the position increases it.
            return "mt5_close_side_matches_open_position"
    return None


def close_volume_check(
    position: MT5Position, volume: Decimal
) -> str | None:
    if volume <= 0:
        return "non_positive_quantity"
    if volume > position.volume:
        return "close_volume_exceeds_position"
    return None


def split_partial(
    outcome: MT5OrderOutcome,
) -> tuple[Decimal, Decimal]:
    """``(filled, remaining)`` for a partially filled request."""

    if not outcome.partially_filled:
        return outcome.volume_filled, Decimal(0)
    return outcome.volume_filled, outcome.volume_requested - outcome.volume_filled
