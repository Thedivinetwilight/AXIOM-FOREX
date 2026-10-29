"""Focused offline provider profile onboarding and workflow-gate tests."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

from axiom_forex import build_fixture_platform
from axiom_forex.cli import main
from axiom_forex.conformance.contracts import REQUIRED_PROVIDER_VALUES
from axiom_forex.profiles import (
    DEFAULT_FIXTURE_PROFILE_PATH,
    PROFILE_SCHEMA_VERSION,
    ProviderProfileOnboarding,
    build_fixture_profile_payload,
    build_template_profile_payload,
    load_provider_profile,
    validate_provider_profile,
)


def call_app(app, path, *, auth=None, method="GET"):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ = {"PATH_INFO": path, "REQUEST_METHOD": method}
    if auth is not None:
        environ["HTTP_AUTHORIZATION"] = auth
    body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], body


def _report(payload, *, evaluated_at=None):
    return validate_provider_profile(
        load_provider_profile(payload), evaluated_at=evaluated_at
    )


def _codes(report):
    return {finding.code for finding in report.findings}


def _set_term_value(payload, key, value):
    changed = deepcopy(payload)
    changed["terms"][key]["value"] = value
    return changed


def _set_nested(payload, key, name, value):
    changed = deepcopy(payload)
    changed["terms"][key]["value"][name] = value
    return changed


def test_fixture_profile_round_trip_and_versioning():
    payload = build_fixture_profile_payload()
    profile = load_provider_profile(payload)

    assert profile.schema_version == PROFILE_SCHEMA_VERSION
    assert profile.parse_issues == ()
    assert set(profile.terms) == {item.key for item in REQUIRED_PROVIDER_VALUES}
    assert profile.to_dict() == payload

    encoded = json.dumps(profile.to_dict(), sort_keys=True)
    reloaded = load_provider_profile(json.loads(encoded))
    assert reloaded.to_dict() == profile.to_dict()

    report = validate_provider_profile(profile)
    assert report.passed is True
    assert report.status == "PASS"
    assert report.activation_allowed is True
    assert report.activation_status == "ACTIVATED_FOR_LOCAL_PAPER"
    assert report.check_count >= 8
    assert len(report.term_status) == len(REQUIRED_PROVIDER_VALUES) == 37
    assert {row["status"] for row in report.term_status} == {"PRESENT"}
    assert all(row["value_included"] is False for row in report.term_status)
    assert all(
        row["provenance"]["value_class"] == "FIXTURE"
        for row in report.term_status
    )
    serialized_report = json.dumps(report.audit_view())
    assert "SYNTHETIC_FEED" not in serialized_report
    assert "AAA/BBB" not in serialized_report

    unsupported_version = deepcopy(payload)
    unsupported_version["schema_version"] = "2.0.0"
    version_report = _report(unsupported_version)
    assert version_report.passed is False
    assert version_report.activation_allowed is False
    assert "unsupported_schema_version" in _codes(version_report)


def test_empty_template_covers_all_required_values_and_fails_closed():
    template = build_template_profile_payload()
    required_keys = {item.key for item in REQUIRED_PROVIDER_VALUES}

    assert set(template["terms"]) == required_keys
    assert all(term["value"] is None for term in template["terms"].values())
    assert all(
        all(value == "" for value in term["provenance"].values())
        for term in template["terms"].values()
    )
    assert template["profile"]["environment"] == "UNSPECIFIED"
    assert template["profile"]["external_execution_enabled"] is False

    report = _report(template)
    view = report.audit_view()
    assert report.passed is False
    assert report.activation_allowed is False
    assert required_keys.issubset(set(report.missing_fields))
    assert len(report.missing_fields) >= 37
    assert view["validation"]["status"] == "FAIL"
    assert view["activation"]["status"] == "BLOCKED"
    assert len(view["required_terms"]) == 37
    assert all(row["value_included"] is False for row in view["required_terms"])
    json.dumps(view)


def test_incomplete_and_contradictory_profiles_block_activation():
    fixture = build_fixture_profile_payload()

    incomplete = _set_term_value(fixture, "market.provider_id", None)
    incomplete_report = _report(incomplete)
    assert incomplete_report.passed is False
    assert "market.provider_id" in incomplete_report.missing_fields
    assert incomplete_report.activation_allowed is False

    contradictory = deepcopy(fixture)
    contradictory["profile"]["synthetic"] = False
    contradictory_report = _report(contradictory)
    assert contradictory_report.passed is False
    assert "fixture_not_marked_synthetic" in _codes(contradictory_report)
    assert contradictory_report.activation_allowed is False

    production_like = deepcopy(fixture)
    production_like["profile"]["environment"] = "PRODUCTION"
    production_like["profile"]["label"] = "TEST_ONLY_COMPLETE_PROFILE"
    production_like["profile"]["synthetic"] = False
    for term in production_like["terms"].values():
        term["provenance"]["value_class"] = "PRODUCTION"
    production_report = _report(production_like)
    assert production_report.passed is True
    assert production_report.activation_allowed is False
    assert production_report.activation_reason == "only_synthetic_fixture_profiles_can_activate"
    assert production_report.activation_status == "BLOCKED"


def test_stale_terms_and_profile_validity_are_reported_by_field():
    fixture = build_fixture_profile_payload()
    stale_term = deepcopy(fixture)
    stale_term["terms"]["broker.capability_version"]["provenance"]["expires_at"] = (
        "2020-01-01T00:00:00+00:00"
    )
    stale_report = _report(stale_term)
    assert stale_report.passed is False
    assert "broker.capability_version" in stale_report.stale_fields
    assert "term_expired" in _codes(stale_report)
    assert stale_report.activation_allowed is False

    expired_profile = deepcopy(fixture)
    expired_profile["profile"]["expires_at"] = "2020-01-01T00:00:00+00:00"
    expired_report = _report(expired_profile)
    assert expired_report.passed is False
    assert "profile.expires_at" in expired_report.stale_fields
    assert "profile_expired" in _codes(expired_report)
    assert expired_report.activation_allowed is False


def test_compatibility_failures_are_field_scoped_and_block_activation():
    fixture = build_fixture_profile_payload()
    cases = (
        (
            _set_nested(fixture, "broker.sizing", "pip_size", "0.001"),
            "pair_specs_and_aliases",
        ),
        (
            _set_nested(fixture, "broker.trading_hours", "supported_timeframes", ["5m"]),
            "timeframes_and_bar_semantics",
        ),
        (
            _set_term_value(fixture, "market.bid_ask_semantics", "MID_ONLY"),
            "quote_semantics",
        ),
        (
            _set_nested(fixture, "broker.account_currency", "conversion_rate_pairs", [["BBB", "CCC"]]),
            "account_currency_and_conversion",
        ),
        (
            _set_nested(fixture, "market.calendar", "timezone", "UTC"),
            "calendar_compatibility",
        ),
        (
            _set_nested(fixture, "broker.margin", "leverage", "20"),
            "costs_and_margin",
        ),
        (
            _set_nested(fixture, "broker.recovery", "no_resubmit_policy", "ALLOW_RESUBMIT"),
            "order_capabilities_and_recovery",
        ),
    )
    for payload, expected_code in cases:
        report = _report(payload)
        assert report.passed is False, expected_code
        assert expected_code in report.compatibility_failures, expected_code
        assert report.activation_allowed is False, expected_code
        view = report.audit_view()
        assert expected_code in view["compatibility_failures"]
        json.dumps(view)


def test_safe_operator_view_redacts_sensitive_values_and_sources():
    fixture = build_fixture_profile_payload()
    sensitive = "SENSITIVE_ACCOUNT_VALUE"
    sensitive_source = "OFFICIAL_DOCS_WITH_ACCOUNT_ID"
    injected = deepcopy(fixture)
    injected["terms"]["broker.margin"]["value"]["account_id"] = sensitive
    injected["terms"]["broker.margin"]["value"]["balance"] = "SENSITIVE_BALANCE_VALUE"
    injected["terms"]["broker.margin"]["provenance"]["source"] = sensitive_source

    report = _report(injected)
    view = report.audit_view()
    serialized = json.dumps(view)
    assert sensitive not in serialized
    assert "SENSITIVE_BALANCE_VALUE" not in serialized
    assert sensitive_source not in serialized
    assert view["note"]
    assert all(row["value_included"] is False for row in view["required_terms"])
    assert "value" not in view["required_terms"][0]
    assert "/profile" not in serialized


def test_invalid_profile_blocks_workflow_and_fixture_profile_preserves_behavior(tmp_path):
    fixture = build_fixture_profile_payload()
    invalid_payload = deepcopy(fixture)
    invalid_payload["terms"]["market.provider_id"]["value"] = None
    invalid_path = tmp_path / "invalid_profile.json"
    invalid_path.write_text(json.dumps(invalid_payload), encoding="utf-8")
    database_path = tmp_path / "profile-workflow.db"

    blocked_platform = build_fixture_platform(
        database_path=str(database_path),
        provider_profile_path=str(invalid_path),
    )
    try:
        before_orders = len(blocked_platform.paper_execution.records())
        before_fills = len(blocked_platform.paper_execution.fills())
        result = blocked_platform.run_paper_workflow(workflow_id="profile-blocked")
        assert result.executed is False
        assert result.blocked is True
        assert result.reason == "provider_profile_not_activated"
        assert result.order is None and result.fill is None
        assert result.profile["passed"] is False
        assert result.persistence["profile_activation_allowed"] is False
        assert result.status["provider_profile"]["activation"]["status"] == "BLOCKED"
        assert len(blocked_platform.paper_execution.records()) == before_orders
        assert len(blocked_platform.paper_execution.fills()) == before_fills
        assert blocked_platform.verify()["overall"] is False
        assert blocked_platform.health().status == "UNSAFE"
        snapshots = blocked_platform.repository.list_snapshots(kind="profile")
        assert snapshots[-1]["passed"] is False
    finally:
        blocked_platform.close()

    valid_platform = build_fixture_platform(
        database_path=str(tmp_path / "valid-profile-workflow.db"),
        provider_profile_path=str(DEFAULT_FIXTURE_PROFILE_PATH),
    )
    try:
        result = valid_platform.run_paper_workflow(workflow_id="profile-valid")
        assert result.executed is True
        assert result.blocked is False
        assert result.reason == "local_paper_fill"
        assert result.profile["passed"] is True
        assert result.profile["activation_allowed"] is True
        assert result.conformance["passed"] is True
        assert result.persistence["profile_activation_allowed"] is True
        assert valid_platform.paper_execution.unresolved_orders() == ()
        assert valid_platform.health().status == "HEALTHY"
        assert valid_platform.verify()["checks"]["provider_profile_onboarding"] is True
    finally:
        valid_platform.close()


def test_operator_surfaces_expose_profile_status_without_values(platform, capsys):
    app = platform.create_wsgi_app()
    auth = "Bearer " + platform.runtime_settings.api_bearer_token

    profile_code, _, profile_body = call_app(app, "/profile", auth=auth)
    assert profile_code.startswith("200")
    profile_payload = json.loads(profile_body)
    assert profile_payload["passed"] is True
    assert profile_payload["activation_allowed"] is True
    assert profile_payload["profile"]["profile_id"] == "offline-fixture-v1"
    assert "SYNTHETIC_FEED" not in profile_body.decode("utf-8")
    assert "AAA/BBB" not in profile_body.decode("utf-8")
    assert platform.runtime_settings.api_bearer_token not in profile_body.decode("utf-8")

    status_code, _, status_body = call_app(app, "/status", auth=auth)
    assert status_code.startswith("200")
    assert json.loads(status_body)["provider_profile"]["passed"] is True

    desktop_code, _, desktop_body = call_app(app, "/desktop", auth=auth)
    assert desktop_code.startswith("200")
    desktop_payload = json.loads(desktop_body)
    assert desktop_payload["profile"]["passed"] is True
    assert desktop_payload["profile_detail"]["passed"] is True

    dashboard_code, _, dashboard_body = call_app(app, "/dashboard", auth=auth)
    assert dashboard_code.startswith("200")
    dashboard_html = dashboard_body.decode("utf-8")
    assert "Provider profile onboarding" in dashboard_html
    assert "PASS" in dashboard_html

    report_code, _, report_body = call_app(app, "/report", auth=auth)
    assert report_code.startswith("200")
    report_markdown = report_body.decode("utf-8")
    assert "Provider profile onboarding: `PASS`" in report_markdown
    assert "The profile view contains no term values" in report_markdown

    assert main(["profile"]) == 0
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["passed"] is True
    assert cli_payload["activation_allowed"] is True
    assert len(cli_payload["required_terms"]) == 37


def test_cli_profile_command_validates_template_and_unreadable_source(tmp_path, capsys):
    template_path = tmp_path / "template.json"
    template_path.write_text(
        json.dumps(build_template_profile_payload()), encoding="utf-8"
    )
    assert main(["profile", "--profile-path", str(template_path)]) == 0
    template_payload = json.loads(capsys.readouterr().out)
    assert template_payload["passed"] is False
    assert template_payload["activation_allowed"] is False
    assert len(template_payload["missing_fields"]) >= 37

    assert main(
        ["profile", "--profile-path", str(tmp_path / "missing.json")]
    ) == 0
    unreadable_payload = json.loads(capsys.readouterr().out)
    assert unreadable_payload["passed"] is False
    assert unreadable_payload["activation_allowed"] is False
    assert unreadable_payload["findings"][0]["code"] == "unreadable_profile"


def test_onboarding_helper_uses_explicit_evaluation_time():
    fixture = build_fixture_profile_payload()
    report = ProviderProfileOnboarding(
        fixture,
        evaluated_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    ).run()
    assert report.passed is False
    assert "profile.expires_at" in report.stale_fields
    assert report.activation_allowed is False
