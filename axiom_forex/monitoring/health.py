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
    # ``detail`` is read on the health page as the reason a check is what it is, so
    # it says a sentence rather than a Python literal.  "OK / False" reads as a
    # contradiction: the check named *disabled* is healthy exactly when the flag is
    # off, and nothing on the page said so.
    checks.append(
        {
            "name": "live_bridge_disabled",
            "ok": not settings.live_bridge_enabled,
            "detail": (
                "no live path can send an order"
                if not settings.live_bridge_enabled
                else "a live path is open; it must be closed"
            ),
        }
    )
    checks.append(
        {
            "name": "data_quality",
            "ok": quality_usable,
            "detail": (
                "bars and quotes pass the gate" if quality_usable else "the gate refuses this data"
            ),
        }
    )
    if not quality_usable:
        unsafe = True
    checks.append(
        {
            "name": "market_open",
            "ok": calendar_open,
            "detail": "inside the session calendar" if calendar_open else "outside the session calendar",
        }
    )
    if not calendar_open:
        degraded = True
    checks.append(
        {
            "name": "quote_freshness",
            "ok": quote_fresh,
            "detail": "inside the allowed age" if quote_fresh else "older than the allowed age",
        }
    )
    if not quote_fresh:
        unsafe = True
    gate_ok = gate_allowed if gate_allowed is not None else True
    checks.append(
        {
            "name": "last_gate_decision",
            "ok": gate_ok,
            "detail": (
                "no decision has been taken yet"
                if gate_allowed is None
                else (
                    "the last decision allowed an order"
                    if gate_allowed
                    else "the last decision refused an order"
                )
            ),
        }
    )
    if gate_allowed is False:
        degraded = True
    checks.append(
        {
            "name": "unresolved_orders",
            "ok": unresolved_orders == 0,
            "detail": (
                "every order reached a final state"
                if unresolved_orders == 0
                else f"{unresolved_orders} order(s) left unresolved"
            ),
        }
    )
    if unresolved_orders:
        unsafe = True
    checks.append(
        {
            "name": "persistence",
            "ok": repository_health.get("schema_version")
            == repository_health.get("expected_schema_version"),
            "detail": (
                f"schema {repository_health.get('schema_version')} of "
                f"{repository_health.get('expected_schema_version')} expected"
            ),
        }
    )
    if conformance_passed is not None:
        checks.append(
            {
                "name": "provider_broker_conformance",
                "ok": conformance_passed,
                "detail": (
                    "every required method is present"
                    if conformance_passed
                    else "a required method is missing or fails"
                ),
            }
        )
        if not conformance_passed:
            unsafe = True
    if profile_activation_allowed is not None:
        checks.append(
            {
                "name": "provider_profile_onboarding",
                "ok": profile_activation_allowed,
                "detail": (
                    "the profile may be activated"
                    if profile_activation_allowed
                    else "profile activation is refused"
                ),
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
