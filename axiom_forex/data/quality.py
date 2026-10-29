"""Calendar-aware bid/ask bar quality validation; no interpolation or fabrication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

from app.calendar.trading import TradingCalendar
from app.common.validation import as_utc
from app.data.models import ForexQuoteBar, QuoteFreshness
from app.universe.specs import CurrencyPairSpec


@dataclass(frozen=True)
class QualityFinding:
    severity: str
    code: str
    index: int
    detail: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "index": self.index,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class QualityReport:
    classification: str
    findings: tuple[QualityFinding, ...]
    evaluated_at: datetime
    bar_count: int

    @property
    def usable(self) -> bool:
        return self.classification in {"VALID", "WARNING"}

    def audit_view(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "usable": self.usable,
            "bar_count": self.bar_count,
            "evaluated_at": self.evaluated_at,
            "findings": [finding.audit_view() for finding in self.findings],
        }


def assess_bars(
    bars: Sequence[ForexQuoteBar],
    *,
    spec: CurrencyPairSpec,
    calendar: TradingCalendar,
    freshness: QuoteFreshness,
    evaluated_at: datetime,
) -> QualityReport:
    now, now_error = as_utc(evaluated_at, "evaluated_at")
    findings: list[QualityFinding] = []
    if not bars:
        return QualityReport(
            "INVALID",
            (QualityFinding("ERROR", "EMPTY_DATASET", 0, "no bars supplied"),),
            now or evaluated_at,
            0,
        )
    if now_error or now is None:
        findings.append(QualityFinding("ERROR", "INVALID_EVALUATION_TIME", 0, "bad evaluated_at"))
    for index, bar in enumerate(bars):
        validation = bar.validate(spec)
        if not validation.valid:
            findings.append(
                QualityFinding("ERROR", "INVALID_BAR", index, validation.reason)
            )
        try:
            bar_end = bar.end_at
        except Exception as exc:
            findings.append(
                QualityFinding(
                    "ERROR", "INVALID_BAR_INTERVAL", index, type(exc).__name__
                )
            )
            continue
        if now is not None and bar_end > now:
            findings.append(
                QualityFinding("ERROR", "FUTURE_BAR", index, "bar ends after evaluated_at")
            )
        quote_check = freshness.check_instant(bar.last_quote_at, bar_end)
        if not quote_check.ok:
            findings.append(
                QualityFinding(
                    "ERROR",
                    "STALE_OR_INVALID_LAST_QUOTE",
                    index,
                    quote_check.reason.value,
                )
            )
        try:
            start_state = calendar.status(spec.canonical_pair or "", bar.timestamp)
            end_state = calendar.status(
                spec.canonical_pair or "", bar_end - timedelta(microseconds=1)
            )
            if not start_state.is_open or not end_state.is_open:
                findings.append(
                    QualityFinding(
                        "ERROR",
                        "OUT_OF_SESSION_BAR",
                        index,
                        f"{start_state.status.value}/{end_state.status.value}",
                    )
                )
        except Exception as exc:
            findings.append(
                QualityFinding("ERROR", "CALENDAR_ERROR", index, type(exc).__name__)
            )
        if index > 0:
            previous = bars[index - 1]
            try:
                previous_end = previous.end_at
            except Exception:
                continue
            if bar.timestamp < previous_end:
                findings.append(
                    QualityFinding("ERROR", "DUPLICATE_OR_OVERLAP", index, "bar overlaps previous")
                )
                continue
            gap = bar.timestamp - previous_end
            if gap >= timedelta(seconds=previous.duration_seconds):
                samples = int(gap.total_seconds() // previous.duration_seconds) + 1
                if samples > 10000:
                    findings.append(
                        QualityFinding(
                            "ERROR", "GAP_TOO_LARGE_TO_VALIDATE", index, str(samples)
                        )
                    )
                    continue
                any_open = False
                sample = previous_end
                while sample < bar.timestamp:
                    try:
                        if calendar.status(spec.canonical_pair or "", sample).is_open:
                            any_open = True
                            break
                    except Exception as exc:
                        findings.append(
                            QualityFinding(
                                "ERROR", "CALENDAR_GAP_ERROR", index, type(exc).__name__
                            )
                        )
                        any_open = True
                        break
                    sample += timedelta(seconds=previous.duration_seconds)
                if any_open:
                    findings.append(
                        QualityFinding("ERROR", "MISSING_BAR", index, str(gap))
                    )
                else:
                    findings.append(
                        QualityFinding("WARNING", "EXPECTED_SESSION_GAP", index, str(gap))
                    )
    if any(finding.severity == "ERROR" for finding in findings):
        classification = "MISSING" if any(f.code == "MISSING_BAR" for f in findings) else "INVALID"
    elif findings:
        classification = "WARNING"
    else:
        classification = "VALID"
    return QualityReport(
        classification, tuple(findings), now or evaluated_at, len(bars)
    )
