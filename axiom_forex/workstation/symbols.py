"""The symbol picker on Account & symbols: what may be selected, and why not.

Everything here is derived from what the connected terminal reported.  Nothing is
invented, renamed, or guessed from the ticker text:

* A symbol is **selectable only when AXIOM can verify it is a foreign-exchange
  currency pair** -- the terminal reported *both* currency legs for it.  The
  broker's own name is kept exactly, suffixes included (`EURUSD.pro` stays
  `EURUSD.pro`; it is never rewritten to `EURUSD`).
* Everything else the terminal reports -- metals, equity CFDs, indices, crypto,
  and any symbol whose legs were not reported -- is listed with a short reason and
  cannot be selected or saved.  A metal code living in the same currency fields as
  a pair is caught by the shared classifier, not by a list written here.
* A symbol reported without a specification at all is unverifiable, and says so
  rather than being assumed to be a pair.

The two bulk controls are defined here as data, not as UI state:

``select_all``   every verified forex pair the account can actually trade
``clear``        the empty selection

**Everything the terminal reports is classified; only what is drawn is capped.**
The list used to be cut to the first N names in alphabetical order *before* any
specification was read, so on a real broker -- whose CFD tickers sort ahead of the
forex board -- the walk stopped shortly after the ``AUD`` block and no pair later in
the alphabet was ever examined.  The read now asks the terminal for the whole symbol
list in one call, classifies every reported name, and caps only the rows the page
draws, saying how many it did not draw.  A symbol is never dropped from the audit
counts because of a rendering limit.

Selecting an instrument the broker reports as not tradable (or without a full
specification) would put a symbol into the observed set that no order could ever
use, so ``select_all`` skips those and the page lists what it skipped and why.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from ..config.fx_symbols import FX_PAIR, METAL, SymbolChoice, UNCLASSIFIED, classify
from ..config.terminal_facts import SYMBOL_EVIDENCE_FIELDS, SymbolFacts, symbol_facts

#: Both currency legs are what make a symbol a *pair*.  Without them the terminal
#: has not said what the symbol is, and AXIOM will not read it off the ticker text.
SPEC_FIELDS: tuple[str, ...] = ("currency_base", "currency_profit")

#: Why a reported symbol cannot even be classified.
REASON_NO_SPEC_REPORTED = (
    "the terminal did not report both currency legs, so AXIOM cannot verify this is a pair"
)
#: Why a verified pair is left out of the bulk selection.
REASON_TRADE_MODE_NOT_REPORTED = (
    "the terminal did not report whether this symbol can be traded in this account"
)
#: Why nothing may be selected at all.
REASON_NO_FOREX = "the terminal reported no symbol AXIOM can verify as a currency pair"
#: Why the count of reported names is larger than the count of names read.
REASON_READ_CAPPED = (
    "the terminal adapter could not return specifications in bulk, so only the first "
    "names were read"
)

#: How many excluded symbols the page draws.  Purely a rendering cap: every reported
#: symbol is still classified and counted, and the page says how many it left undrawn.
MAX_EXCLUDED_SHOWN = 200


def _name_of(record: Mapping[str, Any]) -> str:
    return str(record.get("name") or record.get("broker_symbol") or "").strip()


def _facts_for(record: Mapping[str, Any]) -> SymbolFacts | None:
    """One record's facts, or ``None`` when the terminal reported no spec.

    The name is the terminal's; nothing else is filled in.
    """

    name = _name_of(record)
    if not name:
        return None
    if not all(str(record.get(field) or "").strip() for field in SPEC_FIELDS):
        return None
    try:
        facts = symbol_facts(dict(record))
    except Exception:
        return None
    if not facts.broker_symbol:
        facts = replace(facts, broker_symbol=name)
    return facts


@dataclass(frozen=True)
class SymbolCatalogue:
    """What the terminal reported, split into selectable and excluded."""

    #: True when at least one reported symbol carried a specification, so the page
    #: can honestly claim to have classified the list.
    verified: bool
    reason: str
    sample: bool
    reported: tuple[str, ...]
    pairs: tuple[SymbolChoice, ...]
    excluded: tuple[dict[str, str], ...]
    select_all: tuple[str, ...]
    skipped_by_select_all: tuple[dict[str, str], ...]
    counts: dict[str, int]
    #: Names the terminal reported whose specification was actually read.  Equal to
    #: ``len(reported)`` unless the adapter could not read specifications in bulk;
    #: every name in ``reported`` is classified in that case, never silently skipped.
    examined: int = 0

    @property
    def available(self) -> bool:
        return bool(self.reported)

    @property
    def truncated(self) -> bool:
        """True when fewer names were read than the terminal reported.

        ``reported`` holds the names that *were* classified, so the gap is read from
        the counts: it is non-zero only when the adapter could not return
        specifications in bulk and the read had to stop early.
        """

        return int(self.counts.get("not_examined", 0) or 0) > 0

    @property
    def select_all_available(self) -> bool:
        return bool(self.select_all)

    def split_selectable(self, names: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Split posted names into ``(kept, not_reported, not_selectable)``.

        A name is kept only when the terminal reported it *and* AXIOM verified it as
        a currency pair this account can trade -- the same rule the page's tiles
        apply, so a crafted post cannot select what the page greys out.  Nothing is
        substituted, translated, or canonicalised, so a posted name the terminal did
        not report is dropped and named back to the operator.
        """

        verified_names = set(self.select_all)
        kept: list[str] = []
        unknown: list[str] = []
        unverified: list[str] = []
        seen: set[str] = set()
        for raw in names:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if name not in self.reported:
                unknown.append(name)
            elif name in verified_names:
                kept.append(name)
            else:
                unverified.append(name)
        return tuple(kept), tuple(unknown), tuple(unverified)

    def audit_view(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "verified": self.verified,
            "reason": self.reason,
            "sample": self.sample,
            "reported": list(self.reported),
            "pairs": [item.audit_view() for item in self.pairs],
            "excluded": [dict(item) for item in self.excluded],
            "select_all": list(self.select_all),
            "skipped_by_select_all": [dict(item) for item in self.skipped_by_select_all],
            "counts": dict(self.counts),
            "examined": self.examined,
            "truncated": self.truncated,
        }


#: The specification fields the picker asks the terminal for.  Only what is needed
#: to classify and to describe a symbol; nothing is estimated.
RECORD_FIELDS: tuple[str, ...] = (
    "description",
    "currency_base",
    "currency_profit",
    "digits",
    "point",
    "trade_contract_size",
    "trade_mode",
)


def _snapshot_of(info: Any) -> Mapping[str, Any] | None:
    """One symbol's specification, from whatever form the terminal returned."""

    if info is None:
        return None
    entries = info if isinstance(info, (list, tuple)) else [info]
    for entry in entries:
        if entry is None:
            continue
        if isinstance(entry, Mapping):
            return entry
        fields = getattr(entry, "_asdict", None)
        if callable(fields):
            try:
                mapping = fields()
            except Exception:
                continue
            if isinstance(mapping, Mapping):
                return mapping
        return {
            field: getattr(entry, field, None)
            for field in RECORD_FIELDS
            if hasattr(entry, field)
        }
    return None


@dataclass(frozen=True)
class SymbolRead:
    """What a symbol read saw: the records, and how much of the list it covered."""

    records: tuple[dict[str, Any], ...] = ()
    #: Names the terminal reported, whether or not their spec was read.
    reported: int = 0
    #: Names whose specification was read.
    examined: int = 0
    #: True when one terminal call carried the specifications.
    bulk: bool = False
    #: Empty, or why the read stopped before the end of the reported list.
    reason: str = ""

    @property
    def truncated(self) -> bool:
        return 0 < self.examined < self.reported


def _record_from(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """One record for the catalogue, keeping only fields the terminal reported."""

    name = _name_of(entry)
    if not name:
        return None
    record: dict[str, Any] = {"name": name}
    for field in RECORD_FIELDS:
        if field in entry:
            record[field] = entry[field]
    return record


def _carries_specifications(records: Sequence[dict[str, Any]]) -> bool:
    """True when the bulk answer described symbols, not just named them.

    The field list is shared with the other readers, so none of them can disagree
    about what counts as a specification.  Their *values* are not the test: a broker
    that reports no currency legs on a CFD still reported the field, and an empty leg
    is an answer -- "not a pair" -- while a missing key means the adapter only knows
    names.
    """

    return any(
        field in record for record in records for field in SYMBOL_EVIDENCE_FIELDS
    )


def records_from_terminal(terminal: Any, limit: int | None = None) -> SymbolRead:
    """Read the terminal's symbols *and* their specifications, read-only.

    The bulk call comes first: ``symbol_snapshots()`` returns every symbol with its
    specification fields from a single terminal call, so the whole reported list is
    classified and nothing depends on where a name sorts.  A wrapper that can only
    offer bare names falls back to one ``symbol_info`` read per name, and only that
    fallback is bounded by ``limit`` -- the returned :class:`SymbolRead` then says so,
    and the page reports how many names it did not reach.

    Nothing is written to the terminal, and no field is filled in from the ticker:
    a symbol whose specification the terminal does not return keeps its name and
    nothing else, so the page reports it as unverifiable.
    """

    if terminal is None:
        return SymbolRead()

    bulk_reader = getattr(terminal, "symbol_snapshots", None)
    if callable(bulk_reader):
        try:
            snapshots = bulk_reader()
        except Exception:
            snapshots = None
        records = [
            record
            for record in (
                _record_from(entry) for entry in (snapshots or ()) if isinstance(entry, Mapping)
            )
            if record is not None
        ]
        if records and _carries_specifications(records):
            return SymbolRead(
                records=tuple(records),
                reported=len(records),
                examined=len(records),
                bulk=True,
            )
        # A name-only or empty bulk answer is not a specification; fall through to
        # the per-name read rather than declaring every symbol unverifiable.

    try:
        names = [str(raw or "").strip() for raw in terminal.symbol_names()]
    except Exception:
        return SymbolRead()
    names = [name for name in names if name]
    reported = len(names)
    if limit is not None and limit >= 0:
        examined_names = names[:limit]
        reason = REASON_READ_CAPPED if len(examined_names) < reported else ""
    else:
        examined_names = names
        reason = ""

    records = []
    for name in examined_names:
        record: dict[str, Any] = {"name": name}
        try:
            info = _snapshot_of(terminal.symbol_info(name))
        except Exception:
            info = None
        for field in RECORD_FIELDS:
            if info is not None and field in info:
                record[field] = info[field]
        records.append(record)
    return SymbolRead(
        records=tuple(records),
        reported=reported,
        examined=len(records),
        bulk=False,
        reason=reason,
    )


def build_catalogue(
    records: Iterable[Mapping[str, Any]] | None,
    *,
    sample: bool = False,
    reported_total: int | None = None,
    examined: int | None = None,
    specs_reported: bool | None = None,
) -> SymbolCatalogue:
    """Classify the terminal's reported symbols.  Never invents an entry.

    ``records`` are the names whose specification was read; ``reported_total`` and
    ``examined`` carry the read's own counts when the adapter could not read the
    whole list, so the page can say what it did not reach instead of implying the
    list ended there.  ``reported`` on the result stays the number of names actually
    classified, so a crafted record cannot inflate the counts.

    ``specs_reported`` says whether the terminal described its symbols at all.  A
    read that came back in bulk did: every record carried ``digits``, ``point`` and
    the rest.  That is what makes it honest to say "no symbol here is a pair"
    instead of "these are names only".
    """

    rows = [dict(item) for item in (records or ()) if isinstance(item, Mapping)]
    reported: list[str] = []
    by_name: dict[str, dict[str, Any]] = {}
    pairs: list[SymbolChoice] = []
    excluded: list[dict[str, str]] = []
    facts_seen = 0

    for record in rows:
        name = _name_of(record)
        if not name or name in reported:
            continue
        reported.append(name)
        by_name[name] = record
        facts = _facts_for(record)
        if facts is None:
            excluded.append(
                {
                    "broker_symbol": name,
                    "pair": "",
                    "kind": UNCLASSIFIED,
                    "reason": REASON_NO_SPEC_REPORTED,
                }
            )
            continue
        facts_seen += 1
        choice = classify(facts)
        if choice.kind == FX_PAIR:
            pairs.append(choice)
        else:
            excluded.append(
                {
                    "broker_symbol": name,
                    "pair": choice.pair,
                    "kind": choice.kind if choice.kind else METAL,
                    "reason": choice.reason,
                }
            )

    pairs.sort(key=lambda item: (not item.major, not item.usable, item.pair, item.broker_symbol))
    usable = tuple(item for item in pairs if item.usable)

    def _skipped_reason(item: SymbolChoice) -> str:
        record = by_name.get(item.broker_symbol, {})
        if record.get("trade_mode") is None and not item.usable:
            return REASON_TRADE_MODE_NOT_REPORTED
        return item.reason

    skipped = tuple(
        {"broker_symbol": item.broker_symbol, "reason": _skipped_reason(item)}
        for item in pairs
        if not item.usable
    )

    verified = facts_seen > 0 or bool(specs_reported)
    if not reported:
        reason = "no_terminal_symbols"
    elif not verified:
        reason = "symbol_facts_not_reported"
    elif not pairs:
        reason = "no_verified_forex_pair"
    else:
        reason = ""

    examined_rows = len(reported) if examined is None else max(0, int(examined))
    examined_rows = min(examined_rows, len(reported))
    claimed = len(reported) if reported_total is None else max(len(reported), int(reported_total))
    unexamined = claimed - examined_rows

    return SymbolCatalogue(
        verified=verified,
        reason=reason,
        sample=bool(sample),
        reported=tuple(reported),
        pairs=tuple(pairs),
        excluded=tuple(excluded),
        select_all=tuple(item.broker_symbol for item in usable),
        skipped_by_select_all=skipped,
        counts={
            # What the terminal said it has, and how much of it was classified.
            # The two differ only when the adapter could not read specifications in
            # bulk, and then the difference is stated rather than hidden.
            "reported": claimed,
            "examined": examined_rows,
            "verified_forex_pairs": len(pairs),
            "selectable_for_all": len(usable),
            "excluded": len(excluded),
            "not_examined": max(0, unexamined),
        },
        examined=examined_rows,
    )


def catalogue_message(catalogue: SymbolCatalogue) -> str:
    """One sentence for the page, in the operator's words."""

    if not catalogue.available:
        return (
            "No symbols to show yet: the list comes from the terminal, once it is "
            "connected and reports its Market Watch."
        )
    counts = catalogue.counts
    lead = (
        f"{counts['reported']} symbol(s) reported, {catalogue.examined} examined"
        if catalogue.truncated
        else f"{counts['reported']} symbol(s) reported"
    )
    if not catalogue.verified:
        return (
            f"{lead} by name only. AXIOM will not guess which of these are currency pairs."
        )
    if not catalogue.pairs:
        trailing = REASON_NO_FOREX[0].upper() + REASON_NO_FOREX[1:]
        return f"{lead}: {trailing}."
    sentence = (
        f"{lead}: {counts['verified_forex_pairs']} verified forex pair(s), "
        f"{counts['excluded']} excluded."
    )
    if catalogue.truncated:
        sentence += (
            f" {counts['not_examined']} name(s) were not reached: {REASON_READ_CAPPED}."
        )
    return sentence


__all__ = [
    "MAX_EXCLUDED_SHOWN",
    "RECORD_FIELDS",
    "REASON_READ_CAPPED",
    "REASON_TRADE_MODE_NOT_REPORTED",
    "REASON_NO_FOREX",
    "REASON_NO_SPEC_REPORTED",
    "SPEC_FIELDS",
    "SymbolCatalogue",
    "SymbolRead",
    "build_catalogue",
    "catalogue_message",
    "records_from_terminal",
]
