from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DesktopRequest:
    name: str
    method: str
    path: str
    auth_required: bool
    description: str


DESKTOP_REQUESTS: tuple[DesktopRequest, ...] = (
    DesktopRequest("health", "GET", "/health", False, "Public paper-only health summary"),
    DesktopRequest("dashboard", "GET", "/dashboard", True, "Authenticated read-only HTML dashboard"),
    DesktopRequest("status", "GET", "/status", True, "Authenticated status view"),
    DesktopRequest("workflow", "GET", "/workflow", True, "Authenticated offline paper workflow result"),
    DesktopRequest("conformance", "GET", "/conformance", True, "Authenticated offline provider/broker conformance report"),
    DesktopRequest("profile", "GET", "/profile", True, "Authenticated safe provider profile onboarding status"),
    DesktopRequest("dataset", "GET", "/dataset", True, "Authenticated data class and dataset provenance view"),
    DesktopRequest("backtests", "GET", "/backtests", True, "Authenticated backtest list"),
    DesktopRequest("audit", "GET", "/audit", True, "Authenticated sanitized audit list"),
    DesktopRequest("orders", "GET", "/orders", True, "Authenticated read-only paper orders"),
    DesktopRequest("desktop", "GET", "/desktop", True, "Authenticated desktop view model"),
    DesktopRequest(
        "market_access",
        "GET",
        "/settings/market-access",
        True,
        "Authenticated market access token status (configured or not; never the value)",
    ),
    DesktopRequest("report", "GET", "/report", True, "Authenticated Markdown report"),
)


def contract_audit() -> list[dict[str, Any]]:
    return [asdict(request) for request in DESKTOP_REQUESTS]
