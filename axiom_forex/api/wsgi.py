"""Read-only WSGI callable; no server, deployment, credentials, or controls."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from app.audit.log import sanitize_audit_value


PROTECTED_PATHS = frozenset(
    {
        "/status",
        "/backtests",
        "/audit",
        "/orders",
        "/desktop",
        "/dashboard",
        "/report",
        "/workflow",
        "/conformance",
        "/profile",
        "/dataset",
        "/evaluation",
        "/settings/market-access",
    }
)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(sanitize_audit_value(payload), sort_keys=True).encode("utf-8")


def _respond(
    start_response: Callable[[str, list[tuple[str, str]]], Any],
    status: str,
    content_type: str,
    body: bytes,
) -> list[bytes]:
    start_response(
        status,
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "no-store"),
        ],
    )
    return [body]


def create_app(platform: Any) -> Callable[..., list[bytes]]:
    configured = platform.runtime_settings.api_bearer_token

    def app(
        environ: Mapping[str, Any],
        start_response: Callable[[str, list[tuple[str, str]]], Any],
    ) -> list[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        if method != "GET":
            return _respond(
                start_response,
                "405 Method Not Allowed",
                "application/json",
                _json_bytes({"error": "read_only_api"}),
            )
        if path == "/health":
            return _respond(
                start_response,
                "200 OK",
                "application/json",
                _json_bytes(platform.health().audit_view()),
            )
        if path not in PROTECTED_PATHS:
            return _respond(
                start_response,
                "404 Not Found",
                "application/json",
                _json_bytes({"error": "not_found", "path": path}),
            )
        if not configured:
            return _respond(
                start_response,
                "503 Service Unavailable",
                "application/json",
                _json_bytes({"error": "bearer_not_configured"}),
            )
        header = environ.get("HTTP_AUTHORIZATION", "")
        if header != "Bearer " + configured:
            return _respond(
                start_response,
                "401 Unauthorized",
                "application/json",
                _json_bytes({"error": "unauthorized"}),
            )
        if path == "/dashboard":
            # The dashboard renders the same portfolio figures as /status, so
            # it is gated by the same bearer token as every other
            # account-bearing surface.  Only /health stays public.
            body = platform.dashboard_html().encode("utf-8")
            return _respond(start_response, "200 OK", "text/html; charset=utf-8", body)
        if path == "/status":
            payload = platform.status()
        elif path == "/workflow":
            payload = platform.workflow_status()
        elif path == "/conformance":
            payload = platform.conformance_report()
        elif path == "/profile":
            payload = platform.provider_profile_report()
        elif path == "/dataset":
            payload = platform.dataset_provenance_report()
        elif path == "/evaluation":
            payload = platform.research_evaluation_summary()
        elif path == "/backtests":
            payload = {"items": platform.backtests()}
        elif path == "/audit":
            payload = {"items": platform.audit_events(limit=50)}
        elif path == "/orders":
            payload = {"items": platform.orders()}
        elif path == "/settings/market-access":
            # Masked status only: the value never leaves the OS credential vault,
            # and the settings writes are a desktop action, not an HTTP one.
            payload = platform.market_access_status()
        elif path == "/desktop":
            payload = platform.desktop_viewmodel()
        else:
            body = platform.report_markdown().encode("utf-8")
            return _respond(start_response, "200 OK", "text/markdown; charset=utf-8", body)
        return _respond(
            start_response, "200 OK", "application/json", _json_bytes(payload)
        )

    return app
