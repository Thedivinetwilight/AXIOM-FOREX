from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Split:
    train: tuple[T, ...]
    validation: tuple[T, ...]
    test: tuple[T, ...]

    def counts(self) -> dict[str, int]:
        return {
            "train": len(self.train),
            "validation": len(self.validation),
            "test": len(self.test),
        }


def chronological_split(
    items: Sequence[T], fractions: Sequence[Decimal | float | int] = (0.6, 0.2, 0.2)
) -> Split:
    if len(fractions) != 3:
        raise ValueError("Exactly three chronological fractions are required")
    values = [Decimal(str(value)) for value in fractions]
    if any(value < 0 for value in values) or sum(values, Decimal(0)) != Decimal(1):
        raise ValueError("Fractions must be nonnegative and sum to one")
    total = len(items)
    train_end = int(total * float(values[0]))
    validation_end = train_end + int(total * float(values[1]))
    return Split(
        tuple(items[:train_end]),
        tuple(items[train_end:validation_end]),
        tuple(items[validation_end:]),
    )


class LockedTest:
    def __init__(self, items: Sequence[T]):
        self._items = tuple(items)
        self._used = False

    def use_once(self) -> tuple[T, ...]:
        if self._used:
            raise ValueError("Locked test has already been used")
        self._used = True
        return self._items

    @property
    def used(self) -> bool:
        return self._used
