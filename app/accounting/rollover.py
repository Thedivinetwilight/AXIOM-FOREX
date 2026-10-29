"""Explicit rollover schedule identity and rules; no financing is accrued here.

The triple-rollover weekday must be supplied explicitly (or ``None`` when the
broker has no triple rollover).  M1 validates the schedule and pins its
version; M3 will implement financing accrual against the broker calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta
from typing import Any

from app.common.validation import ValidationResult


@dataclass(frozen=True)
class RolloverPolicy:
    schedule_id: str
    version: str
    daily_cutoff_utc: time
    triple_rollover_weekday: int | None
    calendar_id: str
    source: str

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        for name in ("schedule_id", "version", "calendar_id", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"missing_{name}")
        cutoff = self.daily_cutoff_utc
        if not isinstance(cutoff, time):
            errors.append("invalid_daily_cutoff")
        elif cutoff.tzinfo is not None and cutoff.utcoffset() not in (timedelta(0), None):
            errors.append("daily_cutoff_must_be_utc")
        weekday = self.triple_rollover_weekday
        if weekday is not None:
            if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
                errors.append("invalid_triple_rollover_weekday")
        return ValidationResult.fail(*errors) if errors else ValidationResult.ok()

    def audit_view(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "version": self.version,
            "daily_cutoff_utc": self.daily_cutoff_utc,
            "triple_rollover_weekday": self.triple_rollover_weekday,
            "calendar_id": self.calendar_id,
            "source": self.source,
        }
