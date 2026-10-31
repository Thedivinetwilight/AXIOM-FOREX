"""One year of history from the connected terminal, stored locally, labelled.

The rules this module exists to keep:

* **History comes from the terminal, or it does not exist.**  Rows are read with
  the read-only ``rates()`` call of the terminal the operator connected.  Nothing
  is downloaded, generated, or substituted.  If the terminal has no bars for a
  symbol and timeframe, the result says so.
* **Bid-only is recorded as bid-only.**  MT5 rate bars carry one price series plus
  the spread that applied.  :mod:`axiom_forex.research.series` turns them into a
  two-sided series *for research* and names the operation in the price basis; this
  module never presents the result as an execution-grade quote stream.
* **Every stored file says where it came from.**  ``origin`` is
  ``MT5_TERMINAL_READ_ONLY`` for terminal history and ``SAMPLE_DATA`` for anything
  a test or a demo produced.  Research refuses a sample unless it was asked for by
  name, so a fixture can never quietly become a strategy result.
* **No credentials, no login, no identifiers.**  A stored file holds bars, the
  symbol name, the timeframe, the window, and the coverage audit.  It holds no
  account number, no server, no password, and no path.

The store is plain JSON: readable, diffable, and free of pickle deserialisation
risk.  It lives under ``data/history/``, which this repository already ignores.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .series import (
    BID_PLUS_SPREAD,
    Coverage,
    RateRow,
    Series,
    audit_coverage,
    build_series,
    rows_from_rates,
)

SCHEMA_VERSION = "axiom-history-v1"

#: Where a stored series came from.  Research picks terminal history by default.
ORIGIN_TERMINAL = "MT5_TERMINAL_READ_ONLY"
ORIGIN_SAMPLE = "SAMPLE_DATA"

#: Timeframes this repository can store and evaluate.
TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")

DEFAULT_WINDOW_DAYS = 365

#: A year of one-minute bars is ~370k rows; a cap keeps a runaway request from
#: filling a laptop's disk.  Exceeding it is a refusal, not a truncation.
MAX_ROWS = 500_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _safe(name: str) -> str:
    """A filename fragment that cannot escape the store or collide."""

    cleaned = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in str(name)
    )
    return cleaned.strip("_") or "unnamed"


@dataclass(frozen=True)
class FetchResult:
    """What one symbol/timeframe request actually produced."""

    symbol: str
    timeframe: str
    status: str  # OK | EMPTY | REFUSED | ERROR
    reason: str
    rows: int
    series: Series | None
    coverage: Coverage | None
    pair: str
    requested_from: str
    requested_to: str
    fetched_at: str
    origin: str = ORIGIN_TERMINAL
    point: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "OK" and self.series is not None

    @property
    def bars(self) -> int:
        return len(self.series) if self.series is not None else 0

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "reason": self.reason,
            "rows": self.rows,
            "bars": self.bars,
            "pair": self.pair,
            "requested_from": self.requested_from,
            "requested_to": self.requested_to,
            "fetched_at": self.fetched_at,
            "origin": self.origin,
            "point": self.point,
            "price_basis": BID_PLUS_SPREAD,
            "coverage": self.coverage.audit_view() if self.coverage is not None else None,
            "notes": list(self.notes),
        }

    def summary_row(self) -> dict[str, Any]:
        """The compact line a UI shows per symbol and timeframe."""

        coverage = self.coverage
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pair": self.pair,
            "status": self.status,
            "bars": self.bars,
            "first_at": coverage.first_at if coverage else "",
            "last_at": coverage.last_at if coverage else "",
            "coverage_fraction": (
                round(coverage.coverage_fraction, 3) if coverage is not None else 0.0
            ),
            "usable": bool(coverage and coverage.usable),
            "reason": self.reason or (coverage.reason if coverage else ""),
            "origin": self.origin,
        }


def fetch_series(
    reader: Any,
    *,
    symbol: str,
    timeframe: str,
    pair: str,
    point: float,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    min_bars: int = 500,
    min_coverage: float = 0.90,
    bar_seconds: int | None = None,
) -> FetchResult:
    """Read one symbol and timeframe from the terminal and audit what came back.

    ``reader`` is anything with the terminal's read-only ``rates(symbol, timeframe,
    start=..., end=...)``.  Nothing here calls ``order_send``: history is a read.
    """

    stamp = now or _utc_now()
    end = stamp.astimezone(timezone.utc)
    start = end - timedelta(days=max(1, int(window_days)))
    base = dict(
        symbol=symbol,
        timeframe=timeframe,
        pair=pair,
        requested_from=_iso(start),
        requested_to=_iso(end),
        fetched_at=_iso(end),
        point=float(point or 0.0),
    )
    if timeframe not in TIMEFRAMES:
        return FetchResult(
            status="REFUSED",
            reason="unknown_timeframe",
            rows=0,
            series=None,
            coverage=None,
            notes=("This timeframe is not one this repository stores.",),
            **base,
        )
    if not str(symbol or "").strip():
        return FetchResult(
            status="REFUSED",
            reason="symbol_required",
            rows=0,
            series=None,
            coverage=None,
            notes=("No broker symbol was given, so nothing could be requested.",),
            **base,
        )
    try:
        raw = reader.rates(symbol, timeframe, start=start, end=end)
    except Exception as exc:  # the terminal refuses with typed errors; never raw text
        return FetchResult(
            status="ERROR",
            reason="the terminal refused the history request",
            rows=0,
            series=None,
            coverage=None,
            notes=(f"The terminal reported {type(exc).__name__}; no bars were stored.",),
            **base,
        )
    rows: Sequence[Mapping[str, Any]] = tuple(raw or ())
    if len(rows) > MAX_ROWS:
        return FetchResult(
            status="REFUSED",
            reason="too_many_rows",
            rows=len(rows),
            series=None,
            coverage=None,
            notes=(
                f"{len(rows)} bars exceed the {MAX_ROWS} this run will hold; ask for a "
                "narrower window or a longer timeframe.",
            ),
            **base,
        )
    if not rows:
        return FetchResult(
            status="EMPTY",
            reason="no_bars_for_this_window",
            rows=0,
            series=None,
            coverage=None,
            notes=(
                "The terminal returned no bars for this symbol and timeframe in this "
                "window. Nothing was substituted for them.",
            ),
            **base,
        )
    converted = rows_from_rates(rows)
    series = build_series(
        symbol,
        pair,
        timeframe,
        int(bar_seconds or _bar_seconds(timeframe)),
        point,
        converted,
    )
    coverage = audit_coverage(
        series,
        window_days=window_days,
        min_bars=min_bars,
        min_coverage=min_coverage,
        now=end,
    )
    notes = tuple(series.notes) + tuple(coverage.notes)
    if not coverage.usable and coverage.reason:
        notes = notes + (f"History is not usable yet: {coverage.reason}.",)
    return FetchResult(
        status="OK",
        reason="",
        rows=len(rows),
        series=series,
        coverage=coverage,
        notes=notes,
        **base,
    )


def _bar_seconds(timeframe: str) -> int:
    return {
        "1m": 60,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }.get(timeframe, 60)


@dataclass(frozen=True)
class StoredHistory:
    """A series on disk, with the provenance that makes it usable."""

    series: Series
    origin: str
    fetched_at: str
    window_days: int
    symbol: str
    timeframe: str
    pair: str
    path: str = ""

    @property
    def usable_for_research(self) -> bool:
        return self.origin == ORIGIN_TERMINAL

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pair": self.pair,
            "origin": self.origin,
            "fetched_at": self.fetched_at,
            "window_days": self.window_days,
            "bars": len(self.series),
            "usable_for_research": self.usable_for_research,
        }


class HistoryStore:
    """Plain-JSON storage for terminal history, one file per symbol/timeframe."""

    def __init__(self, root: str | Path = Path("data") / "history" / "research") -> None:
        self.root = Path(root)

    def path_for(self, symbol: str, timeframe: str) -> Path:
        return self.root / f"{_safe(symbol)}__{_safe(timeframe)}.json"

    # -- writing ----------------------------------------------------------

    def save(self, result: FetchResult) -> dict[str, Any]:
        """Write a fetched series, refusing anything that is not labelled."""

        if not result.ok or result.series is None:
            raise ValueError("only a fetched series can be stored")
        if result.origin not in (ORIGIN_TERMINAL, ORIGIN_SAMPLE):
            raise ValueError("a series must declare where it came from")
        series = result.series
        payload = {
            "schema": SCHEMA_VERSION,
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "pair": result.pair,
            "bar_seconds": series.bar_seconds,
            "point": series.point,
            "origin": result.origin,
            "fetched_at": result.fetched_at,
            "window_days": _window_days_of(result),
            "requested_from": result.requested_from,
            "requested_to": result.requested_to,
            "price_basis": BID_PLUS_SPREAD,
            "coverage": result.coverage.audit_view() if result.coverage else None,
            "notes": list(result.notes),
            "bars": [
                [
                    row.timestamp.isoformat(),
                    row.open,
                    row.high,
                    row.low,
                    row.close,
                    row.spread_points,
                    row.tick_volume,
                ]
                for row in series.rate_rows()
            ],
        }
        path = self.path_for(result.symbol, result.timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
        return {
            "path": str(path),
            "bars": len(series),
            "origin": result.origin,
            "usable_for_research": result.origin == ORIGIN_TERMINAL,
        }

    # -- reading ----------------------------------------------------------

    def load(
        self, symbol: str, timeframe: str, *, allow_sample: bool = False
    ) -> StoredHistory | None:
        """Read a stored series back, or ``None`` if there is no usable file."""

        path = self.path_for(symbol, timeframe)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if payload.get("schema") != SCHEMA_VERSION:
            return None
        origin = str(payload.get("origin") or "")
        if origin != ORIGIN_TERMINAL and not allow_sample:
            return None
        rows = []
        for item in payload.get("bars") or ():
            try:
                stamp = datetime.fromisoformat(str(item[0]))
            except (ValueError, IndexError, TypeError):
                continue
            rows.append(
                RateRow(
                    timestamp=stamp,
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    spread_points=int(item[5]),
                    tick_volume=int(item[6]) if len(item) > 6 else 0,
                    source=(
                        ORIGIN_TERMINAL if origin == ORIGIN_TERMINAL else origin
                    ),
                )
            )
        if not rows:
            return None
        series = build_series(
            str(payload.get("symbol") or symbol),
            str(payload.get("pair") or ""),
            str(payload.get("timeframe") or timeframe),
            int(payload.get("bar_seconds") or _bar_seconds(timeframe)),
            float(payload.get("point") or 0.0),
            rows,
        )
        return StoredHistory(
            series=series,
            origin=origin,
            fetched_at=str(payload.get("fetched_at") or ""),
            window_days=int(payload.get("window_days") or DEFAULT_WINDOW_DAYS),
            symbol=str(payload.get("symbol") or symbol),
            timeframe=str(payload.get("timeframe") or timeframe),
            pair=str(payload.get("pair") or ""),
            path=str(path),
        )

    def inventory(self) -> list[dict[str, Any]]:
        """Every stored file, with its origin, so the UI can show what exists."""

        if not self.root.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            bars = payload.get("bars") or ()
            items.append(
                {
                    "symbol": payload.get("symbol") or path.stem,
                    "timeframe": payload.get("timeframe") or "",
                    "pair": payload.get("pair") or "",
                    "origin": payload.get("origin") or "",
                    "fetched_at": payload.get("fetched_at") or "",
                    "bars": len(bars),
                    "usable_for_research": payload.get("origin") == ORIGIN_TERMINAL,
                }
            )
        return items

    def load_all(
        self, *, allow_sample: bool = False
    ) -> dict[tuple[str, str], StoredHistory]:
        """Load every stored series, keyed by ``(symbol, timeframe)``."""

        loaded: dict[tuple[str, str], StoredHistory] = {}
        for item in self.inventory():
            stored = self.load(
                str(item["symbol"]), str(item["timeframe"]), allow_sample=allow_sample
            )
            if stored is not None:
                loaded[(stored.symbol, stored.timeframe)] = stored
        return loaded


def _window_days_of(result: FetchResult) -> int:
    try:
        start = datetime.fromisoformat(result.requested_from)
        end = datetime.fromisoformat(result.requested_to)
    except ValueError:  # pragma: no cover - both are produced by this module
        return DEFAULT_WINDOW_DAYS
    return max(1, int(round((end - start).total_seconds() / 86400)))


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "FetchResult",
    "HistoryStore",
    "MAX_ROWS",
    "ORIGIN_SAMPLE",
    "ORIGIN_TERMINAL",
    "SCHEMA_VERSION",
    "StoredHistory",
    "TIMEFRAMES",
    "fetch_series",
]
