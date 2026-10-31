"""Parameter variants of documented templates -- and how many were tried.

A "large library" here means *variants of a small number of documented templates*,
and every variant that is ever evaluated is written to the attempt ledger.  The
distinction matters: trying 200 combinations and reporting the best one is not 200
discoveries, it is one search with 200 attempts, and the multiple-testing control in
:mod:`axiom_forex.strategies.library.multiplicity` exists to say so out loud.

The grid is declared on each card and is **small on purpose**.  It is fixed before
any result is seen; changing it invalidates the attempts recorded under the old
grid, which is why the ledger stores the grid version with every attempt.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from axiom_forex.strategies.base import Signal, Strategy

from .cards import Capabilities, DEFAULT_CAPABILITIES, ParameterSpec, StrategyCard, card
from .rules import rule

#: A hard ceiling on the search.  The number is a limit on the search, not a
#: licence to fill it: 12 variants of one template is already a multiple-testing
#: problem that the scoring has to pay for.
MAX_VARIANTS_PER_CARD = 12

#: Bumped whenever any card's grid or rule changes.  Attempts recorded under an
#: older version are still readable, but they are not pooled with the new ones.
GRID_VERSION = "2026-10-31.1"


@dataclass(frozen=True)
class Variant:
    """One template plus one predeclared parameter set."""

    variant_id: str
    card_id: str
    parameters: Mapping[str, Any]
    grid_version: str = GRID_VERSION

    @property
    def card(self) -> StrategyCard:
        found = card(self.card_id)
        if found is None:  # pragma: no cover - ids come from CARDS
            raise KeyError(self.card_id)
        return found

    def audit_view(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "card_id": self.card_id,
            "parameters": {name: str(value) for name, value in self.parameters.items()},
            "grid_version": self.grid_version,
        }

    def strategy(self) -> "RuleStrategy":
        return RuleStrategy(self)


class RuleStrategy(Strategy):
    """Adapts a library rule to the engine's ``Strategy`` interface."""

    def __init__(self, variant: Variant) -> None:
        self.variant = variant
        self.card = variant.card
        self.parameters = dict(variant.parameters)
        self.name = variant.variant_id
        function = rule(self.card.rule)
        if function is None:  # pragma: no cover - guarded by the card
            raise ValueError(f"{self.card.card_id} has no rule")
        self._function = function
        # Enough history for the longest window the parameters ask for.
        numeric = [
            int(value)
            for value in self.parameters.values()
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
        ]
        self.history_limit = max([self.card.min_bars] + [value + 2 for value in numeric])

    def generate_signal(self, bars: Sequence[Any]) -> Signal:
        return self._function(bars, **self.parameters)


def _valid(card_item: StrategyCard, parameters: Mapping[str, Any]) -> bool:
    """Combinations that contradict the template are dropped, not evaluated."""

    values = {name: parameters.get(name) for name in parameters}
    if "fast_period" in values and "slow_period" in values:
        fast, slow = values["fast_period"], values["slow_period"]
        if not (isinstance(fast, int) and isinstance(slow, int) and 0 < fast < slow):
            return False
    if "lookback" in values and "exit_lookback" in values:
        if int(values["exit_lookback"]) >= int(values["lookback"]):
            return False
    return True


def _coerce(spec: ParameterSpec, value: Any) -> Any:
    if spec.kind == "int":
        return int(value)
    from decimal import Decimal

    return Decimal(str(value))


def variants_for(card_item: StrategyCard) -> tuple[Variant, ...]:
    """Every predeclared variant of one card, in a stable order."""

    if not card_item.evaluable:
        return ()
    grids: list[tuple[Any, ...]] = []
    for spec in card_item.parameters:
        values = spec.grid or (spec.default,)
        grids.append(tuple(_coerce(spec, value) for value in values))
    names = [spec.name for spec in card_item.parameters]
    built: list[Variant] = []
    for combination in itertools.product(*grids):
        parameters = dict(zip(names, combination))
        if not _valid(card_item, parameters):
            continue
        label = "-".join(f"{name}={value}" for name, value in parameters.items())
        built.append(
            Variant(
                variant_id=f"{card_item.card_id}::{label}",
                card_id=card_item.card_id,
                parameters=parameters,
            )
        )
        if len(built) >= MAX_VARIANTS_PER_CARD:
            break
    return tuple(built)


def all_variants(
    *, capabilities: Capabilities = DEFAULT_CAPABILITIES, cards: Iterable[StrategyCard] = ()
) -> tuple[Variant, ...]:
    """Every variant of every evaluable card this deployment can supply data for."""

    from .cards import CARDS

    items: list[Variant] = []
    for card_item in cards or CARDS:
        if not card_item.availability(capabilities)["evaluable"]:
            continue
        items.extend(variants_for(card_item))
    return tuple(items)


def count_variants() -> dict[str, Any]:
    """The honest count the page shows: templates, families, variants."""

    from .cards import CARDS, FAMILIES

    per_card = {item.card_id: len(variants_for(item)) for item in CARDS}
    return {
        "templates": len(CARDS),
        "families": len(FAMILIES),
        "evaluable_templates": sum(1 for item in CARDS if item.evaluable),
        "variants": sum(per_card.values()),
        "per_template": per_card,
        "grid_version": GRID_VERSION,
        "max_variants_per_template": MAX_VARIANTS_PER_CARD,
        "note": (
            "Variants are predeclared parameter combinations of documented templates, "
            "not independently researched strategies. The count above is what may be "
            "attempted, and every attempt is recorded."
        ),
    }


__all__ = [
    "GRID_VERSION",
    "MAX_VARIANTS_PER_CARD",
    "RuleStrategy",
    "Variant",
    "all_variants",
    "count_variants",
    "variants_for",
]
