"""One reading of the paper ledger: orders, fills, positions, costs and P&L.

The workstation used to show several numbers that could not be reconciled, because
they were counted in different places and with different meanings:

* "5 orders, 0 filled" came from counting entry events as *orders* and close
  events as *fills*, so an entry that opened a position was never a fill;
* "10 open positions" came from the state file while the four names beside it
  came from a truncated slice;
* "P&L" came from a second, in-memory ledger that nothing could trace back to a
  fill, and it silently treated two unmodelled costs as zero.

This module is the single place that answers those questions.  It reads the two
durable artefacts the paper test already writes -- ``catalog-paper-ledger.jsonl``
(append-only events) and ``catalog-paper-state.json`` (the current book) -- and it
derives every count, every position and every money figure from them, once.

Rules it keeps, deliberately:

* **An entry fill is a fill.**  ``orders`` and ``fills`` are the same number,
  derived once from the same events, because a locally simulated order fills where
  it is priced.  Entries and exits are reported separately as well, so nothing has
  to guess which is which.
* **Freshness is judged now, not then.**  A quote carries its own timestamp; its
  age is computed against the clock at the moment of reading, and a stale quote
  pauses trading decisions rather than being reported as a live price.
* **A mark uses the closing side.**  Longs are marked at the bid, shorts at the
  ask, because that is the side a close would actually hit.
* **Unknown costs are unknown.**  Commission and swap are reported as ``None``
  with a reason when the broker did not publish them; they are never zero, and a
  net figure that would need them is marked unavailable instead of being shown
  smaller than it is.
* **Currency is never assumed.**  A pair's P&L is in that pair's quote currency.
  Totals are only added when every trade shares one currency, and only compared
  with the configured paper capital when the currencies match.
* **Nothing here touches a broker.**  There is no order object, no adapter and no
  credential anywhere in this module.  It reads files and arithmetic.

It never raises for a bad file: an unreadable line, a position with no entry event
or an exit with no entry becomes a named integrity warning, and the metrics that
depend on it are marked unavailable.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .catalog_paper import (
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    EVENT_CLOSED,
    EVENT_FILL,
    EVENT_STALE,
    LEDGER_NAME,
    STATE_NAME,
)

#: The configured local simulated starting capital.  Git-ignored (``*.local.json``)
#: and written only by an explicit operator action.
CAPITAL_NAME = "paper-capital.local.json"

#: Currencies the setup offers first.  Any three-letter ISO-shaped code is
#: accepted; the list is a convenience, not a policy.
CURRENCY_CHOICES: tuple[str, ...] = (
    "USD",
    "EUR",
    "GBP",
    "JPY",
    "CHF",
    "AUD",
    "CAD",
    "NZD",
    "SGD",
    "INR",
)

#: How many ledger events the activity page may page through.  A bound on memory,
#: not on the ledger: the count of what was left out is reported alongside it.
EVENT_HISTORY = 2000

#: A ceiling on a typed amount: a typo should be refused, not accepted.
MAX_CAPITAL_AMOUNT = Decimal("1000000000000000")

_CURRENCY = re.compile(r"^[A-Z]{3}$")

#: Where every figure on the paper pages comes from, in the operator's words.
SOURCE_LABEL = "Durable paper ledger · locally simulated"

#: What each metric means, in one plain sentence.  The pages print these beside
#: the figures, so a number is never shown without its definition.
DEFINITIONS: dict[str, str] = {
    "starting_capital": (
        "The local simulated starting capital you configured for this paper "
        "session. It is not a broker balance: no account was read to obtain it."
    ),
    "cash": (
        "Starting capital plus the realised price P&L of the closed paper trades. "
        "It is before commission and swap, because this paper test does not "
        "simulate either."
    ),
    "open_value": (
        "The marked value of the open paper positions: |mark| x contract size x "
        "lots, summed over the positions in the same quote currency."
    ),
    "equity": (
        "Cash plus the unrealised price P&L of the open positions, with longs "
        "marked at the bid and shorts at the ask. Before commission and swap."
    ),
    "realized_gross": (
        "Sum of the closed trades' price P&L: (exit price - entry price) x "
        "direction x contract size x lots, in the pair's quote currency. It "
        "includes whatever the two fill prices carried and nothing for commission "
        "or swap."
    ),
    "realized_net": (
        "Realised price P&L minus commission and swap. Shown only when both are "
        "known; otherwise the figure is unavailable rather than optimistic."
    ),
    "unrealized_gross": (
        "Open positions marked at the closing side of the quote: (mark - entry "
        "price) x direction x contract size x lots. Needs a fresh quote."
    ),
    "unrealized_net": (
        "Unrealised price P&L minus commission and swap. Unavailable while either "
        "is unknown."
    ),
    "net_pnl": "Realised plus unrealised, after commission and swap.",
    "return_pct": (
        "(equity - starting capital) / starting capital x 100. Meaningless while "
        "the equity in it is unavailable."
    ),
    "orders": (
        "Simulated orders the ledger recorded: entry fills plus exit fills. In a "
        "paper test an order fills where it is priced in the same cycle, so this "
        "and the fill count are the same figure, not two different ones."
    ),
    "fills": (
        "Simulated fills written to the ledger. Every one of them is a local "
        "number on a local file; no order was sent anywhere."
    ),
    "entry_fills": "Fills that opened a position.",
    "exit_fills": "Fills that closed a position: the closed trades.",
    "open_positions": (
        "Positions the ledger shows as opened and not yet closed, paired event by "
        "event rather than counted from a truncated list."
    ),
    "closed_trades": "Close events in the ledger, each with both fill prices.",
    "costs": (
        "Commission and swap, taken from the broker's terms when they were "
        "reported. Unknown inputs stay unknown and mark the net figures "
        "unavailable."
    ),
}


# ---------------------------------------------------------------------------
# small numeric helpers
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    """A real, finite float, or ``None``.  ``nan`` is not a number here."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _now(clock: Callable[[], datetime] | None = None) -> datetime:
    return (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)


def money(value: float | None, currency: str = "") -> str:
    """A money figure with its currency, or an explicit em dash."""

    if value is None:
        return "—"
    text = f"{value:,.2f}"
    return f"{text} {currency}".strip()


def _figure(value: float | None, currency: str = "") -> str:
    """A money figure, or the word that says why there is not one.

    An em dash is a placeholder; "Unavailable" is a statement, and the page is
    required to make that statement whenever it cannot compute a figure.
    """

    return "Unavailable" if value is None else money(value, currency)


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}%"


def price(value: float | None) -> str:
    return "—" if value is None else f"{value:,.5f}"


def count(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def age_text(seconds: float | None) -> str:
    if seconds is None:
        return "age unknown"
    if seconds < 90:
        return f"{seconds:,.0f} s old"
    if seconds < 5400:
        return f"{seconds / 60:,.1f} min old"
    return f"{seconds / 3600:,.1f} h old"


# ---------------------------------------------------------------------------
# the configured paper capital
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperCapital:
    """The operator's declared local simulated starting capital."""

    configured: bool
    amount: str = ""
    currency: str = ""
    set_at: str = ""
    #: Why it is not configured, or what was wrong with the stored document.
    reason: str = ""
    #: True when a document exists but could not be used -- a problem worth a
    #: warning, unlike the ordinary "nothing configured yet".
    problem: bool = False
    source: str = "operator (paper-capital.local.json)"

    @property
    def value(self) -> float | None:
        return _finite(self.amount) if self.configured else None

    def audit_view(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "amount": self.amount,
            "currency": self.currency,
            "set_at": self.set_at,
            "reason": self.reason,
            "problem": self.problem,
            "source": self.source,
        }


def capital_path(root: str | Path) -> Path:
    return Path(root) / CAPITAL_NAME


def read_capital(root: str | Path) -> PaperCapital:
    """Read the configured paper capital.  Never invents an amount."""

    path = capital_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PaperCapital(
            configured=False,
            reason="no paper starting capital has been configured yet",
        )
    except (OSError, ValueError) as exc:
        return PaperCapital(
            configured=False,
            problem=True,
            reason=f"the paper capital document could not be read ({type(exc).__name__})",
        )
    if not isinstance(payload, Mapping):
        return PaperCapital(
            configured=False, problem=True, reason="the paper capital document is not an object"
        )
    amount = _finite(payload.get("amount"))
    currency = str(payload.get("currency") or "").strip().upper()
    if amount is None or amount <= 0 or currency == "" or not _CURRENCY.match(currency):
        return PaperCapital(
            configured=False,
            problem=True,
            reason="the stored paper capital is not a positive amount with a currency",
        )
    return PaperCapital(
        configured=True,
        amount=f"{Decimal(str(payload.get('amount'))):.2f}",
        currency=currency,
        set_at=str(payload.get("set_at") or ""),
    )


def write_capital(
    root: str | Path,
    amount: Any,
    currency: Any,
    *,
    clock: Callable[[], datetime] | None = None,
) -> PaperCapital:
    """Store the operator's paper starting capital, or refuse with a reason.

    Validation is deliberately strict: a typed amount is a claim about the local
    simulation, so it has to be a positive, finite, plainly decimal number, and a
    three-letter currency.  Nothing is guessed and nothing is defaulted.
    """

    currency_text = str(currency or "").strip().upper()
    if not _CURRENCY.match(currency_text):
        raise ValueError("the currency must be a three-letter code, for example USD")
    raw = str(amount if amount is not None else "").strip().replace(",", "")
    if raw == "":
        raise ValueError("enter the amount you want this paper session to start with")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValueError("the amount must be a plain decimal number, for example 10000") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("the amount must be a finite number greater than zero")
    if parsed > MAX_CAPITAL_AMOUNT:
        raise ValueError("the amount is too large to be a paper starting capital")
    now = _now(clock)
    payload = {
        "version": 1,
        "amount": f"{parsed:.2f}",
        "currency": currency_text,
        "set_at": now.isoformat(),
        "note": (
            "Local simulated paper capital only. Not a broker balance, not read from "
            "any account, and never used to size or send an order."
        ),
    }
    target = capital_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)
    return PaperCapital(
        configured=True,
        amount=f"{parsed:.2f}",
        currency=currency_text,
        set_at=payload["set_at"],
    )


# ---------------------------------------------------------------------------
# durable inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteMark:
    """The last price seen for one symbol, with the instant it was quoted."""

    symbol: str
    bid: float | None
    ask: float | None
    quote_time: datetime | None
    checked_at: str = ""
    source: str = "paper ledger"

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    def age_seconds(self, now: datetime) -> float | None:
        if self.quote_time is None:
            return None
        return max(0.0, (now - self.quote_time).total_seconds())


def marks_from_snapshot(
    quotes: Iterable[Mapping[str, Any]], *, source: str = "paper session"
) -> dict[str, QuoteMark]:
    """Quote marks from a paper-test snapshot, keeping each quote's own time."""

    marks: dict[str, QuoteMark] = {}
    for row in quotes or ():
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        marks[symbol] = QuoteMark(
            symbol=symbol,
            bid=_finite(row.get("bid")),
            ask=_finite(row.get("ask")),
            quote_time=_parse_time(row.get("quote_time")),
            checked_at=str(row.get("checked_at") or ""),
            source=source,
        )
    return marks


def marks_from_events(events: Sequence[Mapping[str, Any]]) -> dict[str, QuoteMark]:
    """Quote marks recovered from ledger events, for when nothing is live.

    This is a *historical* mark and it is labelled as one: its timestamp is the
    quote the fill was priced from, so the age shown is the real age.
    """

    marks: dict[str, QuoteMark] = {}
    for event in events:
        symbol = str(event.get("symbol") or "")
        if not symbol:
            continue
        when = _parse_time(event.get("quote_time"))
        if when is None:
            continue
        previous = marks.get(symbol)
        if previous is not None and previous.quote_time is not None and previous.quote_time >= when:
            continue
        marks[symbol] = QuoteMark(
            symbol=symbol,
            bid=_finite(event.get("quote_bid")),
            ask=_finite(event.get("quote_ask")),
            quote_time=when,
            checked_at=str(event.get("at") or ""),
            source="paper ledger (last fill)",
        )
    return marks


def read_ledger(path: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Every readable event in the ledger, plus a problem for each bad line."""

    events: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        handle = Path(path).open("r", encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"the ledger could not be opened ({type(exc).__name__})"]
    with handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                problems.append(f"line {number} of the ledger is not valid JSON")
                continue
            if not isinstance(payload, dict):
                problems.append(f"line {number} of the ledger is not an event object")
                continue
            events.append(payload)
    return events, problems


def read_state(path: str | Path) -> tuple[dict[str, Any], str]:
    """The current book the paper test last wrote, or a reason it is absent."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, f"the paper state file could not be read ({type(exc).__name__})"
    if not isinstance(payload, Mapping):
        return {}, "the paper state file is not an object"
    return dict(payload), ""


# ---------------------------------------------------------------------------
# the account
# ---------------------------------------------------------------------------


@dataclass
class _Trade:
    symbol: str
    key: str
    direction: int
    lots: float
    entry_at: str
    exit_at: str
    entry_price: float | None
    exit_price: float | None
    reason: str
    basis: str
    gross: float | None
    currency: str
    contract_size: float
    modelled: tuple[str, ...]
    commission: float | None = None
    swap: float | None = None
    nights: int | None = None
    costs_complete: bool = False
    costs_missing: tuple[str, ...] = ()
    candidate_id: str = ""

    @property
    def net(self) -> float | None:
        if self.gross is None or self.commission is None or self.swap is None:
            return None
        return self.gross - self.commission - self.swap


def _is_priced_close(event: Mapping[str, Any]) -> bool:
    """A close event carries both fill prices; a note about a close does not."""

    return any(
        name in event for name in ("exit_price", "entry_price", "simulated_net_quote")
    )


def _pair_events(
    events: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    int,
]:
    """Split the ledger into entry fills, exit fills, and the problems pairing found.

    Pairing is per position key and strictly in file order, because that is the
    order the paper test wrote them in: an entry opens, a close has to close
    something that was open.

    An older build also wrote a summary record under the close-event kind, with no
    prices in it.  Those are counted and skipped: counting one as a trade is how a
    single close becomes two.
    """

    entries: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    problems: list[str] = []
    notes = 0
    open_by_key: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == EVENT_FILL:
            entries.append(event)
            open_by_key.setdefault(str(event.get("key") or ""), []).append(event)
        elif kind == EVENT_CLOSED:
            if not _is_priced_close(event):
                notes += 1
                continue
            key = str(event.get("key") or "")
            stack = open_by_key.get(key) or []
            if stack:
                stack.pop()
            else:
                problems.append(
                    f"a close event for {event.get('symbol') or key or 'an unnamed position'} "
                    "has no entry fill before it"
                )
            exits.append(event)
    return entries, exits, [item for stack in open_by_key.values() for item in stack], problems, notes


def _nights_between(entry_at: Any, exit_at: Any) -> int | None:
    """Working-day-free count of UTC date boundaries crossed.  A floor, not a rate."""

    start = _parse_time(entry_at)
    end = _parse_time(exit_at)
    if start is None or end is None:
        return None
    return max(0, (end.date() - start.date()).days)


def _trade_from_event(
    event: Mapping[str, Any],
    *,
    cost_model: Callable[[str], Any] | None,
) -> _Trade:
    symbol = str(event.get("symbol") or "")
    direction = 1 if str(event.get("direction") or "").upper() == "LONG" else -1
    lots = _finite(event.get("lots")) or 0.0
    gross = _finite(event.get("simulated_net_quote"))
    if gross is None:
        gross = _finite(event.get("simulated_gross_quote"))
    currency = str(event.get("quote_currency") or "").upper()
    contract_size = _finite(event.get("contract_size")) or 0.0
    model = None
    if cost_model is not None and symbol:
        try:
            model = cost_model(symbol)
        except Exception:  # a terms read never breaks the accounting
            model = None
    if model is not None and currency == "":
        currency = str(getattr(model, "quote_currency", "") or "").upper()
    if model is not None and contract_size <= 0:
        contract_size = _finite(getattr(model, "contract_size", None)) or 0.0

    trade = _Trade(
        symbol=symbol,
        key=str(event.get("key") or ""),
        candidate_id=str(event.get("candidate_id") or ""),
        direction=direction,
        lots=lots,
        entry_at=str(event.get("entry_at") or ""),
        exit_at=str(event.get("at") or ""),
        entry_price=_finite(event.get("entry_price")),
        exit_price=_finite(event.get("exit_price")),
        reason=str(event.get("reason") or ""),
        basis=str(event.get("exit_basis") or "BID_ASK"),
        gross=gross,
        currency=currency,
        contract_size=contract_size,
        modelled=tuple(str(item) for item in (event.get("costs_modelled") or ())),
    )

    missing: list[str] = []
    if model is None:
        missing.extend(
            [
                "commission (no symbol terms were readable)",
                "swap (no symbol terms were readable)",
            ]
        )
        trade.commission = None
        trade.swap = None
    else:
        labels = dict(getattr(model, "labels", lambda: {})() or {})
        # A commission the broker never published is an assumption, not a cost.
        if str(labels.get("commission", "")) != "OBSERVED":
            missing.append("commission not reported by the broker")
            trade.commission = None
        else:
            try:
                trade.commission = model.commission_quote(lots)
            except Exception:
                trade.commission = None
            if trade.commission is None:
                missing.append("commission could not be converted to the quote currency")
        nights = _nights_between(trade.entry_at, trade.exit_at)
        trade.nights = nights
        swap_state = str(getattr(model, "swap_state", "") or "")
        if nights is None:
            missing.append("swap (the holding period could not be read)")
            trade.swap = None
        elif nights == 0:
            # No rollover crossed: zero is the right answer and it is known.
            trade.swap = 0.0
        elif swap_state in ("POINTS", "CURRENCY", "DISABLED"):
            try:
                trade.swap = model.swap_quote(direction, lots, nights)
            except Exception:
                trade.swap = None
            if trade.swap is None:
                missing.append("swap could not be priced in the quote currency")
        else:
            missing.append(
                "swap (the broker did not report a priceable swap rate)"
                if swap_state in ("", "UNKNOWN", "UNAVAILABLE")
                else f"swap ({swap_state.lower()} is not priceable from the reported terms)"
            )
            trade.swap = None
    if gross is None:
        missing.append("the trade's price P&L (the contract size was not recorded)")
    if currency == "":
        missing.append("the pair's quote currency")
    trade.costs_missing = tuple(dict.fromkeys(missing))
    trade.costs_complete = not trade.costs_missing
    return trade


def _open_positions(
    *,
    open_events: Sequence[Mapping[str, Any]],
    state_positions: Sequence[Mapping[str, Any]],
    problems: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The open book, from the ledger, enriched from the state file where it can be.

    The ledger is authoritative for *which* positions are open: it is append-only
    and survives a crash between two state writes.  The state file carries the
    live management levels (stop, target, trailing stop), so its copy of a
    position is used for those fields alone.
    """

    by_key = {str(item.get("key") or ""): dict(item) for item in state_positions}
    positions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in open_events:
        key = str(event.get("key") or "")
        if key in seen:
            problems.append(
                f"{event.get('symbol') or key}: two open entry fills share the position key"
            )
            continue
        seen.add(key)
        state_copy = by_key.get(key)
        positions.append(
            {
                "key": key,
                "symbol": str(event.get("symbol") or ""),
                "candidate_id": str(event.get("candidate_id") or ""),
                "side": "LONG" if str(event.get("direction") or "").upper() == "LONG" else "SHORT",
                "direction": 1
                if str(event.get("direction") or "").upper() == "LONG"
                else -1,
                "lots": _finite(event.get("lots")) or 0.0,
                "entry_at": str(event.get("at") or ""),
                "entry_price": _finite(event.get("price")),
                "currency": str(event.get("quote_currency") or "").upper(),
                "contract_size": _finite(event.get("contract_size")) or 0.0,
                "timeframe": str(event.get("timeframe") or ""),
                "stop": None if state_copy is None else _finite(state_copy.get("stop")),
                "target": None if state_copy is None else _finite(state_copy.get("target")),
                "trail": None if state_copy is None else _finite(state_copy.get("trail")),
                # The bar the trade opened on: the trail is armed only after it, so
                # the page can say when this trail is allowed to act.
                "entry_bar_at": ""
                if state_copy is None
                else str(state_copy.get("entry_bar_at") or ""),
                "in_state": state_copy is not None,
            }
        )
    orphans: list[dict[str, Any]] = []
    for key, item in by_key.items():
        if not key or key in seen:
            continue
        problems.append(
            f"{item.get('symbol') or key}: the state file lists an open position the "
            "ledger has no entry fill for"
        )
        orphans.append(item)
    return positions, orphans


def _metric(
    key: str,
    label: str,
    *,
    raw: float | None,
    formatted: str,
    note: str = "",
    tone: str = "",
    available: bool | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "raw": raw,
        "value": formatted,
        "available": (raw is not None) if available is None else bool(available),
        "note": note,
        "tone": tone,
        "definition": DEFINITIONS.get(key, ""),
    }


def read_account(
    root: str | Path,
    *,
    now: datetime | None = None,
    marks: Mapping[str, QuoteMark] | None = None,
    cost_model: Callable[[str], Any] | None = None,
    ledger_events: Sequence[Mapping[str, Any]] | None = None,
    max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
) -> dict[str, Any]:
    """The whole paper picture, from the durable ledger, read once.

    ``marks`` are the newest quotes the caller has (they may be none at all, in
    which case the ledger's own last fill quotes are used and are labelled as the
    historical marks they are).  ``cost_model`` is an optional callable returning a
    cost model per symbol, used only for commission and swap; a symbol without one
    simply has those two costs reported as unknown.
    """

    now_dt = _now(None) if now is None else (
        now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    )
    now_dt = now_dt.astimezone(timezone.utc)
    root_path = Path(root)
    ledger_path = root_path / LEDGER_NAME
    state_path = root_path / STATE_NAME
    allowance = _finite(max_quote_age_seconds)
    if allowance is None or allowance <= 0:
        allowance = DEFAULT_MAX_QUOTE_AGE_SECONDS

    if ledger_events is None:
        events, problems = read_ledger(ledger_path)
    else:
        events = [dict(item) for item in ledger_events if isinstance(item, Mapping)]
        problems = []
    state, state_problem = read_state(state_path)
    if state_problem:
        problems.append(state_problem)
    # A paper test that could not resume its own book is not a warning to bury:
    # the positions it could not reconstruct are missing from every figure below.
    recovery = state.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, Mapping) else {}
    if recovery.get("ok") is False:
        failed = [item for item in recovery.get("failed") or () if isinstance(item, Mapping)]
        named = ", ".join(
            f"{item.get('symbol') or item.get('key') or 'a position'}: {item.get('reason') or 'incomplete'}"
            for item in failed[:4]
        )
        more = "" if len(failed) <= 4 else f", and {len(failed) - 4} more"
        problems.append(
            (
                "the paper test cannot resume a stored position and is blocked, so the "
                "open book below is incomplete: " + f"{named}{more}. "
                if named
                else "the paper test cannot resume its stored position and is blocked: "
            )
            + (str(recovery.get("detail") or "") or "no reason was recorded").strip(". ")
            + ". No value was substituted to fill the gap."
        )
    capital = read_capital(root_path)

    entries, exits, open_events, pairing_problems, close_notes = _pair_events(events)
    problems.extend(pairing_problems)
    state_positions = [
        dict(item) for item in (state.get("positions") or ()) if isinstance(item, Mapping)
    ]
    positions, unreconciled = _open_positions(
        open_events=open_events, state_positions=state_positions, problems=problems
    )
    for item in unreconciled:
        positions.append(
            {
                "key": str(item.get("key") or ""),
                "symbol": str(item.get("symbol") or ""),
                "candidate_id": str(item.get("candidate_id") or ""),
                "side": "LONG" if str(item.get("direction") or "").upper() == "LONG" else "SHORT",
                "direction": 1
                if str(item.get("direction") or "").upper() == "LONG"
                else -1,
                "lots": _finite(item.get("lots")) or 0.0,
                "entry_at": str(item.get("entry_at") or ""),
                "entry_price": _finite(item.get("entry_price")),
                "currency": str(item.get("quote_currency") or "").upper(),
                "contract_size": _finite(item.get("contract_size")) or 0.0,
                "timeframe": str(item.get("timeframe") or ""),
                "stop": _finite(item.get("stop")),
                "target": _finite(item.get("target")),
                "trail": _finite(item.get("trail")),
                "entry_bar_at": str(item.get("entry_bar_at") or ""),
                "in_state": True,
                "unreconciled": True,
            }
        )

    trades = [_trade_from_event(event, cost_model=cost_model) for event in exits]

    # -- marks -----------------------------------------------------------
    mark_map: dict[str, QuoteMark] = dict(marks or {})
    ledger_marks = marks_from_events(events)
    for symbol, mark in ledger_marks.items():
        mark_map.setdefault(symbol, mark)
    # A live mark that has no bid/ask is worse than the ledger's last one.
    for symbol, mark in list(mark_map.items()):
        if mark.bid is None or mark.ask is None:
            fallback = ledger_marks.get(symbol)
            if fallback is not None and fallback.bid is not None and fallback.ask is not None:
                mark_map[symbol] = fallback

    symbols = sorted(
        {str(item.get("symbol") or "") for item in positions}
        | {s for s in mark_map if s}
        | {t.symbol for t in trades}
    )
    quotes: list[dict[str, Any]] = []
    quote_state: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        mark = mark_map.get(symbol)
        age = mark.age_seconds(now_dt) if mark is not None else None
        fresh = bool(age is not None and age <= allowance and mark and mark.bid and mark.ask)
        row = {
            "symbol": symbol,
            "bid": None if mark is None else mark.bid,
            "ask": None if mark is None else mark.ask,
            "quote_time": "" if mark is None or mark.quote_time is None else mark.quote_time.isoformat(),
            "checked_at": "" if mark is None else mark.checked_at,
            "source": "" if mark is None else mark.source,
            "age_seconds": None if age is None else round(age, 1),
            "age_text": age_text(age),
            "fresh": fresh,
        }
        quotes.append(row)
        quote_state[symbol] = row

    # -- open positions: marks, value, unrealised ------------------------
    def _currency_for(symbol: str, recorded: str) -> str:
        if recorded:
            return recorded.upper()
        if cost_model is None or not symbol:
            return ""
        try:
            model = cost_model(symbol)
        except Exception:
            return ""
        return str(getattr(model, "quote_currency", "") or "").upper()

    position_rows: list[dict[str, Any]] = []
    for item in positions:
        symbol = str(item.get("symbol") or "")
        quote = quote_state.get(symbol) or {}
        direction = int(item.get("direction") or 1)
        # A close hits the other side of the spread: longs sell at the bid,
        # shorts buy at the ask.  Anything else is a made-up price.
        side_name = "bid" if direction > 0 else "ask"
        side_price = _finite(quote.get(side_name))
        applicable = quote.get("bid") if side_name == "bid" else quote.get("ask")
        side_price = side_price if side_price is not None else _finite(applicable)
        contract = _finite(item.get("contract_size")) or 0.0
        entry = _finite(item.get("entry_price"))
        lots = _finite(item.get("lots")) or 0.0
        reason = ""
        unrealized: float | None = None
        value: float | None = None
        if entry is None:
            reason = "the entry price is not in the ledger"
        elif contract <= 0:
            reason = "the contract size was not recorded, so no money figure is possible"
        elif not quote:
            reason = f"no quote for {symbol} has been recorded"
        elif side_price is None:
            reason = f"the last {symbol} quote has no {side_name}"
        elif not quote.get("fresh"):
            reason = (
                f"the last {symbol} quote is {quote.get('age_text')} "
                f"(allowance {allowance:,.0f} s), so it is not a price to mark with"
            )
        else:
            unrealized = direction * (side_price - entry) * contract * lots
            value = abs(side_price * contract * lots)
        position_rows.append(
            {
                **item,
                "mark_side": side_name,
                "mark": side_price,
                "mark_age_seconds": quote.get("age_seconds"),
                "mark_age_text": quote.get("age_text", "age unknown"),
                "mark_fresh": bool(quote.get("fresh")),
                "unrealized_gross": unrealized,
                "value": value,
                "currency": _currency_for(
                    str(item.get("symbol") or ""), str(item.get("currency") or "")
                ),
                "cross_check": "ledger_entry_fill" if not item.get("unreconciled") else "state_only",
                "unavailable_reason": reason,
            }
        )

    # -- realised totals -------------------------------------------------
    currencies = sorted(
        {t.currency for t in trades if t.currency}
        | {str(p.get("currency") or "") for p in position_rows if p.get("currency")}
    )
    currency = currencies[0] if len(currencies) == 1 else ""
    mixed = len(currencies) > 1
    unpriced = [t for t in trades if t.gross is None]
    prices_complete = not unpriced

    # Per-currency subtotals first: they stay truthful even when a single total
    # would not be, so a mixed book is shown broken down rather than dropped.
    by_currency: dict[str, float] = {}
    for trade in trades:
        if trade.gross is None or not trade.currency:
            continue
        by_currency[trade.currency] = by_currency.get(trade.currency, 0.0) + trade.gross

    realized_gross = (
        sum(t.gross for t in trades if t.gross is not None)
        if trades and prices_complete and not mixed
        else (0.0 if not trades else None)
    )
    realized_commission = (
        sum(t.commission for t in trades if t.commission is not None)
        if trades and all(t.commission is not None for t in trades)
        else (0.0 if not trades else None)
    )
    realized_swap = (
        sum(t.swap for t in trades if t.swap is not None)
        if trades and all(t.swap is not None for t in trades)
        else (0.0 if not trades else None)
    )
    realized_net = (
        realized_gross - realized_commission - realized_swap
        if realized_gross is not None
        and realized_commission is not None
        and realized_swap is not None
        else None
    )

    unrealized_gross = (
        sum(p["unrealized_gross"] for p in position_rows if p["unrealized_gross"] is not None)
        if all(p["unrealized_gross"] is not None for p in position_rows)
        else None
    )
    open_value = (
        sum(p["value"] for p in position_rows if p["value"] is not None)
        if all(p["value"] is not None for p in position_rows)
        else None
    )

    # -- the paper capital can only be compared within its own currency --
    capital_currency = capital.currency if capital.configured else ""
    currency_ok = True
    #: Whether the ledger's own totals could be added at all (as opposed to added
    #: and then compared with the configured capital).
    totals_addable = not mixed and prices_complete
    currency_note = ""
    if mixed:
        currency_ok = False
        currency_note = (
            "the ledger holds trades in more than one quote currency ("
            + ", ".join(currencies)
            + "), so they cannot be added without a conversion this ledger does not store"
        )
    elif currency and capital_currency and currency != capital_currency:
        currency_ok = False
        currency_note = (
            f"the ledger's figures are in {currency} and the configured paper capital is "
            f"in {capital_currency}; no conversion rate is recorded, so they are not added"
        )
    totals_available = currency_ok and prices_complete and totals_addable

    starting = capital.value if capital.configured else None
    cash = (
        starting + realized_gross
        if starting is not None and realized_gross is not None and totals_available
        else None
    )
    equity = (
        cash + unrealized_gross
        if cash is not None and unrealized_gross is not None
        else None
    )
    net_pnl = (
        realized_net + unrealized_gross
        if realized_net is not None and unrealized_gross is not None
        else None
    )
    return_pct = (
        (equity - starting) / starting * 100.0
        if equity is not None and starting not in (None, 0)
        else None
    )

    # -- what is unavailable, and why ------------------------------------
    unavailable: dict[str, str] = {}

    def _why(reason: str, *keys: str) -> None:
        for key in keys:
            unavailable.setdefault(key, reason)

    if not capital.configured:
        _why(
            "no paper starting capital is configured, and none is guessed"
            + (f" ({capital.reason})" if capital.reason else ""),
            "starting_capital",
            "cash",
            "equity",
            "return_pct",
        )
    if not totals_addable:
        # The ledger's own totals cannot be added at all: mixed currencies, or a
        # trade whose price P&L was never recorded.
        _why(
            currency_note or "not every closed trade has a price P&L in the ledger",
            "realized_gross",
            "realized_net",
            "cash",
            "equity",
            "return_pct",
            "net_pnl",
        )
    elif not totals_available:
        # Addable, but not comparable with the configured capital's currency.
        _why(currency_note, "cash", "equity", "return_pct", "net_pnl")
    if unpriced:
        _why(
            f"{len(unpriced)} closed trade(s) could not be priced "
            f"({unpriced[0].costs_missing[0] if unpriced[0].costs_missing else 'unpriced'})",
            "realized_gross",
            "realized_net",
        )
    if realized_commission is None or realized_swap is None:
        missing = sorted({part for t in trades for part in t.costs_missing})
        _why(
            "commission and swap are not fully known: " + "; ".join(missing[:4])
            if missing
            else "commission and swap are not fully known",
            "realized_net",
            "net_pnl",
        )
    if position_rows and unrealized_gross is None:
        blockers = [p for p in position_rows if p["unrealized_gross"] is None]
        _why(
            blockers[0]["unavailable_reason"] if blockers else "an open position could not be marked",
            "unrealized_gross",
            "unrealized_net",
            "equity",
            "open_value",
            "return_pct",
            "net_pnl",
        )
    if unrealized_gross is not None:
        _why(
            "commission and swap are not simulated for open positions",
            "unrealized_net",
        )
    if net_pnl is None:
        _why(
            "a net figure needs realised and unrealised P&L that are both known",
            "net_pnl",
        )

    # -- trading decisions: paused or not --------------------------------
    pause_reasons: list[str] = []
    if not bool(state.get("running")):
        pause_reasons.append("the paper session is not running")
    # Only the symbols this session is actually following can pause a decision:
    # a stale quote on a pair nobody is watching is not a reason to stop.
    followed = {str(p.get("symbol") or "") for p in position_rows}
    for watch in state.get("watches") or ():
        if isinstance(watch, Mapping):
            followed.add(str(watch.get("symbol") or ""))
    followed.discard("")
    for row in quotes:
        if row["symbol"] in followed and not row["fresh"]:
            pause_reasons.append(
                f"the {row['symbol']} quote is {row['age_text']}"
                + (f" (allowance {allowance:,.0f} s)" if row["age_seconds"] is not None else "")
            )
    trading = {
        "paused": bool(pause_reasons),
        "reason": pause_reasons[0] if pause_reasons else "",
        "reasons": pause_reasons,
        "allowance_seconds": allowance,
    }

    # -- costs -----------------------------------------------------------
    cost_missing: list[str] = []
    for trade in trades:
        for part in trade.costs_missing:
            if part not in cost_missing:
                cost_missing.append(part)
    costs = {
        "complete": not cost_missing,
        "missing": cost_missing,
        "modelled": sorted({part for t in trades for part in t.modelled}),
        "not_modelled": ["commission", "swap"],
        "note": (
            "Commission and swap are taken from the broker's terms when they were "
            "reported. This paper test does not model either, so a net figure is only "
            "shown when both are known -- otherwise it is marked unavailable."
        ),
    }

    # -- counts ----------------------------------------------------------
    counts = {
        "orders": len(entries) + len(exits),
        "fills": len(entries) + len(exits),
        "entry_fills": len(entries),
        "exit_fills": len(exits),
        "open_positions": len(position_rows),
        "closed_trades": len(trades),
        "ledger_events": len(events),
        # Summary records an older build wrote beside the priced close.  Counted
        # so nothing is invisible, and never mistaken for a trade.
        "close_notes": close_notes,
        "state_positions": len(state_positions),
    }

    # -- equity / P&L curve, one point per UTC day ------------------------
    curve: list[dict[str, Any]] = []
    if trades and totals_available:
        buckets: dict[str, dict[str, Any]] = {}
        running = 0.0
        for trade in sorted(trades, key=lambda item: item.exit_at):
            when = _parse_time(trade.exit_at)
            if when is None or trade.gross is None:
                continue
            day = when.date().isoformat()
            bucket = buckets.setdefault(
                day, {"date": day, "trades": 0, "realized_gross": 0.0, "equity": None}
            )
            bucket["trades"] += 1
            bucket["realized_gross"] += trade.gross
            running += trade.gross
            if starting is not None:
                bucket["equity"] = starting + running
        for day in sorted(buckets):
            curve.append(buckets[day])
    curve_max = max((abs(float(p["realized_gross"])) for p in curve), default=0.0)

    # -- metrics, in the order the pages show them ------------------------
    latest_event_at = ""
    for event in events:
        stamp = str(event.get("at") or "")
        if stamp > latest_event_at:
            latest_event_at = stamp
    latest_update = str(state.get("updated_at") or "") or latest_event_at
    latest_quote = max((row["quote_time"] for row in quotes if row["quote_time"]), default="")

    def _note_for(key: str, fallback: str = "") -> str:
        return unavailable.get(key, fallback)

    cash_note = (
        "includes commission and swap"
        if realized_commission is not None and realized_swap is not None and cash is not None
        else "before commission and swap"
    )
    metrics = [
        _metric(
            "starting_capital",
            "Starting capital (local simulated)",
            raw=starting,
            formatted=money(starting, capital_currency) if starting is not None else "Not set",
            note=(
                f"you configured it at {capital.set_at[:19].replace('T', ' ')} UTC"
                if capital.configured and capital.set_at
                else (capital.reason or "set it on this page")
            ),
            tone="paper",
            available=starting is not None,
        ),
        _metric(
            "cash",
            "Paper cash",
            raw=cash,
            formatted=_figure(cash, currency or capital_currency),
            note=cash_note if cash is not None else _note_for("cash"),
            tone="paper",
        ),
        _metric(
            "open_value",
            "Open position value (marked)",
            raw=open_value,
            formatted=_figure(open_value, currency),
            note=(
                "marked at the closing side; before commission and swap"
                if open_value is not None
                else _note_for("open_value", "no open position could be valued")
            ),
        ),
        _metric(
            "equity",
            "Paper equity",
            raw=equity,
            formatted=_figure(equity, currency or capital_currency),
            note="cash plus unrealised price P&L" if equity is not None else _note_for("equity"),
            tone="accent",
        ),
        _metric(
            "realized_gross",
            "Realised P&L (price)",
            raw=realized_gross,
            formatted=_figure(realized_gross, currency),
            note=(
                (
                    f"{len(trades)} closed trade(s), before commission and swap"
                    if trades
                    else "no closed trade yet — zero because nothing has closed, not an average"
                )
                if realized_gross is not None
                else _note_for("realized_gross", "no closed trade yet")
            ),
        ),
        _metric(
            "realized_net",
            "Realised P&L (after costs)",
            raw=realized_net,
            formatted=_figure(realized_net, currency),
            note=(
                "price P&L minus commission and swap"
                if realized_net is not None
                else _note_for("realized_net", "commission and swap are not fully known")
            ),
        ),
        _metric(
            "unrealized_gross",
            "Unrealised P&L (price)",
            raw=unrealized_gross,
            formatted=_figure(unrealized_gross, currency),
            note=(
                "long at the bid, short at the ask; before commission and swap"
                if unrealized_gross is not None
                else _note_for("unrealized_gross", "no open position")
            ),
        ),
        _metric(
            "net_pnl",
            "Net P&L (after costs)",
            raw=net_pnl,
            formatted=_figure(net_pnl, currency),
            note=(
                "after commission and swap"
                if net_pnl is not None
                else _note_for("net_pnl", "needs commission and swap")
            ),
        ),
        _metric(
            "return_pct",
            "Return on starting capital",
            raw=return_pct,
            formatted=percent(return_pct) if return_pct is not None else "Unavailable",
            note="(equity - starting capital) / starting capital" if return_pct is not None
            else _note_for("return_pct"),
        ),
        _metric(
            "open_positions",
            "Open positions",
            raw=float(counts["open_positions"]),
            formatted=count(counts["open_positions"]),
            note="from the ledger's entry and close events",
        ),
        _metric(
            "closed_trades",
            "Closed trades",
            raw=float(counts["closed_trades"]),
            formatted=count(counts["closed_trades"]),
            note="close events in the ledger",
        ),
        _metric(
            "orders",
            "Simulated orders",
            raw=float(counts["orders"]),
            formatted=count(counts["orders"]),
            note=f"{counts['entry_fills']} entry, {counts['exit_fills']} exit — every one filled",
        ),
        _metric(
            "fills",
            "Simulated fills",
            raw=float(counts["fills"]),
            formatted=count(counts["fills"]),
            note="the same count as orders: a paper order fills where it is priced",
        ),
    ]

    # -- the timeline, for the activity page -----------------------------
    kept = events[-EVENT_HISTORY:]
    timeline = []
    for event in kept:
        kind = str(event.get("kind") or "")
        entry = {
            "at": str(event.get("at") or ""),
            "kind": kind,
            "symbol": str(event.get("symbol") or ""),
            "reason": str(event.get("reason") or ""),
            "simulated": kind in (EVENT_FILL, EVENT_CLOSED),
        }
        if kind == EVENT_CLOSED:
            entry["detail"] = (
                f"{event.get('direction', '')} {event.get('lots', '')} lots · "
                f"{event.get('entry_price', '')} → {event.get('exit_price', '')} · "
                + money(
                    _finite(event.get("simulated_net_quote")),
                    str(event.get("quote_currency") or ""),
                )
            )
        elif kind == EVENT_FILL:
            entry["detail"] = (
                f"{event.get('direction', '')} {event.get('lots', '')} lots at "
                f"{event.get('price', '')}"
            )
        elif kind == EVENT_STALE:
            entry["detail"] = (
                f"quote {event.get('age_seconds', '?')} s old, allowance "
                f"{event.get('allowance_seconds', '?')} s"
            )
        else:
            entry["detail"] = " ".join(
                f"{name}={value}"
                for name, value in sorted(event.items())
                if name not in {"at", "kind", "simulated"} and isinstance(value, (str, int, float))
            )[:120]
        timeline.append(entry)

    integrity = {
        "ok": not problems and not mixed and currency_ok,
        "warnings": [
            {"code": "ledger_integrity", "detail": item, "metrics": []} for item in dict.fromkeys(problems)
        ],
        "currency_mixed": mixed,
        "currency_note": currency_note,
        "unpriced_trades": len(unpriced),
        "unreconciled_positions": len(unreconciled),
    }

    return {
        "available": True,
        "source": SOURCE_LABEL,
        "source_note": (
            "Every figure on this page is read from the paper ledger on this machine. "
            "Nothing here is a broker balance, and no order was sent."
        ),
        "ledger_path": str(ledger_path),
        "state_path": str(state_path),
        "read_at": now_dt.isoformat(),
        "latest_update": latest_update,
        "latest_event_at": latest_event_at,
        "latest_quote_at": latest_quote,
        "session": {
            "status": str(state.get("status") or ""),
            "running": bool(state.get("running")),
            "started_at": str(state.get("started_at") or ""),
            "stopped_at": str(state.get("stopped_at") or ""),
            "reason": str(state.get("reason") or ""),
            "watches": len(state.get("watches") or ()),
            "lots": _finite(state.get("lots")),
            "max_quote_age_seconds": allowance,
            "recovery": recovery,
        },
        "capital": capital.audit_view(),
        "currency": currency or capital_currency,
        "quote_currency": currency,
        "counts": counts,
        "quotes": quotes,
        "trading": trading,
        "values": {
            "starting_capital": starting,
            "cash": cash,
            "open_value": open_value,
            "equity": equity,
            "realized_gross": realized_gross,
            "realized_by_currency": dict(sorted(by_currency.items())),
            "realized_commission": realized_commission,
            "realized_swap": realized_swap,
            "realized_net": realized_net,
            "unrealized_gross": unrealized_gross,
            "unrealized_net": None,
            "net_pnl": net_pnl,
            "return_pct": return_pct,
        },
        "metrics": metrics,
        "metric_by_key": {metric["key"]: metric for metric in metrics},
        "unavailable": unavailable,
        "integrity": integrity,
        "costs": costs,
        "positions": position_rows,
        "trades": [
            {
                "key": trade.key,
                "symbol": trade.symbol,
                "candidate_id": trade.candidate_id,
                "side": "LONG" if trade.direction > 0 else "SHORT",
                "direction": trade.direction,
                "lots": trade.lots,
                "entry_at": trade.entry_at,
                "exit_at": trade.exit_at,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "reason": trade.reason,
                "exit_basis": trade.basis,
                "gross": trade.gross,
                "commission": trade.commission,
                "swap": trade.swap,
                "nights": trade.nights,
                "net": trade.net,
                "currency": trade.currency,
                "contract_size": trade.contract_size,
                "costs_complete": trade.costs_complete,
                "costs_missing": list(trade.costs_missing),
            }
            for trade in trades
        ],
        "equity_curve": curve,
        "equity_curve_scale": curve_max,
        "events": timeline,
        "events_total": len(events),
        "events_truncated": max(0, len(events) - len(kept)),
        "extra_positions": [dict(item) for item in unreconciled],
        "definitions": dict(DEFINITIONS),
        "claim": (
            "Paper only: prices come from the terminal, fills are simulated on this "
            "machine, and no order was sent. Nothing here is a broker balance and "
            "nothing here is a promise of profit."
        ),
    }


__all__ = [
    "CAPITAL_NAME",
    "CURRENCY_CHOICES",
    "DEFINITIONS",
    "MAX_CAPITAL_AMOUNT",
    "PaperCapital",
    "QuoteMark",
    "SOURCE_LABEL",
    "age_text",
    "_figure",
    "capital_path",
    "count",
    "marks_from_events",
    "marks_from_snapshot",
    "money",
    "percent",
    "price",
    "read_account",
    "read_capital",
    "read_ledger",
    "read_state",
    "write_capital",
]
