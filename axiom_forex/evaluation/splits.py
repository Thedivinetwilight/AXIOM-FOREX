"""Chronological splits, walk-forward folds, and out-of-sample window custody.

All boundaries are derived from the *aligned* timeline of bar timestamps across
pairs, so train/validation/test separation is chronological in time and shared
by every pair.  A pair can never contribute a bar from a later phase to an
earlier one, and a test window can only be consumed once per dataset
fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from axiom_forex.validation.split import chronological_split
from axiom_forex.validation.walk_forward import walk_forward

from .contracts import (
    PHASE_TEST,
    PHASE_TRAIN,
    PHASE_VALIDATION,
    EvaluationError,
    PairSeries,
    PhaseWindow,
    TestWindowReuseError,
)


@dataclass(frozen=True)
class SplitPlan:
    """One chronological train/validation/test separation over the timeline."""

    timeline: tuple[datetime, ...]
    phases: Mapping[str, PhaseWindow]

    @property
    def ok(self) -> bool:
        return all(window.end > window.start for window in self.phases.values())

    def counts(self) -> dict[str, int]:
        return {label: len(self.timeline) for label in self.phases}


@dataclass(frozen=True)
class FoldPlan:
    fold: int
    train: PhaseWindow
    validation: PhaseWindow
    test: PhaseWindow

    def audit_view(self) -> dict[str, object]:
        return {
            "fold": self.fold,
            "train": self.train.audit_view(),
            "validation": self.validation.audit_view(),
            "test": self.test.audit_view(),
        }


def aligned_timeline(series: Mapping[str, PairSeries]) -> tuple[datetime, ...]:
    """Sorted union of every pair's completed-bar timestamps."""

    stamps: set[datetime] = set()
    for item in series.values():
        stamps.update(bar.timestamp for bar in item.bars)
    return tuple(sorted(stamps))


def _window(label: str, start: datetime, end: datetime, fold: int = 0) -> PhaseWindow:
    return PhaseWindow(label=label, start=start, end=end, fold=fold)


def chronological_plan(
    timeline: Sequence[datetime],
    *,
    fractions: Sequence[Decimal | float | int],
    grid_seconds: int,
) -> SplitPlan:
    """Split the aligned timeline by time; no item is shared between phases."""

    stamps = tuple(timeline)
    if len(stamps) < 3:
        raise EvaluationError("timeline_too_short_for_split")
    split = chronological_split(stamps, fractions)
    if not split.train or not split.validation or not split.test:
        raise EvaluationError("split_phase_empty")
    train_start = split.train[0]
    validation_start = split.validation[0]
    test_start = split.test[0]
    test_end = split.test[-1] + timedelta(seconds=grid_seconds)
    return SplitPlan(
        timeline=stamps,
        phases={
            PHASE_TRAIN: _window(PHASE_TRAIN, train_start, validation_start),
            PHASE_VALIDATION: _window(PHASE_VALIDATION, validation_start, test_start),
            PHASE_TEST: _window(PHASE_TEST, test_start, test_end),
        },
    )


def walk_forward_plans(
    timeline: Sequence[datetime],
    *,
    train_size: int,
    validation_size: int,
    test_size: int,
    step: int | None,
    grid_seconds: int,
) -> tuple[FoldPlan, ...]:
    """Rolling chronological folds with non-overlapping test windows."""

    stamps = tuple(timeline)
    folds: list[FoldPlan] = []
    for fold in walk_forward(
        stamps,
        train_size=train_size,
        validation_size=validation_size,
        test_size=test_size,
        step_size=step,
    ):
        folds.append(
            FoldPlan(
                fold=fold.index,
                train=_window(PHASE_TRAIN, fold.train[0], fold.validation[0], fold.index),
                validation=_window(
                    PHASE_VALIDATION, fold.validation[0], fold.test[0], fold.index
                ),
                test=_window(
                    PHASE_TEST,
                    fold.test[0],
                    fold.test[-1] + timedelta(seconds=grid_seconds),
                    fold.index,
                ),
            )
        )
    return tuple(folds)


def _bar_content_digest(item: PairSeries) -> str:
    """SHA-256 over the *bars themselves*, not over a label or an import id."""

    digest = hashlib.sha256()
    for bar in item.bars:
        digest.update(
            "|".join(
                (
                    item.pair,
                    item.timeframe,
                    str(bar.timestamp.isoformat()),
                    str(bar.duration_seconds),
                    str(bar.bid.open),
                    str(bar.bid.high),
                    str(bar.bid.low),
                    str(bar.bid.close),
                    str(bar.ask.open),
                    str(bar.ask.high),
                    str(bar.ask.low),
                    str(bar.ask.close),
                )
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def dataset_fingerprint(series: Mapping[str, PairSeries]) -> str:
    """Stable, content-based fingerprint of the evaluated datasets.

    The fingerprint covers the bars themselves (and the instrument terms they
    were evaluated under), deliberately *not* the declared data class, source
    label, or import id.  Identical bars therefore keep the same identity even
    when they are re-imported or re-labelled, so a consumed out-of-sample
    window cannot be re-tested by changing a label; any change to a single
    price or timestamp produces a different fingerprint.
    """

    rows = []
    for pair in sorted(series):
        item = series[pair]
        rows.append(
            {
                "pair": item.pair,
                "timeframe": item.timeframe,
                "spec_version": getattr(item.spec, "spec_version", ""),
                "bars": len(item.bars),
                "first": (
                    item.first_timestamp.isoformat()
                    if item.first_timestamp is not None
                    else ""
                ),
                "last": (
                    item.last_timestamp.isoformat()
                    if item.last_timestamp is not None
                    else ""
                ),
                "content_sha256": _bar_content_digest(item),
            }
        )
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TestWindowRegistry:
    """In-process custody of consumed out-of-sample windows.

    A window is keyed by dataset fingerprint and exact time bounds.  Reusing the
    same window (for example running the same test split twice, or configuring a
    walk-forward step smaller than the test window) raises
    :class:`TestWindowReuseError` instead of silently re-testing.
    """

    def __init__(self) -> None:
        self._claimed: dict[str, tuple[datetime, datetime]] = {}

    @property
    def claimed(self) -> tuple[str, ...]:
        return tuple(sorted(self._claimed))

    def reserve(self, *, fingerprint: str, start: datetime, end: datetime) -> str:
        key = f"{fingerprint}:{start.isoformat()}:{end.isoformat()}"
        if key in self._claimed:
            raise TestWindowReuseError("test_window_already_used:" + key)
        if end <= start:
            raise EvaluationError("empty_test_window")
        for claimed_key, (claimed_start, claimed_end) in self._claimed.items():
            if not claimed_key.startswith(f"{fingerprint}:"):
                continue
            # Any overlap re-tests the same out-of-sample bars, even when the
            # bounds are not identical (e.g. a step smaller than the test size).
            if start < claimed_end and claimed_start < end:
                raise TestWindowReuseError(
                    "test_window_overlaps_used_window:" + claimed_key
                )
        self._claimed[key] = (start, end)
        return key

    def is_claimed(self, *, fingerprint: str, start: datetime, end: datetime) -> bool:
        return (
            f"{fingerprint}:{start.isoformat()}:{end.isoformat()}" in self._claimed
        )

    def audit_view(self) -> dict[str, object]:
        return {"claimed_count": len(self._claimed), "claimed": list(self.claimed)}


def windows_overlap(first: PhaseWindow, second: PhaseWindow) -> bool:
    return first.start < second.end and second.start < first.end


def assert_disjoint(windows: Iterable[PhaseWindow]) -> None:
    items = tuple(windows)
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            if windows_overlap(first, second):
                raise EvaluationError("overlapping_phase_windows")
