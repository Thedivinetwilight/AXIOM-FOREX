"""MetaTrader 5 tick export → canonical quote-bar CSV, with explicit provenance.

The rule this module defends, in one sentence: **a bar is built from the
broker's own two-sided bid/ask ticks, or it is not built at all.**  No candle is
split into a bid and an ask, no empty window is interpolated, and no server
timestamp is guessed.

What MetaTrader 5 can give us
-----------------------------
The terminal's *Ticks* window (and ``copy_ticks_range``) is the only export that
carries a bid **and** an ask per record.  A bar export carries ``PRICE``/
``CLOSE`` (candles) and sometimes a single ``SPREAD``; neither can be turned into
a two-sided quote without inventing the other side, so such a file is refused
outright (``tick_header_incomplete``).

The export itself is written in the *broker server's* clock, which is not UTC.
The offset (or the IANA zone) is therefore a **required operator declaration**:
without it the conversion refuses (``server_time_basis_missing``) instead of
assuming +2, +3, or "probably UTC".

Pipeline, each stage refusing rather than guessing
--------------------------------------------------
``read_ticks``          parse the file, reject crossed/malformed rows, count
                        duplicates, refuse out-of-order rows (never silently
                        sort: a sorted file could hide a broken export)
``resolve_local_times`` server wall time → UTC, refusing ambiguous or
                        nonexistent DST wall times
``build_quote_bars``    aggregate into complete windows only; report every
                        hole, every empty window, and the trailing partial one
``convert``             write the canonical CSV plus a provenance sidecar that
                        names the source file, its SHA-256, the declared time
                        basis, and the derivation

Coverage is reported, not repaired.  A gap is *not* an error: importing a
partial export is the operator's decision, and the coverage block is where they
see what they are deciding about.

Authority
---------
Tick exports are third-party data.  A converted file is a *derived* dataset
(``DERIVED_FROM_BROKER_TICKS``), never broker-distributed history, and the
importer's manifest records that label alongside the operator's declarations.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .canonical_csv import resolve_timestamp
from .contracts import BAR_RECORD, HEADER_COLUMNS, NORMALIZED_TIMEZONE_BASIS

#: Provenance labels.  Both are identifiers, not locators.
SOURCE_FORMAT = "MT5_TICK_EXPORT"
DERIVATION = "DERIVED_FROM_BROKER_TICKS"
SERVER_TIME_BASIS_OFFSET = "DECLARED_FIXED_OFFSET"
SERVER_TIME_BASIS_ZONE = "DECLARED_IANA_ZONE"

#: MetaTrader 5's Ticks window writes these columns, in this order, with no
#: header unless the export was produced by "Save as".
DEFAULT_COLUMNS: tuple[str, ...] = (
    "date",
    "time",
    "bid",
    "ask",
    "last",
    "volume",
    "flags",
)

_DATE_FORMATS = ("%Y.%m.%d", "%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d")
_CLOCK = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2})(?:\.(?P<f>\d{1,9}))?)?$")
_EPOCH = re.compile(r"^\d{9,13}$")
_HEADER_TOKENS = frozenset(
    {"date", "time", "datetime", "bid", "ask", "last", "volume", "flags", "timestamp"}
)

_TIMEFRAME_BY_SECONDS = {
    30: "30s",
    60: "1m",
    120: "2m",
    300: "5m",
    600: "10m",
    900: "15m",
    1800: "30m",
    3600: "1h",
    14400: "4h",
    86400: "1d",
}


class TickExportRefused(Exception):
    """The export cannot be used as broker tick data.  Nothing is invented."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail

    def audit_view(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail}


# Alias kept for callers that prefer the "error" name.
TickConversionError = TickExportRefused


@dataclass(frozen=True)
class Tick:
    """One two-sided tick, as exported, before or after UTC resolution."""

    at: datetime
    bid: Decimal
    ask: Decimal
    volume: int = 1
    flags: int = 0

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class TickExport:
    """A parsed export: the ticks that survived, and everything refused."""

    header: tuple[str, ...]
    delimiter: str
    ticks: tuple[Tick, ...]
    rejected: tuple[dict[str, Any], ...] = ()
    duplicate_count: int = 0
    path: Path | None = None
    server_basis: str = ""
    time_basis_detail: str = ""

    @property
    def count(self) -> int:
        return len(self.ticks)

    @property
    def first_at(self) -> datetime | None:
        return self.ticks[0].at if self.ticks else None

    @property
    def last_at(self) -> datetime | None:
        return self.ticks[-1].at if self.ticks else None


@dataclass(frozen=True)
class Coverage:
    """What the export covers, stated plainly rather than smoothed over."""

    window_seconds: int
    bar_count: int
    tick_count: int
    first_window: datetime | None = None
    last_window: datetime | None = None
    empty_windows: int = 0
    #: One entry per window with no ticks: ``{"window": iso, "reason": "no_ticks"}``.
    gap_windows: list[dict[str, Any]] = field(default_factory=list)
    largest_gap_seconds: Decimal = Decimal(0)
    trailing_partial_window: datetime | None = None
    first_tick_at: datetime | None = None
    last_tick_at: datetime | None = None

    @property
    def complete(self) -> bool:
        """True only when every window between the first and last has ticks."""

        return not self.gap_windows

    def audit_view(self) -> dict[str, Any]:
        return {
            "window_seconds": self.window_seconds,
            "bars": self.bar_count,
            "ticks": self.tick_count,
            "first_tick_at": _iso(self.first_tick_at),
            "last_tick_at": _iso(self.last_tick_at),
            "first_window": _iso(self.first_window),
            "last_window": _iso(self.last_window),
            "empty_windows": self.empty_windows,
            "gap_windows": [dict(item) for item in self.gap_windows],
            "largest_gap_seconds": str(self.largest_gap_seconds),
            "trailing_partial_window": _iso(self.trailing_partial_window),
            "complete": self.complete,
        }


# ---------------------------------------------------------------------------
# 1. parsing
# ---------------------------------------------------------------------------


def read_ticks(path: str | Path) -> TickExport:
    """Parse a MetaTrader 5 tick export.  Refuses anything it cannot prove."""

    source = Path(path)
    try:
        raw = source.read_bytes()
    except FileNotFoundError as exc:
        raise TickExportRefused("tick_file_missing", str(source)) from exc
    except OSError as exc:
        raise TickExportRefused("tick_file_unreadable", str(exc)) from exc
    text = raw.decode("utf-8-sig", errors="strict")
    return parse_ticks(text, path=source)


def parse_ticks(text: str, *, path: Path | None = None) -> TickExport:
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise TickExportRefused("tick_file_empty", "the export contains no rows")

    delimiter = _delimiter_for(lines[0])
    cells = tuple(cell.strip() for cell in lines[0].split(delimiter))
    header, body_start = _header_for(cells)
    columns = {name: index for index, name in enumerate(header)}

    ticks: list[Tick] = []
    rejected: list[dict[str, Any]] = []
    duplicates = 0
    previous: datetime | None = None
    seen: dict[datetime, set[tuple[Decimal, Decimal]]] = {}
    for offset, line in enumerate(lines[body_start:], start=body_start + 1):
        fields = tuple(cell.strip() for cell in line.split(delimiter))
        if len(fields) != len(header):
            rejected.append(
                {
                    "line": offset,
                    "code": "malformed_row",
                    "detail": f"expected {len(header)} fields, found {len(fields)}",
                }
            )
            continue
        try:
            at = _combine(fields[columns["date"]], fields[columns["time"]])
            bid = _price(fields[columns["bid"]])
            ask = _price(fields[columns["ask"]])
        except TickExportRefused as exc:
            rejected.append({"line": offset, "code": exc.reason, "detail": exc.detail})
            continue
        volume = _optional_int(fields[columns["volume"]]) if "volume" in columns else 1
        flags = _optional_int(fields[columns["flags"]]) if "flags" in columns else 0
        if bid <= 0 or ask <= 0:
            rejected.append(
                {"line": offset, "code": "non_positive_price", "detail": f"bid={bid} ask={ask}"}
            )
            continue
        if ask < bid:
            rejected.append(
                {"line": offset, "code": "crossed_quote", "detail": f"bid={bid} ask={ask}"}
            )
            continue
        tick = Tick(at=at, bid=bid, ask=ask, volume=volume, flags=flags)
        prices = seen.setdefault(at, set())
        if (bid, ask) in prices:
            # The same tick exported twice is a fact about the file, not a
            # second quote: count it and keep one.
            duplicates += 1
            continue
        if prices:
            rejected.append(
                {
                    "line": offset,
                    "code": "tick_timestamp_conflict",
                    "detail": f"two different quotes at {at.isoformat()}",
                }
            )
            continue
        prices.add((bid, ask))
        if previous is not None:
            if at < previous:
                # Never silently sort: an out-of-order export is broken, and a
                # sorted file could hide exactly the damage we import to avoid.
                raise TickExportRefused(
                    "tick_rows_out_of_order",
                    f"line {offset}: {at.isoformat()} follows {previous.isoformat()}",
                )
        previous = at
        ticks.append(tick)

    if not ticks:
        raise TickExportRefused(
            "no_valid_ticks",
            f"{len(rejected)} row(s) refused and none accepted",
        )
    return TickExport(
        header=header,
        delimiter=delimiter,
        ticks=tuple(ticks),
        rejected=tuple(rejected),
        duplicate_count=duplicates,
        path=path,
    )


def _delimiter_for(line: str) -> str:
    if "\t" in line:
        return "\t"
    if "," in line:
        return ","
    if ";" in line:
        return ";"
    raise TickExportRefused(
        "tick_delimiter_unknown",
        "no tab, comma, or semicolon found; this is not a MetaTrader 5 tick export",
    )


def _header_for(cells: Sequence[str]) -> tuple[tuple[str, ...], int]:
    """Return (columns, index of the first data row)."""

    lowered = tuple(cell.strip().strip("<>").lower() for cell in cells)
    named = [cell for cell in lowered if cell in _HEADER_TOKENS]
    if not named or _looks_like_data(lowered[0]):
        # Positional layout, as copied straight out of the Ticks window: the
        # first line is already data.
        if len(cells) < 4:
            raise TickExportRefused(
                "tick_header_incomplete",
                "expected at least DATE, TIME, BID, ASK columns",
            )
        return DEFAULT_COLUMNS[: len(cells)], 0
    missing = [name for name in ("bid", "ask") if name not in lowered]
    if missing:
        # A candle export (PRICE/CLOSE) or a one-sided file has no two-sided
        # price: it is refused, never completed from a spread or a candle.
        raise TickExportRefused(
            "tick_header_incomplete",
            "missing " + ", ".join(missing) + f" in {', '.join(lowered)}",
        )
    unknown = [cell for cell in lowered if cell not in _HEADER_TOKENS]
    if unknown:
        raise TickExportRefused(
            "tick_header_unknown_columns",
            ", ".join(unknown),
        )
    if "datetime" in lowered:
        return lowered, 1
    if "date" not in lowered or "time" not in lowered:
        raise TickExportRefused(
            "tick_header_incomplete",
            "missing date/time in " + ", ".join(lowered),
        )
    return lowered, 1


def _looks_like_data(cell: str) -> bool:
    if _EPOCH.match(cell):
        return True
    for pattern in _DATE_FORMATS:
        try:
            datetime.strptime(cell, pattern)
        except ValueError:
            continue
        return True
    return False


def _combine(date_text: str, clock_text: str) -> datetime:
    stripped = date_text.strip()
    if _EPOCH.match(stripped):
        return _from_epoch(stripped)
    day: datetime | None = None
    for pattern in _DATE_FORMATS:
        try:
            day = datetime.strptime(stripped, pattern)
            break
        except ValueError:
            continue
    if day is None:
        raise TickExportRefused("tick_date_invalid", f"cannot read date {date_text!r}")
    match = _CLOCK.match(clock_text.strip())
    if match is None:
        raise TickExportRefused("tick_time_invalid", f"cannot read time {clock_text!r}")
    fraction = match.group("f") or ""
    micro = int((fraction + "000000")[:6]) if fraction else 0
    return day.replace(
        hour=int(match.group("h")),
        minute=int(match.group("m")),
        second=int(match.group("s") or 0),
        microsecond=micro,
    )


def _from_epoch(text: str) -> datetime:
    value = int(text)
    seconds = value / 1000 if len(text) >= 12 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


def _price(text: str) -> Decimal:
    try:
        return Decimal(str(text).strip())
    except (InvalidOperation, ValueError) as exc:
        raise TickExportRefused("tick_price_invalid", f"cannot read price {text!r}") from exc


def _optional_int(text: str) -> int:
    try:
        return int(Decimal(str(text).strip()))
    except (InvalidOperation, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 2. server time → UTC
# ---------------------------------------------------------------------------


def resolve_local_times(
    export: TickExport,
    *,
    zone_name: str = "",
    offset_minutes: int | None = None,
) -> TickExport:
    """Convert the export's server wall times to UTC, on a declared basis only."""

    if not zone_name and offset_minutes is None:
        raise TickExportRefused(
            "server_time_basis_missing",
            "declare the broker server's UTC offset (or its IANA zone); there is no default",
        )
    if zone_name and offset_minutes is not None:
        raise TickExportRefused(
            "server_time_basis_ambiguous",
            "declare either a fixed offset or an IANA zone, not both",
        )

    resolved: list[Tick] = []
    previous: datetime | None = None
    for index, tick in enumerate(export.ticks, start=1):
        if zone_name:
            at, issues = resolve_timestamp(
                _naive_text(tick.at), zone_name=zone_name, field_name="tick_time", line=index
            )
            if issues or at is None:
                code = issues[0].code if issues else "tick_time_invalid"
                reason = (
                    "ambiguous_server_time"
                    if code == "ambiguous_local_timestamp"
                    else "nonexistent_server_time"
                    if code == "nonexistent_local_timestamp"
                    else "tick_time_unresolved"
                )
                raise TickExportRefused(reason, f"line {index}: {code}")
            basis = SERVER_TIME_BASIS_ZONE
        else:
            at = (tick.at - timedelta(minutes=int(offset_minutes or 0))).replace(
                tzinfo=timezone.utc
            )
            basis = SERVER_TIME_BASIS_OFFSET
        if previous is not None and at < previous:
            raise TickExportRefused(
                "tick_rows_out_of_order",
                f"line {index}: {_iso(at)} precedes {_iso(previous)} after conversion",
            )
        previous = at
        resolved.append(replace(tick, at=at))
    return replace(
        export,
        ticks=tuple(resolved),
        server_basis=basis,
        time_basis_detail=(
            f"zone={zone_name}" if zone_name else f"offset_minutes={int(offset_minutes or 0)}"
        ),
    )


# ---------------------------------------------------------------------------
# 3. aggregation: complete windows only
# ---------------------------------------------------------------------------


def build_quote_bars(
    export: TickExport,
    *,
    pair: str,
    interval_seconds: int = 60,
) -> tuple[list[dict[str, Any]], Coverage]:
    """Aggregate ticks into complete bid/ask bars; report holes, never fill them."""

    if interval_seconds <= 0:
        raise TickExportRefused("tick_interval_invalid", str(interval_seconds))
    canonical = str(pair or "").strip().upper()
    if "/" not in canonical or len(canonical.split("/")) != 2:
        raise TickExportRefused("pair_invalid", str(pair))
    if not export.ticks:
        raise TickExportRefused("no_valid_ticks", "nothing to aggregate")

    windows: dict[datetime, list[Tick]] = {}
    for tick in export.ticks:
        start = _window_start(tick.at, interval_seconds)
        windows.setdefault(start, []).append(tick)

    ordered = sorted(windows)
    last_tick_at = export.ticks[-1].at
    bars: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    largest_gap = Decimal(0)
    trailing_partial: datetime | None = None
    previous_start: datetime | None = None
    step = timedelta(seconds=interval_seconds)

    for start in ordered:
        complete = start + step <= last_tick_at
        if previous_start is not None and start - previous_start > step:
            # Enumerate the windows with no ticks.  Each one is a hole the
            # operator must see; none of them becomes a bar.
            missing = previous_start + step
            while missing < start:
                gaps.append({"window": missing.isoformat(), "reason": "no_ticks"})
                missing += step
            jump = Decimal(str((start - previous_start).total_seconds()))
            if jump > largest_gap:
                largest_gap = jump
        previous_start = start
        if not complete:
            # The export ended inside this window: it is reported, not emitted.
            trailing_partial = start
            continue
        bars.append(_bar_from(canonical, start, windows[start], interval_seconds))

    if not bars:
        raise TickExportRefused(
            "no_complete_bars",
            "the export does not cover a single full window",
        )
    coverage = Coverage(
        window_seconds=interval_seconds,
        bar_count=len(bars),
        tick_count=len(export.ticks),
        first_window=bars[0]["timestamp"],
        last_window=bars[-1]["timestamp"],
        empty_windows=len(gaps),
        gap_windows=list(gaps),
        largest_gap_seconds=largest_gap,
        trailing_partial_window=trailing_partial,
        first_tick_at=export.ticks[0].at,
        last_tick_at=last_tick_at,
    )
    return bars, coverage


def _window_start(at: datetime, interval_seconds: int) -> datetime:
    epoch = int(at.replace(tzinfo=timezone.utc).timestamp())
    floored = epoch - (epoch % interval_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _bar_from(
    pair: str, start: datetime, ticks: Sequence[Tick], interval_seconds: int
) -> dict[str, Any]:
    bids = [tick.bid for tick in ticks]
    asks = [tick.ask for tick in ticks]
    return {
        "record_type": BAR_RECORD,
        "pair": pair,
        "timestamp": start,
        "timeframe": _timeframe_for(interval_seconds),
        "duration_seconds": interval_seconds,
        "bid_open": bids[0],
        "bid_high": max(bids),
        "bid_low": min(bids),
        "bid_close": bids[-1],
        "ask_open": asks[0],
        "ask_high": max(asks),
        "ask_low": min(asks),
        "ask_close": asks[-1],
        "quote_count": len(ticks),
        "last_quote_at": ticks[-1].at,
        "complete": True,
    }


def _timeframe_for(interval_seconds: int) -> str:
    return _TIMEFRAME_BY_SECONDS.get(interval_seconds, f"{interval_seconds}s")


# ---------------------------------------------------------------------------
# 4. writing the canonical CSV and its provenance
# ---------------------------------------------------------------------------


def render_canonical(bars: Iterable[Mapping[str, Any]]) -> str:
    """Render bars in the canonical header order, ready for the importer."""

    lines = [",".join(HEADER_COLUMNS)]
    for bar in bars:
        cells: dict[str, str] = {
            "record_type": BAR_RECORD,
            "pair": str(bar["pair"]),
            "timestamp": _iso(bar["timestamp"]),
            "timezone": NORMALIZED_TIMEZONE_BASIS,
            "timeframe": str(bar["timeframe"]),
            "duration_seconds": str(int(bar["duration_seconds"])),
            "quote_count": str(int(bar["quote_count"])),
            "last_quote_at": _iso(bar["last_quote_at"]),
            "complete": "true" if bar.get("complete") else "false",
        }
        for name in ("bid", "ask"):
            for point in ("open", "high", "low", "close"):
                cells[f"{name}_{point}"] = str(bar[f"{name}_{point}"])
        lines.append(",".join(cells.get(column, "") for column in HEADER_COLUMNS))
    return "\n".join(lines) + "\n"


def convert(
    *,
    source: str | Path,
    pair: str,
    output: str | Path | None = None,
    server_offset_minutes: int | None = None,
    server_timezone: str = "",
    interval_seconds: int = 60,
) -> dict[str, Any]:
    """Convert one tick export into a canonical CSV plus a provenance sidecar."""

    path = Path(source)
    export = read_ticks(path)
    resolved = resolve_local_times(
        export, zone_name=str(server_timezone or "").strip(), offset_minutes=server_offset_minutes
    )
    bars, coverage = build_quote_bars(
        resolved, pair=pair, interval_seconds=int(interval_seconds)
    )
    target = Path(output) if output else path.with_name(f"{path.stem}_canonical.csv")
    target.write_text(render_canonical(bars), encoding="utf-8")

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    output_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    provenance_path = target.with_suffix(f"{target.suffix}.provenance.json")
    report = {
        "converted": True,
        "format": SOURCE_FORMAT,
        "derivation": DERIVATION,
        "pair": str(pair),
        "source_file": path.name,
        "source_sha256": digest,
        "source_bytes": path.stat().st_size,
        "output": str(target),
        "output_sha256": output_digest,
        "bars_written": len(bars),
        "tick_count": resolved.count,
        "duplicate_ticks": export.duplicate_count,
        "rejected_rows": [dict(item) for item in export.rejected],
        "server_time_basis": resolved.server_basis,
        "time_basis_detail": resolved.time_basis_detail,
        "interval_seconds": int(interval_seconds),
        "coverage": coverage.audit_view(),
        "provenance_path": str(provenance_path),
        "notes": [
            "every price here is a bid or an ask that the broker exported; no candle was "
            "split and no side was derived from a spread",
            "only complete windows became bars; empty windows are reported, not filled",
            "the server clock conversion is the operator's declared basis, not a guess",
            "received_at is left empty: an export carries no receive time, and one will "
            "not be invented",
        ],
    }
    sidecar = {
        "schema": "axiom-mt5-tick-conversion-v1",
        "format": SOURCE_FORMAT,
        "derivation": DERIVATION,
        "pair": str(pair),
        "source_file": path.name,
        "source_sha256": digest,
        "source_bytes": path.stat().st_size,
        "output_file": target.name,
        "output_sha256": output_digest,
        "bars_written": len(bars),
        "tick_count": resolved.count,
        "duplicate_ticks": export.duplicate_count,
        "rejected_rows": [dict(item) for item in export.rejected],
        "server_time_basis": resolved.server_basis,
        "time_basis_detail": resolved.time_basis_detail,
        "interval_seconds": int(interval_seconds),
        "coverage": coverage.audit_view(),
        "notes": report["notes"],
    }
    provenance_path.write_text(
        json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def load_provenance(path: str | Path) -> dict[str, Any]:
    """Read a sidecar back, for the data audit to cite."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TickExportRefused("provenance_unreadable", str(exc)) from exc
    if not isinstance(payload, dict):
        raise TickExportRefused("provenance_unreadable", "sidecar is not an object")
    return payload


def _naive_text(value: datetime) -> str:
    return value.replace(tzinfo=None, microsecond=value.microsecond).isoformat()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else str(value)


__all__ = [
    "Coverage",
    "DERIVATION",
    "DEFAULT_COLUMNS",
    "SOURCE_FORMAT",
    "Tick",
    "TickConversionError",
    "TickExport",
    "TickExportRefused",
    "build_quote_bars",
    "convert",
    "load_provenance",
    "parse_ticks",
    "read_ticks",
    "render_canonical",
    "resolve_local_times",
]
