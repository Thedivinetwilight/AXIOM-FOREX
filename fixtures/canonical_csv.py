"""Deterministic canonical-CSV builders for offline import tests.

These helpers produce *text* in the documented AXIOM-FOREX canonical import
format.  They are test/research inputs only: nothing here is market data, and
the importer still treats the output as user-supplied with an explicit data
class and provenance label.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Sequence

HEADER_COLUMNS: tuple[str, ...] = (
    "record_type",
    "pair",
    "timestamp",
    "timezone",
    "bid",
    "ask",
    "quote_id",
    "received_at",
    "correction_of",
    "timeframe",
    "duration_seconds",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "quote_count",
    "last_quote_at",
    "complete",
)

HEADER = ",".join(HEADER_COLUMNS)


def _join(cells: dict[str, str]) -> str:
    return ",".join(cells.get(name, "") for name in HEADER_COLUMNS)


def price_path(
    *,
    start: Decimal,
    step: Decimal,
    count: int,
    block_bars: int = 10,
    slope: int = 1,
) -> tuple[Decimal, ...]:
    """Deterministic alternating trend blocks (reversals drive SMA crossings)."""

    values: list[Decimal] = []
    current = start
    direction = 1
    for index in range(count):
        if index and index % block_bars == 0:
            direction = -direction
        current = current + Decimal(direction * slope) * step
        values.append(current)
    return tuple(values)


def quote_row(
    *,
    pair: str,
    timestamp: datetime,
    bid: Decimal,
    ask: Decimal,
    quote_id: str,
    timezone_name: str = "UTC",
    received_at: datetime | None = None,
) -> str:
    received = received_at or timestamp + timedelta(milliseconds=200)
    return _join(
        {
            "record_type": "QUOTE",
            "pair": pair,
            "timestamp": timestamp.isoformat(),
            "timezone": timezone_name,
            "bid": str(bid),
            "ask": str(ask),
            "quote_id": quote_id,
            "received_at": received.isoformat(),
        }
    )


def bar_row(
    *,
    pair: str,
    start: datetime,
    mid_open: Decimal,
    mid_close: Decimal,
    spread: Decimal,
    quote_count: int = 8,
    timeframe: str = "1m",
    duration_seconds: int = 60,
    timezone_name: str = "UTC",
    complete: bool = True,
) -> str:
    high = max(mid_open, mid_close) + spread / Decimal(2)
    low = min(mid_open, mid_close) - spread / Decimal(2)
    half = spread / Decimal(2)
    last_quote = start + timedelta(seconds=duration_seconds - 1)
    return _join(
        {
            "record_type": "BAR",
            "pair": pair,
            "timestamp": start.isoformat(),
            "timezone": timezone_name,
            "timeframe": timeframe,
            "duration_seconds": str(duration_seconds),
            "bid_open": str(mid_open - half),
            "bid_high": str(high - half),
            "bid_low": str(low - half),
            "bid_close": str(mid_close - half),
            "ask_open": str(mid_open + half),
            "ask_high": str(high + half),
            "ask_low": str(low + half),
            "ask_close": str(mid_close + half),
            "quote_count": str(quote_count),
            "last_quote_at": last_quote.isoformat(),
            "complete": "true" if complete else "false",
        }
    )


def build_canonical_csv(
    *,
    pair: str,
    starts: Sequence[datetime],
    mids: Sequence[Decimal],
    spread: Decimal = Decimal("0.00020"),
    timeframe: str = "1m",
    duration_seconds: int = 60,
    include_quotes: bool = True,
    skip_indexes: Iterable[int] = (),
) -> str:
    """Compose one canonical CSV: a BAR per slot plus an optional last QUOTE."""

    skip = set(skip_indexes)
    lines = [
        "# AXIOM-FOREX canonical import CSV (format AXIOM_FOREX_CANONICAL_CSV, "
        "schema 1.0.0)",
        HEADER,
    ]
    half = spread / Decimal(2)
    for index, (start, mid) in enumerate(zip(starts, mids)):
        if index in skip:
            continue
        previous = mids[index - 1] if index else mid
        lines.append(
            bar_row(
                pair=pair,
                start=start,
                mid_open=previous,
                mid_close=mid,
                spread=spread,
                timeframe=timeframe,
                duration_seconds=duration_seconds,
            )
        )
        if include_quotes:
            lines.append(
                quote_row(
                    pair=pair,
                    timestamp=start + timedelta(seconds=duration_seconds - 1),
                    bid=mid - half,
                    ask=mid + half,
                    quote_id=f"{pair}:{index}",
                )
            )
    return "\n".join(lines) + "\n"
