from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class WalkForwardFold:
    index: int
    train: tuple[T, ...]
    validation: tuple[T, ...]
    test: tuple[T, ...]


def walk_forward(
    items: Sequence[T],
    *,
    train_size: int,
    validation_size: int,
    test_size: int,
    step_size: int | None = None,
) -> Iterator[WalkForwardFold]:
    if min(train_size, validation_size, test_size) <= 0:
        raise ValueError("All walk-forward windows must be positive")
    step = step_size or test_size
    if step <= 0:
        raise ValueError("step_size must be positive")
    start = 0
    index = 0
    total = len(items)
    while start + train_size + validation_size + test_size <= total:
        train_end = start + train_size
        validation_end = train_end + validation_size
        test_end = validation_end + test_size
        yield WalkForwardFold(
            index,
            tuple(items[start:train_end]),
            tuple(items[train_end:validation_end]),
            tuple(items[validation_end:test_end]),
        )
        start += step
        index += 1
