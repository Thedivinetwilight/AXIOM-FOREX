import json
from dataclasses import replace

from axiom_forex.dashboard.view import render_dashboard
from axiom_forex.reporting.report import render_markdown_report
from desktop.contract import contract_audit


def call_app(app, path, *, method="GET", auth=None):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ = {"PATH_INFO": path, "REQUEST_METHOD": method}
    if auth is not None:
        environ["HTTP_AUTHORIZATION"] = auth
    body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], body


def test_public_health_and_authenticated_dashboard(platform):
    app = platform.create_wsgi_app()
    status, headers, body = call_app(app, "/health")
    assert status.startswith("200")
    assert headers["Content-Type"] == "application/json"
    health = json.loads(body)
    assert health["research_only"] is True
    assert health["live_bridge_enabled"] is False
    assert health["status"] == "HEALTHY"

    # The dashboard renders portfolio figures, so it is token-gated like /status.
    status, _, _ = call_app(app, "/dashboard")
    assert status.startswith("401")

    auth = "Bearer " + platform.runtime_settings.api_bearer_token
    status, headers, body = call_app(app, "/dashboard", auth=auth)
    assert status.startswith("200")
    assert headers["Content-Type"].startswith("text/html")
    html = body.decode("utf-8")
    assert "Research/paper only" in html
    assert "No order controls" in html


def test_protected_status_requires_bearer_and_exposes_no_secret(platform):
    app = platform.create_wsgi_app()
    good_auth = "Bearer " + platform.runtime_settings.api_bearer_token
    status, _, _ = call_app(app, "/status")
    assert status.startswith("401")
    status, _, body = call_app(app, "/status", auth=good_auth)
    assert status.startswith("200")
    payload = json.loads(body)
    assert payload["strategy_active"] is False
    assert payload["external_broker"]["enabled"] is False
    assert platform.runtime_settings.api_bearer_token not in body.decode("utf-8")


def test_protected_read_only_endpoints(platform):
    app = platform.create_wsgi_app()
    good_auth = "Bearer " + platform.runtime_settings.api_bearer_token
    for path in ("/backtests", "/audit", "/orders", "/desktop"):
        status, _, _ = call_app(app, path, auth=good_auth)
        assert status.startswith("200"), path

    status, headers, body = call_app(app, "/desktop", auth=good_auth)
    payload = json.loads(body)
    assert payload["live_controls"] == []
    assert payload["mode"] == "research_and_paper_only"

    status, headers, body = call_app(app, "/report", auth=good_auth)
    assert status.startswith("200")
    assert headers["Content-Type"].startswith("text/markdown")
    assert "not a claim of profitability" in body.decode("utf-8")


def test_api_rejects_post_and_unknown_paths(platform):
    app = platform.create_wsgi_app()
    status, _, _ = call_app(app, "/health", method="POST")
    assert status.startswith("405")
    status, _, _ = call_app(app, "/unknown")
    assert status.startswith("404")
    assert all(request["method"] == "GET" for request in contract_audit())


def test_missing_bearer_configuration_returns_503(platform):
    platform.runtime_settings = replace(
        platform.runtime_settings, api_bearer_token=""
    )
    app = platform.create_wsgi_app()
    status, _, body = call_app(app, "/status")
    assert status.startswith("503")
    assert json.loads(body)["error"] == "bearer_not_configured"


def test_dashboard_and_report_redact_injected_secrets(platform):
    status = platform.status()
    status["api_token"] = "must-not-appear"
    status["url"] = "https://example.invalid/path"
    dashboard = render_dashboard(status)
    report = render_markdown_report(status)
    assert "must-not-appear" not in dashboard
    assert "example.invalid" not in dashboard
    assert "must-not-appear" not in report
    assert "example.invalid" not in report


def test_desktop_contract_has_only_get_requests(platform):
    requests = contract_audit()
    assert requests
    assert {request["method"] for request in requests} == {"GET"}
    assert {"health", "status", "backtests", "orders"} <= {
        request["name"] for request in requests
    }
    assert platform.desktop_viewmodel()["live_controls"] == []


def test_research_evaluation_endpoint_is_read_only_and_isolated(platform):
    app = platform.create_wsgi_app()
    good_auth = "Bearer " + platform.runtime_settings.api_bearer_token

    status, _, _ = call_app(app, "/evaluation", auth=good_auth)
    assert status.startswith("200")
    status, _, body = call_app(app, "/evaluation", auth=good_auth)
    payload = json.loads(body)
    assert payload["status"] == "NOT_RUN"
    assert payload["execution_isolation"] == {
        "creates_orders": False,
        "touches_paper_order_stream": False,
        "external_execution_enabled": False,
        "paper_broker_used": False,
    }
    # Stress scenarios are opt-in: the read-only view reports NOT_RUN and
    # carries the same isolation guarantees, never auto-running a suite.
    assert payload["stress"]["status"] == "NOT_RUN"
    assert payload["stress"]["mode"] == "HYPOTHETICAL_STRESS"
    assert payload["stress"]["execution_isolation"]["qualifies_strategy"] is False
    assert payload["stress"]["execution_isolation"]["activates_strategy"] is False
    assert (
        payload["stress"]["execution_isolation"]["claims_out_of_sample_window"]
        is False
    )

    # The evaluation view is exposed through the read-only GET API only.
    status, _, _ = call_app(app, "/evaluation", method="POST", auth=good_auth)
    assert status.startswith("405")
    assert all(request["method"] == "GET" for request in contract_audit())


def test_status_and_report_carry_the_evaluation_section_without_running_it(platform):
    status_payload = platform.status()
    assert status_payload["research_evaluation"]["status"] == "NOT_RUN"
    assert platform.report_markdown().count("Research evaluation") <= 1
    # Running verification must not start an evaluation as a side effect.
    verification = platform.verify()
    assert verification["checks"]["research_evaluation_isolated"] is True
    assert platform.research_evaluation_summary()["status"] == "NOT_RUN"
