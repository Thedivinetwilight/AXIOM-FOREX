"""Explicit, DST-aware trading calendars.

A calendar is a versioned set of UTC session windows per canonical pair.  It
does not infer a generic 24/5 market: daily breaks, weekend gaps, holidays,
instrument availability, validity bounds, and timezone are explicit inputs
supplied by a broker calendar adapter or a local test fixture.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.common.validation import ValidationResult, as_utc, canonical_pair


class MarketStatus(str, Enum):
    OPEN = "OPEN"
    NOT_YET_VALID = "NOT_YET_VALID"
    CALENDAR_EXPIRED = "CALENDAR_EXPIRED"
    INSTRUMENT_UNAVAILABLE = "INSTRUMENT_UNAVAILABLE"
    HOLIDAY = "HOLIDAY"
    WEEKEND_GAP = "WEEKEND_GAP"
    DAILY_BREAK = "DAILY_BREAK"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CalendarState:
    status: MarketStatus
    reason: str
    calendar_id: str
    version: str
    signature: str
    pair: str | None
    evaluated_at: datetime | None
    local_date: date | None = None
    next_open_at: datetime | None = None
    window_label: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == MarketStatus.OPEN

    def audit_view(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "calendar_id": self.calendar_id,
            "version": self.version,
            "signature": self.signature,
            "pair": self.pair,
            "evaluated_at": self.evaluated_at,
            "local_date": self.local_date,
            "next_open_at": self.next_open_at,
            "window_label": self.window_label,
        }


@dataclass(frozen=True)
class CalendarWindow:
    start: datetime
    end: datetime
    label: str = "session"

    def validate(self, valid_from: datetime, valid_until: datetime) -> ValidationResult:
        errors: list[str] = []
        start, start_error = as_utc(self.start, "window_start")
        end, end_error = as_utc(self.end, "window_end")
        valid_start, from_error = as_utc(valid_from, "valid_from")
        valid_end, until_error = as_utc(valid_until, "valid_until")
        if start_error or start is None:
            errors.append(start_error or "invalid_window_start")
        if end_error or end is None:
            errors.append(end_error or "invalid_window_end")
        if from_error or until_error or valid_start is None or valid_end is None:
            errors.append("invalid_calendar_validity")
        if not isinstance(self.label, str) or not self.label.strip():
            errors.append("missing_window_label")
        if start is not None and end is not None:
            if end <= start:
                errors.append("window_end_not_after_start")
            if valid_start is not None and valid_end is not None:
                if start < valid_start or end > valid_end:
                    errors.append("window_outside_calendar_validity")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()


@runtime_checkable
class TradingCalendar(Protocol):
    calendar_id: str
    version: str
    source: str

    def validate(self) -> ValidationResult: ...

    def status(self, pair: str, at: datetime) -> CalendarState: ...

    def signature(self) -> str: ...


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)


@dataclass(frozen=True)
class ExplicitTradingCalendar:
    calendar_id: str
    version: str
    source: str
    timezone_name: str
    valid_from: datetime
    valid_until: datetime
    windows_by_pair: Mapping[str, tuple[CalendarWindow, ...]]
    holidays_by_pair: Mapping[str, frozenset[date]]

    def __post_init__(self) -> None:
        windows = {
            pair: tuple(windows)
            for pair, windows in self.windows_by_pair.items()
        }
        holidays = {
            pair: frozenset(holidays)
            for pair, holidays in self.holidays_by_pair.items()
        }
        object.__setattr__(self, "windows_by_pair", MappingProxyType(windows))
        object.__setattr__(self, "holidays_by_pair", MappingProxyType(holidays))

    @property
    def instruments(self) -> tuple[str, ...]:
        return tuple(sorted(self.windows_by_pair))

    def _zone(self) -> ZoneInfo | None:
        try:
            return ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return None

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        for name in ("calendar_id", "version", "source", "timezone_name"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"missing_{name}")
        if self._zone() is None:
            errors.append("unknown_timezone")
        valid_from, from_error = as_utc(self.valid_from, "valid_from")
        valid_until, until_error = as_utc(self.valid_until, "valid_until")
        if from_error or valid_from is None:
            errors.append(from_error or "invalid_valid_from")
        if until_error or valid_until is None:
            errors.append(until_error or "invalid_valid_until")
        if valid_from is not None and valid_until is not None and valid_until <= valid_from:
            errors.append("empty_calendar_validity")
        if not self.windows_by_pair:
            errors.append("no_instrument_windows")
        for pair, windows in self.windows_by_pair.items():
            canonical, pair_error = canonical_pair(pair)
            if pair_error or canonical is None:
                errors.append(f"invalid_calendar_pair:{pair}")
                continue
            if pair != canonical:
                errors.append(f"non_canonical_calendar_pair:{pair}")
            if not windows:
                errors.append(f"no_windows_for:{pair}")
            previous_end: datetime | None = None
            for window in sorted(windows, key=lambda item: item.start):
                if not isinstance(window, CalendarWindow):
                    errors.append(f"invalid_window_for:{pair}")
                    continue
                if valid_from is not None and valid_until is not None:
                    result = window.validate(valid_from, valid_until)
                    errors.extend(result.errors)
                start, start_error = as_utc(window.start, "window_start")
                end, end_error = as_utc(window.end, "window_end")
                if start is not None and end is not None and not start_error and not end_error:
                    if previous_end is not None and start < previous_end:
                        errors.append(f"overlapping_windows_for:{pair}")
                    previous_end = max(previous_end or end, end)
        for pair, holidays in self.holidays_by_pair.items():
            canonical, pair_error = canonical_pair(pair)
            if pair_error or canonical is None or pair != canonical:
                errors.append(f"invalid_holiday_pair:{pair}")
            for holiday in holidays:
                if not isinstance(holiday, date) or isinstance(holiday, datetime):
                    errors.append(f"invalid_holiday_for:{pair}")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def signature(self) -> str:
        payload = {
            "calendar_id": self.calendar_id,
            "version": self.version,
            "source": self.source,
            "timezone": self.timezone_name,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "windows": {
                pair: [
                    {"start": window.start, "end": window.end, "label": window.label}
                    for window in sorted(windows, key=lambda item: item.start)
                ]
                for pair, windows in self.windows_by_pair.items()
            },
            "holidays": {
                pair: sorted(day.isoformat() if isinstance(day, date) else str(day)
                             for day in holidays)
                for pair, holidays in self.holidays_by_pair.items()
            },
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=_json_default
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _local_day_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        zone = self._zone()
        if zone is None:
            return None
        try:
            start_local = datetime.combine(day, time.min, tzinfo=zone)
            end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
            return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            return None

    def status(self, pair: str, at: datetime) -> CalendarState:
        signature = self.signature()
        base = {
            "calendar_id": self.calendar_id,
            "version": self.version,
            "signature": signature,
            "pair": pair,
            "evaluated_at": at,
        }
        validation = self.validate()
        if not validation.valid:
            return CalendarState(
                MarketStatus.UNKNOWN,
                "invalid_calendar:" + validation.reason,
                **base,
            )
        canonical, pair_error = canonical_pair(pair)
        at_utc, at_error = as_utc(at, "evaluated_at")
        if at_error or at_utc is None:
            return CalendarState(
                MarketStatus.UNKNOWN,
                "invalid_evaluation_time",
                **base,
            )
        valid_from, _ = as_utc(self.valid_from, "valid_from")
        valid_until, _ = as_utc(self.valid_until, "valid_until")
        assert valid_from is not None and valid_until is not None
        if at_utc < valid_from:
            return CalendarState(
                MarketStatus.NOT_YET_VALID, "before_calendar_validity", **base
            )
        if at_utc >= valid_until:
            return CalendarState(
                MarketStatus.CALENDAR_EXPIRED, "calendar_expired", **base
            )
        if pair_error or canonical is None or canonical not in self.windows_by_pair:
            return CalendarState(
                MarketStatus.INSTRUMENT_UNAVAILABLE,
                "instrument_not_in_calendar",
                **base,
            )
        zone = self._zone()
        assert zone is not None
        local_date = at_utc.astimezone(zone).date()
        next_open_at = min(
            (
                window.start
                for window in self.windows_by_pair[canonical]
                if isinstance(window, CalendarWindow)
                and isinstance(window.start, datetime)
                and as_utc(window.start, "window_start")[0] is not None
                and as_utc(window.start, "window_start")[0] > at_utc
            ),
            default=None,
        )
        state_base = {**base, "local_date": local_date, "next_open_at": next_open_at}
        if local_date in self.holidays_by_pair.get(canonical, frozenset()):
            return CalendarState(MarketStatus.HOLIDAY, "calendar_holiday", **state_base)
        for window in self.windows_by_pair[canonical]:
            start, _ = as_utc(window.start, "window_start")
            end, _ = as_utc(window.end, "window_end")
            if start is not None and end is not None and start <= at_utc < end:
                return CalendarState(
                    MarketStatus.OPEN,
                    "session_open",
                    window_label=window.label,
                    **state_base,
                )
        bounds = self._local_day_bounds(local_date)
        if bounds is not None:
            day_start, day_end = bounds
            day_windows = [
                window
                for window in self.windows_by_pair[canonical]
                if isinstance(window, CalendarWindow)
                and as_utc(window.start, "window_start")[0] is not None
                and as_utc(window.end, "window_end")[0] is not None
                and as_utc(window.start, "window_start")[0] < day_end
                and as_utc(window.end, "window_end")[0] > day_start
            ]
            if day_windows:
                before = any(
                    as_utc(window.end, "window_end")[0] is not None
                    and as_utc(window.end, "window_end")[0] <= at_utc
                    for window in day_windows
                )
                after = any(
                    as_utc(window.start, "window_start")[0] is not None
                    and as_utc(window.start, "window_start")[0] > at_utc
                    for window in day_windows
                )
                if before and after:
                    return CalendarState(
                        MarketStatus.DAILY_BREAK, "daily_break", **state_base
                    )
        if local_date.weekday() >= 5:
            return CalendarState(
                MarketStatus.WEEKEND_GAP, "weekend_gap", **state_base
            )
        return CalendarState(MarketStatus.CLOSED, "outside_session", **state_base)

    def is_open(self, pair: str, at: datetime) -> bool:
        return self.status(pair, at).is_open

    def audit_view(self) -> dict[str, Any]:
        return {
            "calendar_id": self.calendar_id,
            "version": self.version,
            "source": self.source,
            "timezone": self.timezone_name,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "signature": self.signature(),
            "instruments": self.instruments,
        }
