"""Detect safety-relevant anomalies from local records only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Anomaly:
    severity: str
    code: str
    detail: str

    def audit_view(self) -> dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "detail": self.detail}


def detect_anomalies(
    *,
    audit_events: Sequence[Mapping[str, Any]],
    order_records: Sequence[Mapping[str, Any]],
    calendar_open: bool,
    quality_usable: bool,
    quote_fresh: bool,
    rejection_threshold: int = 3,
) -> tuple[Anomaly, ...]:
    anomalies: list[Anomaly] = []
    if not calendar_open:
        anomalies.append(Anomaly("WARNING", "MARKET_CLOSED", "calendar status is not open"))
    if not quality_usable:
        anomalies.append(Anomaly("ERROR", "DATA_QUALITY_UNUSABLE", "bar quality failed closed"))
    if not quote_fresh:
        anomalies.append(Anomaly("ERROR", "QUOTE_NOT_FRESH", "latest quote freshness failed"))
    unresolved = [
        record for record in order_records if bool(record.get("is_unresolved", False))
    ]
    if unresolved:
        anomalies.append(
            Anomaly("ERROR", "UNRESOLVED_ORDER", str(len(unresolved)))
        )
    rejections = [
        event
        for event in audit_events
        if event.get("event_type") == "PRE_TRADE_DECISION"
        and event.get("allowed") is False
    ]
    if len(rejections) >= rejection_threshold:
        anomalies.append(
            Anomaly("WARNING", "REPEATED_GATE_REJECTIONS", str(len(rejections)))
        )
    live_blocks = [
        event
        for event in audit_events
        if event.get("event_type") == "LOCAL_PAPER_LIVE_REQUEST_BLOCKED"
    ]
    if live_blocks:
        anomalies.append(
            Anomaly("ERROR", "LIVE_REQUEST_BLOCKED", str(len(live_blocks)))
        )
    return tuple(anomalies)
