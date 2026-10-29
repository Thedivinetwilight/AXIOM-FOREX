"""Aligned multi-pair, multi-timeframe grid construction.

Different pairs trade different calendars and can be supplied at different
timeframes.  The evaluation therefore runs on one explicit UTC grid: every pair
is projected onto the same slot timeline, and each slot is labelled for that
pair as covered, missing-while-open, or closed-by-its-own-calendar.  Missing and
closed runs are reported as explicit intervals; nothing is interpolated and no
pair is assumed to be open just because another pair is.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.calendar.trading import TradingCalendar

from .contracts import (
    AlignmentResult,
    CoverageInterval,
    PairCoverage,
    PairSeries,
)

STATE_COVERED = "COVERED"
STATE_MISSING = "MISSING_WHILE_OPEN"
STATE_CLOSED = "CLOSED_BY_CALENDAR"

MAX_GRID_SLOTS = 200_000
MAX_INTERVALS_PER_STATE = 40


def _floor_to_grid(moment: datetime, grid_seconds: int) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seconds = (moment - epoch).total_seconds()
    floored = int(seconds // grid_seconds) * grid_seconds
    return epoch + timedelta(seconds=floored)


def _slot_index(slot: datetime, grid_start: datetime, grid_seconds: int) -> int:
    return int((slot - grid_start).total_seconds()) // grid_seconds


def _intervals(
    states: Sequence[str], slots: Sequence[datetime], wanted: str, grid_seconds: int
) -> tuple[tuple[CoverageInterval, ...], int]:
    """Compress consecutive slots in ``wanted`` state into explicit intervals."""

    intervals: list[CoverageInterval] = []
    total = 0
    start_index: int | None = None
    for index, state in enumerate(list(states) + ["__END__"]):
        if state == wanted:
            if start_index is None:
                start_index = index
            total += 1
            continue
        if start_index is not None:
            if len(intervals) < MAX_INTERVALS_PER_STATE:
                count = index - start_index
                intervals.append(
                    CoverageInterval(
                        start=slots[start_index],
                        end=slots[index - 1] + timedelta(seconds=grid_seconds),
                        slots=count,
                        state=wanted,
                    )
                )
            start_index = None
    return tuple(intervals), total


def _pair_coverage(
    series: PairSeries,
    *,
    slots: Sequence[datetime],
    grid_start: datetime,
    grid_seconds: int,
    calendar: TradingCalendar,
) -> tuple[PairCoverage, list[str]]:
    issues: list[str] = []
    timeframe_seconds = series.timeframe_seconds
    if timeframe_seconds <= 0:
        issues.append(f"invalid_timeframe_seconds:{series.pair}")
        timeframe_seconds = grid_seconds
    if timeframe_seconds % grid_seconds != 0:
        issues.append(f"timeframe_not_grid_multiple:{series.pair}")
    covered: set[int] = set()
    total_slots = len(slots)
    for bar in series.bars:
        start_index = _slot_index(bar.timestamp, grid_start, grid_seconds)
        end_index = _slot_index(bar.end_at, grid_start, grid_seconds)
        for index in range(max(start_index, 0), min(end_index, total_slots)):
            covered.add(index)
    states: list[str] = []
    market_open_slots = 0
    covered_slots = 0
    closed_slots = 0
    out_of_session_bars = 0
    for index, slot in enumerate(slots):
        state = calendar.status(series.pair, slot)
        if state.is_open:
            market_open_slots += 1
            if index in covered:
                states.append(STATE_COVERED)
                covered_slots += 1
            else:
                states.append(STATE_MISSING)
        else:
            closed_slots += 1
            states.append(STATE_CLOSED)
    for bar in series.bars:
        if not calendar.status(series.pair, bar.timestamp).is_open:
            out_of_session_bars += 1
    missing_intervals, missing_slots = _intervals(
        states, slots, STATE_MISSING, grid_seconds
    )
    closed_intervals, _ = _intervals(states, slots, STATE_CLOSED, grid_seconds)
    ratio = (
        Decimal(covered_slots) / Decimal(market_open_slots)
        if market_open_slots
        else Decimal(0)
    )
    quality_findings = tuple(
        finding.audit_view() for finding in getattr(series.quality, "findings", ())
    )
    covered_times = [
        slots[index] for index in sorted(covered) if 0 <= index < total_slots
    ]
    return (
        PairCoverage(
            pair=series.pair,
            timeframe=series.timeframe,
            grid_seconds=grid_seconds,
            grid_slots=total_slots,
            market_open_slots=market_open_slots,
            covered_slots=covered_slots,
            missing_slots=missing_slots,
            closed_slots=closed_slots,
            coverage_ratio=ratio,
            first_covered_at=covered_times[0] if covered_times else None,
            last_covered_at=covered_times[-1] + timedelta(seconds=grid_seconds)
            if covered_times
            else None,
            missing_intervals=missing_intervals,
            closed_intervals=closed_intervals,
            out_of_session_bars=out_of_session_bars,
            quality_classification=str(
                getattr(series.quality, "classification", "") or ""
            ),
            quality_findings=quality_findings,
        ),
        issues,
    )


def build_alignment(
    *,
    series: Mapping[str, PairSeries],
    calendar: TradingCalendar,
    grid_seconds: int,
) -> AlignmentResult:
    """Project every pair onto one UTC grid and report explicit coverage."""

    issues: list[str] = []
    if not series:
        return AlignmentResult(
            grid_seconds=grid_seconds,
            grid_start=None,
            grid_end=None,
            grid_slots=0,
            aligned_slots=0,
            all_pairs_open_slots=0,
            partly_open_slots=0,
            closed_slots=0,
            pairs=(),
            issues=("no_pairs_supplied",),
        )
    if not isinstance(grid_seconds, int) or isinstance(grid_seconds, bool) or grid_seconds <= 0:
        return AlignmentResult(
            grid_seconds=grid_seconds if isinstance(grid_seconds, int) else 0,
            grid_start=None,
            grid_end=None,
            grid_slots=0,
            aligned_slots=0,
            all_pairs_open_slots=0,
            partly_open_slots=0,
            closed_slots=0,
            pairs=(),
            issues=("invalid_grid_seconds",),
        )
    calendar_validation = calendar.validate()
    if not calendar_validation.valid:
        issues.append("invalid_calendar:" + calendar_validation.reason)
    # A pair whose data was removed entirely (for example by a stress scenario)
    # is reported as an explicit issue; it never crashes the grid and never
    # borrows another pair's timestamps.
    empty_pairs = sorted(pair for pair, item in series.items() if not item.bars)
    for pair in empty_pairs:
        issues.append(f"no_bars_for_pair:{pair}")
    covered_series = [item for item in series.values() if item.bars]
    if not covered_series:
        return AlignmentResult(
            grid_seconds=grid_seconds,
            grid_start=None,
            grid_end=None,
            grid_slots=0,
            aligned_slots=0,
            all_pairs_open_slots=0,
            partly_open_slots=0,
            closed_slots=0,
            pairs=(),
            issues=tuple(issues + ["no_bars_in_any_series"]),
        )
    starts = [item.first_timestamp for item in covered_series]
    ends = [item.last_timestamp for item in covered_series]
    grid_start = _floor_to_grid(min(starts), grid_seconds)
    grid_end_raw = max(ends)
    grid_slots = int(
        (grid_end_raw - grid_start).total_seconds() + grid_seconds - 1
    ) // grid_seconds
    if grid_slots <= 0:
        issues.append("empty_grid")
        grid_slots = 0
    if grid_slots > MAX_GRID_SLOTS:
        issues.append(f"grid_too_large:{grid_slots}")
        grid_slots = 0
    slots = [
        grid_start + timedelta(seconds=index * grid_seconds)
        for index in range(grid_slots)
    ]
    pairs: list[PairCoverage] = []
    per_pair_states: dict[str, list[str]] = {}
    for pair in sorted(series):
        coverage, pair_issues = _pair_coverage(
            series[pair],
            slots=slots,
            grid_start=grid_start,
            grid_seconds=grid_seconds,
            calendar=calendar,
        )
        pairs.append(coverage)
        issues.extend(pair_issues)
        # Recompute compact per-slot states for aggregate counts.
        covered = set()
        for bar in series[pair].bars:
            start_index = _slot_index(bar.timestamp, grid_start, grid_seconds)
            end_index = _slot_index(bar.end_at, grid_start, grid_seconds)
            for index in range(max(start_index, 0), min(end_index, grid_slots)):
                covered.add(index)
        per_pair_states[pair] = [
            (
                "COVERED"
                if index in covered
                else "OPEN" if calendar.status(pair, slot).is_open else "CLOSED"
            )
            for index, slot in enumerate(slots)
        ]
    aligned_slots = 0
    all_pairs_open_slots = 0
    partly_open_slots = 0
    closed_slots = 0
    for index in range(grid_slots):
        open_pairs = sum(
            1 for pair in per_pair_states if per_pair_states[pair][index] != "CLOSED"
        )
        if open_pairs == 0:
            closed_slots += 1
        else:
            if open_pairs == len(per_pair_states):
                all_pairs_open_slots += 1
            else:
                partly_open_slots += 1
        if any(
            per_pair_states[pair][index] == "COVERED" for pair in per_pair_states
        ):
            aligned_slots += 1
    return AlignmentResult(
        grid_seconds=grid_seconds,
        grid_start=grid_start if grid_slots else None,
        grid_end=(
            slots[-1] + timedelta(seconds=grid_seconds) if slots else None
        ),
        grid_slots=grid_slots,
        aligned_slots=aligned_slots,
        all_pairs_open_slots=all_pairs_open_slots,
        partly_open_slots=partly_open_slots,
        closed_slots=closed_slots,
        pairs=tuple(pairs),
        issues=tuple(issues),
    )


def coverage_indexes(
    *,
    series: PairSeries,
    calendar: TradingCalendar,
    grid_start: datetime,
    grid_seconds: int,
    slots: Sequence[datetime],
) -> tuple[list[bool], list[bool]]:
    """Return ``(covered, open)`` slot flags for one pair (used by metrics)."""

    covered = [False] * len(slots)
    total = len(slots)
    for bar in series.bars:
        start_index = _slot_index(bar.timestamp, grid_start, grid_seconds)
        end_index = _slot_index(bar.end_at, grid_start, grid_seconds)
        for index in range(max(start_index, 0), min(end_index, total)):
            covered[index] = True
    open_flags = [calendar.status(series.pair, slot).is_open for slot in slots]
    return covered, open_flags


def grid_slots(
    start: datetime, end: datetime, grid_seconds: int
) -> tuple[datetime, ...]:
    """Deterministic slot list for a closed/open window; used by split metrics."""

    slots: list[datetime] = []
    cursor = _floor_to_grid(start, grid_seconds)
    if cursor < start:
        cursor += timedelta(seconds=grid_seconds)
    while cursor < end:
        slots.append(cursor)
        cursor += timedelta(seconds=grid_seconds)
    return tuple(slots)


def slot_of(moment: datetime, grid_start: datetime, grid_seconds: int) -> int:
    return _slot_index(moment, grid_start, grid_seconds)


def coverage_views(result: AlignmentResult) -> dict[str, Any]:
    """Compact operator summary of the alignment result."""

    return {
        "grid_seconds": result.grid_seconds,
        "grid_start": result.grid_start,
        "grid_end": result.grid_end,
        "grid_slots": result.grid_slots,
        "aligned_slots": result.aligned_slots,
        "all_pairs_open_slots": result.all_pairs_open_slots,
        "partly_open_slots": result.partly_open_slots,
        "closed_slots": result.closed_slots,
        "pairs": {
            coverage.pair: {
                "timeframe": coverage.timeframe,
                "coverage_ratio": coverage.coverage_ratio,
                "missing_slots": coverage.missing_slots,
                "missing_intervals": len(coverage.missing_intervals),
                "closed_intervals": len(coverage.closed_intervals),
                "out_of_session_bars": coverage.out_of_session_bars,
                "quality": coverage.quality_classification,
            }
            for coverage in result.pairs
        },
        "issues": list(result.issues),
    }


def pair_phase_coverage(
    *,
    series: PairSeries,
    calendar: TradingCalendar,
    start: datetime,
    end: datetime,
    grid_seconds: int,
) -> PairCoverage:
    """Coverage for one pair restricted to a single evaluation phase window."""

    grid_start = _floor_to_grid(start, grid_seconds)
    slots = grid_slots(start, end, grid_seconds)
    coverage, _ = _pair_coverage(
        series,
        slots=slots,
        grid_start=grid_start,
        grid_seconds=grid_seconds,
        calendar=calendar,
    )
    return coverage
