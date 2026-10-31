"""The catalog: over a thousand reproducible candidates, deduplicated honestly.

"Large" has to mean something.  This module therefore reports three numbers
separately, and never adds them together:

``rule structures``   distinct (entry, exit, filter) *logics* with the parameter
                      values stripped out.  Two candidates that differ only in a
                      period are the same structure.
``signatures``        distinct (entry, exit, filter, parameter values) tuples.
                      This is the real search size: no two candidates in the
                      catalog share a signature, so a thousand candidates are a
                      thousand different tests.
``candidates``        the entries themselves, each with a stable id.

Generation is pure and deterministic: the same grid always produces the same
catalog, in the same order, with the same ids.  Nothing here runs, tunes, or
scores anything; it only declares what exists.

A candidate's id is ``cat-<16 hex>`` of its signature, so a renamed label can
never masquerade as a new candidate, and adding a candidate cannot renumber the
others.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

from .primitives import (
    ENTRIES,
    EXITS,
    FILTERS,
    Context,
    ExitKind,
    FilterKind,
    Kind,
    Params,
    PositionPolicy,
    entry_kind,
    exit_kind,
    filter_kind,
    parameter_signature,
)

#: Families as the report words them.  A candidate belongs to exactly one family.
FAMILIES: tuple[tuple[str, str], ...] = (
    ("trend", "Trend following"),
    ("momentum", "Momentum"),
    ("mean_reversion", "Mean reversion"),
    ("breakout", "Breakout"),
    ("volatility", "Volatility"),
    ("multi_timeframe", "Multi-timeframe"),
)

FAMILY_LABELS: dict[str, str] = dict(FAMILIES)

#: Bar lengths the catalog is evaluated on.  A candidate is offered on the
#: timeframes its family declares (see :func:`timeframes_for`).
TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "30m", "1h", "4h")

#: A candidate is evaluated only where at least this many bars exist, so a
#: 144-bar mean cannot be "tested" on a week of data.
MIN_BARS_PER_CANDIDATE = 600

#: The warm-up a candidate needs before its first decision, in bars.
def warmup_bars(entry: Kind, exit_kind: ExitKind, filter_kind: FilterKind, params: Sequence[Params]) -> int:
    longest = 0
    for mapping in params:
        for name, value in mapping.items():
            if isinstance(value, (int, float)) and float(value).is_integer():
                number = int(value)
                # Period-like parameters are warm-up; multipliers and hours are not.
                # Periods are warm-up.  A hold limit, a multiplier or a buffer is
                # not: it changes behaviour, not how many bars must be read first.
                if any(
                    token in name
                    for token in ("period", "fast", "slow", "mid", "lookback", "window", "k", "d", "squeeze")
                ):
                    longest = max(longest, number)
    if filter_kind.needs_coarse:
        longest = max(longest, 200 * int(params[2].get("ratio", 4)))
    return max(longest + 2, 8)


@dataclass(frozen=True)
class Candidate:
    """One declared strategy: stable id, plain-language logic, fixed parameters."""

    candidate_id: str
    entry: str
    exit: str
    filter: str
    family: str
    parameters: tuple[tuple[str, Params], ...]  # (kind, params) in entry/exit/filter order
    entry_text: str
    exit_text: str
    filter_text: str
    risk_text: str
    summary: str
    min_bars: int

    # -- identity ---------------------------------------------------------

    @property
    def signature(self) -> str:
        return signature_of(self.entry, self.exit, self.filter, self.parameter_map)

    @property
    def structure(self) -> str:
        """The logic without its parameter values."""

        return structure_of(self.entry, self.exit, self.filter)

    @property
    def parameter_map(self) -> dict[str, Params]:
        return {kind: dict(params) for kind, params in self.parameters}

    @property
    def entry_params(self) -> Params:
        return dict(self.parameters[0][1])

    @property
    def exit_params(self) -> Params:
        return dict(self.parameters[1][1])

    @property
    def filter_params(self) -> Params:
        return dict(self.parameters[2][1])

    @property
    def exit_kind(self) -> ExitKind:
        return exit_kind(self.exit)

    @property
    def filter_kind(self) -> FilterKind:
        return filter_kind(self.filter)

    @property
    def entry_kind(self) -> Kind:
        return entry_kind(self.entry)

    def policy(self) -> PositionPolicy:
        return self.exit_kind.policy(self.exit_params)

    def parameter_text(self) -> str:
        return "; ".join(
            f"{kind}: {parameter_signature(params)}" if params else f"{kind}: fixed"
            for kind, params in self.parameters
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "family": self.family,
            "family_label": FAMILY_LABELS.get(self.family, self.family),
            "structure": self.structure,
            "signature": self.signature,
            "entry": self.entry,
            "exit": self.exit,
            "filter": self.filter,
            "parameters": {kind: dict(params) for kind, params in self.parameters},
            "entry_text": self.entry_text,
            "exit_text": self.exit_text,
            "filter_text": self.filter_text,
            "risk": self.risk_text,
            "summary": self.summary,
            "min_bars": self.min_bars,
            "timeframes": list(timeframes_for(self)),
        }


def signature_of(entry: str, exit_name: str, filter_name: str, params: dict[str, Params]) -> str:
    """The canonical identity of one candidate: logic *and* parameters."""

    body = "|".join(
        [
            entry,
            exit_name,
            filter_name,
            parameter_signature(params.get("entry", {})),
            parameter_signature(params.get("exit", {})),
            parameter_signature(params.get("filter", {})),
        ]
    )
    return body


def structure_of(entry: str, exit_name: str, filter_name: str) -> str:
    """The logic with parameters stripped: what a reader calls "the strategy"."""

    return f"{entry}+{exit_name}+{filter_name}"


def candidate_id_for(signature: str) -> str:
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
    return f"cat-{digest[:16]}"


def family_for(entry: Kind, filter_kind: FilterKind) -> str:
    """Multi-timeframe when the filter is, otherwise the entry rule's own family."""

    if filter_kind.needs_coarse:
        return "multi_timeframe"
    return entry.family_hint


def timeframes_for(candidate: Candidate) -> tuple[str, ...]:
    """Which timeframes this candidate is offered on.

    Multi-timeframe candidates need a coarse bar that actually exists: a 12x
    ratio on a 4h series would be a two-day bar, which no session filter or
    short-horizon exit was designed around, so those combinations are dropped
    rather than evaluated meaninglessly.
    """

    if candidate.filter_kind.needs_coarse:
        ratio = int(candidate.filter_params.get("ratio", 4))
        seconds = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
        return tuple(name for name in TIMEFRAMES if seconds[name] * ratio <= 14400)
    return TIMEFRAMES


def _risk_text(entry: Kind, policy: PositionPolicy, filter_kind: FilterKind) -> str:
    parts = [
        "One position at a time per symbol; the opposing entry reverses it.",
        "Every position carries a position-level exit: " + policy.describe() + ".",
        "Size is the operator's fixed lot; ATR is used for exits, never for sizing.",
        "Session windows, holidays and swap cut-offs come from the broker's terms "
        "where they are known; a missing cost is labelled, never treated as zero.",
    ]
    if filter_kind.name == "session":
        parts.append("Outside the session window the candidate does not trade, and an "
                     "open position is still closed by its own exit rule.")
    if filter_kind.name == "htf_trend":
        parts.append("The higher-timeframe filter reads completed coarse bars only, so "
                     "it cannot see the bucket it is inside.")
    return " ".join(parts)


def build_candidates(
    *,
    entries: Sequence[Kind] = ENTRIES,
    exits: Sequence[ExitKind] = EXITS,
    filters: Sequence[FilterKind] = FILTERS,
) -> tuple[Candidate, ...]:
    """The whole declared grid, deduplicated by signature."""

    built: list[Candidate] = []
    seen: set[str] = set()
    for entry, exit_block, filter_block in itertools.product(entries, exits, filters):
        for entry_params, exit_params, filter_params in itertools.product(
            entry.variants(), exit_block.variants(), filter_block.variants()
        ):
            params = {
                "entry": dict(entry_params),
                "exit": dict(exit_params),
                "filter": dict(filter_params),
            }
            signature = signature_of(entry.name, exit_block.name, filter_block.name, params)
            if signature in seen:  # a duplicated combination is dropped, not counted
                continue
            seen.add(signature)
            policy = exit_block.policy(dict(exit_params))
            min_bars = warmup_bars(
                entry,
                exit_block,
                filter_block,
                (entry_params, exit_params, filter_params),
            )
            family = family_for(entry, filter_block)
            summary = (
                f"{entry.label} ({FAMILY_LABELS.get(family, family).lower()}) with "
                f"{exit_block.describe(dict(exit_params))}"
                + (
                    ""
                    if filter_block.name == "none"
                    else ", filtered by " + filter_block.describe(dict(filter_params))
                )
            )
            built.append(
                Candidate(
                    candidate_id=candidate_id_for(signature),
                    entry=entry.name,
                    exit=exit_block.name,
                    filter=filter_block.name,
                    family=family,
                    parameters=(
                        ("entry", dict(entry_params)),
                        ("exit", dict(exit_params)),
                        ("filter", dict(filter_params)),
                    ),
                    entry_text=entry.describe(dict(entry_params)),
                    exit_text=exit_block.describe(dict(exit_params)),
                    filter_text=filter_block.describe(dict(filter_params)),
                    risk_text=_risk_text(entry, policy, filter_block),
                    summary=summary,
                    min_bars=max(min_bars, 8),
                )
            )
    return tuple(built)


_CATALOG: tuple[Candidate, ...] | None = None


def catalog() -> tuple[Candidate, ...]:
    """The catalog, built once."""

    global _CATALOG
    if _CATALOG is None:
        _CATALOG = build_candidates()
    return _CATALOG


def by_id(candidate_id: str) -> Candidate | None:
    needle = str(candidate_id or "").strip()
    for item in catalog():
        if item.candidate_id == needle:
            return item
    return None


def by_structure(structure: str) -> tuple[Candidate, ...]:
    return tuple(item for item in catalog() if item.structure == structure)


def by_family(family: str) -> tuple[Candidate, ...]:
    return tuple(item for item in catalog() if item.family == family)


def summary_counts() -> dict[str, Any]:
    """The three numbers, kept apart on purpose."""

    items = catalog()
    structures = {item.structure for item in items}
    signatures = {item.signature for item in items}
    families: dict[str, int] = {}
    per_structure: dict[str, int] = {}
    for item in items:
        families[item.family] = families.get(item.family, 0) + 1
        per_structure[item.structure] = per_structure.get(item.structure, 0) + 1
    return {
        "candidates": len(items),
        "distinct_signatures": len(signatures),
        "distinct_rule_structures": len(structures),
        "entries": len(ENTRIES),
        "exits": len(EXITS),
        "filters": len(FILTERS),
        "families": {
            key: {"label": FAMILY_LABELS.get(key, key), "candidates": families.get(key, 0)}
            for key, _label in FAMILIES
        },
        "parameter_variants_per_structure": (
            {
                "min": min(per_structure.values()),
                "max": max(per_structure.values()),
                "distinct_counts": sorted(set(per_structure.values())),
            }
            if per_structure
            else {}
        ),
        "note": (
            "Rule structures count distinct (entry, exit, filter) logics with the "
            "parameter values removed. Candidates are those structures at one "
            "predeclared parameter set each; no two share a signature, so no count "
            "here is padded with a renamed copy."
        ),
    }


def filter_candidates(
    items: Iterable[Candidate],
    *,
    families: Sequence[str] = (),
    symbol_ok: bool = True,
    timeframe_ok: Sequence[str] = (),
) -> tuple[Candidate, ...]:
    """The subset a deployment can actually run, in catalog order."""

    allowed_families = set(families)
    allowed_timeframes = set(timeframe_ok)
    chosen: list[Candidate] = []
    for item in items:
        if allowed_families and item.family not in allowed_families:
            continue
        if not symbol_ok:
            continue
        if allowed_timeframes and not (set(timeframes_for(item)) & allowed_timeframes):
            continue
        chosen.append(item)
    return tuple(chosen)


__all__ = [
    "Candidate",
    "FAMILIES",
    "FAMILY_LABELS",
    "MIN_BARS_PER_CANDIDATE",
    "TIMEFRAMES",
    "build_candidates",
    "by_family",
    "by_id",
    "by_structure",
    "candidate_id_for",
    "catalog",
    "family_for",
    "filter_candidates",
    "signature_of",
    "structure_of",
    "summary_counts",
    "timeframes_for",
    "warmup_bars",
]
