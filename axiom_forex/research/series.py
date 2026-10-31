"""The research price series: a year of broker bars in the shape a sweep needs.

One year of one-minute bars is roughly 370,000 records.  Building a
``ForexQuoteBar`` -- six ``Decimal`` prices, a validation, a frozen dataclass --
for every one of them, for every timeframe, is both slow and memory-hungry, and
the sweep needs to touch every bar for every variant.

So the research path keeps a compact, columnar series: floats for arithmetic and
the timestamp array for ordering.  The two-sided prices are:

* **bid** -- the broker's own OHLC from its rate history;
* **ask** -- bid plus the *quoted spread the bar itself carries*, in points.

That second line is the whole honesty of this module.  MT5 rate history is
bid-only; it also reports, per bar, the spread that applied.  Adding the reported
spread to the reported bid reproduces the two sides the broker quoted at that
bar's close without inventing a number.  It is still an approximation inside the
bar -- the spread moved -- so every result built on it says so.

Prices are floats here.  That is a deliberate, bounded choice: it is the research
path only, and the live and paper lanes keep their ``Decimal`` bars.  A signal
computed on floats that lands within ``TIE_EPSILON`` of a rule's threshold is not
trusted: the sweep recomputes that bar with the real rule on real bars (see
:mod:`axiom_forex.research.fastrules`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

BID_PLUS_SPREAD = "BROKER_RATES_BID_PLUS_QUOTED_SPREAD"

#: A relative distance below which a computed comparison is not trusted.
TIE_EPSILON = 1e-10


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(
            tzinfo=timezone.utc
        )
    return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


@dataclass(frozen=True)
class RateRow:
    """One bid-only rate record, as the terminal reported it."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    spread_points: int
    tick_volume: int = 0
    source: str = "mt5_copy_rates_range"

    def audit_view(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "spread_points": self.spread_points,
            "source": self.source,
        }


def rows_from_rates(rows: Iterable[Mapping[str, Any]]) -> tuple[RateRow, ...]:
    """Convert the terminal's raw rate dicts, dropping nothing silently.

    A row without a usable timestamp or an inconsistent OHLC range is *skipped*,
    and the count of skipped rows is reported by the coverage audit.  It is never
    repaired, and never turned into a zero-length bar.
    """

    out: list[RateRow] = []
    for row in rows:
        when = _utc(row.get("timestamp"))
        values = [_float(row.get(name)) for name in ("open", "high", "low", "close")]
        if when is None or any(value is None for value in values):
            out.append(
                RateRow(
                    timestamp=when or datetime(1970, 1, 1, tzinfo=timezone.utc),
                    open=0.0,
                    high=0.0,
                    low=0.0,
                    close=0.0,
                    spread_points=-1,
                    source="UNUSABLE",
                )
            )
            continue
        open_, high, low, close = (float(value) for value in values)  # type: ignore[arg-type]
        if not (low <= min(open_, close) and high >= max(open_, close)) or low <= 0:
            out.append(
                RateRow(
                    timestamp=when,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    spread_points=-1,
                    source="UNUSABLE",
                )
            )
            continue
        spread = row.get("spread_points")
        try:
            spread_points = max(0, int(spread))
        except (TypeError, ValueError):
            spread_points = 0
        try:
            volume = int(row.get("tick_volume") or 0)
        except (TypeError, ValueError):
            volume = 0
        out.append(
            RateRow(
                timestamp=when,
                open=open_,
                high=high,
                low=low,
                close=close,
                spread_points=spread_points,
                tick_volume=max(0, volume),
                source=str(row.get("source") or "mt5_copy_rates_range"),
            )
        )
    return tuple(out)


@dataclass
class Series:
    """One symbol/timeframe as columnar float arrays, in the two-sided shape."""

    symbol: str
    pair: str
    timeframe: str
    bar_seconds: int
    point: float
    timestamps: tuple[datetime, ...] = ()
    o: tuple[float, ...] = ()
    h: tuple[float, ...] = ()
    l: tuple[float, ...] = ()
    c: tuple[float, ...] = ()
    spread: tuple[float, ...] = ()
    volume: tuple[int, ...] = ()
    rows: int = 0
    skipped: int = 0
    price_basis: str = BID_PLUS_SPREAD
    source: str = ""
    notes: tuple[str, ...] = ()
    mid: list[float] = field(default_factory=list, repr=False)
    mid_high: list[float] = field(default_factory=list, repr=False)
    mid_low: list[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        # Mid open is not needed by any rule; mid close, high and low are.  The
        # mid of a two-sided bar is the bid plus half the quoted spread.
        self.mid = [
            (self.c[index] * 2 + self.spread[index]) / 2.0 for index in range(len(self.c))
        ]
        self.mid_high = [(self.h[index] * 2 + self.spread[index]) / 2.0 for index in range(len(self.h))]
        self.mid_low = [(self.l[index] * 2 + self.spread[index]) / 2.0 for index in range(len(self.l))]

    def __len__(self) -> int:
        return len(self.timestamps)

    # -- pricing ---------------------------------------------------------

    def ask(self, index: int, side: str = "c") -> float:
        base = getattr(self, side)[index]
        return base + self.spread[index]

    def bid(self, index: int, side: str = "c") -> float:
        return getattr(self, side)[index]

    # -- fallback window -------------------------------------------------

    def exact_window(self, index: int, count: int) -> list[Any]:
        """Build real ``ForexQuoteBar`` objects for one window.

        Used only when the fast path lands on a threshold and refuses to decide.
        The prices are the same numbers the series holds, so the rule sees exactly
        what the sweep saw.
        """

        from app.data.models import ForexQuoteBar, QuoteSideBar

        start = max(0, index - count + 1)
        bars: list[Any] = []
        for position in range(start, index + 1):
            bid_side = QuoteSideBar(
                open=Decimal(str(self.o[position])),
                high=Decimal(str(self.h[position])),
                low=Decimal(str(self.l[position])),
                close=Decimal(str(self.c[position])),
            )
            ask_side = QuoteSideBar(
                open=Decimal(str(self.o[position] + self.spread[position])),
                high=Decimal(str(self.h[position] + self.spread[position])),
                low=Decimal(str(self.l[position] + self.spread[position])),
                close=Decimal(str(self.c[position] + self.spread[position])),
            )
            stamp = self.timestamps[position]
            bars.append(
                ForexQuoteBar(
                    pair=self.pair,
                    timeframe=self.timeframe,
                    timestamp=stamp,
                    duration_seconds=self.bar_seconds,
                    bid=bid_side,
                    ask=ask_side,
                    source=self.source or BID_PLUS_SPREAD,
                    quote_count=max(0, int(self.volume[position]) if self.volume else 0),
                    last_quote_at=stamp + timedelta(seconds=self.bar_seconds - 1),
                    complete=True,
                )
            )
        return bars

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "bar_seconds": self.bar_seconds,
            "bars": len(self),
            "first_at": self.timestamps[0].isoformat() if self.timestamps else "",
            "last_at": self.timestamps[-1].isoformat() if self.timestamps else "",
            "price_basis": self.price_basis,
            "skipped_rows": self.skipped,
            "source": self.source,
            "notes": list(self.notes),
        }


def build_series(
    symbol: str,
    pair: str,
    timeframe: str,
    bar_seconds: int,
    point: Decimal | float | None,
    rows: Sequence[RateRow],
) -> Series:
    """Turn rate rows into a series, keeping only in-order, usable records."""

    usable = [row for row in rows if row.source != "UNUSABLE"]
    skipped = len(rows) - len(usable)
    usable.sort(key=lambda row: row.timestamp)
    deduped: list[RateRow] = []
    duplicates = 0
    for row in usable:
        if deduped and deduped[-1].timestamp == row.timestamp:
            duplicates += 1
            continue
        deduped.append(row)
    point_value = _float(point) or 0.0
    notes = []
    if point_value <= 0:
        notes.append(
            "The symbol's point size was not available, so the quoted spread could not "
            "be added to the bid. Prices are bid-only in this series."
        )
    if skipped:
        notes.append(f"{skipped} rate record(s) were unusable and were not used.")
    if duplicates:
        notes.append(f"{duplicates} duplicate timestamp(s) were dropped.")
    timestamps = tuple(row.timestamp for row in deduped)
    spreads = tuple(
        (row.spread_points * point_value) if row.spread_points >= 0 and point_value > 0 else 0.0
        for row in deduped
    )
    return Series(
        symbol=symbol,
        pair=pair,
        timeframe=timeframe,
        bar_seconds=bar_seconds,
        point=point_value,
        timestamps=timestamps,
        o=tuple(row.open for row in deduped),
        h=tuple(row.high for row in deduped),
        l=tuple(row.low for row in deduped),
        c=tuple(row.close for row in deduped),
        spread=spreads,
        volume=tuple(row.tick_volume for row in deduped),
        rows=len(rows),
        skipped=skipped,
        source=str(deduped[0].source) if deduped else "",
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class Coverage:
    """Whether a series is enough to evaluate, and by how much it falls short."""

    symbol: str
    timeframe: str
    bars: int
    first_at: str
    last_at: str
    expected_bars: int
    coverage_fraction: float
    gaps: int
    weekend_gaps: int
    largest_gap_seconds: float
    duplicates: int
    usable: bool
    reason: str
    price_basis: str = BID_PLUS_SPREAD
    notes: tuple[str, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "expected_bars": self.expected_bars,
            "coverage_fraction": round(self.coverage_fraction, 4),
            "gaps": self.gaps,
            "weekend_gaps": self.weekend_gaps,
            "largest_gap_seconds": self.largest_gap_seconds,
            "duplicates": self.duplicates,
            "usable": self.usable,
            "reason": self.reason,
            "price_basis": self.price_basis,
            "notes": list(self.notes),
        }


def audit_coverage(
    series: Series,
    *,
    window_days: int,
    min_bars: int = 500,
    min_coverage: float = 0.90,
    now: datetime | None = None,
) -> Coverage:
    """Check timestamps, ordering, gaps and coverage for one series.

    A gap is a step larger than one bar.  Steps of a day or more are counted
    separately as market closures: without the broker's calendar they cannot be
    called either missing data or a holiday, so they are labelled, not judged.
    """

    bars = len(series)
    if bars == 0:
        return Coverage(
            symbol=series.symbol,
            timeframe=series.timeframe,
            bars=0,
            first_at="",
            last_at="",
            expected_bars=0,
            coverage_fraction=0.0,
            gaps=0,
            weekend_gaps=0,
            largest_gap_seconds=0.0,
            duplicates=0,
            usable=False,
            reason="no_bars_for_this_window",
            notes=series.notes,
        )
    first = series.timestamps[0]
    last = series.timestamps[-1]
    span_seconds = max(0.0, (last - first).total_seconds())
    step = max(1, int(series.bar_seconds))
    expected = int(span_seconds // step) + 1
    gaps = 0
    weekend_gaps = 0
    largest = 0.0
    for previous, current in zip(series.timestamps, series.timestamps[1:]):
        delta = (current - previous).total_seconds()
        if delta > step:
            gaps += 1
            largest = max(largest, delta)
            if delta >= 86400:
                weekend_gaps += 1
    coverage = bars / expected if expected else 0.0
    reference = now or datetime.now(timezone.utc)
    reference = reference.astimezone(timezone.utc)
    recent_seconds = (reference - last).total_seconds()
    reason = ""
    if bars < min_bars:
        reason = f"too_few_bars ({bars} < {min_bars})"
    elif coverage < min_coverage:
        reason = f"coverage_below_{min_coverage:.0%} ({coverage:.1%} of the span)"
    elif recent_seconds > window_days * 86400:
        reason = "history_does_not_reach_the_recent_window"
    usable = reason == ""
    return Coverage(
        symbol=series.symbol,
        timeframe=series.timeframe,
        bars=bars,
        first_at=first.isoformat(),
        last_at=last.isoformat(),
        expected_bars=expected,
        coverage_fraction=coverage,
        gaps=gaps,
        weekend_gaps=weekend_gaps,
        largest_gap_seconds=largest,
        duplicates=0,
        usable=usable,
        reason=reason,
        notes=series.notes
        + (
            (
                f"{weekend_gaps} step(s) of a day or more: market closures, counted but "
                "not treated as missing data."
            )
            if weekend_gaps
            else (),
        ),
    )


__all__ = [
    "BID_PLUS_SPREAD",
    "Coverage",
    "RateRow",
    "Series",
    "TIE_EPSILON",
    "audit_coverage",
    "build_series",
    "rows_from_rates",
]
