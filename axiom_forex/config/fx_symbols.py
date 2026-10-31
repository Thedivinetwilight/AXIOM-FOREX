"""Which symbols are forex, which are the majors, and what to offer first.

A broker's symbol list contains far more than foreign exchange: stock CFDs
(`AAOIUSD` and its relatives), indices, crypto, metals and synthetic tickers.  A
forex research and paper tool must start from actual currency pairs, not from a
hard-coded list that includes somebody's equity CFD.

Two rules make that honest:

* **A pair is only named from the terminal's own two currency legs.**  `AAOIUSD`
  has no reported base currency, and a metal code (`XAU`, `XAG`, `XPT`) is
  excluded on purpose: it is a commodity, not a currency pair.  A symbol whose
  legs the terminal did not report is *unclassified* -- shown as such, never
  guessed from the ticker text.
* **The majors are a shortlist, not a filter.**  When the terminal reports a
  major, it is offered first because a small set is all a first paper test needs.
  Everything else that is genuinely a pair stays selectable, after the majors.

Nothing here connects anywhere: it classifies what the terminal already reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .terminal_facts import SymbolFacts

#: The seven conventional majors, as canonical ``BASE/QUOTE`` strings.
MAJOR_PAIRS: tuple[str, ...] = (
    "EUR/USD",
    "GBP/USD",
    "USD/JPY",
    "USD/CHF",
    "AUD/USD",
    "USD/CAD",
    "NZD/USD",
)

#: Precious-metal codes.  They appear in the same currency fields on most brokers,
#: and they are not foreign exchange.
METAL_CODES: frozenset[str] = frozenset({"XAU", "XAG", "XPT", "XPD"})

#: Contract kinds, as the report words them.
FX_PAIR = "FOREX_PAIR"
METAL = "METAL"
UNCLASSIFIED = "UNCLASSIFIED"

#: Why a symbol cannot be used, in the operator's terms.
REASON_NOT_A_PAIR = "the terminal did not report both currency legs, so this is not a pair AXIOM can name"
REASON_METAL = "this is a metal, not a foreign-exchange pair"
REASON_NOT_TRADABLE = "the broker reports this symbol as not tradable in this account"
REASON_NO_SPEC = "the terminal reported no point size or contract size for this symbol"


@dataclass(frozen=True)
class SymbolChoice:
    """One terminal symbol, classified for the picker."""

    broker_symbol: str
    pair: str
    kind: str
    major: bool
    tradable: bool
    digits: int
    spread_points: int
    swap_state: str
    usable: bool
    reason: str = ""

    @property
    def label(self) -> str:
        if self.pair:
            return f"{self.broker_symbol} · {self.pair}"
        return self.broker_symbol

    def audit_view(self) -> dict[str, Any]:
        return {
            "broker_symbol": self.broker_symbol,
            "pair": self.pair,
            "kind": self.kind,
            "major": self.major,
            "tradable": self.tradable,
            "digits": self.digits,
            "spread_points": self.spread_points,
            "swap_state": self.swap_state,
            "usable": self.usable,
            "reason": self.reason,
        }


def classify(symbol: SymbolFacts) -> SymbolChoice:
    """Classify one reported symbol.  Never infers a pair from the ticker."""

    base = str(symbol.base_currency or "").strip().upper()
    profit = str(symbol.profit_currency or "").strip().upper()
    metal = base in METAL_CODES or profit in METAL_CODES
    if metal:
        kind = METAL
    elif symbol.pair:
        kind = FX_PAIR
    else:
        kind = UNCLASSIFIED

    reason = ""
    if kind != FX_PAIR:
        reason = REASON_METAL if kind == METAL else REASON_NOT_A_PAIR
    elif not symbol.tradable:
        reason = REASON_NOT_TRADABLE
    elif symbol.point is None or symbol.contract_size is None:
        reason = REASON_NO_SPEC

    return SymbolChoice(
        broker_symbol=symbol.broker_symbol,
        pair=symbol.pair,
        kind=kind,
        major=symbol.pair in MAJOR_PAIRS,
        tradable=bool(symbol.tradable),
        digits=int(symbol.digits or 0),
        spread_points=int(symbol.spread_points or 0),
        swap_state=str(symbol.swap_state),
        usable=(kind == FX_PAIR and bool(symbol.tradable) and not reason),
        reason=reason,
    )


def forex_symbols(symbols: Iterable[SymbolFacts]) -> tuple[SymbolChoice, ...]:
    """Every reported symbol that is genuinely a currency pair, majors first.

    Unusable pairs (not tradable, no spec) are included, with their reason, so the
    page can say *why* a symbol you expect is missing instead of hiding it.
    """

    choices = [classify(item) for item in symbols]
    pairs = [item for item in choices if item.kind == FX_PAIR]
    pairs.sort(key=lambda item: (not item.major, not item.usable, item.pair, item.broker_symbol))
    return tuple(pairs)


def excluded(symbols: Iterable[SymbolFacts]) -> tuple[SymbolChoice, ...]:
    """The reported symbols that are not currency pairs, for an honest count."""

    return tuple(item for item in (classify(entry) for entry in symbols) if item.kind != FX_PAIR)


def recommended(symbols: Iterable[SymbolFacts], limit: int = 7) -> tuple[str, ...]:
    """A small default set: the majors the terminal actually reports.

    If no major is present (a broker with unusual naming, or a demo account with a
    short list), the first usable pairs are offered instead.  An empty tuple means
    nothing suitable was reported, which the caller must treat as a blocker rather
    than inventing a symbol.
    """

    usable = [item for item in forex_symbols(symbols) if item.usable]
    majors = [item for item in usable if item.major]
    chosen = majors or usable
    return tuple(item.broker_symbol for item in chosen[: max(1, int(limit))])


def resolve_choice(symbols: Iterable[SymbolFacts], wanted: Sequence[str]) -> tuple[SymbolChoice, ...]:
    """The operator's picks, matched to reported symbols, in their chosen order."""

    catalogue = {item.broker_symbol: classify(item) for item in symbols}
    found: list[SymbolChoice] = []
    seen: set[str] = set()
    for name in wanted:
        text = str(name or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        choice = catalogue.get(text)
        if choice is not None:
            found.append(choice)
    return tuple(found)


__all__ = [
    "FX_PAIR",
    "MAJOR_PAIRS",
    "METAL",
    "METAL_CODES",
    "REASON_METAL",
    "REASON_NOT_A_PAIR",
    "REASON_NOT_TRADABLE",
    "REASON_NO_SPEC",
    "SymbolChoice",
    "UNCLASSIFIED",
    "classify",
    "excluded",
    "forex_symbols",
    "recommended",
    "resolve_choice",
]
