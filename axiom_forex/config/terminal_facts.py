"""What the terminal can actually tell us, and what it cannot.

The old provider page asked for forty-odd terms because the *document* needs forty
terms.  Almost none of them are choices: the account currency, the leverage, the
exact broker symbol, its digits, point, contract size, volume grid, stop distance
and usually its swap terms are all sitting in the terminal, already answered.

This module reads exactly those, once, and labels every one with where it came
from:

``OBSERVED``   read from the connected terminal, on this read
``SIMULATED``  the offline sample source -- deliberately marked as not real
``UNKNOWN``    the terminal did not answer, and the reason is recorded

It never raises for a missing field, never guesses a value, never reads a
credential, and never writes anything.  The terminal's own login is *not* part of
the result: an account fingerprint is enough to tell two accounts apart, and a
login is not needed to configure anything.

What the terminal does **not** publish in a form AXIOM can trust, and therefore
what stays unknown until the operator supplies it in the advanced broker-terms
page:

* the commission schedule (MT5's symbol record has no commission field),
* the broker's session calendar windows and holiday list,
* the rollover cutoff hour,
* swap in the modes AXIOM cannot price (pure interest-rate modes).

An unknown value is shown as unknown.  It is never replaced by zero, because a
zero commission is a claim, and it would make every result look better.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

OBSERVED = "OBSERVED"
SIMULATED = "SIMULATED"
UNKNOWN = "UNKNOWN"

#: MT5 ``ENUM_SYMBOL_SWAP_MODE`` values this build can price in its cost model.
SWAP_POINTS = 1
SWAP_CURRENCY_SYMBOL = 2
SWAP_CURRENCY_MARGIN = 3
SWAP_CURRENCY_DEPOSIT = 4
#: Interest-rate modes: the value is a rate, not a per-lot amount.
SWAP_INTEREST_MODES = frozenset({5, 6, 7, 8})

#: Three-letter currency codes.  A pair is only derived when both legs are real
#: codes the terminal reported -- never by splitting a symbol name.
_CURRENCY_CODES = frozenset(
    {
        "AED", "ARS", "AUD", "BGN", "BHD", "BRL", "CAD", "CHF", "CLP", "CNH", "CNY",
        "COP", "CZK", "DKK", "DZD", "EGP", "EUR", "GBP", "HKD", "HUF", "IDR", "ILS",
        "INR", "ISK", "JOD", "JPY", "KES", "KRW", "KWD", "KZT", "LKR", "MAD", "MXN",
        "MYR", "NGN", "NOK", "NZD", "OMR", "PEN", "PHP", "PKR", "PLN", "QAR", "RON",
        "RUB", "SAR", "SEK", "SGD", "THB", "TND", "TRY", "TWD", "UAH", "USD", "UYU",
        "VND", "XAG", "XAU", "XPT", "ZAR",
    }
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _read(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


@dataclass(frozen=True)
class Fact:
    """One value, its provenance, and -- when unknown -- why."""

    name: str
    value: str = ""
    state: str = UNKNOWN
    source: str = ""
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.state in {OBSERVED, SIMULATED} and self.value != ""

    def audit_view(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": self.value,
            "state": self.state,
            "source": self.source,
            "detail": self.detail,
        }


def _fact(name: str, value: Any, state: str, source: str, detail: str = "") -> Fact:
    if value is None or value == "":
        return Fact(name=name, state=UNKNOWN, source=source, detail=detail)
    return Fact(name=name, value=str(value), state=state, source=source, detail=detail)


@dataclass(frozen=True)
class SymbolFacts:
    """One broker symbol's specification, field by field."""

    broker_symbol: str
    description: str = ""
    pair: str = ""
    base_currency: str = ""
    profit_currency: str = ""
    margin_currency: str = ""
    digits: int = 0
    point: Decimal | None = None
    contract_size: Decimal | None = None
    volume_min: Decimal | None = None
    volume_max: Decimal | None = None
    volume_step: Decimal | None = None
    stops_level: int = 0
    spread_points: int = 0
    trade_mode: int = 0
    tradable: bool = False
    swap_long: Decimal | None = None
    swap_short: Decimal | None = None
    swap_mode: int | None = None
    swap_rollover3days: int | None = None
    state: str = OBSERVED
    unknown: tuple[str, ...] = ()
    source: str = "terminal.symbol_info"

    @property
    def swap_state(self) -> str:
        """Whether the reported swap can be priced, and if not, why."""

        if self.swap_mode is None:
            return UNKNOWN
        if int(self.swap_mode) == 0:
            return "DISABLED"
        if int(self.swap_mode) in SWAP_INTEREST_MODES:
            return "INTEREST_MODE"
        if int(self.swap_mode) in {
            SWAP_POINTS,
            SWAP_CURRENCY_SYMBOL,
            SWAP_CURRENCY_MARGIN,
            SWAP_CURRENCY_DEPOSIT,
        }:
            if self.swap_long is None or self.swap_short is None:
                return UNKNOWN
            return "POINTS" if int(self.swap_mode) == SWAP_POINTS else "CURRENCY"
        return UNKNOWN

    @property
    def pip_size(self) -> Decimal | None:
        """A conventional pip, derived from the broker's point -- labelled as derived."""

        point = self.point
        if point is None or point <= 0:
            return None
        # A 5/3-digit symbol quotes a tenth of a pip; 4/2-digit quotes a pip.
        return point * 10 if self.digits in (3, 5) else point

    def missing(self) -> tuple[str, ...]:
        return tuple(self.unknown)

    def audit_view(self) -> dict[str, Any]:
        return {
            "broker_symbol": self.broker_symbol,
            "description": self.description,
            "pair": self.pair,
            "base_currency": self.base_currency,
            "profit_currency": self.profit_currency,
            "margin_currency": self.margin_currency,
            "digits": self.digits,
            "point": str(self.point) if self.point is not None else "",
            "contract_size": str(self.contract_size) if self.contract_size is not None else "",
            "volume_min": str(self.volume_min) if self.volume_min is not None else "",
            "volume_max": str(self.volume_max) if self.volume_max is not None else "",
            "volume_step": str(self.volume_step) if self.volume_step is not None else "",
            "pip_size": str(self.pip_size) if self.pip_size is not None else "",
            "stops_level": self.stops_level,
            "spread_points": self.spread_points,
            "tradable": self.tradable,
            "swap_long": str(self.swap_long) if self.swap_long is not None else "",
            "swap_short": str(self.swap_short) if self.swap_short is not None else "",
            "swap_mode": self.swap_mode,
            "swap_state": self.swap_state,
            "state": self.state,
            "unknown": list(self.unknown),
            "source": self.source,
        }


def _pair_from(base: str, profit: str) -> str:
    base = str(base or "").strip().upper()
    profit = str(profit or "").strip().upper()
    if base in _CURRENCY_CODES and profit in _CURRENCY_CODES and base != profit:
        return f"{base}/{profit}"
    return ""


def symbol_facts(
    info: Any, *, state: str = OBSERVED, source: str = "terminal.symbol_info"
) -> SymbolFacts:
    """Read one symbol record.  Missing fields become ``UNKNOWN``, not defaults."""

    name = str(_read(info, "name") or "").strip()
    unknown: list[str] = []
    base = str(_read(info, "currency_base") or "").strip()
    profit = str(_read(info, "currency_profit") or "").strip()
    margin = str(_read(info, "currency_margin") or "").strip()
    if not base or not profit:
        unknown.append("quote_currencies")
    pair = _pair_from(base, profit)
    if not pair:
        unknown.append("pair")

    digits = int(_read(info, "digits") or 0)
    point = _decimal(_read(info, "point"))
    contract = _decimal(_read(info, "trade_contract_size"))
    volume_min = _decimal(_read(info, "volume_min"))
    volume_max = _decimal(_read(info, "volume_max"))
    volume_step = _decimal(_read(info, "volume_step"))
    for field_name, value in (
        ("digits", digits),
        ("point", point),
        ("contract_size", contract),
        ("volume_min", volume_min),
        ("volume_max", volume_max),
        ("volume_step", volume_step),
    ):
        if not value:
            unknown.append(field_name)

    swap_long = _decimal(_read(info, "swap_long"))
    swap_short = _decimal(_read(info, "swap_short"))
    raw_swap_mode = _read(info, "swap_mode")
    swap_mode = None if raw_swap_mode is None else int(raw_swap_mode)
    raw_triple = _read(info, "swap_rollover3days")
    triple = None if raw_triple is None else int(raw_triple)
    if swap_mode is None:
        unknown.append("swap_mode")
    if swap_long is None or swap_short is None:
        unknown.append("swap_rates")

    trade_mode = int(_read(info, "trade_mode") or 0)
    return SymbolFacts(
        broker_symbol=name,
        description=str(_read(info, "description") or "").strip(),
        pair=pair,
        base_currency=base,
        profit_currency=profit,
        margin_currency=margin,
        digits=digits,
        point=point,
        contract_size=contract,
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        stops_level=int(_read(info, "stops_level") or 0),
        spread_points=int(_read(info, "spread") or 0),
        trade_mode=trade_mode,
        tradable=trade_mode == 4,
        swap_long=swap_long,
        swap_short=swap_short,
        swap_mode=swap_mode,
        swap_rollover3days=triple,
        state=state,
        unknown=tuple(sorted(set(unknown))),
        source=source,
    )


@dataclass(frozen=True)
class TerminalFacts:
    """Everything one read of the terminal produced, with provenance."""

    source_kind: str = "NONE"
    connected: bool = False
    simulated: bool = False
    company: str = ""
    server: str = ""
    account_fingerprint: str = ""
    currency: Fact = field(default_factory=lambda: Fact("account_currency"))
    leverage: Fact = field(default_factory=lambda: Fact("account_leverage"))
    account_mode: Fact = field(default_factory=lambda: Fact("account_mode"))
    symbols: tuple[SymbolFacts, ...] = ()
    #: Names the terminal reported, and how many of them were classified.  The two
    #: differ only when the reader could not return specifications in bulk; the
    #: difference is then reported rather than hidden.
    symbols_reported: int = 0
    symbols_examined: int = 0
    #: True when one terminal call carried the specifications.
    symbols_bulk: bool = False
    read_at: str = ""
    error: str = ""
    notes: tuple[str, ...] = ()

    @property
    def symbols_seen(self) -> int:
        """How many symbols the terminal reported."""

        return self.symbols_reported or len(self.symbols)

    @property
    def not_examined(self) -> int:
        """How many reported names were never read.  Zero unless the read was cut."""

        return max(0, self.symbols_seen - (self.symbols_examined or len(self.symbols)))

    @property
    def symbols_truncated(self) -> bool:
        return self.not_examined > 0

    @property
    def state(self) -> str:
        if self.simulated:
            return SIMULATED
        return OBSERVED if self.connected else UNKNOWN

    def symbol(self, broker_symbol: str) -> SymbolFacts | None:
        wanted = str(broker_symbol or "").strip()
        for item in self.symbols:
            if item.broker_symbol == wanted:
                return item
        return None

    def pairs(self) -> tuple[str, ...]:
        return tuple(item.pair for item in self.symbols if item.pair)

    def ready_for_research(self) -> tuple[str, ...]:
        """What still stops a one-year research run, in the operator's terms."""

        blockers: list[str] = []
        if self.error:
            blockers.append(f"the terminal could not be read ({self.error})")
        elif not self.connected:
            blockers.append("no terminal is connected, so there are no symbols or specs")
        if not self.symbols:
            blockers.append("the terminal reported no symbols")
        if not any(item.pair for item in self.symbols):
            blockers.append(
                "no symbol reported a base and profit currency, so no pair can be named"
            )
        for item in self.symbols:
            if item.state == UNKNOWN:
                blockers.append(f"{item.broker_symbol or 'a symbol'} could not be read")
        return tuple(dict.fromkeys(blockers))

    def audit_view(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "connected": self.connected,
            "simulated": self.simulated,
            "company": self.company,
            "server": self.server,
            "account_fingerprint": self.account_fingerprint,
            "currency": self.currency.audit_view(),
            "leverage": self.leverage.audit_view(),
            "account_mode": self.account_mode.audit_view(),
            "symbols": [item.audit_view() for item in self.symbols],
            "symbols_reported": self.symbols_seen,
            "symbols_examined": self.symbols_examined or len(self.symbols),
            "symbols_truncated": self.symbols_truncated,
            "symbols_bulk": self.symbols_bulk,
            "read_at": self.read_at,
            "error": self.error,
            "state": self.state,
            "notes": list(self.notes),
        }


UNKNOWN_NOTES: tuple[str, ...] = (
    "Commission is not a field in the terminal's symbol record. It stays unknown "
    "until you supply it; research runs label the missing cost instead of assuming "
    "zero.",
    "Broker session windows and holidays are not read here. Live execution still "
    "needs them in the broker-terms document.",
    "Swap is read when the broker reports it in a mode AXIOM can price (points or a "
    "per-lot currency amount). Interest-rate swap modes are recorded as unknown.",
)


#: Fields whose presence proves a bulk answer described symbols rather than naming
#: them.  An empty value is an answer ("not a pair"); a missing key is not.
SYMBOL_EVIDENCE_FIELDS: tuple[str, ...] = (
    "currency_base",
    "currency_profit",
    "digits",
    "point",
    "trade_mode",
)

#: How many per-name reads a *fallback* may make when a reader cannot return
#: specifications in bulk.  It is a safety bound, not a policy: the result reports
#: exactly how many names it did not reach, so a bounded read is never mistaken for
#: the whole symbol list.
MAX_FALLBACK_READS = 300


def _carries_specifications(snapshots: Any) -> bool:
    """True when a bulk answer described symbols, not just named them."""

    return any(
        field in entry
        for entry in (snapshots or ())
        if isinstance(entry, Mapping)
        for field in SYMBOL_EVIDENCE_FIELDS
    )


def _symbols_from_snapshots(snapshots: Any, *, state: str) -> tuple[SymbolFacts, ...]:
    """Facts for every symbol in one bulk answer.  No field is invented."""

    symbols: list[SymbolFacts] = []
    for entry in snapshots or ():
        if not isinstance(entry, Mapping) and not hasattr(entry, "_asdict"):
            continue
        row = dict(entry._asdict()) if hasattr(entry, "_asdict") else dict(entry)
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        captured = symbol_facts(row, state=state)
        if not captured.broker_symbol:
            captured = replace(captured, broker_symbol=name)
        symbols.append(captured)
    return tuple(symbols)


def observe_terminal(reader: Any, *, symbol_limit: int | None = None) -> TerminalFacts:
    """Read account facts and symbol specifications.  Never raises.

    ``reader`` is anything exposing ``status()``, ``symbol_names()`` and
    ``symbol_info(name)`` -- the read-only terminal in production, a fake in tests --
    and ideally ``symbol_snapshots()``, which returns every reported symbol *with* its
    specification in one call.  That path is tried first, because reading names first
    and then the first N specifications is an alphabetical window: on a real broker,
    whose equity CFDs sort ahead of the forex board, it hides every pair after them.

    ``symbol_limit`` bounds the fallback path only, and a bounded read says so through
    ``symbols_reported`` / ``symbols_examined``.  Nothing here writes anywhere.
    """

    if reader is None:
        return TerminalFacts(error="no_terminal", notes=UNKNOWN_NOTES)
    try:
        status = reader.status()
    except Exception as exc:
        return TerminalFacts(error=type(exc).__name__, notes=UNKNOWN_NOTES)

    kind = str(_read(reader, "kind") or _read(status, "source_kind") or "MT5_TERMINAL")
    simulated = bool(_read(reader, "simulated") or _read(status, "simulated"))
    company = str(_read(status, "company") or "")
    server = str(_read(status, "account_server") or _read(status, "server") or "")
    fingerprint = str(_read(status, "account_fingerprint") or "")
    currency_state = SIMULATED if simulated else OBSERVED
    currency = _fact(
        "account_currency",
        _read(status, "account_currency"),
        currency_state,
        "terminal.account_info",
        "the terminal did not report an account currency",
    )
    leverage = _fact(
        "account_leverage",
        _read(status, "leverage"),
        currency_state,
        "terminal.account_info",
        "the terminal did not report leverage",
    )
    mode = _fact(
        "account_mode",
        _read(status, "account_mode"),
        currency_state,
        "terminal.account_info",
        "the terminal did not report whether this is a demo or a real account",
    )

    # -- symbols: one bulk call first -------------------------------------
    bulk_reader = getattr(reader, "symbol_snapshots", None)
    if callable(bulk_reader):
        try:
            snapshots = list(bulk_reader() or ())
        except Exception:
            snapshots = []
        if _carries_specifications(snapshots):
            # An explicit caller bound is honoured here too, and reported the same
            # way: the count of what the terminal reported comes from the full answer,
            # never from the slice.
            reported = len(snapshots)
            kept = snapshots if symbol_limit is None else snapshots[: max(1, int(symbol_limit))]
            symbols = _symbols_from_snapshots(
                kept, state=SIMULATED if simulated else OBSERVED
            )
            if symbols:
                notes = list(UNKNOWN_NOTES)
                if len(symbols) < reported:
                    notes.append(
                        f"The read was limited to {len(symbols)} of {reported} reported "
                        "names by the caller, so the rest were not classified."
                    )
                return TerminalFacts(
                    source_kind=kind,
                    connected=bool(_read(status, "connected")),
                    simulated=simulated,
                    company=company,
                    server=server,
                    account_fingerprint=fingerprint,
                    currency=currency,
                    leverage=leverage,
                    account_mode=mode,
                    symbols=symbols,
                    symbols_reported=reported,
                    symbols_examined=len(symbols),
                    symbols_bulk=True,
                    read_at=_utc().isoformat(),
                    notes=tuple(notes),
                )

    names: Iterable[Any] = ()
    try:
        names = reader.symbol_names() or ()
    except Exception as exc:
        return TerminalFacts(
            source_kind=kind,
            connected=bool(_read(status, "connected")),
            simulated=simulated,
            company=company,
            server=server,
            account_fingerprint=fingerprint,
            currency=currency,
            leverage=leverage,
            account_mode=mode,
            read_at=_utc().isoformat(),
            error=f"symbols_unavailable:{type(exc).__name__}",
            notes=UNKNOWN_NOTES,
        )

    listed = [name for name in list(names) if str(name).strip()]
    bound = MAX_FALLBACK_READS if symbol_limit is None else max(1, int(symbol_limit))
    symbols: list[SymbolFacts] = []
    notes = list(UNKNOWN_NOTES)
    reached = listed[:bound]
    if len(reached) < len(listed):
        notes.append(
            f"This reader could not return symbol specifications in bulk, so only "
            f"{len(reached)} of {len(listed)} reported names were read. The rest were "
            "not classified."
        )
    for name in reached:
        text = str(name).strip()
        if not text:
            continue
        try:
            info = reader.symbol_info(text)
        except Exception:
            symbols.append(
                SymbolFacts(broker_symbol=text, state=UNKNOWN, unknown=("symbol_info",))
            )
            continue
        symbols.append(
            symbol_facts(info, state=SIMULATED if simulated else OBSERVED)
        )

    return TerminalFacts(
        source_kind=kind,
        connected=bool(_read(status, "connected")),
        simulated=simulated,
        company=company,
        server=server,
        account_fingerprint=fingerprint,
        currency=currency,
        leverage=leverage,
        account_mode=mode,
        symbols=tuple(symbols),
        symbols_reported=len(listed),
        symbols_examined=len(reached),
        symbols_bulk=False,
        read_at=_utc().isoformat(),
        notes=tuple(notes),
    )


__all__ = [
    "Fact",
    "MAX_FALLBACK_READS",
    "OBSERVED",
    "SYMBOL_EVIDENCE_FIELDS",
    "SIMULATED",
    "SWAP_CURRENCY_DEPOSIT",
    "SWAP_CURRENCY_MARGIN",
    "SWAP_CURRENCY_SYMBOL",
    "SWAP_INTEREST_MODES",
    "SWAP_POINTS",
    "SymbolFacts",
    "TerminalFacts",
    "UNKNOWN",
    "UNKNOWN_NOTES",
    "observe_terminal",
    "symbol_facts",
]
