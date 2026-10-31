"""Map AXIOM's broker bars onto Kronos' input, field by field, without inventing any.

Kronos wants ``open``/``high``/``low``/``close``, optionally ``volume`` and
``amount``, plus a timestamp series.  A broker terminal gives two things this
project actually has: a **bid** and an **ask** OHLC for each bar, and a **tick
count**.  So the mapping is written down here rather than left to a library
default:

===============  ====================================================  ============
Kronos column    AXIOM source                                          honesty
===============  ====================================================  ============
``open``         ``(bid.open + ask.open) / 2``                          ``MID`
``high``         ``(bid.high + ask.high) / 2``                          ``MID``
``low``          ``(bid.low + ask.low) / 2``                            ``MID``
``close``        ``(bid.close + ask.close) / 2``                        ``MID``
``volume``       ``bar.quote_count`` (ticks in the bar)                 ``TICK_VOLUME``
``amount``       ``tick_volume * mean(mid OHLC)``                       ``DERIVED``
``timestamps``   ``bar.timestamp`` (bar open, UTC)                      exact
===============  ====================================================  ============

Three consequences are stated on the page and stored with every forecast:

* **Mid is not a tradable price.**  A forecast of the mid ignores the spread you
  pay to enter and exit; the strategy layer applies the broker's costs through
  the existing gate, not here.
* **MT5 tick volume is not exchange volume.**  It counts price updates, which
  varies with broker feed frequency and quiet periods.  It is passed labelled
  ``TICK_VOLUME`` because the model accepts a volume column; it is never called
  turnover, and ``amount`` derived from it is labelled ``DERIVED``.
* **If a field is not available it is absent, not invented.**  A bar source with
  no tick count yields ``volume_mode=ABSENT``: the predictor is then given no
  volume and no amount at all (its own zero-fill applies inside the model, and
  that is recorded as the predictor's behaviour, not as data).

The future timestamps are the *nominal* continuation of the bar grid.  They are
labelled nominal because AXIOM does not claim the market is open for every one of
them; scheduled forecasting uses the trading calendar for that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

MID = "MID"
TICK_VOLUME = "TICK_VOLUME"
DERIVED = "DERIVED"
ABSENT = "ABSENT"


class InputRefused(Exception):
    """The bars cannot become a Kronos input.  Carries a stable reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class DataQuality:
    """What a reader needs to know before trusting a forecast built on this."""

    bars: int
    first_at: str
    last_at: str
    timeframe: str
    gaps: int
    duplicates: int
    crossed: int
    stale_seconds: float
    volume_mode: str
    amount_mode: str
    price_basis: str = MID
    nominal_future_timestamps: bool = True
    warnings: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.bars > 0 and self.crossed == 0 and self.duplicates == 0

    def audit_view(self) -> dict[str, Any]:
        return {
            "bars": self.bars,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "timeframe": self.timeframe,
            "gaps": self.gaps,
            "duplicates": self.duplicates,
            "crossed": self.crossed,
            "stale_seconds": self.stale_seconds,
            "volume_mode": self.volume_mode,
            "amount_mode": self.amount_mode,
            "price_basis": self.price_basis,
            "nominal_future_timestamps": self.nominal_future_timestamps,
            "warnings": list(self.warnings),
            "usable": self.usable,
        }

    def label(self) -> str:
        if not self.usable:
            return "UNUSABLE"
        if self.gaps or self.stale_seconds > 120:
            return "DEGRADED"
        return "OK"


@dataclass(frozen=True)
class KronosInput:
    """Exactly what will be handed to the predictor, plus its provenance."""

    symbol: str
    timeframe: str
    rows: tuple[dict[str, float], ...]
    x_timestamps: tuple[datetime, ...]
    y_timestamps: tuple[datetime, ...]
    quality: DataQuality
    last_close: Decimal
    columns: tuple[str, ...] = ("open", "high", "low", "close")
    provenance: dict[str, str] = field(default_factory=dict)

    def frame_view(self) -> list[dict[str, Any]]:
        """The rows as the page and the audit trail see them."""

        return [
            {
                "at": timestamp.isoformat(),
                **{name: str(Decimal(str(value))) for name, value in row.items()},
            }
            for timestamp, row in zip(self.x_timestamps, self.rows)
        ]

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": len(self.rows),
            "horizon": len(self.y_timestamps),
            "columns": list(self.columns),
            "quality": self.quality.audit_view(),
            "last_close": str(self.last_close),
            "first_at": self.x_timestamps[0].isoformat() if self.x_timestamps else "",
            "last_at": self.x_timestamps[-1].isoformat() if self.x_timestamps else "",
            "provenance": dict(self.provenance),
        }


def _mid(bar: Any, point: str) -> Decimal:
    bid, _ = bar.bid.parsed()
    ask, _ = bar.ask.parsed()
    return (bid[point] + ask[point]) / Decimal(2)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def build_input(
    bars: Sequence[Any],
    *,
    symbol: str,
    timeframe: str,
    lookback: int,
    horizon: int,
    bar_seconds: int,
    volume_mode: str = TICK_VOLUME,
    now: datetime | None = None,
) -> KronosInput:
    """Turn the *last* ``lookback`` completed bars into a Kronos input.

    Refuses rather than repairs: an unordered series, an incomplete bar, a
    crossed quote, a duplicate timestamp, a missing price, or too few bars.
    """

    if lookback < 1 or horizon < 1:
        raise InputRefused("invalid_window", f"lookback={lookback} horizon={horizon}")
    if bar_seconds <= 0:
        raise InputRefused("invalid_timeframe", timeframe)
    if not symbol.strip():
        raise InputRefused("symbol_required")

    ordered = list(bars)
    if not ordered:
        raise InputRefused("no_bars", symbol)
    for bar in ordered:
        if not getattr(bar, "complete", False):
            raise InputRefused("incomplete_bar", _utc(bar.timestamp).isoformat())

    timestamps = [_utc(bar.timestamp) for bar in ordered]
    if timestamps != sorted(timestamps):
        raise InputRefused("bars_out_of_order", symbol)
    duplicates = len(timestamps) - len(set(timestamps))
    if duplicates:
        raise InputRefused("duplicate_timestamps", str(duplicates))

    crossed = 0
    for bar in ordered:
        bid, bid_result = bar.bid.parsed()
        ask, ask_result = bar.ask.parsed()
        if not bid_result.valid or not ask_result.valid:
            raise InputRefused("incomplete_quote", _utc(bar.timestamp).isoformat())
        for point in ("open", "high", "low", "close"):
            if bid[point] > ask[point]:
                crossed += 1
    if crossed:
        raise InputRefused("crossed_quotes", str(crossed))

    window = ordered[-lookback:]
    stamps = timestamps[-lookback:]
    if len(window) < lookback:
        raise InputRefused(
            "insufficient_bars", f"need {lookback}, have {len(window)}"
        )

    gaps = 0
    for previous, current in zip(stamps, stamps[1:]):
        if (current - previous) > timedelta(seconds=bar_seconds):
            gaps += 1

    volume_available = all(int(getattr(bar, "quote_count", 0) or 0) > 0 for bar in window)
    mode = volume_mode if volume_mode in {TICK_VOLUME, ABSENT} else ABSENT
    if mode == TICK_VOLUME and not volume_available:
        mode = ABSENT

    rows: list[dict[str, float]] = []
    for bar in window:
        opens = _mid(bar, "open")
        highs = _mid(bar, "high")
        lows = _mid(bar, "low")
        closes = _mid(bar, "close")
        row = {
            "open": float(opens),
            "high": float(highs),
            "low": float(lows),
            "close": float(closes),
        }
        if mode == TICK_VOLUME:
            volume = float(int(bar.quote_count))
            row["volume"] = volume
            # Derived, and labelled: MT5 has no turnover figure here.
            row["amount"] = volume * float((opens + highs + lows + closes) / Decimal(4))
        rows.append(row)

    columns = ("open", "high", "low", "close") + (
        ("volume", "amount") if mode == TICK_VOLUME else ()
    )

    last_at = stamps[-1]
    last_close = Decimal(str(rows[-1]["close"]))
    future = tuple(
        last_at + timedelta(seconds=bar_seconds * (index + 1)) for index in range(horizon)
    )

    reference = _utc(now) if now is not None else datetime.now(timezone.utc)
    stale = max(0.0, (reference - last_at).total_seconds())
    warnings: list[str] = []
    if mode == ABSENT:
        warnings.append(
            "No tick count was available, so no volume or amount is passed. Any zero-fill "
            "happens inside the predictor, not in this project's data."
        )
    if gaps:
        warnings.append(
            f"{gaps} gap(s) in the context window: the market was closed, or a bar is "
            "missing. Nothing was interpolated."
        )
    if stale > bar_seconds * 3:
        warnings.append(
            f"The newest bar is {stale:.0f}s old, which is more than three "
            f"{timeframe} bars. This forecast is about the past, not the present."
        )
    if _crosses_weekend(stamps[-1], future[-1]):
        warnings.append(
            "The nominal forecast timestamps cross a weekend. They are a continuation of "
            "the bar grid, not a claim that the market is open."
        )

    quality = DataQuality(
        bars=len(window),
        first_at=stamps[0].isoformat(),
        last_at=last_at.isoformat(),
        timeframe=timeframe,
        gaps=gaps,
        duplicates=duplicates,
        crossed=crossed,
        stale_seconds=round(stale, 3),
        volume_mode=mode,
        amount_mode=DERIVED if mode == TICK_VOLUME else ABSENT,
        warnings=tuple(warnings),
    )
    return KronosInput(
        symbol=symbol.strip(),
        timeframe=timeframe,
        rows=tuple(rows),
        x_timestamps=tuple(stamps),
        y_timestamps=future,
        quality=quality,
        last_close=last_close,
        columns=columns,
        provenance={
            "price_basis": (
                "Midpoint of the broker's bid and ask OHLC per bar. Not a tradable price."
            ),
            "volume_basis": (
                "MT5 tick count (price updates), not exchange volume or turnover."
                if mode == TICK_VOLUME
                else "Not supplied."
            ),
            "amount_basis": (
                "Derived as tick volume x mean mid OHLC. Not broker turnover."
                if mode == TICK_VOLUME
                else "Not supplied."
            ),
            "timestamps": "Bar open time, UTC.",
            "future_timestamps": (
                "Nominal continuation of the bar grid; market open/closed is not implied."
            ),
            "source": "axiom_forex.kronos.inputs.build_input",
        },
    )


def _crosses_weekend(start: datetime, end: datetime) -> bool:
    cursor = start
    while cursor <= end:
        if cursor.weekday() >= 5:
            return True
        cursor += timedelta(days=1)
    return False


def describe_mapping() -> list[dict[str, str]]:
    """The table the page prints, so nobody has to guess what was sent."""

    return [
        {"column": "open", "source": "(bid.open + ask.open) / 2", "label": MID},
        {"column": "high", "source": "(bid.high + ask.high) / 2", "label": MID},
        {"column": "low", "source": "(bid.low + ask.low) / 2", "label": MID},
        {"column": "close", "source": "(bid.close + ask.close) / 2", "label": MID},
        {
            "column": "volume",
            "source": "bar.quote_count (MT5 tick count)",
            "label": TICK_VOLUME,
        },
        {
            "column": "amount",
            "source": "tick_volume x mean(mid OHLC)",
            "label": DERIVED,
        },
        {"column": "timestamps", "source": "bar.timestamp, UTC", "label": "EXACT"},
    ]


__all__ = [
    "ABSENT",
    "DERIVED",
    "DataQuality",
    "InputRefused",
    "KronosInput",
    "MID",
    "TICK_VOLUME",
    "build_input",
    "describe_mapping",
]
