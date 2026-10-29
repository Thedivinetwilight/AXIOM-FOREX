"""MetaTrader 5 return codes, and what each one means for safety.

The single most dangerous mistake in an MT5 integration is treating an
*unknown* outcome as a failure and resending.  The broker may already have the
order.  Every code is therefore classified into exactly one of:

``OK``          -- the request was accepted/completed
``REJECTED``    -- the broker definitively did not act; the order does not exist
``RETRYABLE``   -- the request was refused *in a way that guarantees no order*
                   (requote/price-off/invalid-price before execution)
``TIMEOUT``     -- no answer within the deadline; the outcome is **unknown**
``UNKNOWN``     -- a code this build does not recognise (for example a newer
                   terminal); the outcome is treated as unknown

Only ``RETRYABLE`` and ``OK`` may ever be followed by another submission, and
even ``RETRYABLE`` is bounded by an explicit retry policy that re-prices from a
fresh tick rather than replaying a stale price.  ``TIMEOUT`` and ``UNKNOWN``
block further orders until reconciliation proves what happened.
"""

from __future__ import annotations

from enum import Enum

# Values from the official MetaTrader 5 Python integration
# (``MetaTrader5.TRADE_RETCODE_*``).  They are declared here so the adapter can
# classify a code without importing the optional module.
DONE = 10009
PLACED = 10008
REQUOTE = 10004
REJECT = 10006
CANCEL = 10007
PARTIAL = 10010
ERROR = 10011
TIMEOUT = 10012
INVALID = 10013
INVALID_VOLUME = 10014
INVALID_PRICE = 10015
INVALID_STOPS = 10016
TRADE_DISABLED = 10017
MARKET_CLOSED = 10018
NO_MONEY = 10019
PRICE_CHANGED = 10020
PRICE_OFF = 10021
INVALID_EXPIRATION = 10022
ORDER_CHANGED = 10023
TOO_MANY_REQUESTS = 10024
NO_CHANGES = 10025
SERVER_DISABLES_AT = 10026
CLIENT_DISABLES_AT = 10027
LOCKED = 10028
FROZEN = 10029
INVALID_FILL = 10030
CONNECTION = 10031
ONLY_REAL = 10032
LIMIT_ORDERS = 10033
LIMIT_VOLUME = 10034
INVALID_ORDER = 10035
POSITION_CLOSED = 10036
INVALID_CLOSE_VOLUME = 10038
CLOSE_ORDER_EXIST = 10039
LIMIT_POSITIONS = 10040
REJECT_CANCEL = 10041
LONG_ONLY = 10042
SHORT_ONLY = 10043
CLOSE_ONLY = 10044
FIFO_CLOSE = 10045
HEDGE_PROHIBITED = 10046

OK_CODES = frozenset({DONE, PLACED, PARTIAL})
#: The broker definitively refused; no position was opened or closed.
REJECTED_CODES = frozenset(
    {
        REJECT,
        CANCEL,
        INVALID,
        INVALID_VOLUME,
        INVALID_STOPS,
        TRADE_DISABLED,
        MARKET_CLOSED,
        NO_MONEY,
        INVALID_EXPIRATION,
        TOO_MANY_REQUESTS,
        SERVER_DISABLES_AT,
        CLIENT_DISABLES_AT,
        LOCKED,
        FROZEN,
        INVALID_FILL,
        ONLY_REAL,
        LIMIT_ORDERS,
        LIMIT_VOLUME,
        INVALID_ORDER,
        POSITION_CLOSED,
        INVALID_CLOSE_VOLUME,
        CLOSE_ORDER_EXIST,
        LIMIT_POSITIONS,
        REJECT_CANCEL,
        LONG_ONLY,
        SHORT_ONLY,
        CLOSE_ONLY,
        FIFO_CLOSE,
        HEDGE_PROHIBITED,
        NO_CHANGES,
        ORDER_CHANGED,
    }
)
#: The broker refused the *price*, but guarantees nothing was executed.
RETRYABLE_CODES = frozenset({REQUOTE, PRICE_CHANGED, PRICE_OFF, INVALID_PRICE})
#: No answer at all.  The order may or may not exist.
UNKNOWN_CODES = frozenset({TIMEOUT, CONNECTION, ERROR})


class RetcodeClass(str, Enum):
    OK = "OK"
    REJECTED = "REJECTED"
    RETRYABLE = "RETRYABLE"
    UNKNOWN = "UNKNOWN"


#: Human labels, for logs and reports (never for control flow).
LABELS = {
    DONE: "DONE",
    PLACED: "PLACED",
    REQUOTE: "REQUOTE",
    REJECT: "REJECT",
    CANCEL: "CANCEL",
    PARTIAL: "PARTIAL",
    ERROR: "ERROR",
    TIMEOUT: "TIMEOUT",
    INVALID: "INVALID",
    INVALID_VOLUME: "INVALID_VOLUME",
    INVALID_PRICE: "INVALID_PRICE",
    INVALID_STOPS: "INVALID_STOPS",
    TRADE_DISABLED: "TRADE_DISABLED",
    MARKET_CLOSED: "MARKET_CLOSED",
    NO_MONEY: "NO_MONEY",
    PRICE_CHANGED: "PRICE_CHANGED",
    PRICE_OFF: "PRICE_OFF",
    INVALID_EXPIRATION: "INVALID_EXPIRATION",
    ORDER_CHANGED: "ORDER_CHANGED",
    TOO_MANY_REQUESTS: "TOO_MANY_REQUESTS",
    NO_CHANGES: "NO_CHANGES",
    SERVER_DISABLES_AT: "SERVER_DISABLES_AT",
    CLIENT_DISABLES_AT: "CLIENT_DISABLES_AT",
    LOCKED: "LOCKED",
    FROZEN: "FROZEN",
    INVALID_FILL: "INVALID_FILL",
    CONNECTION: "CONNECTION",
    ONLY_REAL: "ONLY_REAL",
    LIMIT_ORDERS: "LIMIT_ORDERS",
    LIMIT_VOLUME: "LIMIT_VOLUME",
    INVALID_ORDER: "INVALID_ORDER",
    POSITION_CLOSED: "POSITION_CLOSED",
    INVALID_CLOSE_VOLUME: "INVALID_CLOSE_VOLUME",
    CLOSE_ORDER_EXIST: "CLOSE_ORDER_EXIST",
    LIMIT_POSITIONS: "LIMIT_POSITIONS",
    REJECT_CANCEL: "REJECT_CANCEL",
    LONG_ONLY: "LONG_ONLY",
    SHORT_ONLY: "SHORT_ONLY",
    CLOSE_ONLY: "CLOSE_ONLY",
    FIFO_CLOSE: "FIFO_CLOSE",
    HEDGE_PROHIBITED: "HEDGE_PROHIBITED",
}


def label(retcode: object) -> str:
    try:
        code = int(retcode)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "NO_RETCODE"
    return LABELS.get(code, f"RETCODE_{code}")


def classify(retcode: object, *, has_result: bool = True) -> RetcodeClass:
    """Map a retcode to the safety class that governs what happens next.

    ``has_result=False`` means ``order_send`` returned ``None``: the terminal
    or the connection failed before the broker could answer, which is unknown,
    not a rejection.
    """

    if not has_result:
        return RetcodeClass.UNKNOWN
    try:
        code = int(retcode)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return RetcodeClass.UNKNOWN
    if code in OK_CODES:
        return RetcodeClass.OK
    if code in RETRYABLE_CODES:
        return RetcodeClass.RETRYABLE
    if code in UNKNOWN_CODES:
        return RetcodeClass.UNKNOWN
    if code in REJECTED_CODES:
        return RetcodeClass.REJECTED
    # A code this build does not know: assume the worst, never resend.
    return RetcodeClass.UNKNOWN


def is_definitive_rejection(retcode: object) -> bool:
    return classify(retcode) is RetcodeClass.REJECTED


def summary() -> dict[str, object]:
    """Machine-readable classification table, for docs and tests."""

    return {
        "ok": sorted(OK_CODES),
        "retryable": sorted(RETRYABLE_CODES),
        "rejected": sorted(REJECTED_CODES),
        "unknown": sorted(UNKNOWN_CODES),
        "labels": {code: name for code, name in sorted(LABELS.items())},
    }
