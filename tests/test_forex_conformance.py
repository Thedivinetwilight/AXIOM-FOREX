"""Focused offline provider/broker conformance and persistence tests."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from app.common.types import OrderSide, TradingMode
from app.risk.models import OrderRequest
from axiom_forex import build_fixture_platform
from axiom_forex.cli import main
from axiom_forex.conformance import (
    REQUIRED_PROVIDER_VALUES,
    ConformanceContracts,
    ConformanceSuite,
    build_conformance_environment,
    run_conformance_checks,
)
from fixtures.m1 import PROVIDER_ID, PROVIDER_SYMBOL, QUANTITY


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


def _codes(report) -> set[str]:
    return {item.code for item in report.findings if not item.ok}


def test_default_conformance_suite_passes_and_documents_required_values():
    report = ConformanceSuite.for_platform().run()
    view = report.audit_view()

    assert report.passed is True
    assert report.status == "PASS"
    assert report.error_count == 0
    assert report.check_count >= 20
    assert view["scenarios"]["market"]["gap"]["classification"] == "MISSING"
    assert view["scenarios"]["market"]["gap"]["interpolation_used"] is False
    assert view["scenarios"]["market"]["stale_quote"]["reason"] == "STALE_QUOTE"
    assert view["scenarios"]["market"]["dst"]["offset_changed"] is True
    assert view["scenarios"]["broker"]["restart_recovery_equal"] is True
    assert view["scenarios"]["broker"]["all_external_orders_disabled"] is True
    required_rows = view["required_values"]
    assert {row["key"] for row in required_rows} == {
        item.key for item in REQUIRED_PROVIDER_VALUES
    }
    assert all(row["supplied"] for row in required_rows)
    assert all(row["description"] for row in required_rows)
    assert all(row["fixture_example"] for row in required_rows)


def test_fake_adapters_replay_spreads_gaps_calendar_and_order_failures():
    environment = build_conformance_environment()
    market = environment.scenario_view()["market"]
    broker = environment.scenario_view()["broker"]
    adapter = environment.market_adapter

    assert market["bid_ask"]["valid"] is True
    assert market["bid_ask"]["fresh"] is True
    assert market["stale_quote"]["ok"] is False
    assert market["gap"]["usable"] is False
    assert "MISSING_BAR" in market["gap"]["finding_codes"]
    statuses = {item["scenario"]: item["status"] for item in market["calendar_states"]}
    assert statuses["open"] == "OPEN"
    assert statuses["holiday"] == "HOLIDAY"
    assert statuses["daily_break"] == "DAILY_BREAK"
    assert statuses["weekend_gap"] == "WEEKEND_GAP"
    assert statuses["validity_before"] == "NOT_YET_VALID"
    assert statuses["validity_after"] == "CALENDAR_EXPIRED"
    assert statuses["instrument_unavailable"] == "INSTRUMENT_UNAVAILABLE"
    assert statuses["dst_transition"] != "OPEN"

    selected = adapter.historical_bars(
        environment.provider_symbol,
        "1m",
        environment.bars[1].timestamp,
        environment.bars[3].timestamp,
    )
    assert len(selected) == 2
    assert adapter.historical_bars("UNSUPPORTED", "1m", environment.bars[0].timestamp, environment.bars[-1].end_at) == ()

    assert broker["idempotency_duplicate_same_id"] is True
    assert broker["idempotency_transition_count_unchanged"] is True
    assert broker["idempotency_conflict_rejected"] is True
    assert broker["no_resubmit_while_unresolved"] is True
    assert broker["partial_duplicate_fill_noop"] is True
    assert broker["partial"]["status"] == "PARTIALLY_FILLED"
    assert broker["cancelled_partial"]["status"] == "CANCELLED"
    assert broker["unknown"]["status"] == "UNKNOWN"
    assert broker["unknown_reconciled"]["status"] == "FILLED"
    assert broker["reconcile_status_sequence"].index("UNKNOWN") < broker[
        "reconcile_status_sequence"
    ].index("FILLED")
    assert broker["restart_recovery_equal"] is True
    assert broker["restart_unresolved_equal"] is True
    assert broker["restart_no_resubmit_unresolved"] is True
    assert broker["unresolved_order_ids"]


def test_conformance_fails_closed_for_missing_inconsistent_or_external_terms():
    environment = build_conformance_environment()

    empty_provider = dict(environment.required_values)
    empty_provider["market.provider_id"] = ""
    missing_identity_env = replace(
        environment,
        contracts=ConformanceContracts(
            replace(environment.market_contract, provider_id=""),
            environment.broker_contract,
        ),
        required_values=empty_provider,
    )
    missing_report = run_conformance_checks(missing_identity_env)
    assert missing_report.passed is False
    assert {"contracts_valid", "required_values_supplied", "provider_identity_consistent"} <= _codes(
        missing_report
    )

    enabled_external_values = dict(environment.required_values)
    enabled_external_values["broker.external_orders_supported"] = True
    external_env = replace(
        environment,
        contracts=ConformanceContracts(
            environment.market_contract,
            replace(environment.broker_contract, external_orders_supported=True),
        ),
        required_values=enabled_external_values,
    )
    external_report = run_conformance_checks(external_env)
    assert external_report.passed is False
    assert {"contracts_valid", "broker_external_orders_disabled"} <= _codes(external_report)

    no_partials_env = replace(
        environment,
        contracts=ConformanceContracts(
            environment.market_contract,
            replace(environment.broker_contract, supports_partial_fills=False),
        ),
    )
    no_partials_report = run_conformance_checks(no_partials_env)
    assert no_partials_report.passed is False
    assert {
        "broker_capability_terms_consistent",
        "order_lifecycle_partial_reject_unknown_reconcile",
    } <= _codes(no_partials_report)

    unset_rate_values = dict(environment.required_values)
    unset_rate_values["broker.account_currency"] = ""
    unset_rate_env = replace(environment, required_values=unset_rate_values)
    unset_rate_report = run_conformance_checks(unset_rate_env)
    assert unset_rate_report.passed is False
    assert "required_values_supplied" in _codes(unset_rate_report)


def test_workflow_persists_conformance_and_exposes_it_through_operator_surfaces(tmp_path):
    database_path = tmp_path / "conformance-workflow.db"
    platform = build_fixture_platform(database_path=str(database_path))
    try:
        result = platform.run_paper_workflow(workflow_id="conformance-workflow")
        assert result.executed is True
        assert result.conformance["passed"] is True
        assert result.persistence["conformance_passed"] is True
        assert result.status["conformance"]["passed"] is True
        assert result.audit_view()["conformance"]["passed"] is True

        snapshots = platform.repository.list_snapshots(kind="conformance")
        assert snapshots
        assert snapshots[-1]["passed"] is True
        assert snapshots[-1]["suite_id"] == result.conformance["suite_id"]

        auth = "Bearer " + platform.runtime_settings.api_bearer_token
        app = platform.create_wsgi_app()
        status, _, body = call_app(app, "/conformance", auth=auth)
        assert status.startswith("200")
        assert json.loads(body)["passed"] is True
        status, _, _ = call_app(app, "/conformance")
        assert status.startswith("401")

        status_payload = platform.status()
        assert status_payload["conformance"]["passed"] is True
        assert platform.workflow_status()["conformance"]["passed"] is True
        desktop = platform.desktop_viewmodel()
        assert desktop["conformance"]["passed"] is True
        assert desktop["conformance_detail"]["passed"] is True
        dashboard = platform.dashboard_html()
        assert "Provider/broker conformance" in dashboard
        assert "PASS" in dashboard
        report = platform.report_markdown()
        assert "Provider/broker conformance: `PASS`" in report
        assert platform.verify()["checks"]["provider_broker_conformance"] is True
        assert any(
            check["name"] == "provider_broker_conformance"
            for check in platform.health().checks
        )
    finally:
        platform.close()


def test_workflow_blocks_all_orders_when_conformance_report_fails(tmp_path):
    database_path = tmp_path / "conformance-failure.db"
    platform = build_fixture_platform(database_path=str(database_path))
    try:
        baseline = deepcopy(platform.conformance_report())
        failed = deepcopy(baseline)
        failed["passed"] = False
        failed["status"] = "FAIL"
        failed["error_count"] = 1
        failed["findings"] = list(baseline["findings"]) + [
            {
                "code": "INJECTED_CONFORMANCE_FAILURE",
                "severity": "ERROR",
                "detail": "test-only failed contract",
                "ok": False,
            }
        ]
        before_orders = len(platform.paper_execution.records())
        before_fills = len(platform.paper_execution.fills())

        def failed_refresh():
            platform._conformance_report = deepcopy(failed)
            return deepcopy(failed)

        platform.refresh_conformance_report = failed_refresh
        result = platform.run_paper_workflow(workflow_id="conformance-blocked")

        assert result.executed is False
        assert result.blocked is True
        assert result.reason == "provider_broker_conformance_failed"
        assert result.order is None and result.fill is None
        assert result.conformance["passed"] is False
        assert result.persistence["conformance_passed"] is False
        assert len(platform.paper_execution.records()) == before_orders
        assert len(platform.paper_execution.fills()) == before_fills
        assert platform.health().status == "UNSAFE"
        assert platform.verify()["overall"] is False
        snapshots = platform.repository.list_snapshots(kind="conformance")
        assert snapshots[-1]["passed"] is False
    finally:
        platform.close()


def test_restart_recovers_conformance_and_preserves_unresolved_no_resubmit(tmp_path):
    database_path = tmp_path / "conformance-restart.db"
    first = build_fixture_platform(database_path=str(database_path))
    try:
        first_result = first.run_paper_workflow(workflow_id="conformance-before-restart")
        assert first_result.executed is True
        assert first_result.conformance["passed"] is True
        quote = first.provider.latest_quote(PROVIDER_SYMBOL)
        assert quote is not None
        at = quote.timestamp_utc() + timedelta(milliseconds=200)
        request = OrderRequest(
            request_id="conformance-unresolved",
            provider_id=PROVIDER_ID,
            provider_symbol=PROVIDER_SYMBOL,
            side=OrderSide.BUY,
            quantity=QUANTITY,
            target_mode=TradingMode.PAPER,
            evaluated_at=at,
            quote=quote,
            strategy_id="conformance-recovery-test",
        )
        simulated = first.paper_execution.submit(request, first._gate_context(at=at))
        assert simulated.state == "SIMULATED"
        first.repository.save_orders(first.paper_execution.order_records())
        # Order intents are durable from submission onward, so the database is
        # the record recovery reproduces -- including rows written by other
        # engines that share the same file.
        expected_orders = len(first.repository.list_orders())
        # Fills are durable from settlement onward, so the database -- not
        # this engine's memory -- is what recovery reproduces.
        expected_fills = len(first.repository.list_fills())
    finally:
        first.close()

    second = build_fixture_platform(database_path=str(database_path))
    try:
        assert second.conformance_report()["passed"] is True
        assert second.recovery_result is not None
        assert second.recovery_result.audit_view()["unresolved_order_count"] == 1
        blocked = second.run_paper_workflow(workflow_id="conformance-after-restart")
        assert blocked.executed is False
        assert blocked.blocked is True
        assert blocked.reason == "unresolved_order_present"
        assert blocked.conformance["passed"] is True
        assert blocked.persistence["conformance_passed"] is True
        assert len(second.paper_execution.records()) == expected_orders
        assert len(second.paper_execution.fills()) == expected_fills
        assert len(second.paper_execution.unresolved_orders()) == 1
        assert second.health().status == "UNSAFE"
        assert second.status()["conformance"]["passed"] is True
        assert second.repository.list_snapshots(kind="conformance")[-1]["passed"] is True
    finally:
        second.close()


def test_cli_conformance_command_is_offline_and_machine_readable(capsys):
    assert main(["conformance"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["status"] == "PASS"
    assert payload["required_values"]
    assert payload["scenarios"]["broker"]["all_external_orders_disabled"] is True
