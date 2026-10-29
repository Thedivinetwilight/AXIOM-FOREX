"""Aggregate local health checks with an explicit fail-closed status."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from axiom_forex.config.runtime import ForexRuntimeSettings

from .anomalies import Anomaly


@dataclass(frozen=True)
class HealthSnapshot:
    status: str
    checks: tuple[dict[str, Any], ...]
    research_only: bool
    live_bridge_enabled: bool
    generated_at: datetime

    @property
    def healthy(self) -> bool:
        return self.status == "HEALTHY"

    def audit_view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [dict(check) for check in self.checks],
            "research_only": self.research_only,
            "live_bridge_enabled": self.live_bridge_enabled,
            "generated_at": self.generated_at,
            "healthy": self.healthy,
        }


def build_health(
    *,
    settings: ForexRuntimeSettings,
    quality_usable: bool,
    calendar_open: bool,
    quote_fresh: bool,
    gate_allowed: bool | None,
    unresolved_orders: int,
    anomalies: Sequence[Anomaly],
    repository_health: Mapping[str, Any],
    conformance_passed: bool | None = None,
    profile_activation_allowed: bool | None = None,
    generated_at: datetime | None = None,
) -> HealthSnapshot:
    checks: list[dict[str, Any]] = []
    unsafe = False
    degraded = False
    settings_validation = settings.validate()
    if not settings_validation.valid:
        unsafe = True
    checks.append(
        {
            "name": "research_only_boundary",
            "ok": settings_validation.valid,
            "detail": settings_validation.reason,
        }
    )
    if settings.live_bridge_enabled or not settings.research_only:
        unsafe = True
    checks.append(
        {
            "name": "live_bridge_disabled",
            "ok": not settings.live_bridge_enabled,
            "detail": str(settings.live_bridge_enabled),
        }
    )
    checks.append(
        {"name": "data_quality", "ok": quality_usable, "detail": str(quality_usable)}
    )
    if not quality_usable:
        unsafe = True
    checks.append({"name": "market_open", "ok": calendar_open, "detail": str(calendar_open)})
    if not calendar_open:
        degraded = True
    checks.append({"name": "quote_freshness", "ok": quote_fresh, "detail": str(quote_fresh)})
    if not quote_fresh:
        unsafe = True
    gate_ok = gate_allowed if gate_allowed is not None else True
    checks.append({"name": "last_gate_decision", "ok": gate_ok, "detail": str(gate_allowed)})
    if gate_allowed is False:
        degraded = True
    checks.append(
        {"name": "unresolved_orders", "ok": unresolved_orders == 0, "detail": str(unresolved_orders)}
    )
    if unresolved_orders:
        unsafe = True
    checks.append(
        {
            "name": "persistence",
            "ok": repository_health.get("schema_version")
            == repository_health.get("expected_schema_version"),
            "detail": str(repository_health.get("schema_version")),
        }
    )
    if conformance_passed is not None:
        checks.append(
            {
                "name": "provider_broker_conformance",
                "ok": conformance_passed,
                "detail": str(conformance_passed),
            }
        )
        if not conformance_passed:
            unsafe = True
    if profile_activation_allowed is not None:
        checks.append(
            {
                "name": "provider_profile_onboarding",
                "ok": profile_activation_allowed,
                "detail": str(profile_activation_allowed),
            }
        )
        if not profile_activation_allowed:
            unsafe = True
    for anomaly in anomalies:
        checks.append(
            {
                "name": anomaly.code,
                "ok": anomaly.severity != "ERROR",
                "detail": anomaly.detail,
            }
        )
        if anomaly.severity == "ERROR":
            unsafe = True
        elif anomaly.severity == "WARNING":
            degraded = True
    status = "UNSAFE" if unsafe else "DEGRADED" if degraded else "HEALTHY"
    return HealthSnapshot(
        status,
        tuple(checks),
        settings.research_only,
        settings.live_bridge_enabled,
        generated_at or datetime.now(timezone.utc),
    )
