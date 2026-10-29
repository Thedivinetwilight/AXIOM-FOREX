"""Strict parser for the canonical AXIOM-FOREX import CSV.

The parser is intentionally unforgiving: unknown columns, unknown record types,
non-ISO timestamps, ambiguous or nonexistent local wall times, mismatched UTC
offsets, crossed quotes, and incomplete bars are all reported as findings.  It
never repairs, interpolates, or infers a value.  Records that fail parsing are
dropped and reported; the importer decides whether the whole file is rejected.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from app.common.validation import canonical_pair

from axiom_forex.data.normalization import parse_timestamp

from .contracts import (
    BAR_COLUMNS,
    BAR_RECORD,
    HEADER_COLUMNS,
    QUOTE_COLUMNS,
    QUOTE_RECORD,
    RECORD_TYPES,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    CsvFormat,
    ImportIssue,
)

COMMENT_PREFIX = "#"


@dataclass(frozen=True)
class SourceRecord:
    """One structurally valid CSV record, ready for normalization."""

    line: int
    record_type: str
    pair: str
    timestamp: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)
    declared_timezone: str = "UTC"


@dataclass(frozen=True)
class ParsedFile:
    format: CsvFormat
    records: tuple[SourceRecord, ...]
    issues: tuple[ImportIssue, ...]
    line_count: int
    header_present: bool
    line_types: Mapping[int, str] = field(default_factory=dict)

    @property
    def fatal(self) -> bool:
        """True when the file itself is unusable (header/format problems)."""

        return any(issue.code.startswith("header_") for issue in self.issues)

    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == SEVERITY_ERROR)


def _issue(
    line: int,
    field_name: str,
    code: str,
    detail: str,
    severity: str = SEVERITY_ERROR,
) -> ImportIssue:
    return ImportIssue(
        line=line,
        field=field_name,
        code=code,
        severity=severity,
        detail=detail,
    )


def split_rows(text: str) -> list[tuple[int, list[str]]]:
    """Return ``(line_number, cells)`` for every non-comment, non-blank row."""

    rows: list[tuple[int, list[str]]] = []
    reader = csv.reader(io.StringIO(text))
    for index, cells in enumerate(reader, start=1):
        if not cells:
            continue
        stripped = [cell.strip() for cell in cells]
        if not any(stripped):
            continue
        if stripped[0].startswith(COMMENT_PREFIX):
            continue
        rows.append((index, stripped))
    return rows


def _resolve_zone(name: str) -> ZoneInfo | None:
    text = name.strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except Exception:
        return None


def _roundtrips(aware: datetime, zone: ZoneInfo, naive: datetime) -> bool:
    return aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive


def resolve_timestamp(
    raw: Any,
    *,
    zone_name: str,
    field_name: str,
    line: int,
) -> tuple[datetime | None, list[ImportIssue]]:
    """Resolve one timestamp to UTC without guessing across DST boundaries."""

    issues: list[ImportIssue] = []
    text = "" if raw is None else str(raw).strip()
    if not text:
        issues.append(_issue(line, field_name, f"missing_{field_name}", "timestamp is required"))
        return None, issues
    zone = _resolve_zone(zone_name)
    if zone is None:
        issues.append(
            _issue(line, "timezone", "unknown_timezone", "timezone must be a known IANA zone or UTC")
        )
        return None, issues
    parsed, error = parse_timestamp(text, field_name)
    if parsed is None or error:
        # ``parse_timestamp`` requires an offset; retry as naive wall time.
        naive_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            candidate = datetime.fromisoformat(naive_text)
        except ValueError:
            issues.append(
                _issue(line, field_name, f"invalid_{field_name}", "timestamp must be ISO 8601")
            )
            return None, issues
        if candidate.tzinfo is not None:
            issues.append(
                _issue(line, field_name, f"invalid_{field_name}", "timestamp must be ISO 8601")
            )
            return None, issues
        first = candidate.replace(tzinfo=zone, fold=0)
        second = candidate.replace(tzinfo=zone, fold=1)
        first_ok = _roundtrips(first, zone, candidate)
        second_ok = _roundtrips(second, zone, candidate)
        if not first_ok and not second_ok:
            issues.append(
                _issue(
                    line,
                    field_name,
                    "nonexistent_local_timestamp",
                    "local wall time does not exist in the declared timezone",
                )
            )
            return None, issues
        if first_ok and second_ok and first.utcoffset() != second.utcoffset():
            issues.append(
                _issue(
                    line,
                    field_name,
                    "ambiguous_local_timestamp",
                    "local wall time is ambiguous; supply an explicit UTC offset",
                )
            )
            return None, issues
        aware = first if first_ok else second
        return aware.astimezone(timezone.utc), issues
    expected = parsed.astimezone(zone).utcoffset()
    if parsed.utcoffset() != expected:
        issues.append(
            _issue(
                line,
                field_name,
                "timestamp_offset_mismatch",
                "supplied UTC offset does not match the declared timezone at that instant",
            )
        )
        return None, issues
    return parsed, issues


def _validate_header(cells: Sequence[str], line: int) -> list[ImportIssue]:
    issues: list[ImportIssue] = []
    header = tuple(cell.strip() for cell in cells)
    duplicates = sorted({name for name in header if header.count(name) > 1})
    for name in duplicates:
        issues.append(
            _issue(line, "header", "header_duplicate_column", f"duplicate column {name}")
        )
    unknown = sorted(set(header) - set(HEADER_COLUMNS))
    for name in unknown:
        issues.append(
            _issue(line, "header", "header_unknown_column", f"unknown column {name}")
        )
    missing = [name for name in HEADER_COLUMNS if name not in header]
    for name in missing:
        issues.append(
            _issue(line, "header", "header_missing_column", f"missing column {name}")
        )
    return issues


def _require(cells: Mapping[str, str], name: str, line: int, issues: list[ImportIssue]) -> bool:
    if not cells.get(name, "").strip():
        issues.append(_issue(line, name, f"missing_{name}", f"{name} is required"))
        return False
    return True


def _positive_int(raw: str, name: str, line: int, issues: list[ImportIssue]) -> int | None:
    text = raw.strip()
    if not text:
        issues.append(_issue(line, name, f"missing_{name}", f"{name} is required"))
        return None
    try:
        value = int(text)
    except ValueError:
        issues.append(_issue(line, name, f"invalid_{name}", f"{name} must be an integer"))
        return None
    if value <= 0:
        issues.append(_issue(line, name, f"invalid_{name}", f"{name} must be positive"))
        return None
    return value


def _parse_complete(raw: str, line: int, issues: list[ImportIssue]) -> bool:
    text = raw.strip().lower()
    if not text:
        issues.append(_issue(line, "complete", "missing_complete", "complete flag is required"))
        return False
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        issues.append(
            _issue(line, "complete", "incomplete_bar", "only completed bars may be imported")
        )
        return False
    issues.append(
        _issue(line, "complete", "invalid_complete_flag", "complete must be true or false")
    )
    return False


def _reject_disallowed(
    cells: Mapping[str, str],
    names: Sequence[str],
    record_type: str,
    line: int,
    issues: list[ImportIssue],
) -> None:
    for name in names:
        if cells.get(name, "").strip():
            issues.append(
                _issue(
                    line,
                    name,
                    "field_not_allowed_for_record_type",
                    f"{name} is not allowed on a {record_type} record",
                )
            )


def _parse_quote(
    cells: Mapping[str, str],
    *,
    line: int,
    pair: str,
    timestamp: datetime,
    zone_name: str,
    source: str,
) -> tuple[SourceRecord | None, list[ImportIssue]]:
    issues: list[ImportIssue] = []
    _reject_disallowed(cells, BAR_COLUMNS, QUOTE_RECORD, line, issues)
    for name in ("bid", "ask"):
        if _require(cells, name, line, issues):
            try:
                float(cells[name])
            except ValueError:
                issues.append(_issue(line, name, f"invalid_{name}", f"{name} must be numeric"))
    _require(cells, "quote_id", line, issues)
    quote_id = cells.get("quote_id", "").strip()
    correction_of = cells.get("correction_of", "").strip()
    if correction_of and not quote_id:
        issues.append(
            _issue(line, "correction_of", "correction_without_quote_id", "corrections require a quote_id")
        )
    received_at: datetime | None = None
    if _require(cells, "received_at", line, issues) and cells.get("received_at", "").strip():
        received_at, received_issues = resolve_timestamp(
            cells["received_at"], zone_name=zone_name, field_name="received_at", line=line
        )
        issues.extend(received_issues)
        if received_at is not None and received_at < timestamp:
            issues.append(
                _issue(
                    line,
                    "received_at",
                    "receive_time_before_timestamp",
                    "received_at must not precede the quote timestamp",
                )
            )
    if issues:
        return None, issues
    payload: dict[str, Any] = {
        "timestamp": timestamp.isoformat(),
        "bid": cells["bid"].strip(),
        "ask": cells["ask"].strip(),
        "source": source,
        "quote_id": quote_id,
        "metadata": {},
    }
    if received_at is not None:
        payload["received_at"] = received_at.isoformat()
    if correction_of:
        payload["metadata"]["correction_of"] = correction_of
    return (
        SourceRecord(
            line=line,
            record_type=QUOTE_RECORD,
            pair=pair,
            timestamp=timestamp,
            payload=payload,
            declared_timezone=zone_name,
        ),
        issues,
    )


def _parse_bar(
    cells: Mapping[str, str],
    *,
    line: int,
    pair: str,
    timestamp: datetime,
    zone_name: str,
    source: str,
) -> tuple[SourceRecord | None, list[ImportIssue]]:
    issues: list[ImportIssue] = []
    _reject_disallowed(cells, QUOTE_COLUMNS, BAR_RECORD, line, issues)
    duration = _positive_int(cells.get("duration_seconds", ""), "duration_seconds", line, issues)
    quote_count = _positive_int(cells.get("quote_count", ""), "quote_count", line, issues)
    if _require(cells, "timeframe", line, issues) is False:
        pass
    points = ("open", "high", "low", "close")
    side: dict[str, dict[str, str]] = {}
    for name in ("bid", "ask"):
        values: dict[str, str] = {}
        for point in points:
            key = f"{name}_{point}"
            if _require(cells, key, line, issues):
                try:
                    float(cells[key])
                except ValueError:
                    issues.append(_issue(line, key, f"invalid_{key}", f"{key} must be numeric"))
                else:
                    values[point] = cells[key].strip()
        side[name] = values
    complete = _parse_complete(cells.get("complete", ""), line, issues)
    last_quote_at: datetime | None = None
    if _require(cells, "last_quote_at", line, issues):
        last_quote_at, last_issues = resolve_timestamp(
            cells["last_quote_at"], zone_name=zone_name, field_name="last_quote_at", line=line
        )
        issues.extend(last_issues)
    if last_quote_at is not None and duration is not None:
        if last_quote_at < timestamp:
            issues.append(
                _issue(
                    line,
                    "last_quote_at",
                    "last_quote_before_bar_start",
                    "last_quote_at must not precede the bar timestamp",
                )
            )
        elif last_quote_at.timestamp() > timestamp.timestamp() + duration:
            issues.append(
                _issue(
                    line,
                    "last_quote_at",
                    "last_quote_after_bar_end",
                    "last_quote_at must fall inside the bar interval",
                )
            )
    if issues:
        return None, issues
    assert duration is not None and quote_count is not None and last_quote_at is not None
    payload: dict[str, Any] = {
        "timestamp": timestamp.isoformat(),
        "timeframe": cells["timeframe"].strip(),
        "duration_seconds": duration,
        "quote_count": quote_count,
        "complete": complete,
        "source": source,
        "last_quote_at": last_quote_at.isoformat(),
        "bid": side["bid"],
        "ask": side["ask"],
    }
    return (
        SourceRecord(
            line=line,
            record_type=BAR_RECORD,
            pair=pair,
            timestamp=timestamp,
            payload=payload,
            declared_timezone=zone_name,
        ),
        issues,
    )


def parse_canonical_csv(
    text: str,
    *,
    source_label: str,
) -> ParsedFile:
    """Parse canonical CSV text into typed records plus findings."""

    issues: list[ImportIssue] = []
    rows = split_rows(text)
    header_present = bool(rows)
    if not rows:
        issues.append(
            _issue(0, "header", "header_missing", "file contains no header row")
        )
        return ParsedFile(
            format=CsvFormat(),
            records=(),
            issues=tuple(issues),
            line_count=0,
            header_present=False,
            line_types={},
        )
    header_line, header_cells = rows[0]
    issues.extend(_validate_header(header_cells, header_line))
    header = [cell.strip() for cell in header_cells]
    body = rows[1:]
    if not body:
        issues.append(_issue(header_line, "header", "header_only_file", "file has no records"))
    if any(issue.code.startswith("header_") for issue in issues):
        return ParsedFile(
            format=CsvFormat(),
            records=(),
            issues=tuple(issues),
            line_count=len(body),
            header_present=header_present,
            line_types={},
        )
    records: list[SourceRecord] = []
    line_types: dict[int, str] = {}
    for line, cells in body:
        if len(cells) != len(header):
            issues.append(
                _issue(
                    line,
                    "row",
                    "malformed_row",
                    f"expected {len(header)} fields, found {len(cells)}",
                )
            )
            continue
        row = dict(zip(header, cells))
        record_type = row.get("record_type", "").strip().upper()
        line_types[line] = record_type
        if record_type not in RECORD_TYPES:
            issues.append(
                _issue(line, "record_type", "unknown_record_type", "record_type must be QUOTE or BAR")
            )
            continue
        raw_pair = row.get("pair", "").strip()
        pair, pair_error = canonical_pair(raw_pair)
        if pair_error or pair is None:
            issues.append(_issue(line, "pair", "invalid_pair", "pair must be BASE/QUOTE"))
            continue
        zone_name = row.get("timezone", "").strip()
        if not zone_name:
            issues.append(_issue(line, "timezone", "missing_timezone", "timezone is required"))
            continue
        timestamp, timestamp_issues = resolve_timestamp(
            row.get("timestamp", ""), zone_name=zone_name, field_name="timestamp", line=line
        )
        if timestamp_issues:
            issues.extend(timestamp_issues)
            continue
        assert timestamp is not None
        parse = _parse_quote if record_type == QUOTE_RECORD else _parse_bar
        record, record_issues = parse(
            row,
            line=line,
            pair=pair,
            timestamp=timestamp,
            zone_name=zone_name,
            source=source_label,
        )
        issues.extend(record_issues)
        if record is not None:
            records.append(record)
    return ParsedFile(
        format=CsvFormat(),
        records=tuple(records),
        issues=tuple(issues),
        line_count=len(body),
        header_present=header_present,
        line_types=line_types,
    )


def warning(code: str, detail: str, line: int | None = None) -> ImportIssue:
    return _issue(line or 0, "record", code, detail, SEVERITY_WARNING)
